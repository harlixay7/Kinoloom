# Vendored third-party node packs

These packs are required by the shipped workflow templates. They are
redistributed here from the lab snapshot, keeping the code, license, and
documentation and dropping upstream repository metadata that is not needed to
run them. The first-party packs live one level up in `nodes/`.

| Folder | Upstream | Version | License | Needed by |
|---|---|---|---|---|
| `Comfyui_Minimax_h3_latent_Upscaler` | https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler | upstream commit `40316cf` (2026-09-17); the lab snapshot of 2026-09-25 matches it apart from the modifications listed below and the omitted `.gitattributes` and `examples/` | MIT (see the LICENSE file in the folder; an older lab note called this pack Apache-2.0, the file itself is MIT) | H3 workflows: provides `MinimaxH3LatentUpscaler3D`, `MMH3SpatialSplitParamsV10`, `MMH3SplitUpscale` |
| `ComfyUI-LTX2.5-MSR` | https://github.com/liconstudio/ComfyUI-LTX2.5-MSR | v1.2.2 (lab notes record commit `edec572`) | Apache-2.0 (see the LICENSE file) | LTX workflows: provides `ComfyUILTX25MSRICLoRALoader`, `ComfyUILTX25MSRMultiReferenceGuide` |

## Modifications made to the vendored copies

- `Comfyui_Minimax_h3_latent_Upscaler/workflow_templates/*.json`: two embedded
  VideoHelperSuite `fullpath` metadata values that pointed at the upstream
  author's machine were blanked. No functional change to the example graphs.
  The two files are renamed from `minimax_h3_i2v_Latent Upscaler example
  workflow2.json` and `minimax_h3_r2v_Latent Upscaler example workflow3.json`
  to `minimax_h3_i2v_example.json` and `minimax_h3_r2v_example.json`. With the
  folder prefix the old names were 134 characters long, so a clone into a deep
  folder hit the 260-character Windows path limit (Git for Windows leaves
  `core.longpaths` off by default).
- `Comfyui_Minimax_h3_latent_Upscaler/README.md` and `README_zh.md`: the
  `examples/` video and image references were replaced with a note because the
  demo media is not vendored (not needed to run the pack).
- `Comfyui_Minimax_h3_latent_Upscaler/nodes/*.py`: runtime `print()`/`raise`
  strings were normalized to ASCII (14 strings). Upstream prints emoji and
  Chinese text; when ComfyUI's stdout is redirected to a non-UTF-8 file on
  Windows, `UnicodeEncodeError` propagates out of the node, the prompt-worker
  thread dies, and the queue entry is left running forever (observed
  2026-09-30). Code, node mappings, and UI metadata are unchanged.
- `ComfyUI-LTX2.5-MSR`: none. Omitted files that are not needed to run the
  pack: `.gitignore` and `.github/publish_action.yml`.

## Deployment

`install.ps1 -ComfyUI <path>` copies every folder under `nodes/` (first-party)
and `nodes/third_party/` into the target's `custom_nodes/`; a folder that already exists there is skipped unless `-Force` (`install.sh --force`) is given, which replaces it. The workflows also
assume ComfyUI v0.36.0 core for H3/LTX (the MiniMax-H3 reference, anchor,
sigma-shift and attention-backend nodes are core nodes) and v0.37.4 core for
Qwen-Image.

Upstream is authoritative. Before deploying against a newer ComfyUI, compare
these folders against the upstream repositories and record the exact commits
you pin; the two licenses permit redistribution and modification, with the
license files preserved as they are here.
