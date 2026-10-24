"""Kinoloom - Hardware, VRAM and GPU Broker domain router.

Provides endpoints for VRAM status, auto-offload management, GPU broker leases,
and render mode hardware controls.
"""
from __future__ import annotations

import os
import re
import time
import json
import urllib.request
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import common
import gpu_broker as _gpu
from core import ROOT, log_setup, now, save_config

router = APIRouter()
log = log_setup("common.hardware")

VRAM_DEFAULTS = {"auto_offload": True, "idle_minutes": 8, "poll_seconds": 30}


VRAM_STATE: dict = {"idle_since": time.time(), "released": False,
                    "last_release": None, "ticks": 0,
                    "last_tick": None, "last_busy": None}


def _vram_settings() -> dict:
    """Current `vram` settings with defaults + safe clamping."""
    try:
        raw = common.cfg.get("vram") or {}
    except Exception:
        raw = {}
    out = dict(VRAM_DEFAULTS)
    for k in out:
        if k in raw:
            out[k] = raw[k]
    try:
        out["idle_minutes"] = max(1, min(120, int(float(out["idle_minutes"]))))
    except Exception:
        out["idle_minutes"] = 8
    try:
        out["poll_seconds"] = max(5, min(300, int(float(out["poll_seconds"]))))
    except Exception:
        out["poll_seconds"] = 30
    out["auto_offload"] = bool(out["auto_offload"])
    return out


def _broker_manages() -> bool:
    """True once the GPU broker owns local model lifecycles."""
    try:
        import gpu_broker
        return bool(gpu_broker.is_owner()
                    and gpu_broker.broker.settings().get("enabled"))
    except Exception:
        return False


def _farm_ports() -> list[int]:
    """Every studio ComfyUI port: the production farm + registry models
    (video_models AND the image registry — Qwen-Image-2.1 lives on its own
    models.py-declared instance, port 8190)."""
    ports = set()
    try:
        ports.add(int(common.cfg["ensemble"]["comfyui_port"]))
    except Exception:
        pass
    try:
        import video_models
        for m in video_models.VIDEO_MODELS.values():
            cp = m.get("comfy_port")
            if cp:
                ports.add(int(cp))
    except Exception:
        pass
    try:
        import models as _img_models
        for m in _img_models.MODELS.values():
            cp = m.get("comfy_port")
            if cp:
                ports.add(int(cp))
    except Exception:
        pass
    return sorted(ports)


def _release_ports() -> list[int]:
    """Ports the idle watchdog may release.

    The MiniMax H3 farm is EXCLUDED: reloading its 15 GB text encoder after a
    /free has stalled (livelock during dynamic-VRAM staging — measured twice),
    and its VRAM is already leased away by `_free_other_instances()` before
    any other farm runs a job. Releasing it while idle buys nothing and can
    wedge the next H3 render."""
    skip = set()
    try:
        import video_models
        for m in video_models.VIDEO_MODELS.values():
            fam = str(m.get("family") or "")
            cp = m.get("comfy_port")
            if cp and fam.startswith("h3"):
                skip.add(int(cp))
    except Exception:
        pass
    return [p for p in _farm_ports() if p not in skip]


def _comfy_busy(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/queue", timeout=5) as r:
            q = json.load(r)
        return bool(q.get("queue_running") or q.get("queue_pending"))
    except Exception:
        return False


def _release_farm(port: int) -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/free",
            data=json.dumps({"unload_models": True,
                             "free_memory": True}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _render_activity() -> bool:
    """True while any render/post work is underway anywhere."""
    try:
        import video_studio
        for j in video_studio.jobs(limit=10):
            if j.get("status") in ("queued", "running"):
                return True
    except Exception:
        pass
    try:
        row = common.db.con.execute(
            "SELECT COUNT(*) n FROM shots WHERE status IN "
            "('rendering','post')").fetchone()
        if row and row["n"]:
            return True
    except Exception:
        pass
    return any(_comfy_busy(p) for p in _farm_ports())


def _vram_idle_watchdog() -> None:
    """Keep models warm; release both farms after sustained full idleness.

    Resilient by design: every iteration is wrapped, a failed
    activity probe counts as BUSY (never release on an evaluation error) and
    the thread catches exceptions.
    """
    print("vram idle watchdog started (release after sustained idle)",
          flush=True)
    while True:
        try:
            s = _vram_settings()
            try:
                busy = _render_activity()
            except Exception:
                busy = True          # fail safe: uncertain -> do not release
            VRAM_STATE["ticks"] = VRAM_STATE.get("ticks", 0) + 1
            VRAM_STATE["last_tick"] = now()
            VRAM_STATE["last_busy"] = busy
            if busy:
                VRAM_STATE["idle_since"] = time.time()
                VRAM_STATE["released"] = False
            elif s["auto_offload"] and not VRAM_STATE["released"]:
                if _broker_manages():
                    # the GPU broker owns per-model TTL eviction + verification
                    VRAM_STATE["released"] = True
                    VRAM_STATE["last_release"] = now()
                    continue
                idle_for = time.time() - VRAM_STATE["idle_since"]
                if idle_for >= s["idle_minutes"] * 60:
                    done = [p for p in _release_ports() if _release_farm(p)]
                    VRAM_STATE["released"] = True
                    VRAM_STATE["last_release"] = now()
                    print(f"vram idle watchdog: released {done} after "
                          f"{int(idle_for)}s idle (threshold "
                          f"{s['idle_minutes']} min)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"vram idle watchdog error: {e}", flush=True)
            try:
                VRAM_STATE["released"] = False   # retry next tick
            except Exception:
                pass
        time.sleep(_vram_settings()["poll_seconds"])


_WATCHDOG_LOCK = threading.Lock()
_WATCHDOG: threading.Thread | None = None


def start_vram_watchdog() -> bool:
    """Start the idle watchdog once. Called from the app lifespan (never at
    import time, so importing the API for tests or tooling has no side
    effects on live ComfyUI instances). Returns True when it is running."""
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        if _WATCHDOG is not None and _WATCHDOG.is_alive():
            return True
        try:
            _WATCHDOG = threading.Thread(target=_vram_idle_watchdog,
                                         daemon=True,
                                         name="vram-idle-watchdog")
            _WATCHDOG.start()
            return True
        except Exception as e:  # noqa: BLE001
            print(f"vram idle watchdog start failed: {e}")
            return False


@router.post("/api/render_mode/free")
def free_vram() -> dict:
    return {"ok": True, **common.render_mode.free_vram()}


class VramBody(BaseModel):
    auto_offload: bool | None = None
    idle_minutes: int | None = None
    poll_seconds: int | None = None


def _farm_status() -> list[dict]:
    farms = []
    for p in _farm_ports():
        mem = None
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{p}/system_stats", timeout=5) as r:
                st = json.load(r)
            dev = (st.get("devices") or [{}])[0]
            free_b, total_b = dev.get("vram_free"), dev.get("vram_total")
            if free_b and total_b:
                mb = 1024 * 1024
                mem = {"free_mb": int(free_b // mb),
                       "total_mb": int(total_b // mb)}
        except Exception:
            pass
        farms.append({"port": p, "busy": _comfy_busy(p), "memory": mem})
    try:
        import gpu_broker
        if gpu_broker.is_owner():
            bx = {p["port"]: p for p in gpu_broker.state().get("ports", [])}
            for f in farms:
                b = bx.get(f["port"]) or {}
                f["state"] = b.get("state")
                f["model"] = b.get("model")
                f["warm_remaining_s"] = b.get("warm_remaining_s")
                f["h3"] = b.get("h3")
    except Exception:
        pass
    return farms


@router.get("/api/vram")
def vram_status() -> dict:
    s = _vram_settings()
    farms = _farm_status()
    busy = any(f["busy"] for f in farms)
    idle_for = 0 if busy else int(time.time() - VRAM_STATE["idle_since"])
    broker_state = None
    try:
        import gpu_broker
        if gpu_broker.is_owner():
            broker_state = gpu_broker.state()
    except Exception:
        broker_state = None
    if broker_state is not None:
        warm = [p for p in broker_state["ports"] if p["state"] == "warm"]
        release_in = (max((p.get("warm_remaining_s") or 0) for p in warm)
                      if warm else None)
        released = not warm and not broker_state.get("active_lease")
    else:
        release_in = None
        if s["auto_offload"] and not VRAM_STATE["released"]:
            release_in = max(0, int(s["idle_minutes"] * 60) - idle_for)
        released = VRAM_STATE["released"]
    return {"ok": True, "settings": s,
            "state": {"idle_for_s": idle_for,
                      "release_in_s": release_in,
                      "released": released,
                      "last_release": VRAM_STATE["last_release"],
                      "ticks": VRAM_STATE.get("ticks", 0),
                      "last_tick": VRAM_STATE.get("last_tick"),
                      "last_busy": VRAM_STATE.get("last_busy"),
                      "reconciling": bool(broker_state and
                                          broker_state.get("reconciling")),
                      "active_lease": (broker_state or {}).get("active_lease")},
            "farms": farms,
            "broker": broker_state}


@router.post("/api/vram")
def vram_set(body: VramBody) -> dict:
    cur = _vram_settings()
    if body.auto_offload is not None:
        cur["auto_offload"] = bool(body.auto_offload)
    if body.idle_minutes is not None:
        cur["idle_minutes"] = max(1, min(120, int(body.idle_minutes)))
    if body.poll_seconds is not None:
        cur["poll_seconds"] = max(5, min(300, int(body.poll_seconds)))
    common.cfg["vram"] = cur
    save_config(common.cfg)
    return {"ok": True, "settings": cur}


@router.post("/api/vram/offload")
def vram_offload() -> dict:
    """Direct offload — busy farms are skipped, never interrupted."""
    if _broker_manages():
        res = _gpu.evict_all(mode="warm")
        VRAM_STATE["idle_since"] = time.time()
        VRAM_STATE["released"] = True
        VRAM_STATE["last_release"] = now()
        return {"ok": bool(res.get("ok")), "farms": res.get("results", {}),
                "error": res.get("error")}
    results: dict[str, str] = {}
    for p in _farm_ports():
        if _comfy_busy(p):
            results[str(p)] = "skipped-busy"
        else:
            results[str(p)] = "released" if _release_farm(p) else "unreachable"
    VRAM_STATE["idle_since"] = time.time()
    VRAM_STATE["released"] = True
    VRAM_STATE["last_release"] = now()
    return {"ok": True, "farms": results}


class GpuLeaseBody(BaseModel):
    model: str | None = None
    port: int | None = None
    purpose: str = "remote"
    timeout: float = 3600.0
    hold_s: float | None = None


class GpuReleaseBody(BaseModel):
    lease_id: str


class GpuEvictBody(BaseModel):
    port: int | None = None
    mode: str = "warm"


@router.get("/api/gpu/health")
def gpu_health() -> dict:
    """GPU telemetry only (name, temperature, VRAM, utilisation) for the
    sidebar meter both shells poll; /api/status also reads the bible's git head."""
    try:
        return {"gpu": common.render_mode.health()}
    except OSError:                       # no nvidia-smi on this machine
        return {"gpu": []}


@router.get("/api/gpu/state")
def gpu_state() -> dict:
    return _gpu.state()


@router.post("/api/gpu/lease")
def gpu_lease(body: GpuLeaseBody) -> dict:
    if body.model is None and body.port is None:
        return {"ok": False, "error": "model or port required"}
    hold = max(60.0, float(body.hold_s if body.hold_s is not None else 3600))
    try:
        # bind_thread=False: this request runs on a shared server thread; the
        # lease belongs to the calling process, not to this thread
        lease = _gpu.acquire(model=body.model, port=body.port,
                             purpose=body.purpose, timeout=body.timeout,
                             hold_s=hold, bind_thread=False)
    except Exception as e:
        log.exception("hardware operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return {"ok": True, "lease_id": lease.id, "port": lease.port,
            "managed": lease.managed}


@router.post("/api/gpu/release")
def gpu_release(body: GpuReleaseBody) -> dict:
    return {"ok": _gpu.release_by_id(body.lease_id)}


@router.post("/api/gpu/cancel")
def gpu_cancel() -> dict:
    return _gpu.cancel_active()


@router.post("/api/gpu/evict")
def gpu_evict(body: GpuEvictBody) -> dict:
    if body.port is None:
        return _gpu.evict_all(mode=body.mode)
    return _gpu.evict(int(body.port), mode=body.mode)

