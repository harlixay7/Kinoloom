[Kinoloom](../../README.md) › [Docs](../README.md) › Seamless chains

# Seamless Chains

> **In short:** Kinoloom renders a video longer than one model window as a
> chain of links, each one continuing the last. This page is the entry point
> to the notes on the seamless-chain engine for LTX-2.5 and MiniMax H3: what
> changed, what was measured, what failed and what is still open. Every result
> comes from one 8 GB RTX 3070, mostly at one seed per link.

Kinoloom renders a video longer than one model window as a **chain**: several
links rendered one after another, each continuing the last. These docs cover
the seamless-chain engine for LTX-2.5 and MiniMax H3 on an 8 GB RTX 3070: what
it measured, what failed, and what is still open.

**Status.** The engine was developed and measured with the lab harness
(`tool/scripts/chain_lab.py`) on one LTX gate chain and one H3 gate chain,
mostly at one seed per link. Six more chains were then rendered for the
README showcase with the lab's `--request` mode
(`tool/scripts/chain_lab.py ltx-gate|h3-gate --request <file>`), which builds
the same chain spec as `chain_studio.run_chain`: four LTX-2.5 chains (tier
`ltx25_s6_cq`; one of them, a greenhouse scene, was left out of the showcase)
and two H3 chains (tier `minimax_h3_hero_v2`, on the pruned
ref2va W4A8 checkpoint, not the Singularity one). None of them crashed the
machine. [SHOWCASE.md](../SHOWCASE.md) has the details and every number.
Video Studio has also rendered an LTX-2.5 chain end to end through its own
runner, the path the movie pipeline's continuous takes start through as well;
Video Studio's H3 chains have not run on the GPU yet ([SEAMLESS_CHAINS_04](SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md) §6).

## What changed

The earlier continuation decoded each link, re-encoded its last frames and
pinned them as a hard prefix of the next link. Then it crossfaded the links in
pixels. The new engine keeps everything in latent space (the compressed form
the model works in):

- a soft-pinned latent carry between links;
- a look match on the first free latents (`StudioSeamAdain`, path mode);
- the take's audio joined in latent space;
- the whole take decoded once and encoded once;
- seam QA on every link boundary.

The LTX 1080p finish starts from the draft's own model at sigma 0.55, so it no
longer re-draws the scene. The lead's apparent age rises much less through the
chain than before. At a second seed it still rises from 24 to 32 by link 3,
and the second character still ages. H3 links carry an exact 39-frame
audio-video prefix, and the take is refined at 1080p once. H3 models are no
longer dropped in-process at a job hand-over; the server is restarted instead.
(The upscaler pack's own unloads inside the refine graph remain.) Such drops
preceded the three blue screens during the H3 tests (doc 02 §5).

## Results at a glance

| Gate chain | Old engine, 1080p take | New engine, 1080p take |
|---|---|---|
| LTX `bcb2e4a7`, 4 links, 481 frames | pop 5.40x / 1.80x / 2.57x, saturation steps +10.4 / +13.4 / +7.1; all seams FAIL | v4: every junction and carry end passes the pop and look checks (seam 121 by the local-rate rule, seam 241 via the pixel look path); open flags: detail on content at 97 and 337, one audio level step that is not an onset (217), drift and the overlap gate |
| H3 `5c9b1e8b`, 4 links, 651 frames | pop 3.06x / 2.25x / 2.16x, saturation +18.8 / +11.9 / +10.2; all FAIL | R0: pop 1.08x / 1.25x / 0.94x, saturation +0.78 / +1.29 / +0.43; all seams pass (take verdict FAIL on the drift gate only) |

| Cast (InsightFace, apparent age per link) | Old / earlier engine | New engine |
|---|---|---|
| LTX lead, 1080p take | v3: 40.0 / 39.0 / 38.8 (draft 31.8 -> 41.0, link 4 on one detected face) | v4: 29.9 / 33.3 / 30.2 / 28.1 |
| LTX second character | v3: 38.0 / 41.5 | v4: 34.0 / 38.1 (still ages; open) |
| H3 identity (similarity, woman) | 0.32 -> 0.18 | 0.34 -> 0.28 (falls less; still falls) |

The old and new takes are different renders of the same story. The LTX ones
used the same seeds; the old H3 take had 124-frame links. Each cell is one
render. The details, N and caveats are in the docs below.

## How seams are measured

`tool/seam_qa.py` checks every link boundary of a take:

- pop (frame-difference jump against the local median);
- saturation, lightness and contrast steps (absolute, and against the scene's
  own rate of change);
- detail ratio;
- audio level step and click;
- plus whole-take drift and an overlap PSNR of the carried frames.

The gates were set on the broken chains of the old engine. They are
engineering thresholds for this machine and these chains, not perceptual
thresholds. No threshold was raised later, but two judging rules were added
during the work (the local-rate rule for look steps and the audio onset
exemption), and some seams pass only because of them. Definitions:
[SEAMLESS_CHAINS_01_LTX.md](SEAMLESS_CHAINS_01_LTX.md) §1.

## Hardware

One machine: RTX 3070 8 GB, i9-10980XE, 63.7 GB RAM (63.69 GiB; memory
figures in these docs are binary units written as GB/MB), Windows 11, NVIDIA
driver 616.64, ComfyUI 0.36.0 with comfy-kitchen 0.2.34 and dynamic VRAM, H3
weights on a SATA HDD. No clock locking. CUDA sysmem fallback is at the driver
default, which lets allocations past 8 GB spill to system RAM. That it does so
on this box rests on one probe whose output was not saved (doc 02 §4).

## Documents

| Document | Contents |
|---|---|
| [SEAMLESS_CHAINS_01_LTX.md](SEAMLESS_CHAINS_01_LTX.md) | LTX-2.5: the carry mechanism by mechanism, the decode-once take, look matching, re-roll, stage-2 fidelity, finish speed and the 1080p cliff, memory, cast age and identity, the pixel look path, what the takes still fail |
| [SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md](SEAMLESS_CHAINS_02_H3_MEMORY_AND_CRASHES.md) | MiniMax H3 chains and the gate, RAM and commit limits on the box, the three blue screens and what changed after them |
| [SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md](SEAMLESS_CHAINS_03_RETRACTIONS_AND_LESSONS.md) | approaches that were replaced, claims that were corrected, lessons, open items |
| [SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md](SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md) | Video Studio chains, movie continuous takes, the model catalog, tests, the Video Studio chain rendered on the GPU and what has not run yet |
| [SEAMLESS_CHAINS_EVIDENCE_INDEX.md](SEAMLESS_CHAINS_EVIDENCE_INDEX.md) | where every number comes from (run ids, logs, analysis outputs; not redistributed) |

Related: [VIDEOLAB.md](VIDEOLAB.md) and its documents are the fact-checked
copies of the videolab benchmark repository (single-clip H3, LTX and Qwen on
the same card). `docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md` and
`docs/PIPELINE_AND_CONTINUITY.md` cover the studio's hardware limits and the
continuity pipeline as a whole.

## Reproducing

The lab harness drives an isolated ComfyUI instance per command and writes
every record under `output/seamlab/runs/`:

```
tool\.venv\Scripts\python.exe tool\scripts\chain_lab.py ltx-gate --chain <id> --variant F0
tool\.venv\Scripts\python.exe tool\scripts\chain_lab.py h3-gate --chain <id> --variant R0
```

The gate chains themselves (prompts, references, seeds) are private test
material and are not in the repository. Any 4-link chain with a cast and a camera move
exercises the same paths. On this card the v4 LTX gate's steps took 43 minutes
in total (five drafts including one re-roll, the audio and two take decodes,
four 1080p finishes; server start and assembly not included). The 4-link H3
1080p gate took about 2 h 40 min.
