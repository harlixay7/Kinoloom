"""Shared AI-package normalization (used by api import endpoints and the
LLM Story Architect runner).

Forgives common chat-AI deviations from the shot schema: power as
{character_id, power_id} objects -> strings, dialogue 'speaker' -> 'char',
plain-string state_mutations -> meaning-aware structured dicts,
visible_background filtered to registered crowd types, per-scene hero
tier capped so tiny scenes keep at most one hero moment, and entity ids that
cannot be file names ("dr.kim", "loc:alley") rewritten consistently.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from core import is_safe_component


def _safe_id(raw: str, taken: set[str]) -> str:
    s = re.sub(r"[^A-Za-z0-9 _\-]+", "_", raw).strip(" _-")
    s = re.sub(r"_{2,}", "_", s)[:60] or "id"
    if not s[0].isalnum():
        s = "x" + s
    out, n = s, 2
    while out in taken:
        out, n = f"{s}_{n}", n + 1
    return out


def normalize_ids(pkg: dict) -> dict[str, str]:
    """Rewrite character / location / scene ids that are not safe path
    components (they become bible file names — Bible._entity_path refuses
    them, which used to abort the whole import) and every reference to them
    across bible, beats, scenes and shots. Valid ids are never touched, so a
    well-formed package passes through unchanged. Returns {old: new}."""
    bible: dict = pkg["bible"] if isinstance(pkg.get("bible"), dict) else {}
    beats = (pkg.get("beats") or {}).get("beats") \
        if isinstance(pkg.get("beats"), dict) else None
    scenes = (pkg.get("scenes") or {}).get("scenes") \
        if isinstance(pkg.get("scenes"), dict) else None
    shots = (pkg.get("shots") or {}).get("shots") \
        if isinstance(pkg.get("shots"), dict) else None

    ids: set[str] = set()
    for key in ("characters", "locations"):
        for e in bible.get(key) or []:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                ids.add(e["id"])
    for e in scenes or []:
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            ids.add(e["id"])
    for b in beats or []:
        if isinstance(b, dict):
            ids.update(str(b[k]) for k in ("scene", "location") if b.get(k))
    for s in shots or []:
        if isinstance(s, dict):
            ids.update(str(s[k]) for k in ("scene", "location") if s.get(k))
            ids.update(str(c["id"]) for c in s.get("characters") or []
                       if isinstance(c, dict) and c.get("id"))

    taken = {i for i in ids if is_safe_component(i)}
    remap: dict[str, str] = {}
    for i in sorted(ids - taken):
        remap[i] = _safe_id(i, taken)
        taken.add(remap[i])
    if not remap:
        return {}

    def fix(v: Any) -> Any:
        return remap.get(v, v) if isinstance(v, str) else v

    for key in ("characters", "locations"):
        for e in bible.get(key) or []:
            if isinstance(e, dict):
                e["id"] = fix(e.get("id"))
                if key == "characters" and isinstance(e.get("location_binding"), list):
                    e["location_binding"] = [fix(x) for x in e["location_binding"]]
    for e in scenes or []:
        if isinstance(e, dict):
            for k in ("id", "location"):
                if k in e:
                    e[k] = fix(e[k])
            if isinstance(e.get("characters"), list):
                e["characters"] = [fix(x) for x in e["characters"]]
    for b in beats or []:
        if isinstance(b, dict):
            for k in ("scene", "location"):
                if k in b:
                    b[k] = fix(b[k])
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        old_scene = s.get("scene")
        for k in ("scene", "location"):
            if k in s:
                s[k] = fix(s[k])
        # shot ids are "<scene>_<n>": keep them aligned with a renamed scene
        if isinstance(old_scene, str) and old_scene in remap:
            for k in ("shot_id", "prev_shot"):
                v = s.get(k)
                if isinstance(v, str) and v.startswith(old_scene + "_"):
                    s[k] = remap[old_scene] + v[len(old_scene):]
        for c in s.get("characters") or []:
            if isinstance(c, dict):
                c["id"] = fix(c.get("id"))
        if isinstance(s.get("expected_visible"), list):
            s["expected_visible"] = [fix(x) for x in s["expected_visible"]]
        for d in s.get("dialogue") or []:
            if isinstance(d, dict):
                d["char"] = fix(d.get("char"))
    return remap


def _convert_mutations(s: dict) -> None:
    muts = s.get("state_mutations") or []
    if not muts or not all(isinstance(m, str) for m in muts):
        return
    cids = [c.get("id") for c in (s.get("characters") or [])
            if isinstance(c, dict) and c.get("id")]
    parts = {"palm": "wound_right_hand", "hand": "wound_right_hand",
             "arm": "wound_left_arm", "leg": "wound_leg",
             "shoulder": "wound_shoulder", "face": "wound_face",
             "chest": "wound_chest"}
    converted = []
    for m in muts:
        low = m.lower()
        if any(w in low for w in ("rain", "storm", "weather")):
            converted.append({"target": s.get("location", ""),
                              "key": "weather", "value": m, "persist": True})
            continue
        body = next((k for w, k in parts.items() if w in low), None)
        if any(w in low for w in ("wound", "cut", "bleed", "bandage",
                                  "slashed", "scorched", "burned")):
            converted.append({"target": cids[0] if cids else
                              s.get("location", ""),
                              "key": body or "wound", "value": m,
                              "persist": True})
            continue
        if any(w in low for w in ("cloak", "clothes", "armor", "garment")):
            converted.append({"target": cids[0] if cids else
                              s.get("location", ""),
                              "key": "clothing", "value": m, "persist": True})
            continue
        if any(w in low for w in ("barrier", "shield")):
            converted.append({"target": cids[0] if cids else
                              s.get("location", ""),
                              "key": "shield", "value": m, "persist": True})
            continue
        key = "".join(ch if ch.isalnum() else "_" for ch in low)[:40] \
            .strip("_") or "state"
        converted.append({"target": cids[0] if cids else
                          s.get("location", ""), "key": key,
                          "value": m, "persist": True})
    s["state_mutations"] = converted


def hero_budget_cap(shots: list[dict]) -> None:
    """Per-scene hero tier cap (mirrors the linter): every scene may host
    ONE hero moment even when small; extra heroes downgrade to standard
    (power + close-up are kept)."""
    by_scene: dict[str, list[dict]] = {}
    for s in shots:
        if isinstance(s, dict):
            by_scene.setdefault(s.get("scene", ""), []).append(s)
    for ss in by_scene.values():
        n = len(ss)
        allowed = max(int(n * 0.15), 1 if n <= 6 else 0)
        heroes = [s for s in ss if s.get("tier") == "hero"]
        if len(heroes) > allowed:
            for s in heroes[allowed:]:
                s["tier"] = "standard"


_TIERS = ("hero", "volume", "standard")
_MARKERS = ("cut", "continue", "match_action")


def _is_line(e: list) -> bool:
    return e[0] == "D" and len(e) >= 4


def _line(e: list) -> dict:
    return {"char": str(e[1]), "emotion": str(e[2]), "line": str(e[3])}


def _compact_shot(e: list, scene: str, loc: str, time: str, n: int,
                  before: int) -> tuple[dict, str]:
    """One compact shot entry as a shot dict (continuity CUT) and the
    continuity marker it carries ("" when none): the 7th element or the
    extra object's `continuity`. `n`: the shot's number in its scene;
    `before`: the shots already expanded (an id for a block without a
    scene)."""
    size = str(e[0]) if len(e) > 0 else "medium"
    cam = str(e[1]) if len(e) > 1 else "static"
    cid = str(e[2]) if len(e) > 2 else ""
    emo = str(e[3]) if len(e) > 3 else "calm"
    action = str(e[4]) if len(e) > 4 else ""
    pwr = e[5] if len(e) > 5 else None
    if isinstance(pwr, dict):
        pwr = str(pwr.get("power_id") or pwr.get("id") or "")
    extra = str(e[6]).lower() if len(e) > 6 and e[6] else ""
    # optional 8th element: {weapon, loadout, place, level,
    # handling, sounds, with, end, tier, continuity}
    ext = e[7] if len(e) > 7 and isinstance(e[7], dict) else {}
    tier = "standard"
    marker = ""
    if extra in _TIERS:
        tier = extra
    elif extra in _MARKERS:
        marker = extra.upper()
    # the extra object carries both when a shot needs both
    if str(ext.get("tier") or "").lower() in _TIERS:
        tier = str(ext["tier"]).lower()
    if str(ext.get("continuity") or "").lower() in _MARKERS:
        marker = str(ext["continuity"]).upper()
    # no position or facing: the compact entry has none, and a
    # made-up "foreground centre, facing the camera" on every shot
    # staged every prompt and opening frame the same way
    cobj = {"id": cid, "tier": "main", "emotion": emo, "action": action}
    # the others on screen: ids or [id, emotion, action]
    others: list[dict] = []
    withs = ext.get("with")
    for w in (withs if isinstance(withs, list) else []):
        w = [w] if isinstance(w, str) else w
        if not isinstance(w, list) or not w or not str(w[0]) or \
                str(w[0]) in [cid] + [o["id"] for o in others]:
            continue
        o = {"id": str(w[0]), "tier": "main"}
        for k, v in zip(("emotion", "action"), w[1:3]):
            if v:
                o[k] = str(v)
        others.append(o)
    if ext.get("loadout"):
        cobj["loadout"] = str(ext["loadout"])
    if ext.get("weapon"):
        cobj["weapon"] = str(ext["weapon"])
    if ext.get("handling"):
        cobj["handling"] = str(ext["handling"])
    shot = {
        "shot_id": f"{scene}_{n:03d}" if scene else f"s{before + 1:03d}_{n:03d}",
        "scene": scene, "location": loc, "time": time,
        "characters": ([cobj] if cid else []) + others,
        "power": pwr or None, "camera": cam,
        "shot_size": size, "duration_s": 5, "tier": tier,
        "dialogue": [], "prev_shot": None if n == 1 else f"{scene}_{n - 1:03d}",
        "state_mutations": [], "visible_background": [],
        "weather": "inherit",
        "primary_action": action, "secondary_motion": "",
        "start_state": {}, "end_state": {},
        "continuity": "CUT",
        "expected_visible": ([cid] if cid else []) + [o["id"] for o in others],
        "composition": "", "lens": "",
    }
    if ext.get("place"):
        shot["location_tag"] = str(ext["place"])
    if ext.get("level") is not None:
        try:
            shot["power_level"] = int(ext["level"])
        except (TypeError, ValueError):
            pass
    if ext.get("sounds"):
        shot["diegetic_sounds"] = str(ext["sounds"])
    if str(ext.get("end") or "").strip():
        shot["end_state"] = {"last_frame": str(ext["end"]).strip()}
    return shot, marker


def _norm_place(v) -> str:
    return " ".join(str(v or "").lower().replace("_", " ").split())


def _expand_takes(item: dict, before: int) -> list[dict]:
    """A scene written in takes: {"scene", "location", "time", "takes": [
    {"place": "<view>", "location"?: "<id>", "shots": [compact entries and
    dialogue lines]}]}. The edit follows from the structure, never from a
    marker: the scene's first shot is a CUT; a later take opens on a CUT when
    its place (location and view) differs from the take before, else on a
    MATCH_ACTION (a new angle on the same set, the action carrying on); every
    other shot CONTINUEs the shot before it, so a take renders as one
    continuous video. A take's place is its shots' location_tag. Markers and
    places written on single shots inside a take are left out, with a note."""
    scene = str(item.get("scene") or "")
    loc0 = str(item.get("location") or "")
    time = str(item.get("time") or "night")
    out: list[dict] = []
    n = 0
    prev_place: tuple[str, str] | None = None
    for take in item["takes"]:
        if not isinstance(take, dict) or not isinstance(take.get("shots"), list):
            continue
        loc = str(take.get("location") or loc0)
        view = str(take.get("place") or "").strip()
        first = True
        for e in take["shots"]:
            if not isinstance(e, list) or not e:
                continue
            if _is_line(e):
                # a line belongs to the shot before it, in this scene
                if out:
                    out[-1].setdefault("dialogue", []).append(_line(e))
                continue
            n += 1
            shot, marker = _compact_shot(e, scene, loc, time, n, before + len(out))
            notes = []
            if marker:
                notes.append(f"continuity {marker} inside a take left out: the takes set the edit")
            stray = str(shot.get("location_tag") or "").strip()
            if stray and _norm_place(stray) != _norm_place(view):
                notes.append(f"place {stray!r} on one shot of a take left out: a take keeps "
                             f"its place ({view or 'the main view'})")
            shot.pop("location_tag", None)
            if view:
                shot["location_tag"] = view
            place = (loc.lower(), _norm_place(view))
            if n == 1:
                shot["continuity"] = "CUT"
            elif first:
                shot["continuity"] = "CUT" if place != prev_place else "MATCH_ACTION"
            else:
                shot["continuity"] = "CONTINUE"
            if notes:
                shot["_norm_notes"] = notes
            out.append(shot)
            first = False
            prev_place = place
    return out


def expand_compact(package: dict) -> dict:
    """Expand the compact storyboard format into full shot dicts.

    The chat AI writes scenes as blocks to fit 2-4 scenes per reply:
      {"shots": {"shots": [
        {"scene": "s05", "location": "storm_rooftop", "time": "night",
         "shots": [
            ["wide", "dolly", "kael", "focused", "walks across the roof", null],
            ["close_up", "orbit", "kael", "rage", "unleashes lightning", "lightning", "hero"],
            ["D", "Vladimir", "cold", "Take it off, boy."],
         ]},
        ...
      ]}}
    Compact shot entry = [shot_size, camera, char_id, emotion, action,
                          power|null, tier?|continuity?, extra?] where the
    7th element is "hero"/"volume" (tier) when it starts with those words,
    else a continuity value, and the optional 8th an object: weapon,
    loadout, place, level, handling, sounds, `with` (the others on screen:
    ids or [id, emotion, action]), `end` (the shot's last frame) and `tier` /
    `continuity` (when a shot needs both). Dialogue = ["D", char, emotion,
    line]. Full-dict shots pass through untouched. A block with `takes`
    instead of `shots` is a scene written in takes (_expand_takes): its
    continuity follows from the takes."""
    import copy as _copy
    pkg = _copy.deepcopy(package)
    if "shots" not in pkg:
        return pkg
    shots = ((pkg.get("shots") or {}).get("shots") if
             isinstance(pkg.get("shots"), dict) else [])
    if not isinstance(shots, list):
        return pkg
    flat: list[dict] = []
    for item in shots:
        if isinstance(item, dict) and isinstance(item.get("takes"), list):
            flat.extend(_expand_takes(item, len(flat)))
        elif isinstance(item, dict) and isinstance(item.get("shots"), list):
            scene = str(item.get("scene") or "")
            loc = str(item.get("location") or "")
            time = str(item.get("time") or "night")
            n = 0
            for e in item["shots"]:
                if not isinstance(e, list) or not e:
                    continue
                if _is_line(e):
                    if flat:
                        flat[-1].setdefault("dialogue", []).append(_line(e))
                    continue
                n += 1
                shot, marker = _compact_shot(e, scene, loc, time, n, len(flat))
                if marker:
                    shot["continuity"] = marker
                flat.append(shot)
        elif isinstance(item, dict):
            flat.append(item)
        elif isinstance(item, list):
            # bare compact shot inside a plain shots list (rare)
            flat.append(item)
    pkg["shots"]["shots"] = flat
    return pkg


class _Spelling:
    """The known spelling of a name: an AI writes "sara", "SARA" or "Calm"
    for the Bible's "Sara" and the palette's "calm". Case and outer spaces
    are ignored; a name nothing matches is returned as written. `names` win
    over `extra` (the names a package brings itself)."""

    def __init__(self, names, extra=()) -> None:
        self.tiers = []
        for group in (names, extra):
            exact = {str(n) for n in group if isinstance(n, str)}
            low: dict[str, str] = {}
            for n in sorted(exact):
                low.setdefault(n.strip().lower(), n)
            self.tiers.append((exact, low))

    def __call__(self, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        s = v.strip()
        for exact, low in self.tiers:
            if s in exact:
                return s
            if s.lower() in low:
                return low[s.lower()]
        return v


def match_known(names, want: Any) -> Any:
    """`want` in the spelling of `names` (case and outer spaces ignored)."""
    return _Spelling(names)(want)


def _unique(items: list, key) -> list:
    seen: set = set()
    out = []
    for x in items:
        k = key(x)
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def canonical_ids(pkg: dict, p_bible, known_scenes=()) -> None:
    """Give every reference in an (expanded, safe-id) package the Bible's
    spelling: character, crowd, location, scene, power, weapon, loadout and
    place names, the characters' emotions and the shot vocabulary (sizes,
    moves, tiers, positions); a name the package's own bible section brings
    is the spelling of the package's other references to it. A chat AI that
    lowercased "Sara" made the story import treat her as someone new: on
    Windows "sara.yaml" is "Sara.yaml", and her weapons, loadouts and
    pictures were written over. Names nothing matches stay as written, for
    the linter to name."""
    from linter import CAMERA_MOVES, CONTINUITY_TYPES, FACINGS, POSITIONS, SHOT_SIZES, TIERS
    chars = p_bible.characters() or {}
    crowd = p_bible.crowd_types() or {}
    locs = p_bible.locations() or {}
    bible = pkg.get("bible") if isinstance(pkg.get("bible"), dict) else {}
    new_chars = [c.get("id") for c in bible.get("characters") or [] if isinstance(c, dict)]
    new_locs = [loc.get("id") for loc in bible.get("locations") or [] if isinstance(loc, dict)]
    new_crowd = list(bible.get("crowd_types") or {}) if isinstance(bible.get("crowd_types"), dict) else []
    beats = (pkg.get("beats") or {}).get("beats") if isinstance(pkg.get("beats"), dict) else None
    scenes = (pkg.get("scenes") or {}).get("scenes") if isinstance(pkg.get("scenes"), dict) else None
    shots = (pkg.get("shots") or {}).get("shots") if isinstance(pkg.get("shots"), dict) else None
    new_scenes = [b.get("scene") for b in beats or [] if isinstance(b, dict)]
    who = _Spelling(list(chars) + list(crowd), new_chars + new_crowd)
    where = _Spelling(locs, new_locs)
    # the scenes the project has (its beats; scene files) win over the package's
    scene_of = _Spelling(list(p_bible.scenes() or {}) + [str(s) for s in known_scenes or () if s], new_scenes)
    powers = _Spelling([p for c in chars.values() for p in (c.get("powers") or {})])
    size, move, tier = _Spelling(SHOT_SIZES), _Spelling(CAMERA_MOVES), _Spelling(TIERS)
    pos, facing, cont = _Spelling(POSITIONS), _Spelling(FACINGS), _Spelling(CONTINUITY_TYPES)

    new_by_id: dict = {}          # the package's own new characters (their palettes)

    def of(cid: Any, key: str) -> _Spelling:
        return _Spelling((chars.get(cid) or {}).get(key) or (new_by_id.get(cid) or {}).get(key) or [])

    def target(v: Any) -> Any:
        w = who(v)
        return w if w != v else where(v)

    for c in bible.get("characters") or []:
        if isinstance(c, dict):
            c["id"] = who(c.get("id"))
            if isinstance(c.get("powers"), dict):
                own = of(c["id"], "powers")
                c["powers"] = {own(k): v for k, v in c["powers"].items()}
            if isinstance(c.get("location_binding"), list):
                c["location_binding"] = [where(x) for x in c["location_binding"]]
            new_by_id.setdefault(c["id"], c)
    for loc in bible.get("locations") or []:
        if isinstance(loc, dict):
            loc["id"] = where(loc.get("id"))
    if isinstance(bible.get("crowd_types"), dict):
        bible["crowd_types"] = {who(k): v for k, v in bible["crowd_types"].items()}
    for e in beats or []:
        if isinstance(e, dict):
            for k, sp in (("scene", scene_of), ("location", where)):
                if k in e:
                    e[k] = sp(e[k])
    for e in scenes or []:
        if isinstance(e, dict):
            for k, sp in (("id", scene_of), ("location", where)):
                if k in e:
                    e[k] = sp(e[k])
            if isinstance(e.get("characters"), list):
                e["characters"] = [who(x) for x in e["characters"]]
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        old_scene = s.get("scene")
        if "scene" in s:
            s["scene"] = scene_of(old_scene)
        if isinstance(old_scene, str) and s.get("scene") != old_scene:
            # shot ids are "<scene>_<n>": they follow the scene's spelling
            for k in ("shot_id", "prev_shot"):
                v = s.get(k)
                if isinstance(v, str) and v.startswith(old_scene + "_"):
                    s[k] = s["scene"] + v[len(old_scene):]
        if "location" in s:
            s["location"] = where(s["location"])
        if s.get("location_tag"):
            tags = [r.get("tag") for r in ((locs.get(s.get("location")) or {}).get("reference_images") or [])
                    if isinstance(r, dict) and r.get("tag")]
            s["location_tag"] = _Spelling(tags)(s["location_tag"])
        if s.get("power"):
            s["power"] = powers(s["power"])
        for k, sp in (("shot_size", size), ("camera", move), ("tier", tier), ("continuity", cont)):
            if k in s:
                s[k] = sp(s[k])
        for c in s.get("characters") or []:
            if not isinstance(c, dict):
                continue
            c["id"] = who(c.get("id"))
            for k, field in (("weapon", "weapons"), ("loadout", "loadouts"), ("emotion", "emotion_palette")):
                if c.get(k):
                    c[k] = of(c["id"], field)(c[k])
            for k, sp in (("position", pos), ("facing", facing)):
                if c.get(k):
                    c[k] = sp(c[k])
        if isinstance(s.get("characters"), list):
            # "with": ["sara"] beside the lead "Sara" is the same person once folded
            s["characters"] = _unique(s["characters"], lambda c: c.get("id") if isinstance(c, dict) else id(c))
        for k in ("expected_visible", "visible_background"):
            if isinstance(s.get(k), list):
                s[k] = _unique([who(x) for x in s[k]], lambda x: x)
        for d in s.get("dialogue") or []:
            if isinstance(d, dict):
                d["char"] = who(d.get("char"))
                if d.get("emotion"):
                    d["emotion"] = of(d["char"], "emotion_palette")(d["emotion"])
        for m in s.get("state_mutations") or []:
            if isinstance(m, dict) and m.get("target"):
                m["target"] = target(m["target"])



def normalize_package(package: dict, p_bible, scenes=None) -> dict:
    """Normalize a full AI package (or a {shots: [...]} fragment). `scenes`:
    the project's scene ids (its beats), the spelling of the package's."""
    pkg = expand_compact(copy.deepcopy(package))
    remap = normalize_ids(pkg)
    if remap:
        pkg.setdefault("_norm_notes", []).append(
            "renamed ids that cannot be file names: "
            + ", ".join(f"{k!r} -> {v!r}" for k, v in remap.items()))
    if p_bible is not None:
        canonical_ids(pkg, p_bible, scenes or ())
    if "shots" not in pkg:
        return pkg
    shots = ((pkg.get("shots") or {}).get("shots") if
             isinstance(pkg.get("shots"), dict) else [])
    if not isinstance(shots, list):
        return pkg
    crowd = set(p_bible.crowd_types().keys()) if p_bible else set()
    chars_b = p_bible.characters() if p_bible else {}
    for s in shots:
        if not isinstance(s, dict):
            continue
        # character tiers come from the Bible (compact entries default to
        # "main" — the linter requires exact tier matches)
        for c in (s.get("characters") or []):
            if isinstance(c, dict) and c.get("id") in chars_b:
                bt = chars_b[c["id"]].get("tier")
                if bt:
                    c["tier"] = bt
        # a chat AI sometimes tags the WRONG character with a power (e.g.
        # "Vladimir dives aside as Kael's lightning tears through..." gets
        # power=lightning). If no character in the shot owns that power,
        # it is a tag error — drop it rather than reject the whole scene.
        pwr = s.get("power")
        if pwr:
            owners = [c.get("id") for c in (s.get("characters") or [])
                      if isinstance(c, dict)
                      and pwr in ((chars_b.get(c.get("id"), {}) or {})
                                  .get("powers") or {})]
            if not owners:
                notes = s.setdefault("_norm_notes", [])
                notes.append(f"power '{pwr}' not owned by any in-shot "
                             f"character — dropped (tag error)")
                s["power"] = None
        pwr = s.get("power")
        if isinstance(pwr, dict):
            pid = pwr.get("power_id") or pwr.get("id")
            s["power"] = str(pid) if pid else None
        for d in (s.get("dialogue") or []):
            if isinstance(d, dict) and "char" not in d and d.get("speaker"):
                d["char"] = d.pop("speaker")
        _convert_mutations(s)
        bg = s.get("visible_background") or []
        s["visible_background"] = [b for b in bg if b in crowd]
    _force_cut_on_scene_change(shots)
    hero_budget_cap(shots)
    return pkg


def _force_cut_on_scene_change(shots: list[dict]) -> None:
    """Continuity is an edit decision: a CONTINUE / MATCH_ACTION shot may only
    bridge within the SAME scene and location. The LLM sometimes marks a scene
    transition CONTINUE; coerce it to CUT here (with a note) so the render can
    never anchor on a tail from another world."""
    by_id = {str(s.get("shot_id")): s for s in shots if isinstance(s, dict)}
    for s in shots:
        if not isinstance(s, dict):
            continue
        cont = str(s.get("continuity") or "CUT").upper()
        if cont not in ("CONTINUE", "MATCH_ACTION"):
            continue
        prev = by_id.get(str(s.get("prev_shot") or ""))
        if prev is None:
            continue
        changed = str(prev.get("scene")) != str(s.get("scene"))
        pl, cl = str(prev.get("location") or ""), str(s.get("location") or "")
        if pl and cl and pl != cl:
            changed = True
        if changed:
            s["continuity"] = "CUT"
            notes = s.setdefault("_norm_notes", [])
            notes.append(f"continuity {cont} across a scene/location change "
                         f"forced to CUT")
            continue
        # one take renders with one background picture: a CONTINUE into
        # another view of the place is a new angle on the moving action
        pv, cv = _norm_place(prev.get("location_tag")), _norm_place(s.get("location_tag"))
        if cont == "CONTINUE" and pv and cv and pv != cv:
            s["continuity"] = "MATCH_ACTION"
            s.setdefault("_norm_notes", []).append(
                f"CONTINUE into another view ({prev.get('location_tag')} -> "
                f"{s.get('location_tag')}) made a MATCH_ACTION")
