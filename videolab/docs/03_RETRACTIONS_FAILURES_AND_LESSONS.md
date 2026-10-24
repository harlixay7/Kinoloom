# Retractions, Failures, and Lessons

> Provenance: the numbers and run records cited here come from the original lab
> archive, mapped in [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md). Raw run logs and render
> media are not redistributed with this folder.


This is the record of claims in this program that were tested and disproved, in the order they
were corrected. Each entry states what was claimed, what the measurements showed, why the claim
failed, and what replaced it. The same list is why the main README labels almost every adoption
row `N=1`.

## Retraction 1 — The 3.3× "CUDA 13 speedup" and the PSNR/SSIM table (2026-09-18)

**Claimed.** The pre-implementation evidence base for LTX 2.5 included a third-party claim of a
3.30× speedup from a CUDA-13 build (89.4 s vs 27.1 s), a PSNR/SSIM table (official INT8
29.71 dB / 0.941 vs W4A8 25.39 dB / 0.895), a W4A8 step-time comparison, and a "proven 8 GB
class" workflow.

**Measured.** Verification could not reproduce or source any of the four. No primary source
exists for the PSNR table, the step-time table, or the 3.3× figure. All four were deleted from
the evidence base rather than explained away
(archived laboratory feasibility log, 2026-09-18).

**Why it failed.** The exact provenance of the 3.3× claim was never reconstructed. The class of
error is cold-start contamination: a baseline that includes first-run compilation, kernel
autotuning or model staging can look arbitrarily slower than a warm challenger. This machine
produced that exact artifact repeatedly under controlled conditions: the LTX text encoder path
measured 150.8 s cold vs 70.5 s warm for the same graph (2.1×), and the Qwen benchmark measured
219.6 s of warming time between its cold and warm runs. Any speedup claim of this shape without a
warm-up isolation protocol is unverifiable.

**What replaced it.** A rule: no number enters a document without a run log produced on this
machine. Third-party figures are recorded as claims, not measurements. The deletions are listed
in the feasibility report itself, including the ones that were later shown to be wrong in the
other direction (W4A8 turned out to be a first-party ComfyUI path, so the original "not
supported" conclusion was also retracted after re-verification).

## Retraction 2 — The 6.75–7.78× end-to-end speedup plan (2026-09-22)

**Claimed.** A decision-engine plan projected 6.75–7.78× end-to-end speedup on the LTX pipeline
through adaptive step skipping, token compaction, dynamic CFG and speculative denoising.

**Measured.** My own re-derivation against local measurements showed:

- The FLOP model was 1.7–1.9× optimistic versus measured step times (predicted 2.918 s / 20.399 s
  for stage-1/stage-2; measured 5.30 s / 37–45 s).
- 75% of the 341 s baseline clip was non-sampling overhead (text encode, model load/evict, VAE
  decode) that none of the proposed optimizations touched.
- Adaptive stepping on the distilled LTX ladder moved the trajectory off the trained schedule; the
  same failure class had already been measured on H3 (Retraction 3).
- The VRAM budget arithmetic was wrong (the plan's own line items sum to 8,085 MB against a
  claimed 7,364 MB) and omitted the 9.9 GiB text encoder entirely.

**Why it failed.** The plan optimized the part of the pipeline that was already efficient and
assumed away the part that wasn't. The re-derivation of the same levers was ~1.2× on
sampling, ~284 s end-to-end.

**What replaced it.** The plan was marked NO-GO and re-scoped. The re-scoped program (sigma-ladder
compaction and text-encoder retention) delivered verified improvements measured against isolated
baselines. The early 73-frame exploratory A/B measured the full cold-to-warm composite difference
at 80.3 s (150.8 s cold vs 70.5 s warm). On the 121-frame production pipeline, warm steady-state
execution averaged 292.1 s (~4.9 min) against the ~341 s cold baseline, a net reduction of 48.9 s
driven primarily by text-encoder host caching (26.2 s cold / 1.9 s warm, isolated) combined with
fused W4A8 kernel staging. The rest of the ambitious hooks were either rejected with evidence or
left as research spikes.

## Retraction 3 — PDD-8 acceleration, adopted then banned (2026-09-22)

**Claimed.** The H3 Phase H report certified PDD 8-step as production acceleration: 2.5× faster
sampling (~140 s vs ~277 s warm wall) with "visual parity" on face, motion and VFX scenes, based
on visual review.

**Measured.** A re-test with the same scene and seed, comparing a warm 20-step control against
PDD-8, measured edge-proxy 962.4 → 746.6 (−22%) for roughly 6% faster sampling at 39 frames
(~15% at 124 frames). Whole-frame softening was confirmed on the strips; the frames were not
equivalent.

**Why it failed.** The Phase H certification used visual review without a matched control. At
review scale, whole-frame softening reads as "fine"; against a same-seed control it is 22% of the
texture proxy. The audit that followed found that in my tested pipelines, adaptive step skipping
introduced severe artifacts on distilled checkpoints, functioning reliably only on non-distilled
models with CFG > 1, which H3 (CFG 1.0, shift-12 simple schedule) is not.

**What replaced it.** PDD-8 was banned from quality paths and kept only as an explicitly labeled
draft tier. The same rule now applies to all step-count or schedule changes on this stack.

## Retraction 4 — The "+91% edge" quality claim (2026-09-26/27)

**Claimed.** The ULT-A render (6-step turbo LoRA) was reported as the best render of the program:
edge-variance proxy 2819.8 vs 1472.5 for the 25-step flagship (+91%), and the turbo LoRA family as
a quality default.

**Measured.** The number is real as a proxy measurement and the comparison is same-seed — but it
is one run per configuration, hours apart, with different cache and thermal states, and the proxy
is a Laplacian texture metric, not perceptual quality. The program's own sigma study on the sister
pipeline measured same-seed pair spreads of −56%..+40% (edge) and −46%..+108% (motion) across
trajectories, larger than the claimed LoRA deltas. The metric's ordering does not even survive a
resolution change: at 1920×1088 the clip that looked richest (DFR, seed 469) was the smoothest by
the metric (mean Laplacian variance 36 vs 1232 for the no-DFR clip), and I wrote the correction
into the LTX report itself
(`LTX25_W4A8_IMPLEMENTATION_REPORT.md` §14).

**Why it failed.** A single-seed texture proxy was used as a stand-in for visual quality, and the
metric's own documentation says it is not a quality score. The adoption decisions that followed
(composite anchor, newest turbo adapters) inherited that weak foundation.

**What replaced it.** Adoption of the composite-anchor config rests on frame-level visual checks
(wardrobe and composition held across all 124 frames) with the proxy as a secondary signal; the
docs state the proxy's limits inline. The turbo-LoRA-family adoption is now labeled exploratory
and explicitly queued for a seed sweep with perceptual metrics. Similar language was applied to
the int8 VAE swap, whose "equivalent" status is aggregate parity at one seed, not per-frame
parity.

## Retraction 5 — The H3 token count: 63k → 15,810 (2026-09-29)

**Claimed.** Lab notes stated 544p/124f H3 runs at ~58k–63k tokens, and an earlier, undocumented
pre-audit estimate (~186k tokens) was used to explain the 243-frame 1080p refine failure.

**Measured.** From the documented geometry (f16t4d24, 1×2×2 patch merge): 31 frame-latents ×
(30×17) = 15,810 tokens. The 63k figure is 31 × 2,040 — the spatial grid before the patch merge,
mistaken for the sequence length. If it were the sequence, the linear term alone (4.17e15
FLOP/step) would require ≥62 s/step at the measured 67.2 TFLOP/s kernel ceiling; the measured
step time is 15.5–16.6 s/step. The corrected number also lands within 6% of what the measured step
time implies at that ceiling, which is why it is the one adopted.

**Why it failed.** Pre-patch latent counts and post-patch token counts were mixed in a single
sentence without a derivation. The conclusion (the refined 243f configuration is unviable) is
supported by direct measurement — 532–1087 s/step, first tile group 62.5 min — but the physical
explanation attached to it was wrong.

**What replaced it.** Doc 01 carries the full derivation, and the 243f post-mortem is explained by
measured PCIe streaming behavior rather than a token figure.

## Hardware confounder lessons

These apply to every performance number in this archive.

1. **The GPU is power-limited, and clocks were never logged.** Peak power across runs was
   218.6–221.0 W against the 220 W board limit, at 74–77 °C. The board sat at its power cap for
   the duration of 19-minute renders. SM clock was sampled only in a one-off probe, not in the
   benchmark runner, so boost decay across a run cannot be separated from software deltas. The
   step-time spreads on record (6–8% at matched configs) are within the range such decay could
   explain. Any future timing benchmark must lock clocks (`nvidia-smi -lgc`) and log
   `nvmlDeviceGetClockInfo` alongside power.
2. **Cold and warm states were not consistently matched.** The clearest case: the LTX CQ enhancer
   adoption compared a warm 352 s run against a 448 s baseline whose state is ambiguous; the
   same report attributes ~240 s of cost to a first-run-after-restart. A "−21% speedup" cannot be
   asserted from that pair. Future runs: one discarded warm-up per configuration, then interleaved
   A/B/A/B execution.
3. **Host memory pressure was monitored but not excluded.** Peak swap use in the H3 runs was
   0.30–0.35 GiB and minimum available RAM reached ~9.7 GiB on a 64 GB machine. Nothing in the
   runs suggests paging contaminated the results, but the machine was not held idle to production
   standards and background desktop load was not formally measured per run.
4. **Device-wide VRAM is not allocation accounting.** The 0.5 s NVML `used` counter includes the
   desktop compositor and other contexts. The figures in this archive are measured device peaks,
   but `torch.cuda.max_memory_allocated()` was not logged for video runs, so weight residency vs
   activation residency cannot be separated after the fact.
5. **N=1 is not a benchmark.** This is the largest single weakness of the archive. The sigma study
   exists specifically because the original ±3% quality gate was found to be below the model's own
   seed-to-seed variance, yet most adoption decisions afterwards still used a single seed. The fix
   is mechanical (≥5 seeds, ≥2 repeats, report mean and p95, treat overlapping distributions as
   ties) and is the first item on the follow-up list.
6. **Cross-system throughput is non-fungible.** The absolute numbers in this archive reflect a specific desktop topology: a quad-channel DDR4 memory bus with a dedicated PCIe 3.0 x16 connection to an RTX 3070. On systems with dual-channel RAM, PCIe x8 bifurcation, slower host storage, or different thermal boost curves, weight-streaming step times will scale differently. These benchmarks are shared as an empirical record of what this specific architecture tolerated and where it hit physical limits, not as an invariant performance SLA.

## Process changes already in effect

- Every benchmark row carries its N and seed; single-run comparisons are labeled as such.
- Quality adoption does not rest on the edge proxy alone; frame-level visual evidence or
  structural measurements (seam deltas, gear and composition checks) are required alongside it.
- Unverifiable third-party numbers are recorded as claims and excluded from summary tables.
- Corrections are written into the same documents that contain the original claim, with dates,
  rather than removed.

## Follow-up list

1. Re-run the four adoption-critical comparisons (ULT-A vs flagship, LoRA family at 544p, CQ
   enhancer, H1/H2) with ≥5 seeds, interleaved, clock-locked, and report mean ± σ and p95.
2. Add `torch.cuda.max_memory_allocated()` and NVML SM clocks to the runner telemetry.
3. Commit environment lockfiles (ComfyUI commit, torch, comfy-kitchen, node packs) for the lab
   workspace. The videolab distribution itself already ships pinned dependencies, vendored node
   packs, and a repo-relative layout (enforced by `selftest`).
4. Add at least one perceptual metric family (VBench sub-scores, CLIP-Temp, or optical-flow warp
   error) so the edge proxy is never the headline again.

## Corrections

This is a snapshot of my lab notes as of 2026-10-01. Some figures in it turned out wrong; the corrected version, with each correction listed, is [docs/research/VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](../../docs/research/VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md) in Kinoloom. Quote that one, not this file.
