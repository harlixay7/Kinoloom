"""Kinoloom - model files router (Settings > Model files).

Where each weight file of install/components.json is, the model folders of
settings paths.extra_model_dirs, and a search for the files a user already
has, under any name (tool/model_files.py): those are linked under Kinoloom's
name (on another drive, in the Kinoloom-models folder there), used in their
folder, or, where no link works and the user asks, copied. The user's files
are never changed. The search and the copies run in a background thread; the
page polls GET /api/model-files/job.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import core
import model_files as mf
from core import ROOT, load_config, log_setup

router = APIRouter()
log = log_setup("model_files")

_LOCK = threading.Lock()
_JOB: dict = {}
_COPIES: list[mf.Found] = []      # what the last search could only copy


def _reset(kind: str | None, state: str = "idle") -> None:
    # a new dict, never one emptied in place: a poll may be reading the old one
    global _JOB
    _JOB = dict(state=state, kind=kind, lines=[], found=[], copy=[], added=[],
                error=None, started=time.strftime("%H:%M:%S") if kind else None, ended=None)


_reset(None)


def _say(line: str) -> None:
    line = line.strip()
    if line:
        _JOB["lines"] = (_JOB["lines"] + [line])[-200:]


def _job() -> dict:
    snap = {k: (list(v) if isinstance(v, list) else v) for k, v in list(_JOB.items())}
    snap["copy_gb"] = round(sum(f.size for f in _COPIES) / mf.GB, 2)
    return snap


def _row(f: mf.Found) -> dict:
    return {"file": Path(f.rel).name, "path": f.rel, "from": str(f.src), "how": f.how,
            "checked": f.checked, "size": f.size,
            "folder": str(f.folder) if f.folder else None}


# ------------------------------------------------------------- folders
def _typed_folder(text: str) -> Path:
    """A folder typed or pasted on the page, checked: a full path on this PC."""
    s = mf.clean(text)
    if not s:
        raise HTTPException(status_code=400, detail="type or paste a folder")
    if mf.is_network_path(s):
        raise HTTPException(status_code=400, detail=(
            "a network folder (\\\\server\\share) can't be used: map it to a drive "
            "letter, or copy the files to this PC"))
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail=(
            f"{s} is not a full path: give the whole path, like C:\\ComfyUI\\models"))
    return Path(os.path.abspath(p))


def _listed() -> list[str]:
    """The model folders as tool/settings.yaml has them now (the installer may
    have added one while the studio ran), then any only the studio knows."""
    out: list[str] = []
    try:
        disk = yaml.safe_load((ROOT / "tool" / "settings.yaml").read_text(encoding="utf-8")) or {}
        out = mf.listed_dirs(((disk if isinstance(disk, dict) else {}).get("paths") or {})
                             .get("extra_model_dirs"))
    except (OSError, yaml.YAMLError):
        pass
    for d in mf.listed_dirs((load_config().get("paths") or {}).get("extra_model_dirs")):
        if not any(mf.same_dir(d, o, ROOT) for o in out):
            out.append(d)
    return out


def _save_dirs(dirs: list[str]) -> None:
    """tool/settings.yaml first, only paths.extra_model_dirs changed and its
    comments kept (tool/model_files.py), then the live settings."""
    with core._CFG_LOCK:
        try:
            mf.set_dirs_in_settings(ROOT, dirs)
        except (OSError, ValueError) as e:
            raise HTTPException(status_code=500,
                                detail=f"could not change tool/settings.yaml: {e}") from e
        load_config().setdefault("paths", {})["extra_model_dirs"] = list(dirs)


def _add_dir(folder: Path, first: bool = False) -> bool:
    """Add a model folder to the settings (first: before the others, for a
    Kinoloom-models folder); False when it is already one."""
    dirs = _listed()
    if any(mf.same_dir(d, folder, ROOT) for d in dirs):
        return False
    _save_dirs([folder.as_posix()] + dirs if first else dirs + [folder.as_posix()])
    log.info("model folder added: %s", folder)
    return True


def _folders() -> list[dict]:
    out = []
    for d in _listed():
        p = mf.resolve_dir(d, ROOT)
        out.append({"listed": d, "path": str(p), "exists": p.is_dir()})
    return out


class FolderBody(BaseModel):
    folder: str
    action: str = "add"         # "add" | "remove"


@router.get("/api/model-files")
def model_files_status() -> dict:
    """Every model file of the catalog with where it is, the model folders,
    and the background search or copy."""
    cat = mf.load_catalog(ROOT)
    return {"files": mf.status(cat, load_config(), ROOT), "folders": _folders(),
            "job": _job()}


@router.post("/api/model-files/folders")
def model_folders_edit(body: FolderBody) -> dict:
    """Add or remove a model folder (settings paths.extra_model_dirs)."""
    if body.action not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="action must be add or remove")
    # a folder already in the list can go whatever it is (a network folder
    # written into settings.yaml by hand, say); a new one must be usable
    folder = _typed_folder(body.folder) if body.action == "add" else Path(mf.clean(body.folder))
    with _LOCK:
        if body.action == "add":
            if not folder.is_dir():
                raise HTTPException(status_code=400, detail=f"{folder} is not a folder on this PC")
            if mf.is_drive_root(folder):
                raise HTTPException(status_code=400, detail=(
                    "a whole drive can't be a model folder: choose the folder that holds "
                    "the model files, like D:\\ComfyUI\\models"))
            added = _add_dir(folder)
            msg = (f"added {folder}" if added else f"{folder} is already a model folder")
        else:
            dirs = _listed()
            keep = [d for d in dirs if not mf.same_dir(d, folder, ROOT)]
            if len(keep) == len(dirs):
                raise HTTPException(status_code=404, detail=f"{folder} is not a model folder")
            _save_dirs(keep)
            log.info("model folder removed: %s", folder)
            msg = f"removed {folder}"
    return {"ok": True, "message": msg, "folders": _folders(), "restart": True}


# ------------------------------------------------------------- search, copy
class FindBody(BaseModel):
    folder: str | None = None


def _start(kind: str, target, *args) -> dict:
    with _LOCK:
        if _JOB.get("state") == "running":
            raise HTTPException(status_code=409, detail="a search or copy is still running")
        _reset(kind, "running")
    threading.Thread(target=target, args=args, daemon=True, name=f"model-files-{kind}").start()
    return {"ok": True, "job": _job()}


def _finish(error: str | None = None) -> None:
    _JOB["error"] = error
    _JOB["ended"] = time.strftime("%H:%M:%S")
    _JOB["state"] = "failed" if error else "done"


def _run_find(folder: Path | None) -> None:
    try:
        cfg, cat = load_config(), mf.load_catalog(ROOT)
        mf.repair_case(ROOT, cat, _say)        # Kinoloom's own file under another letter case
        wanted = [r["id"] for r in mf.status(cat, cfg, ROOT)
                  if r["state"] == "missing" and not r["made_here"]]
        _COPIES.clear()
        if not wanted:
            _say("Every model file Kinoloom knows is already in place.")
            _finish()
            return
        finder = mf.Finder(cat, wanted, ROOT, say=_say)
        places = [p for p in [*mf.extra_roots(cfg, ROOT), ROOT / "models"] if p.is_dir()]
        found = finder.search(places + ([folder] if folder else []))
        for f in found:
            if f.how in mf.LINKED:
                try:
                    mf.use(f, ROOT, cat)
                    if f.how == "drivelink" and f.folder is not None:
                        with _LOCK:
                            if _add_dir(f.folder, first=True):
                                _JOB["added"].append(str(f.folder))
                except OSError as e:          # a link that did not work after all
                    root = mf.mount_root(f.src, f.rel)
                    f.how, f.folder = ("folder", root) if root else ("copy", None)
                    _say(f"could not link {f.src} ({e})")
                except HTTPException as e:    # the settings could not take the folder
                    f.how, f.folder = "copy", None
                    _say(str(e.detail))
            if f.how in mf.LINKED:
                _say(f"linked {Path(f.rel).name} = {f.src}"
                     + (f" (the link is in {f.folder})" if f.how == "drivelink" else ""))
            elif f.how == "folder" and f.folder is not None:
                try:
                    with _LOCK:
                        if _add_dir(f.folder):
                            _JOB["added"].append(str(f.folder))
                    _say(f"using {f.src} where it is (model folder {f.folder})")
                except HTTPException as e:
                    f.how, f.folder = "copy", None
                    _say(str(e.detail))
            if f.how == "copy":
                _COPIES.append(f)
                _JOB["copy"].append(_row(f))
                _say(f"{Path(f.rel).name}: {f.src} can be {f.describe()}")
            if f.how != "copy":
                _JOB["found"].append(_row(f))
        missing = len(finder.missing())
        _say(f"Found {len(found)} of {len(wanted)} missing file(s)"
             + (f"; {missing} still missing" if missing else "") + ".")
        _finish()
    except Exception as e:  # noqa: BLE001 - shown on the page and in the log
        log.exception("model file search failed")
        _finish(f"{type(e).__name__}: {e}")


def _run_copy(copies: list[mf.Found]) -> None:
    try:
        cat = mf.load_catalog(ROOT)
        need = sum(f.size for f in copies)
        free = shutil.disk_usage(ROOT).free
        if free < need * 1.02:
            raise OSError(f"{need / mf.GB:.1f} GB to copy, {free / mf.GB:.1f} GB free on {ROOT.anchor}")
        for f in copies:
            _say(f"copying {f.src} ({f.size / mf.GB:.2f} GB)...")
            mf.use(f, ROOT, cat, _say)
            _JOB["found"].append(_row(f))
            _COPIES.remove(f)
            _say(f"copied {Path(f.rel).name}")
        _finish()
    except Exception as e:  # noqa: BLE001
        log.exception("model file copy failed")
        _JOB["copy"] = [_row(f) for f in _COPIES]     # what is left, to try again
        _finish(f"{type(e).__name__}: {e}")


@router.post("/api/model-files/find")
def model_files_find(body: FindBody) -> dict:
    """Search the model folders, Kinoloom's own models folder and the given
    folder for the missing files, under any name, and use what is found."""
    folder = None
    if body.folder and mf.clean(body.folder):
        folder = _typed_folder(body.folder)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"{folder} is not a folder on this PC")
    return _start("find", _run_find, folder)


@router.post("/api/model-files/copy")
def model_files_copy() -> dict:
    """Copy the files the last search could neither link nor use in place."""
    if not _COPIES:
        raise HTTPException(status_code=400, detail="nothing to copy: search first")
    return _start("copy", _run_copy, list(_COPIES))


@router.get("/api/model-files/job")
def model_files_job() -> dict:
    return _job()
