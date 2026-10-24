[Kinoloom](../../README.md) › [Docs](../README.md) › Distributed fleet

# Distributed fleet: LAN workers and Colab workers

> **In short:** Kinoloom can send render work to other machines: worker PCs on your local network, or a Google Colab GPU joined over a private Tailscale network. This doc explains how a worker is set up, how it proves it belongs to the studio (one shared fleet token), how the studio renders through it, and what happens when a worker drops out. The main PC always keeps the projects, the queue and the outputs.

This doc covers how Kinoloom spreads render work over other machines: LAN worker provisioning, the shared-secret fleet token, the `RemoteWorkerClient` that lets the engines render on a worker, and Google Colab runtimes joined to the studio over a private Tailscale network.

**On this page**

- [1. Design idea: cattle and brain](#1-design-idea-cattle-and-brain)
  - [1.1 State and persistence rules](#11-state-and-persistence-rules)
- [2. LAN workers](#2-lan-workers-toolworkerspy-and-toolworkerworker_agentpy)
  - [2.1 Setting up a LAN worker](#21-setting-up-a-lan-worker-installinstall-workerbat)
  - [2.2 Registration, heartbeat and offline reaping](#22-registration-heartbeat-and-offline-reaping)
  - [2.3 Fleet token (shared secret)](#23-fleet-token-shared-secret)
  - [2.4 Remote client](#24-remote-client-remoteworkerclient)
- [3. Colab workers](#3-colab-workers-colab)
  - [3.1 Networking over Tailscale](#31-networking-over-tailscale)
  - [3.2 Secrets](#32-secrets)
  - [3.3 The T4 smoke notebook](#33-the-t4-smoke-notebook-colabkinoloom_smokeipynb)
  - [3.4 Model management and disk guards](#34-model-management-and-disk-guards)

---

## 1. Design idea: cattle and brain

Kinoloom treats render machines as disposable execution nodes ("cattle"). State, control and storage stay on the main studio PC ("the brain").

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       MAIN STUDIO WORKSTATION ("BRAIN")                     │
│                                                                             │
│   FastAPI Control Room (:8080)   Task Queue + Workers (tool/queue.db)       │
│   Shots/Projects (tool/studio.db)   GPU Lease Broker (local farm only)      │
│   Bible (bible/)                    Outputs (output/, shots/)               │
└───────────────────────┬─────────────────────────────┬───────────────────────┘
                        │                             │
       [Local Network / Robocopy]             [Tailscale Mesh (WireGuard)]
                        │                             │
                        ▼                             ▼
┌───────────────────────────────────┐  ┌──────────────────────────────────────┐
│       LAN WORKER PC (NODE 2)      │  │     EPHEMERAL GOOGLE COLAB (T4)      │
│                                   │  │                                      │
│  Worker Agent Daemon (:8120)      │  │  Worker Agent Daemon (:8120)         │
│  Worker ComfyUI Farm (:8188)      │  │  Pinned ComfyUI v0.36 (:8188)        │
│  Local Disk: C:\Kinoloom_worker   │  │  Private HF Weights / Ephemeral Disk │
└───────────────────────────────────┘  └──────────────────────────────────────┘
```

### 1.1 State and persistence rules

- **Stateless workers:** a worker holds no authoritative project state. Bible files (`bible/`), shot and project records (`tool/studio.db`), the task queue and the worker registry (`tool/queue.db`) stay on the main PC. The agent keeps its job records in memory only, so a restarted agent has no record of earlier jobs.
- **Worker loss:** when a worker stops sending heartbeats, queue items running on it are requeued or failed according to `workers.on_worker_loss`. Shots assigned to it are retried by the orchestrator, not moved elsewhere. Section 2.2 has the details, including the cases where you have to step in.
- **Separate serialization** (each machine runs its own work one item at a time): the GPU broker manages only the local ComfyUI instances. A port it does not know gets a no-op lease, that is, a lease that does nothing (`tool/gpu_broker.py`). So remote work never waits for the local card. The queue runs one item at a time per lane ("local" plus each online worker, `task_queue._lane_busy`), and an agent accepts one job at a time (HTTP 409 while busy). Movie-pipeline shots stay sequential: the orchestrator renders one shot after another, even when consecutive shots run on different machines.

---

## 2. LAN workers (`tool/workers.py` and `tool/worker/worker_agent.py`)

Other PCs on the local network can add render lanes next to the main PC.

### 2.1 Setting up a LAN worker (`install/INSTALL-WORKER.bat`)

Download the installer from the studio's **Workers** page and run it on the worker PC. The studio serves `install/INSTALL-WORKER.bat` with its own URL and the fleet token filled in (`tool/routers/fleet.py:render_installer`). Run straight from the share, `install\INSTALL-WORKER.bat` asks for the URL and token instead. You can answer every prompt in advance with an environment variable (`KINOLOOM_SRC`, `KINOLOOM_DST`, `KINOLOOM_WORKER_NAME`, `KINOLOOM_MAIN_URL`, `KINOLOOM_WORKER_TOKEN`, `KINOLOOM_PACKAGES`).

The installer does two things:

1. **Copy from the main PC's share** (`robocopy`, restartable) to `C:\Kinoloom_worker` by default:
   - the embedded Python (`tools/python`) and the shared venv (`tool/.venv`), byte-identical to the main PC;
   - the render-farm ComfyUI tree (`ComfyUI/`) with its custom nodes;
   - the model packages you choose, copied file by file from `install/model_packages.json` by `install/copy_packages.ps1`: Image Studio, post/upscale, both, a full mirror of `models/` + `external_loras/`, or none.
2. **Write the worker config** (`worker/worker_config.yaml`), the worker's `ComfyUI/extra_model_paths.yaml`, and a `WORKER.bat` launcher that starts the agent on port 8120. The agent starts its own ComfyUI on port 8188 and registers with the studio.

**What a LAN worker can render.** The installer sets up the render-farm ComfyUI only. That covers image jobs, legacy Wan 2.2 shots and the post/upscale lane. MiniMax H3 and LTX-2.5 graphs need the `ComfyUI_LTX` tree and its own venv, which the installer does not provision (the venv is bound to an interpreter path on the main PC). The studio checks a worker's ComfyUI node classes before it queues a graph there (`RemoteWorkerClient.queue_prompt`). So such a shot fails at once, with the missing classes named, instead of failing mid-render.

**Weights outside the tree.** Packages are resolved against the shared tree. Weights the main PC keeps elsewhere (`paths.extra_model_dirs`, for example `D:\H3_LAB\models`) are reported as missing, and you must copy them separately.

If the studio itself listens on loopback only (`ui.host: 127.0.0.1`), the served installer prints a warning: workers cannot reach such a studio.

### 2.2 Registration, heartbeat and offline reaping

- **Registration doubles as the heartbeat.** `worker_agent.registration_loop` sends `POST /api/workers/register` at start and again every `heartbeat_s` seconds (default 10; both installers write 10). After a failed call it logs `registration/heartbeat failed: ...` and retries after 5 s. The studio also exposes `POST /api/workers/{wid}/heartbeat` (`workers.heartbeat`), but the shipped agent does not call it.
- **Payload** (fields as sent by `registration_loop`; the values are only examples):
  ```json
  {
    "name": "worker-rig-2",
    "host": "192.168.1.120",
    "port": 8120,
    "token": "<workers.shared_token>",
    "gpu": [{"index": 0, "name": "NVIDIA GeForce RTX 4080", "mem_used": 556,
             "mem_total": 16376, "temp": 41, "util": 0}],
    "models": {"diffusion_models": ["RealVisXL_V5.0-Q5_K_M.gguf"],
               "upscale_models": ["RealESRGAN_x4.pth"]},
    "capabilities": {"roles": ["video", "image", "upscale"],
                     "required_missing": [], "blocked": false,
                     "disk": {"free_gb": 412.3, "total_gb": 931.5}},
    "agent_version": "1.2.0"
  }
  ```
  `host` is `public_base_url` from `worker_config.yaml` when that is set (a URL with a scheme, for example a Colab `tailscale serve` address). Otherwise it is the agent's LAN IP. `gpu` has one row per `nvidia-smi` GPU (MiB, °C, %). `models` lists file names per model folder. `capabilities.blocked` is true when `require_models` is on and a `required_models` file is missing. The agent then answers `POST /job` with 503.
- **Studio side** (`workers.register`): it checks the fleet token and validates the host. Link-local, multicast, unspecified and reserved addresses are refused; private, CGNAT/Tailscale and loopback addresses are allowed. It checks the port. Then it upserts (inserts or updates) the row keyed by a slug of the name (lower-case, runs of other characters become `-`, at most 32 characters), with status `online`, the telemetry and `last_heartbeat`. An empty `host` is replaced by the address the request came from.
- **Online / offline:** a worker counts as online while its last heartbeat is younger than `workers.offline_after_s` (default 30 s, that is, three missed 10 s beats; `workers.is_online`). The queue dispatcher thread (`task_queue._tick`, every `queue.dispatcher_interval_s`, default 2 s) calls `workers.reap_offline()` on every pass. That call flips a silent worker from `online` to `offline` and runs `_requeue_lane` for it.

**What happens to in-flight work when a worker drops**

- *Queue items* (`image`, `video`, `dataset`, `refbatch`) running on that worker's lane: `_requeue_lane` counts an attempt and applies `workers.on_worker_loss`:
  - `requeue_local` (the default) puts the item back to `queued` for the local lane;
  - `fail` fails it;
  - any other value requeues it for the same worker.

  Once the attempts reach the item's `max_attempts` (`queue.max_attempts`, default 2), it fails with `worker went offline mid-job`, whatever the policy. A requeued item keeps its priority and is ordered by priority and age like any other. It does not jump to the head of the queue. The engine thread that was waiting on the worker is not stopped. It ends when its next request to the agent fails.
- Often the engine notices first. `RemoteWorkerClient.wait_for` polls the agent every 5 s, and a refused connection fails the engine's job before the 30 s grace runs out. The dispatcher's normal retry path (`task_queue._fail_or_requeue`) then requeues the item for the *same* worker (one more attempt, same cap). It waits there until that worker is online again or you re-route it.
- *Shots* (movie pipeline) are not dispatched by the queue. The orchestrator renders each shot on the worker assigned to its project or shot (`task_queue.shot_worker`) and never reassigns it. A connection-refused error or a render timeout counts as transient: the shot is retried every 30 s without using its retry budget, at most 60 times, then sent to review. Other errors use the tier's `qa.max_retries` budget. To move the work, change the assignment.

### 2.3 Fleet token (shared secret)

- The fleet uses one shared secret, `settings.yaml` → `workers.shared_token`. `tool/settings.example.yaml` ships the placeholder `__WORKER_SHARED_TOKEN__`. At start, the control room replaces an empty or placeholder value with a random token (`security.ensure_secrets`, `secrets.token_urlsafe(32)`). **Workers page → New install token** (`POST /api/workers/token`) rotates it. Every installed worker then needs the new value: reinstall it, or edit `token` in its `worker_config.yaml`.
- It fails closed: if the token is empty, every worker call is refused (`workers._check_token`).
- **Worker → studio:** `register`, `heartbeat` and `result` are the only routes that skip the API-token gate (`security._WORKER_CALLBACKS`). Their handlers check the fleet token instead, with a constant-time comparison. `register` and `heartbeat` carry it in the JSON body (`token`), `result` in the `X-Worker-Token` header. Result uploads must declare `Content-Length` and stay under `workers.max_result_mb` (default 8192; larger uploads get 413).
- **Per-worker token hash:** `register` stores the SHA-256 hash of the presented token in `workers.token_hash`. `heartbeat` and `result` also require the presented token to match that hash. Every worker uses the same token, so this only ties later calls to the token a worker registered with. It is not a per-worker identity: anyone holding the fleet token can register a worker under any name, and registering an existing name overwrites its host and port. Treat the token like a password.
- **Studio → worker:** `RemoteWorkerClient` sends the fleet token as `X-Worker-Token` on every call. The agent requires it on every route except `/ping` and compares it with the `token` in its `worker_config.yaml`. With no token configured, the agent answers loopback callers only. The studio refuses redirects from an agent (`_NoRedirect`). It also resolves the worker's host again before each call and refuses link-local, multicast, unspecified and reserved addresses (`_assert_dialable`).
- On a LAN the traffic is plain HTTP (`http://<host>:8120`, and the studio URL written into the installer). So anyone who can capture that traffic can read the token. Colab traffic rides the tailnet (section 3.1).

### 2.4 Remote client (`RemoteWorkerClient`)

`workers.client_for(worker)` is the single routing point:

- `local` or empty returns the ordinary `renderer.ComfyClient`;
- `any` picks the first online worker;
- any other value must be a registered worker id.

`tool/workers.py:RemoteWorkerClient` implements the subset of `ComfyClient` that the engines call. So the orchestrator (shots), Video Studio, Image Studio, reference/keyframe images (`shot_images.py`), dataset jobs (`jobs.py`) and the post/upscale lane (`post.py`) render on a worker by swapping the client. It does not implement `ComfyClient.post`, `get`, `queue_position` or `load_models`.

```python
class RemoteWorkerClient:
    def __init__(self, wid: str, timeout: int = 60) -> None: ...

    # ComfyClient subset used by the engines
    def queue_prompt(self, workflow: dict, client_id: str | None = None) -> str: ...
    def wait_for(self, prompt_id: str, timeout_s: float = 3600, cb=None,
                 cancel: threading.Event | None = None) -> dict | None: ...
    def history(self, prompt_id: str) -> dict: ...
    def upload_image(self, local_path: Path) -> str: ...
    def download(self, subfolder: str, filename: str, save_to: str | Path) -> Path: ...
    def interrupt(self) -> None: ...
    def system_stats(self) -> dict: ...          # agent GET /gpu
    def object_info(self) -> dict: ...
    def queue_info(self) -> dict: ...

    # worker-only helpers
    def upload_file(self, local_path: Path, name: str | None = None) -> str: ...
    def upload_stream(self, path: str, src: Path, timeout: int = 7200) -> Any: ...
    def node_classes(self) -> frozenset[str] | None: ...
    def missing_nodes(self, workflow: dict) -> list[str]: ...
    def install_url(self, spec: dict, timeout: int = 14400) -> dict: ...
    def install_group(self, install_id: str, files: list[dict]) -> dict: ...
    def install_group_status(self, install_id: str) -> dict: ...
    def delete_model(self, path: str) -> dict: ...
    def disk(self) -> dict: ...
```

- `queue_prompt` first compares the graph's node classes with the worker's `/object_info` (cached for 10 minutes) and raises an error if any are missing. It then posts the workflow to the agent's `/job` with a new 12-character job id and a deadline of 3600 s × `workers.remote_timeout_scale`, and returns the agent's id for the job.
- `wait_for` polls `GET /job/{id}` every 5 s until the ComfyUI history entry appears. It raises an error if the agent reports the job failed, and returns `None` on timeout (`timeout_s` × `remote_timeout_scale`) or when `cancel` is set.
- `upload_image` posts the file into the worker ComfyUI's `input/` folder under the same content-derived name the local client uses (`renderer.upload_name`: `kf_<16 hex of its SHA-256>.png`). `interrupt` calls the agent's `/cancel/{job}`. The worker's ComfyUI is dedicated to the agent, so its interrupt only hits this job.
- **Where results land:** when a render finishes, the agent pushes each output file to `POST /api/workers/{wid}/result?job=<job>&name=<file>` before it publishes the history. So `wait_for` never returns before the files are stored. `workers.store_result` writes the file to `ROOT/temp/worker_results/{job_id}/{filename}` (a temp file renamed into place; job id and file name must be bare names). `download()` finds it there (`pop_result`, which also matches flattened ComfyUI subfolder names) and copies it to the path the engine would have written locally. The copies in `temp/worker_results/` are not deleted afterwards.

---

## 3. Colab workers (`colab/`)

A Google Colab GPU runtime can join the fleet as a worker. The notebooks and the bootstrap assume a T4 (16 GB). The studio never sends work to Colab on its own: once registered, it is an ordinary worker, and you assign work to it (or to `any`) as you would to a LAN worker. The step-by-step runbook is `colab/README.md`.

```
┌─────────────────────────────────┐                 ┌─────────────────────────────────┐
│     MAIN CONTROL ROOM (:8080)   │                 │       GOOGLE COLAB (TESLA T4)   │
│                                 │                 │                                 │
│  Tailnet IP: 100.x.y.z:8080     │◄──Encrypted────►│  Tailscale (userspace) node     │
│  Task Queue Dispatcher          │   WireGuard     │  Agent :8120 + ComfyUI :8188    │
│  Fleet token (shared secret)    │   Tunnel        │  Weights pulled from HF repo    │
└─────────────────────────────────┘                 └─────────────────────────────────┘
```

### 3.1 Networking over Tailscale

Colab runtimes have no reachable public address, so the studio and the Colab worker talk over a Tailscale tailnet (a private network between your own devices):

- The main PC runs Tailscale. `MAIN_URL` is its tailnet address (for example `http://100.x.y.z:8080`). The studio must listen on a non-loopback address (`ui.host`, for example `0.0.0.0`) to be reachable at all.
- `colab/bootstrap.sh` starts `tailscaled` in userspace-networking mode (Colab containers cannot create a TUN device), with a SOCKS5 proxy on `localhost:1055` and an HTTP proxy on `localhost:1056`. It then runs `tailscale up --auth-key=$TAILSCALE_AUTHKEY --hostname=$WORKER_NAME --accept-dns=false --ephemeral`. The agent reaches the PC through the HTTP proxy (`HTTP_PROXY` in `colab/colab_env.sh`, sourced before the agent starts).
- Studio → agent: the bootstrap runs `tailscale serve --bg 8120`, and the agent advertises `https://<node>.<tailnet>.ts.net`. If `serve` fails, it advertises `http://<tailnet-ip>:8120`, which only works if the userspace netstack forwards that port. The Workers page **Test** button shows whether the studio can reach it.
- Without `TAILSCALE_AUTHKEY` the bootstrap only warns. The agent then advertises the runtime's own address, which the PC cannot reach.
- Traffic between the PC and the worker is WireGuard-encrypted by Tailscale and needs no open router port.

### 3.2 Secrets

The notebooks read these from Colab Secrets (`google.colab.userdata`), and fall back to environment variables:

| Secret key | Used by | Purpose |
|---|---|---|
| `GITHUB_PAT` | optional; the notebooks' clone cell and `bootstrap.sh` use it only when it is set and no clone exists | Clones a private fork. The clone cells reset `origin` to the URL without the PAT afterwards. A read-only, repository-scoped fine-grained token is enough. |
| `HF_TOKEN` | `colab/fetch_models.py` (bootstrap step 5) and the agent (`hf_token`, for Workers-page installs) | Downloads from the private Hugging Face repository. It is sent only to `huggingface.co` hosts. |
| `TAILSCALE_AUTHKEY` | `bootstrap.sh` | Joins the tailnet (the script adds `--ephemeral` itself). Required by the deploy notebook, optional for the smoke notebook. |
| `WORKER_TOKEN` | `bootstrap.sh` → `worker_config.yaml` `token` | The studio's `workers.shared_token`. Required in worker mode. |
| `MAIN_URL` | `bootstrap.sh` → `worker_config.yaml` `main_url` | The studio over the tailnet (`http://100.x.y.z:8080`). `localhost`/`127.*` is refused. With `TAILSCALE_AUTHKEY` set, the bootstrap probes `MAIN_URL/api/health` through the Tailscale HTTP proxy before going on, and a failed probe stops it. Required in worker mode. |

The bootstrap writes `HF_TOKEN` and `WORKER_TOKEN` in plain text into `colab/colab_env.sh` and `tool/worker/worker_config.yaml` on the runtime's disk. Git ignores both files, and that disk is discarded with the runtime.

### 3.3 The T4 smoke notebook (`colab/Kinoloom_Smoke.ipynb`)

It answers one question. The MiniMax H3 W4A8 (ConvRot int8) stack has so far only run on an RTX 3070 (sm_86). Does it load and render on a T4 (sm_75)?

1. It prints torch/CUDA versions, device, compute capability, bf16 support, VRAM and free disk under `/content`. It requires `HF_TOKEN`; `TAILSCALE_AUTHKEY` and `GITHUB_PAT` are optional.
2. It clones the repository and runs `bootstrap.sh` with `SMOKE=1`: ComfyUI, custom nodes, venv and the `MODEL_GROUPS` weights (default `h3_shared,h3_dit_singularity`), but no worker agent.
3. It starts ComfyUI on port 8188 (log `colab/comfyui.log`) and checks ten node classes in `/object_info`: `UNETLoader`, `CLIPLoader`, `VAELoader`, `MiniMaxH3ReferenceToVideo`, `ModelAttentionBackend`, `MiniMaxH3SigmaShift`, `ManualSigmas`, `SamplerCustomAdvanced`, `CreateVideo`, `SaveVideo`.
4. It renders `tool/workflows/h3_t2v.json` once with the Singularity v1.3 W4A8 DiT at 960×544, 8 steps, 22 frames, seed 42, with a 45-minute limit, and samples `nvidia-smi` memory every 2 s.
5. (Optional) It prints the tailnet IP and the `tailscale serve` host for a manual reachability check.

The verdict is `PASS` when ComfyUI reports `status_str == "success"` with no `execution_error`, otherwise `FAIL`. It is written to `/content/kinoloom/colab/smoke_result.json`:

| Field | Meaning |
|---|---|
| `gpu` | the fixed string `"T4 sm_75"` (not detected) |
| `verdict` | `PASS` or `FAIL` |
| `seconds` | wall time from submit to history, model loading included |
| `frames`, `steps` | 22 and 8 |
| `vram_peak_gb` | highest `memory.used` seen by `nvidia-smi` (whole GPU) |
| `s_per_frame` | `seconds` / 22 |
| `errors` | `execution_error` messages (first 200 characters each) |
| `outputs` | the first three output entries |

**What it does not do.** Nothing compiles CUDA kernels. Neither the bootstrap nor the notebook builds anything. They pip-install ComfyUI's requirements, `gguf`, `pyyaml`, `comfy-kitchen` (if PyPI has it) and the custom nodes' requirements into a venv that inherits Colab's torch, and the render uses whatever kernels those packages ship. There is no numerical comparison with the 3070. Nothing in the studio reads `smoke_result.json`, and the verdict does not limit what a worker may run. It is a record for you, and it stays on the Colab disk unless you copy it out.

### 3.4 Model management and disk guards

- **From the Workers page** (per worker):
  - **Install group** (`POST /api/workers/{wid}/install_group`): a background install on the worker, with progress via `GET /api/workers/{wid}/install_group/{gid}`;
  - **Delete model** (`DELETE /api/workers/{wid}/models?path=...`);
  - disk usage (`GET /api/workers/{wid}/disk`);
  - single files via `POST /api/workers/{wid}/install_url` (`{path, url | hf_repo + hf_file, hf_revision?, bytes?, sha256?}`).

  These pulls run on the worker's own connection. They allow https only and public addresses only (unless `install_allow_http` / `install_allow_private_hosts` are set in `worker_config.yaml`), checked again on every redirect. They are streamed to `<file>.part` with HTTP Range resume, then renamed.
- **LAN push:** `POST /api/workers/{wid}/install` sends files from the main PC instead. The studio hashes each file, and the agent checks its size and SHA-256 before renaming it into place.
- **SHA-256:** a pulled file is hash-checked only when its manifest entry has a `sha256`. `install/colab_model_groups.json` ships with every `sha256` empty and most `bytes` at 0. `tool/scripts/publish_models_hf.py` fills both into the PC's copy while it uploads. The Colab side reads the manifest from its own clone, so the values reach it only after the updated manifest is committed and pushed. Until then, downloads are not hash-checked (the agent logs `verified=False`). Where `bytes` is 0, the size is not checked either: `fetch_models.py` treats any existing file as present, while a Workers-page install downloads it again (it skips a file only on a size match).
- **Disk budget (`colab/fetch_models.py`):** it adds up the manifest `bytes` of the selected files. It refuses (exit 2, before downloading) when the sum exceeds `--budget-gb` or the manifest's `disk_budget_gb` (60), or when free disk is below the sum plus 3 GiB. Files with `bytes` 0 count as zero, so with today's manifest the default H3 groups pass the guard unmeasured. It also refuses while `hf_repo` still contains the `OWNER` placeholder. Workers-page installs do not apply the 60 GB budget. The agent refuses a file (507) only when its declared size plus 2 GiB exceeds the free disk.
