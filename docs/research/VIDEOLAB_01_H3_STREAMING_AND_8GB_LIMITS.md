[Kinoloom](../../README.md) › [Docs](../README.md) › Videolab H3 limits

# MiniMax H3 on 8 GB: Streaming, Token Geometry, and Where It Breaks

> Copy of `videolab/docs/01_H3_STREAMING_AND_8GB_LIMITS.md` at videolab commit
> 3f6cabc, fact-checked against the videolab tree, its configs and the
> H3 lab run logs on the studio box (`D:\H3_LAB\renders\*\run.json`, 93 runs,
> 2026-09-19..28; not redistributed), plus the safetensors header of the shipped
> H3 DiT checkpoint. Corrections are listed at the end.

> **In short:** This doc is about running MiniMax H3 on an 8 GB card: what the
> token geometry implies for compute, how much weight traffic crossed PCIe, and
> where the pipeline breaks. The 124-frame two-phase 1080p renders completed
> (876.4–1484.1 s); the 243-frame 1080p refine did not (its first 5-step tile
> group took 62.5 minutes, and the run was stopped at 90 minutes). Most results
> are one run at one seed.

> Provenance: the numbers and run records cited here come from the original lab
> archive, mapped in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md). Raw run logs and render
> media are not redistributed with this repository.


This document covers the MiniMax H3 pipeline only (the long-clip table in section 4 also
lists two LTX paths, labelled as such): what the model geometry implies, what the
NVML instrumentation measured, and which configurations were abandoned and why. All numbers are
from archived laboratory benchmark run records. Configurations are single-run, single-seed
unless stated; the runs of sections 3–5 used seed `519438276610342`, while the step-time rows
of section 2 come from batteries with other seeds (the E1 ladder: 875362541677469,
119283746501928, 539201748304611).

## 1. The constraint

H3 is described in the archived research notes as a 33B dense single-stream DiT (BF16
checkpoint 61.7 GB) with a 32B Qwen3-VL text encoder. The pruned W4A8 checkpoint used here
holds about 20.1B logical parameters: 19.27B in the linear layers of its 50 transformer
blocks (hidden size 5,376) and about 0.8B in bf16, mostly a two-block text token refiner
(counted from the safetensors header). The production stack is:

| Component | File | Size |
|---|---|---|
| DiT (ref2va, pruned W4A8 ConvRot) | `minimax_h3_ref2va_pruned-w4a8_convrot_pruned.safetensors` | 11.68 GiB |
| Text encoder | `qwen3vl_32b_minimax_h3-w4a8_convrot.safetensors` | 14.62 GiB |
| Video VAE fp16 | `minimax_h3_video_vae_fp16.safetensors` | 4.85 GiB |
| Audio VAE fp32 | `minimax_h3_audio_vae_fp32.safetensors` | 0.56 GiB |
| Latent upscaler (3D conv) | `minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors` | 691 MB (0.64 GiB) |

The DiT alone is 1.46× the VRAM budget. On Windows, the failure mode when memory is oversubscribed
is not a clean OOM: WDDM falls back to shared system memory and the workload starts moving weights
over PCIe in both directions, which is what the instruments show in the bad cases. The pipeline is
therefore built as a streaming system, not as a "load model then infer" system: the text encoder
is loaded by `CLIPLoader` with `device: "default"` and left to ComfyUI's memory manager (no node
in the H3 templates releases it); DiT layers stream from the host cache over PCIe during sampling.
None of the H3 templates has an eviction node. In the two-phase graphs, `MMH3SplitUpscale` unloads
its model and clears the cache when the refine finishes (if the model object supports it,
`videolab/nodes/third_party/Comfyui_Minimax_h3_latent_Upscaler/nodes/MMH3_Split_Upscale.py:710-712`);
the single-pass T2 graph decodes with no eviction step.

## 2. Token geometry and FLOP reality

The latent geometry follows the model's native f16t4d24 3D causal VAE downsampling: 16× spatial,
4× temporal, 24 channels, with a 1×2×2 patch merge, giving 32× spatial compression to the DiT
sequence.

For a 960×544, 124-frame clip:

```
latent grid          = (960/16) x (544/16)   = 60 x 34
after 1x2x2 merge    = 30 x 17              = 510 tokens per frame-latent
frame-latents        = 1 + floor((124-1)/4)  = 31
sequence length S    = 31 x 510              = 15,810 tokens
linear-term FLOPs    = 2 x 19.27e9 x 15,810 ~= 0.609e15 FLOP/step
```

The parameter count is the logical count of the linear layers in the 50 transformer blocks of
the shipped checkpoint (per block: qkv 21,504×5,376, attention out 5,376×7,168, gated MLP in
28,672×5,376, MLP out 5,376×14,336 = 385.4M; stored as packed 4-bit weights with fp8 group
scales per 16 weights). The fl2va checkpoint used in t2v mode by the E1, C and E runs has the
same linear count in its header (C4–C6 used an INT8 checkpoint that is no longer on disk). The
token refiner works on text tokens, not on the 15,810 video tokens. An earlier version of this
section used 2 × 33e9 (the base model's size), which overstates this checkpoint's linear term
1.71× (1.043e15 FLOP/step).

S = 15,810 counts the video tokens only. The model packs one sequence of
[text | conditioning or reference rows | audio | video] (`comfy/ldm/minimax/model.py:5-7`): audio
adds 2 rows per 40 Hz audio latent (about 414 rows for 5.17 s, +2.6%), ref2va adds one image
grid per reference (with `ref_image_size: match`, 510 rows each at 960×544), and the text rows
were not counted. The FLOP figures and rates below are therefore lower bounds for the
sequence the blocks actually process.

Cross-check against measurement. Phase-1 step times at 960×544/124f with comfy-kitchen attention,
from the lab run logs (sampler progress bars, elapsed ÷ steps):

| Runs | Steps | s/step |
|---|---|---|
| E1 step ladder, no LoRA (`e1_*`, 2026-09-23) | 16 / 20 / 25 / 32 | 15.5–16.2 |
| 25-step production (`h3_value544_25` v1–v4, `h3_t1_realism`, `h3_h3_vaeint8`, Flagship phase 1) | 25 | 16.9–18.8 |
| Turbo-LoRA phase 1 (ULT-A, C3, H1, H2, composite) | 4–8 | 19.5–21.0 |

Against the synthetic fused-GEMM gate measurement of 67.2 TFLOP/s (`phase0_env_gate.json`;
it is one GEMM shape, see doc 02 §1), the E1 times imply an effective linear-term rate of:

```
0.609e15 / 16.2 s .. 0.609e15 / 15.5 s  =  37.7 .. 39.3 TFLOP/s
```

which is 56–58% of the gate figure (the 25-step production runs: 32.5–36.1 TFLOP/s, 48–54%).
For the 832×480 configuration (latent grid $52 \times 30$, after $1 \times 2 \times 2$ merge
$26 \times 15 = 390$ tokens/frame-latent, $S = 31 \times 390 = 12,090$ tokens, linear-term FLOPs
$\approx 2 \times 19.27\text{e}9 \times 12,090 \approx 0.466 \times 10^{15}\text{ FLOP/step}$), the
Phase C runs (C1–C6, 20 steps, no attention override) measured 15.45–16.0 s/step
(29.1–30.2 TFLOP/s on the linear term, C1–C3 with the W4A8 checkpoint), and the E4–E6 runs with
kitchen attention 11.5–11.65 s/step (40.0–40.5 TFLOP/s).

The estimate above counts only the linear term; attention and embedding overheads are excluded.
Attention is not small here: changing only the attention backend at 832×480 moved the step from
15.45–16.0 s to 11.5–11.65 s. A rough count of the attention matmuls (4 · S² · 7,168 · 50 layers
≈ 0.36e15 FLOP/step at 960×544; an estimate from the header's attention width, not a measurement)
is of the same order as the linear term. The measured time is consistent with a compute-dominated
step (GEMM plus attention), not with one dominated by weight streaming (section 3); it does not
show the GEMMs running at the gate's rate.

**Correction — the earlier token count was wrong.** Preliminary lab notes used ~58k–63k tokens for
this configuration (only the 63k is derivable). That figure is `31 × 2,040 = 63,240`, the spatial
grid *before* the 1×2×2 patch merge (60×34 = 2,040 at 960×544). Using it implies
2 × 19.27e9 × 63,240 ≈ 2.44e15 FLOP/step, which at the gate's 67.2 TFLOP/s requires at least
36 s/step, 2.2–2.3× the measured 15.5–16.2 s/step (with the old 33e9 basis: 4.17e15 and 62 s/step).
The token count cannot be 63k; the sequence the DiT actually processes is ~15.8k. Doc 03 records
this as Retraction 5.

For the 243-frame 1920×1088 refinement, the same geometry gives 61 frame-latents × 2,040 tokens
(at 1920×1088 the latent grid is 120×68, which the merge takes to 60×34 = 2,040) =
**124,440 tokens** (exact integer product `61 × 2,040`), with a full-sequence linear-term floor
of `2 × 19.27e9 × 124,440 / 67.2e12 ≈ 71 s/step` (122 s with the old 33e9 basis). The refine
samples spatial tiles, each smaller than the full sequence, so the per-tile floor is lower
still. The first 5-step tile group averaged 750.7 s/step (the run's progress bar: 5 steps in
1:02:33); the archived report gives individual steps of 532–1087 s. The refinement does not fail
because of arithmetic; per the archived PCIe trace (5–10 GB/s sustained) it fails because the
weights cannot stay resident and every step re-streams them.

## 3. PCIe offloading dynamics

The runner samples `nvmlDeviceGetPcieThroughput` and link generation at a nominal 0.5 s. It waits
0.5 s after each sample, so the logged sample period is 0.556–0.574 s, and the totals multiply
each sample by the nominal 0.5 s: the recorded TX/RX totals and the average rates below are
10–13% low (ULT-A: RX 700.7 GiB recorded, 804.3 GiB integrated over the logged timestamps; TX
101.1 → 116.1 GiB). Single samples in the tabled runs read up to 20.7–21.2 GB/s (21.7 GB/s
across all 93 logs; NVML reports KB/s, taken here as 1,024 bytes), above the PCIe 3.0 x16
theoretical rate, so individual readings are estimates. Totals as recorded for completed runs:

| Run | Wall | PCIe TX | PCIe RX | Average RX rate |
|---|---|---|---|---|
| ULT-A (6-step + 1080p refine) | 1153.3 s | 101.1 GiB | 700.7 GiB | 0.61 GiB/s |
| C3 (4-step + 1080p refine) | 876.4 s | 118.2 GiB | 821.9 GiB | 0.94 GiB/s |
| Flagship (25-step + 1080p refine) | 1484.1 s | 133.2 GiB | 640.6 GiB | 0.43 GiB/s |
| H1 (6-step, 544p single pass) | 426.0 s | 18.8 GiB | 145.7 GiB | 0.34 GiB/s |
| v4 baseline (25-step, 544p) | 739.6 s | 53.3 GiB | 336.0 GiB | 0.45 GiB/s |

Recorded RX equals about 55–70 checkpoint-sized transfers per two-phase clip (Flagship 55×,
ULT-A 60×, C3 70× the 11.68 GiB DiT). RX also carries the text encoder, VAEs, LoRA and latents,
so this is not a count of DiT re-reads. The average rate is far below the link's capability and
the traffic is bursty (ULT-A: mean 0.70 GiB/s, 95th percentile 5.0 GiB/s, 5% of samples above
5 GB/s). The logs carry no phase markers, so where in the run the bursts fall was not established
from them.

Two regimes explain the numbers:

- **Phase 1 (coarse sampling).** A step is 15.5–21 s at 960×544 (section 2). Streaming 11.68 GiB
  (12.54 GB) at a realistic 5–12 GB/s takes 1.0–2.5 s, a small fraction of a step, so streaming
  cannot account for most of the step time even without overlap. The archive cites a separate LTX
  eviction A/B at 1920×1088 (`LTX25_W4A8_IMPLEMENTATION_REPORT.md` §7: disabling eviction doubled
  the runtime) for the overlap; it is not an H3 measurement, it does not by itself demonstrate
  prefetch overlap, and its "identical per-frame output statistics" are archived, not
  re-verifiable here.
- **Refine (1080p, tiled).** At 124 frames the refine tiles ran at 7.0–30.2 s/step (six 5-step
  tile bars per run, 35–151 s each, in ULT-A, C3 and Flagship), and only 4–10% of all samples
  exceed 5 GB/s (Flagship 4.2%, ULT-A 5.3%, C3 9.9%). The sustained 5–10 GB/s with per-step times of
  532–1087 s is the 243-frame refine (section 4); there streaming dominates and the step time is
  no longer a compute number.

C3 streams more total RX than ULT-A (821.9 vs 700.7 GiB) despite finishing faster. Across the
three two-phase runs PCIe volume does not predict wall time: the RX order is C3 > ULT-A >
Flagship, the wall order the reverse. The Flagship–ULT-A gap (330.8 s) is mostly Flagship's
longer phase 1 (25 steps in 437 s against 6 steps in 121 s). C3's shorter wall also reflects its
cache state: 12 nodes (ids 1–8, 16, 30, 47, 60) were cached from the previous job, which in the
shipped template's numbering include the text encoder (the log records ids only), while ULT-A
started with none.

**Wall-time decomposition (ULT-A).** The run log keeps ComfyUI's sampler progress bars; they sum
to ~717 s of the 1153.3 s wall: the phase-1 6-step sampler ~121 s (10.5% of wall) and six refine
tile bars of 135 / 149 / 44 / 117 / 116 / 35 s (596 s, 51.7% of wall). Model staging, text
conditioning, decode, encode and queueing fall in the remaining ~436 s outside the bars.
Phase-1 sampling is not the bottleneck of this pipeline on this machine; refine sampling is about
half of the wall.

## 4. The 243-frame refinement post-mortem

The 124-frame 1080p refinement completes. The 243-frame version does not. This was measured
directly on 2026-09-27 after an earlier record had claimed otherwise.

Attempted configuration (composite anchor, 243 frames, 25 steps, r45 1080p refine):

- Phase 1 completed normally: 25 steps in 19:15 (~1155 s, ~46.2 s/step average), per the progress
  bars kept in the next run record on the same server (`a3ad86a2`); VRAM steady at ~7.5 GiB
  (archived).
- The refine then entered per-layer weight streaming. **The first temporal-chunk tile group took
  62.5 minutes for 5 steps** (1:02:33 in the progress bar, 750.7 s/step average; individual steps
  532–1087 s per the archived report) with sustained 5–10 GB/s PCIe traffic (archived).
- A second tile group started immediately after. At that rate the full refine projects to 6+ hours.
- The run was stopped after 5,400 s (90 min; `h3.proven.json`). That limit was lab tooling, not a
  GPU-driver watchdog, and the ComfyUI server survived it: the next job on it (`a3ad86a2`,
  22:28) reused 15 nodes from the in-memory cache.

The earlier "243f with 1080p refine in 21.2 min" record was phase-1 only. `T2` was a 243-frame,
25-step 960×544 single-pass render (wall 1271.4 s per its run log), and the two-phase 10.13 s
1080p path did not complete in the tested configurations within the observed run window. The correction is recorded in the
archived 2026-09-27 benchmark campaign report and codified into the baseline
deployment specification.

What remains viable for long clips, all measured on this machine:

| Duration | Path | Wall | Label |
|---|---|---|---|
| 10.13 s | H3: 243 f single-pass 544p, 25-step, `iceq_char.png` frame-0 anchor | 1271.4 s (~21 min) | N=1 |
| 10.33 s | H3: 2× C3 124 f links chained on last frame, then concat | 876.4 s + 893.7 s | N=1 each link |
| 10.04 s | LTX: temporal upscaler ×2 on a 121 f render (slow-motion semantics) | 844 s | N=1 |
| 15.12 s | LTX: 3 × 5.04 s renders concatenated | ~55 min (archived) | N=1 |

The chained pair is the only continuous H3 result above 10 s at true 1080p with real-time motion
(the LTX temporal ×2 path is 1080p as well but slow-motion; the 15.12 s row joins three separate
renders): the second link anchors on the first
link's literal last frame, seam mean absolute channel difference was 6.75/255 (~2.6%), and the
assembled clip is 10.33 s at 1920×1088 with native audio
(archived 10-second 1080p reference render; the container's audio stream duration is documented at
2× the video duration in the source lab and was not fixed at archive time, per
`videolab/configs/h3.proven.json`). The seam number is a single measurement pair, not a distribution.

## 5. What was measured, with labels

| Config | N | Wall | Peak device VRAM | Edge-variance proxy | Label |
|---|---|---|---|---|---|
| ULT-A: 6-step turbo LoRA 544p → 1080p refine | 1 run, 1 seed | 1153.3 s | 7.60 GiB | 2819.8 | exploratory |
| C3: 4-step turbo LoRA 544p → 1080p refine | 1 run, 1 seed | 876.4 s | 7.59 GiB | 2363.5 | exploratory |
| Flagship: 25-step base → 1080p refine | 1 run, 1 seed | 1484.1 s | 7.62 GiB | 1472.5 | exploratory |
| Composite anchor variant of ULT-A | 1 run, 1 seed | 1154.0 s | 7.85 GiB | 3053.3 | exploratory; visual wardrobe binding verified on frames |
| T2: 243 f 25-step 544p single pass | 1 run, 1 seed | 1271.4 s | 7.70 GiB | 1611.6 | exploratory |
| Chain link 2 (C3 config) | 1 run, 1 seed | 893.7 s (log), 899 s (report) | 7.70 GiB | 1906.7 (archived) | exploratory; seam 6.75/255 |

The edge-variance proxy is a Laplacian texture metric. It is not perceptual quality, it is not
comparable across resolutions, and none of the deltas above carry error bars. The ULT-A vs
Flagship comparison (+91% on the proxy at 1153 s vs 1484 s) is a single-pair observation on one
seed; Retraction 4 in doc 03 explains why that is not an adoption-grade result.

## 6. Open confounders

- **No clock lock.** Peak power in the runs of section 5 and the H3 rows of the VIDEOLAB.md
  results table was 218.6–221.0 W against the 220 W limit at 75–77 °C (218.2–221.3 W across all
  93 H3 logs except two Phase A probes at 214.3 and 217.2 W and a 2 s fully cached rerun). The board
  ran power-capped during sampling; SM clocks were never logged during these runs, so boost decay
  across a 19-minute run cannot be ruled out as a source of the observed 6–8% step-time spread.
- **Single seed.** Every adoption decision in this document is one seed. The LTX sigma study on the
  same machine measured same-seed pair spreads of −56%..+40% (edge) and −46%..+108% (motion) across
  trajectories, which is wider than most deltas claimed here.
- **No allocator accounting.** VRAM figures are device-wide NVML samples; `torch.cuda.max_memory_allocated()`
  was not logged for these runs. Weight residency vs activation residency cannot be separated
  retroactively.
- **Host state.** In the runs of section 5 and the H3 rows of the VIDEOLAB.md results table, peak
  swap use was 0.30–0.35 GiB and minimum available RAM dropped to ~9.6 GiB (H2; 9.7–9.9 GiB in H1 and chain link 2). The E1 ladder runs of
  section 2 used ~1.0 GiB of swap, and other H3 runs paged more (C6 1.92 GiB of swap;
  `hero_v2_crf16` 0.40 GiB available with 2.48 GiB of swap). The archive describes the machine
  as otherwise idle; concurrent desktop load was not formally excluded.

## Fact-check corrections

How checked: **[code]** videolab tree at 3f6cabc; **[log]** H3 run logs
`D:\H3_LAB\renders\<key>\run.json` (read-only; progress-bar time = elapsed ÷ steps);
**[hdr]** safetensors header of the DiT (no weights read); **[calc]** recomputed.

1. L11 "the seed used across the H3 batteries was 519438276610342" → used by every run
   tabled here (ULT-A, C3, Flagship, composite, H1, H2, v4, T2, chain link 2); the C and E
   batteries used 875362541677469, 119283746501928 and 539201748304611. [log]
2. L15 "H3 is a 33B dense single-stream DiT" → the base model is described as 33B; the
   shipped pruned checkpoint holds ~20.1B logical parameters (19.27B linear in 50 blocks).
   Table: upscaler filename completed, 691 MB = 0.64 GiB. [hdr][code]
3. L28–30 "text encoder … released after conditioning; … the decoder runs after an explicit
   eviction of the transformer" → no H3 template has an eviction or release node
   (`CLIPLoader device: "default"`, plain `VAEDecode`); the only unload is inside
   `MMH3SplitUpscale` (two-phase graphs). [code]
4. L45 and L48–56 FLOP basis 2 × 33e9 → 2 × 19.27e9 (0.609e15 FLOP/step at 960×544,
   0.466e15 at 832×480). [hdr][calc]
5. L48 "measured phase-1 step time across production runs is 15.5–16.6 s/step
   (PHASE_C_REPORT.md)" → the Phase C runs are 832×480×124, 20 steps, no attention override,
   15.45–16.0 s/step by elapsed time (tqdm rates 14.6–16.63). At 960×544 kitchen attention:
   E1 15.5–16.2, 25-step production 16.9–18.8, turbo phase 1 19.5–21.0 s/step. [log]
6. L51–56 "62.8–67.3 TFLOP/s … 93.5–100% of the measured kernel ceiling", "48.1–51.5 TFLOP/s
   (71.6–76.6%)", "consistent with compute-bound operation under the fused-GEMM kernel" →
   37.7–39.3 TFLOP/s (56–58%) from E1, 32.5–36.1 from production; 832×480 measured
   29.1–30.2 (default attention) and 40.0–40.5 TFLOP/s (kitchen). "Consistent with a
   compute-dominated step (GEMM plus attention)". With the old basis the 832×480 kitchen
   runs would put the linear term alone at 68.5–69.4 TFLOP/s, above the gate figure.
   (The original 71.6% also rounds to 71.5%.) [log][calc]
7. L58–62 63k correction → restated with 19.27B (2.44e15, ≥36 s/step, 2.2–2.3×; old basis
   kept in brackets); "four times" was 3.7–4.0× even on the old basis; the 58k end of
   "58k–63k" is not derived anywhere. [calc]
8. L64–68 refine floor 122 s/step, "4.4–8.9× above that floor" → ~71 s/step full-sequence
   floor; the refine runs per tile, so the per-tile floor is lower; the run log shows a
   750.7 s/step average for the first tile group. (The agent's per-tile floor used the 33e9
   basis and is not repeated.) [log][calc]
9. L72 "every 0.5 s" → nominal; logged period 0.556–0.574 s; totals 10–13% low (ULT-A
   700.7 → 804.3 GiB RX, 101.1 → 116.1 GiB TX). `harness.py:182-188, 218`. [code][log]
10. L83–85 "read from host memory about 60 times per completed clip", "concentrated in the
    refine phase" → 55–70× across the three two-phase runs, and RX is not DiT-only;
    burstiness confirmed (ULT-A p95 5.0 GiB/s vs mean 0.70); refine concentration not
    checkable from the logs (no phase markers). [log][calc]
11. L90 "11.68 GiB at 5–12 GB/s takes 1.0–2.3 s" → 12.54 GB / 12–5 GB/s = 1.0–2.5 s. [calc]
12. L91–93 "This was confirmed by an eviction A/B …" → an archived LTX result; not an H3
    measurement and not evidence of overlap by itself. [code: it is the LTX report]
13. L94–96 refine "observed sustained rates of 5–10 GB/s with per-step times of
    532–1087 s" → that is the 243-frame run only; the 124-frame refine tiles ran at
    7.0–30.2 s/step, with 4–10% of samples above 5 GB/s. [log]
14. L98–101 "wall time is set by how many times weights must cross PCIe, not by the step
    count of phase 1" → contradicted by the table: RX order C3 > ULT-A > Flagship, wall
    order reversed; the Flagship–ULT-A gap is mostly phase-1 steps (437 s vs 121 s). C3 also
    ran with 12 cached nodes. [log]
15. L103–107 "the rest covers the refine pass, model staging, text conditioning, decode and
    encode … Sampling is not the bottleneck" → the bars are sampler bars only: phase 1 121 s
    + six refine tiles 596 s; staging, encoding and decode are outside the bars; sampling is
    62% of wall (refine ~52%). [log]
16. L116 "25 steps at ~48.3 s/it, ~1208 s" → 25 steps in 19:15 (~1155 s, ~46.2 s/step). The
    killed run has no run log of its own; the bars are the ones kept in `a3ad86a2`, matched
    by order and time (moderate confidence). [log]
17. L121 "The driver's 90-minute watchdog killed the run" → stopped at 5,400 s by lab
    tooling; the server kept running (`a3ad86a2` reused 15 cached nodes). [log][code:
    `h3.proven.json:161`]
18. L131–136 long-clip table → the 10.04 s and 15.12 s rows are LTX and are now labelled;
    ~55 min for three clips is archived (no matching LTX per-clip wall in the tree). [code]
19. L138 "the only 'true 1080p, hands-off' result" → narrowed to "the only continuous H3
    result above 10 s at true 1080p with real-time motion". [code: ARCHIVE_INDEX calls the
    temporal ×2 path native 1080p]
20. L145 "What is proven" → "What was measured" (every row is labelled exploratory).
21. L154 chain edge 1906.7 → marked archived (not in `h3.proven.json`). [code]
22. L159 "Retraction 2 in doc 03" → Retraction 4 (the +91% claim). [code]
23. L172–173 "minimum available RAM dropped to ~9.7 GiB in one run" → 9.6 GiB in H2,
    9.7–9.9 GiB in H1 and chain link 2. [log]

Second review (an independent re-check against the same evidence):

24. L11 seed sentence (as rewritten in item 1) "every run tabled in this document" → the §2
    step-time rows include the E1 seeds; restated per section. [log]
25. L45 S = 15,810 is the video-token count only; the packed sequence adds text, audio
    (2 rows per audio latent) and ref2va reference rows (`comfy/ldm/minimax/model.py:5-7`,
    `PackedLayout`), so the FLOP and rate figures are lower bounds. The fl2va checkpoint (E1,
    C, E runs) has the same 19.27B linear count; C4–C6 used an INT8 checkpoint not on disk.
    [code][hdr][log]
26. L72, the sentence added by item 9, "Single samples read up to 20.7–21.2 GB/s" → scoped to
    the tabled runs; 21.7 GB/s across all logs; KB taken as 1,024 B. [log]
27. L99, the cache sentence added by item 14, "12 nodes, including the encoders" → the log
    records node ids only; they include the text encoder in the shipped template's numbering.
    [log][code]
28. L163 "218.6–221.0 W … at 74–77 °C" → 75–77 °C in the section-5 logs (74 comes from the
    config note at `h3.proven.json:183`); 218.4–221.3 W across all logs. [log][cfg]
29. L172–174 host state → scoped to section 5; E1 ~1.0 GiB swap, C6 1.92 GiB, `hero_v2_crf16`
    0.40 GiB available / 2.48 GiB swap; "otherwise idle" attributed to the archive. [log]
30. Item 3 above was imprecise: besides `MMH3SplitUpscale`, the two-phase templates'
    `MinimaxH3LatentUpscaler3D` (`force_unload: true`) moves the upscaler to the CPU and empties
    the cache (`minimax_h3_latent_upscaler_3d.py:601-605`). [code]
31. Not listed in the first pass: L15 "(BF16 checkpoint 61.7 GB)" was carried in from the
    archive's research notes (archived, see below); the attention estimate in §2 (4 · S² ·
    7,168 · 50 ≈ 3.6e14) is new and is an estimate. Removed without a separate entry:
    L56 "treat the figures as empirical bounds … consistent with compute-bound operation under
    the fused-GEMM kernel" (superseded by item 6), L91 "is effectively hidden" and L93 "run on
    the same DynamicVRAM mechanism" (superseded by item 12), L95 "the tile structure forces
    weights to be re-staged repeatedly" (item 13; not checkable from the logs) and L99 "C3's
    shorter phase-1 raises the average rate" (item 14). [code][log]

Third check (a light-touch pass against the same run logs):

32. §6 "No clock lock" and "Host state": items 28 and 29 scoped these figures to the runs of
    section 5, but three of them come from runs outside that table: 218.6 W is the v4 baseline
    `fb4991a4` (218.62 W), 75 °C is H1 `d674f0ca` and ~9.6 GiB is H2 `e5c812a2` (9.56 GiB). The
    six section-5 logs alone give 219.0–221.0 W, 76–77 °C and 9.88 GiB (chain link 2
    `d90d7414`). The scope now adds the H3 rows of the VIDEOLAB.md results table, for which every
    figure in both bullets holds; the numbers are unchanged. [log]
33. §6 "218.4–221.3 W across all 93 H3 logs" (item 28) → 218.2–221.3 W with three exceptions.
    Two full renders peaked below 218.4 W: `h3_t1_realism` `e50dd09d` (218.20 W) and
    `iceq_b3_larry6` `fa23981e` (218.28 W). The exceptions are the Phase A probes `A0_te_probe`
    `dc4777dc` (214.30 W) and `A1_smoke_fl2va` `b652e0b4` (217.23 W), and `C1_w4a8_face`
    `ea764c64` (24.84 W; 2.0 s wall, 4 samples, 15 nodes cached). Values are
    `monitor.peak_power_w`, which equals the timeline maximum in every log. [log]

Checked and correct as written: file sizes; 1.46×; f16t4d24 geometry; 15,810 and 12,090
tokens; 63,240; 61 × 2,040 = 124,440; every PCIe-table wall/TX/RX value and its average-rate
division; 700.7/11.68 = 60.0; ULT-A bar sum 717 s, phase 1 121 s, 436 s outside the bars;
5,400 s; T2 1271.4 s; 876.4 / 893.7 s and 6.75/255 = 2.65%; the labelled-runs walls and VRAM
peaks; 218.6–221.0 W (v4 218.62, T2 220.95); swap 0.30–0.35 GiB.

Archived, not re-verifiable from this repository: the 61.7 GB BF16 base size; 532–1087 s
per step and the 5–10 GB/s refine trace; "6+ hours"; the LTX eviction A/B (§7 of the LTX
report); ~55 min for the 15.12 s path; edge 1906.7; the 6–8% step-time spread; 2026-09-27
report contents.
