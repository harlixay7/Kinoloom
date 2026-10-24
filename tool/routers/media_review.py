"""Kinoloom - Video LoRAs, Shot Review & Keyframes domain router.

Provides endpoints for video LoRAs registry, approval/rejection review queue,
keyframe image serving, video tail frames, and identity picking.
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
from .common import video_lora_reg, db, store, orch, cfg, _shot_detail
from core import (ROOT, InvalidInput, is_safe_component, load_config, log_setup, now,
                  safe_join, save_config, to_jsonable)
from lora_video import VideoLoraRegistry

router = APIRouter()
log = log_setup("common.media_review")

def __getattr__(name: str):
    return getattr(common, name)


def _file_in(folder: Path, file: str) -> Path | None:
    """`folder/file` for a bare client-supplied file name, else None (no
    separators, drive letters such as "C:x", "."/"..")."""
    if not file or Path(file).name != file:
        return None
    try:
        return safe_join(folder, file)
    except InvalidInput:
        return None

@router.get("/api/lora/video")
def lora_video_all() -> dict:
    try:
        video_lora_reg.scan()
    except Exception as e:
        log.exception("media_review operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e), "entries": []})
    return {"entries": video_lora_reg.entries()}


class LoraVideoEditBody(BaseModel):
    display: str | None = None
    category: str | None = None
    trigger: str | None = None
    strength: float | None = None
    steps: int | None = None
    cfg: float | None = None
    enabled: bool | None = None
    notes: str | None = None


@router.post("/api/lora/video/{ident}")
def lora_video_edit(ident: str, body: LoraVideoEditBody) -> dict:
    kw = {k: v for k, v in body.dict().items() if v is not None}
    return {"ok": video_lora_reg.update(ident, **kw)}


@router.post("/api/lora/video/rescan")
def lora_video_rescan() -> dict:
    return video_lora_reg.rescan()


@router.get("/api/review")
def review_all() -> dict:
    rows = [r for r in db.all(status="review") if r.get("project_id")]
    out = []
    for r in rows:
        out.append(_shot_detail(r))
    return {"review": out}


class ReviewApproveBody(BaseModel):
    shot_id: str
    project_id: str | None = None


@router.post("/api/review/approve")
def review_approve(body: ReviewApproveBody) -> dict:
    db.set_status(body.shot_id, "passed", qa_stage=0,
                  project=body.project_id)
    return {"ok": True}


@router.post("/api/review/requeue")
def review_requeue(body: ReviewApproveBody) -> dict:
    db.set_status(body.shot_id, "queued", qa_stage=0,
                  project=body.project_id)
    return {"ok": True}


class RequeueAllBody(BaseModel):
    project_id: str | None = None
    statuses: list[str] | None = None    # default: review + failed


@router.post("/api/review/requeue-all")
def review_requeue_all(body: RequeueAllBody) -> dict:
    """Requeue every stuck shot (review/failed) — optionally one project.

    After a pipeline fix the operator should not have to click Continue on
    twenty cards: this clears them all in one action; the running
    orchestrator picks them up.
    """
    want = set(body.statuses or ("review", "failed"))
    n = 0
    for r in db.all(project=body.project_id):
        if r.get("status") in want:
            db.set_status(r["shot_id"], "queued", qa_stage=0,
                          project=r.get("project_id"))
            n += 1
    return {"ok": True, "requeued": n}


@router.get("/api/keyframes/{pid}/{sid}/{file}")
def keyframe_file(pid: str, sid: str, file: str) -> FileResponse:
    if ".." in pid or ".." in sid or ".." in file or "/" in file \
            or "\\" in file or not is_safe_component(sid):
        raise FileNotFoundError(file)
    from continuity import _ns
    # tolerant lookup: namespaced dir first, then legacy layouts (files
    # written by an older server process are still served)
    cands = [
        ROOT / "shots" / "keyframes" / _ns(pid or None) / sid,
        ROOT / "shots" / "keyframes" / "_" / sid,
        ROOT / "shots" / "keyframes" / sid,
    ]
    for d in cands:
        p = _file_in(d, file)
        if p is not None and p.is_file():
            return FileResponse(str(p), media_type="image/png")
    raise FileNotFoundError(file)


@router.get("/api/keyframes/{sid}/{file}")
def keyframe_file_legacy(sid: str, file: str) -> FileResponse:
    """Legacy (project-less) namespace — kept for old URLs."""
    if ".." in sid or ".." in file or "/" in file or "\\" in file \
            or not is_safe_component(sid):
        raise FileNotFoundError(file)
    for d in (ROOT / "shots" / "keyframes" / "_" / sid,
              ROOT / "shots" / "keyframes" / sid):
        p = _file_in(d, file)
        if p is not None and p.is_file():
            return FileResponse(str(p), media_type="image/png")
    raise FileNotFoundError(file)


@router.get("/api/tails/{pid}/{sid}")
def tail_file(pid: str, sid: str) -> FileResponse:
    # is_safe_component also refuses "C:" (a drive-relative join on Windows)
    if ".." in pid or ".." in sid or "/" in sid or "\\" in sid \
            or not is_safe_component(sid):
        raise HTTPException(status_code=400, detail="invalid id")
    from continuity import _ns
    for p in (ROOT / "shots" / "tails" / _ns(pid or None) / f"{sid}_tail.png",
              ROOT / "shots" / "tails" / "_" / f"{sid}_tail.png",
              ROOT / "shots" / "tails" / f"{sid}_tail.png"):
        if p.exists():
            return FileResponse(str(p), media_type="image/png")
    # a missing tail is normal (shot not rendered yet / CUT) — 404, never a
    # 500 with a traceback
    raise HTTPException(status_code=404, detail=f"no tail frame for {sid}")


@router.get("/api/tails/{sid}")
def tail_file_legacy(sid: str) -> FileResponse:
    if ".." in sid or "/" in sid or "\\" in sid or not is_safe_component(sid):
        raise HTTPException(status_code=400, detail="invalid id")
    for p in (ROOT / "shots" / "tails" / "_" / f"{sid}_tail.png",
              ROOT / "shots" / "tails" / f"{sid}_tail.png"):
        if p.exists():
            return FileResponse(str(p), media_type="image/png")
    raise HTTPException(status_code=404, detail=f"no tail frame for {sid}")


@router.get("/api/pick/images")
def pick_images(type: str = "face") -> dict:
    """Browse training batches (faces / environments / powers / weapons /
    outfits) to pick reference photos (batch -> photo selection).

    Each dataset also carries per-file metadata (`meta`) for the stills that
    recorded weapon/outfit/handling variations — the picker shows chips and
    can filter armed vs plain composites instead of guessing visually.
    """
    from training import dataset_dirs
    if type not in ("face", "environment", "power", "weapon", "outfit"):
        return {"ok": False,
                "error": "type must be face|environment|power|weapon|outfit"}
    ds = [d for d in dataset_dirs() if d.get("type") == type]
    out = []
    for d in ds:
        meta: dict = {}
        try:
            mp = (ROOT / "roster" / "datasets" / d["type"] / d["name"]
                  / "meta.json")
            if mp.is_file():
                raw = json.loads(mp.read_text(encoding="utf-8")) or {}
                for f, m in raw.items():
                    v = (m or {}).get("variations") or {}
                    keep = {k: v[k] for k in ("weapon", "outfit", "handling",
                                              "level")
                            if v.get(k) not in (None, "")}
                    if keep:
                        meta[f] = keep
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({"type": d["type"], "name": d["name"],
                    "candidates": d.get("candidates", []),
                    "thumbnail": d.get("thumbnail", ""),
                    "kept": d.get("kept"), "rejected": d.get("rejected"),
                    "meta": meta})
    return {"datasets": out}

