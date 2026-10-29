"""The video prompts of a movie: written by an AI or by hand, never from a
template.

Every LTX-2.5 / MiniMax H3 unit of a movie (movie_request: one shot, or a
seamless take of CONTINUE shots) renders with prompts written for its exact
Video Studio request:

  by an AI    the writer sends the unit's brief: the story so far and where
              the shot before left off, the shots to write with everything in
              them, what follows, and the cast sheet of the request
              (prompt_check: how the model reads a prompt, the pictures it
              receives, how the unit opens and continues). The reply goes
              through the Video Studio's prompt check: the check's own fixes
              are applied and any error goes back to the AI to fix. When the
              endpoint takes tools, the AI can also look up shots, scenes,
              characters and earlier prompts, check its prompts, and hands
              them in with a tool that checks them again.
  by hand     typed on the Shots page, or pasted from a chat AI that was given
              the brief (chat_brief / import_reply). The AI never overwrites a
              hand prompt.

A shot without a usable prompt waits (status "prompt") with the reason.

Each prompt is stored with what it was written for (shot_sigs): the model,
the shot, how it opens or that it continues the shot before, and the cast in
the model's order. When any of that changes, an AI prompt is written again,
and a hand prompt waits for its author to look at it.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Callable

from core import load_config, log_setup
from movie_request import MovieRequest, Unit

log = log_setup("movie_prompts")

NEEDS_RENDER = ("pending", "queued", "failed", "prompt")
DONE = ("rendered", "post", "qa", "passed", "review", "assembled")
WAITING = "prompt"                    # the status of a shot waiting for its prompt
UNREACHED = "the AI could not be reached"   # the reason when nothing answers at the AI
HAND = ("hand", "chat")
# prompt check issues whose own fixes are applied (the Fix buttons of the
# Video Studio's check): label spelling, the opening and continuity lines
# and the cast's look in a clip that leaves it out
AUTO_FIX = {"tag_spelling", "tag_bare", "name_untagged", "name_tag", "media_tag",
            "tag_ignored", "opening", "continuity", "look"}
_REPAIRS = 2                          # times an AI is asked to fix its errors
_TURNS = 10                           # tool rounds per unit before giving up
_MAX_TOKENS = 8192
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(pid: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(pid, threading.Lock())


def _sjson(row: dict | None) -> dict:
    j = (row or {}).get("json")
    return j if isinstance(j, dict) else {}


def _words(s: Any) -> str:
    return " ".join(str(s or "").replace("_", " ").split())


def _digest(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                   default=str).encode("utf-8")).hexdigest()[:20]


def _issue_dicts(issues: list, seg: int | None) -> list[dict]:
    return [{"level": i.level, "code": i.code, "message": i.message}
            for i in issues if i.segment == seg or (seg == 1 and i.segment is None)]


# ------------------------------------------------------------------- units
def plan_units(db, pid: str, cfg: dict | None = None) -> list[Unit]:
    """The project's LTX-2.5 / H3 units in story order (scenes in order, each
    in its prev_shot order), whatever the shots' statuses: a run of CONTINUE
    shots on a model with the seamless engine is one take (up to the model's
    take length, movie_takes.max_shots), every other shot is a unit of its
    own. Wan 2.2 shots are in none."""
    import movie_takes
    import video_studio
    from movie_request import NoVideoModel, shot_model, takes_on
    from shots_db import story_order
    cfg = cfg or load_config()
    on = takes_on(db, pid, cfg)
    length = movie_takes.project_length(db, pid)
    rows = db.all(project=pid)
    models: dict[str, str] = {}

    def model_of(r: dict) -> str:
        sid = r["shot_id"]
        if sid not in models:
            try:
                models[sid] = shot_model(db, r, _sjson(r), pid)
            except NoVideoModel:
                models[sid] = ""
        return models[sid]

    units: list[Unit] = []
    for scene in sorted({str(r.get("scene") or "") for r in rows}):
        ordered = story_order([r for r in rows if str(r.get("scene") or "") == scene])
        cur: Unit | None = None
        for r in ordered:
            mid = model_of(r)
            if not mid:
                cur = None
                continue
            joins = (cur is not None and on and mid == cur.model and video_studio.seamless_engine(mid)
                     and _joins(cur.rows[-1], r, ordered))
            if cur is not None and joins and len(cur.rows) < movie_takes.max_shots(cfg, mid, length):
                cur.rows.append(r)
            else:
                # a run longer than the take length goes on in a new take
                cur = Unit(pid, mid, [r], split=joins)
                units.append(cur)
    return units


def _joins(prev: dict, row: dict, scene_rows: list[dict]) -> bool:
    """`row` carries `prev`'s camera take on in the same take: it continues it
    (movie_takes.continues), neither is a forced text-to-video render, and no
    other shot continues `prev` (a fork ends the take)."""
    import movie_takes
    if _sjson(prev).get("t2v") or _sjson(row).get("t2v") or not movie_takes.continues(prev, row):
        return False
    return sum(1 for r in scene_rows if str(_sjson(r).get("prev_shot") or "") == prev["shot_id"]) == 1


def unit_for(db, pid: str, sid: str, cfg: dict | None = None,
             units: list[Unit] | None = None) -> Unit | None:
    """The unit `sid` renders in: its whole planned unit. A take renders as
    one piece, so a retake of a shot inside it renders the take again under
    its chain id: the shots before it reuse their finished steps (the same
    seeds and prompts) and the latent carry into it stays; it and the shots
    after it render anew. None for a shot on the legacy Wan model."""
    for u in units if units is not None else plan_units(db, pid, cfg):
        if sid in u.sids:
            return u
    return None


def follow_ons(db, last: dict, pid: str, cfg: dict | None = None,
               units: list[Unit] | None = None) -> list[dict]:
    """The shots that open on the last frame of `last`: a CONTINUE of it
    outside its take, and a match cut whose opening frame is made from that
    frame. When `last` renders again, the ones with a clip render again too
    (orchestrator._cascade)."""
    import movie_takes
    import opening_frames
    from shotspec import continuity_of
    out = []
    for r in db.all(scene=str(last.get("scene") or "") or None, project=pid):
        if str(_sjson(r).get("prev_shot") or "") != last["shot_id"]:
            continue
        if movie_takes.continues(last, r):
            out.append(r)
        elif continuity_of(_sjson(r)) == "MATCH_ACTION":
            u = unit_for(db, pid, r["shot_id"], cfg, units)
            if u is not None and u.sids[0] == r["shot_id"] and opening_frames.match_opening(db, u) \
                    and opening_frames.planned(db, u, cfg=cfg):
                out.append(r)
    return out


def rerenders(db, pid: str, sid: str, sjson: dict, cfg: dict | None = None) -> list[str]:
    """The shots with a clip that render again when `sid`'s record becomes
    `sjson` (a continuity change): the take `sid` then renders in renders
    whole, so its other shots from a different take, and those after `sid`,
    render anew; then every shot that opens on the last frame of a unit
    rendered again (follow_ons) renders again with its unit. Nothing is
    written."""
    import movie_takes

    class _After:
        """`db` with `sid`'s record replaced."""

        def __getattr__(self, name):
            return getattr(db, name)

        @staticmethod
        def _swap(r):
            return {**r, "json": sjson} if r and r.get("shot_id") == sid else r

        def all(self, *a, **kw):
            return [self._swap(r) for r in db.all(*a, **kw)]

        def get(self, *a, **kw):
            return self._swap(db.get(*a, **kw))
    after = _After()
    units = plan_units(after, pid, cfg)
    unit = unit_for(after, pid, sid, cfg, units)
    if unit is None:
        return []
    out: list[str] = []
    if unit.chain:
        tid = movie_takes.take_id(pid, unit.model, unit.sids)
        at = unit.sids.index(sid)
        out = [s for i, (s, r) in enumerate(zip(unit.sids, unit.rows))
               if s != sid and r.get("status") not in NEEDS_RENDER
               and (i > at or str((_sjson(r).get("take") or {}).get("id") or "") != tid)]
    todo, seen = [unit], {unit.sids[0]}
    while todo:
        u = todo.pop(0)
        for r in follow_ons(after, u.rows[-1], pid, cfg, units):
            nxt = unit_for(after, pid, r["shot_id"], cfg, units)
            if r.get("status") not in DONE or nxt is None or nxt.sids[0] in seen:
                continue
            seen.add(nxt.sids[0])
            out += [s for s, rr in zip(nxt.sids, nxt.rows)
                    if s != sid and s not in out and rr.get("status") not in NEEDS_RENDER]
            todo.append(nxt)
    return out


def remaining(u: Unit) -> Unit | None:
    """`u` when a shot of it is still to render (it renders whole); else None."""
    return u if any(r.get("status") in NEEDS_RENDER for r in u.rows) else None


def to_render(u: Unit) -> list[str]:
    """The shots of `u` still to render: the ones a retake or a failure moves
    (the others are rendered again only to keep the take in one piece)."""
    return [str(r["shot_id"]) for r in u.rows if r.get("status") in NEEDS_RENDER]


def request_for(db, bible, unit: Unit, *, planning: bool = True,
                head_prompt: dict | None = None, cfg: dict | None = None) -> MovieRequest:
    """The request `unit` renders with (movie_request.build). `planning`: for
    writing its prompts before the shot before it has rendered or its
    opening frame is made. `head_prompt` stands in for the first shot's
    stored prompt (it decides whether the unit opens on an opening frame)."""
    import movie_request
    import opening_frames
    frame = opening_frames.frame_for(db, unit, head_prompt, cfg)
    planned = planning and frame is None and opening_frames.planned(db, unit, head_prompt, cfg)
    return movie_request.build(db, bible, unit, opening_frame=frame, planning=planning,
                               opening_planned=planned)


# -------------------------------------------------------------- signatures
def _cast_key(plan) -> list:
    """The cast as the prompts depend on it: H3 by its <Picture N> numbers,
    LTX by who and what is in it (LTX reads no labels)."""
    fixed = [r for r in plan.refs if r.kind not in ("start image", "previous clip")]
    if plan.family == "h3":
        return [[r.picture, r.kind, r.name, r.look] for r in fixed]
    return sorted([r.kind, r.name, r.look] for r in fixed)


def shot_sigs(db, req: MovieRequest, plan=None) -> list[str]:
    """What each shot's prompt is written for: the model and clip length, how
    the shot opens (a cut and what it opens on, a match cut, or carrying on
    from the shot before), the cast, the shot itself, and for a continuation
    or a match cut the shot before it."""
    from movie_request import shot_content
    plan = plan or req.plan()
    cast = _cast_key(plan)
    sjs = req.unit.sjsons()
    out = []
    for i, sj in enumerate(sjs):
        if i:
            role, prev = "continues", shot_content(sjs[i - 1])
        else:
            role = {"continue": "continues", "match_action": f"match:{plan.opening}"}.get(
                req.opening, f"cut:{plan.opening}")
            prev = _prev_content(db, req.unit.pid, sj) if req.opening != "cut" else None
        out.append(_digest({"model": req.model, "frames": req.frames, "role": role,
                            "cast": cast, "shot": shot_content(sj), "prev": prev}))
    return out


def _prev_content(db, pid: str, sj: dict) -> dict | None:
    from movie_request import shot_content
    prev = db.get(str(sj.get("prev_shot") or ""), pid) if sj.get("prev_shot") else None
    return shot_content(_sjson(prev)) if prev else None


def states(db, req: MovieRequest, plan=None) -> list[dict]:
    """Per shot of the request: {sid, text, source, state, why, sig, image,
    issues}; state is "ready", "missing" or "stale" (written for something
    else)."""
    plan = plan or req.plan()
    sigs = shot_sigs(db, req, plan)
    stored = db.prompts(req.unit.pid, req.unit.sids)
    out = []
    for sid, sig in zip(req.unit.sids, sigs):
        row = stored.get(sid) or {}
        text, source = str(row.get("text") or ""), str(row.get("source") or "")
        if not text.strip():
            st, why = "missing", "no prompt yet"
        elif row.get("sig") != sig:
            st = "stale"
            why = ("written for an earlier version of the shot or of how it opens: "
                   + ("check it and save it again" if source in HAND else "it is written again"))
        else:
            st, why = "ready", ""
        out.append({"sid": sid, "text": text, "source": source, "state": st, "why": why,
                    "sig": sig, "image": str(row.get("image") or ""),
                    "issues": row.get("issues") or []})
    return out


def prompts_for(db, req: MovieRequest) -> tuple[list[str] | None, dict[str, str]]:
    """The prompts `req` renders with: its shots' stored prompts, written for
    it, with the prompt check's own fixes applied and no error left. (None,
    {sid: why}) when a shot has none it may render with."""
    plan = req.plan()
    bad = [i for i in plan.issues if i.level == "error"]
    if bad:
        return None, {sid: bad[0].message for sid in req.unit.sids}
    st = states(db, req, plan)
    problems = {s["sid"]: s["why"] for s in st if s["state"] != "ready"}
    if problems:
        return None, problems
    texts, issues = autofix(plan, [s["text"] for s in st])
    errors = [i for i in issues if i.level == "error"]
    if errors:
        out: dict[str, str] = {}
        for i in errors:
            for sid in ([req.unit.sids[i.segment - 1]] if i.segment else req.unit.sids):
                out.setdefault(sid, i.message)
        return None, out
    return texts, {}


# ---------------------------------------------------------- the check's fixes
def autofix(plan, prompts: list[str]) -> tuple[list[str], list]:
    """Apply the prompt check's own fixes for the AUTO_FIX issues until none is
    left; returns the prompts and the issues that remain."""
    import prompt_check
    texts = [str(t or "") for t in prompts]
    for _ in range(6):
        issues = prompt_check.check(plan, texts)
        fixes: dict[int, list[dict]] = {}
        for i in issues:
            if i.code in AUTO_FIX and i.segment and i.fixes:
                fixes.setdefault(i.segment, []).extend(i.fixes)
        changed = False
        for seg, items in fixes.items():
            t, done = texts[seg - 1], []
            # back to front, so the offsets of the fixes still to apply hold;
            # one fix per place per pass (the next pass checks again)
            for f in sorted(items, key=lambda f: (f["start"], f["end"]), reverse=True):
                s, e = int(f["start"]), int(f["end"])
                if t[s:e] != f["find"] or any(s <= de and ds <= e for ds, de in done):
                    continue
                t = t[:s] + f["replace"] + t[e:]
                done.append((s, s + len(f["replace"])))
                changed = True
            texts[seg - 1] = t
        if not changed:
            return texts, issues
    return texts, prompt_check.check(plan, texts)


# ----------------------------------------------------------------- the brief
_LEVELS = ((3, "subtle: an aura, sparks"), (6, "a controlled display"),
           (9, "violent, it damages the surroundings"), (10, "cataclysmic"))
_JOIN = {
    "cut": "A cut: a new shot with its own framing. The story carries on from the shot before.",
    "match_action": ("A match cut: the camera cuts to a new angle in the middle of the action. "
                     "The action of the shot before is already under way and carries on from exactly "
                     "where it stopped; do not restart it. Show it from this shot's camera."),
    "continue": ("No cut: the same camera take carries on from the last frame of the shot before, "
                 "with the same framing, light and positions."),
}

SYSTEM = (
    "You write the video prompts of a movie for a video generation model, one shot or one "
    "seamless take at a time. The brief gives the story so far and where the shot before left "
    "off, the shots to write, and the rules of the render model. Follow the model's rules "
    "exactly: they decide whether the model can use its reference pictures, and a prompt that "
    "breaks them is refused. Write what is seen and heard, concretely and physically, in the "
    "present tense: the camera, the action from its start to its end, the light, the sound. "
    "Describe every person only with the look the cast sheet gives them, in the same words in every "
    "shot: give no one other clothes, hair, an age or props. Put no one in a shot who is not listed "
    "in it. Every line of dialogue a shot lists is spoken in its prompt, word for word, in quotes. "
    "Continue the story exactly where the shot before left it. Never use the names of famous film, "
    "game or comic characters, and add no commentary: reply in the reply format only.")
TOOLS_NOTE = (
    "\n\nYou have tools: look up any shot, scene, character or earlier prompt when you need "
    "more of the story, check your prompts with the model's prompt check, and hand them in with "
    "submit_prompts, which checks them again and tells you what to fix.")


def _beats(proj: dict) -> list[dict]:
    b = (proj.get("story") or {}).get("beats") or {}
    items = b.get("beats") if isinstance(b, dict) else b
    return [x for x in items or [] if isinstance(x, dict)]


def _clip(v: Any, n: int) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _outline(proj: dict, scene: str) -> list[str]:
    """The film in one line per scene, the scene of the shots marked: every
    brief carries the whole story, so each prompt keeps to where it is."""
    beats = _beats(proj)
    if not beats:
        return []
    out = ["THE FILM (>> = the scene of these shots)"]
    for b in beats:
        sc = str(b.get("scene") or "")
        line = f"{'>>' if sc == scene else '  '} {sc}: {_clip(b.get('goal'), 140) or '(no goal written)'}"
        if b.get("turn"):
            line += f" -> {_clip(b['turn'], 110)}"
        if b.get("hook"):
            line += f"; it ends on {_clip(b['hook'], 110)}"
        out.append(line)
    return out


def _scene_line(proj: dict, locs: dict, scene: str, location: str = "") -> str:
    """The scene's beat in one line: where (the beat's location, else
    `location`, the shot's), the light, time and mood, its goal, turn and end."""
    beats = _beats(proj)
    beat = next((b for b in beats if str(b.get("scene")) == scene), {})
    idx = next((i for i, b in enumerate(beats, 1) if str(b.get("scene")) == scene), 0)
    loc_id = str(beat.get("location") or location or "")
    loc = locs.get(loc_id) or {}
    where = _words(loc.get("name") or loc_id)
    parts = [f"Scene {scene}" + (f" ({idx} of {len(beats)})" if idx else "")]
    if where:
        parts.append(f"at {where}" + (f" ({loc['descriptor']})" if loc.get("descriptor") else ""))
    if loc.get("lighting"):
        parts.append(f"light: {loc['lighting']}")
    for k in ("time", "mood"):
        if beat.get(k):
            parts.append(f"{k}: {beat[k]}")
    line = ", ".join(parts) + "."
    for k, label in (("goal", "Goal"), ("turn", "Turn"), ("hook", "It ends on")):
        if beat.get(k):
            line += f" {label}: {beat[k]}."
    return line


def _state(v: Any) -> str:
    if isinstance(v, dict):
        return "; ".join(f"{_words(k)}: {x}" for k, x in v.items() if x not in (None, "", [], {}))
    return str(v or "")


def _one_line(sid: str, sj: dict) -> str:
    from shotspec import continuity_of
    act = str(sj.get("primary_action") or sj.get("narrative") or "").strip()
    end = _state(sj.get("end_state"))
    return (f"{sid} [{continuity_of(sj)}, {_words(sj.get('shot_size')) or 'shot'}]: "
            f"{act or '(no action written)'}" + (f" It ends with: {end}." if end else ""))


def _merge_state(state: dict[str, dict], sj: dict) -> dict[str, dict]:
    """`state` with the persisting state_mutations of the shot `sj` applied."""
    out = {k: dict(v) for k, v in state.items()}
    for m in sj.get("state_mutations") or []:
        if isinstance(m, dict) and m.get("persist") and m.get("target") and m.get("key"):
            out.setdefault(str(m["target"]), {})[str(m["key"])] = m.get("value")
    return out


def shot_lines(sj: dict, chars: dict, crowd: dict | None = None,
               state: dict[str, dict] | None = None) -> list[str]:
    """The shot as the prompt writer reads it: camera, action, who is in it and
    how they look and act, power, dialogue, sound, and the states it starts
    and ends on."""
    from ltx_refs import resolve_presentation
    out = []
    cam = [_words(sj.get("shot_size")), _words(sj.get("camera"))]
    if sj.get("lens"):
        cam.append(f"{sj['lens']} lens")
    if sj.get("composition"):
        cam.append(str(sj["composition"]))
    out.append("- Camera: " + (", ".join(x for x in cam if x) or "free"))
    if sj.get("primary_action"):
        out.append(f"- The main action: {sj['primary_action']}")
    if sj.get("secondary_motion"):
        out.append(f"- Also moving: {sj['secondary_motion']}")
    for k, label in (("narrative", "Story"), ("beat", "Beat")):
        if sj.get(k):
            out.append(f"- {label}: {sj[k]}")
    weather = sj.get("weather") if sj.get("weather") not in (None, "", "inherit") else ""
    if sj.get("time") or weather:
        out.append("- " + ", ".join(x for x in (f"time: {sj['time']}" if sj.get("time") else "",
                                                 f"weather: {weather}" if weather else "") if x))
    for c in sj.get("characters") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        char = chars.get(str(c["id"])) or {}
        pres = resolve_presentation(char, c, sj)
        act = str(c.get("action") or "")
        bits = [x for x in (
            str(c.get("emotion") or ""),
            act if act and act != str(sj.get("primary_action") or "") else "",
            ", ".join(_words(c.get(k)) for k in ("position", "facing") if c.get(k)),
            f"wearing {pres['outfit']}" if pres.get("outfit") else "",
            (f"holding {pres['weapon_txt']}" + (f" ({pres['handling']})" if pres.get("handling") else ""))
            if pres.get("weapon_txt") else "") if x]
        out.append(f"- {char.get('name') or c['id']} ({c.get('tier') or 'cast'}): " + "; ".join(bits))
        held = (state or {}).get(str(c["id"])) or {}
        if held:
            out.append(f"  still true from earlier: {_state(held)}")
    for b in sj.get("visible_background") or []:
        d = (crowd or {}).get(b) or {}
        out.append(f"- In the background: {_words(b)}" + (f" ({d['descriptor']})" if d.get("descriptor") else ""))
    if sj.get("power"):
        pw_id = str(sj["power"])
        owner = next((chars.get(str(c.get("id"))) for c in sj.get("characters") or []
                      if isinstance(c, dict) and pw_id in ((chars.get(str(c.get("id"))) or {})
                                                           .get("powers") or {})), None) or {}
        pw = (owner.get("powers") or {}).get(pw_id) or {}
        lvl = sj.get("power_level")
        how = next((t for top, t in _LEVELS if isinstance(lvl, int) and lvl <= top), "")
        out.append("- Power: " + (f"{owner['name']}'s " if owner.get("name") else "") + _words(pw_id)
                   + (f" ({pw['descriptor']})" if pw.get("descriptor") else "")
                   + (f", level {lvl} of 10: {how}" if lvl else ""))
    for d in sj.get("dialogue") or []:
        if isinstance(d, dict) and d.get("line"):
            who = str((chars.get(str(d.get("char"))) or {}).get("name") or d.get("char") or "someone")
            out.append(f"- {who} says" + (f" ({d['emotion']})" if d.get("emotion") else "")
                       + f": \"{d['line']}\"")
    if sj.get("diegetic_sounds"):
        out.append(f"- Sounds: {sj['diegetic_sounds']}")
    for k, label in (("start_state", "At the start"), ("end_state", "It must end on")):
        s = _state(sj.get(k))
        if s:
            out.append(f"- {label}: {s}")
    place = (state or {}).get(str(sj.get("location") or "")) or {}
    if place:
        out.append(f"- The place, still from earlier: {_state(place)}")
    return out


def story_rows(db, pid: str) -> list[dict]:
    """Every shot of the project in story order."""
    from shots_db import story_order
    rows = db.all(project=pid)
    out: list[dict] = []
    for scene in sorted({str(r.get("scene") or "") for r in rows}):
        out += story_order([r for r in rows if str(r.get("scene") or "") == scene])
    return out


def wants_image(req: MovieRequest, hand: dict[str, str]) -> bool:
    """The unit opens on an opening frame still to be made, and its first
    shot's prompt is being written now: write the frame's prompt with it."""
    return bool(req.frame_planned) and req.unit.sids[0] not in hand


_IMAGE_RULE = ("Write that image's prompt as well: one paragraph describing the first frame exactly "
               "(shot size and angle, the setting and its light, who is where, how they look, their "
               "pose at that instant). No motion, no sound and no camera move in it. Segment 1 then "
               "starts from that frame.")


# the most segments one reply writes: a longer take is written in parts, each
# with the whole brief and the prompts written before it
SEGMENTS_PER_REPLY = 4


def segments_per_reply(cfg: dict | None) -> int:
    """`movie.prompt_segments_per_reply` (default SEGMENTS_PER_REPLY)."""
    try:
        n = int(((cfg or {}).get("movie") or {}).get("prompt_segments_per_reply") or SEGMENTS_PER_REPLY)
    except (TypeError, ValueError):
        n = SEGMENTS_PER_REPLY
    return max(1, min(n, 50))


def parts(want: list, n: int) -> list[list]:
    """`want` (the shots to write, in story order) in as few replies of at
    most `n` as there can be, of even sizes (6 in replies of 4: 3 and 3)."""
    n = max(1, int(n))
    k = -(-len(want) // n)
    out, at = [], 0
    for i in range(k):
        size = len(want) // k + (1 if i < len(want) % k else 0)
        out.append(want[at:at + size])
        at += size
    return out


def unit_brief(db, bible, proj: dict, req: MovieRequest, plan=None,
               hand: dict[str, str] | None = None, *, tools: bool = False,
               reply: bool = True, head: bool = True,
               shared: frozenset[str] = frozenset(), story_so_far: bool = True,
               next_shot: bool = True, image_rule: bool = True,
               later: frozenset[str] | set[str] = frozenset()) -> str:
    """The brief for writing one unit's prompts: the film, the story so far,
    where the shot before left off, the shots with everything that is in
    them, what follows, and the cast sheet of the exact request.

    `hand`: prompts already written (kept exactly, the others written around
    them). `later`: shots of the take written in a later reply, shown for
    where the take goes but not written now (a take is written in parts of
    segments_per_reply).

    A chat brief of several units (chat_brief) says what they share once:
    the film (`head` False), the story so far before its first part
    (`story_so_far` False for the others: the parts before them are right
    above), the shot after its last part (`next_shot`), the blocks of the
    cast sheet in `shared`, and how an opening image is written
    (`image_rule` False)."""
    import prompt_check
    plan = plan or req.plan()
    hand = hand or {}
    later = frozenset(later)
    unit, sids = req.unit, req.unit.sids
    chars, locs = bible.characters() or {}, bible.locations() or {}
    try:
        crowd = bible.crowd_types() or {}
    except Exception:  # noqa: BLE001
        crowd = {}
    ordered = story_rows(db, unit.pid)
    pos = {r["shot_id"]: i for i, r in enumerate(ordered)}
    head_i = pos.get(sids[0], 0)
    before, after = ordered[:head_i], ordered[pos.get(sids[-1], head_i) + 1:]
    scene = str(unit.head.get("scene") or "")
    take = len(sids) > 1
    L: list[str] = []
    add = L.append

    genre = ", ".join(x for x in (proj.get("genre"), proj.get("tone")) if x)
    if head:
        add(f"BRIEF: the video prompt{'s of one seamless take' if take else ' of one shot'} of "
            f"\"{proj.get('title') or 'the movie'}\"" + (f" ({genre})" if genre else "") + ".")
        tr = (proj.get("story") or {}).get("treatment") or {}
        if isinstance(tr, dict) and tr.get("logline"):
            add(f"Logline: {tr['logline']}")
        film = _outline(proj, scene)
        if film:
            add("")
            L.extend(film)
        add("")
    if story_so_far:
        add("THE STORY SO FAR")
        add(_scene_line(proj, locs, scene, str(_sjson(unit.head).get("location") or "")))
        in_scene = [r for r in before if str(r.get("scene") or "") == scene]
        if in_scene:
            add("Earlier in this scene:")
            for r in in_scene[-6:]:
                add("- " + _one_line(r["shot_id"], _sjson(r)))
        elif before:
            add(f"This is the first shot of the scene. The scene before ({before[-1].get('scene')}) ended with:")
            add("- " + _one_line(before[-1]["shot_id"], _sjson(before[-1])))
        else:
            add("This is the first shot of the movie.")
        if before:
            last = before[-1]
            text = str((db.prompt(last["shot_id"], unit.pid) or {}).get("text") or "").strip()
            if text:
                add(f"The prompt of the shot before ({last['shot_id']}) ended: \"...{text[-700:]}\"")
        add("")
    add(f"HOW {'THE TAKE' if take else 'THE SHOT'} JOINS THE SHOT BEFORE")
    add(_JOIN.get(req.opening, _JOIN["cut"]))
    for w in req.warnings:
        if "forced to CUT" in w or "rendered as a cut" in w:
            add(f"(It is marked {_sjson(unit.head).get('continuity')}, but {w}.)")
    if take:
        add(f"The take is {len(sids)} segments rendered as one continuous shot with no cuts, one "
            f"segment per shot below.")
        now = [k for k, s in enumerate(sids, 1) if s not in hand and s not in later]
        if now and (later or len(now) < len(sids)):
            which = (f"{now[0]} to {now[-1]}" if len(now) > 2 and now == list(range(now[0], now[-1] + 1))
                     else ", ".join(map(str, now[:-1])) + f" and {now[-1]}" if len(now) > 1
                     else str(now[0]))
            notes = (["the segments with a prompt are written (keep them as they are and carry on from them)"]
                     if any(s in hand for s in sids) else [])
            if later:
                notes.append("the ones marked later are written in the next reply (end so that they can follow)")
            add(f"This reply writes segment{'s' if len(now) > 1 else ''} {which} of {len(sids)}: "
                + ", ".join(notes) + ".")
    if req.start_source == "opening frame" and not req.frame_planned:
        image = str((db.prompt(sids[0], unit.pid) or {}).get("image") or "").strip()
        if image:
            add(f"Segment 1 opens on an image already made from this prompt: \"{image}\"")
    state: dict[str, dict] = {}
    for r in before:
        state = _merge_state(state, _sjson(r))
    add("")
    add("THE SHOTS TO WRITE")
    for i, (sid, sj) in enumerate(zip(sids, unit.sjsons()), 1):
        add(f"Segment {i} = Shot {sid}" + (" (its prompt is written: keep it exactly, write the "
                                          "others around it)" if sid in hand else
                                          " (later: written in the next reply, not now)" if sid in later
                                          else ""))
        if sid in later:
            add("- " + _one_line(sid, sj))
        else:
            for line in shot_lines(sj, chars, crowd, state):
                add(line)
        if sid in hand:
            add(f"  Its prompt: \"{hand[sid]}\"")
        state = _merge_state(state, sj)
    if after and next_shot:
        add("")
        add("WHAT COMES NEXT (end so that it can follow)")
        add("- " + _one_line(after[0]["shot_id"], _sjson(after[0])))
    blocks = prompt_check.cast_sheet(plan, attach=False, reply=False).rstrip().split("\n\n")
    own = [b for b in blocks if b not in shared]
    if own:
        add("")
        add("\n\n".join(own))
    if len(own) < len(blocks):
        add("(The RULES OF THE RENDER MODEL at the top apply to this part as well.)")
    image = wants_image(req, hand)
    if image:
        add("")
        add("OPENING IMAGE")
        add("- Segment 1 opens on a still image made first from the same reference pictures. "
            + (_IMAGE_RULE if image_rule else "Write its prompt as well (OPENING IMAGES at the top)."))
    if reply:
        add("")
        add("REPLY FORMAT")
        add("Reply with the prompts only, each block starting on its own line with its label"
            + (", or hand them in with submit_prompts" if tools else "") + ":")
        if image:
            add("Opening image: ...")
        for s in sids:
            if s not in hand and s not in later:
                add(f"Shot {s}: ...")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------- replies
# a label at the start of a line, with a colon or dash after it ("Shot
# s01_002:", "**Segment 2** -"), or alone on its line ("## Shot s01_002")
_LABEL = re.compile(
    r"^[ \t>#*_-]*(?:\*\*|__)?[ \t]*(?P<label>opening[ \t_-]*image(?:[ \t]*\(for[ \t]+shot[ \t]+"
    r"(?P<isid>[A-Za-z0-9][\w.-]*)\))?|shot[ \t]+(?P<sid>[A-Za-z0-9][\w.-]*)|segment[ \t]+(?P<n>\d+))"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*(?:\*\*|__)?[ \t]*(?:[:：.–—-]+[ \t]*(?:\*\*|__)?[ \t]*|(?=\r?\n))",
    re.I | re.M)


def _labels(t: str, sids: list[str]) -> list[re.Match]:
    """The labels of `t` that name one of `sids` (a segment number in range,
    or an opening image): "Shot reverse:" in a prompt is prose."""
    def known(m: re.Match) -> bool:
        if m.group("sid"):
            return _match_sid(m.group("sid"), sids) is not None
        if m.group("n"):
            return 1 <= int(m.group("n")) <= len(sids)
        return True
    return [m for m in _LABEL.finditer(t) if known(m)]


def labelled_ids(text: str) -> list[str]:
    """Every shot id a reply labels a block with ("Shot <id>:")."""
    return [m.group("sid").rstrip(".:,") for m in _LABEL.finditer(str(text or "")) if m.group("sid")]


def parse_reply(text: str, sids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """({shot id: prompt}, {shot id: opening image prompt}) from a reply:
    labelled blocks ("Shot s01_002:", "Segment 2:", "Opening image:" for the
    first shot, "Opening image (for Shot s01_002):"), or JSON ({"shots" |
    "prompts": {id: text} | [...], "opening_image": ...}). A reply for one
    shot may be the bare prompt."""
    t = str(text or "").strip()
    fenced = re.findall(r"```[a-zA-Z]*\s*(.*?)```", t, re.S)
    if fenced and not _LABEL.search(t.split("```")[0]):
        t = max(fenced, key=len).strip()
    got = _parse_json(t, sids)
    if got is not None:
        return got
    marks = _labels(t, sids)
    out: dict[str, str] = {}
    images: dict[str, str] = {}
    for k, m in enumerate(marks):
        body = _clean(t[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(t)])
        if m.group("label").lower().startswith("opening"):
            # "Opening image:" belongs to the next shot block, or to the first shot
            target = _match_sid(m.group("isid") or "", sids)
            if target is None:
                nxt = next((x for x in marks[k + 1:] if x.group("sid") or x.group("n")), None)
                target = (_match_sid(nxt.group("sid") or "", sids) if nxt and nxt.group("sid")
                          else sids[int(nxt.group("n")) - 1] if nxt and nxt.group("n")
                          and 1 <= int(nxt.group("n")) <= len(sids) else sids[0] if sids else None)
            if target and body:
                images[target] = body
        elif m.group("sid"):
            sid = _match_sid(m.group("sid"), sids)
            if sid:
                out[sid] = body
        elif m.group("n") and 1 <= int(m.group("n")) <= len(sids):
            out.setdefault(sids[int(m.group("n")) - 1], body)
    if not marks and len(sids) == 1 and t and not _LABEL.search(t):
        out[sids[0]] = _clean(t)
    return {k: v for k, v in out.items() if v}, images


def _match_sid(raw: str, sids: list[str]) -> str | None:
    raw = str(raw or "").strip().rstrip(".:,").lower()
    return next((s for s in sids if s.lower() == raw), None) if raw else None


def _clean(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    s = re.sub(r"\n[ \t]*(?:-{3,}|\*{3,}|_{3,}|={3,}[^\n]*)[ \t]*$", "", s).strip()
    if len(s) > 1 and s[0] in "\"“" and s[-1] in "\"”":
        s = s[1:-1].strip()
    return s


def _parse_json(t: str, sids: list[str]) -> tuple[dict[str, str], dict[str, str]] | None:
    if not t.startswith(("{", "[")):
        return None
    try:
        data = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    images: dict[str, str] = {}
    if isinstance(data, dict):
        img = str(data.get("opening_image") or "").strip()
        if img and sids:
            images[sids[0]] = img
        data = data.get("shots") or data.get("prompts") or {k: v for k, v in data.items()
                                                             if _match_sid(k, sids)}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            sid = _match_sid(str(k), sids)
            if sid and isinstance(v, str):
                out[sid] = v.strip()
    elif isinstance(data, list):
        for i, v in enumerate(data):
            if isinstance(v, dict):
                sid = _match_sid(str(v.get("shot_id") or v.get("shot") or ""), sids)
                txt = v.get("prompt") or v.get("text")
            else:
                sid, txt = (sids[i] if i < len(sids) else None), v
            if sid and isinstance(txt, str):
                out[sid] = txt.strip()
    return {k: v for k, v in out.items() if v}, images


# ---------------------------------------------------------- checking a reply
def _flat(s: Any) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(s or "").lower()).split())


def cast_names(bible) -> dict[str, str]:
    """character id -> name, for story_checks."""
    try:
        chars = bible.characters() or {}
    except Exception:  # noqa: BLE001 - the names are a check's bonus, never a failure
        return {}
    return {str(k): str((v or {}).get("name") or k) for k, v in chars.items()}


def story_checks(sjsons: list[dict], texts: list[str], names: dict[str, str],
                 strict: set[int] | frozenset[int] = frozenset()) -> list:
    """What a prompt keeps from its shot beyond the render model's rules (the
    prompt check): every line of dialogue the shot lists is spoken in it, word
    for word, since the clip's sound comes from the prompt; and no one who is
    not in the shot is named in it. A missing line is an error for the
    segments in `strict` (written by an AI, which gets it back to fix) and a
    warning for the others (a hand prompt is its author's); a name is a
    warning."""
    from prompt_check import _GENERIC, Issue
    out = []
    for i, (sj, t) in enumerate(zip(sjsons, texts), 1):
        if not str(t or "").strip():
            continue
        flat = _flat(t)
        for d in sj.get("dialogue") or []:
            line = str(d.get("line") or "").strip() if isinstance(d, dict) else ""
            if _flat(line) and _flat(line) not in flat:
                out.append(Issue("error" if i in strict else "warning", "dialogue",
                                 f"The line \"{line}\" is not spoken in the prompt: put it in word for "
                                 f"word, in quotes, with who says it.", i))
        on = {str(c.get("id")) for c in sj.get("characters") or [] if isinstance(c, dict)}
        for cid, name in names.items():
            if cid in on:
                continue
            for word in dict.fromkeys((_flat(name), _flat(cid))):
                if len(word) > 2 and word not in _GENERIC and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", flat):
                    out.append(Issue("warning", "not_in_shot",
                                     f"{name} is named in the prompt but is not in this shot: describe "
                                     f"only the people the shot lists.", i))
                    break
    return out


class _Take:
    """One unit's prompts being written: checks what is handed in (by the AI
    or a pasted reply) against the request, the way Generate would, and
    against the story of its shots (story_checks)."""

    def __init__(self, req: MovieRequest, plan, hand: dict[str, str], want_image: bool,
                 names: dict[str, str] | None = None, later: set[str] | frozenset[str] = frozenset()) -> None:
        self.req, self.plan, self.hand, self.want_image = req, plan, hand, want_image
        self.names = names or {}
        self.sids = req.unit.sids
        # `later`: shots of the take a later reply writes (empty for now)
        self.later = frozenset(s for s in later if s not in hand)
        self.want = [s for s in self.sids if s not in hand and s not in self.later]
        self.texts: list[str] = []
        self.image = ""
        self.issues: list = []

    def take(self, got: dict[str, str], image: str = "") -> list[str]:
        """The problems that keep these prompts from rendering ([] = accepted).
        A hand prompt's own errors are its author's to fix: they never stop
        the prompts around it, and the shots written later are not checked
        yet."""
        missing = [s for s in self.want if not str(got.get(s) or "").strip()]
        problems = [f"Shot {s}: the prompt is missing." for s in missing]
        if self.want_image and not str(image or "").strip():
            problems.append(f"Shot {self.sids[0]}: the opening image's prompt is missing "
                            f"(\"Opening image: ...\").")
        texts = [self.hand.get(s) or ("" if s in self.later else str(got.get(s) or "")) for s in self.sids]
        texts, issues = autofix(self.plan, texts)
        issues = issues + story_checks(self.req.unit.sjsons(), texts, self.names, self.strict())
        for i in issues:
            seg = self.sids[i.segment - 1] if i.segment else ""
            if i.level != "error" or seg in self.hand or seg in self.later:
                continue
            if i.code == "empty" and seg in missing:
                continue
            problems.append((f"Shot {seg}: " if seg else "") + i.message)
        if not problems:
            self.texts, self.image, self.issues = texts, str(image or "").strip(), issues
        return problems

    def strict(self) -> set[int]:
        """The segments written now (not kept prompts, not later ones)."""
        return {k for k, s in enumerate(self.sids, 1) if s in self.want}


def _feedback(problems: list[str]) -> str:
    return ("These keep the prompts from being used:\n" + "\n".join(f"- {p}" for p in problems)
            + "\nFix only these and reply with every prompt again, in the same format.")


def _store(db, job: _Take, st: list[dict], source: str) -> list[str]:
    """Save what `job` accepted; returns the shot ids saved."""
    pid, sids = job.req.unit.pid, job.sids
    saved = []
    for i, s in enumerate(st):
        sid = s["sid"]
        mine = _issue_dicts(job.issues, i + 1)
        if sid in job.hand:
            db.save_prompt(sid, pid, issues=mine)          # kept as its author wrote it
            continue
        db.save_prompt(sid, pid, text=job.texts[i], source=source, sig=s["sig"], unit=sids,
                       model=job.req.model, issues=mine,
                       **({"image": job.image} if i == 0 and job.want_image else {}))
        saved.append(sid)
    release_waiting(db, pid, sids)
    return saved


def release_waiting(db, pid: str, sids: list[str]) -> None:
    """Shots that waited for their prompts go back to the queue."""
    for sid in sids:
        row = db.get(sid, pid)
        if row is not None and row.get("status") == WAITING:
            db.set_status(sid, "queued", error="", project=pid)


# --------------------------------------------------------------------- tools
TOOLS = [
    {"type": "function", "function": {
        "name": "get_shot",
        "description": "One shot of the movie: its camera, action, characters, dialogue, sound, "
                       "how it joins the shot before, and its video prompt if it has one.",
        "parameters": {"type": "object", "properties": {"shot_id": {"type": "string"}},
                       "required": ["shot_id"]}}},
    {"type": "function", "function": {
        "name": "get_scene",
        "description": "A scene's beat (place, time, mood, goal, turn, how it ends) and its shots "
                       "in story order, one line each.",
        "parameters": {"type": "object", "properties": {"scene": {"type": "string"}},
                       "required": ["scene"]}}},
    {"type": "function", "function": {
        "name": "get_character",
        "description": "A character of the cast: name, look, outfit, powers, weapons, emotions.",
        "parameters": {"type": "object", "properties": {"character_id": {"type": "string"}},
                       "required": ["character_id"]}}},
    {"type": "function", "function": {
        "name": "check_prompts",
        "description": "Check prompts for the shots you are writing with the render model's "
                       "prompt check, without handing them in. Returns the errors (which stop the "
                       "render) and the warnings.",
        "parameters": {"type": "object", "properties": {"prompts": {
            "type": "object", "description": "shot id -> prompt",
            "additionalProperties": {"type": "string"}}}, "required": ["prompts"]}}},
    {"type": "function", "function": {
        "name": "submit_prompts",
        "description": "Hand in the finished prompts of the shots you are writing, and the "
                       "opening image's prompt when the brief asks for one. They are checked "
                       "first: accepted, or the errors to fix and submit again.",
        "parameters": {"type": "object", "properties": {
            "prompts": {"type": "object", "description": "shot id -> prompt",
                        "additionalProperties": {"type": "string"}},
            "opening_image": {"type": "string"}}, "required": ["prompts"]}}},
]


class _Tools:
    """The writer's tools: read the project, check, hand in."""

    def __init__(self, db, bible, proj: dict, job: _Take) -> None:
        self.db, self.bible, self.proj, self.job = db, bible, proj, job
        self.submitted = False

    def run(self, name: str, args: dict) -> dict:
        fn = {"get_shot": self._get_shot, "get_scene": self._get_scene,
              "get_character": self._get_character, "check_prompts": self._check_prompts,
              "submit_prompts": self._submit_prompts}.get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return fn(**{k: v for k, v in (args or {}).items()
                         if k in ("shot_id", "scene", "character_id", "prompts", "opening_image")})
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}
        except Exception as e:  # noqa: BLE001 - a tool error goes back to the AI
            return {"error": str(e)}

    def _get_shot(self, shot_id: str) -> dict:
        pid = self.job.req.unit.pid
        row = self.db.get(str(shot_id), pid)
        if row is None:
            return {"error": f"no shot {shot_id}"}
        p = self.db.prompt(str(shot_id), pid) or {}
        return {"shot": _one_line(row["shot_id"], _sjson(row)), "scene": row.get("scene"),
                "details": shot_lines(_sjson(row), self.bible.characters() or {}),
                "prompt": str(p.get("text") or "")[:2000] or None}

    def _get_scene(self, scene: str) -> dict:
        from shots_db import story_order
        rows = story_order(self.db.all(scene=str(scene), project=self.job.req.unit.pid))
        if not rows:
            return {"error": f"no scene {scene}"}
        where = str(_sjson(rows[0]).get("location") or "")
        return {"beat": _scene_line(self.proj, self.bible.locations() or {}, str(scene), where),
                "shots": [_one_line(r["shot_id"], _sjson(r)) for r in rows[:60]]}

    def _get_character(self, character_id: str) -> dict:
        c = (self.bible.characters() or {}).get(str(character_id))
        if not c:
            return {"error": f"no character {character_id}"}
        out = {k: c.get(k) for k in ("name", "tier", "body", "outfit", "emotion_palette", "arc_summary")
               if c.get(k)}
        from cast_look import body_text
        if body_text(c):
            out["body"] = body_text(c)
        out["powers"] = {k: (v or {}).get("descriptor") for k, v in (c.get("powers") or {}).items()}
        out["weapons"] = {k: (v or {}).get("descriptor") for k, v in (c.get("weapons") or {}).items()}
        return out

    def _texts(self, prompts: dict) -> dict[str, str]:
        return {(_match_sid(k, self.job.sids) or str(k)): str(v)
                for k, v in (prompts or {}).items() if isinstance(v, str)}

    def _check_prompts(self, prompts: dict) -> dict:
        import prompt_check
        job, got = self.job, self._texts(prompts)
        texts = [job.hand.get(s) or ("" if s in job.later else got.get(s, "")) for s in job.sids]
        issues = [i for i in prompt_check.check(job.plan, texts) + story_checks(
            job.req.unit.sjsons(), texts, job.names, job.strict())
            if not (i.segment and job.sids[i.segment - 1] in job.later)]

        def say(i) -> str:
            return (f"Shot {job.sids[i.segment - 1]}: " if i.segment else "") + i.message
        return {"errors": [say(i) for i in issues if i.level == "error"],
                "warnings": [say(i) for i in issues if i.level != "error"]}

    def _submit_prompts(self, prompts: dict, opening_image: str = "") -> dict:
        problems = self.job.take(self._texts(prompts), str(opening_image or ""))
        if problems:
            return {"accepted": False, "errors": problems}
        self.submitted = True
        return {"accepted": True}


# -------------------------------------------------------------------- writing
class WriteError(RuntimeError):
    pass


class Unreachable(WriteError):
    """Nothing answers at the AI endpoint (no server there, or the name does
    not resolve): every unit would fail the same way."""


# endpoints that refused a connection, and when: for DOWN_S after, a unit
# fails at once instead of waiting out the retries again
_DOWN: dict[str, float] = {}
DOWN_S = 60.0


def ai_ready(cfg: dict | None = None) -> bool:
    from llm import LLMClient
    return LLMClient(cfg or load_config()).config_ok()


def write_unit(db, bible, proj: dict, unit: Unit, *, req: MovieRequest | None = None,
               client=None, cfg: dict | None = None, force: bool = False,
               stop: Callable[[], bool] | None = None,
               waiting: Callable[[], None] | None = None) -> dict:
    """Write the prompts of `unit` with the configured AI and store them, for
    the request it renders with once its opening frame is made (`req`
    defaults to that planning request). Hand prompts are always kept, and
    unless `force` so are prompts that are ready: only the others are
    written, around them. An AI on this PC waits until no render holds the
    GPU (`waiting` is called while it does). {"ok", "written": [...],
    "kept": [...], "error"?}. Raises WriteError when the AI cannot be
    reached (Unreachable when nothing answers there)."""
    from llm import LLMClient, is_local
    cfg = cfg or load_config()
    client = client or LLMClient(cfg)
    if not client.config_ok():
        return {"ok": False, "error": "no AI is set up (AI settings: provider, model, key)"}
    base = str((cfg.get("architect") or {}).get("base_url") or "")
    if time.time() - _DOWN.get(base, -DOWN_S) < DOWN_S:
        raise Unreachable(f"nothing answered at {base} a moment ago")
    with _lock(unit.pid):
        req = req or request_for(db, bible, unit, cfg=cfg)
        plan = req.plan()
        bad = [i for i in plan.issues if i.level == "error"]
        if bad:
            return {"ok": False, "error": bad[0].message}
        st = states(db, req, plan)
        _, now = autofix(plan, [s["text"] for s in st])
        broken = {st[i.segment - 1]["sid"] for i in now if i.level == "error" and i.segment}
        keep = {s["sid"]: s["text"] for s in st if s["text"].strip() and (
            s["source"] in HAND or (not force and s["state"] == "ready" and s["sid"] not in broken))}
        if len(keep) == len(st):
            return {"ok": True, "written": [], "kept": unit.sids}
        if is_local(base) and not wait_gpu(stop, waiting, base=base):
            return {"ok": False, "error": "stopped"}
        names, image = cast_names(bible), wants_image(req, keep)
        tools_on = client.tools_ok()
        # a long take is written in parts: each reply writes a few segments,
        # with the prompts of the parts before it kept as they are
        chunks = parts([s for s in unit.sids if s not in keep], segments_per_reply(cfg))
        written: dict[str, str] = {}
        opening = ""
        down = False
        try:
            for k, chunk in enumerate(chunks):
                later = {s for c in chunks[k + 1:] for s in c}
                job = _Take(req, plan, {**keep, **written}, image and k == 0, names, later)
                problems = _ask(client, db, bible, proj, job, tools_on, base, stop)
                if problems is None:
                    return {"ok": False, "error": "stopped"}
                if problems:
                    return {"ok": False, "error": problems[0], "problems": problems}
                written.update((s, t) for s, t in zip(unit.sids, job.texts) if s in chunk)
                opening = opening or job.image
            # the whole take, checked as Generate will: a segment the check
            # names is written again, the others kept
            job = _Take(req, plan, keep, image, names)
            problems = job.take(written, opening)
            named = [s for s in written if any(p.startswith(f"Shot {s}:") for p in problems)]
            if problems and len(chunks) > 1 and named:
                fix = _Take(req, plan, {**keep, **{s: t for s, t in written.items() if s not in named}},
                            False, names)
                problems = _ask(client, db, bible, proj, fix, tools_on, base, stop)
                if problems is None:
                    return {"ok": False, "error": "stopped"}
                if not problems:
                    written.update((s, t) for s, t in zip(unit.sids, fix.texts) if s in named)
                    problems = job.take(written, opening)
        except Unreachable:
            down = True
            _DOWN[base] = time.time()
            raise
        finally:
            if not down:
                _release_local(client)
        if problems:
            return {"ok": False, "error": problems[0], "problems": problems}
        return {"ok": True, "written": _store(db, job, st, "ai"), "kept": list(keep)}


def _ask(client, db, bible, proj: dict, job: _Take, tools_on: bool, base: str,
         stop: Callable[[], bool] | None) -> list[str] | None:
    """One conversation writing `job`'s prompts, from its brief, with up to
    _REPAIRS rounds of fixes: the problems left ([] = accepted), None when
    `stop` says so."""
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM + (TOOLS_NOTE if tools_on else "")},
        {"role": "user", "content": unit_brief(db, bible, proj, job.req, job.plan, job.hand,
                                               tools=tools_on, later=job.later)}]
    problems: list[str] = []
    for _ in range(_REPAIRS + 1):
        if stop is not None and stop():
            return None
        tools = _Tools(db, bible, proj, job) if tools_on else None
        text, submitted = _converse(client, messages, tools)
        _DOWN.pop(base, None)
        if submitted:
            return []
        got, images = parse_reply(text, job.sids)
        problems = job.take(got, images.get(job.sids[0], ""))
        if not problems:
            return []
        messages.append({"role": "user", "content": _feedback(problems)})
    return problems


def wait_gpu(stop: Callable[[], bool] | None = None,
             waiting: Callable[[], None] | None = None, base: str = "") -> bool:
    """An AI on this PC shares the GPU with the renders: return once no render
    holds the GPU or waits for it. False when `stop` says so first. `base`:
    the AI's endpoint, asked while a render holds the GPU; one that does not
    answer (not started) raises Unreachable instead of being waited for
    behind every render."""
    import gpu_broker
    while not (stop is not None and stop()):
        if not gpu_broker.busy():
            return True
        if base:
            from llm import reachable
            if not reachable(base):
                _DOWN[base] = time.time()
                raise Unreachable(f"nothing answered at {base}")
        if waiting is not None:
            waiting()
        time.sleep(5)
    return False


def _converse(client, messages: list[dict], tools: _Tools | None) -> tuple[str, bool]:
    """Run the conversation until the AI replies in text or its prompts are
    accepted through submit_prompts. (reply text, submitted)."""
    for _ in range(_TURNS):
        res = client.chat(messages, tools=TOOLS if tools is not None else None, max_tokens=_MAX_TOKENS)
        if not res.get("ok"):
            raise (Unreachable if res.get("unreachable") else WriteError)(
                res.get("error") or "the AI request failed")
        msg = res["message"]
        calls = (msg.get("tool_calls") or []) if res.get("tools") else []
        content = str(msg.get("content") or "")
        if not calls:
            messages.append({"role": "assistant", "content": content})
            if not content.strip():
                raise WriteError("the AI replied with nothing"
                                 + (" (the reply was cut off: raise max_tokens)"
                                    if res.get("finish") == "length" else ""))
            return content, False
        messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        for n, call in enumerate(calls):
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = None
            out = (tools.run(str(fn.get("name") or ""), args) if tools is not None and isinstance(args, dict)
                   else {"error": "the arguments are not valid JSON"})
            messages.append({"role": "tool", "tool_call_id": call.get("id") or f"call_{n}",
                             "content": json.dumps(out, ensure_ascii=False)[:12000]})
        if tools is not None and tools.submitted:
            return "", True
    raise WriteError(f"the AI did not hand in its prompts within {_TURNS} rounds")


def _release_local(client) -> None:
    """A local AI shares the GPU with the renders: ask it to unload its model."""
    try:
        if hasattr(client, "release_memory"):
            client.release_memory()
    except Exception as e:  # noqa: BLE001
        log.warning(f"could not unload the local AI model: {e}")


# --------------------------------------------------------------- hand prompts
def save_hand(db, bible, pid: str, sid: str, text: str, image: str | None = None,
              source: str = "hand", cfg: dict | None = None) -> dict:
    """Store a prompt typed (or pasted) for one shot, checked against the
    request it renders with: an error refuses it, the check's own fixes are
    applied and its warnings kept. `image` sets the opening image's prompt
    (a unit's first shot); None keeps it. An empty text clears the prompt."""
    unit = unit_for(db, pid, sid, cfg)
    if unit is None:
        return {"ok": False, "error": "this shot renders on the legacy Wan model, which writes "
                                      "its own prompts"}
    text = str(text or "").strip()
    if not text:
        db.delete_prompt(sid, pid)
        return {"ok": True, "cleared": True}
    old = db.prompt(sid, pid) or {}
    keep_image = str(old.get("image") or "") if image is None else str(image).strip()
    head = {"source": source, "image": keep_image} if sid == unit.sids[0] else None
    req = request_for(db, bible, unit, head_prompt=head, cfg=cfg)
    plan = req.plan()
    bad = [i for i in plan.issues if i.level == "error"]
    if bad:
        return {"ok": False, "error": bad[0].message}
    st = {s["sid"]: s for s in states(db, req, plan)}
    i = unit.sids.index(sid)
    texts = [text if s == sid else st[s]["text"] for s in unit.sids]
    fixed, issues = autofix(plan, texts)
    issues = issues + story_checks(unit.sjsons(), fixed, cast_names(bible))
    errors = [x for x in issues if x.level == "error" and x.segment == i + 1]
    if errors:
        return {"ok": False, "error": errors[0].message,
                "issues": [vars(x) for x in issues if x.segment == i + 1]}
    mine = _issue_dicts(issues, i + 1)
    db.save_prompt(sid, pid, text=fixed[i], source=source, sig=st[sid]["sig"], unit=unit.sids,
                   model=req.model, issues=mine, **({"image": keep_image} if head is not None else {}))
    release_waiting(db, pid, unit.sids)        # a take waits as one piece
    return {"ok": True, "text": fixed[i], "fixed": fixed[i] != text, "issues": mine}


def import_reply(db, bible, proj: dict, text: str, cfg: dict | None = None) -> dict:
    """Store a chat AI's reply to a brief (chat_brief): every "Shot <id>:"
    block of a shot that renders on LTX-2.5 / H3, checked with the other
    prompts of its unit. A unit whose prompts have an error is refused whole;
    the others are saved. A shot of a take that is not in the reply and has
    no prompt yet is written in a later batch (a long take is written in
    parts): `waiting` lists the shots of the takes saved that still have no
    ready prompt."""
    pid = proj["id"]
    units = plan_units(db, pid, cfg)
    all_sids = [s for u in units for s in u.sids]
    got, images = parse_reply(text, all_sids)
    if not got:
        return {"ok": False, "error": "no \"Shot <id>:\" blocks for this movie's shots in the reply",
                "unknown": [s for s in labelled_ids(text) if _match_sid(s, all_sids) is None]}
    saved: list[str] = []
    refused: dict[str, str] = {}
    warnings: dict[str, list[str]] = {}
    waiting: list[str] = []
    names = cast_names(bible)
    done: set[str] = set()
    for u in units:
        mine = [s for s in u.sids if s in got and s not in done]
        if not mine:
            continue
        unit = unit_for(db, pid, mine[0], cfg, units) or u
        mine = [s for s in unit.sids if s in got]
        done |= set(unit.sids)
        image = images.get(unit.sids[0], "")
        head = ({"source": "chat", "image": image} if unit.sids[0] in got else None)
        req = request_for(db, bible, unit, head_prompt=head, cfg=cfg)
        plan = req.plan()
        st = states(db, req, plan)
        keep = {s["sid"]: s["text"] for s in st if s["sid"] not in got and s["text"].strip()}
        later = {s["sid"] for s in st if s["sid"] not in got and not s["text"].strip()}
        job = _Take(req, plan, keep, bool(req.frame_planned) and unit.sids[0] in got, names, later)
        problems = job.take(got, image)
        if problems:
            for s in mine:
                refused[s] = next((p for p in problems if p.startswith(f"Shot {s}:")), problems[0])
            continue
        for k, s in enumerate(st):
            if s["sid"] in got:
                mine_issues = _issue_dicts(job.issues, k + 1)
                db.save_prompt(s["sid"], pid, text=job.texts[k], source="chat", sig=s["sig"],
                               unit=unit.sids, model=req.model, issues=mine_issues,
                               **({"image": image} if k == 0 else {}))
                saved.append(s["sid"])
                notes = [x["message"] for x in mine_issues if x["level"] != "error"]
                if notes:
                    warnings[s["sid"]] = notes
        rest = [s["sid"] for s in st if s["sid"] not in got and s["state"] != "ready"]
        waiting += rest
        if not rest:
            release_waiting(db, pid, unit.sids)        # a take waits as one piece
    unknown = [s for s in labelled_ids(text) if _match_sid(s, all_sids) is None]
    out = {"ok": bool(saved), "saved": saved, "refused": refused, "unknown": unknown,
           "warnings": warnings, "waiting": waiting}
    if not saved:
        out["error"] = next(iter(refused.values()), "nothing to save")
    return out


def chat_brief(db, bible, proj: dict, sids: list[str] | None = None, limit: int | None = None,
               cfg: dict | None = None) -> dict:
    """One brief for a chat AI, a batch at a time: the next units still to
    render without ready prompts, in story order, takes of one scene with up
    to `limit` prompts to write (default segments_per_reply, as one AI reply
    writes them); or the units of `sids`, whose shots are written again. A
    take with more to write than that is written in parts: this batch writes
    the first, the rest of the take is shown, marked later. Ready prompts
    are kept, the others written around them. It is built when asked, so a
    batch carries the prompts saved for the shots before it. The film and the
    rules the units share are said once. Its reply is pasted back with
    import_reply."""
    import prompt_check
    pid = proj["id"]
    limit = segments_per_reply(cfg) if limit is None else max(1, int(limit))
    units = plan_units(db, pid, cfg)
    # (request, plan, prompts kept, shots written in a later batch)
    picked: list[tuple[MovieRequest, Any, dict[str, str], set[str]]] = []
    n = 0
    seen: set[str] = set()
    scene = ""
    for u in units:
        pick = [s for s in u.sids if sids is None or s in sids]
        if not pick:
            continue
        unit = unit_for(db, pid, pick[0], cfg, units) if sids else remaining(u)
        if unit is None or seen & set(unit.sids):
            continue
        req = request_for(db, bible, unit, cfg=cfg)
        plan = req.plan()
        hand = {s["sid"]: s["text"] for s in states(db, req, plan)
                if s["state"] == "ready" and (sids is None or s["sid"] not in sids)}  # a shot asked for by name is written again
        todo = [s for s in unit.sids if s not in hand]
        if not todo:
            continue
        sc = str(unit.head.get("scene") or "")
        if picked and ((sids is None and sc != scene) or n + len(todo) > limit):
            break
        now = parts(todo, limit)[0]
        picked.append((req, plan, hand, set(todo) - set(now)))
        seen |= set(unit.sids)
        n += len(now)
        scene = sc
        if len(now) < len(todo):
            break
    progress = brief_progress(db, bible, pid, cfg, limit)
    if not picked:
        return {"ok": True, "text": "", "shots": [], "units": 0, "scene": "", "progress": progress}
    # a block of the cast sheet two parts or more have is said once, at the top
    sheets = [prompt_check.cast_sheet(plan, attach=False, reply=False).rstrip().split("\n\n")
              for _req, plan, _hand, _later in picked]
    seen_blocks: dict[str, int] = {}
    for sheet in sheets:
        for b in dict.fromkeys(sheet):
            seen_blocks[b] = seen_blocks.get(b, 0) + 1
    shared = frozenset(b for b, c in seen_blocks.items() if c > 1)
    images = sum(1 for req, _plan, hand, _later in picked if wants_image(req, hand))
    head = [SYSTEM, ""]
    genre = ", ".join(x for x in (proj.get("genre"), proj.get("tone")) if x)
    head.append(f"BRIEF: the video prompts of {n} shot{'s' if n != 1 else ''} of "
                f"\"{proj.get('title') or 'the movie'}\"" + (f" ({genre})" if genre else "")
                + (f", in scene {scene}" if scene else "") + ".")
    tr = (proj.get("story") or {}).get("treatment") or {}
    if isinstance(tr, dict) and tr.get("logline"):
        head.append(f"Logline: {tr['logline']}")
    film = _outline(proj, scene)
    if film:
        head += [""] + film
    if shared:
        head += ["", "RULES OF THE RENDER MODEL (for the parts below; a part that gives its own rule "
                     "for something follows its own)",
                 "\n\n".join(b for b in seen_blocks if b in shared)]
    if images > 1:
        head += ["", "OPENING IMAGES",
                 "- A part marked OPENING IMAGE opens on a still image made first from the same reference "
                 "pictures. " + _IMAGE_RULE]
    blocks = []
    want: list[str] = []
    tail = ["REPLY FORMAT"]
    if len(picked) > 1:
        tail.append("Write the parts in story order: each shot carries on from the prompt you write "
                    "for the shot before it in this reply.")
    tail.append("Reply with the prompts only, each block starting on its own line with its label:")
    for k, (req, plan, hand, later) in enumerate(picked, 1):
        body = unit_brief(db, bible, proj, req, plan, hand, reply=False, head=False, shared=shared,
                          story_so_far=k == 1, next_shot=k == len(picked) and not later,
                          image_rule=images < 2, later=later)
        blocks.append((f"===== PART {k} of {len(picked)} =====\n" if len(picked) > 1 else "") + body)
        if wants_image(req, hand):
            tail.append(f"Opening image (for Shot {req.unit.sids[0]}): ...")
        for s in req.unit.sids:
            if s not in hand and s not in later:
                tail.append(f"Shot {s}: ...")
                want.append(s)
    text = "\n".join(head) + "\n\n" + "\n".join(blocks) + "\n" + "\n".join(tail) + "\n"
    return {"ok": True, "text": text, "shots": want, "units": len(picked), "scene": scene,
            "progress": progress}


def brief_progress(db, bible, pid: str, cfg: dict | None = None, limit: int | None = None) -> dict:
    """How far the prompts of the shots still to render are: {ready, total,
    batches}, `batches` being the chat briefs still to paste (takes of one
    scene with up to `limit` prompts to write, a longer take in parts, as
    chat_brief makes them)."""
    limit = segments_per_reply(cfg) if limit is None else max(1, int(limit))
    view = project_view(db, bible, pid, cfg)["shots"]
    todo = [v for v in view.values() if v["state"] != "rendered"]
    batches, n, scene = 0, 0, None
    for u in plan_units(db, pid, cfg):
        rest = remaining(u)
        if rest is None:
            continue
        k = sum(1 for s in rest.sids if view.get(s, {}).get("state") not in ("ready", "rendered"))
        if not k:
            continue
        sc = str(rest.head.get("scene") or "")
        if not batches or sc != scene or n + k > limit:
            batches, n = batches + 1, 0
        sizes = [len(p) for p in parts(list(range(k)), limit)]
        batches += len(sizes) - 1          # a longer take: a batch per part
        n += sizes[-1] if len(sizes) > 1 else k
        scene = sc
    return {"ready": sum(1 for v in todo if v["state"] == "ready"), "total": len(todo), "batches": batches}


# ------------------------------------------------------------------- the job
class PromptWriter:
    """Writes a project's missing and stale prompts with the configured AI, in
    story order, one unit at a time: a background job (the Shots page), or
    run() inline (the director). Either shows its progress in status(), and
    one project is written by one job at a time."""

    def __init__(self, cfg: dict | None = None, db=None, store=None) -> None:
        self.cfg = cfg or load_config()
        self.db, self.store = db, store
        self._jobs: dict[str, dict] = {}
        self._stop: set[str] = set()
        self._guard = threading.Lock()

    def start(self, pid: str, sids: list[str] | None = None, force: bool = False) -> dict:
        if not ai_ready(self.cfg):
            return {"ok": False, "error": "no AI is set up: open the AI settings and pick a "
                                          "provider and a model"}
        if not self._claim(pid):
            return {"ok": False, "error": "the prompts are already being written"}
        threading.Thread(target=self._thread, args=(pid, sids, force), daemon=True,
                         name=f"prompt-writer-{pid}").start()
        return {"ok": True}

    def _claim(self, pid: str) -> bool:
        with self._guard:
            job = self._jobs.get(pid)
            if job is not None and job.get("running"):
                return False
            self._stop.discard(pid)
            self._jobs[pid] = {"running": True, "done": 0, "total": 0, "message": "starting",
                               "written": [], "failed": {}, "started": time.time()}
            return True

    def status(self, pid: str) -> dict:
        with self._guard:
            job = self._jobs.get(pid)
            return {"active": json.loads(json.dumps(job, default=str)) if job else None}

    def stop(self, pid: str) -> dict:
        with self._guard:
            self._stop.add(pid)
        return {"ok": True}

    def running(self, pid: str) -> bool:
        with self._guard:
            return bool((self._jobs.get(pid) or {}).get("running"))

    def _set(self, pid: str, **kw) -> None:
        with self._guard:
            self._jobs.setdefault(pid, {}).update(kw)

    def _thread(self, pid: str, sids: list[str] | None, force: bool) -> None:
        try:
            self._write(pid, sids, force)
        except Exception:  # noqa: BLE001 - _write recorded it
            log.exception(f"prompt writer failed for {pid}")

    def run(self, pid: str, sids: list[str] | None = None, force: bool = False) -> dict:
        """Write the units with a shot still to render (or the units of
        `sids`) whose prompts are not ready, after the job already writing
        this project's prompts, if any. Synchronous; a summary."""
        while not self._claim(pid):
            if pid in self._stop:
                return {"message": "stopped", "written": [], "failed": {}}
            time.sleep(2)
        return self._write(pid, sids, force)

    def _write(self, pid: str, sids: list[str] | None, force: bool) -> dict:
        try:
            res = self._units(pid, sids, force)
        except Exception as e:
            self._set(pid, running=False, message=f"failed: {e}")
            raise
        finally:
            with self._guard:
                self._stop.discard(pid)
        self._set(pid, running=False, message=res["message"], written=res["written"],
                  failed=res["failed"])
        return res

    def _units(self, pid: str, sids: list[str] | None, force: bool) -> dict:
        from bible import Bible
        from llm import LLMClient, reachable
        proj = (self.store.get(pid) if self.store is not None else None) or {"id": pid}
        bible = Bible(pid)
        units = plan_units(self.db, pid, self.cfg)
        todo: list[Unit] = []
        seen: set[str] = set()
        for u in units:
            pick = [s for s in u.sids if sids is None or s in sids]
            if not pick:
                continue
            unit = unit_for(self.db, pid, pick[0], self.cfg, units) if sids else remaining(u)
            if unit is None or seen & set(unit.sids):
                continue
            seen |= set(unit.sids)
            todo.append(unit)
        client = LLMClient(self.cfg)
        base = str((self.cfg.get("architect") or {}).get("base_url") or "")
        written: list[str] = []
        failed: dict[str, str] = {}
        self._set(pid, total=len(todo), done=0)
        for k, unit in enumerate(todo):
            if pid in self._stop:
                return {"message": "stopped", "written": written, "failed": failed}
            self._set(pid, done=k, message=f"writing {', '.join(unit.sids)}")
            try:
                if k == 0 and not reachable(base):
                    # asked once, at once: a server that is not there would
                    # otherwise be found out after the request's retries
                    _DOWN[base] = time.time()
                    raise Unreachable(f"nothing answered at {base}")
                res = write_unit(
                    self.db, bible, proj, unit, client=client, cfg=self.cfg, force=force,
                    stop=lambda: pid in self._stop,
                    waiting=lambda: self._set(pid, message="waiting: the AI runs on this PC and "
                                                           "a render is using the GPU"))
            except Unreachable as e:
                # the next units would fail the same way: stop here
                for s in unit.sids:
                    failed[s] = str(e)
                log.warning(f"{pid}: prompts not written, the AI writer did not answer: {e}")
                self._set(pid, done=k + 1, written=list(written), failed=dict(failed))
                return {"message": f"the AI writer at {base} did not answer", "written": written,
                        "failed": failed, "unreachable": True}
            except WriteError as e:
                res = {"ok": False, "error": str(e)}
            if pid in self._stop:
                return {"message": "stopped", "written": written, "failed": failed}
            if res.get("ok"):
                written += res.get("written") or []
            else:
                for s in unit.sids:
                    failed[s] = str(res.get("error") or "failed")
                log.warning(f"{pid}: prompts of {unit.sids} not written: {res.get('error')}")
            self._set(pid, done=k + 1, written=list(written), failed=dict(failed))
        msg = f"{len(written)} prompt(s) written" + (f"; {len(failed)} shot(s) not" if failed else "")
        return {"message": msg, "written": written, "failed": failed}


# --------------------------------------------------------------------- views
def _mtime(p) -> int:
    try:
        return int(p.stat().st_mtime)
    except OSError:              # replaced right now (Make again): the page asks again
        return 0


def project_view(db, bible, pid: str, cfg: dict | None = None) -> dict:
    """Every LTX-2.5 / H3 shot's prompt, for the Shots page: its text, who
    wrote it, its opening image's prompt, whether it is ready for the request
    it renders with next ("rendered" for a shot not to render again), what it
    opens on (`start`: the request's start_source, or "take" inside a take),
    and the prompt check's warnings; an opening shot whose opening frame is
    made has `frame` (its likeness to the cast, opening_frames.frame_info).
    `counts` covers the shots still to render."""
    import opening_frames
    units = plan_units(db, pid, cfg)
    out: dict[str, dict] = {}
    stored = db.prompts(pid)
    for u in units:
        rest = remaining(u)
        for r in u.rows[: len(u.rows) - (len(rest.rows) if rest else 0)]:
            out[r["shot_id"]] = {"state": "rendered", "why": "", "unit": u.sids, "model": u.model}
        if rest is None:
            continue
        try:
            req = request_for(db, bible, rest, cfg=cfg)
            frame = opening_frames.frame_for(db, rest, cfg=cfg)
            info = (opening_frames.frame_info(db, rest) or {}) if frame is not None else {}
            for s in states(db, req):
                head = s["sid"] == rest.sids[0]
                out[s["sid"]] = {"state": s["state"], "why": s["why"], "unit": rest.sids,
                                 "model": req.model, "opening": req.opening if head else "continue",
                                 "start": (req.start_source or "") if head else "take",
                                 "wants_image": head and req.frame_planned}
                if head and frame is not None:
                    out[s["sid"]]["frame"] = {
                        "v": _mtime(frame), "likeness": info.get("likeness"),
                        "weak": bool(info.get("weak")), "scored": bool(info.get("scored")),
                        "min_likeness": info.get("min_likeness"), "people": info.get("people") or {}}
        except Exception as e:  # noqa: BLE001 - one broken unit must not hide the others
            for sid in rest.sids:
                out[sid] = {"state": "missing", "why": str(e), "unit": rest.sids, "model": rest.model}
    for sid, v in out.items():
        p = stored.get(sid) or {}
        v.update(text=str(p.get("text") or ""), source=str(p.get("source") or ""),
                 image=str(p.get("image") or ""), issues=p.get("issues") or [],
                 updated=str(p.get("updated") or ""))
    counts: dict[str, int] = {}
    for v in out.values():
        if v["state"] != "rendered":
            counts[v["state"]] = counts.get(v["state"], 0) + 1
    return {"shots": out, "counts": counts, "ai": ai_ready(cfg)}


def needing(db, bible, pid: str, cfg: dict | None = None) -> list[str]:
    """Shots whose prompt is missing or stale in a unit still to render (a
    take renders whole, so a rendered shot of it counts)."""
    return [sid for sid, v in project_view(db, bible, pid, cfg)["shots"].items()
            if v["state"] not in ("ready", "rendered")]
