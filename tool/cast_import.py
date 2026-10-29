"""Batch -> cast import (wizard Tab 4).

Turns a finished image dataset into Bible entries so the rest of the tool
adapts automatically:

  face        -> character: identity text (body/outfit), identity plates as
                 `plate:close|medium|full` hero refs, weapon configs, and
                 loadouts (outfit [+ weapon]) with their composite stills
                 tagged by loadout name. The resolver's loadout/framing
                 matching and the story LLM's vocabulary pick these up.
  environment -> location: one tagged reference per PLACE (the tag the story
                 LLM uses as `place`), with a people-free gate — an
                 environment ref containing people reads as a subject to the
                 video model (measured: a market crowd in the background ref
                 flipped the lead character's gender).
  power       -> power on a character: level-tagged stills (the resolver
                 floors by the shot's power_level).
  weapon      -> weapon on a character: design stills as extra refs.

Everything is idempotent: re-importing the same dataset replaces entries by
tag/level/file instead of duplicating them. `dry_run` reports the plan
without writing anything (used by tests and the UI preview).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from core import ROOT, InvalidInput, is_safe_component, log_setup, safe_join

log = log_setup("cast_import")

_PLATE_FROM_SHOT = {"close-up": "close", "3/4 body": "medium", "full body": "full"}


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _slug(name: str) -> str:
    out = "".join(c if (c.isalnum() or c in "_-") else "_"
                  for c in str(name or "").strip().lower())
    out = "_".join(x for x in out.split("_") if x)
    return out[:60] or "unnamed"


def _reuse_id(existing_keys, want: str) -> str:
    """Return the EXISTING key that matches `want` case-insensitively.

    Ids are user-facing (a dataset named `IcePower5` must extend the existing
    power `IcePower5`, not create a lowercase twin). Without this, every
    import whose dataset name differs in case from the Bible id duplicated
    the entity — measured: Sara ended up with both `IcePower5` and
    `icepower5`, and the architect prompt listed the power twice.
    """
    w = str(want or "").strip().lower()
    for k in existing_keys or []:
        if str(k).strip().lower() == w:
            return str(k)
    return str(want or "").strip()


def _copy_name(type_: str, name: str, file: str) -> str:
    """Deterministic project-copy name (same convention as the Cast pickers):
    re-importing the same still overwrites the same copy."""
    raw = f"{type_}_{name}_{file}"
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in raw)
    return f"ref_{safe}"[:140]


def _batch_image_path(type_: str, name: str, file: str) -> Path | None:
    # safe_join refuses absolute / drive-letter / UNC paths and traversal
    if not is_safe_component(type_) or not is_safe_component(name) or not file:
        return None
    base = ROOT / "roster" / "datasets" / type_ / name
    for root in (base, base / "stills", base / "masters"):
        try:
            cand = safe_join(root, str(file))
        except InvalidInput:
            continue
        if cand.is_file():
            return cand
    return None


def _rows(dtype: str, name: str) -> list[dict]:
    try:
        import datasets_db
        return datasets_db.list_images(dtype, name, limit=200000) or []
    except Exception as e:  # noqa: BLE001
        log.warning("cast_import: could not list %s/%s: %s", dtype, name, e)
        return []


def _run_params(rows: list[dict]) -> dict:
    """Merged params of every run that produced this dataset (newest first).

    The identity-plates run carries the identity fields, the wardrobe-matrix
    run carries the weapon configs — merging keeps both. First non-empty
    value wins, so the newest run's values take precedence.
    """
    merged: dict = {}
    seen: set = set()
    try:
        import datasets_db
    except Exception:  # noqa: BLE001
        return merged
    for row in rows:
        rid = row.get("run_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        try:
            r = datasets_db.get_run(rid) or {}
            p = json.loads(r.get("params") or "{}")
        except Exception:  # noqa: BLE001
            continue
        for k, v in (p or {}).items():
            if k not in merged and v not in (None, "", [], {}):
                merged[k] = v
    return merged


def _curated_kept(dtype: str, name: str) -> set[str]:
    """Basenames of stills the operator marked Keep in curation.json.

    Empty set = nothing curated yet (the import then uses every still). The
    Curate pane writes verdicts under `_auto` keyed by relative posix paths;
    legacy absolute keys are tolerated via basename matching. This is what
    makes "select Keep → it is saved for that character" true: the cast
    import prefers the curated set over the raw batch.
    """
    p = ROOT / "roster" / "datasets" / dtype / name / "curation.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    auto = data.get("_auto") if isinstance(data, dict) else None
    if not isinstance(auto, dict):
        auto = data if isinstance(data, dict) else {}
    out: set[str] = set()
    for rel, meta in auto.items():
        if isinstance(meta, dict) and meta.get("keep") is True:
            out.add(Path(str(rel)).name)
    return out


def _rows_for_import(dtype: str, name: str) -> tuple[list[dict], bool]:
    """Rows to import + whether a curated keep-set narrowed them."""
    rows = _rows(dtype, name)
    kept = _curated_kept(dtype, name)
    if kept:
        rows = [r for r in rows
                if Path(str(r.get("file") or "")).name in kept]
    return rows, bool(kept)


def _variations(row: dict) -> dict:
    v = row.get("variations")
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _plate_of(v: dict) -> str:
    p = str(v.get("plate") or "").strip().lower()
    if p in ("close", "medium", "full"):
        return p
    return _PLATE_FROM_SHOT.get(str(v.get("shot") or "").strip().lower(), "")


def _has_faces(path) -> int:
    """People gate for environment refs. Fail-open (0) — a missing detector
    must never block an import."""
    try:
        from impact_onnx_face import detect_faces
        return int(detect_faces(path))
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------- text helpers
def _identity_text(p: dict) -> str:
    bits = []
    age = str(p.get("age") or "").strip()
    pres = str(p.get("presentation") or p.get("gender") or "").strip()
    if age or pres:
        bits.append(f"{age} {pres}".strip())
    hair = " ".join(x for x in (str(p.get("hair_style") or "").strip(),
                                str(p.get("hair_color") or "").strip()) if x)
    if hair:
        bits.append(f"{hair} hair")
    eyes = " ".join(x for x in (str(p.get("eye_color") or "").strip(),
                                str(p.get("eye_shape") or "").strip()) if x)
    if eyes:
        bits.append(f"{eyes} eyes")
    if p.get("skin"):
        bits.append(f"{p['skin']} skin")
    face = ", ".join(x for x in (str(p.get("face") or "").strip(),
                                 str(p.get("face_shape") or "").strip(),
                                 str(p.get("brows") or "").strip(),
                                 str(p.get("nose") or "").strip(),
                                 str(p.get("lips") or "").strip(),
                                 str(p.get("facial_hair") or "").strip()) if x)
    if face:
        bits.append(f"{face} face" if "face" not in face else face)
    build = str(p.get("build") or p.get("body") or "").strip()
    if build:
        bits.append(f"{build} build")
    mark = str(p.get("marks") or p.get("distinctive") or "").strip()
    if mark and mark != "none":
        bits.append(f"with {mark}")
    lin = str(p.get("lineage") or "").strip()
    if lin and lin != "human":
        bits.append(lin)
    return ", ".join(bits)


def _outfit_text(p: dict, variations: dict) -> str:
    v = variations or {}
    sil = str(p.get("silhouette") or p.get("outfit") or "").strip()
    color = str(p.get("wardrobe_color") or "").strip()
    mats = p.get("materials") or []
    detail = str(p.get("detail") or "").strip()
    cond = str(p.get("wardrobe_condition") or "").strip()
    outfit_var = str(v.get("outfit") or "").strip()
    bits = []
    if outfit_var and outfit_var != "base outfit":
        bits.append(outfit_var)
    if sil:
        bits.append(sil)
    if color:
        bits.append(color)
    if mats:
        bits.append(", ".join(str(m) for m in mats[:3]))
    if detail and detail != "none":
        bits.append(detail)
    if cond and cond != "pristine":
        bits.append(cond)
    return ", ".join(bits)


def _loadout_name(outfit: str, weapon: str) -> str:
    o = str(outfit or "").strip()
    w = str(weapon or "").strip()
    return f"{o} + {w}" if w else o


# ------------------------------------------------------------- ref manifests
def _upsert(refs: list[dict], entry: dict, key: str) -> list[dict]:
    """Replace the entry with the same key (tag/level/file) or append."""
    k = str(entry.get(key) or "")
    for i, r in enumerate(refs):
        if str(r.get(key) or "") == k:
            refs[i] = entry
            return refs
    refs.append(entry)
    return refs


def _copy_ref(pid: str, sub: str, iid: str, src: Path, dtype: str,
              name: str, file: str) -> str | None:
    from bible import BIBLE_DIR
    try:
        d = BIBLE_DIR / "projects" / pid / "refs" / sub / iid
        d.mkdir(parents=True, exist_ok=True)
        dst = d / _copy_name(dtype, name, file)
        shutil.copyfile(src, dst)
        return dst.name
    except Exception as e:  # noqa: BLE001
        log.warning("cast_import: copy failed %s -> %s/%s/%s: %s",
                    src, sub, iid, file, e)
        return None


def _write_selected(pid: str, sub: str, iid: str, src: Path) -> None:
    from bible import BIBLE_DIR
    try:
        d = BIBLE_DIR / "projects" / pid / "refs" / sub / iid
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, d / "selected.png")
    except Exception as e:  # noqa: BLE001
        log.warning("cast_import: selected.png failed: %s", e)


# --------------------------------------------------------------- importers
def import_character(pid: str, name: str, opts: dict | None = None,
                     dtype: str = "face") -> dict:
    from bible import Bible
    from promptspec import weapon_txt

    opts = opts or {}
    dry = bool(opts.get("dry_run"))
    rows, curated = _rows_for_import(dtype, name)
    if not rows:
        return {"ok": False,
                "error": (f"no kept stills in {dtype}/{name} — curate the batch "
                          f"or clear the keep selection") if curated else
                         (f"no generated stills in {dtype}/{name}")}
    params = _run_params(rows)
    cid = str(opts.get("character_id") or "").strip() or _slug(name)
    if not is_safe_component(cid):
        return {"ok": False, "error": f"bad character id '{cid}'"}
    bible = Bible(pid)
    cid = _reuse_id(bible.characters().keys(), cid)
    existing = bible.characters().get(cid) or {}
    char = dict(existing)
    created = not existing
    overwrite = bool(opts.get("overwrite"))
    char.setdefault("id", cid)
    char.setdefault("name", str(opts.get("display_name") or "").strip() or cid)
    char.setdefault("tier", str(opts.get("tier") or "main"))
    palette = [str(e) for e in (params.get("emotions") or [])][:6]
    if palette and (overwrite or not char.get("emotion_palette")):
        char["emotion_palette"] = palette
    body = _identity_text(params)
    outfit = _outfit_text(params, {})
    wrote = False
    if body and (overwrite or not char.get("body")):
        char["body"], wrote = body, True
    if outfit and (overwrite or not char.get("outfit")):
        char["outfit"], wrote = outfit, True
    if wrote:
        # the batch's own prompt: a later main look rewrites it (cast_look)
        char["look_source"] = f"casting:{dtype}/{name}"

    # ---- weapons: configs from the batch (descriptor = the prompt language)
    weapons = dict(char.get("weapons") or {})
    added_weapons: list[str] = []
    for wid, cfg in (params.get("weapon_configs") or {}).items():
        wid = str(wid).strip()
        if not wid or not is_safe_component(wid):
            continue
        cfg = dict(cfg or {})
        w = dict(weapons.get(wid) or {})
        w.setdefault("type", str(cfg.get("type") or wid))
        w.setdefault("descriptor", weapon_txt(cfg))
        for k in ("material", "style", "size", "color", "energy", "glow",
                  "ornament", "condition"):
            if cfg.get(k) and not w.get(k):
                w[k] = cfg[k]
        w.setdefault("handling", ["held at side"])
        w.setdefault("sfx", "")
        weapons[wid] = w
        added_weapons.append(wid)
    if added_weapons:
        char["weapons"] = weapons

    # ---- classify the stills: identity plates vs loadout composites
    plates: list[tuple[str, dict, Path, str]] = []
    loadouts: dict[tuple[str, str], tuple[dict, Path, str]] = {}
    for row in rows:
        rel = str(row.get("file") or "")
        src = _batch_image_path(dtype, name, rel)
        if src is None:
            continue
        v = _variations(row)
        plate = _plate_of(v)
        outfit_v = str(v.get("outfit") or "").strip()
        weapon_v = str(v.get("weapon") or "").strip()
        if plate and not weapon_v and outfit_v in ("", "base outfit"):
            plates.append((plate, row, src, rel))
        elif outfit_v and outfit_v != "base outfit":
            key = (outfit_v, weapon_v)
            loadouts.setdefault(key, (row, src, rel))

    refs = [r for r in (char.get("reference_images") or [])
            if isinstance(r, dict)]
    n_plates = n_loadouts = 0
    lds = dict(char.get("loadouts") or {})

    for plate, row, src, rel in plates:
        entry = {"type": "face", "name": name, "file": rel,
                 "role": "primary" if plate == "medium" else "secondary",
                 "tag": f"plate:{plate}", "added": _ts()}
        if not dry:
            copied = _copy_ref(pid, "chars", cid, src, dtype, name, rel)
            if not copied:
                continue
            entry["copied"] = copied
            if plate == "medium":
                _write_selected(pid, "chars", cid, src)
        refs = _upsert(refs, entry, "tag")
        n_plates += 1

    for (outfit_v, weapon_v), (row, src, rel) in loadouts.items():
        lname = _loadout_name(outfit_v, weapon_v)
        ld = dict(lds.get(lname) or {})
        ld.setdefault("outfit", outfit_v)
        if weapon_v:
            ld.setdefault("weapon", weapon_v)
        lds[lname] = ld
        entry = {"type": "face", "name": name, "file": rel,
                 "role": "secondary", "tag": lname, "added": _ts()}
        if not dry:
            copied = _copy_ref(pid, "chars", cid, src, dtype, name, rel)
            if not copied:
                continue
            entry["copied"] = copied
        refs = _upsert(refs, entry, "tag")
        n_loadouts += 1

    if lds:
        char["loadouts"] = lds
    char["reference_images"] = refs
    if not dry:
        bible.save_character(char)
    return {"ok": True, "dry_run": dry, "id": cid, "created": created,
            "plates": n_plates, "loadouts": sorted(lds.keys()),
            "weapons": added_weapons,
            "body": char.get("body", ""), "outfit": char.get("outfit", ""),
            "refs_total": len(refs), "curated": curated}


def import_environment(pid: str, name: str, opts: dict | None = None,
                       dtype: str = "environment") -> dict:
    from bible import Bible

    opts = opts or {}
    dry = bool(opts.get("dry_run"))
    rows, curated = _rows_for_import(dtype, name)
    if not rows:
        return {"ok": False,
                "error": (f"no kept stills in {dtype}/{name} — curate the batch "
                          f"or clear the keep selection") if curated else
                         (f"no generated stills in {dtype}/{name}")}
    params = _run_params(rows)
    lid = str(opts.get("location_id") or "").strip() or _slug(name)
    if not is_safe_component(lid):
        return {"ok": False, "error": f"bad location id '{lid}'"}
    bible = Bible(pid)
    lid = _reuse_id(bible.locations().keys(), lid)
    existing = bible.locations().get(lid) or {}
    loc = dict(existing)
    created = not existing
    overwrite = bool(opts.get("overwrite"))
    loc.setdefault("id", lid)
    loc.setdefault("name", str(opts.get("display_name") or "").strip() or lid)
    world = str(params.get("world") or "").strip()
    if world and (overwrite or not loc.get("descriptor")):
        loc["descriptor"] = world

    picks: dict[str, tuple[dict, Path, str]] = {}
    untagged: list[tuple[dict, Path, str]] = []
    for row in rows:
        rel = str(row.get("file") or "")
        src = _batch_image_path(dtype, name, rel)
        if src is None:
            continue
        v = _variations(row)
        # the place of an environment still is recorded as `place` (new
        # batches) or `location` (the structured env combo key every existing
        # batch used) — reading only `place` silently untagged every import
        place = str(v.get("place") or v.get("location") or "").strip()
        if place:
            picks.setdefault(place, (row, src, rel))
        else:
            untagged.append((row, src, rel))

    refs = [r for r in (loc.get("reference_images") or [])
            if isinstance(r, dict)]
    imported: list[str] = []
    blocked: list[str] = []

    def _add(tag: str, row: dict, src: Path, rel: str) -> bool:
        nonlocal refs
        if _has_faces(src):
            blocked.append(tag or Path(rel).name)
            return False
        entry = {"type": "environment", "name": name, "file": rel,
                 "role": "primary" if not refs else "secondary",
                 "added": _ts()}
        if tag:
            entry["tag"] = tag
        if not dry:
            copied = _copy_ref(pid, "locs", lid, src, dtype, name, rel)
            if not copied:
                return False
            entry["copied"] = copied
            if not refs:
                _write_selected(pid, "locs", lid, src)
        refs = _upsert(refs, entry, "tag" if tag else "file")
        imported.append(tag or Path(rel).name)
        return True

    for place, (row, src, rel) in picks.items():
        _add(place, row, src, rel)
    if not picks:
        for row, src, rel in untagged[:8]:
            _add("", row, src, rel)

    if not imported:
        if blocked:
            return {"ok": False,
                    "error": f"every candidate still contains people "
                             f"({len(blocked)} blocked: "
                             f"{', '.join(blocked[:6])}) — regenerate the "
                             f"batch or pick people-free stills"}
        return {"ok": False, "error": "no importable stills found"}

    loc["reference_images"] = refs
    if not dry:
        bible.save_location(loc)
    return {"ok": True, "dry_run": dry, "id": lid, "created": created,
            "places": imported, "blocked_people": blocked,
            "refs_total": len(refs), "curated": curated}


def import_power(pid: str, name: str, opts: dict | None = None,
                 dtype: str = "power") -> dict:
    from bible import Bible

    opts = opts or {}
    dry = bool(opts.get("dry_run"))
    cid = str(opts.get("character_id") or "").strip()
    if not cid:
        return {"ok": False, "error": "character_id required for a power import"}
    if not is_safe_component(cid):         # becomes a refs/powers/<cid> dir
        return {"ok": False, "error": f"bad character id '{cid}'"}
    bible = Bible(pid)
    char = bible.characters().get(cid)
    if char is None:
        return {"ok": False, "error": f"character '{cid}' not found"}
    rows, curated = _rows_for_import(dtype, name)
    if not rows:
        return {"ok": False,
                "error": (f"no kept stills in {dtype}/{name} — curate the batch "
                          f"or clear the keep selection") if curated else
                         (f"no generated stills in {dtype}/{name}")}
    params = _run_params(rows)
    pw = str(opts.get("power_id") or "").strip() or _slug(name)
    if not is_safe_component(pw):
        return {"ok": False, "error": f"bad power id '{pw}'"}

    char = dict(char)
    powers = dict(char.get("powers") or {})
    pw = _reuse_id(powers.keys(), pw)
    power = dict(powers.get(pw) or {})
    for src_key, dst in (("color", "color"), ("material", "material"),
                         ("glow", "glow"), ("behavior", "motion")):
        if params.get(src_key) and not power.get(dst):
            power[dst] = params[src_key]
    power.setdefault("motion", str(params.get("behavior") or "flowing"))
    # Descriptor: a readable phrase built from the batch's vfx vocabulary —
    # never the bare dataset/id name (an earlier import wrote "IcePower5" as
    # the descriptor and the architect prompt showed exactly that).
    desc = str(power.get("descriptor") or "").strip()
    if not desc or desc.lower() in (str(pw).lower(), str(name).lower()):
        core = " ".join(x for x in (str(power.get("color") or "").strip(),
                                    str(power.get("material") or "").strip())
                        if x) or str(params.get("power_type") or pw)
        motion = str(power.get("motion") or "").strip()
        glow = str(power.get("glow") or "").strip()
        power["descriptor"] = (f"{core} energy"
                               + (f", {motion}" if motion else "")
                               + (f", {glow}" if glow and glow not in core
                                  else ""))

    refs = [r for r in (power.get("reference_images") or [])
            if isinstance(r, dict)]
    n = 0
    for row in rows:
        rel = str(row.get("file") or "")
        src = _batch_image_path(dtype, name, rel)
        if src is None:
            continue
        v = _variations(row)
        try:
            level = int(v.get("level"))
        except (TypeError, ValueError):
            continue
        entry = {"type": "power", "name": name, "file": rel, "level": level,
                 "added": _ts()}
        if not dry:
            copied = _copy_ref(pid, "powers", f"{cid}/{pw}", src,
                               dtype, name, rel)
            if not copied:
                continue
            entry["copied"] = copied
        refs = _upsert(refs, entry, "level")
        n += 1
    if n == 0:
        return {"ok": False, "error": "no level-tagged stills found — run the "
                                      "power batch with the level ladder on"}
    power["reference_images"] = refs
    powers[pw] = power
    char["powers"] = powers
    if not dry:
        bible.save_character(char)
    return {"ok": True, "dry_run": dry, "character": cid, "power": pw,
            "levels": sorted({int(r.get("level") or 0) for r in refs
                              if r.get("level")}),
            "refs_total": len(refs), "curated": curated}


def import_weapon(pid: str, name: str, opts: dict | None = None,
                  dtype: str = "weapon") -> dict:
    from bible import Bible
    from promptspec import weapon_txt

    opts = opts or {}
    dry = bool(opts.get("dry_run"))
    cid = str(opts.get("character_id") or "").strip()
    if not cid:
        return {"ok": False, "error": "character_id required for a weapon import"}
    if not is_safe_component(cid):         # becomes a refs/powers/<cid> dir
        return {"ok": False, "error": f"bad character id '{cid}'"}
    bible = Bible(pid)
    char = bible.characters().get(cid)
    if char is None:
        return {"ok": False, "error": f"character '{cid}' not found"}
    rows, curated = _rows_for_import(dtype, name)
    if not rows:
        return {"ok": False,
                "error": (f"no kept stills in {dtype}/{name} — curate the batch "
                          f"or clear the keep selection") if curated else
                         (f"no generated stills in {dtype}/{name}")}
    params = _run_params(rows)
    wid = str(opts.get("weapon_id") or "").strip() or _slug(name)
    if not is_safe_component(wid):
        return {"ok": False, "error": f"bad weapon id '{wid}'"}

    char = dict(char)
    weapons = dict(char.get("weapons") or {})
    wid = _reuse_id(weapons.keys(), wid)
    w = dict(weapons.get(wid) or {})
    cfg = {
        "type": params.get("weapon_type") or w.get("type") or wid,
        "material": params.get("material") or w.get("material") or "",
        "style": params.get("style") or w.get("style") or "",
        "size": params.get("size") or w.get("size") or "",
        "color": params.get("color") or w.get("color") or "",
        "energy": params.get("energy") or w.get("energy") or "none",
        "glow": params.get("glow") or w.get("glow") or "",
        "ornament": params.get("ornament") or w.get("ornament") or "none",
        "condition": params.get("condition") or w.get("condition") or "pristine",
    }
    if not w.get("descriptor"):
        w["descriptor"] = weapon_txt(cfg)
    for k, v in cfg.items():
        if v and not w.get(k):
            w[k] = v
    w.setdefault("handling", ["held at side"])
    w.setdefault("sfx", "")

    refs = [r for r in (w.get("reference_images") or [])
            if isinstance(r, dict)]
    # clean product stills first ("no hands"), then any other still
    ordered = sorted(rows, key=lambda r: 0 if str(
        (_variations(r).get("handling") or "")) == "no hands" else 1)
    n = 0
    for row in ordered[:2]:
        rel = str(row.get("file") or "")
        src = _batch_image_path(dtype, name, rel)
        if src is None:
            continue
        entry = {"type": "weapon", "name": name, "file": rel, "added": _ts()}
        if not dry:
            copied = _copy_ref(pid, "powers", f"{cid}/weapon_{wid}", src,
                               dtype, name, rel)
            if not copied:
                continue
            entry["copied"] = copied
        refs = _upsert(refs, entry, "file")
        n += 1
    if n == 0:
        return {"ok": False, "error": "no usable stills found"}
    w["reference_images"] = refs
    weapons[wid] = w
    char["weapons"] = weapons
    if not dry:
        bible.save_character(char)
    return {"ok": True, "dry_run": dry, "character": cid, "weapon": wid,
            "descriptor": w.get("descriptor", ""), "refs_total": len(refs),
            "curated": curated}


def import_batch(pid: str, dtype: str, name: str,
                 opts: dict | None = None) -> dict:
    """Dispatch a batch import by dataset type. Idempotent + dry-run capable."""
    opts = opts or {}
    if not is_safe_component(str(name or "")):
        return {"ok": False, "error": f"bad dataset name '{name}'"}
    d = str(dtype or "face").strip().lower()
    if d == "face":
        return import_character(pid, name, opts, dtype=d)
    if d == "environment":
        return import_environment(pid, name, opts, dtype=d)
    if d == "power":
        return import_power(pid, name, opts, dtype=d)
    if d == "weapon":
        return import_weapon(pid, name, opts, dtype=d)
    return {"ok": False, "error": f"import not supported for dataset type "
                                  f"'{dtype}' (use face/environment/power/weapon)"}
