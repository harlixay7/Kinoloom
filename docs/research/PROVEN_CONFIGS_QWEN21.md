[Kinoloom](../../README.md) › [Docs](../README.md) › Qwen-Image 2.1 configs

# Qwen-Image 2.1: proven configs

> **In short:** Qwen-Image 2.1 is the model Kinoloom uses for 2K stills with an alpha (transparency) channel. It needs a newer ComfyUI than the main render farm, so it runs on its own ComfyUI copy in `ComfyUI_Qwen21\`. This doc covers that instance, its weights, the two configs that were measured and adopted, how the studio is wired to them, and how to set it up on a fresh machine. It is for anyone who wants to run or change the Qwen image path.

> **Provenance:** verified on the studio stack (the RTX 3070 8 GB) and updated
> once afterwards. The measurement records are
> `videolab/configs/qwen21.proven.json` (the raw run logs are not redistributed)
> plus the live studio smoke test in §7. The setup follows the same pattern as
> the LTX/H3 instances: a dedicated instance, weights mounted from the studio
> `models\` tree, and pinned versions.

**On this page**

- [1. Why a fourth instance](#1-why-a-fourth-instance)
- [2. The instance](#2-the-instance)
- [3. Weights](#3-weights-all-in-the-studio-models-tree)
- [4. Proven configs](#4-proven-configs-registry-modelspy--auto-load-in-image-studio)
- [5. Studio wiring](#5-studio-wiring)
- [6. Reference images](#6-reference-images-roster-stills)
- [7. Verification log](#7-verification-log-this-machine-rtx-3070-not-re-run-since)
- [8. Setup on a fresh machine](#8-setup-on-a-fresh-machine)

## 1. Why a fourth instance

Qwen-Image-2.1 needs **ComfyUI >= v0.37** (the 2.1 VAE loader branch, core
`TextEncodeQwenImage21` and ConvRot INT8 text-encoder support). The production
farm stays at **v0.30.0** and cannot load the model, and v0.36.0 was seen to
fail on the 2.1 VAE with size mismatches. The dedicated tree is the LTX/H3
precedent applied a third time, and the production farm stays untouched.

## 2. The instance

| Item | Value |
|---|---|
| Tree | `ComfyUI_Qwen21\` (ComfyUI **v0.37.4**, `comfyui_version.py`; not a git checkout) |
| venv | `ComfyUI_Qwen21\venv`: torch **2.13.0+cu130**, pinned in `install\requirements-frozen\qwen21-venv.txt` (comfy-kitchen 0.2.35, comfy-aimdo 0.5.5, gguf 0.19.0); on a card or driver that runs only CUDA 12.6, torch 2.13.0+cu126 from `qwen21-venv-cu126.txt`. The videolab measurements ran on PyTorch 2.13.0+cu130 as well (`qwen21.proven.json`). |
| Port | **8190** (8188 farm · 8189 LTX · 8199 H3) |
| Launch flags | `--listen 127.0.0.1 --highvram --disable-auto-launch` (+ `--extra-model-paths-config` when `paths.extra_model_dirs` is set). Per `tool/launcher.py`: the old `--lowvram` let the Q5_K DiT drop into ComfyUI-GGUF cast mode with per-module CPU dequantization at 20–100 s/step; `--highvram` materializes the 4.97 GB model directly in VRAM. NVML memory pressure stays on. |
| custom_nodes | ComfyUI-GGUF @ `6ea2651` + ComfyUI-Qwen21-Optimized (tracked in this repo; see below) |
| Weights | mounted from the studio `models\` tree (no copy) via `ComfyUI_Qwen21\extra_model_paths.yaml` (`kinoloom` block, `base_path: ../models/`; `unet` aliased onto `diffusion_models` for the GGUF loader) |

The venv needs one package beyond requirements.txt: `gguf` (ComfyUI-GGUF imports it).

**What the studio graph uses from ComfyUI-Qwen21-Optimized:**
* `TextEncodeQwenImage21CPU` (inputs `clip`, `prompt`, `token_budget`, `measure_dma`, optional `vae`, `image1`..`image4`). Load the text encoder with core `CLIPLoader`, `type: qwen_image`, `device: cpu`. The node counts encoder bytes resident on CUDA and logs a warning if there are any. It encodes **only the positive prompt**, with the same reference handling as the positive half of core `TextEncodeQwenImage21`. If the tokenizer API changed, it falls back to the core node and discards that node's negative. Each instance encodes one prompt, so the graph uses one node for the prompt and one for the negative. `token_budget` (default 4096) does not truncate: a warning is logged when the encoded sequence is longer. More than 4 references, or a conditioning payload above 32 MB, raises an error. The result is copied to the GPU from pinned memory on a separate CUDA stream that the compute stream waits on. With `measure_dma`, the two timing events bracket the copies on that stream, and the copy time is written to `qwen21_metrics.jsonl` in the ComfyUI user directory.
* `EmptyQwen21LatentImage`: a 64-channel latent at 1/16 of the pixel size (width and height must divide by 16).
* `SplitImageRGBA` / `MergeImageRGBA`: route RGB through the 3-channel ESRGAN upscaler while alpha is resized separately.

The pack also has nodes the studio graph does not use: `VAEDecodeTiledPremulAlpha` (512 px windows, 64 px overlap, premultiplied-alpha blending; a canvas smaller than a window is one window per axis), `LatentTiledRefiner` (72-token windows, 16-token overlap → 2×2 on a 128×128 latent; an axis no longer than the window is one window; CFG > 1.0 raises an error) and `PromptEnhancerHostPE`. The two tiled nodes require 7,000 MB of free VRAM before they start, and then assert a ceiling of the card's real VRAM minus 800 MB (7,392 MB on an 8 GB card).

## 3. Weights (all in the studio `models\` tree)

| File | Folder | Size |
|---|---|---|
| `qwen_image_2.1_Q5_K_M.gguf` | diffusion_models (+ unet alias) | 5.01 GB |
| `qwen_image_2.1_turbo_Q4_K_M.gguf` | diffusion_models (+ unet alias) | 4.19 GB |
| `qwen3vl_8b_int8_convrot.safetensors` | text_encoders | 9.35 GB |
| `qwen_image_2.1_vae_bf16_compact.safetensors` | vae | 0.68 GB |
| `p_qwen_image_2.1_8step_v0.1.safetensors` (Pruna DMD) | loras | 0.34 GB |
| `Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r256.safetensors` | loras | 1.36 GB |
| `4x-UltraSharp.pth` | upscale_models | 0.07 GB |

The studio registry loads the Q5_K_M DiT, the text encoder, the VAE, `4x-UltraSharp.pth` and, in fast mode, the Pruna DMD LoRA. The studio does not reference `qwen_image_2.1_turbo_Q4_K_M.gguf` or the viggle 6-step LoRA. They appear only in the videolab evaluation (`videolab/configs/qwen21.proven.json`).

## 4. Proven configs (registry `models.py` → auto-load in Image Studio)

**Quality: `qwen21`** (measured: 2K in ~215 s, that is 214.2 s, single run; the baseline for fg (foreground) sharpness)
```
Q5_K_M · euler/simple · 40 steps · CFG 3.5 · REAL negative prompt
1 MP base (1024² or 1344×768) → untiled VAEDecode → 4x-UltraSharp on RGB,
bicubic ×4 on alpha → merge → bicubic ×0.5 finish (2048² / 2688×1536) → 8-bit RGBA PNG
```

**Fast: `qwen21_fast`** (measured: 39–78 s per 2K image in the five-image fast
suite, one run each; equal fg sharpness, flatter sky drama)
```
same base + Pruna DMD 8-step adapter (LoraLoaderModelOnly, strength 1.0) · euler/simple ·
8 steps · CFG 1.0 — the negative encode is dropped from the graph (inert at CFG 1.0) ·
same upscale finish
```

Evidence (same-seed single runs, RTX 3070, `videolab/configs/qwen21.proven.json`):
- Removing the latent repaint pass raised fg sharpness 453 → 513 (**+13%**).
- CFG 3.5 + real negative vs CFG 1.0 on Q4_K_M: 2729 → 3536 (**+30%**).
- DMD 8-step: **48.3 s warm, fg 3808 (highest measured)**.
- Tiled decode 304.3 s vs untiled 214.2 s (~90 s of staging, control
  inherited), so the untiled decode was adopted.

Sharpness depends on the scene. Comparisons within one scene are the evidence;
numbers across scenes are only informational.

## 5. Studio wiring

* `tool/models.py`: registry entries `qwen21` + `qwen21_fast` (family
  `qwen21`, `comfy_port: 8190`) and the `build_workflow` family branch. The branch
  fills the `QW21_*` tokens, drops the negative encode when CFG ≤ 1.0
  (`_prune_inert_negative`), and removes the VAE input from the text-encode
  nodes so the VAE is not resident during sampling. `build_ref_workflow`
  hands Qwen models to `_build_qwen21_ref_workflow`: 1–4 references are wired
  into `image1`..`image4` of the positive `TextEncodeQwenImage21CPU` node
  (the one with `measure_dma` on). That node gets the VAE back, so the references
  become identity latents.
* `tool/workflows.py`: `QW21_*` SUBST defaults.
* `tool/workflows/qwen21_t2i.json`: a 16-node template. `CLIPLoader`
  (qwen_image, cpu) → two `TextEncodeQwenImage21CPU` (prompt with
  `measure_dma: true`, negative with `false`; `token_budget` 4096) →
  `UnetLoaderGGUF` → `LoraLoaderModelOnly` → `EmptyQwen21LatentImage` →
  `KSampler` → `VAEDecode` → `SplitImageRGBA` → `ImageUpscaleWithModel` (RGB)
  + `ImageScaleBy` bicubic 4.0 (alpha) → `MergeImageRGBA` → `ImageScaleBy`
  bicubic 0.5 → `SaveImageAdvanced` (png, 8-bit, sRGB). The LoRA node is
  "none"-gated (the builder removes it in quality mode), so one template
  serves both modes.
* `tool/images.py`: per-model `comfy_port` routing in `ImageGen._run`
  (default 8188 for every other model). Stop cancels only this run's prompt
  on the instance it runs on.
* `tool/launcher.py`: the `qwen-render` spawn block (port 8190) and its log tail. The
  component is skipped with a message when `ComfyUI_Qwen21\venv` is missing.

## 6. Reference images (roster stills)

Image Studio uploads the reference images per run (`upload_image` → the Qwen instance's
input dir), and `build_ref_workflow` wires them into `image1..image4` of the
positive encode node. Each reference is resized to about a 512×512-pixel area
(aspect kept, sides in multiples of 32). The vision tower sees alpha composited
over white; the VAE gets all four channels.

Prompt guidance from the model card:
- Refer to the inputs by number ("the woman from image_1").
- State identity-preservation boundaries.
- Describe the scene photographically.
- Put exclusions in prose.

Seen live: a Sara/Anna duel in the palace arena, with both
faces locked to their roster stills (`output/final_suite/fast` + studio smoke
outputs).

## 7. Verification log (this machine, RTX 3070; not re-run since)

* Registry: both entries built valid graphs via the studio `WorkflowBuilder`
  (quality 15 nodes / fast 16 nodes with DMD LoRA; the ref build wired
  `image1..image3`). Since then the fast graph also drops the inert negative
  encode, so both modes now build 15 nodes.
* Instance: it boots clean on 8190, all nodes are registered, and every weight is listed
  through the extra-path mount.
* Live generation through the studio tool path: T2I 220.5 s (cold, including all
  model loads) and multi-ref duel 140.4 s. Both are 2048-wide RGBA PNGs, checked
  visually.
* At the time `ComfyUI\` (v0.30 farm), `ComfyUI_LTX\` and the production
  weights were left untouched. The tool edits were additive (registry
  entries, one elif branch, port routing defaulting to 8188).

## 8. Setup on a fresh machine

The launcher points you here when `ComfyUI_Qwen21\venv` is missing. The commands are in
`RESTORE_INSTRUCTIONS.md` §3 Step 5.

1. ComfyUI v0.37.4 in `ComfyUI_Qwen21\`, without overwriting the tracked
   `extra_model_paths.yaml` and `custom_nodes\ComfyUI-Qwen21-Optimized\`.
2. `custom_nodes\ComfyUI-GGUF` cloned from https://github.com/city96/ComfyUI-GGUF.git at `6ea2651`.
3. `ComfyUI_Qwen21\venv` with Python 3.12:
   `pip install -r ..\install\requirements-frozen\qwen21-venv.txt --extra-index-url https://download.pytorch.org/whl/cu130`.
   On a GTX 900- or 10-series card, or a driver older than R580, use
   `qwen21-venv-cu126.txt` with `https://download.pytorch.org/whl/cu126`
   instead (the installer makes that choice itself).
4. The weights from §3 under `models\` (or a `paths.extra_model_dirs` root).
