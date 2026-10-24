"""Renderer client — ComfyUI HTTP/WebSocket API.
Submits workflows, polls progress, fetches results. Ensemble split configs
and warm-model keeper live here."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from core import ROOT, load_config, log_setup

log = log_setup("renderer")


def _remote_watch(base: str, lease_id: str, port: int, prompt_id: str) -> None:
    """Release a CLI process's brokered lease once its prompt is history."""
    deadline = time.time() + 2 * 60 * 60
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        time.sleep(3)
        try:
            with opener.open(
                    f"http://127.0.0.1:{port}/history/{prompt_id}",
                    timeout=5) as r:
                if json.loads(r.read().decode()):
                    break
        except Exception:
            pass
    try:
        import gpu_broker as gb
        gb.remote_release(base, lease_id)
    except Exception as e:  # noqa: BLE001
        log.warning("remote lease release failed (%s): %s", lease_id, e)


def queue_position(queue_payload: dict, prompt_id: str) -> int | None:
    """Where a prompt sits in ComfyUI's queue.

    0 = currently running, 1..n = 1-based place in the pending queue,
    None = not in the queue (finished, unknown, or malformed payload).
    """
    if not isinstance(queue_payload, dict) or not prompt_id:
        return None
    for entry in queue_payload.get("queue_running") or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 \
                and entry[1] == prompt_id:
            return 0
    for idx, entry in enumerate(queue_payload.get("queue_pending") or []):
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 \
                and entry[1] == prompt_id:
            return idx + 1
    return None


def upload_name(data: bytes) -> str:
    """The input-folder name of an uploaded still, derived from its bytes: the same
    still gets the same name on every upload, so a graph that names it stays the same
    graph and a chain step rendered from it can be reused (chain_engine.graph_digest)."""
    return f"kf_{hashlib.sha256(data).hexdigest()[:16]}.png"


class ComfyClient:
    def __init__(self, port: int = 8188, host: str | None = None) -> None:
        # host=None keeps the historical localhost behaviour byte-for-byte;
        # workers.py passes a remote host for second-PC routing.
        self.port = int(port)
        self.host = host
        self.base = f"http://{host or '127.0.0.1'}:{port}"

    def post(self, path: str, data: dict | None = None) -> Any:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(data or {}).encode() if data is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

    def get(self, path: str) -> Any:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=60) as r:
            return json.loads(r.read().decode())

    # ------------------------------------------------------------- workflow
    def _submit(self, body: dict) -> str:
        res = self.post("/prompt", body)
        return res["prompt_id"]

    def queue_prompt(self, workflow: dict, client_id: str | None = None) -> str:
        """Submit a workflow. Local instances are serialized by the GPU
        broker: producers that hold an explicit lease pass straight through;
        everyone else gets an implicit per-prompt lease (released when the
        prompt leaves ComfyUI's queue)."""
        body: dict = {"prompt": workflow}
        if client_id:
            body["client_id"] = client_id
        if self.host is not None:
            return self._submit(body)
        gb = None
        try:
            import gpu_broker as gb  # noqa: F811
        except Exception:
            gb = None
        if gb is None or not gb.is_local(self.port):
            return self._submit(body)
        if gb.current_lease() is not None:
            return self._submit(body)
        if gb.is_owner():
            lease = gb.acquire(port=self.port, purpose="implicit",
                               timeout=3600)
            try:
                pid = self._submit(body)
            except Exception:
                gb.release(lease)
                raise
            gb.watch_prompt(lease, pid)
            return pid
        if os.environ.get("KINOLOOM_ALLOW_UNBROKERED") == "1":
            return self._submit(body)
        base = os.environ.get("KINOLOOM_BROKER_URL",
                              "http://127.0.0.1:8080")
        try:
            res = gb.remote_acquire(base, port=self.port, purpose="cli",
                                    timeout=3600)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"GPU broker unreachable at {base} ({e}); start the studio "
                f"or set KINOLOOM_ALLOW_UNBROKERED=1 to bypass") from e
        lease_id = str(res.get("lease_id") or "")
        try:
            pid = self._submit(body)
        except Exception:
            if lease_id:
                gb.remote_release(base, lease_id)
            raise
        if lease_id:
            threading.Thread(
                target=_remote_watch, args=(base, lease_id, self.port, pid),
                daemon=True, name="gpu-remote-lease").start()
        return pid

    def history(self, prompt_id: str) -> dict:
        return self.get(f"/history/{prompt_id}")

    def object_info(self) -> dict:
        return self.get("/object_info")

    def system_stats(self) -> dict:
        return self.get("/system_stats")

    def interrupt(self) -> None:
        """Interrupt the active prompt. When this process owns the broker,
        local interrupts are routed through it so a stray stop control can
        never kill an unrelated render on the same instance."""
        if self.host is None:
            try:
                import gpu_broker as gb
                if gb.is_owner() and gb.is_local(self.port):
                    # broker-managed: only the active lease on THIS port may
                    # be interrupted; never a raw local-global interrupt
                    gb.cancel_active(self.port)
                    return
            except Exception:
                pass
        self.post("/interrupt", {})

    def queue_info(self) -> dict:
        return self.get("/queue")

    def queue_position(self, prompt_id: str) -> int | None:
        """0 = running, N = place in the pending queue, None = not queued."""
        return queue_position(self.queue_info(), prompt_id)

    def load_models(self, names: list[str]) -> list[dict]:
        """Models available in the ComfyUI instance."""
        info = self.get("/object_info")
        checkpoints = sorted(info.get("CheckpointLoaderSimple", {})
                             .get("input", {}).get("required", {})
                             .get("ckpt_name", [])[0] if
                             "CheckpointLoaderSimple" in info else [])
        return [{"name": n} for n in checkpoints]

    # -------------------------------------------------------------- progress
    def wait_for(self, prompt_id: str, timeout_s: float = 3600,
                 cb=None, cancel: threading.Event | None = None) -> dict | None:
        """Poll history until done. cb(status, progress) called on updates.

        Transient poll failures (socket timeouts / connection resets while
        ComfyUI streams 15 GB of weights, or during a model swap) must NOT
        kill a healthy render — they are retried until the deadline. The old
        loop let one `socket.timeout: timed out` propagate and marked the job
        failed while the farm kept rendering it.

        cancel: returns None as soon as it is set — a stopped job must not
        sit here (holding its GPU lease) until the timeout."""
        t0 = time.time()
        transient = 0

        def _pause() -> bool:
            if cancel is None:
                time.sleep(5)
                return False
            return cancel.wait(5)

        while time.time() - t0 < timeout_s:
            if cancel is not None and cancel.is_set():
                return None
            try:
                h = self.history(prompt_id)
            except Exception as e:  # noqa: BLE001
                transient += 1
                if transient % 6 == 1:      # ~every 30 s, not every 5 s
                    log.warning(f"wait_for({prompt_id}): poll failed "
                                f"({transient}): {e}")
                if _pause():
                    return None
                continue
            if prompt_id in h:
                return h[prompt_id]
            if cb:
                try:
                    cb(self.get("/queue"))
                except Exception:
                    pass
            if _pause():
                return None
        return None

    def download(self, subfolder: str, filename: str, save_to: Path) -> Path:
        """Download with timeout + retry — ComfyUI occasionally drops long
        transfers (WinError 10054); a hang here used to strand post workers."""
        # encoded: names with spaces, '&', '#' or '+' broke the raw query
        url = f"{self.base}/view?" + urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder or "",
             "type": "output"})
        save_to = Path(save_to)
        save_to.parent.mkdir(parents=True, exist_ok=True)
        # stream into a sibling temp file: a dropped transfer must never
        # leave a truncated clip at save_to for the caller to pick up
        part = save_to.with_name(save_to.name + ".part")
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=300) as r, \
                        open(part, "wb") as f:
                    shutil.copyfileobj(r, f, length=1 << 20)
                if part.stat().st_size > 0:
                    os.replace(part, save_to)
                    return save_to
                raise IOError("empty download")
            except (urllib.error.URLError, ConnectionResetError,
                    TimeoutError, OSError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(6)
        part.unlink(missing_ok=True)
        raise last_err or IOError("download failed")

    def upload_image(self, local_path: Path) -> str:
        """Upload a still to the ComfyUI input dir; returns its file name there
        (upload_name). Re-uploading the same still overwrites it with itself."""
        data = Path(local_path).read_bytes()
        name = upload_name(data)
        boundary = "----kinoloom" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="image"; filename="{name}"\r\n'
                f"Content-Type: image/png\r\n\r\n").encode() + data
        body += f"\r\n--{boundary}\r\nContent-Disposition: form-data; " \
                f'name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            f"{self.base}/upload/image", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode())
        return res.get("name", name)


class Renderer:
    """High-level wrapper combining ensemble settings + client."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.client = ComfyClient(self.cfg["ensemble"]["comfyui_port"])

    def model_path(self, tier: str) -> str:
        m = self.cfg["tiers"]["model"]
        name = m.get(tier, "") if isinstance(m, dict) else m
        return str(ROOT / "models" / "diffusion_models" / name)

    def build_workflow(self, tier: str, prompt: str, negative: str, seed: int,
                       width: int, height: int, steps: int, frames: int,
                       shot_id: str = "shot", sjson: dict | None = None) -> dict:
        from workflows import WorkflowBuilder
        from lora_registry import LoRARegistry
        b = WorkflowBuilder()
        # single model everywhere: T2V for hero/volume-env,
        # I2V (keyframe-driven) for standard/volume-people
        if tier in ("standard", "volume_people"):
            base = "wan2.2_ti2v_5b"
        else:
            base = "wan2.2_t2v_5b"
        extra = {"SHOT_ID": shot_id}
        reg = LoRARegistry(self.cfg)
        # the 5B templates always reference the *_STR tokens; their LoRA
        # slots default to "none" (stripped) when no shot json selects any
        extra.update(reg.strengths())
        if sjson:
            extra.update(reg.resolve(sjson))
        return b.build(base, prompt, negative, seed, width, height,
                       steps, frames, self.cfg, extra=extra)

    def health(self) -> dict:
        try:
            stats = self.client.system_stats()
            return {"up": True, "devices": stats.get("devices", [])}
        except Exception as e:
            return {"up": False, "error": str(e)}
