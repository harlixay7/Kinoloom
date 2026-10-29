[Kinoloom](../README.md) › Docs

# Docs map

> **In short.** This page lists every document in the repository and what it is for. If you only want to see what Kinoloom can do, read the [README](../README.md) and the [showcase](SHOWCASE.md). If you want to set it up, start with the README's [Quick start](../README.md#quick-start); [RESTORE_INSTRUCTIONS.md](setup/RESTORE_INSTRUCTIONS.md) has the manual steps behind the installer. If you want to know exactly how something works or where a number comes from, use the tables below.

The docs follow three rules: every number comes from a saved record (where a number was only printed on screen and not saved, the doc says so); what was measured is kept apart from what is only inferred; and when a claim turns out wrong, the correction is recorded instead of the old claim being deleted. Everything was tested on one machine: an RTX 3070 8 GB, i9-10980XE, 64 GB RAM, Windows 11.

## Start here

| Doc | What's in it |
| :--- | :--- |
| [README](../README.md) | What Kinoloom is, the showcase, what it can do, whether your PC can run it, a quick start, and how it works in short |
| [SHOWCASE.md](SHOWCASE.md) | How each sample video and still was made: inputs, every step's time for the videos (as rendered, and warm estimates), seam QA, cast identity, and what is wrong with each one |

## Set it up and run it

| Doc | What's in it | Read it if... |
| :--- | :--- | :--- |
| [RESTORE_INSTRUCTIONS.md](setup/RESTORE_INSTRUCTIONS.md) | What a fresh clone lacks and how to get each piece by hand: the three ComfyUI trees, their venvs, the weights, then verification | the installer stopped and you want to see what it does, or you are rebuilding after a disk failure |
| [install/ADD-A-MODEL.md](../install/ADD-A-MODEL.md) | Adding your own model: a video model needs no code changes, an image model a small one | you have a model file the studio doesn't list yet |
| [SECURITY.md](../SECURITY.md) | What the API gate enforces, the tokens, the worker fleet, the threat model, how to report a vulnerability | you want to open the studio to your network, or you found a security problem |
| [DISTRIBUTED_FLEET.md](setup/DISTRIBUTED_FLEET.md) | LAN render workers, the fleet token, the remote client, Colab workers over Tailscale | you want a second PC or a Colab GPU to take render jobs |
| [colab/README.md](../colab/README.md) | The Colab worker runbook: one-time setup, the first-run check, daily use, limits, troubleshooting | you are running the Colab worker |
| [PINNED_VERSIONS.md](setup/PINNED_VERSIONS.md) | The ComfyUI versions, the upstream commits where git recorded one, the packages and the custom node packs the studio was built and measured against | you are rebuilding a ComfyUI tree, or chasing a version difference |
| [TOOLING_BACKUP.md](setup/TOOLING_BACKUP.md) | What git does not hold on purpose, and where it lives | you want to back up a working studio |
| [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | The files in the repository that are not under the MIT License, the software the studio runs, and what each model's license allows | you want to know what you may do with the code, the models or their outputs |

## How it works

| Doc | What's in it |
| :--- | :--- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The launcher and its Win32 Job Objects (§1), the GPU lease broker (§2), the ComfyUI fleet (§3), the project director (§4), the databases (§5), LoRA training (§6), and the control room's request path, security gate and worker routing (§7) |
| [PIPELINE_AND_CONTINUITY.md](PIPELINE_AND_CONTINUITY.md) | The story bible (§1), the Story Architect and its LLM guardrails (§2), the canon linter (§3), continuity between shots, Video Studio chains and movie takes (§4), the QA director (§5), post-processing (§6) and film assembly (§7) |

## Seamless chains: long takes joined inside the model (`docs/research/`)

| Doc | What's in it |
| :--- | :--- |
| [SEAMLESS_CHAINS.md](research/SEAMLESS_CHAINS.md) | Start here: what changed, the results at a glance, how seams are measured, how to reproduce |
| [SEAMLESS_CHAINS_01_LTX.md](research/SEAMLESS_CHAINS_01_LTX.md) | The LTX-2.5 engine mechanism by mechanism, the 1080p finish and its VRAM cliff, cast age and identity |
| [SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md](research/SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md) | The MiniMax H3 chain, the H3 gate chain (the forest take), host-memory limits, and the blue screens during the H3 tests |
| [SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md](research/SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md) | Approaches that were replaced, claims that were corrected, lessons and open items |
| [SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md](research/SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md) | How Video Studio and the movie pipeline use the engine, the model catalog, the tests, the Video Studio chain rendered on the GPU and what has not run yet (§6), and two fixed defects (§7) |
| [SEAMLESS_CHAINS_EVIDENCE_INDEX.md](research/SEAMLESS_CHAINS_EVIDENCE_INDEX.md) | Where the record behind each number lives (the records are not redistributed) |

## Measurements and research (`docs/research/`)

| Doc | What's in it |
| :--- | :--- |
| [HARDWARE_BENCHMARKS_AND_LIMITS.md](research/HARDWARE_BENCHMARKS_AND_LIMITS.md) | The test machine (§1), model geometry and FLOPs (§2), the W4A8 kernel gate (§3), compute-bound against bandwidth-bound sampling (§4), the Windows paging cliff (§5), the LTX-2.5 decoder fix (§6) |
| [PROVEN_CONFIGS_QWEN21.md](research/PROVEN_CONFIGS_QWEN21.md) | The dedicated Qwen-Image 2.1 instance, its weights, the proven configs and their verification log |
| [MOVIE_CONTINUITY.md](research/MOVIE_CONTINUITY.md) | The movie pipeline's measured defaults: the 8-shot LTX-2.5 take behind the take length of 6 (§1), the opening-frame comparison behind its references (§2), the calibration of the QA's cast check (§3), a five-shot scene through the movie path (§4) |
| [VIDEOLAB.md](research/VIDEOLAB.md) | Fact-checked copy of the videolab README: benchmarks of the LTX-2.5 22B and MiniMax H3 video DiTs on one 8 GB GPU, mostly single runs |
| [VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md](research/VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) | H3 token geometry, FLOPs, PCIe streaming, the 243-frame refine post-mortem |
| [VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](research/VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) | The W4A8 kernel gate, ComfyUI #15606, pipeline rules |
| [VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](research/VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md) | The lab's retraction ledger |
| [VIDEOLAB_ARCHIVE_INDEX.md](research/VIDEOLAB_ARCHIVE_INDEX.md) | Map of the lab archive (not redistributed) |
| [videolab/README.md](../videolab/README.md) | The lab itself, as a snapshot at its commit `3f6cabc`. Its documents predate the corrections in the `VIDEOLAB*.md` copies above, so quote the copies, not the snapshot. |

## What turned out wrong (`docs/research/`)

| Doc | What's in it |
| :--- | :--- |
| [RETRACTIONS_AND_POSTMORTEMS.md](research/RETRACTIONS_AND_POSTMORTEMS.md) | The five core retractions of the research program, rejected hypotheses, and the method rules that came out of them |
| [SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md](research/SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md) | What went wrong in the seamless-chain work |

## The project

| Doc | What's in it |
| :--- | :--- |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Development setup, the rules the code keeps, conventions, tests and the checks CI runs |
| [CHANGELOG.md](../CHANGELOG.md) | Notable changes, release by release |
| [LICENSE](../LICENSE) | MIT, except the files [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) lists: two modified GPL-3.0 files (ComfyUI's `nodes_lt_audio.py` and Impact-Pack's `impact_onnx.py`) and the web fonts under the SIL Open Font License. Model weights are not part of the repository and keep their own licenses. |
| [.github/](../.github/) | The issue templates (bug report, hardware report) and the pull request checklist |

## Small docs next to the code

| Doc | What's in it |
| :--- | :--- |
| [tool/train/INSTALL.md](../tool/train/INSTALL.md) | Installing kohya sd-scripts and training a character face LoRA from the command line (the studio's Wan 2.2 LoRA training, linked from the foot of the Casting page, uses ai-toolkit instead) |
| [ReferenceImages/README.md](../ReferenceImages/README.md) | The one check that reads this folder: face QA of shots that belong to no project |
| [install/overlay/README.md](../install/overlay/README.md) | `progress_relay.html`, the live-progress page that has to be copied into each ComfyUI runtime |
| [ComfyUI-Qwen21-Optimized/README.md](../ComfyUI_Qwen21/custom_nodes/ComfyUI-Qwen21-Optimized/README.md) | The studio's Qwen-Image 2.1 nodes, which ones the studio graph uses, and what each one checks |
| [tool/scripts/README.md](../tool/scripts/README.md) | The helper scripts: install checks, the chain lab and the showcase, benchmarks, model tools |
| [tool/skills/](../tool/skills/) | Instructions written for the LLM, not for people: two skill files (pipeline, storytelling) |

## Find a fact fast

| Question | Where |
| :--- | :--- |
| How long does a sample take, cold and warm? | [SHOWCASE.md](SHOWCASE.md) §4-§11 |
| Why does only one model use the GPU at a time? | [ARCHITECTURE](ARCHITECTURE.md) §2 |
| What happens to the ComfyUI processes when I close the window? | [ARCHITECTURE](ARCHITECTURE.md) §1 |
| Which port runs which model, with which flags? | [ARCHITECTURE](ARCHITECTURE.md) §3, [PINNED_VERSIONS](setup/PINNED_VERSIONS.md) §1 |
| What is in `studio.db`, `queue.db` and `datasets.db`? | [ARCHITECTURE](ARCHITECTURE.md) §5 |
| How does the LLM story generation stay on canon? | [PIPELINE](PIPELINE_AND_CONTINUITY.md) §2-§3 |
| How does a shot continue from the last one? | [PIPELINE](PIPELINE_AND_CONTINUITY.md) §4 |
| How does QA decide a shot failed, and what happens next? | [PIPELINE](PIPELINE_AND_CONTINUITY.md) §5 |
| How many shots does one movie take hold, and why? | [PIPELINE](PIPELINE_AND_CONTINUITY.md) §4.4.1, [MOVIE_CONTINUITY](research/MOVIE_CONTINUITY.md) §1 |
| How are long takes joined, and how is a seam measured? | [SEAMLESS_CHAINS](research/SEAMLESS_CHAINS.md), [01](research/SEAMLESS_CHAINS_01_LTX.md) §1-§4 |
| Why does the LTX-2.5 1080p finish sometimes crawl? | [SEAMLESS_CHAINS_01](research/SEAMLESS_CHAINS_01_LTX.md) §7, [HARDWARE](research/HARDWARE_BENCHMARKS_AND_LIMITS.md) §5.4 |
| Why did the PC blue-screen during the H3 tests, and what changed? | [SEAMLESS_CHAINS_02](research/SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md) §5 |
| Which chain paths have rendered on the GPU? | [SEAMLESS_CHAINS_04](research/SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md) §6 |
| What breaks when a model doesn't fit in 8 GB on Windows? | [HARDWARE](research/HARDWARE_BENCHMARKS_AND_LIMITS.md) §5 |
| Why was the 243-frame H3 1080p refine abandoned? | [HARDWARE](research/HARDWARE_BENCHMARKS_AND_LIMITS.md) §4.2, [VIDEOLAB_01](research/VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) |
| Which earlier claims were wrong? | [RETRACTIONS](research/RETRACTIONS_AND_POSTMORTEMS.md) §1, [SEAMLESS_CHAINS_03](research/SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md) |
| Is it safe to set `ui.host: 0.0.0.0`? | [SECURITY.md](../SECURITY.md) §1 |
| How do I report a security problem? | [SECURITY.md](../SECURITY.md) §6 |
| Which files does a fresh clone lack? | [RESTORE_INSTRUCTIONS](setup/RESTORE_INSTRUCTIONS.md) §1, [TOOLING_BACKUP](setup/TOOLING_BACKUP.md) |
| How do I add a model? | [install/ADD-A-MODEL.md](../install/ADD-A-MODEL.md) |
| How do I train a LoRA (legacy Wan 2.2 only)? | Wan 2.2 LoRA training, at the foot of the Casting page (ai-toolkit): [ARCHITECTURE](ARCHITECTURE.md) §6; a face LoRA from the command line: [tool/train/INSTALL.md](../tool/train/INSTALL.md) |
| Where is the record behind a seamless-chain number? | [SEAMLESS_CHAINS_EVIDENCE_INDEX](research/SEAMLESS_CHAINS_EVIDENCE_INDEX.md) |

## Where the code lives

| Folder | What's there |
| :--- | :--- |
| `tool/` | The control room: `api.py` and `routers/` (web API), `launcher.py`, `gpu_broker.py`, `orchestrator.py`, the story, linter, QA, post and assembly modules, the chain engine (`chain_engine.py`, `chain_graphs.py`, `chain_studio.py`, `seam_qa.py`), model registries (`models.py`, `video_models.py`, `video_models_user.json`), ComfyUI graphs in `workflows/`, the web UI in `static/` |
| `tool/scripts/` | Helper and lab scripts, including `chain_lab.py` (the seamless-chain lab harness, which rendered the showcase videos with `--request`) and `showcase.py` (the showcase stills, and the re-encodes and GIFs) |
| `tool/worker/` | The optional worker agent for other PCs and Colab |
| `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/` | The studio's own ComfyUI nodes for LTX-2.5 and the chain engine ([TOOLING_BACKUP.md](setup/TOOLING_BACKUP.md) lists which files of this tree git tracks) |
| `ComfyUI_Qwen21/custom_nodes/ComfyUI-Qwen21-Optimized/` | The studio's own nodes for Qwen-Image 2.1 ([its README](../ComfyUI_Qwen21/custom_nodes/ComfyUI-Qwen21-Optimized/README.md) says which ones the studio uses) |
| `install/` | Installers, the model package lists, frozen requirements, `verify_install.py` (the machine-specific `manifest.json` is built locally and gitignored) |
| `tests/` | The test suite; it copies `tool/` into a sandbox and refuses to run against a live tree |
| `benchmarks/` | Benchmark scripts |
| `colab/` | The Colab worker notebooks and bootstrap |
| `docs/` | How it works (`ARCHITECTURE.md`, `PIPELINE_AND_CONTINUITY.md`), the showcase (`SHOWCASE.md`, its media in `showcase/`), setup guides in `setup/`, research notes in `research/` |
| `videolab/` | The research lab (a snapshot) and the model downloader |
| `ReferenceImages/` | Your own reference photos for face QA of shots without a project; git keeps only its README |
