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

CUT and MATCH_ACTION shots (an edit, a new camera set-up) render one at a time.
Which shots form a take is planned by movie_prompts.plan_units. A take holds at
most `max_shots` shots (the take length: measured defaults, set in the
settings and per project); a longer run goes on in the next take, which opens
as a match cut on an opening frame made from the last frame of the one before
(without opening frames, on that last frame itself: movie_request).
A retake of a shot inside a take renders the whole take again under the same
chain id: the shots before it reuse their finished steps, so the latent carry
into the retaken shot stays (movie_prompts.unit_for).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from core import log_setup

log = log_setup("movie_takes")

# An H3 take is refined at 1080p in one piece: on the 64 GB studio PC the 4-link
# gate take (651 frames) left 5-7 GB of RAM in one graph, and no longer H3 take
# has rendered (chain_engine.H3Chain.refine). LTX-2.5 finishes link by link and
# decodes the take in streamed windows, so its takes are as long as a chain.
H3_TAKE_MAX_SHOTS = 4
# An 8-link LTX-2.5 take (the cliff walk, 32 s) kept the cast's identity in every
# link (ArcFace 0.31-0.51, no decline); links 7 and 8 added seam steps larger
# than any in links 1-6, though none showed to the eye. 6 is the measured
# default; `movie.ltx_take_max_shots` and each project's take length change it.
LTX_TAKE_MAX_SHOTS = 6
_SLOT_ORDER = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6", "background")
_CAST_TYPE = {"character": "face", "power": "power", "weapon": "weapon", "location": "environment"}


def settings(cfg: dict) -> bool:
    """Whether CONTINUE runs render as seamless takes (`movie.seamless_takes`,
    on by default)."""
    return bool(((cfg or {}).get("movie") or {}).get("seamless_takes", True))


def _count(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def max_shots(cfg: dict, mid: str, project: int = 0) -> int:
    """The most shots one take of `mid` holds (the take length). LTX-2.5: the
    project's take length when set, else `movie.ltx_take_max_shots` (default
    LTX_TAKE_MAX_SHOTS), at most a Video Studio chain's segments. H3:
    `movie.h3_take_max_shots` (the older `movie.take_max_shots` too, default
    4), the most its 1080p refine holds in memory, so a project's take length
    can only shorten it."""
    import video_models
    import video_studio
    top = video_studio.CHAIN_MAX_SEGMENTS
    fam = str((video_models.VIDEO_MODELS.get(mid) or {}).get("family") or "")
    m = (cfg or {}).get("movie") or {}
    if fam.startswith("h3"):
        top = min(top, _count(m.get("h3_take_max_shots")) or _count(m.get("take_max_shots"))
                  or H3_TAKE_MAX_SHOTS)
        return max(1, min(_count(project) or top, top))
    n = _count(project) or _count(m.get("ltx_take_max_shots")) or LTX_TAKE_MAX_SHOTS
    return max(1, min(n, top))


def story_length(cfg: dict, proj: dict | None) -> int:
    """The take length a story is written for: the shortest take of the
    project's video models (its model and its per-tier models), with its
    own take length."""
    import video_models
    proj = proj or {}
    try:
        mids = {str(proj.get("video_family") or "") or video_models.default_model_id() or ""}
    except Exception:  # noqa: BLE001 - a registry problem shows elsewhere
        mids = set()
    vm = proj.get("video_models")
    mids |= {str(v) for v in (vm.values() if isinstance(vm, dict) else ()) if v}
    mids.discard("")
    own = _count(proj.get("take_max_shots"))
    lengths = [max_shots(cfg, m, own) for m in sorted(mids)]
    return min(lengths) if lengths else max_shots(cfg, "", own)


def project_length(db, pid: str) -> int:
    """The project's take length (projects.take_max_shots, set on the Movie
    side); 0 when it uses the studio's."""
    try:
        row = db.con.execute("SELECT take_max_shots FROM projects WHERE id=?", (pid,)).fetchone()
        return _count(row[0]) if row is not None else 0
    except Exception:  # noqa: BLE001 - a DB without the column predates the setting
        return 0


def _sjson(row: dict) -> dict:
    j = row.get("json")
    return j if isinstance(j, dict) else {}


def continues(prev: dict, row: dict) -> bool:
    """`row` is a CONTINUE of `prev` in the same scene and location."""
    from shotspec import continuity_of
    pj, j = _sjson(prev), _sjson(row)
    if continuity_of(j) != "CONTINUE" or str(j.get("prev_shot") or "") != prev["shot_id"]:
        return False
    if str(row.get("scene") or "") != str(prev.get("scene") or ""):
        return False
    pl, cl = str(pj.get("location") or ""), str(j.get("location") or "")
    return not (pl and cl and pl != cl)


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
    from cast_look import body_text, place_entry, place_text
    from ltx_refs import resolve_presentation
    try:
        chars = bible.characters() or {}
    except Exception:  # noqa: BLE001 - a look sentence is a bonus, never a failure
        chars = {}
    try:
        locs = bible.locations() or {}
    except Exception:  # noqa: BLE001
        locs = {}
    by_id = {str(c.get("id")): c for c in shot.get("characters") or [] if isinstance(c, dict)}
    out = []
    for r in slot_sorted(refs):
        parts = str(r.get("source") or "").split(":")
        entry = {"slot": r["slot"], "path": str(r["path"]), "type": _CAST_TYPE.get(parts[0], ""),
                 "source": r.get("source") or ""}
        if parts[0] == "character" and len(parts) > 1:
            char = chars.get(parts[1]) or {}
            pres = resolve_presentation(char, by_id.get(parts[1]) or {}, shot)
            look = [body_text(char)]
            if pres.get("outfit"):
                look.append(f"wearing {pres['outfit']}")
            entry["name"] = str(char.get("name") or parts[1])
            entry["description"] = ", ".join(x for x in look if x)
        elif parts[0] == "location" and len(parts) > 1:
            # the set the background reference shows, for the prompt writer
            seen = place_text(place_entry(locs.get(parts[1]) or {}, parts[2] if len(parts) > 2 else "",
                                          entry["path"]))
            if seen:
                entry["description"] = seen
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
