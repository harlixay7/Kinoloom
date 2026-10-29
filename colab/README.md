[Kinoloom](../README.md) › [Docs](../docs/README.md) › Colab worker

# Colab worker: operations runbook

> **In short:** This runbook shows how to use a free Google Colab GPU as an extra render worker for the studio. You set up the PC once, run a feasibility notebook first, and then start the deploy notebook whenever you want the extra GPU. The studio on your PC stays in control, and the Colab machine is disposable.

> **Licence:** the MiniMax H3 Community License does not cover use in the EU, the UK, South Korea or the USA, and Google decides where a Colab runtime runs, which can be the USA. Read the license before you put H3 groups on a Colab worker ([THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) section 3).

A Google Colab T4 (16 GB, free tier) runs your studio's GPU work as a **remote render worker**. Once the notebook is running, you control it from the PC control room you already use. The PC keeps the brain (UI, queue, projects, registries, local 3070 farm). Colab is a disposable GPU lane. When the runtime dies, the job running there is lost and retried as described in section 3. Nothing else is lost.

```
PC (brain)                                     Colab T4 (cattle)
 control room :8080  ── jobs/upload/pull ──►  worker_agent :8120
 queue · workflows · registries                └ ComfyUI v0.36 :8188 (pinned)
 local farm :8188/8189/8190/8199               └ models/ ← private HF repo
              ▲  registration · heartbeat · artifacts (pushed back)  │
              └──────────────── Tailscale mesh (private) ◄────────────┘
```

Files:

- `colab/Kinoloom_Colab.ipynb`: the deploy notebook
- `colab/Kinoloom_Smoke.ipynb`: the feasibility check
- `colab/bootstrap.sh`: does the actual work
- `colab/fetch_models.py`: model pulls
- `install/colab_model_groups.json`: the model manifest
- `tool/scripts/publish_models_hf.py`: the publisher, which runs on the PC

Architecture details: `docs/setup/DISTRIBUTED_FLEET.md` section 3.

**On this page**

- [1. One-time PC setup](#1-one-time-pc-setup)
- [2. First run: the feasibility check (do not skip)](#2-first-run-the-feasibility-check-do-not-skip)
- [3. Daily flow](#3-daily-flow)
- [4. Honest limits](#4-honest-limits)
- [5. Security](#5-security)
- [6. Troubleshooting](#6-troubleshooting)
- [7. What runs where (change map)](#7-what-runs-where-change-map)

---

## 1. One-time PC setup

1. **Tailscale.** Install it on the PC and log in. Note the PC's IP: `tailscale ip -4` → for example `100.x.y.z`. In the admin console, create a **reusable auth key** (Settings → Keys). The bootstrap joins with `--ephemeral`.
2. **Make the studio reachable.** Set `ui.host: 0.0.0.0` in `tool/settings.yaml` and restart the control room. On the first start with a non-loopback host, it generates `security.api_token`, which remote browsers must enter. Worker callbacks use the fleet token instead.
3. **Fleet token.** The control room generates `workers.shared_token` on first start. It sits in plain text in `settings.yaml` and in every worker's `worker_config.yaml`. To rotate it before a cloud box joins, use the Worker PCs page (System menu) → *New token*. Every existing worker then needs the new value (reinstall, or edit `token` in its `worker_config.yaml`).
4. **Publish the weights** (once; re-run to resume). Run this from the studio root, because the manifest's `source` paths are relative:
   ```
   "tool\.venv\Scripts\python.exe" -m pip install huggingface_hub hf_transfer
   set HF_TOKEN=hf_...            (a WRITE token, fine-grained)
   "tool\.venv\Scripts\python.exe" tool\scripts\publish_models_hf.py ^
       --repo datasets/<your-hf-user>/kinoloom-models --groups all
   ```
   The publisher creates the repo as private. As it goes, it fills exact sizes + sha256 into `install/colab_model_groups.json`, and it remembers the repo. Files already in the repo are skipped by name. Then **commit and push the updated manifest**. Colab reads `hf_repo`, sizes and hashes from its own clone, and `fetch_models.py` refuses to run while `hf_repo` still says `OWNER`. The manifest's `_readme` lists rough per-group sizes.
5. **Point the notebooks at your fork.** The worker reads the published manifest from its own clone, so it needs a repository you can push that manifest to: your fork of Kinoloom. Both notebooks clone `harlixay7/Kinoloom.git` by default. Change `REPO_SLUG` in cell 2 of `Kinoloom_Colab.ipynb` and `REPO` in cell 2 of `Kinoloom_Smoke.ipynb` to your fork. For a private fork, also add the `GITHUB_PAT` secret.
6. **Colab secrets.** Open `colab/Kinoloom_Colab.ipynb` in Colab (File → Upload notebook, or keep it in Drive). Click the key icon and add each secret below, each with *notebook access*:

   | Secret | Value |
   |---|---|
   | `HF_TOKEN` | read token for `<you>/kinoloom-models` |
   | `TAILSCALE_AUTHKEY` | the auth key from step 1 |
   | `WORKER_TOKEN` | the fleet token (`workers.shared_token`) from step 3 |
   | `MAIN_URL` | `http://100.x.y.z:8080` (your PC's tailnet IP) |
   | `GITHUB_PAT` | optional: a read-only fine-grained PAT, only for a private fork |

   The deploy notebook stops if any of the first four is missing. The smoke notebook needs only `HF_TOKEN`.

## 2. First run: the feasibility check (do not skip)

Run `colab/Kinoloom_Smoke.ipynb` (Run all). On the real T4, it checks whether the **H3 W4A8 int8 ConvRot stack loads and renders on sm_75**. So far that stack has only run on an sm_86 RTX 3070. The notebook:

- builds ComfyUI and fetches the default groups via `bootstrap.sh` in smoke mode (no agent);
- checks the H3 node classes;
- renders `tool/workflows/h3_t2v.json` once at 960×544 (8 steps, 22 frames);
- records the VRAM peak and seconds per frame;
- writes a PASS/FAIL verdict to `/content/kinoloom/colab/smoke_result.json` (fields: `gpu`, `verdict`, `seconds`, `frames`, `steps`, `vram_peak_gb`, `s_per_frame`, `errors`, `outputs`).

PASS means ComfyUI reported success with no execution error. Nothing in the studio reads the file, and nothing is compiled. It is your record, and it stays on the Colab disk unless you copy it out.

- **PASS** → the deploy notebook's default `MODEL_GROUPS = h3_shared,h3_dit_singularity` is what the check rendered with (the manifest estimates ~20 GB for it).
- **FAIL** (for example, the W4A8 node crashes) → H3 does not run on this GPU, so keep H3 work on the PC. Also note that the bootstrap installs no GGUF loader (ComfyUI-GGUF) and no Qwen-Image 2.1 nodes. So the stills groups' workflows (`qwen21_t2i`, `z_image_t2i`) do not run on this worker either. The studio refuses such graphs before queueing them (missing node classes).

## 3. Daily flow

1. Open the Colab notebook and choose **Run all**. It runs the secrets, the repo clone, then `bootstrap.sh` (see below), then a status cell. How long it takes depends mostly on the model download.
2. On the PC, open the **Worker PCs** page (System menu). `kinoloom-colab` shows **online** with GPU, disk and model inventory. `Test` pings the agent and runs its selftest (ComfyUI up, the H3 node classes the bootstrap configured, disk).
3. Assign work: in Video Studio or the Render queue (System menu), pick worker `kinoloom-colab`, or `any` (the dispatcher then uses whichever lane is idle first, the local one included). Local 3070 lanes keep rendering in parallel, because the GPU broker does not serialize remote workers. Movie-pipeline shots stay sequential, even when they hop machines.
4. Artifacts are pushed back automatically to `temp/worker_results/{job}/` and copied to the same output paths local renders use.

### What `bootstrap.sh` does

You can re-run it. It uses `set -euo pipefail`, so a failing step stops the script, unless the step is marked "ignored" below (`|| true` or similar in the script).

| Step | What happens | Failures |
|---|---|---|
| 0 sanity | GPU visible; `HF_TOKEN` set; in worker mode `WORKER_TOKEN`, `MAIN_URL` set and `MAIN_URL` not localhost/127.* | stop |
| 1 repo | existing clone: `git fetch`, then `git reset --hard origin/HEAD` (or `origin/main`), which discards local edits; otherwise clone `REPO_URL` (with `GITHUB_PAT` when it is set, which is then stripped from the remote) | fetch ignored. For a private fork the PAT-less remote usually cannot fetch, so a re-run in the same runtime keeps the cloned commit |
| 2 tailscale | writes `colab/colab_env.sh`; with `TAILSCALE_AUTHKEY`: installs Tailscale if needed, `tailscaled` in userspace mode, `tailscale up --ephemeral`, then `tailscale serve --bg 8120` (advertise `https://<node>.ts.net`) or the raw `http://<tailnet-ip>:8120`; worker mode probes `MAIN_URL/api/health` through Tailscale's HTTP proxy (`127.0.0.1:1056`, the route the agent uses) | `tailscale up` (log `colab/tailscale_up.log`) and an unreachable `MAIN_URL` stop; `serve` falls back; no authkey only warns |
| 3 ComfyUI | clone if missing, checkout `ee71d5c` (v0.36.0); custom nodes `Comfyui_Minimax_h3_latent_Upscaler` @ `40316cf` and `ComfyUI-LTX2.5-MSR` @ `9894117`; copies the repo's tracked patches (`MMH3_Split_Upscale.py`, `ComfyUI-LTX25-Studio`, `nodes_lt_audio.py`); writes `extra_model_paths.yaml` | fetch, custom-node checkouts and patch copies ignored; the ComfyUI checkout stops |
| 4 venv | `.colab-venv` with `--system-site-packages` (inherits Colab's torch); `pip install -r ComfyUI/requirements.txt`; pip upgrade, `gguf pyyaml`, `comfy-kitchen`, every custom node's `requirements.txt` | ComfyUI requirements stop; the rest ignored |
| 5 models | `fetch_models.py --groups $MODEL_GROUPS` | non-zero exit stops (unpublished manifest, budget/disk refusal, failed file) |
| 6 config | `tool/worker/worker_config.yaml`: name, `main_url`, `token`, `hf_token`, `public_base_url`, `require_models` with the selected groups' files, `selftest_nodes` | stop |
| 7 agent | worker mode only: restart `worker_agent.py` (log `colab/agent.out`), wait up to 30 s for `/ping` | no `/ping` stops; `SMOKE=1` exits after step 6 |

In smoke mode (`SMOKE=1`), `MAIN_URL` and `WORKER_TOKEN` may be unset. The script treats them as empty.

### Model management from the PC

- **Add a group**: Worker PCs page → *Add a group* → tick groups. The worker pulls them from the HF repo on its own connection, resuming `.part` files. Files are sha256-checked only when the manifest has a hash, and skipped only when the manifest size matches (see section 4).
- **Delete**: Worker PCs page → the trash button (*Delete a model file*) removes one file on the worker (the PC copy and the HF repo are untouched).
- **Arbitrary file**: `POST /api/workers/<wid>/install_url` `{path, url|hf_repo+hf_file, hf_revision?, bytes?, sha256?}`. It is streamed to disk with resume, never buffered in RAM.
- Disk guards: `fetch_models.py` (bootstrap) refuses a selection whose manifest sizes exceed `disk_budget_gb` (60) or the free disk minus 3 GiB. Installs from the Worker PCs page skip the budget. The agent refuses a file only when its declared size plus 2 GiB exceeds the free disk.

### After a session death

Colab ends runtimes on its own schedule (free-tier sessions last at most ~12 h).

- **On Colab:** start a fresh runtime and choose **Run all**. The disk is new, so the models are fetched again.
- **On the PC:** heartbeats stop. After `workers.offline_after_s` (30 s) the dispatcher marks the worker offline. Queue items that were running there follow `workers.on_worker_loss` (default `requeue_local`: back to the local lane, counting an attempt; failed once `queue.max_attempts`, default 2, is reached).
- **Shots** assigned to the worker are **not** moved. The orchestrator keeps retrying them against it (every 30 s, up to 60 times, then review). Reassign them if the worker will not come back soon. Re-running with the same `WORKER_NAME` registers under the same id, so those assignments work again once it is online.

## 4. Honest limits

- **Free-tier Colab** cannot be guaranteed by software: GPU availability varies, sessions are time-capped, GPU quota is limited, and the disk is wiped every session. Colab **Pro** (background execution, longer runtimes) reduces the friction if this lane becomes load-bearing.
- **T4 speed**: no bf16, and older tensor cores. Expect it to be slower than the 3070. Use the smoke check's `s_per_frame` for your own numbers. `workers.remote_timeout_scale` (3.0 in `tool/settings.example.yaml`) stretches both the agent's job deadline (3600 s × scale) and the studio's wait. Raise it if you see timeout interrupts.
- **Terms of service**: this design stays app-level (your own AI pipeline driven by your own UI, the standard ComfyUI-on-Colab pattern). Do not add SSH/remote-desktop/file-hosting to the Colab box, and do not script anti-idle keep-alives. Those are explicitly disallowed and risk the account.
- **Disk**: check the smoke notebook's disk line. The manifest budget is 60 GB. By the manifest's own estimates (`h3_shared` + one H3 DiT ~20 GB, each extra DiT ~12 GB, `ltx_core` ~27 GB), `h3_shared` + all three H3 DiTs + `ltx_core` exceed it, so pick DiT groups per session. The fetcher can only refuse an over-budget selection once the manifest has real sizes (published and pushed). Today most `bytes` are 0 and count as nothing.
- **Hashes**: every `sha256` in the shipped manifest is empty, so downloads are not hash-checked until you publish and push the filled-in manifest.

## 5. Security

- Studio ↔ worker traffic rides the **Tailscale mesh** (WireGuard-encrypted, private). The agent is reached via `tailscale serve` or the tailnet IP, and no router port is opened. Model downloads go to Hugging Face over https.
- Every agent route except `/ping` requires the `X-Worker-Token` header (401 otherwise). With no token in `worker_config.yaml`, the agent answers loopback callers only.
- `/install_url` refuses non-https URLs and hosts that resolve to non-public addresses (loopback, private, link-local, CGNAT/tailnet), unless `install_allow_http` / `install_allow_private_hosts` are set in the worker config. Every redirect hop is checked again, and the HF token is sent only to `huggingface.co` hosts.
- `GITHUB_PAT`, when set, is used for the clone and removed from `.git/config`. The secrets live in Colab's secret store, not in the repo. But the bootstrap writes `HF_TOKEN` and `WORKER_TOKEN` in plain text into `colab/colab_env.sh` and `tool/worker/worker_config.yaml` on the runtime's disk. Git ignores both files.
- The publisher creates the HF repo as **private**. Hash checks need the published manifest (see section 4).

## 6. Troubleshooting

| Symptom | Where to look | Fix |
|---|---|---|
| Worker never appears online | `colab/agent.out` | `registration/heartbeat failed` → check that `MAIN_URL` is the PC's **tailnet IP**, Tailscale is up on the PC, `ui.host` is not loopback, and `WORKER_TOKEN` matches `workers.shared_token` (401 = wrong token) |
| Online but `Test` fails | Worker PCs page | PC→Colab inbound is blocked → check `tailscale serve status` in Colab; the bootstrap falls back to the raw tailnet IP. The advertised address is in the bootstrap output (`tailscale up: ...`) and the agent's start line (`advertise=...`) |
| ComfyUI down / job fails "local ComfyUI is not reachable" | `tool/worker/comfyui.log` (worker mode; the smoke notebook logs to `colab/comfyui.log`) | import error of a custom node → re-run the bootstrap (it checks out the pinned commits again, but ignores checkout errors, so confirm with `git -C ComfyUI/custom_nodes/<pack> rev-parse --short HEAD`); check the `PINNED_VERSIONS.md` pins |
| `HTTP 401` during model pull | bootstrap output (`fetch_models.py`), or `colab/agent.out` for installs from the Worker PCs page | `HF_TOKEN` lacks read access on the repo, or the models are not published yet |
| `manifest hf_repo not published yet` | bootstrap output | publish the weights and push the updated manifest (section 1, step 4) |
| `REFUSING: not enough disk` / `REFUSING: selection exceeds the 60GB disk budget` | bootstrap output | drop a DiT group for this session (`MODEL_GROUPS` in cell 1) |
| Render killed by timeout | control-room log | raise `workers.remote_timeout_scale` in `tool/settings.yaml` |
| 503 "worker blocked — missing required models" | job error | group install incomplete → run *Add a group* again (it resumes; `.part` files continue) |
| W4A8 node crashes at load | `tool/worker/comfyui.log` | the stack does not run on this GPU/torch combination (see section 2, FAIL); keep H3 work on the PC |

## 7. What runs where (change map)

| Piece | Runs on | Notes |
|---|---|---|
| Control room + queue + registries | PC | worker routes in `tool/routers/fleet.py` |
| `gpu_broker` lease choreography | PC | local fleet only; remote ports = no-op leases by design |
| `worker_agent.py` (`AGENT_VERSION` 1.2.0) | Colab | jobs, uploads, pull-installs, group installs, delete, disk, token auth |
| ComfyUI v0.36 + H3 packs | Colab | rebuilt by `bootstrap.sh` at the `PINNED_VERSIONS.md` pins (custom-node checkouts not enforced, see step 3) |
| Weights | HF private repo | published from PC; pulled per group by Colab |
| `progress_relay.html` live iframe | PC only | the browser loads it from `127.0.0.1`, so it cannot reach a worker; REST progress + logs unaffected |
