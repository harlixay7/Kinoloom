"""FastAPI backend — the control surface for the web UI.

Request pipeline (outermost first):

1. ``CORSMiddleware``      answers preflights and decorates every response,
                           including 401/403s, with CORS headers.
2. ``_response_headers``   no-store caching for the UI shell + static assets
                           and baseline security headers.
3. ``_security_gate``      Host (DNS rebinding), Origin (CSRF) and
                           authentication checks — see ``security.py``.
4. Domain routers in ``routers/`` (shared singletons live in
   ``routers/common.py``).

Background services (GPU broker reconcile, queue dispatcher, dataset worker,
VRAM idle watchdog) start in ``lifespan`` and never at import time, so
importing this module for tests or tooling has no side effects on a live
studio.
"""
from __future__ import annotations

import os as _os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# This process OWNS the video_studio job threads, so it is the only one
# allowed to sweep orphaned jobs (see video_studio). Tests and diagnostic
# scripts set KINOLOOM_OWN_JOBS=0 before importing.
_os.environ.setdefault("KINOLOOM_OWN_JOBS", "1")

import security  # noqa: E402
from core import InvalidInput, save_config  # noqa: E402
from routers import common  # noqa: E402
from routers.common import STATIC, cfg, log  # noqa: E402,F401  (cfg re-exported for scripts)


def _bind_host() -> str:
    """The address uvicorn was told to bind (launcher passes ui.host)."""
    return (_os.environ.get("KINOLOOM_BIND_HOST")
            or str((common.cfg.get("ui") or {}).get("host") or "127.0.0.1"))


def _startup_security() -> None:
    """Generate missing secrets before the first request is served."""
    notes = security.ensure_secrets(common.cfg, _bind_host())
    if not notes:
        return
    save_config(common.cfg)
    for note in notes:
        # Console only: never write secret material (or hints of it) to logs.
        print(f"[security] {note}; stored in tool/settings.yaml", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_security()
    common._recover_stuck_shots()
    common._queue_boot()
    from routers import hardware as _hardware
    _hardware.start_vram_watchdog()
    yield
    try:
        import task_queue as _tq_shutdown
        _tq_shutdown.stop_dispatcher()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="Kinoloom", version="0.1.1", lifespan=lifespan)


# ------------------------------------------------------------- error mapping
@app.exception_handler(FileNotFoundError)
async def _not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})


@app.exception_handler(PermissionError)
async def _forbidden(request: Request, exc: PermissionError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"ok": False, "error": str(exc) or "forbidden"})


@app.exception_handler(InvalidInput)
async def _invalid_input(request: Request, exc: InvalidInput) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc) or "invalid input"})


# ------------------------------------------------------------- security gate
def _deny(status: int, error: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


@app.middleware("http")
async def _security_gate(request: Request, call_next):
    # A NUL byte is never part of a legitimate path or name; pathlib raises
    # ValueError on it, which surfaced as a 500 from the file endpoints.
    if "\x00" in request.url.path or "\x00" in str(request.query_params) \
            or "%00" in request.url.query:
        return _deny(400, "invalid character in request")
    sec = security.settings(common.cfg)
    host_hdr = request.headers.get("host", "")
    if not security.host_allowed(host_hdr, sec["allowed_hosts"]):
        return _deny(400, "Host header not allowed (add it to security.allowed_hosts)")
    if not security.origin_allowed(request.method, request.headers, host_hdr,
                                   sec["cors_origins"]):
        return _deny(403, "Cross-origin request blocked")

    path = request.url.path
    if security.is_public_path(path) or security.is_worker_callback(request.method, path):
        return await call_next(request)

    client_host = request.client.host if request.client else ""
    if (sec["local_bypass"] and security.is_loopback_client(client_host)
            and security.is_loopback_host_header(host_hdr)):
        return await call_next(request)

    expected = sec["api_token"]
    if not expected:
        return _deny(401, "Remote access requires an API token: set "
                          "security.api_token in tool/settings.yaml (or the "
                          "KINOLOOM_API_TOKEN environment variable)")
    provided = security.provided_token(request.headers, request.cookies)
    if not security.token_matches(provided, expected):
        return _deny(401, "Unauthorized")
    return await call_next(request)


# ------------------------------------------------------------- response headers
_SHELL_PATHS = ("/", "/project", "/studio-tools")


@app.middleware("http")
async def _response_headers(request: Request, call_next):
    """Never let the browser serve a stale UI (version queries + no-store
    kill the cached-old-page class of bugs) and set baseline hardening."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in _SHELL_PATHS:
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# ------------------------------------------------------------- CORS (outermost)
_cors = security.settings(cfg)["cors_origins"] or [
    "http://127.0.0.1:8080", "http://localhost:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------- static files
class _NoCacheStatic(StaticFiles):
    """Serve UI assets with no-cache headers."""
    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStatic(directory=STATIC), name="static")


@app.get("/api/health")
def health() -> dict:
    """Unauthenticated liveness probe (reveals nothing about studio state)."""
    return {"ok": True}


# ------------------------------------------------------------- mount routers
from routers import (  # noqa: E402
    system as _r_system,
    shots_bible as _r_shots_bible,
    images as _r_images,
    training as _r_training,
    pipeline as _r_pipeline,
    hardware as _r_hardware,
    assembly as _r_assembly,
    projects as _r_projects,
    project_shots as _r_project_shots,
    cast as _r_cast,
    media_review as _r_media_review,
    videostudio as _r_videostudio,
    fleet as _r_fleet,
    queue as _r_queue,
    model_folders as _r_model_folders,
)

_ROUTERS = [
    _r_system.router,
    _r_shots_bible.router,
    _r_images.router,
    _r_training.router,
    _r_pipeline.router,
    _r_hardware.router,
    _r_assembly.router,
    _r_projects.router,
    _r_project_shots.router,
    _r_cast.router,
    _r_media_review.router,
    _r_videostudio.router,
    _r_fleet.router,
    _r_queue.router,
    _r_model_folders.router,
]

for _router in _ROUTERS:
    app.include_router(_router)

# Flatten app.router.routes so that len(app.routes) reflects all individual
# routes and route introspection works without AttributeError.
_flattened = []
for r in app.routes:
    if hasattr(r, "original_router"):
        _flattened.extend(r.original_router.routes)
    else:
        _flattened.append(r)
app.router.routes = _flattened

# ------------------------------------------------------------- re-export symbols
# Handlers are re-exported so scripts/tests can call them as api.<name>.
from routers.system import *  # noqa: E402,F401,F403
from routers.shots_bible import *  # noqa: E402,F401,F403
from routers.images import *  # noqa: E402,F401,F403
from routers.training import *  # noqa: E402,F401,F403
from routers.pipeline import *  # noqa: E402,F401,F403
from routers.hardware import *  # noqa: E402,F401,F403
from routers.assembly import *  # noqa: E402,F401,F403
from routers.projects import *  # noqa: E402,F401,F403
from routers.project_shots import *  # noqa: E402,F401,F403
from routers.cast import *  # noqa: E402,F401,F403
from routers.media_review import *  # noqa: E402,F401,F403
from routers.videostudio import *  # noqa: E402,F401,F403
from routers.fleet import *  # noqa: E402,F401,F403
from routers.queue import *  # noqa: E402,F401,F403
