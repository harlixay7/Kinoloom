[Kinoloom](../README.md) › [Docs](../docs/README.md) › Add a model

# Adding a model (video or image)

> **In short:** This page shows how to add a new video or image model to the
> studio. A new video model needs a weight file and one JSON entry, with no
> code changes. A new image model is a small code change in the Python model
> registry. The last part explains how to reproduce the model set on another
> machine or a worker.

## New video model, no code changes

This covers, for example, your own optimized or distinct quant.

1. **Drop the weight file** into `models/diffusion_models/` (or a folder
   listed in `paths.extra_model_dirs`). The entry's `text_encoder` and `vae`
   files must exist too. If you route renders to workers, use the same
   relative location on every worker.
2. **Open `tool/video_models_user.json`** and add one entry under `"models"`.
   Follow the minimum-viable example in that file's `_readme` block. Key it
   by a fresh unique id (for example `"my_quant_v1"`).
   - The common case is to reuse an existing `workflow` template (such as
     `ltx25_msr_dfr`, `h3_ref2va` or the legacy `wan2.2_ti2v_5b`). Wan and H3 templates
     take the entry's `path` file name through `{MODEL_5B}`. The LTX-2.5
     templates name their weights inside the template, so a different LTX
     file needs its own template. LTX-2.5 and MiniMax H3 entries also carry
     instance and reference fields (`comfy_port`, `comfy_root`, `workflows`,
     `ref_nodes`, …). Copy an existing entry of the same family.
   - If your model needs a different graph, add a new
     `tool/workflows/<name>.json` template and point the entry's
     `"workflow"` at it. A template is API-format JSON with `{TOKEN}`
     placeholders: copy the closest existing template and edit it. The
     defaults are the `SUBST` table at the top of `tool/workflows.py`.
     `WorkflowBuilder.build` adds `PROMPT`, `SEED`, `WIDTH`, `HEIGHT`,
     `STEPS`, `FRAMES` and a few derived values. A token left unset raises
     `WorkflowError`.
   - Optionally, give it a `catalog` block. This places it in the model
     pickers (Video Studio and the Generate tab):
     `{"group": "ltx25", "label": "My quant 5 s", "detail": "1080p · ~8 min", "rank": 25}`.
     `group` is one of `ltx25`, `h3_1080p`, `h3_draft`, `wan`, `legacy`
     (`video_models.CATALOG_GROUPS`). `label` is the short name shown in the
     list. `detail` goes in the tooltip and under the select. `rank` orders
     the group (lower first; 99 by default). `"recommended": true` puts it
     first with a ★ (keep one per group). Without the block, the entry falls
     into its family's group under its `display_name`. The catalog changes
     nothing about how the model renders.
3. **Restart the control room.** The user file is merged once, when
   `tool/video_models.py` is imported. After that, availability is computed
   from disk on every request: the weight, text encoder, VAE and every named
   template must exist. Reload the Video Studio tab and the model appears in
   its group of the select. It is greyed out and marked `(unavailable)` if
   something is missing. It is marked ⛓ when its chains render as one
   seamless take (`video_models.seamless_kind`: an LTX entry with a `chain`
   workflow on the `ComfyUI_LTX` instance, or an H3 entry with reference
   images). The tooltip calls it experimental unless the entry sets
   `"experimental": false` (or `"status": "production"`).

Rules of the merge (`video_models._merge_user_registry`):
- Same id as a built-in model + `"override": true` → replaces that entry.
  Fields you omit are inherited from the built-in. Same id without
  `override` → replaces it wholesale, with nothing inherited.
- `"disabled": true` → hides it (built-in entries too) without deleting the
  entry.
- A malformed user file never stops the studio. It is logged and reported
  as `registry_error` by `/api/videostudio/models`, and only the built-in
  models stay available. A single non-object entry is skipped.

**Using it in the movie pipeline** is a separate, manual step. The pipeline
renders an LTX-2.5 or MiniMax H3 family entry when the Generate tab sets it
as the project's video model (`video_family`) or per-tier model
(`video_models`), or when a shot names it in `video_model`
(`orchestrator._video_family`). An entry of another family, or one that is
not installed, falls back to the studio default (below). The legacy Wan 2.2
entry renders only when a project or shot picks it. On a ⛓ entry, runs of
`CONTINUE` shots render as one seamless take
(`docs/PIPELINE_AND_CONTINUITY.md`, section 4.4.1).

**The studio default** is `DEFAULT_MODEL` in `tool/video_models.py`
(`ltx25_s6_cq`). `video_models.default_model_id()` returns it when it is
installed, otherwise the first installed LTX-2.5 or MiniMax H3 tier in
picker order, and never a Wan entry. A project without a model renders on
it, Video Studio opens on it, and the Generate tab's empty choice means it.
With no LTX-2.5 or H3 tier installed, a project shot goes to review with an
error. To change the default, set `DEFAULT_MODEL` to another LTX-2.5 or H3
id. `tool/scripts/verify_restore.py` fails when no LTX-2.5 or H3 tier is
installed and warns when `DEFAULT_MODEL` itself is missing.
`POST /api/videostudio/promote?model=<id>` lists these places and changes
nothing.

The `REQUIRED` list in `verify_restore.py` covers the files of the other
templates. It holds `(folder under models/, file name, hard)` tuples. A hard
entry fails the check when the file is missing or under 1 MB; a soft one
only warns. Only `models/` is searched (not `paths.extra_model_dirs`). The
only hard entries today are the IP-Adapter pair, and only when an SDXL model
is installed.

The legacy Wan 2.2 graphs take their unet from `SUBST["MODEL_5B"]` in
`tool/workflows.py` (`Wan2.2-TI2V-5B-Q4_K_M.gguf`). The H3 templates use the
same token for their DiT, filled from the entry's `path`.
`settings.yaml → tiers.model` (also the Wan file name in
`tool/settings.example.yaml`) is not read by the render path. Only the
warm-up script `tool/scripts/warm_studio.py` uses it, and its video warm-up
is off unless `ensemble.warm_video` is `true`. The other `tiers.*` keys set
per-tier resolution, steps and fps (`orchestrator.py`) and the per-tier
upscaler (`post.py`).

## New image model

Image models live in the Python registry `tool/models.py: MODELS`, one dict
entry per model. Editing it is a code change and needs a control-room
restart. An entry of an existing `family` (`sdxl`, `wan`, `zimage`,
`qwen21`) with an existing `template` works in the Images tab: the model
cards are built from the registry, and `models.build_workflow` maps the
entry's `files` onto that family's template tokens. Availability checks the
`unet` file in `models/diffusion_models/` (or the `ckpt` in
`models/checkpoints/` for `"loader": "checkpoint"`). A new architecture
needs its own `tool/workflows/<template>.json` and a branch in
`models.build_workflow`.

## Reproducing the model set elsewhere

- Build a manifest on the golden machine (the PC that already has the
  complete model set): `python install/make_manifest.py` (sha256 + size) or
  `--quick` (sizes only). It writes `install/manifest.json`, which is
  machine-specific and gitignored. It covers `models/`, `external_loras/`
  and `bible/luts/*.cube` only (not `paths.extra_model_dirs`). Re-run it
  after adding a model.
- On the target, with that manifest copied over:
  `python install/verify_install.py` checks the layout and every manifest
  file's size (and sha256 when the manifest was built without `--quick`),
  then runs `tool/scripts/verify_restore.py`. `--quick` checks sizes only,
  reports missing assets as warnings and skips `verify_restore.py`.
- `INSTALL-MAIN.bat` mode [2] mirrors a golden tree with robocopy (manifest
  included) and runs `verify_install.py --quick`, then the full check. Mode
  [1] (control room from a clone) runs only `--quick`.
- `install/install_components.py` (INSTALL-MAIN.bat modes [1] and [3]) downloads
  what `install/components.json` lists. For the installer to fetch a new
  model, add it there: its path, size, sha256 and sources, and its id in a
  component's `models` (or `manual`, for a file it must never download).
  `tests/unit/test_install_components.py` fails when a file the studio loads
  is missing from that catalog.
- INSTALL-WORKER.bat installs a worker and lets you pick model packages.
  `install/model_packages.json` is the single source of truth for what each
  package contains: the installer copies exactly those files through
  `install/copy_packages.ps1`. If workers should get a new model, put it in
  a package there. Files the main PC keeps outside the tree
  (`paths.extra_model_dirs`) are not visible through the share and are
  reported as missing.
