"""Kinoloom - Project Shots & Scene Pipeline domain router.

Provides endpoints for project shots querying/inspection, prompt editing, keyframe
generation/approval, shot rerender/continuation, continuity tracking, per-shot reference choices,
image generation planning, scenes listing, ETA calculation, and run/stop/resume execution.
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import common
from .common import (
    db, keyframe_gen, shot_images, video_lora_reg, director, store,
    shot_image_gen, Bible, _REF_MAX_BYTES, _REF_EXTS, _REF_MEDIA, _shot_detail,
    cfg
)
from core import ROOT, is_safe_component, load_config, log_setup, now, save_config, to_jsonable

router = APIRouter()
log = log_setup("common.project_shots")

def __getattr__(name: str):
    return getattr(common, name)

@router.get("/api/projects/{pid}/scenes")
def project_scenes(pid: str) -> dict:
    from bible import Bible
    return {"scenes": Bible(pid).scenes(),
            "shots": [to_jsonable(r) for r in db.all(project=pid)]}


@router.get("/api/projects/{pid}/shots")
def project_shots(pid: str) -> dict:
    return {"shots": [to_jsonable(r) for r in db.all(project=pid)],
            "stats": db.project_stats(pid)}


@router.get("/api/projects/{pid}/shots/{sid}")
def project_shot_get(pid: str, sid: str) -> dict:
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    return {"shot": _shot_detail(row, pid)}


class ShotPromptBody(BaseModel):
    prompt: str = ""
    negative: str = ""
    image: str | None = None             # the opening image's prompt (LTX-2.5 / H3)


def _movie_model(row: dict, pid: str) -> str:
    """The LTX-2.5 / H3 model the shot renders with; "" for Wan 2.2."""
    from movie_request import NoVideoModel, shot_model
    j = row.get("json")
    try:
        return shot_model(db, row, j if isinstance(j, dict) else {}, pid)
    except NoVideoModel:
        return ""


@router.post("/api/projects/{pid}/shots/{sid}/prompt")
def project_shot_prompt(pid: str, sid: str, body: ShotPromptBody) -> dict:
    """Edit a shot's prompt and render it again with it. An LTX-2.5 / H3
    shot's prompt is checked for the request it renders with first
    (movie_prompts.save_hand); a Wan 2.2 prompt overrides the built one."""
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    if _movie_model(row, pid):
        import movie_prompts
        res = movie_prompts.save_hand(db, Bible(pid), pid, sid, body.prompt, image=body.image)
        if res.get("ok") and row.get("status") != "rendering":
            db.set_status(sid, "queued", project=pid)
        return res
    db.set_status(sid, "queued", prompt=body.prompt, negative=body.negative,
                  qa_stage=max(1, row.get("qa_stage") or 0), project=pid)
    return {"ok": True}


class KeyframeGenBody(BaseModel):
    count: int = 2


@router.post("/api/projects/{pid}/shots/{sid}/keyframes/generate")
def project_keyframe_generate(pid: str, sid: str,
                              body: KeyframeGenBody) -> dict:
    from bible import Bible
    from lora_video import resolve_chain
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    sjson = row.get("json") or {}
    p_bible = Bible(pid)
    reg = video_lora_reg
    try:
        reg.scan()
    except Exception:
        pass
    chain = resolve_chain(sjson, p_bible, reg, is_i2v=True)
    res = keyframe_gen.generate_candidates(
        sjson, p_bible, chain, count=body.count,
        base_seed=db.assign_seed(sjson) + 500)
    return res


@router.get("/api/projects/{pid}/shots/{sid}/keyframes")
def project_keyframes(pid: str, sid: str) -> dict:
    return {"keyframes": keyframe_gen.candidates(sid, pid)}


class KfApproveBody(BaseModel):
    file: str


@router.post("/api/projects/{pid}/shots/{sid}/keyframes/approve")
def project_keyframe_approve(pid: str, sid: str,
                             body: KfApproveBody) -> dict:
    fname = Path(body.file).name
    if ".." in body.file or fname in ("", ".", "..") \
            or not is_safe_component(sid):
        return {"ok": False, "error": "invalid file"}
    ok = keyframe_gen.approve(sid, body.file, pid)
    if ok:
        from keyframe_gen import _kf_dir
        kf_dir = _kf_dir(sid, pid)
        db.set_status(sid, "queued", keyframe_path=str(kf_dir / fname),
                      project=pid)
    return {"ok": ok}


@router.post("/api/projects/{pid}/shots/{sid}/keyframes/regenerate")
def project_keyframe_regenerate(pid: str, sid: str) -> dict:
    # clear() rmtree's shots/keyframes/<pid>/<sid>: the id must be a plain
    # component (keyframe_gen._kf_dir enforces it too)
    if not is_safe_component(sid):
        return {"ok": False, "error": "invalid shot id"}
    keyframe_gen.clear(sid, pid)
    db.set_status(sid, "queued", keyframe_path=None, project=pid)
    return {"ok": True}


class RerenderBody(BaseModel):
    profile: str = ""


@router.post("/api/projects/{pid}/shots/{sid}/rerender")
def project_shot_rerender(pid: str, sid: str, body: RerenderBody) -> dict:
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    kw: dict[str, Any] = {"status": "queued", "qa_stage": 0, "prompt": None, "negative": None}
    if _movie_model(row, pid):
        # LTX-2.5 / H3: the same written prompt, the next seed (a failed
        # render's retries keep theirs, so a take reuses its finished steps)
        from orchestrator import REROLL_SEED
        seed = int(row["seed"]) if row.get("seed") is not None else db.stable_seed(row)
        kw["seed"] = (seed + REROLL_SEED) % (2 ** 31)
    if body.profile:
        sjson = dict(row.get("json") or {})
        sjson["render_profile"] = body.profile
        db.set_json(sid, sjson, project=pid)
        kw["render_profile"] = body.profile
    db.set_status(sid, project=pid, **kw)
    return {"ok": True}


class DryRunBody(BaseModel):
    # only report the shots with a clip the change renders again
    dry_run: bool = False


def _rerenders(pid: str, sid: str, sjson: dict) -> list[str]:
    try:
        import movie_prompts
        return movie_prompts.rerenders(db, pid, sid, sjson)
    except Exception as e:  # noqa: BLE001 - a planning problem shows on the Shots page
        log.warning(f"{sid}: shots rendered again not known: {e}")
        return []


@router.post("/api/projects/{pid}/shots/{sid}/continue")
def project_shot_continue(pid: str, sid: str, body: DryRunBody | None = None) -> dict:
    """CONTINUE from the previous shot's tail frame."""
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    sjson = dict(row.get("json") or {})
    sjson["continuity"] = "CONTINUE"
    again = _rerenders(pid, sid, sjson)
    if body is not None and body.dry_run:
        return {"ok": True, "rerenders": again}
    db.set_json(sid, sjson, project=pid)
    db.set_status(sid, "queued", qa_stage=0, project=pid)
    return {"ok": True, "rerenders": again}


class LoraChainBody(BaseModel):
    chain: list[dict] = []


class ContinuityBody(DryRunBody):
    continuity: str


@router.post("/api/projects/{pid}/shots/{sid}/continuity")
def project_shot_continuity(pid: str, sid: str,
                            body: ContinuityBody) -> dict:
    """Set a shot's continuity type (CUT / CONTINUE / MATCH_ACTION)."""
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    if body.continuity not in ("CUT", "CONTINUE", "MATCH_ACTION"):
        return {"ok": False, "error": "continuity must be CUT | CONTINUE | "
                                      "MATCH_ACTION"}
    sjson = dict(row.get("json") or {})
    sjson["continuity"] = body.continuity
    again = _rerenders(pid, sid, sjson)
    if body.dry_run:
        return {"ok": True, "continuity": body.continuity, "rerenders": again}
    db.set_json(sid, sjson, project=pid)
    db.set_status(sid, "queued", project=pid)
    return {"ok": True, "continuity": body.continuity, "rerenders": again}


@router.post("/api/projects/{pid}/shots/{sid}/loras")
def project_shot_loras(pid: str, sid: str, body: LoraChainBody) -> dict:
    """Manual LoRA override per shot."""
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    sjson = dict(row.get("json") or {})
    clean = []
    for e in body.chain[:6]:
        if isinstance(e, dict) and e.get("name"):
            clean.append({"name": e["name"],
                          "strength": float(e.get("strength", 0.7)),
                          "enabled": bool(e.get("enabled", True))})
    sjson["selected_loras"] = clean
    db.set_json(sid, sjson, project=pid)
    db.set_status(sid, "queued", project=pid)
    return {"ok": True, "chain": clean}


class ShotLocationBody(BaseModel):
    location: str


@router.post("/api/projects/{pid}/shots/{sid}/location")
def project_shot_location(pid: str, sid: str, body: ShotLocationBody) -> dict:
    """Set the environment this shot plays in (must exist in the project
    Bible — the linter enforces the same rule at ingest)."""
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    loc = (body.location or "").strip()
    if not loc or not is_safe_component(loc):
        return {"ok": False, "error": "invalid location id"}
    if loc not in Bible(pid).locations():
        return {"ok": False, "error": f"location '{loc}' not in project Bible"}
    sjson = dict(row.get("json") or {})
    sjson["location"] = loc
    db.set_json(sid, sjson, project=pid)
    db.set_status(sid, "queued", project=pid)
    return {"ok": True}


@router.get("/api/projects/{pid}/eta")
def project_eta(pid: str) -> dict:
    import statistics
    rows = db.all(project=pid)
    remaining = sum(1 for r in rows if r["status"] not in
                    ("passed", "review", "failed", "assembled"))
    times = [r["render_time_s"] for r in rows if r.get("render_time_s")]
    avg = statistics.median(times) if times else 15 * 60
    eta_min = round(avg * remaining / 60)
    per_scene: dict[str, int] = {}
    for r in rows:
        if r["status"] in ("passed", "review", "failed", "assembled"):
            continue
        per_scene[r["scene"]] = per_scene.get(r["scene"], 0) + 1
    return {"remaining": remaining, "eta_min": eta_min,
            "avg_render_min": round(avg / 60, 1),
            "remaining_per_scene": per_scene}


# seconds Start production waits for the director's first outcome: a run
# that stops at once (a step waits for something) answers with the reason
RUN_WAIT_S = 4.0


@router.post("/api/projects/{pid}/run")
def project_run(pid: str) -> dict:
    return director.start(pid, wait=RUN_WAIT_S)


@router.post("/api/projects/{pid}/stop")
def project_stop(pid: str) -> dict:
    return director.stop(pid)


@router.post("/api/projects/{pid}/resume")
def project_resume(pid: str) -> dict:
    """Approve the pending gate and continue (manual/checkpoints modes)."""
    proj = store.get(pid)
    if proj and proj.get("status") == "gate_pending":
        stage = proj.get("stage") or "story"
        nxt = {"story": "cast", "shots": "prompts", "prompts": "keyframes",
               "keyframes": "render", "review": "assemble"}.get(stage, stage)
        if stage == "keyframes":
            # the frames looked at stay passed after a Stop and Start
            try:
                import opening_frames
                opening_frames.looked_at(db, pid, cfg)
            except Exception as e:  # noqa: BLE001 - the project goes on; a later Start shows them again
                log.warning(f"{pid}: the opening frames were not marked looked at: {e}")
        store.set_status(pid, "running", stage=nxt)
    return director.resume(pid, wait=RUN_WAIT_S)


@router.post("/api/projects/{pid}/shots/{sid}/refs/upload")
async def shot_ref_upload(pid: str, sid: str, request: Request) -> dict:
    import time as _t
    if not is_safe_component(pid) or not is_safe_component(sid):
        return {"ok": False, "error": "invalid id"}
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    form = await request.form()
    fields = [f for name, f in form.multi_items()
              if name == "files" and hasattr(f, "read")]
    if not fields:
        return {"ok": False, "error": "no file"}
    field = fields[0]
    size = getattr(field, "size", None)
    if size is not None and size > _REF_MAX_BYTES:
        return {"ok": False, "error": "file too large (25MB max)"}
    data = await field.read()
    if not data:
        return {"ok": False, "error": "empty file"}
    if len(data) > _REF_MAX_BYTES:
        return {"ok": False, "error": "file too large (25MB max)"}
    ext = Path(getattr(field, "filename", "") or "x.png").suffix.lower()
    if ext not in _REF_EXTS:
        return {"ok": False, "error": "png/jpg/jpeg/webp only"}
    refs_dir = ROOT / "shots" / "refs" / pid / sid
    refs_dir.mkdir(parents=True, exist_ok=True)
    fn = f"ref_{int(_t.time() * 1000)}{ext}"
    (refs_dir / fn).write_bytes(data)
    db.set_status(sid, "queued", ref_image=str(refs_dir / fn), project=pid)
    return {"ok": True, "ref": fn}


@router.post("/api/projects/{pid}/shots/{sid}/refs/clear")
def shot_ref_clear(pid: str, sid: str) -> dict:
    if not is_safe_component(pid) or not is_safe_component(sid):
        return {"ok": False, "error": "invalid id"}
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    db.con.execute(
        "UPDATE shots SET ref_image=NULL WHERE shot_id=? AND project_id IS ?",
        (sid, pid))
    db.con.commit()
    return {"ok": True}


class ShotImgBody(BaseModel):
    model: str = ""             # "" = models.default_still_model()
    count: int = 1
    steps: int | None = None
    cfg: float | None = None
    seed: int | None = None
    loras: list[dict] = []
    prompt: str | None = None
    negative: str | None = None


class ShotImgUseBody(BaseModel):
    file: str


def _shot_row(pid: str, sid: str) -> dict:
    if not is_safe_component(pid) or not is_safe_component(sid):
        raise FileNotFoundError(sid)
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    return row


def _shot_json(row: dict, pid: str) -> dict:
    shot = dict(row.get("json") or {})
    shot.setdefault("shot_id", row.get("shot_id"))
    shot.setdefault("project_id", pid)
    return shot


@router.get("/api/projects/{pid}/shots/{sid}/img/plan")
def shot_img_plan(pid: str, sid: str) -> dict:
    row = _shot_row(pid, sid)
    return {"ok": True,
            **shot_image_gen.plan(_shot_json(row, pid), Bible(pid))}


@router.post("/api/projects/{pid}/shots/{sid}/img/generate")
def shot_img_generate(pid: str, sid: str, body: ShotImgBody) -> dict:
    row = _shot_row(pid, sid)
    return shot_image_gen.start(_shot_json(row, pid), Bible(pid),
                                body.dict())


@router.get("/api/projects/{pid}/shots/{sid}/img/status")
def shot_img_status(pid: str, sid: str) -> dict:
    _shot_row(pid, sid)
    return shot_image_gen.status(sid, pid)


@router.post("/api/projects/{pid}/shots/{sid}/img/stop")
def shot_img_stop(pid: str, sid: str) -> dict:
    _shot_row(pid, sid)
    return shot_image_gen.stop(sid)


@router.post("/api/projects/{pid}/shots/{sid}/img/use")
def shot_img_use(pid: str, sid: str, body: ShotImgUseBody) -> dict:
    row = _shot_row(pid, sid)
    fname = body.file
    # Path.name rejects separators/drives; "."/".." would address the folder
    if Path(fname).name != fname or fname in ("", ".", "..") \
            or "\x00" in fname:
        return {"ok": False, "error": "invalid file"}
    p = shot_images.KF_ROOT / shot_images._ns(pid) / sid / fname
    if not p.is_file():
        return {"ok": False, "error": "file not found"}
    db.set_status(sid, "queued", ref_image=str(p), project=pid)
    return {"ok": True}


class ShotRefChoicesBody(BaseModel):
    power_level: int | None = None       # 1..99; 0 = reset to auto; None = unchanged
    location_tag: str | None = None      # "" = automatic (scene map / primary)


@router.post("/api/projects/{pid}/shots/{sid}/reference-choices")
def project_shot_reference_choices(pid: str, sid: str,
                                   body: ShotRefChoicesBody) -> dict:
    """Per-shot power intensity + environment variant (requeues on change).

    These two fields decide WHICH power-level image and WHICH environment
    variant the renderer resolves; the story pipeline writes defaults, this
    endpoint is the manual override the UI exposes per shot.
    """
    row = db.get(sid, pid)
    if not row:
        raise FileNotFoundError(sid)
    sjson = dict(row.get("json") or {})
    changed = False
    if body.power_level is not None:
        lvl = int(body.power_level)
        if lvl == 0:
            sjson.pop("power_level", None)
        elif lvl < 1 or lvl > 99:
            return {"ok": False, "error": "power level must be 0 (auto) or 1..99"}
        else:
            sjson["power_level"] = lvl
        changed = True
    if body.location_tag is not None:
        tag = body.location_tag.strip()
        if tag:
            locs = Bible(pid).locations()
            loc = locs.get(sjson.get("location") or "") or {}
            tags = {str(r.get("tag") or "") for r in
                    (loc.get("reference_images") or [])
                    if isinstance(r, dict)}
            if tag not in tags:
                return {"ok": False,
                        "error": f"no environment image tagged '{tag}' on "
                                 f"location '{sjson.get('location')}'"}
        sjson["location_tag"] = tag
        changed = True
    if not changed:
        return {"ok": False, "error": "nothing to change"}
    db.set_json(sid, sjson, project=pid)
    db.set_status(sid, "queued", project=pid)
    return {"ok": True, "power_level": sjson.get("power_level"),
            "location_tag": sjson.get("location_tag")}

