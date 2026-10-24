"""Kinoloom - Training and Dataset domain router.

Provides endpoints for training datasets, dataset curation/autofilter,
dataset viewing/clearing/crops/tags, training run launches, active jobs,
batch generation, and LoRA training/import/export/management.
"""
from __future__ import annotations

import os
import re
import time
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import common
from .common import trainer, photo_gen, dataset_gen, lora_registry, jsave, jload, cfg, db
from core import (ROOT, is_safe_component, load_config, log_setup, now,
                  save_config, to_jsonable)
from training import TrainingRunner, dataset_dirs, curate_save, VALID_TYPES
from lora_registry import LoRARegistry
from photo_gen import PhotoGen
from dataset_gen import DatasetGen
import datasets_db
import jobs

router = APIRouter()
log = log_setup("common.training")

def __getattr__(name: str):
    return getattr(common, name)

@router.get("/api/training/datasets")
def training_datasets() -> dict:
    return {"datasets": dataset_dirs()}


@router.get("/api/training/dataset_categories")
def training_dataset_categories(type: str, name: str) -> dict:
    """Per-file category tags for the Curate grid (tiny, meta.json only).

    The datasets list deliberately trims meta.json to the restore keys, so
    the grid asks for the recorded category separately: outfit / weapon /
    plate / level per still — this is what groups the wardrobe-matrix looks.
    """
    if not is_safe_component(type) or not is_safe_component(name):
        return {"ok": False, "error": "invalid id"}
    mp = ROOT / "roster" / "datasets" / type / name / "meta.json"
    out: dict[str, dict] = {}
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        for f, e in (meta or {}).items():
            if not isinstance(e, dict):
                continue
            v = e.get("variations") or {}
            tags = {k: v.get(k) for k in ("outfit", "weapon", "plate", "level")
                    if v.get(k) not in (None, "")}
            if str(type).lower() == "environment":
                # env batches record the place as `place` (new) or `location`
                # (the structured combo key every existing batch used)
                p = v.get("place") or v.get("location")
                if p:
                    tags["place"] = p
            # per-file model: the datasets list only carries model COUNTS, so
            # the grid's model badge/filter read it from here
            if e.get("model"):
                tags["model"] = str(e["model"])
            if tags:
                out[str(f)] = tags
    return {"ok": True, "categories": out}


class CurateBody(BaseModel):
    type: str
    name: str
    keep: dict | None = None        # {path: bool}
    caption: str | None = None
    license: str | None = None


@router.post("/api/training/curate")
def training_curate(body: CurateBody) -> dict:
    import json
    from pathlib import Path
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    d = Path(ROOT) / "roster" / "datasets" / body.type / body.name
    state_path = d / "curation.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) \
        if state_path.exists() else {"_auto": {}}
    if body.keep:
        for path, keep in body.keep.items():
            state["_auto"].setdefault(path, {})["keep"] = bool(keep)
    if body.caption is not None:
        state["_caption"] = body.caption
    if body.license is not None:
        state["_license"] = body.license
        (d / "LICENSE.md").write_text(body.license, encoding="utf-8")
    curate_save(d, state)
    return {"ok": True}


class ImportBody(BaseModel):
    type: str
    name: str
    urls: list[str] | None = None
    license: str = ""


@router.post("/api/training/autofilter")
def training_autofilter(body: ImportBody) -> dict:
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    from dataset import auto_filter
    return auto_filter(body.type, body.name)


@router.get("/api/dataset_img")
def dataset_img(type: str, name: str, file: str) -> FileResponse:
    from pathlib import Path as P
    import os
    if not is_safe_component(type) or not is_safe_component(name):
        raise FileNotFoundError(type)
    clean_file = file.replace("\\", "/").strip().lstrip("/")
    if not clean_file or ".." in clean_file or P(clean_file).is_absolute():
        raise HTTPException(status_code=400, detail="Path traversal rejected")
    base_dir = (P(ROOT) / "roster" / "datasets" / type / name).resolve()
    p = (base_dir / clean_file).resolve()
    if not p.is_relative_to(base_dir) or not p.exists() or not p.is_file():
        raise FileNotFoundError(file)
    return FileResponse(str(p))


@router.post("/api/training/import")
def training_import(body: ImportBody) -> dict:
    from dataset import import_urls
    if not body.urls:
        return {"ok": False, "error": "no urls"}
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    return import_urls(body.urls, body.type, body.name, body.license)


class RunBody(BaseModel):
    type: str
    dataset: str
    steps: int | None = None
    rank: int | None = None
    lr: float | None = None
    id_name: str | None = None
    resume: bool = False


@router.post("/api/training/start")
def training_start(body: RunBody) -> dict:
    return trainer.start(body.dict())


@router.get("/api/training/runs")
def training_runs() -> dict:
    return {"runs": trainer.status()}


@router.get("/api/training/presets")
def training_presets() -> dict:
    tr = trainer.cfg["training"]
    return {"presets": {
        t: {"steps": tr["steps_presets"].get(t, 4000),
            "rank": tr["rank_presets"].get(t, 24),
            "lr": tr["lr_presets"].get(t, 1e-4)}
        for t in VALID_TYPES}}


@router.post("/api/training/stop")
def training_stop() -> dict:
    trainer.stop()
    return {"ok": True}


class VerdictBody(BaseModel):
    run_id: str
    verdict: str


@router.post("/api/training/verdict")
def training_verdict(body: VerdictBody) -> dict:
    from training import _load_runs, _save_runs
    runs = _load_runs()
    if body.run_id in runs:
        runs[body.run_id]["verdict"] = body.verdict
        _save_runs(runs)
    return {"ok": True}


class GenPhotoBody(BaseModel):
    type: str
    name: str
    prompt: str
    count: int = 4


@router.post("/api/training/gen_photo")
def training_gen_photo(body: GenPhotoBody) -> dict:
    return photo_gen.start(body.type, body.name, body.prompt, body.count)


@router.get("/api/training/gen_photo/status")
def training_gen_photo_status() -> dict:
    return {"gen": photo_gen.active}


@router.post("/api/training/gen_dataset")
def training_gen_dataset(body: dict) -> dict:
    mode = body.get("mode")
    params = body.get("params") or {}
    return dataset_gen.start(mode, params)


@router.get("/api/training/gen_dataset/status")
def training_gen_dataset_status() -> dict:
    return {"gen": dataset_gen.active}


class GenBatchBody(BaseModel):
    mode: str
    params: dict


@router.post("/api/training/generate")
def training_generate(body: GenBatchBody) -> dict:
    """New wizard entry point: Model Mix + continuation + jobs."""
    return dataset_gen.start(body.mode, body.params)


@router.get("/api/training/jobs")
def training_jobs(limit: int = 50) -> dict:
    import datasets_db as d
    rows = []
    con = None
    try:
        import sqlite3
        con = sqlite3.connect(str(ROOT / "tool" / "datasets.db"))
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM gen_runs ORDER BY id DESC LIMIT ?", (int(limit),))]
    except Exception:
        pass
    finally:
        if con:
            con.close()
    return {"jobs": rows}


class JobBody(BaseModel):
    run_id: int


@router.get("/api/training/jobs/{run_id}")
def training_job_status(run_id: int) -> dict:
    return {"job": datasets_db.run_stats(run_id)}


@router.post("/api/training/jobs/cancel")
def training_job_cancel(body: JobBody) -> dict:
    jobs.cancel(body.run_id)
    return {"ok": True}


@router.post("/api/training/jobs/retry")
def training_job_retry(body: JobBody) -> dict:
    return jobs.retry(body.run_id)


class EstimateBody(BaseModel):
    models: list[dict]


@router.post("/api/training/estimate")
def training_estimate(body: EstimateBody) -> dict:
    """Total + per-model estimate with measured-sample counts, so the UI
    can show 'based on your last N renders'."""
    import models as _m
    rows = []
    total_s = 0.0
    for row in body.models:
        mid = row.get("model", "")
        m = _m.get_model(mid)
        steps = row.get("steps") or (m["steps"] if m else None)
        res = row.get("res") or (m["default_size"] if m else "1024x1024")
        hires = bool(row.get("hires"))
        est = _m.estimate_seconds(mid, res, steps, hires)
        samples = len(_m._load_perf().get(
            _m.perf_key(mid, res, steps if steps is not None
                        else (m["steps"] if m else 20), hires), []))
        rows.append({"model": mid, "name": m["name"] if m else mid,
                     "count": int(row.get("count", 0)),
                     "est_s": est, "samples": samples})
        total_s += est * int(row.get("count", 0))
    return {"seconds": total_s, "minutes": round(total_s / 60, 1),
            "rows": rows}


@router.get("/api/training/env_options")
def training_env_options() -> dict:
    """Place catalog for the environment wizard (labels + locations)."""
    from dataset_gen import ENV_PLACES
    return {"places": ENV_PLACES}


@router.post("/api/training/gen_dataset/stop")
def training_gen_dataset_stop() -> dict:
    dataset_gen.stop()
    return {"ok": True}


@router.post("/api/training/gen_photo/stop")
def training_gen_photo_stop() -> dict:
    photo_gen.stop()
    return {"ok": True}


@router.get("/api/lora")
def lora_all() -> dict:
    # training writes the registry file outside this instance — reload
    # from disk so a finished run shows up without a server restart
    lora_registry._load()
    return {"lora": lora_registry.all()}


class PreviewBody(BaseModel):
    mode: str
    params: dict
    model: str = "realvis"


@router.post("/api/training/preview")
def training_preview(body: PreviewBody) -> dict:
    """Exact prompt + negative preview for the wizard."""
    import promptspec as ps
    from models import adapt_prompt, get_model, LORA_TRIGGER_EXTRA
    spec = ps.spec_from_params(body.mode, body.params or {})
    pr = body.params or {}
    # apply the SAME variation axes the batch engine applies, so the preview
    # can never drift from what actually renders (outfits, weapons, level
    # ladder, interior scope, ...). The combo is built by the batch engine
    # too: matrix looks (outfit + weapon) and identity plates only exist as
    # combos, so an empty combo previewed the wrong language entirely.
    k = int(pr.get("k") or 0)
    combo: dict = {}
    try:
        from jobs import build_combos
        combos = build_combos(body.mode, pr) or []
        if combos:
            combo = combos[k % len(combos)]
    except Exception as e:  # noqa: BLE001
        log.warning("preview combo build failed: %s", e)
    try:
        from jobs import apply_spec_axes
        apply_spec_axes(spec, body.mode, pr, combo, k)
    except Exception as e:  # noqa: BLE001
        log.warning("preview axis application failed: %s", e)
    base = spec.to_prompt()
    m = get_model(body.model) or get_model("realvis")
    # prompt modes: override -> custom replaces blocks; add -> custom is
    # already appended by spec.to_prompt(); structured -> blocks only.
    user_prompt = pr.get("custom_prompt") if pr.get("mode") == "override" \
        else None
    fprompt, fneg = adapt_prompt(body.model, base, "training",
                                 user_prompt=user_prompt,
                                 user_negative=pr.get("custom_negative"))
    has_lora = bool(pr.get("loras")) or any(
        (r or {}).get("loras") for r in (pr.get("models") or []))
    if has_lora and fneg == m["negative"]:
        fneg = fneg + ", " + LORA_TRIGGER_EXTRA
    return {"ok": True, "prompt": fprompt, "negative": fneg,
            "caption": spec.to_caption()}


class ClearBody(BaseModel):
    type: str
    name: str | None = None
    files: list[str] | None = None


@router.post("/api/training/dataset/clear")
def dataset_clear(body: ClearBody) -> dict:
    """Clear ALL candidates of a dataset (physical files + curation)."""
    import shutil as sh
    from pathlib import Path as P
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    d = P(ROOT) / "roster" / "datasets" / body.type / body.name
    if not d.is_dir():
        return {"ok": False, "error": "dataset not found"}
    for sub in ("stills", "frames"):
        s = d / sub
        if s.exists():
            sh.rmtree(s)
    for f in list(d.glob("*.png")) + list(d.glob("*.jpg")):
        f.unlink()
    # leftover metadata must go too, otherwise the folder survives deletion
    for f in list(d.glob("*.json")) + list(d.glob("*.txt")):
        f.unlink()
    (d / "curation.json").unlink(missing_ok=True)
    datasets_db.delete_image(body.type, body.name, "")  # no-op guard
    import sqlite3
    try:
        con = sqlite3.connect(str(ROOT / "tool" / "datasets.db"))
        con.execute("DELETE FROM dataset_images WHERE dataset_type=? "
                    "AND dataset_name=?", (body.type, body.name))
        con.commit()
        con.close()
    except Exception:
        pass
    # remove the folder entirely if nothing remains
    if not any(d.iterdir()):
        d.rmdir()
    return {"ok": True, "removed": True}


@router.post("/api/training/dataset/clear_type")
def dataset_clear_type(body: ClearBody) -> dict:
    """Clear ALL datasets of a type (face/power/environment/...)."""
    import shutil as sh
    from pathlib import Path as P
    if not is_safe_component(body.type):
        return {"ok": False, "error": "invalid type"}
    base = P(ROOT) / "roster" / "datasets" / body.type
    if not base.is_dir():
        return {"ok": False, "error": "type not found"}
    removed = 0
    for d in base.iterdir():
        if d.is_dir():
            sh.rmtree(d)
            removed += 1
    return {"ok": True, "removed": removed}


@router.post("/api/training/dataset/delete_files")
def dataset_delete_files(body: ClearBody) -> dict:
    import os
    import json as _json
    from pathlib import Path as P, Path
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    d = P(ROOT) / "roster" / "datasets" / body.type / body.name
    d_resolved = d.resolve()
    state_path = d / "curation.json"
    state = _json.loads(state_path.read_text(encoding="utf-8")) \
        if state_path.exists() else {}
    removed = 0
    for rel in body.files or []:
        clean_rel = rel.replace("\\", "/").strip().lstrip("/")
        if not clean_rel or ".." in clean_rel or Path(clean_rel).is_absolute():
            continue
        p = (d / clean_rel).resolve()
        if not p.is_relative_to(d_resolved):
            continue
        if p.exists() and p.is_file():
            p.unlink()
            removed += 1
        datasets_db.delete_image(body.type, body.name, rel)
        if clean_rel != rel:
            datasets_db.delete_image(body.type, body.name, clean_rel)
        for key in list(state.keys()):
            if str(key).endswith(rel) or str(key).endswith(clean_rel) or str(key) == str(d / clean_rel) or str(key) == str(d / rel):
                state.pop(key, None)
    if state:
        jsave(state_path, state)
    return {"ok": True, "removed": removed}


class DatasetOpBody(BaseModel):
    type: str
    name: str
    new_name: str = ""


@router.post("/api/training/dataset/rename")
def dataset_rename(body: DatasetOpBody) -> dict:
    import shutil as sh
    from pathlib import Path as P
    nn = body.new_name.strip()
    if not nn or ".." in nn or "/" in nn or "\\" in nn:
        return {"ok": False, "error": "invalid new name"}
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    src = P(ROOT) / "roster" / "datasets" / body.type / body.name
    dst = P(ROOT) / "roster" / "datasets" / body.type / nn
    if not src.is_dir():
        return {"ok": False, "error": "dataset not found"}
    if dst.exists():
        return {"ok": False, "error": "name already exists"}
    sh.move(str(src), str(dst))
    import sqlite3
    con = sqlite3.connect(str(ROOT / "tool" / "datasets.db"))
    con.execute("UPDATE dataset_images SET dataset_name=? WHERE "
                "dataset_type=? AND dataset_name=?", (nn, body.type, body.name))
    con.execute("UPDATE gen_runs SET dataset_name=? WHERE dataset_type=? "
                "AND dataset_name=?", (nn, body.type, body.name))
    con.commit()
    con.close()
    return {"ok": True}


@router.post("/api/training/dataset/duplicate")
def dataset_duplicate(body: DatasetOpBody) -> dict:
    import shutil as sh
    from pathlib import Path as P
    nn = body.new_name.strip()
    if not nn or ".." in nn or "/" in nn or "\\" in nn:
        return {"ok": False, "error": "invalid new name"}
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    src = P(ROOT) / "roster" / "datasets" / body.type / body.name
    dst = P(ROOT) / "roster" / "datasets" / body.type / nn
    if not src.is_dir():
        return {"ok": False, "error": "dataset not found"}
    if dst.exists():
        return {"ok": False, "error": "name already exists"}
    sh.copytree(str(src), str(dst))
    return {"ok": True}


@router.post("/api/training/dataset/export")
def dataset_export(body: DatasetOpBody) -> dict:
    """Zip the dataset folder to output/exports/<name>.zip."""
    import zipfile
    from pathlib import Path as P
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    src = P(ROOT) / "roster" / "datasets" / body.type / body.name
    if not src.is_dir():
        return {"ok": False, "error": "dataset not found"}
    out_dir = ROOT / "output" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{body.type}_{body.name}.zip"
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(str(f), f.relative_to(src).as_posix())
    return {"ok": True, "path": str(out)}


@router.post("/api/training/dataset/clear_rejected")
def dataset_clear_rejected(body: DatasetOpBody) -> dict:
    """Physically delete all rejected candidates (curation keep=false)."""
    import json as _json
    from pathlib import Path as P
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    d = P(ROOT) / "roster" / "datasets" / body.type / body.name
    state_path = d / "curation.json"
    if not state_path.exists():
        return {"ok": True, "removed": 0}
    state = _json.loads(state_path.read_text(encoding="utf-8"))
    removed = 0
    for key, v in list(state.items()):
        if isinstance(v, dict) and v.get("keep") is False:
            p = d / key
            if ".." not in key and p.exists():
                p.unlink()
                datasets_db.delete_image(body.type, body.name, key)
                removed += 1
            state.pop(key, None)
    jsave(state_path, state)
    return {"ok": True, "removed": removed}


class ImagesRegenBody(BaseModel):
    type: str
    name: str
    file: str


@router.post("/api/training/images/regenerate")
def training_image_regenerate(body: ImagesRegenBody) -> dict:
    """Regenerate one image from its provenance (same model/prompt/seed),
    for reproducing or regenerating selected images."""
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    row = None
    for r in datasets_db.list_images(body.type, body.name, limit=200000):
        if r["file"] == body.file:
            row = r
            break
    if not row:
        return {"ok": False, "error": "no provenance for this image"}
    import jobs as _j
    params = {
        "mode": "photo",
        "prompt": row.get("prompt") or "",
        "models": [{"model": row.get("model") or "realvis", "count": 1}],
        "custom_negative": row.get("negative") or None,
    }
    if row.get("seed") is not None:
        params["seed"] = int(row["seed"])
    res = _j.submit(params, body.type, body.name)
    if not res.get("ok"):
        return res
    return {"ok": True, "run_id": res["run_id"]}


class ImageMetaBody(BaseModel):
    type: str
    name: str
    file: str


@router.post("/api/training/refs/upload")
async def training_refs_upload(request: Request) -> dict:
    """Upload reference images for a dataset (saved to refs/)."""
    from pathlib import Path as P
    from fastapi import UploadFile
    import time as _t
    form = await request.form()
    dtype = str(form.get("type", ""))
    name = str(form.get("name", ""))
    d = P(ROOT) / "roster" / "datasets" / dtype / name / "refs"
    if ".." in dtype or ".." in name or not name:
        return {"ok": False, "error": "invalid path"}
    d.mkdir(parents=True, exist_ok=True)
    saved = []
    for fname, field in form.multi_items():
        if fname != "files" or not hasattr(field, "read"):
            continue
        data = await field.read()
        if not data:
            continue
        ext = P(getattr(field, "filename", "") or "x.png").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        fn = f"ref_{int(_t.time() * 1000)}{ext}"
        (d / fn).write_bytes(data)
        saved.append(f"refs/{fn}")
    return {"ok": bool(saved), "refs": saved}


@router.post("/api/training/images/meta")
def training_image_meta(body: ImageMetaBody) -> dict:
    if not is_safe_component(body.type) or not is_safe_component(body.name):
        return {"ok": False, "error": "invalid type/name"}
    for r in datasets_db.list_images(body.type, body.name, limit=200000):
        if r["file"] == body.file:
            return {"ok": True, "meta": r}
    return {"ok": False, "error": "no provenance record"}


class LoraOpBody(BaseModel):
    filename: str
    new_name: str = ""


@router.post("/api/lora/rename")
def lora_rename(body: LoraOpBody) -> dict:
    """Rename a trained LoRA file + registry entry (both copies)."""
    import shutil as sh
    from pathlib import Path as P
    def _bad(fn: str) -> bool:
        return not fn or ".." in fn or "/" in fn or "\\" in fn or ":" in fn
    if _bad(body.filename) or _bad(body.new_name):
        return {"ok": False, "error": "invalid filename"}
    nn = body.new_name.strip()
    if not nn.endswith(".safetensors"):
        nn += ".safetensors"
    if ".." in nn or "/" in nn or "\\" in nn:
        return {"ok": False, "error": "invalid name"}
    moved = []
    for base in (P(ROOT) / "models" / "loras", P(ROOT) / "roster" / "loras"):
        src = base / body.filename
        dst = base / nn
        if src.exists():
            sh.move(str(src), str(dst))
            moved.append(str(dst))
    lora_registry.update(body.filename, status="renamed")
    reg = lora_registry.all()
    if body.filename in reg:
        reg[nn] = reg.pop(body.filename)
        lora_registry._entries = reg
        lora_registry._save()
    return {"ok": True, "moved": moved, "new_name": nn}


@router.post("/api/lora/archive")
def lora_archive(body: LoraOpBody) -> dict:
    """Toggle archive status (archived LoRAs stop auto-applying)."""
    if not body.filename or ".." in body.filename or "/" in body.filename \
            or "\\" in body.filename:
        return {"ok": False, "error": "invalid filename"}
    lora_registry.update(body.filename, status="archived")
    return {"ok": True}


@router.post("/api/lora/unarchive")
def lora_unarchive(body: LoraOpBody) -> dict:
    if not body.filename or ".." in body.filename or "/" in body.filename \
            or "\\" in body.filename:
        return {"ok": False, "error": "invalid filename"}
    lora_registry.update(body.filename, status="ready")
    return {"ok": True}


@router.post("/api/lora/export")
def lora_export(body: LoraOpBody) -> dict:
    """Copy the LoRA file to output/exports/."""
    import shutil as sh
    from pathlib import Path as P
    if not body.filename or ".." in body.filename or "/" in body.filename \
            or "\\" in body.filename:
        return {"ok": False, "error": "invalid filename"}
    src = P(ROOT) / "models" / "loras" / body.filename
    if not src.exists():
        src = P(ROOT) / "roster" / "loras" / body.filename
    if not src.exists():
        return {"ok": False, "error": "file not found"}
    out_dir = ROOT / "output" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / body.filename
    sh.copy2(str(src), str(dst))
    return {"ok": True, "path": str(dst)}


class LoraDeleteBody(BaseModel):
    filename: str


@router.post("/api/lora/delete")
def lora_delete(body: LoraDeleteBody) -> dict:
    """Delete a trained LoRA: models/loras + roster copy + registry entry."""
    from pathlib import Path as P
    fname = body.filename
    if not fname or ".." in fname or "/" in fname or "\\" in fname \
            or ":" in fname:
        return {"ok": False, "error": "invalid filename"}
    deleted = []
    for base in (P(ROOT) / "models" / "loras", P(ROOT) / "roster" / "loras"):
        p = base / fname
        if p.exists():
            p.unlink()
            deleted.append(str(p))
    lora_registry.update(fname, status="deleted")
    return {"ok": True, "deleted": deleted}

