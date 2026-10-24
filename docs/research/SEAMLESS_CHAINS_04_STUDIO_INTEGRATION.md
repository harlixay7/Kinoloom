[Kinoloom](../../README.md) › [Docs](../README.md) › Chains in the studio

# Seamless chains in the studio: Video Studio, movie takes, catalog and tests

> **In short:** This doc explains how the seamless-chain engine is wired into
> the studio itself: Video Studio chains, the movie pipeline's continuous
> takes, the model catalog and the unit tests. It records the two Video Studio
> chains rendered on the GPU so far (one landscape, one portrait), what has not
> run on the GPU yet, and two code defects that were found and fixed.

> Provenance: the code in this repository (functions are named rather than
> line numbers, which drift), the unit test suite, the records and step
> manifests of Video Studio chains `25a6760b36f0` and `9de7e018337e`, the
> portrait take's file (probed with ffprobe), and one CPU-only check of the
> take splitter on a real lab take. The GPU measurements in docs 01 and 02
> were made with the lab harness (`tool/scripts/chain_lab.py`), which calls
> the same engine (`chain_engine`) but not `chain_studio`, `StudioRunner`, the
> GPU broker lease or the orchestrator.

This doc explains how the seamless-chain engine of
[SEAMLESS_CHAINS_01_LTX.md](SEAMLESS_CHAINS_01_LTX.md) and
[SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md](SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md)
is wired into the product: Video Studio chains, the movie pipeline's
continuous takes, the model catalog, and what the tests cover.

On this page:

- [1. Which models use the engine](#1-which-models-use-the-engine)
- [2. Video Studio chains](#2-video-studio-chains)
- [3. Movie pipeline: continuous takes](#3-movie-pipeline-continuous-takes)
- [4. Model catalog](#4-model-catalog)
- [5. Tests](#5-tests)
- [6. What has run on the GPU](#6-what-has-run-on-the-gpu)
- [7. Fixed defects](#7-fixed-defects)

## 1. Which models use the engine

`video_models.seamless_kind(entry)` (`tool/video_models.py`) decides per
registry entry:

- `"ltx"`: an LTX-2.5 entry with a `chain` workflow on `ComfyUI_LTX` (in the
  shipped registry: `ltx25_s6_cq`).
- `"h3"`: every H3 tier with a `refs` workflow.
- `None`: everything else (Wan, legacy LTX-Video, the other LTX tiers), which
  keeps the older tail-frame continuation.

`video_studio.seamless_engine(mid)` adds one setting,
`video_studio.seamless_chains` (default `true`). It is the single check used
by Video Studio chains, the movie's takes and the pickers' ⛓ marker.

`video_models.chain_has_finish(entry)` says whether a chain has a separate
finish pass after its draft take: always for LTX (the 1080p stage 2), and for
H3 only on tiers with a `refine_target` (the 1080p tiers). On a base-only H3
tier the decode-once take is the delivery.

## 2. Video Studio chains

**Request.** The Video Studio page sends `POST /api/videostudio/chain` with the
segment prompts, seed, segment length, size, start image (or the clip chosen
with "From last clip" / ↪), the reference cards and `draft_only`.
`video_studio.start_chain` checks the model and the start image, stores a
chain record and runs the chain in a background thread under one GPU broker
lease. The start image is a file name in a ComfyUI input folder, and one that
is in neither input folder is refused. With ↪ and no start image chosen, the
page sends the name of the tail frame that `continue_from` staged in both
input folders (section 7, item 2).

**Prompt check.** Before that, the page and the endpoint run `tool/prompt_check.py`
on the request. It plans the references from the same functions the chain uses
(`video_studio.chain_cast`, `chain_studio.ltx_reference_slots`) and refuses a
request with errors: an H3 picture label that is misspelled, points past the
attached pictures or is bound to the wrong name; a reference that cannot be
found; an LTX chain without a person reference or a start image (the MSR guide
needs `pic1`). Suggestions, such as a later segment without the continuity
line, never block. The Cast sheet button builds the brief for the prompt
writer from the same plan.

**Engine.** `chain_studio.run_chain` turns the request into a chain-engine
spec:

- **LTX**: link 1 renders the requested frames (snapped up to 8k+1). Every
  later link renders the 25 carried frames plus the requested frames minus one
  (`ltx_link_frames`), so each link adds the same span. Seeds are
  `seed + link index`. The draft renders at 960x544, or at 544x960 when the
  requested size is portrait (`ltx_draft_size`); the finish doubles it.
  References: the start image owns `pic1`, cast `pic1..3` move to `pic2..4`,
  and the background stays (`ltx_reference_slots`). Face close-ups of the cast
  portraits, lead first, fill the subject slots `pic1..4` that are still free.
  So with a start image and three cast references there is no room, and no
  close-up is added (`ltx_face_slots`, `LTX_FACE_REFS = True`). When a cast
  reference has a description, one cast sentence goes into every link prompt
  (`with_cast_line`, `LTX_CAST_LINE = True`). Order of work:
  1. every link's draft;
  2. the take's audio (more than one link);
  3. the draft take decoded once;
  4. the draft preview assembled and seam-QA'd;
  5. every link's 1080p finish;
  6. the final take decoded once, assembled and QA'd.
- **H3**: the tier's own recipe (UNet, steps, LoRAs, shifts, attention, refine
  sigmas and tiles) is read from the graph the studio builds for that tier
  (`h3_tier_params`). Link windows are 39 / 90 / 141 / 192 frames
  (`h3_window`, capped at 192); later links add the 39 carried frames. The
  cast fills `<Picture 1..N>` in slot order; link 1 opens on the start image or
  the lead cast still. Order of work:
  1. every link at base resolution;
  2. the base take decoded once (`decode_B0`, preceded by a server restart
     when the server still holds the link models, doc 02 §5);
  3. the preview assembled and QA'd;
  4. on 1080p tiers, the three-step refine (`refcond`, `refine`, `decode`).
- The H3 path does **not** use the cast sentence or the face close-ups; those
  are LTX-only.

**Preview first.** With the "Preview first" switch on a model that has a
finish pass, the chain stops after the draft take with status `draft_ready`.
The History card then shows **Finish 1080p**, which calls
`POST /api/videostudio/chain/{cid}/finish` (`video_studio.finish_chain`) and
renders the finish from the saved drafts.

**Records and History.** Chain records are saved as JSON under
`output/videostudio/chains/<id>.json` after every status change
(`chain_studio.save_record`), so they survive a studio restart. History
(`GET /api/videostudio/history`, `video_studio.history`) lists clips and
chains newest first, including the movie's takes (tagged "movie"). A record
left mid-run by an earlier studio process is shown as `interrupted`; nothing
resumes it. The Video Studio API takes no chain id (`ChainBody`,
`tool/routers/videostudio.py`), so a new request gets a new random id and
renders from scratch. The interrupted chain's finished steps stay on disk but
are not reused. Only a chain stopped at `draft_ready` can be continued
(Finish). Chains from before records existed are rebuilt from their files.
The player shows the draft and final takes with their seam QA, drift and the
per-link cast report (`face_drift`), which is reported and never gates.

**Progress and segment previews.** A chain record carries a step plan
(`chain_studio.chain_plan`): every step the chain will run, in order, each
with its state, start time and seconds, and whether an earlier run's result
was reused or a seam re-roll drew the link again (`_plan_mark`). The History
card and the player draw the progress bar from it. Each step's share of the
bar comes from step times measured in the showcase chains, rescaled by the
chain's own pace so far, and the History response carries the server's clock
so the elapsed times are not skewed by the browser's. On LTX chains, a
background thread encodes each link's own frames and sound (the carried frames
skipped) into a small mp4 after its draft and again after its finish
(`chain_assemble.link_preview`, x264 at below-normal priority), served at
`GET /api/videostudio/chain/{cid}/preview/{name}`. The player lists them as
segments, so each one can be watched before the whole take is decoded. The
runner queues its graphs without a ComfyUI client id, so ComfyUI broadcasts
the sampler's progress to the studio's progress relay.

**Reference cards.** Character and face cards have a "look" field (age, hair,
outfit, up to 400 characters). It becomes the cast sentence in **LTX** chains.
For H3 chains it is not rendered. The cast sheet gives it to the prompt writer
for both models. When an LTX chain's card has no look, the prompt check
suggests one only if the segments do not already describe that person in the
same words (a shared run of eight or more words).

**Step budget.** `StudioRunner.step_timeout`: a link step (`..._l<N>`) gets
the model's per-clip budget (`timeout_s`, 2,700 s when unset) once. A
whole-take step (LTX audio and take decode, H3 refcond, refine and decode)
gets it once per link. The reason: the H3 gate's 4-link 1080p refine took
3,653 s, more than one clip budget. A step that exceeds its budget is
interrupted on the server, and the chain fails.

## 3. Movie pipeline: continuous takes

A `CONTINUE` shot carries the camera on from the shot before it. When the
shot's model has the seamless-chain engine, the orchestrator renders a run of
them as one chain and cuts it back into shots (`tool/movie_takes.py`;
`Orchestrator._take_run`, `_render_take`, `_wait_take` and `_take_failed` in
`tool/orchestrator.py`). The chain itself is started through
`video_studio.start_chain`, the same call a Video Studio chain uses.

**Which shots join a take** (`movie_takes.plan_take`). Starting from the head
shot, the next shot joins while all of these hold:

- exactly one shot names the current last shot as its `prev_shot` (two shots
  continuing the same one is a fork and ends the take);
- it is `pending` or `queued`, is not a QA retake (`qa_stage` 0) and is not a
  forced text-to-video render;
- its continuity is `CONTINUE`, in the same scene and the same location (when
  both name one);
- it renders on the head shot's model;
- the take has fewer than `movie.take_max_shots` shots (default 4).

The head shot itself renders alone if it is a retake or t2v. If planning
raises an error, the shot renders alone (logged).

**Switches.** The project's `seamless_takes` column (Generate tab, default
on), settings `movie.seamless_takes` (default on) and
`video_studio.seamless_chains`. With any of them off, or on a model without
the engine, every shot renders alone as before.

**Rendering a take** (`_render_take`):

- One reference set and cast line for the whole take: the head shot with the
  characters of every shot in the take, first appearance order, highest tier,
  and the first power any shot shows (`take_shot`, `take_cast`, references in
  slot order).
- The opening frame is the previous shot's tail when the head shot continues
  one.
- Each shot keeps its own prompt (H3: `_h3_take_prompt`; LTX: the reference
  legend and the LTX prompt recipe; the negative comes from the head shot).
- The chain id is `sha1(project | model | shot ids)[:16]` (`take_id`), and
  shot seeds are `base_seed + index`; a retried take reuses the seed stored on
  its head row. The engine reuses a step only when its graph is unchanged.
  Every start uploads the references again, under names derived from their
  bytes (`renderer.upload_name`), so the uploads leave the graphs as they were
  and a re-run or retry with the same prompts skips the steps that already
  finished (section 7, item 1).
- The orchestrator waits on the chain while this process renders it. The
  per-step budgets end a slow chain, and an outer deadline of
  `8 x shots x timeout_s` only guards against a hung chain thread
  (`_wait_take`).

**Cutting the take** (`movie_takes.split_take`). The delivered take is cut at
its seam frames into one clip per shot: frame-exact video in one x264 pass
(CRF 14, preset slow, BT.709 tags kept) and the audio trimmed at the same
instants (AAC 256 kbit/s). Every clip's frame count is checked; any mismatch
raises an error. Each shot then continues as a normal render: its own render
record, provenance (take id, index, engine, seam QA verdict), tail frame, post
queue and QA.

**Failure handling** (`_take_failed`). A transient failure (connection
refused, timeout) re-queues the whole take without using a retry, up to 60
times 30 s apart. Any other failure counts one attempt on every shot of the
take and re-queues it whole with the same seeds. Once the tightest retry
budget among its shots is spent, all of them go to review.

**Checked on the CPU, on a real take.** The v4 gate's 1080p take
(`20261023_125729`, 481 frames, seams 121 / 241 / 361) was cut with
`split_take` and joined again with the assembler's concat list, as the movie
assembly does. Results:

- 481 frames back;
- PSNR 39.7-42.5 dB at the seams and 40.1-42.4 dB mid-shot (the CRF 14
  re-encode, the same at seams and elsewhere);
- the audio 962,000 samples in both with zero lag; around the seams the
  waveform error was at most 0.0002 against 0.0425 elsewhere (AAC re-encode).

This shows that cutting the take and joining it again adds no seam of its
own.

## 4. Model catalog

The pickers in Video Studio and on the project's Generate tab group the
registry (`video_models.catalog`; `tool/static/js/model-catalog.js`) into
LTX-2.5 · 22B, MiniMax H3 · 1080p, MiniMax H3 · drafts, Wan 2.2 · legacy and
Legacy. The recommended tier of each group comes first, and the chain-capable
tiers are marked ⛓. A registry entry may carry an optional `catalog` block
(`group`, `label`, `detail`, `rank`, `recommended`); without one it falls into
its family's group under its display name. No model id was renamed.
`GET /api/videostudio/models` returns the groups and each model's `catalog`,
`seamless` and `chain_finish` fields. New projects default to `ltx25_s6_cq`
(`video_models.DEFAULT_MODEL`, used by `tool/routers/projects.py`), and a
project that picks no model renders on it. The legacy Wan 2.2 entry is never
chosen on its own.

## 5. Tests

The full hermetic suite (`tool\.venv\Scripts\python.exe -m pytest tests`, run
from the repository root with `CUDA_VISIBLE_DEVICES=-1`) passes with 710 tests
(`output/seamlab/analysis/docs_evidence/pytest_2026-10-25_release.txt`). The
chain-related unit test files and their test functions (a parametrized
function counts once here, so pytest collects more cases than this column
shows):

| File | Tests | What they cover |
|---|---|---|
| `tests/unit/test_studio_chain_core.py` | 34 | the chain nodes: H3 token grid, carry prefix and take concat, refine chunk planning, LTX latent and audio carry, take latent join and blend, decode windows, carry restore, seam AdaIN (block, path, phase period) |
| `tests/unit/test_chain_engine.py` | 31 | LTX and H3 spec checks and graph wiring, carry recipe, stage-2 options, the CQ default, text-encoder release, step digests and input tracking, resume and output checks, re-roll, H3 restarts and the three-step refine, the lab's request mode, the graph validator |
| `tests/unit/test_chain_studio.py` | 30 | link frames, portrait draft size, reference policy, face slots, cast line, H3 tier recipe, records, the step plan (order, timing, reused and re-drawn steps, draft-only then Finish), segment previews, runner budgets and restarts, legacy resume, content-named uploads (local and worker) and a re-run that reuses every step, the ↪ start image of a chain |
| `tests/unit/test_prompt_check.py` | 24 | the prompt check and the cast sheet: H3 picture labels and name bindings, what each model receives for a clip or a chain, openings, continuity lines and looks, the endpoints that refuse a request |
| `tests/unit/test_chain_assemble.py` | 11 | timeline planning, blend weights, audio gains across joins, end-to-end assembly, look path, segment previews |
| `tests/unit/test_video_catalog.py` | 10 | catalog groups, `seamless_kind`, `chain_finish`, the `seamless_chains` switch |
| `tests/unit/test_movie_takes.py` | 9 | take planning, take id, cast merge, shot bounds, the splitter |
| `tests/unit/test_seam_qa.py` | 8 | seam steps against lighting moves, carry-end check, audio onset rule, window pop, overlap fidelity |
| `tests/unit/test_orchestrator_takes.py` | 7 | take render, failure and transient retries, waiting, the switches |
| `tests/unit/test_video_history.py` | 7 | History list, interrupted records, GPU health route, the segment preview route |
| `tests/unit/test_continuity_chain.py` | 6 | the legacy tail-frame chain path |
| `tests/unit/test_face_drift.py` | 2 | the cast report |

The tests run the engine against fake ComfyUI clients and fake broker leases.
`test_split_take_cuts_frame_exact_clips_with_their_sound` needs `ffmpeg` on
`PATH` and PyAV, and is skipped without them.

## 6. What has run on the GPU

**A Video Studio chain, end to end.** Chain `25a6760b36f0` went through the
whole studio path: the Video Studio page, `video_studio.start_chain` under one
GPU broker lease, then `chain_studio.run_chain` and `StudioRunner` on the
studio's LTX instance (8189). Record:
`output/videostudio/chains/25a6760b36f0.json`; step manifest:
`ComfyUI_LTX/output/studio_chain/studio_25a6760b36f0/manifest.json`.

| | |
|---|---|
| Model | `ltx25_s6_cq`, image to video, seed 721093809 |
| References | the start image, one cast portrait and its face close-up (`pic1`-`pic3`) |
| Links | 97 / 121 / 121 frames, joins at frames 97 and 193 |
| Take | 289 frames, 12.04 s at 1920x1088 and 24 fps, with sound |
| Step time | 1,514.7 s in all: drafts 214.5 / 166.0 / 176.6 s, take audio 5.1 s, draft take 35.1 s, 1080p finishes 181.0 / 419.6 / 186.5 s, final take 130.3 s |
| Wall time | about 29 min from the record's creation to its last write |

Link 2's finish took 419.6 s, against 181.0 and 186.5 s for the other two;
the records don't show why.

Seam QA of the 1080p take:

| Boundary | Frame | Pop | dSat | Detail | Verdict |
|---|---|---|---|---|---|
| carry end, link 2 | 73 | 0.973x | +0.25 | 0.964 | **fail: audio level** (2.18 dB step) |
| junction 1-2 | 97 | 1.059x | -0.87 | 0.761 | **fail: detail** |
| carry end, link 3 | 169 | 0.955x | +1.73 | 1.001 | pass |
| junction 2-3 | 193 | 1.107x | +1.51 | 1.064 | **fail: audio level** (4.00 dB step) |

Every boundary passes the pop gate. Take verdict: FAIL. Besides the three
flags above, it fails two take-wide gates:

- **drift:** from link 1 to the last segment, lightness rises 30.91 % (the
  worst measure) and high-frequency detail falls 22.91 %; saturation (7.13 %)
  is also past the 5 % gate;
- **overlap:** PSNR of the carried frames in the per-link decodes, minimum
  22.58 / 20.48 dB against the 32 dB gate (the soft pin, as in the showcase
  takes).

The draft take failed the same way (detail 0.795 at frame 97, the same two
audio flags, drift 28.18 %).

The cast report measured the one cast member at identity 0.170 / 0.248 /
0.375 per link, and an apparent age of 24.5 / 30.7 / 38.9 against 31 for her
portrait: an upward trend like the LTX cliff walk's ([SHOWCASE.md](../SHOWCASE.md)
§13), and larger. Link 1 opens on a full-length shot, and the face model found
her in 6 of its sampled frames there, against 11 and 16 in links 2 and 3.

**A portrait chain, end to end.** Chain `9de7e018337e` took the same path at a
portrait size. Record: `output/videostudio/chains/9de7e018337e.json`; step
manifest: `ComfyUI_LTX/output/studio_chain/studio_9de7e018337e/manifest.json`.

| | |
|---|---|
| Model | `ltx25_s6_cq`, image to video, seed 1669365672 |
| References | the start image, one cast portrait and its face close-up (`pic1`-`pic3`) |
| Size | drafts at 544x960, finishes at 1088x1920 (`ltx_draft_size`) |
| Links | 89 / 113 frames, join at frame 89 |
| Take | 177 frames, 7.375 s at 1088x1920 and 24 fps, H.264 with AAC 48 kHz stereo |
| Step time | 905.9 s in all: drafts 200.9 / 170.3 s, take audio 5.1 s, draft take 20.0 s and its assembly 55.8 s, finishes 155.6 / 171.6 s, final take 65.1 s and its assembly 61.5 s |
| Wall time | 915.6 s (about 15 min) from the record's creation to its last write |

Seam QA of the 1088x1920 take:

| Boundary | Frame | Pop | dSat | Detail | Verdict |
|---|---|---|---|---|---|
| carry end, link 2 | 65 | 1.014x | +0.75 | 1.035 | pass (audio step 1.20 dB) |
| junction 1-2 | 89 | 0.999x | +0.57 | 0.970 | pass (audio step 1.34 dB) |

Both boundaries pass. Take verdict: FAIL, on the two take-wide gates:

- **drift:** from link 1 to link 2, high-frequency detail falls 32.61 % (the
  worst measure) and lightness rises 6.30 %, against the 5 % gate;
- **overlap:** PSNR of the carried frames, minimum 19.23 dB and mean
  27.66 dB against the 32 dB gate (the soft pin, as above).

The draft take failed the same two gates (drift 30.47 %, overlap minimum
20.19 dB) with both boundaries passing. The cast report measured the cast
member at identity 0.334 / 0.461 per link and an apparent age of 27.5 / 31.3
against 31 for her portrait, with 15 faces found in each link.

**The engine through the lab harness.** Every chain measured in docs 01 and
02, and every chain in the showcase, ran through the lab harness. For the
showcase chains its `--request` mode built the chain spec with the same
functions `chain_studio.run_chain` uses; the runner and the instance
management were the lab's ([SHOWCASE.md](../SHOWCASE.md) §2). The two H3
showcase chains ran the restart before `decode_B0` and the ones before the
refine and its decode, through the lab's own restart path.

**Not run on the GPU yet:**

- an H3 chain through Video Studio, and with it the H3 server restart under a
  studio lease (`gpu_broker.restart_leased`), which is unit-tested only;
- Preview first followed by Finish 1080p from History.

**Other limits:**

- `TAKE_MAX_SHOTS = 4` was chosen from the gate chains (481 frames for 4 LTX
  links of the gate's length; 651 frames and about 2 h 40 min for 4 H3 links
  of 192 frames). A movie take's real length depends on its shots' frame
  counts. The comment in `tool/movie_takes.py` ("4 links = 20 s") holds for
  the LTX gate only.
- An interrupted Video Studio chain cannot be resumed (see section 2). A movie
  take is started again under its stable chain id when it is re-queued or
  retried, and reuses its finished steps (section 3). Finish 1080p from
  History reuses the drafts, because it continues the saved chain instead of
  starting a new one.

## 7. Fixed defects

Two defects in the studio's chain paths were found by reading the code and
then fixed. Each fix has tests in `tests/unit/test_chain_studio.py` that fail
on the old code.

1. **A re-queued or retried movie take rendered every step again.**
   `renderer.ComfyClient.upload_image` named every upload
   `kf_<8 random hex>.png`, and `chain_studio.run_chain` uploads the
   references again on every start (its `up` helper caches names within one
   run only). The names go into the graphs' `LoadImage` nodes, and a step is
   reused only when its graph digest matches (`chain_engine.graph_digest`,
   `_Manifest.done`), so a re-run take got new digests and rendered
   everything again. The unit tests' fake client returned the file's own name,
   which hid this. The random names also piled up copies: the studio PC's
   `ComfyUI_LTX/input` held 495 uploaded stills with 139 distinct contents,
   one of them 58 times
   (`output/seamlab/analysis/docs_evidence/comfyui_ltx_input_uploads_2026-10-25.txt`).
   Uploads are now named after their bytes (`renderer.upload_name`: `kf_`
   plus the first 16 hex digits of the SHA-256), on the local ComfyUI and on
   worker PCs (`RemoteWorkerClient.upload_image`) alike. The tests upload
   through the real client to a stand-in for ComfyUI's `/upload/image` and
   start the same 2-link chain twice: the first run renders 7 steps, the
   second none.
2. **Video Studio's "From last clip" (↪) chain request was refused.** For a
   chain the page sent `start_image: VS_CONT.path`, the absolute path of the
   tail frame's thumbnail copy, and `start_chain` accepts only a file name in
   a ComfyUI input folder ("start_image must be a filename in a ComfyUI input
   folder"). Single clips were not affected: they send the tail as the
   separate `continuation` field. The page now sends `VS_CONT.input_filename`,
   the copy that `continue_from` stages in both input folders. `start_chain`
   also refuses a start image that is in neither input folder; before, the
   chain thread logged a warning and rendered without its opening frame. In
   the page itself (an isolated copy of the studio, with the chain request
   intercepted before it reached the server), the request carried the
   absolute path before the fix and the staged file name after it.
