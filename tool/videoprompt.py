"""Motion-first video prompt assembly.

VideoPromptBuilder extends prompts.PromptBuilder for cinematic video clip
prompts. Policy:

- LEGACY shots (no ShotSpec keys) are passed through VERBATIM: build()
  returns exactly prompts.PromptBuilder.build(shot, state) and
  negative() returns exactly prompts.PromptBuilder.negative(shot) —
  byte-identical, so every existing batch renders the same as before.
- NEW shots rendered with I2V + LoRAs get a motion-first structured
  prompt. The start keyframe already shows identity and scene, so the
  text describes MOTION only. With a character face LoRA the body/outfit
  description is omitted entirely (identity is carried by the I2V
  keyframe + LoRA; the locks section keeps it stable).
- NEW shots without I2V+LoRA fall back to the legacy assembly verbatim,
  with primary_action / secondary_motion appended when present.

Structure (only non-empty sections, joined ", "):
  1. start state:     shot.start_state k:v pairs
  2. timing:          "0-1s settle, 1-4s action, 4-5s settle" (only when
                      primary_action AND secondary_motion are both set)
  3. primary action:  shot.primary_action (else first character's action)
  4. performance:     "expression: <emotion>"
  5. secondary motion: shot.secondary_motion
  6. VFX motion:      Bible power descriptor + motion for shot.power
  7. environment reaction: shot.end_state / accumulated location state
                      keys (weather/lighting/condition)
  8. camera:          "camera: <shot_size> shot, <camera>"
  9. end state:       shot.end_state k:v pairs
  10. locks:          identity/camera stability locks (I2V + LoRA only)
"""
from __future__ import annotations

from typing import Any

from bible import Bible
from cast_look import body_text
from prompts import CHAR_TIER_ORDER, PromptBuilder
from shotspec import is_legacy

TIMING_LINE = "0-1s settle, 1-4s action, 4-5s settle"
LOCKS_LINE = ("locks: same face, same outfit, no new people, "
              "preserve camera orientation")
_LOCATION_STATE_KEYS = ("weather", "lighting", "condition")


class VideoPromptBuilder:
    """Motion-first builder. Mirrors prompts.PromptBuilder for legacy
    shots; assembles the structured prompt for new shots."""

    def __init__(self, bible: Bible, db: Any = None) -> None:
        self.b = bible
        self.db = db                     # reserved (keyframe source lookup)
        self.legacy = PromptBuilder(bible)

    # ------------------------------------------------------------- helpers
    def _sorted_chars(self, shot: dict) -> list[dict]:
        """Shot characters in legacy tier order (main first)."""
        return sorted(
            shot.get("characters") or [],
            key=lambda c: CHAR_TIER_ORDER.get(
                self.b.characters().get(c.get("id"), {}).get("tier", "crowd"),
                3))

    def _char_lora_present(self, shot: dict, loras: list) -> bool:
        """True when any LoRA hook targets a shot character: entry name
        (stem) equals a character id or follows the <id>_face convention
        used by the training pipeline."""
        cids = {c.get("id") for c in shot.get("characters") or []}
        for l in loras or []:
            name = str(l.get("name") if isinstance(l, dict) else l)
            stem = name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem in cids or stem.endswith("_face") or "face" in stem.lower():
                return True
        return False

    def _lora_triggers(self, loras: list) -> list[str]:
        """Trigger phrases for the active LoRAs, from the video LoRA registry.

        A trained LoRA only binds reliably when its trigger phrase is present.
        `env_EnvMagicCity` was trained *entirely* on "a magical fantasy city
        with soaring spires and glowing runes" (see
        `roster/datasets/environment/EnvMagicCity/meta.json`) — its registry
        `trigger` was empty until 2026-09-17, and this builder never injected
        triggers anyway, so the LoRA had nothing to latch onto and the prompt
        text alone decided the look.

        Returns deduplicated, non-empty phrases. Never raises: a registry
        problem must not break prompt assembly.
        """
        try:
            from lora_video import VideoLoraRegistry
            reg = VideoLoraRegistry()
        except Exception:
            return []
        out: list[str] = []
        for l in loras or []:
            name = str(l.get("name") if isinstance(l, dict) else l)
            stem = name.replace("\\", "/").rsplit("/", 1)[-1]
            try:
                e = reg.get(stem) or {}
            except Exception:
                continue
            t = str(e.get("trigger") or "").strip()
            if t and t not in out:
                out.append(t)
        return out

    def _identity(self, shot: dict, state: dict) -> list[str]:
        """Verbatim identity strings (legacy _character minus expression
        and action): Bible body/outfit plus accumulated wound/clothing/
        shield/stance state."""
        parts: list[str] = []
        for c in self._sorted_chars(shot):
            char = self.b.characters().get(c.get("id"))
            if char is None:
                ct = self.b.crowd_types().get(c.get("id"), {})
                parts.append(f"background: {ct.get('descriptor', c['id'])}")
                continue
            bits = [body_text(char), char.get("outfit")]
            st = state.get(c["id"], {})
            for k, v in st.items():
                if k.startswith("wound_") or k in ("clothing", "shield",
                                                   "stance"):
                    bits.append(f"{k.replace('_', ' ')}: {v}")
            parts.append(", ".join(x for x in bits if x))
        return parts

    def _background(self, shot: dict) -> str:
        """Crowd / army descriptors (visible_background, text-only)."""
        return self.legacy._background(shot)

    def _power(self, shot: dict) -> str:
        """Bible power descriptor + motion for shot.power (legacy
        lookup — same result as prompts._power)."""
        pwr_id = shot.get("power")
        if not pwr_id:
            return ""
        for c in shot.get("characters", []):
            char = self.b.characters().get(c.get("id"), {})
            p = char.get("powers", {}).get(pwr_id)
            if p:
                d = p.get("descriptor") or pwr_id
                motion = p.get("motion")
                return f"{d}, {motion}" if motion else d
        return pwr_id

    def _env_reaction(self, shot: dict, state: dict) -> list[str]:
        """Location-affecting end_state pairs + accumulated location
        state mutations (weather/lighting/condition)."""
        parts: list[str] = []
        for k, v in (shot.get("end_state") or {}).items():
            if k in _LOCATION_STATE_KEYS:
                parts.append(f"{k}: {v}")
        locstate = state.get(shot.get("location", ""), {})
        for k, v in locstate.items():
            if k in _LOCATION_STATE_KEYS:
                parts.append(f"{k}: {v}")
        return parts

    # ------------------------------------------------------------- H3 (T2VA)
    _H3_CAMERA = (
        ("push in", "the camera pushes in with small amplitude at slow speed"),
        ("push_in", "the camera pushes in with small amplitude at slow speed"),
        ("pull back", "the camera pulls out with small amplitude at slow speed"),
        ("pull_back", "the camera pulls out with small amplitude at slow speed"),
        ("pull out", "the camera pulls out with small amplitude at slow speed"),
        ("crash zoom", "the camera zooms in with large amplitude at fast speed"),
        ("zoom", "the camera zooms in with small amplitude at slow speed"),
        ("track", "the camera holds a tracking shot with small amplitude"),
        ("dolly", "the camera trucks with small amplitude at slow speed"),
        ("truck", "the camera trucks with small amplitude at slow speed"),
        ("pan", "the camera pans with small amplitude at slow speed"),
        ("tilt", "the camera tilts with small amplitude at slow speed"),
        ("crane", "the camera pedestals up with small amplitude at slow speed"),
        ("arc", "the camera arcs around the subject at slow speed"),
        ("handheld", "the camera shakes slightly"),
        ("shake", "the camera shakes slightly"),
        ("static", "the camera holds a static shot"),
        ("locked", "the camera holds a static shot"),
    )
    _H3_MUSIC = (
        ("panic", "Low pulsing strings under a distant horn, tempo rising, "
                  "non-diegetic, quiet."),
        ("awe", "Sustained choral pad swelling slowly, non-diegetic, quiet."),
        ("tension", "Low sustained strings and a slow heartbeat pulse, "
                    "non-diegetic, very quiet."),
        ("regroup", "Low sustained strings and a slow heartbeat pulse, "
                    "non-diegetic, very quiet."),
        ("confrontation", "Dark brass under driving low percussion at a "
                          "measured tempo, non-diegetic."),
        ("cataclysm", "Massive orchestral brass and choral hit building to a "
                      "peak, non-diegetic."),
        ("awakening", "Massive orchestral brass and choral hit building to a "
                      "peak, non-diegetic."),
        ("stand", "Massive orchestral brass and choral hit building to a "
                  "peak, non-diegetic."),
        ("final", "Massive orchestral brass and choral hit building to a "
                  "peak, non-diegetic."),
        ("dawn", "Warm string line rising gently over soft low brass, "
                 "non-diegetic."),
        ("hope", "Warm string line rising gently over soft low brass, "
                 "non-diegetic."),
    )

    def _h3_camera(self, shot: dict) -> str:
        cam = str(shot.get("camera") or "").lower()
        for needle, line in self._H3_CAMERA:
            if needle in cam:
                return line
        size = str(shot.get("shot_size") or "medium").replace("_", " ")
        return f"the camera holds a static shot framing a {size}"

    def _h3_soundscape(self, shot: dict, state: dict) -> str:
        loc_id = str(shot.get("location") or "")
        loc = (self.b.locations() or {}).get(loc_id) or {}
        desc = str(loc.get("descriptor") or loc_id or "the scene")
        time_of_day = str(shot.get("time") or "")
        weather = str(shot.get("weather") or "")
        loc_state = (state or {}).get(loc_id) or {}
        bits = [f"Ambient atmosphere of {desc}"]
        if time_of_day:
            bits.append(f"{time_of_day} air")
        w = str(loc_state.get("weather") or weather or "")
        if w and w != "inherit":
            bits.append(f"{w}")
        bits.append("footsteps, cloth movement and breathing close to the "
                    "camera; distant environment hum")
        return "; ".join(bits) + "."

    def _h3_music(self, shot: dict) -> str:
        mood = f"{shot.get('beat') or ''} {shot.get('narrative') or ''}".lower()
        for needle, line in self._H3_MUSIC:
            if needle in mood:
                return line
        return "N/A"

    def build_h3(self, shot: dict, state: dict | None = None) -> str:
        """MiniMax H3 structured prompt (docs/H3_PROMPT_GUIDE.md).

        integrated_multimodal_description / overall_soundscape /
        non_diegetic_music, camera grammar "motion + amplitude + speed",
        speaker IDs (S1..) with delivery OUTSIDE <d>, references introduced by
        the orchestrator's <Picture N> legend (submission-order safe). One
        continuous shot: no timestamps, matching the proven hero config.
        """
        state = state or {}
        chars = self._sorted_chars(shot)
        uni = self.b.universe() or {}
        cine = uni.get("cinematography") or {}
        stock = str(cine.get("default_film_stock") or "cinematic film stock")
        lens = str(cine.get("default_lens") or "35mm")
        genre = str(uni.get("genre") or "").strip()
        opener = ("Ultra-realistic live-action blockbuster cinematography, "
                  f"shot on a full-frame cinema camera with {lens} lenses, "
                  f"{stock}")
        if genre:
            opener += f", {genre}"

        parts: list[str] = [opener]
        loc_id = str(shot.get("location") or "")
        loc = (self.b.locations() or {}).get(loc_id) or {}
        loc_desc = str(loc.get("descriptor") or loc_id).strip()
        size = str(shot.get("shot_size") or "medium").replace("_", " ")
        parts.append(f"a {size} shot set in {loc_desc}")

        # --- subjects (identity verbatim; the H3 legend binds the pictures).
        # Outfit + weapon text come from the RESOLVED loadout presentation, so
        # the prompt always describes the exact clothes/weapon the reference
        # image shows (references reinforce, text is never the only carrier).
        from ltx_refs import resolve_presentation
        from promptspec import WEAPON_HANDLING
        by_state = state or {}
        subs: list[str] = []
        weapon_sfx: list[str] = []
        for c in chars:
            cid = str(c.get("id"))
            ch = self.b.characters().get(cid) or {}
            pres = resolve_presentation(ch, c, shot)
            bits = [body_text(ch),
                    str(pres.get("outfit") or ch.get("outfit") or "").strip()]
            if pres.get("weapon_txt"):
                wline = str(pres["weapon_txt"])
                hand = str(pres.get("handling") or "")
                if hand:
                    wline += f", {WEAPON_HANDLING.get(hand, hand)}"
                bits.append(f"carrying {wline}")
            st = by_state.get(cid) or {}
            for k, v in st.items():
                if k.startswith("wound_") or k in ("clothing", "shield",
                                                   "stance"):
                    bits.append(f"{k.replace('_', ' ')}: {v}")
            subs.append(", ".join(x for x in bits if x))
            if pres.get("sfx"):
                weapon_sfx.append(str(pres["sfx"]))
        if subs:
            parts.append("; ".join(subs))
        bg = self._background(shot)
        if bg:
            parts.append(bg)

        # --- action / performance / power
        primary = str(shot.get("primary_action") or "").strip()
        if not primary and chars:
            primary = str(chars[0].get("action") or "").strip()
        if primary:
            parts.append(primary)
        emo = next((str(c.get("emotion") or "") for c in chars
                    if c.get("emotion")), "")
        if emo:
            parts.append(f"expression {emo}")
        secondary = str(shot.get("secondary_motion") or "").strip()
        pwr = self._power(shot)
        if pwr:
            parts.append(f"the power manifests as: {pwr}")
        if secondary and secondary != pwr:
            parts.append(secondary)
        parts += self._env_reaction(shot, state)

        # --- camera grammar + dialogue (speaker IDs in stable cast order)
        parts.append(self._h3_camera(shot))
        ids = {str(c.get("id")): f"S{i + 1}" for i, c in enumerate(chars)}
        for d in (shot.get("dialogue") or []):
            if not isinstance(d, dict) or not d.get("line"):
                continue
            cid = str(d.get("char") or "")
            name = str((self.b.characters().get(cid) or {}).get("name") or cid)
            sid = ids.get(cid, "S1")
            delivery = f", {d.get('emotion')}" if d.get("emotion") else ""
            parts.append(f"the {name} ({sid}){delivery} says: "
                         f"<d>[English] {d['line']}</d>")

        body = ". ".join(p.strip().rstrip(".") for p in parts if p)
        sound = self._h3_soundscape(shot, state)
        # diegetic sound cues: weapon sfx + the power's own sound hint
        cues = list(weapon_sfx)
        try:
            pv = self._power(shot)
            if pv:
                ptype = str(shot.get("power") or "")
                owner = None
                for c in chars:
                    ch = self.b.characters().get(str(c.get("id"))) or {}
                    pw = (ch.get("powers") or {}).get(ptype)
                    if isinstance(pw, dict):
                        owner = pw
                        break
                hint = str((owner or {}).get("sfx") or "")
                if not hint:
                    from promptspec import POWER_TYPES as PT
                    for t, meta in PT.items():
                        if t in pv.lower():
                            hint = str(meta.get("sound_hint") or "")
                            break
                if hint:
                    cues.append(hint)
        except Exception:  # noqa: BLE001
            pass
        if cues:
            sound = sound.strip()
            if sound and not sound.endswith("."):
                sound += "."
            sound = (sound + " " + "; ".join(c for c in cues if c) + ".").strip()
        music = self._h3_music(shot)
        return (f"integrated_multimodal_description: {body}.\n"
                f"overall_soundscape: {sound}\n"
                f"non_diegetic_music: {music}")

    # ------------------------------------------------------------ assembly
    def build(self, shot: dict, state: dict | None = None,
              is_i2v: bool = False, loras: list | None = None) -> str:
        """Video prompt for a shot (returns the prompt string — legacy
        interface, byte-identical for legacy shots). state = accumulated
        scene state; is_i2v = I2V render with start keyframe; loras =
        active LoRA hooks (names or {name, strength} dicts)."""
        state = state or {}
        loras = loras or []

        if is_legacy(shot):
            return self.legacy.build(shot, state)

        if not (is_i2v and loras):
            # No I2V keyframe or no LoRAs: identity must live in the text.
            # Legacy assembly verbatim + motion sections appended.
            base = self.legacy.build(shot, state)
            # The legacy assembly ALREADY contains the primary action, so
            # appending it again duplicates the motion instruction — and the
            # copy lands *after* the camera clause, which reads as a
            # contradictory direction cue. Measured 2026-09-17: a prompt
            # contained "walking forward" twice. Only append what is genuinely
            # missing. Same rule as the LoRA triggers below.
            low = base.lower()
            extras = [x for x in (shot.get("primary_action"),
                                  shot.get("secondary_motion"))
                      if x and str(x).lower() not in low]
            extras += [t for t in self._lora_triggers(loras)
                       if t.lower() not in low]
            if not extras:
                return base
            return ", ".join([base] + extras)

        chars = self._sorted_chars(shot)
        main = chars[0] if chars else {}
        primary = shot.get("primary_action") or main.get("action", "")
        secondary = shot.get("secondary_motion", "")

        parts: list[str] = []
        # identity — carried by the I2V keyframe + LoRA when a character
        # LoRA is active; described verbatim otherwise
        if not self._char_lora_present(shot, loras):
            parts += self._identity(shot, state)
        bg = self._background(shot)
        if bg:
            parts.append(bg)
        # 1. start state
        ss = shot.get("start_state") or {}
        if ss:
            parts.append("start state: " + ", ".join(
                f"{k}: {v}" for k, v in ss.items()))
        # 2. timing (needs both motion fields)
        if primary and secondary:
            parts.append(TIMING_LINE)
        # 3. primary action
        if primary:
            parts.append(primary)
        # 4. performance
        emo = main.get("emotion")
        if emo:
            parts.append(f"expression: {emo}")
        # 5. secondary motion
        if secondary:
            parts.append(secondary)
        # 6. VFX motion
        pwr = self._power(shot)
        if pwr:
            parts.append(f"VFX: {pwr}")
        # 7. environment reaction
        parts += self._env_reaction(shot, state)
        # 7b. LoRA trigger phrases (see `_lora_triggers`)
        parts += self._lora_triggers(loras)
        # 8. camera — ALWAYS emitted. An absent camera clause lets the model
        #    invent movement, which reads as handheld wobble ("a 5-year-old
        #    filming"). An explicit movement — even a locked-off static — is
        #    what keeps it steady. `locked-off tripod` is unambiguous
        #    cinematography vocabulary, so it pins the camera rather than
        #    inviting drift.
        cam = shot.get("camera") or "static, locked-off tripod"
        size = shot.get("shot_size") or "medium"
        parts.append(f"camera: {size} shot, {cam}")
        # 9. end state
        es = shot.get("end_state") or {}
        if es:
            parts.append("end state: " + ", ".join(
                f"{k}: {v}" for k, v in es.items()))
        # 10. locks (this path only runs when is_i2v and loras are set)
        parts.append(LOCKS_LINE)
        return ", ".join(p for p in parts if p)

    def negative(self, shot: dict) -> str:
        """Negative prompt — identical to the legacy builder for all
        shots (the same quality guards apply)."""
        return self.legacy.negative(shot)
