"""Kinoloom - Shots & Bible domain router.

Provides endpoints for workflows, shots querying/ingestion/linting/lifecycle,
script generation, bible data access/saving, git logs, and roster inspection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import common
from core import ROOT, log_setup, resolve_user_path, to_jsonable
from workflows import list_templates

router = APIRouter()
log = log_setup("common.shots_bible")


@router.get("/api/workflows")
def workflows() -> dict:
    return {
        "templates": list_templates(),
        "files": [p.name for p in (ROOT / "tool" / "workflows").glob("*.json")],
    }


# ------------------------------------------------------------- shots
@router.get("/api/shots")
def shots(
    scene: str | None = None,
    status: str | None = None,
    limit: int = 5000,
) -> dict:
    rows = common.db.all(scene=scene, status=status, limit=limit)
    return {"shots": [to_jsonable(r) for r in rows], "stats": common.db.stats()}


class IngestBody(BaseModel):
    shots: list[dict] | None = None
    path: str | None = None


@router.post("/api/shots/ingest")
def ingest(body: IngestBody) -> dict:
    shots_data = body.shots
    if shots_data is None and body.path:
        try:
            p = resolve_user_path(body.path, common.cfg)
        except FileNotFoundError:
            return {"ok": False, "error": f"not found: {body.path}"}
        if p.suffix.lower() != ".json":
            return {"ok": False, "error": "shot list must be a .json file"}
        try:
            shots_data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"unreadable shot list: {e}"}
        if isinstance(shots_data, dict) and "shots" in shots_data:
            shots_data = shots_data["shots"]
    if not shots_data:
        return {"ok": False, "error": "no shots"}
    if not isinstance(shots_data, list) or not all(isinstance(s, dict) for s in shots_data):
        return {"ok": False, "error": "shots must be a list of objects"}
    return {"ok": True, **common.orch.ingest(shots_data)}


@router.post("/api/shots/lint")
def lint(body: IngestBody) -> dict:
    shots_data = body.shots or []
    return {"ok": True, "report": common.orch.lint_only(shots_data)}


class ReqBody(BaseModel):
    shot_id: str
    status: str | None = None
    project_id: str | None = None


@router.post("/api/scriptgen/generate")
def scriptgen_generate() -> dict:
    """Generate a complete linter-valid 2-minute movie package and ingest it."""
    from scriptgen import generate
    package = generate(common.bible)
    result = common.orch.ingest(package["shots"])
    return {
        "ok": result["lint"]["ok"],
        "package": package["summary"],
        "lint": result["lint"],
        "stored": result["stored"],
    }


@router.post("/api/shots/requeue")
def requeue(body: ReqBody) -> dict:
    common.db.set_status(
        body.shot_id,
        body.status or "queued",
        error=None,
        project=getattr(body, "project_id", None),
    )
    return {"ok": True}


@router.post("/api/shots/delete")
def delete(body: ReqBody) -> dict:
    common.db.delete_shot(body.shot_id, getattr(body, "project_id", None))
    return {"ok": True}


# ------------------------------------------------------------- bible
@router.get("/api/bible")
def get_bible() -> dict:
    return to_jsonable(common.bible.all())


class YmlBody(BaseModel):
    kind: str      # character | location | scene | universe | crowd
    id: str | None = None
    yaml: str | None = None
    data: dict | None = None


@router.post("/api/bible/save")
def bible_save(body: YmlBody) -> dict:
    import yaml
    if body.yaml:
        data = yaml.safe_load(body.yaml) or {}
    else:
        data = body.data or {}
    if body.kind == "character":
        if "id" not in data and body.id:
            data["id"] = body.id
        if not data.get("id"):
            return {"ok": False, "error": "id required"}
        common.bible.save_character(data)
    elif body.kind == "location":
        common.bible.save_location(data)
    elif body.kind == "scene":
        common.bible.save_scene(data)
    elif body.kind == "universe":
        common.bible.save_universe(data)
    elif body.kind == "crowd":
        common.bible.save_crowd_types(data)
    else:
        return {"ok": False, "error": f"unknown kind {body.kind}"}
    return {"ok": True, "commit": common.bible._head()}


@router.get("/api/bible/log")
def bible_log() -> dict:
    return {"log": common.bible.git_log()}


@router.get("/api/roster")
def roster() -> dict:
    base = ROOT / "roster"
    out = {}
    for d in base.iterdir():
        if d.is_dir():
            out[d.name] = (
                sorted(f.name for f in d.iterdir())
                if any(f.is_file() for f in d.iterdir())
                else []
            )
    return {"roster": out}
