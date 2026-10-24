"""Kinoloom - System domain router.

Provides endpoints for UI front door, workspace aliases, system status,
configuration settings, and log file retrieval.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import common
import security
from assembler import Assembler
from core import ROOT, load_config, log_setup, now, save_config, to_jsonable
from face_qa import FaceQA
from post import PostPipeline
from renderer import Renderer
from support import RenderMode, versions

router = APIRouter()
log = log_setup("common.system")


@router.get("/")
def index() -> FileResponse:
    """Front door: the filmmaking workspace."""
    return FileResponse(common.STATIC / "project.html")


@router.get("/project")
def project_ui() -> FileResponse:
    """Workspace alias (original path, kept working)."""
    return FileResponse(common.STATIC / "project.html")


@router.get("/studio-tools")
def studio_tools() -> FileResponse:
    """Legacy tool shell: batches, training, videos, logs."""
    return FileResponse(common.STATIC / "index.html")


# ------------------------------------------------------------- system
@router.get("/api/status")
def status() -> dict:
    return {
        "time": now(),
        "orchestrator": common.orch.status(),
        "gpu": common.render_mode.health(),
        "versions": versions(),
        "events": common.EVENTS[-30:],
        "bible_commit": common.bible._head(),
    }


# ------------------------------------------------------------- settings
# Sections every engine reads unconditionally; a settings document without
# them is a client mistake (e.g. a pasted fragment), not a valid config.
_REQUIRED_SECTIONS = ("paths", "ensemble")


@router.get("/api/settings")
def get_settings() -> dict:
    """Current settings with every secret replaced by a placeholder."""
    return to_jsonable(security.redact(common.cfg))


class SettingsBody(BaseModel):
    settings: dict


@router.post("/api/settings")
def set_settings(body: SettingsBody) -> dict:
    """Replace the whole settings document (the UI's raw editor).

    Placeholders for secrets that came from GET /api/settings are swapped
    back for the stored values. The live dict is updated in place, so every
    module holding a reference sees the new settings at once."""
    new = copy.deepcopy(body.settings)
    missing = [s for s in _REQUIRED_SECTIONS if not isinstance(new.get(s), dict)]
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"settings are missing required section(s): {', '.join(missing)}")
    security.merge_secrets(new, common.cfg)
    try:
        yaml.safe_dump(new)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=422, detail=f"settings are not serialisable: {e}")
    save_config(new)                  # updates common.cfg in place
    common.renderer = Renderer(common.cfg)
    common.orch.renderer = common.renderer
    common.post_pipe = PostPipeline(common.cfg)
    common.render_mode = RenderMode(common.cfg)
    common.face_qa = FaceQA(common.cfg)
    common.assembler = Assembler(common.cfg)
    return {"ok": True}


# ------------------------------------------------------------- logs
@router.get("/api/logs")
def logs(name: str = "studio") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name):
        return {"lines": [], "error": "invalid log name"}
    p = ROOT / "logs" / f"{name}.log"
    if not p.exists():
        return {"lines": []}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": lines[-200:]}
