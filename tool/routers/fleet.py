"""Kinoloom - Distributed Worker Fleet domain router.

Provides endpoints for worker node registration, heartbeats, draining, model deployment,
package download/upload, model group installations, and remote result reception.
"""
from __future__ import annotations

import ntpath
import os
import re
import time
import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import common
import security
import task_queue as _tq
from core import ROOT, is_safe_component, load_config, log_setup, now, save_config, to_jsonable

router = APIRouter()
log = log_setup("common.fleet")

def __getattr__(name: str):
    return getattr(common, name)

class WorkerBody(BaseModel):
    name: str
    host: str = ""
    port: int = 8120
    token: str = ""
    # The agent reports nvidia-smi rows (list) and a per-folder model
    # inventory (dict). Both were typed too narrowly (dict/list only), so
    # EVERY agent registration was rejected with 422 before this fix —
    # no worker could ever appear on the Workers page.
    gpu: list | dict | None = None
    models: list | dict | None = None
    capabilities: dict | None = None
    agent_version: str = ""


# Main-tree roots a worker install may read from. Package paths in
# install/model_packages.json are rooted at one of these.
_INSTALL_ROOTS = (("models",), ("ComfyUI", "models"))


def _install_source(rel: str) -> Path:
    """Resolve a main-tree model path for shipping to a worker.

    Only relative paths that stay inside ROOT/models or ROOT/ComfyUI/models
    are accepted — this endpoint streams file bytes off the main machine, so
    anything else (``C:/Users/...``, ``../settings.yaml``) is refused."""
    s = str(rel or "").replace("\\", "/").strip()
    p = Path(s)
    if not s or p.is_absolute() or p.drive or s.startswith("/") \
            or any(part == ".." for part in s.split("/")):
        raise ValueError("model path must be relative to models/ or ComfyUI/models/")
    fp = (ROOT / s).resolve()
    for parts in _INSTALL_ROOTS:
        base = ROOT.joinpath(*parts).resolve()
        if fp.is_relative_to(base) and fp != base:
            return fp
    raise ValueError("model path must be inside models/ or ComfyUI/models/")


def _public_worker(row: dict) -> dict:
    row = dict(row)
    row.pop("token_hash", None)
    return row


@router.get("/api/workers")
def workers_list() -> dict:
    """Fleet view for the Workers page: registry + dispatcher state."""
    import workers as workers_mod
    wcfg = load_config().get("workers") or {}
    return {"workers": [_public_worker(w) for w in workers_mod.list_all()],
            "queue": _tq.summary(),
            "settings": {"heartbeat_s": wcfg.get("heartbeat_s", 10),
                         "token_set": bool(wcfg.get("shared_token"))}}


@router.post("/api/workers/register")
def workers_register(body: WorkerBody, request: Request) -> dict:
    """Called by the worker agent at boot and then every heartbeat_s."""
    import workers as workers_mod
    # An agent that cannot tell its own address leaves host empty: use the
    # address its request arrived from.
    host = body.host or (request.client.host if request.client else "")
    try:
        return workers_mod.register(body.name, host, body.port,
                                    body.token, body.gpu, body.models,
                                    body.capabilities, body.agent_version)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/workers/{wid}/heartbeat")
def workers_heartbeat(wid: str, body: dict) -> dict:
    import workers as workers_mod
    try:
        return workers_mod.heartbeat(wid, body.get("token", ""),
                                     body.get("gpu"), body.get("models"),
                                     body.get("agent_version", ""))
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/workers/{wid}/result")
async def workers_result(wid: str, request: Request) -> dict:
    """Agent pushes finished artefacts here (multipart field `file`, query
    job= & name=); stored under temp/worker_results/{job}/ until the
    engine's client.download() copies them to the exact path it would have
    used locally.

    The body must declare its Content-Length and stay under
    workers.max_result_mb; the upload is spooled to disk by the multipart
    parser and copied in chunks, never held in memory whole."""
    import workers as workers_mod
    from starlette.concurrency import run_in_threadpool
    from starlette.datastructures import UploadFile

    row = workers_mod.get(wid)
    if not row:
        raise HTTPException(status_code=404, detail="unknown worker")
    token = (request.headers.get("X-Worker-Token")
             or security.provided_token(request.headers, {}))
    try:
        workers_mod._check_token(token, row)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))

    limit = workers_mod.max_result_bytes()
    try:
        declared = int(request.headers.get("content-length") or -1)
    except ValueError:
        declared = -1
    if declared < 0:
        raise HTTPException(status_code=411, detail="Content-Length required")
    if declared > limit + 64 * 1024:           # multipart framing overhead
        raise HTTPException(status_code=413, detail="result exceeds workers.max_result_mb")

    raw_job = request.query_params.get("job", "")
    raw_name = request.query_params.get("name", "")
    if not raw_job or not raw_name:
        raise HTTPException(status_code=400, detail="job and name query params required")
    clean_job = ntpath.basename(raw_job.replace("\\", "/").strip())
    clean_name = ntpath.basename(raw_name.replace("\\", "/").strip())
    if not clean_job or clean_job != raw_job or not clean_name or clean_name != raw_name or ".." in raw_job or ".." in raw_name:
        raise HTTPException(status_code=400, detail="Path traversal rejected")
    form = await request.form(max_files=1, max_fields=4)
    try:
        upload = next((f for _, f in form.multi_items() if isinstance(f, UploadFile)), None)
        if upload is None:
            raise HTTPException(status_code=400, detail="no file payload")
        try:
            return await run_in_threadpool(
                workers_mod.store_result, clean_job, clean_name, upload.file, limit)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        await form.close()


save_worker_result = workers_result


save_result = workers_result


@router.get("/api/workers/{wid}/test")
def workers_test(wid: str) -> dict:
    """Probe a worker's agent: reachability + selftest (comfy up, node
    classes, disk). Read-only on the worker — renders nothing."""
    import workers as workers_mod
    row = workers_mod.get(wid)
    if not row:
        raise HTTPException(status_code=404, detail="unknown worker")
    client = workers_mod.RemoteWorkerClient(wid)
    out = {"reachable": False, "selftest": None}
    try:
        out["ping"] = client._req("GET", "/ping", timeout=8)
        out["reachable"] = True
        out["selftest"] = client._req("GET", "/selftest", timeout=180)
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


@router.delete("/api/workers/{wid}")
def workers_delete(wid: str) -> dict:
    import workers as workers_mod
    return workers_mod.remove(wid)


@router.get("/api/workers/packages")
def workers_packages() -> dict:
    """Installable model packages for worker PCs (install/model_packages.json).

    A worker only ever gets what the operator picks here — exact files, exact
    layout, hash-verified in transit."""
    import json as _json
    p = ROOT / "install" / "model_packages.json"
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"packages manifest unreadable: {e}"})
    out = {}
    for name, pk in (data.get("packages") or {}).items():
        files = []
        total = 0
        for rel in (pk.get("paths") or []):
            try:
                fp = _install_source(str(rel))
            except ValueError:
                log.warning("package %s: path %r is outside the model roots", name, rel)
                continue
            if fp.is_file():
                sz = fp.stat().st_size
                total += sz
                files.append({"path": str(rel), "size_mb": round(sz / 2**20, 1)})
            elif fp.is_dir():
                files.append({"path": str(rel), "dir": True,
                              "size_mb": None})
        out[name] = {"description": pk.get("description", ""),
                     "files": files, "total_mb": round(total / 2**20, 1)}
    return {"ok": True, "packages": out}


class WorkerInstallBody(BaseModel):
    package: str | None = None
    files: list[str] | None = None


@router.post("/api/workers/{wid}/install")
def workers_install(wid: str, body: WorkerInstallBody) -> dict:
    """Stream model files from THIS machine to a worker's models dir.

    Exactly the files the operator picked — sha256 + size verified end to end
    (the agent refuses a bad payload), written with the same relative layout
    the main tree uses, so the worker runs byte-identical models to the ones
    every workflow was tuned on. Files are hashed and sent in chunks, so
    multi-GB weights never sit in memory on either side.
    """
    import hashlib
    import json as _json
    import urllib.parse
    import workers as workers_mod
    row = workers_mod.get(wid)
    if not row:
        raise HTTPException(status_code=404, detail="unknown worker")
    files: list[str] = []
    if body.files:
        files = [str(f) for f in body.files]
    elif body.package:
        p = ROOT / "install" / "model_packages.json"
        try:
            pk = (_json.loads(p.read_text(encoding="utf-8"))
                  .get("packages") or {}).get(body.package) or {}
        except Exception as e:
            log.exception("fleet operation failed: %s", e)
            return JSONResponse(status_code=500, content={"ok": False, "error": f"packages manifest unreadable: {e}"})
        files = [str(x) for x in (pk.get("paths") or [])
                 if not (ROOT / str(x)).is_dir()]
    if not files:
        return {"ok": False,
                "error": "nothing to install (unknown package or empty list)"}
    sources: list[tuple[str, Path]] = []
    for rel in files:
        try:
            sources.append((rel.replace("\\", "/"), _install_source(rel)))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{rel}: {e}")
    client = workers_mod.RemoteWorkerClient(wid)
    results = []
    ok_all = True
    for rel, fp in sources:
        if not fp.is_file():
            results.append({"file": rel, "ok": False,
                            "error": "not found on the main machine"})
            ok_all = False
            continue
        hasher = hashlib.sha256()
        with open(fp, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 2**20), b""):
                hasher.update(chunk)
        sha = hasher.hexdigest()
        size = fp.stat().st_size
        try:
            r = client.upload_stream(
                f"/install?path={urllib.parse.quote(rel)}&sha256={sha}"
                f"&size={size}", fp, timeout=7200)
            ok = bool(r.get("ok"))
            results.append({"file": rel, "ok": ok,
                            "verified": bool(r.get("verified")),
                            "error": r.get("error", "")})
            ok_all = ok_all and ok
        except Exception as e:  # noqa: BLE001
            results.append({"file": rel, "ok": False, "error": str(e)[:300]})
            ok_all = False
    return {"ok": ok_all, "results": results}


@router.post("/api/workers/token")
def workers_token_generate() -> dict:
    """Rotate the fleet's shared token (settings.yaml workers.shared_token).

    The API security gate already restricts this route to the local operator
    or an API-token holder. Rotating invalidates every installed worker: each
    one must be reinstalled (or have worker_config.yaml ``token`` updated)
    before it can register again."""
    cfg = common.cfg
    wcfg = cfg.get("workers")
    if not isinstance(wcfg, dict):
        wcfg = cfg["workers"] = {}
    wcfg["shared_token"] = security.generate_token()
    save_config(cfg)
    log.info("worker fleet token rotated")
    return {"ok": True, "token": wcfg["shared_token"]}


def render_installer(text: str, main_url: str, token: str, ui_host: str) -> str:
    """Fill the INSTALL-WORKER.bat placeholders. The template's own
    "was it filled in?" checks spell them as %_P%NAME%_P%, so this plain
    replace never touches them."""
    loopback = ui_host.strip().lower() in ("127.0.0.1", "localhost", "::1")
    warning = ("WARNING: the studio listens on " + ui_host + " only - set "
               "ui.host: 0.0.0.0 in tool/settings.yaml and restart it, or "
               "this worker cannot reach it") if loopback else ""
    # batch metacharacters in a value would break the generated script
    for v in (main_url, token):
        if any(c in v for c in '"%&|<>^!\r\n'):
            raise ValueError("installer value contains a batch metacharacter")
    return text.replace("__MAIN_URL__", main_url) \
               .replace("__TOKEN__", token) \
               .replace("__BIND_WARNING__", warning) \
               .replace("__WORKER_NAME__", "")


@router.get("/api/workers/installer")
def workers_installer(request: Request) -> StreamingResponse:
    """Serve INSTALL-WORKER.bat with this studio's URL + the shared token
    baked in. The template lives in install/INSTALL-WORKER.bat (never
    modified in place)."""
    import io
    from fastapi.responses import PlainTextResponse, StreamingResponse
    tpl = ROOT / "install" / "INSTALL-WORKER.bat"
    if not tpl.exists():
        return PlainTextResponse(
            "installer template not generated yet", status_code=404)
    cfg = load_config().get("workers") or {}
    token = cfg.get("shared_token") or ""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    # A worker must dial the MAIN MACHINE, never itself: if the installer is
    # downloaded while the studio is open as 127.0.0.1/localhost, swap in this
    # box's LAN IP (the request port is kept). Downloading over the LAN keeps
    # the host the browser used.
    h = str(host).split(":")[0].lower()
    if h in ("127.0.0.1", "localhost", "::1"):
        import socket
        port = (str(host).split(":")[1] if ":" in str(host) else "8080")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                host = f"{s.getsockname()[0]}:{port}"
            finally:
                s.close()
        except Exception:
            host = f"127.0.0.1:{port}"
    main_url = f"{scheme}://{host}"
    ui_host = str((load_config().get("ui") or {}).get("host") or "127.0.0.1")
    try:
        text = render_installer(tpl.read_text(encoding="utf-8"),
                                main_url, token, ui_host)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="application/x-bat",
        headers={"Content-Disposition": 'attachment; filename="INSTALL-WORKER.bat"'}
    )


@router.get("/api/workers/groups")
def workers_groups() -> dict:
    """Summarise install/colab_model_groups.json for the Workers page."""
    import json as _json
    p = ROOT / "install" / "colab_model_groups.json"
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"groups manifest unreadable: {e}"})
    groups = {}
    for name, g in (data.get("groups") or {}).items():
        files = g.get("files") or []
        known = sum(int(f.get("bytes") or 0) for f in files)
        groups[name] = {"description": g.get("description", ""),
                        "files": len(files),
                        "total_gb": round(known / 2**30, 1) if known else None,
                        "measured": bool(known)}
    return {"ok": True, "hf_repo_set": bool(data.get("hf_repo")),
            "groups": groups}


def _group_file_specs(names: list[str]) -> list[dict]:
    import json as _json
    p = ROOT / "install" / "colab_model_groups.json"
    data = _json.loads(p.read_text(encoding="utf-8"))
    repo = str(data.get("hf_repo") or "")
    rev = str(data.get("hf_revision") or "main")
    if not repo:
        raise ValueError("manifest has no hf_repo — publish the models first "
                         "(tool/scripts/publish_models_hf.py)")
    seen: set[str] = set()
    specs: list[dict] = []
    for name in names:
        g = (data.get("groups") or {}).get(name)
        if not g:
            raise ValueError(f"unknown group: {name}")
        for f in (g.get("files") or []):
            path = str(f.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            specs.append({"path": path, "hf_repo": repo, "hf_file": path,
                          "hf_revision": rev,
                          "bytes": int(f.get("bytes") or 0),
                          "sha256": str(f.get("sha256") or "")})
    return specs


class WorkerGroupBody(BaseModel):
    groups: list[str] | None = None
    files: list[dict] | None = None


@router.post("/api/workers/{wid}/install_group")
def workers_install_group(wid: str, body: WorkerGroupBody) -> dict:
    """Start a background group install on the worker; returns an install id
    to poll via GET /api/workers/{wid}/install_group/{gid}."""
    import uuid as _uuid
    import workers as workers_mod
    if not workers_mod.get(wid):
        raise HTTPException(status_code=404, detail="unknown worker")
    try:
        files = body.files or _group_file_specs(body.groups or [])
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    if not files:
        return {"ok": False, "error": "no files in the selected group(s)"}
    gid = _uuid.uuid4().hex[:10]
    try:
        r = workers_mod.RemoteWorkerClient(wid).install_group(gid, files)
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"worker unreachable: {e}"})
    return {"ok": True, "install_id": r.get("install_id", gid),
            "files": len(files)}


@router.get("/api/workers/{wid}/install_group/{gid}")
def workers_install_group_status(wid: str, gid: str) -> dict:
    import workers as workers_mod
    try:
        return workers_mod.RemoteWorkerClient(wid).install_group_status(gid)
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "error": str(e)[:300]}


class WorkerInstallUrlBody(BaseModel):
    path: str
    url: str = ""
    hf_repo: str = ""
    hf_file: str = ""
    hf_revision: str = "main"
    bytes: int = 0
    sha256: str = ""


@router.post("/api/workers/{wid}/install_url")
def workers_install_url(wid: str, body: WorkerInstallUrlBody) -> dict:
    """One-off pull of a single file on the worker's own pipe (advanced —
    group installs via the manifest are the normal path)."""
    import workers as workers_mod
    if not workers_mod.get(wid):
        raise HTTPException(status_code=404, detail="unknown worker")
    spec = {k: getattr(body, k) for k in
            ("path", "url", "hf_repo", "hf_file", "hf_revision", "bytes",
             "sha256")}
    try:
        return workers_mod.RemoteWorkerClient(wid).install_url(spec)
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:300]})

@router.delete("/api/workers/{wid}/models")
def workers_delete_model(wid: str, path: str) -> dict:
    import workers as workers_mod
    if not workers_mod.get(wid):
        raise HTTPException(status_code=404, detail="unknown worker")
    try:
        return workers_mod.RemoteWorkerClient(wid).delete_model(path)
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:300]})

@router.get("/api/workers/{wid}/disk")
def workers_disk(wid: str) -> dict:
    import workers as workers_mod
    try:
        return workers_mod.RemoteWorkerClient(wid).disk()
    except Exception as e:
        log.exception("fleet operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:300]})
