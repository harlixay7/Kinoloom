# LTX 2.5: Kernel Correctness Gating and Pipeline Stability

> Provenance: the numbers and run records cited here come from the original lab
> archive, mapped in [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md). Raw run logs and render
> media are not redistributed with this folder.


This document covers the LTX 2.5 W4A8 work and the runtime defects that were found while getting
it to run reliably on 8 GB. The common thread is that the pipeline failures were in the runtime
and the harness, not in the model weights: a fused kernel that needed validation, a test that
caught a real bug in itself, a WDDM decode crash, and a tiling scheme with exact closure algebra.

Sources: the archived W4A8 implementation report (phases 0–9), the phase-1 W4A8
gate record (`phase1_w4a8_gate`), the keep-text-encoder campaign report
(`BENCH_2026-09-22_REPORT_KTE`), and the Qwen-Image 2.1 tiling-closure
validation logs.

## 1. Why the kernel was gated before anything else

The W4A8 checkpoints are 4-bit weights / 8-bit activations quantized with ConvRot, executed through
comfy-kitchen fused kernels. A silent fallback to eager dequantization would still produce video —
just slower — and would invalidate every performance number downstream. So the first artifact in
the program is a gate that answers three questions at once: which kernel runs, does it agree with
a float reference, and how fast is it.

Gate results (archive records `phase1_w4a8_gate` for LTX, `phase0_env_gate` for the H3 build):

| Check | Result |
|---|---|
| Fused kernel executed | `w4a8_codebook_linear_chunked`, 1 call, selected backend `cuda` |
| Silent eager fallback | 0 calls (instrumented counter) |
| Kernel vs same-activation float reference | 1.66e-3 relative L2 |
| Kernel vs fp32 dequantized-weight reference | 0.96% relative L2, cosine 0.999954 |
| CUDA vs eager backend | 4.52e-3 relative L2 |
| Throughput, m=1024, n=k=4096 | LTX gate: 0.496 ms → 69.26 TFLOP/s vs 4.96 eager (13.96×). H3 gate: 0.511 ms → 67.23 TFLOP/s (13.34×) |

The measured 67.23–69.26 TFLOP/s achieved by the fused W4A8 GEMM corresponds to at or slightly
exceeding the measured synthetic GEMM baseline (67.2 TFLOP/s) on this card, representing 41.35%–42.60% of the theoretical dense INT8
hardware peak (162.6 TOPS on GA104 SM 8.6, where dense INT8 tensor throughput is double the 81.3 TFLOPS
dense FP16 tensor rate: 46 SMs × 4 × 256 × 1.725 GHz; 67.23 / 162.6 ≈ 41.35%, 69.26 / 162.6 ≈ 42.60%),
on a synthetic square GEMM where all weights are resident. That number is a kernel ceiling, not a pipeline number:
in the real pipeline the same kernel is bounded by weight streaming (see doc 01 §3).

The 0.96% figure matters for honesty in both directions. It is the relative output error in the
layer-level kernel reference test (`gate_w4a8.py`) against the fp32 dequantized reference (with
9.7e-3 as the worst observed error among sampled benchmark layers), not a
global aggregate error across all checkpoint tensors; it is not a video-quality measurement, and
it does not establish that W4A8 output is perceptually identical to fp16. No per-frame parity test
was run (the fp16 checkpoint does not fit on this card), which is why "mathematically equivalent"
does not appear in these documents.

## 2. The N==K==4096 broadcast bug

During gate development, an intermediate "same-activation" check reported an 11.4% relative error
and looked like a broken kernel. It was not. The scale tensor `s_channel` has shape `[N]`
(per-output-channel). The harness was applying it along the K axis. With the standard square test
shape N == K == 4096, the wrong axis produces a valid-looking broadcast with no shape error, and
the result is silently wrong by a small amount.

The fix was in the harness, not the kernel: apply the scale on the correct axis and test with
non-square shapes so the axes cannot alias. With the corrected harness the same kernel matches at
1.66e-3.

Takeaways kept in the harness:

- Square matrices hide axis errors. At least one test per quantized path uses n != k and n != m.
- A failing correctness check implicates the harness first, then the kernel. The commit history
  here is: wrong result → wrong test → fixed test → passing kernel.
- Tolerances were never widened to make a test pass; the mechanism was found and fixed.

## 3. ComfyUI crash #15606: the Windows VAE decode crash

Symptom: sampling completes, then `VAEDecodeTiled` crashes or hangs the process. This is the
upstream ComfyUI issue #15606, reproduced on larger cards as well, and it is a Windows driver-path
problem rather than an 8 GB problem.

Diagnosis from the instruments: at the point of decode, the DynamicVRAM allocator had already
offloaded the transformer, with only 0.03 GiB allocated / 0.06 GiB reserved on the device at
eviction time. The decoder itself is not out of memory. What breaks is the sequence of
driver-level allocations around the tiled decode while stale offload state exists.

Fix: a passthrough node between the sampler and `VAEDecodeTiled` that calls
`unload_all_models()`, `soft_empty_cache()` and `torch.cuda.empty_cache()`, then lets decode load
cleanly. Result: 19/19 generated videos decoded without a crash, and 30+ crash-free runs in later
campaigns with the guard installed.

Follow-on optimization: the first version of the guard unloaded everything, which meant every
shot with a new prompt re-staged the 9.88 GiB Gemma encoder (56.3 s) and re-streamed the DiT for
the first step (+20.4 s), 80.3 s of avoidable overhead per shot. A `keep_text_encoder` flag
unloads everything except the text encoder. Controlled A/B/C/D on the same session, same 73f
configuration:

| Step | keep_te | Start state | Wall | Encode | Stage-1 | Edge proxy | Motion |
|---|---|---|---|---|---|---|---|
| A | off | warm | 72.4 s | 4.9 s | 31.7 s | 3195.7 | 4.07 |
| B | off | cold | 150.8 s | 56.3 s | 52.1 s | 3269.8 | 4.48 |
| C | on | cold | 124.6 s | 30.7 s | 51.8 s | 3052.7 | 4.30 |
| D | on | warm | 70.5 s | 5.3 s | 31.7 s | 3258.6 | 4.08 |

The B → D delta of −80.3 s per shot (−53%) reflects the total composite cold-to-warm pipeline difference
across prompts. Isolated matched comparisons show the specific contribution of `keep_text_encoder`:
on cold initialization (B → C), text-encoder retention saved 26.2 s (−17.4%); on warm steady-state runs
(A → D), it saved 1.9 s (−2.6%). The output statistics are identical at the same seed (same edge/motion
values; 7.66 vs 7.59 GB peak measured with the flag on), so this is pure overhead removal. A three-prompt
production simulation on the same session measured 295.6 / 299.0 / 281.7 s (mean 292.1 s, ~4.9 min), with
metrics stable across prompts — that is the only LTX row in the summary table with N=3.

## 4. Tiling mechanics

Two different tiling problems exist in this program and both were solved with exact algebra
rather than overlap tuning by feel.

**LTX VAE decode (video):** `VAEDecodeTiled(512, 64, 64, 16)` — 512 px tiles, 64 px overlap,
64 px temporal chunk, 16 px temporal overlap. Decode is the peak-VRAM moment after eviction, and
tiling keeps the conv VAE decode inside the budget. The conv VAE (`ltx-2.5-video-vae-conv-bf16`)
cut decode from 28.8 s to 10.9 s at 1024×576×121 versus the diffusion VAE with no regression
detected by the tested objective metrics at 1024×576×121, which is why it is the default.

**Qwen-Image 2K latent refinement (stills):** a 128×128 latent must be refined in 2×2 windows
without seams. For an n×n grid, window T, overlap O, exact closure requires n·T − (n−1)·O = H.
With H=128, n=2, O=16 the unique solution is T=72; the 64-token window in the original plan closes
only with zero overlap (seams) or with a 3×3 grid (9 tiles, over budget). The deployed scheme is
72×72 windows, 2×2 grid, stride 56, closure `2·72 − 16 = 128` exact.

Boundary blending uses premultiplied alpha. For window weights W_i and decoded color/alpha
(C_i, α_i):

```
num(p) = Σ W_i · α_i · C_i
den(p) = Σ W_i · α_i
C~(p)  = num(p) / max(den(p), ε)          α~(p) = den(p) / Σ W_i
```

Where overlapping tiles carry the same true color, num = C·den for any weight/alpha split, so the
reconstructed color is exact and no dark fringe forms (a naive RGB cross-fade biases toward
`ΣW(1−α)C`). The offline selftest asserts partition-of-unity closure (≤1e-7) and the premultiplied
seam property with a dark-fringe negative control; the runtime seam metric is an excess-gradient
statistic at the tile boundary, and the measured value on real 2K output was 0.0067 against a
0.001 target — recorded as a miss, attributed to the refiner's latent-window blend on real
scene content rather than the VAE seam, with the fix (32-token overlap or lower re-noise) written
down but not applied.

## 5. Pipeline stability rules

These are the operational rules that came out of the failure cases, all measured:

1. **MSR reference conditioning is stage-1 only.** References applied at stage 2 (1920×1088,
   ~34×60 latent grid) take ~4 minutes of model initialization and ~4 minutes per step, and with
   two or more references the tested stage-2 reference configurations stalled due to memory bandwidth limits
   (a silent stall with no diagnostic output). Stage-1-only references is the single most important rule in the LTX
   pipeline.
2. **Never wait blind.** A watchdog polls progress; a stall kills the run and the server restarts
   before the next job. Silent livelocks in dynamic-VRAM staging are real on this stack.
3. **Evict before decode, always.** Section 3; the cost of the guard is ~2 s.
4. **Determinism is checkable and was checked.** A warm rerun of the same graph reproduced the
   cold baseline exactly (edge 2138.6 / motion 2.73 identical; at production shape 2693.8 / 10.11
   twice). Same-seed execution controlled for pseudo-random initialization variance across
   identical graphs and driver states, isolating pipeline modifications from initialization noise
   (thermal decay and background OS variance remain unexcluded confounders).
5. **Stage-2 resolution falls for long clips.** An 8.04 s clip at 1080p stage-2 (51k stage-2
   tokens) thrashes weight streaming: 28.3 min wall. The same duration at 1280×704 stage-2 takes
   211.5 s (3.5 min) with a clean PCIe profile. Long-clip playbook: 5 s at 1080p stage-2, 6.7–8 s
   at 1280×704 plus post-upscale, beyond that concatenation or segmentation.
6. **Quantize the decode, not the sampling decisions.** The int8 ConvRot video VAE saved 2.2 GiB
   and decoded 1.5× faster in vendor measurements; on this machine the swap produced aggregate
   metrics within 0.1% of fp16 at the same seed (edge 1351.3 vs 1350.5). That is aggregate parity,
   not per-frame parity, and it is recorded as such.

## 6. Limits of this document

- The resolution ladder (512×288×73 through 1920×1088×121) is one run per rung; step times are the
  clean signal (1.04–37 s/step across the ladder), not the wall times, which include cold loads.
- No clock locking was used; the thermal confounder described in doc 03 ("Hardware confounder lessons", item 1) applies here too.
- The kernel gate is synthetic (m=1024, n=k=4096). It establishes kernel correctness and a
  ceiling, not per-layer behavior inside the transformer; a layer-level check ran on the real
  checkpoint (with 9.7e-3 as the worst observed error among the sampled benchmark layers vs physical reference, cosine 0.999953).

## Corrections

This is a snapshot of my lab notes as of 2026-10-01. Some figures in it turned out wrong; the corrected version, with each correction listed, is [docs/research/VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](../../docs/research/VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) in Kinoloom. Quote that one, not this file.
