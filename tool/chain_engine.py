"""Orchestration of seamless chain takes on top of tool/chain_graphs.py.

A chain is a list of links (prompt, seed, frames). The engine builds each step's
graph, hands it to a runner, checks the files the step must leave behind and keeps
a manifest of what was done, so a chain can be resumed, re-finished with other
settings, or re-rolled one link at a time.

    runner.run(name, graph) -> dict      # blocking; raises on failure
    runner.fresh_process(name)           # optional: restart the server first (H3Chain)

LTX takes run in two passes over the whole chain: every link is drafted first
(stage 1, per-stage carry), then finished (stage 2, carrying the previous link's
FINAL frames). Finishes live under a variant directory, so several finishing
settings can be tried from the same drafts. H3 takes render every link at base
resolution, then refine and decode the whole take once.

All paths handed to graphs are relative to ComfyUI's output directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import chain_assemble as ca
import chain_graphs as cg
import seam_qa


class Runner(Protocol):
    def run(self, name: str, graph: dict) -> dict: ...
    # optional: def fresh_process(self, name: str) -> None  (restart the server)


@dataclass
class Link:
    prompt: str
    seed: int
    frames: int                      # pixel frames of the link, carried frames included
    negative: str = ""


# Memory-management nodes whose output is their input (name of that input): they never
# change what a step renders, so the step digest splices them out and adding, moving
# or dropping an eviction keeps finished work valid.
_PASSTHROUGH = {"LTX25EvictModels": "value"}
DIGEST_VERSION = 2
REROLL_SEED_OFFSET = 7919      # a seam re-roll drafts the link with seed + this


def _sha(graph: dict) -> str:
    return hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()[:16]


def _splice(graph: dict, drop) -> dict:
    """graph without the passthrough nodes drop(node) selects (links through them go
    to their input), renumbered 1..N in build order like chain_graphs builds it."""
    gone = {k: n["inputs"].get(_PASSTHROUGH[n["class_type"]]) for k, n in graph.items()
            if n["class_type"] in _PASSTHROUGH and drop(n)}
    keep = sorted((k for k in graph if k not in gone), key=lambda k: (0, int(k)) if k.isdigit() else (1, k))
    new = {k: str(i + 1) for i, k in enumerate(keep)}

    def relink(v):
        while isinstance(v, list) and len(v) == 2 and v[0] in gone:
            v = gone[v[0]]
        return [new[v[0]], v[1]] if isinstance(v, list) and len(v) == 2 and v[0] in new else v
    return {new[k]: {**graph[k], "inputs": {name: relink(v) for name, v in graph[k]["inputs"].items()}}
            for k in keep}


def graph_digest(graph: dict) -> str:
    """Digest of what a step renders: its graph with the passthroughs spliced out."""
    return _sha(_splice(graph, lambda n: True))


def digest_matches(record: dict, graph: dict) -> bool:
    """A step record still describes `graph`. Records written before DIGEST_VERSION 2
    hashed the raw graph, built without the text-encoder release that
    chain_graphs._release_text_encoder now inserts; they match the graph with those
    releases removed."""
    d = record.get("digest")
    if d == graph_digest(graph):
        return True
    if record.get("digest_v", 1) >= DIGEST_VERSION:
        return False
    released = lambda n: n["inputs"].get("keep_text_encoder") is False  # noqa: E731
    return d in (_sha(graph), _sha(_splice(graph, released)))


# Nodes that read chain files, by the input naming them (one relpath, or a newline
# list in `links`); every such file is "<relpath>.safetensors" under the output dir.
_READERS = {"StudioLoadLatent": "relpath", "StudioLoadImages": "relpath", "StudioLtxDecodeTake": "links",
            "StudioLtxAudioTake": "links", "StudioH3ConcatTake": "links", "StudioLoadConditioning": "relpath"}


def input_signature(graph: dict, output_dir: Path) -> dict[str, str]:
    """size:mtime of every chain file the graph reads. An unchanged graph is not
    enough to reuse a step: when an upstream step re-renders (an edited link prompt,
    a new carry setting), the next link's draft, the finishes and the takes keep
    their graphs but read different files."""
    rels = set()
    for n in graph.values():
        key = _READERS.get(n["class_type"])
        v = n["inputs"].get(key) if key else None
        if isinstance(v, str):
            rels.update(r.strip() for r in v.splitlines() if r.strip())
    sig = {}
    for rel in sorted(rels):
        try:
            st = (Path(output_dir) / f"{rel}.safetensors").stat()
            sig[rel] = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            sig[rel] = "missing"
    return sig


def _newer_than(inputs: dict[str, str], at: str | None) -> bool:
    """An input file is missing or was written after the record's time `at`."""
    try:
        t = time.mktime(time.strptime(str(at), "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return True                                      # no usable time: do not trust it
    for v in inputs.values():
        if v == "missing" or int(v.split(":")[1]) / 1e9 > t + 1:
            return True
    return False


class _Manifest:
    def __init__(self, path: Path, spec: dict) -> None:
        self.path = path
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"spec": spec, "steps": {}}
        self.data["spec"] = spec
        self.save()

    def done(self, step: str, graph: dict, inputs: dict | None = None) -> bool:
        """The step's record describes `graph` and, when given, was rendered from the
        `inputs` files as they are now. A record from before input tracking counts
        while none of its inputs is newer than the record (then it learns them)."""
        s = self.data["steps"].get(step)
        if not (s and s.get("ok") and digest_matches(s, graph)):
            return False
        if inputs is not None and "inputs" in s and s["inputs"] != inputs:
            return False
        if inputs is not None and "inputs" not in s and _newer_than(inputs, s.get("at")):
            return False
        changed = False
        if s.get("digest_v", 1) < DIGEST_VERSION:      # migrate the record in place
            s.update(digest=graph_digest(graph), digest_v=DIGEST_VERSION)
            changed = True
        if inputs is not None and "inputs" not in s:
            s["inputs"] = inputs
            changed = True
        if changed:
            self.save()
        return True

    def record(self, step: str, **info: Any) -> None:
        if "digest" in info:
            info.setdefault("digest_v", DIGEST_VERSION)
        self.data["steps"][step] = {**info, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def _sidecar(path: Path) -> dict:
    return json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))


def _need(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"step left no output at {path}")
    return path


# ======================================================================== LTX
@dataclass
class LtxChainSpec:
    chain_id: str
    links: list[Link]
    refs: dict[str, str]                           # MSR slot -> ComfyUI input image name
    fps: float = 24.0
    width: int = 960
    height: int = 544
    carry_frames: int = cg.LTX_CARRY_FRAMES
    draft: dict = field(default_factory=dict)      # LtxDraft field overrides
    # The CQ enhancer LoRA in the draft (stage 2 refines with the draft's model). Off:
    # at 0.6 it aged the cast. Gate link 1 (2026-10-23, same seed), the lead's face
    # against her portrait (InsightFace apparent age / ArcFace similarity): 32.6 /
    # 0.205 at 0.6, 25.0 / 0.350 off, and the climb through the link (27 -> 39 as she
    # starts crying) became 23 -> 28. The draft is ~20% softer in fine detail, which
    # the 1080p finish re-renders.
    cq_strength: float = 0.0
    crossfade: int = 8                             # pixel-dissolve fallback only (take_decode=False)
    # The seamless-carry recipe (both stages), measured on the bcb2e4a7 gate
    # (lab 2026-10-23): a hard pin of the carry made the first new latent jump
    # (exposure -3.5 L, 5x motion, ragged edges); a soft ramp spreads it but the
    # model inflates soft-pinned latents (x1.06-1.09 gain, +10 saturation), which
    # the restore removes; re-encoding the decoded tail costs a VAE round trip per
    # link (+1.7 saturation, measured once), which the latent carry avoids; the first
    # free latent still bursts (+5 saturation, +30% detail), which the seam AdaIN
    # eases over 4 latents; and the take is decoded once from the joined latents,
    # so the switch between links is one latent step instead of a pixel dissolve.
    pin_strengths: str = cg.LTX_PIN_RAMP           # soft carry ("" = hard pin)
    latent_carry: bool = True                      # carry the previous link's latents, not re-encoded pixels
    carry_restore: float = 1.0                     # undo the soft pin's gain on the carry
    seam_adain: float = 1.0                        # look match across each carry end
    seam_ramp: int = 4
    # each ramp latent matched on its own to the carry->content line: one block match
    # left a one-latent +15 saturation flash at a carry end (gate link 3, 2026-10-23)
    seam_mode: str = "path"
    take_decode: bool = True                       # decode the joined take once (else pixel dissolve)
    take_join: int = cg.LTX_TAKE_JOIN
    take_blend: int = cg.LTX_TAKE_BLEND            # latents before the join crossfaded in latent space
    # A drafted link whose carry end fails a check its sampling decides (pop,
    # saturation, lightness, contrast: seam_qa.carry_end_check) is drafted once more
    # with another seed, and the better attempt is kept (one extra draft, ~4 min).
    reroll_seams: bool = True
    # A carry end whose look still steps in the decoded take (a change inside the
    # first free latent, which the latent match cannot shape) gets a 16-frame look
    # path in pixel space at the delivery encode (chain_assemble.look_path_plan).
    look_path: bool = True

    def check(self) -> None:
        if not self.links:
            raise ValueError("a chain needs at least one link")
        if self.pin_strengths:
            s = [float(x) for x in self.pin_strengths.replace(";", ",").split(",") if x.strip()]
            if len(s) > (self.carry_frames - 1) // 8 + 1 or any(not 0.0 <= v <= 1.0 for v in s):
                raise ValueError(f"pin strengths {s} must be 0..1, at most one per carried latent")
        for i, lk in enumerate(self.links):
            if (lk.frames - 1) % 8:
                raise ValueError(f"link {i + 1}: {lk.frames} frames is off the 1+8k grid")
            if i and lk.frames <= self.carry_frames + 8:
                raise ValueError(f"link {i + 1}: {lk.frames} frames leaves no room after the carry")
        if (self.carry_frames - 1) % 8:
            raise ValueError("the carry must be 1+8k frames")
        if not 0 <= self.crossfade <= self.carry_frames:
            raise ValueError("crossfade must fit inside the carry")
        if not 1 <= self.take_join <= (self.carry_frames - 1) // 8 + 1:
            raise ValueError(f"take join {self.take_join} must be a carried latent after the anchor")
        if self.seam_mode not in ("window", "path"):
            raise ValueError(f"seam mode {self.seam_mode!r} must be 'window' or 'path'")
        if not 0 <= self.take_blend <= self.take_join - 1:
            raise ValueError(f"take blend {self.take_blend} would reach the anchor before join {self.take_join}")

    def seam_frames(self) -> list[int]:
        """First frame of every later link's new content in the take."""
        out, total = [], self.links[0].frames
        for lk in self.links[1:]:
            out.append(total)
            total += lk.frames - self.carry_frames
        return out


class LtxChain:
    def __init__(self, spec: LtxChainSpec, runner: Runner, output_dir: Path,
                 models: cg.LtxModels | None = None) -> None:
        spec.check()
        self.spec, self.runner = spec, runner
        self.output_dir = Path(output_dir)
        self.models = models or cg.LtxModels()
        self.base = f"studio_chain/{spec.chain_id}"
        self.manifest = _Manifest(self.output_dir / self.base / "manifest.json",
                                  {"kind": "ltx", **asdict(spec)})

    # ---------------------------------------------------------------- paths
    def draft_rel(self, i: int) -> str:
        return f"{self.base}/link{i + 1}"

    def finish_rel(self, i: int, variant: str) -> str:
        return f"{self.base}/{variant}/link{i + 1}"

    def path(self, rel: str, suffix: str) -> Path:
        return self.output_dir / (rel + suffix)

    # --------------------------------------------------------------- graphs
    def draft_graph(self, i: int) -> dict:
        s, lk = self.spec, self.spec.links[i]
        carry = None
        if i:
            prev = self.draft_rel(i - 1)
            carry = cg.LtxCarry(tail_frames=f"{prev}_draft_tail", audio=f"{prev}_s1_audio",
                                source_frames=s.links[i - 1].frames, frames=s.carry_frames,
                                latent=f"{prev}_s1_video" if s.latent_carry else None)
        p = cg.LtxDraft(prompt=lk.prompt, seed=self.link_seed(i), frames=lk.frames, out=self.draft_rel(i),
                        refs=dict(s.refs), negative=lk.negative, width=s.width, height=s.height,
                        fps=s.fps, carry=carry, tail_out=s.carry_frames, models=self.models,
                        pin_strengths=s.pin_strengths, carry_restore=s.carry_restore,
                        seam_adain=s.seam_adain, seam_ramp=s.seam_ramp, seam_mode=s.seam_mode,
                        cq_strength=s.cq_strength)
        for k, v in s.draft.items():
            setattr(p, k, v)
        return cg.ltx_draft_graph(p)

    def draft_model(self) -> dict:
        """The LoRA strengths the drafts were sampled with (stage 2 refines with them)."""
        s = self.spec
        return {"cq_strength": s.draft.get("cq_strength", s.cq_strength),
                "msr_strength": s.draft.get("msr_strength", cg.LtxDraft.msr_strength)}

    def finish_graph(self, i: int, variant: str, overrides: dict) -> dict:
        s, lk = self.spec, self.spec.links[i]
        # stage 2 refines with the model that drew the draft
        p = cg.LtxFinish(**self.draft_model(), prompt=lk.prompt, seed=lk.seed + 1, frames=lk.frames, draft=self.draft_rel(i),
                         out=self.finish_rel(i, variant), negative=lk.negative, fps=s.fps,
                         carry_tail=f"{self.finish_rel(i - 1, variant)}_final_tail" if i else None,
                         carry_latent=(f"{self.finish_rel(i - 1, variant)}_s2_video"
                                       if i and s.latent_carry else None),
                         carry_frames=s.carry_frames, tail_out=s.carry_frames, models=self.models,
                         drift_reference=f"{self.draft_rel(0)}_s1_video", refs=dict(s.refs),
                         pin_strengths=s.pin_strengths, carry_restore=s.carry_restore,
                         seam_adain=s.seam_adain, seam_ramp=s.seam_ramp, seam_mode=s.seam_mode)
        for k, v in overrides.items():
            setattr(p, k, v)
        return cg.ltx_finish_graph(p)

    # ---------------------------------------------------------------- steps
    def _step(self, step: str, graph: dict, expect: list[Path], frames: int, force: bool) -> dict:
        digest = graph_digest(graph)
        inputs = input_signature(graph, self.output_dir)
        if not force and all(p.is_file() for p in expect) and self.manifest.done(step, graph, inputs):
            return {"skipped": True, **self.manifest.data["steps"][step]}
        t0 = time.time()
        try:
            res = self.runner.run(step, graph)
        except Exception as e:
            self.manifest.record(step, ok=False, digest=digest, error=str(e)[:2000])
            raise
        for p in expect:
            _need(p)
        side = _sidecar(expect[0])
        if int(side["frames"]) != frames:
            self.manifest.record(step, ok=False, digest=digest, error=f"{side['frames']} frames != {frames}")
            raise RuntimeError(f"{step}: decoded {side['frames']} frames, expected {frames}")
        info = {"ok": True, "digest": digest, "inputs": inputs, "seconds": round(time.time() - t0, 1),
                "frames": frames, "audio": side.get("audio"),
                "runner": {k: v for k, v in (res or {}).items() if k != "outputs"}}
        self.manifest.record(step, **info)
        return info

    def link_seed(self, i: int) -> int:
        """The seed link i is drafted with: the spec's, or the one a seam re-roll kept
        for it (the spec's seed plus REROLL_SEED_OFFSET steps). A link whose spec seed
        changed since (a movie retake under the same chain id) drafts from the new one."""
        spec = self.spec.links[i].seed
        kept = (self.manifest.data.get("seeds") or {}).get(str(i))
        if kept is not None and int(kept) >= spec and (int(kept) - spec) % REROLL_SEED_OFFSET == 0:
            return int(kept)
        return spec

    def draft(self, i: int, force: bool = False) -> dict:
        rel = self.draft_rel(i)
        expect = [self.path(rel, "_draft.mkv"), self.path(rel, "_s1_video.safetensors"),
                  self.path(rel, "_s1_kf.safetensors"), self.path(rel, "_s1_audio.safetensors"),
                  self.path(rel, "_draft_tail.safetensors")]
        step = f"draft_l{i + 1}"
        info = self._step(step, self.draft_graph(i), expect, self.spec.links[i].frames, force)
        if not self.spec.reroll_seams or i == 0 or info.get("skipped"):
            return info
        first = info["carry_end"] = self._carry_end(expect[0])
        self.manifest.data["steps"][step]["carry_end"] = first
        self.manifest.save()
        if first["pass"]:
            return info
        return self._reroll(i, step, expect, first)

    def _carry_end(self, video: Path) -> dict:
        try:
            return seam_qa.carry_end_check(video, self.spec.carry_frames)
        except Exception as e:  # noqa: BLE001 - a check that cannot read the video decides nothing
            return {"pass": True, "skipped": True, "score": 0.0, "error": str(e)[:300]}

    def _reroll(self, i: int, step: str, expect: list[Path], first: dict) -> dict:
        """Draft link i once more with another seed and keep the attempt whose carry
        end scores better; the other attempt's files go to rejected/<seed>/."""
        seed1 = self.link_seed(i)
        seed2 = seed1 + REROLL_SEED_OFFSET
        rec1 = dict(self.manifest.data["steps"][step])
        stash = self.output_dir / self.base / "rejected" / f"link{i + 1}_seed{seed1}"
        files = sorted(p for p in (self.output_dir / self.base).glob(f"link{i + 1}_*") if p.is_file())
        stash.mkdir(parents=True, exist_ok=True)
        for p in files:
            os.replace(p, stash / p.name)
        self.manifest.data.setdefault("seeds", {})[str(i)] = seed2
        try:
            info2 = self._step(step, self.draft_graph(i), expect, self.spec.links[i].frames, True)
            second = self._carry_end(expect[0])
        except Exception:
            # the re-roll failed: the first attempt stands
            for p in stash.iterdir():
                os.replace(p, self.output_dir / self.base / p.name)
            self.manifest.data["seeds"][str(i)] = seed1
            self.manifest.data["steps"][step] = rec1
            self.manifest.save()
            raise
        tried = [{"seed": seed1, **first}, {"seed": seed2, **second}]
        if second["score"] <= first["score"]:
            kept, info = seed2, {**info2, "carry_end": second}      # the stash keeps attempt 1 for review
        else:
            kept, info = seed1, {**rec1, "carry_end": first}
            back = stash.with_name(f"link{i + 1}_seed{seed2}")
            back.mkdir(parents=True, exist_ok=True)
            for p in sorted(q for q in (self.output_dir / self.base).glob(f"link{i + 1}_*") if q.is_file()):
                os.replace(p, back / p.name)
            for p in stash.iterdir():
                os.replace(p, self.output_dir / self.base / p.name)
            stash.rmdir()
        self.manifest.data["seeds"][str(i)] = kept
        self.manifest.data["steps"][step] = {**info, "reroll": {"tried": tried, "kept": kept}}
        self.manifest.save()
        return self.manifest.data["steps"][step]

    def finish(self, i: int, variant: str = "F0", force: bool = False, **overrides) -> dict:
        rel = self.finish_rel(i, variant)
        expect = [self.path(rel, "_final.mkv"), self.path(rel, "_s2_video.safetensors"),
                  self.path(rel, "_final_tail.safetensors")]
        return self._step(f"finish_{variant}_l{i + 1}", self.finish_graph(i, variant, overrides), expect,
                          self.spec.links[i].frames, force)

    def audio_take_rel(self) -> str:
        return f"{self.base}/take_audio"

    def audio_take_graph(self) -> dict:
        s = self.spec
        return cg.ltx_audio_take_graph([f"{self.draft_rel(i)}_s1_audio" for i in range(len(s.links))],
                                       [lk.frames for lk in s.links], self.audio_take_rel(),
                                       carry_frames=s.carry_frames, fps=s.fps, models=self.models)

    def audio_take(self, force: bool = False) -> dict:
        """Decode the whole take's sound once from the joined stage-1 audio latents
        (shared by the draft and every finish variant: stage 2 freezes the audio)."""
        graph = self.audio_take_graph()
        digest = graph_digest(graph)
        inputs = input_signature(graph, self.output_dir)
        p = self.path(self.audio_take_rel(), ".audio.safetensors")
        step = "audio_take"
        if not force and p.is_file() and self.manifest.done(step, graph, inputs):
            return {"skipped": True, **self.manifest.data["steps"][step]}
        t0 = time.time()
        try:
            res = self.runner.run(step, graph)
        except Exception as e:
            self.manifest.record(step, ok=False, digest=digest, error=str(e)[:2000])
            raise
        _need(p)
        side = json.loads(self.path(self.audio_take_rel(), ".json").read_text(encoding="utf-8"))
        join = side.get("join") or {}
        bad = [j for j in join.get("joins", []) if not j.get("carry_exact")]
        info = {"ok": not bad, "digest": digest, "inputs": inputs, "seconds": round(time.time() - t0, 1),
                "audio": side["audio"], "join": join,
                "runner": {k: v for k, v in (res or {}).items() if k != "outputs"}}
        self.manifest.record(step, **info)
        if bad:
            raise RuntimeError(f"audio take: carried latents were not pinned exactly: {bad}")
        return info

    # ------------------------------------------------------------- take/QA
    def take_rel(self, stage: str, variant: str = "F0") -> str:
        return f"{self.base}/take_draft" if stage == "draft" else f"{self.base}/{variant}/take_final"

    def take_frames(self) -> int:
        s = self.spec
        return s.links[0].frames + sum(lk.frames - s.carry_frames for lk in s.links[1:])

    def take_graph(self, stage: str, variant: str = "F0") -> dict:
        s = self.spec
        lats = [f"{self.draft_rel(i)}_s1_video" if stage == "draft" else f"{self.finish_rel(i, variant)}_s2_video"
                for i in range(len(s.links))]
        return cg.ltx_decode_take_graph(lats, self.take_rel(stage, variant), carry_frames=s.carry_frames,
                                        join=s.take_join, blend=s.take_blend, fps=s.fps, models=self.models)

    def take(self, stage: str, variant: str = "F0", force: bool = False) -> dict:
        """Decode the whole take once from the links' joined video latents."""
        info = self._step(f"take_{stage}" + ("" if stage == "draft" else f"_{variant}"),
                          self.take_graph(stage, variant), [self.path(self.take_rel(stage, variant), ".mkv")],
                          self.take_frames(), force)
        side = _sidecar(self.path(self.take_rel(stage, variant), ".mkv"))
        info["joins"] = (side.get("take") or {}).get("joins", [])
        return info

    def junction_frames(self) -> list[int]:
        """Take frame where each later link's frames start in a decode-once take."""
        s, out, total = self.spec, [], self.spec.links[0].frames
        for lk in s.links[1:]:
            out.append(total - s.carry_frames + 8 * (s.take_join - s.take_blend) - 7)
            total += lk.frames - s.carry_frames
        return out

    def link_media(self, stage: str, variant: str = "F0") -> list[ca.LinkMedia]:
        out = []
        for i, lk in enumerate(self.spec.links):
            rel = self.draft_rel(i) + "_draft" if stage == "draft" else self.finish_rel(i, variant) + "_final"
            v = self.path(rel, ".mkv")
            out.append(ca.LinkMedia(video=v, audio=v.with_suffix(".audio.safetensors"), frames=lk.frames,
                                    carry=self.spec.carry_frames if i else 0))
        return out

    def overlaps(self, links: list[ca.LinkMedia]) -> list[dict]:
        res = []
        for i in range(1, len(links)):
            o = seam_qa.overlap_fidelity(links[i - 1].video, links[i].video, links[i].carry)
            o["link"] = i + 1
            pa, ca_ = links[i - 1].audio, links[i].audio
            if pa is not None and ca_ is not None and Path(pa).is_file() and Path(ca_).is_file():
                o["audio"] = seam_qa.audio_overlap_fidelity(pa, ca_, links[i].carry, self.spec.fps)
            res.append(o)
        return res

    def assemble(self, out_mp4: Path, ffmpeg: str, stage: str = "final", variant: str = "F0",
                 crossfade: int | None = None, crf: int = 16, qa_dir: Path | None = None,
                 master: bool = False) -> dict:
        """Encode the take once. With take_decode (the default) the take is the
        decode-once mkv (run take() first); QA looks at each junction and at each
        carry end. Otherwise the links' own decodes are cross-faded (fallback)."""
        links = self.link_media(stage, variant)
        take_audio = self.path(self.audio_take_rel(), ".audio.safetensors")
        audio = take_audio if take_audio.is_file() else None
        if self.spec.take_decode:
            mkv = _need(self.path(self.take_rel(stage, variant), ".mkv"))
            tl = ca.plan_timeline(links, 0)
            res = ca.encode_take(mkv, Path(out_mp4), ffmpeg, fps=self.spec.fps, crf=crf, take_audio=audio,
                                 master=master, look_path=tl["seams"] if self.spec.look_path else None)
            res.update({"seams": tl["seams"], "junctions": self.junction_frames(), "total": tl["total"],
                        "join": self.spec.take_join})
            checks, tag = sorted(set(res["seams"]) | set(res["junctions"])), f"assemble_{stage}_{variant}_take"
        else:
            xf = self.spec.crossfade if crossfade is None else int(crossfade)
            res = ca.assemble_ltx(links, Path(out_mp4), ffmpeg, fps=self.spec.fps, crossfade=xf, crf=crf,
                                  master=master, take_audio=audio)
            checks, tag = res["seams"], f"assemble_{stage}_{variant}_x{xf}"
        if qa_dir is not None:
            have_links = all(Path(lk.video).is_file() for lk in links)
            rep = seam_qa.analyze_take(out_mp4, checks, fps=self.spec.fps,
                                       audio=Path(out_mp4).with_suffix(".wav") if res["audio"] else None,
                                       out_dir=qa_dir, overlaps=self.overlaps(links) if have_links else None)
            res["qa"] = rep.to_json()
            res["qa_summary"] = seam_qa.summarize(rep)
        self.manifest.record(tag, ok=True, **{k: v for k, v in res.items() if k != "qa"})
        return res


# ========================================================================= H3
@dataclass
class H3ChainSpec:
    chain_id: str
    links: list[Link]                       # link 1: window frames; later: window incl. carry
    refs: list[str]
    start_image: str | None = None
    carry_frames: int = cg.H3_CARRY_FRAMES
    # look match across each carry end, phase by phase (H3Link.seam_adain): on the smoke
    # chain's 544p take ramp 5 passed the seam (pop 1.74 -> 1.21, dSat +2.98 -> +1.76);
    # ramp 10 eased the saturation more but still popped (1.57)
    seam_adain: float = 1.0
    seam_ramp: int = 5
    look_path: bool = True                        # LtxChainSpec.look_path, at the take encode
    link: dict = field(default_factory=dict)      # H3Link field overrides
    refine: dict = field(default_factory=dict)    # H3Refine field overrides

    def check(self) -> None:
        # the H3 grid (studio_chain_core.h3_tokens_for_clip / h3_audio_aligned): clips are
        # 17k+5 frames, and only frame counts divisible by 3 land on whole 40 Hz audio ticks
        if not self.links:
            raise ValueError("a chain needs at least one link")
        for what, f in [("carry", self.carry_frames)] + [(f"link {i + 1}", lk.frames)
                                                         for i, lk in enumerate(self.links)]:
            if f < 5 or (f - 5) % 17:
                raise ValueError(f"{what}: {f} frames is off the H3 17k+5 grid")
            if f % 3:
                raise ValueError(f"{what}: {f} frames do not align with the 40 Hz audio")
        for i, lk in enumerate(self.links[1:], start=2):
            if lk.frames <= self.carry_frames:
                raise ValueError(f"link {i}: no new frames after the carry")

    def seam_frames(self) -> list[int]:
        """First frame of every later link's new content in the joined take."""
        out, total = [], self.links[0].frames
        for lk in self.links[1:]:
            out.append(total)
            total += lk.frames - self.carry_frames
        return out

    def take_frames(self) -> int:
        return self.links[0].frames + sum(lk.frames - self.carry_frames for lk in self.links[1:])


# The H3 loads a server must not drop while it runs (H3Chain._fresh_if_dropping).
_H3_HELD = ("UNETLoader", "CLIPLoader")


def held_models(graph: dict) -> frozenset:
    """The DiT and text-encoder loads of a graph, as (class, inputs)."""
    return frozenset((n["class_type"], json.dumps(n["inputs"], sort_keys=True))
                     for n in graph.values() if n["class_type"] in _H3_HELD)


class H3Chain:
    def __init__(self, spec: H3ChainSpec, runner: Runner, output_dir: Path,
                 models: cg.H3Models | None = None) -> None:
        spec.check()
        self.spec, self.runner = spec, runner
        self.output_dir = Path(output_dir)
        self.models = models or cg.H3Models()
        self.base = f"studio_chain/{spec.chain_id}"
        self.manifest = _Manifest(self.output_dir / self.base / "manifest.json",
                                  {"kind": "h3", **asdict(spec)})
        self._held: frozenset | None = None       # the server's DiT / encoder loads; None: unknown

    def link_rel(self, i: int) -> str:
        return f"{self.base}/link{i + 1}"

    def path(self, rel: str, suffix: str) -> Path:
        return self.output_dir / (rel + suffix)

    def link_graph(self, i: int) -> dict:
        s, lk = self.spec, self.spec.links[i]
        p = cg.H3Link(prompt=lk.prompt, seed=lk.seed, frames=lk.frames, out=self.link_rel(i), refs=list(s.refs),
                      start_image=s.start_image if i == 0 else None,
                      carry_from=f"{self.link_rel(i - 1)}_av" if i else None,
                      carry_frames=s.carry_frames, seam_adain=s.seam_adain, seam_ramp=s.seam_ramp,
                      models=self.models)
        for k, v in s.link.items():
            setattr(p, k, v)
        return cg.h3_link_graph(p)

    def refine_graph(self, variant: str, overrides: dict) -> dict:
        return cg.h3_refine_graph(self.refine_params(variant, overrides))

    def refine_cond_graph(self, variant: str, overrides: dict) -> dict:
        return cg.h3_refine_cond_graph(self.refine_params(variant, overrides))

    def refine_params(self, variant: str, overrides: dict) -> cg.H3Refine:
        s = self.spec
        starts, f = [], 0
        for i, lk in enumerate(s.links):
            starts.append(f)
            f += lk.frames - (s.carry_frames if i else 0)
        lps = [cg.H3RefineLink(prompt=lk.prompt, frames=lk.frames, start_frame=st)
               for lk, st in zip(s.links, starts)]
        p = cg.H3Refine(links=[f"{self.link_rel(i)}_av" for i in range(len(s.links))], link_prompts=lps,
                        seed=s.links[0].seed + 1000, out=f"{self.base}/{variant}/take", refs=list(s.refs),
                        carry_frames=s.carry_frames, seam_frames=s.seam_frames(), models=self.models)
        for k, v in {**s.refine, **overrides}.items():
            setattr(p, k, v)
        return p

    def _fresh_if_dropping(self, step: str, need: frozenset) -> bool:
        """Restart the server before a graph that would make it drop its DiT or text
        encoder. ComfyUI's RAM-pressure cache releases every cached model the running
        graph does not use, and the studio box blue-screened within a minute of an
        in-process H3 release three times on 2026-10-23: 07:05 and 14:46 after the
        lab's /free between jobs (both dumps fault in dxgmms2.sys, the Windows GPU
        memory manager), 15:54 at link -> take decode (models dropped by the cache; that
        dump was not parsed). A normal H3 render loads its models once and samples and
        decodes in one graph, and the broker restarts H3 rather than free it
        (h3_mode: restart). Unknown server state counts as holding models only when
        the graph has none: the lease may have found the instance warm."""
        drops = (not need) if self._held is None else bool(self._held - need)
        fresh = getattr(self.runner, "fresh_process", None)
        if not drops or fresh is None:
            return False
        fresh(step)
        self._held = frozenset()
        return True

    def _step(self, step: str, graph: dict, expect: list[Path], frames: int | None, force: bool) -> dict:
        digest = graph_digest(graph)
        inputs = input_signature(graph, self.output_dir)
        if not force and all(p.is_file() for p in expect) and self.manifest.done(step, graph, inputs):
            return {"skipped": True, **self.manifest.data["steps"][step]}
        need = held_models(graph)
        fresh = self._fresh_if_dropping(step, need)
        t0 = time.time()
        try:
            res = self.runner.run(step, graph)
        except Exception as e:
            self._held = None
            self.manifest.record(step, ok=False, digest=digest, error=str(e)[:2000])
            raise
        self._held = need
        for p in expect:
            _need(p)
        info: dict = {"ok": True, "digest": digest, "inputs": inputs, "seconds": round(time.time() - t0, 1),
                      "runner": {k: v for k, v in (res or {}).items() if k != "outputs"}}
        if fresh:
            info["fresh_process"] = True
        mkv = next((p for p in expect if p.suffix == ".mkv"), None)
        if mkv is not None and frames is not None:
            side = _sidecar(mkv)
            info["frames"], info["audio"] = int(side["frames"]), side.get("audio")
            if int(side["frames"]) != frames:
                self.manifest.record(step, ok=False, digest=digest, error=f"{side['frames']} != {frames}")
                raise RuntimeError(f"{step}: decoded {side['frames']} frames, expected {frames}")
        self.manifest.record(step, **info)
        return info

    def link(self, i: int, force: bool = False) -> dict:
        rel = self.link_rel(i)
        decode = self.spec.link.get("decode_draft", True)
        expect = [self.path(rel, "_av.safetensors")] + ([self.path(rel, "_draft.mkv")] if decode else [])
        expect = sorted(expect, key=lambda p: p.suffix != ".mkv")
        return self._step(f"link_l{i + 1}", self.link_graph(i), expect, self.spec.links[i].frames, force)

    def refine(self, variant: str = "R0", force: bool = False, **overrides) -> dict:
        """Refine the take at 1080p in three steps, each in a server that holds only
        what it needs: the link conditionings are encoded and saved (info
        ["conditioning"]), the refine samples with the DiT alone, and the refined
        latent is decoded on its own (info["decode"]). In one graph (encode, upscale
        and refine, no decode) the gate's refine held 41 GB with the text encoder and
        left 5-7 GB of RAM, at and below the RAM-pressure cache's headroom (~6.4 GB on
        the 64 GB box), below which it starts evicting the running graph's outputs;
        decoded in the same process, the take's frames (16 GB of floats for four
        links) would have had to fit in that."""
        p = self.refine_params(variant, overrides)
        rel = f"{self.base}/{variant}/take"
        cond = self._step(f"refcond_{variant}", cg.h3_refine_cond_graph(p),
                          [self.path(r, ".safetensors") for r in cg.h3_refine_cond_rels(p)], None, force)
        info = self._step(f"refine_{variant}", cg.h3_refine_graph(p),
                          [self.path(rel, "_take_av.safetensors")], None, force)
        info["conditioning"] = cond
        info["decode"] = self._decode(f"decode_{variant}", [f"{rel}_take_av"], rel, force)
        return info

    def decode_take(self, variant: str = "B0", force: bool = False) -> dict:
        """Base-resolution take decoded once from the joined link latents."""
        return self._decode(f"decode_{variant}", [f"{self.link_rel(i)}_av" for i in range(len(self.spec.links))],
                            f"{self.base}/{variant}/take", force)

    def _decode(self, step: str, latents: list[str], rel: str, force: bool) -> dict:
        graph = cg.h3_decode_take_graph(latents, rel, carry_frames=self.spec.carry_frames, models=self.models)
        return self._step(step, graph, [self.path(rel, "_take_final.mkv")], self.spec.take_frames(), force)

    def draft_take_links(self) -> list[ca.LinkMedia]:
        return [ca.LinkMedia(video=self.path(self.link_rel(i), "_draft.mkv"),
                             audio=self.path(self.link_rel(i), "_draft.audio.safetensors"),
                             frames=lk.frames, carry=self.spec.carry_frames if i else 0)
                for i, lk in enumerate(self.spec.links)]

    def finish_take(self, variant: str, out_mp4: Path, ffmpeg: str, crf: int = 16,
                    qa_dir: Path | None = None) -> dict:
        mkv = self.path(f"{self.base}/{variant}/take", "_take_final.mkv")
        res = ca.encode_take(mkv, Path(out_mp4), ffmpeg, crf=crf,
                             look_path=self.spec.seam_frames() if self.spec.look_path else None)
        res["seams"] = self.spec.seam_frames()
        if qa_dir is not None:
            rep = seam_qa.analyze_take(out_mp4, res["seams"], fps=24.0,
                                       audio=mkv.with_suffix(".wav") if mkv.with_suffix(".wav").is_file() else None,
                                       out_dir=qa_dir)
            res["qa"] = rep.to_json()
            res["qa_summary"] = seam_qa.summarize(rep)
        self.manifest.record(f"take_{variant}", ok=True, **{k: v for k, v in res.items() if k != "qa"})
        return res
