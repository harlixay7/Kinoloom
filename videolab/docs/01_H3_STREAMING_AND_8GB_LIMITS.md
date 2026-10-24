# MiniMax H3 on 8 GB: Streaming, Token Geometry, and Where It Breaks

> Provenance: the numbers and run records cited here come from the original lab
> archive, mapped in [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md). Raw run logs and render
> media are not redistributed with this folder.


This document covers the MiniMax H3 pipeline only: what the model geometry implies, what the
NVML instrumentation measured, and which configurations were abandoned and why. All numbers are
from archived laboratory benchmark run records. Configurations are single-run, single-seed
unless stated; the seed used across the H3 batteries was `519438276610342`.

## 1. The constraint

H3 is a 33B dense single-stream DiT with a 32B Qwen3-VL text encoder. The production stack is:

| Component | File | Size |
|---|---|---|
| DiT (ref2va, pruned W4A8 ConvRot) | `minimax_h3_ref2va_pruned-w4a8_convrot_pruned.safetensors` | 11.68 GiB |
| Text encoder | `qwen3vl_32b_minimax_h3-w4a8_convrot.safetensors` | 14.62 GiB |
| Video VAE fp16 | `minimax_h3_video_vae_fp16.safetensors` | 4.85 GiB |
| Audio VAE fp32 | `minimax_h3_audio_vae_fp32.safetensors` | 0.56 GiB |
| Latent upscaler (3D conv) | `minimax_h3_latent_upscaler_3d_conv_v1_bf16` | 691 MB |

The DiT alone is 1.46× the VRAM budget. On Windows, the failure mode when memory is oversubscribed
is not a clean OOM: WDDM falls back to shared system memory and the workload starts moving weights
over PCIe in both directions, which is what the instruments show in the bad cases. The pipeline is
therefore built as an explicit streaming system, not as a "load model then infer" system:
the text encoder is staged in host RAM and released after conditioning; DiT layers stream from the
host cache over PCIe during sampling; the decoder runs after an explicit eviction of the transformer.

## 2. Token geometry and FLOP reality

The latent geometry follows the model's native f16t4d24 3D causal VAE downsampling: 16× spatial,
4× temporal, 24 channels, with a 1×2×2 patch merge, giving 32× spatial compression to the DiT
sequence.

For a 960×544, 124-frame clip:

```
latent grid          = (960/16) x (544/16)  = 60 x 34
after 1x2x2 merge    = 30 x 17             = 510 tokens per frame-latent
frame-latents        = 1 + floor((124-1)/4) = 31
sequence length S    = 31 x 510             = 15,810 tokens
linear-term FLOPs    = 2 x 33e9 x 15,810   ~= 1.04e15 FLOP/step
```

Cross-check against measurement. For the 960×544 configuration ($S = 15,810$ tokens, $\approx 1.043 \times 10^{15}\text{ FLOP/step}$), the measured phase-1 step time across production runs is 15.5–16.6 s/step (`PHASE_C_REPORT.md`). Against the synthetic fused-GEMM gate measurement of 67.2 TFLOP/s (`phase0_env_gate.json`), the implied effective linear-term rate is:

```
1.043e15 / 16.6 s .. 1.043e15 / 15.5 s  =  62.8 .. 67.3 TFLOP/s
```

which is 93.5–100% of the measured kernel ceiling. For the 832×480 configuration (latent grid $52 \times 30$, after $1 \times 2 \times 2$ merge $26 \times 15 = 390$ tokens/frame-latent, $S = 31 \times 390 = 12,090$ tokens, linear-term FLOPs $\approx 2 \times 33\text{e}9 \times 12,090 \approx 0.798 \times 10^{15}\text{ FLOP/step}$), the same 15.5–16.6 s/step latency range corresponds to an effective linear-term rate of $48.1\text{--}51.5\text{ TFLOP/s}$ (71.6–76.6% of the GEMM ceiling).

The estimate above counts only the linear term; attention and embedding overheads are excluded, so treat the figures as empirical bounds rather than exact FLOP counts. The conclusion does not depend on the exact fraction: the measured time is consistent with compute-bound operation under the fused-GEMM kernel.

**Correction — the earlier token count was wrong.** Preliminary lab notes used ~58k–63k tokens for
this configuration. That figure is `31 × 2,040 = 63,240`, the spatial grid *before* the 1×2×2 patch
merge (60×34 = 2,040). Using it implies 4.17e15 FLOP/step, which at the measured 67.2 TFLOP/s
requires at least 62 s/step, four times the measured 15.5–16.6 s/step. The token count cannot be
63k; the sequence the DiT actually processes is ~15.8k. Doc 03 records this as Retraction 5.

For the 243-frame 1920×1088 refinement, the same geometry gives 61 frame-latents × 2,040
post-merge spatial tokens = **124,440 tokens** (exact integer product `61 × 2,040`), with a full-sequence compute floor of
`2 × 33e9 × 124,440 / 67.2e12 ≈ 122 s/step`. The measured refine step time was 532–1087 s/step,
4.4–8.9× above that floor. The refinement does not fail because of arithmetic; it fails because the
weights cannot stay resident and every step re-streams them.

## 3. PCIe offloading dynamics

The runner samples `nvmlDeviceGetPcieThroughput` and link generation every 0.5 s. Totals for
completed runs:

| Run | Wall | PCIe TX | PCIe RX | Average RX rate |
|---|---|---|---|---|
| ULT-A (6-step + 1080p refine) | 1153.3 s | 101.1 GiB | 700.7 GiB | 0.61 GiB/s |
| C3 (4-step + 1080p refine) | 876.4 s | 118.2 GiB | 821.9 GiB | 0.94 GiB/s |
| Flagship (25-step + 1080p refine) | 1484.1 s | 133.2 GiB | 640.6 GiB | 0.43 GiB/s |
| H1 (6-step, 544p single pass) | 426.0 s | 18.8 GiB | 145.7 GiB | 0.34 GiB/s |
| v4 baseline (25-step, 544p) | 739.6 s | 53.3 GiB | 336.0 GiB | 0.45 GiB/s |

The 11.68 GiB checkpoint is effectively read from host memory about 60 times per completed clip
(700.7 / 11.68). The average rate is far below the link's capability; the traffic is bursty and
concentrated in the refine phase.

Two regimes explain the numbers:

- **Phase 1 (coarse sampling).** A step is ~15.5–16.6 s of compute. Streaming 11.68 GiB at a
  realistic 5–12 GB/s takes 1.0–2.3 s. With async prefetch, the transfer overlaps the previous
  layer's compute and is effectively hidden. This was confirmed by an eviction A/B: disabling
  eviction entirely doubled the runtime at 1920×1088 with identical per-frame output statistics
  (`LTX25_W4A8_IMPLEMENTATION_REPORT.md` §7, run on the same DynamicVRAM mechanism).
- **Refine (1080p, tiled).** Per-step compute grows with the token count while the tile structure
  forces weights to be re-staged repeatedly; observed sustained rates of 5–10 GB/s with per-step
  times of 532–1087 s. Here streaming dominates and the step time is no longer a compute number.

C3 streams more total RX than ULT-A (821.9 vs 700.7 GiB) despite finishing faster: the refine pass
dominates the traffic in both, and C3's shorter phase-1 raises the average rate. The practical
reading is that once the pipeline enters the tiled 1080p refine, the wall time is set by how many
times weights must cross PCIe, not by the step count of phase 1.

**Wall-time decomposition (ULT-A).** The run log parses ComfyUI node timings; the unique node bars
sum to ~717 s of the 1153.3 s wall. Within those bars, the phase-1 6-step sampler accounts for
~121 s (10.5%); the rest covers the refine pass, model staging, text conditioning, decode and
encode. The remaining ~436 s of wall time lies outside the node bars (queueing and untracked
overhead). Sampling is not the bottleneck of this pipeline on this machine.

## 4. The 243-frame refinement post-mortem

The 124-frame 1080p refinement completes. The 243-frame version does not. This was measured
directly on 2026-09-27 after an earlier record had claimed otherwise.

Attempted configuration (composite anchor, 243 frames, 25 steps, r45 1080p refine):

- Phase 1 completed normally: 25 steps at ~48.3 s/it, ~1208 s, VRAM steady at ~7.5 GiB.
- The refine then entered per-layer weight streaming. **The first temporal-chunk tile group took
  62.5 minutes for 5 steps** (~750 s/it average; individual steps 532–1087 s) with sustained
  5–10 GB/s PCIe traffic.
- A second tile group started immediately after. At that rate the full refine projects to 6+ hours.
- The driver's 90-minute watchdog killed the run.

The earlier "243f with 1080p refine in 21.2 min" record was phase-1 only. `T2` was a 243-frame,
25-step 960×544 single-pass render (wall 1271.4 s per its run log), and the two-phase 10.13 s
1080p path did not complete in the tested configurations within the observed run window. The correction is recorded in the
archived 2026-09-27 benchmark campaign report and codified into the baseline
deployment specification.

What remains viable for long clips, all measured on this machine:

| Duration | Path | Wall | Label |
|---|---|---|---|
| 10.13 s | 243 f single-pass 544p, 25-step, `iceq_char.png` frame-0 anchor | ~21 min | N=1 |
| 10.33 s | 2× C3 124 f links chained on last frame, then concat | 876.4 s + 893.7 s | N=1 each link |
| 10.04 s | temporal upscaler ×2 on a 121 f LTX render (slow-motion semantics) | 844 s | N=1 |
| 15.12 s | 3 × 5.04 s renders concatenated | ~55 min | N=1 |

The chained pair is the only "true 1080p, hands-off" result: the second link anchors on the first
link's literal last frame, seam mean absolute channel difference was 6.75/255 (~2.6%), and the
assembled clip is 10.33 s at 1920×1088 with native audio
(archived 10-second 1080p reference render; the container's audio stream duration is documented at
2× the video duration in the source lab and was not fixed at archive time, per
`configs/h3.proven.json`). The seam number is a single measurement pair, not a distribution.

## 5. What is proven, with labels

| Config | N | Wall | Peak device VRAM | Edge-variance proxy | Label |
|---|---|---|---|---|---|
| ULT-A: 6-step turbo LoRA 544p → 1080p refine | 1 run, 1 seed | 1153.3 s | 7.60 GiB | 2819.8 | exploratory |
| C3: 4-step turbo LoRA 544p → 1080p refine | 1 run, 1 seed | 876.4 s | 7.59 GiB | 2363.5 | exploratory |
| Flagship: 25-step base → 1080p refine | 1 run, 1 seed | 1484.1 s | 7.62 GiB | 1472.5 | exploratory |
| Composite anchor variant of ULT-A | 1 run, 1 seed | 1154.0 s | 7.85 GiB | 3053.3 | exploratory; visual wardrobe binding verified on frames |
| T2: 243 f 25-step 544p single pass | 1 run, 1 seed | 1271.4 s | 7.70 GiB | 1611.6 | exploratory |
| Chain link 2 (C3 config) | 1 run, 1 seed | 893.7 s (log), 899 s (report) | 7.70 GiB | 1906.7 | exploratory; seam 6.75/255 |

The edge-variance proxy is a Laplacian texture metric. It is not perceptual quality, it is not
comparable across resolutions, and none of the deltas above carry error bars. The ULT-A vs
Flagship comparison (+91% on the proxy at 1153 s vs 1484 s) is a single-pair observation on one
seed; Retraction 4 in doc 03 explains why that is not an adoption-grade result.

## 6. Open confounders

- **No clock lock.** Peak power was 218.6–221.0 W against the 220 W limit at 74–77 °C. The board
  ran power-capped; SM clocks were never logged during these runs, so boost decay across a 19-minute
  run cannot be ruled out as a source of the observed 6–8% step-time spread.
- **Single seed.** Every adoption decision in this document is one seed. The LTX sigma study on the
  same machine measured same-seed pair spreads of −56%..+40% (edge) and −46%..+108% (motion) across
  trajectories, which is wider than most deltas claimed here.
- **No allocator accounting.** VRAM figures are device-wide NVML samples; `torch.cuda.max_memory_allocated()`
  was not logged for these runs. Weight residency vs activation residency cannot be separated
  retroactively.
- **Host state.** Peak swap use in these runs was 0.30–0.35 GiB and minimum available RAM dropped to
  ~9.7 GiB in one run. The machine was otherwise idle, but concurrent desktop load was not
  formally excluded.

## Corrections

This is a snapshot of my lab notes as of 2026-10-01. Some figures in it turned out wrong; the corrected version, with each correction listed, is [docs/research/VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md](../../docs/research/VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) in Kinoloom. Quote that one, not this file.
