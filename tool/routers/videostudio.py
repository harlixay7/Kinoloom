"""Kinoloom - Video Studio domain router.

Provides endpoints for experimental video studio generation, multi-stage pipelines,
upscaling, frame interpolation, face fix, preset management, and history.
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
from .common import _REF_MAX_BYTES, _REF_EXTS, _REF_MEDIA
from core import ROOT, is_safe_component, load_config, log_setup, now, save_config, to_jsonable
import video_studio

router = APIRouter()
log = log_setup("common.videostudio")

def __getattr__(name: str):
    return getattr(common, name)

class VideoStudioBody(BaseModel):
    model: str
    prompt: str
    mode: str = "t2v"
    negative: str = ""
    seed: int = -1
    width: int = 832
    height: int = 480
    frames: int = 25
    steps: int | None = None
    cfg: float | None = None
    loras: list[dict] = []
    start_image: str | None = None
    teacache_thresh: float = 0.1
    teacache_end: float = 0.6
    no_cache: bool = False
    text_encoder: str | None = None
    worker: str | None = None          # target worker; 'local' default
    # Continuity: absolute path to a previous clip's tail frame. The API layer
    # translates it per model family (H3: slot "continuation" → frame-0
    # anchor; LTX: free pic slot via add_continuation; Wan: native first
    # frame). server-side validation happens in video_studio.start().
    continuation: str | None = None
    # LTX-2.5 reference stills: ordered MSR slots. pic1..pic4 are subjects,
    # background is the location. Paths are absolute local files; video_studio
    # uploads them to the target ComfyUI and drops slots without a file.
    references: list[dict] = []
    # LTX-2.5 detail pass toggle (DFR keyframes). None = the model's default.
    dfr: bool | None = None


@router.get("/api/videostudio/models")
def videostudio_models() -> dict:
    """Video models with *computed* availability, in picker order: `groups`
    (display order) and `models` sorted by group, recommended first. A model
    whose weights or workflow are absent reports available=false and the UI
    must not offer it. `seamless` follows video_studio.seamless_chains."""
    import video_models
    cat = video_models.catalog()
    for m in cat["models"]:
        m["seamless"] = video_studio.seamless_engine(m["id"])
    return {
        **cat,
        # the studio default (LTX-2.5 s6 CQ): what the pickers start on and
        # what a project with no model renders with. Never Wan 2.2 (legacy).
        "production_model": video_models.production_model_id(),
        "default_model": video_models.default_model_id(),
        "registry_error": video_models.USER_REGISTRY_ERROR,
    }


@router.get("/api/videostudio/loras")
def videostudio_loras(model: str) -> dict:
    """Only LoRAs whose family the selected model actually accepts."""
    import video_studio
    return video_studio.loras_for_model(model)


@router.post("/api/videostudio/upload_start_image")
async def videostudio_upload_start_image(request: Request) -> dict:
    """Upload an I2V start image from the browser file picker (Video Studio).

    Saves into ComfyUI/input/ (the documented START_IMAGE location — worker
    renders upload from there) AND ComfyUI_LTX/input/ (H3/LTX local renders
    read their own input dir directly). Returns the filename that the
    vs-startimg field / START_IMAGE token expects.
    """
    import time as _t
    from core import ROOT
    form = await request.form()
    data = None
    orig = ""
    for fname, field in form.multi_items():
        if fname in ("file", "image", "files") and hasattr(field, "read"):
            data = await field.read()
            orig = getattr(field, "filename", "") or "x.png"
            break
    if not data:
        return {"ok": False, "error": "no image file in form (field 'file')"}
    if len(data) > _REF_MAX_BYTES:
        return {"ok": False, "error": "image too large (25 MB max)"}
    ext = Path(orig).suffix.lower() or ".png"
    if ext not in _REF_EXTS:
        return {"ok": False,
                "error": f"unsupported type '{ext}' (png, jpg, webp only)"}
    fn = f"vs_i2v_{int(_t.time() * 1000)}{ext}"
    saved_any = False
    for d in (ROOT / "ComfyUI" / "input", ROOT / "ComfyUI_LTX" / "input"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / fn).write_bytes(data)
            saved_any = True
        except OSError:
            continue
    if not saved_any:
        return {"ok": False,
                "error": "could not write the start image to any ComfyUI input dir"}
    return {"ok": True, "filename": fn}


@router.post("/api/videostudio/continue_from")
async def videostudio_continue_from(request: Request) -> dict:
    """Continue from a previous Video Studio clip.

    Extracts the previous clip's tail frame (best last frame by sharpness —
    the studio's proven continuity extractor), stages it for the reference
    system AND the ComfyUI input dirs, and returns everything the UI needs:
      * path  → sent back as `continuation` on generate (H3: frame-0 anchor;
                LTX: pic1 subject ref; Wan: native first frame)
      * ref   → an optional identity-reference entry for the vs-refs strip
      * input_filename → pre-staged in ComfyUI input dirs (Wan native path)
    """
    import re as _re
    import time as _t
    import shutil
    from core import ROOT
    import continuity as _cont
    import video_studio as _vs
    body = await request.json()

    # Three ways to name the source clip: a completed job id, a direct path
    # into the studio's output tree, or a file name in it (`name`, what the
    # history list carries) — chain takes and segments are not job records,
    # but their rendered files live in the same folder.
    direct = str((body or {}).get("path") or "").strip()
    name = str((body or {}).get("name") or "").strip()
    if name and not direct:
        if not _re.fullmatch(r"[\w.-]+\.mp4", name):
            return {"ok": False, "error": "bad file name"}
        direct = str(ROOT / "output" / "videostudio" / name)
    jid = str((body or {}).get("job_id") or "").strip()
    video = None
    prev_model = None
    if direct:
        root = (ROOT / "output" / "videostudio").resolve()
        try:
            p = Path(direct).resolve()
        except OSError:
            return {"ok": False, "error": "invalid path"}
        if p.parent != root or p.suffix.lower() != ".mp4" or not p.is_file():
            return {"ok": False,
                    "error": "path must be an .mp4 inside output/videostudio"}
        video = p
        jid = p.stem  # used for the staged names below
    else:
        if not _re.fullmatch(r"[a-f0-9]{6,32}", jid or ""):
            return {"ok": False, "error": "bad job id"}
        j = _vs.job(jid)
        if not j:
            return {"ok": False, "error": "job not found"}
        if j.get("status") not in ("completed", None):
            return {"ok": False, "error": f"job status is {j.get('status')} — "
                                          f"only completed clips can continue"}
        out_dir = Path(j.get("output_dir") or (ROOT / "output" / "videostudio"))
        prev_model = j.get("model")
        for o in j.get("outputs") or []:
            cand = out_dir / o
            if cand.suffix.lower() == ".mp4" and cand.is_file():
                video = cand
                break
    if video is None:
        return {"ok": False,
                "error": "the clip's video file is missing from output/videostudio "
                         "(recover it from the instance output tree first)"}

    tail = _cont.extract_tail(str(video), f"vs_{jid}", n=5)
    if tail is None:
        return {"ok": False, "error": "tail extraction failed on the source clip"}

    # stage copies: vs_refs (thumbnail endpoint) + ComfyUI input dirs (Wan
    # native i2v reads its input dir directly)
    stamp = int(_t.time() * 1000)
    vs_ref_dir = ROOT / "output" / "uploads" / "vs_refs"
    vs_ref_dir.mkdir(parents=True, exist_ok=True)
    thumb_name = f"vsref_{stamp}.png"
    thumb_path = vs_ref_dir / thumb_name
    shutil.copy2(tail, thumb_path)

    input_name = f"vs_cont_{stamp}.png"
    staged = []
    for d in (ROOT / "ComfyUI" / "input", ROOT / "ComfyUI_LTX" / "input"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tail, d / input_name)
            staged.append(str(d / input_name))
        except OSError:
            continue

    return {"ok": True, "job_id": jid, "path": str(thumb_path),
            "filename": thumb_name,
            "url": f"/api/videostudio/ref_image?name={thumb_name}",
            "input_filename": input_name, "input_staged": staged,
            "prev_model": prev_model}


@router.get("/api/videostudio/chain_segments")
def videostudio_chain_segments() -> dict:
    """Newest rendered chain segment on disk (chain segments are not job
    records, so the Continue-from button cannot find them in the jobs store —
    this lists the newest chain_*_seg*.mp4 for the direct-path continuation)."""
    from core import ROOT
    d = ROOT / "output" / "videostudio"
    best = None
    if d.is_dir():
        for p in d.glob("chain_*_seg*.mp4"):
            if best is None or p.stat().st_mtime > best.stat().st_mtime:
                best = p
    if best is None:
        return {"ok": False, "error": "no chain segments rendered yet"}
    return {"ok": True, "path": str(best), "name": best.name,
            "ts": best.stat().st_mtime}


@router.post("/api/videostudio/upload_ref")
async def videostudio_upload_ref(request: Request) -> dict:
    """Upload an arbitrary disk image as a Video Studio reference.

    References no longer have to come from training batches: the returned
    entry ({slot-less} path + url + display fields) drops straight into the
    vs-refs strip, survives refreshes (localStorage), and resolves server-side
    through ltx_refs.resolve_reference_entry's `{path}` branch — so LTX AND
    H3 both accept it (H3 pins the first reference as the frame-0 identity
    anchor).
    """
    import time as _t
    from core import ROOT
    form = await request.form()
    data = None
    orig = ""
    for fname, field in form.multi_items():
        if fname in ("file", "image", "files") and hasattr(field, "read"):
            data = await field.read()
            orig = getattr(field, "filename", "") or "x.png"
            break
    if not data:
        return {"ok": False, "error": "no image file in form (field 'file')"}
    if len(data) > _REF_MAX_BYTES:
        return {"ok": False, "error": "image too large (25 MB max)"}
    ext = Path(orig).suffix.lower() or ".png"
    if ext not in _REF_EXTS:
        return {"ok": False,
                "error": f"unsupported type '{ext}' (png, jpg, webp only)"}
    d = ROOT / "output" / "uploads" / "vs_refs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        fn = f"vsref_{int(_t.time() * 1000)}{ext}"
        (d / fn).write_bytes(data)
    except OSError as e:
        return {"ok": False, "error": f"could not store reference: {e}"}
    return {"ok": True, "filename": fn,
            "path": str(d / fn),
            "url": f"/api/videostudio/ref_image?name={fn}"}


@router.get("/api/videostudio/ref_image")
def videostudio_ref_image(name: str) -> FileResponse:
    """Serve one uploaded Video Studio reference thumbnail.

    Strict allow-list on the filename (vsref_<digits>.<ext>) — no traversal.
    """
    import re as _re
    from core import ROOT
    if not _re.fullmatch(r"vsref_\d+\.(?:png|jpg|jpeg|webp)", name or ""):
        raise HTTPException(status_code=400, detail="invalid reference name")
    p = ROOT / "output" / "uploads" / "vs_refs" / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="reference not found")
    return FileResponse(p)


class PromptCheckBody(BaseModel):
    """A Video Studio request as Generate would send it: one prompt is a
    clip, two or more a chain. `segments` (the cast sheet) counts the segment
    boxes, empty ones included; `start_from_clip` marks a chain whose start
    image is the previous clip's last frame (↪)."""
    model: str
    prompts: list[str] = []
    segments: int | None = None
    references: list[dict] = []
    start_image: str | None = None
    continuation: str | None = None
    start_from_clip: bool = False
    frames: int | None = None


def _check_request(body: PromptCheckBody) -> dict:
    return {"references": list(body.references or []), "start_image": body.start_image,
            "continuation": body.continuation, "start_from_clip": body.start_from_clip,
            "frames": body.frames}


@router.post("/api/videostudio/prompt_check")
def videostudio_prompt_check(body: PromptCheckBody) -> dict:
    """The prompt check of a request (prompt_check): errors stop Generate,
    warnings never do. Each issue may carry text fixes for its segment."""
    import prompt_check
    return prompt_check.report(body.model, list(body.prompts), **_check_request(body))


@router.post("/api/videostudio/cast_sheet")
def videostudio_cast_sheet(body: PromptCheckBody) -> dict:
    """The cast sheet for the agent that writes the prompts: how the model
    reads a prompt, the images it receives, the opening and the continuity
    lines, and the reply format."""
    import prompt_check
    segments = max(1, min(int(body.segments or len(body.prompts) or 1), 50))
    return prompt_check.sheet(body.model, segments, **_check_request(body))


@router.post("/api/videostudio/generate")
def videostudio_generate(body: VideoStudioBody) -> dict:
    """Start one render. Continuity (`continuation`) is translated per model
    family inside video_studio.start(), the same path queued jobs take. A
    request the prompt check finds errors in is refused with its issues."""
    import prompt_check
    import video_studio
    refused = prompt_check.refusal(
        body.model, [body.prompt], references=list(body.references or []),
        start_image=body.start_image, continuation=body.continuation, frames=body.frames)
    if refused:
        return refused
    try:
        return video_studio.start(
            body.model, body.prompt, mode=body.mode, negative=body.negative,
            seed=body.seed, width=body.width, height=body.height,
            frames=body.frames, steps=body.steps, cfg=body.cfg,
            loras=body.loras, start_image=body.start_image,
            teacache_thresh=body.teacache_thresh,
            teacache_end=body.teacache_end, no_cache=body.no_cache,
            text_encoder=body.text_encoder,
            worker=body.worker,
            references=list(body.references or []),
            dfr=body.dfr,
            continuation=body.continuation)
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/videostudio/jobs")
def videostudio_jobs(limit: int = 50) -> dict:
    import video_studio
    return {"jobs": video_studio.jobs(limit)}


@router.get("/api/videostudio/history")
def videostudio_history(limit: int = 60) -> dict:
    """The studio's renders, newest first: single clips and chains (seamless
    takes, segment chains, the movie's continuous takes) in one list."""
    import video_studio
    # `now`: the server's clock, so a browser on another machine can time the
    # running chain step (rec["plan"][i]["t0"] is server time)
    return {"items": video_studio.history(min(max(1, limit), 300)), "now": round(time.time(), 1)}


@router.get("/api/videostudio/jobs/{jid}")
def videostudio_job(jid: str) -> dict:
    import video_studio
    if not re.fullmatch(r"[a-f0-9]{6,32}", jid):
        return {"ok": False, "error": "bad job id"}
    j = video_studio.job(jid)
    return j or {"ok": False, "error": "not found"}


@router.get("/api/videostudio/chain/{cid}")
def videostudio_chain_status(cid: str) -> dict:
    import video_studio
    c = video_studio.get_chain(cid)
    return c or {"ok": False, "error": "chain not found"}


class ChainBody(BaseModel):
    model: str
    prompts: list[str]
    mode: str = "t2v"
    negative: str = ""
    seed: int | None = -1
    width: int = 1344
    height: int = 768
    frames: int = 124
    steps: int | None = None
    cfg: float | None = None
    start_image: str | None = None
    worker: str | None = None
    references: list[dict] = []
    draft_only: bool = False


@router.get("/api/videostudio/chain/{cid}/preview/{name}")
def videostudio_chain_preview(cid: str, name: str) -> FileResponse:
    """One segment's quick-look MP4 of a seamless chain (rec["link_previews"]),
    encoded as soon as the segment is rendered. Strict names: no traversal."""
    import chain_studio
    if not re.fullmatch(r"[a-f0-9]{6,32}", cid) or not re.fullmatch(r"link\d{1,2}_(?:draft|final)\.mp4", name):
        raise HTTPException(status_code=404, detail="bad preview name")
    p = chain_studio.preview_dir(cid) / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"preview not found: {name}")
    return FileResponse(str(p), media_type="video/mp4")


@router.post("/api/videostudio/chain/{cid}/finish")
def videostudio_finish_chain(cid: str) -> dict:
    """Render the 1080p pass of a seamless chain stopped at its draft preview."""
    import video_studio
    if not re.fullmatch(r"[a-f0-9]{6,32}", cid):
        return {"ok": False, "error": "bad chain id"}
    try:
        return video_studio.finish_chain(cid)
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/videostudio/chain")
def videostudio_start_chain(body: ChainBody) -> dict:
    """Start a chain. A request the prompt check finds errors in is refused
    with its issues."""
    import prompt_check
    import video_studio
    if not body.prompts:
        return {"ok": False, "error": "a chain needs at least one prompt"}
    refused = prompt_check.refusal(
        body.model, list(body.prompts), references=list(body.references or []),
        start_image=body.start_image, frames=body.frames)
    if refused:
        return refused
    try:
        return video_studio.start_chain(
            body.model, body.prompts, mode=body.mode,
            negative=body.negative,
            seed=-1 if body.seed is None else body.seed,
            width=body.width, height=body.height,
            frames=body.frames, steps=body.steps,
            cfg=body.cfg, start_image=body.start_image,
            worker=body.worker,
            references=list(body.references or []),
            draft_only=body.draft_only)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.exception("videostudio operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@router.get("/api/videostudio/file/{name}")
def videostudio_file(name: str) -> FileResponse:
    """Serve a Video Studio artefact. Confined to the studio's own output
    directory — `Path(name).name` strips any directory component, so a
    traversal attempt resolves inside OUT_DIR or not at all.

    Missing files return a clean 404: legacy job records can name an output
    that was later renamed/cleaned, and raising FileNotFoundError made the
    UI's player poll produce a 500 + traceback every few seconds (the
    terminal spam in the server logs)."""
    import video_studio
    if Path(name).name != name or not name:
        raise HTTPException(status_code=404, detail="bad artefact name")
    p = video_studio.OUT_DIR / name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404,
                            detail=f"artefact not found: {name}")
    media = {".mp4": "video/mp4", ".webm": "video/webm",
             ".gif": "image/gif", ".png": "image/png"}.get(
                 p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media)

