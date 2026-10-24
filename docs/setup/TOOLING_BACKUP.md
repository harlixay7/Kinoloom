[Kinoloom](../../README.md) › [Docs](../README.md) › What git does not hold

# What git leaves out on purpose, and where it lives

> **In short:** Git holds the studio's source code, not its heavy or personal files. This page lists what stays out of git on purpose: weights and tools you can download again, state the studio rebuilds while it runs, and files that only a backup can protect. It also explains how to commit changes to the files git tracks inside the `ComfyUI_LTX` tree. Read it before you back up, move or rebuild the studio.

Git tracks source. The assets below must never be mistaken for "lost": they are either re-downloadable, runtime state, or deliberately backed up as files. The sizes are from the development machine and are only a guide.

## Re-downloadable
- `models/`: 85.4 GB of diffusion/text-encoder/VAE/LoRA weights. LoRAs you train yourself are the exception (see "Locally trained models" below). `install/model_packages.json` only groups the weights a worker PC can pick (H3, LTX-2.5, SDXL, Z-Image, CyberRealistic, post). It does not list every file in `models/`; the Qwen-Image 2.1 and Wan weights, for example, are not in it. The file names the code loads are in [RESTORE_INSTRUCTIONS.md, step 6](RESTORE_INSTRUCTIONS.md#step-6-model-weights), and every file the installer handles is listed in `install/components.json`.
- `tools/`: 6.3 GB (embedded Python, FFmpeg, ai-toolkit).
- `temp/wheels/`: 8.8 GB pip wheel cache.
- Upstream ComfyUI cores + cloneable packs (see [PINNED_VERSIONS.md](PINNED_VERSIONS.md)).

## Runtime state (regenerated in operation)
- `logs/`, `temp/` job state, `*.db` (studio.db, queue.db, datasets.db,
  ComfyUI asset DBs)
- three small files the studio writes on each machine (the code starts empty
  when one is missing): `tool/video_lora_registry.json` (the scanned LoRA
  list), `tool/perf_records.json` (render times, used for estimates) and
  `tool/training_runs.json`.
- `output/`: 1.5 GB of renders (videostudio, images, drafts). One exception: `output/seamlab/` is not regenerated. It holds the chain lab's run records, the evidence behind the seamless-chain docs ([SEAMLESS_CHAINS_EVIDENCE_INDEX.md](../research/SEAMLESS_CHAINS_EVIDENCE_INDEX.md)). Back it up as files if you want to keep that evidence.

## User assets: not in git, back these up as files
- `shots/work/`: 0.55 GB of rendered shot material.
- `roster/datasets/`: 0.50 GB of training datasets.
- `bible/`: 0.18 GB. **It is already git-versioned per project on its own**:
  each project carries its own repo history. Do not nest it into this repo.

## Locally trained models: not on the internet, not in git (too big)
- LoRAs trained on the Training page (ai-toolkit) are copied into `models/loras/` when a run completes, and the run folders stay in `models/train_models/aitk_out/` (`training.aitk_out`).

Back these files up out-of-band. Unlike everything in the re-downloadable list, they cannot be downloaded again.

## Deployed overlay (source tracked here)
- `install/overlay/progress_relay.html`: deploy it into
  `comfyui_frontend_package/static/` of every ComfyUI-hosting runtime
  (see install/overlay/README.md).


## Updating the tracked files inside ComfyUI_LTX

Nine files of the `ComfyUI_LTX` tree are tracked here: the studio's own files,
two patched upstream files (`nodes_lt_audio.py` from ComfyUI,
`MMH3_Split_Upscale.py` from the H3 upscaler pack) and the two license files
that go with them:

- `ComfyUI_LTX/LICENSE` (ComfyUI's GPL-3.0, for `nodes_lt_audio.py`)
- `ComfyUI_LTX/extra_model_paths.yaml`
- `ComfyUI_LTX/comfy_extras/nodes_lt_audio.py`
- `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/__init__.py`
- `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/studio_chain_core.py`
- `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/studio_chain_nodes.py`
- `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/selftest.py`
- `ComfyUI_LTX/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/LICENSE`
- `ComfyUI_LTX/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/nodes/MMH3_Split_Upscale.py`

On the development machine this tree is an upstream git clone with its own
`.git`, so git treats it as an embedded repository, and the first files were
added via plumbing because a normal `git add` silently skipped them. A tree set
up with `robocopy … /XD .git` as in [RESTORE_INSTRUCTIONS.md](RESTORE_INSTRUCTIONS.md)
has no `.git` of its own. After you edit any of these files, stage the change
with either:

    git commit -a            (index-based; picks up tracked-file changes)

or, for one file:

    git update-index --add --cacheinfo 100644,$(git hash-object -w <file>),<file>

Now that these files are tracked, a plain `git add <file>` works for them too:
`git add --dry-run -- ComfyUI_LTX/extra_model_paths.yaml` (git 2.55.0) printed
`add 'ComfyUI_LTX/extra_model_paths.yaml'`. Untracked files in that tree are
matched by `.gitignore` (the same dry run on `ComfyUI_LTX/main.py` reports it
as ignored), so add a new one with `git add -f <file>` or the plumbing command
above.

The render farm tree has three tracked files: `ComfyUI/extra_model_paths.yaml`,
and the patched Impact-Pack detector with its license,
`ComfyUI/custom_nodes/ComfyUI-Impact-Pack/modules/impact/impact_onnx.py` and
`ComfyUI/custom_nodes/ComfyUI-Impact-Pack/LICENSE.txt`. Upstream ComfyUI's own
`.gitignore` ignores `custom_nodes/`, so the two Impact-Pack files were added
with `git add -f`. The installer never overwrites a tracked file when it
installs a tree or a node pack.
