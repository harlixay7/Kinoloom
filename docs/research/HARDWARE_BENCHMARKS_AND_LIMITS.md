[Kinoloom](../../README.md) › [Docs](../README.md) › Hardware benchmarks

# Hardware benchmarks and limits

> **In short:** Kinoloom was built and tested on one PC with an 8 GB RTX 3070 under Windows 11. This doc collects what was measured there: how much work the video models do per step, how fast the card really runs them, what happens when model weights have to cross the PCIe bus, and where the card's 8 GB of memory runs out. It also lists the guards in the studio code that follow from those limits. Read it if you want to run the studio on similar hardware, or to understand why it is built the way it is.

This doc collects the derivations, telemetry, kernel gate results and execution limits measured on a consumer 8 GB GPU under Windows 11 WDDM (the Windows display driver model), and the guards in the studio code that follow from them.

> **Sources:** Sections 1–4 and 6 come from the videolab archive, as fact-checked in the copies in this folder, [VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md), [VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) and [VIDEOLAB.md](VIDEOLAB.md). The `videolab/` folder is a snapshot of the lab: apart from its model sizes, it still carries the uncorrected figures. The H3 numbers were checked against the 93 H3 lab run logs still on the studio PC; the LTX and Qwen figures, and anything marked archived, could not be re-verified. Section 5.4 and §6.4 are the studio box's own measurements from the seamless-chain work ([SEAMLESS_CHAINS_01_LTX.md](SEAMLESS_CHAINS_01_LTX.md) §7–8). The raw run logs are not redistributed. Unless stated otherwise, every configuration is a single run at a single seed (N=1).

**On this page**

- [1. The test machine](#1-the-test-machine)
- [2. Model geometry and FLOP counts](#2-model-geometry-and-flop-counts)
- [3. The fused W4A8 kernel gate](#3-the-fused-w4a8-kernel-gate-videolabbenchgate_w4a8py)
- [4. Two regimes: compute-bound and bandwidth-bound](#4-two-regimes-compute-bound-and-bandwidth-bound)
- [5. The Windows WDDM paging cliff and Linux OOM](#5-the-windows-wddm-paging-cliff-and-linux-oom)
- [6. ComfyUI bug #15606 and the `LTX25EvictModels` workaround](#6-comfyui-bug-15606-and-the-ltx25evictmodels-workaround)

---

## 1. The test machine

The archived measurements were captured on this desktop workstation:

- **GPU:** NVIDIA GeForce RTX 3070 8 GB GDDR6 (GA104, SM 8.6 Ampere, 5,888 CUDA cores, 46 SMs, 220 W board power limit).
- **CPU:** Intel Core i9-10980XE (Cascade Lake-X, 18 cores / 36 threads, base 3.0 GHz; Intel specifies Turbo Boost 2.0 up to 4.6 GHz and Turbo Boost Max 3.0 up to 4.8 GHz).
- **System memory:** 64 GB DDR4, quad-channel.
- **Bus:** PCIe 3.0 x16 (Gen 2–3 under load per the archive; some H3 run logs also record Gen 1).
- **Storage:** The videolab notes say the archived benchmarks staged weights from a SATA SSD (earlier runs from a cold HDD at ~127 MB/s). This is archived and can't be re-verified. On the studio PC now, the lab model folders (`D:\H3_LAB\models`, `D:\LTX25_LAB\models`) are on a SATA hard disk (WDC WD5000AAKX), and the repository with its `models/` is on a SATA SSD (Samsung 850 EVO). The run logs do not record where the weights were read from in each run.
- **Operating system:** Windows 11 Pro (WDDM).
- **Display driver:** NVIDIA 616.64 (`videolab/README.md`: CUDA 13.4 driver API, PyTorch CUDA 13.0 runtime in the lab). The studio's own runtimes need R560+ (cu126) and R580+ (cu130, `ComfyUI_LTX/venv` and `ComfyUI_Qwen21/venv`, which gets the cu126 build on a card or driver that runs only that one), per `install/INSTALL-MAIN.bat`.

> **Tested hardware:**
> This is the only configuration tested. Other cards (for example RTX 3060 12 GB, RTX 40-series, professional RTX GPUs) are untested. If you run a different card, your step times, thermal limits and bus transfer rates will differ. Reports from other machines are welcome.

---

## 2. Model geometry and FLOP counts

The studio runs large diffusion transformers on an 8 GB card: MiniMax H3 (its base model is described as 33B, and its shipped pruned W4A8 checkpoint holds about 20.1B logical parameters) and LTX-Video 2.5 at 22B. With models this size, the token count (the length of the sequence the transformer processes) is the first thing to get right.

### 2.1 MiniMax H3 latent geometry
MiniMax H3 uses an `f16t4d24` 3D causal VAE (the part of the model that compresses video into latents):
- **Spatial compression:** $16\times$ downsampling.
- **Temporal compression:** $4\times$ downsampling.
- **Latent channels:** 24 channels.
- **Patch merge:** $1 \times 2 \times 2$ patch merge at the DiT (diffusion transformer) input, giving an effective **$32\times$ spatial compression** from pixels to DiT tokens.

#### Worked example: the flagship resolution (960×544, 124 frames)
1. **Spatial latent grid:**
   $$\text{Grid} = \left(\frac{960}{16}\right) \times \left(\frac{544}{16}\right) = 60 \times 34 = 2,040\text{ latents per frame}$$
2. **Spatial tokens per frame after the patch merge:**
   $$\text{Tokens}_{\text{frame}} = \left(\frac{60}{2}\right) \times \left(\frac{34}{2}\right) = 30 \times 17 = 510\text{ tokens}$$
3. **Temporal frame-latents:**
   $$\text{Frames}_{\text{temporal}} = 1 + \left\lfloor\frac{124 - 1}{4}\right\rfloor = 31\text{ frame-latents}$$
4. **Total sequence length ($S$):**
   $$S = 31 \times 510 = 15,810\text{ tokens}$$
5. **Linear-term operations per step:**
   The 50 transformer blocks of the shipped checkpoint hold 19.27B logical parameters in their linear layers (hidden size 5,376; counted from the safetensors header). The linear-projection FLOPs per denoising step are:
   $$\text{FLOP}_{\text{step}} = 2 \times (19.27 \times 10^9) \times 15,810 \approx 0.609 \times 10^{15}\text{ FLOP/step}$$
   An earlier version of this doc used $2 \times 33 \times 10^9$ (the base model's size), which overstates the linear term 1.71× ($1.043 \times 10^{15}$).
   $S$ counts the video tokens only. The model packs one sequence of text, conditioning or reference rows, audio and video (`ComfyUI_LTX/comfy/ldm/minimax/model.py`). Audio adds about 414 rows at 5.17 s, each ref2va reference image adds a 510-row grid at 960×544, and the text rows were not counted. So the FLOP figure and the rates below are lower bounds.

### 2.2 Measured throughput and the kernel gate figure
Phase-1 step times at 960×544, 124 frames, with comfy-kitchen attention, from the H3 run logs (elapsed time ÷ steps):

| Runs | s/step | Linear-term rate | Share of the 67.2 TFLOP/s gate figure |
|---|---|---|---|
| E1 step ladder (16/20/25/32 steps, no LoRA) | 15.5–16.2 | 37.7–39.3 TFLOP/s | 56–58% |
| 25-step production runs (ref2va; the 18.8 end ran a realism LoRA) | 16.9–18.8 | 32.5–36.1 TFLOP/s | 48–54% |

The rates count only the 15,810 video tokens (see above), so they are lower bounds. The ref2va production runs also carry reference-image rows, which fits their slower steps.

The estimate counts only the linear term, and attention is not small here. At 832×480, changing only the attention backend moved the step from 15.45–16.0 s to 11.5–11.65 s. A rough count of the attention matmuls at 960×544 (~0.36e15 FLOP/step, an estimate) is of the same order as the linear term. The measured step time is consistent with a compute-dominated step (GEMMs, the large matrix multiplications, plus attention), not with one dominated by weight streaming. It does not show the GEMMs running at the gate's rate. (The earlier 15.5–16.6 s/step figure was the 832×480 Phase C range with default attention, not 960×544.)

---

## 3. The fused W4A8 kernel gate (`videolab/bench/gate_w4a8.py`)

W4A8 means 4-bit weights and 8-bit activations. If the fused kernel silently fell back to eager PyTorch dequantization, the pipeline would still produce video frames, only much slower, and every downstream performance number would be invalid.

`videolab/bench/gate_w4a8.py` checks for this. You run it by hand (`python bench/gate_w4a8.py --out runs/gate_w4a8.json` inside `videolab/`) or through `videolab.py doctor` and `videolab.py selftest`. The studio launcher does not run it. Archived results (records `phase1_w4a8_gate` for LTX, `phase0_env_gate` for the H3 build):

| Check | Measured result | What it shows |
|---|---|---|
| **Fused Kernel Executed** | `w4a8_codebook_linear_chunked`, backend `cuda` | The comfy-kitchen CUDA backend was selected. |
| **Silent Fallback Calls** | **0** (instrumented counter) | No eager-dequantization fallback was invoked. |
| **Numeric L2 Parity (Float Ref)** | $1.66 \times 10^{-3}$ relative L2 | Against a same-activation float reference. |
| **Numeric L2 Parity (FP32 Dequant)** | **0.96%** relative output error, cosine $0.999954$ | Against fp32 dequantized weights. `gate_w4a8.py` attributes it to "the layer-level reference test"; the gate also runs its own check on random synthetic weights, and which of the two produced 0.96% is not established. The worst error among sampled benchmark layers was $9.7 \times 10^{-3}$ (cosine 0.999953). Neither is a video-quality measurement. |
| **Throughput ($M=1024, N=K=4096$)** | LTX: 0.496 ms → **69.26 TFLOP/s** vs 4.96 TFLOP/s eager (**13.96×**); H3: 0.511 ms → **67.23 TFLOP/s** (13.34×) | Synthetic square GEMM with resident weights: a reference throughput at one shape (about 42% of the card's 162.6 TOPS dense INT8 peak), not a ceiling and not a pipeline number. |

### 3.1 Lesson: test with non-square matrices ($N \ne K, N \ne M$)
While the gate was being built, an intermediate same-activation check reported an 11.4% relative error, and the kernel looked broken.

**Root cause:** The channel scale tensor `s_channel` has shape `[N]` (one value per output channel), but the test harness applied it along the `K` axis. With $N = K = 4096$ the wrong axis still broadcasts without a shape error, so the result was silently wrong. The error was in the harness, not in the CUDA kernel. With the corrected harness, the kernel matches at $1.66 \times 10^{-3}$.

**Lesson:** Square test shapes hide axis bugs, so at least one test per quantized path should use $N \ne K$ and $N \ne M$ ([VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) §2). `gate_w4a8.py` itself still uses the square shape $N = K = 4096$, $M = 1024$.

---

## 4. Two regimes: compute-bound and bandwidth-bound

The lab runner sampled `nvmlDeviceGetPcieThroughput` (the driver's PCIe traffic counter) at a nominal 0.5 s. It waits 0.5 s after each sample, so the logged period is 0.556–0.574 s. The recorded TX/RX totals multiply each sample by 0.5 s, so they are 10–13% low (ULT-A: RX 700.7 GiB recorded, 804.3 GiB integrated over the logged timestamps). Single samples read up to 21.2 GB/s in the tabled runs (21.7 GB/s across all 93 H3 logs). That is above the PCIe 3.0 x16 theoretical rate, so individual readings are estimates. Two regimes show up on the 8 GB card:

```
Regime 1: Coarse Sampling (960x544, 124 frames)
  Step time:             15.5 - 21 s per step (no LoRA 15.5-16.2, production 16.9-18.8, turbo 19.5-21.0)
  PCIe weight transfer:  ~1.0 - 2.5 s per full DiT pass at 5-12 GB/s (estimate, not measured)
  Status: consistent with a compute-dominated step (GEMMs plus attention)

Regime 2: 1080p Refine of 243 frames (1920x1088)
  Sequence length:       124,440 tokens
  Linear-term floor:     ~71 s/step for the full sequence (the refine works in smaller tiles)
  Measured:              first 5-step tile group in 62.5 min (750.7 s/step average;
                         532-1087 s per step and 5-10 GB/s sustained PCIe per the archive)
  Status: dominated by weight streaming; stopped by the lab's 90-minute run timeout
```

### 4.1 Phase 1: coarse 544p sampling
- The DiT checkpoint is 11.68 GiB (12.54 GB). It is streamed from host memory over PCIe during sampling.
- At a realistic 5–12 GB/s, moving it once takes about 1.0–2.5 s (an estimate, not a measurement).
- A phase-1 step takes 15.5–21 s at 960×544 (§2.2).
- **Result:** Streaming is a small fraction of a step, even without overlap. Whether the transfer actually overlaps compute was not measured for H3.

### 4.2 Phase 2: 1080p latent refine of 243 frames
- The sequence length grows to:
  $$S = 61 \times \left(\frac{1920}{32} \times \frac{1088}{32}\right) = 61 \times (60 \times 34) = 61 \times 2,040 = 124,440\text{ tokens}$$
- The linear-term floor for the full sequence at 67.2 TFLOP/s is $2 \times 19.27 \times 10^9 \times 124,440 / 67.2 \times 10^{12} \approx 71\text{ s/step}$. The refine samples spatial tiles, so the per-tile floor is lower still.
- **Measured:** The first 5-step tile group averaged **750.7 s/step** (the run's progress bar: 5 steps in 1:02:33). The archived report gives individual steps of 532–1,087 s with sustained 5–10 GB/s PCIe traffic.
- **Post-mortem:** The weights cannot stay resident during this refine, so they are re-streamed over PCIe every step. Streaming, not arithmetic, sets the step time. VRAM (the graphics card's own memory) was sampled device-wide through NVML, and `torch.cuda.max_memory_allocated()` was not logged. So weight residency can't be separated from activation residency after the fact.
- Phase 1 completed normally (25 steps in 19:15, ~1,155 s, ~46.2 s/step; VRAM steady at ~7.5 GiB per the archive). The refine's first tile group then took 62.5 minutes for 5 steps, and the full refine projected to 6+ hours. The run was stopped after 5,400 s by the lab tooling's run timeout (`h3.proven.json`), not by a driver watchdog. The ComfyUI server survived it. At 124 frames the same refine completes: its tiles ran at 7.0–30.2 s/step in ULT-A, C3 and Flagship.

### 4.3 Practical limits
Archived single runs at one seed. Peak VRAM is the device-wide NVML peak, so it includes the desktop.

| Clip configuration | Resolution and frames | Steps | Wall time | Peak VRAM | Status |
|---|---|---|---|---|---|
| **ULT-A (6-step Turbo)** | 960×544 $\to$ 1080p refine (124f) | 6 + refine | 1153.3 s (~19.2 min) | 7.60 GiB | Completed (N=1, exploratory) |
| **C3 (4-step Turbo)** | 960×544 $\to$ 1080p refine (124f) | 4 + refine | 876.4 s (~14.6 min) | 7.59 GiB | Completed (N=1, exploratory) |
| **Flagship (25-step Base)** | 960×544 $\to$ 1080p refine (124f) | 25 + refine | 1484.1 s (~24.7 min) | 7.62 GiB | Completed (N=1, exploratory) |
| **T2 Single Pass** | 960×544 (243f, ~10.1 s) | 25 | 1271.4 s (~21.2 min) | 7.70 GiB | Completed (N=1, exploratory) |
| **1080p Refine (243f)** | 960×544 $\to$ 1920×1088 (243f, ~10.1 s) | 25 + refine | not completed (phase 1 ~1,155 s; first refine tile group 62.5 min; projected 6+ h) | ~7.5 GiB in phase 1 | **ABANDONED (stopped by the lab's 90-min run timeout)** |

---

## 5. The Windows WDDM paging cliff and Linux OOM

When a job asks for more VRAM than the card has (oversubscription), the two operating systems behave differently.

### 5.1 Linux: a clean error
On Linux the CUDA driver does not page device allocations out to system RAM. An allocation beyond physical VRAM fails with `torch.cuda.OutOfMemoryError`, which an orchestrator can see immediately.

### 5.2 Windows WDDM: silent paging
Under Windows WDDM, oversubscription does not fail cleanly. WDDM falls back to shared system memory, and the workload starts moving weights over PCIe in both directions. That is what the NVML traces show in the bad cases ([VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md](VIDEOLAB_01_H3_STREAMING_AND_8GB_LIMITS.md) §1). PCIe 3.0 x16 is more than an order of magnitude slower than the card's 448 GB/s GDDR6. So instead of an error, the step time grows from seconds to many minutes (§4.2).

### 5.3 Guards in the studio code
- **One local render at a time** (`tool/gpu_broker.py`): Before a job starts, every other local ComfyUI instance is emptied, and the emptiness is verified: `/queue` is empty and `vram_free` is back within `gpu_broker.vram_tolerance_mb` (default 512 MB, minimum 64) of that instance's boot baseline. A baseline is only recorded when the instance reports at least 5,500 MB free. By default the H3 instance is restarted instead of being asked to `/free` (`gpu_broker.h3_mode: restart`). Remote workers are out of scope.
- **Qwen-Image 2.1 node gates** (`ComfyUI_Qwen21/custom_nodes/ComfyUI-Qwen21-Optimized/qwen21_common.py`): `LatentTiledRefiner` and `VAEDecodeTiledPremulAlpha` require 7,000 MB free before they start (`vram_gate_mb`). After a collect/evict attempt they accept 98.5% of the gate; otherwise they raise an error. Afterwards they assert a ceiling of the card's real VRAM (from `torch.cuda.mem_get_info`) minus an 800 MB OS/display buffer: 7,392 MB on an 8 GB card. `LatentTiledRefiner` also re-checks a 900 MB paging guard before each window. The studio's `tool/workflows/qwen21_t2i.json` graph uses neither node, so these gates apply only to graphs that do.
- **Pre-decode eviction for LTX-2.5**: `LTX25EvictModels` (§6).

### 5.4 Measured on the studio PC (LTX-2.5 chain finish)
- **Sysmem fallback appears to be on** (the driver default; it lets VRAM spill into system RAM). A plain torch probe allocated 12 GB on the 8 GB card without an error. Chunks up to ~6.75 GB read at ~408 GB/s, and every chunk past ~7 GB read at ~12 GB/s, which means it lives in shared system RAM. The probe's output was printed in the session and not saved, so this rests on that one run.
- **LTX stage 2 at 1920×1088 × 121 frames + 5 detailing keyframes (~42.8k tokens)** runs at ~80 W / ~210 s per step, with 17–20 GB/s sustained PCIe receive and ~1 % VRAM-bandwidth use. The only 49-frame finishes on record are the smoke chain's own graph (`20261022_233142`, ~18k tokens with its 1–2 keyframes). That one was compute-bound: ~210 W busy, the first step 36–38 s including model initialisation, and the later steps 12–14 s.
- **Without the detailing keyframes (the current stage-2 default)** the finish stays compute-bound: 121 frames (~32.6k tokens) at 34–37 s per step, and 145 frames (~38.8k tokens) in 119–150 s for both steps including model initialisation (gate v2 S5, busy-power medians 197–204 W, peak VRAM 7.9 GB, process RSS 22.5 GB). Those 145-frame links finished in 215–244 s (2 steps, 1080p decode included). In that run the cliff lay between ~38.8k and ~42.8k tokens. Later 145-frame finishes at the same ~38.8k tokens spilled as well (next two points), so the cliff is not a fixed token count on this card.
- **The placement varies from run to run.** The same three 145-frame finish graphs, started from similar VRAM baselines (439 and 423 MB in use at ComfyUI start), sampled in 150 / 128 / 119 s in one chain (v2 S5, 05:06) and in 180 / 238 / 295 s in the next (v3, 05:56; whole finishes 272 / 336 / 390 s). The slow ones spent minutes at ~86–100 W with 100 % utilisation, the spill signature above. Without the driver setting (last point below), 1080p finish times near the cliff are not reproducible.
- **Later runs were slower still, and the cause is not established.** The v4 gates (11:52–13:25, 906–1,110 MB in use at ComfyUI start; which other applications were open was not recorded) finished their 145-frame links in 426 / 425 / 275 s (v4), 406 / 414 / 436 s (v4 at a second seed) and 434 / 366 / 603 s (v4 with the CQ LoRA in stage 2), with the same spill signature. Link 1 (121 frames, below the cliff) finished in 143–174 s in v3, v4 and v4 at the second seed. More desktop VRAM in use is a suspected contributor, not an established cause: the v3 run spilled as well from a 423 MB baseline, the v4 recipe differs from v3's, and CPU-heavy lab assemblies overlapped some of the v4 finishes. `nvidia-smi` shows no per-process VRAM under WDDM. Windows' `GPU Process Memory(*)\Dedicated Usage` counters do report it per process, but they were not recorded, so the share of each app is unknown.
- **Not the cause:** attention kernels and torch activations. A cuDNN SDPA benchmark at these shapes gives ~0.74 s per block for the 42.8k self-attention, ~40 s per step over 48 blocks, masks included. This Windows torch build has no flash kernel, and the benchmark's output was printed, not saved. Torch activations were 1.8–2.9 GB allocated and 3.9 GB reserved at peak, sampled during the step.
- **No effect:** `--vram-headroom 1` (195 s/step) and `--vram-headroom 2` (208 s/step). aimdo honours the headroom only against its own weight pages, and device VRAM still reached 7.9 GB. `--disable-dynamic-vram` was worse: its first step took ~590 s (593.5 s in the events, 589.8 s in the server log of `20261023_032748`; that probe has no `result.json`) against 173–215 s for the same graph with dynamic VRAM.
- **Working hypothesis, consistent with every number above:** Part of DynamicVRAM's weight pages are placed in fallback system memory, so each int8 GEMM re-reads those weights over PCIe once per token tile (~335 tiles per GEMM at 42.8k tokens). The test is the NVIDIA driver setting *CUDA – Sysmem Fallback Policy = Prefer No Sysmem Fallback*, applied to the interpreter that runs `ComfyUI_LTX` (the uv CPython 3.12 `python.exe` that the venv launches). With it, an oversubscription fails, and aimdo streams the weights through its cast buffers instead. Not applied yet: it is a system setting.

---

## 6. ComfyUI bug #15606 and the `LTX25EvictModels` workaround

On Windows, building the LTX diffusion decoder while the DiT is still resident raises "Windows fatal exception: access violation" inside `na_diffusion_decoder.py`. This is ComfyUI issue #15606. It was reproduced upstream on a 32 GB card, so it is not an 8 GB problem. In the lab, the symptom was that sampling completed and then the tiled VAE decode crashed or hung the process.

### 6.1 Root cause
Two explanations are on record, and they are not the same mechanism. The node's docstring gives the upstream one: building the diffusion decoder while the DiT is resident raises the access violation, and evicting between sampler and decoder is the reporter's workaround. The archive's own diagnosis is that at decode time DynamicVRAM had already offloaded the transformer (0.03 GiB allocated / 0.06 GiB reserved at eviction), so the decoder was not out of memory, and that the failure lies in driver-level allocations around the decode while stale offload state exists. That diagnosis is archived and can't be re-verified here. Which mechanism applies to the shipped conv-VAE path is not established ([VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](VIDEOLAB_02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md) §3).

### 6.2 The fix
`LTX25EvictModels` (`ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/__init__.py`) is a passthrough node that evicts models before the decoder:
```python
if keep_text_encoder:
    keep = [...]  # loaded models whose class name contains "TE", "TextEncoder" or "CLIP"
    for device in mm.get_all_torch_devices():
        mm.free_memory(1e30, device, keep_loaded=keep)
else:
    mm.unload_all_models()
mm.soft_empty_cache()
gc.collect()
torch.cuda.synchronize()
torch.cuda.empty_cache()
torch.cuda.ipc_collect()
```
Every `tool/workflows/ltx25_*.json` graph uses it with `keep_text_encoder: true`: directly before `VAEDecodeTiled`, and in `ltx25_temporal_10s` before the temporal latent upsampler. The H3 workflow graphs do not use it. The chain graphs use it a second time (§6.4). The node also has a `text_encoder_only` mode that unloads only the text encoder. The only graph that inserts that mode is an H3 chain link with `H3Link.release_text_encoder` on, which is off by default.

### 6.3 Result
With the guard, 19/19 generated videos decoded without a crash, and 30+ later runs completed crash-free on the lab machine. These are archived figures; the LTX run logs are not on the studio box. The guard costs about 2 s per shot. `keep_text_encoder` saves re-staging the 9.88 GiB Gemma encoder on the next shot: 26.2 s on a cold start and 1.9 s warm, in isolated A/Bs. (The 150.8 s → 70.5 s figure is the whole cold-to-warm difference, not the flag alone.)

### 6.4 Text-encoder release before sampling (chain graphs)
The LTX chain graphs (`tool/chain_graphs.py`, `_release_text_encoder`) also run `LTX25EvictModels` with `keep_text_encoder: false` right after the last prompt is encoded, before the sampler, in both the draft and the finish. The release is `unload_all_models`. Without it, the encoder stays pinned in RAM next to the DiT for the whole sampling run (the server logs `0 models unloaded` when the DiT loads).

Gate v2's second 1080p finish (`20261023_005904`) still had its five stage-2 keyframes (~49k tokens, past the cliff of §5.4). In it, the server's RSS grew to 39 GB and available RAM fell to 0.55 GB in the last sample. But that finish ran at ~80 W from its first minute of sampling, with 16.8 GB of RAM still available. Its first step appeared after 938 s of job time (about 876 s after the sampler started, model initialisation included), and the lab watchdog stopped it after 1,172 s without further progress. The rerun with the release (`20261023_035222`) held 13–15 GB less RSS at the same points (peak 19.1 GB, at least 21.7 GB available), and it still made no step in 15 minutes at ~80 W. So the release fixes the RAM side, not the cliff. Whether disk paging added to the first stall was not measured, and no record shows commit exhaustion. The price is re-staging the encoder on the next prompt (1.9 s warm, 26.2 s cold, §6.3).

The H3 chain link graphs used a release as well at first, and they no longer do. It made H3 links re-stage the 11 GB DiT, and in-process H3 model drops came before the three blue screens during the H3 tests. Instead, H3 chains restart the server before a graph that would drop a model at a job hand-over. The upscaler pack's own unloads inside the refine graph remain ([SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md](SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md) §5).
