[Kinoloom](../../README.md) › [Docs](../README.md) › Videolab retractions

# Retractions, Failures, and Lessons

> Copy of `videolab/docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md` at videolab commit
> 3f6cabc, fact-checked against the videolab tree, its configs and the H3
> lab run logs on the studio box (`D:\H3_LAB\renders\*\run.json`, 93 runs,
> 2026-09-19..28; not redistributed), plus the safetensors header of the shipped H3
> DiT checkpoint. Corrections are listed at the end.

> **In short:** This is the record of five claims in this program that were
> tested and found wrong, in the order they were corrected: what was claimed, what was
> measured, why it failed and what replaced it. After them come the hardware
> confounders that apply to every number in the archive, the process rules now in
> effect, and the follow-up list.

> Provenance: the numbers and run records cited here come from the original lab
> archive, mapped in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md). Raw run logs and render
> media are not redistributed with this repository.


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
was found for the PSNR table, the step-time table, or the 3.3× figure. All four were deleted from
the evidence base rather than explained away
(archived laboratory feasibility log, 2026-09-18).

**Why it failed.** The exact provenance of the 3.3× claim was never reconstructed. A plausible
class of error is cold-start contamination: a baseline that includes first-run compilation, kernel
autotuning or model staging can look arbitrarily slower than a warm challenger. This machine
produced that exact artifact repeatedly under controlled conditions: the LTX text encoder path
measured 150.8 s cold vs 72.4 s warm for the same graph (2.1×; runs B and A of the
keep-text-encoder A/B, doc 02 §3), and the Qwen benchmark measured
219.6 s of warming time between its cold and warm runs. Any speedup claim of this shape without a
warm-up isolation protocol is unverifiable.

**What replaced it.** A rule: no measurement claim enters a document without a run log produced on
this machine. Third-party figures are recorded as claims, not measurements. The deletions are listed
in the feasibility report itself, including the ones that were later shown to be wrong in the
other direction (W4A8 turned out to be a first-party ComfyUI path, so the original "not
supported" conclusion was also retracted after re-verification).

## Retraction 2 — The 6.75–7.78× end-to-end speedup plan (2026-09-22)

**Claimed.** A decision-engine plan projected 6.75–7.78× end-to-end speedup on the LTX pipeline
through adaptive step skipping, token compaction, dynamic CFG and speculative denoising.

**Measured.** An independent re-derivation against local measurements showed:

- The FLOP model was 1.8–2.2× optimistic versus measured step times (predicted 2.918 s / 20.399 s
  for stage-1/stage-2; measured 5.30 s / 37–45 s).
- The audit put the non-sampling overhead (text encode, model load/evict, VAE decode) at 75% of the
  341 s baseline clip, none of which the proposed optimizations touched. That share does not
  follow from the step times quoted above if the baseline ran the distilled ladder's 8 + 3 steps
  (its step counts are not recorded in this tree): 8 × 5.30 s + 3 × (37–45) s = 153–177 s of
  sampling is 45–52% of 341 s, i.e. about half the clip would be non-sampling. The audit's own
  derivation is archived, not re-verifiable here.
- Adaptive stepping on the distilled LTX ladder moved the trajectory off the trained schedule; the
  same failure class had already been measured on H3 (Retraction 3).
- The VRAM budget arithmetic was wrong (the plan's own line items sum to 8,085 MB against a
  claimed 7,364 MB) and omitted the 9.9 GiB text encoder entirely.

**Why it failed.** The plan optimized the part of the pipeline that was already efficient and
assumed away the part that wasn't. The re-derivation of the same levers came to ~284 s end-to-end
(341/284 = 1.2×; the source also calls it "~1.2× on sampling", and which ratio was meant is not
recoverable here).

**What replaced it.** The plan was marked NO-GO and re-scoped. The re-scoped program (sigma-ladder
compaction and text-encoder retention) delivered verified improvements measured against isolated
baselines. The early 73-frame exploratory A/B measured the full cold-to-warm composite difference
at 80.3 s (150.8 s cold vs 70.5 s warm, flag setting changed as well). On the 121-frame production
pipeline, warm steady-state execution averaged 292.1 s (~4.9 min, N=3) against the ~341 s
baseline, 48.9 s lower. That comparison is warm against a baseline of unrecorded state, with the
sigma ladder (S6a) and the text-encoder policy both changed, so the 48.9 s is not attributed to a
single lever; the isolated `keep_text_encoder` effect, from the 73-frame A/B, is 26.2 s cold and
1.9 s warm. The rest of the ambitious hooks were either rejected with evidence or left as research
spikes.

## Retraction 3 — PDD-8 acceleration, adopted then banned (2026-09-22)

**Claimed.** The H3 Phase H report certified PDD 8-step as production acceleration: 2.5× fewer
sampling steps (8 vs 20) with "visual parity" on face, motion and VFX scenes, based on visual
review. On wall time that was ~2.0× (~140 s vs ~277 s warm; the H1–H3 PDD runs at 832×480 took
138.7–148.3 s, the E5–E6 20-step runs 274.0–274.5 s).

**Measured.** A re-test with the same scene and seed, comparing a warm 20-step control against
PDD-8, measured edge-proxy 962.4 → 746.6 (−22%). Whole-frame softening was confirmed on the
strips; the frames were not equivalent. The re-test's speed figures ("~6% faster at 39 frames,
~15% at 124 frames", `h3.proven.json:156`, the 124-frame one marked "documented") have no run log
here. The studio registry's note for the PDD draft tier gives their basis as an A/B/C at 39
frames with a 1080p refine and calls them end-to-end (`tool/video_models_user.json`), so they
mostly reflect the unchanged refine, not the sampler. Without a refine the logs show a large
gain: in the Phase H runs (832×480×124, t2v, same scenes and seeds) PDD-8 sampled in 91–94 s
against 230–233 s for the 20-step E4–E6 runs (2.45–2.55× less sampler time) and finished in
138.7–148.3 s against 274.0–274.5 s wall (~1.9×); at 960×544 the PDD CERT runs sampled in about
130 s against about 314 s for 20 steps of the E1 ladder at the same seed. The reason to ban PDD-8
from quality paths is the quality loss, not a small speed gain.

**Why it failed.** The Phase H certification used visual review without a matched control. At
review scale, whole-frame softening reads as "fine"; against a same-seed control it is 22% of the
texture proxy. The audit that followed found that in the tested pipelines, adaptive step skipping
introduced severe artifacts on distilled checkpoints; its view was that such methods need
non-distilled models with CFG > 1 (not tested here), which H3 (CFG 1.0, shift-12 simple schedule)
is not.

**What replaced it.** PDD-8 was banned from quality paths and kept only as an explicitly labeled
draft tier. The same matched-control requirement now applies to step-count or schedule changes on
this stack.

## Retraction 4 — The "+91% edge" quality claim (2026-09-26/27)

**Claimed.** The ULT-A render (6-step turbo LoRA) was reported as the best render of the program:
edge-variance proxy 2819.8 vs 1472.5 for the 25-step flagship (+91%), and the turbo LoRA family as
a quality default.

**Measured.** The number is real as a proxy measurement and the comparison is same-seed — but it
is one run per configuration, hours apart, with different cache and thermal states, and the proxy
is a Laplacian texture metric, not perceptual quality. The program's own sigma study on the sister
pipeline measured same-seed pair spreads of −56%..+40% (edge) and −46%..+108% (motion) across
trajectories, and a cross-seed edge span of 173.6 to 729.5 on one scene. The claimed LoRA deltas
(+85% to +91%) are larger than that pair spread, but each rests on one run per configuration, so
their size is not established. The metric's ordering also disagrees with visual judgment: at
1920×1088 the clip that looked richest (DFR, seed 469) was the smoothest by
the metric (mean Laplacian variance 36 vs 1232 for the no-DFR clip), and the author of the LTX
report wrote the correction into the same document
(`LTX25_W4A8_IMPLEMENTATION_REPORT.md` §14).

**Why it failed.** A single-seed texture proxy was used as a stand-in for visual quality, and the
metric's own documentation says it is not a quality score. The adoption decisions that followed
(composite anchor, newest turbo adapters) inherited that weak foundation.

**What replaced it.** Adoption of the composite-anchor config rests on frame-level visual checks
(wardrobe and composition held across all 124 frames) with the proxy as a secondary signal; the
docs state the proxy's limits inline. The turbo-LoRA-family adoption is now labeled exploratory
and explicitly queued for a seed sweep with perceptual metrics. Similar language was applied to
the int8 VAE swap, whose "equivalent" status is aggregate parity at one seed, not per-frame
parity (and whose run record does not confirm the swap, see doc 02 §5 rule 6).

## Retraction 5 — The H3 token count: 63k → 15,810 (2026-09-29)

**Claimed.** Lab notes stated 544p/124f H3 runs at ~58k–63k tokens, and an earlier, undocumented
pre-audit estimate (~186k tokens) was used to explain the 243-frame 1080p refine failure.

**Measured.** From the documented geometry (f16t4d24, 1×2×2 patch merge): 31 frame-latents ×
(30×17) = 15,810 tokens. The 63k figure is 31 × 2,040 — the spatial grid before the patch merge,
mistaken for the sequence length. If it were the sequence, the linear term alone (2 × 19.27e9 ×
63,240 ≈ 2.44e15 FLOP/step, with the linear-layer parameter count of the shipped checkpoint) would
require ≥36 s/step at the 67.2 TFLOP/s GEMM gate figure; the measured phase-1 step time at
960×544 is 15.5–16.2 s/step (E1 step ladder, kitchen attention). The corrected count rests on the
geometry. (It counts the video tokens; the packed sequence also carries text rows, ~2 rows per
audio latent and, in ref2va, one frame grid per reference image, doc 01 §2.) (An earlier version also argued that 15,810 lands within 6% of what the step time
implies at the gate rate; that fit used a 33e9 parameter count and the 832×480 Phase C step times,
and does not hold with the checkpoint's 19.27B: at the gate rate the linear term would take
about 9.1 s, 56–58% of the measured step, doc 01 §2.)

**Why it failed.** Pre-patch latent counts and post-patch token counts were mixed in a single
sentence without a derivation. The conclusion (the refined 243f configuration is unviable) is
supported by direct measurement — first tile group 62.5 min for 5 steps (750.7 s/step average;
532–1087 s/step per the archived report) — but the physical explanation attached to it was wrong.

**What replaced it.** Doc 01 carries the full derivation, and the 243f post-mortem is explained by
measured PCIe streaming behavior rather than a token figure.

## Hardware confounder lessons

These apply to every performance number in this archive.

1. **The GPU is power-limited, and clocks were never logged.** Peak power in the tabled H3 runs
   was 218.6–221.0 W against the 220 W board limit (218.2–221.3 W across all 93 H3 logs except two
   Phase A probes at 214.3 and 217.2 W and a 2 s fully cached rerun), at
   75–77 °C peak (the config note at `h3.proven.json:183` says 74–77 °C). The board ran near its cap for most
   of a render, not all of it (ULT-A: 72.5% of samples at or above 200 W, mean 172 W over the
   whole run, the ten time-slices averaging 37.8–213.7 W). SM clock was sampled only
   in a one-off probe, not in the benchmark runner, so boost decay across a run cannot be
   separated from software deltas. The step-time spreads on record (6–8% at matched configs) are
   within the range such decay could explain. Any future timing benchmark must lock clocks and log
   `nvmlDeviceGetClockInfo` alongside power. `nvidia-smi` failed on this machine
   (`videolab/bench/harness.py:12-13`), so clock locking would have to go through NVML
   (`nvmlDeviceSetGpuLockedClocks`, administrator rights).
2. **Cold and warm states were not consistently matched.** The clearest case: the LTX CQ enhancer
   adoption compared a warm 352 s run against a 448 s baseline whose state is ambiguous; the
   same report attributes ~240 s of cost to a first-run-after-restart. A "−21% speedup" cannot be
   asserted from that pair. Future runs: one discarded warm-up per configuration, then interleaved
   A/B/A/B execution.
3. **Host memory pressure was monitored but not excluded.** In the runs tabled in doc 01 §5 and
   the H3 rows of the VIDEOLAB.md results table, peak swap use was 0.30–0.35 GiB and minimum available RAM 9.56 GiB on a 64 GB machine. Other
   H3 runs paged more: the E1 step ladder (doc 01 §2) used ~1.0 GiB of swap, C6 1.92 GiB with
   3.98 GiB available, A4 dropped to 2.94 GiB available, and `hero_v2_crf16` to 0.40 GiB
   available with 2.48 GiB of swap. Whether paging affected those runs' timings was not
   examined; the machine was not held idle to production standards and background desktop load
   was not formally measured per run.
4. **Device-wide VRAM is not allocation accounting.** The NVML `used` counter (nominal 0.5 s
   sampling, ~0.57 s in practice) includes the desktop compositor and other contexts. The figures
   in this archive are measured device peaks, but `torch.cuda.max_memory_allocated()` was not
   logged for video runs, so weight residency vs activation residency cannot be separated after
   the fact.
5. **N=1 is not a benchmark.** This is the largest single weakness of the archive. The sigma study,
   run to qualify the S6a ladder, found that the original ±3% quality gate was below the model's
   own seed-to-seed variance, yet most adoption decisions afterwards still used a single seed. The
   fix is mechanical (≥5 seeds, ≥2 repeats, report mean and p95, treat overlapping distributions
   as ties) and is the first item on the follow-up list.
6. **Cross-system throughput is non-fungible.** The absolute numbers in this archive reflect a specific desktop topology: a quad-channel DDR4 memory bus with a dedicated PCIe 3.0 x16 connection to an RTX 3070. On systems with dual-channel RAM, PCIe x8 bifurcation, slower host storage, or different thermal boost curves, weight-streaming step times will scale differently. These benchmarks are shared as an empirical record of what this specific architecture tolerated and where it hit physical limits, not as an invariant performance SLA.

## Process changes already in effect

- Adoption rows carry their N and seed; single-run comparisons are labeled as such. (Some
  supporting tables, e.g. doc 02's A/B/C/D table and doc 01's PCIe table, do not have N or seed
  columns.)
- Quality adoption does not rest on the edge proxy alone; frame-level visual evidence or
  structural measurements (seam deltas, gear and composition checks) are required alongside it.
- Unverifiable third-party numbers are recorded as claims and excluded from summary tables.
- Corrections are written into the documents that contained the original claim, with dates;
  claims removed from the evidence base (Retraction 1) are listed as removed.

## Follow-up list

1. Re-run the four adoption-critical comparisons (ULT-A vs flagship, LoRA family at 544p, CQ
   enhancer, H1/H2) with ≥5 seeds, interleaved, clock-locked, and report mean ± σ and p95.
2. Add `torch.cuda.max_memory_allocated()` and NVML SM clocks to the runner telemetry.
3. Commit environment lockfiles (ComfyUI commit, torch, comfy-kitchen, node packs) for the lab
   workspace. The videolab distribution itself already ships pinned dependencies (psutil is a
   lower bound), vendored node packs, and a repo-relative layout (checked by `selftest --lint`).
4. Add at least one perceptual metric family (VBench sub-scores, CLIP-Temp, or optical-flow warp
   error) so the edge proxy is never the headline again.

## Fact-check corrections

How checked: **[code]** videolab tree at 3f6cabc; **[cfg]** `videolab/configs/*.proven.json`;
**[log]** H3 run logs on `D:`; **[hdr]** H3 DiT safetensors header; **[calc]** recomputed.

1. L20–21 "No primary source exists" → "No primary source was found". [wording]
2. L25–27 "The class of error is cold-start contamination" right after "provenance … never
   reconstructed" → "A plausible class of error". [wording]
3. L28–29 "150.8 s cold vs 70.5 s warm for the same graph (2.1×)" → B and D differ in
   `keep_text_encoder`; the same-graph pair is B vs A, 150.8 vs 72.4 s (2.08×). [cfg]
4. L33 "no number enters a document without a run log" → "no measurement claim"; the docs
   also carry labelled vendor and derived figures. [wording]
5. L46–47 "1.7–1.9× optimistic" → 5.30/2.918 = 1.82, 37/20.399 = 1.81, 45/20.399 = 2.21:
   1.8–2.2×. [calc]
6. L48–49 "75% of the 341 s baseline clip was non-sampling overhead" → inconsistent with the
   quoted step times if the baseline ran 8 + 3 steps (sampling 153–177 s = 45–52%; the step
   counts are an assumption, not recorded); kept as the audit's figure with the discrepancy
   stated. [calc]
7. L56–57 "~1.2× on sampling, ~284 s end-to-end" → 341/284 = 1.20 is end-to-end; noted.
   [calc]
8. L60–66 48.9 s "driven primarily by text-encoder host caching (26.2 s cold / 1.9 s warm,
   isolated) combined with fused W4A8 kernel staging" → warm vs a baseline of unrecorded state;
   ladder and encoder policy both changed; 26.2 / 1.9 s come from the 73-frame A/B; the 341 s
   baseline already ran W4A8 (ARCHIVE_INDEX, LTX `IMPLEMENTATION_REPORT.md`), so kernel staging
   is not a lever here; not attributed. [cfg][code]
9. L70–71 "2.5× faster sampling (~140 s vs ~277 s warm wall)" → 2.5× is the step ratio
   (20/8); 277/140 = 1.98× on wall; H1–H3 PDD walls 138.7–148.3 s vs E5–E6 274.0–274.5 s.
   [log][calc]
10. L81–83 "functioning reliably only on non-distilled models with CFG > 1" → the audit's
    view, not tested here. [wording]
11. L86 "The same rule now applies to all step-count or schedule changes" → the
    matched-control requirement (the 6-step ULT-A, 4-step C3 and S6a were later adopted).
    [cfg: `h3.proven.json:46,69`, `ltx25.proven.json:103`]
12. L96–98 spreads "larger than the claimed LoRA deltas" → false as written: +85/+89/+91%
    exceed the +40% end of the pair spread; restated, with the cross-seed span 173.6–729.5
    (`ltx25.proven.json:109`). [cfg][calc]
13. L98–101 "ordering does not even survive a resolution change" → the example shows the
    metric disagreeing with visual judgment at one resolution; reworded. [wording]
14. L121–125 Retraction 5: "4.17e15 … ≥62 s/step … measured step time is 15.5–16.6 s/step
    … within 6% of what the measured step time implies at that ceiling, which is why it is the
    one adopted" → 33e9 → 19.27B (2.44e15, ≥36 s/step); 15.5–16.6 s/step is the 832×480
    Phase C range (runs C1–C6), 960×544 measured 15.5–16.2; the "within 6%" fit (which was
    −0.1% to +7.0% even on its own terms) no longer holds and is withdrawn; the count rests on
    geometry. [hdr][log][calc]
15. L128–129 "532–1087 s/step, first tile group 62.5 min" → added the logged 750.7 s/step
    average (1:02:33 for 5 steps, `a3ad86a2`); 532–1087 stays as the archived figure. [log]
16. L140–141 "The board sat at its power cap for the duration of 19-minute renders" →
    ULT-A `bd27b7ba`: mean 171.9 W, 72.5% of samples ≥ 200 W, decile means 37.8–213.7 W. [log]
17. L144–145 "lock clocks (`nvidia-smi -lgc`)" → `nvidia-smi` failed on this machine
    (`harness.py:12-13`); NVML `nvmlDeviceSetGpuLockedClocks` is the available route. [code]
18. L152 "minimum available RAM reached ~9.7 GiB" → 9.6 GiB (H2 `e5c812a2`). [log]
19. L155 "The 0.5 s NVML used counter" → nominal 0.5 s, ~0.57 s in practice. [code][log]
20. L159–161 the sigma study "exists specifically because the original ±3% quality gate was
    found to be below the model's own seed-to-seed variance" → it was the S6a qualification run
    that found this (`ltx25.proven.json:102-112`, ARCHIVE_INDEX). [cfg]
21. L168 "Every benchmark row carries its N and seed" → "Adoption rows"; two supporting tables
    lack the columns. [code]
22. L172–173 "rather than removed" vs Retraction 1's deletions → reworded so both hold.
    [wording]
23. L181–182 "repo-relative layout (enforced by `selftest`)" → checked only by
    `selftest --lint` (`videolab.py:635, 932`); psutil is `>=6.0.0` (`requirements.txt:16`).
    [code]

Second review (an independent re-check against the same evidence):

24. L75–76 Retraction 3 speed: "roughly 6% faster sampling at 39 frames (~15% at 124 frames)" →
    the re-test's figures are archived (`h3.proven.json:156`) and, per the studio registry's
    source note, end-to-end with a 1080p refine, not sampling; without a refine the 124-frame
    Phase H logs show 2.45–2.55× less sampler time (91–94 s vs 230–233 s) and ~1.9× less wall
    (138.7–148.3 vs 274.0–274.5 s), and the 960×544 CERT runs ~130 s vs ~314 s sampling at
    the same seed. [log][code]
25. L140 hardware lesson 1 "218.6–221.0 W … at 74–77 °C" → scoped to the tabled runs (75–77 °C
    in their logs; 74–77 °C is the config note at `h3.proven.json:183`; all 93 logs:
    218.4–221.3 W). [log][cfg]
26. L151–154 hardware lesson 3 "Peak swap use in the H3 runs was 0.30–0.35 GiB … ~9.6 GiB … Nothing
    in the runs suggests paging contaminated the results" → true only for the doc 01 §5 runs;
    E1 ~1.0 GiB swap, C6 1.92 GiB, A4 2.94 GiB available, `hero_v2_crf16` 0.40 GiB available
    with 2.48 GiB swap; the "nothing suggests" sentence is withdrawn. [log]
27. L112–113 Retraction 4, last sentence: the parenthesis on the int8 VAE run record was added in the
    first pass without a numbered entry; it is true (every H3 run config records the fp16 VAE).
    [log]
28. Correction 11 cited `h3.proven.json:70`; C3's `adopted_as` is at line 69. [cfg]
29. L121 Retraction 5: 15,810 counts video tokens only; the packed H3 sequence also carries text,
    audio rows and (ref2va) reference rows (`comfy/ldm/minimax/model.py:5-7`), so rates derived
    from it are lower bounds. See doc 01 §2. [code]

Third check (a light-touch pass against the same run logs):

30. Hardware lesson 1 "218.4–221.3 W across all 93 H3 logs" (item 25) → 218.2–221.3 W with
    three exceptions. Two full renders peaked below 218.4 W: `h3_t1_realism` `e50dd09d`
    (218.20 W) and `iceq_b3_larry6` `fa23981e` (218.28 W). The exceptions are the Phase A probes
    `A0_te_probe` (214.30 W) and `A1_smoke_fl2va` (217.23 W), and `C1_w4a8_face` `ea764c64`
    (24.84 W; 2.0 s wall, 4 samples, 15 nodes cached). Values are `monitor.peak_power_w`. [log]
31. Hardware lesson 3 "In the runs tabled in doc 01 §5 … minimum available RAM 9.56 GiB" (item
    26) → 9.56 GiB is H2 `e5c812a2`, which is not in doc 01 §5; the doc 01 §5 runs bottom out at
    9.88 GiB (chain link 2 `d90d7414`). The scope now adds the H3 rows of the VIDEOLAB.md
    results table, for which both the 0.30–0.35 GiB swap and the 9.56 GiB hold; the numbers are
    unchanged. [log]

Checked and correct as written: 89.4/27.1 = 3.30; 481.88 − 262.29 = 219.59 s
(`qwen21.proven.json:42-44`); 80.3 and 48.9 s; −22.4% and ~6% / ~15%
(`h3.proven.json:156`); H3 sampler constants (CFG 1.0, shift 12, simple); 2819.8 / 1472.5 = +91%;
36 vs 1232; 15,810; 63,240; peak power 218.6–221.0 W in the tabled runs (run logs); swap
0.30–0.35 GiB in the doc 01 §5 runs; no clock or `max_memory_allocated` logging in `harness.py`.

Archived, not re-verifiable from this repository: the Retraction 1 claims and the feasibility
log; the audit's 2.918 / 20.399 s predictions, 5.30 / 37–45 s measurements, the 75% share and
the 8,085 / 7,364 MB VRAM arithmetic; ~284 s; the Phase H certification; 962.4 → 746.6; the
LTX report §14 example; ~186k tokens; 532–1087 s/step; the one-off clock probe; the 6–8%
step-time spread; the CQ 352 / 448 s and ~240 s figures.
