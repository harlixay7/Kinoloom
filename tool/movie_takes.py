"""Continuous takes for the movie pipeline.

A CONTINUE shot carries the camera on from the shot before it: same scene, same
location, no cut. A run of them is one continuous take, and on a model with the
seamless chain engine (video_models.seamless_kind: LTX-2.5 quality, every
MiniMax H3 tier) the orchestrator renders the run as ONE chain: an exact latent
carry between links, one reference set and cast line for the whole take, the
take decoded once and checked at every seam (chain_studio). The delivered take
is then cut back into one clip per shot at the link seams, so every shot keeps
its own row, QA, tail frame and place in the edit, and the assembler's concat
puts the take back together frame for frame.

CUT and MATCH_ACTION shots (an edit, a new camera set-up) render one at a time
as before. A retake of a single shot of a take (the QA ladder) renders that
shot alone, opening on its predecessor's tail frame.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Callable

from core import log_setup

log = log_setup("movie_takes")

TAKE_MAX_SHOTS = 4          # 4 links = 20 s; H3 at 1080p renders that in ~2 h 40 min
_JOINABLE = ("pending", "queued")
_SLOT_ORDER = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6", "background")
_CAST_TYPE = {"character": "face", "power": "power", "weapon": "weapon", "location": "environment"}


def settings(cfg: dict) -> tuple[bool, int]:
    """(enabled, max shots per take) from settings `movie.seamless_takes` /
    `movie.take_max_shots`; on by default."""
    m = (cfg or {}).get("movie") or {}
    return bool(m.get("seamless_takes", True)), max(1, int(m.get("take_max_shots") or TAKE_MAX_SHOTS))


def _sjson(row: dict) -> dict:
    j = row.get("json")
    return j if isinstance(j, dict) else {}


def _fresh(row: dict) -> bool:
    """Not a QA retake or a forced text-to-video render (those render alone)."""
    return not int(row.get("qa_stage") or 0) and not _sjson(row).get("t2v")


def _continues(prev: dict, row: dict) -> bool:
    from shotspec import continuity_of
    pj, j = _sjson(prev), _sjson(row)
    if continuity_of(j) != "CONTINUE" or str(j.get("prev_shot") or "") != prev["shot_id"]:
        return False
    if str(row.get("scene") or "") != str(prev.get("scene") or ""):
        return False
    pl, cl = str(pj.get("location") or ""), str(j.get("location") or "")
    return not (pl and cl and pl != cl)


def plan_take(head: dict, scene_rows: list[dict], model_of: Callable[[dict], str],
              max_shots: int = TAKE_MAX_SHOTS) -> list[dict]:
    """`head` and the CONTINUE shots that follow it without a break, in story
    order: each continues the one before it, is still to render, is not a QA
    retake, and renders on head's model. A fork (two shots continuing the same
    one) ends the take. [head] alone when nothing follows."""
    if not _fresh(head):
        return [head]
    mid = model_of(head)
    run = [head]
    while len(run) < max_shots:
        nxt = [r for r in scene_rows if str(_sjson(r).get("prev_shot") or "") == run[-1]["shot_id"]]
        if len(nxt) != 1:
            break
        r = nxt[0]
        if (r.get("status") not in _JOINABLE or not _fresh(r) or not _continues(run[-1], r)
                or r["shot_id"] in {x["shot_id"] for x in run} or model_of(r) != mid):
            break
        run.append(r)
    return run


def take_id(pid: str, mid: str, sids: list[str]) -> str:
    """Stable chain id of a take: a re-run of the same take (after a failure, or
    a re-render with unchanged prompts and seeds) reuses every rendered step."""
    return hashlib.sha1("|".join([pid, mid, *sids]).encode("utf-8")).hexdigest()[:16]


def take_shot(sjsons: list[dict]) -> dict:
    """The take as one shot for reference resolution: the head shot with the
    characters of every shot (first appearance order, highest tier) and the
    first power any of them shows."""
    head = dict(sjsons[0])
    chars: dict[str, dict] = {}
    rank = {"main": 0, "supporting": 1, "crowd": 2}
    for sj in sjsons:
        for c in sj.get("characters") or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            cid = str(c["id"])
            if cid not in chars or rank.get(str(c.get("tier")), 9) < rank.get(str(chars[cid].get("tier")), 9):
                chars[cid] = {**chars.get(cid, {}), **c}
    head["characters"] = list(chars.values())
    if not head.get("power"):
        src = next((sj for sj in sjsons if sj.get("power")), None)
        if src is not None:
            head["power"], head["power_level"] = src["power"], src.get("power_level")
    return head


def slot_sorted(refs: list[dict]) -> list[dict]:
    """References in slot order (pic1..pic6, background): the order the chain
    numbers them in (H3 <Picture N>), so prompts must use it too."""
    rank = {s: i for i, s in enumerate(_SLOT_ORDER)}
    return sorted(refs, key=lambda r: rank.get(str(r.get("slot")), len(rank)))


def take_cast(refs: list[dict], bible, shot: dict) -> list[dict]:
    """resolve_reference_set references as the chain's cast: each with its kind
    (face / power / weapon / environment), and characters with their name and
    look (bible body + the shot's outfit), the identity sentence the chain puts
    in every link prompt."""
    from ltx_refs import resolve_presentation
    try:
        chars = bible.characters() or {}
    except Exception:  # noqa: BLE001 - a look sentence is a bonus, never a failure
        chars = {}
    by_id = {str(c.get("id")): c for c in shot.get("characters") or [] if isinstance(c, dict)}
    out = []
    for r in slot_sorted(refs):
        parts = str(r.get("source") or "").split(":")
        entry = {"slot": r["slot"], "path": str(r["path"]), "type": _CAST_TYPE.get(parts[0], ""),
                 "source": r.get("source") or ""}
        if parts[0] == "character" and len(parts) > 1:
            char = chars.get(parts[1]) or {}
            pres = resolve_presentation(char, by_id.get(parts[1]) or {}, shot)
            look = [str(char.get("body") or "").strip()]
            if pres.get("outfit"):
                look.append(f"wearing {pres['outfit']}")
            entry["name"] = str(char.get("name") or parts[1])
            entry["description"] = ", ".join(x for x in look if x)
        out.append(entry)
    return out


def shot_bounds(seams: list[int], total: int) -> list[tuple[int, int]]:
    """[start, end) frames of each shot in a take cut at its link seams."""
    edges = [0, *seams, total]
    if any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError(f"seams {seams} do not split a {total}-frame take")
    return list(zip(edges, edges[1:]))


def _media(path: Path) -> tuple[int, float, bool]:
    """(video frames, fps, has audio) of a clip, from its packets (no decode)."""
    import av
    with av.open(str(path)) as ct:
        vs = ct.streams.video[0]
        fps = float(vs.average_rate or vs.base_rate or 24)
        n = sum(1 for p in ct.demux(vs) if p.size)
        return n, fps, bool(ct.streams.audio)


def split_take(take: Path, seams: list[int], outs: list[Path], ffmpeg: str, crf: int = 14) -> list[Path]:
    """Cut a take into consecutive clips at its seams: frame-exact video (one
    high-quality x264 pass, BT.709 kept) and the audio cut at the same instants.
    Every clip's frame count is checked; raises on any mismatch."""
    total, fps, has_audio = _media(take)
    bounds = shot_bounds(list(seams), total)
    if len(bounds) != len(outs):
        raise ValueError(f"{len(bounds)} shots in the take, {len(outs)} clip paths")
    for (a, b), out in zip(bounds, outs):
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [ffmpeg, "-y", "-v", "error", "-i", str(take), "-map", "0:v:0",
               "-vf", f"trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS",
               "-c:v", "libx264", "-crf", str(int(crf)), "-preset", "slow", "-pix_fmt", "yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-color_range", "tv", "-movflags", "+faststart"]
        if has_audio:
            cmd += ["-map", "0:a:0", "-af", f"atrim=start={a / fps!r}:end={b / fps!r},asetpts=PTS-STARTPTS",
                    "-c:a", "aac", "-b:a", "256k"]
        p = subprocess.run([*cmd, str(out)], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"cutting frames {a}-{b} of {take.name} failed: {p.stderr[-600:]}")
        got = _media(out)[0]
        if got != b - a:
            raise RuntimeError(f"{out.name}: {got} frames, expected {b - a}")
    log.info(f"split {take.name} into {len(outs)} shots at {list(seams)}")
    return outs
