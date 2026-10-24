"""Kinoloom - Cast & Reference Images domain router.

Provides endpoints for characters and locations cast management, hero references,
power references, slot references (outfit/weapon), scene mappings, and batch reference workflows.
"""
from __future__ import annotations

import os
import re
import time
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import common
from .common import (
    Bible, store, db, batch_gen, shot_images, datasets_db, _proj_guard,
    _REF_EXTS, _REF_MAX_BYTES, _REF_MEDIA, CHAR_TIERS_CAST
)
from core import (ROOT, InvalidInput, is_safe_component, load_config, log_setup, now,
                  safe_join, save_config, to_jsonable)
from cast_import import import_batch as _import_batch
from cast_import import _identity_text, _outfit_text

router = APIRouter()
log = log_setup("common.cast")

def __getattr__(name: str):
    return getattr(common, name)


def _ref_path(folder: Path, file: str) -> Path | None:
    """`folder/file` for a client-supplied bare file name, or None when it is
    not a plain name inside `folder` (separators, drive letters, "."/"..")."""
    if not file or Path(file).name != file:
        return None
    try:
        return safe_join(folder, file)
    except InvalidInput:
        return None


def _too_big(field) -> bool:
    """Reject an oversized upload from its declared size, before reading it."""
    size = getattr(field, "size", None)
    return size is not None and size > _REF_MAX_BYTES

@router.post("/api/projects/{pid}/cast/scenes/{sid}/delete")
def cast_scene_delete(pid: str, sid: str) -> dict:
    """Delete a scene: project bible scene YAML + its shots in the queue
    (rendered files on disk are kept)."""
    if not is_safe_component(pid) or not is_safe_component(sid):
        return {"ok": False, "error": "invalid id"}
    p = Bible(pid).dir / "scenes" / f"{sid}.yaml"
    if p.exists():
        p.unlink()
        Bible(pid)._git("add", "-A")
        Bible(pid)._git("commit", "-m", f"delete scene {sid}",
                        "--allow-empty")
    removed = 0
    for r in db.all(project=pid):
        if r.get("scene") == sid:
            db.delete_shot(r["shot_id"], pid)
            removed += 1
    return {"ok": True, "removed_shots": removed}


def _cast_slug(name: str) -> str:
    s = "".join(c.lower() if c.isalnum() else "_" for c in name.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "character"


def _valid_character_fields(data: dict, partial: bool = False) -> dict | None:
    """Validate/whitelist character fields. Returns error dict or None."""
    if not partial:
        for key in ("name", "tier", "body", "outfit"):
            if data.get(key) is None:
                return {"ok": False, "error": f"{key} required"}
    if data.get("tier") is not None and data["tier"] not in CHAR_TIERS_CAST:
        return {"ok": False, "error": "tier must be "
                "main|supporting|recurring|crowd"}
    if "emotion_palette" in data and data["emotion_palette"] is not None \
            and not isinstance(data["emotion_palette"], list):
        return {"ok": False, "error": "emotion_palette must be a list"}
    if data.get("powers") is not None and not isinstance(data["powers"], dict):
        return {"ok": False, "error": "powers must be a dict"}
    if data.get("weapons") is not None and not isinstance(data["weapons"], dict):
        return {"ok": False, "error": "weapons must be a dict"}
    if data.get("outfits") is not None and not isinstance(data["outfits"], dict):
        return {"ok": False, "error": "outfits must be a dict"}
    if data.get("loadouts") is not None and not isinstance(data["loadouts"], dict):
        return {"ok": False, "error": "loadouts must be a dict"}
    if data.get("scene_tags") is not None and not isinstance(data["scene_tags"], dict):
        return {"ok": False, "error": "scene_tags must be a dict"}
    if data.get("lora") is not None and \
            not isinstance(data["lora"], dict):
        return {"ok": False, "error": "lora must be a dict or null"}
    if data.get("voice") is not None and not isinstance(data["voice"], dict):
        return {"ok": False, "error": "voice must be a dict"}
    if data.get("enabled") is not None and \
            not isinstance(data["enabled"], bool):
        return {"ok": False, "error": "enabled must be a bool"}
    # Reference images for the LTX-2.5 pipeline: an ordered list of
    # {type,name,file,role} entries picked from the training batches. Guarded
    # here because an unnoticed non-list would corrupt the cast YAML and every
    # later render would silently lose its references.
    if data.get("reference_images") is not None:
        refs = data["reference_images"]
        if not isinstance(refs, list):
            return {"ok": False, "error": "reference_images must be a list"}
        for r in refs:
            if not isinstance(r, dict) or not r.get("file"):
                return {"ok": False,
                        "error": "each reference_images entry needs a file"}
    return None


@router.post("/api/projects/{pid}/cast/characters")
def cast_character_create(pid: str, body: dict) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    bad = _valid_character_fields(body)
    if bad:
        return bad
    name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    cid = str(body.get("id") or "").strip() or _cast_slug(name)
    if not is_safe_component(cid):
        return {"ok": False, "error": "invalid character id"}
    p_bible = Bible(pid)
    existing = p_bible.characters()
    if cid in existing and not str(body.get("id") or "").strip():
        # AUTO-UNIQUIFY when the id was derived from the name: the UI's
        # default ("New Character") collided on the second click and the
        # create failed with "already exists". Explicit ids still error.
        base = cid
        n = 2
        while cid in existing:
            cid = f"{base}_{n}"
            n += 1
        name = f"{name} {n - 1}"
    if cid in existing:
        return {"ok": False, "error": f"character {cid} already exists"}
    char = {
        "id": cid, "name": name, "tier": body.get("tier") or "supporting",
        "body": str(body.get("body") or ""),
        "outfit": str(body.get("outfit") or ""),
        # a character without a palette is unusable (the linter only allows
        # palette emotions) — seed an escalating default when none is given
        "emotion_palette": list(body.get("emotion_palette") or [])
                           or ["stoic", "calm", "determined", "angry"],
        "powers": body.get("powers") or {},
        "enabled": bool(body.get("enabled", True)),
    }
    for key in ("weapons", "outfits", "loadouts", "scene_tags"):
        if isinstance(body.get(key), dict):
            char[key] = body[key]
    if body.get("voice") is not None:
        char["voice"] = body["voice"]
    else:
        char["voice"] = {"engine": "moss-tts", "style_tag": "", "params": {}}
    if body.get("lora") is not None:
        char["lora"] = body["lora"]
    if body.get("reference_images") is not None:
        char["reference_images"] = list(body["reference_images"])
    p_bible.save_character(char)
    return {"ok": True, "character": char}


@router.post("/api/projects/{pid}/cast/characters/{cid}")
def cast_character_update(pid: str, cid: str, body: dict) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(cid):
        return {"ok": False, "error": "invalid character id"}
    bad = _valid_character_fields(body, partial=True)
    if bad:
        return bad
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    for key in ("name", "tier", "body", "outfit"):
        if body.get(key) is not None:
            char[key] = body[key]
    if body.get("emotion_palette") is not None:
        char["emotion_palette"] = list(body["emotion_palette"])
    if body.get("powers") is not None:
        char["powers"] = body["powers"]  # replaced wholesale
    for key in ("weapons", "outfits", "loadouts", "scene_tags"):
        if body.get(key) is not None:
            char[key] = body[key]  # replaced wholesale
    if body.get("voice") is not None:
        char["voice"] = body["voice"]
    if "lora" in body:
        if body["lora"] is None:
            char.pop("lora", None)
        else:
            char["lora"] = body["lora"]
    if body.get("reference_images") is not None:
        char["reference_images"] = list(body["reference_images"])
    if body.get("enabled") is not None:
        char["enabled"] = bool(body["enabled"])
    p_bible.save_character(char)
    return {"ok": True, "character": char}


@router.post("/api/projects/{pid}/cast/characters/{cid}/delete")
def cast_character_delete(pid: str, cid: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(cid):
        return {"ok": False, "error": "invalid character id"}
    p_bible = Bible(pid)
    p = p_bible.dir / "characters" / f"{cid}.yaml"
    if not p.exists():
        return {"ok": False, "error": "character not found"}
    p.unlink()
    p_bible._git("add", "-A")
    p_bible._git("commit", "-m", f"delete character {cid}", "--allow-empty")
    return {"ok": True}


def _valid_location_fields(data: dict, partial: bool = False) -> dict | None:
    if not partial and not data.get("id"):
        return {"ok": False, "error": "id required"}
    if data.get("enabled") is not None and \
            not isinstance(data["enabled"], bool):
        return {"ok": False, "error": "enabled must be a bool"}
    lora = data.get("lora")
    if lora is not None and not isinstance(lora, dict):
        return {"ok": False, "error": "lora must be {file, strength} or null"}
    if isinstance(lora, dict) and not lora.get("file"):
        return {"ok": False, "error": "lora.file required"}
    if data.get("reference_images") is not None:
        refs = data["reference_images"]
        if not isinstance(refs, list):
            return {"ok": False, "error": "reference_images must be a list"}
        for r in refs:
            if not isinstance(r, dict) or not r.get("file"):
                return {"ok": False,
                        "error": "each reference_images entry needs a file"}
    return None


@router.post("/api/projects/{pid}/cast/locations")
def cast_location_create(pid: str, body: dict) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    bad = _valid_location_fields(body)
    if bad:
        return bad
    lid = str(body.get("id") or "").strip()
    if not is_safe_component(lid):
        return {"ok": False, "error": "invalid location id"}
    p_bible = Bible(pid)
    if lid in p_bible.locations():
        return {"ok": False, "error": f"location {lid} already exists"}
    loc = {
        "id": lid,
        "descriptor": str(body.get("descriptor") or ""),
        "lighting": str(body.get("lighting") or ""),
        "enabled": bool(body.get("enabled", True)),
    }
    lora = body.get("lora")
    if isinstance(lora, dict) and lora.get("file"):
        loc["lora"] = {"file": str(lora["file"]),
                       "strength": float(lora.get("strength") or 0.6)}
    if body.get("reference_images") is not None:
        loc["reference_images"] = list(body["reference_images"])
    p_bible.save_location(loc)
    return {"ok": True, "location": loc}


@router.post("/api/projects/{pid}/cast/locations/{lid}")
def cast_location_update(pid: str, lid: str, body: dict) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(lid):
        return {"ok": False, "error": "invalid location id"}
    bad = _valid_location_fields(body, partial=True)
    if bad:
        return bad
    p_bible = Bible(pid)
    locs = p_bible.locations()
    if lid not in locs:
        return {"ok": False, "error": "location not found"}
    loc = dict(locs[lid])
    for key in ("descriptor", "lighting"):
        if body.get(key) is not None:
            loc[key] = body[key]
    if body.get("enabled") is not None:
        loc["enabled"] = bool(body["enabled"])
    if "lora" in body:
        lora = body.get("lora")
        if isinstance(lora, dict) and lora.get("file"):
            loc["lora"] = {"file": str(lora["file"]),
                           "strength": float(lora.get("strength") or 0.6)}
        else:
            loc.pop("lora", None)
    if body.get("reference_images") is not None:
        loc["reference_images"] = list(body["reference_images"])
    p_bible.save_location(loc)
    return {"ok": True, "location": loc}


@router.post("/api/projects/{pid}/cast/locations/{lid}/delete")
def cast_location_delete(pid: str, lid: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(lid):
        return {"ok": False, "error": "invalid location id"}
    p_bible = Bible(pid)
    p = p_bible.dir / "locations" / f"{lid}.yaml"
    if not p.exists():
        return {"ok": False, "error": "location not found"}
    p.unlink()
    p_bible._git("add", "-A")
    p_bible._git("commit", "-m", f"delete location {lid}", "--allow-empty")
    return {"ok": True}


@router.post("/api/projects/{pid}/refs/upload")
async def project_refs_upload(pid: str, request: Request) -> dict:
    import time as _t
    proj, err = _proj_guard(pid)
    if err:
        return err
    form = await request.form()
    refs_dir = Bible(pid).dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    ts = int(_t.time() * 1000)
    saved = []
    idx = 0
    for fname, field in form.multi_items():
        if fname != "files" or not hasattr(field, "read") or _too_big(field):
            continue
        data = await field.read()
        if not data or len(data) > _REF_MAX_BYTES:
            continue
        ext = Path(getattr(field, "filename", "") or "x.png").suffix.lower()
        if ext not in _REF_EXTS:
            continue
        fn = f"ref_{ts}_{idx}{ext}"
        (refs_dir / fn).write_bytes(data)
        saved.append(fn)
        idx += 1
    return {"ok": bool(saved), "refs": saved}


@router.get("/api/projects/{pid}/refs")
def project_refs(pid: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    refs_dir = Bible(pid).dir / "refs"
    story = proj.get("story") or {}
    active = story.get("reference") if isinstance(story, dict) else None
    files = ([f.name for f in sorted(refs_dir.glob("*")) if f.is_file()]
             if refs_dir.is_dir() else [])
    return {"refs": [{"file": f, "active": f == active} for f in files]}


@router.post("/api/projects/{pid}/refs/{file}/activate")
def project_ref_activate(pid: str, file: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    p = _ref_path(Bible(pid).dir / "refs", file)
    if p is None or not p.is_file():
        return {"ok": False, "error": "ref not found"}
    story = proj.get("story") or {}
    story = dict(story) if isinstance(story, dict) else {}
    story["reference"] = file
    store.update(pid, story=story)
    return {"ok": True, "active": file}


@router.post("/api/projects/{pid}/refs/{file}/delete")
def project_ref_delete(pid: str, file: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    p = _ref_path(Bible(pid).dir / "refs", file)
    if p is None:
        return {"ok": False, "error": "invalid ref name"}
    if p.is_file():
        p.unlink()
    story = proj.get("story") or {}
    story = dict(story) if isinstance(story, dict) else {}
    if story.get("reference") == file:
        story.pop("reference", None)
        store.update(pid, story=story)
    return {"ok": True}


@router.get("/api/refs/{pid}/{file}")
def project_ref_file(pid: str, file: str) -> FileResponse:
    if not is_safe_component(pid):
        raise FileNotFoundError(file)
    p = _ref_path(Bible(pid).dir / "refs", file)
    if p is None or not p.is_file():
        raise FileNotFoundError(file)
    return FileResponse(str(p),
                        media_type=_REF_MEDIA.get(p.suffix.lower(),
                                                  "image/png"))


@router.get("/api/shotrefs/{pid}/{sid}/{file}")
def shot_ref_file(pid: str, sid: str, file: str) -> FileResponse:
    if not is_safe_component(pid) or not is_safe_component(sid):
        raise FileNotFoundError(file)
    for d in (ROOT / "shots" / "refs" / pid / sid,
              ROOT / "shots" / "refs" / sid):
        p = _ref_path(d, file)
        if p is not None and p.is_file():
            return FileResponse(str(p),
                                media_type=_REF_MEDIA.get(p.suffix.lower(),
                                                          "image/png"))
    raise FileNotFoundError(file)


@router.get("/api/projects/{pid}/refbatch/options")
def refbatch_options(pid: str) -> dict:
    """Shots available for batch reference generation + model/LoRA catalogs
    (mirrors the per-shot studio)."""
    proj, err = _proj_guard(pid)
    if err:
        return err
    import models as _models
    shots = []
    for r in db.all(project=pid):
        j = r.get("json") or {}
        shots.append({"shot_id": r["shot_id"], "scene": r["scene"],
                      "tier": r.get("tier", ""),
                      "status": r.get("status", ""),
                      "ref_image": bool(r.get("ref_image")),
                      "action": (j.get("primary_action")
                                 or (j.get("characters") or [{}])[0]
                                 .get("action", ""))[:60]})
    return {"ok": True, "shots": shots,
            "models": _models.all_model_info(),
            "loras": _models.GEN_LORAS,
            "ref_fallback": int(store.get(pid).get("ref_fallback", 1) or 1)}


class RefBatchBody(BaseModel):
    shots: list[str] = []
    config: dict = {}


@router.post("/api/projects/{pid}/refbatch/start")
def refbatch_start(pid: str, body: RefBatchBody) -> dict:
    """Queue reference-image generation for many shots at once, then
    auto-assign the best result to each shot."""
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not body.shots:
        return {"ok": False, "error": "select at least one shot"}
    cfg = dict(body.config or {})
    cfg.setdefault("model", "realvis")
    cfg.setdefault("count", 1)
    import models as _models
    m = _models.get_model(str(cfg["model"]))
    if m is None:
        return {"ok": False, "error": f"unknown model '{cfg['model']}'"}
    if cfg.get("hires") and not m["capabilities"].get("hires", False):
        return {"ok": False,
                "error": f"{m['name']} does not support the hires fix "
                         f"(SDXL-family models only)"}
    return batch_gen.start(pid, body.shots, cfg)


@router.get("/api/projects/{pid}/refbatch/status")
def refbatch_status(pid: str) -> dict:
    return batch_gen.status(pid)


@router.post("/api/projects/{pid}/refbatch/stop")
def refbatch_stop(pid: str) -> dict:
    return batch_gen.stop(pid)


class RefFallbackBody(BaseModel):
    enabled: bool


@router.get("/api/projects/{pid}/refs-fallback")
def refs_fallback_get(pid: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    return {"ok": True, "enabled": bool(store.get(pid).get("ref_fallback", 1))}


@router.post("/api/projects/{pid}/refs-fallback")
def refs_fallback_set(pid: str, body: RefFallbackBody) -> dict:
    """When OFF, shots with no reference/keyframe render T2V (LoRAs +
    prompt) instead of starting from the character's roster portrait."""
    proj, err = _proj_guard(pid)
    if err:
        return err
    store.update(pid, ref_fallback=1 if body.enabled else 0)
    return {"ok": True, "enabled": body.enabled}


@router.post("/api/projects/{pid}/cast/characters/{cid}/herorefs/upload")
async def cast_herorefs_upload(pid: str, cid: str, request: Request) -> dict:
    import time as _t
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(cid):
        return {"ok": False, "error": "invalid character id"}
    form = await request.form()
    refs_dir = Bible(pid).dir / "refs" / "chars" / cid
    refs_dir.mkdir(parents=True, exist_ok=True)
    ts = int(_t.time() * 1000)
    saved = []
    idx = 0
    for fname, field in form.multi_items():
        if fname != "files" or not hasattr(field, "read") or _too_big(field):
            continue
        data = await field.read()
        if not data or len(data) > _REF_MAX_BYTES:
            continue
        ext = Path(getattr(field, "filename", "") or "x.png").suffix.lower()
        if ext not in _REF_EXTS:
            continue
        fn = f"ref_{ts}_{idx}{ext}"
        (refs_dir / fn).write_bytes(data)
        saved.append(fn)
        idx += 1
    return {"ok": bool(saved), "refs": saved}


@router.get("/api/projects/{pid}/cast/characters/{cid}/herorefs")
def cast_herorefs(pid: str, cid: str) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(cid):
        return {"ok": False, "error": "invalid character id"}
    refs_dir = Bible(pid).dir / "refs" / "chars" / cid
    files = ([f.name for f in sorted(refs_dir.glob("*")) if f.is_file()]
             if refs_dir.is_dir() else [])
    auto = [{"file": f, "url": f"/api/herorefs/{pid}/{cid}/{f}"}
            for f in shot_images.auto_identity_files(pid, cid)]
    # the ranked manifest (batch provenance + role) used by the LTX-2.5
    # pipeline; `refs` stays the flat file list for backward compatibility
    manifest = []
    try:
        ch = Bible(pid).characters().get(cid) or {}
        manifest = [r for r in (ch.get("reference_images") or [])
                    if isinstance(r, dict)]
    except Exception:
        manifest = []
    return {"refs": files, "auto": auto, "references": manifest}


@router.get("/api/herorefs/{pid}/{cid}/{file}")
def heroref_file(pid: str, cid: str, file: str) -> FileResponse:
    if Path(file).name != file or not is_safe_component(pid) \
            or not is_safe_component(cid):
        raise FileNotFoundError(file)
    p = shot_images.resolve_identity_path(pid, cid, file)
    if p is None:
        raise FileNotFoundError(file)
    return FileResponse(str(p),
                        media_type=_REF_MEDIA.get(p.suffix.lower(),
                                                  "image/png"))


@router.get("/api/powerrefs/{pid}/{cid}/{pw}/{file}")
def powerref_file(pid: str, cid: str, pw: str, file: str) -> FileResponse:
    """Serve one stored power reference image (thumbnails in the Cast tab)."""
    if not is_safe_component(pid) \
            or not is_safe_component(cid) or not is_safe_component(pw):
        raise FileNotFoundError(file)
    p = _ref_path(Bible(pid).dir / "refs" / "powers" / cid / pw, file)
    if p is None or not p.is_file():
        raise FileNotFoundError(file)
    return FileResponse(str(p),
                        media_type=_REF_MEDIA.get(p.suffix.lower(),
                                                  "image/png"))


@router.get("/api/slotrefs/{pid}/{cid}/{kind}/{sid}/{file}")
def slotref_file(pid: str, cid: str, kind: str, sid: str,
                 file: str) -> FileResponse:
    """Serve one stored weapon/outfit reference image (Cast tab thumbnails)."""
    if kind not in _SLOTREF_KINDS:
        raise FileNotFoundError(file)
    if not is_safe_component(pid) \
            or not is_safe_component(cid) or not is_safe_component(sid):
        raise FileNotFoundError(file)
    p = _ref_path(Bible(pid).dir / "refs" / "powers" / cid / f"{kind}_{sid}",
                  file)
    if p is None or not p.is_file():
        raise FileNotFoundError(file)
    return FileResponse(str(p),
                        media_type=_REF_MEDIA.get(p.suffix.lower(),
                                                  "image/png"))


class PickSelectBody(BaseModel):
    type: str
    name: str
    file: str
    # LTX-2.5 reference slots: a character may carry a primary and an optional
    # secondary still; locations carry their primary environment still.
    role: str = "primary"          # primary | secondary
    append: bool = False           # False = replace the entry with this role
    # Loadout composites: a named set ("JulietWithWeapon") whose still already
    # carries identity + outfit + weapon. Entries with the same tag replace
    # each other; the shot's loadout selects which one is passed.
    tag: str = ""


def _ref_copy_name(type_: str, name: str, file: str) -> str:
    """Deterministic project-copy name for a picked batch image.

    Deterministic so re-picking the same batch image overwrites the same copy
    instead of accumulating cruft, and so the resolver can find it from the
    manifest without a directory scan.
    """
    raw = f"{type_}_{name}_{file}"
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in raw)
    return f"ref_{safe}"[:140]


def _batch_image_path(type_: str, name: str, file: str) -> Path | None:
    """Resolve a batch image reference to a file on disk.

    The batch browser serves paths relative to the dataset folder, e.g.
    `stills/v_002.png` or `master_front-facing.png`, and trainers write stills
    under `stills/`. The lookup therefore accepts a relative sub-path (never
    traversal) and searches the batch root, `stills/` and `masters/` — the
    previous single-root lookup silently failed for every `stills/...` pick.
    safe_join refuses absolute / drive-letter / UNC paths and traversal.
    """
    if not is_safe_component(type_) or not is_safe_component(name) or not file:
        return None
    base = ROOT / "roster" / "datasets" / type_ / name
    for root in (base, base / "stills", base / "masters"):
        try:
            cand = safe_join(root, file)
        except InvalidInput:
            continue
        if cand.is_file():
            return cand
    return None


def _update_ref_list(item: dict, entry: dict, append: bool) -> list:
    """Insert/replace a reference entry in a cast item's reference_images list."""
    refs = [r for r in (item.get("reference_images") or []) if isinstance(r, dict)]
    if append:
        if not any(r.get("file") == entry["file"] and r.get("type") == entry.get("type")
                   for r in refs):
            refs.append(entry)
    else:
        role = entry.get("role") or "primary"
        idx = next((i for i, r in enumerate(refs)
                    if (r.get("role") or "primary") == role), None)
        if idx is None:
            refs.append(entry)
        else:
            refs[idx] = entry
    # primary first so the resolver and the UI agree on the order
    refs.sort(key=lambda r: 0 if (r.get("role") or "primary") == "primary" else 1)
    return refs


def _upsert_ref(item: dict, entry: dict, append: bool,
                key: str = "") -> list:
    """Tag/file-aware reference upsert used by locations and powers.

    Locations and powers may hold MANY images (variants / power levels), so the
    character rule (replace by role) is too coarse. When an explicit key is
    given (tag for locations, file for powers) entries with the same key
    replace each other; append always adds (deduped by key+file). Order is
    preserved — manifests keep their curated order.
    """
    refs = [r for r in (item.get("reference_images") or [])
            if isinstance(r, dict)]

    def _key(r: dict) -> str:
        if key:
            return str(r.get(key) or "")
        return str(r.get("role") or "primary")

    if append:
        if not any(r.get("file") == entry["file"]
                   and (not key or _key(r) == _key(entry))
                   for r in refs):
            refs.append(entry)
        return refs
    k = _key(entry)
    idx = next((i for i, r in enumerate(refs) if _key(r) == k), None)
    if idx is None:
        refs.append(entry)
    else:
        refs[idx] = entry
    return refs


@router.post("/api/projects/{pid}/cast/characters/{cid}/herorefs/select")
def cast_heroref_select(pid: str, cid: str, body: PickSelectBody) -> dict:
    """Pick a photo from a training batch as a character reference.

    Backward compatible: the default (role=primary, append=false) still writes
    `selected.png` exactly as before, and additionally records the pick in the
    cast YAML so the LTX-2.5 pipeline can resolve it (with provenance) and the
    Cast tab can show a ranked list. Appending a secondary still keeps the
    primary untouched.
    """
    import shutil as sh
    from datetime import datetime, timezone
    if not is_safe_component(pid) or not is_safe_component(cid):
        return {"ok": False, "error": "invalid id"}
    if body.type != "face":
        return {"ok": False, "error": "character identity refs come from face "
                                      "batches only (power refs go on the "
                                      "power, environment refs on the "
                                      "location)"}
    if body.role not in ("primary", "secondary"):
        return {"ok": False, "error": "role must be primary|secondary"}
    src = _batch_image_path(body.type, body.name, body.file)
    if src is None:
        return {"ok": False, "error": "source image not found"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    dst_dir = p_bible.dir / "refs" / "chars" / cid
    dst_dir.mkdir(parents=True, exist_ok=True)
    copy_name = _ref_copy_name(body.type, body.name, body.file)
    sh.copy2(src, dst_dir / copy_name)
    if body.role == "primary":
        sh.copy2(src, dst_dir / "selected.png")   # legacy consumers
    entry = {"type": body.type, "name": body.name, "file": body.file,
             "copied": copy_name, "role": body.role,
             "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if body.tag.strip():
        entry["tag"] = body.tag.strip()
    char = dict(chars[cid])
    if entry.get("tag"):
        # tagged picks are loadout composites: one entry per loadout name
        char["reference_images"] = _upsert_ref(char, entry, body.append,
                                               key="tag")
    else:
        char["reference_images"] = _update_ref_list(char, entry, body.append)
    p_bible.save_character(char)
    return {"ok": True, "references": char["reference_images"],
            "ref": copy_name,
            "url": f"/api/herorefs/{pid}/{cid}/{copy_name}"}


class RefRemoveBody(BaseModel):
    file: str


@router.post("/api/projects/{pid}/cast/characters/{cid}/herorefs/remove")
def cast_heroref_remove(pid: str, cid: str, body: RefRemoveBody) -> dict:
    """Remove a picked character reference (manifest entry + project copy)."""
    if not is_safe_component(pid) or not is_safe_component(cid):
        return {"ok": False, "error": "invalid id"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    refs = [r for r in (char.get("reference_images") or []) if isinstance(r, dict)]
    keep = [r for r in refs if r.get("file") != body.file]
    if len(keep) == len(refs):
        return {"ok": False, "error": "reference not found"}
    removed = next((r for r in refs if r.get("file") == body.file), None)
    char["reference_images"] = keep
    p_bible.save_character(char)
    if removed:
        for name in (removed.get("copied"),):
            if name:
                p = _ref_path(p_bible.dir / "refs" / "chars" / cid,
                              Path(str(name)).name)
                if p is None or not p.is_file():
                    continue
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
    return {"ok": True, "references": keep}


class LocRefSelectBody(BaseModel):
    type: str
    name: str
    file: str
    append: bool = False           # False = replace the primary entry
    # Environment variants: a free-form name ("arena", "palace lobby"). Entries
    # with the same tag replace each other; the shot's `location_tag` (or the
    # location's per-scene map) selects the variant at render time.
    tag: str = ""


@router.post("/api/projects/{pid}/cast/locations/{lid}/refs/upload")
async def cast_locref_upload(pid: str, lid: str, request: Request) -> dict:
    import time as _t
    if not is_safe_component(pid) or not is_safe_component(lid):
        return {"ok": False, "error": "invalid id"}
    form = await request.form()
    d = Bible(pid).dir / "refs" / "locs" / lid
    d.mkdir(parents=True, exist_ok=True)
    ts = int(_t.time() * 1000)
    saved, idx = [], 0
    for fname, field in form.multi_items():
        if fname != "files" or not hasattr(field, "read") or _too_big(field):
            continue
        data = await field.read()
        if not data or len(data) > _REF_MAX_BYTES:
            continue
        ext = Path(getattr(field, "filename", "") or "x.png").suffix.lower()
        if ext not in _REF_EXTS:
            continue
        fn = f"ref_{ts}_{idx}{ext}"
        (d / fn).write_bytes(data)
        saved.append(fn)
        idx += 1
    return {"ok": bool(saved), "refs": saved}


@router.post("/api/projects/{pid}/cast/locations/{lid}/refs/select")
def cast_locref_select(pid: str, lid: str, body: LocRefSelectBody) -> dict:
    """Pick an environment batch photo as the location reference.

    Same semantics as the character picker: `selected.png` is still written
    for backward compatibility, and the pick is recorded in the location YAML
    so the LTX-2.5 pipeline resolves it with provenance.
    """
    import shutil as sh
    from datetime import datetime, timezone
    if not is_safe_component(pid) or not is_safe_component(lid):
        return {"ok": False, "error": "invalid id"}
    if body.type != "environment":
        return {"ok": False, "error": "location refs come from environment "
                                      "batches"}
    src = _batch_image_path(body.type, body.name, body.file)
    if src is None:
        return {"ok": False, "error": "source image not found"}
    # people gate: a face in an environment reference reads as a SUBJECT to
    # the video model (measured: a market crowd in the background ref flipped
    # the lead character's gender). Advisory only — the pick still goes
    # through, with the warning surfaced in the UI.
    faces = 0
    try:
        from cast_import import _has_faces
        faces = _has_faces(src)
    except Exception:  # noqa: BLE001
        faces = 0
    p_bible = Bible(pid)
    locs = p_bible.locations()
    if lid not in locs:
        return {"ok": False, "error": "location not found"}
    d = p_bible.dir / "refs" / "locs" / lid
    d.mkdir(parents=True, exist_ok=True)
    copy_name = _ref_copy_name(body.type, body.name, body.file)
    sh.copy2(src, d / copy_name)
    if not body.tag.strip():
        sh.copy2(src, d / "selected.png")   # legacy consumers
    entry = {"type": body.type, "name": body.name, "file": body.file,
             "copied": copy_name, "role": "primary",
             "tag": body.tag.strip(),
             "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    loc = dict(locs[lid])
    loc["reference_images"] = _upsert_ref(loc, entry, body.append, key="tag")
    p_bible.save_location(loc)
    out = {"ok": True, "references": loc["reference_images"],
           "ref": copy_name, "faces": faces,
           "url": f"/api/locrefs/{pid}/{lid}/{copy_name}"}
    if faces:
        out["warning"] = (f"this still contains {faces} face(s) — people in an "
                          f"environment reference can bleed into the render "
                          f"(identity/size). Prefer a people-free still.")
    return out


@router.post("/api/projects/{pid}/cast/locations/{lid}/refs/remove")
def cast_locref_remove(pid: str, lid: str, body: RefRemoveBody) -> dict:
    """Remove a picked location reference (manifest entry + project copy)."""
    if not is_safe_component(pid) or not is_safe_component(lid):
        return {"ok": False, "error": "invalid id"}
    p_bible = Bible(pid)
    locs = p_bible.locations()
    if lid not in locs:
        return {"ok": False, "error": "location not found"}
    loc = dict(locs[lid])
    refs = [r for r in (loc.get("reference_images") or []) if isinstance(r, dict)]
    keep = [r for r in refs if r.get("file") != body.file]
    if len(keep) == len(refs):
        return {"ok": False, "error": "reference not found"}
    removed = next((r for r in refs if r.get("file") == body.file), None)
    loc["reference_images"] = keep
    p_bible.save_location(loc)
    if removed and removed.get("copied"):
        p = _ref_path(p_bible.dir / "refs" / "locs" / lid,
                      Path(str(removed["copied"])).name)
        try:
            if p is not None and p.is_file():
                p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return {"ok": True, "references": keep}


class LocSceneMapBody(BaseModel):
    scene: str                     # "s01"
    tag: str = ""                  # "" clears the mapping for that scene


@router.post("/api/projects/{pid}/cast/locations/{lid}/scene-map")
def cast_locref_scene_map(pid: str, lid: str, body: LocSceneMapBody) -> dict:
    """Bind a scene to an environment variant tag ("" = automatic).

    The resolver reads this map when a shot carries no explicit
    `location_tag`, so a story that moves arena -> palace lobby switches the
    environment reference automatically, scene by scene.
    """
    if not is_safe_component(pid) or not is_safe_component(lid):
        return {"ok": False, "error": "invalid id"}
    if not is_safe_component(body.scene):
        return {"ok": False, "error": "invalid scene id"}
    p_bible = Bible(pid)
    locs = p_bible.locations()
    if lid not in locs:
        return {"ok": False, "error": "location not found"}
    loc = dict(locs[lid])
    smap = dict(loc.get("scene_tags") or {})
    tag = body.tag.strip()
    if tag:
        smap[body.scene] = tag
    else:
        smap.pop(body.scene, None)
    loc["scene_tags"] = smap
    p_bible.save_location(loc)
    return {"ok": True, "scene_tags": smap}


class PowerRefBody(BaseModel):
    type: str = "power"            # enforced: powers only accept power batches
    name: str
    file: str
    level: int | None = None       # 1..N intensity; default = append order
    append: bool = True            # power levels accumulate by default


@router.post("/api/projects/{pid}/cast/characters/{cid}/powers/{pw}/refs/select")
def cast_powerref_select(pid: str, cid: str, pw: str,
                         body: PowerRefBody) -> dict:
    """Pick a POWER batch photo as one intensity level of a character's power.

    Type discipline is enforced here: only `power` batches are accepted, and
    the entry is stored on the power itself — it can never leak into the
    character's identity slots or a location's environment slots.
    """
    import shutil as sh
    from datetime import datetime, timezone
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(pw):
        return {"ok": False, "error": "invalid id"}
    if body.type != "power":
        return {"ok": False, "error": "power refs come from power batches only"}
    src = _batch_image_path(body.type, body.name, body.file)
    if src is None:
        return {"ok": False, "error": "source image not found"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    powers = dict(char.get("powers") or {})
    if pw not in powers:
        return {"ok": False, "error": f"power '{pw}' not found on {cid}"}
    power = dict(powers[pw]) if isinstance(powers.get(pw), dict) else {}
    d = p_bible.dir / "refs" / "powers" / cid / pw
    d.mkdir(parents=True, exist_ok=True)
    copy_name = _ref_copy_name(body.type, body.name, body.file)
    sh.copy2(src, d / copy_name)
    existing = [r for r in (power.get("reference_images") or [])
                if isinstance(r, dict)]
    level = body.level
    if level is None:
        level = len(existing) + 1
    entry = {"type": body.type, "name": body.name, "file": body.file,
             "copied": copy_name, "level": int(level),
             "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    power["reference_images"] = _upsert_ref(power, entry, body.append,
                                            key="file")
    powers[pw] = power
    char["powers"] = powers
    p_bible.save_character(char)
    return {"ok": True, "power": pw,
            "references": power["reference_images"],
            "ref": copy_name,
            "url": f"/api/powerrefs/{pid}/{cid}/{pw}/{copy_name}"}


class PowerLevelBody(BaseModel):
    file: str
    level: int


@router.get("/api/projects/{pid}/cast/characters/{cid}/powers/{pw}/refs")
def cast_powerrefs(pid: str, cid: str, pw: str) -> dict:
    """Manifest + files for one power's stored reference levels (Cast tab)."""
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(pw):
        return {"ok": False, "error": "invalid id"}
    p_bible = Bible(pid)
    char = (p_bible.characters().get(cid) or {})
    power = ((char.get("powers") or {}).get(pw) or {})
    refs = [r for r in (power.get("reference_images") or [])
            if isinstance(r, dict)]
    d = p_bible.dir / "refs" / "powers" / cid / pw
    files = ([f.name for f in sorted(d.glob("*")) if f.is_file()]
             if d.is_dir() else [])
    return {"refs": files, "references": refs}


@router.post("/api/projects/{pid}/cast/characters/{cid}/powers/{pw}/refs/update")
def cast_powerref_update(pid: str, cid: str, pw: str,
                         body: PowerLevelBody) -> dict:
    """Change the intensity level of one stored power reference."""
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(pw):
        return {"ok": False, "error": "invalid id"}
    lvl = int(body.level)
    if lvl < 1 or lvl > 99:
        return {"ok": False, "error": "level must be 1..99"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    powers = dict(char.get("powers") or {})
    power = dict(powers.get(pw) or {})
    refs = [dict(r) for r in (power.get("reference_images") or [])
            if isinstance(r, dict)]
    hit = next((r for r in refs if r.get("file") == body.file), None)
    if hit is None:
        return {"ok": False, "error": "reference not found"}
    hit["level"] = lvl
    power["reference_images"] = refs
    powers[pw] = power
    char["powers"] = powers
    p_bible.save_character(char)
    return {"ok": True, "references": refs}


@router.post("/api/projects/{pid}/cast/characters/{cid}/powers/{pw}/refs/remove")
def cast_powerref_remove(pid: str, cid: str, pw: str,
                         body: RefRemoveBody) -> dict:
    """Remove a stored power reference (manifest entry + project copy)."""
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(pw):
        return {"ok": False, "error": "invalid id"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    powers = dict(char.get("powers") or {})
    power = dict(powers.get(pw) or {})
    refs = [r for r in (power.get("reference_images") or [])
            if isinstance(r, dict)]
    keep = [r for r in refs if r.get("file") != body.file]
    if len(keep) == len(refs):
        return {"ok": False, "error": "reference not found"}
    removed = next((r for r in refs if r.get("file") == body.file), None)
    power["reference_images"] = keep
    powers[pw] = power
    char["powers"] = powers
    p_bible.save_character(char)
    if removed and removed.get("copied"):
        p = _ref_path(p_bible.dir / "refs" / "powers" / cid / pw,
                      Path(str(removed["copied"])).name)
        try:
            if p is not None and p.is_file():
                p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return {"ok": True, "references": keep}


class AutofillBody(BaseModel):
    type: str
    name: str
    file: str


class RenameIdBody(BaseModel):
    new_id: str


def _rename_entity(pid: str, kind: str, old: str, new: str) -> dict:
    """Rename a cast id: YAML file, its refs folder and shot references.

    ids are the vocabulary the story LLM and the prompts use — leaving them
    as `new_location` produced scripts full of placeholder ids.
    """
    import shutil as sh
    from bible import Bible
    if not is_safe_component(new) or not new:
        return {"ok": False, "error": "invalid new id (letters, digits, _ -)"}
    b = Bible(pid)
    sub = "characters" if kind == "characters" else "locations"
    old_p = b.dir / sub / f"{old}.yaml"
    new_p = b.dir / sub / f"{new}.yaml"
    if not old_p.is_file():
        return {"ok": False, "error": f"{kind[:-1]} not found"}
    if new_p.exists():
        return {"ok": False, "error": f"{new} already exists"}
    data = {}
    try:
        import yaml as _yaml
        data = _yaml.safe_load(old_p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        data = {}
    data["id"] = new
    try:
        import yaml as _yaml
        new_p.write_text(_yaml.safe_dump(data, allow_unicode=True,
                                         sort_keys=False),
                         encoding="utf-8")
    except Exception as e:
        log.exception("cast operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"write failed: {e}"})
    old_p.unlink()
    # refs folder (project copies live under refs/{chars,locs}/<id>)
    ref_kind = "chars" if kind == "characters" else "locs"
    old_ref = b.dir / "refs" / ref_kind / old
    new_ref = b.dir / "refs" / ref_kind / new
    try:
        if old_ref.is_dir() and not new_ref.exists():
            sh.move(str(old_ref), str(new_ref))
    except Exception as e:  # noqa: BLE001
        log.warning("rename %s: refs move failed: %s", old, e)
    # best-effort: shots referencing the old id
    updated = 0
    try:
        for r in db.all(project=pid):
            j = dict(r.get("json") or {})
            hit = False
            if kind == "locations" and str(j.get("location") or "") == old:
                j["location"] = new
                hit = True
            if kind == "characters":
                for c in (j.get("characters") or []):
                    if isinstance(c, dict) and str(c.get("id")) == old:
                        c["id"] = new
                        hit = True
                for d in (j.get("dialogue") or []):
                    if isinstance(d, dict) and str(d.get("char") or "") == old:
                        d["char"] = new
                        hit = True
                j["expected_visible"] = [new if str(x) == old else x
                                         for x in (j.get("expected_visible") or [])]
            if hit:
                db.set_json(r["shot_id"], j, project=pid)
                updated += 1
    except Exception as e:  # noqa: BLE001
        log.warning("rename %s: shot update failed: %s", old, e)
    return {"ok": True, "id": new, "shots_updated": updated}


@router.post("/api/projects/{pid}/cast/locations/{lid}/rename")
def cast_location_rename(pid: str, lid: str, body: RenameIdBody) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(lid):
        return {"ok": False, "error": "invalid id"}
    return _rename_entity(pid, "locations", lid, body.new_id.strip())


@router.post("/api/projects/{pid}/cast/characters/{cid}/rename")
def cast_character_rename(pid: str, cid: str, body: RenameIdBody) -> dict:
    proj, err = _proj_guard(pid)
    if err:
        return err
    if not is_safe_component(cid):
        return {"ok": False, "error": "invalid id"}
    return _rename_entity(pid, "characters", cid, body.new_id.strip())


@router.post("/api/projects/{pid}/cast/characters/{cid}/autofill")
def cast_character_autofill(pid: str, cid: str, body: AutofillBody) -> dict:
    """Body/Outfit text derived from a batch still's recorded provenance."""
    if not is_safe_component(pid) or not is_safe_component(cid):
        return {"ok": False, "error": "invalid id"}
    if not is_safe_component(body.name):
        return {"ok": False, "error": "invalid batch name"}
    row = None
    for r in datasets_db.list_images(body.type, body.name, limit=200000):
        if r["file"] == body.file:
            row = r
            break
    if row is None:
        return {"ok": False, "error": "no provenance record for that still"}
    try:
        variations = json.loads(row.get("variations") or "{}")
    except Exception:  # noqa: BLE001
        variations = {}
    params: dict = {}
    rid = row.get("run_id")
    if rid:
        run = datasets_db.get_run(int(rid))
        try:
            params = json.loads((run or {}).get("params") or "{}")
        except Exception:  # noqa: BLE001
            params = {}
    ident = _identity_text(params)
    outfit = _outfit_text(params, variations)
    return {"ok": True, "body": ident, "outfit": outfit,
            "weapon": str(variations.get("weapon") or ""),
            "handling": str(variations.get("handling") or ""),
            "from": f"{body.type}/{body.name}/{body.file}"}


class ImportBatchBody(BaseModel):
    type: str = "face"
    name: str
    character_id: str = ""
    location_id: str = ""
    power_id: str = ""
    weapon_id: str = ""
    display_name: str = ""
    tier: str = ""
    overwrite: bool = False
    dry_run: bool = False


@router.post("/api/projects/{pid}/cast/import-batch")
def cast_import_batch(pid: str, body: ImportBatchBody) -> dict:
    """Batch -> cast import (wizard Tab 4 / dataset card action).

    Creates or extends the Bible entry for a generated dataset: character
    (identity text + framing plates + loadouts + weapon configs), location
    (place-tagged refs behind a people-free gate), power (level-tagged refs)
    or weapon (design refs). Idempotent; `dry_run` returns the plan without
    writing anything.
    """
    if not is_safe_component(pid):
        return {"ok": False, "error": "invalid project"}
    if not is_safe_component(body.name):
        return {"ok": False, "error": "invalid batch name"}
    opts = {"character_id": body.character_id.strip(),
            "location_id": body.location_id.strip(),
            "power_id": body.power_id.strip(),
            "weapon_id": body.weapon_id.strip(),
            "display_name": body.display_name.strip(),
            "tier": body.tier.strip(),
            "overwrite": bool(body.overwrite),
            "dry_run": bool(body.dry_run)}
    try:
        return _import_batch(pid, body.type, body.name, opts)
    except Exception as e:  # noqa: BLE001
        log.exception("import-batch failed for %s/%s", body.type, body.name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


_SLOTREF_KINDS = {"weapon": ("weapons", "weapon"),
                  "outfit": ("outfits", "outfit")}


class SlotRefBody(BaseModel):
    type: str
    name: str
    file: str
    append: bool = True


@router.post("/api/projects/{pid}/cast/characters/{cid}/slotrefs/{kind}/{sid}/select")
def cast_slotref_select(pid: str, cid: str, kind: str, sid: str,
                        body: SlotRefBody) -> dict:
    """Pick a WEAPON/OUTFIT batch photo as an extra reference for one
    weapon/outfit config entry. Type discipline is enforced."""
    import shutil as sh
    from datetime import datetime, timezone
    if kind not in _SLOTREF_KINDS:
        return {"ok": False, "error": "kind must be weapon|outfit"}
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(sid):
        return {"ok": False, "error": "invalid id"}
    field, want_type = _SLOTREF_KINDS[kind]
    if body.type != want_type:
        return {"ok": False,
                "error": f"{kind} refs come from {want_type} batches only"}
    src = _batch_image_path(body.type, body.name, body.file)
    if src is None:
        return {"ok": False, "error": "source image not found"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    items = dict(char.get(field) or {})
    if sid not in items or not isinstance(items.get(sid), dict):
        return {"ok": False, "error": f"{kind} '{sid}' not found on {cid}"}
    item = dict(items[sid])
    d = p_bible.dir / "refs" / "powers" / cid / f"{kind}_{sid}"
    d.mkdir(parents=True, exist_ok=True)
    copy_name = _ref_copy_name(body.type, body.name, body.file)
    sh.copy2(src, d / copy_name)
    entry = {"type": body.type, "name": body.name, "file": body.file,
             "copied": copy_name,
             "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    item["reference_images"] = _upsert_ref(item, entry, body.append,
                                           key="file")
    items[sid] = item
    char[field] = items
    p_bible.save_character(char)
    return {"ok": True, kind: sid, "references": item["reference_images"],
            "ref": copy_name}


@router.get("/api/projects/{pid}/cast/characters/{cid}/slotrefs/{kind}/{sid}")
def cast_slotrefs(pid: str, cid: str, kind: str, sid: str) -> dict:
    if kind not in _SLOTREF_KINDS:
        return {"ok": False, "error": "kind must be weapon|outfit"}
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(sid):
        return {"ok": False, "error": "invalid id"}
    field, _ = _SLOTREF_KINDS[kind]
    p_bible = Bible(pid)
    char = (p_bible.characters().get(cid) or {})
    item = ((char.get(field) or {}).get(sid) or {})
    refs = [r for r in (item.get("reference_images") or [])
            if isinstance(r, dict)]
    d = p_bible.dir / "refs" / "powers" / cid / f"{kind}_{sid}"
    files = ([f.name for f in sorted(d.glob("*")) if f.is_file()]
             if d.is_dir() else [])
    return {"refs": files, "references": refs}


@router.post("/api/projects/{pid}/cast/characters/{cid}/slotrefs/{kind}/{sid}/remove")
def cast_slotref_remove(pid: str, cid: str, kind: str, sid: str,
                        body: RefRemoveBody) -> dict:
    if kind not in _SLOTREF_KINDS:
        return {"ok": False, "error": "kind must be weapon|outfit"}
    if not is_safe_component(pid) or not is_safe_component(cid) \
            or not is_safe_component(sid):
        return {"ok": False, "error": "invalid id"}
    field, _ = _SLOTREF_KINDS[kind]
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    items = dict(char.get(field) or {})
    item = dict(items.get(sid) or {})
    refs = [r for r in (item.get("reference_images") or [])
            if isinstance(r, dict)]
    keep = [r for r in refs if r.get("file") != body.file]
    if len(keep) == len(refs):
        return {"ok": False, "error": "reference not found"}
    removed = next((r for r in refs if r.get("file") == body.file), None)
    item["reference_images"] = keep
    items[sid] = item
    char[field] = items
    p_bible.save_character(char)
    if removed and removed.get("copied"):
        p = _ref_path(p_bible.dir / "refs" / "powers" / cid / f"{kind}_{sid}",
                      Path(str(removed["copied"])).name)
        try:
            if p is not None and p.is_file():
                p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return {"ok": True, "references": keep}


class CharSceneMapBody(BaseModel):
    scene: str                     # "s01"
    tag: str = ""                  # loadout name; "" clears the mapping


@router.post("/api/projects/{pid}/cast/characters/{cid}/scene-map")
def cast_character_scene_map(pid: str, cid: str,
                             body: CharSceneMapBody) -> dict:
    """Bind a scene to one of the character's loadouts (mirrors the location
    scene map). The renderer resolves the loadout per shot: explicit shot
    override -> this map -> beat-text match -> the base outfit."""
    if not is_safe_component(pid) or not is_safe_component(cid):
        return {"ok": False, "error": "invalid id"}
    p_bible = Bible(pid)
    chars = p_bible.characters()
    if cid not in chars:
        return {"ok": False, "error": "character not found"}
    char = dict(chars[cid])
    tag = body.tag.strip()
    if tag and tag not in (char.get("loadouts") or {}):
        return {"ok": False,
                "error": f"loadout '{tag}' is not defined on {cid}"}
    smap = dict(char.get("scene_tags") or {})
    if tag:
        smap[body.scene] = tag
    else:
        smap.pop(body.scene, None)
    char["scene_tags"] = smap
    p_bible.save_character(char)
    return {"ok": True, "scene_tags": smap}


@router.get("/api/projects/{pid}/cast/locations/{lid}/refs")
def cast_locrefs(pid: str, lid: str) -> dict:
    if not is_safe_component(pid) or not is_safe_component(lid):
        return {"ok": False, "error": "invalid id"}
    d = Bible(pid).dir / "refs" / "locs" / lid
    refs = sorted(p.name for p in d.glob("*")
                  if p.is_file() and p.suffix.lower() in _REF_EXTS) \
        if d.is_dir() else []
    manifest = []
    try:
        loc = Bible(pid).locations().get(lid) or {}
        manifest = [r for r in (loc.get("reference_images") or [])
                    if isinstance(r, dict)]
    except Exception:
        manifest = []
    return {"refs": refs, "references": manifest}


@router.get("/api/locrefs/{pid}/{lid}/{file}")
def locref_file(pid: str, lid: str, file: str) -> FileResponse:
    if not is_safe_component(pid) or not is_safe_component(lid):
        raise FileNotFoundError(file)
    p = _ref_path(Bible(pid).dir / "refs" / "locs" / lid, file)
    if p is None or not p.is_file():
        raise FileNotFoundError(file)
    media = _REF_MEDIA.get(p.suffix.lower(), "image/png")
    return FileResponse(str(p), media_type=media)

