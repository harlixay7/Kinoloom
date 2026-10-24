# ComfyUI-Qwen21-Optimized

My ComfyUI node pack for running Qwen-Image-2.1 within the design constraints of an
8 GB VRAM workstation (RTX 3070 + i9-10980XE + 64 GB DDR4). Each node implements
one of those constraints; the numeric
derivations were developed in the historical lab archive, which is not
redistributed (see `docs/ARCHIVE_INDEX.md`).

## Nodes

| Node class | Pipeline role |
|---|---|
| `TextEncodeQwenImage21CPU` | Text conditioning — Qwen3-VL conditioning on host CPU (AVX-512, 0 MB VRAM), pinned-memory DMA push with CUDA-event fencing, ≤4 references capped at 512 px / 4,096 tokens (8 GB workstation deployment constraint; Qwen officially supports up to 10) |
| `PromptEnhancerHostPE` | Prompt expansion — PE-T2I INT8 prompt expansion (CPU-orchestrated token generation, CUDA ConvRot dequantization paging; logged pass-through degradation when absent) |
| `LatentTiledRefiner` | 2K refinement — low-denoise refinement in the latent domain: 2×2 grid of 72×72-token windows, 16-token (256 px) overlap baseline default, sinusoidal blending, CFG ≤ 1.0 enforced, peak VRAM observed ≤ 5.2 GB under defaults |
| `VAEDecodeTiledPremulAlpha` | Decode — tiled RGBA decode with premultiplied-alpha composition (no dark fringes from straight RGB cross-fades; the measured 2K seam metric still missed its target, see `docs/02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md` §4) |
| `EmptyQwen21LatentImage` | Latent init — 64-channel latent initializer, 16× spatial compression (1024² → 64×64×64, S=4096) |

## Design constraints enforced in code

* CFG > 1.0 raises in `LatentTiledRefiner` — guidance-distilled latents burn above 1.0 during
  detail refinement (the base sampling pass runs at CFG 3.5 with a real negative prompt, while
  the tiled refiner operates strictly at CFG 1.0 with denoise 0.25).
* Peak-VRAM ceiling asserted from `torch.cuda.mem_get_info()` samples: usage
  must stay ≤ 7,392 MB (8,192 physical − 800 OS/display buffer).
* Pre-sampling VRAM gate at 7,000 MB with `gc.collect()` → `empty_cache()` →
  model unload → re-poll before proceeding.
* Per-node telemetry appended as JSONL (`qwen21_metrics.jsonl` in the ComfyUI
  user directory): one JSON object per event with stable keys (`ts`, `node`,
  `event`, plus node-specific fields shown in the source) for lab drivers and
  external audit scripts to consume.

## Intended graph wiring

See `workflows/qwen_2k_tiled_refine.json` in the videolab folder (the Kinoloom studio runs its own copy of this pack). Canonical two-stage chain:

```
TextEncodeQwenImage21CPU (positive) + TextEncodeQwenImage21 (negative)
    → KSampler Base (CFG 3.5, 40 steps, Euler/simple)
    → VAEDecode (1 MP intermediate) → UpscaleModel(4x UltraSharp) → ImageScale(2K Lanczos)
    → VAEEncodeTiled → LatentTiledRefiner (CFG 1.0, 8 steps, denoise 0.25, 72x72)
    → VAEDecodeTiledPremulAlpha (2K premul)
    → SaveImageAdvanced (PNG, RGBA)
```

Companion GGUF backbones load through `UnetLoaderGGUF`; the text encoder loads
through core `CLIPLoader` with **type=`qwen_image`, device=`cpu`** — that
combination is what keeps the 9.35 GB encoder out of VRAM.
