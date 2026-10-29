"""The Video Studio request of a movie unit.

A unit is what the Video Studio renders in one go: one shot (a clip, the
Generate button with one prompt) or a run of CONTINUE shots (a seamless take,
one segment per shot: Generate with several prompts). The movie renders every
LTX-2.5 / MiniMax H3 unit through exactly the request the Video Studio page
sends: the cast as references with their names and looks, a start image, the
previous clip's last frame as the continuation, the page's size and frame
rules, no negative, the model's own steps. The same prompt check
(prompt_check.refusal) decides whether it may render, and the prompts come
from movie_prompts (written by an AI or by hand), never from a template.

How a unit opens follows the head shot's continuity:

  CUT           a fresh shot: it opens on the shot's start image (the one set
                on the Shots page, else its opening frame, opening_frames.py),
                or on the prompt alone.
  CONTINUE      the same camera take: a clip continues the previous shot's last
                frame (the Video Studio's continuation), a take starts from it
                (the ↪ start image of a chain).
  MATCH_ACTION  a cut to a new camera angle in the middle of the action: it
                opens like a CUT (the previous frame would pin the old angle),
                and the prompt carries the action on from where it stopped.

A scene or location change is always a CUT. A CONTINUE that starts a new take
because the take before reached the take length (Unit.split) opens as a match
cut on an opening frame made from that take's last frame when opening frames
can be made (a new angle hides where one take ends and the next begins), and
else continues from that last frame.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core import ROOT, log_setup

log = log_setup("movie_request")

CLIP_FRAMES = 121            # 5 s at 24 fps, snapped to each model's grid (Video Studio)
_INPUT_DIRS = (ROOT / "ComfyUI" / "input", ROOT / "ComfyUI_LTX" / "input")
_PLACEHOLDER = ROOT / "temp" / "movie_plan_placeholder.png"
# shot fields that record a render, not what the shot is: they never make a
# written prompt stale
_VOLATILE = ("applied_model", "applied_references", "applied_loras", "take",
             "continuity_applied", "continuation_frame", "continued_from",
             "reference_warnings", "opening_frame", "render_profile", "_norm_notes",
             "seed_offset", "request", "dfr", "selected_loras", "keyframe_source")


# ---------------------------------------------------------------------- units
@dataclass
class Unit:
    """One Video Studio request of a project: a shot, or a seamless take.
    `split`: its first shot carries on the take before it, which ended at
    the take length (movie_takes.max_shots)."""
    pid: str
    model: str
    rows: list[dict]
    split: bool = False

    @property
    def sids(self) -> list[str]:
        return [str(r["shot_id"]) for r in self.rows]

    @property
    def head(self) -> dict:
        return self.rows[0]

    @property
    def chain(self) -> bool:
        return len(self.rows) > 1

    def sjsons(self) -> list[dict]:
        out: list[dict] = []
        for r in self.rows:
            j = r.get("json")
            out.append(j if isinstance(j, dict) else {})
        return out


def family(mid: str) -> str:
    import video_models
    fam = str((video_models.VIDEO_MODELS.get(mid) or {}).get("family") or "")
    return next((f for f in ("h3", "ltx", "wan") if fam.startswith(f)), "")


class NoVideoModel(RuntimeError):
    """A project shot has no installed model to render with."""


_WARNED: set[tuple[str, str]] = set()


def shot_model(db, shot: dict, sjson: dict, pid: str) -> str:
    """The LTX-2.5 / MiniMax H3 model id this project shot renders with, or ""
    for the legacy Wan 2.2 path.

    Selection is per shot override first (`video_model` in the shot json),
    then the project's per-tier override, then its `video_family` column. No
    model, an unknown id or an unavailable one resolves to the studio default
    (video_models.default_model_id: LTX-2.5 s6 CQ) with a warning logged once,
    so a stale id can never brick a project. Wan 2.2 is legacy: it renders
    only when the shot or project names an installed Wan entry. Raises
    NoVideoModel when no LTX-2.5 / H3 model is installed."""
    mid = str(shot.get("video_model") or sjson.get("video_model") or "")
    if not mid:
        try:
            row = db.con.execute(
                "SELECT video_family, video_models FROM projects WHERE id=?",
                (pid,)).fetchone()
            mid = str((row[0] if row else "") or "")
            # per-tier override: hero / standard / volume each may run a
            # different model (e.g. H3 for hero shots, LTX for the rest)
            try:
                per_tier = json.loads((row[1] if row and len(row) > 1 else "") or "{}")
            except Exception:  # noqa: BLE001
                per_tier = {}
            tier = str(shot.get("tier") or "standard")
            mid = str((per_tier or {}).get(tier) or mid or "")
        except Exception:  # noqa: BLE001
            mid = ""
    import video_models
    if mid:
        try:
            st = video_models.model_status(mid)
        except Exception as e:  # noqa: BLE001
            st = {"available": False, "missing": [f"registry error: {e}"]}
        fam = str(st.get("family") or "")
        if st.get("available") and fam.startswith("wan"):
            return ""       # legacy Wan 2.2, picked explicitly
        if st.get("available") and (fam.startswith("ltx") or fam.startswith("h3")):
            return mid
        if (pid, mid) not in _WARNED:
            _WARNED.add((pid, mid))
            why = (f"unavailable ({st.get('missing') or st.get('reason')})"
                   if not st.get("available") else "not a movie model")
            log.warning(f"{pid}: video model '{mid}' is {why} - using the default model")
    default = video_models.default_model_id()
    if default:
        return default
    import gpu_support
    gpu = gpu_support.problem("cu130")
    if gpu:
        raise NoVideoModel(f"{gpu}, and the project does not pick the legacy Wan 2.2 model")
    raise NoVideoModel(
        "no LTX-2.5 or MiniMax H3 model is installed (docs/setup/RESTORE_INSTRUCTIONS.md "
        "step 6), and the project does not pick the legacy Wan 2.2 model")


def takes_on(db, pid: str, cfg: dict) -> bool:
    """Whether CONTINUE runs render as seamless takes: settings
    `movie.seamless_takes` and the project's switch (projects.seamless_takes,
    the Generate tab toggle; on by default). How long a take may be is
    movie_takes.max_shots."""
    import movie_takes
    on = movie_takes.settings(cfg)
    try:
        row = db.con.execute("SELECT seamless_takes FROM projects WHERE id=?", (pid,)).fetchone()
        if row is not None and row[0] is not None:
            on = on and bool(int(row[0]))
    except Exception:  # noqa: BLE001 - a DB without the column predates the switch
        pass
    return on


# ---------------------------------------------------------------- the request
@dataclass
class MovieRequest:
    unit: Unit
    references: list[dict]
    opening: str                              # "cut", "continue" or "match_action"
    start_image: str | None = None            # a ComfyUI input file name
    start_from_clip: bool = False             # a take's start image is the previous clip's last frame
    continuation: str | None = None           # a clip continues this frame (local path)
    start_source: str = ""                    # "shot" (Shots page), "opening frame", "previous clip"
    frames: int = CLIP_FRAMES
    width: int = 1920
    height: int = 1088
    warnings: list[str] = field(default_factory=list)
    frame_planned: bool = False               # opens on an opening frame still to be made

    @property
    def model(self) -> str:
        return self.unit.model

    def check_args(self) -> dict:
        """The request as the prompt check reads it (prompt_check.plan/report)."""
        return {"references": list(self.references), "start_image": self.start_image,
                "continuation": self.continuation, "start_from_clip": self.start_from_clip,
                "frames": self.frames}

    def plan(self):
        import prompt_check
        return prompt_check.plan(self.model, len(self.unit.rows), **self.check_args())

    def refusal(self, prompts: list[str]) -> dict | None:
        """None when the prompts may render with this request, else the refusal
        the Video Studio's Generate would get (first error + every issue)."""
        import prompt_check
        return prompt_check.refusal(self.model, list(prompts), **self.check_args())

    def summary(self) -> dict:
        """What the shot record keeps of the request it rendered with."""
        return {"model": self.model, "shots": self.unit.sids, "opening": self.opening,
                "start": self.start_source or None, "continuation": bool(self.continuation),
                "size": [self.width, self.height], "frames": self.frames,
                "references": [{k: r.get(k) for k in ("slot", "name", "type", "source")}
                               for r in self.references]}


def build(db, bible, unit: Unit, *, opening_frame: Path | None = None,
          planning: bool = False, opening_planned: bool = False) -> MovieRequest:
    """The Video Studio request of `unit`.

    opening_frame    the unit's generated opening image (opening_frames.py), used
                     when the head shot has no start image of its own.
    planning         build the request the unit WILL render with, for writing
                     its prompts: a previous frame that does not exist yet, and
                     an opening frame still to be made (`opening_planned`),
                     stand in as a placeholder image, so the plan (picture
                     numbers, slots, how it opens) is the one of the render.
    """
    import movie_takes
    import video_models
    from ltx_refs import resolve_reference_set
    mid = unit.model
    entry = video_models.VIDEO_MODELS.get(mid) or {}
    fam = str(entry.get("family") or "")
    sjsons = unit.sjsons()
    merged = movie_takes.take_shot(sjsons)
    merged.setdefault("project_id", unit.pid)
    refs, warnings = resolve_reference_set(merged, bible, family=fam)
    cast = _named(movie_takes.take_cast(refs, bible, merged), bible)
    warnings = list(warnings)

    mode, tail, prev_exists = continuation_tail(db, unit.pid, sjsons[0], warnings)
    if unit.split and mode == "CONTINUE" and (
            opening_planned or (opening_frame is not None and Path(opening_frame).is_file())):
        mode, tail = "MATCH_ACTION", None       # the take before is full: a new angle
    req = MovieRequest(unit=unit, references=cast, opening="cut", warnings=warnings)
    if mode == "CONTINUE" and (tail or (planning and prev_exists)):
        req.opening = "continue"
        frame = tail or str(placeholder())
        if unit.chain:
            req.start_image = stage_input_image(Path(frame))
            req.start_from_clip, req.start_source = True, "previous clip"
        else:
            req.continuation, req.start_source = frame, "previous clip"
    else:
        req.opening = "match_action" if mode == "MATCH_ACTION" else "cut"
        own = _shot_start_image(unit.head)
        if own is not None:
            req.start_image, req.start_source = stage_input_image(own), "shot"
        elif opening_frame is not None and Path(opening_frame).is_file():
            req.start_image, req.start_source = stage_input_image(Path(opening_frame)), "opening frame"
        elif planning and opening_planned:
            req.start_image, req.start_source = stage_input_image(placeholder()), "opening frame"
            req.frame_planned = True
    anchored = bool(req.references or req.start_image or req.continuation)
    req.width, req.height = studio_size(mid, anchored)
    req.frames = studio_frames(mid)
    return req


def _named(cast: list[dict], bible) -> list[dict]:
    """The cast with a name on every reference, as the prompt writer reads it:
    people carry theirs (take_cast); a place gets the location's name, a power
    or weapon its owner's ("Mara's lightning")."""
    try:
        chars = bible.characters() or {}
    except Exception:  # noqa: BLE001 - a name is a bonus, never a failure
        chars = {}
    try:
        locs = bible.locations() or {}
    except Exception:  # noqa: BLE001
        locs = {}

    def words(s: str) -> str:
        return " ".join(str(s).replace("_", " ").split())

    for r in cast:
        if r.get("name"):
            continue
        kind, *rest = str(r.get("source") or "").split(":")
        if kind == "location" and rest:
            loc = locs.get(rest[0]) or {}
            r["name"] = words(loc.get("name") or rest[0]) + (f" ({words(rest[1])})" if len(rest) > 1 else "")
        elif kind in ("power", "weapon") and len(rest) > 1:
            owner = str((chars.get(rest[0]) or {}).get("name") or words(rest[0]))
            r["name"] = f"{owner}'s {words(rest[1])}"
    return cast


def _shot_start_image(row: dict) -> Path | None:
    """The start image picked for the shot on the Shots page (upload or image
    studio), when it still exists."""
    p = row.get("ref_image")
    if p and Path(str(p)).is_file():
        return Path(str(p))
    return None


def continuation_tail(db, pid: str, sjson: dict,
                      warnings: list[str]) -> tuple[str, str | None, bool]:
    """(effective continuity, the previous shot's last frame or None, whether
    the previous shot exists in the same place).

    CONTINUE / MATCH_ACTION bridge only within one scene and location: a
    change there is a CUT, noted in `warnings`. A CONTINUE whose previous shot
    has no last frame yet renders as a cut (the render order normally prevents
    that, see orchestrator._next_shot)."""
    from shotspec import continuity_of
    mode = continuity_of(sjson)
    if mode not in ("CONTINUE", "MATCH_ACTION"):
        return mode, None, False
    prev_id = str(sjson.get("prev_shot") or "")
    prev_row = db.get(prev_id, pid) if prev_id else None
    if prev_row is None:
        warnings.append(f"continuity {mode} but the shot before it ({prev_id or 'none'}) "
                        f"does not exist - rendered as a cut")
        return "CUT", None, False
    pj = prev_row.get("json") if isinstance(prev_row.get("json"), dict) else {}
    prev_loc, cur_loc = str(pj.get("location") or ""), str(sjson.get("location") or "")
    if prev_loc and cur_loc and prev_loc != cur_loc:
        warnings.append(f"continuity {mode} forced to CUT (location changed {prev_loc} -> {cur_loc})")
        return "CUT", None, False
    if str(pj.get("scene") or prev_row.get("scene") or "") != str(sjson.get("scene") or ""):
        warnings.append(f"continuity {mode} forced to CUT (a new scene)")
        return "CUT", None, False
    tail = prev_row.get("tail_frame")
    if mode == "CONTINUE" and not (tail and Path(str(tail)).is_file()):
        warnings.append(f"continuity CONTINUE but '{prev_id}' has no last frame yet - "
                        f"rendered as a cut")
        return mode, None, True
    return mode, (str(tail) if mode == "CONTINUE" else None), True


# ------------------------------------------------------- the page's rules
def studio_size(mid: str, anchored: bool) -> tuple[int, int]:
    """The width/height the Video Studio page renders `mid` at (app.js
    vsProvenSize and the H3 rule of vsUpdateRefsNote): the registry's proven
    size (`defaults` width/height, else its first native size). MiniMax H3
    with nothing to anchor it (no reference, start image or previous frame)
    renders single-pass at 1344x768."""
    import video_models
    m = video_models.VIDEO_MODELS.get(mid) or {}
    if family(mid) == "h3" and not anchored:
        return 1344, 768
    d = m.get("defaults") or {}
    if d.get("width") and d.get("height"):
        return int(d["width"]), int(d["height"])
    native = (m.get("resolution_limits") or {}).get("native") or []
    if native:
        w, h = (int(x) for x in str(native[0]).lower().split("x"))
        return w, h
    return 1920, 1088


def studio_frames(mid: str, n: int = CLIP_FRAMES) -> int:
    """`n` frames snapped to the model's frame grid, as the page does
    (app.js vsSnapFrames): LTX 121, H3 124 for a 5 s clip."""
    import video_models
    m = video_models.VIDEO_MODELS.get(mid) or {}
    fl = m.get("frame_limits") or {}
    lo, stp = int(fl.get("min", 9) or 9), int(fl.get("step", 8) or 8)
    mx = int(fl.get("max", 100000) or 100000)
    n = max(lo, min(int(n), mx))
    if (n - lo) % stp == 0:
        return n
    snap = str(m.get("frame_snap") or fl.get("snap") or "floor")
    k = round((n - lo) / stp) if snap == "nearest" else (n - lo) // stp
    return max(lo, min(lo + k * stp, mx))


# ------------------------------------------------------------------ images
def placeholder() -> Path:
    """The stand-in image of a planned opening: a 1x1 grey PNG, never rendered."""
    if not _PLACEHOLDER.is_file():
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))
        _PLACEHOLDER.parent.mkdir(parents=True, exist_ok=True)
        _PLACEHOLDER.write_bytes(png)
    return _PLACEHOLDER


def stage_input_image(src: Path) -> str:
    """Copy a local still into the ComfyUI input folders under a name derived
    from its content and return that name (what a Video Studio start image
    is). The same image always gets the same name: a retry reuses it, and the
    chain engine's content-named uploads keep their step digests."""
    data = Path(src).read_bytes()
    name = f"mv_{hashlib.sha1(data).hexdigest()[:16]}{Path(src).suffix.lower() or '.png'}"
    staged = False
    for base in _INPUT_DIRS:
        try:
            base.mkdir(parents=True, exist_ok=True)
            dst = base / name
            if not (dst.is_file() and dst.stat().st_size == len(data)):
                shutil.copyfile(src, dst)
            staged = True
        except OSError as e:
            log.warning(f"could not stage {src} into {base}: {e}")
    if not staged:
        raise ValueError(f"could not stage {Path(src).name} into a ComfyUI input folder")
    return name


# --------------------------------------------------------------- signatures
def shot_content(sjson: dict) -> dict:
    """The parts of a shot that its prompt is written from."""
    return {k: v for k, v in (sjson or {}).items() if k not in _VOLATILE}
