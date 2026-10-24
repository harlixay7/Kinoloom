[Kinoloom](../../README.md) › [Docs](../README.md) › Retractions

# Retractions and post-mortems

> **In short:** This is the project's retraction ledger. It lists claims, speed-up plans and ideas from the research program that were tested on the studio PC and did not hold up, what the measurements showed instead, and what changed because of them. Read it if you want to know how far to trust the numbers in the other docs.

This doc records quantitative claims, optimization proposals and engineering hypotheses from this program that were tested on the studio's hardware (RTX 3070 8 GB, see `docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md` §1) and that did not hold up.

> **Sources.** This is the studio-side summary of the videolab retraction ledger, as fact-checked (and reviewed a second time) in the copy in this folder, [VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md). The `videolab/` folder is a snapshot of the lab and still carries the uncorrected text, with a pointer to the corrected copies. The numbers come from the archived lab records indexed in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md). The raw run logs are not redistributed, and only the H3 run logs could be re-checked. Most measurements are single runs at a single seed (N=1). Corrections are written down instead of deleted, and third-party figures are recorded as claims, not measurements. What went wrong in the seamless-chain work is in [SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md](SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md).

**On this page**

- [1. The five core retractions](#1-the-five-core-retractions)
  - [Retraction 1: the CUDA 13 speedup and the PSNR tables](#retraction-1-the-33-cuda-13-speedup-and-the-unsourced-psnr-tables-2026-09-18)
  - [Retraction 2: the end-to-end speedup plan](#retraction-2-the-675778-end-to-end-speedup-plan-2026-09-22)
  - [Retraction 3: PDD-8 banned from production](#retraction-3-pdd-8-acceleration-banned-from-production-2026-09-22)
  - [Retraction 4: the edge score artifact](#retraction-4-the-91-edge-score-single-seed-metric-artifact-2026-09-2627)
  - [Retraction 5: the H3 sequence length error](#retraction-5-the-minimax-h3-sequence-length-error-63k--15810-tokens-2026-09-29)
- [2. Other rejected hypotheses](#2-other-rejected-hypotheses)
- [3. How measurements are made now](#3-how-measurements-are-made-now)

---

## 1. The five core retractions

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SUMMARY OF CORE RETRACTIONS                         │
├─────────────────┬───────────────────────────────┬───────────────────────────┤
│ Retraction      │ Invalidation Mechanism        │ Corrective Standard       │
├─────────────────┼───────────────────────────────┼───────────────────────────┤
│ 1. CUDA 13 3.3x │ Not reproducible; no primary  │ Warm-up isolation;        │
│    & PSNR Table │ source (cold start suspected) │ 3rd-party figures removed │
├─────────────────┼───────────────────────────────┼───────────────────────────┤
│ 2. 6.75–7.78x   │ Amdahl: 48–75% non-sampling;  │ NO-GO; re-scoped to       │
│    Speedup Plan │ 9.9 GiB text encoder omitted  │ text-encoder retention    │
├─────────────────┼───────────────────────────────┼───────────────────────────┤
│ 3. PDD-8 Sampler│ 22% edge-proxy loss at about  │ Banned from production;   │
│    Acceleration │ 2× speed (8 vs 20 steps)      │ draft preview tier only   │
├─────────────────┼───────────────────────────────┼───────────────────────────┤
│ 4. +91% Edge    │ Single run per arm (N=1);     │ Labeled exploratory;      │
│    Metric Score │ metric inversion at 1080p     │ N>=5 seed sweep queued    │
├─────────────────┼───────────────────────────────┼───────────────────────────┤
│ 5. H3 Sequence  │ Pre-merge grid mistaken for   │ Token count: 15,810;      │
│    Length Math  │ sequence tokens (63k vs 15.8k)│ PCIe streaming diagnosed  │
└─────────────────┴───────────────────────────────┴───────────────────────────┘
```

### Retraction 1: the 3.3× "CUDA 13 speedup" and the unsourced PSNR tables (2026-09-18)
- **What was claimed:** third-party material in the LTX-Video 2.5 evidence base claimed a **3.30× speedup** from a CUDA-13 build (89.4 s baseline vs. 27.1 s challenger). It also held a PSNR/SSIM table (two standard image-similarity scores; official INT8: 29.71 dB / 0.941 vs. W4A8: 25.39 dB / 0.895), a W4A8 step-time comparison and a "proven 8 GB class" workflow.
- **What the check found:** on the local RTX 3070, none of the four could be reproduced or sourced. There was no primary source for the PSNR table, the step-time table or the 3.3× figure. All four were deleted from the evidence base.
- **Suspected cause (cold-start contamination):** Where exactly the 3.3× figure came from was never reconstructed. A plausible class of error is cold-start contamination. "Cold" is the first run after a start, "warm" is a run with everything already loaded. A baseline that includes first-run compilation, autotuning or model staging looks arbitrarily slower than a warm challenger. This machine produced that artifact again and again:
  - the same 73-frame LTX graph took **150.8 s** cold vs. **72.4 s** warm ($2.08\times$, whole-shot wall time; runs B and A of the `keep_text_encoder` A/B);
  - the Qwen-Image-2.1 two-pass 2K benchmark took 481.9 s cold vs. 262.3 s warm, that is **219.6 s** of warming (`videolab/configs/qwen21.proven.json`).
- **What changed:** no measurement claim enters a document without a run log from this machine. External figures are recorded as claims, never as ground truth. The same review also retracted the earlier conclusion that W4A8 was unsupported: it turned out to be a first-party ComfyUI path.

---

### Retraction 2: the 6.75–7.78× end-to-end speedup plan (2026-09-22)
- **What was claimed:** a decision-engine plan projected a **6.75× to 7.78× end-to-end speedup** on the LTX pipeline from adaptive step skipping, token compaction, dynamic CFG and speculative denoising.
- **What the measurements showed:** an independent re-derivation against local measurements found four problems:
  1. *Optimistic FLOP math:* the FLOP model was $1.8\times\text{--}2.2\times$ optimistic (predicted 2.918 s / 20.399 s for stage 1 / stage 2; measured 5.30 s / 37–45 s).
  2. *Amdahl's law (the total speedup is limited by the part you do not speed up):* the audit put **75%** of the 341 s baseline clip in non-sampling overhead (text encoding, model load/evict, VAE decode) that none of the proposed optimizations touched. With 75% outside sampling, even an infinitely fast sampler gives at most $1/0.75 \approx 1.33\times$ end-to-end. That share does not follow from the step times above if the baseline ran the distilled 8 + 3 steps (not recorded). A distilled model is one trained to finish in only a few steps. With those steps, 153–177 s of sampling would be 45–52% of the clip, which leaves about half as overhead and a ceiling near 2×. Either way the plan's 6.75–7.78× was out of reach.
  3. *Distilled trajectory:* adaptive stepping moved the distilled LTX ladder off its trained schedule. That is the same failure class already measured on H3 (Retraction 3).
  4. *Arithmetic errors in the VRAM budget:* the plan's own line items sum to 8,085 MB against a claimed 7,364 MB, and the **9.9 GiB Gemma text encoder** was left out entirely.
- **Outcome:** the proposal was marked NO-GO. Re-deriving the same levers gave ~284 s end-to-end (341 / 284 ≈ 1.2×; the source also calls it "~1.2× on sampling", and which ratio was meant is not recoverable).
- **What replaced it:** a re-scoped program (sigma-ladder compaction and text-encoder retention). On the 121-frame production pipeline, three new prompts in a row in one warm session took 295.6 / 299.0 / 281.7 s (mean **292.1 s**, ~4.9 min, N=3). The ~341 s baseline it compares with has no record of its warm/cold state. The sigma ladder and the encoder policy both changed, so the difference is not attributed to one lever. In isolated A/Bs, keeping the text encoder staged saved 26.2 s on a cold start and 1.9 s warm. The studio's LTX graphs run `LTX25EvictModels` with `keep_text_encoder: true` (`tool/workflows/ltx25_*.json`).

---

### Retraction 3: PDD-8 acceleration banned from production (2026-09-22)
- **What was claimed:** the H3 Phase H report certified PDD 8-step sampling as a production acceleration tier: $2.5\times$ fewer sampling steps (8 vs. 20; ~2.0× on wall time, ~140 s vs. ~277 s warm) with "visual parity", based on visual review.
- **What the re-test measured:** a re-test with the same scene and seed against a warm 20-step control measured a **22% drop in the edge proxy** (Laplacian score 962.4 → 746.6). The edge proxy is a texture score computed with the Laplacian, an edge-detection filter. The frame strips confirmed whole-frame softening.
  - The re-test's speed figures ("~6% faster at 39 frames, ~15% at 124 frames") have no run log on the studio box. Per the registry's source note they were end-to-end with a 1080p refine after the sampler, so they mostly reflect the unchanged refine.
  - Without a refine the gain is large. In the Phase H runs (832×480×124, same scenes and seeds), PDD-8 sampled in 91–94 s against 230–233 s for 20 steps, and finished in 138.7–148.3 s against 274.0–274.5 s.
  - PDD-8 is fast. It was banned for the quality loss.
- **Root cause:** Phase H relied on visual review without a matched same-seed control. In the tested pipelines, adaptive step skipping introduced severe artifacts on distilled checkpoints. The audit's view (not tested here) was that such methods need non-distilled models with $\text{CFG} > 1$, and H3 (CFG 1.0, shift-12 simple schedule) is not such a model.
- **Outcome:** **PDD-8 was banned** from quality paths and kept only as an explicitly labeled draft tier: `minimax_h3_draft_pdd8` in `tool/video_models_user.json` (graph `h3_ref2va_pdd8`), whose note says to use it for iteration only.

---

### Retraction 4: the "+91% edge score" single-seed metric artifact (2026-09-26/27)
- **What was claimed:** ULT-A (a 6-step Turbo LoRA configuration) was reported as better than the 25-step flagship, citing a **+91% higher edge-variance proxy** (2819.8 vs. 1472.5). The turbo LoRA family was proposed as the quality default.
- **What the measurements showed:** the numbers are real proxy measurements from a same-seed comparison. But there was only one run per configuration, the runs were hours apart with different cache and thermal states, and the Laplacian edge proxy is a texture metric, not perceptual quality:
  1. *Seed-to-seed variance:* the sigma study on the sister LTX pipeline measured same-seed pair spreads of $-56\%$ to $+40\%$ (edge) and $-46\%$ to $+108\%$ (motion) across trajectories, and a cross-seed edge span of 173.6 to 729.5 on one scene. The claimed LoRA deltas (+85% to +91%) are larger than that pair spread. But each rests on one run per configuration, so their size is not established.
  2. *The metric disagrees with the eye:* at $1920\times 1088$ the clip that looked richest (DFR, seed 469) was the smoothest by the metric (mean Laplacian variance 36 vs. 1232 for the no-DFR clip).
- **The correction:** the +91% claim was retracted as a quality indicator. The turbo LoRA family is labeled exploratory and queued for a multi-seed sweep with perceptual metrics. Adoption decisions rest on frame-level visual checks, with the proxy as a secondary signal.

---

### Retraction 5: the MiniMax H3 sequence length error (63k → 15,810 tokens) (2026-09-29)
- **What was claimed:** lab notes stated that MiniMax H3 processed ~58,000 to 63,000 tokens per step at 960×544 (124 frames). An earlier, undocumented estimate of ~186,000 tokens was used to explain the 243-frame 1080p refine failure.
- **What the geometry gives:** the derivation from the `f16t4d24` VAE and the $1 \times 2 \times 2$ patch merge gives:
  $$S = 31\text{ frame-latents} \times (30 \times 17\text{ spatial tokens}) = \mathbf{15,810\text{ tokens}}$$
  The 63,240 figure ($31 \times 2,040$) is the spatial grid **before** the patch merge. If the sequence really were 63,240 tokens, the linear term alone ($2 \times 19.27 \times 10^9 \times 63,240 \approx 2.44 \times 10^{15}$ FLOP/step, with the shipped checkpoint's 19.27B linear-layer parameters) would need $\ge 36\text{ s/step}$ at the 67.2 TFLOP/s gate figure. That is 2.2–2.3× the measured 15.5–16.2 s/step at 960×544. The corrected count rests on the geometry.

  An earlier version of this argument also claimed a fit that is now withdrawn. It used a 33B parameter count, 4.17e15 FLOP and ≥62 s/step, compared that with 15.5–16.6 s/step (which is the 832×480 Phase C range), and said 15,810 lands within 6% of what the step time implies. With the checkpoint's real parameter count that fit does not hold.
- **The correction:** the token count was corrected to 15,810. The conclusion that the 243-frame 1080p refine is not viable still stands on direct measurement: the first tile group took 62.5 min for 5 steps (750.7 s/step average; 532–1087 s/step per the archived report). Its explanation changed from a token explosion to measured PCIe weight re-streaming, that is, model weights copied again and again from system RAM over the PCIe bus.

---

## 2. Other rejected hypotheses

| Hypothesis or experiment | Idea behind it | What was measured | Verdict |
|---|---|---|---|
| **MSR stage-2 reference conditioning** (MSR is the multi-reference guide) | Applying character image conditioning during the 1080p stage-2 refinement will improve identity consistency. | Stage 2 ($34 \times 60$ latent grid) took ~4 min of model initialization and ~4 min per step. With $\ge 2$ references the tested configurations stalled silently (cause not diagnosed; bandwidth saturation suspected). ([VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) §5, archived.) On the studio PC, the one chain finish run with MSR references in stage 2 (~73k tokens) had not finished its first step after 7 min 37 s ([SEAMLESS_CHAINS_01_LTX.md](SEAMLESS_CHAINS_01_LTX.md) §7). | **Rejected (stage-1-only rule)** |
| **TeaCache on the 4-step Wan 2.2 A14B path** | Cache transformer outputs across adjacent steps to save compute. | With the template's `WanVideoTeaCacheKJ` (rel_l1_thresh 0.1, end 0.6), the sampler skipped most of its 4 steps. The result was an almost motionless clip ("looks like a still"). | **Rejected: forced off for MoE models** (`tool/video_studio.py`) |
| **Qwen-Image 2.1 memory flags** | Running without `--highvram`, or with NVML memory pressure disabled, keeps the Qwen instance fast on 8 GB. | Without `--highvram` the Q5_K DiT fell into ComfyUI-GGUF cast mode (per-module CPU dequantization, 20–100 s/step). Disabling NVML pressure overcommitted the card (60–104 s/step, 487–704 s runs). | **Rejected: the studio keeps `--highvram` and NVML pressure on** (`tool/launcher.py`) |
| **Qwen-Image 2.1 2K latent refine pass** | A tiled low-denoise latent pass (`LatentTiledRefiner`) adds detail at 2K. | With the pass wired in, the run peaked at 7,811 MB, above the 7,392 MB usable ceiling. Foreground sharpness was also lower with the pass (453) than without it (513). | **Rejected: not in the studio graph** (`videolab/configs/qwen21.proven.json`) |
| **Host-CPU prompt enhancer (PE-T2I)** | Expand terse prompts on the CPU before encoding. | The ConvRot INT8 checkpoint needs CUDA kernels, so it pages through VRAM instead of running on the CPU: 124 s per call. | **Bypassed** (`PromptEnhancerHostPE` docstring; `qwen21.proven.json`) |

---

## 3. How measurements are made now

These rules were adopted after the retractions above. Most archived numbers are older than the rules (single seed, no clock lock), which is why they carry N=1 labels.

1. **No benchmark from $N=1$ (one run at one seed):**
   A single run at a single seed is an exploratory observation, not a benchmark. Verification needs $\ge 5$ seeds across $\ge 2$ repeats, reporting mean, standard deviation ($\sigma$) and 95th percentile ($p95$). Overlapping distributions count as a tie. Re-running the adoption-critical comparisons this way is the first item on the videolab follow-up list.
2. **Clock locking:**
   The RTX 3070 ran near its power cap for most of a render (peaks 218.6–221.0 W against the 220 W limit at 75–77 °C in the tabled H3 runs; ULT-A: 72.5% of samples at or above 200 W, mean 172 W). SM clocks were not logged in the benchmark runner. The 6–8% step-time spreads on record at matched configurations are within what boost decay (the card lowering its clock speed during a long run) could explain. Future timing benchmarks lock clocks (NVML `nvmlDeviceSetGpuLockedClocks` or `nvidia-smi -lgc`, both with administrator rights; the lab harness notes that `nvidia-smi` once failed on this machine while NVML worked) and log NVML clock telemetry next to power.
3. **Warm-up isolation:**
   Each configuration gets one discarded warm-up run (model staging, kernel compilation, host caches), then A/B/A/B interleaved runs.
4. **Numbers from other systems are not interchangeable:**
   The numbers reflect one topology: quad-channel DDR4 and a dedicated PCIe 3.0 x16 link to an RTX 3070. Systems with dual-channel RAM, PCIe x8 bifurcation, slower storage or different thermal behaviour will stream weights at different speeds. These benchmarks are a record of one machine, not a portable performance guarantee.
