"""Worker agent — runs on a second PC (or a cloud GPU box) next to its own
ComfyUI.

Register with the main studio, heartbeat GPU/model telemetry, execute job
envelopes on the LOCAL ComfyUI, and push finished artefacts back so the main
machine's engines find them exactly where they would have written them.

Contract with the main side (tool/workers.py RemoteWorkerClient):
  POST /job            {job_id, client_id, workflow}   → {prompt_id}
  GET  /job/{id}       → {status: queued|running|done|failed|canceled,
                          prompt_id, queue_position, queue_snapshot, error,
                          history: {prompt_id: <comfy history entry>},
                          outputs: [{filename, subfolder, size}]}
  POST /cancel/{id}    → local interrupt (this ComfyUI is DEDICATED — safe)
  POST /upload         multipart → ComfyUI input dir (same filename)
  GET  /gpu            → nvidia-smi + comfy system_stats + disk
  GET  /models         → filenames per model dir
  GET  /selftest       → comfy reachable + required node classes + disk
  GET  /ping           → liveness
Model management (v1.1 — pull-based, for fat-pipe boxes like Colab):
  POST /install_url    {path, url, sha256?, size?} → streams the download on
                       THIS box's pipe straight to disk (.part + Range resume,
                       incremental sha256, atomic rename)
  POST /install_group  {install_id, files:[…]} → background batch of the
                       above, progress via GET /install_group/{install_id}
  DELETE /models       ?path=models/loras/x.safetensors → remove one file
Every route except /ping requires the X-Worker-Token header (the fleet's
shared token from worker_config.yaml). With no token configured the agent
only answers loopback callers.

Self-contained by design: this worker is completely isolated from the main studio root, so nothing from the
main tree (core.py/renderer.py) is imported — the local Comfy client below
is the only ComfyUI integration the agent needs.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import ntpath
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile

AGENT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = AGENT_DIR / "worker_config.yaml"
AGENT_VERSION = "1.2.0"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    boot()
    yield


app = FastAPI(title="Kinoloom Worker Agent", version=AGENT_VERSION,
              lifespan=_lifespan)


def load_cfg() -> dict:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        # no config yet (bootstrap writes it): run on pure defaults rather
        # than dying with an opaque traceback
        print(f"[worker_agent] {CONFIG_PATH.name} not found — using defaults; "
              "run colab/bootstrap.sh (or write one) for real use", flush=True)
        cfg = {}
    cfg.setdefault("name", socket.gethostname())
    cfg.setdefault("main_url", "http://127.0.0.1:8080")
    cfg.setdefault("token", "")
    cfg.setdefault("agent_port", 8120)
    cfg.setdefault("comfyui_port", 8188)
    cfg.setdefault("autostart_comfy", True)
    cfg.setdefault("heartbeat_s", 10)
    cfg.setdefault("comfyui_dir", "")
    cfg.setdefault("models_dir", "")
    cfg.setdefault("python", "python")
    # extra args appended to the ComfyUI spawn (e.g. ["--disable-metadata"])
    cfg.setdefault("comfy_args", [])
    # Advertised base URL, e.g. "http://100.x.y.z:8120" or a tunnel/mesh URL.
    # When set, the agent registers THIS instead of its LAN IP — required
    # whenever the main box cannot reach the LAN address directly (cloud VM,
    # tailscale serve, tunnel). Must be scheme-ful; the port below is then
    # only informational.
    cfg.setdefault("public_base_url", "")
    # HuggingFace token for private model repos (or env HF_TOKEN).
    cfg.setdefault("hf_token", "")
    # /install_url SSRF guard: https-only and public hosts only, unless the
    # config explicitly opens it up (a worker on a private net may want http
    # LAN mirrors).
    cfg.setdefault("install_allow_http", False)
    cfg.setdefault("install_allow_private_hosts", False)
    # extra node classes /selftest must verify (beyond the GGUF baseline)
    cfg.setdefault("selftest_nodes", [])
    # --- worker role + model gating
    # roles: which job kinds this PC accepts — the studio's assignment UI
    # filters on them (video renders / image jobs / post-upscale).
    cfg.setdefault("roles", ["video", "image", "upscale"])
    # required_models: "subdir/name" entries that MUST exist under models_dir
    # before this worker serves any job. The studio installs them (Workers
    # page -> Install models) with hash verification; until then the worker
    # reports itself blocked and refuses work instead of failing mid-render.
    cfg.setdefault("require_models", False)
    cfg.setdefault("required_models", [])
    return cfg


CFG = load_cfg()
_comfy_base = f"http://127.0.0.1:{CFG['comfyui_port']}"

_lock = threading.Lock()
_jobs: dict[str, dict] = {}          # job_id → record
_current: str | None = None          # single-flight
_cancel_ids: set[str] = set()
_installs: dict[str, dict] = {}      # install_id → batch record


# ------------------------------------------------------------- infra
def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(AGENT_DIR / "worker_agent.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def comfy_get(path: str, timeout: int = 30):
    with urllib.request.urlopen(f"{_comfy_base}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def comfy_post(path: str, data: dict):
    req = urllib.request.Request(
        f"{_comfy_base}{path}", data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def comfy_up() -> bool:
    try:
        comfy_get("/system_stats", timeout=5)
        return True
    except Exception:
        return False


def gpu_stats() -> list[dict]:
    """Same nvidia-smi query as support.RenderMode.health on the main side."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,"
             "temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout
        gpus = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 6:
                gpus.append({"index": int(p[0]), "name": p[1],
                             "mem_used": int(p[2]), "mem_total": int(p[3]),
                             "temp": int(p[4]), "util": int(p[5])})
        return gpus
    except Exception:
        return []


def disk_stats() -> dict:
    try:
        root = Path(CFG.get("models_dir") or AGENT_DIR)
        u = shutil.disk_usage(str(root))
        return {"free_gb": round(u.free / 2**30, 1),
                "total_gb": round(u.total / 2**30, 1)}
    except Exception:
        return {}


def model_inventory() -> dict:
    inv = {}
    root = Path(CFG.get("models_dir") or "")
    if not root.exists():
        return inv
    for sub in ("diffusion_models", "checkpoints", "loras",
                "text_encoders", "vae", "clip", "upscale_models",
                "latent_upscale_models"):
        d = root / sub
        if d.exists():
            inv[sub] = sorted(p.name for p in d.iterdir() if p.is_file())
    return inv


def required_missing() -> list[str]:
    """Configured required models that are not installed yet (subdir/name)."""
    root = Path(CFG.get("models_dir") or "")
    missing = []
    for rel in (CFG.get("required_models") or []):
        rel = str(rel).replace("\\", "/").strip("/")
        if not rel or ".." in rel.split("/"):
            continue
        if not (root / rel).is_file():
            missing.append(rel)
    return missing


def capabilities() -> dict:
    """Everything the main station needs to dispatch the right work here."""
    missing = required_missing()
    return {"roles": [str(r) for r in (CFG.get("roles") or [])],
            "required_missing": missing,
            "blocked": bool(CFG.get("require_models")) and bool(missing),
            "disk": disk_stats()}


def ensure_input_dir() -> Path:
    d = Path(CFG["comfyui_dir"]) / "input"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    d = Path(CFG["comfyui_dir"]) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "worker"


def _hf_token() -> str:
    return str(CFG.get("hf_token") or "") or os_env_hf()


def os_env_hf() -> str:
    import os
    return str(os.environ.get("HF_TOKEN") or "")


# --------------------------------------------------------- main linkage
def _post_main(path: str, data: dict) -> dict:
    req = urllib.request.Request(
        f"{CFG['main_url']}{path}", data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json",
                 "X-Worker-Token": CFG.get("token", "")})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


class _MultipartFile:
    """File-like multipart body (one file field) that streams from disk, so
    multi-GB outputs are never loaded into memory."""

    def __init__(self, path: Path, boundary: str) -> None:
        safe = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        self._head = (f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"file\"; filename=\"{safe}\"\r\n"
                      f"Content-Type: application/octet-stream\r\n\r\n").encode()
        self._tail = f"\r\n--{boundary}--\r\n".encode()
        self._fh = open(path, "rb")
        self._stage = 0
        self.length = len(self._head) + path.stat().st_size + len(self._tail)

    def read(self, n: int = -1) -> bytes:
        if self._stage == 0:
            self._stage = 1
            return self._head
        if self._stage == 1:
            chunk = self._fh.read(n if n and n > 0 else 4 * 2**20)
            if chunk:
                return chunk
            self._stage = 2
            return self._tail
        return b""

    def close(self) -> None:
        self._fh.close()


def _push_output(job_id: str, path: Path) -> None:
    boundary = uuid.uuid4().hex
    body = _MultipartFile(path, boundary)
    query = urllib.parse.urlencode({"job": job_id, "name": path.name})
    try:
        req = urllib.request.Request(
            f"{CFG['main_url']}/api/workers/{_slug(CFG['name'])}/result?{query}",
            data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Content-Length": str(body.length),
                     "X-Worker-Token": CFG.get("token", "")})
        with urllib.request.urlopen(req, timeout=1800) as r:
            r.read()
    finally:
        body.close()


def _advertised_host() -> str:
    """What the main box should dial to reach this agent. public_base_url
    wins (scheme-ful: mesh IP, tailscale-serve URL, tunnel host); without it
    we fall back to the classic LAN IP + port pair."""
    base = str(CFG.get("public_base_url") or "").strip()
    if base:
        return base.rstrip("/")
    return _lan_ip()


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def registration_loop() -> None:
    announced = False
    while True:
        try:
            _post_main("/api/workers/register", {
                "name": CFG["name"], "host": _advertised_host(),
                "port": CFG["agent_port"], "token": CFG.get("token", ""),
                "gpu": gpu_stats(), "models": model_inventory(),
                "capabilities": capabilities(),
                "agent_version": AGENT_VERSION})
            if not announced:
                log(f"registered with {CFG['main_url']} as {CFG['name']} "
                    f"(host={_advertised_host()}:{CFG['agent_port']})")
                announced = True
            time.sleep(float(CFG.get("heartbeat_s", 10)))
        except Exception as e:
            announced = False
            log(f"registration/heartbeat failed: {e}")
            time.sleep(5)


def comfy_watchdog() -> None:
    """Restart local ComfyUI if it dies (unless config says externally
    managed). Same spawn args as the main launcher uses."""
    while True:
        if CFG.get("autostart_comfy") and not comfy_up():
            try:
                log("comfy down — spawning")
                subprocess.Popen(
                    [CFG.get("python") or "python", "main.py", "--port",
                     str(CFG["comfyui_port"]), "--default-device", "0",
                     "--disable-auto-launch"]
                    + [str(a) for a in (CFG.get("comfy_args") or [])],
                    cwd=CFG["comfyui_dir"],
                    stdout=open(AGENT_DIR / "comfyui.log", "ab"),
                    stderr=subprocess.STDOUT)
            except Exception as e:
                log(f"comfy spawn failed: {e}")
        time.sleep(15)


# ------------------------------------------------------------ execution
class LocalComfy:
    """Minimal ComfyUI client — self-contained by design (no main-tree
    imports; completely isolated from the main studio process).
    Same HTTP surface as renderer.ComfyClient: POST /prompt,
    GET /history/{id}, GET /queue, POST /interrupt."""

    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def queue_prompt(self, workflow: dict, client_id: str | None) -> str:
        body: dict = {"prompt": workflow}
        if client_id:
            body["client_id"] = client_id
        return comfy_post("/prompt", body)["prompt_id"]

    def history(self, prompt_id: str) -> dict:
        return comfy_get(f"/history/{prompt_id}")

    def queue_info(self) -> dict:
        return comfy_get("/queue")

    def interrupt(self) -> None:
        comfy_post("/interrupt", {})


def _pos(q: dict, pid: str) -> int | None:
    if not isinstance(q, dict):
        return None
    for e in q.get("queue_running") or []:
        if isinstance(e, (list, tuple)) and len(e) >= 2 and e[1] == pid:
            return 0
    for i, e in enumerate(q.get("queue_pending") or []):
        if isinstance(e, (list, tuple)) and len(e) >= 2 and e[1] == pid:
            return i + 1
    return None


def _extract_error(history_entry: dict) -> str:
    try:
        for m in history_entry["status"]["messages"]:
            if m[0] == "execution_error":
                d = m[1]
                return (f"{d.get('node_type')}: {d.get('exception_message')}"
                        )[:400]
    except Exception:
        pass
    return "execution failed"


def _collect_outputs(history_entry: dict) -> list[dict]:
    outs = []
    for node in (history_entry.get("outputs") or {}).values():
        for key in ("images", "gifs", "videos"):
            for o in node.get(key) or []:
                if o.get("type", "output") == "output":
                    outs.append({"filename": o["filename"],
                                 "subfolder": o.get("subfolder", "")})
    return outs


def _run_job(job_id: str) -> None:
    global _current
    rec = _jobs[job_id]
    try:
        if not comfy_up():
            raise RuntimeError("local ComfyUI is not reachable")
        client = LocalComfy(CFG["comfyui_port"])
        pid = client.queue_prompt(rec["workflow"], rec.get("client_id"))
        with _lock:
            rec["prompt_id"] = pid
            rec["status"] = "running"

        deadline = time.time() + float(rec.get("timeout", 3600))
        hist = None
        while time.time() < deadline:
            if job_id in _cancel_ids:
                raise RuntimeError("canceled")
            try:
                rec["queue_snapshot"] = client.queue_info()
                rec["queue_position"] = _pos(rec["queue_snapshot"], pid)
            except Exception:
                pass
            h = client.history(pid)
            if pid in h:
                hist = h[pid]
                break
            time.sleep(5)
        if hist is None:
            raise TimeoutError(f"no history for {pid} before timeout")

        outs = _collect_outputs(hist)
        od = output_dir()
        pushed = []
        for o in outs:
            f = (od / o["subfolder"] / o["filename"]) if o["subfolder"] \
                else (od / o["filename"])
            if not f.is_file():
                continue
            _push_output(job_id, f)
            pushed.append(o)
        with _lock:
            # publish history ONLY after every artefact has been pushed: the
            # main's wait_for() treats history as "job finished", and the
            # immediate download() must find the file already stored (a race
            # here made remote jobs look failed right after a success).
            rec["history"] = {pid: hist}
            rec["status"] = "done"
            rec["outputs"] = pushed
        log(f"job {job_id} done — {len(pushed)} output(s) pushed")
    except Exception as e:
        with _lock:
            rec["status"] = "failed"
            if job_id in _cancel_ids:
                rec["status"] = "canceled"
                rec["error"] = "canceled"
                _cancel_ids.discard(job_id)
            else:
                rec["error"] = str(e)[:400]
        log(f"job {job_id} → {rec['status']}: {rec['error']}")
    finally:
        with _lock:
            if _current == job_id:
                _current = None


# ------------------------------------------------------- model installs
def _resolve_model_path(rel: str) -> Path:
    """Map a main-tree-relative path onto this box's dirs — the exact
    contract of POST /install:
      models/<rest>         -> models_dir/<rest>
      ComfyUI/models/<rest> -> comfyui_dir/models/<rest>
      anything else         -> models_dir/<path>
    Traversal-safe: the resolved path must stay inside its root."""
    rel = str(rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise HTTPException(422, "invalid path")
    if parts[0] == "ComfyUI" and len(parts) > 1:
        root = Path(CFG.get("comfyui_dir") or "")
        parts = parts[1:]
    elif parts[0] == "models" and len(parts) > 1:
        root = Path(CFG.get("models_dir") or "")
        parts = parts[1:]
    else:
        root = Path(CFG.get("models_dir") or "")
    if not root.exists():
        raise HTTPException(500, "worker model/comfyui dir is not configured")
    root_res = root.resolve()
    dest = (root.joinpath(*parts)).resolve()
    if not dest.is_relative_to(root_res):
        raise HTTPException(422, "path escapes the worker tree")
    return dest


def _assert_fetchable_url(url: str) -> None:
    """SSRF guard for /install_url: https-only public hosts by default."""
    u = urllib.parse.urlparse(str(url or ""))
    if u.scheme == "https":
        pass
    elif u.scheme == "http" and CFG.get("install_allow_http"):
        pass
    else:
        raise HTTPException(422, f"scheme not allowed: {u.scheme or '?'}")
    host = (u.hostname or "").lower()
    if not host:
        raise HTTPException(422, "URL has no host")
    if CFG.get("install_allow_private_hosts"):
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise HTTPException(422, f"cannot resolve {host}: {e}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        # is_global also rules out CGNAT/mesh space (100.64/10), which
        # is_private does not cover.
        if not ip.is_global or ip.is_multicast:
            raise HTTPException(
                422, f"{host} resolves to a non-public address ({ip}) — "
                     "refused by the install guard")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF guard on every redirect hop (Hugging Face resolves to
    a CDN, so redirects must work) and never forward the HF token to a host
    other than the one it was issued for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_fetchable_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
            new_host = (urllib.parse.urlparse(newurl).hostname or "").lower()
            if old_host != new_host:
                new.remove_header("Authorization")
        return new


_DOWNLOAD_OPENER = urllib.request.build_opener(_GuardedRedirect())


def _hf_headers(url: str) -> dict:
    headers: dict = {}
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "huggingface.co" or host.endswith(".huggingface.co"):
        tok = _hf_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    return headers


def _download_to_model(url: str, dest: Path, sha256: str = "",
                       size: int = 0, resume: bool = True) -> dict:
    """Stream url → dest via a .part file with Range resume, incremental
    sha256 and an atomic rename. Never holds more than one chunk in RAM
    (the whole point vs POST /install for multi-GB weights)."""
    _assert_fetchable_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(dest.parent)).free
    if size and free < int(size) + 2 * 2**30:
        raise HTTPException(
            507, f"not enough disk: need {size / 2**30:.1f}GB + margin, "
                 f"only {free / 2**30:.1f}GB free")
    tmp = dest.with_name(dest.name + ".part")
    done = tmp.stat().st_size if (resume and tmp.exists()) else 0
    hasher = hashlib.sha256()
    if done:
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(8 * 2**20), b""):
                hasher.update(chunk)
        if size and done > int(size):
            done = 0            # stale/partial from a different file
            hasher = hashlib.sha256()
            tmp.unlink(missing_ok=True)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "kinoloom-worker-agent/" + AGENT_VERSION)
    for k, v in _hf_headers(url).items():
        req.add_header(k, v)
    if done:
        req.add_header("Range", f"bytes={done}-")
    t0 = time.time()
    try:
        with _DOWNLOAD_OPENER.open(req, timeout=90) as r:
            restarted = not (done and r.status == 206)
            if restarted and done:
                hasher = hashlib.sha256()
                done = 0
            mode = "ab" if done else "wb"
            got = done
            with open(tmp, mode) as f:
                while True:
                    chunk = r.read(8 * 2**20)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    got += len(chunk)
                    if size and got > int(size):
                        raise HTTPException(422, "download exceeds declared size")
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 416:       # range beyond EOF → .part already complete
            got = done
        else:
            raise
    total = tmp.stat().st_size
    if size and total != int(size):
        raise HTTPException(
            422, f"size mismatch after download: got {total}, want {size}")
    if sha256:
        actual = hasher.hexdigest()
        if actual.lower() != str(sha256).lower():
            tmp.unlink(missing_ok=True)
            raise HTTPException(422, "sha256 mismatch — download corrupted")
    tmp.replace(dest)
    dt = max(time.time() - t0, 0.001)
    log(f"installed {dest.name} ({total / 2**30:.2f}GB, "
        f"{total / dt / 2**20:.0f}MB/s avg, verified={bool(sha256)})")
    return {"ok": True, "path": str(dest), "size": total,
            "verified": bool(sha256), "resumed_from": done}


def _spec_to_url(spec: dict) -> str:
    """Accept a direct url, or an HF repo + file (private-repo friendly)."""
    if spec.get("url"):
        return str(spec["url"])
    repo = str(spec.get("hf_repo") or "").strip().strip("/")
    fname = str(spec.get("hf_file") or spec.get("path") or "").strip("/")
    if not repo or not fname:
        raise HTTPException(422, "need url, or hf_repo + hf_file")
    rev = str(spec.get("hf_revision") or "main")
    if repo.startswith("datasets/"):
        return f"https://huggingface.co/{repo}/resolve/{rev}/{fname}"
    return f"https://huggingface.co/{repo}/resolve/{rev}/{fname}"


def _install_one(spec: dict) -> dict:
    rel = str(spec.get("path") or "")
    dest = _resolve_model_path(rel)
    if dest.exists() and spec.get("bytes") \
            and dest.stat().st_size == int(spec["bytes"]) \
            and not spec.get("verify_existing"):
        return {"path": rel, "ok": True, "skipped": "exists (size match)",
                "size": dest.stat().st_size}
    url = _spec_to_url(spec)
    return _download_to_model(
        url, dest, sha256=str(spec.get("sha256") or ""),
        size=int(spec.get("bytes") or 0))


# ---------------------------------------------------------------- routes
def _require_token(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    is_loopback = False
    if client_host in ("127.0.0.1", "::1", "localhost"):
        is_loopback = True
    elif client_host:
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            pass

    expected = str(CFG.get("token") or "").strip()
    if not expected:
        if not is_loopback:
            raise HTTPException(401, "unauthenticated: worker token not configured; remote requests rejected")
        return

    provided = request.headers.get("X-Worker-Token", "").strip()
    if not provided or not secrets.compare_digest(provided.encode("utf-8"),
                                                  expected.encode("utf-8")):
        raise HTTPException(401, "invalid worker token")


token_dep = Depends(_require_token)


@app.get("/ping")
def ping():
    return {"ok": True, "agent_version": AGENT_VERSION, "name": CFG["name"]}


@app.get("/gpu", dependencies=[token_dep])
def gpu():
    comfy = {}
    try:
        comfy = comfy_get("/system_stats")
    except Exception:
        pass
    return {"gpus": gpu_stats(), "comfy": comfy, "current_job": _current,
            "disk": disk_stats()}


@app.get("/disk", dependencies=[token_dep])
def disk():
    return disk_stats()


@app.get("/models", dependencies=[token_dep])
def models():
    return model_inventory()


@app.get("/queue", dependencies=[token_dep])
def queue_info():
    try:
        return comfy_get("/queue")
    except Exception as e:
        raise HTTPException(503, f"comfy unreachable: {e}")


@app.get("/object_info", dependencies=[token_dep])
def object_info():
    try:
        return comfy_get("/object_info", timeout=120)
    except Exception as e:
        raise HTTPException(503, f"comfy unreachable: {e}")


@app.post("/upload")
async def upload(request: Request, image: UploadFile):
    _require_token(request)
    raw_name = image.filename or ""
    clean_name = ntpath.basename(raw_name.replace("\\", "/").strip())
    if not clean_name or clean_name != raw_name or ".." in raw_name:
        raise HTTPException(400, "Path traversal rejected: invalid filename")
    target_dir = ensure_input_dir().resolve()
    dest = (target_dir / clean_name).resolve()
    if not dest.is_relative_to(target_dir):
        raise HTTPException(400, "Path traversal rejected: destination escapes input directory")
    tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex[:8]}.part")
    try:
        with open(tmp, "wb") as out:
            shutil.copyfileobj(image.file, out, 4 * 2**20)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"name": clean_name}


@app.post("/install")
async def install(request: Request, path: str = "", sha256: str = "",
                  size: int = 0):
    """Receive ONE model file from the main station (raw body), verify it and
    write it under the worker's tree.

    The body is streamed to a .part file with an incremental sha256, so the
    main station can push multi-GB weights without either side buffering
    them. Size and hash are checked before the atomic rename."""
    _require_token(request)
    dest = _resolve_model_path(path)
    want = int(size or 0)
    if want < 0:
        raise HTTPException(422, "invalid size")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if want:
        free = shutil.disk_usage(str(dest.parent)).free
        if free < want + 2 * 2**30:
            raise HTTPException(
                507, f"not enough disk: need {want / 2**30:.1f}GB + margin, "
                     f"only {free / 2**30:.1f}GB free")
    tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex[:8]}.part")
    hasher = hashlib.sha256()
    got = 0
    verified = False
    try:
        with open(tmp, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                got += len(chunk)
                if want and got > want:
                    raise HTTPException(422, f"size mismatch: body exceeds {want} bytes")
                hasher.update(chunk)
                out.write(chunk)
        if want and got != want:
            raise HTTPException(422, f"size mismatch: got {got}, want {want}")
        if sha256:
            if hasher.hexdigest().lower() != str(sha256).lower():
                raise HTTPException(422, "sha256 mismatch — transfer corrupted")
            verified = True
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"installed {path} -> {dest} ({got} bytes, verified={verified})")
    return {"ok": True, "path": path, "dest": str(dest),
            "size": got, "verified": verified}


@app.post("/install_url")
def install_url(request: Request, spec: dict):
    """Pull ONE model file onto this box's own pipe (HF repo or any URL).
    Streams to disk with resume — the multi-GB path."""
    _require_token(request)
    try:
        return _install_one(spec)
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"download failed: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"install failed: {e}")


@app.post("/install_group")
def install_group(request: Request, body: dict):
    """Background batch install (one model group). body:
      {install_id, files: [{path, url|hf_repo+hf_file, bytes?, sha256?}]}
    Progress: GET /install_group/{install_id}"""
    _require_token(request)
    iid = str(body.get("install_id") or uuid.uuid4().hex[:10])
    files = body.get("files") or []
    if not files:
        raise HTTPException(422, "no files in group")
    with _lock:
        prev = _installs.get(iid)
        if prev and prev["status"] == "running":
            return {"ok": True, "install_id": iid, "already_running": True}
        _installs[iid] = {"status": "running", "files": [
            {"path": str(f.get("path")), "ok": False, "done": False}
            for f in files], "current": "", "started": time.time()}

    def run() -> None:
        rec = _installs[iid]
        for i, spec in enumerate(files):
            rec["current"] = str(spec.get("path"))
            try:
                r = _install_one(spec)
                rec["files"][i] = {"path": rec["current"], "ok": True,
                                   "done": True, **r}
            except Exception as e:  # noqa: BLE001
                rec["files"][i] = {"path": rec["current"], "ok": False,
                                   "done": True, "error": str(e)[:300]}
                log(f"install_group {iid}: {rec['current']} failed: {e}")
        rec["status"] = "done" if all(f["ok"] for f in rec["files"]) \
            else "failed"
        rec["finished"] = time.time()
        rec["current"] = ""
        log(f"install_group {iid} → {rec['status']} "
            f"({sum(1 for f in rec['files'] if f['ok'])}/{len(rec['files'])})")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "install_id": iid}


@app.get("/install_group/{iid}", dependencies=[token_dep])
def install_group_status(iid: str):
    rec = _installs.get(iid)
    if not rec:
        raise HTTPException(404, "unknown install id")
    return rec


@app.delete("/models")
def delete_model(request: Request, path: str = ""):
    """Delete one model file (traversal-safe — same mapping as /install)."""
    _require_token(request)
    dest = _resolve_model_path(path)
    if not dest.is_file():
        raise HTTPException(404, f"no such model file: {path}")
    size = dest.stat().st_size
    dest.unlink()
    log(f"deleted {path} ({size} bytes)")
    return {"ok": True, "path": path, "freed": size}


@app.post("/job")
def job(request: Request, body: dict):
    global _current
    _require_token(request)
    caps = capabilities()
    if caps.get("blocked"):
        raise HTTPException(
            503, "worker blocked — missing required models: "
                 + ", ".join(caps.get("required_missing") or []))
    job_id = str(body.get("job_id") or uuid.uuid4().hex[:12])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise HTTPException(422, "invalid job_id")
    with _lock:
        if _current is not None:
            raise HTTPException(409, "worker is busy with another job")
        _jobs[job_id] = {"job_id": job_id, "status": "queued",
                         "workflow": body.get("workflow") or {},
                         "client_id": body.get("client_id"),
                         "timeout": body.get("timeout", 3600),
                         "prompt_id": "", "error": "",
                         "queue_position": None, "queue_snapshot": {},
                         "history": {}, "outputs": []}
        _current = job_id
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"ok": True, "job_id": job_id, "prompt_id": job_id}


@app.get("/job/{job_id}", dependencies=[token_dep])
def job_status(job_id: str):
    with _lock:
        rec = _jobs.get(job_id)
        if not rec:
            raise HTTPException(404, "unknown job")
        return dict(rec)


@app.post("/cancel/{job_id}")
def cancel(request: Request, job_id: str):
    global _current
    _require_token(request)
    with _lock:
        rec = _jobs.get(job_id)
        if not rec:
            raise HTTPException(404, "unknown job")
        if rec["status"] in ("queued", "running"):
            _cancel_ids.add(job_id)
            try:
                comfy_post("/interrupt", {})
            except Exception as e:
                log(f"interrupt failed: {e}")
            if rec["status"] == "queued" or not rec.get("prompt_id"):
                rec["status"] = "canceled"
                rec["error"] = "canceled"
                if _current == job_id:
                    _current = None
    return {"ok": True}


@app.get("/selftest", dependencies=[token_dep])
def selftest():
    res = {"comfy": comfy_up(), "gpus": gpu_stats(), "checks": {},
           "disk_free_gb": None, "models": model_inventory(),
           "required_missing": required_missing()}
    if res["comfy"]:
        try:
            info = comfy_get("/object_info", timeout=120)
            need = (["UnetLoaderGGUF", "CLIPLoaderGGUF", "VAELoader",
                     "KSampler", "LoraLoaderModelOnly", "CLIPTextEncode"]
                    if not CFG.get("selftest_nodes")
                    else [str(n) for n in CFG["selftest_nodes"]])
            res["checks"]["nodes"] = {n: (n in info) for n in need}
            res["checks"]["video_saver"] = any(
                k in info for k in ("VHS_SaveVideo", "SaveVideo", "SaveImage"))
        except Exception as e:
            res["checks"]["object_info"] = f"failed: {e}"
    try:
        res["disk_free_gb"] = round(shutil.disk_usage(str(AGENT_DIR)).free
                                    / 2**30, 1)
    except Exception:
        pass
    res["ok"] = bool(res["comfy"])
    return res


def boot() -> None:
    threading.Thread(target=registration_loop, daemon=True).start()
    threading.Thread(target=comfy_watchdog, daemon=True).start()
    log(f"agent {AGENT_VERSION} up — main={CFG['main_url']} "
        f"comfy={_comfy_base} advertise={_advertised_host()}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(CFG["agent_port"]))
