"""Kinoloom - Images domain router.

Provides endpoints for image generation, upscale state/files, models catalog,
skills documentation, prompt specifications, and image gallery references/albums.
"""
from __future__ import annotations

import shutil
import time as _t
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import common
from core import ROOT, InvalidInput, log_setup, safe_join, save_config
from images import GALLERY, PRESETS
from models import GEN_LORAS, LORA_TRIGGER_EXTRA, all_model_info
from post import PostPipeline

router = APIRouter()
log = log_setup("common.images")

_REF_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_REF_MAX_BYTES = 25 * 1024 * 1024
_REF_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

_UPSCALE_METHODS = ("none", "esrgan", "seedvr2", "lanczos")


# --------------------------------------------------- upscale toggle (UI)
@router.get("/api/upscale/state")
def upscale_state() -> dict:
    tiers = common.cfg.get("tiers", {}).get("upscale", {}) or {}
    method = tiers.get("hero") or "none"
    return {
        "enabled": method not in ("none", "", None),
        "method": method,
        "tiers": {k: tiers.get(k) for k in ("hero", "standard", "volume")},
        "methods": list(_UPSCALE_METHODS),
        "cost_note": {
            "esrgan": "~405 s / 121 frames — AI upscale, no flicker",
            "seedvr2": "~1064 s / 121 frames — AI upscale, temporally consistent",
            "lanczos": "instant — plain resize, adds NO detail",
        },
    }


class UpscaleBody(BaseModel):
    enabled: bool
    method: str = "esrgan"


@router.post("/api/upscale/state")
def set_upscale(body: UpscaleBody) -> dict:
    method = body.method if body.enabled else "none"
    if method not in _UPSCALE_METHODS:
        return {"ok": False, "error": f"unknown method {method!r}"}
    ups = common.cfg.setdefault("tiers", {}).setdefault("upscale", {})
    for t in ("hero", "standard", "volume"):
        ups[t] = method
    save_config(common.cfg)
    common.post_pipe = PostPipeline(common.cfg)
    return {"ok": True, "enabled": body.enabled, "method": method}


# ------------------------------------------------------------- skills
_SKILLS_DIR = ROOT / "tool" / "skills"


@router.get("/api/skills")
def skills_get(name: str | None = None) -> dict:
    """The pipeline skill documents (storytelling / pipeline) as text."""
    out: dict = {}
    if not _SKILLS_DIR.is_dir():
        return {"ok": False, "error": "skills folder missing", "skills": out}
    names = (
        [name]
        if name
        else sorted(p.name for p in _SKILLS_DIR.iterdir() if p.is_dir())
    )
    for n in names:
        p = _SKILLS_DIR / n / "SKILL.md"
        if p.is_file():
            try:
                out[n] = p.read_text(encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                out[n] = f"(unreadable: {e})"
    return {"ok": bool(out), "skills": out}


# ------------------------------------------------------------- images
class ImagesBody(BaseModel):
    prompt: str
    model: str = "realvis"
    width: int = 1024
    height: int = 1024
    steps: int | None = None
    cfg: float | None = None
    seed: int | None = None
    count: int = 1
    negative: str | None = None
    album: str = ""
    loras: list[dict] = []
    hires: bool = False
    refs: list[dict] = []
    ref_strength: float = 0.8
    anchor: bool = False
    worker: str = "local"


@router.get("/api/images/stacks")
def images_stacks() -> dict:
    return {
        "stacks": {m["id"]: m for m in all_model_info()},
        "presets": PRESETS,
    }


@router.get("/api/models")
def api_models() -> dict:
    return {
        "models": all_model_info(),
        "loras": GEN_LORAS,
        "extra_negative": LORA_TRIGGER_EXTRA,
    }


@router.get("/api/promptspec")
def api_promptspec() -> dict:
    """The structured cinematic prompt engine catalogs."""
    import promptspec as ps

    out = {
        k: v
        for k, v in vars(ps).items()
        if k.isupper() and isinstance(v, (dict, list))
    }
    out["PRESETS"] = ps.SYSTEM_PRESETS
    out["EXPRESSIONS"] = ps.EXPRESSIONS
    out["EMOTION_LOOKUP"] = ps._EMOTION_LOOKUP
    return out


@router.get("/api/images/loras")
def images_loras() -> dict:
    return {"loras": GEN_LORAS, "extra_negative": LORA_TRIGGER_EXTRA}


def _worker_missing_model_files(wid: str, model_id: str) -> list[str]:
    """Model files the worker's inventory does not contain (basenames)."""
    try:
        import models as _models
        import workers as _wm
        from workflows import SUBST

        inv = (_wm.get(wid) or {}).get("models") or {}
        have = {f for files in inv.values() for f in files}
        m = _models.get_model(model_id) or {}
        need: list[str] = []
        for v in (m.get("files") or {}).values():
            name = str(v or "")
            if name in SUBST:
                name = str(SUBST[name])
            if (
                name
                and name.endswith(
                    (".gguf", ".safetensors", ".pth", ".ckpt", ".pt")
                )
                and name not in have
            ):
                need.append(name)
        return need
    except Exception:
        return []


@router.post("/api/images/generate")
def images_generate(body: ImagesBody) -> dict:
    if body.worker and body.worker != "local":
        miss = _worker_missing_model_files(body.worker, body.model)
        if miss:
            import workers as _wm

            name = (_wm.get(body.worker) or {}).get("name") or body.worker
            return {
                "ok": False,
                "error": (
                    f"{name} is missing {len(miss)} model file(s) for "
                    f"'{body.model}': {', '.join(miss[:4])}"
                    f"{'…' if len(miss) > 4 else ''}. Add them "
                    f"on the Worker PCs page (Add models)."
                ),
            }
    return common.image_gen.start(
        body.prompt,
        body.model,
        body.width,
        body.height,
        body.steps,
        body.cfg,
        body.seed,
        body.count,
        body.negative,
        body.album,
        body.loras,
        body.hires,
        body.refs,
        body.ref_strength,
        body.anchor,
        body.worker,
    )


class UpscaleFileBody(BaseModel):
    folder: str = "raw"
    name: str
    worker: str
    rife: bool | None = None


@router.post("/api/upscale/file")
def upscale_file(body: UpscaleFileBody) -> dict:
    """Send an EXISTING video to a worker PC for RIFE + RealESRGAN upscaling."""
    import post

    if body.folder == "videostudio":
        import video_studio

        base = video_studio.OUT_DIR
    elif body.folder == "raw":
        base = ROOT / "shots" / "video"
    elif body.folder in ("drafts", "films", "scenes"):
        base = ROOT / "output" / body.folder
    else:
        return {"ok": False, "error": f"unknown folder '{body.folder}'"}
    try:
        p = safe_join(base, body.name)
    except InvalidInput:
        return {"ok": False, "error": "bad name"}
    if not p.is_file():
        return {"ok": False, "error": "video not found"}
    if not body.worker or body.worker == "local":
        return {"ok": False, "error": "pick a worker PC for the upscale"}
    out = post.PostPipeline(common.cfg).upscale_remote(
        p, body.worker, None, body.rife
    )
    if out is None:
        return {
            "ok": False,
            "error": (
                f"worker '{body.worker}' unavailable or the job "
                f"failed — nothing changed"
            ),
        }
    final = p.with_name(f"{p.stem}_upscaled.mp4")
    try:
        if out.resolve() != final.resolve():
            if final.exists():
                final.unlink()
            out.replace(final)
    except Exception as e:  # noqa: BLE001
        log.exception("upscale replace failed: %s", e)
        return {
            "ok": True,
            "name": out.name,
            "note": f"kept as {out.name} ({e})",
        }
    return {"ok": True, "name": final.name, "folder": body.folder}


@router.post("/api/images/refs/upload")
async def images_refs_upload(request: Request) -> dict:
    """Upload reference images for the Images tab (SDXL IP-Adapter)."""
    form = await request.form()
    d = GALLERY / "refs"
    d.mkdir(parents=True, exist_ok=True)
    saved = []
    for fname, field in form.multi_items():
        if fname not in (
            "files",
            "file",
            "images",
            "image",
            "ref",
            "refs",
        ) or not hasattr(field, "read"):
            continue
        data = await field.read()
        if not data or len(data) > _REF_MAX_BYTES:
            continue
        ext = Path(getattr(field, "filename", "") or "x.png").suffix.lower()
        if ext not in _REF_EXTS:
            continue
        fn = f"ref_{int(_t.time() * 1000)}_{len(saved)}{ext}"
        (d / fn).write_bytes(data)
        saved.append(fn)
    if not saved:
        return {
            "ok": False,
            "refs": [],
            "error": (
                "No valid image files uploaded (png, jpg, webp up to 25MB supported)"
            ),
        }
    return {"ok": True, "refs": saved}


@router.post("/api/images/refs/from-gallery")
def images_refs_from_gallery(body: dict) -> dict:
    """Copy a gallery image into the refs directory for reference generation."""
    file = str(body.get("file") or "").strip()
    if not file or ".." in file or "/" in file or "\\" in file or ":" in file:
        return {"ok": False, "error": "invalid file"}
    try:
        src = safe_join(GALLERY, file)
    except InvalidInput:
        return {"ok": False, "error": "invalid file"}
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": "source image not found"}
    ext = src.suffix.lower()
    if ext not in _REF_EXTS:
        return {"ok": False, "error": "unsupported image type"}
    d = GALLERY / "refs"
    d.mkdir(parents=True, exist_ok=True)
    fn = f"ref_{int(_t.time() * 1000)}_{src.stem[:24]}{ext}"
    shutil.copy2(src, d / fn)
    return {"ok": True, "ref": fn}


@router.get("/api/images/refs")
def images_refs() -> dict:
    d = GALLERY / "refs"
    if not d.is_dir():
        return {"refs": []}
    return {
        "refs": sorted(
            p.name
            for p in d.glob("*")
            if p.is_file() and p.suffix.lower() in _REF_EXTS
        )
    }


@router.get("/api/images/refs/{file}")
def images_refs_file(file: str) -> FileResponse:
    if ".." in file or "/" in file or "\\" in file:
        raise FileNotFoundError(file)
    p = GALLERY / "refs" / file
    if not p.exists():
        raise FileNotFoundError(file)
    media = _REF_MEDIA.get(p.suffix.lower(), "image/png")
    return FileResponse(str(p), media_type=media)


@router.post("/api/images/refs/delete")
def images_refs_delete(body: dict) -> dict:
    file = str(body.get("file") or "")
    if ".." in file or "/" in file or "\\" in file:
        return {"ok": False, "error": "invalid file"}
    p = GALLERY / "refs" / file
    if not p.exists():
        return {"ok": False, "error": "not found"}
    p.unlink()
    return {"ok": True}


@router.get("/api/images/status")
def images_status() -> dict:
    return {"gen": common.image_gen.active}


@router.post("/api/images/stop")
def images_stop() -> dict:
    common.image_gen.stop()
    return {"ok": True}


@router.get("/api/images/list")
def images_list() -> dict:
    return common.image_gen.list()


class ImagesAlbumBody(BaseModel):
    action: str  # create | rename | delete
    title: str = ""
    old: str = ""
    new: str = ""


@router.post("/api/images/albums")
def images_albums(body: ImagesAlbumBody) -> dict:
    if body.action == "create":
        return common.image_gen.create_album(body.title)
    if body.action == "rename":
        return common.image_gen.rename_album(body.old, body.new)
    if body.action == "delete":
        return common.image_gen.delete_album(body.title)
    return {"ok": False, "error": f"unknown action '{body.action}'"}


class ImagesAssignBody(BaseModel):
    name: str
    album: str = ""


@router.post("/api/images/assign")
def images_assign(body: ImagesAssignBody) -> dict:
    return common.image_gen.assign(body.name, body.album)


class ImagesDeleteBody(BaseModel):
    name: str


@router.post("/api/images/delete")
def images_delete(body: ImagesDeleteBody) -> dict:
    return {"ok": common.image_gen.delete(body.name)}


@router.get("/api/images/file")
def images_file(name: str) -> FileResponse:
    p = GALLERY / name
    if ".." in name or "/" in name or "\\" in name or not p.exists():
        raise FileNotFoundError(name)
    return FileResponse(str(p), media_type="image/png")
