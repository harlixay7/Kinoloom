"""Kinoloom - Shared singletons, state, and lifecycle routines.

Houses shared state and singletons to decouple domain APIRouters from tool/api.py.
"""
from __future__ import annotations

import os as _os
import json
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from assembler import Assembler
from bible import Bible
from core import (ROOT, is_safe_component, jload, jsave, load_config, now, save_config,
                  to_jsonable, log_setup)
from face_qa import FaceQA
from linter import Linter
from orchestrator import Orchestrator
from post import PostPipeline
from prompts import PromptBuilder
from renderer import Renderer
from shots_db import ShotsDB
from support import RenderMode, versions
from workflows import WorkflowBuilder, list_templates
from pipeline import Pipeline
from training import TrainingRunner, dataset_dirs, curate_save, VALID_TYPES
from lora_registry import LoRARegistry
from photo_gen import PhotoGen
from dataset_gen import DatasetGen
from images import ImageGen, PRESETS
from models import (GEN_LORAS, LORA_TRIGGER_EXTRA, all_model_info)
import datasets_db
import jobs
import shot_images
import video_studio
from shot_images import ShotImageGen

from director import Director
from keyframe_gen import KeyframeGen
from lora_video import VideoLoraRegistry
from projects import ProjectStore
from storygen_llm import ArchitectJob
from batch_gen import BatchGen
from cast_import import import_batch as _import_batch
from cast_import import _identity_text, _outfit_text
import task_queue as _tq
import queue_adapters as _qa
import gpu_broker as _gpu

log = log_setup("api.common")

_REF_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_REF_MAX_BYTES = 25 * 1024 * 1024
_REF_MEDIA = {".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".webp": "image/webp"}
CHAR_TIERS_CAST = ("main", "supporting", "recurring", "crowd")

STATIC = ROOT / "tool" / "static"

# ------------------------------------------------------------- singletons
cfg = load_config()
bible = Bible()
db = ShotsDB()
builder = PromptBuilder(bible)
linter = Linter(bible)
renderer = Renderer(cfg)
wf_builder = WorkflowBuilder()
orch = Orchestrator(bible, db, renderer, builder, linter)
post_pipe = PostPipeline(cfg)
face_qa = FaceQA(cfg)
assembler = Assembler(cfg)
render_mode = RenderMode(cfg)
pipeline = Pipeline(orch, db, assembler)
trainer = TrainingRunner(cfg)
lora_registry = LoRARegistry(cfg)
photo_gen = PhotoGen()
dataset_gen = DatasetGen()
image_gen = ImageGen()
shot_image_gen = ShotImageGen(cfg)
store = ProjectStore(db)
keyframe_gen = KeyframeGen(cfg)
video_lora_reg = VideoLoraRegistry(cfg)
director = Director(cfg, db, store, orch, assembler)
architect_job = ArchitectJob(cfg, db, store)
batch_gen = BatchGen(cfg, db)


def _proj_guard(pid: str):
    """Validate pid + project existence; returns (project|None, error)."""
    if not is_safe_component(pid):
        return None, {"ok": False, "error": "invalid project id"}
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    return proj, None


def _shot_detail(row: dict, pid: str | None = None) -> dict:
    sid = row["shot_id"]
    pid = pid or row.get("project_id")
    out = dict(row)
    kf = keyframe_gen.candidates(sid, pid)
    from continuity import _ns
    tail = ROOT / "shots" / "tails" / _ns(pid) / f"{sid}_tail.png"
    out["keyframes"] = kf
    out["tail_frame"] = str(tail) if tail.exists() else None
    kfdir = shot_images.KF_ROOT / shot_images._ns(pid) / sid
    out["img_files"] = sorted(p.name for p in kfdir.glob("img_*.png")) \
        if kfdir.is_dir() else []
    prev = db.get(row["json"].get("prev_shot"), pid) \
        if row["json"].get("prev_shot") else None
    from continuity import continuity_notes
    out["continuity_note"] = continuity_notes(row["json"], prev)
    return to_jsonable(out)


EVENTS: list[dict] = []


def _on_job(evt: dict) -> None:
    EVENTS.append({"ts": now(), **evt})
    del EVENTS[:-200]


orch.job_callbacks.append(_on_job)


def _recover_stuck_shots() -> None:
    """If we restart mid-run, shots stuck in rendering/qa/drafting go back to
    queued — the queue is resume-safe. 'rendered' and 'post' shots are left
    to Orchestrator._recover_post_backlog, which re-runs post on the raw
    render when it still exists instead of rendering the shot again."""
    try:
        for s in db.all():
            if s["status"] in ("rendering", "qa", "drafting"):
                db.set_status(s["shot_id"], "queued",
                              project=s.get("project_id"))
                print(f"recovered stuck shot {s['shot_id']} -> queued")
    except Exception as e:
        print(f"recovery pass failed: {e}")
    try:
        datasets_db.init()
        n = datasets_db.recover_runs()
        jobs.enqueue_pending()   # re-queue recovered runs (resume-safe)
        datasets_db.backfill_all()
        jobs.start_worker()
        print(f"dataset jobs recovered ({n}) + worker started")
    except Exception as e:
        print(f"dataset job recovery failed: {e}")


def _queue_boot() -> None:
    """Boots queue.db, reaps crashed items everywhere, wires the
    engine adapters to the singletons above, and starts the dispatcher."""
    try:
        # reconcile per settings (gpu_broker.reconcile_on_start, default on);
        # video_studio's reaper below makes the same decision
        _gpu.start()
    except Exception as e:
        print(f"gpu broker boot failed (unbrokered fallback): {e}")
    try:
        _tq.init()
        _tq.reap_on_startup()
        import video_studio
        video_studio.reap_stale_running()
        _tq.init_engines(image_gen=image_gen, batch_gen=batch_gen)
        _qa.register_all()
        _tq._engines["worker_cancel"] = _qa.make_worker_cancel()
        _tq.start_dispatcher()
    except Exception as e:
        print(f"queue boot failed (generation still works): {e}")
