"""Generation job engine.

- Balanced, deterministic combination scheduler: model counts are FINAL
  image counts; combinations form a prompt pool that each model's images
  are distributed through (strided — never "first N", never multiplied
  with model count unless the user explicitly enables full-cross mode).
- Runs are persisted in datasets_db; one worker thread executes them
  sequentially (single GPU). Crashed runs are recovered on startup.
- Per-image provenance + learned performance recording.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from core import ROOT, log_setup
from renderer import ComfyClient
from models import (adapt_prompt, build_workflow, get_model,
                    record_performance)
import datasets_db as db

log = log_setup("jobs")

DATASETS_DIR = ROOT / "roster" / "datasets"
MAX_BATCH = 120          # configurable safety cap

_lock = threading.Lock()
_cancel_ids: set[int] = set()
_worker: threading.Thread | None = None
_queue: list[int] = []
# Serializes still-index allocation + meta.json writes: lanes run
# concurrently, and two lanes must never pick the same file name.
_save_lock = threading.Lock()
# One lock PER MACHINE (lane). A run's lane holds its machine's lock for its
# whole segment, so a NEW run assigned to a busy machine waits until the
# previous run's lane there has finished, then starts automatically; lanes on
# free machines start immediately.
_lane_locks: dict[str, threading.Lock] = {}
_lane_locks_guard = threading.Lock()
_MAX_ACTIVE_RUNS = 4
_run_threads: dict[int, threading.Thread] = {}


def _lane_lock(lane: str) -> threading.Lock:
    with _lane_locks_guard:
        lk = _lane_locks.get(lane)
        if lk is None:
            lk = threading.Lock()
            _lane_locks[lane] = lk
        return lk


# --------------------------------------------------- combination planning
LEGACY_ANGLE_MAP = {"front-facing": "portrait", "three-quarter": "3/4 body",
                    "profile right": "profile", "profile left": "profile",
                    "looking up": "high angle", "looking down": "low angle hero",
                    "low angle": "low angle hero", "over-shoulder": "over-the-shoulder"}
LEGACY_POWER_CAM_MAP = {"low-angle dramatic shot": "low angle hero",
                        "wide shot": "wide", "extreme close-up": "close-up",
                        "overhead shot": "high angle",
                        "three-quarter angle": "3/4 body"}
LEGACY_ENV_CAM_MAP = {"wide establishing shot": "wide", "aerial shot": "wide",
                      "low-angle shot": "low angle hero", "dutch angle": "medium",
                      "ground-level shot": "low angle hero",
                      "ultra-wide shot": "wide"}


def is_legacy(mode: str, params: dict) -> bool:
    """Old wizard shapes take the legacy prompt path (full backward
    compatibility); new PromptSpec shapes take the structured path.

    Guarded: a payload that carries a NEW structured field (level ladder,
    place selection) always takes the structured path, even if a stale client
    still sends a legacy alias. The unguarded version silently ran the old
    "two raised hands" power prompt while the wizard showed the new UI.
    """
    if mode == "character":
        return bool(params.get("angles")) and not params.get("shots")
    if mode == "power":
        if params.get("level_ladder"):
            return False
        return bool(params.get("forms") or params.get("envs"))
    if mode == "environment":
        if params.get("places"):
            return False
        return bool(params.get("subtypes"))
    return False


def build_combos(mode: str, params: dict) -> list[dict]:
    """Deterministic prompt-combination pool per mode.

    Stacking: Fixed (identity) / Cycle (variation by
    image index) / Multiply (cross-product combos).
      character: expression multiply (× shots for legacy), pose/scene/
                 lighting cycle
      power:     intensity multiply, manifestation/behavior/reaction cycle
      environment: location × time multiply, atmosphere/weather/foreground
                 cycle
    Legacy wizard shapes keep their old multiply semantics. Invalid
    combinations are filtered OUT of the pool (dependency validation) —
    skipped, never errors."""
    combos: list[dict] = []
    if mode == "character":
        if params.get("identity_plates"):
            # Identity plates (Tab 1): exactly three framing plates with ONE
            # stable expression, no weapon, plain backdrop — these become the
            # close/medium/full hero refs on import (framing-matched picks).
            combos = [
                {"emotion": "stoic", "shot": "close-up", "plate": "close"},
                {"emotion": "stoic", "shot": "3/4 body", "plate": "medium"},
                {"emotion": "stoic", "shot": "full body", "plate": "full"},
            ]
        elif params.get("matrix"):
            # Wardrobe x weapon matrix (Tab 2): explicit (outfit, weapon)
            # pairs from the wizard. `weapon` absent/"" = the unarmed look.
            # The per-row `outfits` filter in distribute() routes each look
            # to its own model (per-clothing model selection).
            emo = (params.get("emotions") or ["stoic"])[0]
            shot = (params.get("shots") or ["3/4 body"])[0]
            for row in params.get("matrix") or []:
                if not isinstance(row, dict):
                    continue
                outfit = str(row.get("outfit") or "").strip()
                if not outfit:
                    continue
                weapon = str(row.get("weapon") or "").strip()
                combo = {"emotion": emo, "shot": shot, "outfit": outfit}
                if weapon:
                    combo["weapon"] = weapon
                combos.append(combo)
        else:
            emotions = params.get("emotions") or ["stoic"]
            shots = params.get("shots") or ["portrait"]
            if is_legacy(mode, params):
                shots = [LEGACY_ANGLE_MAP.get(a, a) for a in
                         (params.get("angles") or ["front-facing"])]
            for emo in emotions:
                for sh in shots:
                    combos.append({"emotion": emo, "shot": sh})
    elif mode == "power":
        if is_legacy(mode, params):
            forms = params.get("forms") or ["hands"]
            envs = params.get("envs") or ["dark city street"]
            cameras = params.get("cameras") or ["low-angle dramatic shot"]
            for form in forms:
                for env in envs:
                    for cam in cameras:
                        combos.append({"form": form, "env": env,
                                       "camera": cam})
        else:
            intensities = params.get("intensities") or ["steady"]
            for i in intensities:
                combos.append({"intensity": i})
    elif mode == "environment":
        if is_legacy(mode, params):
            subtypes = params.get("subtypes") or ["city"]
            locs_by_sub = params.get("locations") or {}
            times = params.get("times") or ["night"]
            cameras = params.get("cameras") or ["wide establishing shot"]
            for subtype in subtypes:
                if isinstance(locs_by_sub, dict):
                    pool = locs_by_sub.get(subtype) or [""]
                elif isinstance(locs_by_sub, list):
                    pool = locs_by_sub or [""]
                else:
                    pool = [str(locs_by_sub)] if locs_by_sub else [""]
                for loc in pool:
                    for tm in times:
                        for cam in cameras:
                            combos.append({"subtype": subtype, "location": loc,
                                           "time": tm, "camera": cam})
        else:
            world = params.get("world") or "royal palace"
            from promptspec import ENV_WORLDS, env_place_profile, env_places
            # scope (interior/exterior/both) filters the place families;
            # `places` is the explicit multi-select from the wizard. Every
            # place carries its own camera/framing profile, so one image per
            # selected place is genuinely different from the next.
            scope = str(params.get("scope") or "both")
            kinds = env_places(world)
            places = []
            if scope in ("interior", "both"):
                places += [{"place": p, "place_kind": "interior"}
                           for p in kinds.get("interiors") or []]
            if scope in ("exterior", "both"):
                places += [{"place": p, "place_kind": "exterior"}
                           for p in kinds.get("exteriors") or []]
            if not places:
                places = [{"place": p, "place_kind": ""} for p in
                          ENV_WORLDS.get(world, {}).get("locs", [])]
            sel = params.get("places")
            if sel:
                wanted = {str(x) for x in sel}
                places = [p for p in places if p["place"] in wanted] or places
            times = params.get("times") or ["night"]
            for pl in places:
                prof = env_place_profile(world, pl["place"])
                for tm in times:
                    combos.append({"location": pl["place"],
                                   "place_kind": pl["place_kind"],
                                   "place_desc": str(prof.get("d") or ""),
                                   "place_camera": str(prof.get("c") or ""),
                                   "place_framing": str(prof.get("f") or ""),
                                   "time": tm})
    elif mode == "weapon":
        combos = [{}]
    elif mode == "photo":
        combos = [{}]
    # dependency validation: drop combos that don't make sense together
    out = []
    for c in combos:
        if _combo_valid(mode, params, c):
            out.append(c)
    return out


def _combo_valid(mode: str, params: dict, combo: dict) -> bool:
    """Skip invalid combos instead of prompting errors (§10)."""
    if mode == "character":
        sh = combo.get("shot", "portrait")
        if sh in ("close-up", "portrait", "profile"):
            pose = params.get("pose", "")
            if pose in ("running", "combat-ready") and not params.get("allow_pose_face"):
                return False
    if mode == "power":
        # structured power combos only carry "intensity"; the shot comes
        # from the combo (legacy "camera") or params["shots"] (structured)
        sh = combo.get("shot") or combo.get("camera") or ""
        if not sh:
            sh = (params.get("shots") or [""])[0]
        if sh in ("close-up", "portrait", "profile", "extreme close-up"):
            manif = params.get("manifestation", "hands")
            if manif in ("wave", "explosion", "ground eruption",
                         "sky event"):
                return False   # scale doesn't fit a face shot
    return True


def cycle_value(cycle_list: list, idx: int, default: str) -> str:
    return cycle_list[idx % len(cycle_list)] if cycle_list else default


def _level_intensity(level: int) -> str:
    """Power-level ladder -> the intensity word the model reads."""
    if level <= 3:
        return "faint" if level == 1 else "steady"
    if level <= 7:
        return "violent"
    return "cataclysmic"


def _level_scale(level: int) -> str:
    if level <= 3:
        return "contained"
    if level <= 5:
        return "local"
    if level <= 8:
        return "devastating"
    return "apocalyptic"


def _level_destruction(level: int) -> str:
    if level <= 3:
        return "none" if level == 1 else "surface"
    if level <= 6:
        return "surface"
    if level <= 8:
        return "structural"
    return "cataclysmic"


def distribute(model_rows: list[dict], combos: list[dict],
               seed: int | None = None, refs: list | None = None,
               ref_strength: float = 0.8, anchor: bool = False) -> list[dict]:
    """Balanced scheduler: for each model row, strided combo selection.
    Returns flat task list: [{model, strength..., combo, idx}].
    Same params + seed -> same plan (deterministic).

    A row may carry an `outfits` filter (wardrobe matrix, Tab 2): only combos
    whose outfit is in the list are eligible for that row, so each clothing
    can run on its own model (e.g. zimage_unc for explicit looks). A filter
    with no matches falls back to the full pool — a stale name must never
    silently drop a whole row's images.

    Seed rule (identity consistency): the FIRST render of every combo uses
    the same base seed, so a character's plates and wardrobe composites are
    the SAME person (the prompt carries the variation, not the seed). Only a
    REPEATED render of the same combo (count multiplier) shifts by the repeat
    index — the 2x duplicates are genuinely different takes.

    Every task gets its OWN copy of the combo: the axes engine MUTATES the
    combo (outfit/weapon/handling/level/plate) while applying coverage and
    cycles. With shared dicts, one armed task permanently armed every later
    task that reused the same combo — measured: 50% weapon coverage produced
    33/35 armed images because `combo["weapon"]` leaked between tasks."""
    tasks: list[dict] = []
    seed_rep: dict[tuple[str, int], int] = {}
    for row in model_rows:
        filt = {str(x).strip() for x in (row.get("outfits") or [])
                if str(x).strip()}
        pool = combos
        if filt:
            matched = [c for c in combos
                       if str(c.get("outfit") or "").strip() in filt]
            if matched:
                pool = matched
        L = max(1, len(pool))
        n = int(row.get("count", 0))
        for k in range(n):
            cidx = round(k * L / n) % L if n > 0 else 0
            rep_key = (str(row["model"]), cidx)
            rep = seed_rep.get(rep_key, 0)
            seed_rep[rep_key] = rep + 1
            tasks.append({"model": row["model"],
                          "combo": dict(pool[cidx] or {}),
                          "k": k,
                          "steps": row.get("steps"),
                          "cfg": row.get("cfg"),
                          "res": row.get("res", "1024x1024"),
                          "hires": bool(row.get("hires", False)),
                          "seed": (seed + rep) if seed is not None else None,
                          "loras": row.get("loras"),
                          "refs": refs,
                          "ref_strength": ref_strength,
                          "anchor": anchor,
                          # optional per-row worker —
                          # run_job routes this task's render there
                          "worker": row.get("worker") or None})
    return tasks


def build_plan(params: dict) -> dict:
    """Validate + expand params into {combos, model_rows, total, warnings}."""
    mode = params.get("mode", "character")
    combos = build_combos(mode, params)
    rows = params.get("models") or [{"model": params.get("model", "realvis"),
                                     "count": params.get("count", 0)}]
    warnings: list[str] = []
    total = 0
    for r in rows:
        try:
            total += int(r.get("count", 0))
        except (TypeError, ValueError):
            warnings.append(f"invalid count for model '{r.get('model', '?')}'")
    if total > MAX_BATCH:
        warnings.append(f"total {total} exceeds the safety cap of "
                        f"{MAX_BATCH} images")
    if total == 0:
        warnings.append("no images requested — set counts in the Model Mix")
    for r in rows:
        m = get_model(r["model"])
        if m is None:
            warnings.append(f"unknown model '{r['model']}'")
            continue
        if not m["capabilities"].get("loras", False) and r.get("loras"):
            warnings.append(f"{m['name']} does not support LoRAs")
        if r.get("hires") and not m["capabilities"].get("hires", False):
            warnings.append(f"{m['name']} does not support the hires fix — "
                            f"uncheck hires for this model")
        for l in r.get("loras") or []:
            from models import GEN_LORAS
            if l.get("file") not in GEN_LORAS:
                warnings.append(f"unknown LoRA '{l.get('file')}'")
    if not combos:
        warnings.append("no valid combinations for these params")
    if mode == "character" and params.get("matrix"):
        # every matrix look must be covered by a model row, or it silently
        # never renders (the per-row `outfits` filter routes combos)
        covered: set[str] = set()
        for r in rows:
            covered |= {str(x).strip() for x in (r.get("outfits") or [])
                        if str(x).strip()}
        seen_o: set[str] = set()
        for r in params.get("matrix") or []:
            o = str((r or {}).get("outfit") or "").strip() \
                if isinstance(r, dict) else ""
            if o and o not in covered and o not in seen_o:
                seen_o.add(o)
                warnings.append(f"matrix outfit '{o}' has no model row — "
                                f"it will not be generated")
        wcfg = params.get("weapon_configs") or {}
        seen_w: set[str] = set()
        for c in combos:
            wid = str(c.get("weapon") or "")
            if wid and wid not in wcfg and wid not in seen_w:
                seen_w.add(wid)
                warnings.append(f"matrix weapon '{wid}' has no config — "
                                f"using prompt defaults")
    refs = params.get("refs") or []
    if len(refs) > 5:
        warnings.append("max 5 reference images")
    if refs:
        if not params.get("dataset_type") or not params.get("dataset_name"):
            warnings.append("reference images need a dataset")
        else:
            d = DATASETS_DIR / params.get("dataset_type") / \
                params.get("dataset_name")
            for item in refs:
                # refs may be plain paths (legacy) or {file, type} dicts (§7)
                rel = item if isinstance(item, str) else \
                    (item.get("file", "") if isinstance(item, dict) else "")
                if (not rel or ".." in rel or "\\" in rel
                        or Path(rel).is_absolute()
                        or not (d / rel).exists()):
                    warnings.append(f"reference image missing: {rel}")
    try:
        ref_strength = float(params.get("ref_strength", 0.8))
    except (TypeError, ValueError):
        ref_strength = 0.8
        warnings.append("invalid ref strength — using default 0.8")
    if not (0.1 <= ref_strength <= 1.5):
        warnings.append("ref strength must be 0.1-1.5")
    return {"mode": mode, "combos": combos, "rows": rows, "total": total,
            "warnings": warnings, "combo_count": len(combos)}


# -------------------------------------------------------------- execution
def _next_still_index(dtype: str, name: str, prefix: str) -> int:
    d = DATASETS_DIR / dtype / name / "stills"
    best = 0
    if d.is_dir():
        for f in d.glob(f"{prefix}_*.png"):
            try:
                best = max(best, int(f.stem.split("_")[-1]) + 1)
            except (ValueError, IndexError):
                continue
    return best


def apply_spec_axes(spec, mode: str, params: dict, combo: dict,
                    idx: int) -> None:
    """Apply the wizard variation axes to a PromptSpec (shared by the
    batch engine and the prompt preview so they can never drift)."""
    from promptspec import (ENV_WORLDS, WARDROBE_PRESETS,
                            ENV_LOCATION_DESC)
    if mode == "character":
        spec.blocks["performance"]["expression"] = combo.get(
            "emotion", "stoic")
        spec.blocks["shot"]["preset"] = combo.get("shot", "portrait")
        if params.get("identity_plates"):
            # Identity plates are deliberately CONSTANT: same neutral studio,
            # same soft light, same stance and gaze — only the framing
            # changes. Nothing cycles and no weapon is ever attached, so the
            # three plates share one face/body read.
            spec.blocks["performance"]["pose"] = "standing"
            spec.blocks["performance"]["gaze"] = "camera"
            spec.blocks["scene"]["scene"] = "neutral studio (no environment)"
            spec.blocks["scene"]["location"] = ""
            spec.blocks["lighting"]["setup"] = "soft window light"
            return
        # cycle axes: pose / scene / lighting / gaze
        pose = cycle_value(params.get("poses"), idx, "")
        if pose:
            spec.blocks["performance"]["pose"] = pose
        gaze = cycle_value(params.get("gazes"), idx, "")
        if gaze:
            spec.blocks["performance"]["gaze"] = gaze
        sc = cycle_value(params.get("scenes"), idx, "")
        if sc:
            spec.blocks["scene"]["scene"] = sc
            spec.blocks["scene"]["location"] = cycle_value(
                params.get("scene_locations"), idx, "")
        li = cycle_value(params.get("lightings"), idx, "")
        if li:
            spec.blocks["lighting"]["setup"] = li
        # wardrobe cycle (§6): precomposed sets — never redefines identity
        ward = cycle_value(params.get("wardrobes"), idx, "")
        if ward:
            from promptspec import WARDROBE_PRESETS
            if ward in WARDROBE_PRESETS:
                spec.blocks["wardrobe"] = dict(WARDROBE_PRESETS[ward])
        # outfit variants (replaces the old pose axis): a named outfit is
        # either a wardrobe preset or a free-text descriptor. Matrix combos
        # already carry their outfit (Tab 2) — the cycle must not overwrite.
        outfit = str(combo.get("outfit") or "") or \
            cycle_value(params.get("outfits"), idx, "")
        if outfit:
            from promptspec import WARDROBE_PRESETS
            if outfit in WARDROBE_PRESETS:
                spec.blocks["wardrobe"] = dict(WARDROBE_PRESETS[outfit])
            else:
                spec.blocks["wardrobe"]["silhouette"] = outfit
            combo["outfit"] = outfit
        # weapons: "some of the images with a weapon" — a coverage percent
        # decides how many, the weapon list cycles, the handling cycles.
        # The config travels as TEXT (identity is never redefined). Matrix
        # combos carry their weapon explicitly (armed when present; unarmed
        # looks stay unarmed even if the coverage axis says otherwise).
        weapons = params.get("weapons") or []
        combo_w = str(combo.get("weapon") or "")
        if weapons or combo_w:
            try:
                cov = int(params.get("weapon_coverage") or 100)
            except (TypeError, ValueError):
                cov = 100
            cov = max(0, min(100, cov))
            armed = bool(combo_w) or cov >= 100 or (
                cov > 0 and idx % max(1, int(round(100.0 / cov))) == 0)
            if armed:
                wid = combo_w or cycle_value(weapons, idx, "")
                wcfg = dict((params.get("weapon_configs") or {}).get(wid) or {})
                wcfg.setdefault("type", wid or "custom")
                handling = cycle_value(params.get("weapon_handling"), idx, "")
                if handling:
                    wcfg["handling"] = handling
                spec.blocks["weapon"] = wcfg
                combo["weapon"] = wid
                if handling:
                    combo["handling"] = handling
    elif mode == "power":
        if params.get("level_ladder"):
            # THE LADDER DRIVES EVERYTHING: one image per level, and every
            # level changes manifestation + framing + subject action + scale
            # + destruction together (the old version changed only intensity
            # words, which is why every image looked the same).
            try:
                n_lv = max(1, int(params.get("level_count") or 10))
            except (TypeError, ValueError):
                n_lv = 10
            lvl = 1 + (idx % n_lv)
            combo["level"] = lvl
            from promptspec import POWER_LADDER
            lad = POWER_LADDER.get(min(10, max(1, lvl))) or {}
            for key in ("manifestation", "behavior", "intensity", "scale",
                        "destruction", "aftermath", "env_reaction"):
                if lad.get(key):
                    spec.blocks["vfx"][key] = lad[key]
            if lad.get("shot"):
                spec.blocks["shot"]["preset"] = lad["shot"]
            spec.blocks["figure"] = {"text": lad.get("figure", ""),
                                     "notes": lad.get("notes", "")}
            # High levels NEED a place to destroy: a cataclysm in a neutral
            # studio reads as a studio with a light. When the base scene is
            # the neutral studio, the ladder supplies an environment.
            hint = str(lad.get("scene_hint") or "")
            base_scene = str(spec.blocks.get("scene", {}).get("scene") or "")
            if hint and (not base_scene or
                         base_scene.startswith("neutral studio")):
                spec.blocks["scene"]["scene"] = ""
                spec.blocks["scene"]["custom_desc"] = hint
        else:
            # manual axes (legacy): intensity multiply + cycles
            spec.blocks["vfx"]["intensity"] = combo.get("intensity", "steady")
            manif = cycle_value(params.get("manifestations"), idx, "")
            if manif:
                spec.blocks["vfx"]["manifestation"] = manif
            beh = cycle_value(params.get("behaviors"), idx, "")
            if beh:
                spec.blocks["vfx"]["behavior"] = beh
            reac = cycle_value(params.get("env_reactions"), idx, "")
            if reac:
                spec.blocks["vfx"]["env_reaction"] = reac
            scale = cycle_value(params.get("scales"), idx, "")
            if scale:
                spec.blocks["vfx"]["scale"] = scale
            dmg = cycle_value(params.get("destructions"), idx, "")
            if dmg:
                spec.blocks["vfx"]["destruction"] = dmg
            after = cycle_value(params.get("aftermaths"), idx, "")
            if after:
                spec.blocks["vfx"]["aftermath"] = after
        sc = cycle_value(params.get("scenes"), idx, "")
        if sc:
            spec.blocks["scene"]["scene"] = sc
    elif mode == "weapon":
        # clean design stills: handling cycles (product view, held, aimed)
        handling = cycle_value(params.get("weapon_handling"), idx, "")
        if handling:
            spec.blocks["weapon"]["handling"] = handling
            combo["handling"] = handling
    else:  # environment
        from promptspec import ENV_LOCATION_DESC, env_place_profile
        # the batch combo carries the place; the preview derives it from
        # params (location, or the first selected place)
        loc_txt = combo.get("location") or str(params.get("location") or "")
        if not loc_txt:
            pl = params.get("places") or []
            loc_txt = str(pl[0]) if pl else ""
        spec.blocks["scene"]["location"] = loc_txt
        spec.blocks["scene"]["scene"] = params.get("world", "royal palace")
        spec.blocks["scene"]["time"] = combo.get("time", "night")
        prof = env_place_profile(params.get("world", "royal palace"), loc_txt) \
            if loc_txt else {}
        # place kind: the combo carries it; else the profile's own kind
        kind = combo.get("place_kind", "")
        if not kind and prof.get("k"):
            kind = "interior" if prof["k"] == "i" else "exterior"
        if not kind:
            scope = str(params.get("scope") or "")
            if scope in ("interior", "exterior"):
                kind = scope
            elif loc_txt:
                from promptspec import env_places
                kd = env_places(params.get("world", "royal palace"))
                if loc_txt in (kd.get("interiors") or []):
                    kind = "interior"
                elif loc_txt in (kd.get("exteriors") or []):
                    kind = "exterior"
        spec.blocks["scene"]["place_kind"] = kind
        spec.blocks["scene"]["place"] = loc_txt
        # per-place prompt profile (camera / framing / description) — the
        # whole point of the place catalog: twelve places, twelve different
        # pictures instead of the same establishing shot with new seeds
        desc = combo.get("place_desc") or str(prof.get("d") or "")
        camera = combo.get("place_camera") or str(prof.get("c") or "")
        framing = combo.get("place_framing") or str(prof.get("f") or "")
        if desc:
            spec.blocks["scene"]["place_desc"] = desc
        if camera:
            spec.blocks["scene"]["camera"] = camera
        if framing:
            spec.blocks["scene"]["framing"] = framing
        spec.blocks["scene"]["architecture"] = \
            ENV_WORLDS.get(params.get("world", "royal palace"),
                           {}).get("architecture", "")
        # legacy location text only when it ADDS something (a mapped
        # description) and the place profile did not already cover it
        legacy_desc = ENV_LOCATION_DESC.get(loc_txt, "")
        if loc_txt and legacy_desc and legacy_desc != loc_txt and not desc:
            spec.blocks["scene"]["location_desc"] = legacy_desc
        atm = cycle_value(params.get("atmospheres"), idx, "")
        if atm:
            spec.blocks["scene"]["atmosphere"] = atm
        wea = cycle_value(params.get("weathers"), idx, "")
        if wea:
            spec.blocks["scene"]["weather"] = wea
        fg = cycle_value(params.get("foregrounds"), idx, "")
        if fg:
            spec.blocks["scene"]["foreground"] = fg


def _run_one(c: ComfyClient, task: dict, params: dict) -> dict | None:
    """Render one image. Returns {file, prompt, neg, dur} or None on fail."""
    from dataset_gen import (character_prompt, env_prompt, power_prompt,
                             EMOTION_LOOKUP)
    mode = params.get("mode", "character")
    combo = task["combo"]
    caption = ""   # legacy branches build no caption; structured/photo set it
    # All wizard subject fields travel flat in params (gender, age, outfit,
    # color, type...) — they are the prompt builder's base. Only structural
    # job keys are excluded. (Bug fixed 2026-08-12: this was
    # `params.get("base", {})`, silently dropping every subject field.)
    _STRUCT = {"mode", "models", "dataset_type", "dataset_name",
               "custom_prompt", "custom_negative", "loras", "stack",
               "model", "count", "prompt", "continue", "prompt_mode"}
    base = {k: v for k, v in params.items() if k not in _STRUCT}
    m = get_model(task["model"])
    if m is None:
        raise ValueError(f"unknown model {task['model']}")
    res = task.get("res") or m["default_size"]
    w, h = map(int, res.lower().split("x"))
    steps = int(task.get("steps") or m["steps"])
    cfg = float(task.get("cfg") if task.get("cfg") is not None else m["cfg"])
    if task.get("seed") is not None:
        seed = int(task["seed"])          # coherent base + image index
    else:
        seed = 100000 + int(time.time()) % 900000 + task["k"]

    if mode == "photo":
        base_prompt = params.get("prompt", "")
    elif is_legacy(mode, params):
        # ---- legacy wizard shapes (unchanged behavior) ----
        if mode == "character":
            p = dict(base)
            # combo["shot"] already carries the mapped angle value (build_combos)
            p.update({"angle": combo.get("shot") or combo.get("angle")
                      or "front-facing",
                      "emotion": combo.get("emotion", "stoic")})
            base_prompt = character_prompt(p)
        elif mode == "power":
            idx = task["k"]
            from dataset_gen import (AURA_TEXT, POWER_AURAS, POWER_CAMERA,
                                     POWER_ENVS, POWER_SCENES)
            p = dict(base)
            p.setdefault("power", params.get("name") or "lightning")
            subjects = params.get("subjects") or []
            auras = params.get("auras") or []
            scenes = params.get("scenes") or []
            form = combo.get("form", "hands")
            subject = cycle_value(subjects, idx,
                                  "figure" if form in ("sky", "strike")
                                  else "hands")
            aura = (AURA_TEXT.get(cycle_value(auras, idx, ""), POWER_AURAS[0])
                    if auras else POWER_AURAS[0])
            scene = cycle_value(scenes, idx, POWER_SCENES[0])
            p.update({"form": form, "env": combo.get("env", POWER_ENVS[0]),
                      "subject": subject, "aura": aura,
                      "camera": combo.get("camera", POWER_CAMERA[0]),
                      "scene": scene})
            base_prompt = power_prompt(p)
        else:  # environment legacy
            idx = task["k"]
            from dataset_gen import (ENV_CAMERA, ENV_FOREGROUND,
                                     ENV_LOCATIONS, ENV_WEATHER)
            p = dict(base)
            moods = params.get("moods") or []
            weathers = params.get("weathers") or []
            foregrounds = params.get("foregrounds") or []
            p.update({"subtype": combo.get("subtype", "city"),
                      "time": combo.get("time", "night"),
                      "mood": cycle_value(moods, idx,
                                          base.get("mood", "epic")),
                      "weather": cycle_value(weathers, idx, ENV_WEATHER[0]),
                      "foreground": cycle_value(foregrounds, idx,
                                                ENV_FOREGROUND[1]),
                      "camera": combo.get("camera", ENV_CAMERA[0]),
                      "location": combo.get("location", "")})
            base_prompt = env_prompt(p)
    else:
        # ---- PromptSpec structured path ----
        from promptspec import spec_from_params
        idx = task["k"]
        spec = spec_from_params(mode, params)
        apply_spec_axes(spec, mode, params, combo, idx)
        base_prompt = spec.to_prompt()
        caption = spec.to_caption()

    # prompt modes: structured -> blocks only; add ->
    # blocks + custom (spec.to_prompt() already appends it on the
    # structured path); override -> custom replaces everything. The legacy
    # builders have no spec, so "add" is applied manually here. The wizard's
    # prompt mode arrives as `prompt_mode` (dataset_gen overwrites `mode`
    # with the generation mode) — fall back to `mode` for old callers.
    pmode = params.get("prompt_mode") or params.get("mode") or "structured"
    custom_p = params.get("custom_prompt") or ""
    user_prompt = None
    if pmode == "override" and custom_p:
        user_prompt = custom_p
    elif pmode == "add" and custom_p and is_legacy(mode, params) \
            and mode != "photo":
        base_prompt = (base_prompt + ", " + custom_p).strip(", ")
    fprompt, fneg = adapt_prompt(task["model"], base_prompt, "training",
                                 user_prompt=user_prompt,
                                 user_negative=params.get("custom_negative"))
    if fneg:
        # Realism enforcement (user requirement: every generation must read
        # as a photograph). Only models that actually consume negatives get
        # this — z-image has an empty negative and no override.
        from promptspec import ANTI_ILLUSTRATION
        extra = list(ANTI_ILLUSTRATION)
        if mode == "environment":
            # references are BACKDROPS: keep them empty and unobstructed —
            # an arena with a throne in the middle is useless as a location
            # reference
            extra += ["people", "crowd", "person", "figure",
                      "throne", "statue", "monument", "sculpture",
                      "structure in the center", "clutter"]
        if mode == "power":
            extra += ["raised hands", "palms facing forward", "palms up",
                      "symmetrical raised arms", "close-up of hands only",
                      "duplicate hands", "extra fingers", "two pairs of hands",
                      "stock photo pose"]
        low = {x.strip().lower() for x in fneg.split(",")}
        add = [t for t in extra if t.lower() not in low]
        if add:
            fneg = (fneg + ", " + ", ".join(add)).strip(", ")
    loras = task.get("loras") or params.get("loras") or []
    if loras and fneg == m["negative"]:
        from models import LORA_TRIGGER_EXTRA
        fneg = fneg + ", " + LORA_TRIGGER_EXTRA
    if mode == "photo":
        caption = params.get("prompt", "")
    refs_comfy = task.get("refs_comfy") or []
    if refs_comfy:
        from models import build_ref_workflow
        wf = build_ref_workflow(
            task["model"], fprompt, fneg, seed, w, h, steps, cfg, loras,
            refs_comfy, float(task.get("ref_strength", 0.8)),
            bool(task.get("anchor", False)),
            shot_id=f"ds_{int(time.time())}_{uuid.uuid4().hex[:6]}")
    else:
        wf = build_workflow(task["model"], fprompt, fneg, seed, w, h, steps,
                            cfg, loras, hires=False,
                            shot_id=f"ds_{int(time.time())}_{uuid.uuid4().hex[:6]}")
    t0 = time.time()
    pid = c.queue_prompt(wf)
    res = c.wait_for(pid, timeout_s=2400)
    if res is None:
        raise TimeoutError("render timed out")
    st = res.get("status", {})
    if st.get("status_str") == "error":
        for msg in st.get("messages", []):
            if msg[0] == "execution_error":
                raise RuntimeError(
                    (msg[1].get("exception_message") or "")[:300])
        raise RuntimeError("workflow failed")
    dur = time.time() - t0
    record_performance(task["model"], f"{w}x{h}", steps, False, dur)
    for o in res.get("outputs", {}).values():
        for v in o.get("images", []):
            fname = v.get("filename")
            if not fname:
                continue
            return {"subfolder": v.get("subfolder", ""),
                    "filename": fname, "prompt": fprompt,
                    "negative": fneg, "seed": seed, "dur": dur,
                    "caption": caption}
    raise RuntimeError("no image output")


def _hires_pass(c: ComfyClient, m, png: Path, prompt: str, negative: str,
                cfg: float, seed: int, w: int, h: int) -> None:
    """Per-model hires refine: RealESRGAN upscale +
    low-denoise re-render, overwrites png in place."""
    from models import build_hires_workflow
    fname = c.upload_image(png)
    wf = build_hires_workflow(m, prompt, negative, seed, cfg, w, h, fname,
                              shot_id=f"hr_{int(time.time())}_{uuid.uuid4().hex[:6]}")
    pid = c.queue_prompt(wf)
    res = c.wait_for(pid, timeout_s=1800)
    if res is None:
        raise RuntimeError("hires refine timed out")
    st = res.get("status", {})
    if st.get("status_str") == "error":
        raise RuntimeError("hires refine failed")
    for o in res.get("outputs", {}).values():
        for v in o.get("images", []):
            c.download(v.get("subfolder", ""), v["filename"], png)
            return
    raise RuntimeError("hires refine produced no output")


def run_job(rid: int) -> None:
    run = db.get_run(rid)
    if not run or run["status"] != db.RUN_QUEUED:
        return
    params = json.loads(run["params"] or "{}")
    dtype, name = run["dataset_type"], run["dataset_name"]
    plan = build_plan(params)
    if plan["warnings"]:
        db.set_run_status(rid, db.RUN_FAILED, "; ".join(plan["warnings"]))
        return
    # continuation: tasks are deterministic and saved in order, so the
    # already-persisted leading images are skipped instead of re-rendered
    # (retry() re-renders only the missing tail, never duplicates).
    skip = min(db.count_images_for_run(rid), plan["total"])
    db.update_run(rid, total=plan["total"], done=skip, failed=0)
    db.set_run_status(rid, db.RUN_ACTIVE)
    c = ComfyClient(8188)
    # registry models may live on their own local instance (Qwen-Image-2.1
    # -> comfy_port 8190); the 8188 farm cannot load them
    local_clients: dict[int, ComfyClient] = {8188: c}

    def _local_for(mid: str) -> ComfyClient:
        port = int((get_model(mid) or {}).get("comfy_port") or 8188)
        return local_clients.setdefault(port, ComfyClient(port))

    # per-image worker assignment: key = 0-based task
    # index, '*' = run default. 'local' keeps the run's own client.
    worker_map: dict = {}
    try:
        import task_queue
        worker_map = task_queue.get_run_worker_map(rid) or {}
    except Exception:
        pass
    t0 = time.time()
    try:
        # wait for comfy
        for _ in range(36):
            if rid in _cancel_ids:
                db.set_run_status(rid, db.RUN_CANCELED)
                return
            try:
                c.get("/system_stats")
                break
            except Exception:
                time.sleep(5)
        combos = plan["combos"]
        rows = plan["rows"]
        seed = params.get("seed")
        if seed is not None:
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                log.warning(f"run {rid}: invalid seed {seed!r} — ignored")
                seed = None
        tasks = distribute(rows, combos, seed,
                           params.get("refs"),
                           params.get("ref_strength", 0.8),
                           bool(params.get("anchor", False)))
        # upload reference images once per run for the local lane (they live
        # in the dataset refs/ folder; ComfyUI needs them in its input dir).
        # Items may be plain paths (legacy) or {file, type} objects (§7).
        refs_comfy: dict[str, str] = {}
        ref_types: dict[str, str] = {}
        for item in params.get("refs") or []:
            rel = item if isinstance(item, str) else item.get("file", "")
            rtype = "" if isinstance(item, str) else item.get("type", "")
            rp = DATASETS_DIR / dtype / name / rel
            if rp.exists() and rp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                refs_comfy[rel] = c.upload_image(rp)
                if rtype:
                    ref_types[rel] = rtype
        ref_order = [r if isinstance(r, str) else r.get("file", "")
                     for r in (params.get("refs") or [])]

        # ------------------------------------------------------ lanes
        # Every machine (local + each worker PC) gets its OWN lane with its
        # own sequential queue; lanes run CONCURRENTLY. Inside a lane the
        # order is preserved (task index ascending), so a run mixing local
        # and worker images renders on both machines at the same time.
        def _task_worker(i: int, task: dict) -> str:
            w = (task.get("worker") or worker_map.get(str(i))
                 or worker_map.get("*") or params.get("worker")
                 or "local")
            if w and w != "local":
                try:
                    import workers as _workers_mod
                    _workers_mod.client_for(w)      # reachability probe
                except Exception as e:
                    log.warning(f"run {rid} task {i}: worker {w} unavailable"
                                f" ({e}) — falling back to local")
                    return "local"
            return w or "local"

        lanes: dict[str, list[tuple[int, dict]]] = {}
        for i, task in enumerate(tasks):
            if i < skip:
                continue
            lanes.setdefault(_task_worker(i, task), []).append((i, task))

        done_total = 0
        failed_total = 0
        prog_lock = threading.Lock()
        cancelled = {"hit": False}

        def _record(ok: bool) -> None:
            nonlocal done_total, failed_total
            with prog_lock:
                if ok:
                    done_total += 1
                else:
                    failed_total += 1
                db.update_run(rid, done=skip + done_total, failed=failed_total)

        def _refs_for(lane: str, tc, task: dict) -> None:
            if not refs_comfy:
                return
            if lane == "local":
                task["ref_types"] = {r: ref_types[r] for r in ref_order
                                     if r in ref_types}
            if lane == "local" and tc is c:
                task["refs_comfy"] = [refs_comfy[r] for r in ref_order
                                      if r in refs_comfy]
                return
            # reference files must exist in THAT instance's input dir too (a
            # worker PC, or a dedicated local instance such as the Qwen farm);
            # uploaded once per instance per run (names mirror local).
            key = f"{lane}:{getattr(tc, 'port', '')}"
            if key not in _refs_for.cache:
                uploaded = []
                for r in ref_order:
                    if r not in refs_comfy:
                        continue
                    srcpath = DATASETS_DIR / dtype / name / r
                    if Path(srcpath).exists():
                        uploaded.append(tc.upload_image(srcpath))
                _refs_for.cache[key] = uploaded
            task["refs_comfy"] = _refs_for.cache[key]

        _refs_for.cache = {}  # type: ignore[attr-defined]

        def _process(i: int, task: dict, tclient, lane_key: str) -> None:
            """One task end-to-end (refs, render, save, provenance)."""
            if lane_key == "local":
                tclient = _local_for(task["model"])
            _refs_for(lane_key, tclient, task)
            try:
                out = _run_one(tclient, task, params)
            except Exception as e:
                log.warning(f"run {rid} [{lane_key}] image failed: {e}")
                out = None
            if out is not None:
                fname = _save_output(dtype, name, task, out, client=tclient)
                if task.get("hires"):
                    try:
                        png = DATASETS_DIR / dtype / name / fname
                        m = get_model(task["model"])
                        _hires_pass(tclient, task["model"], png,
                                    out["prompt"], out["negative"],
                                    float(task.get("cfg")
                                          if task.get("cfg") is not None
                                          else m["cfg"]),
                                    out["seed"],
                                    *map(int, (task.get("res") or
                                               m["default_size"])
                                         .lower().split("x")))
                    except Exception as e:
                        log.warning(f"run {rid} hires failed: {e}")
                db.add_image(
                    rid, dtype, name, fname, task["model"],
                    out["prompt"], out["negative"], out["seed"],
                    int(task.get("steps") or
                        get_model(task["model"])["steps"]),
                    float(task.get("cfg") if task.get("cfg") is not None
                          else get_model(task["model"])["cfg"]),
                    get_model(task["model"]).get("sampler", ""),
                    get_model(task["model"]).get("scheduler", ""),
                    task.get("res") or
                    get_model(task["model"])["default_size"],
                    params.get("loras"),
                    {**dict(task["combo"]),
                     **({"refs": params.get("refs")}
                        if params.get("refs") else {}),
                     **({"ref_types": task.get("ref_types")}
                        if task.get("ref_types") else {}),
                     **({"hires": True} if task.get("hires") else {}),
                     **({"caption": out.get("caption", "")}
                        if out.get("caption") else {})},
                    duration=out["dur"])
                _record(True)
            else:
                _record(False)

        def _lane_worker(lane: str, items: list) -> None:
            """One lane = one machine.

            The machine's lane lock is held for the WHOLE lane segment: two
            runs assigned to the same machine never interleave, and a queued
            run starts on that machine the moment the previous one releases
            it. Lanes on different machines run concurrently.
            """
            if lane == "mixed":
                # continuation runs: per-task dispatch, lock per machine
                for i, task in items:
                    if rid in _cancel_ids:
                        cancelled["hit"] = True
                        return
                    w = _task_worker(i, task)
                    if w == "local":
                        tclient = c
                    else:
                        try:
                            import workers as _workers_mod
                            tclient = _workers_mod.client_for(w)
                        except Exception as e:
                            log.warning(f"run {rid} task {i}: worker {w} "
                                        f"unavailable ({e}) — local")
                            w, tclient = "local", c
                    lk = _lane_lock(w)
                    while not lk.acquire(timeout=1.0):
                        if rid in _cancel_ids:
                            cancelled["hit"] = True
                            return
                    try:
                        _process(i, task, tclient, w)
                    finally:
                        lk.release()
                return
            if lane == "local":
                tc = c
            else:
                try:
                    import workers as _workers_mod
                    tc = _workers_mod.client_for(lane)
                except Exception as e:
                    log.warning(f"run {rid}: lane {lane} unavailable ({e})")
                    return
            lk = _lane_lock(lane)
            waited = False
            while not lk.acquire(timeout=1.0):
                if rid in _cancel_ids:
                    cancelled["hit"] = True
                    return
                if not waited:
                    waited = True
                    log.info(f"run {rid}: lane {lane} queued behind the "
                             f"machine's current run")
            try:
                for i, task in items:
                    if rid in _cancel_ids:
                        cancelled["hit"] = True
                        return
                    _process(i, task, tc, lane)
            finally:
                lk.release()

        # continuation runs keep the proven single-thread dispatch order
        # (the saved prefix is only meaningful in task order)
        if skip > 0 and len(lanes) > 1:
            merged = sorted((x for items in lanes.values() for x in items),
                            key=lambda x: x[0])
            lanes = {"mixed": merged}
        if lanes:
            log.info(f"run {rid}: {len(lanes)} lane(s) — "
                     + ", ".join(f"{k}:{len(v)}" for k, v in lanes.items()))
            threads = [threading.Thread(target=_lane_worker, args=(lane, items),
                                        name=f"lane-{lane}", daemon=True)
                       for lane, items in lanes.items()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        if cancelled["hit"]:
            db.set_run_status(rid, db.RUN_CANCELED)
            log.info(f"run {rid}: canceled "
                     f"({skip + done_total} done, {failed_total} failed)")
            return
        done_all = skip + done_total
        if failed_total == 0:
            db.set_run_status(rid, db.RUN_DONE)
        elif done_all == 0:
            db.set_run_status(rid, db.RUN_FAILED,
                              f"{failed_total} of {plan['total']} images failed")
        else:
            db.set_run_status(rid, db.RUN_PARTIAL,
                              f"{failed_total} of {plan['total']} images failed")
        log.info(f"run {rid}: finished — {done_all} done, {failed_total} "
                 f"failed in {time.time() - t0:.0f}s")
    except Exception as e:
        log.exception(f"run {rid} crashed")
        db.set_run_status(rid, db.RUN_FAILED, str(e)[:300])
    finally:
        _cancel_ids.discard(rid)


def _save_output(dtype: str, name: str, task: dict, out: dict,
                 client: ComfyClient | None = None) -> str:
    d = DATASETS_DIR / dtype / name / "stills"
    d.mkdir(parents=True, exist_ok=True)
    prefix = {"face": "v", "power": "p", "environment": "e",
              "weapon": "w", "outfit": "o"}.get(dtype, "g")
    # reserve the name under the lock: the placeholder makes the index
    # visible to other lanes before the (slow) download finishes
    with _save_lock:
        idx = _next_still_index(dtype, name, prefix)
        dst = d / f"{prefix}_{idx:03d}.png"
        try:
            dst.touch()
        except OSError:
            pass
    c = client or ComfyClient(8188)
    c.download(out["subfolder"], out["filename"], dst)
    # also mirror into meta.json (backward compat with curation readers)
    meta_path = DATASETS_DIR / dtype / name / "meta.json"
    with _save_lock:
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta[dst.name] = {"model": task["model"], "prompt": out["prompt"],
                          "negative": out["negative"], "seed": out["seed"],
                          "source": "generated",
                          "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "variations": task["combo"]}
        meta_path.write_text(json.dumps(meta, indent=1, ensure_ascii=False),
                             encoding="utf-8")
    return dst.relative_to(DATASETS_DIR / dtype / name).as_posix()


# -------------------------------------------------------------- worker
def _worker_loop() -> None:
    """Dispatcher: starts each queued RUN in its own thread.

    Runs are concurrent (up to _MAX_ACTIVE_RUNS); the per-machine lane locks
    inside run_job serialize what actually touches a given GPU, so a second
    run starts instantly on free machines and queues behind the current run
    on busy ones.
    """
    while True:
        rid = None
        with _lock:
            alive = sum(1 for t in _run_threads.values() if t.is_alive())
            if _queue and alive < _MAX_ACTIVE_RUNS:
                rid = _queue.pop(0)
        if rid is None:
            time.sleep(2)
            continue
        t = threading.Thread(target=_run_thread, args=(rid,), daemon=True,
                             name=f"run-{rid}")
        with _lock:
            _run_threads[rid] = t
        t.start()


def _run_thread(rid: int) -> None:
    try:
        run_job(rid)
    finally:
        with _lock:
            _run_threads.pop(rid, None)


def start_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_worker_loop, daemon=True)
        _worker.start()


def submit(params: dict, dtype: str, name: str,
           worker_map: dict | None = None) -> dict:
    params = dict(params)
    if str(params.get("mode")) == "character" and \
            params.get("seed") in (None, "", 0):
        # Identity consistency: every still of a character dataset shares one
        # deterministic seed base derived from the dataset name. The plates
        # and the wardrobe matrix therefore render the SAME person, and
        # re-running the dataset reproduces the same face family (the prompt
        # carries the variation, the seed carries the identity).
        import zlib
        params["seed"] = 100000 + (zlib.crc32(str(name).encode("utf-8"))
                                   % 900000)
    plan = build_plan(params)
    if plan["total"] == 0:
        return {"ok": False, "error": "no images requested"}
    if plan["total"] > MAX_BATCH:
        return {"ok": False,
                "error": f"total {plan['total']} exceeds safety cap "
                         f"{MAX_BATCH}"}
    if plan["warnings"]:
        return {"ok": False, "error": "; ".join(plan["warnings"])}
    start_worker()
    rid = db.create_run(dtype, name, params, plan["total"])
    if worker_map:
        # per-image worker assignment — persisted in
        # queue.db so an interrupted run resumes with the same assignments;
        # datasets.db schema stays untouched.
        try:
            import task_queue
            task_queue.set_run_worker_map(rid, worker_map)
        except Exception as e:
            log.warning(f"run {rid}: worker map not persisted: {e}")
    with _lock:
        _queue.append(rid)
    return {"ok": True, "run_id": rid, "total": plan["total"],
            "combo_count": plan["combo_count"]}


def cancel(rid: int) -> None:
    # Per-task _cancel_ids checks abort between images. We deliberately do
    # NOT call ComfyClient.interrupt() — it interrupts ComfyUI GLOBALLY and
    # would kill concurrent renders queued from other pages.
    _cancel_ids.add(rid)


def enqueue_pending() -> int:
    """Re-enqueue runs left 'queued' in the DB (startup recovery — api.py
    calls this once at boot after recover_runs()). Returns count added."""
    n = 0
    with _lock:
        for r in db.list_runs(db.RUN_QUEUED):
            rid = int(r["id"])
            if rid not in _queue:
                _queue.append(rid)
                n += 1
    if n:
        log.info(f"enqueued {n} pending run(s) from db")
    return n


def retry(rid: int) -> dict:
    """Retry a failed/interrupted run: mark it queued again; already-done
    images are skipped because deterministic tasks are saved in order and
    run_job() resumes after the persisted image count (job continuation —
    never re-renders or duplicates existing outputs)."""
    run = db.get_run(rid)
    if not run:
        return {"ok": False, "error": "run not found"}
    if run["status"] == db.RUN_ACTIVE:
        return {"ok": False, "error": "run is active"}
    db.update_run(rid, status=db.RUN_QUEUED, done=0, failed=0, error=None)
    start_worker()
    with _lock:
        _queue.append(rid)
    return {"ok": True}
