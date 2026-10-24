# Laboratory Archive & Provenance Index

Curated index of the documents, benchmarks, and experiments from the video/image
generation research program executed across isolated local laboratory workspaces between
**2026-09-18 and 2026-09-29**.

## [PROVENANCE NOTICE: THIS FOLDER VS. HISTORICAL LAB CORPUS]

> [!NOTE]
> **External Provenance Citation Map:**
> The section headers and file paths listed below (`00_TOP_LEVEL_DOCS/`, `01_MINIMAX_H3/`,
> `02_LTX25/`, `03_QWEN_IMAGE_21/`, and `04_BENCH_CAMPAIGNS/`) cite the private ~1,800-file
> experimental laboratory corpus where research was performed and recorded.
>
> The **`videolab`** folder is the published part of the lab. It holds the telemetry harness
> (`bench/`), the ComfyUI workflow templates (`workflows/`), first-party custom node packs (`nodes/`),
> offline self-tests (`videolab.py selftest`; there are no first-party test files), and model manifests
> (`configs/`) needed to re-run the shipped configurations and benchmark recipes on an 8 GB GPU.
>
> **Two-Tier Evidentiary Classification:**
> - **Tier A (Shipped & Locally Reproducible):** ComfyUI workflows (`workflows/`), kernel parity gates (`bench/gate_w4a8.py`), model download manifests (`configs/models.manifest.json`), and offline self-tests (`videolab.py selftest`) included directly in this folder.
> - **Tier B (Archived Laboratory Telemetry):** The 12-day experimental campaign (Sept 18–29, 2026) produced 200+ telemetry-logged runs, negative ablation post-mortems, and thermal traces cited directly from my archived lab records.
>
> For the main videolab documentation, consult:
> - [README.md](../README.md): Architecture, quickstart, hardware envelope, and benchmark commands.
> - [docs/01_H3_STREAMING_AND_8GB_LIMITS.md](01_H3_STREAMING_AND_8GB_LIMITS.md): Token/FLOP derivations, PCIe bandwidth bounds, and 243-frame refinement analysis.
> - [docs/02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md](02_LTX_W4A8_KERNEL_AND_PIPELINE_STABILITY.md): Fused W4A8 INT8 kernel validation, $N == K$ fix, and WDDM decode stability.
> - [docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md](03_RETRACTIONS_FAILURES_AND_LESSONS.md): The empirical retraction ledger and post-mortem investigations.
>
> I publish this index so that the ablation paths, dead ends, and configuration sweeps behind the docs can be traced by name, even though the files themselves are not published, and so that others can skip the failure modes I already hit.

**Hardware context (all lab runs were measured on this workstation):**
RTX 3070 8 GB (SM 8.6 Ampere — no native FP8), i9-10980XE 18C/36T, 64 GB quad-channel DDR4,
Windows 11, ComfyUI v0.36.0 / comfy-kitchen 0.2.34 at initial baselines, torch 2.13.0+cu130;
H3 and Qwen execution was later verified on ComfyUI v0.37.4 / comfy-kitchen 0.2.35 (W4A8/INT8 ConvRot fused kernels).

**Three model programs, one production goal (AI movie studio):**
| Program | Role in production | Headline result |
|---|---|---|
| **MiniMax H3** (Hailuo 3.0, 33B) | climax / VFX / action hero shots | 5.17 s true-1080p hero render in 19.2 min (ULT-A); edge-variance proxy +91% over the 25-step baseline (single seed; exploratory) |
| **LTX 2.5** (22B distilled) | dialogue / standard shots / fast turnaround | 5.04 s 1080p clip in ~4.9 min warm (292.1 s mean), peak VRAM 7.8 GiB; the architecture generates joint audio-video latents, but the shipped 8 GB production workflow (`ltx25_w4a8_production.json`) executes the video-only decoding path and discards the audio latent to hold the 8 GB VRAM ceiling |
| **Qwen-Image-2.1** (7B DiT base; Qwen3-VL 8B text encoder) | still images (posters/refs/anchors) | dual mode: quality 2K RGBA ~215–304 s/img; FAST (Pruna DMD 8-step) 39–78 s/img at the highest measured sharpness (single run per variant; adapter not redistributed) |

---

## ARCHIVE MAP

### 00_TOP_LEVEL_DOCS — global plans, audits, and state
| File | What it is |
|---|---|
| my lab summary notes (not published) | Running summary of the whole lab: machine/env, studio architecture (ports 8080/8188/8189/8199), LTX + H3 full results, every render chronologically with metrics, every LoRA tested/adopted/rejected, every rejection with evidence, prompt rules, reference-binding rules, proven duration/resolution envelope, next steps, file map. |
| my revised speed plan (not published) | Revised speed plan (2026-09-22) that replaced my original plan after the audit below: a decision-governed DiT engine with eight gated speed levers; expected ~1.5×/clip, ~2.5–2.8×/film. Contains the do-not-resurrect rejection table. |
| my original speed plan (not published) | The original NO-GO plan (claimed 6.75–7.78×) kept for the record; superseded by the decision engine audit and the revised plan. |
| `DECISION_ENGINE_AUDIT.md` | My own audit notes that sank the original plan: FLOP model 1.7–1.9× optimistic, 75% of the 341 s clip is non-sampling overhead, adaptive stepping invalid on distilled ladders, VRAM arithmetic wrong. GO on the re-scoped program. |
| `ROUTING_DIT_BLUEPRINT.md` | Architecture specification for non-autoregressive decision dispatch + DiT hooks (sigma-subset compaction, query-gated attention, decision-gated tiering), corrected speedup table ~1.79×/clip, a list of requirements. |
| my quality-push notes (not published) | Quality-push plan (2026-09-23): H3 refine sigma defect, step-ladder experiments E1–E8, decision repair and calibration gates, a staged work order, rejected list. |
| `Benchg.txt` | Benchmark summary of both video labs: LTX W4A8 native on SM86 (69–74 TFLOP/s fused kernel, ~14× eager), H3 W4A8 stack, role allocation, flagship numbers. |
| `History.txt` | Work log of the speed round that shipped the S6a sigma ladder and keep_text_encoder (−53% per-shot overhead, identical output statistics at the same seed). |
| `LTX25_FEASIBILITY_VERIFICATION_REPORT.md` | Pre-implementation feasibility audit of LTX-2.5 22B on 8 GB (2026-09-18); "official INT8 on 8 GB" premise unsupported; gated 12-step blueprint. |
| `LTX25_ADVERSARIAL_AUDIT.md` | Adversarial second pass correcting 5 material errors (W4A8 IS first-party, kitchen ships Windows wheels, Winnougan 11.66 GiB checkpoint exists, PCIe Gen3 under load, VRAM is the constraint). Verdict: pursue experimentally. |
| `.reddit_h3_steps.json` | Archived Reddit research thread data on MiniMax H3 workflows (community steps; raw source material). |
| archived feasibility study (2026-09-18) | Work journal of the feasibility day — the self-audit that corrected the LTX feasibility errors, plus box quirks (nvidia-smi fails → use NVML via ctypes). |
| `_research_probes\` | Low-level hardware probes written before any model ran: CUDA/SM86 kernel tests, NVML probe, PCIe/disk benchmarks, ComfyUI "kitchen" load tests. These established what the box could actually do. |

### 01_MINIMAX_H3 — the MiniMax H3 (Hailuo 3.0) lab
`H3_LAB\`
- `docs\PROVEN_CONFIGS_H3.md` — **frozen production record**: 3 proven tiers, fixed sampler constants (euler/simple, CFG 1.0, shifts 12/3, 24 fps, 17k+5 frame grid), measured failures (PDD-8 loss of structural high-frequency detail, NVFP4 unusable on SM86, default h264 bitrate → CRF 16 became the encode baseline for the tested configurations).
- `docs\H3_PROMPT_GUIDE.md` — canonical H3 prompt anatomy (FL2VA/Ref2VA modes, `[Shot N]` blocks, soundscape sections, `<scenetrans>`/`<cutoff>` tags, camera grammar) + lab cinematography addendum (the model's whole-frame-motion weakness and how to frame around it).
- `Research.txt` — external research compendium: H3 architecture (33B DiT, Qwen3-VL-32B conditioning, f16t4d24 VAE, 40 Hz stereo audio latents), checkpoint ladder (BF16 61.7 GB → pruned W4A8 12.5 GB), ConvRot quantization, community acceleration survey (larryvrh turbo v4, LightX2V, Spectrum, SageAttention, De-RoPE).
- `benchmarks\` — **the H3 benchmark archive (~300 files)**:
  - `PHASE_A/C/E/H_REPORT.md(+.json)` — phase reports: runnability gate, W4A8-vs-INT8 quantization A/B (speed wash, W4A8 adopted), attention-backend sweep (kitchen INT8 1.35× at 124f), PDD 8-step (2.5× sampling, later banned from quality paths).
  - `PROGRAM_REPORT.md` — original program certification; `PRODUCTION_MODES.md` — frozen runbook (CINEMATIC FAST / QUALITY / DRAFT modes).
  - `phase1_h3_checkpoint_report_*.json` — per-checkpoint validation of every quantized variant.
  - Hundreds of `*_prompt.json` / `*_run.json` pairs — every A/B experiment reproducibly captured (step ladders, sigma sweeps, LoRA batteries, certification cells).
  - `cert_strip_*.png`, `iso_*.png`, `final_*.png`, `epic_*.png`, `ab_*` folders — visual A/B comparison strips (the evidence).
- `scripts\` — lab drivers: `run_h3.py` (the lab runner with --refine-sigmas, --lora, --attention, --vae-override, etc.), phase freeze scripts, metric tools (`temporal_sharpness.py`, `detail_metric.py`), server watchdogs, `capture_object_info.py`.
- `logs\`, `scenes\five_stress_scenes.json` — server logs and the fixed 5-scene stress battery used for one-variable comparisons.
- `ComfyUI_custom_nodes_project_code\` — node packs installed for H3 (code/docs only, demo media stripped): H3-FaceRefine (per-frame face repair), Spectrum-MiniMax-H3 (training-free 34% acceleration), Minimax_h3_latent_Upscaler, MAINodes (De-RoPE + Motion Adapter), h3lab_nodes (chunked fp16 upscaler), plus GGUF/VideoHelperSuite support packs.
- `ComfyUI_user_misc\qwen21_metrics.jsonl` — metrics captured from a Qwen node run inside this lab.

### 02_LTX25 — the LTX 2.5 lab
`LTX25_LAB\`
- `IMPLEMENTATION_REPORT.md` — end-to-end verification in the AI movie studio (port 8189, MSR reference nodes, registry/templates, 1080p 121f at 341 s, peak 7.5 GiB, zero black frames).
- `LTX25_W4A8_IMPLEMENTATION_REPORT.md` — the W4A8-on-SM86 proof: fused CUDA kernel 69–74 TFLOP/s (~14× eager, relL2 1.66e-3), real checkpoint validated, resolution ladder clean through 1920×1088×121, conv VAE −25% wall, verdict GO (~332 s footage/hour at 1024×576).
- my studio integration notes (not published) — the integration design (second isolated ComfyUI, family-aware registry, per-cast reference system, VRAM leasing, safety rules).
- `PROVEN_CONFIGS.md` — **frozen configs**: MAIN 1920×1088×121 + DFR + MSR refs stage-1-only = 465.5 s/clip; the non-negotiable rules (never refs at stage 2, explicit eviction before decode, conv VAE always, prompt motion explicitly).
- `benchmarks\` — ~260 files of phase runs 1–9 (W4A8 gate, resolution ladder, conv VAE, evict/keep strategies, DFR A/B, seed studies, FLF2V/I2V face tests, audio) as prompt/run/verify JSON triples.
- `frames\` — comparison grids, contact sheets, and A/B compare images (raw per-frame extracts omitted; grids contain the same frames downscaled).
- `REVIEW\` — visual evidence: all comparison PNGs, the small production sample clips (I2V/FLF2V face, FIGHT, JULIET takes), and the definitive renders (05_RECOMMENDED, 10_MAX, **11_BEST**, 30/31_FINAL motion pair, **32_BEST 1080p MSR**).
- `workflows\` — MSR sample workflow + ainvfx fluid workflow JSON.
- `scripts\` — 170+ lab scripts (run_ltx25.py runner, phase drivers, repair/diagnostic scripts).
- `logs\` — server logs incl. the stall/crash forensics.
- `ComfyUI_custom_nodes_project_code\` — ComfyUI-LTX2.5-MSR (reference node pack, pinned edec572) + ltx25_lab_nodes (frames_to_latent A3 node).

### 03_QWEN_IMAGE_21 — the Qwen-Image-2.1 lab
`Qwen2.1Image_LAB\`
- my Qwen deployment plan (not published) — the starting design (Q4_K_M GGUF, TE pinned in host RAM, CFG 1.0, two-pass 2K, a numbered list of claims and requirements to check).
- `Research.txt` — research compendium: architecture (7.1B DiT + 8B Qwen3-VL TE, 64-ch RGBA VAE), distillations (Viggle/PAI/Pruna), GGUF vs ConvRot quantization tables, LoRA ecosystem, PE-T2I prompt enhancement.
- `docs\engineering_scratchpad.md` — derivation layer that reconciled the plan against the real runtime (tiling math fixed to 72-token 2×2 windows, banned CLI flags purged with a hard validator, CPU latency model).
- `docs\VERIFICATION_REPORT.md` — **live verification (2026-09-28)**: 27.94 GB assets checksummed, 24/24 selftest, measured empirical deviations (2.33 s/step vs assumed 1.283) and the evidence-based quality pivot (CFG 3.5 + euler/simple/40 + real negative beats CFG 1.0; Q5_K_M; pass-2 re-denoise removed; PE bypassed) → locked production config + FAST mode.
- `reports\` — benchmark JSONs: turbo/twopass timings, exp_quality A/Bs, `speed_suite` (dual-mode decision: Pruna DMD 8-step adopted as FAST), `final_suite` (5-image 2K suite; threshold FAIL rows recorded where the sharpness gate does not apply).
- `output\` — the generated evidence PNGs: ab_tests, exp_quality (the CFG/quantizer pivot A/B), quality_suite, speed_suite (dual-mode A/B), final_suite (the 2K deliverable suite), photoreal_2k outputs, sara_vs_anna arena. (Early turbo draft renders omitted.)
- `workflows\` — the two production workflows (photorealism 2-pass 2K, turbo draft preview) as ComfyUI JSON.
- `ComfyUI-Qwen21-Optimized\` + root `*.py` — the optimized node pack and all suite drivers (`quality_suite.py`, `speed_suite.py`, `final_suite.py`, `benchmark_pipeline.py`, memory-safe launcher).
- `_probe\` — one-off probes (sharpness comparisons, CFG sweeps, upscale sources).

### 04_BENCH_CAMPAIGNS — dated benchmark campaigns (the chronological experiment record)
| Folder | What happened there (read `REPORT*.md` / `*.md` inside first) |
|---|---|
| `BENCH_2026-09-22` | LTX "animated look" root-cause (fantasy prompt language → photographic substitution layer), PDD-8 verdict for H3, **REPORT_KTE** (keep_text_encoder: −53% overhead, identical output statistics at the same seed), **REPORT_L2_L3** (S6a sigma ladder adopted; the ±3% quality gate shown to be below seed variance). Includes the full studio backup (workflows + code) of that day. |
| `BENCH_2026-09-23` | Quality push: CQ enhancer LoRA +85% edge proxy adopted (single-seed exploratory); CRF 16 encoding mandated (pixelation was the encoder); LTX-as-upscaler rejected with evidence; H3 refine repaired (r45 re-noise); `BEST_CONFIGS.md` (frozen), `LTX_PROMPTING_GUIDE.md` (10 verified rules), and my planning notes of that day (not published: a seven-step realism program, the temporal upscaler semantics, an execution order). |
| `BENCH_2026-09-25` | H3 LoRA research battery (`H3_LORA_RESEARCH.md` — no continuation LoRAs exist; AddGuide anchor is the identity mechanism; larryvrh 6-step adopted, +89% edge; int8 VAE decode −46% VRAM, achieved aggregate metric parity ($\Delta \le 0.1\%$) at the tested seed without per-frame bitwise equivalence), 768p G5 quality standard, storage cleanup (268.6 GB freed; `CLEANUP_MANIFEST.md`). |
| `BENCH_2026-09-26` | Final H3 batteries: combination stacks (C1/C3/C4), ULT-A adopted config (`PROVEN_CONFIGS_FINAL.md` with exact recipes), 768p/243f failure mapping (LoRA dispatch collapse, thrash zone), flagship vs C3 vs ULT-A. |
| `BENCH_2026-09-27` | Composite-anchor program (`REPORT.md`): composite anchor ADOPTED (edge proxy +8.3% at one seed; wardrobe held all 124 f visually); 243f+1080p refine measured NOT viable (6 h+; corrects the record); single-pass 243f adopted for 10.13 s; last-frame chaining measured a low mean boundary delta ($6.75/255$ mean absolute channel difference, 2.6%) across the tested seam and wired into the production studio (45 checks green). |

---

## KEY FINDINGS — MiniMax H3

1. **Proven stack on 8 GB:** pruned W4A8 ConvRot DiT (11.68 GiB) + Qwen3-VL-32B W4A8 encoder (14.62 GiB, streamed) + comfy-kitchen INT8 attention (1.35× at 124f) + INT8 VAE decode (−46% VAE VRAM; aggregate metrics within 0.1% at one seed, not per-frame parity). INT8 transformer = speed wash; NVFP4 unusable on SM86.
2. **larryvrh turbo_v4_step600_ema at 6 steps is the new default** — edge proxy +89% over the 25-step baseline at 544p (single seed; exploratory). It powers ULT-A (highest edge-proxy render of the program): 6-step 544p phase-1 → latent upscaler → 5-step r45 refine on the clean base at 1920×1088 = 19.2 min, edge proxy 2819.8 vs flagship 1472.5 (single seed each; exploratory).
3. **Identity binding:** in my tested H3 configurations, frame-0 AddGuide anchoring was the only reliable identity binding mechanism observed (subject = frame-0 anchor; environment = described in words; references alone did not bind reliably). **Composite anchor** (a frame from a prior render of the same scene, chosen to match the prompt's Opening State) is the strongest form — +8.3% on the edge proxy (single seed) and wardrobe held across all 124 frames (frame-level visual check).
4. **Measured hard walls:** 243f+1080p refine = 6 h+ (62.5 min for the FIRST tile group) — not viable; 345f/362f two-phase = thrash/OOM; LoRA dispatch collapse at 768p/243f (582 s/it). >10 s at true 1080p = multi-beat concat, single-pass 243f 544p + post-upscale, or **last-frame chaining** (measured: +2.0–2.6% wall on one link pair, seam frame diff 6.75/255, single measured pair), or a 16 GB card.
5. **Always:** CRF 16 encode (eliminated the observed compression blockiness in the tested encode A/B vs default ~2 Mbps h264), CFG 1.0, euler/simple, video shift 12 / audio shift 3, frame counts on the 17k+5 grid (124f = 5.17 s). PDD-8 and Turbo-as-full-trajectory are banned from quality paths.

## KEY FINDINGS — LTX 2.5

1. **W4A8 makes 22B real on 8 GB:** 11.66 GiB distilled transformer + 9.88 GiB Gemma TE via DynamicVRAM at 7.5–7.8 GiB peak; full ladder through 1920×1088×121; conv VAE mandatory (2.6× faster decode); explicit eviction before tiled decode = the Windows decoder-crash guard (30+ crash-free runs).
2. **Proven recipe:** stage-1 960×544 on the 6-step S6a ladder → latent upscale → stage-2 1080p; **MSR references at STAGE 1 ONLY** (stage-2 refs stall ~4 min/step or hang silently); DFR keyframes interval 24 for detail; warm production ~292.1 s (~4.9 min) per 1080p shot against the ~341 s cold baseline (a 48.9 s net reduction). The early 73-frame exploratory A/B measured the full cold-to-warm composite difference at 80.3 s (150.8 s cold vs 70.5 s warm); isolated matched benchmarks on the production pipeline show `keep_text_encoder` saving 26.2 s on cold runs (−17.4%) and 1.9 s on warm runs (−2.6%), with fused W4A8 kernel staging as the other major contributor.
3. **Biggest quality wins were prompt/LoRA-side:** photographic substitution layer fixed the "animated/CG" look for free; CQ enhancer LoRA @0.6 = +85% edge proxy / +188% floor (single seed; exploratory); explicit motion verbs doubled measured motion (11.2%→21.6%); CRF 16 eliminated the observed compression blockiness in the tested encode A/B.
4. **Duration envelope:** 8 s clips drop stage-2 to 1280×704 (211.5 s vs 28.3 min thrashing at 1080p); temporal upscaler ×2 gives 10.04 s native 1080p (94.4% sharpness retention, interpolation semantics, no audio); native joint AV audio verified on the experimental audio branch (−10.8 dB, AAC 48 kHz; the shipped production template decodes video only).
5. **Bounded negatives:** the tested no-pixel-conditioning approach failed to preserve structural coherence on imported latents (LTX-as-upscaler probe); the tested saliency-gated attention mechanism produced unusable attention selection; official IC-LoRAs (Pixel-Spatial Upscaler, Deblur) remain HF-gated pending a user token.

## KEY FINDINGS — Qwen-Image-2.1

1. **Memory contract held** (peak 5.9–6.5 GB vs 7,392 MB ceiling, zero paging; Qwen text encoder weights remain pinned in host DDR4; parameter residency confirmed off-device prior to invocation) but the plan's speed targets were physically wrong (measured 2.33 s/step, not 1.283): turbo ≈ 23.5 s, two-pass 2K ≈ 262 s warm.
2. **Evidence overturned four plan settings:** CFG 3.5 + euler/simple + 40 steps + a real negative prompt beats CFG 1.0 (+30% sharpness 2729→3536); Q5_K_M replaces Q4_K_M; pass-2 latent re-denoise removed ("painted" detail; sharpness 513 vs 453 without); PE-T2I enhancer bypassed (124 s via paging). Turbo mode correctly stays CFG 1.0.
3. **Shipped dual mode:** QUALITY = Q5_K_M, 40 steps, CFG 3.5, untiled decode, 2K RGBA at ~215–304 s/img; FAST = Pruna DMD 8-step CFG 1.0 at 39–78 s/img with the highest measured sharpness (3808 vs 3499 control; adapter not redistributed — baseline research); Viggle 6-step LoRA (117 s, 3072) is the softer middle option.
4. **Runtime realities:** ComfyUI had to move to v0.37.4 (v0.36 cannot build the 2.1 VAE); `--gpu-only-unet` and `res_2m` do not exist (argparse-validated launcher purges banned flags); 2K refinement = 72-token 2×2 tiled windows with 16-token overlap; direct 2K latent generation prohibited.
5. **Deliverables verified:** 5-image final 2K suite (all RGBA, dHash-distinct), every claim in reports JSONs, including two FAIL rows where the sharpness threshold does not apply (dark/low-DOF scenes).

## GLOBAL FINDINGS (decision engine + speed program)

- My original 6.75–7.78× speed plan was audited **NO-GO** (physics wrong); the revised plan re-scopes to gate-governed levers worth ~1.5×/clip, ~2.5–2.8×/film.
- **Shipped and verified:** sigma compaction (S6a) and keep_text_encoder. **Measured and rejected:** query-gating (the tested saliency-gated attention mechanism produced unusable attention selection). **Research-only:** Tier-2 latent observer.
- Decision engine rules: non-autoregressive routers act on discrete text state only, never latents; must be evicted before the DiT loads; advisory until calibrated (agreement ≥80%, ECE ≤0.10, permutation Δp ≤0.05). The remote client requires external endpoint authorization; the local router v0 fine-tuned (tier agreement 1.00 on 28 shots) but n is far below the ≥5k gate.

## Exact proven configs (where to look)

| Config | Where documented |
|---|---|
| H3 ULT-A "Quality 5s 1080p" (19.2 min, edge 2819.8) | `04_BENCH_CAMPAIGNS\BENCH_2026-09-26\PROVEN_CONFIGS_FINAL.md` |
| H3 C3 "Speed-Quality" (14.6 min, edge 2363.5) | same file |
| H3 243f 10.13 s standard (`iceq_char.png` frame-0 anchor); last-frame chaining | `04_BENCH_CAMPAIGNS\BENCH_2026-09-27\REPORT.md` (+ drivers\h3_c3_continuation_prompt.txt) |
| H3 three production tiers + fixed constants | `01_MINIMAX_H3\H3_LAB\docs\PROVEN_CONFIGS_H3.md` |
| LTX quality default (S6a + DFR + CQ @0.6, ~4.9 min warm) | `04_BENCH_CAMPAIGNS\BENCH_2026-09-23\BEST_CONFIGS.md` + `02_LTX25\LTX25_LAB\PROVEN_CONFIGS.md` |
| LTX 10 s temporal / 8 s 1280×704 paths | `BEST_CONFIGS.md` §long-clip + my temporal-upscaler notes (not published) |
| Qwen QUALITY + FAST production modes | `03_QWEN_IMAGE_21\Qwen2.1Image_LAB\docs\VERIFICATION_REPORT.md` §7 |
| 10-rule LTX prompting guide | `04_BENCH_CAMPAIGNS\BENCH_2026-09-23\LTX_PROMPTING_GUIDE.md` |
| H3 prompt anatomy + reference binding rules | `01_MINIMAX_H3\H3_LAB\docs\H3_PROMPT_GUIDE.md` + my lab summary notes (prompt and reference-binding sections) |

## REJECTION LEDGER (do not retry without new evidence)

With measured evidence, archived in my lab summary notes (rejection section) and the BENCH reports: PDD-8/Turbo-LoRA as quality paths (−22% edge / loss of structural high-frequency detail), fal Realism-People (−27% edge, wardrobe drift), 243f/345f/362f two-phase 1080p (6 h+/thrash/OOM), 768p fl2v turbo (582 s/it dispatch collapse), the tested no-pixel-conditioning approach on imported latents (LTX-as-upscaler), query-gated attention in production, adaptive step skipping / token-gather / CFG modulation on distilled models (audit NO-GO), ltx23_surfaces stacked on CQ (no gain), S6b ladder (pillarbox artifact), local classifier as routing backend before ≥5k labels.

## HISTORICAL LAB ARTIFACTS & NON-REDISTRIBUTED DATA

To keep this folder lightweight, clean, and portable, non-essential heavy binary artifacts from the historical lab corpus remain unbundled:

**Infrastructure & Weights (Reconstructable via CLI):**
- Python virtual environments, package caches, and local ComfyUI installations (reproducible via `install.ps1` or `install.sh`).
- Model weight binaries are not redistributed in git: `python videolab.py download --profile <profile>` fetches the checksum-verified downloadable set (65.5 GB for `full`); the MiniMax H3 bring-your-own-weights stack (52.83 GB recorded) is validated in place by `--profile h3-byow`.

**Raw & Intermediate Media (Historical Lab Scratch):**
- Uncompressed gigabyte-scale intermediate video frames, render scratch directories, and transient debug dumps from the 2026-09-18 through 2026-09-29 runs.
- Duplicate review MP4 encodes (archived, not redistributed).
- Third-party node pack demo media (original repositories are linked in `nodes/third_party/`).
- Mathematical derivations and telemetry metrics are preserved in this distribution; core test and verification harnesses are included (raw lab scratchpad scripts remain archived).

*Historical lab corpus finalized 2026-09-29. Reproducible benchmark harnesses are provided for shipped workflows; historical campaign figures reflect archived laboratory telemetry records.*

## Corrections

This is a snapshot of my lab notes as of 2026-10-01. Some figures in it turned out wrong; the corrected version, with each correction listed, is [docs/research/VIDEOLAB_ARCHIVE_INDEX.md](../../docs/research/VIDEOLAB_ARCHIVE_INDEX.md) in Kinoloom. Quote that one, not this file.
