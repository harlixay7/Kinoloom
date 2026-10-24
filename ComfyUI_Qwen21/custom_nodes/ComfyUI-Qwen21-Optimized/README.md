[Kinoloom](../../../README.md) › [Docs](../../../docs/README.md) › Qwen-Image 2.1 node pack

# ComfyUI-Qwen21-Optimized

> **In short:** These are the studio's custom ComfyUI nodes for running
> Qwen-Image 2.1 on an 8 GB card (tested on an RTX 3070, i9-10980XE, 64 GB
> DDR4). The studio's Qwen graph uses four of them: the CPU text encoder, the
> empty latent, and the RGBA split and merge. The other three are left from
> the lab work, and the studio does not use them. The measured and adopted
> settings are in
> [PROVEN_CONFIGS_QWEN21.md](../../../docs/research/PROVEN_CONFIGS_QWEN21.md).

## Nodes

| Node class | What it does | Used by the studio graph |
|---|---|---|
| `TextEncodeQwenImage21CPU` | Encodes one prompt with the Qwen3-VL text encoder (loaded on the CPU), with up to 4 reference images. Each reference is resized to about 512×512 pixels of area in steps of 32, keeping its aspect ratio, so small images are scaled up too. Then it copies the result to the GPU from pinned memory on its own CUDA stream. | Yes, twice: once for the prompt, once for the negative |
| `EmptyQwen21LatentImage` | Makes an empty 64-channel latent at 1/16 of the pixel size. Width and height must divide by 16. | Yes |
| `SplitImageRGBA` / `MergeImageRGBA` | Split an RGBA image so the RGB part can go through a 3-channel upscaler, then put the alpha back. | Yes |
| `VAEDecodeTiledPremulAlpha` | Tiled RGBA decode with premultiplied-alpha blending (512 px windows, 64 px overlap by default). | No |
| `LatentTiledRefiner` | A low-denoise 2K refine in latent space: 72-token windows with 16 tokens of overlap (a 2×2 grid on a 128×128 latent), sinusoidal blending. Defaults: 8 steps, denoise 0.25, `res_multistep` + `beta`, CFG 1.0. | No |
| `PromptEnhancerHostPE` | Expands a short prompt with a PE-T2I checkpoint from `models/prompt_generator/`. Despite "Host" in its name, it runs on the GPU: the checkpoint's int8 kernels need CUDA, so ComfyUI pages it through VRAM. Without that checkpoint it returns the prompt unchanged and logs a warning (or raises, when `fallback_passthrough` is off). | No |

Why the studio doesn't use the other three. These are single runs from the
lab, recorded in `videolab/configs/qwen21.proven.json`:
- The untiled decode was faster: 214.2 s against 304.3 s for a 2K image. The
  tiled number is a control carried over from an earlier session, not
  re-measured in the same one.
- With the refiner wired in, the run peaked at 7,811 MB, over
  the 7,392 MB ceiling, and the foreground came out less sharp: 453 against
  513 without it.
- The prompt enhancer took 124 s per call, because it pages through VRAM, so it
  was bypassed. Its checkpoint is no longer on the development machine.

[PROVEN_CONFIGS_QWEN21.md](../../../docs/research/PROVEN_CONFIGS_QWEN21.md) section 4 has
the adopted settings.

## How the studio wires them

`tool/workflows/qwen21_t2i.json` is the template, and `tool/models.py` fills
it in. In short:

```
CLIPLoader (type qwen_image, device cpu)
  → TextEncodeQwenImage21CPU (prompt) + TextEncodeQwenImage21CPU (negative)
UnetLoaderGGUF → LoraLoaderModelOnly (fast mode only)
EmptyQwen21LatentImage → KSampler → VAEDecode
  → SplitImageRGBA → 4x upscaler on RGB, bicubic ×4 on alpha → MergeImageRGBA
  → bicubic ×0.5 → SaveImageAdvanced (8-bit RGBA PNG)
```

In fast mode (CFG 1.0) the builder drops the negative encode, because it does
nothing at CFG 1.0. Loading the text encoder with `device: cpu` is what keeps
its 9.35 GB out of VRAM. The node counts the encoder's bytes on the GPU and
logs a warning if it finds any.

## Rules the code checks

- `LatentTiledRefiner` raises an error when CFG is above 1.0.
- `TextEncodeQwenImage21CPU` takes at most 4 references (it has four image
  inputs) and raises an error for a conditioning result above 32 MB. Its
  `token_budget` (default 4096) does not cut the prompt. It only logs a
  warning when the encoded prompt is longer.
- The two tiled nodes need free VRAM before they start: their `vram_gate_mb`
  input, 7,000 MiB by default (`ensure_vram_headroom` in `qwen21_common.py`). Below that, they free
  memory and unload models, up to 5 tries, and then stop with an error. They
  accept 98.5% of the gate, because the Windows desktop moves free VRAM by a
  few dozen MB.
- Afterwards they check that peak use, worked out from the lowest free VRAM
  they saw, stayed under the card's VRAM minus 800 MiB. That is 7,392 MiB on an
  8 GB card.
- All nodes except the RGBA split and merge write their measurements to
  `qwen21_metrics.jsonl` in the ComfyUI user directory.

## Where this came from

The pack was first built in the research lab, `videolab` (the `videolab/`
folder of this repository, a snapshot). It has an older copy under
`videolab/nodes/comfyui_qwen21_optimized/`, and the full tiled graph that the
studio does not use, `videolab/workflows/qwen_2k_tiled_refine.json`. The copy
in this folder has been changed since then, and it is the one the studio
runs.

Some design constraints in the code (the 7,000 MiB VRAM gate, the 7,392 MiB
ceiling, CFG 1.0 in the refiner) come from the lab's planning notes. The
notes are not in this repo or in `videolab`. The code, the comments and
[PROVEN_CONFIGS_QWEN21.md](../../../docs/research/PROVEN_CONFIGS_QWEN21.md) are the
record.

## License

MIT, like the rest of Kinoloom: see [LICENSE](LICENSE). The nodes run inside
ComfyUI, which is GPL-3.0.
