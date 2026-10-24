[Kinoloom](../../README.md) › [Docs](../README.md) › LTX chains

# LTX-2.5 seamless chains: carry, look matching, stage-2 fidelity and cast age

> **In short:** This doc covers how LTX-2.5 chains are joined so that a long
> take shows no cut between its links. It goes through what was wrong with the
> old continuation, what each fix changed on one test chain, how the 1080p
> finish was made to keep the draft, and what happened to the cast's apparent
> age from link to link. Most rows are one render at one seed on one 8 GB card.

> Provenance: every number below comes from the chain lab's run records on the
> studio PC (`output/seamlab/runs/<run id>/`: `summary.json`, `lab.json`,
> per-step `result.json` and `telemetry.jsonl`) or from the analysis outputs
> listed in [SEAMLESS_CHAINS_EVIDENCE_INDEX.md](SEAMLESS_CHAINS_EVIDENCE_INDEX.md).
> `output/` is gitignored, so the records and the media are not in the
> repository. All runs were made on one machine (RTX 3070 8 GB, 64 GB RAM,
> Windows 11), on one gate chain, mostly at one seed per link.

This doc covers the LTX-2.5 side of the seamless-chain engine
(`tool/chain_engine.py`, `tool/chain_graphs.py`, `tool/chain_assemble.py`,
`tool/seam_qa.py`, nodes in `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/`).
It explains what was wrong with the earlier continuation, what each mechanism
changed on the gate chain, how the stage-2 (1080p) finish was made to keep the
draft, and how far the cast's aging from link to link came down (and where it
did not). The H3 side, memory and the blue screens are in
[SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md](SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md).

On this page:

- [1. The problem and the gate chain](#1-the-problem-and-the-gate-chain)
- [2. The carry, mechanism by mechanism](#2-the-carry-mechanism-by-mechanism)
- [3. One decode for the whole take](#3-one-decode-for-the-whole-take)
- [4. Seam look matching along a path](#4-seam-look-matching-along-a-path)
- [5. Re-rolling a bad carry end](#5-re-rolling-a-bad-carry-end)
- [6. Stage 2 (the 1080p finish): keep the draft](#6-stage-2-the-1080p-finish-keep-the-draft)
- [7. Finish speed and the 1080p cliff](#7-finish-speed-and-the-1080p-cliff)
- [8. Memory: the text encoder leaves before the sampler](#8-memory-the-text-encoder-leaves-before-the-sampler)
- [9. Cast age and identity](#9-cast-age-and-identity)
- [10. A change inside the first free latent: the pixel look path](#10-a-change-inside-the-first-free-latent-the-pixel-look-path)
- [11. What the delivered takes still fail](#11-what-the-delivered-takes-still-fail)
- [12. Limits](#12-limits)

## 1. The problem and the gate chain

The earlier Video Studio continuation rendered each link on its own, then
continued from the previous link's mp4. Its last frames were decoded,
re-encoded and pinned as a hard prefix, and the links were joined with a pixel
crossfade. The takes showed visible micro-cuts, a contrast change at each
cut, and characters getting older from link to link.

The gate chain is an earlier Video Studio chain, `bcb2e4a7`: 4 links (121, 145, 145, 145 frames at
24 fps; 25 carried frames per later link), 481 frames in the take, the
original seeds (546606451-546606454) and references read from the old mp4's
metadata. The story pushes in from a waist-up two-shot to a tight close-up, so
the look changes across the take by design. Seams are judged locally, and
whole-take drift is content.

The old engine's 1080p take of this chain (`baselines.json`,
`chain_bcb2e4a7_final.mp4`):

| Seam | pop | saturation step | lightness step | Verdict |
|---|---|---|---|---|
| 121 | 5.40x | +10.4 | -2.67 | FAIL pop, saturation, lightness, contrast |
| 241 | 1.80x | +13.4 | -3.68 | FAIL pop, saturation, lightness, contrast, detail |
| 361 | 2.57x | +7.1 | -0.56 | FAIL pop, saturation, detail |

### Seam QA (`tool/seam_qa.py`)

| Check | Definition | Gate |
|---|---|---|
| pop | mean absolute frame difference at the seam / median local difference (480x272 RGB thumbnail, all channels) | <= 1.30 |
| saturation | 6-frame mean HSV saturation step (0-255) | fails only when the step exceeds both 2.0 and 2.5x the median of the same step measured at +-12..24 frames |
| lightness | 6-frame mean CIELAB L* step (0-100) | 1.5, same local rule |
| contrast | 6-frame mean L* standard-deviation step | 1.5, same local rule |
| detail | Laplacian variance, 4 frames after / 4 before | 0.85-1.15, widened by the local ratio |
| audio | RMS level step in 100 ms windows; sample-jump click ratio | fails above both 1.5 dB and 2x the local step, judged only above -45 dBFS and not for a sound starting from or ending into silence below -60 dBFS (an onset); click <= 3.0 |
| drift | link 1 -> last link change of L*, contrast, saturation and the hf texture measure (the Laplacian variance is reported, not gated) | <= 5 % |
| overlap PSNR | carried frames re-rendered vs the originals | >= 32 dB |

The absolute thresholds were set from an analysis of the earlier
chains (pop 1.75-4.27x, saturation steps of +4.2 to +22.9;
`analysis/session_2026-10-23/chain_analysis_2026-10-22/`, copied there from the
session's scratch folder). Measured with the shipped QA, the old
engine's takes in `baselines.json` show pop 1.80-5.40x and saturation steps of
+7.1 to +13.4 (LTX `bcb2e4a7`), and pop 2.16-3.06x and +10.2 to +18.8 (H3
`5c9b1e8b`). They are engineering gates for this one machine and chain, not
perceptual thresholds. Motion ratio (optical flow before/after) is reported,
not gated.

The judging rules changed during the work. The local rule for the look
steps (a step fails only above both the gate and 2.5x the local median) was
added between 00:49 and 05:06 (run ids carry their start time), and the audio
onset exemption at about 13:25. Some seams pass only because of the local rule: the v4 1080p seam 121
(saturation +2.85), and the v2 S5 seam 241 flash of section 4 (saturation
+9.65 against a local limit of 9.67).

## 2. The carry, mechanism by mechanism

Each change below was measured on link 2 of the gate (seam 121) with the same
seed, at draft resolution (960x544). Several checks were decode-only: the
latent is re-processed and decoded again, ~13-30 s instead of a ~4 min
render.

| Step (run) | pop | saturation | lightness | detail | motion ratio | Seam 121 |
|---|---|---|---|---|---|---|
| Hard pin of the re-encoded tail, crossfade 8 (`20261023_000513`, base) | 3.18x | +1.45 | -3.57 | 0.82 | 5.16x | FAIL pop, lightness, detail |
| Soft pin ramp 1.0/0.8/0.6/0.4 (R1) | 1.16x | -4.34 | -0.52 | 1.17 | 1.23x | FAIL saturation, detail |
| Flat soft pin 0.5 (L5) | 1.27x | -4.86 | -0.15 | 1.14 | 1.18x | FAIL saturation, contrast |
| R1 + carry restore (`20261023_002354`, R1cr) | 1.10x | +3.72 | -1.09 | 1.43 | 1.24x | FAIL saturation, detail |
| R1 + restore + latent carry (`20261023_003835`, LC) | 1.11x | +5.73 | -1.12 | 1.53 | 1.12x | FAIL saturation, detail |
| LC + seam AdaIN 1.0 over 4 latents (`20261023_004944`, A) | 0.92x | +0.35 | +0.55 | 0.97 | 1.22x | pass |

Rows 1-5 are pixel-crossfade assemblies (crossfade 8). Row A was measured on a
decode-once take joined at latent 2 (section 3). Its like-for-like baseline is
the latent carry on the same kind of take (`20261023_004427`, LC_j2:
saturation +4.84, detail 1.32, FAIL).

What each step does and why it stayed:

- **Hard pin -> soft pin.** Pinning the four carried latents at full strength
  made the first free latent jump: lightness -3.6, five times the motion, and
  ragged hair edges on the frames. A ramp (1.0, 0.8, 0.6, 0.4) spreads the
  join. Pop fell from 3.18x to 1.16x, and the motion ratio from 5.2x to 1.2x.
  Default: `LtxChainSpec.pin_strengths = cg.LTX_PIN_RAMP`.
- **Carry restore** (`StudioCarryRestore`). The model returns soft-pinned
  latents with a per-channel gain of about 1.06-1.09 (about +10 saturation
  over the carry; printed by a session script, not saved). That shows as the
  saturation dip at the carry end (-4.3). The restore regresses that gain out
  and keeps the encoded carry plus the residual. It changes the sign of the
  step rather than removing it: the first free latent is still hotter than the
  carry (+3.7).
- **Latent carry** (`StudioLtxLatentCarry`). Re-encoding the decoded tail
  costs a VAE round trip (decode to pixels and encode back) per link. It was
  measured once (decode-only check `20261023_003452`; the values were
  printed, not saved in its summary). The re-encoded carry latents differed
  from the originals by RMSE 0.24-0.42 (latent std 0.92) and decoded +1.7
  saturation and sharper. Repeated every link, that shift is expected to
  accumulate. The hard-pin take's rising saturation steps (+1.5, +6.6, +9.1 at
  seams 121 / 241 / 361, `20261023_000522`) fit that, but the accumulation was
  not isolated. The latent carry keeps latent 0 encoded from one frame and
  copies the previous link's last 3 latents exactly. At seam 121 alone it did
  not help (+5.7 saturation). It stays to remove the round trip, not for this
  seam.
- **Seam AdaIN** (`StudioSeamAdain`). The first free latent still bursts (+5
  to +6 saturation, +30 to +50 % detail). Matching the mean and spread of the
  first 4 free latents to the carry, fading out over the ramp, passed the seam
  (+0.35 saturation, detail 0.97). Factor 1.0 over 6 latents also passed
  (+0.97). Factor 0.6 over 4 passed at +1.96, just under the 2.0 gate.

Defaults: `LtxChainSpec` in `tool/chain_engine.py` (`latent_carry=True`,
`carry_restore=1.0`, `seam_adain=1.0`, `seam_ramp=4`, `seam_mode="path"`).
Chains drafted before these defaults resume with their old settings
(`chain_studio.LTX_LEGACY_CARRY`).

## 3. One decode for the whole take

A pixel crossfade between separately decoded links ghosts: both links are
visible at once. (That is a visual finding on the review strips, not a
metric.) The LTX VAE decoder is non-causal, so a link decoded alone has no
context past its own end. `StudioLtxDecodeTake` joins the links' latents and
decodes the take once in streamed windows (24 latents, 2 context latents each
side). So the switch between links is a latent step decoded with real context
on both sides.

The switch point was measured on the v2 chain's 1080p finishes
(decode-only, ~220 s per variant). The variant is S5p: the S5 finish latents
with the path-mode look match of section 4 re-applied by a session script,
`repath_variant.py`.

| Join / blend (run) | Switch frame | Seams failing the look checks |
|---|---|---|
| join 2, no blend (`20261023_052929`) | 105 / 225 / 345 | 345: saturation +3.09 |
| join 2, blend 1 (`20261023_053725`) | 105 / 225 / 345 | 345: saturation +2.37 |
| join 3, blend 2 (`20261023_054302`) | 113 / 233 / 353 | 233: saturation +3.05 |
| join 4, blend 3 (`20261023_054833`) | 121 / 241 / 361 | none (241 fails detail only) |

With join 4 / blend 3, the 3 carried latents after the anchor are crossfaded
in latent space and the switch lands on the carry end. So each link boundary
is one event instead of two. That is the default (`cg.LTX_TAKE_JOIN`,
`cg.LTX_TAKE_BLEND`). Seam 241's detail ratio (0.61) is the same in every
variant; it is link 3's own content.

The take's audio is joined in latent space from the stage-1 audio latents and
decoded once (`StudioLtxAudioTake`, ~10 s per take). The carried audio latents
are exact copies (checked: `carry_exact`).

## 4. Seam look matching along a path

On the v2 chain's S5 1080p take, link 3's first free latent flashed: about +12
to +15 saturation on the static background for 8 frames (241-248; seen on the
review strips, re-measured on the lossless take). The take's
seam 241 measured +9.65 saturation (`20261023_050612`, final take). Under the
local rule that passed the saturation check (local median 3.87, limit 9.67),
and the take failed at 241 on detail only. The block match of section 2
corrects the ramp latents as one block, so a single bright latent inside the
block survives.

`StudioSeamAdain` mode `path` matches each latent after the carry on its own,
to the line from the carry's look to the look of the content after the ramp.
On the v3 verification chain (all defaults, link 1 reused from v2), seam 241
measured -2.64 saturation in the 1080p take and passed the saturation check
(`20261023_055607`). It still failed detail (0.61, content). Chains drafted
before the mode existed keep the block match.

## 5. Re-rolling a bad carry end

A drafted link's own decode can fail a check that its sampling decides: pop,
saturation, lightness or contrast at the carry end (`seam_qa.carry_end_check`).
Such a link is drafted once more with seed + 7919, and the attempt with the
better score is kept (`LtxChainSpec.reroll_seams`). The kept seed goes into the
manifest; the other attempt stays under `rejected/`. It fired once on the v4
gate (link 3 drafted twice, `20261023_115153`). It costs one extra draft (~3-4
min).

## 6. Stage 2 (the 1080p finish): keep the draft

Stage 2 upsamples the draft latent x2 and refines it at 1920x1088. With the
official distilled schedule (bare model from sigma 0.909375, euler, plus the
stage-1 detailing keyframes), the finish re-drew what the prompt does not
name. On link 1 the woman's cream cable-knit turtleneck came out floral, the
man's overshirt plaid, and both faces changed. References cannot be used in
stage 2 on this card (section 7), so the prompt is the only identity cue
there. (Sigma here is the noise level the finish starts from.)

Isolation matrix on v2 link 1 (`20261023_04xxxx_ltx_gate_*`, `finish_cmp.py`
against the draft; SSIM is a standard image-similarity score, texture =
mid-band texture correlation). `finish_cmp.py` printed the SSIM and texture
values, and they were not saved. An independent re-computation from the
lossless files reproduced every row to three decimals.

| Variant | Model | Sigmas | Sampler | SSIM vs draft | Texture corr | Finish wall |
|---|---|---|---|---|---|---|
| F0 | bare + stage-2 keyframes | 0.909375 ... | euler | 0.551 | 0.348 | 733 s |
| F1 | bare, no keyframes | 0.909375 ... | euler | 0.521 | 0.322 | 287 s |
| S1 | bare | 0.85, 0.725, 0.421875, 0 | euler | 0.613 | 0.522 | 248 s |
| S2 | draft's (CQ 0.6 + MSR IC-LoRA, no guide) | 0.909375 ... | euler | 0.501 | 0.293 | 193 s |
| S3 | draft's | 0.85, 0.725, 0.421875, 0 | euler_ancestral | 0.583 | 0.455 | 212 s |
| S4 | draft's | 0.85, 0.725, 0.421875, 0 | euler | 0.602 | 0.499 | 215 s |
| S5 | draft's | 0.725, 0.421875, 0 | euler_ancestral | 0.719 | 0.710 | 182 s |

The starting sigma is the main lever on these metrics. At equal sigmas the
draft's model scored slightly lower than the bare model (S2 0.501 / 0.293
against F1 0.521 / 0.322; S4 0.602 / 0.499 against S1 0.613 / 0.522). Choosing
it rests on the wardrobe and face crops, not on the numbers. S4 measured
better than S3 on pixels but invented embroidery on the crops; I chose on the
crops, not on the metric. S5 was adopted first. The aging work (section 9)
then moved the default to sigmas 0.55, 0.3, 0 (`cg.LTX_STAGE2_SIGMAS`). On
link 1 of a CQ-0 + face close-up draft it gave SSIM 0.818 and texture 0.827,
against 0.742 / 0.695 from 0.725, with the same 1080p detail (144.4 vs 147.9
on the same scale) and the same two steps (`combo3_measure.log`).

## 7. Finish speed and the 1080p cliff

Stage-2 time is steeply superlinear in tokens on this card: it grows much
faster than the token count (tokens are the pieces the model cuts the video
into). Measured on the gate (`HARDWARE_BENCHMARKS_AND_LIMITS.md` §5.4 has the
probes):

- Link 1 (121 frames, ~32.6k tokens): the official 3-step finish took 287 s
  without the five stage-2 detailing keyframes (F1) and 733 s with them
  (+10.2k tokens, F0 of the matrix in section 6). That is why they are off
  (`LtxFinish.stage2_keyframes = False`). The adopted finishes of link 1 took
  134-182 s (206 s with the CQ LoRA in stage 2).
- 145-frame links (~38.8k tokens). The stage-2 recipe is not the same in every
  row: v2 S5 and v3 ran two steps from 0.725 with the draft's CQ 0.6 model,
  v4 and v4s2 two steps from 0.55 without CQ, and Fcq the v4 recipe with CQ
  0.6:

  | Run (start) | VRAM in use at ComfyUI start | Finish, links 2 / 3 / 4 |
  |---|---|---|
  | v2 S5 (05:06) | 439 MB | 244 / 230 / 215 s |
  | v3 (05:56) | 423 MB | 272 / 336 / 390 s |
  | v4 (11:52) | 1,110 MB | 426 / 425 / 275 s |
  | v4s2 (12:40) | 906 MB | 406 / 414 / 436 s |
  | v4 Fcq (13:25) | 1,023 MB | 434 / 366 / 603 s |

  The slow steps spend minutes at ~86-100 W with 100 % utilisation. That is
  the signature of weights read over PCIe from shared system memory
  (`HARDWARE_BENCHMARKS_AND_LIMITS.md` §5.4). In v3 that signature shows in
  links 3-4, while link 2 was slower without it. v2 S5 stayed compute-bound at
  38.8k tokens and later runs spilled at the same token count, so the cliff is
  not a fixed token count on this machine. The runs that started with more
  VRAM already in use were slower. Desktop applications were open during them,
  per the session notes; which ones, and their VRAM, were not recorded. But the
  v3 run spilled as well from a low baseline, and CPU-heavy lab assemblies of
  earlier takes overlapped v4s2 link 2 and Fcq links 1-3. Desktop VRAM use is
  a suspected contributor, not an established cause; nothing was isolated.
  Finish times near this cliff are not reproducible on this machine, and the
  v4 timings must not be compared with v2's as if only the recipe had changed.
- MSR references in stage 2 take the token count from ~43k to ~73k. In the
  only run that tried it (`20261022_233852_ltx_gate_F0`, link 1), the first
  stage-2 step had not finished after 7 min 37 s, at 78-92 W, when the run
  ended. The connection to the server dropped (WinError 10054), and the driver
  logged `nvlddmkm` event 153 in the same second. So `msr_stage2` is off.
  Stage 2 runs the draft's model (MSR IC-LoRA loaded, no guide).

## 8. Memory: the text encoder leaves before the sampler

Gate v2's second 1080p finish (`20261023_005904`, `finish_F0_l2`) still had
the five stage-2 keyframes: about 49k tokens on a 145-frame link, past the
cliff of section 7. It ran at about 80 W from its first minute of sampling,
with 16.8 GB of RAM available. Its first step appeared after 938 s of job time
(about 876 s after the sampler started, model initialisation included), and
the lab watchdog stopped it after 1,172 s without further progress. Meanwhile
the 9.9 GiB Gemma encoder stayed loaded next to the DiT (the main video
model), the server's RSS (its resident RAM) grew to 39 GB, and available RAM
fell to 0.55 GB in the last sample before the stop. Whether disk paging added
to the stall was not measured.

The LTX chain graphs now run `LTX25EvictModels(keep_text_encoder=false)`
(`unload_all_models`) after the last prompt is encoded
(`chain_graphs._release_text_encoder`). The rerun of the same finish with the
release (`20261023_035222`) held 13-15 GB less RSS at the same points of the
run (peak 19.1 GB) and kept at least 21.7 GB available. But it still ran at
~80 W, made no step in 15 minutes, and was stopped the same way. The release
fixes the RAM side, not the cliff. What made the 145-frame finishes complete
was dropping the keyframes (v2 S5: links 2-4 in 215-244 s, section 7). H3
chain graphs do not use the release any more (doc 02, section 5).

## 9. Cast age and identity

Measured with InsightFace `buffalo_l` (apparent age, and ArcFace similarity
to a reference crop; faces sampled every 3 or 6 frames). Read these numbers
with three caveats:

1. The age estimate is noisy and reads crying as older. It read the two
   reference images of the same actress as 29 and 35.
2. Similarities are only comparable against the same reference crop. The
   link-1 matrix (set A) used one pair of reference crops, the later runs
   (set B) another; the tables keep them apart.
3. Every row is one render at one seed, except the seed-shift check.

**Link-1 draft levers (set A, same seed, 41 faces per person):**

| Draft variant (run) | Woman age / similarity | Man age / similarity |
|---|---|---|
| base: CQ enhancer 0.6 (`lab_ltx_gate_bcb2e4a7_v3` link 1) | 32.6 / 0.205 | 37.7 / 0.342 |
| + cast sentence in the prompt (lab `--cast-line`, written by hand; `20261023_103847`) | 31.2 / 0.231 | 36.7 / 0.360 |
| + face close-up in the free slot pic4 (`20261023_104301`) | 32.4 / 0.303 | 36.9 / 0.365 |
| upper-body crops in pic2/pic3 (`20261023_104925`) | 30.6 / 0.324 | 37.8 / 0.404 |
| CQ enhancer off (`20261023_104629`) | 25.0 / 0.350 | 35.4 / 0.374 |

**Link-1 finishes of the base draft** (set A). Each is one change against the
v3 finish, the default then: the draft's model from sigma 0.725, euler
ancestral.

- that finish: 40.1 / 0.178;
- the same with CQ 0 in stage 2: 39.0 / 0.176;
- from sigma 0.55: 35.8 / 0.197 (`age_l1_s2a.json`);
- from 0.725 with the cast sentence in the finish prompt: 36.0 / 0.207 (run
  `s2cast`; this measurement was printed during the session and is not in a
  saved JSON).

The 1080p finish itself carved forehead furrows into the draft's face.

**Link 1, CQ-0 draft with the face close-up** (set B, 21 faces per person,
`combo3_measure.log`):

- draft 29.5 / 0.394;
- finish from 0.725: 33.1 / 0.241;
- from 0.55: 29.5 / 0.351;
- from 0.55 with CQ 0.6 in stage 2: 31.0 / 0.339 (+20 % detail, the "eyes
  pick" option, `--set cq_strength=0.6`);
- from 0.725 with CQ 0.6: 36.1 / 0.248.

**Seed check** (set B, seed + 7919, `glow_age.json`): woman 26.5 / 29.1 / 30.6
and man 38.0 / 41.7 / 43.2 at draft CQ 0 / 0.3 / 0.6. CQ ages both at the
second seed as well.

What changed:

- `LtxChainSpec.cq_strength = 0.0` (chains drafted before keep 0.6);
- an automatic face close-up of each cast portrait in the MSR subject slots
  pic1-pic4 that are still free, lead first (`tool/face_ref.py`,
  `chain_studio.ltx_face_slots`). In a full-length portrait the face is about
  90 px tall at draft size; the close-up gives about 270 px. A start image
  plus three cast portraits leaves no slot free;
- one cast sentence after the first sentence of every link prompt, when the
  reference carries a description (`chain_studio.with_cast_line`);
- stage 2 from 0.55.

The studio's cast sentence is generated ("Cast, the same age and look in every
shot: ..."). The sentence measured above was the lab's hand-written
`--cast-line`, so the studio's wording itself was not measured.

**Whole chain (set B, 1080p takes, 1 face every 6 frames; n = faces found):**

| Take | Woman age per link (n) | Woman similarity link 1 -> last | Man age per link (n) | Man similarity |
|---|---|---|---|---|
| v3 (CQ 0.6, no close-up, 0.725), final | 40.0 (21) / 39.0 (16) / 38.8 (4) / - (0) | 0.204 -> 0.171 | 38.0 (21) / 41.5 (13) | 0.301 -> 0.224 |
| v3 draft | 31.8 (20) / 35.5 (19) / 38.7 (16) / 41.0 (1) | 0.241 -> 0.153 | 37.9 / 38.6 / 41.3 (3) | 0.344 -> 0.200 |
| v4 (all defaults, seed 1), final | 29.9 (20) / 33.3 (19) / 30.2 (20) / 28.1 (20) | 0.315 -> 0.346 | 34.0 (21) / 38.1 (16) | 0.409 -> 0.280 |
| v4 draft | 29.5 / 32.5 / 28.3 / 25.9 | 0.328 -> 0.372 | 36.1 / 36.6 (17) | 0.395 -> 0.261 |
| v4s2 (seed + 7919), final | 24.0 / 30.2 / 32.1 / 27.9 | 0.260 -> 0.353 | 34.0 / 33.0 / 33.8 (4) | 0.373 -> 0.230 |
| v4 Fcq (stage-2 CQ 0.6), final | 30.2 / 34.5 / 30.2 / 28.4 | 0.301 -> 0.346 | 35.8 / 40.9 (16) | 0.388 -> 0.256 |

For the lead the aging is much smaller than before, but not gone at both
seeds. v3's 1080p take read about 40 from the first link on, and its draft
climbed 32 -> 41 (link 4 rests on one detected face). At seed 1, v4 stays
within 28-33 and her similarity rises along the chain. At the second seed
(v4s2) she reads 24.0 in link 1 and 30.2 / 32.1 in links 2-3, a rise of about
8 years, before link 4 comes back to 27.9; her similarity still rises. The man
is detected mainly in links 1-2 (a few faces in link 3 of some takes). In v4
he still reads 4 years older in link 2 than in link 1 (34 -> 38), his
similarity falls (0.41 -> 0.28), and in Fcq the climb is 36 -> 41. That is not
solved, and it has not been studied separately.

A glowing hand-held light appeared in link 1 at seed 1 whenever the face
close-up was combined with CQ 0 or 0.3 (the prompt reads "both holding a ...
brightly lit close two-shot"). It did not appear at seed 2 or at CQ 0.6. I
judged it a seed/prompt interaction and kept CQ 0; it is not explained.

## 10. A change inside the first free latent: the pixel look path

On the v4 gate, link 2 ended on a growing sun glare, and link 3's draft dropped
it within two frames, at both seeds. On the lossless v4 draft take, seam 241
measured pop 1.51x, saturation +7.79, lightness -4.87 (printed in the session,
not saved; re-measured later: 1.508 / +7.785 / -4.870). In the encoded
takes it measured 1.52x / +9.09 / -4.84 (draft) and 1.51x / +9.98 / -4.61
(1080p) (`20261023_115153`). The latent look match corrects each 8-frame
latent as a whole, so a change inside one latent survives it.

`chain_assemble.look_path_plan` / `apply_look_path` (`look_path` on the LTX
and H3 specs) act at the delivery encode, only on carry ends that fail a look
check. Frames b..b+15 are matched one by one in Lab colour space to the line
from the last carried frame's look to the look 16 frames later: lightness in
mean and spread, chroma in mean, then scaled so the HSV saturation follows its
line too. The lossless take stays as decoded.

| Seam 241 after the look path | pop | saturation | lightness | contrast | Look checks |
|---|---|---|---|---|---|
| v4 draft take (`20261023_125729`) | 0.83x | -1.62 | -2.06 | -1.06 | pass |
| v4 1080p take (`20261023_125729`) | 0.89x | -1.95 | -1.78 | -1.03 | pass |
| v4s2 1080p take, with the HSV-saturation scale (`20261023_133436`) | 0.81x | -1.93 | -0.99 | -0.92 | pass |

The lightness step of -1.78 to -2.06 is above the absolute 1.5 gate. It passes
because the scene's own lightness changes that fast at that point (the glare
fading), which the local rule allows. When the v4 takes were assembled, seam
241 still failed one check: an audio level step of 34.97 dB. That is a breath
starting out of silence (in the QA's 100 ms windows about -75 dBFS before and
-40 dBFS after, rising to about -26 dBFS a window later; re-measured
later). The QA rule added afterwards (`SILENCE_DBFS`) reports it as an
onset. The re-QA of the same v4 draft take at 13:25 marks seam 241 ok
(`20261023_132506`); the v4 1080p take was not re-assembled after that change.

## 11. What the delivered takes still fail

v4 1080p take (`20261023_125729`), checks at the three junctions and three
carry ends:

- **Detail at 97 (0.78) and 337 (0.74)**: link 1's own motion blur and link
  4's macro bokeh, both content.
- **Audio level at 217 (16.4 dB, click 0.113)**: a level step that is not an
  onset, in the v4 F0 and Fcq takes (same audio). Not investigated.
- **Audio level at 97 (43.6 dB) and 241 (34.97 dB)** in this record, which
  was QA'd before the onset rule existed. Both are sounds starting out of
  silence, and they pass as onsets when the same audio is re-QA'd with the
  rule (`20261023_132506`).
- **Drift 55.9 %** (link 1 -> link 4): the push-in from a two-shot to a
  close-up.
- **Overlap PSNR 23.1-25.5 dB minimum** per overlap against the 32 dB gate.
  The soft pin re-renders the carry by design; the gate was written for a hard
  pin.

On the v4s2 take the open flags are detail at 241 and 337. No take passed
every gate. No threshold was raised, but the judging rules did change during the
work (the local rule and the audio onset exemption, section 1), and some seams
pass only because of them.

## 12. Limits

- One story, one gate chain, at most two seeds per configuration; most rows
  are N=1. No clock locking; the GPU ran at its 220 W limit.
- The seam metrics are proxies for what a viewer sees. The visual review
  ("transitions look good, contrast doesn't change, but they still get older"
  at v3, then the v4 takes) is one viewer's judgement.
- InsightFace age is an apparent-age estimate with years of error. It is used
  here only to compare variants of the same person at one width, never as an
  absolute age.
- Timings depend on the machine's VRAM state at ComfyUI start (section 7).
- Every run here was driven by the lab harness (`tool/scripts/chain_lab.py`).
  Video Studio has also rendered an LTX chain through the studio's own path
  (`chain_studio`, `StudioRunner` and the GPU broker);
  [SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md](SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md)
  §6 has its record.
