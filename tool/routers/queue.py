"""Kinoloom - Unified Task Queue domain router.

Provides endpoints for job queue management, priorities, cancellation, retries,
status tracking, and shot-to-worker assignments.
"""
from __future__ import annotations

import os
import re
import time
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import common
import task_queue as _tq
from core import ROOT, is_safe_component, load_config, log_setup, now, save_config, to_jsonable

router = APIRouter()
log = log_setup("common.queue")

def __getattr__(name: str):
    return getattr(common, name)

class QueueBody(BaseModel):
    kind: str
    label: str = ""
    payload: dict = {}
    worker: str = "local"
    status: str | None = None
    priority: int = 5
    parent_id: str = ""


class QueuePatch(BaseModel):
    label: str | None = None
    worker: str | None = None
    priority: int | None = None
    payload: dict | None = None


class ShotAssignBody(BaseModel):
    project_id: str
    shot_id: str = ""
    worker: str = "local"


_DS_STATUS_MAP = {"queued": "queued", "active": "running", "done": "done",
                  "failed": "failed", "canceled": "canceled",
                  "partial": "failed"}


def _dataset_queue_items(limit: int = 10) -> list[dict]:
    """Dataset batch runs as queue items.

    They live in datasets.db (their own job engine + lanes), NOT in queue.db
    — the Queue tab promised "dataset batches" but never showed them, so a
    running batch looked like an empty queue. Active + recent runs appear
    here with progress and Cancel/Retry actions.
    """
    import datasets_db as dsdb
    runs = dsdb.list_runs()[-limit:]
    runs.reverse()                    # newest first (queue order)
    out: list[dict] = []
    for r in runs:
        try:
            params = json.loads(r.get("params") or "{}")
        except Exception:  # noqa: BLE001
            params = {}
        bits, lanes = [], []
        for m in (params.get("models") or []):
            w = m.get("worker") or "local"
            if w not in lanes:
                lanes.append(w)
            bits.append(f"{m.get('model', '?')}×{m.get('count', 1)}")
        label = (f"{r.get('dataset_type', '?')}/{r.get('dataset_name', '?')}"
                 + (" — " + " · ".join(bits) if bits else ""))
        out.append({
            "id": f"ds:{r['id']}",
            "kind": "dataset",
            "label": label,
            "worker": " + ".join(lanes) if lanes else "local",
            "status": _DS_STATUS_MAP.get(str(r.get("status")),
                                         str(r.get("status"))),
            "progress_done": int(r.get("done") or 0),
            "progress_total": int(r.get("total") or 0),
            "created": r.get("created") or "",
            "error": r.get("error") or "",
            "run_id": r["id"],
        })
    return out


@router.get("/api/queue")
def queue_items(status: str | None = None, kind: str | None = None,
                parent_id: str | None = None, limit: int = 500) -> dict:
    items = _tq.items(status, kind, parent_id, limit)
    try:
        items = _dataset_queue_items() + items
    except Exception as e:  # noqa: BLE001
        log.warning("queue: dataset runs unavailable: %s", e)
    return {"items": items, "summary": _tq.summary()}


@router.get("/api/queue/summary")
def queue_summary() -> dict:
    return _tq.summary()


@router.post("/api/queue/hold-all")
def queue_hold_all(on: bool) -> dict:
    return _tq.hold_all(on)


@router.post("/api/queue")
def queue_add(body: QueueBody) -> dict:
    try:
        iid = _tq.add(body.kind, body.label, body.payload, worker=body.worker,
                      status=body.status, priority=body.priority,
                      parent_id=body.parent_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _tq.item(iid)


@router.patch("/api/queue/{iid}")
def queue_patch(iid: str, body: QueuePatch) -> dict:
    try:
        return _tq.patch(iid, body.label, body.worker, body.priority,
                         body.payload)
    except LookupError:
        raise HTTPException(status_code=404, detail="unknown queue item")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/api/queue/{iid}")
def queue_delete(iid: str) -> dict:
    try:
        return _tq.remove(iid)
    except LookupError:
        raise HTTPException(status_code=404, detail="unknown queue item")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/api/queue/{iid}/{verb}")
def queue_verb(iid: str, verb: str) -> dict:
    fns = {"start": _tq.start, "hold": _tq.hold, "cancel": _tq.cancel,
           "retry": _tq.retry}
    if verb == "remove":
        try:
            return _tq.remove(iid)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    if verb == "raise" or verb == "lower":
        return _tq.bump(iid, -1 if verb == "raise" else 1)
    fn = fns.get(verb)
    if not fn:
        raise HTTPException(status_code=404, detail=f"unknown verb {verb}")
    try:
        return fn(iid)
    except LookupError:
        raise HTTPException(status_code=404, detail="unknown queue item")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/api/queue/shot-assignments")
def shot_assignments_list(project_id: str) -> dict:
    with _tq.connect() as c:
        rows = c.execute(
            "SELECT project_id, shot_id, worker, updated FROM shot_assignments"
            " WHERE project_id=?", (project_id,)).fetchall()
    return {"assignments": [dict(r) for r in rows]}


@router.put("/api/queue/shot-assignments")
def shot_assignments_put(body: ShotAssignBody) -> dict:
    try:
        return _tq.set_shot_worker(body.project_id, body.shot_id, body.worker)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/api/queue/shot-assignments")
def shot_assignments_clear(project_id: str, shot_id: str = "") -> dict:
    return _tq.clear_shot_worker(project_id, shot_id)

