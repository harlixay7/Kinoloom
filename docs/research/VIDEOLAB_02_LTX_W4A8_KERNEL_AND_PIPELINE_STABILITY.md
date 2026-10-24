[Kinoloom](../../README.md) › [Docs](../README.md) › Videolab LTX kernel

# LTX 2.5: Kernel Correctness Gating and Pipeline Stability

> Copy of `videolab/docs/02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md` at videolab
> commit 3f6cabc, fact-checked against the videolab tree, its configs and
> the H3 lab run logs on the studio box (`D:\H3_LAB\renders\*\run.json`, 93 runs,
> 2026-09-19..28; not redistributed). No LTX run logs are on this machine, so the LTX
> measurements here were checked against `videolab/configs/ltx25.proven.json` only.
> Corrections are listed at the end.

> **In short:** This doc covers the LTX 2.5 W4A8 work on an 8 GB card and the
> runtime defects found on the way: the fused-kernel gate, a broadcast bug that
> sat in the test harness rather than the kernel, the Windows VAE decode crash and
> the eviction node that fixed it, and the tiling algebra. No LTX run logs are on
> this machine, so the LTX figures here rest on `ltx25.proven.json` and the archive.

> Provenance: the numbers and run records cited here come from the original lab
> archive, mapped in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md). Raw run logs and render
> media are not redistributed with this repository.


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
the program is a gate that answers three questions at once: which kernel runs (it counts the fused
kernel's calls and asserts zero eager-fallback calls and CUDA backend selection; it does not assert
a minimum kernel call count), does it agree with a float reference, and how fast is it.

Gate results (archive records `phase1_w4a8_gate` for LTX, `phase0_env_gate` for the H3 build):

| Check | Result |
|---|---|
| Fused kernel executed | `w4a8_codebook_linear_chunked`, 1 call, selected backend `cuda` |
| Silent eager fallback | 0 calls (instrumented counter) |
| Kernel vs same-activation float reference | 1.66e-3 relative L2 |
| Kernel vs fp32 dequantized-weight reference | 0.96% relative L2, cosine 0.999954 |
| CUDA vs eager backend | 4.52e-3 relative L2 |
| Throughput, m=1024, n=k=4096 | LTX gate: 0.496 ms → 69.26 TFLOP/s vs 4.96 eager (13.96×). H3 gate: 0.511 ms → 67.23 TFLOP/s (13.34×) |

Two gate builds measured 67.23 TFLOP/s (H3) and 69.26 TFLOP/s (LTX) for the fused W4A8 GEMM at this
one shape. That is 41.35%–42.60% of the theoretical dense INT8
hardware peak (162.6 TOPS on GA104 SM 8.6, where dense INT8 tensor throughput is double the 81.3 TFLOPS
dense FP16 tensor rate: 46 SMs × 4 × 256 × 1.725 GHz; 67.23 / 162.6 ≈ 41.35%, 69.26 / 162.6 ≈ 42.60%),
on a synthetic square GEMM where all weights are resident. (The "67.2 TFLOP/s" used as a reference
in doc 01 is the H3 gate's own 67.23, not an independent ceiling.) It is a reference throughput at
one shape, not a pipeline number: in the real pipeline the same kernel runs inside a step that also
includes attention and weight streaming (see doc 01 §2–§3).

The 0.96% figure matters for honesty in both directions. It is a relative output error against
an fp32 dequantized-weight reference; `videolab/bench/gate_w4a8.py` (docstring and the comment at
:300-301) attributes it to "the layer-level reference test", and the worst observed error among
the sampled benchmark layers was 9.7e-3 (cosine 0.999953). The gate also runs its own
fp32-reference check on random synthetic weights (m=1024, n=k=4096); whether 0.96% is that
check's result or a separate layer-level test is not established, because the measured gate
outputs are archived. Neither figure is a
global aggregate error across all checkpoint tensors; neither is a video-quality measurement, and
they do not establish that W4A8 output is perceptually identical to fp16. No per-frame parity test
was run (the fp16 checkpoint does not fit on this card), which is why "mathematically equivalent"
does not appear in these documents.

## 2. The N==K==4096 broadcast bug

During gate development, an intermediate "same-activation" check reported an 11.4% relative error
and looked like a broken kernel. It was not. The scale tensor `s_channel` has shape `[N]`
(per-output-channel). The harness was applying it along the K axis. With the standard square test
shape N == K == 4096, the wrong axis produces a valid-looking broadcast with no shape error, and
the result is silently wrong by about 11%.

The fix was in the harness, not the kernel: apply the scale along dim 0
(`s_channel.view(-1, 1)`, `gate_w4a8.py:320-323`). With the corrected harness the same kernel
matches at 1.66e-3. The shipped gate still uses the square N = K = 4096 shape (`gate_w4a8.py:178`);
a non-square case is not part of it.

Takeaways:

- Square matrices hide axis errors. A test with n != k and n != m would catch this class of bug;
  the shipped gate does not have one yet.
- A failing correctness check implicates the harness first, then the kernel. The lab history
  was: wrong result → wrong test → fixed test → passing kernel.
- Tolerances were not widened after failures to make a test pass; the mechanism was found and
  fixed. (The cross-backend bound is deliberately loose, `gate_w4a8.py:298`, for the reason given
  there.)

## 3. ComfyUI crash #15606: the Windows VAE decode crash

Symptom: sampling completes, then the decode crashes or hangs the process. This is the
upstream ComfyUI issue #15606, reproduced on larger cards as well, and it is a Windows driver-path
problem rather than an 8 GB problem.

Mechanism: the eviction node's own documentation
(`videolab/nodes/ltx25_lab_nodes/evict.py:1-7`) gives the upstream mechanism — on Windows,
constructing the diffusion VAE decoder while the DiT is resident raises an access violation and the
process dies — and the reporter's bisected workaround (`unload_all_models` + `empty_cache` between
sampler and decode). The archive's own diagnosis (at decode time the DynamicVRAM allocator had
already offloaded the transformer, 0.03 GiB allocated / 0.06 GiB reserved at eviction time, and the
failure lies in driver-level allocations around the tiled decode) is archived, not re-verifiable
here, and is not the same mechanism; which one applies to the shipped conv-VAE path was not
established in this repository.

Fix: a passthrough node between the sampler and `VAEDecodeTiled` that calls
`unload_all_models()` (or, with `keep_text_encoder`, `free_memory(…, keep_loaded=<text encoder>)`),
`soft_empty_cache()` and `torch.cuda.empty_cache()`, then lets decode load
cleanly. Result: 19/19 generated videos decoded without a crash, and 30+ crash-free runs in later
campaigns with the guard installed (archived).

Follow-on optimization: the first version of the guard unloaded everything, which meant every
shot with a new prompt re-staged the 9.88 GiB Gemma encoder (56.3 s) and re-streamed the DiT for
the first step (+20.4 s), up to ~77 s of cold staging per shot. A `keep_text_encoder` flag
unloads everything except the text encoder. Controlled A/B/C/D on the same session, same 73f
configuration:

| Step | keep_te | Start state | Wall | Encode | Stage-1 | Edge proxy | Motion |
|---|---|---|---|---|---|---|---|
| A | off | warm | 72.4 s | 4.9 s | 31.7 s | 3195.7 | 4.07 |
| B | off | cold | 150.8 s | 56.3 s | 52.1 s | 3269.8 | 4.48 |
| C | on | cold | 124.6 s | 30.7 s | 51.8 s | 3052.7 | 4.30 |
| D | on | warm | 70.5 s | 5.3 s | 31.7 s | 3258.6 | 4.08 |

(`ltx25.proven.json:88-100` holds the four walls, the B and D encode times and the B and D
stage-1 times; the other cells and all edge and motion values are archived.)

The B → D delta of −80.3 s per shot (−53%) is the composite cold-to-warm difference, with the flag
setting changed as well. The matched comparisons show the specific contribution of
`keep_text_encoder`: on cold initialization (B → C) it saved 26.2 s (−17.4%); on warm steady-state
runs (A → D) it saved 1.9 s (−2.6%). The rows' output statistics differ (edge 3052.7–3269.8, motion
4.07–4.48), so the table itself does not show identical output; the archive's statement that the
flag leaves output identical at the same seed rests on a pair not shown here (7.66 vs 7.59 GB peak
were measured with the flag on). A three-prompt
production simulation on the same session measured 295.6 / 299.0 / 281.7 s (mean 292.1 s, ~4.9 min), with
metrics stable across prompts — that is the only LTX row in the summary table with N=3.

## 4. Tiling mechanics

Two different tiling problems exist in this program and both were solved with exact algebra
rather than overlap tuning by feel.

**LTX VAE decode (video):** `VAEDecodeTiled(512, 64, 64, 16)` — 512 px tiles, 64 px overlap,
64-frame temporal chunks, 16-frame temporal overlap (ComfyUI counts `temporal_size` and
`temporal_overlap` in frames). Decode is the peak-VRAM moment after eviction, and
tiling keeps the conv VAE decode inside the budget. The conv VAE (`ltx-2.5-video-vae-conv-bf16`)
cut decode from 28.8 s to 10.9 s at 1024×576×121 versus the diffusion VAE with no regression
detected by the tested objective metrics at 1024×576×121, which is why it is the default.

**Qwen-Image 2K latent refinement (stills):** a 128×128 latent must be refined in 2×2 windows
without seams. For an n×n grid, window T, overlap O, exact closure requires n·T − (n−1)·O = H.
With H=128, n=2, O=16 the unique solution is T=72; the 64-token window in the original plan closes
only with zero overlap (seams) or with a 3×3 grid (9 tiles, over budget). The implemented scheme is
72×72 windows, 2×2 grid, stride 56, closure `2·72 − 16 = 128` exact. It is not deployed: in the
locked Qwen config the refiner branch is present but unreachable (`SaveImageAdvanced` reads the
upscaled image directly; wiring it in peaked at 7,811 MB, over the 7,392 MB ceiling,
`videolab/configs/qwen21.proven.json:18`).

The latent windows are blended with a separable sinusoidal partition of unity,
ΣW_i·f_i / ΣW_i (`videolab/nodes/comfyui_qwen21_optimized/nodes/node_latent_tiled_refiner.py:18-20`).
Premultiplied alpha belongs to the pixel-domain tiled decode (`VAEDecodeTiledPremulAlpha`,
512 px tiles, 64 px overlap, `node_premul_vae_tiled.py:1-14`). For tile weights W_i and decoded
color/alpha (C_i, α_i):

```
num(p) = Σ W_i · α_i · C_i
den(p) = Σ W_i · α_i
C~(p)  = num(p) / max(den(p), ε)          α~(p) = den(p) / Σ W_i
```

Where overlapping tiles carry the same true color, num = C·den for any weight/alpha split, so the
reconstructed color is exact and no dark fringe forms (a cross-fade of premultiplied RGB that is
not de-multiplied darkens semi-transparent edges, by ΣW_i(1−α_i)C/ΣW_i). The offline selftest
checks the tiling closure as integer arithmetic (T = 72 from `(128+16)/2`, `2·72 − 16 = 128`,
72² ≤ 6,400) and the premultiplied reconstruction with a two-sample scalar example plus a darkening
negative control (`videolab/videolab.py:532-556`). The runtime seam metric is an excess-gradient
statistic at the tile boundary, and the measured value on real 2K output was 0.0067 against a
0.001 target (archived) — recorded as a miss, attributed to the refiner's latent-window blend on real
scene content rather than the VAE seam, with the fix (32-token overlap or lower re-noise) written
down but not applied.

## 5. Pipeline stability rules

These are the operational rules that came out of the failure cases, all measured:

1. **MSR reference conditioning is stage-1 only.** References applied at stage 2 (1920×1088,
   ~34×60 latent grid) take ~4 minutes of model initialization and ~4 minutes per step, and with
   two or more references the tested stage-2 reference configurations stalled silently (no
   diagnostic output; memory-bandwidth saturation is the suspected cause). Stage-1-only references
   is the single most important rule in the LTX pipeline.
2. **Never wait blind.** In the lab, a watchdog script polled progress, killed a stalled run and
   restarted the server before the next job. Silent livelocks in dynamic-VRAM staging are real on
   this stack. The shipped harness enforces only a total timeout (`--timeout`, default 7,200 s);
   it does not detect stalls, cancel the ComfyUI job or restart the server
   (`videolab/bench/harness.py:322-343`).
3. **Evict before decode, always (LTX).** Section 3; the cost of the guard is ~2 s (archived). The H3
   templates have no eviction node (doc 01 §1).
4. **Determinism is checkable and was checked.** A warm rerun of the same graph reproduced the
   cold baseline exactly (edge 2138.6 / motion 2.73 identical; at production shape 2693.8 / 10.11
   twice). Same-seed execution controlled for pseudo-random initialization variance across
   identical graphs and driver states, isolating pipeline modifications from initialization noise
   (thermal decay and background OS variance remain unexcluded confounders).
5. **Stage-2 resolution falls for long clips.** An 8.04 s clip at 1080p stage-2 (51k stage-2
   tokens) thrashes weight streaming: 28.3 min wall. The same duration at 1280×704 stage-2 takes
   211.5 s (3.5 min) with a clean PCIe profile. Long-clip playbook: 5 s at 1080p stage-2, 6.7–8 s
   at 1280×704 plus post-upscale, 10.04 s with the temporal upscaler ×2 (slow-motion semantics,
   844 s, doc 01 §4), beyond that concatenation or segmentation.
6. **Quantize the decode, not the sampling decisions (H3).** This rule concerns the H3 int8
   ConvRot video VAE (the only int8 ConvRot VAE in the manifest). It saved 2.2 GiB of weights
   (4.85 → 2.62 GiB) and decoded 1.5× faster in vendor measurements; on this machine the swap was
   reported as aggregate metrics within 0.1% of fp16 at the same seed (edge 1351.3 vs 1350.5, the
   H3 v4 baseline; both values archived, neither is in a run log or config). The run record of that A/B (`h3_h3_vaeint8`, `7a7f6042`) lists the fp16 video
   VAE in its config, identical to the v4 baseline's config, so the record does not confirm that
   the int8 VAE was used; the parity claim is not verified. It was in any case aggregate parity,
   not per-frame parity.

## 6. Limits of this document

- The resolution ladder (512×288×73 through 1920×1088×121) is one run per rung; step times are the
  clean signal (1.04–37 s/step across the ladder), not the wall times, which include cold loads.
- No clock locking was used; the thermal confounder described in doc 03 ("Hardware confounder
  lessons", item 1) applies here too.
- The kernel gate is synthetic (m=1024, n=k=4096). It establishes kernel correctness and a
  reference throughput at one shape, not per-layer behavior inside the transformer; a layer-level
  check ran on the real checkpoint (with 9.7e-3 as the worst observed error among the sampled
  benchmark layers vs the fp32 dequantized-weight reference, cosine 0.999953).

## Fact-check corrections

How checked: **[code]** videolab tree at 3f6cabc; **[cfg]** `videolab/configs/*.proven.json`;
**[log]** H3 run logs on `D:`; **[calc]** recomputed; **[comfy]** ComfyUI node definitions
(`ComfyUI_LTX/nodes.py:349-350`).

1. L22–24 "which kernel runs" → the gate counts kernel calls but asserts only zero eager
   fallback and CUDA backend selection (`gate_w4a8.py:277-282, 197`). [code]
2. L37–42 "at or slightly exceeding the measured synthetic GEMM baseline (67.2)", "a kernel
   ceiling" → the 67.2 is the H3 gate's own 67.23; restated as two gate figures at one shape,
   a reference throughput, not a ceiling. [code]
3. L44–47 "0.96% … in the layer-level kernel reference test (`gate_w4a8.py`)" → the gate's
   fp32-reference check is synthetic (random W, `gate_w4a8.py:207-210, 289-293`); the
   real-checkpoint layer check is the separate 9.7e-3 / 0.999953. [code]
4. L58 "silently wrong by a small amount" → by about 11% (the 11.4% stated two sentences
   earlier). [code]
5. L60–66 "test with non-square shapes"; "At least one test per quantized path uses n != k
   and n != m" → the only gate shape is N = K = 4096, M = 1024 (`gate_w4a8.py:178`), and no
   other first-party test of a quantized path exists; the fix is the dim-0 scale
   (`:320-323`). [code]
6. L67–68 "The commit history here is" → "the lab history" (this repository's history starts
   with the gate already fixed). [code]
7. L69 "Tolerances were never widened" → "not widened after failures"; the cross-backend
   bound is loose on purpose (`gate_w4a8.py:298`). [code]
8. L73–80 crash mechanism (transformer already offloaded, stale offload state in the tiled
   decode) → conflicts with `evict.py:1-7` (access violation when the diffusion VAE decoder is
   built while the DiT is resident); both are now stated, neither re-verified. [code]
9. L82–83 "calls `unload_all_models()`" → with the shipped `keep_text_encoder: true`
   (`workflows/ltx25_w4a8_production.json:373`) the node calls `free_memory(…, keep_loaded=…)`
   instead (`evict.py:40-54`). [code]
10. L87–89 "(56.3 s) … (+20.4 s), 80.3 s of avoidable overhead per shot" → 56.3 + 20.4 =
    76.7 s; 80.3 s is B − D, a cold-vs-warm composite with the flag changed. [calc][cfg]
11. L100–105 "The output statistics are identical at the same seed (same edge/motion values) … so
    this is pure overhead removal" → contradicted by the table (edge spread 7%, motion 10%);
    `ltx25.proven.json:98` ("bit-identical output statistics") has the same problem. [cfg][calc]
12. L113–114 "64 px temporal chunk, 16 px temporal overlap" → frames. [comfy]
13. L122–123 "The deployed scheme is 72×72 …" → implemented, not deployed
    (`qwen21.proven.json:15, 18`). [cfg]
14. L125–131 "Boundary blending uses premultiplied alpha" (placed under the latent windows) →
    the latent windows use an alpha-free sinusoidal ΣW·f/ΣW blend; premultiplied compositing is
    the pixel-domain tiled decode. [code]
15. L134–136 "a naive RGB cross-fade biases toward ΣW(1−α)C" → restated as a cross-fade of
    premultiplied RGB without de-multiplying (what `videolab.py:548-550` models). [code]
16. L136–137 "The offline selftest asserts partition-of-unity closure (≤1e-7) and the
    premultiplied seam property with a dark-fringe negative control" → the closure checks are
    integer arithmetic; the premultiplied check is a two-sample scalar example (C = 0.8, α = 0.5,
    tolerance 1e-9) that does not call the node's compositing code; no 1e-7 tolerance exists in
    first-party code (`videolab.py:532-556`). [code]
17. L149–150 "stalled due to memory bandwidth limits (a silent stall with no diagnostic
    output)" → "stalled silently; bandwidth saturation suspected". [wording: no diagnostics
    exist to support a cause]
18. L152–153 "A watchdog polls progress; a stall kills the run and the server restarts" →
    lab practice; the shipped harness has only a total timeout (`harness.py:322-343`). [code]
19. L154 "Evict before decode, always" → LTX; H3 templates have no eviction node. [code]
20. L162–163 long-clip playbook → added the temporal ×2 path listed in doc 01 §4. [code]
21. L164–167 rule 6 int8 VAE parity → it is an H3 result (1350.5 is the H3 v4 baseline edge;
    the only int8 ConvRot VAE in the manifest is H3's). New finding: the run record
    `7a7f6042` (`h3_h3_vaeint8`, 2026-09-26) has the same config as the v4 baseline
    `fb4991a4`, including `video_vae: minimax_h3_video_vae_fp16.safetensors`, and none of the
    93 H3 run configs names the int8 VAE; the parity claim is therefore not verified. [log]
22. L173 "doc 03 §6" → doc 03 has no numbered sections; pointer changed to "Hardware
    confounder lessons", item 1. [code]
23. L176 "vs physical reference" → "vs the fp32 dequantized-weight reference". [wording]

Second review (an independent re-check against the same evidence):

24. Item 3 went too far: `gate_w4a8.py` (docstring and `:300-301`) attributes 0.96% to "the
    layer-level reference test", and the gate's measured output is archived, so it is not
    established that 0.96% is the synthetic check's result. §1 now states both. [code]
25. L73 "`VAEDecodeTiled` crashes or hangs" → "the decode crashes or hangs" (changed within
    item 8, not stated there; the upstream mechanism concerns building the diffusion decoder,
    not the tiled decode specifically). [code]
26. "Checked and correct" had listed the whole A/B/C/D table against `ltx25.proven.json:88-100`;
    the config holds only the walls and the B/D encode and stage-1 times. The other cells and
    the edge/motion values are archived, so item 11's 7% / 10% spread rests on archived values.
    [cfg]
27. L166 (rule 6) edge 1351.3 / 1350.5 → marked archived. [cfg][log]

Checked and correct as written: the gate mechanisms exist (`gate_w4a8.py:247-330`);
34.36 GFLOP / 0.496 ms = 69.27 and / 0.511 ms = 67.24 TFLOP/s; 69.26 / 4.96 = 13.96×;
46 × 4 × 256 × 1.725 GHz = 81.25 TFLOPS (×2 = 162.5 TOPS INT8; vendor 162.6), so 41.35% / 42.60%;
the `s_channel` aliasing at N == K; the A/B/C/D walls and the B/D encode and stage-1 times
(`ltx25.proven.json:88-100`);
26.2 s (−17.4%), 1.9 s (−2.6%), 80.3 s (−53%); mean 292.1 s; `VAEDecodeTiled(512, 64, 64, 16)`;
28.8 / 10.9 = 2.64×; T = 72, stride 56; the compositing formula
(`node_premul_vae_tiled.py:8-9`); references wired to stage 1 only; 8.04 s = 193 frames;
211.5 s = 3.5 min; 4.85 − 2.62 = 2.23 GiB; 1351.3 vs 1350.5 = 0.06%.

Archived, not re-verifiable from this repository: the gate's measured values (1.66e-3,
0.96%, 4.52e-3, 9.7e-3, the timings); 0.03 / 0.06 GiB at eviction; 19/19 and 30+ runs; the
~2 s guard cost; 7.66 / 7.59 GB; the determinism reruns; 28.3 min and the 1280×704 PCIe
profile; the vendor 1.5× decode figure; 0.0067 vs 0.001; the resolution-ladder step times.
