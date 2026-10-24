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
                          power|null, tier?|continuity?] where the 7th
    element is "hero"/"volume" (tier) when it starts with those words,
    else a continuity value. Dialogue = ["D", char, emotion, line].
    Full-dict shots pass through untouched."""
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
        if isinstance(item, dict) and isinstance(item.get("shots"), list):
            scene = str(item.get("scene") or "")
            loc = str(item.get("location") or "")
            time = str(item.get("time") or "night")
            entries = item["shots"]
            n = 0
            for e in entries:
                if not isinstance(e, list) or not e:
                    continue
                if e[0] == "D" and len(e) >= 4:
                    if flat:
                        flat[-1].setdefault("dialogue", []).append(
                            {"char": str(e[1]), "emotion": str(e[2]),
                             "line": str(e[3])})
                    continue
                n += 1
                size = str(e[0]) if len(e) > 0 else "medium"
                cam = str(e[1]) if len(e) > 1 else "static"
                cid = str(e[2]) if len(e) > 2 else ""
                emo = str(e[3]) if len(e) > 3 else "calm"
                action = str(e[4]) if len(e) > 4 else ""
                pwr = e[5] if len(e) > 5 else None
                if isinstance(pwr, dict):
                    pwr = str(pwr.get("power_id") or pwr.get("id") or "")
                extra = str(e[6]).lower() if len(e) > 6 and e[6] else ""
                # optional 8th element: reference-facing choices
                # {weapon, loadout, place, level, handling, sounds}
                ext = e[7] if len(e) > 7 and isinstance(e[7], dict) else {}
                tier = "standard"
                continuity = "CUT"
                if extra in ("hero", "volume", "standard"):
                    tier = extra
                elif extra in ("cut", "continue", "match_action"):
                    continuity = extra.upper()
                cobj = {"id": cid, "tier": "main",
                        "emotion": emo, "action": action,
                        "position": "foreground_center",
                        "facing": "camera"}
                if ext.get("loadout"):
                    cobj["loadout"] = str(ext["loadout"])
                if ext.get("weapon"):
                    cobj["weapon"] = str(ext["weapon"])
                if ext.get("handling"):
                    cobj["handling"] = str(ext["handling"])
                shot = {
                    "shot_id": f"{scene}_{n:03d}" if scene else f"s{len(flat) + 1:03d}_{n:03d}",
                    "scene": scene, "location": loc, "time": time,
                    "characters": ([cobj] if cid else []),
                    "power": pwr or None, "camera": cam,
                    "shot_size": size, "duration_s": 5, "tier": tier,
                    "dialogue": [], "prev_shot": None if n == 1 else
                    f"{scene}_{n - 1:03d}",
                    "state_mutations": [], "visible_background": [],
                    "weather": "inherit",
                    "primary_action": action, "secondary_motion": "",
                    "start_state": {}, "end_state": {},
                    "continuity": continuity,
                    "expected_visible": [cid] if cid else [],
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
                flat.append(shot)
        elif isinstance(item, dict):
            flat.append(item)
        elif isinstance(item, list):
            # bare compact shot inside a plain shots list (rare)
            flat.append(item)
    pkg["shots"]["shots"] = flat
    return pkg


def normalize_package(package: dict, p_bible) -> dict:
    """Normalize a full AI package (or a {shots: [...]} fragment)."""
    pkg = expand_compact(copy.deepcopy(package))
    remap = normalize_ids(pkg)
    if remap:
        pkg.setdefault("_norm_notes", []).append(
            "renamed ids that cannot be file names: "
            + ", ".join(f"{k!r} -> {v!r}" for k, v in remap.items()))
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
