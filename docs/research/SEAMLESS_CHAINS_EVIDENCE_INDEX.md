[Kinoloom](../../README.md) › [Docs](../README.md) › Chain evidence index

# Seamless chains: evidence index

> **In short:** This index says where every number in the seamless-chain docs
> comes from: run ids, logs, analysis outputs and the crash records. The
> records stay on the studio PC and are not part of the repository. If you
> have the files, use it to check a claim; if not, it still shows which
> numbers rest on printed output only.

Where every number in the `SEAMLESS_CHAINS_*` documents comes from.

**None of this evidence is in the repository.** It lives on the studio PC
under `output/`, which is gitignored, and it includes rendered video of
private characters, so it is not redistributed. This index lets anyone with
the files check each claim. For everyone else, the docs state their
numbers, their N and their limits.

On this page:

- [Where the records are](#where-the-records-are)
- [Runs by document section](#runs-by-document-section)
- [videolab evidence used for the VIDEOLAB copies](#videolab-evidence-used-for-the-videolab-copies)

## Where the records are

| Location | What it holds |
|---|---|
| `output/seamlab/runs/<run id>/` | 63 run directories from the seamless-chain work (`20261022_*` and `20261023_*`), 62 of them from `tool/scripts/chain_lab.py` (`20261023_004500_join_ab` is not a `chain_lab` run and has none of the run-level files). A complete run has `lab.json` (command line, git head, GPU state at start), `spec.json` (the chain spec), `summary.json` (seam QA, A/B tables), the ComfyUI server log (`comfy_ltx.log` / `comfy_h3.log`), and per step `graph.json`, `result.json` (wall time, per-node timings, peaks, the lab's clean step), `telemetry.jsonl` (2 s samples: VRAM, power, SM clock, RSS, free RAM) and `events.jsonl`; 25 of the 63 lack one or more of the run-level files (see below). Assembled takes and review strips sit next to them. |
| `output/seamlab/baselines/baselines.json` | Seam QA of the old engine's takes (LTX `bcb2e4a7`, H3 `5c9b1e8b` and two others). |
| `output/seamlab/console/` | Console logs of the first LTX smoke, gate and A/B commands, the speed and memory probes. |
| `output/seamlab/analysis/session_2026-10-23/` | Copies of the session's analysis outputs: `logs/` (lab command logs), `measurements/` (InsightFace age/identity JSONs, colour curves, the take-split check, the `s2cast` age output), `scripts/` (the analysis and run scripts), `images/` (face grids and crops), `docs_evidence/` (the record dump and summary scripts used to write these documents). |
| `ComfyUI_LTX/output/studio_chain/<chain>/` | The chains' state. For the lab chains (`lab_*`), only the JSON is left: per-chain `manifest.json` (step digests, input signatures) and the per-file sidecars. Their lossless part (per-link latents `*.safetensors`, lossless link and take videos `.mkv`, `.wav`) was deleted; see the note below. The showcase chains (`show_*`) were rendered later and still have their lossless files. |
| `output/seamlab/analysis/session_2026-10-23/chain_analysis_2026-10-22/` | The analysis of the old engine's chains (`c4353`, `c5c9b`, `cbcb2`: `metrics.json`) and its scripts; the source of the threshold-origin ranges in doc 01 §1. Copied from the session's scratch folder, where it was the only copy. |
| Windows System event log, `C:\Windows\Minidump\` | Kernel-Power 41, WER 1001 and `nvlddmkm` 153 events; the minidumps of the blue screens. |
| A crash-dump folder outside the repository | Copies of the minidumps of 2026-09-18 22:04, 2026-10-23 07:05 and 14:46, the ones parsed with `kdump_triage.py`. The 15:54 dump was not copied out (administrator rights) and was not parsed. |

**Showcase records.** The chains rendered for the README
showcase after this work (three LTX-2.5 chains, shown, and two H3 chains,
described in SHOWCASE.md but not shown for licence reasons, plus one dropped
LTX-2.5 chain; all through the lab's `--request` mode) have their
own records, under `output/seamlab/runs/20261024_*`, `output/showcase/` and
`ComfyUI_LTX/output/studio_chain/show_*/`. The
[Records section of SHOWCASE.md](../SHOWCASE.md#15-records) lists them.

**Lossless files deleted.** To free disk space, the lab's lossless
intermediates were deleted: every `*.mkv`,
`*.safetensors` and `*.wav` under `ComfyUI_LTX/output/studio_chain/lab_*/`,
and the lossless take masters `output/seamlab/runs/*/*.master.mkv`. That came
to 571 files, 38.37 GiB. The list of deleted files, with sizes, is
`output/seamlab/analysis/cleanup_2026-10-24.txt`. Kept: every JSON
(manifests, sidecars, run records), the server logs, telemetry, the encoded
`.mp4` takes with their `.wav`, and the QA outputs and strips. The
re-measurements on lossless files listed below (made before the
deletion) stand as recorded, but they can no longer be repeated on those
files. The encoded takes allow only an approximate re-check; the lab chains
would have to be re-rendered to re-check them exactly.

**Missing run-level files.** `spec.json` is missing in 17 runs (the early LTX
checks and A/B runs, the probes, the take checks and the H3 seam check).
`summary.json` is missing in 10 (the four probes, `20261023_053702`,
`20261023_004500_join_ab` and the H3 runs `063918`, `140354`, `151355` and
`180139`), and the ComfyUI server log in 5 (`000522`, `004500`, `125729`,
`132551`, `133436`). For the H3 runs without a summary, the seam QA of the
gate's 544p take B0 exists only in `logs/h3_gate.log`, and the smoke chain's
per-link QA only in `logs/h3_smoke.log`.

**Numbers that rest on printed output only.** These were printed in the
session and not saved to any record; the docs mark them:

| Number | Where used | Status |
|---|---|---|
| Carry-restore gain 1.06-1.09 (about +10 saturation over the carry), `carry_gain.py` | doc 01 §2 | printed only |
| VAE round trip: RMSE 0.24-0.42, latent std 0.92, +1.7 saturation | doc 01 §2, doc 03 item 2 | printed only; the `003452` summary has no such values |
| Stage-2 isolation matrix (SSIM, texture correlation), `finish_cmp.py` | doc 01 §6 | printed only; re-computed from the lossless files by the fact-check, every row matching to three decimals (that output was not saved either) |
| v4 lossless draft take, seam 241: pop 1.51x, saturation +7.79, lightness -4.87 | doc 01 §10 | printed only; re-measured later: 1.508 / +7.785 / -4.870 |
| The +12 to +15 saturation flash over frames 241-248 (v2) | doc 01 §4 | re-measured later on corner regions |
| The breath in the audio, -75 -> -40 -> about -26 dBFS | doc 01 §10 | re-measured later in 100 ms windows; the QA step itself is -75 -> -40 |
| Sysmem-fallback probe (12 GB on the 8 GB card, ~408 GB/s in VRAM, ~12 GB/s past ~7 GB) and the cuDNN SDPA bench | doc 02 §4, overview, hardware doc §5.4 | scripts exist, output not saved |
| Commit limit 87.7 GB at 01:00 and about 44 GB held by other processes | doc 02 §4 | printed only; 81.7 GB read again later |

Two printed outputs were copied into `measurements/` afterwards and are
saved: the take-split check (`split_check_output.txt`) and the `s2cast` age
measurement (`s2cast_age_output.txt`).

## Runs by document section

### Doc 01 (LTX)

| Run id | Command | Used in |
|---|---|---|
| `baselines.json` (`ltx_bcb2e4a7`) | old engine's 1080p take | §1 old seams |
| `chain_analysis_2026-10-22/*/metrics.json` | the old engine's chains, analysed before the new engine existed | §1 threshold origins |
| `20261022_233852_ltx_gate_F0` | stage-2 finish with MSR references (~73k tokens); ended after 7 min 37 s with WinError 10054 and an `nvlddmkm` 153 event | §7; doc 02 §5; doc 03 item 6 |
| `20261023_000513_ltx_seam_ab` | soft pin R1 / flat L5 vs hard pin, seam 121 | §2 table rows 1-3 |
| `20261023_000522_ltx_gate_-` | hard-pin take assembled | §2 rising saturation steps |
| `20261023_002354_ltx_restore_check` | carry restore | §2 R1cr |
| `20261023_003452_ltx_latent_carry_check` | VAE round trip vs latent carry (decode-only) | §2 RMSE 0.24-0.42, +1.7 |
| `20261023_003835_ltx_seam_ab` | latent carry | §2 LC |
| `20261023_004427_ltx_take_check` | latent carry, decode-once take at join 2 (`LC_j2`) | §2 note under the table (the like-for-like baseline for AdaIN A) |
| `20261023_004944_ltx_seam_adain_check` | seam AdaIN A / B / C | §2 AdaIN |
| `20261023_005904_ltx_gate_F0` | gate v2 drafts and finishes; `finish_F0_l2` stalled | §8 memory |
| `20261023_031113`, `032748`, `041104_speed_probe`, `034247_mem_profile` | finish speed / memory probes (`032748`: the `--disable-dynamic-vram` probe; events, telemetry, `dmon_nodyn.txt` and the server log, but no `result.json`) | §7, hardware doc §5.4 |
| `20261023_035222_ltx_gate_F0` | gate v2's link-2 finish rerun with the text-encoder release; no step in about 15 min, stopped by the watchdog at 04:10:16 | §8; doc 02 §5 |
| `20261023_042759` ... `045956_ltx_gate_F1..S5` | stage-2 isolation matrix, link 1 | §6 table |
| `20261023_050612_ltx_gate_S5` | v2 S5 finishes and take (seam 241 +9.65) | §4, §7 |
| `20261023_052929`, `053725`, `054302`, `054833_ltx_take_check_final` | join / blend variants | §3 table |
| `20261023_055607_ltx_gate_F0` | v3 verification chain | §4, §7, §9 |
| `20261023_103847`, `104301`, `104629`, `104925_ltx_gate_age_*` | link-1 draft levers (cast line, face close-up, CQ 0, upper-body crops) | §9 set A |
| `20261023_105235`, `105519`, `105759`, `110105_ltx_gate_s2*` | link-1 stage-2 age variants (CQ 0, from 0.55, cast sentence) | §9 finishes |
| `20261023_110357` ... `114526_ltx_gate_F0` (chains `lab_age_*`) | CQ-0 + face close-up drafts and finishes, seed-shift check | §9 set B, glow |
| `20261023_112506`, `112800`, `113051_ltx_gate_lo/locq/cq` | finish from 0.55 / 0.55 + CQ / 0.725 + CQ | §6, §9 (`combo3_measure.log`) |
| `20261023_115153_ltx_gate_F0` | v4 gate (all defaults): re-roll, sun glare | §5, §7, §10 |
| `20261023_123935_ltx_gate_F0` | v4s2 (seed + 7919) | §7, §9 |
| `20261023_125729_ltx_gate_F0` | v4 takes with the look path | §10, §11; doc 04 split check |
| `20261023_132506_ltx_gate_Fcq` | v4 with CQ 0.6 in stage 2; re-QA of the v4 draft take | §7, §9, §10 |
| `20261023_132551`, `133436_ltx_gate_F0` | v4s2 takes, look path with the HSV scale | §10 |

Age and identity: `measurements/age_l1_*.json`, `age_v3*.json`,
`combo3_age.json`, `glow_age.json`, `v3v4_age.json`, `v4_age.json`; logs
`age_matrix.log`, `s2_age.log`, `combo*_age.log`, `combo3_measure.log`,
`glow.log`. Summaries: `docs_evidence/age_summary.py`.

### Doc 02 (H3, memory, crashes)

| Run id | Command | Used in |
|---|---|---|
| `20261023_063918_h3_smoke` | smoke links 1-2 (per-link decodes); the machine crashed at `decode_B0` (07:05) | §2 row 1, §5; doc 03 item 8 (first-step times in `comfy_h3.log`) |
| `20261023_101357_h3_smoke` | smoke `decode_B0` and one-graph refine R0 | §2 last row |
| `20261023_112044_h3_seam_check` | seam match none / r5 / r10 | §2 table |
| `20261023_140354_h3_gate_R0` | gate links 1-2; crashed at link 3 (14:46) | §3 time table, §5 |
| `20261023_151355_h3_gate_R0` | gate links 3-4; crashed at `decode_B0` (15:54) | §3, §5 |
| `20261023_180139_h3_gate_R0` | `decode_B0`, then the one-graph refine (interrupted at step 23) | §3, §4 |
| `20261023_182700_h3_gate_R0` | three-step refine R0 | §3, §4 |
| `20261023_194620_h3_gate_R25` | three-step refine R25 | §3, §4 |
| `baselines.json` (`h3_5c9b1e8b`) | old engine's H3 take | §3 seams |

Identity: `measurements/h3_age_old.json`, `h3_age_new.json`,
`h3_age_r25.json`. Lab command logs: `logs/h3_smoke*.log`, `h3_seam.log`,
`h3_gate*.log`, `h3_refine.log`. Dump triage: `scripts/kdump_triage.py`.

From the same runs and the showcase runs:

- The checkpoint (§1): the `UNETLoader` node of every `link_l<N>/graph.json`
  and `refine_<variant>/graph.json` in the runs above. The `112044` seam-check
  graphs load no UNet.
- The refine sigmas (§3): the `ManualSigmas` node of
  `20261023_182700_h3_gate_R0/refine_R0/graph.json` and
  `20261023_194620_h3_gate_R25/refine_R25/graph.json`.
- The showcase restart before the 544p take decode (§5, doc 04 §2): the
  second "Starting server" in the `comfy_h3.log` of
  `20261024_024833_h3_req_h3_cliff_R0` and
  `20261024_040010_h3_req_h3_kitchen_R0`. No Kernel-Power 41 or `nvlddmkm`
  event appears in the System event log after the 15:54 crash.

### Doc 04 (studio integration)

The take-split check ran `scripts/split_check.py` on the v4 1080p take of
`20261023_125729_ltx_gate_F0` (output in
`measurements/split_check_output.txt`). The per-file test counts come from
the test files in `tests/unit/`, and the suite total (710) from
`output/seamlab/analysis/docs_evidence/pytest_2026-10-25_release.txt`.

The Video Studio chain of §6: its record
`output/videostudio/chains/25a6760b36f0.json` (request, references, seam QA
of both takes, cast reports), its step manifest
`ComfyUI_LTX/output/studio_chain/studio_25a6760b36f0/manifest.json` (step
seconds), and the take `output/videostudio/chain_25a6760b_final.mp4`
(ffprobe: H.264 1920x1088 at 24 fps, 289 frames, AAC). The portrait chain
of §6 the same way: `output/videostudio/chains/9de7e018337e.json`,
`ComfyUI_LTX/output/studio_chain/studio_9de7e018337e/manifest.json` and
`output/videostudio/chain_9de7e018_final.mp4` (ffprobe: H.264 1088x1920 at
24 fps, 177 frames, 7.375 s, AAC 48 kHz stereo). The wall time is the
record's creation time (`ts`) to the file's last write.

The two defects of doc 04 §7 were found by reading the code and reproduced
without a render: each by tests in `tests/unit/test_chain_studio.py` that fail
on the old code, the upload names also by the copies in the LTX tree's input
folder (`output/seamlab/analysis/docs_evidence/comfyui_ltx_input_uploads_2026-10-25.txt`),
and the ↪ request also in the page, on an isolated copy of the studio with the
chain request intercepted.

## videolab evidence used for the VIDEOLAB copies

The fact-check of the `VIDEOLAB*.md` copies in `docs/research/` used the videolab tree at
3f6cabc, its configs, the safetensors headers of the H3 and LTX checkpoints
(read without loading weights), and 93 H3 lab run logs on the studio PC
(`D:\H3_LAB\renders\<render key>\run.json`, 2026-09-19..28), which are not
redistributed either. Each copy lists its corrections and evidence at the end.
