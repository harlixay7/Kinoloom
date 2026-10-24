"""ShotSpec — the filmmaking contract for a shot.

Extends the legacy shot schema (scene/camera/characters/power/state_mutations)
with a film-grammar layer:

    narrative/beat     — story intent of the clip
    continuity         — how this clip joins the previous one
    primary_action     — the ONE dominant motion the video model must show
    secondary_motion   — supporting motion (hair, cloth, sparks, ...)
    start_state/end_state — facts true at frame 0 / the last frame
    composition/lens   — framing overrides
    keyframe_source    — where the I2V start image comes from
    selected_loras     — LoRA hooks for this clip
    expected_visible   — character ids that must be on screen

Every field is optional. A legacy shot (no ShotSpec key present) flows
through the pipeline untouched — backward compatibility is law: existing
batches keep producing identical prompts and lint results.

Field roles:
    narrative         (str)  what happens in this shot, in story terms
    beat              (str)  the story beat this shot realizes
    wardrobe          (str)  wardrobe override id (cycle axis, §6)
    props             (list) named props present in the shot
    start_state       (dict) k:v facts true at frame 0 (wounds, held
                             items, posture, ...)
    end_state         (dict) k:v facts true at the last frame — the goal
                             the clip must end on
    primary_action    (str)  the ONE dominant motion the model must show;
                             keep under ~25 words
    secondary_motion  (str)  supporting motion that sells the shot
                             (hair, cloth, debris, sparks, ...)
    continuity        (str)  CUT | CONTINUE | MATCH_ACTION — how this
                             shot joins the previous one
    keyframe_source   (str)  auto | bible | roster | none — where the
                             I2V start image is taken from
    selected_loras    (list) LoRA hooks for this clip: [{name, strength}]
    expected_visible  (list) character ids that must be visible in frame
    composition       (str)  free-text framing/composition override
    lens              (str)  lens override (24mm/35mm/50mm/85mm/135mm)
    spec_version      (int)  contract version for future migrations
"""
from __future__ import annotations

import copy
from typing import Any

CONTINUITY_TYPES = ("CUT", "CONTINUE", "MATCH_ACTION")

SHOTSPEC_DEFAULTS: dict[str, Any] = {
    "narrative": "",
    "beat": "",
    "wardrobe": "",
    "props": [],
    "start_state": {},
    "end_state": {},
    "primary_action": "",
    "secondary_motion": "",
    "continuity": "CUT",
    "keyframe_source": "auto",
    "selected_loras": [],
    "expected_visible": [],
    "composition": "",
    "lens": "",
    "spec_version": 1,
}


def apply_defaults(shot: dict) -> dict:
    """Return a NEW dict: the shot with SHOTSPEC_DEFAULTS filled in for
    any missing key. Mutable defaults are deep-copied so callers never
    share list/dict state. The input shot is not mutated."""
    out = copy.deepcopy(SHOTSPEC_DEFAULTS)
    out.update(shot)
    return out


def validate(shot: dict) -> list[str]:
    """Contract warnings for a shot (never raised — the linter owns
    hard errors). Checks: continuity value, primary_action length,
    expected_visible ids, selected_loras shape. Legacy shots (no keys)
    produce no warnings."""
    warnings: list[str] = []

    cont = shot.get("continuity")
    if cont is not None and cont not in CONTINUITY_TYPES:
        warnings.append(f"continuity '{cont}' not in {CONTINUITY_TYPES}")

    pa = shot.get("primary_action")
    if pa is not None:
        words = str(pa).split()
        if not words:
            warnings.append("primary_action empty")
        elif len(words) > 25:
            warnings.append(f"primary_action {len(words)} words (>25)")

    chars = shot.get("characters") or []
    char_ids = {c.get("id") for c in chars if isinstance(c, dict)}
    for ev in shot.get("expected_visible") or []:
        if ev not in char_ids:
            warnings.append(f"expected_visible '{ev}' not among shot "
                            "characters")

    loras = shot.get("selected_loras")
    if loras is not None:
        if not isinstance(loras, list):
            warnings.append("selected_loras must be a list of "
                            "{name, strength}")
        else:
            for i, l in enumerate(loras):
                if not isinstance(l, dict) or "name" not in l \
                        or "strength" not in l:
                    warnings.append(f"selected_loras[{i}] must be "
                                    "{name, strength}")

    return warnings


def is_legacy(shot: dict) -> bool:
    """True when the shot carries NONE of the ShotSpec keys — i.e. it was
    written before ShotSpec existed and must pass through every pipeline
    unchanged."""
    return not any(k in shot for k in SHOTSPEC_DEFAULTS)


def continuity_of(shot: dict) -> str:
    """Effective continuity type: the shot's value when present and
    valid, else CUT (the legacy default)."""
    cont = shot.get("continuity", "CUT")
    return cont if cont in CONTINUITY_TYPES else "CUT"
