"""Kinoloom - Video Assembly and Outputs domain router.

Provides endpoints for final movie assembly, output directory file listings,
video posters, video deletion, and video clip streaming.
"""
from __future__ import annotations

import ntpath
import os
import re
import time
import json
import hashlib
import ipaddress
import secrets
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import common
from core import ROOT, ffmpeg_exe, InvalidInput, is_safe_component, load_config, log_setup, safe_join

router = APIRouter()
log = log_setup("common.assembly")

class AssemblyBody(BaseModel):
    scenes: list[str] | None = None
    title: str = "movie"
    # a project id: assemble that project's PASSED shots (its own clips, in
    # story order) instead of the legacy output/drafts scene files
    project: str | None = None


@router.post("/api/assembly/run")
def assembly_run(body: AssemblyBody) -> dict:
    # scene ids and the title become file names (temp/<x>.txt, output/scenes,
    # output/films): plain ids only, and no ":" — "C:x" is a drive-relative
    # path on Windows (assembler only strips separators and "..")
    if body.scenes and not all(is_safe_component(str(s)) for s in body.scenes):
        return {"ok": False, "error": "invalid scene id"}
    if ":" in (body.title or "") or "\x00" in (body.title or ""):
        return {"ok": False, "error": "title may not contain ':'"}
    if body.project is not None and not is_safe_component(str(body.project)):
        return {"ok": False, "error": "invalid project id"}
    try:
        if body.project:
            from assembler import project_clips
            clips = project_clips(common.db.all(project=body.project), body.scenes)
            if not clips:
                return {"ok": False, "error": "no passed shots to assemble in the chosen scenes"}
            master = common.assembler.assemble_movie(sorted(clips), body.title, clips=clips,
                                                     ns=body.project)
            return {"ok": True, "master": str(master)}
        scenes = body.scenes or [p.stem for p in
                                 (ROOT / "output" / "scenes").glob("*.mp4")]
        if not scenes:
            scenes = sorted({s["scene"] for s in common.db.all()})
        scenes = [s for s in scenes if is_safe_component(str(s))]
        master = common.assembler.assemble_movie(scenes, body.title)
        return {"ok": True, "master": str(master)}
    except Exception as e:
        log.exception("assembly operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@router.get("/api/outputs")
def outputs() -> dict:
    base = ROOT / "output"
    import time as _t
    def info(d):
        out = []
        if not (base / d).exists():
            return out
        for p in sorted((base / d).glob("*.mp4")):
            try:
                st = p.stat()
                dur = ""
                rc = subprocess.run(
                    [ffmpeg_exe(common.cfg), "-i", str(p)],
                    capture_output=True, text=True)
                m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", rc.stderr)
                if m:
                    hh, mm, ss = m.groups()
                    dur = f"{int(hh):02d}:{int(mm):02d}:{int(float(ss)):02d}"
            except Exception:
                st = p.stat()
                dur = ""
            out.append({"name": p.name, "size_mb": round(st.st_size / 1e6, 1),
                        "date": _t.strftime("%Y-%m-%d %H:%M",
                                            _t.localtime(st.st_mtime)),
                        "duration": dur})
        return out
    raw = []
    if (ROOT / "shots" / "video").exists():
        for p in sorted((ROOT / "shots" / "video").glob("*.mp4")):
            try:
                st = p.stat()
            except Exception:
                continue
            raw.append({"name": p.name,
                        "size_mb": round(st.st_size / 1e6, 1),
                        "date": _t.strftime("%Y-%m-%d %H:%M",
                                            _t.localtime(st.st_mtime)),
                        "duration": ""})
    return {"films": info("films"), "scenes": info("scenes"),
            "drafts": info("drafts"), "raw": raw,
            # Video Studio artefacts (output/videostudio) — listed so they can
            # be played, deleted and sent to a worker for upscaling like any
            # other video.
            "videostudio": info("videostudio")}


@router.get("/api/video/poster")
def video_poster(folder: str, name: str) -> FileResponse:
    import hashlib
    if folder not in ("raw", "drafts", "films", "scenes", "exports") \
            or ".." in name:
        raise FileNotFoundError(name)
    clean_name = ntpath.basename(name.replace("\\", "/").strip())
    if not clean_name or clean_name != name or Path(name).is_absolute():
        raise FileNotFoundError(name)
    base = (ROOT / "shots" / "video").resolve() if folder == "raw" \
        else (ROOT / "output" / folder).resolve()
    p = (base / clean_name).resolve()
    if not p.is_relative_to(base) or not p.is_file():
        raise FileNotFoundError(name)
    key = hashlib.md5(f"{folder}:{name}".encode()).hexdigest()[:12]
    thumb = ROOT / "temp" / f"thumb_{key}.jpg"
    if not thumb.exists() or thumb.stat().st_size < 1000:
        subprocess.run([ffmpeg_exe(common.cfg), "-y", "-v", "error",
                        "-i", str(p), "-frames:v", "1", "-vf",
                        "scale=480:-2", "-q:v", "4", str(thumb)],
                       capture_output=True)
    if not thumb.exists() or thumb.stat().st_size < 1000:
        raise FileNotFoundError(name)
    return FileResponse(str(thumb), media_type="image/jpeg")


class VideoDeleteBody(BaseModel):
    folder: str
    name: str


@router.post("/api/videos/delete")
def videos_delete(body: VideoDeleteBody) -> dict:
    """Delete a video from any listing (raw renders, drafts/films/scenes, and
    Video Studio artefacts). Pipeline *records* are untouched — a deleted file
    simply disappears from the listings and 404s cleanly in players.

    Authorisation is the API security gate's job (local operator or API
    token), like every other mutating route."""
    clean_name = ntpath.basename(body.name.replace("\\", "/").strip())
    if not clean_name or clean_name != body.name or ".." in body.name or Path(body.name).is_absolute():
        raise HTTPException(status_code=400, detail="Path traversal rejected")

    if body.folder == "videostudio":
        import video_studio
        base = video_studio.OUT_DIR.resolve()
    elif body.folder == "raw":
        base = (ROOT / "shots" / "video").resolve()
    elif body.folder in ("drafts", "films", "scenes"):
        base = (ROOT / "output" / body.folder).resolve()
    else:
        return {"ok": False, "error": f"unknown folder '{body.folder}'"}

    p = (base / clean_name).resolve()
    if not p.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Path traversal rejected")
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "not found"}
    p.unlink()
    return {"ok": True}


@router.get("/api/video/{folder}/{name}")
def video(folder: str, name: str) -> FileResponse:
    if folder not in ("raw", "drafts", "films", "scenes", "exports"):
        raise FileNotFoundError(name)
    clean_name = ntpath.basename(name.replace("\\", "/").strip())
    if not clean_name or clean_name != name or ".." in name or Path(name).is_absolute():
        raise HTTPException(status_code=400, detail="Path traversal rejected")
    base = (ROOT / "shots" / "video").resolve() if folder == "raw" \
        else (ROOT / "output" / folder).resolve()
    p = (base / clean_name).resolve()
    if not p.is_relative_to(base) or not p.exists() or not p.is_file():
        raise FileNotFoundError(name)
    return FileResponse(str(p), media_type="video/mp4")


@router.get("/api/clip/{name}")
def video_file(name: str) -> FileResponse:
    """Serve a shot clip by FILE NAME, wherever the pipeline put it.

    The card used `/api/video/raw/<name>`, which only looked in
    shots/video/raw — but renders (and their `_wsup` upscales) live in
    output/videostudio, so the RAW/FINAL boxes silently failed to load even
    though the file existed. (Registered as /api/clip/* because
    /api/video/{folder}/{name} would shadow /api/video/file/{name}.)
    """
    if ".." in name or "/" in name or "\\" in name or not name \
            or Path(name).name != name:          # e.g. "C:x.mp4" (drive-relative)
        raise FileNotFoundError(name)
    cands = [
        ROOT / "output" / "videostudio",     # orchestrator renders
        ROOT / "output" / "raw",
        ROOT / "shots" / "video" / "raw",    # legacy shot clips
        ROOT / "shots" / "video" / "drafts",
        ROOT / "output" / "drafts",
        ROOT / "output" / "films",
    ]
    for d in cands:
        try:
            p = safe_join(d, name)
        except InvalidInput:
            raise FileNotFoundError(name) from None
        if p.is_file():
            return FileResponse(str(p), media_type="video/mp4")
    raise FileNotFoundError(name)

