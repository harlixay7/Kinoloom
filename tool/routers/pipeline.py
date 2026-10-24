"""Kinoloom - Pipeline & Orchestrator domain router.

Provides endpoints for pipeline execution, settings, and orchestrator lifecycle
(start, pause, resume, stop).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import common
from core import is_safe_component, log_setup, save_config

router = APIRouter()
log = log_setup("common.pipeline")


# ------------------------------------------------------------- control
@router.get("/api/pipeline")
def pipeline_status() -> dict:
    return common.pipeline.status()


@router.post("/api/pipeline/run")
def pipeline_run() -> dict:
    return common.pipeline.start()


@router.post("/api/pipeline/stop")
def pipeline_stop() -> dict:
    return common.pipeline.stop()


@router.post("/api/pipeline/settings")
def pipeline_settings(body: dict) -> dict:
    common.cfg["automation"] = {**common.cfg.get("automation", {}), **body}
    save_config(common.cfg)
    common.pipeline.cfg = common.cfg
    return {"ok": True, "automation": common.cfg["automation"]}


class OrchStartBody(BaseModel):
    project: str | None = None


@router.post("/api/orchestrator/start")
def orch_start(body: OrchStartBody | None = None) -> dict:
    """Start rendering, SCOPED to one project.

    The Generate tab is per-project and always sends its pid. Without the
    scope the loop renders EVERY project's queued/pending shots — measured:
    starting a new movie rendered an old project's leftover queue instead
    (the operator watched the wrong story generate).
    """
    pid = ((body.project if body else None) or "").strip() or None
    if pid and not is_safe_component(pid):
        return {"ok": False, "error": "invalid project"}
    common.orch.project_filter = pid
    if not pid:
        log.warning(
            "orchestrator started WITHOUT a project filter — every "
            "project's queue will render"
        )
    common.orch.start()
    return {"ok": True, "project": pid}


@router.post("/api/orchestrator/pause")
def orch_pause() -> dict:
    common.orch.pause()
    return {"ok": True}


@router.post("/api/orchestrator/resume")
def orch_resume() -> dict:
    common.orch.resume()
    return {"ok": True}


@router.post("/api/orchestrator/stop")
def orch_stop() -> dict:
    common.orch.stop()
    return {"ok": True}
