"""Opening frames: the start image of a movie shot that opens fresh.

The Video Studio renders best from a start image (Start from Image): it fixes
the framing, the set and the light, and on MiniMax H3 it is the exact first
frame. A movie shot that opens fresh (a CUT, or a MATCH_ACTION cut to a new
angle) and has no start image of its own gets one made here, with Qwen-Image
2.1 fast from its cast's reference pictures and the prompt the shot's prompt
writer wrote for it ("Opening image:", movie_prompts).

Only images a render will use are made:
  - only for a unit's first shot that opens fresh (a CONTINUE opens on the
    previous clip's last frame, the later shots of a take on the take itself);
  - never for a shot with its own start image (the Shots page);
  - only when its prompt asks for one: a hand prompt without an opening image
    renders from its references alone;
  - right before the render, a few at a time (one model load), and kept: the
    same image prompt for the same shot is never made twice.
Settings `movie.opening_frames` (default on) and the Qwen-Image 2.1 weights
decide whether frames are made at all.

A frame is made from the cast's own pictures (_refs): each person's outfit
or loadout still, one per person, and the set; for a match cut the set is
the last frame of the shot before, and the frame is made again when that
frame changes. The prompt names everyone's look (the cast sentence of the
LTX takes). Of
`movie.key_image_candidates` candidates (different seeds) the one whose faces
look most like the cast's pictures (ArcFace, face_drift.likeness; the
least-alike person counts) is kept; under `movie.key_image_min_likeness`
up to two more are tried, and a frame still under it is marked weak. The
record beside the frame (frame_info) keeps the scores. Without InsightFace
one candidate is made and not scored.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from core import ROOT, load_config, log_setup

log = log_setup("opening_frames")

MODEL = "qwen21_fast"
FRAME_DIR = ROOT / "shots" / "opening"
BATCH = 8                       # frames made under one lease, in story order
_FAILED: dict[str, float] = {}  # frame key -> when it failed (retried after an hour)
_RETRY_S = 3600
_MAX_REFS = 4                   # Qwen-Image 2.1 takes 1-4 reference images
# how frames are made (their references, candidates): frames made another way
# are made again
KEY_VERSION = 2
CANDIDATES = 2                  # movie.key_image_candidates
# movie.key_image_min_likeness: in the key-image tests every frame made the
# current way scored 0.314 or more and kept the cast's look; the score judges
# faces only, not the outfit (docs/research/MOVIE_CONTINUITY.md §2)
MIN_LIKENESS = 0.25
_EXTRA = 2                      # more candidates when the best is under it
_NOTED: set[str] = set()


def candidates(cfg: dict | None = None) -> int:
    """`movie.key_image_candidates` (default CANDIDATES), 1 to 6."""
    try:
        n = int(((cfg or {}).get("movie") or {}).get("key_image_candidates") or CANDIDATES)
    except (TypeError, ValueError):
        n = CANDIDATES
    return max(1, min(n, 6))


def min_likeness(cfg: dict | None = None) -> float:
    """`movie.key_image_min_likeness` (default MIN_LIKENESS), 0 to 1."""
    v = ((cfg or {}).get("movie") or {}).get("key_image_min_likeness")
    try:
        x = float(MIN_LIKENESS if v is None or v == "" else v)
    except (TypeError, ValueError):
        x = MIN_LIKENESS
    return max(0.0, min(x, 1.0))


def _ns(pid: str) -> str:
    from continuity import _ns as ns
    return ns(pid)


def enabled(cfg: dict | None = None) -> bool:
    import models
    cfg = cfg or load_config()
    on = (cfg.get("movie") or {}).get("opening_frames", True)
    return bool(on) and models.is_available(MODEL)


def _head_prompt(db, unit, head_prompt: dict | None) -> dict:
    if head_prompt is not None:
        return head_prompt
    return db.prompt(unit.sids[0], unit.pid) or {}


def opens_fresh(db, unit, cfg: dict | None = None) -> bool:
    """The unit's first shot is a cut (CUT, MATCH_ACTION, or a CONTINUE the
    render cannot continue), not a continuation of the shot before; or it
    carries on a take that reached the take length (Unit.split), which
    opens on a new angle when opening frames can be made."""
    from movie_request import continuation_tail
    mode, _tail, prev = continuation_tail(db, unit.pid, unit.sjsons()[0], [])
    if mode == "CONTINUE" and prev:
        return bool(getattr(unit, "split", False)) and enabled(cfg)
    return True


def planned(db, unit, head_prompt: dict | None = None, cfg: dict | None = None) -> bool:
    """Whether the unit opens on an opening frame (made now or later)."""
    from movie_request import _shot_start_image
    if not enabled(cfg) or _shot_start_image(unit.head) is not None or not opens_fresh(db, unit, cfg):
        return False
    row = _head_prompt(db, unit, head_prompt)
    image = str(row.get("image") or "").strip()
    if row.get("source") in ("hand", "chat") and not image:
        return False                     # written without one: render from the references
    key = _key(db, unit, image) if image else None
    return not (key and time.time() - _FAILED.get(key, 0) < _RETRY_S)


def _key(db, unit, image: str) -> str:
    """The frame's key: its image prompt, its shot, the model, how frames are
    made and, for a match cut, the last frame it is made from (the shot
    before rendered again: its frame is made again)."""
    from movie_request import shot_content
    k = {"image": image, "shot": shot_content(unit.sjsons()[0]), "model": unit.model, "v": KEY_VERSION}
    last = _last_frame(db, unit) if match_opening(db, unit) else None
    if last is not None:
        k["last"] = _digest(last)
    return hashlib.sha1(json.dumps(k, sort_keys=True, ensure_ascii=False,
                                   default=str).encode("utf-8")).hexdigest()[:16]


_DIGESTS: dict[tuple, str] = {}


def _digest(p: Path) -> str:
    st = p.stat()
    k = (str(p), st.st_mtime_ns, st.st_size)
    if k not in _DIGESTS:
        if len(_DIGESTS) > 256:
            _DIGESTS.clear()
        _DIGESTS[k] = hashlib.sha1(p.read_bytes()).hexdigest()[:16]
    return _DIGESTS[k]


def match_opening(db, unit) -> bool:
    """The unit opens as a match cut (a MATCH_ACTION in the place of the shot
    before, or a take past the take length): its frame is made from the
    last frame of the shot before."""
    from movie_request import continuation_tail
    mode, _tail, prev = continuation_tail(db, unit.pid, unit.sjsons()[0], [])
    return mode == "MATCH_ACTION" or (mode == "CONTINUE" and prev and bool(getattr(unit, "split", False)))


def _last_frame(db, unit) -> Path | None:
    """The last frame of the shot before; None while that shot is still to
    render (again): the frame it has is about to change."""
    import movie_prompts
    prev = str(unit.sjsons()[0].get("prev_shot") or "")
    row = (db.get(prev, unit.pid) or {}) if prev else {}
    if row.get("status") in (*movie_prompts.NEEDS_RENDER, "rendering"):
        return None
    tail = row.get("tail_frame")
    return Path(str(tail)) if tail and Path(str(tail)).is_file() else None


def waits(db, unit) -> bool:
    """A match cut whose frame cannot be made yet: the shot before has no
    last frame, or is still to render (again)."""
    return match_opening(db, unit) and _last_frame(db, unit) is None


def frame_path(db, unit, head_prompt: dict | None = None) -> Path | None:
    """Where the unit's opening frame for its current image prompt lives
    (None when there is no image prompt)."""
    image = str(_head_prompt(db, unit, head_prompt).get("image") or "").strip()
    if not image:
        return None
    return FRAME_DIR / _ns(unit.pid) / f"{unit.sids[0]}_{_key(db, unit, image)}.png"


def frame_for(db, unit, head_prompt: dict | None = None, cfg: dict | None = None) -> Path | None:
    """The unit's opening frame when it is planned and made."""
    if not planned(db, unit, head_prompt, cfg):
        return None
    p = frame_path(db, unit, head_prompt)
    return p if p is not None and p.is_file() else None


def ensure(db, bible, unit, cfg: dict | None = None) -> Path | None:
    """The unit's opening frame, made now if it is planned and missing
    (together with the next frames of the project still to make, under one
    lease). None when the unit opens without one, or making it failed."""
    cfg = cfg or load_config()
    have = frame_for(db, unit, cfg=cfg)
    if have is not None or not planned(db, unit, cfg=cfg):
        return have
    if frame_path(db, unit) is None:
        return None                       # no image prompt yet
    import movie_prompts
    todo = [unit]
    for u in movie_prompts.plan_units(db, unit.pid, cfg):
        if len(todo) >= BATCH:
            break
        if u.sids[0] == unit.sids[0] or not any(r.get("status") in movie_prompts.NEEDS_RENDER
                                                 for r in u.rows):
            continue
        # a match cut is made from the last frame of the shot before: not before it exists
        if waits(db, u):
            continue
        if planned(db, u, cfg=cfg) and frame_path(db, u) is not None and frame_for(db, u, cfg=cfg) is None:
            todo.append(u)
    make(db, bible, todo, cfg)
    return frame_for(db, unit, cfg=cfg)


def again(db, bible, unit, cfg: dict | None = None) -> Path | None:
    """Make the unit's frame again, with seeds not tried before (the Shots
    page's Make again); the prompts written for it stay as they are. The
    frame it had stays until the new one replaces it. A match cut's frame is
    made again only once the shot before has its last frame."""
    p = frame_path(db, unit)
    if p is None:
        return None
    _FAILED.pop(_key(db, unit, str(_head_prompt(db, unit, None).get("image") or "").strip()), None)
    if not planned(db, unit, cfg=cfg) or waits(db, unit):
        return None
    make(db, bible, [unit], cfg, again=True)
    return frame_for(db, unit, cfg=cfg)


def _to_render(db, pid: str, cfg: dict | None) -> list:
    import movie_prompts
    return [u for u in movie_prompts.plan_units(db, pid, cfg)
            if any(r.get("status") in movie_prompts.NEEDS_RENDER for r in u.rows)]


def pending(db, pid: str, cfg: dict | None = None) -> list:
    """The units still to render whose frame can be made now: planned, its
    image prompt written, not made yet; a match cut once the shot before it
    has its last frame."""
    return [u for u in _to_render(db, pid, cfg)
            if planned(db, u, cfg=cfg) and frame_path(db, u) is not None and frame_for(db, u, cfg=cfg) is None
            and not waits(db, u)]


def made(db, pid: str, cfg: dict | None = None) -> list[tuple[str, dict]]:
    """(first shot, frame_info) of the units still to render whose frame is made."""
    return [(u.sids[0], frame_info(db, u) or {}) for u in _to_render(db, pid, cfg)
            if frame_for(db, u, cfg=cfg) is not None]


def to_look_at(db, pid: str, cfg: dict | None = None) -> list[tuple[str, dict]]:
    """made() without the frames passed at the keyframes gate (looked_at)."""
    return [(sid, info) for sid, info in made(db, pid, cfg) if not info.get("looked_at")]


def looked_at(db, pid: str, cfg: dict | None = None) -> int:
    """The keyframes gate was passed: the frames made so far are marked looked
    at in their records, so a Stop and Start does not stop at the gate for
    them again (a frame made again, or made later, is not). Returns how many."""
    n = 0
    for u in _to_render(db, pid, cfg):
        p = frame_for(db, u, cfg=cfg)
        info = frame_info(db, u)
        if p is None or info is None or info.get("looked_at"):
            continue
        info["looked_at"] = True
        try:
            p.with_suffix(".json").write_text(json.dumps(info, indent=1), encoding="utf-8")
            n += 1
        except OSError as e:
            log.warning(f"{pid}/{u.sids[0]}: the opening frame's record was not updated: {e}")
    return n


def _refs(db, bible, unit) -> dict:
    """What the frame is made from. `images`: the Qwen references (1-4), most
    important first: the lead's picture, the set (for a match cut the last
    frame of the shot before, else the place picture), the others' pictures,
    and for a match cut the place picture when there is room. One picture per
    person, their still (it carries the outfit): given a face close-up and
    the still, Qwen drew the lead more than once in 2 of 16 key-image tests,
    given one picture in none of 26; given the close-up alone it drew another
    outfit in all 10 (docs/research/MOVIE_CONTINUITY.md §2). `faces`: each
    person's face close-up, for the likeness score. `looks`: each person's
    look, for the prompt."""
    import chain_studio
    import face_ref
    import movie_takes
    import video_models
    from ltx_refs import character_refs, resolve_reference_set
    fam = str((video_models.VIDEO_MODELS.get(unit.model) or {}).get("family") or "")
    sj = dict(unit.sjsons()[0])
    sj.setdefault("project_id", unit.pid)
    refs, _ = resolve_reference_set(sj, bible, family=fam)
    try:
        chars = bible.characters() or {}
    except Exception:  # noqa: BLE001 - the stills still resolve without the names
        chars = {}
    faces: dict[str, Path] = {}
    people: list[Path] = []
    for r in movie_takes.slot_sorted(refs):
        parts = str(r.get("source") or "").split(":")
        if parts[0] != "character" or len(parts) < 2:
            continue
        # the person's picture the video model gets (their outfit or loadout still)
        still = Path(r["path"])
        # scored against the face of the identity still, not of a loadout still where it is small
        own = character_refs(unit.pid, parts[1], chars.get(parts[1]))
        base = own[0] if own else still
        name = str((chars.get(parts[1]) or {}).get("name") or parts[1])
        faces[name if name not in faces else f"{name} ({parts[1]})"] = (
            face_ref.face_closeup(base, chain_studio.FACE_CACHE) or base)
        people.append(still)
    place = [Path(r["path"]) for r in refs if str(r.get("source") or "").startswith("location:")]
    last = _last_frame(db, unit) if match_opening(db, unit) else None
    order = people[:1] + ([last] if last else place[:1]) + people[1:] + (place[:1] if last else [])
    images: list[Path] = []
    for p in order:
        if p not in images and p.is_file():
            images.append(p)
    looks = [str(c["description"]) for c in movie_takes.take_cast(refs, bible, sj)
             if c.get("type") == "face" and c.get("description")]
    return {"images": images[:_MAX_REFS], "faces": faces, "looks": looks, "last": last}


def with_looks(prompt: str, looks: list[str]) -> str:
    """The image prompt with everyone's look after its first sentence, as the
    LTX takes carry it (chain_studio.with_cast_line): the references show the
    faces, the words say who is who."""
    if not looks:
        return prompt
    line = "The people: " + "; ".join(x.rstrip(". ") for x in looks) + "."
    i = prompt.find(". ")
    return f"{prompt} {line}" if i < 0 else f"{prompt[:i + 1]} {line}{prompt[i + 1:]}"


def frame_info(db, unit, head_prompt: dict | None = None) -> dict | None:
    """The record kept beside the unit's frame: its likeness (the least-alike
    person's ArcFace score, None when no face was scored), each person's,
    whether it is weak, the candidates tried and the references."""
    p = frame_path(db, unit, head_prompt)
    if p is None:
        return None
    try:
        return json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _size(unit) -> tuple[int, int]:
    from movie_request import studio_size
    w, h = studio_size(unit.model, True)
    return (768, 1344) if h > w else (1344, 768)


def make(db, bible, units: list, cfg: dict | None = None, again: bool = False) -> list[Path]:
    """Make the opening frames of `units` with Qwen-Image 2.1 fast, under one
    GPU lease (`again`: also the ones made already, with new seeds). Returns
    the frames made; a failure is logged and remembered."""
    import gpu_broker
    import models
    from renderer import ComfyClient
    cfg = cfg or load_config()
    m = models.get_model(MODEL) or {}
    port = int(m.get("comfy_port") or 8190)
    jobs = []
    for u in units:
        dest = frame_path(db, u)
        image = str(_head_prompt(db, u, None).get("image") or "").strip()
        if dest is not None and image and (again or not dest.is_file()):
            jobs.append((u, dest, image))
    if not jobs:
        return []
    made: list[Path] = []
    lease = None
    try:
        lease = gpu_broker.acquire(model=MODEL, port=port, purpose="image", timeout=3600)
        client = ComfyClient(port)
        _ready(client)
        for u, dest, image in jobs:
            try:
                made.append(_make_one(client, db, bible, u, dest, image, m, cfg))
                info = frame_info(db, u) or {}
                log.info(f"{u.pid}/{u.sids[0]}: opening frame made ({dest.name}, likeness "
                         f"{info.get('likeness')}{', weak' if info.get('weak') else ''})")
            except Exception as e:  # noqa: BLE001 - the shot renders from its references instead
                _FAILED[_key(db, u, image)] = time.time()
                log.warning(f"{u.pid}/{u.sids[0]}: opening frame failed ({e}) - "
                            f"the shot renders from its references")
    except Exception as e:  # noqa: BLE001
        for u, _dest, image in jobs:
            _FAILED[_key(db, u, image)] = time.time()
        log.warning(f"opening frames not made: {e}")
    finally:
        if lease is not None:
            gpu_broker.release(lease)
    return made


def _ready(client, timeout_s: float = 180) -> None:
    """The Qwen-Image instance answers (a lease may have just restarted it)."""
    t0 = time.time()
    while True:
        try:
            client.system_stats()
            return
        except Exception:  # noqa: BLE001
            if time.time() - t0 > timeout_s:
                raise RuntimeError(f"the image server on port {client.port} is not up")
            time.sleep(5)


def _make_one(client, db, bible, unit, dest: Path, image: str, m: dict,
              cfg: dict | None = None) -> Path:
    """The candidates of one frame, scored; the best becomes `dest`, with
    its record beside it (frame_info)."""
    import face_drift
    refs = _refs(db, bible, unit)
    prompt = with_looks(image, refs["looks"])
    uploaded = [client.upload_image(p) for p in refs["images"]]
    row = db.get(unit.sids[0], unit.pid) or {}
    seed = int(row.get("seed") or db.stable_seed(row)) + 1
    try:        # made again: on from the seeds tried before
        seed = int(json.loads(dest.with_suffix(".json").read_text(encoding="utf-8")).get("next_seed") or seed)
    except (OSError, ValueError, TypeError):
        pass
    scored = bool(refs["faces"]) and face_drift.available()
    if refs["faces"] and not scored and unit.pid not in _NOTED:
        _NOTED.add(unit.pid)
        log.info(f"{unit.pid}: InsightFace is not installed - one opening frame per shot, not scored")
    floor = min_likeness(cfg)
    want = candidates(cfg) if scored else 1
    tried: list[dict] = []
    try:
        while len(tried) < want:
            k = len(tried)
            path = dest.with_name(f"{dest.stem}_c{k}.png")
            try:
                _render(client, unit, prompt, uploaded, seed + k, m, path)
            except Exception as e:  # noqa: BLE001 - the candidates made so far still count
                if not tried:
                    raise
                log.warning(f"{unit.pid}/{unit.sids[0]}: opening frame candidate {k + 1} failed ({e})")
                break
            people = face_drift.likeness(path, refs["faces"]) if scored else None
            sims = list((people or {}).values())
            low = min(sims) if sims and None not in sims else None      # the least-alike person counts
            tried.append({"file": path, "seed": seed + k, "people": people, "likeness": low})
            best = max(tried, key=lambda c: (c["likeness"] is not None, c["likeness"] or 0.0))
            if (len(tried) == want and want < candidates(cfg) + _EXTRA and best["likeness"] is not None
                    and best["likeness"] < floor):
                want = candidates(cfg) + _EXTRA        # under the floor: up to two more
        best = max(tried, key=lambda c: (c["likeness"] is not None, c["likeness"] or 0.0))
        Path(best["file"]).replace(dest)
    finally:
        for c in tried:
            Path(c["file"]).unlink(missing_ok=True)
    low = best["likeness"]
    info = {"likeness": low, "people": best["people"], "weak": low is not None and low < floor,
            "min_likeness": floor, "scored": scored, "seed": best["seed"],
            "next_seed": max(c["seed"] for c in tried) + 1,
            "candidates": [{"seed": c["seed"], "likeness": c["likeness"]} for c in tried],
            "references": [p.name for p in refs["images"]],
            "last_frame": refs["last"].name if refs["last"] else None}
    dest.with_suffix(".json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    return dest


def _render(client, unit, prompt: str, uploaded: list[str], seed: int, m: dict, dest: Path) -> None:
    import models
    w, h = _size(unit)
    steps, cfgv = int(m.get("steps") or 8), float(m.get("cfg") or 1.0)
    if uploaded:
        # Qwen-Image 2.1 takes its references natively (models.build_ref_workflow)
        wf = models.build_ref_workflow(MODEL, prompt, "", seed, w, h, steps, cfgv, None,
                                       uploaded, 1.0, False, shot_id=f"open_{unit.sids[0]}")
    else:
        wf = models.build_workflow(MODEL, prompt, "", seed, w, h, steps, cfgv, None,
                                   shot_id=f"open_{unit.sids[0]}")
    pid = client.queue_prompt(wf)
    res = client.wait_for(pid, timeout_s=900)
    if not res:
        raise TimeoutError("no image after 15 minutes")
    st = res.get("status") or {}
    if st.get("status_str") not in (None, "success"):
        raise RuntimeError(f"the image graph failed: {json.dumps(st.get('messages') or [])[:300]}")
    img = next((i for o in (res.get("outputs") or {}).values() for i in (o.get("images") or [])), None)
    if not img:
        raise RuntimeError("the image graph returned no image")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.png")
    client.download(img.get("subfolder", ""), img["filename"], tmp)
    if not tmp.is_file() or tmp.stat().st_size < 1000:
        raise RuntimeError("the downloaded image is empty")
    tmp.replace(dest)
