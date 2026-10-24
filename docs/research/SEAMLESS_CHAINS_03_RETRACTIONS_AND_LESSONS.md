[Kinoloom](../../README.md) › [Docs](../README.md) › Chain retractions and lessons

# Seamless chains: retractions, failures and lessons

> **In short:** This is the list of things that went wrong while the
> seamless chains were built, and what replaced each one. It also lists the lessons and
> the items that are still open. Read it to see how far the results in the
> other chain docs can be trusted.

> Provenance: the run records and logs listed in
> [SEAMLESS_CHAINS_EVIDENCE_INDEX.md](SEAMLESS_CHAINS_EVIDENCE_INDEX.md), the
> ComfyUI server logs kept per run (`comfy_ltx.log` / `comfy_h3.log`), and the
> session's own working notes where a number exists only there (marked). All work
> on one machine.

This doc keeps what went wrong during the seamless-chain work: approaches that
were built and then replaced, claims that were made and then corrected, and
the process mistakes behind them. It follows the format of
[VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md](VIDEOLAB_03_RETRACTIONS_FAILURES_AND_LESSONS.md).
Nothing here was deleted from the record; each item says what replaced it.

On this page:

- [Approaches that were replaced](#approaches-that-were-replaced) (items 1-8)
- [Claims that were corrected](#claims-that-were-corrected) (items 9-17)
- [Lessons](#lessons)
- [Open items](#open-items)

## Approaches that were replaced

**1. Hard pin of the re-encoded tail.** The first engine pinned the previous
link's decoded, re-encoded tail at full strength. On seam 121 of the gate the
first free latent jumped: pop 3.18x, lightness -3.57, motion ratio 5.16x, and
ragged hair edges on the frames. *Replaced by* the soft pin ramp 1.0 / 0.8 /
0.6 / 0.4 (pop 1.16x, motion 1.23x), then latent carry, carry restore and seam
AdaIN (doc 01 §2).

**2. A VAE round trip per link.** Re-encoding the decoded tail every link
changed the carry. Measured once: RMSE 0.24-0.42 between re-encoded and
original carry latents, +1.7 saturation in the decode (printed in the session,
not saved; the run's summary does not hold these values). Whether it
accumulates over links was not isolated. *Replaced by* the latent carry, which
copies the previous link's last latents exactly (doc 01 §2).

**3. A pixel crossfade between separately decoded links.** Both links were
visible at once in the overlap (ghosting, judged on the review strips; no
metric). The LTX decoder is non-causal, so a link decoded alone has no
context past its end. *Replaced by* decoding the joined take once in streamed
windows, then join 4 / blend 3 in latent space (doc 01 §3). The intermediate
join 2 still showed a separate junction step of +3.1 saturation.

**4. Block seam AdaIN.** Matching the ramp latents as one block could not
remove a single bright latent inside it: link 3's first free latent on the v2
take, about +15 saturation on the static background for 8 frames, seam 241 at
+9.65. *Replaced by* path mode, which matches each latent on its own
(-2.64 on v3; doc 01 §4).

**5. The official distilled stage 2.** The bare model from sigma 0.909375
with the stage-1 detailing keyframes re-drew what the prompt does not name: a
cream cable-knit turtleneck came out floral, an overshirt plaid, and both
faces changed (SSIM to the draft 0.551, texture correlation 0.348 on v2 link
1). *Replaced by* the draft's own model (MSR IC-LoRA, no reference guide),
euler ancestral, first from 0.725 (S5: 0.719 / 0.710 on the same draft) and
then from 0.55. The 0.55 default was compared on a different draft (CQ 0 with
a face close-up): 0.818 / 0.827 against 0.742 / 0.695 from 0.725 (doc 01 §6).
So 0.551 and 0.818 are not a like-for-like pair.

**6. References and keyframes in stage 2.** The five stage-2 detailing
keyframes add about 10.2k tokens. They took the official 3-step link-1 finish
from 287 s without them (F1) to 733 s (F0); the adopted 2-step link-1 finishes
later took 134-182 s. MSR references in stage 2 take a 1080p finish from
about 43k to about 73k tokens. In the one run that tried them
(`20261022_233852`), the first stage-2 step had not finished after 7 min 37 s
when the run ended with a dropped server connection (WinError 10054), within
seconds of an `nvlddmkm` 153 event. On this card both push the finish past
the 1080p cliff. *Replaced by* both off by default (`stage2_keyframes`,
`msr_stage2`; doc 01 §7).

**7. The CQ enhancer LoRA in the draft.** videolab adopted the CQ enhancer at
0.6 for the LTX quality tier on a single-seed edge-variance proxy (+85 %,
[VIDEOLAB_ARCHIVE_INDEX.md](VIDEOLAB_ARCHIVE_INDEX.md)), and its Retraction 4
already says that proxy is not a quality score. Measured here with
InsightFace, the LoRA in the draft aged the lead: draft 32.6 / similarity
0.205 with it, 25.0 / 0.350 without it (one seed). At a second seed both cast
members read older at CQ 0.3 and 0.6 than at 0. *Replaced by*
`LtxChainSpec.cq_strength = 0` for chains. Single clips on `ltx25_s6_cq` still
render with CQ 0.6; their faces have not been measured.

**8. Releasing models inside H3 graphs.** The smoke chain's H3 link graphs
evicted every loaded model between the reference encode and the sampler
(`LTX25EvictModels`), as the LTX graphs release the text encoder. The lab also
sent `/free` between jobs. The first sampling step then pays for re-staging
the 11 GB DiT (`comfy_h3.log` of each run):

- In the smoke chain (90-frame links, `20261023_063918_h3_smoke`), link 2's
  first step took 92.7 s against about 12 s for every later step, as slow as
  link 1's cold start (93.5 s).
- On the gate (192-frame links) the first step took 93.8 s and 93.6 s cold
  (links 1 and 3, each the first job of a new server), 67.9 s for link 2 (the
  lab's `/free` before it plus an encoder-only release in the graph) and
  32.6 s for link 4 (nothing released), against about 30-33 s for the steps
  after it.

The in-process model drops also preceded the blue screens (item 10).
*Replaced by* no release in the H3 link graphs
(`H3Link.release_text_encoder = False`), a refine whose sampling graph loads
no text encoder, and a server restart before any graph that would drop a
model (doc 02 §5; the upscaler pack's own end-of-node unloads inside the
refine graph remain).

## Claims that were corrected

**9. "Desktop apps make the 1080p finish spill."** This was written in the
session's notes and in `docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md` §5.4 when the
v4 finishes ran slow with 906-1,110 MB of VRAM in use at ComfyUI start. The v3
run spilled as well from a 423 MB start (272 / 336 / 390 s for links 2-4), so
the cause is not established. *Corrected to* "a suspected contributor" (doc 01
§7) and in the hardware document.

**10. The blue-screen fixes, round by round.** Between the 07:05 crash and
the H3 gate, the H3 graphs' full eviction was narrowed to a text-encoder-only
release to keep the DiT warm. The lab still sent its own `/free` between
jobs, and 14:46 followed exactly such a `/free`. The lab then stopped sending
`/free` to H3, and 15:54 followed ComfyUI's own RAM-pressure cache dropping
the models when the decode-only graph ran. Only then were the
restart-before-drop rule and the split refine added. A first draft of doc 02
also attributed 07:05 to the cache release; the step records show the lab's
`/free` before it (VRAM back to 584 MB). *Corrected* in doc 02 §5. The cause
of the crashes is still an inference from timing and dump locations, not a
reproduction.

**11. Step reuse by graph digest alone.** A chain step was reused when its
graph was unchanged, even after an upstream step had been re-rendered. Editing
one link's prompt left the next link drafted from the old tail, and
re-rendered finishes left the old final take in place. Found during the v2/v3
work. *Replaced by* a per-step `input_signature` (size and mtime of every file
the graph reads; `chain_engine.input_signature`).

**12. Pixel metrics as the judge of a finish.** In the stage-2 matrix S4
scored better than S3 on SSIM and texture correlation (0.602 / 0.499 against
0.583 / 0.455) but invented embroidery on the clothing crops. The choice was made on
the crops. The seam metrics are kept as gates; adoption also needs a visual
check.

**13. The overlap PSNR gate.** The 32 dB overlap gate compares the re-rendered
carried frames with the originals. It was written for a hard pin. With the
soft pin the carry is re-rendered by design: the worst overlap frame of each
delivered take measures 22.77-23.08 dB (doc 01 §11 lists one take's seams at
23.1-25.5 dB). The gate still fails those takes. It has not been loosened, and
it should be redefined or retired for soft pins (open).

**14. The compiler A/B.** A planned finish A/B with ComfyUI's compiler off
did not start. The session put that down to the flag being passed as
`--comfy-arg --disable-comfy-compiler`, which argparse reads as a new option
(it has to be `--comfy-arg=--disable-comfy-compiler`). The failed start left
no record, because `logs/compiler_ab.log` was later overwritten. It was
re-queued after the H3 gate and never ran (no run directory exists). Whether
the compiler helps or hurts the finish on this card is unknown.

**15. CHANGELOG statements.** The `[Unreleased]` CHANGELOG entries for this
work were corrected where they had drifted from the records:

- the text-encoder release is LTX-only (H3 link graphs no longer release);
- the sun-glare seam figures and the look-path result (doc 01 §10);
- the link-1 age figures, which mixed two reference crops (doc 01 §9);
- "identity holds" on the H3 gate (it falls less than with the old engine);
- the man's aging in v4, which was missing;
- the 07:05 crash attribution, the restart count and cost (by design three
  restarts per 1080p H3 chain since the split refine, before the take decode,
  the refine and the refine's decode; 19-21 s each where measured, which was
  before the refine and the decode of R0 and R25; not one of 25 s), the
  RAM headroom of the split refine (at least 24.4 GB, not 25 GB) and the S5
  finish time (182 s, not 185 s);
- that the Video Studio chain path had not rendered on the GPU at that point
  (it has since; doc 04 §6);
- the take-split check (PSNR 39.7-42.5 dB; the audio sample-aligned, not
  bit-identical, after the AAC re-encode).

**16. Errors caught while writing these documents.** The first drafts of docs
01 and 02:

- carried v3 finish times copied from the hardware document instead of the
  records;
- described seam AdaIN variant C as failing (it passed at +1.96);
- stated the +1.7 saturation of the round trip as per-link compounding;
- placed the interrupted one-graph refine at step 19 (it was step 23);
- rounded the post-fix crash-free GPU time up.

All were checked against the records and fixed before these documents were
linked.

**17. Errors found by the second fact-check.** An independent
read-only check of these documents against the run records, the logs and the
code found these, all corrected in docs 01, 02 and 04:

- Gate v2's stalled 1080p finish was explained as running out of commit and
  re-reading the DiT from disk, "one step 938 s instead of ~215 s". The 938 s
  is job time to the first step, the graph was about 49k tokens with its
  keyframes, it ran at about 80 W from the start with 16.8 GB of RAM free, and
  the rerun with the encoder released stalled the same way. No record shows
  commit exhaustion or disk reads (doc 01 §8).
- The H3 one-graph refine was described as including the decode; its graph
  has none (doc 02 §4). "VRAM back to its baseline" after its interrupt was
  not recorded.
- The smoke chain's H3 links were said to evict their models before the
  decode; they evicted between the reference encode and the sampler.
- `nvlddmkm` event 153 was called a GPU reset; the event says only "Error
  occurred on GPUID".
- The H3 drift was attributed to the Laplacian; the gated measure is `hf`.
- Pop was described as measured on a grey thumbnail; it uses all RGB
  channels of a 480x272 thumbnail.
- The statement that no gate was loosened left out that the judging rules
  changed during the work (the local-rate rule between 00:49 and 05:06, the
  audio onset rule at about 13:25). It also left out that the
  local rule is what passes the v4 1080p seam 121 and passed the visible v2 S5
  flash at 241 (doc 01 §1).
- "The aging is gone at both seeds" for the lead: at seed 2 she still reads
  24.0 / 30.2 / 32.1 / 27.9 (doc 01 §9).
- A re-queued movie take was said to reuse its finished steps. It did not,
  and the chain request of "From last clip" was refused (doc 04 §7; both
  fixed since).
- Several numbers that exist only in printed session output were not marked
  as such; they are now (evidence index).

## Lessons

1. **Judge a seam locally, and the take's drift as content.** The gate story
   pushes in from a two-shot to a close-up. A whole-take drift gate fails
   every take of it (55.9 % LTX, 54 % H3) without saying anything about the
   seams.
2. **Never leave latent space inside a chain.** Every decode-and-re-encode
   between links, every per-link decode and every pixel crossfade showed up as
   a seam defect somewhere. The working engine carries latents, decodes the
   take once and encodes once.
3. **A gate written for one mechanism must be revisited when the mechanism
   changes** (item 13).
4. **Look at the frames before adopting a variant** (items 3, 5, 12). The
   metrics found the seams; only the crops found the redraws and the
   invented detail.
5. **On this box, H3 models must not be dropped in-process at a job
   hand-over.** Restart the server instead. The studio's broker already did
   this for single H3 renders (`gpu_broker.h3_mode: restart`); the chain
   engine and the lab had reintroduced in-process drops.
6. **Record the machine state with every timing.** VRAM in use at ComfyUI
   start varied from 369 to 1,323 MB across the LTX runs
   (`vram_baseline_mb`), and the run with the lowest baseline (`035222`)
   stalled worst, so the baseline alone does not explain the spills. Finishes
   near the token cliff are not comparable across runs whose state was not
   recorded (other GPU clients, per-process VRAM and commit use).
7. **Every number needs a record.** A few measurements of this work exist only
   in the session's printed output; they are marked as such in these
   documents.

## Open items

- The man's apparent age still rises from link 1 to link 2 in v4 (34 -> 38;
  similarity 0.41 -> 0.28). Not solved, not studied separately.
- Audio level step at frame 217 of the v4 takes (16.4 dB, click 0.113), not an
  onset; not investigated.
- The glowing hand-held light at seed 1 with CQ 0 / 0.3 plus the face close-up;
  not explained.
- No delivered take passes every gate (detail flags on content, drift, the
  overlap gate).
- H3 chains in Video Studio have not rendered on the GPU yet; Video Studio
  has rendered one LTX chain (doc 04 §6).
- The compiler A/B (item 14).
- The *Prefer No Sysmem Fallback* driver policy, the 15:54 minidump, an
  NVIDIA driver update and a RAM test: system changes, not made yet.
- Every result here rests on one gate chain and at most two seeds per
  configuration.
