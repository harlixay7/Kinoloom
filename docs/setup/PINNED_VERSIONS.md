[Kinoloom](../../README.md) › [Docs](../README.md) › Pinned versions

# Pinned versions

> **In short:** Kinoloom runs on upstream ComfyUI trees, custom node packs and Python runtimes that git does not store. This doc pins the exact versions the studio was built and measured against, and shows which service runs on which port with which runtime. Use it with the rebuild steps in `RESTORE_INSTRUCTIONS.md` when you set up the studio on a new PC.

**Reconstruction contract.** The upstream trees stay out of git on purpose: they are available on the internet, and they are heavy. This file pins what the studio was built and measured against. Git tracks only the studio-owned files inside the trees (see `.gitignore`). `RESTORE_INSTRUCTIONS.md` §3 says how to rebuild each tree at these pins.

**Paths.** The Python code in `tool/` resolves paths from the repository root (`Path(__file__).resolve().parent.parent`), and the batch launchers from their own folder (`%~dp0`). `tool/*.py` contains no hardcoded drive paths, and the tracked `tool/settings.example.yaml` uses relative paths. A local `tool/settings.yaml` may hold absolute paths, and weights outside the tree are configured in `paths.extra_model_dirs`.

**On this page**

- [1. Processes and ports](#1-processes-and-ports)
- [2. Model lineup](#2-model-lineup)
- [3. Production farm: `ComfyUI/` (port 8188)](#3-production-farm-comfyui-port-8188)
- [4. LTX / H3 farm: `ComfyUI_LTX/` (ports 8189 + 8199)](#4-ltx--h3-farm-comfyui_ltx-ports-8189--8199)
- [5. Qwen-Image-2.1 farm: `ComfyUI_Qwen21/` (port 8190)](#5-qwen-image-21-farm-comfyui_qwen21-port-8190)
- [6. Python runtimes](#6-python-runtimes)
- [7. Cloud worker (Colab T4): the Linux rebuild](#7-cloud-worker-colab-t4-the-linux-rebuild)

---

## 1. Processes and ports

`start.bat` → `tool/launcher.py` starts every row except the worker agent. Every ComfyUI instance also gets `--extra-model-paths-config temp/extra_model_paths.studio.yaml` when `paths.extra_model_dirs` lists an existing folder.

| Service | Host and port | Runtime and arguments | What it runs |
|---|---|---|---|
| **Control Room (FastAPI Core)** | settings `ui.host` (default `127.0.0.1`) `:8080` | base Python (`tools/python`, else the Python running the launcher) + `PYTHONPATH=tool;tool/.venv/Lib/site-packages`; `uvicorn api:app` | The Movie side (`/`, also `/project`), the Studio side (`/studio-tools`), REST APIs (`/api/...`, for example `/api/models`), orchestrator; SQLite databases in `tool/` |
| **Production Farm (ComfyUI)** | `127.0.0.1:8188` | ComfyUI v0.30.0; base Python + `PYTHONPATH=tool/.venv/Lib/site-packages`; `--default-device 0 --disable-auto-launch` | RealVisXL V5.0 (GGUF), CyberRealistic XL v10.0, Z-Image Turbo, the legacy Wan 2.2, face fix, post (RIFE, RealESRGAN, SeedVR2) |
| **LTX-Video 2.5 Farm** | `127.0.0.1:8189` | ComfyUI v0.36.0 (`ComfyUI_LTX/venv`); `--listen 127.0.0.1 --enable-dynamic-vram --bf16-vae --disable-auto-launch` | LTX-2.5 22B distilled W4A8 ConvRot DiT, MSR multi-reference LoRA (`ComfyUI-LTX2.5-MSR`), audio VAE |
| **MiniMax H3 Farm** | `127.0.0.1:8199` | ComfyUI v0.36.0 (same tree and venv as 8189); `--listen 127.0.0.1 --enable-dynamic-vram --database-url sqlite:///…/ComfyUI_LTX/user/comfyui_h3.db --disable-auto-launch` | MiniMax H3 W4A8 DiT (ref2va / fl2va / Singularity tiers; 33B base model, the pruned ref2va checkpoint holds ~20B logical parameters), fp16 video VAE; own SQLite database so the two instances of one tree do not share a file |
| **Qwen-Image-2.1 Farm** | `127.0.0.1:8190` | ComfyUI v0.37.4 (`ComfyUI_Qwen21/venv`); `--listen 127.0.0.1 --highvram --disable-auto-launch` | Qwen-Image-2.1 7.1B single-stream DiT (GGUF Q5_K_M): 1 MP base, finished to 2× (for example 2048×2048) RGBA PNG |
| **Worker Agent Daemon** | `0.0.0.0:8120` (default `agent_port`) on a worker PC | `tool/worker/worker_agent.py`; not started by `start.bat` | LAN workers (`install/INSTALL-WORKER.bat`: render-farm ComfyUI only) and the Colab worker (`colab/bootstrap.sh`, Tailscale) |

The launcher skips the LTX, H3 and Qwen rows with a message when their venv is missing or still marked unfinished, and any row this PC's graphics card or driver can't run (`tool/gpu_support.py`: the cu130 rows need a GTX 16- or RTX 20-series card or newer and R580, the farm a GTX 900- to RTX 40-series card and R560; the Qwen row is checked against the build its venv holds). It never falls back to another Python.

---

## 2. Model lineup

File names and folders: `RESTORE_INSTRUCTIONS.md` §3 Step 6.

- **MiniMax H3 (33B base; pruned W4A8 checkpoints, ~20B logical parameters in ref2va)**: W4A8 ConvRot DiT, in `models/diffusion_models/` (registry: `tool/video_models_user.json`).
- **LTX-Video 2.5 (22B)**: W4A8 ConvRot distilled DiT, in `models/diffusion_models/`; up to five reference images through `ComfyUI-LTX2.5-MSR`.
- **Qwen-Image-2.1 (7.1B)**: single-stream DiT for RGBA stills on its own instance (`tool/models.py` entries `qwen21`, `qwen21_fast`).
- **RealVisXL V5.0**: SDXL GGUF Q5_K_M for photoreal stills (DPM++ 2M Karras, 28 steps, CFG 6.5).
- **CyberRealistic XL v10.0**: native SDXL checkpoint (DPM++ 3M SDE Karras, 28 steps, CFG 7.5).
- **Z-Image Turbo**: 6B DiT (GGUF Q4_K_M), distilled to 8 steps at CFG 1.0 for fast stills.
- **Wan 2.2 (legacy, not recommended)**: the TI2V-5B and A14B entries in `tool/video_models.py` (render farm; the A14B entries are disabled), plus the Image Studio `wan` entry. It was the studio's first video model; the studio renders it only when a project or shot picks it.

---

## 3. Production farm: `ComfyUI/` (port 8188)
- ComfyUI core: **v0.30.0** (`ComfyUI/comfyui_version.py`). The installer downloads it at commit `b1693ec` (`install/components.json`). The golden machine's tree is not a git checkout, so no commit is recorded for it; `tool/versions.txt` keeps the historical 2026-08-06 pin (commit `2eb60976`), and whether that commit matches the golden machine's v0.30.0 tree is not recorded.
- Custom nodes: the installer installs 8 packs, each at the commit `install/components.json` pins (GGUF, IPAdapter_plus, Impact-Pack, Frame-Interpolation, SeedVR2_VideoUpscaler, VideoHelperSuite, KJNodes, WanVideoWrapper). The golden machine has 13 third-party packs installed without git (those 8 and Florence2, Manager, MultiGPU, PuLID and CFGStar). File-level provenance (size + sha256 per file) is in `install/manifests/comfyui_v030_custom_nodes.manifest.json`. No script checks the tree against it.
- No venv of its own: it imports its packages from `tool/.venv` through the launcher's `PYTHONPATH`.

---

## 4. LTX / H3 farm: `ComfyUI_LTX/` (ports 8189 + 8199)
- ComfyUI core: git clone of https://github.com/comfyanonymous/ComfyUI.git at commit **`ee71d5c`** ("ComfyUI v0.36.0", 2026-09-15).
- Local core patch (tracked): `comfy_extras/nodes_lt_audio.py`. In it, `LTXVEmptyLatentAudio` takes `frame_rate` as a plain float input instead of a MultiType, and casts `frames_number` / `frame_rate` / `batch_size` to numbers (the studio's template substitution delivers them as strings).
- Custom nodes:
  - `ComfyUI-LTX2.5-MSR` @ `9894117` (https://github.com/liconstudio/ComfyUI-LTX2.5-MSR.git, clean clone, unmodified).
  - `Comfyui_Minimax_h3_latent_Upscaler` @ `40316cf` (https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler; tracked local modification: `nodes/MMH3_Split_Upscale.py`).
  - `ComfyUI-LTX25-Studio`: studio-owned node pack (tracked). It holds `LTX25EvictModels`, the pre-decode eviction that works around the Windows access-violation crash of ComfyUI issue #15606 (every `tool/workflows/ltx25_*.json` graph uses it, with `keep_text_encoder: true`), and `LTX25ZeroAudioLatent` (used by `ltx25_msr_dfr_s6_cq_chain`).

---

## 5. Qwen-Image-2.1 farm: `ComfyUI_Qwen21/` (port 8190)
- ComfyUI core: **v0.37.4** (`ComfyUI_Qwen21/comfyui_version.py`). The installer downloads it at commit `8ff6dc3` (`install/components.json`); the golden machine's tree is not a git checkout, so no commit is recorded for it.
- Launch arguments: `--highvram`. Per the comment in `tool/launcher.py`: without it, the Q5_K DiT kept falling into ComfyUI-GGUF cast mode (per-module CPU dequantization, 20–100 s/step). `--highvram` disables DynamicVRAM, so the model materializes on the card. NVML memory pressure stays on: disabling it made the run overcommit the card and thrash (60–104 s/step).
- Custom nodes:
  - `ComfyUI-GGUF` @ `6ea2651` (https://github.com/city96/ComfyUI-GGUF.git, clean clone, unmodified).
  - `ComfyUI-Qwen21-Optimized`: studio-owned node pack (tracked). The studio graph (`tool/workflows/qwen21_t2i.json`) uses `TextEncodeQwenImage21CPU`, `EmptyQwen21LatentImage`, `SplitImageRGBA` and `MergeImageRGBA`. The pack also has `LatentTiledRefiner` (72-token windows with 16-token overlap: a 2×2 grid on a 128×128 latent, 2·72 − 16 = 128; an axis no longer than the window is one window), `VAEDecodeTiledPremulAlpha` (premultiplied-alpha tile blending) and `PromptEnhancerHostPE`. The studio graph uses none of these three.

---

## 6. Python runtimes
- **Python**: 3.12 in every runtime (the golden machine's older `tools/python` is 3.12.10). The pinned numpy/scipy need 3.12. On a new PC, `install/bootstrap.ps1` (run by `INSTALL.bat`) puts Python 3.12.15 from python-build-standalone 20261009 in `tools/python`, pinned by URL, size and SHA-256 in `install/components.json` (`tools.python`); python.org has no Windows build of 3.12 after 3.12.10.
- **Git**: pip needs `git.exe` for the two git requirements of `tool/requirements.txt`. When the PC has none, `install/bootstrap.ps1` puts MinGit 2.56.0.windows.3 in `tools/git` (`tools.git`).
- **pip**: 26.2.2 in every venv the installer builds (`install/install_support.py` `PIP`, the pin in `install/requirements-frozen/base-python.txt`). A new venv comes with an older pip, which the installer replaces first.
- **Studio core backend**: `tool/.venv` → PyTorch 2.13.0+cu126, installed from `tool/requirements.txt` (which names the cu126 index itself); snapshot in `install/requirements-frozen/tool-venv.txt`. The control room and the production farm both import from it. SAM-2, the line marked `[second-pass]`, is installed after the rest and built inside the venv against that torch (`install/install_support.py`), not in a build environment that would fetch a torch of its own.
- **LTX / H3 runtime**: `ComfyUI_LTX/venv` → PyTorch 2.13.0+cu130 (torchvision 0.28.0+cu130, torchaudio 2.11.0+cu130, comfy-kitchen 0.2.34, comfy-aimdo 0.5.3); frozen in `install/requirements-frozen/ltx-venv.txt` (install with `--extra-index-url https://download.pytorch.org/whl/cu130`).
- **Qwen21 runtime**: `ComfyUI_Qwen21/venv` → PyTorch 2.13.0+cu130 (torchvision 0.28.0+cu130, torchaudio 2.11.0+cu130, comfy-kitchen 0.2.35, comfy-aimdo 0.5.5, gguf 0.19.0); frozen in `install/requirements-frozen/qwen21-venv.txt` (install with `--extra-index-url https://download.pytorch.org/whl/cu130`). Its torch is built for `sm_75` to `sm_120` (GTX 16- and RTX 20- to 50-series; it has run on an RTX 3070 only), and comfy-kitchen's CUDA kernels are on (ComfyUI turns them off below cu130). On a card or driver that runs only cu126 (a GTX 900 or 10 card, a driver older than R580), the installer builds it from `install/requirements-frozen/qwen21-venv-cu126.txt` instead: the same pins with torch 2.13.0+cu126 and torchvision 0.28.0+cu126 (`--extra-index-url https://download.pytorch.org/whl/cu126`), the list 0.1.0 installed everywhere.
- The two ComfyUI venv lists were derived from the installed `*.dist-info` folders, because pip freeze was not available in those venvs.
- **Base embedded Python**: `tools/python` → pip and PyYAML on the golden machine (`install/requirements-frozen/base-python.txt`); a new install's has pip only. It runs the launcher, control room and production farm, which get their packages from `tool/.venv` via `PYTHONPATH`; the launcher itself reads its settings with `tool/.venv`'s PyYAML when the base Python has none. `tools/` is not in git.
- **Research and videolab runtime**: PyTorch 2.13.0+cu130 (`videolab/README.md`).
- **NVIDIA driver and card**: R560 or newer for the cu126 runtimes (a GTX 900- to RTX 40-series card: their torch holds kernels for `sm_50` to `sm_90`), R580 or newer and a GTX 16- or RTX 20-series card or newer for the cu130 LTX/H3 and Qwen21 runtimes (`sm_75` to `sm_120`); tested on 616.64 (`install/INSTALL-MAIN.bat`). The 581.57 pin in `tool/versions.txt` is historical.
- **FFmpeg**: `tools/ffmpeg` (9.0 essentials build, `tool/versions.txt`), or settings `paths.ffmpeg`, or `ffmpeg` on PATH. Drafts try NVENC and fall back to libx264.

---

## 7. Cloud worker (Colab T4): the Linux rebuild
`colab/bootstrap.sh` rebuilds the LTX/H3 rendering node on Linux from the same pins. If you change a pin here, mirror the change there:
- ComfyUI core: git clone, checked out at **`ee71d5c`** (the script runs with `set -e`, so a failed checkout stops it).
- `Comfyui_Minimax_h3_latent_Upscaler` @ **`40316cf`** and `ComfyUI-LTX2.5-MSR` @ **`9894117`**: cloned and checked out, but a failed checkout is ignored (`|| true`), so check the commits after a run.
- Copied from the repo (best effort): `MMH3_Split_Upscale.py`, `ComfyUI-LTX25-Studio`, `comfy_extras/nodes_lt_audio.py`.
- Python: `venv --system-site-packages` inherits Colab's PyTorch (not pinned). ComfyUI's `requirements.txt`, `gguf`, `pyyaml`, `comfy-kitchen` and the custom nodes' requirements are installed on top.
- Model distribution: `colab/fetch_models.py` downloads the groups in `install/colab_model_groups.json` into `models/<subdir>/<file>`. The expected size is checked when an entry's `bytes` is set (non-zero). Every `sha256` field in that file is empty, so no hash is verified there (the installer checks the one `install/components.json` gives every file it downloads).
