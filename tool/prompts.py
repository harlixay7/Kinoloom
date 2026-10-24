"""Deterministic prompt assembly.
Fixed token order: [CINEMATOGRAPHY] -> [ENVIRONMENT/LOCATION] -> [MAIN CHAR
identity+action] -> [SECONDARY] -> [POWER/VFX] -> [CAMERA] -> [MOOD].
Identity/outfit/power strings are quoted VERBATIM from the Bible — the same
sentence appears in every clip, so the model cannot drift."""
from __future__ import annotations

from typing import Any

from bible import Bible

CHAR_TIER_ORDER = {"main": 0, "supporting": 1, "recurring": 2, "crowd": 3}


def _with_noun(value: Any, noun: str) -> str:
    """Append `noun` only when `value` does not already end with it.

    `bible/universe.yaml` stores `default_lens: "35mm prime lens"` and
    `default_film_stock: "cinematic film stock"` — the noun is *already in the
    value*. The builder used to append it unconditionally, so **every prompt**
    contained "35mm prime lens lens" and "cinematic film stock film stock".
    Duplicated tokens dilute the prompt for a 5B model, so this is idempotent
    and therefore correct whether or not a given value carries the noun.
    """
    v = str(value).strip()
    return v if v.lower().endswith(noun.lower()) else f"{v} {noun}"


class PromptBuilder:
    def __init__(self, bible: Bible) -> None:
        self.b = bible

    def _cinematography(self) -> str:
        uni = self.b.universe()
        c = uni.get("cinematography", {})
        parts = []
        if c.get("default_lens"):
            parts.append(_with_noun(c["default_lens"], "lens"))
        if c.get("default_film_stock"):
            parts.append(_with_noun(c["default_film_stock"], "film stock"))
        if c.get("depth_of_field"):
            parts.append(f"depth of field {c['depth_of_field']}")
        if c.get("color_temp_key") and c.get("color_temp_fill"):
            parts.append(
                f"key light {c['color_temp_key']}K, fill {c['color_temp_fill']}K")
        if c.get("grain"):
            parts.append(c["grain"])
        return ", ".join(p for p in parts if p)

    def _location(self, shot: dict, state: dict) -> str:
        loc = self.b.locations().get(shot.get("location", ""), {})
        parts = []
        if loc.get("descriptor"):
            parts.append(loc["descriptor"])
        if shot.get("time"):
            parts.append(f"time of day: {shot['time']}")
        if loc.get("lighting"):
            parts.append(f"lighting: {loc['lighting']}")
        # accumulated state mutations (6.7.7)
        locstate = state.get(shot.get("location", ""), {})
        for k, v in locstate.items():
            if k in ("weather", "lighting", "condition"):
                parts.append(f"{k}: {v}")
        return ", ".join(parts)

    def _character(self, c: dict, shot: dict,
                   state: dict[str, dict] | None = None) -> str:
        char = self.b.characters().get(c["id"])
        if char is None:
            ct = self.b.crowd_types().get(c["id"], {})
            return f"background: {ct.get('descriptor', c['id'])}"
        parts = []
        for f in ("body", "outfit"):          # VERBATIM locked strings
            if char.get(f):
                parts.append(char[f])
        st = self._state(shot, c["id"], state)
        for k, v in st.items():
            if k.startswith("wound_") or k in ("clothing", "shield", "stance"):
                parts.append(f"{k.replace('_', ' ')}: {v}")
        emo = c.get("emotion")
        if emo:
            parts.append(f"expression: {emo}")
        action = c.get("action")
        if action:
            parts.append(action)
        return f"{', '.join(parts)}"

    def _power(self, shot: dict) -> str:
        pwr_id = shot.get("power")
        if not pwr_id:
            return ""
        for c in shot.get("characters", []):
            char = self.b.characters().get(c["id"], {})
            p = char.get("powers", {}).get(pwr_id)
            if p:
                d = p.get("descriptor") or pwr_id
                motion = p.get("motion")
                return f"{d}, {motion}" if motion else d
        return pwr_id

    def _state(self, shot: dict, char_id: str,
               accumulated: dict[str, dict] | None = None) -> dict:
        """Accumulated persistent state for a character at this shot:
        inherited scene state merged with this shot's persist mutations
        (this shot wins on key conflicts)."""
        out: dict[str, str] = dict((accumulated or {}).get(char_id, {}))
        for c in shot.get("characters", []):
            if c["id"] == char_id:
                for mut in shot.get("state_mutations", []):
                    if mut.get("target") == char_id and mut.get("persist"):
                        out[mut["key"]] = mut["value"]
        return out

    def _background(self, shot: dict) -> str:
        """Crowd / army descriptors for the shot's visible_background.

        Crowds NEVER take a reference slot (there is no image of an army) —
        they are pure prompt text. The linter tells the LLM to move extra
        characters here, so this is what makes "an army of guards" actually
        appear in the render instead of being silently dropped.
        """
        ids = [str(b) for b in (shot.get("visible_background") or []) if b]
        if not ids:
            return ""
        try:
            ct = self.b.crowd_types() or {}
        except Exception:  # noqa: BLE001
            ct = {}
        descs: list[str] = []
        for cid in ids:
            d = str((ct.get(cid) or {}).get("descriptor") or cid)
            if d and d not in descs:
                descs.append(d)
        return "background: " + ", ".join(descs) if descs else ""

    def build(self, shot: dict, state: dict[str, dict] | None = None) -> str:
        """Deterministic prompt for a shot. state = accumulated scene state."""
        state = state or {}
        parts = []
        cine = self._cinematography()
        if cine:
            parts.append(cine)
        # realism clause (user requirement: every generation must read as
        # live-action photography — illustration drift is the top complaint).
        # "real actors, practical sets" is the anti-animation anchor: without
        # it the reference conditioning and fantasy nouns drag renders into a
        # stylized CG look.
        parts.append("photorealistic live-action footage, live-action "
                     "photography with real actors and practical sets, "
                     "real-world materials and lighting, natural film grain, "
                     "no illustration or animation")
        loc = self._location(shot, state)
        if loc:
            parts.append(loc)
        chars = sorted(shot.get("characters", []),
                       key=lambda c: CHAR_TIER_ORDER.get(
                           self.b.characters().get(c["id"], {}).get(
                               "tier", "crowd"), 3))
        for c in chars:                        # main first = most attention
            parts.append(self._character(c, shot, state))
        bg = self._background(shot)
        if bg:
            parts.append(bg)
        pwr = self._power(shot)
        if pwr:
            parts.append(f"VFX: {pwr}")
        cam = shot.get("camera")
        size = shot.get("shot_size")
        if cam or size:
            parts.append(f"camera: {size or 'medium'} shot, {cam or 'static'}")
        mood = shot.get("mood")
        if mood:
            parts.append(f"mood: {mood}")
        return ", ".join(parts)

    def negative(self, shot: dict) -> str:
        return ("blurry, low quality, deformed face, face shape morphing, "
                "extra limbs, limb deformation, hands morphing, watermark, "
                "text, grain artifacts, flicker, ghosting, double exposure, "
                "jpeg artifacts, fog, haze, smeared details, "
                # camera stability — without these the model freely invents
                # handheld drift, which is the "sloppy camera" symptom
                "shaky camera, handheld wobble, camera jitter, "
                # reversed locomotion — the prompt says "walking forward" but
                # never relative to WHAT, so the model is free to pick a
                # direction and was observed walking the subject backwards.
                "walking backwards, moonwalking, reversed motion")
