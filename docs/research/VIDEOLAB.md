[Kinoloom](../../README.md) › [Docs](../README.md) › Videolab

# videolab

> Copy of `videolab/README.md` at videolab commit 3f6cabc, fact-checked
> against the videolab tree, its configs and the H3 lab run logs on
> the studio box (`D:\H3_LAB\renders\*\run.json`, 93 runs, 2026-09-19..28; not
> redistributed), plus the safetensors header of the shipped H3 DiT checkpoint.
> Corrections are listed at the end. The `videolab/` folder in this repository
> is that snapshot, edited only for publication: planning references removed,
> the model sizes corrected, and a pointer to this copy at the top of its README
> and the end of each of its documents. Its other figures (including
> `configs/h3.proven.json`) still carry the original values. Commands below run
> from the `videolab/` directory.

> **In short:** videolab is where video diffusion transformers of about
> 20-22B (MiniMax H3's pruned checkpoint, LTX-Video 2.5) and Qwen-Image-2.1
> stills were benchmarked on one 8 GB RTX 3070 under Windows. It ships the ComfyUI workflow templates, node
> packs, configs and NVML harness behind the measurements, plus a ledger of five
> claims that were tested and disproved. Most results are one run at one seed (N=1), so
> treat them as measurements from one machine, not a leaderboard.

Reproducible benchmarks for running 20B–33B video diffusion transformers on a single
8 GB consumer GPU. The labs behind this repository ran MiniMax H3 (a pruned W4A8 DiT of
about 20B logical parameters, derived from the 33B base model, + 32B text encoder),
LTX-Video 2.5 (22B), and Qwen-Image-2.1 (7B DiT base; Qwen3-VL 8B text encoder) stills on
one NVIDIA RTX 3070 under Windows, with no oversized video-DiT checkpoint fully resident
in VRAM. Every configuration here is built around host-to-device weight streaming and
quantized GEMMs; the LTX configuration adds explicit pre-decode eviction and tiled
decode (the H3 templates decode with untiled `VAEDecode` and have no eviction node).
Every benchmark timing and telemetry trace was collected on this testbed; theoretical
rooflines, FLOP bounds, and vendor specifications are identified separately.

What this repository is for:

- **Reproduce the pipelines.** Portable ComfyUI workflow templates, first-party
  node packs, and the exact configurations that were adopted or rejected.
- **Reproduce the measurements.** The NVML harness (nominal 0.5 s sampling of
  VRAM, power, temperature, PCIe TX/RX, link generation; the real sample period
  is about 0.57 s, see the metric definitions) records the same aggregate fields
  as the original lab run logs.
- **Retraction ledger with error bounds and post-mortem analyses.** The ledger
  documents five claims that were tested, disproved, and corrected, including the
  confounders that remain.

What it is not: a leaderboard. Most adoption decisions in the source archive
were single-run, single-seed measurements (`N=1`); the tables say so, and
[VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md)
explains what that means.

> ### Project Mission & Hardware Scope
>
> **Why this repository exists:**  
> Running 20B–33B Diffusion Transformers on an 8 GB consumer GPU means working inside strict memory budgets — WDDM paging thrash, driver allocation timeouts, kernel broadcast quirks, and bandwidth cliffs. Finding configurations that complete without crashing typically demands extended trial-and-error, especially on constrained hardware. This project is published as an open experimental reference and field guide to share what worked, what failed, and the exact architectural workarounds discovered along the way, helping developers with constrained hardware get stable, higher-tier models running without starting from zero.
>
> **Two-Tier Evidentiary Classification:**  
> - **Tier A (Shipped & Locally Reproducible):** ComfyUI workflows (`videolab/workflows/`), kernel parity gates (`videolab/bench/gate_w4a8.py`), model download manifests (`videolab/configs/models.manifest.json`), and offline self-tests (`videolab.py selftest`) included directly in the videolab repository.
> - **Tier B (Archived Laboratory Telemetry):** The 12-day experimental campaign (Sept 18–29, 2026) produced 200+ telemetry-logged runs, negative ablation post-mortems, and thermal traces cited directly from the archived lab records (indexed in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md)). Of these, the 93 H3 run logs (2026-09-19..28) are still on the studio box and were used for this fact-check; the LTX and Qwen run logs and the rest of the corpus are archived, not re-verifiable from this repository.
>
> **Reproducibility Notice:**  
> All benchmark timings, PCIe transfer rates, and VRAM footprints documented here reflect empirical measurements on a specific single-node testbed (Intel i9-10980XE, 64 GB DDR4 quad-channel, PCIe 3.0 x16, RTX 3070 8 GB @ 220 W TDP on Windows 11). They are presented as a relative engineering compass, not a universal cross-system throughput guarantee. Step latencies and streaming bottlenecks will vary across systems depending on PCIe generation, lane bifurcation, motherboard DMI bandwidth, system memory speed, background OS compositor load, and driver revisions.

## Hardware and runtime

| Component | Spec |
|---|---|
| GPU | NVIDIA RTX 3070 8 GB GDDR6, SM 8.6, 46 SMs, 220 W |
| CPU | Intel i9-10980XE (18C/36T) |
| RAM | 64 GB DDR4 quad-channel |
| Bus | PCIe 3.0 x16 (Gen 2–3 observed under load per the archive; Gen 1 also appears in some H3 run logs) |
| Storage | Weights staged on SATA SSD per the archive (historical benchmark runs initially staged from cold HDD storage at ~127 MB/s). On the studio box today the lab model folders (`D:\H3_LAB\models`, `D:\LTX25_LAB\models`) are on a SATA HDD (WDC WD5000AAKX); the run logs do not record where the weights sat for each run. |
| OS | Windows 11, NVIDIA driver 616.64 (CUDA 13.4 Driver API / PyTorch CUDA 13.0 Runtime) |
| Runtime | ComfyUI v0.36.0 / comfy-kitchen 0.2.34 at initial baselines for H3 and LTX; H3 later re-verified on ComfyUI v0.37.4 / comfy-kitchen 0.2.35; the Qwen template requires v0.37.4 (v0.36 cannot build the Qwen-Image 2.1 VAE); Python 3.12.14 (lab; the offline checks also pass on 3.13); PyTorch 2.13.0+cu130 |

SM 8.6 has no native FP8 tensor path, so quantized runs use W4A8 (4-bit weights /
8-bit activations) and INT8 through the comfy-kitchen fused kernels, which
execute on native INT8 tensor cores. Model weights per stack: H3 pruned W4A8 DiT
11.68 GiB + Qwen3-VL-32B W4A8 text encoder 14.62 GiB; LTX 2.5 W4A8 transformer
11.66 GiB + Gemma4-12B W4A8 encoder 9.88 GiB.

## Quickstart

```
# 1. Bootstrap a venv and the dependencies (see install.ps1 for options)
powershell -ExecutionPolicy Bypass -File .\install.ps1

# 2. Check the host: NVML, PyTorch CUDA, and the fused W4A8 parity gate
.venv\Scripts\python videolab.py doctor

# 3. Fetch checksum-verified weights for a profile (28.2 GB for smoke)
.venv\Scripts\python videolab.py download --profile smoke

# 4. Offline sanity checks; add --comfy/--checkpoint for a live micro-render
.venv\Scripts\python videolab.py selftest

# 4b. Point ComfyUI at the downloaded weights (section below)

# 5. Run a proven workflow against a running ComfyUI and record telemetry
# (image values are filenames ComfyUI resolves from its input folder)
.venv\Scripts\python videolab.py bench --model ltx25 --set PROMPT="a weathered wooden pier at blue hour, low camera push-in" --set REF_IMAGE_1=character.png --set REF_IMAGE_2=prop.png --set REF_BACKGROUND=environment.png --set SHOT_ID=demo
```

`videolab.py bench` refuses to run with missing placeholders and rejects `--set`
keys that match no placeholder (unless `--allow-extra-params` is given), so a typo
cannot silently produce a run with the wrong configuration. Both video stacks
require reference images — LTX: `REF_IMAGE_1` (subject), `REF_IMAGE_2` (secondary
subject) and `REF_BACKGROUND` (stage-1 MSR guides); H3: `CHARACTER_IMAGE`,
`POWER_IMAGE`, `ENVIRONMENT_IMAGE`. The three H3 keys are also checked for
non-empty values before submission (`videolab.py:54`); the LTX `REF_*` keys must be
present but are not checked for emptiness.
Repeat the command with different `SEED` values to build a distribution; one run
is one sample and the docs treat it that way.

### 4b. Point ComfyUI to downloaded models

When running `videolab.py download`, models are stored by default in `./models`,
which ComfyUI does not search. Pick one of two wirings before running `bench`:

1. **Download into ComfyUI's own models folder** (simplest):

   ```powershell
   # Windows (PowerShell)
   $env:VIDEOLAB_MODELS_DIR = "path/to/ComfyUI/models"
   .venv\Scripts\python videolab.py download --profile smoke
   ```

   ```bash
   # Linux / macOS / WSL2
   export VIDEOLAB_MODELS_DIR="/path/to/ComfyUI/models"
   .venv/bin/python videolab.py download --profile smoke
   ```

2. **Keep the repository tree and register it with ComfyUI.** Add a section to
   `<ComfyUI>/extra_model_paths.yaml` (create the file if it does not exist),
   then restart ComfyUI:

   ```yaml
   # <ComfyUI_Root>/extra_model_paths.yaml
   videolab:
     base_path: path/to/videolab/models
     checkpoints: checkpoints
     diffusion_models: diffusion_models
     text_encoders: text_encoders
     vae: vae
     loras: loras
     latent_upscale_models: latent_upscale_models
     upscale_models: upscale_models
     prompt_generator: prompt_generator
   ```

   `latent_upscale_models` is required by both video graphs (the H3 3D upscaler
   and the LTX spatial upscaler, smoke profile included); `prompt_generator` is
   only needed for the optional Qwen prompt-enhancer node, which the shipped Qwen
   graph does not use; `checkpoints` holds the optional LTX audio VAE (`full`
   profile) and may be left in place even when empty.

`install.ps1 -ComfyUI <path> -WriteModelPaths` and
`install.sh --comfy-ui <path> --write-model-paths` write option 2 for you when
`extra_model_paths.yaml` does not already exist (an existing file is never
modified; the snippet is printed instead). The installers' snippet has no
`checkpoints:` line (`videolab/install.ps1:125-135`, `videolab/install.sh:170-178`);
add it by hand if you need the audio VAE. `bench` prints a warning listing any
workflow models it cannot find under `MODELS_DIR` — expected when ComfyUI uses a
different models root.

## Results summary

The configurations below represent the verified architectural milestones distilled from over 200 telemetry-logged runs, ablation sweeps, and failure tests across three model programs and five benchmark campaigns (full run index cataloged in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md)). The wider corpus is ~1,800 files; each table row isolates one architectural rung. The run counts and file counts are archived, not re-verifiable from this repository; the H3 rows were checked against the 93 H3 run logs still on the studio box.

### Video pipelines (H3: 124 f @ 24 fps = 5.17 s; LTX: 121 f @ 24 fps = 5.04 s)

| Config | N (runs × seeds) | Wall | Peak device VRAM | Torch alloc peak | PCIe RX per clip (recorded) | Notes |
|---|---|---|---|---|---|---|
| H3 ULT-A — 6-step turbo LoRA 544p → 1080p refine | 1 × 1 | 1153.3 s | 7.60 GiB | not logged | 700.7 GiB | edge-variance proxy 2819.8 |
| H3 C3 — 4-step turbo LoRA 544p → 1080p refine | 1 × 1 | 876.4 s | 7.59 GiB | not logged | 821.9 GiB | edge proxy 2363.5 |
| H3 Flagship — 25-step base → 1080p refine | 1 × 1 | 1484.1 s | 7.62 GiB | not logged | 640.6 GiB | edge proxy 1472.5 |
| H3 composite anchor — ULT-A recipe, prior-render frame as anchor | 1 × 1 | 1154.0 s | 7.85 GiB | not logged | 685.1 GiB | edge proxy +8.3% vs ULT-A; wardrobe held 124 f (visual) |
| H3 H1 — 6-step turbo LoRA 544p single pass | 1 × 1 | 426.0 s | 7.51 GiB | not logged | 145.7 GiB | cold (the run log shows no cached nodes) |
| H3 H2 — 8-step turbo LoRA 544p single pass | 1 × 1 | 211.3 s | 7.83 GiB | not logged | 132.4 GiB | warm (14 nodes cached from H1); not a controlled A/B vs H1 |
| H3 v4 baseline — 25-step 544p | 1 × 1 | 739.6 s | 7.50 GiB | not logged | 336.0 GiB | edge proxy 1350.5 (archived; not in the run log) |
| H3 T2 — 243 f (10.13 s) 25-step 544p single pass | 1 × 1 | 1271.4 s | 7.70 GiB | not logged | 458.3 GiB | no 1080p refine; see [VIDEOLAB_01](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) §4 |
| LTX 2.5 — 1920×1088, top rung of the resolution ladder | 1 × 1 | 269.0 s | 7.48 GiB | not logged | ~132 GiB sampled | no references; step counts and VAE of this rung are not recorded in `ltx25.proven.json` (archived) |
| LTX 2.5 production — S6a + DFR + refs stage-1 + kept text encoder | 3 × 1 | 281.7–299.0 s (mean 292.1) | 7.64–7.75 GiB | not logged | not logged | three consecutive new prompts, one session |

All H3 rows above ran at seed `519438276610342` (confirmed in each run log). The
four main H3 rows map to shipped templates: ULT-A →
`videolab/workflows/h3_ulta_8gb_production.json`, C3 →
`videolab/workflows/h3_c3_8gb_speed.json`, Flagship →
`videolab/workflows/h3_flagship_8gb.json`, T2 →
`videolab/workflows/h3_t2_243f_8gb.json`; select one with `bench --workflow <file>`
and tune `STEPS`/`FRAMES` with `--set`. The composite-anchor and H1/H2 rows are
configuration variants of those graphs, documented with their measurements in
`videolab/configs/h3.proven.json`. The v4 row is documented in
[VIDEOLAB_01](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) §3 and its run log
(`fb4991a4…`: 739.57 s, 7.498 GiB, 336.0 GiB RX); it has no entry in
`h3.proven.json`. The only seed sweep in the archive is the LTX sigma-ladder study
(3 scenes × 3 seeds), documented in [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md)
and doc 01; the other LTX rows use one fixed seed per prompt.

The recorded PCIe totals understate the transfer by 10–13% (see metric 3 below).

### Still-image pipeline (Qwen-Image-2.1, 2048×2048 RGBA)

| Config | N | Wall | Peak device VRAM | Torch alloc peak | Plan target | Verdict |
|---|---|---|---|---|---|---|
| Turbo draft (Q4_K_M, 6 steps) | 3 runs (1 cold + 2 warm), distinct seeds | 23.4–23.5 s warm (cold 68.8 s) | ~5.9–6.1 GB | text encoder 0 MB CUDA (parameter residency, not an allocator peak) | 10 ± 2 s | FAIL recorded |
| Two-pass 2K (step count, CFG and quant not recorded for this run) | 2 × 1 | 262.3 s warm (cold 481.9 s) | 6.22 GB warm / 6.54 GB cold | text encoder 0 MB CUDA | 88.6 ± 4 s | FAIL recorded |
| FAST mode (Pruna DMD 8 steps) | 5 images × 1 | 39.3–78.4 s/img | not separated per image | — | — | adopted at archive time; adapter not redistributed (baseline research) |

> **Qwen VRAM methodology:** Qwen VRAM figures (5.9–6.5 GB) come from
> `torch.cuda.mem_get_info()`, which reports device-level free memory (not a
> per-process allocation). The README's earlier statement of a ~7.49 GiB NVML
> device peak for these runs has no source in the tree and is not repeated here.

### Metric definitions and test constraints

1. **Peak device VRAM** = maximum of the NVML `nvmlDeviceGetMemoryInfo().used`
   samples (nominal 0.5 s interval) across the whole run. Device-wide, includes
   the desktop compositor. It is not `torch.cuda.max_memory_allocated()`.
2. **Torch allocator peaks** were only instrumented in the synthetic kernel gate
   (the README gives 0.498 GiB for the m=1024, n=k=4096 W4A8 GEMM; that value is
   archived, not in the tree). The Qwen text-encoder figure (0 MB CUDA-resident)
   is a parameter-residency check measured from parameter bytes, not an
   allocator peak.
3. **PCIe RX** = sum of `nvmlDeviceGetPcieThroughput` samples × the nominal 0.5 s
   interval. The sampler waits 0.5 s *after* each sample
   (`videolab/bench/harness.py:182-188`), so the real period in the H3 logs is
   0.556–0.574 s, while the total multiplies by 0.5 s (`harness.py:218`). Recorded
   totals and the average rates derived from them are therefore 10–13% low (ULT-A:
   700.7 GiB recorded, 804.3 GiB when integrated over the logged timestamps).
   Single samples in the tabled runs also read up to 21.2 GB/s (21.7 GB/s across
   all 93 H3 logs), above the PCIe 3.0 x16 theoretical rate, so individual
   readings are estimates. Rectangular integration of a
   bursty counter; approximate, not exact.
4. **N (runs × seeds)** counts completed runs. No video row has repeats across
   multiple seeds; the LTX sigma study is the exception. Community contributions
   exploring multi-seed statistical variance across diverse prompt suites are
   welcome.
5. **Edge-variance proxy** = Laplacian edge energy of frames downscaled to 480 px
   and sampled at 4 fps. Texture energy, not perceived quality; ordering does not
   transfer across resolutions. Read it as a diagnostic, not a score.
6. **Timing state**: runs were not clock-locked. The GPU sat near its 220 W power
   limit during sampling (peaks 218.6–221.0 W at 75–77 °C in the tabled H3 runs). SM clocks were not
   logged during benchmark runs, so boost/thermal decay cannot be separated from
   software changes. Together with N=1 this is the largest open confounder; see
   [VIDEOLAB_01](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) §6 and
   [VIDEOLAB_03](VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md), "Hardware
   confounder lessons", item 1.

## Pipeline architecture (host/device staging)

```
 [weights on disk, HDD or SSD cache]
              | safetensors load (cold ~60-180 s)
              v
 +--------------------------- HOST: 64 GB DDR4 ------------------------------+
 |  Text encoder: Qwen3-VL-32B W4A8 (14.62 GiB) or Gemma4-12B W4A8 (9.88 GiB)|
 |  -> CLIPLoader device "default": staged in host RAM and managed by        |
 |     DynamicVRAM (only the Qwen-Image 8B encoder runs on CPU, 0 MB CUDA)   |
 |  ComfyUI scheduler, prompt conditioning, DynamicVRAM weight cache         |
 +-------------------------------+-------------------------------------------+
                                 | per-layer weight streaming, PCIe 3.0 x16
                                 | (Gen 1-3 logged; 0.3-0.9 GiB/s average RX as recorded,
                                 |  10-13% low, see metric 3)
                                 v
 +------------------------ GPU: RTX 3070 8 GB, 220 W ------------------------+
 |  Streamed DiT backbone (W4A8 ConvRot, comfy-kitchen fused INT8 kernels)   |
 |  11.68 GiB (H3) / 11.66 GiB (LTX) checkpoint never fully resident         |
 |    |- phase 1: coarse latent sampling 960x544                            |
 |    |- latent upscale x2                                                  |
 |    +- phase 2 at 1920x1088: H3 tiled r45 refine (MMH3SplitUpscale);      |
 |       LTX untiled 3-step stage 2                                          |
 +-------------------------------+-------------------------------------------+
                                 | LTX: explicit eviction before decode
                                 | (LTX25LabEvictModels, keep_text_encoder=true:
                                 |  selective free_memory + soft_empty_cache + empty_cache)
                                 | H3: no eviction node; MMH3SplitUpscale unloads its
                                 |  model after the refine
                                 v
              pre-decode VRAM eviction (LTX; Windows crash guard, ComfyUI #15606)
                                 |
                                 v
        LTX: conv bf16 VAE, VAEDecodeTiled (512 px tiles, 64-frame chunks)
        H3:  fp16 video VAE, untiled VAEDecode (int8 ConvRot VAE was tested)
                                 |
                                 v
        libx264 CRF 16 assembly; native AAC 48 kHz where the model has audio
```

## Engineering notes

- **NVML telemetry harness.** Every run samples device VRAM, utilization, power,
  temperature, PCIe TX/RX and link generation at a nominal 0.5 s
  (`videolab/bench/harness.py`, ported from the lab runner). The shipped harness
  saves only the aggregate `monitor` statistics (`harness.py:356-359`), whose field
  names match the lab logs; it does not save the per-sample timeline that the lab
  logs carry (e.g. 2,012 samples for the ULT-A render).
- **Fused W4A8 kernel validation.** The fused kernel is gated before any quality
  claim: zero eager fallback calls (instrumented), 1.66e-3 relative L2 against a
  same-activation float reference, 0.96% relative output error against an fp32
  dequantized-weight reference (cosine 0.999954; `gate_w4a8.py` attributes it to "the
  layer-level reference test"), and a 9.7e-3 worst observed error (cosine 0.999953)
  among sampled benchmark layers. The gate itself also runs an fp32-reference check on
  random synthetic weights (m=1024, n=k=4096); whether 0.96% is that check's result or
  a separate test is not established (the measured gate outputs are archived). Two gate
  builds measured 67.23 TFLOP/s (H3) and 69.26 TFLOP/s (LTX) at that one shape, 41.35%–42.60% of
  theoretical dense INT8 hardware peak (162.6 TOPS on GA104 SM 8.6). `videolab.py doctor`
  runs the gate; the code lives in `videolab/bench/gate_w4a8.py`.
- **Windows VAE decode crash (ComfyUI #15606).** Decode after sampling
  crashed the process on Windows (`videolab/nodes/ltx25_lab_nodes/evict.py:1-7`
  gives the upstream mechanism: an access violation when the diffusion VAE decoder
  is built while the DiT is resident). An explicit eviction node between sampler
  and decode fixed it on the LTX path: 19/19 decoded videos clean, 30+ crash-free
  runs (archived).
- **H3 phase-1 compute bounds.** From the latent geometry (f16t4 + 1×2×2 patch merge),
  960×544/124f is S = 15,810 tokens. The linear layers of the shipped checkpoint's 50
  transformer blocks hold 19.27B logical parameters (safetensors header), so the linear
  term is ≈ 2 × 19.27e9 × 15,810 ≈ 0.609e15 FLOP/step. Measured phase-1 latency at
  960×544 with kitchen attention is 15.5–16.2 s/step (E1 step ladder, no LoRA) and
  16.9–18.8 s/step (25-step production runs; 16.9–17.9 without the realism LoRA run),
  an effective linear-term rate of 32–39 TFLOP/s, about 48–58% of the 67.2 TFLOP/s
  synthetic GEMM gate figure. S counts video tokens only (the packed sequence also
  carries text, audio and reference rows), so these rates are lower bounds. Attention
  is excluded and is large (switching only the attention backend at 832×480 moved the
  step from 15.45–16.0 s to 11.5–11.65 s). Phase 1 is consistent with a
  compute-dominated step (GEMM plus attention), not a streaming-bound one; it is not
  shown to run at the GEMM gate's rate. Derivation in
  [VIDEOLAB_01](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) §2.
- **The 243-frame refine is bandwidth-bound, and it was measured, not inferred.** A
  243-frame (124,440 tokens) 1080p refine ran its first 5-step tile group in 1:02:33
  (750.7 s/step average per the run log's progress bar; the archived report gives
  individual steps of 532–1087 s) with 5–10 GB/s sustained PCIe traffic (archived),
  against a full-sequence linear-term floor of ~71 s/step. De-prioritized with
  empirical roofline bounds. The 124-frame refine is not in this regime: its tiles
  ran at 7.0–30.2 s/step.
- **Negative results are kept.** PDD-8 acceleration (−22% edge proxy), query-gated
  attention (edge −45%, unusable), the tested no-pixel-conditioning approach failed
  to preserve structural coherence on imported latents (LTX-as-upscaler), 768p turbo
  LoRA dispatch collapse (582 s/it).

### What was tested and explored across the campaigns

- **Quantization and kernels:** eager PyTorch fallbacks vs comfy-kitchen fused W4A8/INT8 ConvRot kernels; INT8 vs W4A8 transformer A/B; attention-backend sweep (native SDPA, kitchen INT8, Sage, Sol); layer-level quantization error checked against fp32 dequantized-weight references.
- **Sampling and schedules:** fixed distilled sigma ladders (8-step, S6a, S6b, S5), PDD 8-step, few-step distillation (larryvrh v4 and LightX2V LoRAs on H3; the Viggle turbo DiT checkpoint and the Pruna DMD adapter on Qwen), shift-12/3 Euler schedules, step counts from 4 to 32, and adaptive step/CFG modulation evaluated and rejected for distilled CFG-1.0 backbones.
- **Memory and staging policies:** dynamic VRAM offloading, text encoder kept resident vs full unload (A/B/C/D on 73 frames: keep_text_encoder saves 26.2 s cold, 1.9 s warm; the cold→warm composite difference is 80.3 s), explicit pre-decode eviction barriers, tiled vs untiled decode, host-RAM pinning and DMA staging, swap-pressure monitoring.
- **Multi-stage and long-clip architectures:** single-pass 544p/768p, two-phase latent-upscale + tiled 1080p refinement, temporal ×2 upscaling, multi-beat concatenation, last-frame chained continuation, and LTX-as-upscaler with and without pixel-space conditioning.
- **Identity and reference binding:** reference-image trials v1–v4, up to 9 references per prompt, frame-0 anchors, composite anchors from prior renders, MSR stage-1-only conditioning, `<Picture N>` role assignment.
- **Quality measurement and encoding:** Laplacian and temporal sharpness profiling, frame-difference motion proxies, seam metrics, dHash distinctness, CRF 16 vs default h264 A/B, and prompt-level photographic/motion experiments.
- **Planning and decision-engine archaeology:** a full end-to-end speedup plan audited to NO-GO, a locally fine-tuned routing model evaluated against a 5k-label gate, and an external decision API client rewritten against its published contract.

Most of these trials ended as evidence-backed rejections rather than adopted
defaults. The rejections and their measurements are in
[VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md).

## Repository layout

```
videolab/
├── README.md                  this file (copy: docs/research/VIDEOLAB.md)
├── LICENSE                    MIT (code/docs; weights are not distributed)
├── .gitignore                 weights, models/, runs/, outputs
├── pyproject.toml             package metadata and extras
├── requirements.txt           pinned environment contract
├── install.ps1                bootstrap (Windows PowerShell): venv, dependencies, optional node deploy
├── install.sh                 bootstrap (Linux / WSL2 bash): venv, dependencies, optional node deploy
├── videolab.py                CLI: doctor | download | selftest | bench
├── configs/
│   ├── models.manifest.json   sizes, sources, digests, gating flags
│   ├── profiles.json          smoke | standard | full | h3-byow
│   ├── h3.proven.json         ULT-A, C3, baselines, rejected configs
│   ├── ltx25.proven.json      S6a + DFR + CQ + kept text encoder
│   └── qwen21.proven.json     quality and FAST modes, tiling algebra
├── docs/
│   ├── 01_H3_STREAMING_AND_8GB_LIMITS.md
│   ├── 02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md
│   ├── 03_RETRACTIONS_FAILURES_AND_LESSONS.md
│   └── ARCHIVE_INDEX.md       provenance map for every measurement
├── workflows/                 portable ComfyUI API-format templates
├── nodes/                     first-party packs (three) + third_party/ (vendored, licenses,
│                              third_party/README.md: vendoring record)
├── bench/                     NVML harness, W4A8 gate, edge/motion metrics (+ __init__.py)
│                              (video profiling needs ffmpeg on PATH)
└── runs/                      run records, created at runtime (gitignored)
```

## Weights, licensing, and profiles

No model weights are distributed or committed (`models/` is gitignored; the
documented stack is 118.3 GB, of which 65.5 GB is downloadable and 52.83 GB is
MiniMax H3 bring-your-own-weights — 49,328,354,772 bytes of recorded primary
weights plus 3,501,658,176 bytes of auxiliary assets, both measured on disk).
`videolab/configs/models.manifest.json` records what
each asset is, where it came from, its size, and — where a digest was written
during the lab downloads — its SHA-256. Assets without a recorded digest are
marked; they were size-verified only.

- `smoke` (≈28.2 GB / 26.27 GiB): LTX 2.5 W4A8 core — transformer, Gemma4 encoder, conv VAE,
  spatial upscaler, and required production LoRAs (MSR v1, CQ enhancer). Enables immediate
  execution of `workflows/ltx25_w4a8_production.json` out-of-the-box.
- `standard` (≈40.7 GB / 37.88 GiB): the LTX core *without* the MSR v1 and CQ
  enhancer LoRAs, plus the Qwen-Image-2.1 quality stack (Q5_K_M). It is not a
  superset of `smoke` and cannot run the LTX template by itself
  (`videolab/configs/profiles.json:25-40`). The manifest also tags
  `qwen21_dit_base_q4` as `standard`, but `profiles.json` leaves it out.
- `full` (≈65.5 GB / 60.97 GiB download): everything downloadable, including
  LTX support assets and Qwen extras. H3 entries are listed but never downloaded
  (their 52.83 GB / 52,830,012,948 bytes is reported separately as
  `byow_bytes_exact_sum`; neither figure is counted in the download size.)
- `h3-byow`: **no downloads.** MiniMax H3 weights fall under the MiniMax H3
  Community License, which carries territorial restrictions. This profile
  validates user-supplied copies only and requires `--acknowledge-license`.

Gated repositories need `HF_TOKEN` or `huggingface-cli login`; every downloaded
file is checksum-verified against the manifest when a digest exists.

All recorded sources were re-verified against the Hugging Face API on
2026-09-30: every archived lab digest that could be checked (11 of 11) matched
the served files byte-for-byte. Two corrections resulted: the conv-VAE entry
previously carried the diffusion-VAE digest (fixed, and the served conv file is
now pinned), and the CQ enhancer, Q5_K_M quant, temporal upscaler, audio VAE and
MSR V2 are pinned to the digests of the files actually served, labeled
`sha256_source` in the manifest because the lab never archived a digest for
them. `download --profile <name> --check-sources` (`--profile` is required;
`--profile full` covers every downloadable asset) repeats the check without
downloading anything; `--only <asset_id>` targets a single asset when one fails.

## Known limits of this repository

- The measured adoption decisions are mostly `N=1` single-seed runs. The harness
  supports repeat runs; the archived results do not contain them.
- No clock locking was used for the archived timings; GPU power ran at the 220 W
  board limit during sampling and SM clocks were not logged.
- The raw lab archive (run logs, review media, the full evidence corpus) is not
  redistributed here; [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md) maps the names.
- Third-party node packs are vendored under `videolab/nodes/third_party/` (LBH-123-AI
  upscaler, MIT; LiconStudio MSR v1.2.2, Apache-2.0) with their license files
  and upstream links. The H3 and LTX templates target ComfyUI v0.36.0 core
  (initial baselines; H3 was later re-verified on v0.37.4); the Qwen template
  requires v0.37.4. Two machine-specific metadata values in the upstream LBH
  example workflows were blanked (documented in `videolab/nodes/third_party/README.md`).
- The Qwen FAST-mode adapter (`p_qwen_image_2.1_8step_v0.1.safetensors`, Pruna
  DMD) is not redistributed and no FAST workflow template ships; that row in the
  results table is baseline research, not a reproducible pipeline. The shipped
  Qwen workflow covers the locked quality mode.
- Executing Qwen-Image-2.1 workflows with GGUF DiT checkpoints requires the standard
  [city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) extension installed in
  your ComfyUI `custom_nodes/` directory.

## Documentation

- [VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) — token/FLOP derivation, PCIe offload dynamics, the 243-frame refinement post-mortem.
- [VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) — kernel correctness gating, the N==K==4096 broadcast bug, the Windows decoder crash fix, tiling mechanics.
- [VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md) — the five retractions, hardware confounders, process changes, follow-up list.
- [VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md) — provenance map of the ~1,800-file source archive and its 200+ telemetry-logged runs.

## Fact-check corrections

How checked: **[code]** = read in the videolab tree at 3f6cabc; **[log]** = the H3
run logs `D:\H3_LAB\renders\<key>\run.json` (read-only scripts; progress-bar
times are elapsed ÷ steps); **[hdr]** = safetensors header of
`minimax_h3_ref2va_pruned-w4a8_convrot_pruned.safetensors` (no weights read);
**[calc]** = recomputed arithmetic.

1. L3–6 "22B–33B", "MiniMax H3 (33B dense DiT…)", "(20B–33B)" → the shipped H3
   checkpoint is a pruned DiT of ~20.1B logical parameters (19.27B in the linear
   layers of 50 blocks, ~0.8B in bf16) derived from the 33B base. [hdr]
2. L7–8 "Every configuration … explicit eviction … and tiled decode" → LTX only;
   the H3 templates use untiled `VAEDecode` and no eviction node
   (`workflows/h3_*.json`; only `ltx25_w4a8_production.json` has
   `LTX25LabEvictModels` + `VAEDecodeTiled`). [code]
3. L15–17, 179, 185–186, 211, 235 "0.5 s sampling" → nominal; real period
   0.556–0.574 s; totals integrate with 0.5 s and are 10–13% low (ULT-A
   `bd27b7ba`: 2,012 samples over 1,152.6 s, period 0.5731 s; RX 700.7 recorded vs
   804.3 GiB timestamp-integrated, TX 101.1 vs 116.1). `harness.py:182-188, 218`.
   Single samples reach 20.7 (ULT-A) and 21.2 GB/s (Flagship `922de11a`). [code][log]
4. L45 "Gen 2–3 observed under load" → Gen 1 also appears (composite, C3,
   Flagship, chain link 2 logs list `pcie_gen_observed` [1, 2, 3]). [log]
5. L46 storage → added: the lab model folders are on a SATA HDD today (`D:`, WDC
   WD5000AAKX); per-run placement is not logged. [checked on the box]
6. L48, L350–351 "H3 and Qwen execution later verified on v0.37.4" → the Qwen
   template requires v0.37.4 (`configs/qwen21.proven.json:5`,
   `nodes/third_party/README.md:33-36`, ARCHIVE_INDEX "v0.36 cannot build the 2.1
   VAE"). [code]
7. L80–83 "all are validated as non-empty" → only the three H3 image keys
   (`videolab.py:54` `NONEMPTY_PARAMS`). [code]
8. L124–126 required folders → `latent_upscale_models` is needed by both video
   graphs; `prompt_generator` is not used by the shipped Qwen graph
   (`workflows/qwen_2k_tiled_refine.json` has no prompt-generator node);
   `checkpoints` holds the LTX audio VAE (`models.manifest.json:122`, `full`). [code]
9. L128–131 installers "write option 2" → their snippet omits `checkpoints:`
   (`install.ps1:125-135`, `install.sh:170-178`). [code]
10. L149, 158–160 v4 row "documented … in configs/h3.proven.json" → it has no entry
    there; values confirmed in run log `fb4991a4` (739.57 s, 7.498 GiB, 336.0 GiB,
    seed 519438276610342). [code][log]
11. L151 "8+3 steps, conv VAE" → neither label is recorded for this rung in
    `ltx25.proven.json:36-49`; marked archived. (The agent's inference that the
    ladder used the diffusion VAE was not adopted: it rests on a wall-time
    comparison only.) [code]
12. L147–148 H1/H2 "documented as cold/warm" → confirmed: H1 `d674f0ca` has no
    cached nodes, H2 `e5c812a2` reused 14. [log]
13. L168 Turbo draft N "3 × 1" → 3 runs (1 cold + 2 warm), distinct seeds
    (`qwen21.proven.json:31-37`). [code]
14. L169 "(40 steps, CFG 3.5; …)" → step count and CFG are not recorded for this run
    either (`qwen21.proven.json:41`). [code]
15. L172–175 "process-allocated headroom … mem_get_info"; "~7.49 GiB" →
    `mem_get_info` is device-level free memory; 7.49 GiB has no source in the tree
    and was removed. [code]
16. L182–184 allocator peaks → 0.498 GiB has no source in the tree (archived); the
    Qwen 0 MB figure is a parameter-residency check (`qwen21.proven.json:74-77`). [code]
17. L194–197 "This is the largest open confounder; see doc 03 §6" → doc 03 has no
    §6; pointer changed to doc 01 §6 / doc 03 hardware lesson 1, and doc 03 itself
    calls N=1 the largest single weakness. [code]
18. Diagram L206–207 "runs on CPU/pinned RAM path, 0 MB CUDA-resident by
    construction" → H3 and LTX templates load the encoder with `CLIPLoader`
    `device: "default"`; only the Qwen graph uses `"cpu"`. [code]
19. Diagram L218 "tiled refine … (H3 r45 / LTX stage-2)" → LTX stage 2 is an
    untiled `SamplerCustomAdvanced` (`ltx25_w4a8_production.json`). [code]
20. Diagram L220–221 "unload_all_models() + empty_cache()" → with the shipped
    `keep_text_encoder: true` the node calls `free_memory(…, keep_loaded=…)`
    (`nodes/ltx25_lab_nodes/evict.py:40-55`); H3 has no eviction node. [code]
21. Diagram L226 "tiled (conv bf16 or int8 ConvRot; 512 px tiles)" → LTX conv bf16
    tiled 512 px; H3 fp16 VAE untiled. All 93 H3 run configs record the fp16 VAE.
    [code][log]
22. L234–237 "per-run timeline … same field names" → the shipped harness saves only
    aggregate `monitor` stats (`harness.py:356-359`). [code]
23. L240–242 "0.96% … in the layer-level reference test" → the fp32-reference check
    in the gate uses random synthetic weights (`gate_w4a8.py:207-210`); the
    real-checkpoint layer check is the separate 9.7e-3 / cosine 0.999953. [code]
24. L242–243 "~100% of the measured synthetic GEMM ceiling" → circular: the "67.2
    ceiling" is the H3 gate's own 67.23 TFLOP/s; restated as two gate figures at one
    shape. [code]
25. L248–253 H3 phase-1 bounds: 1.043e15 FLOP/step (2 × 33e9 × S), "15.5–16.6
    s/step", 62.8–67.3 TFLOP/s, "compute-bound" → 0.609e15 (19.27B params);
    15.5–16.6 s/step is the Phase C 832×480 default-attention range (runs
    C1–C6: 832×480×124, 20 steps, no attention override, 15.45–16.0 s/step);
    960×544 kitchen measured 15.5–16.2 (E1 ladder) and 16.9–18.8 s/step (25-step
    production: value544 v1–v4, t1, vaeint8, Flagship); 32–39 TFLOP/s, ~48–58% of
    the gate figure. With the old basis, the 832×480 kitchen runs (11.5–11.65
    s/step) would imply 68.5–69.4 TFLOP/s on the linear term alone, above the
    gate figure, which is one sign the 33e9 basis cannot be right. [hdr][log][calc]
26. L251–252 832×480 "corresponding to 48.1–51.5 TFLOP/s" (a hypothetical) →
    replaced by the measured 832×480 runs (see VIDEOLAB_01 §2). [log]
27. L254–256 refine → added the log's 750.7 s/step average for the first tile group
    (`a3ad86a2` progress bars: 5 steps in 1:02:33), the corrected ~71 s/step floor,
    and that the 124-frame refine tiles ran at 7.0–30.2 s/step (ULT-A, C3,
    Flagship bars). [log][calc]
28. L265 "distillation adapters (… Viggle and Pruna DMD on Qwen)" → Viggle is a turbo
    distilled DiT checkpoint (`models.manifest.json:364`). [code]
29. L266 "keep_text_encoder saves … 80.3 s composite" → 80.3 s is the cold→warm
    composite (B→D), not the flag's saving. [calc on `ltx25.proven.json:88-99`]
30. L278–302 layout → added `.gitignore`, `bench/__init__.py`,
    `nodes/third_party/README.md`, `runs/`. [code]
31. L318 `standard` = "smoke plus the Qwen stack" → it lacks `ltx25_lora_msr_v1` and
    `ltx25_lora_cq_enhancer` (`profiles.json:25-40`); manifest/profile mismatch for
    `qwen21_dit_base_q4` (`models.manifest.json:343`). [code]
32. L337 "`download --check-sources` repeats the whole check" → `--profile` is
    required (`videolab.py:916`) and the check covers that profile. [code]

Second review (an independent re-check against the same evidence):

33. L240–242 (as rewritten in item 23) the 0.96% figure was reassigned to the gate's synthetic
    GEMM check; `gate_w4a8.py:10` and `:300-301` attribute it to "the layer-level reference
    test", and the measured gate output is archived. Restated with both possibilities.
    [code]
34. L141, L176 recorded PCIe totals: the column is now "(recorded)" and a sentence after the
    table says they are 10–13% low (part of item 3, not listed there). Single samples: 21.2
    GB/s in the tabled runs, 21.7 GB/s across all 93 H3 logs. [log]
35. L194–195 "74–77 °C" → 75–77 °C in the tabled H3 runs (74 is the config note at
    `h3.proven.json:183`). [log]
36. Diagram L211 "Gen 2-3 observed" (the first pass changed it to "under load") → Gen 1–3
    logged (item 4 had fixed only the table). [log]
37. L149 v4 "edge proxy 1350.5" → marked archived; no run log or config holds it. [log][cfg]
38. L250–251 "16.9–18.8 s/step": the 18.8 end is `h3_t1_realism`, which ran with a LoRA
    (16.9–17.9 without it); the rates are lower bounds because S counts video tokens only
    (`comfy/ldm/minimax/model.py:5-7`). [log][code]
39. Changes the first pass made without a numbered entry:
    - L79: "(unless `--allow-extra-params` is given)" added (`videolab.py:809-812`).
    - L139: header "LTX: 121 f = 5.04 s" → "121 f @ 24 fps".
    - L194 and L344–345: "during sampling" added to the power-limit statements.
    - L244–246: "Tiled decode after sampling crashed the WDDM driver path" → the access
      violation that `evict.py:1-7` documents, limited to the LTX path. The WDDM-driver-path
      wording was dropped because nothing in the tree supports it.
    - L253: "Derivation and the corrected earlier count in doc 01" → "Derivation in
      VIDEOLAB_01 §2" (the count's correction is Retraction 5 in doc 03).
    - L28–36: the first pass had stripped the Markdown line breaks after the three bold
      labels; restored. [code]

Checked and correct as written (selection): every H3 table wall, VRAM peak and RX
value against its run log; the seed of all eight H3 rows; 2,012 ULT-A samples;
peak power 218.6–221.0 W (v4 218.62, T2 220.95); profile byte sums;
CLI flags; template-to-row mapping; durations (124/24 = 5.17 s, 121/24 = 5.04 s,
243/24 = 10.13 s).

Archived, not re-verifiable from this repository: driver 616.64 / CUDA 13.4;
Python 3.12.14 and the 3.13 claim; SATA SSD staging and ~127 MB/s HDD; cold load
~60–180 s; "200+ runs" and "~1,800 files"; 1.66e-3 rel. L2; 0.498 GiB; 19/19
decodes and 30+ crash-free runs; 532–1087 s/step and the 5–10 GB/s refine trace;
the LTX and Qwen rows (they match `ltx25.proven.json` / `qwen21.proven.json`, but
no LTX or Qwen run logs are on this machine); ComfyUI #15606 details.

Not adopted from the fact-check agents: the per-tile compute floors they derived
with the 33e9 basis; the inference that the LTX top rung used the diffusion VAE;
the "43.31 GB" size of a corrected `standard` profile (not recomputed here).
