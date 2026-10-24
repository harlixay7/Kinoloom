# videolab

**Snapshot:** this folder is my video-model lab as of 2026-10-01. Some figures in it turned out wrong; the corrected version, with each correction listed, is [docs/research/VIDEOLAB.md](../docs/research/VIDEOLAB.md) in Kinoloom. It holds reproducible benchmarks for running 20B–33B video diffusion transformers on a single
8 GB consumer GPU. In the labs behind this folder I ran MiniMax H3 (a pruned W4A8 DiT of about 20B logical parameters, derived from the 33B base model, +
32B text encoder), LTX-Video 2.5 (22B), and Qwen-Image-2.1 (7B DiT base; Qwen3-VL 8B text encoder) stills on one NVIDIA
RTX 3070 under Windows, with no oversized video-DiT checkpoint fully resident in VRAM. Every
configuration here is built around host-to-device weight streaming, explicit
eviction, quantized GEMMs, and tiled decode; every benchmark timing and telemetry trace was collected on
this testbed; theoretical rooflines, FLOP bounds, and vendor specifications are identified separately.

What this folder is for:

- **Reproduce the pipelines.** Portable ComfyUI workflow templates, first-party
  node packs, and the exact configurations that were adopted or rejected.
- **Reproduce the measurements.** The NVML harness (0.5 s sampling of VRAM,
  power, temperature, PCIe TX/RX, link generation) records the same fields as
  the original lab run logs.
- **Retraction ledger with error bounds and post-mortem analyses.** The ledger
  documents five claims that were tested, disproved, and corrected, including the
  confounders that remain.

What it is not: a leaderboard. Most adoption decisions in the source archive
were single-run, single-seed measurements (`N=1`); the tables say so, and
`docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md` explains what that means.

> ### Purpose and hardware scope
> 
> **Why this folder exists:**  
> Running 20B–33B diffusion transformers on an 8 GB consumer GPU means working inside tight memory budgets: WDDM paging thrash, driver allocation timeouts, a broadcast bug in the kernel test harness, and bandwidth cliffs. Finding configurations that finished without crashing took me a lot of trial and error. I publish these notes as a record of what worked and what failed on one machine, with the workarounds I used, so that someone with similar hardware does not have to start from zero. They are measurements from one box, mostly single runs, not a guarantee for other systems.
> 
> **Two-Tier Evidentiary Classification:**  
> - **Tier A (Shipped & Locally Reproducible):** ComfyUI workflows (`workflows/`), kernel parity gates (`bench/gate_w4a8.py`), model download manifests (`configs/models.manifest.json`), and offline self-tests (`videolab.py selftest`) included directly in this folder.
> - **Tier B (Archived Laboratory Telemetry):** The 12-day experimental campaign (Sept 18–29, 2026) produced 200+ telemetry-logged runs, negative ablation post-mortems, and thermal traces cited directly from my archived lab records (indexed in `docs/ARCHIVE_INDEX.md`).
> 
> **Reproducibility Notice:**  
> All benchmark timings, PCIe transfer rates, and VRAM footprints documented here reflect empirical measurements on a specific single-node testbed (Intel i9-10980XE, 64 GB DDR4 quad-channel, PCIe 3.0 x16, RTX 3070 8 GB @ 220 W TDP on Windows 11). They are presented as a relative engineering compass, not a universal cross-system throughput guarantee. Step latencies and streaming bottlenecks will vary across systems depending on PCIe generation, lane bifurcation, motherboard DMI bandwidth, system memory speed, background OS compositor load, and driver revisions.

## Hardware and runtime

| Component | Spec |
|---|---|
| GPU | NVIDIA RTX 3070 8 GB GDDR6, SM 8.6, 46 SMs, 220 W |
| CPU | Intel i9-10980XE (18C/36T) |
| RAM | 64 GB DDR4 quad-channel |
| Bus | PCIe 3.0 x16 (Gen 2–3 observed under load) |
| Storage | Weights staged on SATA SSD (historical benchmark runs initially staged from cold HDD storage at ~127 MB/s) |
| OS | Windows 11, NVIDIA driver 616.64 (CUDA 13.4 Driver API / PyTorch CUDA 13.0 Runtime) |
| Runtime | ComfyUI v0.36.0 / comfy-kitchen 0.2.34 at initial baselines; H3 and Qwen execution later verified on ComfyUI v0.37.4 / comfy-kitchen 0.2.35; Python 3.12.14 (lab; the offline checks also pass on 3.13); PyTorch 2.13.0+cu130 |

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
keys that match no placeholder, so a typo cannot silently produce a run with the
wrong configuration. Both video stacks require reference images — LTX:
`REF_IMAGE_1` (subject), `REF_IMAGE_2` (secondary subject) and `REF_BACKGROUND`
(stage-1 MSR guides); H3: `CHARACTER_IMAGE`, `POWER_IMAGE`,
`ENVIRONMENT_IMAGE` — and all are validated as non-empty before submission.
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

2. **Keep the videolab models tree and register it with ComfyUI.** Add a section to
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

   `latent_upscale_models` and `prompt_generator` are required by the shipped H3
   and Qwen graphs; `checkpoints` is listed for completeness and may be left in
   place even when empty.

`install.ps1 -ComfyUI <path> -WriteModelPaths` and
`install.sh --comfy-ui <path> --write-model-paths` write option 2 for you when
`extra_model_paths.yaml` does not already exist (an existing file is never
modified; the snippet is printed instead). `bench` prints a warning listing any
workflow models it cannot find under `MODELS_DIR` — expected when ComfyUI uses a
different models root.

## Results summary

The configurations below are the main ones I measured, picked from over 200 telemetry-logged runs, ablation sweeps, and failure tests across three model programs and five benchmark campaigns (full run index cataloged in [docs/ARCHIVE_INDEX.md](docs/ARCHIVE_INDEX.md)). The wider corpus is ~1,800 files; each table row isolates one architectural rung.

### Video pipelines (H3: 124 f @ 24 fps = 5.17 s; LTX: 121 f = 5.04 s)

| Config | N (runs × seeds) | Wall | Peak device VRAM | Torch alloc peak | PCIe RX per clip | Notes |
|---|---|---|---|---|---|---|
| H3 ULT-A — 6-step turbo LoRA 544p → 1080p refine | 1 × 1 | 1153.3 s | 7.60 GiB | not logged | 700.7 GiB | edge-variance proxy 2819.8 |
| H3 C3 — 4-step turbo LoRA 544p → 1080p refine | 1 × 1 | 876.4 s | 7.59 GiB | not logged | 821.9 GiB | edge proxy 2363.5 |
| H3 Flagship — 25-step base → 1080p refine | 1 × 1 | 1484.1 s | 7.62 GiB | not logged | 640.6 GiB | edge proxy 1472.5 |
| H3 composite anchor — ULT-A recipe, prior-render frame as anchor | 1 × 1 | 1154.0 s | 7.85 GiB | not logged | 685.1 GiB | edge proxy +8.3% vs ULT-A; wardrobe held 124 f (visual) |
| H3 H1 — 6-step turbo LoRA 544p single pass | 1 × 1 | 426.0 s | 7.51 GiB | not logged | 145.7 GiB | documented as cold |
| H3 H2 — 8-step turbo LoRA 544p single pass | 1 × 1 | 211.3 s | 7.83 GiB | not logged | 132.4 GiB | documented as warm; not a controlled A/B vs H1 |
| H3 v4 baseline — 25-step 544p | 1 × 1 | 739.6 s | 7.50 GiB | not logged | 336.0 GiB | edge proxy 1350.5 |
| H3 T2 — 243 f (10.13 s) 25-step 544p single pass | 1 × 1 | 1271.4 s | 7.70 GiB | not logged | 458.3 GiB | no 1080p refine; see docs/01 §4 |
| LTX 2.5 — 1920×1088, 8+3 steps, conv VAE | 1 × 1 | 269.0 s | 7.48 GiB | not logged | ~132 GiB sampled | top rung, no references |
| LTX 2.5 production — S6a + DFR + refs stage-1 + kept text encoder | 3 × 1 | 281.7–299.0 s (mean 292.1) | 7.64–7.75 GiB | not logged | not logged | three consecutive new prompts, one session |

All H3 rows above ran at seed `519438276610342`. The four main H3 rows map to
shipped templates: ULT-A → `workflows/h3_ulta_8gb_production.json`, C3 →
`workflows/h3_c3_8gb_speed.json`, Flagship → `workflows/h3_flagship_8gb.json`,
T2 → `workflows/h3_t2_243f_8gb.json`; select one with `bench --workflow <file>`
and tune `STEPS`/`FRAMES` with `--set`. The composite-anchor, H1/H2 and v4 rows
are configuration variants of those graphs, documented with their measurements
in `configs/h3.proven.json`. The only seed sweep in the archive is the LTX
sigma-ladder study (3 scenes × 3 seeds), documented in `docs/ARCHIVE_INDEX.md`
and doc 01; the other LTX rows use one fixed seed per prompt.

### Still-image pipeline (Qwen-Image-2.1, 2048×2048 RGBA)

| Config | N | Wall | Peak device VRAM | Torch alloc peak | Target | Verdict |
|---|---|---|---|---|---|---|
| Turbo draft (Q4_K_M, 6 steps) | 3 × 1 | 23.4–23.5 s warm (cold 68.8 s) | ~5.9–6.1 GB | text encoder 0 MB CUDA | 10 ± 2 s | FAIL recorded |
| Two-pass 2K (40 steps, CFG 3.5; quant not recorded for this run) | 2 × 1 | 262.3 s warm (cold 481.9 s) | 6.22 GB warm / 6.54 GB cold | text encoder 0 MB CUDA | 88.6 ± 4 s | FAIL recorded |
| FAST mode (Pruna DMD 8 steps) | 5 images × 1 | 39.3–78.4 s/img | not separated per image | — | — | adopted at archive time; adapter not redistributed (baseline research) |

> **Qwen VRAM methodology:** Qwen VRAM figures (5.9–6.5 GB) reflect
> process-allocated headroom sampled via `torch.cuda.mem_get_info()`. Device-wide
> NVML telemetry peaks at ~7.49 GiB under Windows due to Desktop Window Manager
> (DWM) compositor overhead.

### Metric definitions and test constraints

1. **Peak device VRAM** = maximum of 0.5 s NVML `nvmlDeviceGetMemoryInfo().used`
   samples across the whole run. Device-wide, includes the desktop compositor.
   It is not `torch.cuda.max_memory_allocated()`.
2. **Torch allocator peaks** were only instrumented in the synthetic kernel gates
   (0.498 GiB for the m=1024, n=k=4096 W4A8 GEMM) and the Qwen text-encoder
   residency check (0 MB CUDA-resident, measured from parameter bytes).
3. **PCIe RX** = sum of 0.5 s `nvmlDeviceGetPcieThroughput` samples × interval.
   Rectangular integration of a bursty counter; approximate, not exact.
4. **N (runs × seeds)** counts completed runs. No video row has repeats across
   multiple seeds; the LTX sigma study is the exception. Community contributions
   exploring multi-seed statistical variance across diverse prompt suites are
   welcome.
5. **Edge-variance proxy** = Laplacian edge energy of frames downscaled to 480 px
   and sampled at 4 fps. Texture energy, not perceived quality; ordering does not
   transfer across resolutions. Read it as a diagnostic, not a score.
6. **Timing state**: runs were not clock-locked. The GPU sat near its 220 W power
   limit (peaks 218.6–221.0 W) at 74–77 °C. SM clocks were not logged during
   benchmark runs, so boost/thermal decay cannot be separated from software
   changes. This is the largest open confounder; see doc 01 §6 and doc 03, "Hardware confounder lessons", item 1.

## Pipeline architecture (host/device staging)

```
 [weights on disk, HDD or SSD cache]
              | safetensors load (cold ~60-180 s)
              v
 +--------------------------- HOST: 64 GB DDR4 ------------------------------+
 |  Text encoder: Qwen3-VL-32B W4A8 (14.62 GiB) or Gemma4-12B W4A8 (9.88 GiB)|
 |  -> runs on CPU/pinned RAM path, 0 MB CUDA-resident by construction       |
 |  ComfyUI scheduler, prompt conditioning, DynamicVRAM weight cache         |
 +-------------------------------+-------------------------------------------+
                                 | per-layer weight streaming, PCIe 3.0 x16
                                 | (Gen 2-3 observed; 0.3-0.9 GiB/s average RX across runs)
                                 v
 +------------------------ GPU: RTX 3070 8 GB, 220 W ------------------------+
 |  Streamed DiT backbone (W4A8 ConvRot, comfy-kitchen fused INT8 kernels)   |
 |  11.68 GiB (H3) / 11.66 GiB (LTX) checkpoint never fully resident         |
 |    |- phase 1: coarse latent sampling 960x544                            |
 |    |- latent upscale x2                                                  |
 |    +- phase 2: tiled refine at 1920x1088 (H3 r45 / LTX stage-2)          |
 +-------------------------------+-------------------------------------------+
                                 | explicit eviction before decode:
                                 | unload_all_models() + empty_cache()
                                 v
              pre-decode VRAM eviction (Windows crash guard, ComfyUI #15606)
                                 |
                                 v
        3D causal VAE decode, tiled (conv bf16 or int8 ConvRot; 512 px tiles)
                                 |
                                 v
        libx264 CRF 16 assembly; native AAC 48 kHz where the model has audio
```

## Engineering notes

- **NVML telemetry harness.** Every run samples device VRAM, utilization, power,
  temperature, PCIe TX/RX and link generation at 0.5 s into a per-run timeline
  (`bench/harness.py`, ported from the lab runner). Run records keep the same
  field names as the archived logs, e.g. 2,012 samples for the ULT-A render.
- **Fused W4A8 kernel validation.** The fused kernel is gated before any quality
  claim: zero eager fallback calls (instrumented), 1.66e-3 relative L2 against a
  same-activation float reference, 0.96% relative output error in the layer-level
  reference test against an fp32 dequantized-weight reference (cosine 0.999954; 9.7e-3 worst
  observed error across sampled benchmark layers), 67.23–69.26 TFLOP/s synthetic (~100% of
  the measured synthetic GEMM ceiling and 41.35%–42.60% of theoretical dense INT8 hardware
  peak: 162.6 TOPS on GA104 SM 8.6). `videolab.py doctor` runs the gate; the code lives in `bench/gate_w4a8.py`.
- **Windows VAE decode crash (ComfyUI #15606).** Tiled decode after sampling
  crashed the WDDM driver path. An explicit eviction node between sampler and
  decode fixed it: 19/19 decoded videos clean, 30+ crash-free runs.
- **H3 phase-1 compute bounds.** From the latent geometry (f16t4 + 1×2×2 patch merge),
  960×544/124f is S = 15,810 tokens ≈ 1.043e15 FLOP/step; measured phase-1 latency (15.5–16.6 s/step)
  implies an effective linear-term throughput of 62.8–67.3 TFLOP/s, operating within the vicinity
  of the 67.2 TFLOP/s synthetic GEMM ceiling (for 832×480/124f: S = 12,090 tokens ≈ 0.798e15 FLOP/step,
  corresponding to 48.1–51.5 TFLOP/s). Phase-1 is compute-bound, not streaming-bound.
  Derivation and the corrected earlier count in doc 01.
- **The refine is bandwidth-bound, and it was measured, not inferred.** A
  243-frame (124,440 tokens) 1080p refine step took 532–1087 s with 5–10 GB/s sustained PCIe
  traffic; the first tile group alone ran 62.5 minutes. De-prioritized with empirical roofline bounds.
- **Negative results are kept.** PDD-8 acceleration (−22% edge proxy), query-gated
  attention (edge −45%, unusable), the tested no-pixel-conditioning approach failed
  to preserve structural coherence on imported latents (LTX-as-upscaler), 768p turbo
  LoRA dispatch collapse (582 s/it).

### What was tested and explored across the campaigns

- **Quantization and kernels:** eager PyTorch fallbacks vs comfy-kitchen fused W4A8/INT8 ConvRot kernels; INT8 vs W4A8 transformer A/B; attention-backend sweep (native SDPA, kitchen INT8, Sage, Sol); layer-level quantization error checked against fp32 dequantized-weight references.
- **Sampling and schedules:** fixed distilled sigma ladders (8-step, S6a, S6b, S5), PDD 8-step, few-step distillation adapters (larryvrh v4 and LightX2V on H3; Viggle and Pruna DMD on Qwen), shift-12/3 Euler schedules, step counts from 4 to 32, and adaptive step/CFG modulation evaluated and rejected for distilled CFG-1.0 backbones.
- **Memory and staging policies:** dynamic VRAM offloading, text encoder kept resident vs full unload (A/B/C/D; keep_text_encoder saves 26.2 s cold, 1.9 s warm, 80.3 s composite), explicit pre-decode eviction barriers, tiled vs untiled decode, host-RAM pinning and DMA staging, swap-pressure monitoring.
- **Multi-stage and long-clip architectures:** single-pass 544p/768p, two-phase latent-upscale + tiled 1080p refinement, temporal ×2 upscaling, multi-beat concatenation, last-frame chained continuation, and LTX-as-upscaler with and without pixel-space conditioning.
- **Identity and reference binding:** reference-image trials v1–v4, up to 9 references per prompt, frame-0 anchors, composite anchors from prior renders, MSR stage-1-only conditioning, `<Picture N>` role assignment.
- **Quality measurement and encoding:** Laplacian and temporal sharpness profiling, frame-difference motion proxies, seam metrics, dHash distinctness, CRF 16 vs default h264 A/B, and prompt-level photographic/motion experiments.
- **Planning and decision-engine archaeology:** a full end-to-end speedup plan audited to NO-GO, a locally fine-tuned routing model evaluated against a 5k-label gate, and an external decision API client rewritten against its published contract.

Most of these trials ended as evidence-backed rejections rather than adopted
defaults. The rejections and their measurements are in
[docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md](docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md).

## Folder layout

```
videolab/
├── README.md                  this file
├── LICENSE                    MIT (code/docs; weights are not distributed)
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
├── nodes/                     first-party packs (three) + third_party/ (vendored, licenses)
└── bench/                     NVML harness, W4A8 gate, edge/motion metrics
                               (video profiling needs ffmpeg on PATH)
```

## Weights, licensing, and profiles

No model weights are distributed or committed (`models/` is gitignored; the
documented stack is 118.3 GB, of which 65.5 GB is downloadable and 52.83 GB is
MiniMax H3 bring-your-own-weights — 49,328,354,772 bytes of recorded primary
weights plus 3,501,658,176 bytes of auxiliary assets, both measured on disk).
`configs/models.manifest.json` records what
each asset is, where it came from, its size, and — where a digest was written
during the lab downloads — its SHA-256. Assets without a recorded digest are
marked; they were size-verified only.

- `smoke` (≈28.2 GB / 26.27 GiB): LTX 2.5 W4A8 core — transformer, Gemma4 encoder, conv VAE,
  spatial upscaler, and required production LoRAs (MSR v1, CQ enhancer). Enables immediate
  execution of `workflows/ltx25_w4a8_production.json` out-of-the-box.
- `standard` (≈40.7 GB / 37.88 GiB): smoke plus the Qwen-Image-2.1 quality stack.
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
them. `download --check-sources` repeats the whole check without downloading
anything; `--only <asset_id>` targets a single asset when one fails.

## Known limits of this folder

- The measured adoption decisions are mostly `N=1` single-seed runs. The harness
  supports repeat runs; the archived results do not contain them.
- No clock locking was used for the archived timings; GPU power ran at the 220 W
  board limit and SM clocks were not logged.
- The raw lab archive (run logs, review media, the full evidence corpus) is not
  redistributed here; `docs/ARCHIVE_INDEX.md` maps the names.
- Third-party node packs are vendored under `nodes/third_party/` (LBH-123-AI
  upscaler, MIT; LiconStudio MSR v1.2.2, Apache-2.0) with their license files
  and upstream links. The templates target ComfyUI v0.36.0 core (initial
  baselines); H3 and Qwen execution was later verified on v0.37.4. Two
  machine-specific metadata values in the upstream LBH example workflows were
  blanked (documented in the vendoring README).
- The Qwen FAST-mode adapter (`p_qwen_image_2.1_8step_v0.1.safetensors`, Pruna
  DMD) is not redistributed and no FAST workflow template ships; that row in the
  results table is baseline research, not a reproducible pipeline. The shipped
  Qwen workflow covers the locked quality mode.
- Executing Qwen-Image-2.1 workflows with GGUF DiT checkpoints requires the standard
  [city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) extension installed in
  your ComfyUI `custom_nodes/` directory.

## Documentation

- [docs/01_H3_STREAMING_AND_8GB_LIMITS.md](docs/01_H3_STREAMING_AND_8GB_LIMITS.md) — token/FLOP derivation, PCIe offload dynamics, the 243-frame refinement post-mortem.
- [docs/02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](docs/02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) — kernel correctness gating, the N==K==4096 broadcast bug, the Windows decoder crash fix, tiling mechanics.
- [docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md](docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md) — the five retractions, hardware confounders, process changes, follow-up list.
- [docs/ARCHIVE_INDEX.md](docs/ARCHIVE_INDEX.md) — provenance map of the ~1,800-file source archive and its 200+ telemetry-logged runs.

## Corrections

This is a snapshot of my lab notes as of 2026-10-01. Some figures in it turned out wrong; the corrected version, with each correction listed, is [docs/research/VIDEOLAB.md](../docs/research/VIDEOLAB.md) in Kinoloom. Quote that one, not this file.
