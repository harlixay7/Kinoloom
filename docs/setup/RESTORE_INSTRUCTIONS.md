[Kinoloom](../../README.md) › [Docs](../README.md) › Setup and recovery

# Setup and disaster recovery

> **In short:** This guide is for installing Kinoloom on a Windows PC, or rebuilding it after a disk or machine is lost. The ComfyUI trees, their Python environments and the model weights are not in git. `INSTALL.bat` installs everything but the weights in one go, and `install\INSTALL-MAIN.bat` installs the parts you choose, weights included (Step 2). Steps 3–7 describe what it does, as manual steps you can follow instead, and Step 8 runs the read-only checks.

The Python code finds its paths from the repository root (`Path(__file__).resolve().parent.parent`), and the batch files from their own folder (`%~dp0`). So the tree can live on any drive or in any folder. Two things stay specific to one machine: `tool/settings.yaml` (created from `tool/settings.example.yaml`; a local copy may hold absolute paths) and the model roots listed in `paths.extra_model_dirs`.

A fresh git clone is **not** a runnable studio until the installer has run. The three ComfyUI trees are mostly untracked: only the studio's own files inside them are in git (see `.gitignore`). Their venvs (Python virtual environments, the folders that hold each program's packages), `tools/` and all model weights are untracked too. Section 3 says how to get each piece.

**On this page**

- [1. Folder overview](#1-folder-overview)
- [2. What you need first](#2-what-you-need-first)
- [3. Install or restore, step by step](#3-install-or-restore-step-by-step)
  - [Step 1: Get the tree](#step-1-get-the-tree)
  - [Step 2: Control room](#step-2-control-room-toolvenv-with-installinstall-mainbat)
  - [Step 3: Render farm](#step-3-render-farm-comfyui-port-8188)
  - [Step 4: LTX-2.5 and MiniMax H3 tree](#step-4-ltx-25-and-minimax-h3-tree-comfyui_ltx-ports-8189--8199)
  - [Step 5: Qwen-Image 2.1 tree](#step-5-qwen-image-21-tree-comfyui_qwen21-port-8190)
  - [Step 6: Model weights](#step-6-model-weights)
  - [Step 7: Weights you already have, and weights outside the tree](#step-7-weights-you-already-have-and-weights-outside-the-tree-pathsextra_model_dirs)
  - [Step 8: Verify the install](#step-8-verify-with-installverify_installpy)
- [4. Starting the studio](#4-starting-the-studio)
- [5. Web interface and ports](#5-web-interface-and-ports)

---

## 1. Folder overview

This is the tree after a full install. The folders marked untracked or "not in git" do not come with a clone.

```text
Kinoloom/
├── INSTALL.bat                # One-click install of everything but the model weights (Step 2)
├── start.bat                  # Single-window launcher (Ctrl+C or closing the window stops every component)
├── tool/                      # FastAPI control room (api.py + routers/), orchestrator, web UI
│   ├── settings.example.yaml  # Tracked template, copied to settings.yaml (gitignored) on first start
│   ├── studio.db              # Shots, scene state, projects, story versions (created on first start)
│   ├── queue.db               # Task queue, worker registry, shot assignments (created on first start)
│   └── datasets.db            # Training-image provenance & generation runs (created on first start)
├── install/                   # INSTALL-MAIN.bat, bootstrap.ps1, INSTALL-WORKER.bat, verify_install.py, frozen requirement lists
├── ComfyUI/                   # Render farm, port 8188 (ComfyUI v0.30.0)            [untracked except studio files]
├── ComfyUI_LTX/               # Port 8189 LTX-2.5 + port 8199 MiniMax H3 (v0.36.0)   [untracked except studio files]
├── ComfyUI_Qwen21/            # Port 8190 Qwen-Image 2.1 (v0.37.4)                   [untracked except studio files]
├── videolab/                  # Research lab: benchmarks, the model downloader, archived measurements
├── colab/                     # Colab worker bootstrap (Tailscale)
├── docs/                      # Architecture, pipeline, showcase, setup guides (setup/), research notes (research/)
├── models/                    # Model weights                                       [not in git]
├── bible/ roster/ shots/ output/                                                    [not in git]
└── tools/                     # Portable runtimes:                                   [not in git]
                               #   python (3.12.10 on the golden machine; INSTALL.bat installs 3.12.15),
                               #   git (Git for Windows 2.55.0 on the golden machine; INSTALL.bat
                               #   installs MinGit only when there is no git.exe on PATH), ffmpeg;
                               #   on the golden machine also sd-scripts and ai-toolkit
```

---

## 2. What you need first

### Hardware

- **GPU**: NVIDIA GeForce RTX 3070 8 GB GDDR6. It is the only card tested so far. LTX-2.5 and MiniMax H3 run on a CUDA 13.0 build of PyTorch, which needs a GTX 16- or RTX 20-series card or newer (compute capability 7.5 and up). The render farm's CUDA 12.6 build runs on a GTX 900- to RTX 40-series card (5.0 to 9.0; it has no kernels for the RTX 50 series). Qwen-Image 2.1 runs on CUDA 13.0, or on CUDA 12.6 where the card or driver runs only that one: the installer picks the build (`install/components.json`, the Qwen venv's `fallback`). `tool/gpu_support.py` reads the card from `nvidia-smi`: a model this PC can't run is listed as not on this graphics card, and the launcher does not start its ComfyUI instance.
- **CPU**: Intel Core i9-10980XE (18 cores / 36 threads) on the tested machine. The launcher sets `OMP_NUM_THREADS` to the CPU's thread count for the render farm (`tool/launcher.py`).
- **System RAM**: 64 GB on the tested machine. The video models keep large parts in host RAM (MiniMax H3: 14.62 GiB text encoder + 11.68 GiB DiT; LTX-2.5: 9.88 GiB text encoder + 11.66 GiB DiT, per `videolab/README.md`). Smaller amounts are untested.
- **VRAM** (the graphics card's own memory): 8 GB is the tested size. The video DiTs (LTX-2.5 22B; MiniMax H3, ~20B logical parameters in the pruned W4A8 ref2va checkpoint of a 33B base model) do not fit in it. They run by streaming weights from host RAM (ComfyUI DynamicVRAM), and `tool/gpu_broker.py` lets only one local render use the GPU at a time. Some configurations still do not fit, for example the 243-frame 1080p H3 refine. See `docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md`.
- **Storage**: the golden machine's `models/` + `external_loras/` are roughly 150 GB (`install/make_manifest.py`). (The golden machine is the reference install that other copies are made from.) The videolab notes say the archived benchmarks staged weights from a SATA SSD. On the tested machine, the H3 and LTX lab model folders sit on a SATA hard disk ([VIDEOLAB.md](../research/VIDEOLAB.md), hardware table).

### Software

1. **NVIDIA driver**: R560 or newer for the CUDA 12.6 runtimes (studio venv, render farm). R580 or newer, and a GTX 16- or RTX 20-series card or newer, for the CUDA 13.0 runtimes in `ComfyUI_LTX/venv` (LTX-2.5 / MiniMax H3) and `ComfyUI_Qwen21/venv` (Qwen-Image 2.1; on a card or driver that runs only CUDA 12.6, the installer builds it from `install/requirements-frozen/qwen21-venv-cu126.txt` instead). Tested on 616.64. The 581.57 pin in `tool/versions.txt` is historical.
2. **Operating system**: Windows 11 64-bit, the only version tested (Windows 10 may work; the installer needs version 1803 or later for its `tar.exe`, and on x64 only). The launcher uses Win32 APIs (Job Objects, console control handler) and does not run anywhere else.
3. **Git**: `git.exe` on PATH, or `tools\git\cmd\git.exe`. `tool/requirements.txt` installs kohya sd-scripts and SAM-2 from git, and pip starts `git.exe` itself (a `git.cmd` wrapper does not count). When there is none, `install\bootstrap.ps1` (run by `INSTALL.bat` and by `INSTALL-MAIN.bat` mode [1]) puts MinGit 2.56.0.windows.3 in `tools\git`. The manual steps below also use git to clone the ComfyUI trees.
4. **Python**: 3.12 64-bit, exactly; the pinned numpy/scipy wheels need 3.12. `install\bootstrap.ps1` puts Python 3.12.15 (python-build-standalone 20261009) in `tools\python` unless `tool\.venv` already exists, and every venv is made from it. A Python installed on the PC is not used. python.org has no Windows build of 3.12 after 3.12.10, which is what the golden machine's `tools\python` holds.
5. **Microsoft Visual C++ Redistributable (x64)**, 14.40 or newer: torch needs it (binaries built with Visual Studio 2022 17.10 or later crash on an older `msvcp140.dll`). When it is missing or older, `install\bootstrap.ps1` offers to install the current one from Microsoft (`https://aka.ms/vc14/vc_redist.x64.exe`). It runs that installer only with a valid Microsoft Corporation signature, and Windows asks for administrator rights.
6. **ffmpeg**: the studio uses settings `paths.ffmpeg` (default `tools\ffmpeg\bin\ffmpeg.exe`), else `tools\ffmpeg\bin\ffmpeg.exe`, else `ffmpeg` on PATH (`tool/core.py` `ffmpeg_exe`). The golden machine uses the 9.0 "essentials" build (`tool/versions.txt`). Post and the assembler try NVENC (the NVIDIA hardware encoder) and fall back to libx264.
7. **An AI writer** (optional; it writes the story and the movie's video prompts): any server that speaks the OpenAI-compatible `/chat/completions` API, in the cloud with a key or on this PC ([Ollama](https://ollama.com/), LM Studio). None is set up after the install (`architect.base_url` and `architect.model` are empty in `tool\settings.example.yaml`): pick a provider under AI writer (the System menu, or Settings), or paste the same prompts into any chat AI. The Image and Video Studio pages do not use it.

---

## 3. Install or restore, step by step

The commands below are for an interactive `cmd.exe` window (Command Prompt). First set `AIS` to your studio root (the path is an example):

```cmd
set "AIS=C:\Kinoloom"
```

### Step 1: Get the tree

You have two ways to do this.

**Option A: clone it** with git, then follow Steps 2–8:

```cmd
git clone https://github.com/harlixay7/Kinoloom.git "%AIS%"
```

`videolab/` is a plain folder in the repository, so the clone includes it.

Keep `%AIS%` short, like `C:\Kinoloom`. In a full install the deepest file inside a venv sits 211 characters below the studio root, and Windows stops at 260 characters unless [long paths are enabled](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation). With a root longer than about 45 characters and long paths off, `pip install` fails partway; `install\bootstrap.ps1` then offers to turn long paths on (`LongPathsEnabled`, administrator rights) and stops if you decline.

**Option B: mirror** a complete working tree from another PC with `install\INSTALL-MAIN.bat` mode [2] (Step 2).

Either way, no particular drive letter or NTFS junction is required.

### Step 2: Control room (`tool\.venv`) with `install\INSTALL-MAIN.bat`

**`INSTALL.bat`** in the studio root does all of mode [1] below in one go. After its first key press it asks only before it installs the Visual C++ runtime, before it turns on long paths for a long folder path, before it installs into a folder OneDrive syncs, and at the end whether to start Kinoloom (in the same window). It runs `install\INSTALL-MAIN.bat /auto`, which is mode [1] in this tree with every part's runtime and no weights (`install_components.py --components all --skip-models --yes`). Exit code 2 means some parts did not install; run it again. Run from inside the ZIP (where Windows unpacks only that one file), it says to extract the ZIP first. In a network folder (`\\server\share`), `INSTALL.bat` and `start.bat` stop before anything runs, since cmd can't make one its current folder and both find the next script relative to it.

`start.bat` starts Kinoloom only when `tool\.venv` is finished: `tool\.venv\Scripts\python.exe` exists and starts, and `tool\.venv` no longer holds the `.kinoloom-incomplete` marker. Otherwise it shows `logs\install\LAST-FAILURE.txt`, when there is one, and offers to run `INSTALL.bat`. The launcher checks the same marker, and skips a ComfyUI instance whose venv still has it. A part that failed to install does not stop `start.bat`: Kinoloom starts with what is installed.

Every run of the installer is logged in `logs\install\install.log`: the PC (Windows edition and build, processor, free space, NVIDIA driver), the steps with their times, everything `install_components.py` and `verify_install.py` print, and each failure. When `bootstrap.ps1` stops without its own report (Windows PowerShell did not run it to the end, for example because a policy of the PC allows no scripts, or only signed ones), the log gets PowerShell's execution policies and language mode. pip's own verbose logs sit beside it (`pip-tool-venv.log`, `pip-ComfyUI_LTX.log`, `pip-ComfyUI_Qwen21.log`; the run before is kept as `*.prev.log`), without pip's line for every file it saw on a package index page, which would make them tens of MB each. When a step fails, the window shows the step, what happened (a download that timed out or was refused, a server the PC could not look up, a certificate check that failed, a full disk, a file an antivirus blocked or held open, a path that got too long, an archive that did not unpack, a package that did not build), what to do, and the lines of the log that show it. The same report is saved as `logs\install\LAST-FAILURE.txt`; a new run deletes it when it starts. Both files are UTF-8: `INSTALL-MAIN.bat` switches its window to UTF-8 (code page 65001) after its last question and switches it back when it ends.

Or run `install\INSTALL-MAIN.bat` and choose a mode. A folder pasted in quotes (Explorer's "Copy as path") works at its prompts:

- **[1] Set up the control room in this tree** (a git clone). This mode:
  - runs `install\bootstrap.ps1` (Section 2): Python 3.12 in `tools\python` unless a finished `tool\.venv` exists, MinGit in `tools\git` when there is no `git.exe` on PATH, the Visual C++ runtime check, and the long-path check above. Before a first install it also checks the free space on the studio folder's drive, and asks before it installs into a folder OneDrive syncs (OneDrive would upload the install and can hold files open while pip writes them). A folder path with `[` or `]` stops it before anything is installed: Windows PowerShell and parts of Kinoloom read them as wildcards. tar.exe unpacks Python and MinGit with paths relative to `temp\bootstrap`, because it reads its arguments in the ANSI code page and could not open a folder whose name has a letter outside it. Python and MinGit are pinned by URL, size and SHA-256 in `install\components.json` (`tools`), and a download that doesn't match is deleted. curl retries a download 6 times and gives up on one that stalls for 2 minutes; then Windows' own downloader, which uses the Windows proxy settings, tries once more. Moving the unpacked folder into place is tried 5 times, 3 s apart, since an antivirus can hold a file it is scanning. An error nobody planned for, a damaged `install\components.json` among them, ends in the same report;
  - clears `PYTHONPATH` and `PYTHONHOME` for its own run, so packages from elsewhere on the PC don't leak into the venvs;
  - creates `tool\.venv` from `tools\python` if it is missing, with the `.kinoloom-incomplete` marker in it first. A `tool\.venv` that still has the marker, has no `Scripts\python.exe`, or whose python no longer starts (its `pyvenv.cfg` names the Python of a folder that has moved) is deleted (marker last, so a removal that stops part-way leaves it marked) and created again;
  - fills it with `install\install_support.py`:
    - **Packages:** `pip==26.2.2` first, then `tool\requirements.txt` (torch 2.13.0+cu126 comes from the cu126 index named inside that file) with `SAM2_BUILD_CUDA=0`, because SAM-2's optional CUDA extension needs the CUDA toolkit and a C++ compiler. SAM-2, the one line marked `[second-pass]`, is installed last with `--no-build-isolation --no-deps`, built inside the venv against that torch: in a build environment of its own, pip would download and unpack a second, newer torch for it.
    - **Import check:** `uvicorn`, `fastapi`, `yaml`, `psutil`, `numpy`, `PIL`, `cv2` and `torch` must import, which catches a file an antivirus removed or a DLL Windows cannot load.
    - **Network:** pip runs with a 60 s timeout and 10 retries (`PIP_TIMEOUT`, `PIP_RETRIES`, `PIP_RESUME_RETRIES`; a value set on the PC wins) and never stops to ask for input. A pip run that fails on a network error (a download that timed out, broke off or was refused) is run once more after 30 s. With 10 retries, pip takes about 4 to 5 minutes to give up on a server it cannot reach at all, so with the second run about 10 minutes before the report. pip reads the proxy in the Windows settings itself, with its bypass list; git, which pip runs for the two packages from git, does not, so when the PC sets no `HTTP_PROXY`/`HTTPS_PROXY` and git has no proxy of its own, the helper gives git that proxy as its `http.proxy` setting for the install (`GIT_CONFIG_*`).
    - **Downloads:** pip keeps its downloads in `temp\pip-cache` and its temporary files in `temp\pip-tmp` on the studio folder's drive. A finished install deletes both; after a failure they stay, so the next run does not download again.
    - **Finished:** the marker is deleted once all of this went well, and `tool\.venv\.kinoloom-installed` records the SHA-256 of `tool\requirements.txt` (with the pip pin) it was finished from. A later run whose list has not changed only runs the import check, so it needs no network. A run that cannot reach a server leaves a venv that worked as it was, without the marker, since pip downloads everything before it changes anything;
  - creates `temp`, `logs`, `output`, `shots`, `models` and `external_loras\VideoGeneration`;
  - copies `tool\settings.example.yaml` to `tool\settings.yaml` unless that file exists;
  - writes the colour-grade LUT `bible\luts\cinematic_teal_amber.cube` with `tool\lutgen.py` unless it exists (post skips the grade without it, and the install says so);
  - copies `install\overlay\progress_relay.html`, the live-progress page, into the `comfyui_frontend_package\static\` folder of `tool\.venv` (see `install\overlay\README.md`);
  - runs `install\install_components.py`, which lists the parts (LTX-2.5, MiniMax H3, Qwen-Image 2.1, post-processing, SeedVR2, Z-Image Turbo, CyberRealistic XL, RealVisXL, the legacy Wan 2.2) and installs the ones you choose: Steps 3–6 for each part, and ffmpeg 9.0 into `tools\ffmpeg` when there is none on PATH. Enter alone installs LTX-2.5, Qwen-Image 2.1 and post-processing.

  Every download is pinned in `install\components.json`: each ComfyUI release and node pack by git commit, each weight file by its source repositories, size and SHA-256, and ffmpeg by size and SHA-256. A weight that doesn't match is deleted and reported. Files that already exist are never overwritten, so the files git tracks inside the ComfyUI trees stay as they are, and a re-run skips finished steps. A venv that pip did not finish (the run stopped part-way: the `.kinoloom-incomplete` marker is still in it, a package lacks its `RECORD`, or a pin of its list is not installed; a pin without a local label such as `+cu126` matches the build with one) or that was made in another folder (the Python its `pyvenv.cfg` names is gone) is deleted and built again from its pinned list; `tool\.venv` gets the same marker from mode [1]. MiniMax H3 and RealVisXL weights are never downloaded; the installer lists them with the full path each one goes to (Step 6).
- **[3] Add parts to an installed studio, or check what is installed.** It runs the same `install\install_components.py` with the existing `tool\.venv`. On the command line: `--list`, `--components ltx25,h3`, `--components all --dry-run`, `--check` (read-only), `--use-folder <folder>` (weights you already have, Step 7), `--skip-models`.
- **[2] Mirror a golden tree** from a path or share. It uses `robocopy /E`, which never deletes anything; re-run it to resume. It skips folders named `temp`, `output`, `logs`, `__pycache__`. This brings the ComfyUI trees, venvs and models too, and also `tool\settings.yaml` with its API and fleet tokens. Delete those two values to have new ones generated on the next start. A venv records the absolute path of the Python it was made from (`pyvenv.cfg`, `home =`). If that Python is not at the same path on the new PC, recreate the venv with the commands in Steps 2, 4 and 5.

When it finishes, every mode runs `install\verify_install.py --quick` (mode [2] also runs the full check, see Step 8), with its output in `install.log` too. They also print your installed driver version next to the minimum driver versions listed above, and `tool\gpu_support.py` names the card, its compute capability and the driver, with what this PC can't run and why. When a part did not install, modes [1] and [3] end with NOT FINISHED instead of DONE (exit code 2).

The manual equivalent of mode [1], up to the parts menu, is:

```cmd
cd /d "%AIS%"
set PYTHONPATH=
set PYTHONHOME=
powershell -NoProfile -ExecutionPolicy Bypass -File install\bootstrap.ps1
if exist tools\git\cmd\git.exe set "PATH=%AIS%\tools\git\cmd;%PATH%"
tools\python\python.exe -m venv tool\.venv
tool\.venv\Scripts\python.exe -m pip install pip==26.2.2
set SAM2_BUILD_CUDA=0
findstr /v /l /c:"[second-pass]" tool\requirements.txt > "%TEMP%\kinoloom-first-pass.txt"
tool\.venv\Scripts\python.exe -m pip install -r "%TEMP%\kinoloom-first-pass.txt"
tool\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps "SAM-2 @ git+https://github.com/facebookresearch/sam2@2b90b9f5ceec907a1c18123530e92e794ad901a4"
if not exist tool\settings.yaml copy tool\settings.example.yaml tool\settings.yaml
tool\.venv\Scripts\python.exe tool\lutgen.py
copy /y install\overlay\progress_relay.html tool\.venv\Lib\site-packages\comfyui_frontend_package\static\
```

`pip install -r tool\requirements.txt` in one pass works too; SAM-2 then builds in an isolated environment that downloads a torch of its own.

`install\requirements-frozen\tool-venv.txt` is a snapshot of the golden `tool\.venv`. It differs from `tool\requirements.txt` in a few entries (wheel, dill, the torchaudio local tag, and einops: 0.7.0 in the snapshot, 0.8.2 in `tool\requirements.txt`, because rotary-embedding-torch 0.9.1 needs einops 0.8 or newer and pip refuses the pair on a fresh install). The installer uses `tool\requirements.txt`.

### Step 3: Render farm, `ComfyUI\` (port 8188)

The installer does this step for the parts that run on the render farm (post-processing, SeedVR2, Z-Image Turbo, CyberRealistic XL, RealVisXL, Wan 2.2). By hand:

- **Core**: ComfyUI **v0.30.0** (`ComfyUI\comfyui_version.py`; the installer uses its commit `b1693ec`). The golden tree is not a git checkout (`tool/versions.txt` holds an older historical pin, commit `2eb60976` from 2026-08-06). Copy the upstream `v0.30.0` tag in without overwriting the files git tracks there (`extra_model_paths.yaml`, and the patched Impact-Pack file below):

  ```cmd
  cd /d "%AIS%"
  git clone https://github.com/comfyanonymous/ComfyUI.git "%TEMP%\comfy_farm"
  git -C "%TEMP%\comfy_farm" checkout v0.30.0
  robocopy "%TEMP%\comfy_farm" ComfyUI /E /XC /XN /XO /XD .git
  ```

  Then check that `comfyui_version.py` reads `0.30.0`.
- **Custom nodes**: the golden tree has 13 third-party packs, and the studio's graphs use 8 of them. Clone each one into `ComfyUI\custom_nodes\<folder>` and check out its commit; a checkout with `core.autocrlf=true` reproduces the golden files byte for byte, and the installer's GitHub zip of the same commit differs from them only in line endings. Version is what each pack's `pyproject.toml` records. Other versions are untested.

  | Folder | Source | Commit | Version | Used by |
  |---|---|---|---|---|
  | `ComfyUI-GGUF` | https://github.com/city96/ComfyUI-GGUF | `6ea2651` | 2.0.0 | Z-Image, RealVisXL, Wan 2.2 |
  | `ComfyUI_IPAdapter_plus` | https://github.com/cubiq/ComfyUI_IPAdapter_plus | `a0f451a` | 2.0.0 | SDXL reference images |
  | `ComfyUI-Impact-Pack` | https://github.com/ltdrdata/ComfyUI-Impact-Pack | `429d015` | 8.28.3 | face detection (face fix, cast import, dataset curation) |
  | `ComfyUI-Frame-Interpolation` | https://github.com/Fannovel16/ComfyUI-Frame-Interpolation | `26545cc` | 1.0.11 | post RIFE |
  | `ComfyUI-SeedVR2_VideoUpscaler` | https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler | `4490bd1` | 2.5.24 | SeedVR2 upscale |
  | `ComfyUI-VideoHelperSuite` | https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite | `4ee72c0` | 1.7.9 | post (RIFE, Real-ESRGAN, SeedVR2), face fix, Wan 2.2 V2V |
  | `ComfyUI-KJNodes` | https://github.com/kijai/ComfyUI-KJNodes | `8692bc8` | 1.4.9 | Wan 2.2 (TeaCache) |
  | `ComfyUI-WanVideoWrapper` | https://github.com/kijai/ComfyUI-WanVideoWrapper | `088128b` | 1.4.7 | Wan 2.2 V2V refine |
  | `ComfyUI-Florence2` | https://github.com/kijai/ComfyUI-Florence2 | `9ece3de` | 1.1.0 | no studio graph (not installed) |
  | `ComfyUI-Manager` | https://github.com/Comfy-Org/ComfyUI-Manager | `d47c934` | 3.41 | no studio graph (not installed) |
  | `ComfyUI-MultiGPU` | https://github.com/pollockjj/ComfyUI-MultiGPU | `b51c99a` | 2.6.4 | no studio graph (not installed); the golden copy has one local change |
  | `ComfyUI-PuLID` | https://github.com/yashikawa3/ComfyUI-PuLID | `c672e2a` | not recorded | no studio graph (not installed); ships no licence file |
  | `ComfyUI_CFGStar` | https://github.com/bvhari/ComfyUI_CFGStar | `37c0b2d` | 1.1.0 | no studio graph (not installed) |

  The installer installs only the packs your parts need, at these commits (full hashes in `install\components.json`). One Impact-Pack file is patched and tracked in git: `modules\impact\impact_onnx.py` also reads a standard Ultralytics ONNX export, which the face detector is (its header says what changed; GPL-3.0, with the pack's `LICENSE.txt`). `install\manifests\comfyui_v030_custom_nodes.manifest.json` records the size and sha256 of every file the golden packs had when it was recorded, so you can compare by hand. No script checks it. Their licences are listed in [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md), section 2.
- **No venv of its own**: the launcher runs it with `tools\python` (or the Python that runs the launcher) and `PYTHONPATH=tool\.venv\Lib\site-packages`. So its packages come from `tool\requirements.txt`, and the node packs' own `requirements.txt` files are not installed separately.

### Step 4: LTX-2.5 and MiniMax H3 tree, `ComfyUI_LTX\` (ports 8189 + 8199)

The installer does this step for the LTX-2.5 and MiniMax H3 parts. By hand: these files are tracked in this repo. Do not overwrite them: `extra_model_paths.yaml`, `comfy_extras\nodes_lt_audio.py` (local patch), `custom_nodes\ComfyUI-LTX25-Studio\`, `custom_nodes\Comfyui_Minimax_h3_latent_Upscaler\nodes\MMH3_Split_Upscale.py` (local patch). `robocopy … /XC /XN /XO` copies only files that do not exist yet, so the tracked files survive.

**Core and custom nodes.** Run:

```cmd
cd /d "%AIS%"
git clone https://github.com/comfyanonymous/ComfyUI.git "%TEMP%\comfy_ltx"
git -C "%TEMP%\comfy_ltx" checkout ee71d5c
robocopy "%TEMP%\comfy_ltx" ComfyUI_LTX /E /XC /XN /XO /XD .git

git clone https://github.com/liconstudio/ComfyUI-LTX2.5-MSR.git ComfyUI_LTX\custom_nodes\ComfyUI-LTX2.5-MSR
git -C ComfyUI_LTX\custom_nodes\ComfyUI-LTX2.5-MSR checkout 9894117

git clone https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler "%TEMP%\mmh3"
git -C "%TEMP%\mmh3" checkout 40316cf
robocopy "%TEMP%\mmh3" ComfyUI_LTX\custom_nodes\Comfyui_Minimax_h3_latent_Upscaler /E /XC /XN /XO /XD .git
```

`ee71d5c` is the upstream `v0.36.0` tag. robocopy exit codes below 8 mean success. `/XD .git` keeps the upstream history out of the studio tree, so git does not treat these folders as nested repositories.

**venv**: `ComfyUI_LTX\venv`, torch **2.13.0+cu130**. `install\requirements-frozen\ltx-venv.txt` pins `+cu130` wheels but names no index, so pass the CUDA 13.0 index:

```cmd
cd /d "%AIS%\ComfyUI_LTX"
..\tools\python\python.exe -m venv venv
venv\Scripts\python.exe -m pip install pip==26.2.2
venv\Scripts\python.exe -m pip install -r ..\install\requirements-frozen\ltx-venv.txt --extra-index-url https://download.pytorch.org/whl/cu130
copy /y ..\install\overlay\progress_relay.html venv\Lib\site-packages\comfyui_frontend_package\static\
```

The last line deploys the live-progress page for the LTX-2.5 and H3 instances (`install\overlay\README.md`).

The frozen list was made from the golden venv's `*.dist-info` folders, because pip freeze was not available there. If a pin does not resolve when you install by hand, `ComfyUI_LTX\requirements.txt` with the same index is the unpinned alternative. The installer never falls back to it: a venv whose pinned list fails stays unfinished, and the next run builds it again. Without `ComfyUI_LTX\venv\Scripts\python.exe` the launcher skips the LTX and H3 instances with a message. It never falls back to another Python.

### Step 5: Qwen-Image 2.1 tree, `ComfyUI_Qwen21\` (port 8190)

The installer does this step for the Qwen-Image 2.1 part. By hand: these files are tracked in this repo. Do not overwrite them: `extra_model_paths.yaml`, `custom_nodes\ComfyUI-Qwen21-Optimized\`.

- **Core**: ComfyUI **v0.37.4** (`ComfyUI_Qwen21\comfyui_version.py`). The golden tree is not a git checkout. Copy the upstream `v0.37.4` tag in without overwriting the tracked files:

  ```cmd
  cd /d "%AIS%"
  git clone https://github.com/comfyanonymous/ComfyUI.git "%TEMP%\comfy_qwen"
  git -C "%TEMP%\comfy_qwen" checkout v0.37.4
  robocopy "%TEMP%\comfy_qwen" ComfyUI_Qwen21 /E /XC /XN /XO /XD .git
  ```

  Then check that `comfyui_version.py` reads `0.37.4`.
- **Custom node**:

  ```cmd
  cd /d "%AIS%"
  git clone https://github.com/city96/ComfyUI-GGUF.git ComfyUI_Qwen21\custom_nodes\ComfyUI-GGUF
  git -C ComfyUI_Qwen21\custom_nodes\ComfyUI-GGUF checkout 6ea2651
  ```

- **venv**: `ComfyUI_Qwen21\venv`, torch **2.13.0+cu130**. `install\requirements-frozen\qwen21-venv.txt` also names no index:

  ```cmd
  cd /d "%AIS%\ComfyUI_Qwen21"
  ..\tools\python\python.exe -m venv venv
  venv\Scripts\python.exe -m pip install pip==26.2.2
  venv\Scripts\python.exe -m pip install -r ..\install\requirements-frozen\qwen21-venv.txt --extra-index-url https://download.pytorch.org/whl/cu130
  ```

  On a GTX 900- or 10-series card, or a driver older than R580, use the CUDA 12.6 list instead: `-r ..\install\requirements-frozen\qwen21-venv-cu126.txt --extra-index-url https://download.pytorch.org/whl/cu126`. Without the venv the launcher skips the Qwen instance. Proven settings: `PROVEN_CONFIGS_QWEN21.md`.

### Step 6: Model weights

**The installer downloads the weights of the parts you choose** (Step 2): 29 files in `install\components.json`, each with its source repositories (the first one, then digest-identical mirrors), its size and its SHA-256. It downloads into `models\<folder>\` (post-processing files into `ComfyUI\`), checks each file and deletes one that doesn't match. The face detector's ONNX file has no public copy: the installer exports it from `face_yolov8m.pt` with Ultralytics, on the CPU, and checks its input and output shapes. To fetch the weights again, delete the file and re-run mode [3].

- Several sources are community uploads and quantisations, not the model authors' own repositories (for example the W4A8 LTX-2.5 transformer and text encoder). The installer prints each part's licences before it downloads. Read each model card and licence ([THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md), section 3).
- **MiniMax H3 is never downloaded.** The MiniMax H3 Community License grants use only outside the EU, the UK, South Korea and the USA. Where it allows you to use H3, get the ten files yourself (the [README](../../README.md#5-minimax-h3-bring-your-own-weights) and the installer list where each one is published) and put them in the folders below, or keep them where they are, under any name, and give the installer their folder (Step 7). The installer then checks their sizes, and the SHA-256 of a file under another name. The studio runs without H3: its default model is LTX-2.5.
- **RealVisXL is not downloaded either.** The four RealVisXL files the studio was tested with have no public copy to check against, so the installer lists them and leaves them to you. Without RealVisXL, keyframe stills use the first installed of Qwen-Image 2.1, Z-Image Turbo and CyberRealistic XL.
- The videolab downloader is the older way to get the LTX-2.5 and Qwen-Image 2.1 files, from the same manifest the installer's entries come from: `set "VIDEOLAB_MODELS_DIR=%AIS%\models"`, then `tool\.venv\Scripts\python.exe videolab\videolab.py download --profile full --dry-run`, and again without `--dry-run`. Its `full` profile is about 65.5 GB by the manifest's sizes and doesn't cover the image models, the Qwen 8-step LoRA, Wan 2.2 or post-processing.

**Every file the code loads.** The file names below are the ones the code loads (`tool/models.py`, `tool/workflows.py`, `tool/video_models.py`, `tool/video_models_user.json`, `tool/workflows/*.json`). Folders are relative to `models\` unless the table says otherwise. In each row, the folders and their files are listed in the same order, separated by a dot (·). Files that share a folder are separated by commas. `install/model_packages.json` groups most of them into packages.

| Component | Folder | Files |
|---|---|---|
| RealVisXL V5 (Image Studio, 8188) | `diffusion_models` · `clip` · `vae` | `RealVisXL_V5.0-Q5_K_M.gguf` · `clip_l.safetensors`, `clip_g.safetensors` · `xlVAEC_c91.safetensors` |
| SDXL reference images (IP-Adapter) | `ipadapter` · `clip_vision` | `ip-adapter-plus_sdxl_vit-h.safetensors` · `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` |
| CyberRealistic XL v10 | `checkpoints` | `cyberrealisticXL_v100_fp16.safetensors` |
| Z-Image Turbo | `diffusion_models` · `text_encoders` · `vae` | `z_image_turbo-Q4_K_M.gguf` · `qwen_3_4b.safetensors` · `z_image_ae.safetensors` |
| Qwen-Image 2.1 (8190) | `diffusion_models` · `text_encoders` · `vae` · `upscale_models` · `loras` | `qwen_image_2.1_Q5_K_M.gguf` · `qwen3vl_8b_int8_convrot.safetensors` · `qwen_image_2.1_vae_bf16_compact.safetensors` · `4x-UltraSharp.pth` · `p_qwen_image_2.1_8step_v0.1.safetensors` (fast mode only) |
| Wan 2.2 TI2V-5B (8188, legacy and optional: only for projects that pick it, face fix, LoRA training, Image Studio `wan`) | `diffusion_models` · `text_encoders` · `vae` · `loras` | `Wan2.2-TI2V-5B-Q4_K_M.gguf` · `umt5-xxl-encoder-Q4_K_M.gguf` · `wan2.2_vae.safetensors` · `Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors`. The other Wan entries (A14B, Q5/Q6 quants, LTX-Video 2B legacy) are listed in `tool/video_models.py` and `tool/workflows.py` `SUBST`. |
| LTX-2.5 (8189) | `diffusion_models` · `text_encoders` · `vae` · `checkpoints` | `ltx-2.5-22b-distilled-transformer-w4a8_convrot.safetensors` · `gemma4-12b-with-proj-ltx-2.5-w4a8_convrot.safetensors` · `ltx-2.5-video-vae-conv-bf16.safetensors` · `ltx-2.5-audio-vae-bf16.safetensors` (audio VAE) |
| | `latent_upscale_models` · `loras` | `ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors` (+ `ltx-2.5-latent-temporal-upscaler-x2-bf16-1.0.safetensors` for `ltx25_temporal_10s`) · `LTX-2.5-Licon-MSR-V1.safetensors`, `ltx2.5-CQ-enhancer-lora-for-videos-rank128.safetensors` |
| MiniMax H3 (8199) | `diffusion_models` | one DiT per tier: `minimax_h3_ref2va_pruned-w4a8_convrot_pruned.safetensors`, `minimax_h3_fl2va_pruned-w4a8_convrot_pruned.safetensors`, `Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.safetensors` |
| | `text_encoders` · `vae` · `latent_upscale_models` | `qwen3vl_32b_minimax_h3-w4a8_convrot.safetensors` · `minimax_h3_video_vae_fp16.safetensors`, `minimax_h3_audio_vae_fp32.safetensors` · `minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors` |
| | `loras` (tier-specific) | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`, `minimax_h3_turbo_v4_step600_ema.safetensors`, `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` (PDD-8 draft tier) |
| Post (8188) | `upscale_models` | `RealESRGAN_x4plus.pth` (video post), `RealESRGAN_x4.pth` (SDXL hires) |
| | inside `ComfyUI\` | `models\SEEDVR2\seedvr2_ema_3b-Q8_0.gguf`, `models\SEEDVR2\ema_vae_fp16.safetensors`, `models\onnx\bbox\face_yolov8m.onnx`, `custom_nodes\ComfyUI-Frame-Interpolation\ckpts\rife\rife47.pth` |

### Step 7: Weights you already have, and weights outside the tree (`paths.extra_model_dirs`)

**Weights you already have.** Weight files that ComfyUI or another program already keeps, under any name and in any folder, are used instead of a download (`tool/model_files.py`). Mode [3] asks for their folder before it shows the plan (Enter alone goes on); on the command line give it with `--use-folder <folder>`, more than once for more folders (`--h3-dir <folder>` is the older name of the same option). **Settings › Model files** in the web app runs the same search. The search:

- goes through the folders it is given with their subfolders, the model folders below and `models\` itself. It skips `.git`, venvs, `node_modules`, `site-packages` and unfinished downloads (`.part`, `.incomplete`, `.tmp`), and at the top of a drive also `Windows`, `Program Files`, `ProgramData` and the like. Folder links and junctions are followed, each real folder once. A network folder (`\\server\share`) is not searched: map it to a drive letter first.
- takes a file with the name and size of a catalog file as that file, as the installer always has. A file of a catalog file's size under another name counts only when its SHA-256 matches `install\components.json`. Hashing reads the whole file once; the result is kept in `models\.kinoloom-hashes.json` (by path, size and modification time, and by the file's ID where the drive gives one, so Kinoloom's links to a file aren't read again either), so a second search doesn't read it again. The four RealVisXL files have no size or SHA-256 in the catalog, so only their own names find them.

The plan then shows how each file found will be used, before anything is linked, copied or downloaded. The user's file itself is never renamed, moved or changed:

- **Linked**, when the file is on the drive of the studio folder: a hard link at its catalog path in `models\`, the same file under Kinoloom's name. It takes no extra space and needs no administrator rights or Developer Mode, and deleting either name keeps the other.
- **Used where it is**, when it is on another drive and sits under its catalog name (in the same letter case, since ComfyUI lists names as they are) in its catalog subfolder (ComfyUI's older `unet\` for `diffusion_models\`, and `clip\` for `text_encoders\`, count too): the folder above that subfolder (for example `E:\ComfyUI\models`) is added to `paths.extra_model_dirs`. That folder must be named `models` or hold at least two of the model subfolders, so a stray file never turns a whole user folder into a model folder; the top of a drive is never added.
- **Linked on its own drive**, when it is on another drive in any other place or under another name (a `models\` catalog file, on Windows, not on a network drive): a hard link in `Kinoloom-models\<subfolder>\<catalog name>` at the top of the file's drive, for example `E:\Kinoloom-models\vae\`. A hard link can't cross drives, so it is made on the file's own drive. It takes no extra space and needs no administrator rights or Developer Mode (Windows lets a standard user make a folder at the top of a drive). The folder gets a `README.txt` that says what it is, and goes first in `paths.extra_model_dirs`: ComfyUI loads the first file of a name, and the files there are checked ones. Explorer shows them at their full size, but the drive's free space doesn't change. Deleting the folder deletes only Kinoloom's names: the user's files stay, and the models count as missing until the next search.
- **Linked by a symbolic link** at the catalog path in `models\`, when no hard link can be made (an exFAT or network drive, say) and Windows allows symbolic links: with Developer Mode on, or to an administrator. No extra space; when the file is moved later, the next search links it again.
- **Copied**, when none of these works, after you agree (`n` downloads it instead). Besides a drive without links, that is a post-processing file on another drive (RIFE, the face detector, SeedVR2): ComfyUI reads those only from inside the studio folder, so a model folder can't hold them. The copy goes through a `.part` file, only a copy whose SHA-256 matches is kept, and a file already at the catalog path is never replaced.

A file that cannot be read (locked by another program, or not allowed) is skipped with a message, and the search goes on. If a link fails after all, the file is used where it is when it can be, and otherwise downloaded. Running the search again changes nothing that is already in place: a file already linked or copied is taken as it is. A read-only file is linked the same way: that attribute belongs to the file, so Kinoloom's name for it is read-only too, which ComfyUI doesn't mind.

**Letter case.** On Windows, ComfyUI opens a model file in any letter case, but its render graphs want the exact name in its list of files, as the name is on disk. The studio and the installer count a file the same way. A file in Kinoloom's own folders saved in another letter case (`LTX-VAE.safetensors` for `ltx-vae.safetensors`, say) is renamed to the catalog name by the installer, by the launcher at every start and by the search under Settings › Model files. Files outside Kinoloom's folders are never renamed.

`models\.kinoloom-sources.json` records which files were linked or copied from where, and where each link is; Settings › Model files shows it. A file in a model folder with a catalog file's name but another size is another build of the model (another quantisation, say). It stays as it is, the installer names it, and the studio counts the file as missing until its own copy is there.

**Model folders (`paths.extra_model_dirs`).** Big weights can stay on another drive. List the roots in `tool\settings.yaml`:

```yaml
paths:
  extra_model_dirs:
    - D:/models/h3
    - E:/ComfyUI/models
```

The installer and Settings › Model files add and remove entries for you; both change only this list and keep the file's comments. Each entry (absolute, or relative to the studio root) uses the same subfolder names as `models\`, or ComfyUI's older `unet\` and `clip\`. On every start, the launcher writes `temp\extra_model_paths.studio.yaml` from the entries that exist (subfolders `checkpoints`, `diffusion_models`, `text_encoders`, `vae`, `loras`, `upscale_models`, `latent_upscale_models`, `clip_vision`, `ipadapter`, `unet`, `clip`). It passes that file with `--extra-model-paths-config` to all four ComfyUI instances, after the tree's own `extra_model_paths.yaml`, so a file in `<studio>\models` wins over one of the same name in a model folder. A root whose path contains `h3` or `ltx` also fills `H3_LAB_MODELS` / `LTX25_LAB_MODELS` for `ComfyUI_LTX\extra_model_paths.yaml`. Restart the studio after the list changes.

The studio checks these roots when it decides which models are available: the image models (`tool/models.py` `is_available`), the video tiers (`tool/video_models.py`) and `tool\scripts\verify_restore.py`. A file there counts only with the size `install\components.json` gives it; one of another size is another build and does not count. ComfyUI loads the first file of a name in the order of the list (and, inside each folder, `diffusion_models\` before `unet\`, `text_encoders\` before `clip\`), so the studio counts that first one only. `install\make_manifest.py` covers `<studio>\models` only.

### Step 8: Verify with `install\verify_install.py`

```cmd
cd /d "%AIS%"
tool\.venv\Scripts\python.exe install\install_components.py --check
tool\.venv\Scripts\python.exe install\verify_install.py --quick
tool\.venv\Scripts\python.exe install\verify_install.py
```

`install_components.py --check` is read-only. It reports each part as installed, partly installed or not installed, lists every missing file with its full path (the files you bring with where they are published), and ends with the video tiers and image models the studio can see.

`verify_install.py` is read-only too; with `--log <file>` it also appends what it prints to that file. It exits with 0 only when no hard failure remains. It checks, in this order:

1. **Layout**: five core files (`tool/launcher.py`, `tool/api.py`, `tool/requirements.txt`, `tool/settings.example.yaml`, `start.bat`) must exist. A missing `ComfyUI\`, `ComfyUI_LTX\` or `ComfyUI_Qwen21\` (`main.py`) is only a warning.
2. **Manifest**: `install\manifest.json` is machine-specific and gitignored. Build it on the golden machine with `install\make_manifest.py`. It covers `models\`, `external_loras\` and `bible\luts\*.cube`, not `paths.extra_model_dirs`, and with `--quick` it records sizes only.
   - Every recorded file must exist with the exact size.
   - sha256 is compared only in a full run, and only when the manifest was built without `--quick`.
   - With `--quick`, missing assets are warnings, so a fresh clone passes.
   - Without `install\manifest.json` (every machine where you haven't built one), this check is skipped with a warning and the full run goes on to REQUIRED. A manifest you name with `--manifest` must exist.
3. **REQUIRED** (full run only): runs `tool\scripts\verify_restore.py` with the `tool\.venv` Python and an empty `PYTHONPATH`. It checks:
   - torch + CUDA;
   - the render-farm model files in `<studio>\models` or a model folder of `paths.extra_model_dirs` (Step 7), as warnings: the legacy Wan 2.2 files, Z-Image, the Wan A14B and legacy LTX-Video 2B files, and SDXL. The one exception is the IP-Adapter pair, which is a hard requirement when an SDXL model is installed;
   - the movie models: every LTX-2.5 and MiniMax H3 tier in the video registry, looked up in `models\` and the extra roots. A missing tier is a warning, and so is a tier this PC's graphics card or driver can't run, with the reason. Having none available at all is a hard failure, because the studio's default model (`ltx25_s6_cq`) and its fallbacks are LTX-2.5 and H3 tiers. A warning also says when `ltx25_s6_cq` itself is not available and another tier stands in for it;
   - optional extras (SeedVR2, RIFE, face detector, `tools\ffmpeg`, `tools\git`, `tools\python`, LUT), as warnings;
   - imports of the studio modules.

   It does not check the Qwen-Image 2.1 weights.

---

## 4. Starting the studio

Double-click `start.bat` in the studio root. When `tool\.venv` is missing, it says Kinoloom is not installed and offers to run `INSTALL.bat`. Otherwise it runs `tool\launcher.py` with `tools\python\python.exe` if present, else `tool\.venv\Scripts\python.exe`.

What happens on start:

1. **Relative paths**: `start.bat` changes to its own folder (`%~dp0`). Nothing is written to the registry, and no fixed drive letter is needed. The launcher reads `tool\settings.yaml` with the base Python's PyYAML, or with the one in `tool\.venv` when the base Python has none (as on a new install), so the base Python needs no packages of its own.
2. **Settings**: if `tool\settings.yaml` does not exist, the control room copies `tool\settings.example.yaml` to it (`tool/core.py` `load_config`). It also generates `workers.shared_token` when that is empty or still the placeholder, and `security.api_token` when `ui.host` is not a loopback address (an address only this PC can reach). It saves both (`tool/security.py` `ensure_secrets`).
3. **Databases**: `tool\studio.db`, `tool\queue.db` and `tool\datasets.db` and their tables are created when missing (`CREATE TABLE IF NOT EXISTS`).
4. **Components**: render farm (8188), control room (8080), LTX-2.5 (8189), MiniMax H3 (8199), Qwen-Image 2.1 (8190). Logs go to `logs\` (`comfyui_8188.log`, `ui.log`, `ltx_8189.log`, `h3_8199.log`, `qwen_8190.log`). A component whose ComfyUI tree or venv is missing, or whose venv is still marked unfinished, is skipped with a message, and so is one this PC's graphics card or driver can't run (`tool/gpu_support.py`). A component that crashes three times in a row within 45 s of starting is not restarted again; a restart the GPU broker makes on purpose, to free the card's memory, is started again at once and does not count.
5. **Ready**: the launcher polls the control room's `/api/health` for up to 300 s. When it answers, the window title shows the web app's address, a "Kinoloom is ready" banner prints it below the startup logs (with the components that were not installed or not started on this PC and why, and a hint when no model weights are found; the startup header above names the graphics card and driver), and the default browser opens it. `ui.open_browser: false` or `start.bat --no-browser` keeps the browser closed. With `ui.host: 0.0.0.0` the address is `http://127.0.0.1:<port>/`.
6. **Shutdown**: children are assigned to a Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so closing the window or pressing `Ctrl+C` ends them. The port-conflict and shutdown sweeps only kill processes whose command line is one of the studio's own components (`launcher.is_studio_cmd`).

---

## 5. Web interface and ports

Once the launcher shows the components as started, open:

- **Movie** (the film you are making, step by step): [http://localhost:8080/](http://localhost:8080/), also [/project](http://localhost:8080/project)
- **Studio** (Video Studio, Image Studio and the Library): [http://localhost:8080/studio-tools](http://localhost:8080/studio-tools). Casting, the Movie side's first step, is served from there too (`/studio-tools#casting`).
- **Active model registry endpoint**: [http://localhost:8080/api/models](http://localhost:8080/api/models)

The control room listens on settings `ui.host` (default `127.0.0.1`, this PC only). With `0.0.0.0`, other PCs can reach it, and they must supply the API token (`SECURITY.md`).

### Local ports

- **Port 8080**: control room (uvicorn `api:app`).
- **Port 8188**: render farm (`ComfyUI\`): RealVisXL, CyberRealistic XL, Z-Image Turbo, the legacy Wan 2.2, face fix, post (RIFE, RealESRGAN, SeedVR2).
- **Port 8189**: LTX-2.5 (`ComfyUI_LTX\`, `--enable-dynamic-vram --bf16-vae`).
- **Port 8190**: Qwen-Image 2.1 (`ComfyUI_Qwen21\`, `--highvram`).
- **Port 8199**: MiniMax H3 (`ComfyUI_LTX\`, `--enable-dynamic-vram`, own database `ComfyUI_LTX\user\comfyui_h3.db`).
- **Port 8120**: worker agent (`tool/worker/worker_agent.py`). It runs on worker PCs set up with `install\INSTALL-WORKER.bat` or `colab/bootstrap.sh`, and `start.bat` does not start it. A LAN worker gets the render-farm ComfyUI only. See `docs/setup/DISTRIBUTED_FLEET.md`.
