[Kinoloom](README.md) › [Docs](docs/README.md) › Changelog

# Changelog

> **In short:** This is the list of notable changes to Kinoloom, grouped by
> area. Nothing here is released yet. The version in `pyproject.toml` is a
> placeholder until the first tagged release. The public repository starts
> with a fresh history at this state, so where an entry says something was
> renamed, removed or fixed, it compares with the private development tree
> it came from.

## [Unreleased]

### Video Studio: prompt checks and the cast sheet
- **Prompt check** (`tool/prompt_check.py`). It works out what a request
  renders with, from the functions the renders use: `video_studio.resolve_references`
  for a clip, `video_studio.chain_cast` and `chain_studio` for a chain (the
  first two were moved out of `start()` and `_run_chain_body` unchanged). Then
  it reads the prompts against that. Errors stop Generate:
  - MiniMax H3 picture labels. The text encoder writes `<Picture N>: ` before
    each attached image, so a prompt points at an image only through that exact
    label. Near misses (`<picture1>`, `[Picture 2]`, `(pic 1)`, `Picture 3>`),
    labels past the attached pictures, a name bound to the wrong label
    (`Ruth (<Picture 2>)` when Ruth is Picture 1) and `<Video N>` / `<Audio N>`
    labels are errors.
  - Requests that would fail or lose an image: a reference that cannot be found
    or shares a slot, a start image that is not in a ComfyUI input folder, and
    an LTX request without `pic1` (the MSR guide needs it).
- Suggestions never block: pictures no segment labels, a name without its
  label, picture labels in an LTX or Wan prompt, a segment 1 that does not
  start from the start image, a later segment of a seamless take without the
  continuity line, a prompt that describes no sound, an LTX chain cast member
  with no look on the card whom the segments do not describe in the same
  words, and references the model does not get, with the reason.
- **Check** shows the result under the prompt. Where the fix is certain, an
  item has a button that edits the prompt (**Fix all** applies them all).
  Generate runs the same check, and `POST /api/videostudio/generate` and
  `/chain` refuse a request with errors and return its issues. The check is
  `POST /api/videostudio/prompt_check`.
- **Cast sheet from the server** (`POST /api/videostudio/cast_sheet`,
  `prompt_check.cast_sheet`), built from the same plan: how the model reads a
  prompt, the images in the model's own order (H3 `<Picture N>` numbers, LTX
  slots) with their names and looks, how segment 1 opens, the continuity line
  for later segments ("Opening Frame: Continue seamlessly from the terminal
  frame of the previous clip." for H3, "... continues seamlessly in one
  continuous shot from the same camera angle" for LTX), the segment lengths of
  the chain engine, and the reply format. The page used to build it, without a
  continuity line. A reference without a name is listed by its role and image
  file (`pic2: a person (image ….jpg)`), not under its file name as if that
  were a cast name. Where a person has no look, the sheet asks the writer to
  take it from the attached image and not to guess hair or clothes.
- **Paste reply** fills the prompt and the segment boxes from the reply's
  "Segment N:" blocks, then runs the check.
- **Fixed:** the H3 background reference is the last `<Picture N>`; the slot
  labels, the hint and the old cast sheet called it "not a Picture". Each H3
  reference card now shows its label, numbered over the filled slots (pic3 is
  `<Picture 2>` when pic2 is empty).
- **Fixed:** with the main prompt empty and one segment box filled, Generate
  sent the empty main prompt as the clip's prompt.
- **Fixed:** LTX-2.5 sizes off a 64 px grid rendered smaller than asked. The
  LTX templates sample stage 1 at half the size, on a 32 px latent grid, so
  512x288 came out as a 512x256 clip. The LTX tiers now snap to 64 px
  (`resolution_limits.step`, smallest 512x256), and `start()` refuses other
  sizes. The 544p-class H3 tiers listed 480x272 as their smallest size, off
  their own 32 px grid (H3 stops with a shape error at a height of 272); it is
  480x288 now. A test keeps every listed size on its model's grid. Seamless
  LTX chains were not affected: they render a fixed 960x544 draft.
- Over the 151 Video Studio job prompts recorded on the development machine,
  the check finds errors in 4: H3 renders whose prompts label pictures that
  were not attached (three with `<Picture 2>` and one picture, one with
  Pictures 1-3 and none). It finds nothing in the showcase prompts
  (`docs/showcase/inputs/`).

### Security updates of third-party packages
- GitHub listed 426 Dependabot alerts on the dependency lists (119 advisories
  in 19 packages, across 7 files). Every alerted package is moved to a release
  with the fix, or removed (basicsr, below):
  - `tool/requirements.txt` and the `tool-venv` snapshot: Pillow 12.2.0 ->
    13.0.0; transformers 4.54.1 -> 5.17.0 with huggingface-hub 0.34.3 ->
    1.32.0, tokenizers 0.21.4 -> 0.23.2, safetensors 0.4.5 -> 0.8.0,
    accelerate 1.6.0 -> 1.15.0 and diffusers 0.32.1 -> 0.38.0; aiohttp 3.14.5,
    urllib3 2.8.0, Werkzeug 3.1.9, PyJWT 2.15.1, GitPython 3.2.1, setuptools
    84.2.1, uv 0.13.5, and newer certifi, filelock, fsspec, hydra-core, Mako,
    multidict and yarl;
  - the `ltx-venv` and `qwen21-venv` snapshots: Pillow 12.3.0 -> 13.0.0,
    setuptools 78.1.0 -> 84.2.1, aiohttp, certifi and filelock (LTX also Mako
    and multidict); `base-python.txt`: pip 25.0.1 -> 26.2.2; `videolab`:
    Pillow 13.0.0.
- basicsr 1.4.2, gfpgan 1.3.8 and realesrgan 0.3.0 are removed from the tool
  venv: no code imports them (face fix and upscaling run as ComfyUI nodes with
  their own model files).
- Checked with the test suite; every node pack of the three ComfyUI trees
  importing as before (a CPU `--quick-test-for-ci` start); the CLIP and
  InsightFace checks; `videolab.py selftest --skip-gpu`; and pip-audit, which
  reports no known vulnerability in any pinned list.
- A short render on each engine with the updated packages, each on a ComfyUI
  started for it, from the graphs the studio builds (every request passed the
  prompt check first). All finished; times include loading the models from
  disk:
  - LTX-2.5 (`ltx25_s6_cq`), 9 frames, a person and a background reference:
    158 s, picture and sound. It asked for 512x288 and got 512x256, the size
    fix above.
  - MiniMax H3 (`minimax_h3_draft_pdd8`), 480x288, 22 frames, two pictures:
    173 s, picture and sound.
  - Qwen-Image 2.1 (`qwen21_fast`), 512x512, text only and with one reference:
    72 s and 47 s.
  - On the main ComfyUI, which loads transformers 5 from the studio venv: Wan
    2.2 5B, 512x288, 9 frames: 73 s; Z-Image, 512x512: 67 s.
- `.github/workflows/dependency-audit.yml` runs pip-audit 2.10.1 on the pinned
  lists whenever one changes, and weekly. SECURITY.md says what it cannot check
  (the CUDA builds of torch and the two git-installed packages).

### Fixes in the chain paths
- **A retried movie take reuses its finished steps.** Every upload got a
  random name (`kf_<8 hex>.png`), and those names are part of the graphs the
  chain engine compares before it reuses a step. A take that was re-queued or
  retried uploaded its references again under new names, so every step
  rendered again. Uploads are now named after their bytes
  (`renderer.upload_name`: `kf_` plus 16 hex digits of the SHA-256), on the
  local ComfyUI and on worker PCs alike, and the same still keeps its name.
- **"From last clip" (↪) works for chains.** The page sent the absolute path
  of the tail frame's thumbnail copy as the chain's start image, and
  `start_chain` accepts only a file name in a ComfyUI input folder, so the
  request was refused. The page now sends the name of the copy
  `continue_from` stages in the input folders. `start_chain` also refuses a
  start image that is in neither input folder, instead of rendering the chain
  without its opening frame.

### Setup checks and licence files
- **`install/verify_install.py` without a manifest.** `install/manifest.json`
  describes one reference machine and is not in git, so the full check
  reported a hard failure on every fresh clone. Without that file the asset
  comparison is now skipped with a warning, and the result depends on the
  other checks (layout, and torch, CUDA, model files and imports in
  `verify_restore.py`). A manifest named with `--manifest` must exist.
- **LoRA training guide.** `tool/train/INSTALL.md` cloned sd-scripts at its
  newest commit and installed that commit's requirements into the studio's
  venv, which could change the studio's pinned packages. It now checks out
  the commit `tool/requirements.txt` pins, which already installs everything
  sd-scripts needs.
- **Node pack licences.** `ComfyUI-LTX25-Studio` and
  `ComfyUI-Qwen21-Optimized` carry their own MIT `LICENSE`. Before, the
  nearest licence file above the LTX pack was ComfyUI's GPL-3.0
  `ComfyUI_LTX/LICENSE`.

### Video Studio chains: progress, segment previews, portrait
- **Progress you can follow.** A chain record carries a step plan
  (`chain_studio.chain_plan`): every step in order, with its state, start
  time and seconds, and whether an earlier run's result was reused or a seam
  re-roll drew the link again. The History card and the player show a
  segmented bar from it, each step sized by the step times measured in the
  showcase and rescaled by the chain's own pace, plus a row of step pills. The
  History response carries the server's clock (`now`), so elapsed times don't
  depend on the browser's. Before, the bar followed the link counter only and
  reached 100 % long before the 1080p steps had finished.
- **Watch each segment as it lands (LTX).** After every link's draft and again
  after its finish, a background thread encodes that link's own frames and
  sound (carried frames skipped) into a small mp4
  (`chain_assemble.link_preview`, x264 at below-normal priority), served at
  `GET /api/videostudio/chain/{cid}/preview/{name}`. The player lists them
  under the take, so a segment can be watched before the whole take is
  decoded.
- **Live sampler progress for chains.** `StudioRunner` queues its graphs
  without a client id, so ComfyUI broadcasts the progress events to the
  studio's progress relay. Before, they went to the client id
  `studio-chain`, which no page listened to.
- **Portrait LTX chains.** A portrait size (for example 1088x1920) drafts at
  544x960 and finishes at 1088x1920 (`chain_studio.ltx_draft_size`); seam QA
  (`seam_qa.fit`) and the cast report size their analysis frames by
  orientation. Rendered end to end from Video Studio: chain `9de7e018337e`,
  2 links, 177 frames at 1088x1920 in 915.6 s (record and seam QA in
  SEAMLESS_CHAINS_04 §6).
- **Fixed:** the Video Studio page no longer flashes while it polls. History
  cards are updated in place instead of rebuilt, and the sidebar's GPU meter
  repaints only when its reading changes.

### One-click install
- **`INSTALL.bat`.** On a fresh Windows 11 PC (x64; Windows 10 1803 or later
  is untested), a double-click and one key press set up everything except
  the model weights. Its first step, `install/bootstrap.ps1`, puts a private
  Python 3.12.15 in `tools/python` and MinGit in `tools/git` (only when there
  is no `git.exe` on the PATH), and installs the Visual C++ runtime when the
  PC's is missing or older than 14.40 (it asks first, Windows asks for
  administrator rights, and the installer runs only with a valid Microsoft
  signature). Python and Git are pinned in `install/components.json` by size
  and SHA-256. `INSTALL.bat` runs it as step 1 of `INSTALL-MAIN.bat /auto`,
  whose other steps build the control room's venv and every part's runtime
  (`--components all --skip-models`). Exit code 2 means some parts did not
  install; the studio starts without them. Tested end to end on two PCs: one
  where the Visual C++ runtime and ffmpeg were already installed, and one
  set up from the GitHub ZIP, where it also downloaded ffmpeg. A second run
  skips every finished step.
- **The install shows its progress.** `INSTALL.bat` says up front roughly
  how much it downloads and how long it takes. Each of the 5
  steps prints its number and start time, the window title shows the step
  (in the last one, which item of the list), the slow steps say what they
  are doing, and `install_components.py` prints how long each step of 30 s
  or more took. Its output is line-buffered, so it stays in order with pip's.
- **Long folder paths.** torch puts files about 211 characters below the
  studio folder, past Windows' default 260-character limit from a folder path
  of about 50 characters (a ZIP unpacked on the Desktop is enough). When the
  path is that long and Windows long paths are off, `bootstrap.ps1` says so in
  a few lines and offers to turn them on (`LongPathsEnabled`; Windows asks for
  administrator rights). Declined, it stops and suggests a short folder such
  as `C:\Kinoloom`.
- **A stopped install is repaired.** A venv that pip did not finish (the
  `.kinoloom-incomplete` marker is still in it, a package lacks its `RECORD`,
  or a pin of its list is missing) is deleted and built again on the next
  run: `tool/.venv` by `INSTALL-MAIN.bat`, the two ComfyUI venvs by
  `install_components.py`. **Fixed:** when a pinned list failed,
  `install_components.py` tried ComfyUI's own unpinned `requirements.txt` on
  top of the half-installed venv; that second try is gone.
- **`start.bat` offers the install.** On a PC without `tool/.venv` it says
  Kinoloom is not installed yet and runs `INSTALL.bat` on Enter, instead of
  failing with a pointer to the restore guide. It no longer falls back to
  whatever `python` is on the PATH.
- **The address is easy to find.** Once `/api/health` answers, the launcher
  prints a banner with the web app's address, puts it in the window title and
  opens it in the default browser (`ui.open_browser: false` or
  `start.bat --no-browser` turns that off). A `0.0.0.0` bind opens as
  `127.0.0.1`. The banner also names parts that are not installed and says
  when there are no model weights yet.
- **Fixed:** `tool/requirements.txt` did not install on a new PC: `einops`
  0.7.0 clashes with `rotary-embedding-torch` 0.9.1, which needs 0.8 or
  later. It is now 0.8.2. A new `resolve` job in the dependency audit runs
  `pip install --dry-run` on the lists the installer uses, on Windows, so a
  clash like this fails CI.
- **Fixed:** on a new install the launcher ignored `tool/settings.yaml`,
  because the bare `tools/python` has no PyYAML. It now reads the settings
  with `tool/.venv`'s PyYAML.
- **Fixed:** `INSTALL-MAIN.bat` clears `PYTHONPATH` and `PYTHONHOME`, which
  could leak another Python's packages into the venvs. It also sets
  `SAM2_BUILD_CUDA=0`, so SAM-2 does not try to build its optional CUDA
  extension.
- `install_components.py --skip-models` no longer prints the list of weights
  to download by hand; `--check` and an install with weights still do.
- The ffmpeg download is checked by size as well as SHA-256. The venv sizes
  in `install/components.json` are now those of a fresh install (4.35 and
  5.69 GB).

### Component installer
- **Choose what to install.** `install/install_components.py`, run by
  `INSTALL-MAIN.bat` mode [1] after the control room and by the new mode [3],
  installs the parts you pick: LTX-2.5, MiniMax H3, Qwen-Image 2.1,
  post-processing, SeedVR2, Z-Image Turbo, CyberRealistic XL, RealVisXL and
  the legacy Wan 2.2. For each it copies the pinned ComfyUI release into its
  tree, adds the node packs at pinned commits, builds the tree's venv from the
  frozen list, downloads the weights into `models/` and ffmpeg 9.0 into
  `tools/ffmpeg`. `--check` reports what is installed and changes nothing.
- **Everything pinned.** `install/components.json` lists every ComfyUI
  release and node pack by commit and every weight by source, size and
  SHA-256. A download that doesn't match is deleted. Tracked files inside the
  ComfyUI trees are never overwritten.
- **Bring your own: MiniMax H3 and RealVisXL.** The installer never downloads
  them. It installs their runtime and lists each file with its full path and,
  for H3, where it is published; `--h3-dir` registers a folder that already
  holds the H3 weights. The README's quick start explains it.
- **Face detector.** The face detector's ONNX file has no public copy, so the
  installer exports it from `face_yolov8m.pt`. The patched Impact-Pack file
  that reads that export (`impact_onnx.py`, GPL-3.0) and the pack's
  `LICENSE.txt` are now tracked.
- **Keyframes fall back.** When `keyframes.model` (default RealVisXL) is not
  installed, keyframe stills use the first installed of Qwen-Image 2.1,
  Z-Image Turbo and CyberRealistic XL.
- **Fixed:** the Z-Image Turbo entry was labelled "CyberRealistic Z-Image
  Turbo"; it is the stock Z-Image Turbo (model id unchanged).

### Release audit
- **Licenses.** New [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): the files
  that are not under the MIT License, the software the studio runs, and a
  summary of each model's license. `LICENSE` is now the plain MIT text. The two
  patched upstream files (`nodes_lt_audio.py`, GPL-3.0; `MMH3_Split_Upscale.py`,
  MIT) start with a header naming their origin and the changes, and their
  license texts are tracked next to them. The web fonts ship their SIL OFL
  texts. `publish_models_hf.py` refuses to upload to a public Hugging Face
  repository. CONTRIBUTING says how contributions are licensed.
- **No MiniMax H3 media.** The MiniMax H3 Community License does not grant use
  in the EU, the UK, South Korea or the USA and limits displaying outputs in the
  same way, and Kinoloom is developed in the EU. So the showcase shows only
  the LTX-2.5 takes.
  The H3 takes' timings, QA and identity scores stay in `docs/SHOWCASE.md` as
  text, and their requests in `docs/showcase/inputs/`.
- **`videolab/` is a plain folder.** It was a git submodule of a separate,
  private repository, so a clone could not fetch it. It is now a snapshot of
  that lab at its commit `3f6cabc`, edited only for publication: planning
  references removed, the model sizes corrected, a pointer to the fact-checked
  copies in `docs/research/`, and installers that skip an existing node-pack
  folder unless `--force` / `-Force` is given. CI no longer checks a pin.
- **Removed:**
  - an alternative text-encoder option for legacy Wan 2.2, the extra Wan
    tier that used it, its UI, and the Video Studio `promote` route;
  - the advisory decision engine (`decision_engine.py`, `decision_data.py`
    and its hook in the orchestrator);
  - 50 one-off lab scripts in `tool/scripts/` (the 15 that remain are listed
    in `tool/scripts/README.md`);
  - four workflow templates nothing loads (`flux_schnell_t2i`,
    `wan2.2_i2v_clip`, `wan2.2_t2v_a14b_ens`, `wan2.2_ti2v_5b_ens`), the
    Flux file constants, the unused `pages/queue.js`, `tool/agent_spec.md`
    and `tool/py.typed`;
  - settings keys nothing reads, the unused backup helpers in `support.py`,
    and two dead methods (`post._add_audio`, `renderer.split`).
- **Setup on a new machine.** `install\INSTALL-MAIN.bat` mode [1] also writes
  the colour-grade LUT and deploys the live-progress page. RESTORE_INSTRUCTIONS
  gives the exact ComfyUI release tags, the source and version of every render
  farm node pack, `robocopy /XD .git` so no upstream history lands in the tree,
  and a weights download with `videolab.py download --profile full` (LTX-2.5
  and Qwen-Image 2.1). The README's quick start follows the same steps. The
  Colab notebooks and `bootstrap.sh` clone the public repository, with
  `GITHUB_PAT` only for a private fork.
- **Fixes:** the upload and download name checks use `ntpath.basename`, so a
  name like `C:x` is rejected on Linux as it is on Windows; `/api/status`
  reports no GPUs instead of failing when `nvidia-smi` is missing; the
  launcher skips a ComfyUI instance whose tree is missing; the render farm's
  `OMP_NUM_THREADS` follows the CPU's thread count instead of a fixed 36; the
  face-drift test skips when PyAV is not installed.
- **Wording.** Comments and docstrings no longer carry planning tags (phase
  numbers, requirement ids), and UI texts no longer overstate: an LTX-2.5
  chain's latent carry is soft-pinned, not exact, and the movie pipeline's
  seamless takes are marked experimental. The version is 0.1.0, "Alpha".

### Name and layout
- The project is now **Kinoloom** (formerly AIStudio, and "AI Movie Studio"
  in the web UI). What changed with the name:
  - environment variables `AISTUDIO_*` are now `KINOLOOM_*` (for example
    `KINOLOOM_API_TOKEN`, `KINOLOOM_BIND_HOST`, `KINOLOOM_BROKER_URL`), and
    `AI_STUDIO_OWN_JOBS` is `KINOLOOM_OWN_JOBS`. The old names are no longer
    read;
  - the browser keeps its API token as `kinoloom_token` (localStorage and
    cookie). A token saved under the old names moves over on the first page
    load (`tool/static/js/api.js`), and so do the Image Studio settings
    (`tool/static/app.js`);
  - renders land in the ComfyUI output subfolder `kinoloom/` instead of
    `aistudio/` (every workflow template, post, the scripts); earlier renders
    stay where they are. Post stages its frames in `ComfyUI/input/kinoloom_post/`;
  - the `extra_model_paths.yaml` section names, the worker installer's
    defaults (`C:\Kinoloom_worker`), the Colab paths (`/content/kinoloom`,
    worker name `kinoloom-colab`, Hugging Face dataset placeholder
    `kinoloom-models`), the notebooks (`colab/Kinoloom_Colab.ipynb`,
    `colab/Kinoloom_Smoke.ipynb`), the `pyproject.toml` name and the GitHub
    URLs (`harlixay7/Kinoloom`).
- The repository root holds only the README, CHANGELOG, CONTRIBUTING,
  SECURITY and LICENSE docs. `SHOWCASE.md` moved to `docs/`; the setup guides
  (`RESTORE_INSTRUCTIONS`, `PINNED_VERSIONS`, `TOOLING_BACKUP`,
  `DISTRIBUTED_FLEET`) to `docs/setup/`; the research notes (`SEAMLESS_CHAINS*`,
  `VIDEOLAB*`, `PROVEN_CONFIGS_QWEN21`, `HARDWARE_BENCHMARKS_AND_LIMITS`,
  `RETRACTIONS_AND_POSTMORTEMS`) to `docs/research/`. Every relative link was
  rewritten and checked, and code messages that name a doc use the new path.
  The README has a repository layout.
- Out of git: runtime state the studio writes on each machine
  (`tool/video_lora_registry.json`, `tool/perf_records.json`,
  `tool/training_runs.json`), the decision-label files in
  `tool/decision_labels/` (built from this machine's projects), and anything
  in `ReferenceImages/` but its README.
- Into git: the chain engine's ComfyUI node files (`studio_chain_core.py`,
  `studio_chain_nodes.py`, `selftest.py` in
  `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/`). The tracked `__init__.py`
  imports the first two, so a clone could not load the pack without them.
- Removed: nine `tool/workflows/*.json.bak_noaudio` backups, the root
  `INSTALL-WORKER.bat` stub (the installer is `install/INSTALL-WORKER.bat`), and
  the unreferenced `tool/requirements_incremental.txt` and
  `tool/requirements_missing.txt`.
- Comments and docstrings no longer cite internal planning documents that are
  not in the repository. Lab records that are not in
  the repository are named as lab records; `docs/research/VIDEOLAB_ARCHIVE_INDEX.md`
  lists them. No code changed in that pass (each file's syntax tree was compared
  before and after).
- Fix: the training-run store shipped as an empty JSON list, and
  `training._load_runs()` returned it as is, so starting a run would fail on
  `runs[run_id] = ...`. It now reads only a mapping (new test in
  `tests/unit/test_training_progress.py`).

### Structure
- `tool/api.py` split into domain routers under `tool/routers/`, with shared
  singletons in `tool/routers/common.py`. Background services start in the
  FastAPI `lifespan`, never at import time.
- New hermetic test suite in `tests/`: `tests/conftest.py` copies `tool/` and
  `install/` into a temporary sandbox and refuses to run against a live tree.
  The root `test_api.py`, which imported the live tree, was removed.
- `benchmarks/` microbenchmarks (`python -m benchmarks.run`). They time small
  helpers in isolation; they are informational, not performance guarantees.

### Security
- One request gate (`tool/security.py`, `tool/api.py`): Host-header
  allowlist, Origin check on state-changing requests, and an API token for
  every non-loopback request. A loopback browser on a loopback Host header
  needs no token (`security.local_bypass`). `security.api_token` is generated
  on first start when the studio binds a non-loopback host.
- CORS allows only `http://127.0.0.1:8080` and `http://localhost:8080` unless
  `security.cors_origins` says otherwise.
- The worker fleet is fail-closed: `workers.shared_token` is generated when
  empty (or still the shipped placeholder) and every worker call must carry
  it. It is one shared secret: whoever holds it can register a worker.
- Path guards for every file-serving and file-reading route (`core.safe_join`,
  `resolve_user_path`, `is_safe_component`); invalid input returns 400/403/404
  instead of 500. Requests that contain a NUL byte are refused.
- Dataset URL import (`tool/dataset.py`): https only, every DNS answer must be
  a public address and the resolved IP is pinned for the download (no DNS
  rebinding), no redirects, at most 500 URLs and 50 MB per file, and a file is
  kept only if it decodes as an image.
- Worker-reported fields are escaped on the Workers page (stored XSS), and
  inline `onclick` handlers no longer interpolate ids.

### Video Studio seamless chains
- New chain engine for the LTX-2.5 chain tier and H3 reference tiers
  (`tool/chain_engine.py`, `tool/chain_graphs.py`, `tool/chain_studio.py`,
  nodes in `ComfyUI-LTX25-Studio/studio_chain_nodes.py`), on by default
  (`video_studio.seamless_chains`). Links continue from saved lossless
  latents and frames instead of encoded mp4 tails, each step is recorded in a
  resumable manifest, a chain can stop at `draft_ready` and be finished later
  (`POST /api/videostudio/chain/{cid}/finish`), and the take is encoded once.
  See `docs/PIPELINE_AND_CONTINUITY.md`, section 4.3.
- LTX carry: soft pin ramp, latent carry (no VAE round trip per link),
  carry restore of the soft-pin gain, seam AdaIN on the first free latents,
  the take's audio joined in latent space and decoded once, and the take
  video decoded once from the joined latents in streamed windows. On the
  same-seed gate chain this removed the seam jolt (lightness -3.5, 5x motion).
  Whether the earlier engine's saturation accumulated from link to link was
  not isolated.
- H3: a 39-frame masked audio-video latent prefix instead of a frame-0 still,
  one decode of the joined take, and one 1080p refine of the whole take with
  the temporal chunk junctions kept off the seams.
- `tool/seam_qa.py` (seam, drift, motion and audio QA of a take) and the lab
  harness `tool/scripts/chain_lab.py` (isolated ComfyUI, telemetry, stall
  watchdog with stack dumps, gate and A/B commands).
- LTX chain graphs unload the models (`unload_all_models`, which frees the
  text encoder's VRAM and pinned RAM) after the last prompt is encoded and
  before the sampler. Before, the 9.9 GiB Gemma encoder stayed pinned next to
  the DiT. On gate v2's stalled 1080p finish (still with its keyframes, ~49k
  tokens) the server's RSS grew to 39 GB and available RAM fell to 0.55 GB;
  the rerun with the release held 13-15 GB less RSS and kept at least 21.7 GB
  available, but it stalled as well: that finish was past the 1080p token
  cliff, and dropping the keyframes is what made the finishes complete. (H3
  chain link graphs released their models too at first; they no longer do,
  see below.)
- Step digests skip memory-only passthrough nodes, so adding or moving an
  eviction no longer invalidates finished links; manifests written before
  this change are matched and migrated in place.
- A chain step is reused only while the chain files its graph reads are the
  ones it was rendered from (size and mtime, recorded per step). These are the
  chain files read by the six chain loader and take nodes, not reference
  images, which the graph names directly. Before, a step with an unchanged
  graph was reused after an upstream re-render: editing one
  link's prompt left the next link drafted from the old tail, and re-rendered
  finishes left the old final take in place. Records from before this change
  count while none of their inputs is newer than the record.
- LTX finish (stage 2) refines with the draft's own model (MSR IC-LoRA and
  the draft's CQ strength, without the reference guide) from sigma 0.55 with
  euler ancestral, and no longer re-applies the detailing keyframes. The
  official distilled stage 2 (bare model from 0.909375) re-drew what the
  prompt does not name: on the gate chain a cream cable-knit turtleneck came
  out floral, an overshirt plaid, and both faces changed. From 0.725, SSIM to
  the draft went 0.55 -> 0.72 and texture correlation 0.35 -> 0.71 on the same
  draft, with link 1 in 182 s instead of 733 s, but the faces still came out
  older (forehead furrows). On a later draft (CQ 0 with the face close-up),
  from 0.55 the lead keeps the draft's apparent age (29.5, against 33.1 from
  0.725; ArcFace similarity 0.351 against 0.241), with SSIM 0.82 and texture
  0.83 against 0.74 and 0.70 from 0.725, nearly the same 1080p detail (144.4
  against 147.9) and the same two steps.
- Seam look matching (`StudioSeamAdain`) has a `path` mode, now the chain
  default: each latent after a carry is matched on its own to the line from
  the carry's look to the content's. The block match could not remove a
  one-latent flash of the first generated latent (+15 saturation over 8
  frames on the gate chain, +12 to +15 on the static background; the seam's
  saturation step went from +9.6 to -2.6). Chains drafted before keep the
  block match.
- The decode-once LTX take crossfades the 3 carried latents after the anchor
  in latent space and switches links at the carry end (join 4, blend 3)
  instead of switching hard on carried latent 2: each link boundary is one
  event instead of two, and the junction step of +3.1 saturation is gone.
- LTX chains age the lead far less. Measured on gate link 1 with the same
  seed, as the lead's apparent age and ArcFace similarity to her portrait
  (InsightFace): the CQ enhancer LoRA in the draft aged her (draft 32.6 /
  0.205; without it 25.0 / 0.350), so chains draft without it
  (`LtxChainSpec.cq_strength = 0`; chains drafted before keep 0.6). Face
  close-ups of the cast portraits, lead first, now fill the MSR subject slots
  `pic1`-`pic4` that are still free (none when a start image and three cast
  references fill them; `tool/face_ref.py`, `chain_studio.ltx_face_slots`): in a full-length
  portrait the face is ~90 px tall at draft size, the close-up gives it ~270
  px, and her similarity rose from 0.205 to 0.303. With both, link 1's 1080p
  finish read 33.1 from sigma 0.725 and 29.5 from 0.55 (draft 29.5), against
  40.1 for the earlier recipe (those runs used different reference crops, so
  their similarities are not compared). Over the whole 4-link gate chain
  (same seeds, with the cast line and stage 2 from 0.55) the lead's apparent
  age per link went from 40 / 39 / 39 in the 1080p take (32 -> 41 in the
  draft, link 4 on one detected face) to 30 / 33 / 30 / 28 (draft 30 -> 26),
  and her similarity rises along the chain (0.32 -> 0.35) instead of falling
  (0.20 -> 0.17); at a second seed she stays within 24-32. The second
  character (detected mainly in links 1-2) still reads older in link 2 (34 -> 38) and his
  similarity falls (0.41 -> 0.28): not solved.
- When a cast reference carries a `description`, or comes from a face dataset
  whose generation parameters describe the person, every LTX link prompt gets
  one cast sentence after its first sentence (`chain_studio.with_cast_line`).
  Stage 2 refines without references, so the prompt is its only identity cue:
  on link 1 (stage 2 from 0.725) a hand-written cast sentence in the lab
  (`chain_lab.py --cast-line`) took the lead's apparent age in the 1080p
  finish from 40 to 36 and her similarity from 0.178 to 0.207. The studio's
  generated sentence ("Cast, the same age and look in every shot: ...") has
  not been measured. H3 chains do not use it.
- Each chain take gets a per-link cast report: identity similarity and apparent
  age of every cast member, link by link (`tool/face_drift.py`, stored as
  `draft_cast` / `cast` on the chain record). It is reported, not gated: the
  age estimate reads crying as older and is off by years in absolute terms.
- A drafted LTX link whose carry end fails a check its own sampling decides
  (pop, saturation, lightness or contrast; `seam_qa.carry_end_check`) is
  drafted once more with another seed, and the better attempt is kept
  (`LtxChainSpec.reroll_seams`). The kept seed is stored in the manifest; the
  other attempt is kept under `rejected/`.
- A carry end whose look still steps in the decoded take gets a 16-frame look
  path in pixel space at the delivery encode (`chain_assemble.look_path_plan`,
  `LtxChainSpec.look_path` / `H3ChainSpec.look_path`). The latent look match
  adjusts each 8-frame latent as a whole, so a change inside the first free
  latent survives it: on the v4 gate, link 2 ended on a growing sun glare and
  link 3 dropped it within 2 frames at both seeds (lossless draft take: pop
  1.51x, saturation step +7.79, lightness -4.87). Frames b..b+15 are matched
  one by one in Lab to the line from the last carried frame's look to the
  look 16 frames on (lightness in mean and spread, chroma in mean, then
  scaled so the HSV saturation follows its line as well); the seam then
  measured pop 0.83x, saturation -1.62, lightness -2.06 in the draft take and
  0.89x / -1.95 / -1.78 in the 1080p take, the lightness step passing on the
  scene's own rate of change. Seams that pass are not touched, and the
  lossless take stays as decoded.
- Seam QA no longer fails an audio level step where a sound starts out of
  silence or ends into it (the quieter side below -60 dBFS): with the audio
  joined in latent space that is content (a breath starting on the carry end),
  and it is reported as an onset. A step between two sounds, or a click, still
  fails.
- H3 links match their look across the carry end (`H3ChainSpec.seam_adain`,
  `StudioSeamAdain` path mode over 5 tokens). H3's video tokens come in
  groups of 5 whose first token covers one frame and the rest four, and each
  phase has its own level, so the match follows each phase on its own line
  (`period=5`). On the 544p smoke take the seam failed without it (pop 1.74x,
  saturation step +2.98, detail 1.21x) and passed with it (1.21x, +1.76,
  1.13x). Chains linked before keep their links unmatched.
- H3 link graphs no longer release the text encoder or the DiT before
  sampling (`H3Link.release_text_encoder` now off), as the studio's own H3
  workflows do not either; the upscaler pack's end-of-node unloads inside the
  refine graph (and inside the studio's H3 1080p workflows) are unchanged. The
  full release, together with the lab's `/free` between jobs, made H3 links
  re-stage the 11 GB DiT (on the smoke chain link 2's first sampling step took
  92.7 s against about 12 s for the steps after it, as slow as link 1's cold
  start), and an in-process model drop at an H3 job hand-over (the lab's
  `/free`, or the RAM-pressure cache, below) preceded each of the three blue
  screens during the H3 tests. Two of the three dumps (07:05, 14:46) were
  parsed: both fault inside the Windows GPU memory manager (`dxgmms2.sys`);
  the third (15:54) was not. Not every drop crashed: the lab's `/free`
  dropped a loaded DiT at four hand-overs in those tests, and two were
  followed by a bug check. The lab harness keeps an H3 instance warm between
  jobs instead of sending `/free`.
- An H3 chain restarts its server before a graph that would make it drop the
  DiT or text encoder (`H3Chain._fresh_if_dropping`; in the studio through the
  new `gpu_broker.restart_leased`, under the chain's own lease). ComfyUI's
  RAM-pressure cache releases every cached model the running graph does not
  use, so when the decode-only take graph (`decode_B0`) started at 15:53 the
  server's RSS fell from 34.6 GB to 2.9 GB within about 6 s, a minute before
  the third blue screen; the other two (07:05, 14:46) came within a minute of
  the lab's `/free` unloading the same models. Links run on in one warm
  server; by design a 1080p chain restarts it three times (before
  `decode_B0`, before the refine and before the refine's decode) plus the
  model loads that follow. Four restarts were measured on the lab's gate,
  before the refine and the decode of R0 and R25 (19-21 s each). The
  lab no longer sends `/free` to H3 when it stops it, and waits up to 90 s for
  the old process's VRAM to come back before it starts the new one.
- The H3 1080p refine runs in three steps, each in a server that holds only
  what it needs (`H3Chain.refine`): `refcond_<variant>` encodes every link's
  prompt with the references and saves the conditionings (new nodes
  `StudioSaveConditioning` / `StudioLoadConditioning`: tensors in safetensors,
  the structure as JSON, no pickle), `refine_<variant>` samples with the DiT
  and the upscaler only and saves the refined take latent, and
  `decode_<variant>` decodes it. In one graph (encode, upscale and refine; it
  had no decode) the gate's refine held 41 GB with the text encoder and left
  5-7 GB of RAM, at and below the RAM-pressure cache's headroom (10 % of RAM,
  about 6.4 GB here); a 1080p decode of the take (16 GB of float frames for
  four links) in the same process would have had to fit in that. Split, the
  steps peaked at 20.5 / 17.2 / 22.7 GB with at least 24.4 GB of RAM
  available.
- H3 gate (the old engine's 4-link chain re-rendered, 651 frames): the 1080p
  take passes every seam (pop 1.08 / 1.25 / 0.94, saturation step at most
  +1.29) where the old take failed all three (pop 2.2-3.1, +10 to +19); its
  overall verdict still fails on the drift gate (54 %, the story's push-in to
  a close-up). Identity to the references falls less along the chain than
  with the old engine (woman 0.34 -> 0.28, man 0.37 -> 0.38; old 0.32 -> 0.18,
  0.40 -> 0.25). One render per engine.
- Movie projects render a run of `CONTINUE` shots as one seamless take
  (`tool/movie_takes.py`, `Orchestrator._render_take`) when the shot model
  has the chain engine (`video_models.seamless_kind`: `ltx25_s6_cq` and every
  H3 tier): one chain with each shot's own prompt and one cast for the whole
  take, under a stable chain id, cut back into one clip per shot at the seams
  (frame-exact, audio cut at the same instants) so every shot keeps its own
  record, tail, post-processing and QA. A take stops at a cut, a match cut, a
  scene or location change, a fork, a model change, a retake, a forced
  text-to-video shot or after `movie.take_max_shots` (4) shots. A failed or
  transiently interrupted take is re-queued whole with the same seeds and
  reuses the steps already rendered. Projects can
  turn takes off (`seamless_takes`, Generate tab); `movie.seamless_takes`
  turns them off for the studio. See `docs/PIPELINE_AND_CONTINUITY.md`,
  section 4.4.1. Cutting the 4-link LTX gate take into shots and joining them
  as the assembler does gave back all 481 frames at 39.7-42.5 dB, the same at
  the seams as mid-shot (the CRF 14 re-encode), and the audio with the same
  962,000 samples at zero lag.
- Whole-take chain steps (the H3 refine and decode, the LTX take decode and
  audio) get the clip budget once per link (`StudioRunner.step_timeout`):
  the gate's 4-link H3 refine took 3653 s against a 2700 s step budget.
- LTX chain records carry `seam_frames`, like H3 records.
- Video Studio: a chain names the engine of the selected model; a ⛓ model
  with a finish pass (LTX-2.5, H3 1080p; `chain_finish` in the model status)
  can render a QA'd draft take first (**Preview first**), and **Finish 1080p**
  on the chain's History card renders the finish from the same drafts. The
  player shows the draft and final takes with their seam QA, drift and cast
  report. Reference cards have a **look** field (age, hair, outfit) for the
  LTX cast line; H3 chains do not add it to their prompts. The cast sheet
  gives it to the prompt writer for both.

### Video model catalog
- The model pickers (Video Studio, the project's Generate tab) group the
  registry by family and tier (LTX-2.5 · 22B, MiniMax H3 · 1080p, MiniMax H3 ·
  drafts, Wan 2.2 · legacy, Legacy) with short labels, the recommended tier of each
  group first (★) and the chain-capable tiers marked ⛓
  (`tool/static/js/model-catalog.js`). Each registry entry may carry a
  `catalog` block (`group`, `label`, `detail`, `rank`, `recommended`;
  `install/ADD-A-MODEL.md`); entries without one fall into their family's
  group under their display name. No model id or config changed.
- `GET /api/videostudio/models` returns the catalog groups and each model's
  `catalog`, `seamless` and `chain_finish` fields. `seamless` comes from
  `video_studio.seamless_engine`, the one check (registry plus
  `video_studio.seamless_chains`) that Video Studio chains, the movie's takes
  and the ⛓ marker share.
- New projects default to `ltx25_s6_cq` (the chain-capable LTX tier) instead
  of `ltx25_w4a8_refs`.

### Wan 2.2 is legacy
- Wan 2.2 TI2V-5B, the studio's first video model, stays in the registry but
  is no longer a default anywhere. It is not as good as LTX-2.5 or MiniMax
  H3. Its video entry is `status: legacy`, and its picker group and labels say
  "legacy, not recommended" (`tool/video_models.py`). The Images-tab `wan`
  entry is "Wan 2.2 (legacy)" and listed last (`tool/models.py`). No model id
  changed.
- One studio default, `video_models.DEFAULT_MODEL` (`ltx25_s6_cq`).
  `default_model_id()` returns it, or the first installed LTX-2.5 / H3 tier
  when it is missing, and never a Wan entry. Video Studio now opens on it;
  before, it opened on Wan, the one entry marked `production`. The Generate
  tab's empty choice now means it; before, it meant the Wan path.
- Movie pipeline (`orchestrator._video_family`): a project with no model, a
  tier that is not installed or an unknown id now renders on the default
  instead of the Wan path. Wan renders only when a project or shot picks it.
  With no LTX-2.5 or H3 tier installed, the shot goes to review with an error
  (`NoVideoModel`). Shots without a project (`/api/shots/ingest`) keep the Wan
  path.
- Keyframe candidates render with an Images-tab model (`keyframes.model`,
  default `realvis`), with that model's steps and CFG, at its size nearest the
  keyframe aspect. When the model takes reference images, a story reference
  goes through its IP-Adapter graph. `keyframes.model: wan` keeps the old Wan
  graphs.
- Dataset generation without Model Mix rows uses RealVisXL. Only the retired
  `standard` / `fastwan` stack names still map to Wan.
- `tool/scripts/warm_studio.py` warms the Wan graph only when
  `ensemble.warm_video` is true; a missing key used to mean true.
- The UI says where Wan still matters: video LoRAs, the cast LoRA pickers and
  the Training page apply to legacy Wan renders only. Post face-fix and V2V
  refine also run Wan graphs.

### Interface
- One sidebar for both pages (`tool/static/js/shell.js`): the Workspace's
  project pages, then Studio (Video Studio, Images, Training, Library) and
  System (Queue, Workers, Settings, Logs). Every entry is a link; moving
  between the Workspace and Studio Tools fades instead of cutting. The
  sidebar's GPU meter reads `GET /api/gpu/health`.
- Studio Tools no longer carries its own Production pages (Studio, Shots,
  Bible, Roster, Assembly, Agent): the movie lives in the Workspace. The
  global bible files and their git history moved to Settings → Bible files;
  the studio-wide upscaler and Close GPU apps moved to the Workspace's
  Generate page. Their API routes are unchanged.
- Video Studio: History (`GET /api/videostudio/history`) lists every clip
  and chain, newest first, including the movie's continuous takes and chains
  rendered before chain records existed (rebuilt from their files); before,
  chains never appeared. Cards play on hover and open a player with the
  details; ↪ continues from any finished clip or take
  (`continue_from` accepts a file `name`). The composer keeps the model,
  prompt with its chain segments, references and Continue in view, with
  sampling, cache, LoRAs and the worker under Advanced.
- Generate: renderer controls in the page header, long explanations moved
  into tooltips, per-tier models in a fold, and the page no longer rebuilds
  on every status poll (which closed open dropdowns). Shots: a header with
  Batch refs, three frames per card (start, clip or final, tail), one row of
  shot settings and a shorter footer (the Continue button duplicated the
  continuity select). Queue: the machine assignment picks a project from a
  list.
- One design layer (`tool/static/slate.css`) for both pages: controls,
  cards, folds, segmented switches, toasts, tooltips and motion.
- Image Studio LoRAs: the "Auto — apply the right LoRAs from my prompt" and
  "auto-add trigger words to prompt" checkboxes are gone (both were on by
  default), and so are the two LoRA rows that were preset. You add the LoRAs
  and set their strengths yourself, and the prompt is sent exactly as typed;
  each row still shows its LoRA's trigger words. The auto mode never worked:
  its keyword map pointed at LoRA files the Images tab does not list. The
  section is replaced by a note for models that take no LoRAs (only the SDXL
  models do).

### Fixed (production pipeline)
- The Film tab's Assemble built the film from the legacy `output/drafts`
  glob instead of the project's clips; it now sends the project, and project
  clips are joined in story order (the `prev_shot` chain,
  `assembler.project_clips`) instead of shot id order.
- Chain cast lines and face close-ups ignored references typed `character`
  (the type the Video Studio cards use); both `face` and `character` count as
  a person now.
- A shot's scene state was read from every project's shots with the same
  scene id (`scene_order` is now called with the project).
- `ProjectStore` gained the `error` column (setting a project to error used to
  crash) and per-column migrations for older databases.
- Movie assembly uses the project's own passed clips and writes
  per-project scene files (`output/scenes/{project}__{scene}.mp4`); shot ids
  repeat across projects, so scenes from other projects could be mixed in.
- Post-processing keeps the source audio, probes the source fps and only
  interpolates (RIFE) when the target fps is higher; face-fix results are
  propagated to the frames between the fixed ones.
- QA: freeze detection is advisory again; the ladder's adjust-LoRA stage is
  implemented (effect LoRAs are halved from `qa_stage` 3).
- Linter: a time-of-day change across a CONTINUE edge is a warning; an
  overflowing `power_level` no longer crashes the linter.
- Orchestrator: a single loop thread, and shots left in rendered/post are
  recovered on the first start.
- Story architect: one failed scene no longer aborts the job (stage
  `partial` with `failed_scenes`); unsafe ids from the LLM are normalised.
- Image Studio's Stop cancels only its own prompt instead of interrupting and
  clearing the whole render farm.
- Text-to-video is not offered on tiers whose t2v graph is a reference graph
  or drops LoRAs (`video_models.t2v_problem`), and image-to-video is not
  offered on LTX/H3 tiers without a reference graph
  (`video_models.i2v_problem`): their start image used to be dropped silently,
  and a chain continuation on such a tier failed to build.
- Qwen dataset jobs are routed to the model's own ComfyUI port.
- Training progress reads tqdm's `it/s` rate correctly (it was treated as
  `s/it`, inverting the speed and the ETA of fast runs). `weapon` and
  `outfit` datasets, which the trainer accepts, no longer crash the run with
  a `KeyError` when it is named and registered.
- Director: approving the review gate now continues to assembly; the gate
  used to be re-entered on every resume while any shot stayed flagged.
- Startup recovery leaves `post` shots to the orchestrator, which re-runs
  post on the raw render, instead of queueing them for a full re-render.
- The QA retry ladder sends a shot to review when the ladder itself is
  exhausted, even if the tier's `qa.max_retries` budget is larger.
- The `wan` image model and its defaults were restored to the registry (as a
  legacy entry, see "Wan 2.2 is legacy").

### Fixed (launcher, fleet, install)
- Launcher: no silent fallback to the base Python for the LTX/Qwen farms (the
  component is skipped with a message); external model folders come from
  `paths.extra_model_dirs` through a generated
  `temp/extra_model_paths.studio.yaml`; the hardcoded `D:\` fallbacks are gone;
  restart and shutdown sweeps only match the studio's own processes; the Job
  Object assignment uses a real process handle.
- `install/INSTALL-WORKER.bat`: model packages are copied file by file from
  `install/model_packages.json` (`install/copy_packages.ps1`); the previous
  robocopy filters copied nothing. The served installer keeps its embedded
  URL and token (the placeholder checks used to be rewritten as well), the
  fleet token is required, and every copy step is checked.
- Workers: a graph whose node classes the worker's ComfyUI lacks is refused
  before it is queued (a LAN worker runs the render farm only).
- `install/INSTALL-MAIN.bat`: requires Python 3.12 64-bit, fails on a failed
  pip install, and says what a clone-based setup does not install.
- `install/verify_install.py`: hashes are streamed (model files are up to
  ~13 GB), the manifest covers only untracked assets, and a fresh clone
  passes `--quick`. `install/manifest.json` is machine-specific and ignored.
- `install/model_packages.json` and `install/colab_model_groups.json` list the
  MSR LoRA the LTX graphs actually load (V1).
- `tool/requirements.txt` no longer pins `opencv-python-headless` next to
  `opencv-python` (both install the same `cv2` package).
- `tool/scripts/verify_restore.py` requires at least one installed LTX-2.5
  or MiniMax H3 tier, looked up through the video registry (extra model roots
  included), and warns when `ltx25_s6_cq` itself is missing. The IP-Adapter
  pair is hard when an SDXL model is installed. Every other file, the legacy
  Wan 2.2 set among them, is a warning, so a full verify on the development
  machine no longer fails on retired models.
- Colab `bootstrap.sh`: step 6 reads all eight arguments (it raised
  `ValueError` on every run, so no worker ever started); smoke mode no longer
  aborts on an unset `MAIN_URL`/`WORKER_TOKEN`; the reachability probe goes
  through Tailscale's HTTP proxy to the public `/api/health` and fails on an
  HTTP error.
- `tool/settings.example.yaml` sets `gpu_broker.prewarm: false` (the code
  default is true, which contradicted the "nothing loads at launch" note)
  and says what `tiers.model` really controls.
- The launcher's missing-venv message points to the guide that builds that
  venv.

### Fixed (ComfyUI custom nodes)
- MMH3 Split Upscale: progress messages are ASCII (a cp1252 console used to
  crash the render), and resizing a mismatched keyframe latent works on its
  5-D shape.
- Qwen-Image 2.1 nodes: the DMA timer events bracket the copies (the metric
  read ~0 ms before); conditioning tensors are handed to the compute stream
  safely; only the positive prompt is encoded (CFG is 1.0, the negative was a
  wasted second CPU encode); an over-long sequence is reported against
  `token_budget`; canvases smaller than the tile are refined as one window;
  the VRAM ceiling check uses the card's real size.

### CI and tooling
- CI installs a slim, pinned test set (`tests/requirements-ci.txt`), compiles
  every tracked Python file, runs ruff's definite-bug rules, checks the
  JavaScript syntax, and runs the hermetic suite and the install gate, with
  read-only repository permissions. Benchmarks run as an informational step. The broader ruff style set in `pyproject.toml` is not
  clean yet.
- `pyproject.toml` carries metadata and tool configuration only; the app is
  not an installable package and its dependencies are pinned in
  `tool/requirements.txt`.
- `tool/scripts/chain_lab.py --request <file>` runs a Video Studio chain
  request through the lab harness, with the chain spec built by the same
  functions `chain_studio.run_chain` uses (unit test
  `test_lab_request_builds_the_studio_spec_and_stages_refs_by_content`).
- `tool/scripts/showcase.py` renders the showcase stills on an isolated Qwen
  instance, records the picks, builds the hard-cut comparison, the cast
  identity report and the web encodes.
- GitHub issue templates (bug report, hardware report) and a pull request
  checklist in `.github/`.

### Documentation
- `SEAMLESS_CHAINS.md`, its parts 01-04 and an evidence index cover the
  seamless-chain work: what was measured (with N and run ids), what was
  inferred, what was replaced or corrected, and what has not been verified.
  The run records themselves are not in the repository.
  `SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md` §6 records the chains rendered
  through Video Studio itself (LTX-2.5: 3 links, 12.04 s at 1920x1088; 2
  links, 7.375 s at 1088x1920; seam QA FAIL as recorded) and what has not
  run on the GPU yet.
- `VIDEOLAB.md`, `VIDEOLAB_01`-`03` and `VIDEOLAB_ARCHIVE_INDEX.md` are copies
  of the videolab documents at the commit the `videolab/` snapshot comes from
  (`3f6cabc`), fact-checked against the
  videolab tree, the 93 H3 lab run logs and the H3 checkpoint header, with
  every correction listed at the end of its file. The `videolab/` folder
  keeps the original figures. Among the corrections:
  the H3 FLOP math used the 33B base model where the shipped pruned
  checkpoint has 19.27B linear-layer parameters, so the 960×544 step runs
  the linear term at 56-58% of the kernel-gate figure, not 93.5-100%; the
  15.5-16.6 s/step range was an 832×480 measurement; the run tagged as the
  int8 video VAE A/B used the fp16 VAE; the 243-frame refine was stopped by
  the lab's 90-minute timeout, not a driver watchdog.
- `docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md`, `docs/research/RETRACTIONS_AND_POSTMORTEMS.md`,
  `README.md`, `PINNED_VERSIONS.md` and `RESTORE_INSTRUCTIONS.md` were
  corrected to match.
- A second, independent fact-check of the seamless-chain documents against
  the run records, logs and code corrected them, this changelog
  section, `docs/PIPELINE_AND_CONTINUITY.md`, the hardware document and the
  code comments that repeated the same claims. Among the corrections: gate
  v2's stalled 1080p finish was past the token cliff, not out of commit (the
  938 s was job time to the first step); the H3 one-graph refine had no
  decode; `nvlddmkm` 153 is not recorded as a GPU reset; several
  printed-only numbers are now marked as such. `SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md` item 17 lists them.
  The check also found two code defects in the studio chain paths, fixed
  since ("Fixes in the chain paths" above;
  `SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md` §7).
- Showcase: `SHOWCASE.md` and `docs/showcase/` (27.8 MiB of re-encoded
  takes, GIF previews and stills). Three LTX-2.5 seamless chains, nine
  Qwen-Image 2.1 stills (the chains' cast portraits and opening frames) and
  two earlier Qwen stills, each with its inputs, step times, seam QA and
  flaws. Two MiniMax H3 chains and the 27-second H3 gate take are described
  in text only (see "No MiniMax H3 media" above).
  The README and SHOWCASE lead with warm times (a 5-second 1080p clip: about
  6 min with LTX-2.5, about 20-22 min with H3), and each chain's warm time is
  an estimate from its step records, shown next to the measured cold-start
  total. The `minimax_h3_hero_v2` catalog label now reads "~20 min warm".
- Every doc rewritten in plain English, with a breadcrumb, a short summary
  at the top and a section list on long pages. Each rewrite was checked
  number by number against the previous text, and facts changed only where
  the records or the code proved them wrong (the next bullets). New docs map in `docs/README.md`. The README now opens for
  people who are not engineers: what the studio is, the showcase, what it can
  do and whether your PC can run it.
- `tool/train/INSTALL.md` corrected against `tool/train/train_lora.py`: the
  command takes `--type` and `--dataset`, datasets live in
  `roster\datasets\face\<char>\`, and the output is `char_<id>.safetensors`.
  `ReferenceImages/README.md` now says only what the code does with that
  folder (face QA of shots without a project).
- Corrections found while re-checking the docs:
  `TOOLING_BACKUP.md` (the Laya checkpoint's real path, and it is a public
  Hugging Face download; locally trained LoRAs, which only a backup
  protects; `output/seamlab/` holds evidence, it is not regenerated; a plain
  `git add` now works for the four tracked `ComfyUI_LTX` files);
  `SEAMLESS_CHAINS_01_LTX.md` (gates v2 S5 and v3 finished in two steps from
  0.725, not three); `docs/PIPELINE_AND_CONTINUITY.md` (the LTX-2.5
  workflows do decode sound, so LTX segment chains are not silent); a third
  check of the `VIDEOLAB*.md` copies (lowest H3 peak power and the scope of
  three hardware figures, listed in each file).
  `ComfyUI_Qwen21/custom_nodes/ComfyUI-Qwen21-Optimized/README.md` now says
  which nodes the studio graph uses, and no longer calls the prompt enhancer
  a CPU node (its code runs it on the GPU).
- Two independent fact-checks of the README, SHOWCASE, the docs map and the
  contributor docs, applied where the records or the code proved them: four ComfyUI servers run from three trees, not four; new projects
  start on `ltx25_s6_cq`, not Wan 2.2; the showcase LTX
  chains ran without the CQ LoRA; the QA face check, the retry budgets and
  face-fix gating now match the code; the reproduce steps now work from a
  fresh checkout; the LICENSE exception for `nodes_lt_audio.py` (GPL-3.0) is
  stated in the README; the docs map now lists every doc in the repository.
