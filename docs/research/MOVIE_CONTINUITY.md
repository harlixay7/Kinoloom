[Kinoloom](../../README.md) › [Docs](../README.md) › Movie continuity

# Movie continuity: take length, opening frames, the cast check and a scene

> **In short:** The measurements behind three of the movie pipeline's
> defaults: how many shots a movie's LTX-2.5 take gets (6, a setting), how an
> opening frame is made from the cast's pictures, and the identity limit of
> the movie QA (0.25, for faces large enough to judge); then one five-shot
> scene rendered through the movie path with those defaults. Every number
> comes from a saved record on one 8 GB RTX 3070, at few seeds (one for the
> take length, two to six for the opening frames); the records are not
> redistributed. How the pipeline uses these values is in
> [PIPELINE_AND_CONTINUITY.md](../PIPELINE_AND_CONTINUITY.md) §4.4 and §5.

The records are in
`output/seamlab/analysis/docs_evidence/movie_continuity_2026-10-28/`
(`take_length/`, `key_image_ab/`, `qa_calibration/`, `e2e_scene/`), each with
the script that measured it.

## 1. How many shots one LTX-2.5 take holds

**Run.** The LTX-2.5 cliff-walk take of the showcase (`show_ltx_cliff`, four
links) was extended with four more 4 s links through the unchanged chain
engine: chain `lab_ltx_cliff_x8`, model `ltx25_s6_cq`, seed 2410241, start
image `cliff_open.png`, one cast member (`maren_portrait.png`), a 97-frame
first link, 25 carry frames and 96 new frames per later link: 769 frames,
32.0 s. Links 1-4 are the showcase take's links, reused after the engine's
digest check proved that the current code builds byte-identical graphs (8 of
8 steps match; their overlap PSNR in the new take's QA equals the old take's:
21.74 / 29.78 / 24.36 dB at the lowest). Links 5-8 rendered fresh on the lab
ComfyUI: drafts 199 / 133 / 133 / about 130 s, finishes about 190 s each,
the final take decode 352.6 s. Record: `take_length/W0_RECORD.md`, the run's
`summary.json` and `qa_final_F0_take` in
`output/seamlab/runs/20261028_133059_ltx_req_lab_ltx_cliff_x8`, the log
`take_length/run.log`.

**Identity and apparent age per link** (the `face_drift` method on the final
take: InsightFace buffalo_l on the CPU, every 6th frame, longer side 960 px,
faces of 40 px and more, against `maren_portrait.png`, whose own apparent age
is 27):

| Link | Frames | Identity, mean (lowest) | Apparent age | Face height |
|---|---|---|---|---|
| 1 | 0-96 | 0.306 (0.22) | 24.8 | 131 px |
| 2 | 97-192 | 0.412 (0.34) | 28.6 | 165 px |
| 3 | 193-288 | 0.413 (0.37) | 31.9 | 225 px |
| 4 | 289-384 | 0.352 (0.26) | 30.7 | 216 px |
| 5 | 385-480 | 0.374 (0.30) | 26.7 | 114 px |
| 6 | 481-576 | 0.508 (0.36) | 37.6 | 230 px |
| 7 | 577-672 | 0.443 (0.35) | 32.1 | 323 px |
| 8 | 673-768 | 0.378 (0.25) | 29.1 | 176 px |

Links 1-4 average 0.371 and links 5-8 0.426. The 4-link take measured the
same way gives 0.306 / 0.412 / 0.412 / 0.344 and ages 24.8 / 28.6 / 31.9 /
32.4 (links 3 and 4 differ slightly because the take is decoded in streamed
windows that depend on its length). Identity follows the face's
size, not the link number, and the apparent age moves with expression and
framing rather than steadily up.

**Seam QA** (the final take, `chain_lab` thresholds):
- Links 1-4 (6 seams): FAIL at frame 193 (detail 0.759) and 289 (detail
  0.709). The 4-link take fails at 193 too (0.758) and passes 289 (0.872,
  the length-dependent decode).
- Links 5-8 (8 seams): 361, 385 and 457 pass; 481 FAIL (detail 0.700); 553
  passes; 577 FAIL (saturation +2.34, detail 0.759); 649 passes; 673 FAIL (pop
  1.349, detail 0.809).
- The largest pop and saturation step in links 1-4 are 1.16 and +1.15.
- Take drift from link 1 to 8: lightness 15.19 %, saturation -13.33 %, detail
  (Laplacian) -26.97 %, high frequencies -13.93 % (the 4-link take: 9.06 /
  -12.05 / -48.76 / -17.65 %).
- The seam strips at 577 and 673 show no visible jump. The frame sheet per
  link (`take_length/link_sheet.jpg`) keeps the coat, sweater, scarf, hair,
  path and light.

**Decision.** The rule set before the run: the take length is the last link
count where identity stays within 0.05 of links 1-4's mean and no new kind of
seam failure appears. Identity passes in the four links added: their lowest
mean, 0.374 (link 5), is above 0.321. (Link 1 of the showcase take is lower,
0.306, with one of the smallest faces.) Links 5 and 6 add no failure beyond
what links 1-4 show; link 7 adds a saturation step and link 8 a pop larger
than any in links 1-4. So `movie.ltx_take_max_shots` is **6**. It is one take
of one person in one place, and links 7 and 8 showed no visible loss, so the
value is a setting (and a project's **Shots per take**), not a limit of the
engine.

## 2. Opening frames made from the cast's pictures

**Run.** The opening frames of three shots of the first test movie, made
again from their stored opening-image prompts with Qwen-Image 2.1 fast (8
steps, cfg 1.0, 1344x768) on a lab ComfyUI: s01_002 (medium-wide, the lead
stepping off a boat), s01_005 (close-up) and s01_006 (medium). One person,
the lead: her primary still, its face close-up and the place picture. Five
arms:

| Arm | References | Look text |
|---|---|---|
| A | still, place | the story AI's look ("a weathered exile ...") |
| B | face close-up, place, still | her Casting look and the "The people:" line |
| C | face close-up, place, still | the story AI's look |
| D | still, place | her Casting look and the "The people:" line |
| F | face close-up, place | her Casting look and the "The people:" line |

Run 1: A-D on the three shots at two seeds (24 frames). Run 2: F on the
three shots at the same seeds, and B, D and F on s01_002 at four more seeds
(18 frames). Each frame is scored with its likeness to the primary still
(`face_drift.likeness`: ArcFace, the best face) and the faces of 40 px or
more InsightFace finds in it. Records: `key_image_ab/` (`ab.py`, `ab2.py`,
both runs' records and frames, `ab_summary.py` and `ab_summary.json`, the
contact sheets).

| Arm | Frames | Likeness, mean (lowest) | s01_002 | s01_005 | s01_006 | Lead drawn more than once |
|---|---|---|---|---|---|---|
| A | 6 | 0.355 (0.193) | 0.273 | 0.398 | 0.392 | 0 |
| B | 10 | 0.469 (0.041) | 0.297 | 0.786 | 0.670 | 2 |
| C | 6 | 0.421 (0.136) | 0.275 | 0.623 | 0.366 | 0 |
| D | 10 | 0.519 (0.314) | 0.410 | 0.651 | 0.717 | 0 |
| F | 10 | 0.458 (0.109) | 0.296 | 0.703 | 0.698 | 0 |

The shot columns are means: s01_002 has 6 frames for B, D and F and 2 for A
and C; s01_005 and s01_006 have 2 each. The medium-wide shot scores lowest
in every arm, likely because its face is small (as with the QA's faces, §3;
the face sizes are not recorded in this test).

Read by eye on the sheets:
- A and C draw the story AI's cloak instead of her outfit.
- B and D keep the still's outfit (a strappy sequined dress) in every frame;
  F, without the still, draws another dress in all 10.
- B draws her more than once in 2 of its 10 frames (InsightFace counts 3 and
  2 faces). Given the face close-up and the still as two pictures of one
  person, that happened in 2 of 16 frames (B and C); given one picture, in
  none of 26 (A, D and F).

**Decision.** One picture per person: the still the video model gets (arm D).
It has the highest mean and the highest lowest score (the highest mean also on
the six frames per arm at run 1's seeds: D 0.563, B 0.546, F 0.540, C 0.421, A
0.355), keeps the outfit in every frame and never draws her twice. The face
close-up still scores the candidates (`opening_frames._refs`).

D's frames scored 0.314 to 0.788. Of the 7 frames under 0.25, 6 had lost her
look: the story AI's cloak in 4 (A and C), her drawn more than once in 1
(B, 0.041) and another dress in 1 (F, 0.109); the seventh (B, 0.248) kept
it. So `movie.key_image_min_likeness` is **0.25**: when the better of two
candidates is under it, two more are made, and a frame still under it is
marked weak. At 0.35 the better of D's first two s01_002 frames (0.329),
which kept her look, would have cost two more candidates (D's next two
seeds there scored 0.491 and 0.491). Both values are settings
(`movie.key_image_candidates`, default 2).

The likeness judges the face only. By eye, 18 of the 35 frames at 0.25 or
more had lost her look as well: the cloak in 8, another dress in 9, her
drawn more than once in 1. What keeps the outfit is the still among the
references, not the floor.

## 3. The movie QA's identity limit

The QA judges a rendered shot by the best face in its last 5 frames against
the cast's portraits (`qa_director.face_similarity`, InsightFace buffalo_l on
the CPU, the first face of each portrait). It was calibrated on two groups,
each shot scored as the QA sees it (a take cut at its seams into its links):
- **Approved:** the 17 links of the 3 LTX-2.5 showcase takes (cliff walk,
  letter, lantern; against `cast_final.json`'s cast) and the 2 Video Studio
  chains `25a6760b36f0` and `9de7e018337e` (against their `cast_faces`).
- **Lost the cast:** the 6 LTX-2.5 clips of the first test movie, rendered
  before this work, in which the lead's look changed from shot to shot
  (against the lead's primary picture).

Records: `qa_calibration/qcal2.py` and `qcal2.json` (every face with its
height), `qcal.json` (the best face per link).

| Group | Link | Best face | Its height (share of the short side) |
|---|---|---|---|
| approved | cliff walk 1-4 | 0.306 / 0.444 / 0.398 / 0.351 | 0.28 / 0.27 / 0.54 / 0.20 |
| approved | letter 1-4 | 0.443 / 0.483 / 0.448 / 0.514 | 0.35 / 0.52 / 0.36 / 0.24 |
| approved | lantern 1-4 | 0.606 / 0.537 / 0.489 / 0.414 | 0.36 / 0.46 / 0.54 / 0.20 |
| approved | `25a6760b36f0` 1-3 | 0.106 / 0.341 / 0.377 | 0.09 / 0.26 / 0.45 |
| approved | `9de7e018337e` 1-2 | 0.427 / 0.405 | 0.26 / 0.48 |
| lost the cast | 6 clips | 0.008 / 0.137 / 0.015 / 0.277 / 0.043 / 0.025 | 0.05 / 0.09 / 0.08 / 0.50 / 0.04 / 0.12 |

The one low approved link (0.106) is a full-figure shot whose face is 99 px
tall; the person is the same, the face too small for ArcFace to tell. Most of
the clips that lost the cast are small faces too. So:
- a face smaller than **0.12 of the frame's short side** (`qa.cast_face_size`,
  about 130 px at 1088 px) is not judged. On the approved side that leaves 16
  links, which scored 0.306 to 0.606;
- a judged face under **0.25** (`qa.cast_face_min`) sends the shot to review.
  No approved link is under it. Of the 6 clips that lost the cast, two have a
  face large enough to judge: the one at 0.025 fails and the large face at
  0.277 passes. The other four are too small to judge.

The best face against any of the portraits counts, so with several people on
screen one face that matches passes the shot, and a large face of someone
without a portrait (an extra) is judged like a cast face. The limit is a
safety net for a face that is clearly someone else, not a measure of quality:
the lowest approved face (0.306) is 0.056 above it, one face that had lost the
cast (0.277) is above it, and most of those clips were wide shots that it
cannot judge at all. The story rules keep faces at medium range or closer for
dialogue, emotion and hero moments
([PIPELINE_AND_CONTINUITY.md](../PIPELINE_AND_CONTINUITY.md) §2.3), where the
check can see them. Both values are settings.

**CLIP.** The QA's prompt check (`clip_alignment`,
`openai/clip-vit-base-patch32` on the middle frame, the prompt cut at CLIP's
77 tokens) was run on the same 17 approved links with the prompts they
rendered with (`qa_calibration/clipcal.py`, `clipcal.json`): 0.222 to 0.379, 8
links under the Wan limit of 0.28 (`qa.clip_score_min`). The same frames
against the next link's prompt score 0.215 to 0.399. Inside a take the check
cannot tell a link's prompt from its neighbour's, so for LTX-2.5 and H3 shots
the score is recorded and never fails a shot.

## 4. A scene through the movie path

**Run.** A separate copy of the studio, with its own data and one project: one
person (the cliff walk's `maren_portrait.png`), one place (a cliff-top path,
its people-free picture made with Qwen-Image 2.1) and five shots in one scene:

| Shot | Continuity | Size, camera | Action |
|---|---|---|---|
| s01_001 | CUT | medium, tracking | she walks along the path |
| s01_002 | CONTINUE | medium, tracking | she slows and stops at a fence post |
| s01_003 | CONTINUE | medium close-up, push in | the wind lifts her scarf, her eyes close |
| s01_004 | MATCH_ACTION | close-up, static | from the sea side, she opens her eyes |
| s01_005 | CONTINUE | close-up, pull back | a faint smile, she turns back to the path |

The studio's own code ran every step outside its web server: the chat brief,
the prompts written from it as a chat AI would and imported, the opening
frames, `Orchestrator._render_shot` for each take and the post worker's QA,
then the assembler. The image and video servers were swapped by hand (in the
studio the GPU broker swaps them), and nothing was retaken. Record:
`e2e_scene/` (scripts, briefs, replies, frames, takes with their chain
records, logs, `e2e_record.json`, the scene and its sheets).

- **Planned:** two units, the take s01_001-003 and the take s01_004-005.
- **Opening frames:** s01_001 from her still and the place, likeness 0.698
  (the better of 2); s01_004 from her still, the last frame of s01_003 and
  the place, likeness 0.537 (the better of 2).
- **Takes:** 361 frames in 2165 s and 241 frames in 1169 s, each from a cold
  server; take 2 opened as a match cut on its frame. All 6 seams pass the
  seam QA. Both takes fail its whole-take measures: the drift from the
  first link to the last (36.13 % and 7.58 %, against 5 %) and the lowest
  overlap PSNR (21.21 and 29.8 dB, against 32 dB). The 4-link take of §1
  fails them too.
- **QA:** all five shots passed, with faces of 0.442 / 0.538 / 0.446 / 0.436
  / 0.413 and CLIP scores (recorded only) of 0.377 / 0.358 / 0.308 / 0.323 /
  0.240. The takes' own face reports give her identity per shot as 0.363 /
  0.490 / 0.406 / 0.434 / 0.358.
- **The scene:** 602 frames, 25.1 s.

Read by eye on the sheets: one woman, one outfit and one light through all
five shots; no visible jump at the seams (`take1_seams.png`,
`take2_seams.png`); the match cut carries her closed-eyes moment on from a
new, frontal angle. Two things are off. The match cut was asked for from the
sea side, yet its frame shows the sea behind her too, so the camera's side
is not clear. And at the end of take 1 (eyes closed, low side light) and in
take 2's close-ups her face reads older than the portrait at times. The
apparent ages say so for s01_003 (43.8) and s01_005 (38.1), not for s01_004
(28.5); s01_001 and s01_002 are 22.7 and 27.9, the portrait 27.
