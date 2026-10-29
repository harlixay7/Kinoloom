[Kinoloom](README.md) › [Docs](docs/README.md) › Changelog

# Changelog

> **In short:** This is the list of notable changes to Kinoloom, grouped by
> area. Releases are tagged on GitHub; 0.1.0 is the first. The public
> repository starts with a fresh history at 0.1.0, so where an entry under
> 0.1.0 says something was renamed, removed or fixed, it compares with the
> private development tree it came from.

## [0.1.1] - 2026-10-29

### Model files you already have
- **Weights you already have are used, under any name and in any folder.**
  `install_components.py` (`INSTALL-MAIN.bat` mode [3]) looks for the files
  of the parts you choose in the model folders of `tool\settings.yaml` and in
  `models\`, then asks for the folder of your own copies (a ComfyUI `models`
  folder, say; Enter alone goes on), or takes it with `--use-folder <folder>`.
  A file counts with the name and size `install\components.json` gives it,
  or with that size and its SHA-256 under any other name; a hash is read
  once and remembered. Your files are never renamed, moved or changed. On
  the drive of the Kinoloom folder a file is hard-linked into `models\`: no
  extra space, and deleting either name keeps the other. On another drive
  it is used where it is when it sits in a ComfyUI-style folder under its
  own name (that folder becomes a model folder), and otherwise hard-linked
  on its own drive, in a `Kinoloom-models` folder at the top of that drive
  that becomes the first model folder: no extra space there either. Only a
  file on a drive that can't hold links (exFAT, or a network drive), or a
  post-processing file on another drive (ComfyUI reads those only from
  Kinoloom's own folder), needs a copy, and you are asked first; with
  Windows Developer Mode on, it gets a symbolic link instead. The plan
  shows each file found and how it will be used before anything is linked,
  copied or downloaded, and running it again changes nothing that is in
  place. `--h3-dir` is now another name for `--use-folder`, and finds the
  H3 files under any name too.
- **A model file saved in another letter case is renamed.** ComfyUI on
  Windows opens such a file, but its render graphs want the exact name in
  its list. A file in Kinoloom's own folders saved as `LTX-VAE.safetensors`
  for `ltx-vae.safetensors`, say, gets the catalog name from the installer,
  the launcher at start or the search; files elsewhere are never renamed.
- **Settings › Model files** in the web app lists every model file with
  where it is (in `models\`, linked or copied from where, or in a model
  folder), runs the same search, offers the copies, and adds or removes
  model folders; that changes only `paths.extra_model_dirs` in
  `tool\settings.yaml` and keeps the file's comments. The tooltip of a model
  that is not installed points there, and so does the launcher's banner when
  there are no weights yet.
- **Model folders count for the image models too.** `paths.extra_model_dirs`
  was read only by the video models. Image Studio, the keyframe fallback
  and `verify_restore.py` now find image weights there as well, and the
  launcher also mounts each folder's `ipadapter\` and ComfyUI's older
  `unet\` and `clip\` folders on every ComfyUI instance. Kinoloom's own
  `models\` still comes first.
- **Another build is not used in its place.** A file in a model folder with
  a catalog name but another size (another quantisation, say) no longer
  counts as installed, for the studio or the installer: the installer names
  it, leaves it as it is and downloads Kinoloom's own copy. Between model
  folders the studio counts the file ComfyUI loads, the first of that name.
- **Fixed:** adding a model folder handles every way the list can be
  written in `tool\settings.yaml` (the web app saves it without indentation),
  keeps the file's comments and line endings, and checks the result before
  it saves it.
- **Tested.** Unit tests cover the search, every way a found file is used
  (a link in `models\`, a model folder, a link on its own drive, a symbolic
  link, a copy), read-only files, letter case, running it again, the
  settings edits and the Settings page's API. On one PC, without
  administrator rights, with Kinoloom on C: and 57.3 GB of LTX-2.5,
  Qwen-Image 2.1, Z-Image and post-processing weights on a second drive, D:
  (read-only; most renamed, some in a ComfyUI install there, one saved in
  another letter case and one of another size):
  - mode [3] found 17 files: 11 linked in `D:\Kinoloom-models`, 2 linked
    into the Kinoloom folder, 3 used in the ComfyUI folder where they were,
    and one post-processing file (0.02 GB) copied. It named the file of
    another size as another build and downloaded Kinoloom's own. The files
    kept their names, sizes, dates and read-only flags. Hashing a renamed
    12.5 GB file on that drive (a hard disk) took about 2 minutes; the same
    run again took 5 s and changed nothing;
  - the studio's renders used the files where they were: a short LTX-2.5
    clip with two reference images and both its LoRAs, a Qwen-Image 2.1
    still and a Z-Image still. An earlier build rendered a CyberRealistic XL
    still with its checkpoint, IP-Adapter and CLIP vision files only in a
    ComfyUI folder on another drive;
  - Settings › Model files listed each link and the file it points to, and
    its search restored a deleted link.

  Not exercised: a drive without hard links (exFAT, say), symbolic links
  (Developer Mode), a mapped network drive, and a second PC.

### Qwen-Image 2.1 on RTX 50-series cards
- **The Qwen-Image 2.1 instance (port 8190) runs on PyTorch 2.13.0+cu130**,
  the build `ComfyUI_LTX\venv` already uses, instead of +cu126. The cu126
  build has no kernels for RTX 50-series cards (`sm_120`); the cu130 build
  covers `sm_75` to `sm_120`. ComfyUI v0.37.4, the node packs and every other
  package of `install\requirements-frozen\qwen21-venv.txt` keep their
  versions. On cu130, ComfyUI also turns on comfy-kitchen's CUDA kernels,
  which it leaves off below cu130.
- On an RTX 3070, the studio's Qwen graphs (fast, quality, and fast with two
  reference pictures) with the same seeds rendered the same pictures: same
  composition, faces and light, with differences of about 1 level in 255 on
  average along fine edges. Two runs on each build gave identical pictures,
  so the same seed still renders the same picture, and the build alone makes
  that difference. Speed was the same or a little better: in two runs each,
  the warm fast render took 40.4 and 42.6 s (42.5 and 42.5 s on cu126) and a
  12-step quality render 90.0 and 91.0 s (95.9 and 94.1 s). It has not run
  on a 50-series card.
- The cu130 build needs NVIDIA driver R580 or newer, like LTX-2.5 and
  MiniMax H3, and a GTX 16- or RTX 20-series card or newer: it has no
  kernels for GTX 10-series and older cards. On a card or driver that runs
  only cu126 (a GTX 900 or 10 card, or a driver from R560 to before R580),
  the installer builds the Qwen venv from
  `install\requirements-frozen\qwen21-venv-cu126.txt` instead: the 0.1.0
  list, with the same pins and torch 2.13.0+cu126. So a card that ran
  Qwen-Image 2.1 with 0.1.0 gets the same build.
- An existing `ComfyUI_Qwen21\venv` of the other build no longer matches its
  list, so the next install says so and builds it again. On disk the venv
  takes about 4.4 GB on cu130 and 5.7 GB on cu126.

### Install
- **`start.bat` no longer starts an unfinished install.** When the web app's
  venv (`tool\.venv`) is missing, still marked unfinished or no longer
  starts, it says so, shows the last install's failure report if there is
  one, and offers to run the install again. Before, it started the launcher,
  which stopped on `No module named 'uvicorn'`. The launcher checks the same
  marker, skips a ComfyUI part whose venv still has it, and when it stops
  restarting a part that keeps failing, prints the missing module or DLL from
  that part's log. Told at the end to start Kinoloom, `INSTALL.bat` starts
  it in the same window instead of opening a second one.
- **Every install is logged, and a failure is shown in the window.**
  `logs\install\install.log` gets every run: the PC (Windows edition and
  build, processor, free space, NVIDIA driver), each step with its time and
  commands, everything `install_components.py` and `verify_install.py`
  print, and each failure. When Windows PowerShell does not run
  `bootstrap.ps1` to the end (a policy of the PC can allow no scripts, or
  only signed ones), the log gets its execution policies and language mode.
  The log and the report are UTF-8: the window switches to UTF-8 while the
  install writes them, so a folder name in any script reads right.
  pip keeps a log per venv (`pip-tool-venv.log`, `pip-ComfyUI_LTX.log`,
  `pip-ComfyUI_Qwen21.log`; the run before as `*.prev.log`), without its line
  for every file it saw on a package index page. When a step fails, the
  window shows the step, what happened, what to do and the log lines that
  show it, and the same report is saved as `logs\install\LAST-FAILURE.txt`.
  It names the cause: a download that broke off or was refused, a server
  name the PC could not look up, a certificate check, a proxy, a full disk,
  an antivirus block, a file that is held open or not writable, a path that
  got too long, a hash mismatch, a package that did not build or was not
  found, a missing Media Feature Pack or DLL, or an install that was
  stopped.
- **Slow and unstable connections.** pip waits 60 s for a server (its default
  is 15), retries 10 times and resumes a broken download 10 times; a pip run
  that still fails on a network error runs once more after 30 s. With no
  connection at all, these retries take about 4 to 10 minutes before the
  report. Values set on the PC (`PIP_TIMEOUT`, `PIP_RETRIES`,
  `PIP_RESUME_RETRIES`) are kept. Python, Git and the Visual C++ runtime
  download with curl's retries, and with `Invoke-WebRequest`, which uses the
  Windows proxy settings, when curl fails. ComfyUI, the node packs, ffmpeg and
  weights from a direct link (`install_components.py`) download with up to 6
  tries, each going on where the last one stopped when the server allows it,
  and each download is logged with its size. A server that did not answer at
  all through those tries (the connection refused, its name not found, no
  route, or a connection that timed out) is not tried again in that run: the
  next downloads from it fail at once, with the same cause in the report, so
  mode [3] with no network ends within a few minutes.
- **SAM-2 is built against the installed torch.** pip built SAM-2 in an
  isolated environment, which downloaded a torch of its own for the build and
  unpacked it under `%TEMP%`, where its long paths could break the build. It
  now installs after the rest of `tool/requirements.txt` with
  `--no-build-isolation --no-deps` (the line is marked `[second-pass]`).
- **pip's downloads stay on the Kinoloom drive.** pip's cache and temporary
  files go to `temp\pip-cache` and `temp\pip-tmp` in the Kinoloom folder
  instead of the system drive, and are deleted when the install finishes. A
  `PIP_CACHE_DIR` set on the PC is kept.
- **Checks before the long steps.** `INSTALL.bat` and `start.bat` say when
  they run from inside a ZIP, and stop in a network folder
  (`\\server\share`), which cmd can't make its current folder.
  `bootstrap.ps1` asks before it installs into a OneDrive folder (OneDrive
  syncs and locks the files; the safe answer is no), and on a first install
  it checks the free disk space. After a venv is built, its main modules
  must import; on a Windows N edition without the Media Feature Pack the
  report says so. Step 4 checks that `tool\settings.yaml` was written, and
  says when the colour-grade LUT could not be.
- **pip 26.2.2 in every venv,** the version of the frozen base, installed
  first. It checks certificates against the Windows certificate store, so a
  network that inspects HTTPS with a certificate Windows trusts works.
- **Nothing waits on a hidden prompt or a sleeping PC.** The PC stays awake
  and QuickEdit is off in the window while the long steps run (the web app's
  packages and the render engines), so a click in the window no longer
  pauses them. pip and Git never ask for input, pip and the import checks
  run Python in UTF-8 mode, and pip settings of the PC that would change
  what is installed (`PIP_USER`, `PIP_TARGET`, `PIP_NO_DEPS`,
  `PIP_CONSTRAINT` and similar) are cleared for the install.
- **A second run needs no network for a finished web app venv.**
  `tool\.venv\.kinoloom-installed` records the `tool\requirements.txt` the
  venv was finished from (its SHA-256, with the pip pin). A run whose list
  has not changed only checks that the packages import. Before, every run
  installed the list again, which needs the network for the two packages
  from git, and a run that failed there left a working venv to be rebuilt.
  Now a run that cannot reach a server leaves a venv that worked as it was
  (pip downloads everything before it changes anything).
- **A moved Kinoloom folder is repaired.** A venv runs the Python its
  `pyvenv.cfg` names, which is gone once the folder has moved. `start.bat`
  says so, the next install run builds such a venv again (the web app's and
  the ComfyUI ones), and the launcher names the cause when it stops
  restarting a ComfyUI instance that fails on it.
- **An unfinished venv stays marked.** The `.kinoloom-incomplete` marker is
  written before a venv is created and removed last when one is deleted, so
  an install stopped while it creates or deletes a venv leaves one that
  `start.bat`, the launcher and the next run treat as unfinished.
- **Behind a proxy.** pip reads the proxy in the Windows settings itself,
  with its list of addresses that bypass it. git, which pip runs for the two
  packages from git, does not, so it now gets that proxy as its own
  `http.proxy` setting for the install (`GIT_CONFIG_*`), unless git has a
  proxy of its own or the PC sets `HTTP_PROXY`/`HTTPS_PROXY`.
- **A damaged download fails only its own part.** A ComfyUI or node pack
  archive that does not unpack (damaged, or a guest network's sign-in page
  instead of the zip) is reported with that cause, and the other parts go
  on. Any other error in `install_components.py` or `bootstrap.ps1` (a
  damaged `install\components.json`, for one) ends in the same report
  instead of a traceback, and a part that failed without a report gets one
  from `INSTALL-MAIN.bat`.
- **Fixed: folder names.** In a folder whose path has a `%` or `^`,
  `INSTALL.bat` and `start.bat` could not find the script they call next
  (`call` read the character as its own), and Python and MinGit did not unpack
  in a folder whose path has a letter outside the PC's ANSI code page (Chinese
  on a Western PC, for example). Both work now, as do `&`, `!` and `'`. In a
  folder whose path has a `'`, the face detector's export (part of
  post-processing) failed with "No such file or directory", since Ultralytics
  drops that character from a model path; it works there now too. A path with
  `[` or `]` stops the install before anything is installed, since Windows
  PowerShell and parts of Kinoloom read them as wildcards.
- **Fixed:** every run after the first counted the Qwen-Image 2.1 venv as
  unfinished and built it again, downloading torch once more. Its list pins
  `torchaudio==2.11.0`, and the cu126 index installs `2.11.0+cu126`, which
  that pin allows; the check compared the two as text. A pin without a local
  label now matches the build pip installed for it.
- **Fixed: what is typed at a prompt.** A quote or an `&` typed at the
  yes/no question of `INSTALL.bat` or `start.bat` stopped the script, and a
  folder pasted in quotes (Explorer's "Copy as path") at a prompt of
  `INSTALL-MAIN.bat` did not work when its path has an `&`. Both work now.
- **Fixed:** `INSTALL-MAIN.bat` treated a step that ended with a negative
  exit code (a crash, or the window's Ctrl+C) as a success. Every step's
  exit code is now checked exactly. `INSTALL-MAIN.bat /auto` exits 0 when
  everything installed, 2 when some parts did not, and 1 otherwise.
- **Fixed:** `INSTALL-MAIN.bat` modes [1] and [3] ended with "DONE. Start the
  studio" and exit code 0 when a part did not install, so its report scrolled
  away behind it. They now end with NOT FINISHED, say the parts that did
  install work and that a run again finishes the others, and exit 2.
- **The graphics card and driver are checked.** LTX-2.5 and MiniMax H3 run
  on a CUDA 13.0 build, which needs a GTX 16- or RTX 20-series card or newer
  and NVIDIA driver R580 or newer. The render farm (the SDXL image models,
  Z-Image, post-processing and the legacy Wan 2.2) runs on CUDA 12.6, which
  needs a GTX 900- to RTX 40-series card and R560: its PyTorch holds kernels
  for `sm_50` to `sm_90`, none for the RTX 50 series. Qwen-Image 2.1 is
  checked against the build its venv holds. `tool\gpu_support.py` asks
  `nvidia-smi` for the card, its compute capability and the driver. The
  install's last check prints them and, for each build the PC cannot run,
  why (for an old driver: update it from nvidia.com/drivers). The launcher's
  banner names the card and driver; it does not start a ComfyUI server the
  card cannot run and says which ones it left out and why. The model lists of
  Video Studio, Image Studio, the Render page and the Shots page's start
  pictures show such a model as "not on this graphics card" with the reason,
  instead of "not installed"; start pictures then open on the first image
  model the PC can run, and a cut opens without a Qwen-Image frame. When
  `nvidia-smi` does not answer, nothing is refused.
- **Tested.** Unit tests run the real `INSTALL.bat`, `start.bat` and
  `INSTALL-MAIN.bat` against stand-in steps, in folders whose names have
  spaces, `&`, `%`, `^`, `!`, `'` and letters outside the ANSI code page,
  from a network path and on code page 437, and parts of `bootstrap.ps1` in
  PowerShell. End to end on one PC (Windows 11 Pro 25H2, RTX 3070 8 GB,
  driver 616.64), from a source archive of a release candidate, with no
  Git or ffmpeg on the PATH (the release differs from it in hydra-core
  1.3.8, the Shots page's next-batch button, one disk figure in
  `bootstrap.ps1` and two lines that matter only on Linux):
  - a first install, in a folder named `Kino & 100% ^ !'s (7) é`: done in
    19 min 58 s, with 6.3 GB downloaded. It needed 25.2 GB of free space at
    its peak, and the folder then holds 16.2 GB. Its last check named the
    card and driver and the parts they run. A run again rebuilt nothing
    and was done in 10 s;
  - mode [3] with LTX-2.5 and Qwen-Image 2.1, given a folder that held
    their 13 files (44.3 GB): all linked in 5 s, nothing copied or
    downloaded. The install's own ComfyUI servers then rendered a
    2048×1152 Qwen-Image 2.1 still and, from it, a 640×384 LTX-2.5 clip of
    25 frames, and the web app listed both models as available;
  - mode [3] with every connection refused ended with NOT FINISHED and
    exit code 2 after 3 min 13 s, and with a proxy whose name doesn't
    resolve, after 31 s, each with its report. With the network back, the
    refused run was done in 61 s;
  - as on a GTX 1080 with driver 560.94 (through a stand-in `nvidia-smi`):
    the Qwen-Image 2.1 venv got the CUDA 12.6 build, which rendered a still,
    and the last check and the web app's model lists gave the reason the
    card can't run LTX-2.5 and MiniMax H3;
  - 0.1.0 with a project in it, the 0.1.1 files copied over it and
    `INSTALL.bat` run again: done in 9 min 47 s. It built the Qwen-Image 2.1
    venv again on CUDA 13.0 (2.6 GB downloaded) and kept
    `tool\settings.yaml` as it was, and the project opened and saved its
    edits with 0.1.1. As on the GTX 1080, the same update kept the CUDA 12.6
    venv and took 2 min 38 s.

  A second PC, with a GTX 1080, installed a release candidate and started
  it; nothing was rendered there.

  An earlier build with the same `bootstrap.ps1` and `start.bat` also ran
  the failure paths: a package index that refuses connections (the report
  after about 10 minutes) or whose name doesn't resolve (about 4 minutes),
  the OneDrive question answered no and yes, the window closed in step 3,
  and the folder renamed after the install. `start.bat` did not start an
  unfinished install, and after the closed window and the renamed folder
  the next run built the venvs again.

  Not exercised in these runs: the Visual C++ runtime install, the
  long-path prompt, a real proxy, a PowerShell that a policy blocks, a
  Windows N edition, a Windows in another language, and a render on a
  graphics card other than the RTX 3070.

### Renders
- **Fixed:** on a studio where MiniMax H3's server does not run (one without
  the LTX-2.5 tree, say), the first render after each start, and after the PC
  wakes from sleep, waited about three and a half minutes: the GPU check
  restarted that server and waited for it to come back. With nothing running
  there, it now goes on. In a test install with no ComfyUI running, the first
  render could start after 33 s instead of 212 s.
- **Fixed:** when the GPU broker restarted a ComfyUI server to free the
  card's memory (Qwen-Image's, when its models did not unload), the launcher
  reported it in red as a server that died and counted it toward the crashes
  after which it stops restarting one. It now says the broker restarted it
  on purpose and starts it again at once. A restart mark older than 5
  minutes counts as a crash again.

### Video Studio
- **Start from** is a switch, **Image** (the default) or **Text only**.
  Before, the mode menu started on T2V and the start image field appeared only
  after switching it. The start image sits under the switch: Browse, drop a
  picture on it, or type the name of a file already in the ComfyUI input
  folder. A thumbnail shows a picked file, and a line says what the selected
  model does with it: H3 opens exactly on it; LTX-2.5 takes it as the key
  image, which guides the render but is not pinned as the first frame. With
  Image selected and no picture, the clip renders from the prompt alone, as
  before.
- **Length** sits beside the prompt and shows the video's length and number
  of segments ("10 s · 2 segments"). Its **+** adds a segment and **−**
  removes the last one, with no upper limit; it replaces the **+ Segment**
  button. A line under it says how the segments render. An empty segment box
  now stops Generate with a prompt-check error. Before, it was skipped, so a
  chain could have fewer segments than shown.
- The LTX-2.5 10 s tier is now "Slow motion 10 s (not recommended)", last in
  its group, with a note under the picker in Video Studio and on the movie's
  Render page. It stretches one 5 s clip, so the extra time adds no new
  action; for a 10 s video, use Quality 5 s with Length 10 s. Its registry
  note no longer says the tier has no audio: its graph attaches the 5 s
  render's audio track, unstretched. The H3 draft "Cinematic 768p
  single-pass" is now "768p single-pass".
- The question before a clip without references is now "No start image or
  references". It is not asked when Text only is picked on LTX-2.5.

### Movie pipeline: the Video Studio's request and written prompts
- **Movie shots on LTX-2.5 and H3 render the way Video Studio renders them.**
  Each unit (one shot, or a seamless take of shots that continue each other)
  gets exactly the request the Video Studio page sends: the cast as
  references with their names and looks, a start image or the previous
  clip's last frame, the page's size and frame count, no negative prompt,
  the model's own steps, and the same prompt check as Generate. Before,
  movie shots ignored the start images picked on the Shots page, their
  prompts came from a template, and an H3 take whose cast had no pictures
  went out with empty prompts.
- **Prompts are written by an AI or by hand, never from a template.** The AI
  gets a brief per unit: the story so far, where the shot before left off,
  the shots with their cast, dialogue and sounds, the shot that comes next,
  and the model's rules for its pictures. When the endpoint supports
  function calling, it can also look up shots, scenes, characters and
  earlier prompts, check its prompts, and hand them in. Its reply goes
  through the prompt check: the check's own fixes are applied, and errors go
  back to the AI, at most twice. Each prompt remembers what it was written
  for. When the shot, how it opens or its cast changes, an AI prompt is
  written again and a hand prompt is marked stale.
- **The Shots page** shows each shot's prompt and its state (ready, missing,
  stale, rendered), with a prompt editor, **Write the prompts** (a background
  job with its progress and a Stop button) and **Use a chat AI**: a brief to
  paste into any chat AI, whose reply is pasted back and stored by its
  `Shot <id>:` blocks. A shot without a prompt it may render with waits
  with the reason (status `prompt`), and saving its prompt queues it again
  (when production has stopped for prompts, it renders on **Continue
  production**).
- **The AI writer** (System menu, Settings › AI writer, and the Story and
  Shots pages):
  - provider presets for OpenAI, Google Gemini, OpenRouter, DeepSeek, Groq,
    Mistral, Ollama and LM Studio, or any other OpenAI-compatible endpoint;
  - a key field with a link to the provider's key page, plus **List models**
    and **Test**.

  The key is shown masked and is cleared when the endpoint moves to another
  host. An AI on this PC writes only while no render uses the GPU, and
  Ollama unloads its model after each unit.
- **Opening frames.** A cut with no start image of its own opens on a frame
  made just before it renders, with Qwen-Image 2.1 fast. The frame is drawn
  from the cast's pictures and the opening image prompt the AI wrote with
  the shot's prompt. Frames are made only for shots that open on one, up to
  8 under one model load, and kept: the same prompt for the same shot is
  never made twice. `movie.opening_frames` turns them off.
- **Continuity follows Video Studio.** `CONTINUE` is the same camera take: a
  clip continues the previous shot's last frame, and a run renders as one
  seamless take. `MATCH_ACTION` is a cut to a new angle while the action
  carries on, so it opens like a cut instead of on the previous frame. The
  Story Architect's shot rules and the linter say the same. The linter warns
  about a run of `CONTINUE` shots longer than a take (the project's own
  length, else 6 shots on LTX-2.5 and 4 on H3) and about a match cut that
  keeps the shot size and camera; before, it warned after two bridges in a
  row.
- **An LTX-2.5 take renders like a Video Studio chain**, so every join inside
  it is a latent carry; how many shots it holds is under **A take holds 6
  shots on LTX-2.5** below. Before, a take stopped at 4 shots and the next one
  opened on a still of the last frame. An H3 take still holds at most 4
  shots, because its 1080p refine holds the whole take in memory:
  `movie.h3_take_max_shots` (an older `movie.take_max_shots` now applies to
  H3 only).
- **The Director has a `prompts` stage** after `shots`. With an AI set up it
  writes the prompts; the shots that have theirs render, and production
  stops at the shots still without one (see **Production renders the prompts
  it has** below).
- **Retakes take a new seed.** **Render again** on the Shots page and a QA
  retry give an LTX-2.5 / H3 shot its seed + 104729 with the same prompt. A
  failed render keeps its seeds, so a take's retry reuses its finished steps.
  A retake inside a take renders the take again under its chain id, with each
  shot's own seed: the shots before it reuse their finished steps, so the
  latent carry into it stays, and only it and the shots after it take GPU
  time. A rendered shot that continues the take renders again. Before, a QA
  retry's seed was replaced at render, and a retake inside a take rendered
  that shot alone from a still of the shot before.
- **Video Studio chains take one seed per segment** from in-process callers
  (`video_studio.start_chain(seeds=...)`), and a seam re-roll's kept seed
  applies only while the link keeps the seed it was rolled from.
- **A `CONTINUE` shot waits until the shot before it has rendered.** Before,
  queued shots rendered before pending ones, so an edited `CONTINUE` shot
  could render before the shot it continues.
- **Compact shots hold more.** The Story Architect's compact shot entries take
  `with` (the other characters on screen, so each gets a reference picture),
  `end` (the shot's last frame, for the shot that carries on from it), and
  `tier` together with `continuity`. Before, a compact shot had one
  character.
- **Fixed:** the linter gave MiniMax H3 projects the LTX-2.5 reference budget
  (5 pictures instead of 6).
- **Fixed:** importing a story replaced each character's powers with the AI's
  text and dropped the power-level pictures added on the Cast page. The AI's
  text is now merged into each power.

### Movie pipeline: from the idea to the render with any AI
- **The story is written with a chat AI or with the AI writer, by the same
  prompts.** On the Story page, **Copy prompt** gives the whole film in one
  reply for a short film (up to 60 shots) and the outline for a longer one (an
  hour is 26 scenes and 720 shots); before, it asked one reply for every shot.
  Then each scene row has its own prompt and **Paste reply**. The AI writer
  sends the same text, with the authority rule (ids exactly as the cast lists
  them) and the cast as it is (see **The story keeps to the cast**). Each
  scene prompt now also carries the story around the scene: the logline, every
  scene on one line, how the scene before ended and the scene that follows, so
  a model that writes one scene at a time knows where it sits.
- **One click writes a scene.** With an AI writer set up, each scene row has
  **Write**, and the list has **Write the *N* missing scenes**. **Rewrite
  part** on the Story page and **Rewrite the story around this cast** on Cast
  & World use the AI writer too; before, they ran the built-in generator from
  templates. Without an AI writer, Cast & World's rewrite opens the Story
  page, whose prompt carries the cast.
- **Start production goes from the story to the render.** The Director has the
  AI writer write the outline, then the scenes that have no shots. With no AI
  writer, it waits and says what to paste and where, and the scenes already
  written go on to their video prompts and their render. The film is cut once
  every scene has its shots (deleting a scene removes its beat), and a
  finished project runs again on Start when it has new scenes or shots to
  render. Before, the Director wrote a story from templates when there was
  none, cut a film from whichever scenes had shots, and a finished project
  ignored scenes added later. The Story page and the Director share one AI
  writer, so a project is written by one run at a time, and **Stop** stops it
  too.
- **A reply's names take the cast's spelling.** A chat AI that writes `sara`
  for `Sara`, `Calm` for the palette's `calm` or `Close_Up` for `close_up` is
  read as meaning the existing names: characters, crowds, places, the
  project's scenes, powers, weapons, loadouts, named views, emotions and the
  shot vocabulary. **Fixed:** on Windows such a reply replaced the character's
  file with the reply's text, and its weapons, loadouts and picked looks were
  lost; every shot that named the character then failed the check. No id can
  now be saved that differs from an existing one only in capitals, from the
  story import or from Cast & World's **New character** and **New place**.
- **A refused reply says why, and only a scene's own problems refuse it.** The
  paste window stays open with every problem listed and **Copy a fix
  request**, to paste into the same chat. A scene that is imported reports the
  problems still left in other scenes; a reply that also holds other scenes
  imports only the one pasted for, and names the others. **Fixed:** one scene
  with a problem made every later scene import fail with only "lint failed",
  and such a problem also hid the shot-order checks of the other scenes.
- **An AI writer that is not running is reported in seconds.** A fresh install
  has no AI writer set up, and the Story page checks that the one set up
  answers ("Set up, but nothing answers at …"); saving the settings lets the
  writers ask again at once. The story writer and the prompt writer stop at
  the first refused request and name the address instead of trying every scene
  or unit, and the shots that already have a video prompt render while the
  others wait. Before, every install counted a local Ollama as connected, and
  **Start production** could spend minutes on retries with nothing on the GPU.
- **Fixed:** the AI writer no longer copies the global story bible into a
  project before it writes. For a project without its own universe file,
  that copy cleared the project's characters, places and scenes.
- **Video prompts from a chat AI, batch by batch.** **Use a chat AI** on the
  Shots page gives a brief for the next prompts of one scene, up to
  `movie.prompt_segments_per_reply` (4; a take with more is written in parts),
  and says how many prompts are ready and how many batches are left. **Import
  & next** saves the pasted reply and shows the next brief, made from the
  prompts just saved, so every batch carries the end of the prompt before it;
  a refused shot stays, with **Copy a fix request** for the same chat. A brief
  says the film (every scene on one line), the story so far and the render
  model's rules once, and each shot only what is its own: a brief for 8 shots
  of the 3-minute test film (measured when a batch held 8) took about 14,000
  characters, where the single brief for 24 took 112,000 and its later shots
  could not see the prompts written before them. Once a first batch is in, the
  button reads **Use a chat AI for the next batch**, and so does the one in the
  note under the top bar; without an AI writer it is the Shots page's main
  button. Each shot's prompt editor has **Use a chat AI** too, for that shot or
  its take alone.
- **Start production says why it waits.** When production stops at once (no
  story, scenes without shots, or shots without video prompts and no AI
  writer that answers), the button answers with the reason, and a note under
  the top bar keeps it on every Movie page with the one action that moves it
  on (**Write them with a chat AI**, **Open the Story page**). While shots
  render, the note counts the shots that wait for a prompt. Before, the
  reason was only a tooltip on the status, so Start looked like it did
  nothing. With an AI writer set up but not answering, **Use a chat AI**
  becomes the Shots page's main button.
- **Production renders the prompts it has, stops where they end and goes on
  from there.** With video prompts for only some shots (a first batch pasted
  from a chat AI, say, and no AI writer), every shot that has its prompt
  renders, then production stops at the shots without one and the shots that
  open on them (the rest of their take, and a match cut in the same place).
  The note says how many shots are rendered and which ones need a prompt,
  and the render queue stops with it. The prompts pasted or written next
  render on **Continue production**, which the chat window also offers after
  **Import & next**. Before, the shots behind the first missing prompt stayed
  pending, and the project showed "rendering" with nothing on the GPU until
  it was stopped. When the AI writer did not answer or could not write a
  prompt, the stop says that, with the AI writer's address.
- **The story keeps to the cast.** The story writer can't see the cast's
  pictures, so the looks it wrote pulled the renders away from them. Now an
  existing character keeps its name, tier, look (body and outfit), powers,
  weapons and loadouts; the story bible adds its arc, and a voice style and
  emotions where none were set. Places, crowd types and camera defaults the
  cast has stay as they are, and a character or place the cast does not
  have is added only to a project with no cast yet (otherwise the shots that
  use it fail the check). Every story prompt lists each character's look as
  fixed, and the Story page shows the cast as it stands.
- **A picked look writes the character's look.** Picking a look from a
  Casting batch fills an empty Body and Outfit from what the picture was made
  with (the **Fill in from a Casting picture** text), and a character card
  with pictures but no look says so. Compact shots no longer get a made-up
  "foreground centre, facing the camera" on every character.
- **Every prompt keeps its shot.** A prompt for an LTX-2.5 clip that leaves
  out a person's look from the cast gets the cast sentence the seamless takes
  already carry ("Cast, the same age and look in every shot: …"); the Video
  Studio's check offers the same fix. A prompt written by an AI or pasted
  from a chat AI must speak the shot's dialogue word for word (refused with
  the line otherwise; a warning for a hand prompt), and a name of someone not
  in the shot is a warning.
- **A local AI writer that is not running is found out at once.** The prompt
  writer asks whether the AI answers before its first request, instead of
  after the request's retries, and an AI on this PC that does not answer is
  no longer waited for behind the render that holds the GPU.
- **Cast & World.** **New place** asks for the place's name and numbers a
  name already taken; a place is renamed by typing its new name and pressing
  Enter. The batch picker selects a picture on a click and adds it with **Use
  this picture**; a double-click adds it once, and the card's picture count
  follows. When another program holds the project's copy of a picture open
  (a viewer, the virus check), the copy is tried again for a few seconds,
  then the picker names the file that is held.
- **Fixed: changes on Cast & World were lost.** A character's name and part,
  and whether a character or place is in the movie, sit on the card's head,
  where nothing saved them; they now save as they change. Edits in an open
  card mark it **Unsaved changes** (its button reads **Save changes**), the
  page no longer draws itself again over them when the project changes, and
  leaving the page or reloading it asks first.
- **Tested.** Unit and integration tests cover the name matching, the case
  guard, the scene import, the AI writer's modes, its one-run claim and its
  stop on an endpoint that does not answer, the Director's stages and the
  reachability check, and the chat batches, the cast contract of the story
  bible, the look written from a picked picture, the story checks of a
  prompt and Start's answer. A test runs a real render queue with stand-in
  renders on a five-shot scene with the first take's three prompts pasted:
  the take renders, production stops at the fourth shot and the match cut
  that opens on it, and after the next reply is pasted the fourth renders
  and it stops at the fifth. The Story, Cast & World, Shots and Start
  production flows were checked in a browser on a copy of a project, with no
  AI writer and with one that does not answer; nothing rendered on the GPU.

### Movie pipeline: continuous takes and a cast that stays itself
- **The story is written in takes.** A scene is a few takes; a take is one
  continuous camera passage in one place, its shots consecutive moments that
  render as one video. The cuts follow from the takes: another place is a
  cut, a new take in the same place a match cut (a new angle while the
  action goes on), and every other shot continues the one before. Before,
  the story AI marked each shot itself, and most shots of a scene came out
  as cuts that each opened a new clip. The story rules also keep faces at
  medium range or closer for dialogue, emotion and hero moments, use wide
  shots only to set a place or show big movement, and stage the action in
  the set the place's picture shows. The older shot format still imports.
- **A take holds 6 shots on LTX-2.5, and that is a setting.** An 8-shot
  LTX-2.5 take kept the cast in every shot; shots 7 and 8 had larger steps
  at their joins than any before them, though none showed. So
  `movie.ltx_take_max_shots` is 6, and **Shots per take** on the Render page
  sets a project's own (empty for the studio's). H3 takes stay at 4
  (`movie.h3_take_max_shots`). A longer run goes on in a new take that
  opens on a new angle made from the last frame of the one before.
- **The look comes from the picture.** A character's Body and Outfit text
  comes from the Casting record of its main picture and follows it when
  another picture becomes the main one; text typed by hand is never
  replaced. A text that names the other gender, another age or other
  clothes than the picture is shown on its card with **Use the picture's
  look** and **Keep mine**, and Start production waits until it is settled.
  Every prompt says woman or man: a Body text without the word gets the
  picture's. Places are described by their pictures' records too, so the
  story is staged in the set the video model receives.
- **A cut opens on a frame made from the cast's pictures.** A cut without a
  start image of its own opens on a frame Qwen-Image 2.1 makes from the
  shot's opening-image prompt, one picture per person (the outfit or loadout
  still the video model gets) and the place's picture; a match cut's frame
  is made from the last frame of the shot before, and made again when that
  shot renders again. Of two candidates the one most like the cast is kept,
  and under `movie.key_image_min_likeness` (0.25) two more are tried. In the
  key-image tests a face close-up next to the still drew the lead more than
  once in 2 of 16 frames, and the close-up alone drew another outfit in all
  10, so each person gets one picture, the still. With Checkpoints the
  project stops before the render while the frames are looked at on the
  Shots page, where a frame's refresh button makes it again; the frames
  passed there stay passed after a Stop.
- **Start pictures are made with Qwen-Image 2.1 fast.** The Shots page's
  **Start pictures** and a shot's picture studio open on Qwen-Image 2.1 fast,
  the image model the installer sets up (it does not download RealVisXL), or
  on the first installed image model when it is missing. A Qwen-Image picture
  is made on Qwen-Image's own server, from each person's still and the
  place's picture as the opening frames are; before, these windows opened on
  RealVisXL, and a Qwen-Image picture was sent to the SDXL server, which has
  no Qwen-Image, without the cast's pictures. The picture studio shows the
  pictures the chosen model gets, and controls a model does not use (LoRAs,
  the sharpen pass, the fast mode's negative prompt) are hidden.
- **A long take's prompts are written a few at a time.** An AI reply writes
  at most `movie.prompt_segments_per_reply` (4) of a take's prompts, with the
  whole brief and the prompts written before it; then the whole take is
  checked, and only a segment the check names is written again. A chat
  brief follows the same parts. The cast sheet tells the writer what the
  video model renders well and badly (MODEL LIMITS for LTX-2.5 and H3), and
  the story writer gets the same words.
- **The QA checks the cast on LTX-2.5 and H3 shots.** A shot's last frames
  are compared with the identity pictures of the people it rendered with.
  A face large enough to judge (`qa.cast_face_size`) that is less like the
  cast than `qa.cast_face_min` (0.25) sends the shot to review with its
  score, without a retake. A face too small, turned away or off screen is
  not a failure. Each shot of a take also keeps the take's face report for
  its part. The prompt-to-picture score is recorded for these shots and
  never fails one: on the approved takes it was as high against the next
  shot's prompt as against the shot's own. A prompt longer than the score's
  77 tokens, which gave no score, is cut there for these shots.
- **A continuity change says what it renders again.** Changing a shot to
  continue or to cut on the Shots page first lists the rendered shots it
  would render again (in its take, and the shots that open on a last frame
  that changes), and asks.
- **Tested.** Unit and integration tests cover the takes format and its edit,
  the take length and the split, the opening frames' references, candidates
  and likeness, the match cut's frame, the keyframes stop and a Stop after it,
  the prompt writer's parts, the QA's cast check and its review, the take's
  face report, the continuity warning and what it renders again, the
  take-length setting, and the start pictures' default model, server and
  references. The Shots, Render and Cast & World pages were checked in a
  browser on a copy with a test movie. On the GPU: the 8-shot take, the
  opening-frame comparison, and one five-shot LTX-2.5 scene through the movie
  path, run by the studio's code outside the web server: two takes joined by
  a match cut, every shot passed the QA and all six seams passed the seam QA.
  Its whole-take measures failed both takes, as they fail the showcase's
  cliff-walk take ([MOVIE_CONTINUITY.md](docs/research/MOVIE_CONTINUITY.md)
  §1, §2 and §4).

### Interface: a Movie side and a Studio side
- **Two sides, one sidebar.** A switch at the top of the sidebar picks
  **Movie** or **Studio**, and each side opens on the page it was last on.
  Movie lists the steps of a film in the order they are done, numbered:
  Casting, Cast & World, Story, Shots, Render, Review and Film, with Overview
  above them. Each step shows whether it is done, partly done, running or
  still to do, and Overview names the one thing to do next, with a button that
  goes there. Studio holds Video Studio, Image Studio and the Library.
- **Production that stops for you says so.** When production waits at a
  checkpoint or stops with an error, a dialog on either side says what
  it waits for, in the director's words, and opens the page that needs you
  (opening frames: Shots; the cast's look text: Cast & World); the tab's
  title starts with "Waiting for you" or "Stopped" until it goes on, and
  while the window is in the background a desktop notification says the
  same (the browser asks once, on Start production). Each stop is announced
  once, and closing its dialog settles it for every tab. The note under the
  top bar gives the same reason and a link to that page. Before, a stop
  reached minutes after Start showed only in that note.
- **The System menu** at the foot of the sidebar, under the GPU meter, holds
  the Render queue, Worker PCs, the AI writer, Settings and Logs. Before,
  each had its own row.
- **Renamed pages.** Training is now Casting (step 1: make the cast's
  looks), Generate is Render, Images is Image Studio, Queue is Render queue
  and Workers is Worker PCs. Cast & World now comes before Story. Links to
  the old names (`#training`, `#generate`) open the new pages.
- **Wan 2.2 LoRA training is out of the way.** It applies to the legacy
  Wan 2.2 model only, so Casting no longer shows it as a tab: a line at the
  foot of the page opens it and the trained LoRAs, and the caption, licence
  and train steps of a batch sit in a closed "Wan 2.2 LoRA training" fold.
  Anatomy and quality batches are marked legacy.
- **The AI writer has templates.** Each provider is a card with a line on
  what it is and a few model names to start from, grouped into "In the
  cloud, with a key" and "On this PC, free". After **List models**, a
  suggested model that the endpoint does not list for your key is marked.
- **Casting** works one batch at a time: pick it from a list, then keep or
  reject pictures on cards with round selection marks. Each batch card has
  its next actions on it: Pick the best, Finish the batch, Send to Cast &
  World.
- **Image Studio** asks for a model, a prompt and a shape, in that order.
  Steps, guidance, seed, the worker, the negative prompt, upscaling, LoRAs
  and references sit in folds. Models list the installed ones first, with
  their variants together, and Wan only when it is picked.
- **The Library** shows the count on each tab, skips empty sections and
  shows each video's length on its poster.
- **Worker PCs** shows each PC as a card with its GPU, roles and disk, and
  the steps to add a PC. **Settings** opens on Model files; the settings
  file is under Advanced.
- **A new look.** Archivo and Courier Prime replace Barlow; script text
  (the story, scene and beat lines) is set in Courier Prime. Page copy is
  shorter and in sentence case, and on/off choices are switches.

### Security updates of third-party packages
- hydra-core 1.3.7 -> 1.3.8 in `tool/requirements.txt` and the `tool-venv`
  snapshot, for GHSA-wf6j-7hvr-gr93 (Hydra's legacy blocklist of targets
  allowed unsafe PyYAML loading). SAM-2 needs it; no Kinoloom code uses
  Hydra itself. pip-audit reports no other known vulnerability in the
  pinned lists.

### Docs
- `docs/setup/RESTORE_INSTRUCTIONS.md` says what the install logs and where,
  and its manual commands use pip 26.2.2 and the SAM-2 second pass.
  **Fixed:** it said the installer falls back to ComfyUI's own
  `requirements.txt` when a pinned list fails; it never does.
- The bug report template asks for `install.log` and `LAST-FAILURE.txt`.
- `docs/PIPELINE_AND_CONTINUITY.md` §4.4 describes the movie's request,
  opening frames, prompts and retakes, and `docs/ARCHITECTURE.md` §4.1 the
  `prompts` stage.
- New [docs/research/MOVIE_CONTINUITY.md](docs/research/MOVIE_CONTINUITY.md):
  the measurements behind the take length, the opening frames' references
  and the QA's cast check, and a five-shot scene through the movie path.

## [0.1.0] - 2026-10-25

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
