[Kinoloom](../../README.md) › [Docs](../README.md) › H3 chains and crashes

# MiniMax H3 chains, memory limits and the blue screens

> **In short:** This doc covers how MiniMax H3 chains are built and joined,
> the RAM and memory limits of the one test machine, and the three blue
> screens during the H3 tests. It says what is known about the crashes, what
> changed after them, and what is still only an inference. It rests on one gate chain and one smoke
> chain, one seed per link.

> Provenance: chain-lab run records under `output/seamlab/runs/<run id>/` and
> lab logs, mapped in [SEAMLESS_CHAINS_EVIDENCE_INDEX.md](SEAMLESS_CHAINS_EVIDENCE_INDEX.md);
> crash data from the Windows System event log and the minidumps of the studio
> PC. None of it is redistributed. One machine (RTX 3070 8 GB, i9-10980XE,
> 63.7 GB RAM, Windows 11, NVIDIA driver 616.64; H3 weights on a SATA HDD,
> `D:`), one gate chain, one seed per link. Memory figures are
> binary units written as GB/MB, as the tools report them (63.7 GB is
> 63.69 GiB, 68.4 decimal GB).

This doc covers the H3 side of the seamless-chain engine, the memory limits
that shaped both engines on this machine, and the three blue screens
during the H3 tests: what is known about them, what changed, and what is
still an inference. LTX is in [SEAMLESS_CHAINS_01_LTX.md](SEAMLESS_CHAINS_01_LTX.md).

On this page:

- [1. How an H3 chain is built](#1-how-an-h3-chain-is-built)
- [2. Look matching across an H3 carry end](#2-look-matching-across-an-h3-carry-end)
- [3. The H3 gate](#3-the-h3-gate)
- [4. Memory on the studio box](#4-memory-on-the-studio-box)
- [5. The blue screens](#5-the-blue-screens)
- [6. Limits](#6-limits)

## 1. How an H3 chain is built

From `tool/chain_graphs.py` and `tool/chain_engine.py` (`H3Link`, `H3Refine`,
`H3Chain`):

- **Links** render at 960x544 with the studio's H3 base recipe (32 steps,
  euler, simple schedule, video shift 12, audio shift 3, comfy-kitchen
  attention). Link 1 opens on the start image (frame-0 `MiniMaxH3AddGuide`).
  Every later link gets the previous link's last 39 frames as an exact
  audio-video latent prefix (`StudioH3CarryPrefix`): 12 video tokens and 65
  audio ticks, with no VAE round trip and no still-image anchor. Link lengths
  sit on H3's 17k+5 frame grid and are divisible by 3, so the 40 Hz audio
  lands on whole ticks (`H3ChainSpec.check`).
- **The take** is joined in latent space (`StudioH3ConcatTake`) and decoded
  once, so no decoder seam exists between links.
- **The 1080p refine** runs once over the whole take: the learned 3D latent
  upscaler, then `MMH3SplitUpscale` in temporal chunks of 124 frames (22
  overlap) and 1024x704 tiles at 1920x1088, sigmas 0.45 -> 0. Each chunk pins
  the previous chunk's refined overlap. It is conditioned on the prompt of the
  link that holds the middle of its fresh (unpinned) frames
  (`MMH3SplitUpscale`, `pick_conditioning`). Chunks do span seams. The planner
  keeps every junction where fresh refine output begins at least 5 tokens
  (17 frames) away from a chain seam, so each seam is refined inside one chunk
  (`studio_chain_core.h3_plan_chunks`).

**Which checkpoint.** The smoke and gate chains in this doc ran on the lab's
default H3 model (`H3Models` in `tool/chain_graphs.py`): the Singularity
checkpoint, not the pruned ref2va one. Every link graph and every refine graph
of the runs `063918`, `101357`, `140354`, `151355`, `180139`, `182700` and
`194620` loads `UNETLoader unet_name=Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.safetensors`
(for example `20261023_140354_h3_gate_R0/link_l1/graph.json`,
`20261023_151355_h3_gate_R0/link_l4/graph.json` and
`20261023_194620_h3_gate_R25/refine_R25/graph.json`). The seam check
`20261023_112044` loads no UNet: its graphs re-process and decode the smoke
chain's saved link latents. A studio chain reads its UNet from the graph the
studio builds for its tier (doc 04 §2). The H3 showcase chains
(tier `minimax_h3_hero_v2`) ran on the pruned ref2va checkpoint
(`minimax_h3_ref2va_pruned-w4a8_convrot_pruned.safetensors`; see
[SHOWCASE.md](../SHOWCASE.md)).

## 2. Look matching across an H3 carry end

H3's video tokens come in groups of five. The first token covers one frame,
the next four cover four frames each, and each phase has its own level. A
plain per-token match would drag the phases together, so `StudioSeamAdain`
runs in path mode with `period=5`: each phase follows its own line from the
carry's look to the content's.

Measured on a 2-link smoke chain (`lab_h3_smoke_1023_0639`, 141 frames, seam
at 90), all at 544p except the last row:

| Variant (run) | pop | saturation | detail | Seam 90 |
|---|---|---|---|---|
| Each link decoded on its own, joined (`20261023_063918`) | 2.29x | +3.43 | 1.09 | FAIL pop, saturation |
| Joined latent, one decode, no match (`20261023_112044`, none) | 1.74x | +2.98 | 1.21 | FAIL pop, saturation, detail |
| + match over 5 tokens = 17 frames (r5) | 1.21x | +1.76 | 1.13 | pass |
| + match over 10 tokens (r10) | 1.57x | +1.25 | 1.09 | FAIL pop |
| One-graph 1080p refine of the unmatched take (`20261023_101357`, R0) | 1.27x | +1.29 | 1.14 | pass |

Default: `H3ChainSpec.seam_adain = 1.0`, `seam_ramp = 5`. Chains linked before
keep their links unmatched (`chain_studio.H3_LEGACY_CARRY`). The match runs on
the base links, so the 544p draft take benefits too.

## 3. The H3 gate

Chain `lab_h3_gate_5c9b1e8b` is an old 4-link Video Studio chain re-rendered with the new
engine, on the Singularity checkpoint (section 1): 4 links of 192 frames (39
carried), 651 frames in the take, seeds 437180088-437180091, two cast
references and the old start image.

**Time** (all from the step records):

| Step | Wall | Notes |
|---|---|---|
| link 1 | 1,303 s | cold: encoder 39 s, reference encode 139 s, DiT load 32 s, sampling 1,088 s |
| link 2 | 1,152 s | encoder-only release still on; sampling 1,074 s |
| link 3 | 1,291 s | fresh process after the 14:46 crash; sampling 1,082 s |
| link 4 | 1,038 s | warm, nothing released or reloaded; sampling 1,026 s |
| decode_B0 (544p take) | 221 s | fresh process |
| refcond / refine / decode, R0 (refine from 0.45) | 167 / 3,653 / 726 s | refine = 210 sampler steps, about 17 s each |
| refcond / refine / decode, R25 (refine from 0.25) | 173 / 2,211 / 711 s | |

The refine sigmas, read from the `ManualSigmas` node of each refine graph:
R0 "0.4500, 0.3612, 0.3032, 0.1967, 0.1081, 0.0000"
(`20261023_182700_h3_gate_R0/refine_R0/graph.json`) and R25
"0.2500, 0.1967, 0.1081, 0.0000"
(`20261023_194620_h3_gate_R25/refine_R25/graph.json`).

So a full 4-link 1080p H3 take costs about 2 h 40 min on this card (links
about 80 min, R0 refine about 61 min, decodes about 16 min). The H3 models
live on a SATA HDD here. Read cold (the first load after a reboot), the
text-encoder and DiT loader nodes took about 39 s and 32 s. After a server
restart, with the files still in the Windows file cache, they took about 1 s
(the later refcond and refine steps). The first reference encode in each
new process took 131-139 s either way. So an SSD copy would shorten only the
cold loads. The weights have not been moved to an SSD.

**Seams.** The old take is a different render of the same story (the old
engine, 124-frame links, 496 frames). So the comparison is old pipeline
against new pipeline, not one mechanism against another:

| Take | Seam 1 | Seam 2 | Seam 3 | Verdict |
|---|---|---|---|---|
| old engine, 1080p (`baselines.json`) | pop 3.06x, sat +18.8 | 2.25x, +11.9 | 2.16x, +10.2 | all three FAIL |
| new, 544p draft take B0 | 1.30x, +0.32 | 1.30x, +2.74 | 0.96x, +0.63 | seam 345 FAIL (saturation, detail 1.21) |
| new, 1080p R0 | 1.08x, +0.78 | 1.25x, +1.29 | 0.94x, +0.43 | all pass |
| new, 1080p R25 | 1.105x, +0.77 | 1.28x, +1.37 | 0.96x, +0.59 | all pass |

Drift (link 1 -> link 4) is 54 % in R0 and R25: the gated texture measure
(`hf`) falls 54 %. The Laplacian variance falls 81 %, but it is reported only,
not gated (`seam_qa.py`). The story moves to a darker, softer close-up.
Because of that drift gate, the overall QA verdict of both 1080p takes is FAIL
although every seam passes. The 498 seam's audio step (4.9 dB) is a sound
starting out of silence and is reported as an onset.

**Identity and apparent age** (InsightFace `buffalo_l`, one face every 6
frames, n = faces found; the same reference crops for all rows; see doc 01 §9
for the estimator's limits):

| Take | Woman similarity per link (n) | Woman age per link | Man similarity per link (n) | Man age per link |
|---|---|---|---|---|
| old engine | 0.32 / 0.29 / 0.23 / 0.18 (21/21/18/6) | 24.9 / 31.6 / 27.3 / 25.8 | 0.40 / 0.31 / 0.25 (21/21/3) | 29.2 / 36.7 / 31.0 |
| new B0 (544p) | 0.40 / 0.40 / 0.35 / 0.27 (32/26/25/8) | 25.5 / 31.4 / 29.2 / 27.0 | 0.43 / 0.40 / 0.42 (32/26/7) | 34.0 / 35.3 / 36.3 |
| new R0 (1080p) | 0.34 / 0.36 / 0.34 / 0.28 | 23.8 / 30.2 / 28.6 / 28.2 | 0.37 / 0.39 / 0.38 | 31.0 / 34.2 / 34.0 |
| new R25 (1080p) | 0.35 / 0.37 / 0.34 / 0.28 | 23.8 / 30.5 / 29.0 / 28.2 | 0.38 / 0.40 / 0.40 | 30.9 / 34.5 / 35.3 |

Identity falls less along the new chain than along the old one. The woman's
similarity still drops in link 4 (0.28, 8 faces). The 1080p refine lowers
similarity against the 544p take by up to 0.06 in links 1-2 (refining from
0.25 instead of 0.45 changes that little). Which refine strength looks better
is a single visual judgement; R25 refines in 37 minutes instead of 61.

## 4. Memory on the studio box

The box has 63.7 GB of RAM and a system-managed pagefile (commit limit 81.7
GB when read; the commit limit is RAM plus pagefile). Commit
figures read during the session (87.7 GB around 01:00, about 44 GB held by
other processes) were printed, not saved to any record. Which processes held
that commit was not recorded.

ComfyUI's default output cache (the RAM-pressure cache, used by every server
here) works in two ways. Both are checked after each node finishes
(`comfy_execution/caching.py`, `execution.py`):

- cached outputs that the running graph does not use, models included, are
  released at once;
- when less than the headroom (10 % of RAM, about 6.4 GB on this box) is
  available, it evicts the running graph's own outputs of 512 MB or more. On
  Windows it releases pinned memory and further outputs only once less than
  512 MB is available or the pagefile is at least 5 % used.

A long single node, such as the refine sampler, is not interrupted by it.

- **Text encoders next to the DiT.** In ComfyUI the text encoder stays loaded
  after the prompt is encoded. The LTX chain graphs now release it before the
  sampler (`unload_all_models`). Gate v2's stalled finish was a 49k-token
  graph past the 1080p cliff. Its rerun with the release held 13-15 GB less
  RSS but stalled as well (doc 01 §8).
- **The H3 one-graph refine** (18:07, `20261023_180139`, 24 nodes: the four
  links' prompt and reference encodes, the latent upscale and the refine,
  ending in a latent save; it had no decode). It held 41.2 GB RSS with the
  text encoder loaded and left 5.0-7.4 GB of RAM available, at and below the
  cache's 6.4 GB headroom. A 1080p decode of the take (about 16 GB of float32
  frames, 651 x 1920 x 1088 x 3 x 4 bytes) would have had to fit in that
  headroom had it run in the same process. It was stopped after about 15
  minutes, at step 23 of 210 (`/interrupt`, no GPU events). The last
  telemetry sample after the interrupt read 3,545 MB of VRAM (5,931 MB when
  the job was queued, 1,607 MB at server boot), and no later VRAM reading was
  recorded.
- **The refine in three steps** (`H3Chain.refine`). `refcond_<variant>`
  encodes every link's prompt with the references and saves the conditionings
  (`StudioSaveConditioning`: tensors in safetensors, structure in JSON, no
  pickle). `refine_<variant>` loads them and samples with only the DiT and the
  upscaler. `decode_<variant>` decodes the refined latent. Peak RSS 20.5 /
  17.2 / 22.7 GB (R0), with at least 24.4 GB of RAM available throughout R0
  and R25.
- **CUDA sysmem fallback.** The driver's default lets allocations past the
  card's 8 GB land in shared system RAM. A torch probe allocated 12 GB on the
  8 GB card without an error, and pages past about 7 GB read at about 12 GB/s.
  The probe's output was printed in the session and not saved. So these two
  figures, and the conclusion that the fallback is on, rest on that one
  unsaved run. LTX 1080p finishes near the token cliff (about 38.8k-42.8k
  tokens) slow down to minutes per step at about 80-100 W when that happens;
  details and probes are in `docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md` §5.4 and
  doc 01 §7. The driver setting that would make such an allocation fail
  instead (*CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback*) is a
  system setting and has not been applied.

## 5. The blue screens

**What happened** (System event log: Kernel-Power 41, WER 1001; lab logs for
the job state):

| Time | Bug check | Lab state when it happened |
|---|---|---|
| 2026-09-18 22:04 | 0x7E | before this work; about 16 minutes after the last Video Studio LTX output (21:48) and 12 minutes after a restart of the main ComfyUI on :8188 (21:52); new reference images were staged in `ComfyUI_LTX/input` at 22:03:31, under a minute before it |
| 2026-10-23 07:05 | 0x7E (SYSTEM_THREAD_EXCEPTION_NOT_HANDLED) | H3 smoke: link 2 finished 07:04:01, the lab sent `/free` (its `clean` step; VRAM back to 584 MB), the decode-only take graph (`decode_B0`) queued 07:04:25 |
| 2026-10-23 14:46 | 0x50 (PAGE_FAULT_IN_NONPAGED_AREA) | H3 gate: link 2 finished 14:45:27, the lab sent `/free`, link 3 queued 14:45:30 |
| 2026-10-23 15:54 | 0x50 | H3 gate: link 4 finished 15:53:11 (no `/free` any more), `decode_B0` queued 15:53:12 |

The machine was booting again 34-67 s after the hand-over was queued. The
driver also logged `nvlddmkm` event 153 three times during the LTX runs (00:03:55,
03:41:31, 04:10:16). The event text says only "Error occurred on GPUID", so
what the driver did is not recorded. Each coincides, within seconds, with the
end of an LTX 1080p finish that was running slowly or had stalled:

- the MSR stage-2 run `20261022_233852` (it ended with a dropped connection,
  WinError 10054);
- the `--disable-dynamic-vram` probe `20261023_032748`;
- the lab watchdog's stop of `20261023_035222`.

None of those took the machine down. There were no further bug checks or GPU
events after 15:54, through the R0 and R25 refines that ended at 20:39.

**What the dumps show.** The minidumps of 2026-09-18, 07:05 and 14:46 were
parsed with a small triage script (no debugger or symbols were available on the box).
All three fault inside `dxgmms2.sys`, the Windows GPU video-memory manager,
with `watchdog.sys` on the stack; `nvlddmkm.sys` is not on the saved stacks.
Copies of those three dumps are kept in a folder outside the repository. The 15:54 dump
was not copied out of `C:\Windows\Minidump` (it needs administrator rights)
and was not analysed, so where it faults is not known.

**What is inferred, not proven.** Each of the three crashes happened at an H3
job hand-over that made the server drop large models in-process. At 07:05 and
14:46 the lab's `/free` between jobs unloaded them (the lab logs show its
`clean` step before the next job, with VRAM back to 584 MB and 1,137 MB). At
15:54, after the lab had stopped sending `/free` to H3, ComfyUI's RAM-pressure
cache released the DiT and the text encoder when the decode-only graph ran,
because that graph does not name them (section 4). The server's RSS was
34.6 GB in the last link-4 sample and 2.9 GB about 6 s later, in the decode's
second sample (about 31.7 GB released; it was back at 5.8 GB about 30 s later
as the decode loaded). The smoke chain's link graphs also evicted every loaded
model in-graph at the time, between the reference encode and the sampler
(`LTX25EvictModels`, text encoder not kept; change 1).

Not every such drop was followed by a crash. The lab's `/free` dropped a
loaded H3 DiT at four hand-overs in those tests (smoke 06:52 and 07:04, gate 14:26
and 14:45). Two of them (07:04, 14:45) were followed by a bug check; the
in-graph evictions of the smoke links were not.

The studio's own H3 renders avoid most of this. A normal H3 render loads its
models once and samples and decodes in one graph. (Its 1080p workflows contain
the same two upscaler-pack unloads as the chain refine; see change 1.) And the
GPU broker restarts the H3 server instead of freeing it
(`gpu_broker.h3_mode: restart`, because of an earlier livelock in the
dynamic-VRAM allocator). The chain graphs and the lab had added in-process
unload-and-reload on H3. The picture these records fit is a fault while
Windows pages GPU memory under oversubscription. The specific trigger was not
reproduced on purpose, and the 2026-09-18 crash shows the machine was
susceptible before this work.

**What changed:**

1. H3 link graphs no longer release the text encoder or the DiT before
   sampling (`H3Link.release_text_encoder = False`; the step digest ignores
   the flag, so links rendered before stay valid). The split refine's sampling
   graph loads no text encoder at all (change 4). Inside that graph two nodes
   of the third-party upscaler pack still free GPU memory, as they did before
   this work: `MinimaxH3LatentUpscaler3D` moves its 0.64 GiB model to the CPU
   after the upscale (`force_unload=True`), and `MMH3SplitUpscale` unloads the
   DiT from the GPU when the refine ends (`unload_model_and_clones`). Both ran
   in R0 and R25 without a bug check; the server is restarted before the
   decode step (change 3).
2. The lab keeps an H3 server warm between jobs instead of sending `/free`,
   and when it stops H3 it sends no `/free`. Before starting the next process
   it waits up to 90 s for VRAM to fall within 300 MB of the level the old
   instance booted to, then starts it either way (`chain_lab.fresh_process`).
3. `H3Chain._fresh_if_dropping`: before a graph that does not load the models
   the server holds (by `UNETLoader` / `CLIPLoader` class and inputs,
   `chain_engine.held_models`), the server is restarted
   (`StudioRunner.fresh_process` -> `gpu_broker.restart_leased`, which keeps
   the chain's lease). When the server's state is unknown (at the start of a
   chain run, or after a failed step), only a graph that loads no model
   restarts it; a graph that loads models runs without a restart. Measured in
   the lab: 19-21 s from the stop to the next job being queued (four
   restarts), plus the next step's model loads (section 3). The studio path
   (`gpu_broker.restart_leased`) has unit tests but has not restarted a server
   on the GPU.
4. The refine was split into three steps (section 4), each in a process that
   holds only what it needs.

These came in rounds, and the earlier rounds did not prevent the next crash.
Between 07:05 and the gate, the H3 graphs' full in-graph eviction was narrowed
to a text-encoder-only release, to keep the DiT warm between links (gate links
1-2 ran with it). But the lab still sent `/free` between jobs, and 14:46
followed exactly that. After 14:46 the release was switched off and the lab
stopped sending `/free` to H3 (gate links 3-4 ran that way). 15:54 then came
from ComfyUI's own cache release at the decode-only graph. Only after 15:54
were the restart-before-drop rule (change 3) and the split refine (change 4)
added.

After these changes the box ran decode_B0, R0 and R25 (about 2 h 10 min of
GPU steps, four in-chain server restarts, plus the interrupted one-graph
refine) without a bug check or GPU event. `decode_B0` was the first job of a
new lab server, so the restart rule did not fire there. The four restarts
came before the refine and the decode steps of R0 and R25. The gate did not
exercise a restart before a link -> take decode in a warm server. The two H3
showcase chains did, in the lab (a server restart after the warm links, then
the 544p take decode), without a bug check in the System event log
([SHOWCASE.md](../SHOWCASE.md)). The event log has shown no bug check since
the 15:54 crash; that is consistent with the cause, not proof that it is gone.

**Recommended, not applied.** These are system changes and have not been
made: the *Prefer No Sysmem Fallback* policy for the ComfyUI interpreters;
hardware GPU scheduling off if crashes come back; an NVIDIA driver update or
clean install; a RAM test (`mdsched`).

## 6. Limits

- One H3 gate chain and one smoke chain, one seed per link, no repeats.
- The causal link between the H3 model drops and the bug checks rests on
  timing and on where the dumps fault, not on a controlled reproduction.
- Times depend on the HDD the H3 weights live on, on whether the files are in
  the Windows file cache, and on what else holds RAM and VRAM on the box.
- The session's commit readings and the sysmem-fallback probe were printed,
  not saved; per-process VRAM and commit use were never recorded.
- Four of the H3 runs (`063918`, `140354`, `151355`, `180139`) have no
  `summary.json`; their seam QA is in the lab command logs (`h3_smoke.log`,
  `h3_gate.log`).
- Every H3 chain here ran through the lab harness. The studio's own chain
  path (`chain_studio`, `StudioRunner`, the broker lease) has rendered an LTX
  chain on the GPU, but not yet an H3 one (doc 04 §6).
