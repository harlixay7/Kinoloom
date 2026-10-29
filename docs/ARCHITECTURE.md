[Kinoloom](../README.md) › [Docs](README.md) › Architecture

# Kinoloom architecture and process supervision

> **In short:** This doc explains how Kinoloom runs on one Windows PC with one 8 GB graphics card. A launcher starts the studio's five components and is built to shut them all down together, so no leftover process keeps the card's memory or a port. A broker gives the card to one model stack at a time across four ComfyUI servers, and a director walks each project from story to finished film. It is for anyone who wants to run, debug or change the studio's code.

This doc explains how the studio runs, as the code in `tool/` does it today: how the operating system supervises its processes, how GPU work is serialized, the local ComfyUI fleet, the project director, the database schemas, the LoRA training subsystem, and the control room's request path, worker routing and render guards.

**On this page**

- [1. Process supervision with Win32 Job Objects](#1-process-supervision-with-win32-job-objects-toollauncherpy)
- [2. The GPU lease broker](#2-the-gpu-lease-broker-toolgpu_brokerpy)
- [3. The ComfyUI fleet: one port per model family](#3-the-comfyui-fleet-one-port-per-model-family)
- [4. Project director](#4-project-director-tooldirectorpy)
- [5. Database schemas](#5-database-schemas)
- [6. LoRA training](#6-lora-training-tooltrainingpy-tooldatasetpy-tooltraintrain_lorapy)
- [7. Control room internals](#7-control-room-internals-toolapipy-toolrouters-toolsecuritypy)

---

## 1. Process supervision with Win32 Job Objects (`tool/launcher.py`)

On Windows, running several Python runtimes with native CUDA libraries makes process cleanup fragile. Closing the terminal, `Ctrl+C` or an unhandled exception can leave child processes running. An orphaned ComfyUI keeps its CUDA context, and with it its VRAM (the graphics card's own memory), and its port. The next launch then fails on a port collision or on a card that is already occupied.

`tool/launcher.py` (started by `start.bat`) tears the process tree down through four layers that back each other up.

### 1.1 The Job Object (`JobKiller`)
The launcher creates one Windows Job Object (a kernel object that groups processes so Windows can manage them as one unit). It is configured with the `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` flag (`0x2000`). The kernel32 prototypes are declared explicitly (`HANDLE` return types), so 64-bit handles are not truncated.

```python
class LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]

class EXT(ctypes.Structure):
    _fields_ = [
        ("Basic", LIMIT),
        ("Io", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

info = EXT()
info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
self.k32.SetInformationJobObject(
    self.job, 9, ctypes.byref(info), ctypes.sizeof(info)
)
```

Each spawned component is assigned to the job right after `Popen`, through its process handle. Descendants found later are assigned by PID (section 1.2). Both are best effort: a failed assignment is ignored.
```python
def add(self, proc: subprocess.Popen) -> None:
    if self.job:
        self._assign_handle(int(proc._handle))

def _assign_pid(self, pid: int) -> None:
    h = self.k32.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE,
                             False, int(pid))
    ...  # AssignProcessToJobObject(self.job, h), then CloseHandle(h)
```

The launcher process can end by any route: a clean exit, a task kill or a console crash. In every case its last handle to the job closes, and the kill-on-close limit terminates every process that was assigned to the job. This does not rely on Python signal handlers.

### 1.2 Descendant sweep (`add_tree`)
Processes that a component creates after it has joined the job inherit the job automatically. `add_tree` is a best-effort sweep for descendants that started before the assignment took effect:
- 4.0 seconds after each spawn (`threading.Timer(4.0, lambda: self.job.add_tree(p.pid)).start()`), the launcher lists all processes with a PowerShell CIM query:
  ```powershell
  Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress
  ```
- The JSON output becomes a PID-to-parent map. The launcher grows the descendant set of `root_pid` until it stops changing. It opens every PID in that set (`PROCESS_SET_QUOTA | PROCESS_TERMINATE`) and assigns it to the job. A PID that is already in the job just fails the assignment.

### 1.3 Console control handler and the layered `shutdown()`
- `launcher.py` registers a console control handler (`SetConsoleCtrlHandler`) through a module-level C callback (`_HANDLER`, built with `PHANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint32)`). Because the reference lives at module level, the function pointer is not garbage collected.
- Every console event calls `shutdown()`. The handler returns `True` for `CTRL_C_EVENT` (handled) and `False` otherwise. So for `CTRL_CLOSE_EVENT` (closing the console window), the default Windows termination still runs as a backstop. `SIGINT`, `SIGTERM` and `KeyboardInterrupt` also call `shutdown()`.
- `shutdown(why: str)` runs three layers:
  1. **Layer 1 (recursive kill):** every live process in `self.procs` gets `taskkill /PID <pid> /T /F` (15 s timeout; `terminate()` if that fails).
  2. **Layer 2 (command-line sweep):** this covers parent/child races where an intermediate process has already exited. All `python.exe` processes are listed through CIM:
     ```powershell
     Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress
     ```
     Only processes that `is_studio_cmd()` recognizes are killed (`taskkill /PID <pid> /T /F`). It recognizes a command line containing `main.py --port N` for N in 8188, 8189, 8199 or 8190, or one containing `uvicorn`, `api:app` and `--port <ui port>`. Other FastAPI apps on the machine are left alone.
  3. **Layer 3 (job handle close):** closes `self.job` through `CloseHandle`, which triggers `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` for any process that is still assigned. `main()` closes the job again in its `finally` block.

### 1.4 Fast-crash window and restart cap
This stops a component that dies at startup (corrupt checkpoint, CUDA init failure, blocked port) from restarting in a loop forever:
- Constants: `RESTART_CAP = 3`, `RESTART_WINDOW_S = 45`.
- The health loop checks the components every 4 seconds. An exit within 45 seconds of launch (`time.time() - born < RESTART_WINDOW_S`) counts as a fast crash. An exit after a longer run resets that component's counter to 0.
- Below the cap, the launcher relaunches the component from its spec after a 3-second pause. On the third fast crash in a row (`self._fast_crashes[name] >= RESTART_CAP`), it prints `crashed 3x in a row — NOT restarting.` and stops restarting that component. The other components keep running.
- A restart the GPU broker makes on purpose (section 2.5) is not a crash: `_restart_instance` first leaves `logs/.planned-restart-<port>` (`gpu_broker.planned_restart_file`). When the component on that port exits, the launcher reads and deletes the mark (`launcher.planned_restart`), says the broker restarted it to free the card's memory, starts it again at once and resets its fast-crash count. A mark older than 5 minutes (`PLANNED_RESTART_S`) is ignored, so it cannot hide a later crash.

### 1.5 Components, skips and model paths
- The launcher starts five components in this order: `comfy-ui` (8188), `control-room` (`ui.port`, default 8080), `ltx-render` (8189), `h3-render` (8199) and `qwen-render` (8190). Their launch arguments are in the table in section 3. Each component logs to its own file in `logs/` (`comfyui_8188.log`, `ui.log`, `ltx_8189.log`, `h3_8199.log`, `qwen_8190.log`), and the previous run's file is kept as `*.old.log`. The launcher tails all of them into one console.
- `comfy-ui` and `control-room` run on `tools/python/python.exe` (or on the interpreter running the launcher when that file is missing), with `PYTHONPATH` pointing at `tool/.venv/Lib/site-packages` (for `control-room`, `tool/` comes first: `tool/launcher.py:532-533`). `ltx-render` and `h3-render` run on `ComfyUI_LTX/venv/Scripts/python.exe`, and `qwen-render` runs on `ComfyUI_Qwen21/venv/Scripts/python.exe`. These three do not get the studio `PYTHONPATH`.
- When a component's venv is missing, the launcher prints `!! venv not found: ... — component not started` and skips it; a venv that still holds the installer's `.kinoloom-incomplete` marker is skipped the same way (`... was not finished by the installer`). There is no fallback to the base Python, because the base Python lacks the node-pack dependencies. A missing base interpreter skips a component the same way.
- A component this PC's graphics card or driver can't run is not started either (`!! not started: <why>`). `tool/gpu_support.py` reads the card's name, compute capability and driver from `nvidia-smi` once. The CUDA 13.0 build on 8189 and 8199 (LTX-2.5, MiniMax H3) needs compute capability 7.5 or newer (a GTX 16- or RTX 20-series card or newer) and driver R580. The render farm's CUDA 12.6 build on 8188 needs 5.0 to 9.0 (a GTX 900- to RTX 40-series card: its torch holds no kernels for the RTX 50 series) and R560. Qwen-Image 2.1 on 8190 is checked against the build its venv holds (`qwen_runtime`, from its torch's `version.py`): the installer builds it on CUDA 13.0, or on CUDA 12.6 where only that one runs (`qwen_pick`; `install/components.json`, the venv's `fallback`), and a venv built for another card says to run `INSTALL.bat` again. When `nvidia-smi` does not answer, nothing is refused. The startup header names the card and driver, and the "Kinoloom is ready" banner the components it did not start on this PC, with the first reason. The model registries use the same rules (`models.gpu_problem`, `video_models.model_status`): such a model is unavailable with the reason in its `gpu` field.
- Model folders outside the tree come from settings `paths.extra_model_dirs`. Relative paths resolve against the studio root, and folders that do not exist are ignored.
  - **Mounted in ComfyUI:** on every launch the launcher writes `temp/extra_model_paths.studio.yaml`, with one `studio_extra_<i>` entry per folder: a `base_path` plus the subfolders `checkpoints`, `diffusion_models`, `text_encoders`, `vae`, `loras`, `upscale_models`, `latent_upscale_models`, `clip_vision`, `ipadapter` and ComfyUI's older `unet` and `clip` (`model_files.MOUNTED`). It passes the file to all four ComfyUI instances with `--extra-model-paths-config`, which ComfyUI reads after the tree's own `extra_model_paths.yaml`, so a file in the studio's `models/` wins over one of the same name in a model folder. With no folders configured, nothing is written or passed.
  - **Counted by the studio:** a file in these folders counts the same way (`tool/model_files.py`): the image models (`models.is_available`), the video tiers (`video_models._exists`) and the installer find it in its place below a folder, and a file listed in `install/components.json` only with the size given there; between folders only the first file of a name counts, the one ComfyUI loads.
  - **Files the user already has:** the installer and Settings › Model files (`routers/model_folders.py`) find copies a user already has under other names (size, then SHA-256) and use them without changing them: a hard link in `models/`, their own folder as a model folder, a hard link in a `Kinoloom-models` folder on their own drive (made the first model folder), a symbolic link, or, asked first, a copy.
  - **Letter case:** the launcher, the installer and that search give a file in the studio's own folders that was saved in another letter case its catalog name, since ComfyUI's graphs want the exact name.
  - **Lab variables:** the same folders also fill `H3_LAB_MODELS` / `LTX25_LAB_MODELS` when the shell has not set them (the first folder whose path contains `h3` / `ltx`). `ComfyUI_LTX/extra_model_paths.yaml` mounts those variables.
- Before launching, the launcher checks ports 8188, the UI port, 8189, 8199 and 8190. If only studio processes hold them (`is_studio_cmd`), it offers `[R]estart` or `[E]xit`. Restart kills the old instance, and the launcher also chooses it automatically when there is no interactive console. If any holder is a foreign process, the launcher refuses to start and prints the PIDs.
- Once 8188 answers (polled for up to 90 × 2 s), the launcher starts `tool/scripts/warm_studio.py`. That script reads `ensemble.warm_models` / `warm_image_models` / `warm_video` and does nothing when they are off, which is how `settings.example.yaml` ships.

---

## 2. The GPU lease broker (`tool/gpu_broker.py`)

Kinoloom runs four ComfyUI instances against one 8 GB GPU (the tested machine has an NVIDIA GeForce RTX 3070). Two resident model stacks do not fit on that card at once. So `tool/gpu_broker.py` is the single authority for local GPU work: one local render at a time, and every other instance verified empty before a job starts.

```
                          ┌────────────────────────────────┐
                          │  FIFO ticket queue (Condition) │
                          └───────────────┬────────────────┘
                                          │
                     [acquire(model, port, purpose, timeout, hold_s)]
                                          │
                                          ▼
                         ┌────────────────────────────────┐
                         │  _prepare_for: other ports     │
                         │  evicted + verified empty      │
                         │  (/queue empty, vram_free back │
                         │  to baseline - 512 MB; 5500 MB │
                         │  only while no baseline exists)│
                         └───────────────┬────────────────┘
                                          │
                         ┌────────────────┴────────────────┐
              Target warm, same model?             Different model?
                         │                                 │
                         ▼                                 ▼
                 ┌───────────────┐                 ┌───────────────┐
                 │  Keep loaded  │                 │ Evict target  │
                 │ (no eviction) │                 │ (/free or H3  │
                 └───────┬───────┘                 │   restart)    │
                         │                         └───────┬───────┘
                         └────────────────┬────────────────┘
                                          │
                                          ▼
                          ┌────────────────────────────────┐
                          │   Lease granted (state: busy)  │
                          └───────────────┬────────────────┘
                                          │
                                  [release(lease)]
                                          │
                                          ▼
                          ┌────────────────────────────────┐
                          │ warm until TTL (default 8 min);│
                          │ TTL expiry / other model →     │
                          │ evict (/free, H3: restart)     │
                          └────────────────────────────────┘
```

### 2.1 Lease model
- A lease is the broker's permission to use the card for one job. There is one global lease (`_active`) and a FIFO ticket queue under a single `threading.Condition`. `acquire()` waits until no lease is active, no reconcile is running and its ticket is the oldest. The wait is bounded by `timeout` (default 3600 s), after which it raises `GpuBusyError`. With `wait=False` it raises at once when the GPU is busy.
- The local registry is settings `ensemble.comfyui_port` plus the `comfy_port` of every image and video model (cached for 30 s). A port outside it, such as a remote worker, gets an unmanaged no-op lease. So does every request when `gpu_broker.enabled` is false.
- `hold_s` (default: the same value as `timeout`) limits how long a lease may be held. The maintenance tick (every `poll_seconds`, default 30, clamped to 5–300) releases an expired lease as warm. But while that instance's queue is still running, the tick extends the lease by `max(60, 2 × poll_seconds)` instead.
- Who takes leases:
  - `renderer.ComfyClient.queue_prompt` takes an implicit per-prompt lease for a local port when the calling thread holds none. `watch_prompt` releases it once the prompt shows up in `/history` (it gives up after 2 h).
  - Image jobs (`tool/images.py`) and Video Studio jobs (`tool/video_studio.py`) take an explicit lease with `acquire()`, so the prompts they submit pass straight through. A Video Studio chain holds one lease for all of its segments (`hold_s = segments × per-segment timeout + 1800`).
  - Processes outside the control room lease through `POST /api/gpu/lease` and `POST /api/gpu/release`. If the control room cannot be reached, `queue_prompt` refuses to submit unless `KINOLOOM_ALLOW_UNBROKERED=1` is set.

### 2.2 Instance states
Each local port has an `_Instance` in one of six states:
- **`unknown`**: the initial state, and the state after a lease ends without the instance being kept warm.
- **`empty`**: verified empty (section 2.3).
- **`warm`**: the last lease's model stays loaded until the instance's TTL deadline (TTL, time to live: how long an idle model may stay loaded).
- **`busy`**: the instance holds the active lease.
- **`evicting`**: an eviction is in progress.
- **`degraded`**: eviction verification failed, including after a restart.

### 2.3 Pre-flight check (`_prepare_for`) and empty verification
Before `acquire()` returns a managed lease for the target port:
1. Every other port that is not `empty` is evicted (made to drop its models from the card) with verification. With `max_warm_stacks` > 1 (default 1), the `max_warm_stacks - 1` most recently used warm stacks are kept.
2. The target is kept if it is warm with the same model (or if either model id is unknown). Otherwise it is evicted too. A `degraded` target must verify empty within 5 s, or `acquire()` raises `GpuUnavailableError`.
3. Final gate: every other port that was not kept must verify empty within 5 s, or `acquire()` raises `GpuUnavailableError` (`port N not empty before render`).

`_verify_empty(port)` polls every 0.5 s until its timeout:
- A port that does not answer and is not listening counts as empty (no process, no VRAM).
- Otherwise `/queue` must report nothing running or pending, and `devices[0].vram_free` from `/system_stats` must be back at the instance's baseline: `free >= baseline_mb - vram_tolerance_mb` (`vram_tolerance_mb` default 512, minimum 64).
- The baseline is the free VRAM recorded the first time the instance verified empty. Until a baseline exists, `free >= _EMPTY_FALLBACK_MB` (5500 MB) is accepted and becomes the baseline. The code comment notes that an idle 8 GB card shows about 7.1 GB free. A restart clears the baseline.
- `vram_free` is the card's free memory as that instance reports it, so the check measures what all processes on the card leave free.

### 2.4 Warm retention and TTL
- On release the instance becomes `warm`, with a deadline of now + TTL. The TTL comes from the `gpu_broker.ttl_minutes` entry for the model id, then for its family, then `default`. Otherwise it is `gpu_broker.default_ttl_minutes`, which falls back to `vram.idle_minutes` (default 8, clamped to 1–120).
- A later lease for the same model on the same port skips the eviction, so the weights ComfyUI already loaded are reused. Any other model, on that port or another, evicts the warm stack first (section 2.3).
- The maintenance tick evicts a warm instance whose TTL has passed, unless its queue is still running. In that case the eviction waits for a later tick. No TTL eviction runs while any lease is active.

### 2.5 Asynchronous eviction and MiniMax H3 restarts
ComfyUI's `/free` endpoint is asynchronous. It sets a flag that is consumed between prompts, so sending `/free` does not free VRAM at once. That is why every eviction is verified.
- **Regular instances:** if the instance is listening and its queue is not empty, the broker sends `/interrupt` and `/queue {"clear": true}`. It then posts `/free` with `{"unload_models": True, "free_memory": not evict_keep_conditioning}` (`evict_keep_conditioning` defaults to true, which keeps the prompt-conditioning cache). After that it runs `_verify_empty` for `verify_timeout_s` (default 60, clamped to 10–300). If verification fails and `restart_on_verify_fail` is on (default), the broker restarts the instance and verifies again. If it still fails, the instance is marked `degraded`.
- **MiniMax H3:** an instance is flagged `h3` when a registered video model on its port belongs to a family starting with `h3`. With `h3_mode: restart` (the default), every eviction of that instance is a restart instead of `/free`, unless nothing runs on its port (no process found and nothing listening: its ComfyUI tree is not installed, or the launcher has not started it yet), which counts as empty without a restart. The module notes that the H3 runtime's aimdo 0.5.3 livelocks (stays busy without making progress) on `/free` followed by a reload. Releasing an H3 lease does not restart the instance. It stays warm like the others until its TTL lapses or another model needs the card.
- **`_restart_instance`:** finds the PIDs that listen on the port or whose command line contains `main.py --port N` (through psutil). It kills each with `taskkill /PID <pid> /T /F`, then waits up to 180 s for `/system_stats` to answer again (or raises `GpuUnavailableError`). The broker does not spawn the process itself. The launcher's health loop (section 1.4) relaunches the dead component at once; the planned-restart mark the broker leaves first keeps it from counting as a crash.

### 2.6 Startup, resume and manual control
- The control room starts the broker during its lifespan (`routers/common._queue_boot`). With `reconcile_on_start` (default true), acquires are blocked until every local instance has been brought to a verified-empty state (hard eviction, restarts allowed).
- With `gpu_broker.prewarm` on, the broker then pre-warms Qwen (model `qwen21_fast` at 1024x1024 with 8 steps). `tool/settings.example.yaml` ships it off (`prewarm: false`, `prewarm_max_rounds: 3`); with the keys missing, the code's defaults are on and 5 rounds. It runs up to `prewarm_max_rounds` real renders. It stops at the first round that finishes within `prewarm_fast_seconds` (150 s), or as soon as user work is waiting. The module's notes say that the first 1–3 Qwen generations in a fresh process go through ComfyUI-GGUF's slow per-module cast path. After that the process settles at about 3.7 s/step.
- **Resume from sleep:** when wall-clock time runs ahead of monotonic time by more than 90 s (`_RESUME_GAP_S`) between ticks, the broker reconciles (`reconcile_on_resume`). If a lease is active, the reconcile waits until it ends.
- **HTTP:**
  - `GET /api/gpu/state` returns per-port state, baseline, warm time left and the last 40 decisions.
  - `POST /api/gpu/lease` and `POST /api/gpu/release` take and return a lease.
  - `POST /api/gpu/cancel` interrupts only the active lease's prompt.
  - `POST /api/gpu/evict` evicts one port or all of them. It is refused while a render is active.

---

## 3. The ComfyUI fleet: one port per model family

The model families need different ComfyUI versions, PyTorch builds and node-pack dependencies. So Kinoloom runs them as separate instances with separate runtimes, instead of forcing conflicting packages into one environment. The ComfyUI versions below are read from each tree's `comfyui_version.py`.

| Port | Service | Runtime and Python environment | Launch arguments | Role and workload |
|---|---|---|---|---|
| **8080** (`ui.port`) | **Control Room** | `tools/python/python.exe` + `tool/.venv` site-packages | `-m uvicorn api:app --host <ui.host> --port <ui.port>` (default host `127.0.0.1`) | Web UI, REST API (`/api/*`), project and shot databases, orchestrator, GPU broker, task queue. |
| **8188** | **Render Farm** | `tools/python/python.exe` + `tool/.venv` site-packages<br>ComfyUI 0.30.0 (`ComfyUI/`) | `--port 8188 --default-device 0 --disable-auto-launch` | SDXL (RealVisXL V5, CyberRealistic XL v10) and Z-Image Turbo stills, the legacy Wan 2.2 video model (only when a project or shot picks it), and the post workflows (RIFE, RealESRGAN, face-fix). |
| **8189** | **LTX-2.5 Farm** | `ComfyUI_LTX/venv`<br>ComfyUI 0.36.0 (`ComfyUI_LTX/`, commit `ee71d5c`) | `--port 8189 --listen 127.0.0.1 --enable-dynamic-vram --bf16-vae --disable-auto-launch` | LTX-2.5 22B W4A8 tiers; the reference tiers use the `ComfyUI-LTX2.5-MSR` multi-reference guide. |
| **8190** | **Qwen-Image Farm** | `ComfyUI_Qwen21/venv`<br>ComfyUI 0.37.4 (`ComfyUI_Qwen21/`) | `--port 8190 --listen 127.0.0.1 --highvram --disable-auto-launch` | Qwen-Image 2.1 stills (`qwen21` / `qwen21_fast`; a 7.1B DiT as Q5_K_M GGUF per `tool/models.py`), 1024x1024 or 1344x768 with a 4x-UltraSharp + lanczos finish. |
| **8199** | **MiniMax H3 Farm** | `ComfyUI_LTX/venv`<br>same tree as 8189 | `--port 8199 --listen 127.0.0.1 --enable-dynamic-vram --database-url sqlite:///<root>/ComfyUI_LTX/user/comfyui_h3.db --disable-auto-launch`<br>*(without `--bf16-vae`)* | MiniMax H3 W4A8 ConvRot tiers (Ref2VA reference graphs, fp16 video VAE), with a database separate from 8189. |
| **8120** | **Worker Agent** (on each worker PC; not started by the launcher) | the worker PC's own Python running `tool/worker/worker_agent.py` | uvicorn app on `agent_port` from `worker_config.yaml` (default 8120) | Runs job envelopes on that PC's own ComfyUI (default port 8188). A cloud box (for example Colab) can register a full base URL instead of host:port. |

When `paths.extra_model_dirs` is set, every ComfyUI row also gets `--extra-model-paths-config temp/extra_model_paths.studio.yaml` (section 1.5).

### Why each family gets its own instance
1. **Database lock contention:** ports 8189 (LTX) and 8199 (H3) run from the same `ComfyUI_LTX` tree. Without a separate `--database-url`, both servers use the same SQLite file, and the launcher's notes record lock errors from that. So H3 uses `ComfyUI_LTX/user/comfyui_h3.db`.
2. **VAE precision:** the VAE is the part of the model that converts between latents (the model's compressed working form) and video frames. Port 8189 runs with `--bf16-vae` for LTX-2.5. Port 8199 does not, because the H3 video VAE is fp16 and the proven 1080p two-phase H3 config was measured without the bf16-VAE override (`tool/launcher.py`, and `PROVEN_CONFIGS_H3.md` in the lab archive, which is not redistributed; see [VIDEOLAB_ARCHIVE_INDEX.md](research/VIDEOLAB_ARCHIVE_INDEX.md)).
3. **DynamicVRAM vs. HighVRAM:** LTX (8189) and H3 (8199) run with ComfyUI's `--enable-dynamic-vram`. Qwen-Image 2.1 (8190) runs with `--highvram`. The reason is recorded in `tool/launcher.py`: without that flag the Q5_K DiT kept falling into ComfyUI-GGUF's per-module CPU dequantization, which ran on one CPU core at 20–100 s/step, against about 3.7 s/step on the fast path. `--highvram` disables DynamicVRAM, so the 4.97 GB model loads fully onto the card.

---

## 4. Project director (`tool/director.py`)

The Project Director walks one project through the production stages in a background thread. A per-project mode and per-project gate settings decide where it pauses for approval.

### 4.1 The 9-stage production flow
`STAGE_FLOW` has nine stages:
```
story ⟶ cast ⟶ scenes ⟶ shots ⟶ prompts ⟶ keyframes ⟶ render ⟶ review ⟶ assemble
```
For each stage the loop first asks `_stage_done()`. It skips finished stages, so a project that was stopped or hit an error resumes where it left off. A project with status `done` runs again only for new work (a beat without shots, or a shot still to render), from the earliest stage that has it; without new work, Start does nothing. The Director shares the story's AI writer (`storygen_llm.ArchitectJob`) with the Story page: one run writes a project at a time, the Director waits for a run the page holds and does not write again an outline that run has written, and stopping the Director stops that run too. When the run is stopped from the Story page instead, the project pauses at its stage until the next Start.

The Movie side has one production button whose label follows the project (`state.js` `transport`): **Start production**, **Resume production** after a Stop, **Continue production** when it waits for the story, the shots or video prompts to be written, and **Approve & continue** at an approval gate (the opening frames, the review). The first three call `POST /api/projects/{pid}/run`, the last `/resume`; each waits up to `RUN_WAIT_S` (4 s) for the run to settle (`Director.start(pid, wait=...)`). A run that stopped by then answers with the project's status, stage and reason; the page then watches a run still working for half a minute and says why when it stops. Whatever the page, a note under the top bar keeps the reason of a project that waits (`gate_pending` with a reason) or stopped with an error, with the action that moves it on, and while shots render it counts the shots that wait for a video prompt. When production stops for the user, a dialog on either side says what it waits for and opens the page that needs them (`Shell.stopInfo`: the Shots page for prompts and opening frames, Cast & World for a look text), the tab's title starts with "Waiting for you" or "Stopped", and a desktop notification says the same while the window is in the background; each stop is announced once.
- **`story`**: done once the project has a treatment (a prose outline of the film) and its beats, or, for a story written before beats existed, any shot row. Otherwise, when an AI writer is set up, the Director has it write the outline only: treatment, story bible and beats (`ArchitectJob.run(pid, outline=True, scenes="none")`). Without one, or when it fails or does not answer, the project pauses (status `gate_pending`, stage `story`, with the reason), and the outline is written on the Story page with any chat AI or the AI writer. The Director never writes a story from templates. When the project's story gate is manual, a written story still waits for approval.
- **`cast`**: done when the project Bible has characters and none of them has a look text that contradicts its main picture (`cast_look.checks`, `docs/PIPELINE_AND_CONTINUITY.md`, section 1.1). With such a text, the project pauses (status `gate_pending`, stage `cast`) and names the character and the problem, until the text is put right on Cast & World.
- **`scenes`**: does no work of its own. It counts as done when the project Bible has scenes. Otherwise the stage only advances the project status.
- **`shots`**: done when every beat has shots, or once the project's stage is past `shots`. When an AI writer is set up, it writes the beats that have no shots (`ArchitectJob.run(pid, outline=False, scenes="missing")`). Without one, they are written on the Story page (Shots scene by scene), and the scenes already written go on to their prompts and their render. A project with no shot at all pauses at stage `shots`, naming the scenes that need them.
- **`prompts`**: the video prompts of the LTX-2.5 / H3 shots (`docs/PIPELINE_AND_CONTINUITY.md`, section 4.4.3). Done when every shot still to render has a ready prompt, or once the project's stage is past `prompts`. When an AI writer is set up, the Director has it write the missing and stale prompts, one unit at a time in story order (`movie_prompts.PromptWriter.run`). The shots that have their prompt go on to render while the others wait for theirs in the render stage. When no shot could render yet, the project pauses in every mode (status `gate_pending`, stage `prompts`, with the shot ids and the reason), and the prompts are written on the Shots page, by hand or with a chat AI (**Use a chat AI**); **Continue production** then runs from the first stage not done. An AI writer that does not answer is named in that reason, with its address: the writer asks whether it answers before its first unit, and the endpoint is remembered for a minute (`movie_prompts._DOWN`), so the later units do not each wait out the retries.
- **`keyframes`**: the LTX-2.5 / H3 opening frames (`docs/PIPELINE_AND_CONTINUITY.md`, section 4.4.2). With the keyframes gate manual (the Checkpoints and Manual default), the stage makes the opening frames of the cuts still to render, before the render, and the project pauses (status `gate_pending`, stage `keyframes`) with the number of frames to look at on the Shots page and the shots whose likeness is under `movie.key_image_min_likeness`. **Approve & continue** moves on to `render` and marks those frames looked at in their records (`opening_frames.looked_at`), so a Stop and Start does not pause for them again; a frame made again, or made later, pauses the project again. A match cut's frame is made from the last frame of the shot before, so at render time. With the gate on `auto`, or with nothing to look at, the stage counts as done, and every frame is made right before its unit renders. The Wan 2.2 path's keyframes are made on demand during the render; LTX-2.5 / H3 shots never open on them.
- **`render`**: sets `orchestrator.project_filter` to the project, starts the orchestrator and polls every 4 s until every shot of the project is terminal (`passed`, `review`, `failed` or `assembled`). When nothing still to render can render before more video prompts are in (`Director._held_by_prompts`: every such shot waits for its prompt, status `prompt`, or opens on one that does, `orchestrator.waits_for`: a `CONTINUE`, or a match cut in the same place), the Director stops the orchestrator and pauses the project at stage `prompts` (status `gate_pending`), with how many shots are rendered, the shots that wait and the reason. A prompt saved on the Shots page, by hand or from a chat AI's reply, queues its shot again (`movie_prompts.release_waiting`): while other shots still render it renders in the same run, and after the stop on **Continue production**.
- **`review`**: pauses when any shot is in `review` and the review gate is manual. Otherwise it continues, and the review shots stay flagged.
- **`assemble`**: first checks that every beat has shots. Otherwise the project pauses at stage `shots` with the scenes still to write, and the scenes already rendered stay as they are. Deleting a scene on the Shots page removes its beat too, so the film does not wait for it. Then it takes the project's `passed` shots (`final_path` when post-processing produced one, else `video_path`), grouped by scene in shot order, and calls `assemble_movie(..., ns=<project id>)` (section 7.4). On success the project is set to status `done`, stage `film`.

QA scoring (the automatic quality check) is not a Director stage. It runs in the orchestrator's post worker (`docs/PIPELINE_AND_CONTINUITY.md`, section 5).

### 4.2 Three modes and the gates
The project `mode` is `autopilot`, `checkpoints` or `manual` (validated by the Projects router). The approval gates are stored per project in `gates`, default `{"story": "auto", "keyframes": "manual", "review": "manual"}`. A gate name missing from that dict counts as `manual`.
1. **`autopilot`**: creating a project in this mode sets all three gates to `auto`.
2. **`checkpoints`**: uses the gates from the request, or the defaults. With the defaults the Director pauses (project status `gate_pending`) at `keyframes` when there are opening frames to look at, and at `review` when shots were flagged.
3. **`manual`**: uses the same gates. In addition, the `shots` stage pauses after it has had to (re)generate shots. All other stages advance without stopping.

Changing the mode later does not rewrite the gates. Approving (`resume`) restarts the loop and skips completed stages. A review gate pauses again on resume while any shot is still in `review`.

Mode changes do not lose work. The project row and shots live in `tool/studio.db`, story stages are versioned in `story_versions`, and the project Bible is a git-tracked folder under `bible/projects/<project_id>/`.

---

## 5. Database schemas

Kinoloom keeps its persistent state in three SQLite files under `tool/`, all opened in WAL mode (`PRAGMA journal_mode=WAL`):
- `queue.db` (`tool/task_queue.py`, which also sets `PRAGMA synchronous=NORMAL`);
- `studio.db` (`tool/shots_db.py`, `tool/projects.py`);
- `datasets.db` (`tool/datasets_db.py`).

The last two also set `PRAGMA busy_timeout=30000`.

### 5.1 Task queue database (`tool/queue.db`)
This file holds the task queue, the retry counts and the registered workers.

```sql
CREATE TABLE IF NOT EXISTS queue_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                     -- 'image' | 'video' | 'dataset' | 'shot' | 'refbatch'
    label TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',     -- JSON execution parameters
    worker TEXT NOT NULL DEFAULT 'local',   -- Assigned worker node ('local' or worker slug)
    status TEXT NOT NULL DEFAULT 'draft',   -- 'draft'|'queued'|'running'|'done'|'failed'|'canceled'|'held'
    priority INTEGER NOT NULL DEFAULT 5,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    prompt_id TEXT DEFAULT '',
    worker_job_id TEXT DEFAULT '',
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    parent_id TEXT DEFAULT '',
    engine_ref TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    started TEXT DEFAULT '',
    finished TEXT DEFAULT '',
    lease TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_items_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS ix_items_parent ON queue_items(parent_id);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,                    -- Worker slug
    name TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 8120,
    token_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'offline', -- 'online' | 'offline'
    gpu TEXT NOT NULL DEFAULT '{}',         -- Telemetry payload (VRAM, device name)
    models TEXT NOT NULL DEFAULT '[]',      -- Cached model identifiers
    capabilities TEXT NOT NULL DEFAULT '{}',
    agent_version TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    last_heartbeat TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shot_assignments (
    project_id TEXT NOT NULL,
    shot_id TEXT NOT NULL DEFAULT '',       -- '' = the project default
    worker TEXT NOT NULL DEFAULT 'local',
    updated TEXT NOT NULL,
    PRIMARY KEY (project_id, shot_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_workers (
    run_id INTEGER PRIMARY KEY,
    worker_map TEXT NOT NULL DEFAULT '{}'
);
```

The queue never dispatches `shot` items. The orchestrator keeps the shot order, and the queue rows for shots only mirror status and carry the per-shot worker assignment. A single dispatcher thread runs one lane per worker ("local" plus each online worker PC), and the local lane runs at most one dispatched item at a time.

#### Crash recovery at startup (`reap_on_startup()`)
When the application starts, `reap_on_startup()` in `tool/task_queue.py` sweeps `queue_items`:
- It recovers any item with `status == 'running'` (left behind by a power loss, a crash or an abrupt restart).
- `attempts` goes up by 1.
- If `attempts >= max_attempts`, the item is marked `failed` with the error message:
  ```text
  did not survive an app restart
  ```
- If `attempts < max_attempts`, its status goes back to `queued` with the lease cleared, so the next dispatcher cycle retries it.

Shots in `studio.db` are recovered separately:
- At API startup, `routers/common._recover_stuck_shots()` moves shots in `rendering`, `post`, `qa` or `drafting` back to `queued`.
- When the orchestrator starts, any remaining `rendering` rows go to `pending`. `rendered` / `post` rows are re-queued for post + QA when the raw render still exists. Otherwise they go to `pending`.

### 5.2 Studio project database (`tool/studio.db`)
This file holds projects, shot definitions and story versions. `ShotsDB` creates `shots`, `scene_state`, `projects` and `story_versions`. Its migration adds the per-shot columns and rebuilds `shots` with the composite primary key `(shot_id, project_id)`, because two projects may both have a shot `s01_001`.

```sql
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT,
    idea TEXT,
    genre TEXT,
    tone TEXT,
    duration_target INTEGER DEFAULT 60,
    mode TEXT DEFAULT 'checkpoints',
    gates TEXT DEFAULT '{"story":"auto","keyframes":"manual","review":"manual"}',
    status TEXT DEFAULT 'draft',
    stage TEXT DEFAULT 'story',
    story TEXT DEFAULT '{}',
    bible_commit TEXT DEFAULT '',
    created TEXT,
    updated TEXT
    -- added by projects._ensure_project_columns:
    -- ref_fallback INTEGER DEFAULT 1, video_family TEXT DEFAULT '',
    -- video_models TEXT DEFAULT '', post_processing INTEGER DEFAULT 0,
    -- post_worker TEXT DEFAULT '', seamless_takes INTEGER DEFAULT 1,
    -- take_max_shots INTEGER DEFAULT 0, error TEXT DEFAULT ''
);

CREATE TABLE shots (
    shot_id TEXT NOT NULL,
    scene TEXT,
    json TEXT,
    status TEXT DEFAULT 'pending',
    tier TEXT,
    model TEXT,
    resolution TEXT,
    seed INTEGER,
    attempts INTEGER DEFAULT 0,
    render_time_s REAL,
    qa_clip REAL,
    qa_face REAL,
    qa_motion REAL,
    qa_notes TEXT,
    video_path TEXT,
    error TEXT,
    updated TEXT,
    project_id TEXT,
    spec_version INTEGER DEFAULT 1,
    render_profile TEXT,
    qa_stage INTEGER DEFAULT 0,
    keyframe_path TEXT,
    tail_frame TEXT,
    prompt TEXT,
    negative TEXT,
    ref_image TEXT,
    final_path TEXT,
    PRIMARY KEY (shot_id, project_id)
);

CREATE TABLE IF NOT EXISTS story_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    stage TEXT,
    version INTEGER,
    content TEXT,
    created TEXT
);

CREATE TABLE IF NOT EXISTS scene_state (
    scene TEXT PRIMARY KEY,
    state TEXT
);
```

### 5.3 Dataset provenance database (`tool/datasets.db`)
This file records where each training image came from, the settings that generated it, and the dataset runs.

```sql
CREATE TABLE IF NOT EXISTS dataset_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_type TEXT,
    dataset_name TEXT,
    file TEXT,
    run_id INTEGER,
    model TEXT,
    prompt TEXT,
    negative TEXT,
    seed INTEGER,
    steps INTEGER,
    cfg REAL,
    sampler TEXT,
    scheduler TEXT,
    resolution TEXT,
    loras TEXT,
    variations TEXT,
    source TEXT DEFAULT 'generated',
    ts TEXT,
    duration REAL,
    status TEXT DEFAULT 'done'
);
CREATE INDEX IF NOT EXISTS idx_img_ds ON dataset_images(dataset_type, dataset_name);
CREATE INDEX IF NOT EXISTS idx_img_run ON dataset_images(run_id);

CREATE TABLE IF NOT EXISTS gen_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_type TEXT,
    dataset_name TEXT,
    status TEXT,
    total INTEGER,
    done INTEGER,
    failed INTEGER,
    params TEXT,
    created TEXT,
    updated TEXT,
    error TEXT
);
```

---

## 6. LoRA training (`tool/training.py`, `tool/dataset.py`, `tool/train/train_lora.py`)

A LoRA is a small add-on file that teaches a base model a new face, look or concept. Kinoloom includes a LoRA training subsystem for character (face), power, environment, anatomy and quality adapters. It is set up to fit an 8 GB card through layer offloading and cached latents.

Both backends train for the legacy Wan 2.2 TI2V-5B model, the studio's first video model. It is not as good as LTX-2.5 or MiniMax H3, and the studio only renders it when a project or shot picks it. LTX-2.5 and H3 shots take the cast's reference images instead of LoRAs.

### 6.1 Dataset pipeline and automatic curation
- **Where the images come from (`tool/dataset.py`):**
  - `import_urls` downloads direct image URLs. Only `https` is allowed, and every DNS answer for the host must be a public address. The download uses `curl` pinned to the validated IP (`--resolve`), with no redirects. One call takes at most 500 URLs, and each file may be at most 50 MB. A file is kept only if it is larger than 10 KB and decodes as an image.
  - `self_render_batch` renders short Wan 2.2 5B clips on port 8188 (512x288, 6 steps, 49 frames) and keeps the first frame of each.
- **Automatic curation (`dataset.auto_filter`):**
  - **Sharpness:** a Laplacian variance below 60 marks an image `blurry` and rejects it.
  - **Deduplication:** a 16×16 average hash identical to an earlier image's marks it as a duplicate and rejects it.
  - **Face presence:** face datasets run the Impact Pack ONNX face detector (`face_yolov8m`). `no_face_detected` is advisory only and does not reject the image, because the detector is trained on real photos and often misses generated faces.
  - Verdicts are stored under `_auto` in the dataset's `curation.json`. `datasets_db.backfill_dataset` records the folder contents in `datasets.db`. Training stages only the kept images (or all of them when nothing has been curated) into `temp/trainsets/`, each with a `.txt` caption.

### 6.2 Presets by training type
The web UI path reads the presets from settings `training.steps_presets` / `rank_presets` / `lr_presets`. The values below are the ones in `settings.example.yaml`, and the kohya CLI hardcodes the same values. LoRA alpha is `rank // 2`. The ai-toolkit config trains at resolution 512, and the kohya CLI defaults to `--resolution 512,512`.

| Dataset type | Target steps | LoRA rank ($\alpha = \text{rank}/2$) | Learning rate | Resolution |
|---|---|---|---|---|
| **`face`** | 1,800 | 16 | $1.0 \times 10^{-4}$ | $512 \times 512$ |
| **`power`** | 4,000 | 24 | $5.0 \times 10^{-5}$ | $512 \times 512$ |
| **`environment`** | 4,500 | 24 | $5.0 \times 10^{-5}$ | $512 \times 512$ |
| **`anatomy`** | 7,000 | 32 | $5.0 \times 10^{-5}$ | $512 \times 512$ |
| **`quality`** | 4,500 | 24 | $5.0 \times 10^{-5}$ | $512 \times 512$ |

### 6.3 Two training backends
1. **ai-toolkit (`tool/training.py`, used by the web UI):** `TrainingRunner` writes `temp/train_<name>.yaml` and runs `tools/ai-toolkit/run.py` with `training.aitk_python`.
   - The base model is Wan 2.2 TI2V-5B in diffusers layout (`training.wan22_diffusers`), trained with ai-toolkit's `wan21` architecture.
   - Memory settings: `layer_offloading` with `layer_offloading_transformer_percent: 0.7`, `low_vram`, gradient checkpointing, latents cached to disk (`cache_latents_to_disk: True`), cached text embeddings, fp16, batch size 1.
   - Optimizer and schedule: `adamw8bit` and the `flowmatch` noise scheduler. Checkpoints are saved every `max(25, steps // 6)` steps, and the newest 2 are kept.
   - On completion the LoRA (ComfyUI-format keys) is copied to `models/loras/` (the newest step save if the final file is missing) and registered. The check submits a 1-step Wan 2.2 5B graph that loads the LoRA and then interrupts it. If ComfyUI accepts the prompt, the LoRA is marked `ready`.
2. **kohya sd-scripts (`tool/train/train_lora.py`, CLI only):** runs `train_network.py` against `models/train_models/wan2.2_ti2v_5B_fp16.safetensors` with `networks.lora`, adafactor, a cosine scheduler, an fp8 base model, cached latents and gradient checkpointing. Output goes to `models/loras/`, plus a copy in `roster/loras/`. `--resume` needs an sd-scripts training-state checkpoint: a file whose name contains `state`, written only with `--save_state`, which the script does not pass. The final `.safetensors` is not a valid resume point.

---

## 7. Control room internals (`tool/api.py`, `tool/routers/`, `tool/security.py`)

### 7.1 App and router split
- `tool/api.py` builds the FastAPI app and mounts 16 domain routers from `tool/routers/`: `system`, `shots_bible`, `images`, `training`, `pipeline`, `hardware`, `assembly`, `projects`, `project_shots`, `cast`, `media_review`, `videostudio`, `fleet`, `queue`, `model_folders` (Settings › Model files) and `ai_prompts` (the AI writer's settings, the movie's video prompts and its opening frames). Shared singletons (the settings dict, Bible, ShotsDB, Orchestrator, PostPipeline, Assembler, Director, ArchitectJob and others) live in `routers/common.py`. Modules use flat imports, with `tool/` on the path.
- Background services start in the app `lifespan`, never at import time:
  - secret generation (section 7.2);
  - stuck-shot recovery (section 5.1);
  - broker start and queue boot (`reap_on_startup`, `video_studio.reap_stale_running`, the dispatcher);
  - the VRAM idle watchdog.
- Exception mapping: `core.InvalidInput` → 400, `FileNotFoundError` → 404, `PermissionError` → 403.

### 7.2 Security gate
The middleware runs in this order, outermost first:
1. `CORSMiddleware`, with `allow_origins` from `security.cors_origins` (default `http://127.0.0.1:8080` and `http://localhost:8080`) and credentials allowed.
2. `_response_headers`: `no-store` caching for the UI shell and `/static/`, plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN` and `Referrer-Policy: same-origin`.
3. `_security_gate`, which checks in this order:
   1. A NUL byte in the path or query → 400.
   2. **Host header (DNS-rebinding guard, which stops a hostile web page from reaching the studio through a DNS trick):** IP literals and `localhost` always pass. Host names must be listed in `security.allowed_hosts` (exact names, `*.suffix`, or `*`). Anything else → 400.
   3. **Origin (CSRF guard, which stops another website from sending requests through your browser)** for POST/PUT/PATCH/DELETE: the `Origin` must match a `cors_origins` entry or the request's own Host, and `Origin: null` is refused. A request without `Origin` is refused only when `Sec-Fetch-Site: cross-site` is present. Failures → 403.
   4. Public paths skip authentication: `/`, `/project`, `/studio-tools`, `/favicon.ico`, `/api/health` and `/static/*`. So do the three worker callbacks (`POST /api/workers/register`, `/api/workers/<id>/heartbeat`, `/api/workers/<id>/result`), whose handlers verify the fleet token themselves.
   5. With `security.local_bypass` (default true), a request whose client address and Host header are both loopback passes without a token.
   6. Every other request needs the API token: `security.api_token`, overridden by the `KINOLOOM_API_TOKEN` environment variable. It can be sent as `Authorization: Bearer <token>`, as `X-API-Token`, or in the `kinoloom_token` cookie, and it is compared in constant time. With no token configured, such requests get 401.
- **Startup secrets (`security.ensure_secrets`):**
  - `workers.shared_token` is generated when it is empty or still the shipped placeholder `__WORKER_SHARED_TOKEN__`. Worker calls are refused when the fleet token is empty.
  - `security.api_token` is generated when the bind host (`KINOLOOM_BIND_HOST`, else `ui.host`) is not a loopback address and no token is configured.
  - Both are saved to `tool/settings.yaml` with a console note, and both are redacted in the Settings UI.
- **Path guards:** `core.safe_join` accepts only relative names (no absolute paths, drive letters, UNC paths or `..`). `core.resolve_user_path` requires a client-supplied absolute path to lie inside `media_roots()`: the studio root, `paths.comfyui` / `shots` / `output` / `roster` / `temp`, `paths.extra_model_dirs` and `security.media_roots`. UNC paths are always refused.

### 7.3 Worker routing
- `workers.client_for(worker)` is the single routing point:
  - `local` or empty returns a `renderer.ComfyClient` for the port (default `ensemble.comfyui_port`);
  - `any` picks the first online worker;
  - any other value must be a registered worker id and returns a `RemoteWorkerClient`.
- `RemoteWorkerClient` duck-types `ComfyClient` (`queue_prompt`, `wait_for`, `history`, `upload_image` / `upload_file`, `download`, `interrupt`, `object_info`, `system_stats`). So engines route to another machine just by swapping the client object. Callers:
  - the orchestrator, for legacy Wan 2.2 shots and shots without a project, with the per-shot worker read from `queue.db` `shot_assignments` (per-shot override, then the project default);
  - the post pipeline's remote upscale;
  - images, shot images, dataset jobs and Video Studio.
  - LTX-2.5 / MiniMax H3 movie shots are always rendered locally (`video_studio.start(..., worker="local")`).
- **Node-class guard:** before sending a graph, `RemoteWorkerClient.queue_prompt` compares the graph's `class_type`s with the worker ComfyUI's `/object_info`, cached for 600 s per worker and cleared when the worker re-registers. Missing classes raise `RuntimeError`, which names up to six of them and says that MiniMax H3 / LTX-2.5 graphs need a worker with the `ComfyUI_LTX` tree. When the worker cannot report its node set, the job is let through.
- Every worker call carries `X-Worker-Token` and refuses redirects. The worker host is resolved again before each call, and link-local, multicast, unspecified or reserved addresses are refused. Remote deadlines are stretched by `workers.remote_timeout_scale`. Fleet setup is covered in `docs/setup/DISTRIBUTED_FLEET.md`.

### 7.4 Assembler namespacing (`tool/assembler.py`)
- `assemble_scene(scene, clips, ns)` concatenates the clips it is given. For the Director, those are the project's own passed shots. It writes `output/scenes/{ns}__{scene}.mp4` (concat list `temp/{ns}__{scene}.txt`), because scene ids repeat across projects (every story has an `s01`).
- Without `clips` and `ns` (`POST /api/assembly/run` and the legacy `Pipeline`), it falls back to globbing `output/drafts/{scene}_*.mp4`. Those per-shot drafts from `tool/post.py` are not namespaced per project.
- Films are written as `output/films/{title}_draft.mp4` and `{title}_master.mp4`, named after the sanitized title, not the project id.
- Continuity files are namespaced per project: tails in `shots/tails/<project_id>/`, keyframes in `shots/keyframes/<project_id>/`, and `_` when the id is missing or unsafe (`continuity._ns`).

### 7.5 Text-to-video guard (`tool/video_models.py`, `tool/video_studio.py`)
- `video_models.t2v_graph_problem()` reports that a tier cannot render text-to-video in two cases:
  - its t2v template contains a reference-guide node (`ComfyUILTX25MSRMultiReferenceGuide` or `MiniMaxH3ReferenceToVideo`), which needs real reference images;
  - its refs template has `Lora*` nodes that the t2v template lacks, which means a distilled tier's adapter LoRA would be dropped.
- `model_status()` then removes `t2v` from the tier's `supported_modes` and sets `t2v_note`, so the UI does not offer it. At render time `video_studio._build()` calls `_check_t2v_graph()` and raises `ValueError` with the same message.
- In the same way, `i2v` is removed for an LTX/H3 tier that has no reference graph to carry a start image (`i2v_problem`).

### 7.6 QA and the orchestrator's scope
- `QADirector.evaluate()` computes motion, face similarity and CLIP alignment. Each metric is optional, and a shot passes when none of the computed metrics fails. The `frozen` flag is advisory.
- Project shots are QA'd after post-processing when the project's `post_processing` switch is on (default off). Otherwise the raw render is QA'd (`_finalize_raw`), and a failure sends the shot to `review` without a re-render.
- With post on, a QA failure increments `attempts`. Once `attempts` reaches `qa.max_retries[tier]`, the shot goes to `review`. Before that, the retry ladder decides the next attempt. Details are in `docs/PIPELINE_AND_CONTINUITY.md`, section 5.
- The orchestrator runs one loop thread that renders one shot at a time, with a pipelined post worker beside it. Its `project_filter` is a single global value, so only one project renders at a time. This is a known limitation.
