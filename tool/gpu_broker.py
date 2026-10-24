"""GPU model-lifecycle broker — one authority for the local ComfyUI fleet.

The studio runs four ComfyUI servers on one 8 GB card (8188 farm, 8189 LTX,
8199 H3, 8190 Qwen). This module owns their model lifecycles so every
generation starts from a known state:

  * one local render at a time (global lease);
  * before a job starts, every instance other than the target is EMPTY and
    that emptiness is verified (`/queue` empty and `vram_free` back at the
    boot baseline);
  * after a job, the target stays WARM for a per-model TTL (default 8 min);
  * a request for another model evicts the warm stack on demand;
  * wedged instances are restarted (H3 always restarts: its aimdo 0.5.3
    livelocks on /free -> reload);
  * startup and resume-after-sleep reconcile to a clean slate.

Remote workers are out of scope: a port that is not in the local registry
gets a no-op lease, so worker lanes keep their own serialization.

ComfyUI `/free` is asynchronous (a flag consumed between prompts), so every
eviction is verified by polling `/queue` and `/system_stats`.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    import psutil
except ImportError:
    _venv_site = Path(__file__).resolve().parent.parent / "tool" / ".venv" / "Lib" / "site-packages"
    if _venv_site.exists() and str(_venv_site) not in sys.path:
        sys.path.insert(0, str(_venv_site))
    import psutil

from core import log_setup

log = log_setup("gpu_broker")

_DECISIONS_MAX = 200
_EMPTY_FALLBACK_MB = 5500      # an 8 GB card idles at ~7.1 GB free
_RESUME_GAP_S = 90.0           # wall-vs-monotonic drift that means "resumed"
_HTTP_TIMEOUT = 10.0

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GpuBusyError(RuntimeError):
    """Another local render held the GPU past the caller's deadline."""


class GpuUnavailableError(RuntimeError):
    """The target instance could not be brought to a usable state."""


# --------------------------------------------------------------- helpers
def _http_json(port: int, path: str, payload: dict | None = None,
               timeout: float = _HTTP_TIMEOUT) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def _vram_free_mb(port: int) -> int | None:
    try:
        st = _http_json(port, "/system_stats", timeout=5)
    except Exception:
        return None
    devs = st.get("devices") or []
    if not devs:
        return None
    free = devs[0].get("vram_free")
    if free is None:
        return None
    return int(free // (1024 * 1024))


def _queue_empty(port: int) -> bool | None:
    try:
        q = _http_json(port, "/queue", timeout=5)
    except Exception:
        return None
    return not (q.get("queue_running") or q.get("queue_pending"))


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _port_pids(port: int) -> list[int]:
    """PIDs listening on port or whose command line runs `main.py --port N`."""
    pids: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            try:
                if (conn.laddr and conn.laddr.port == port and
                        conn.status == psutil.CONN_LISTEN and conn.pid):
                    pids.add(conn.pid)
            except (AttributeError, psutil.AccessDenied):
                continue
    except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
        pass

    target = f"main.py --port {port}"
    try:
        for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline")
                if cmdline:
                    cmd_str = " ".join(str(c) for c in cmdline)
                    if target in cmd_str:
                        pids.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        pass

    return sorted(pids)


class Lease:
    """One granted GPU slot. Remote (unmanaged) leases are no-ops."""

    __slots__ = ("id", "port", "model", "purpose", "created", "expires_at",
                 "managed", "cancelled")

    def __init__(self, port: int, model: str | None, purpose: str,
                 managed: bool = True, timeout: float | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.port = int(port)
        self.model = model
        self.purpose = purpose
        self.created = time.time()
        self.expires_at = (time.time() + timeout) if timeout else None
        self.managed = managed
        self.cancelled = False

    def as_dict(self) -> dict:
        return {"id": self.id, "port": self.port, "model": self.model,
                "purpose": self.purpose, "managed": self.managed,
                "held_s": round(time.time() - self.created, 1),
                "cancelled": self.cancelled}


class _Instance:
    __slots__ = ("port", "state", "model", "deadline", "baseline_mb",
                 "last_used", "last_error", "lease_id", "h3")

    def __init__(self, port: int, h3: bool = False):
        self.port = port
        self.state = "unknown"        # unknown|empty|warm|busy|degraded
        self.model = None
        self.deadline = 0.0
        self.baseline_mb = None
        self.last_used = 0.0
        self.last_error = ""
        self.lease_id = None
        self.h3 = h3


class _Broker:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active: Lease | None = None
        self._tls = threading.local()
        self._instances: dict[int, _Instance] = {}
        self._next_ticket = 0
        self._waiters: dict[int, bool] = {}
        self._reconciling = False
        self._needs_reconcile = False
        self._started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._decisions: list[dict] = []
        self._last_wall = time.time()
        self._last_mono = time.monotonic()
        self._registry_cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------ registry
    def _registry(self) -> dict:
        """{port: {"models": set(), "h3": bool}} for the local fleet."""
        now = time.time()
        if self._registry_cache and now - self._registry_cache[0] < 30:
            return self._registry_cache[1]
        reg: dict[int, dict] = {}
        try:
            from core import load_config
            port = int(load_config()["ensemble"]["comfyui_port"])
            reg.setdefault(port, {"models": set(), "h3": False})
        except Exception:
            port = None
        try:
            import models as _m
            for m in _m.MODELS.values():
                p = m.get("comfy_port")
                if p:
                    reg.setdefault(int(p), {"models": set(), "h3": False})
                    reg[int(p)]["models"].add(m.get("id"))
        except Exception as e:
            log.warning("registry: image models unavailable: %s", e)
        try:
            import video_models as _v
            for m in _v.VIDEO_MODELS.values():
                p = m.get("comfy_port")
                if p:
                    entry = reg.setdefault(int(p), {"models": set(), "h3": False})
                    entry["models"].add(m.get("id"))
                    if str(m.get("family", "")).startswith("h3"):
                        entry["h3"] = True
        except Exception as e:
            log.warning("registry: video models unavailable: %s", e)
        self._registry_cache = (now, reg)
        return reg

    def _instance(self, port: int) -> _Instance:
        if port not in self._instances:
            reg = self._registry()
            self._instances[port] = _Instance(port, reg.get(port, {}).get("h3", False))
        return self._instances[port]

    def _ports(self) -> list[int]:
        return sorted(self._registry().keys())

    def is_local(self, port: int) -> bool:
        return int(port) in self._registry()

    def port_for_model(self, model: str | None) -> int | None:
        if not model:
            return None
        for port, info in self._registry().items():
            if model in info["models"]:
                return port
        try:
            import models as _m
            m = _m.MODELS.get(model)
            if m and m.get("comfy_port"):
                return int(m["comfy_port"])
        except Exception:
            pass
        try:
            import video_models as _v
            m = _v.VIDEO_MODELS.get(model)
            if m and m.get("comfy_port"):
                return int(m["comfy_port"])
        except Exception:
            pass
        return None

    def family_of(self, model: str | None) -> str:
        if not model:
            return ""
        try:
            import models as _m
            m = _m.MODELS.get(model)
            if m:
                return str(m.get("family") or "")
        except Exception:
            pass
        try:
            import video_models as _v
            m = _v.VIDEO_MODELS.get(model)
            if m:
                return str(m.get("family") or "")
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        try:
            from core import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
        gb = cfg.get("gpu_broker") or {}
        vram = cfg.get("vram") or {}
        ttl = gb.get("ttl_minutes")
        if not isinstance(ttl, dict):
            ttl = {}
        try:
            default_ttl = int(gb.get("default_ttl_minutes",
                                    vram.get("idle_minutes", 8)) or 8)
        except (TypeError, ValueError):
            default_ttl = 8
        return {
            "enabled": bool(gb.get("enabled", vram.get("auto_offload", True))),
            "default_ttl_minutes": max(1, min(120, default_ttl)),
            "ttl_minutes": ttl,
            "poll_seconds": max(5, min(300, int(
                gb.get("poll_seconds", vram.get("poll_seconds", 30)) or 30))),
            "evict_keep_conditioning": bool(
                gb.get("evict_keep_conditioning", True)),
            "verify_timeout_s": max(10, min(300, int(
                gb.get("verify_timeout_s", 60)))),
            "vram_tolerance_mb": max(64, int(gb.get("vram_tolerance_mb", 512))),
            "restart_on_verify_fail": bool(
                gb.get("restart_on_verify_fail", True)),
            "reconcile_on_start": bool(gb.get("reconcile_on_start", True)),
            "reconcile_on_resume": bool(gb.get("reconcile_on_resume", True)),
            "h3_mode": str(gb.get("h3_mode", "restart")),
            "max_warm_stacks": max(1, int(gb.get("max_warm_stacks", 1))),
            "prewarm": bool(gb.get("prewarm", True)),
            "prewarm_model": str(gb.get("prewarm_model", "qwen21_fast")),
            "prewarm_size": str(gb.get("prewarm_size", "1024x1024")),
            "prewarm_steps": max(1, int(gb.get("prewarm_steps", 8))),
            "prewarm_max_rounds": max(1, int(gb.get("prewarm_max_rounds", 5))),
            "prewarm_fast_seconds": float(gb.get("prewarm_fast_seconds", 150)),
        }

    def _ttl_seconds(self, model: str | None) -> float:
        s = self.settings()
        ttl = s["ttl_minutes"]
        fam = self.family_of(model)
        for key in (model, fam, "default"):
            if key and key in ttl:
                try:
                    return max(1, int(ttl[key])) * 60.0
                except (TypeError, ValueError):
                    continue
        return float(s["default_ttl_minutes"]) * 60.0

    # ------------------------------------------------------------ decisions
    def _decide(self, action: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action}
        rec.update(fields)
        log.info("%s %s", action, json.dumps(fields, default=str))
        with self._cond:
            self._decisions.append(rec)
            if len(self._decisions) > _DECISIONS_MAX:
                del self._decisions[:-_DECISIONS_MAX]

    # ------------------------------------------------------------ lifecycle
    def start(self, reconcile: bool | None = None) -> None:
        """Idempotent. Starts the maintenance thread + optional reconcile."""
        s = self.settings()
        do_reconcile = s["reconcile_on_start"] if reconcile is None else reconcile
        with self._cond:
            if self._started:
                return
            self._started = True
            if do_reconcile:
                # block acquires from this instant: no job may run on top of
                # a state that has not been reconciled yet
                self._reconciling = True

        def _boot() -> None:
            if do_reconcile:
                try:
                    self._reconcile_body("startup")
                finally:
                    with self._cond:
                        self._reconciling = False
                        self._last_wall = time.time()
                        self._last_mono = time.monotonic()
                        self._cond.notify_all()
            self._warm_qwen("startup")
            self._maintenance_loop()

        self._thread = threading.Thread(target=_boot, daemon=True,
                                        name="gpu-broker")
        self._thread.start()
        self._decide("broker-start", reconcile=do_reconcile,
                     ports=self._ports())

    def is_owner(self) -> bool:
        return self._started

    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            poll = self.settings()["poll_seconds"]
            self._stop.wait(poll)
            if self._stop.is_set():
                return
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("maintenance tick failed: %s", e)

    def _tick(self) -> None:
        # resume-from-sleep detection (wall clock outruns monotonic)
        wall, mono = time.time(), time.monotonic()
        drift = (wall - self._last_wall) - (mono - self._last_mono)
        self._last_wall, self._last_mono = wall, mono
        s = self.settings()
        poll_s = float(s["poll_seconds"])
        if drift > _RESUME_GAP_S:
            self._decide("resume-detected", gap_s=round(drift, 1))
            if s["reconcile_on_resume"]:
                with self._cond:
                    if self._active is not None:
                        self._needs_reconcile = True
                    else:
                        self.reconcile("resume")
        if not s["enabled"]:
            return
        with self._cond:
            active = self._active
            # lease safety: expire a lease nobody released — but never while
            # its instance is still rendering. Releasing then would let the
            # next acquire evict (for H3: restart) the instance under a live
            # render; extend instead and re-check next tick.
            if active is not None and active.expires_at and \
                    time.time() > active.expires_at:
                if active.managed and _queue_empty(active.port) is False:
                    active.expires_at = time.time() + max(60.0, 2.0 * poll_s)
                    self._decide("lease-extended", lease=active.id,
                                 port=active.port, reason="queue running")
                else:
                    self._decide("lease-expired", lease=active.id,
                                 port=active.port)
                    self._finish_locked(active, warm=True)
                active = self._active
            if active is None and self._needs_reconcile:
                self._needs_reconcile = False
                self.reconcile("deferred")
                return
            if active is not None:
                return
            now = time.time()
            for port in self._ports():
                inst = self._instance(port)
                if inst.state == "warm" and inst.deadline and now >= inst.deadline:
                    # never evict under a live render: a long chain whose
                    # lease lapsed would otherwise have its in-flight segment
                    # killed by the H3 restart eviction minutes later
                    q = _queue_empty(port)
                    if q is False:
                        self._decide("ttl-deferred", port=port,
                                     model=inst.model, reason="queue running")
                        continue
                    self._decide("ttl-expired", port=port,
                                 model=inst.model,
                                 warm_s=round(now - inst.last_used, 1))
                    self._evict_locked(inst, mode="warm", verify=True)

    # ------------------------------------------------------------ reconcile
    def reconcile(self, reason: str = "manual") -> dict:
        """Bring every local instance to a verified empty state."""
        with self._cond:
            if self._reconciling:
                return {"ok": False, "error": "reconcile already running"}
            self._reconciling = True
            self._cond.notify_all()
        try:
            return self._reconcile_body(reason)
        finally:
            with self._cond:
                self._reconciling = False
                self._last_wall, self._last_mono = time.time(), time.monotonic()
                self._cond.notify_all()

    def _reconcile_body(self, reason: str) -> dict:
        self._decide("reconcile-start", reason=reason)
        results: dict[str, str] = {}
        for port in self._ports():
            inst = self._instance(port)
            if inst.state == "empty" and self._verify_empty(port, 5):
                results[str(port)] = "empty"
                self._baseline(inst)
                continue
            res = self._evict_locked(inst, mode="hard", verify=True,
                                     allow_restart=True)
            results[str(port)] = res.get("result", "?")
        self._decide("reconcile-done", reason=reason, results=results)
        return {"ok": True, "results": results}

    # ------------------------------------------------------------ prewarm
    def _warm_qwen(self, reason: str) -> None:
        """Production-shaped warm-up rounds after startup.

        Measured 2026-10-06 (live stack + telemetry): the first 1-3 Qwen
        generations after a fresh process run through ComfyUI-GGUF's
        per-module cast path (one CPU core, 50 -> 12 -> 4 s/step) and then
        the process latches into the fast path (3.7 s/step) for good — even
        across model unloads and reloads. These rounds reach that state in
        the background. Each round is a real 8-step render at the production
        size; the loop stops as soon as a round is fast, and aborts before
        the next round when the user has submitted work.
        """
        s = self.settings()
        if not s["prewarm"]:
            return
        model = s["prewarm_model"]
        port = self.port_for_model(model)
        if port is None or not self.is_local(port):
            return
        max_rounds = max(1, int(s.get("prewarm_max_rounds", 5)))
        fast_s = float(s.get("prewarm_fast_seconds", 150))
        for i in range(1, max_rounds + 1):
            with self._cond:
                busy = self._active is not None or bool(self._waiters)
            if busy:
                self._decide("prewarm-stop", reason="user-work",
                             round=i - 1)
                return
            secs, ok = self._warm_round(model, port, i)
            if not ok:
                return
            if secs is not None and secs <= fast_s:
                self._decide("prewarm-warm", reason=reason, rounds=i,
                             seconds=round(secs, 1))
                return
        self._decide("prewarm-max", reason=reason, rounds=max_rounds)

    def _warm_round(self, model: str, port: int,
                    i: int) -> tuple[float | None, bool]:
        s = self.settings()
        try:
            w, h = (int(x) for x in
                    str(s.get("prewarm_size", "1024x1024")).lower()
                    .split("x"))
        except (TypeError, ValueError):
            w = h = 1024
        steps = max(1, int(s.get("prewarm_steps", 8)))
        try:
            from models import build_workflow
            wf = build_workflow(model, "warm-up frame, soft cinematic "
                                "light", "", 1000 + i, w, h, steps, 1.0,
                                loras=None, hires=False,
                                shot_id=f"broker_prewarm_{i}")
        except Exception as e:  # noqa: BLE001
            self._decide("prewarm-skip", reason=f"workflow: {e}")
            return None, False
        lease = None
        try:
            lease = self.acquire(model=model, port=port, purpose="prewarm",
                                 timeout=1200)
            from renderer import ComfyClient
            c = ComfyClient(port)
            t0 = time.time()
            pid = c.queue_prompt(wf, client_id="studio-prewarm")
            res = c.wait_for(pid, timeout_s=1800)
            ok = bool(res) and ((res.get("status") or {})
                                .get("status_str") == "success")
            secs = time.time() - t0
            self._decide("prewarm-round", round=i, ok=ok,
                         seconds=round(secs, 1))
            return secs, ok
        except Exception as e:  # noqa: BLE001
            self._decide("prewarm-fail", error=str(e))
            return None, False
        finally:
            if lease is not None:
                self.release(lease)

    # ------------------------------------------------------------ verification
    def _baseline(self, inst: _Instance) -> None:
        if inst.baseline_mb is None:
            free = _vram_free_mb(inst.port)
            if free is not None and free >= _EMPTY_FALLBACK_MB:
                inst.baseline_mb = free
                self._decide("baseline", port=inst.port, free_mb=free)

    def _verify_empty(self, port: int, timeout: float, inst: _Instance | None = None) -> bool:
        inst = inst or self._instance(port)
        s = self.settings()
        tol = s["vram_tolerance_mb"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            q = _queue_empty(port)
            if q is None:
                # instance not answering: if it is not listening at all it
                # holds no GPU memory; give it a moment then accept.
                if not _listening(port):
                    inst.state = "empty"
                    inst.model = None
                    return True
                time.sleep(0.5)
                continue
            if q:
                free = _vram_free_mb(port)
                if free is None:
                    time.sleep(0.5)
                    continue
                if inst.baseline_mb is None:
                    if free >= _EMPTY_FALLBACK_MB:
                        inst.baseline_mb = free
                        inst.state = "empty"
                        inst.model = None
                        return True
                elif free >= inst.baseline_mb - tol:
                    inst.state = "empty"
                    inst.model = None
                    return True
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------ eviction
    def _evict_locked(self, inst: _Instance, mode: str = "warm",
                      verify: bool = True, allow_restart: bool = True) -> dict:
        port = inst.port
        s = self.settings()
        t0 = time.time()
        use_restart = (inst.h3 and s["h3_mode"] == "restart")
        if inst.state in ("empty",) and verify:
            if self._verify_empty(port, 3, inst):
                inst.state = "empty"
                inst.model = None
                return {"result": "empty", "ms": 0, "port": port}
        mode_used = "restart" if use_restart else mode
        inst.state = "evicting"
        self._decide("evict", port=port, model=inst.model, mode=mode_used)
        try:
            if use_restart:
                self._restart_instance(inst)
            elif _listening(port):
                if _queue_empty(port) is False:
                    try:
                        _http_json(port, "/interrupt", {})
                        _http_json(port, "/queue", {"clear": True})
                    except Exception:
                        pass
                payload = {"unload_models": True,
                           "free_memory": not s["evict_keep_conditioning"]}
                try:
                    _http_json(port, "/free", payload, timeout=15)
                except Exception as e:
                    inst.last_error = f"free failed: {e}"
            # else: process not up yet — nothing to unload
        except Exception as e:  # noqa: BLE001
            inst.last_error = str(e)
            log.warning("evict %s failed: %s", port, e)

        ok = True
        if verify:
            ok = self._verify_empty(port, s["verify_timeout_s"], inst)
            if not ok and allow_restart and not use_restart and \
                    s["restart_on_verify_fail"]:
                self._decide("evict-escalate", port=port,
                             reason="verify failed")
                try:
                    self._restart_instance(inst)
                    ok = self._verify_empty(port, s["verify_timeout_s"], inst)
                except Exception as e:  # noqa: BLE001
                    ok = False
                    inst.last_error = str(e)
        ms = int((time.time() - t0) * 1000)
        if ok:
            inst.baseline_mb = inst.baseline_mb if inst.baseline_mb else \
                (_vram_free_mb(port) or None)
            inst.state = "empty"
            inst.model = None
            inst.deadline = 0.0
            inst.last_error = ""
            self._decide("evict-done", port=port, mode=mode_used,
                         ok=True, ms=ms)
            return {"result": "empty", "ms": ms, "port": port}
        inst.state = "degraded"
        inst.model = None
        self._decide("evict-done", port=port, mode=mode_used, ok=False,
                     error=inst.last_error, ms=ms)
        return {"result": "degraded", "ms": ms, "port": port,
                "error": inst.last_error}

    def _restart_instance(self, inst: _Instance) -> None:
        port = inst.port
        pids = _port_pids(port)
        self._decide("instance-restart", port=port, pids=pids)
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=30)
            except Exception:
                pass
        deadline = time.time() + 180
        while time.time() < deadline:
            if _vram_free_mb(port) is not None:
                # fresh process: give it a moment to finish booting
                time.sleep(1.0)
                inst.baseline_mb = None
                inst.state = "empty"
                inst.model = None
                inst.last_error = ""
                self._baseline(inst)
                self._decide("instance-up", port=port)
                return
            time.sleep(2)
        raise GpuUnavailableError(f"instance on port {port} did not come back")

    def restart_leased(self, lease: Lease) -> dict:
        """Restart the instance a live lease holds and keep the lease (the
        launcher relaunches the process). For a step that would otherwise make
        an H3 server drop its models mid-session: chain_engine.H3Chain."""
        with self._cond:
            if self._active is not lease or not lease.managed:
                return {"ok": False, "error": "not the active lease"}
            inst = self._instance(lease.port)
        t0 = time.time()
        try:
            self._restart_instance(inst)
        except Exception as e:  # noqa: BLE001
            inst.last_error = str(e)
            self._decide("lease-restart", lease=lease.id, port=lease.port, ok=False, error=str(e))
            return {"ok": False, "error": str(e), "port": lease.port}
        with self._cond:
            inst.state, inst.lease_id = "busy", lease.id
            inst.model = lease.model or inst.model
        ms = int((time.time() - t0) * 1000)
        self._decide("lease-restart", lease=lease.id, port=lease.port, ok=True, ms=ms)
        return {"ok": True, "ms": ms, "port": lease.port}

    def evict(self, port: int, mode: str = "warm") -> dict:
        with self._cond:
            if self._active is not None:
                return {"ok": False, "error": "a render is active"}
            inst = self._instance(int(port))
            res = self._evict_locked(inst, mode=mode, verify=True,
                                     allow_restart=True)
        return {"ok": res.get("result") == "empty", **res}

    def evict_all(self, mode: str = "warm") -> dict:
        results: dict[str, str] = {}
        with self._cond:
            if self._active is not None:
                return {"ok": False, "error": "a render is active"}
            for port in self._ports():
                inst = self._instance(port)
                if inst.state == "empty":
                    results[str(port)] = "already-empty"
                    continue
                res = self._evict_locked(inst, mode=mode, verify=True,
                                         allow_restart=True)
                results[str(port)] = res.get("result", "?")
        return {"ok": True, "results": results}

    # ------------------------------------------------------------ leases
    def current_lease(self) -> Lease | None:
        """The lease this thread holds — only while it is still THE active
        lease. A thread-local left behind by a lease that expired or was
        released from another thread (watch_prompt, release_by_id) would
        otherwise let this thread's next submit bypass the broker."""
        lease = getattr(self._tls, "lease", None)
        if lease is None:
            return None
        with self._cond:
            if self._active is lease:
                return lease
        self._tls.lease = None
        return None

    def _set_current(self, lease: Lease | None) -> None:
        self._tls.lease = lease

    def acquire(self, model: str | None = None, port: int | None = None,
                purpose: str = "", timeout: float | None = None,
                wait: bool = True, hold_s: float | None = None,
                bind_thread: bool = True) -> Lease:
        """Blocking FIFO acquire. Returns a managed Lease for local ports
        and a no-op Lease for anything else.

        ``timeout`` bounds the WAIT for the GPU. ``hold_s`` bounds how long
        the lease may be held before the maintenance tick reclaims it (it is
        extended while the instance is still rendering); default = timeout,
        the historical behaviour. ``bind_thread=False`` is for leases handed
        to another process (HTTP /api/gpu/lease): the granting thread is a
        shared server thread and must not inherit the lease."""
        port = int(port) if port else self.port_for_model(model)
        if port is None or not self.is_local(port):
            return Lease(port or 0, model, purpose, managed=False)
        s = self.settings()
        if not s["enabled"]:
            return Lease(port, model, purpose, managed=False)

        timeout = 3600.0 if timeout is None else float(timeout)
        deadline = time.time() + timeout
        with self._cond:
            self._next_ticket += 1
            ticket = self._next_ticket
            self._waiters[ticket] = True
            try:
                while True:
                    if not wait:
                        if self._active is not None or self._reconciling:
                            raise GpuBusyError("GPU busy")
                    head = min(self._waiters.keys()) if self._waiters else ticket
                    if self._active is None and not self._reconciling and \
                            head == ticket:
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise GpuBusyError(
                            f"GPU still busy after {timeout:.0f}s")
                    self._cond.wait(min(remaining, 5.0))
            finally:
                self._waiters.pop(ticket, None)

            lease = Lease(port, model, purpose,
                          timeout=hold_s if hold_s is not None else timeout)
            self._active = lease
            if bind_thread:
                self._set_current(lease)
            inst = self._instance(port)
            inst.state = "busy"
            inst.lease_id = lease.id

        # outside the ticket section but the slot is ours
        try:
            self._prepare_for(lease, s)
        except Exception:
            with self._cond:
                self._finish_locked(lease, warm=False)
                if bind_thread:
                    self._set_current(None)
            raise
        self._decide("lease-acquire", lease=lease.id, port=port,
                     model=model, purpose=purpose)
        return lease

    def _prepare_for(self, lease: Lease, s: dict) -> None:
        """Evict everything that would compete with the target instance."""
        target = self._instance(lease.port)
        capacity = s["max_warm_stacks"]
        # 1) all other ports: keep at most capacity-1 warm stacks
        others = [self._instance(p) for p in self._ports()
                  if p != lease.port]
        warm = [i for i in others if i.state == "warm"]
        keep: set[int] = set()
        if capacity > 1 and warm:
            warm.sort(key=lambda i: i.last_used, reverse=True)
            keep = {i.port for i in warm[:capacity - 1]}
        for inst in others:
            if inst.state == "empty":
                continue
            if inst.port in keep:
                continue
            self._evict_locked(inst, mode="warm", verify=True,
                               allow_restart=True)
        # 2) the target: keep it only when it already holds this model
        if target.state == "warm":
            same = (lease.model is None or target.model is None or
                    target.model == lease.model)
            if not same:
                self._evict_locked(target, mode="warm", verify=True,
                                   allow_restart=True)
        elif target.state in ("degraded",):
            if not self._verify_empty(target.port, 5, target):
                raise GpuUnavailableError(
                    f"instance on port {target.port} is degraded: "
                    f"{target.last_error}")
        target.state = "busy"
        target.model = lease.model or target.model
        target.lease_id = lease.id
        # 3) final gate: every other port verified empty
        for inst in others:
            if inst.port in keep:
                continue
            if inst.state != "empty":
                if not self._verify_empty(inst.port, 5, inst):
                    raise GpuUnavailableError(
                        f"port {inst.port} not empty before render")

    def release(self, lease: Lease | None) -> None:
        if lease is None:
            return
        if not lease.managed:
            return
        with self._cond:
            self._finish_locked(lease, warm=True)
            if getattr(self._tls, "lease", None) is lease:
                self._set_current(None)

    def release_by_id(self, lease_id: str) -> bool:
        """Release a lease by id (remote/CLI processes)."""
        with self._cond:
            lease = self._active
            if lease is None or lease.id != lease_id:
                return False
            self._finish_locked(lease, warm=True)
            return True

    def _finish_locked(self, lease: Lease, warm: bool) -> None:
        if self._active is not lease:
            # already expired/released — and the port may now belong to a
            # newer lease, whose instance state must not be touched
            if self._active is None or self._active.port != lease.port:
                inst = self._instance(lease.port)
                if inst.lease_id == lease.id:
                    inst.lease_id = None
            self._decide("lease-release-stale", lease=lease.id, port=lease.port)
            self._cond.notify_all()
            return
        inst = self._instance(lease.port)
        inst.lease_id = None
        if warm and lease.port in self._ports():
            inst.state = "warm"
            inst.model = lease.model or inst.model
            inst.last_used = time.time()
            inst.deadline = inst.last_used + self._ttl_seconds(inst.model)
            self._decide("lease-release", lease=lease.id, port=lease.port,
                         model=inst.model,
                         warm_for_s=int(inst.deadline - time.time()))
        else:
            inst.state = "unknown"
        if self._active is lease:
            self._active = None
        self._cond.notify_all()

    @contextmanager
    def lease_scope(self, model: str | None = None, port: int | None = None,
                    purpose: str = "", timeout: float | None = None):
        cur = self.current_lease()
        if cur is not None and (port is None or cur.port == int(port)):
            yield cur
            return
        lease = self.acquire(model=model, port=port, purpose=purpose,
                             timeout=timeout)
        try:
            yield lease
        finally:
            self.release(lease)

    def watch_prompt(self, lease: Lease, prompt_id: str) -> None:
        """Release an implicit lease once its prompt leaves the queue."""
        if not lease.managed:
            return

        def _watch() -> None:
            deadline = time.time() + 2 * 60 * 60
            while time.time() < deadline:
                time.sleep(3)
                try:
                    if _http_json(lease.port, f"/history/{prompt_id}",
                                  timeout=5):
                        break
                except Exception:
                    pass
            else:
                log.warning("watch_prompt(%s): giving up after 2h", prompt_id)
            self.release(lease)

        threading.Thread(target=_watch, daemon=True,
                         name=f"gpu-lease-{lease.id}").start()

    # ------------------------------------------------------------ cancel
    def cancel_active(self, port: int | None = None) -> dict:
        """Interrupt the active local prompt (broker owns the GPU, so this
        cannot hit an unrelated render)."""
        with self._cond:
            lease = self._active
            if lease is None or not lease.managed:
                return {"ok": False, "error": "no active lease"}
            if port is not None and int(port) != lease.port:
                return {"ok": False, "error": "active lease is on another port"}
            lease.cancelled = True
        try:
            _http_json(lease.port, "/interrupt", {})
            self._decide("lease-interrupt", lease=lease.id, port=lease.port)
            return {"ok": True, "port": lease.port, "lease": lease.id}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        s = self.settings()
        now = time.time()
        with self._cond:
            active = self._active.as_dict() if self._active else None
            decisions = list(self._decisions)[-40:]
            reconciling = self._reconciling
        ports = []
        for port in self._ports():
            inst = self._instance(port)
            free = _vram_free_mb(port)
            warm_remaining = None
            if inst.state == "warm" and inst.deadline:
                warm_remaining = max(0, int(inst.deadline - now))
            ports.append({
                "port": port,
                "state": inst.state,
                "model": inst.model,
                "h3": inst.h3,
                "busy": inst.state == "busy",
                "free_mb": free,
                "baseline_mb": inst.baseline_mb,
                "warm_remaining_s": warm_remaining,
                "last_used": (time.strftime(
                    "%H:%M:%S", time.localtime(inst.last_used))
                    if inst.last_used else None),
                "last_error": inst.last_error or None,
            })
        return {"ok": True, "settings": s, "active_lease": active,
                "reconciling": reconciling, "ports": ports,
                "decisions": decisions}


broker = _Broker()


# ------------------------------------------------------- module-level API
def start(reconcile: bool | None = None) -> None:
    broker.start(reconcile=reconcile)


def is_owner() -> bool:
    return broker.is_owner()


def is_local(port: int) -> bool:
    return broker.is_local(port)


def acquire(model: str | None = None, port: int | None = None,
            purpose: str = "", timeout: float | None = None,
            wait: bool = True, hold_s: float | None = None,
            bind_thread: bool = True) -> Lease:
    return broker.acquire(model=model, port=port, purpose=purpose,
                          timeout=timeout, wait=wait, hold_s=hold_s,
                          bind_thread=bind_thread)


def release(lease: Lease | None) -> None:
    broker.release(lease)


def release_by_id(lease_id: str) -> bool:
    return broker.release_by_id(lease_id)


def current_lease() -> Lease | None:
    return broker.current_lease()


def lease_scope(model: str | None = None, port: int | None = None,
                purpose: str = "", timeout: float | None = None):
    return broker.lease_scope(model=model, port=port, purpose=purpose,
                              timeout=timeout)


def watch_prompt(lease: Lease, prompt_id: str) -> None:
    broker.watch_prompt(lease, prompt_id)


def cancel_active(port: int | None = None) -> dict:
    return broker.cancel_active(port)


def restart_leased(lease: Lease) -> dict:
    return broker.restart_leased(lease)


def evict(port: int, mode: str = "warm") -> dict:
    return broker.evict(port, mode)


def evict_all(mode: str = "warm") -> dict:
    return broker.evict_all(mode)


def reconcile(reason: str = "manual") -> dict:
    return broker.reconcile(reason)


def state() -> dict:
    return broker.state()


# -------------------------------------------------- remote (HTTP) leases
def remote_acquire(base_url: str, model: str | None = None,
                   port: int | None = None, purpose: str = "remote",
                   timeout: float = 60.0) -> dict:
    """For CLI processes: lease the GPU through the control room."""
    url = base_url.rstrip("/") + "/api/gpu/lease"
    req = urllib.request.Request(
        url, data=json.dumps({"model": model, "port": port,
                              "purpose": purpose, "timeout": timeout,
                              "hold_s": 3600}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout + 30) as r:
        return json.loads(r.read().decode("utf-8"))


def remote_release(base_url: str, lease_id: str) -> bool:
    url = base_url.rstrip("/") + "/api/gpu/release"
    req = urllib.request.Request(
        url, data=json.dumps({"lease_id": lease_id}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _OPENER.open(req, timeout=15):
            return True
    except Exception:
        return False


if __name__ == "__main__":
    print(json.dumps(state(), indent=2, default=str))
