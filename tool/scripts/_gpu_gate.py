"""Pre-flight GPU gate for non-user GPU producers (benchmarks, automation scripts).

The studio's ComfyUI (:8188) and image API (:8080) belong to the person at
the keyboard: the user's renders get the GPU first. Any script that submits
work to :8188 outside the studio itself — benchmark harnesses, evaluation
suites, one-off experiments — must pass this gate before queueing so it
never runs on top of, or ahead of, a studio render.

Verdict model:

  idle    - no studio work in flight; safe to queue.
  busy    - studio work is running or waiting; do not queue.
  unknown - the decisive signal could not be read; absence of evidence is not
            evidence of idleness, so do not queue.

A refused connection on the queue is the one exception: it means the studio's
ComfyUI is not running, so there is no queue to contend with — recorded as
idle (a submit will fail on its own if ComfyUI is truly down).

Deliberate bypass (a human explicitly asked for it):
    KINOLOOM_ALLOW_BUSY_GPU=1   or   check(force=True)   or   --allow-busy

Every check appends one JSON line to logs/gpu_gate.log for the record.
Run directly for a quick verdict:

    .venv/Scripts/python.exe tool/scripts/_gpu_gate.py
    exit 0 = idle, 3 = busy, 4 = unknown
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "gpu_gate.log"
STUDIO_QUEUE_URL = "http://127.0.0.1:8188/queue"
STUDIO_IMAGES_URL = "http://127.0.0.1:8080/api/images/status"
BYPASS_ENV = "KINOLOOM_ALLOW_BUSY_GPU"
TIMEOUT_S = 2.0
MODEL_INPUT_KEYS = ("unet_name", "ckpt_name", "model_name")

# When the shell exports HTTP_PROXY/HTTPS_PROXY, urllib honours them by
# default, so loopback requests would go through a proxy that can answer 502 —
# which looks exactly like "ComfyUI is down". Bypass proxies for the gate.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_json(url: str, timeout: float = TIMEOUT_S) -> tuple[str, Any]:
    """(state, payload); state in ok|http_error|refused|unreachable|malformed."""
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return "http_error", e.code
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, ConnectionRefusedError) or \
                "refused" in str(reason).lower():
            return "refused", None
        return "unreachable", str(reason)
    except (TimeoutError, OSError) as e:
        return "unreachable", str(e)
    try:
        return "ok", json.loads(raw)
    except json.JSONDecodeError:
        return "malformed", None


def _model_names(prompt: Any, limit: int = 4) -> list[str]:
    names: list[str] = []
    if not isinstance(prompt, dict):
        return names
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        for key in MODEL_INPUT_KEYS:
            value = inputs.get(key)
            if isinstance(value, str) and value not in names:
                names.append(value)
                if len(names) >= limit:
                    return names
    return names


def _queue_details() -> dict[str, Any]:
    state, payload = _fetch_json(STUDIO_QUEUE_URL)
    out: dict[str, Any] = {"endpoint": STUDIO_QUEUE_URL, "state": state}
    if state != "ok":
        return out
    if not isinstance(payload, dict):
        out["state"] = "malformed"
        return out
    running = payload.get("queue_running")
    pending = payload.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        out["state"] = "malformed"
        return out
    out["running"] = len(running)
    out["pending"] = len(pending)
    models: list[str] = []
    for entry in list(running) + list(pending):
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            for name in _model_names(entry[2]):
                if name not in models:
                    models.append(name)
    out["models"] = models
    return out


def _images_details() -> dict[str, Any]:
    state, payload = _fetch_json(STUDIO_IMAGES_URL)
    out: dict[str, Any] = {"endpoint": STUDIO_IMAGES_URL, "state": state}
    if state == "ok" and isinstance(payload, dict):
        gen = payload.get("gen")
        if isinstance(gen, dict):
            out["running"] = bool(gen.get("running"))
            out["model"] = gen.get("model")
        else:
            out["state"] = "unavailable"
    elif state == "http_error":
        out["state"] = "unavailable"
        out["http_status"] = payload
    return out


def _verdict(details: dict[str, Any]) -> tuple[str, str]:
    """Pure verdict from fetched signals — kept side-effect free for tests."""
    queue = details["queue"]
    if queue.get("state") == "ok":
        running = int(queue.get("running") or 0)
        pending = int(queue.get("pending") or 0)
        if running or pending:
            where = "running" if running else "waiting"
            names = ", ".join(queue.get("models") or []) or "?"
            return "busy", (f"studio ComfyUI queue has {running} running / "
                            f"{pending} pending job(s) ({where}: {names})")
    images = details["images"]
    if images.get("state") == "ok" and images.get("running"):
        return "busy", ("studio image generation is running "
                        f"({images.get('model') or '?'})")
    if queue.get("state") == "refused":
        return "idle", ("studio ComfyUI is not running (connection refused); "
                        "no studio queue exists to contend with")
    if queue.get("state") in ("unreachable", "malformed", "http_error"):
        return "unknown", (f"studio queue signal is {queue.get('state')}; "
                           f"cannot confirm the studio is idle")
    return "idle", "studio queue empty and no supporting signal reports work"


def _broker_details() -> dict[str, Any]:
    """GPU broker state from the control room (single source of truth when
    present). Absence falls back to the legacy queue/image probes."""
    url = "http://127.0.0.1:8080/api/gpu/state"
    state, payload = _fetch_json(url)
    out: dict[str, Any] = {"endpoint": url, "state": state}
    if state == "ok" and isinstance(payload, dict):
        out["active_lease"] = payload.get("active_lease")
        out["reconciling"] = bool(payload.get("reconciling"))
        out["ports"] = payload.get("ports") or []
    elif state == "http_error":
        out["state"] = "unavailable"
    return out


def check(force: bool = False, log: bool = True) -> dict[str, Any]:
    """Read the studio signals and return {ok, verdict, forced, details, ts}."""
    details: dict[str, Any] = {
        "broker": _broker_details(),
        "queue": _queue_details(),
        "images": _images_details(),
    }
    broker = details["broker"]
    if broker.get("state") == "ok":
        if broker.get("active_lease") or broker.get("reconciling"):
            who = (broker.get("active_lease") or {}).get("purpose") or \
                  ("reconcile" if broker.get("reconciling") else "?")
            verdict, summary = "busy", f"gpu broker active ({who})"
        else:
            verdict, summary = "idle", "gpu broker idle"
    else:
        verdict, summary = _verdict(details)
    details["summary"] = summary
    forced = bool(force or os.environ.get(BYPASS_ENV))
    result = {"ok": verdict == "idle" or forced, "verdict": verdict,
              "forced": forced, "details": details,
              "ts": datetime.now().isoformat(timespec="seconds")}
    if log:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result) + "\n")
        except Exception:
            pass
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Studio GPU gate — run before queueing work to :8188.")
    ap.add_argument("--force", action="store_true",
                    help="report idle regardless (deliberate override)")
    a = ap.parse_args(argv)
    r = check(force=a.force)
    suffix = "  [FORCED]" if r["forced"] else ""
    print(f"gpu gate: {r['verdict']} — {r['details']['summary']}{suffix}")
    if r["ok"]:
        return 0
    return 3 if r["verdict"] == "busy" else 4


if __name__ == "__main__":
    raise SystemExit(main())
