"""GPU lab for the seamless-chain work: clean instances, validated graphs, monitored runs.

Every experiment gets its own run directory under output/seamlab/runs/ holding the
environment (git head, hashes of the chain code, GPU/driver, ComfyUI flags), and per
job: the submitted graph, the websocket event log, a 2 s GPU/RAM telemetry trace,
the server log slice, per-node timings and a result summary.

Clean state: an experiment starts its own ComfyUI_LTX instance on a lab port (never
the studio's 8189/8199), and before every job the instance is told to unload all
models and drop its node cache; if VRAM does not come back to the post-start
baseline the instance is restarted.

Hang protection: a job is declared stalled when no websocket event arrived for
--stall seconds while GPU and server CPU sat idle for the last two minutes, for
--hard-stall seconds while the GPU was not kept busy (a slow 1080p sampler step can
run past 15 min without an event while the GPU computes), or for an hour at all. A
stalled job gets a Python stack dump of every server thread (see
_comfy_lab_boot.py), an interrupt, and the instance is killed if the interrupt does
not land.

usage (tool\\.venv python):
  chain_lab.py baseline chain_bcb2e4a7
  chain_lab.py ltx-smoke
  chain_lab.py ltx-gate [--steps draft,finish,assemble] [--variant F0] [--set cq_strength=0.6 ...]
  chain_lab.py h3-smoke
  chain_lab.py h3-gate [--steps link,refine,take] [--variant R0] [--set sigmas=...]
  chain_lab.py qa <video> --seams 121,241 [--out DIR]
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))

import chain_engine as ce  # noqa: E402
import chain_graphs as cg  # noqa: E402
import seam_qa  # noqa: E402

TREE = ROOT / "ComfyUI_LTX"
COMFY_OUT = TREE / "output"
COMFY_IN = TREE / "input"
LAB = ROOT / "output" / "seamlab"
BOOT = Path(__file__).with_name("_comfy_lab_boot.py")
PROFILES = {
    "ltx": {"port": 8289, "tree": TREE, "args": ["--enable-dynamic-vram", "--bf16-vae"]},
    "h3": {"port": 8299, "tree": TREE, "args": ["--enable-dynamic-vram"]},
    # Qwen-Image 2.1 on its own tree, with the studio launcher's flags (docs/research/PROVEN_CONFIGS_QWEN21.md §2)
    "qwen": {"port": 8291, "tree": ROOT / "ComfyUI_Qwen21", "args": ["--highvram"]},
}
TRACKED = [
    "ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/studio_chain_nodes.py",
    "ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/studio_chain_core.py",
    "ComfyUI_LTX/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/nodes/MMH3_Split_Upscale.py",
    "tool/chain_graphs.py", "tool/chain_engine.py", "tool/chain_assemble.py", "tool/seam_qa.py",
    "tool/scripts/chain_lab.py",
]


def ts() -> str:
    return time.strftime("%H:%M:%S")


def say(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages").glob(
            "Gyan.FFmpeg*/*/bin/ffmpeg.exe"):
        return str(p)
    raise SystemExit("ffmpeg not found")


# ================================================================ telemetry
def gpu_sample() -> dict:
    q = "memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu,clocks.sm"
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        used, total, util, power, temp, sm = [x.strip() for x in out.split(",")]
        return {"vram_mb": int(float(used)), "vram_total_mb": int(float(total)), "util": int(float(util)),
                "power_w": float(power), "temp_c": int(float(temp)), "sm_mhz": int(float(sm))}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


class Telemetry(threading.Thread):
    def __init__(self, pid: int, path: Path, period: float = 2.0) -> None:
        super().__init__(daemon=True, name="lab-telemetry")
        import psutil
        self.proc = psutil.Process(pid)
        self.path, self.period = path, period
        self.stop_evt = threading.Event()
        self.recent: collections.deque = collections.deque(maxlen=90)
        self.peak = {"vram_mb": 0, "rss_mb": 0, "power_w": 0.0, "temp_c": 0}
        self.min_ram_avail_mb = 1 << 30

    def _cpu_rss(self) -> tuple[float, float]:
        # cpu_percent() measures since the previous call on the SAME Process object,
        # so children are cached by pid (fresh objects would always read 0)
        import psutil
        procs = getattr(self, "_procs", None)
        if procs is None:
            procs = self._procs = {self.proc.pid: self.proc}
        try:
            for c in self.proc.children(recursive=True):
                procs.setdefault(c.pid, c)
        except psutil.Error:
            pass
        cpu, rss = 0.0, 0.0
        for pid, p in list(procs.items()):
            try:
                cpu += p.cpu_percent(None)
                rss += p.memory_info().rss
            except psutil.Error:
                procs.pop(pid, None)
        return cpu, rss / 2**20

    def run(self) -> None:
        import psutil
        self._cpu_rss()
        with open(self.path, "a", encoding="utf-8") as f:
            while not self.stop_evt.wait(self.period):
                g = gpu_sample()
                cpu, rss = self._cpu_rss()
                avail = psutil.virtual_memory().available / 2**20
                row = {"t": round(time.time(), 1), **g, "cpu_pct": round(cpu, 1), "rss_mb": round(rss),
                       "ram_avail_mb": round(avail)}
                f.write(json.dumps(row) + "\n")
                f.flush()
                self.recent.append(row)
                for k in ("vram_mb", "power_w", "temp_c"):
                    if k in g:
                        self.peak[k] = max(self.peak[k], g[k])
                self.peak["rss_mb"] = max(self.peak["rss_mb"], round(rss))
                self.min_ram_avail_mb = min(self.min_ram_avail_mb, round(avail))

    def idle_for(self, seconds: float) -> bool:
        now = time.time()
        rows = [r for r in self.recent if now - r["t"] <= seconds]
        if len(rows) < seconds / self.period * 0.6:
            return False
        return all(r.get("util", 100) < 5 and r.get("cpu_pct", 100) < 15 for r in rows)

    def busy_for(self, seconds: float) -> bool:
        """GPU kept at >= 90% utilisation for the whole window (a job still computing,
        however slowly: one 1080p sampler step can exceed 15 min without an event)."""
        now = time.time()
        rows = [r for r in self.recent if now - r["t"] <= seconds]
        if len(rows) < seconds / self.period * 0.6:
            return False
        return all(r.get("util", 0) >= 90 for r in rows)

    def starved_for(self, seconds: float, max_power_w: float = 110.0) -> bool:
        """GPU pinned at 100% but drawing little power: weights streaming over PCIe
        (dynamic VRAM thrash): progress continues at a fraction of normal speed."""
        now = time.time()
        rows = [r for r in self.recent if now - r["t"] <= seconds]
        if len(rows) < seconds / self.period * 0.6:
            return False
        return all(r.get("util", 0) >= 95 and r.get("power_w", 999) < max_power_w for r in rows)

    def last(self) -> dict:
        return self.recent[-1] if self.recent else {}

    def stop(self) -> None:
        self.stop_evt.set()
        self.join(timeout=10)


# ================================================================= instance
class Instance:
    def __init__(self, profile: str, run_dir: Path, extra_args: list[str] | None = None) -> None:
        self.profile = profile
        self.port = PROFILES[profile]["port"]
        self.tree = Path(PROFILES[profile]["tree"])
        self.base = f"http://127.0.0.1:{self.port}"
        self.run_dir = run_dir
        self.extra_args = list(extra_args or [])
        self.log_path = run_dir / f"comfy_{profile}.log"
        self.dump_trigger = run_dir / f".dump_{profile}"
        self.dump_file = run_dir / f"stackdump_{profile}.txt"
        self.proc: subprocess.Popen | None = None
        self.object_info: dict = {}
        self.system_stats: dict = {}
        self.vram_baseline_mb = 0
        self.starts = 0
        self.jobs = 0                    # graphs queued since the last start

    # http
    def get(self, path: str, timeout: float = 30) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")

    def post(self, path: str, body: dict, timeout: float = 60) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_offset(self) -> int:
        return self.log_path.stat().st_size if self.log_path.exists() else 0

    def start(self, timeout: float = 420) -> None:
        import socket
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", self.port)) == 0:
                raise RuntimeError(f"port {self.port} is already in use; stop that server first")
        db = (LAB / f"comfy_lab_{self.profile}.db").as_posix()
        py = self.tree / "venv" / "Scripts" / "python.exe"
        cmd = [str(py), str(BOOT), str(self.tree / "main.py"), "--port", str(self.port), "--listen", "127.0.0.1",
               "--disable-auto-launch", "--preview-method", "none", "--database-url", f"sqlite:///{db}",
               *PROFILES[self.profile]["args"], *self.extra_args]
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        env.update({"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                    "LAB_DUMP_TRIGGER": str(self.dump_trigger), "LAB_DUMP_FILE": str(self.dump_file)})
        self.starts += 1
        self.jobs = 0
        with open(self.log_path, "ab") as lf:
            lf.write(f"\n===== lab start #{self.starts} {time.strftime('%Y-%m-%d %H:%M:%S')}: "
                     f"{' '.join(cmd)}\n".encode())
        log = open(self.log_path, "ab")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen(cmd, cwd=str(self.tree), stdout=log, stderr=subprocess.STDOUT, env=env,
                                     creationflags=flags)
        log.close()
        t0 = time.time()
        say(f"{self.profile}: starting ComfyUI on :{self.port} (pid {self.proc.pid})")
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"ComfyUI exited with {self.proc.returncode} during start; see {self.log_path}")
            try:
                self.system_stats = self.get("/system_stats", timeout=5)
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(2)
        else:
            self.kill()
            raise RuntimeError(f"ComfyUI did not come up in {timeout:.0f} s; see {self.log_path}")
        self.object_info = self.get("/object_info", timeout=120)
        time.sleep(3)
        self.vram_baseline_mb = int(gpu_sample().get("vram_mb", 0))
        say(f"{self.profile}: up in {time.time() - t0:.0f} s, {len(self.object_info)} node types, "
            f"VRAM baseline {self.vram_baseline_mb} MB")

    def free(self) -> None:
        try:
            self.post("/free", {"unload_models": True, "free_memory": True}, timeout=30)
        except Exception as e:  # noqa: BLE001
            say(f"{self.profile}: /free failed: {e}")

    def clean(self, slack_mb: int = 700, wait_s: float = 45) -> dict:
        """Unload everything and drop the cache; restart if VRAM stays high.

        An H3 instance is not sent /free: it stays warm between the jobs of one
        command, as a studio lease runs a chain's links (each command starts its own
        instance). /free followed by a reload is the H3 path the studio's broker
        avoids (gpu_broker h3_mode: restart, aimdo livelock), and the studio box
        blue-screened within a minute of an H3 model release three times on 2026-10-23
        (07:05 0x7E and 14:46 0x50 after /free, 15:54 0x50 at link -> take decode).
        A graph that would drop a model gets a fresh process (fresh_process)."""
        if self.profile == "h3":
            return {"vram_mb": int(gpu_sample().get("vram_mb", 0)), "restarted": False, "kept_warm": True}
        self.free()
        t0 = time.time()
        vram = 0
        while time.time() - t0 < wait_s:
            time.sleep(3)
            vram = int(gpu_sample().get("vram_mb", 0))
            if vram <= self.vram_baseline_mb + slack_mb:
                return {"vram_mb": vram, "restarted": False}
        say(f"{self.profile}: VRAM stuck at {vram} MB after /free (baseline {self.vram_baseline_mb}); restarting")
        self.stop()
        self.start()
        return {"vram_mb": int(gpu_sample().get("vram_mb", 0)), "restarted": True}

    def stack_dump(self, wait_s: float = 8) -> str:
        before = self.dump_file.stat().st_size if self.dump_file.exists() else 0
        self.dump_trigger.write_text("dump", encoding="utf-8")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            time.sleep(0.5)
            if self.dump_file.exists() and self.dump_file.stat().st_size > before:
                time.sleep(1.0)
                with open(self.dump_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(before)
                    return f.read()
        return "(no stack dump: the watcher did not answer)"

    def interrupt(self) -> None:
        try:
            self.post("/interrupt", {}, timeout=10)
        except Exception as e:  # noqa: BLE001
            say(f"{self.profile}: interrupt failed: {e}")

    def kill(self) -> None:
        if self.proc is None:
            return
        import psutil
        try:
            parent = psutil.Process(self.proc.pid)
            for p in parent.children(recursive=True) + [parent]:
                try:
                    p.kill()
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        self.proc = None

    def stop(self) -> None:
        if not self.alive():
            self.proc = None
            return
        assert self.proc is not None
        if self.profile != "h3":         # H3 is never sent /free (clean): the kill releases it
            self.free()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        self.kill()
        time.sleep(3)
        say(f"{self.profile}: stopped")


# ================================================================ validation
def _spec_type(spec) -> tuple[str, list | None]:
    t = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
    if isinstance(t, list):
        return "COMBO", t
    opts = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict) else {}
    if t == "COMBO":
        return "COMBO", list(opts.get("options") or [])
    return str(t), None


def validate(graph: dict, info: dict) -> list[str]:
    """Static check of an API graph against /object_info: node classes, required and
    unknown inputs, link targets and slots, link types, combo values (model files)."""
    errs: list[str] = []
    for nid, node in graph.items():
        ct = node.get("class_type")
        spec = info.get(ct)
        if spec is None:
            errs.append(f"#{nid}: unknown node class {ct}")
            continue
        req = (spec.get("input") or {}).get("required") or {}
        opt = (spec.get("input") or {}).get("optional") or {}
        known = {**req, **opt}
        inputs = node.get("inputs") or {}
        for name, s in req.items():
            t, _ = _spec_type(s)
            if name in inputs or any(k.startswith(name + ".") for k in inputs):
                continue
            if t.startswith("COMFY_"):
                continue
            errs.append(f"#{nid} {ct}: missing required input '{name}'")
        for name, v in inputs.items():
            head = name.split(".", 1)[0]
            if head not in known:
                errs.append(f"#{nid} {ct}: unknown input '{name}'")
                continue
            if "." in name:
                continue
            t, options = _spec_type(known[name])
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                src = graph.get(v[0])
                if src is None:
                    errs.append(f"#{nid} {ct}.{name}: links to missing node #{v[0]}")
                    continue
                outs = (info.get(src.get("class_type")) or {}).get("output") or []
                if not 0 <= int(v[1]) < len(outs):
                    errs.append(f"#{nid} {ct}.{name}: slot {v[1]} out of range for {src.get('class_type')}")
                    continue
                st = outs[int(v[1])]
                st = "COMBO" if isinstance(st, list) else str(st)
                if t != "COMBO" and "*" not in (t, st) and not (set(t.split(",")) & set(st.split(","))):
                    errs.append(f"#{nid} {ct}.{name}: expects {t}, gets {st} from #{v[0]} "
                                f"{src.get('class_type')}[{v[1]}]")
            elif options is not None and options and v not in options:
                errs.append(f"#{nid} {ct}.{name}: '{v}' is not an available option")
    return errs


# =================================================================== runner
class JobFailed(RuntimeError):
    pass


class LabRunner:
    """ce.Runner over a lab Instance: clean state, websocket tracking, watchdog."""

    def __init__(self, inst: Instance, run_dir: Path, stall_s: float = 600, hard_stall_s: float = 900,
                 timeout_s: float = 4 * 3600, clean: bool = True, ceiling_s: float = 3600) -> None:
        self.inst, self.run_dir = inst, run_dir
        self.stall_s, self.hard_stall_s, self.timeout_s = stall_s, hard_stall_s, timeout_s
        self.ceiling_s = ceiling_s       # no event for this long is a hang even with the GPU busy
        self.clean_between = clean
        self.results: list[dict] = []

    def fresh_process(self, name: str) -> None:
        """chain_engine.H3Chain: restart the instance before a graph that would make
        it drop a model in-process (nothing to do if it has run nothing yet)."""
        if self.inst.alive() and self.inst.jobs:
            say(f"{name}: restarting {self.inst.profile} first, so no model is dropped in-process")
            self.inst.stop()
            # the driver has torn the old process's memory down once VRAM is back at the
            # level the instance booted to; only then does a new one allocate
            t0, vram = time.time(), 0
            while time.time() - t0 < 90:
                vram = int(gpu_sample().get("vram_mb", 0))
                if vram <= self.inst.vram_baseline_mb + 300:
                    break
                time.sleep(2)
            say(f"{name}: VRAM {vram} MB after the stop (boot baseline {self.inst.vram_baseline_mb} MB)")
            self.inst.start()

    def _job_dir(self, name: str) -> Path:
        d = self.run_dir / name
        k = 2
        while d.exists():
            d = self.run_dir / f"{name}_{k}"
            k += 1
        d.mkdir(parents=True)
        return d

    def run(self, name: str, graph: dict) -> dict:
        from websockets.sync.client import connect
        jd = self._job_dir(name)
        (jd / "graph.json").write_text(json.dumps(graph, indent=1), encoding="utf-8")
        if not self.inst.alive():
            self.inst.start()
        errs = validate(graph, self.inst.object_info)
        if errs:
            (jd / "validation.txt").write_text("\n".join(errs), encoding="utf-8")
            raise JobFailed(f"{name}: graph failed validation:\n  " + "\n  ".join(errs[:20]))
        clean = self.inst.clean() if self.clean_between else {}
        cid = uuid.uuid4().hex
        log0 = self.inst.log_offset()
        tel = Telemetry(self.inst.proc.pid if self.inst.proc else 0, jd / "telemetry.jsonl")
        events = open(jd / "events.jsonl", "w", encoding="utf-8")
        ws = connect(f"ws://127.0.0.1:{self.inst.port}/ws?clientId={cid}", max_size=None, open_timeout=30,
                     ping_interval=None)
        t0 = time.time()
        try:
            res = self.inst.post("/prompt", {"prompt": graph, "client_id": cid})
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            (jd / "rejected.json").write_text(body, encoding="utf-8")
            ws.close()
            events.close()
            raise JobFailed(f"{name}: ComfyUI rejected the graph: {body[:1500]}") from e
        pid = res["prompt_id"]
        self.inst.jobs += 1
        tel.start()
        say(f"{name}: queued {pid[:8]} ({len(graph)} nodes, clean {clean})")
        status, error = "running", None
        timings: list[dict] = []
        cur, cur_t0 = None, None
        last_event = time.time()
        last_print = 0.0
        last_starve_warn = 0.0
        prog = ""
        cached: list[str] = []
        stall_info = None
        warnings: list[str] = []
        from websockets.exceptions import ConnectionClosed
        try:
            while True:
                now = time.time()
                if now - t0 > self.timeout_s:
                    status, error = "timeout", f"no result after {self.timeout_s:.0f} s"
                    break
                if not self.inst.alive():
                    status, error = "crashed", "ComfyUI process exited during the job"
                    break
                quiet = now - last_event
                # idle GPU+CPU after stall_s, or no event for hard_stall_s while the GPU is
                # not computing, or no event at all for ceiling_s
                if (quiet > self.ceiling_s or (quiet > self.hard_stall_s and not tel.busy_for(120))
                        or (quiet > self.stall_s and tel.idle_for(120))):
                    stall_info = {"quiet_s": round(quiet), "last": tel.last(), "node": cur, "progress": prog}
                    say(f"{name}: STALL ({stall_info}); dumping stacks and interrupting")
                    (jd / "stall_stacks.txt").write_text(self.inst.stack_dump(), encoding="utf-8")
                    self.inst.interrupt()
                    status, error = "stalled", f"no progress for {quiet:.0f} s"
                    break
                if now - last_print > 30:
                    last_print = now
                    g = tel.last()
                    nm = graph.get(cur or "", {}).get("class_type", "-")
                    say(f"{name}: {now - t0:6.0f}s node {cur}:{nm} {prog} | VRAM {g.get('vram_mb')} MB "
                        f"util {g.get('util')}% {g.get('power_w')} W | RSS {g.get('rss_mb')} MB "
                        f"RAM free {g.get('ram_avail_mb')} MB")
                if now - last_starve_warn > 300 and tel.starved_for(120):
                    last_starve_warn = now
                    w = (f"GPU starved for 2+ min at node {cur} ({graph.get(cur or '', {}).get('class_type')}): "
                         f"100% util at <110 W - weights streaming over PCIe, expect a large slowdown")
                    warnings.append(w)
                    say(f"{name}: WARNING {w}")
                try:
                    msg = ws.recv(timeout=5)
                except TimeoutError:
                    continue
                except ConnectionClosed as e:
                    status, error = "crashed", f"websocket closed ({e}); server alive: {self.inst.alive()}"
                    break
                if isinstance(msg, bytes):
                    continue
                m = json.loads(msg)
                d = m.get("data") or {}
                if d.get("prompt_id") not in (None, pid):
                    continue
                typ = m.get("type")
                if typ in ("status", "crystools.monitor"):
                    continue
                last_event = time.time()
                if typ != "progress_state":
                    events.write(json.dumps({"t": round(last_event - t0, 2), **m}) + "\n")
                    events.flush()
                if typ == "executing":
                    if cur is not None and cur_t0 is not None:
                        timings.append({"node": cur, "class": graph.get(cur, {}).get("class_type"),
                                        "seconds": round(last_event - cur_t0, 2)})
                    cur, cur_t0 = d.get("node"), last_event
                    prog = ""
                    if cur is None:
                        status = "success"
                        break
                elif typ == "progress":
                    prog = f"{d.get('value')}/{d.get('max')}"
                elif typ == "execution_cached":
                    cached = list(d.get("nodes") or [])
                elif typ == "execution_error":
                    status, error = "error", d
                    break
                elif typ == "execution_interrupted":
                    status, error = "interrupted", d
                    break
                elif typ == "execution_success":
                    status = "success"
                    break
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            events.close()
            tel.stop()
        if status == "stalled":
            t1 = time.time()
            while time.time() - t1 < 90 and self.inst.alive():
                try:
                    if not (self.inst.get("/queue", timeout=5).get("queue_running") or []):
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(3)
            try:
                busy = bool(self.inst.get("/queue", timeout=5).get("queue_running"))
            except Exception:  # noqa: BLE001
                busy = True
            if busy:
                say(f"{name}: interrupt did not land; killing the instance")
                self.inst.kill()
        elapsed = time.time() - t0
        hist = {}
        if self.inst.alive():
            try:
                hist = self.inst.get(f"/history/{pid}", timeout=30).get(pid, {})
            except Exception:  # noqa: BLE001
                hist = {}
        with open(self.inst.log_path, "rb") as f:
            f.seek(log0)
            server_log = f.read().decode("utf-8", errors="replace")
        (jd / "server.log").write_text(server_log, encoding="utf-8")
        flags = [ln.strip()[:300] for ln in server_log.splitlines()
                 if any(k in ln for k in ("Traceback", "Error", "error:", "WARNING", "Warning", "out of memory",
                                          "OOM", "nan", "NaN"))
                 and "Prompt executed" not in ln][:60]
        result = {"name": name, "prompt_id": pid, "status": status, "seconds": round(elapsed, 1),
                  "timings": timings, "slowest": sorted(timings, key=lambda r: -r["seconds"])[:6],
                  "cached_nodes": cached, "peak": tel.peak, "min_ram_avail_mb": tel.min_ram_avail_mb,
                  "vram_baseline_mb": self.inst.vram_baseline_mb, "clean": clean, "log_flags": flags,
                  "stall": stall_info, "error": error, "warnings": warnings,
                  "history_status": (hist.get("status") or {}).get("status_str"),
                  "outputs": hist.get("outputs") or {}}
        (jd / "result.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        self.results.append(result)
        top = ", ".join(f"{r['class']} {r['seconds']:.0f}s" for r in result["slowest"][:3])
        say(f"{name}: {status} in {elapsed:.0f} s | peak VRAM {tel.peak['vram_mb']} MB RSS {tel.peak['rss_mb']} MB "
            f"| {top}")
        if status != "success":
            msg = error if isinstance(error, str) else json.dumps(error, default=str)[:3000]
            raise JobFailed(f"{name}: {status}: {msg}")
        return result


# ============================================================== experiments
def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else "-"


def new_run(tag: str) -> Path:
    d = LAB / "runs" / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}"
    d.mkdir(parents=True)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    env = {"tag": tag, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "git_head": head,
           "code": {f: file_sha(ROOT / f) for f in TRACKED}, "gpu": gpu_sample(), "argv": sys.argv}
    (d / "lab.json").write_text(json.dumps(env, indent=1), encoding="utf-8")
    say(f"run dir {d}")
    return d


def _graph_from_media(path: Path) -> dict:
    import av
    with av.open(str(path)) as ct:
        tags = dict(ct.metadata or {})
    raw = tags.get("prompt") or tags.get("PROMPT")
    if not raw:
        raise ValueError(f"{path.name}: no embedded graph")
    return json.loads(raw)


def baseline_links(prefix: str) -> list[dict]:
    """Prompts, seeds, frame counts and references of an old chain's segments, read
    from the graph ComfyUI embedded in each segment mp4."""
    vs = ROOT / "output" / "videostudio"
    segs = sorted(vs.glob(f"{prefix}_seg*.mp4"), key=lambda p: int(p.stem.rsplit("seg", 1)[1]))
    out = []
    for s in segs:
        g = _graph_from_media(s)
        by = lambda ct: [n for n in g.values() if n["class_type"] == ct]  # noqa: E731
        seeds = sorted(int(n["inputs"]["noise_seed"]) for n in by("RandomNoise"))
        row: dict = {"segment": s.name, "seed": seeds[0] if seeds else None}
        r2v = by("MiniMaxH3ReferenceToVideo")
        if r2v:
            row["kind"] = "h3"
            row["prompt"] = r2v[0]["inputs"]["prompt"]
            row["frames"] = int(r2v[0]["inputs"]["length"])
            refs = sorted((k, v) for k, v in r2v[0]["inputs"].items() if k.startswith("ref_images."))
            row["refs"] = [g[v[0]]["inputs"].get("image") for _, v in refs if isinstance(v, list)]
            guides = [g[n["inputs"]["image"][0]]["inputs"].get("image") for n in by("MiniMaxH3AddGuide")
                      if isinstance(n["inputs"].get("image"), list)]
            row["anchor"] = guides[0] if guides else None
            row["steps"] = [n["inputs"].get("steps") for n in by("BasicScheduler")]
        else:
            row["kind"] = "ltx"
            cond = by("LTXVConditioning")[0]
            row["prompt"] = g[cond["inputs"]["positive"][0]]["inputs"]["text"]
            row["negative"] = g[cond["inputs"]["negative"][0]]["inputs"]["text"]
            row["frames"] = int(by("EmptyLTXVLatentVideo")[0]["inputs"]["length"])
            guide = by("ComfyUILTX25MSRMultiReferenceGuide")
            row["refs"] = {k: g[v[0]]["inputs"].get("image") for k, v in (guide[0]["inputs"] if guide else {}).items()
                           if k in ("pic1", "pic2", "pic3", "pic4", "background") and isinstance(v, list)}
        out.append(row)
    return out


def _parse_set(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _write_summary(run_dir: Path, runner: LabRunner | None, extra: dict) -> None:
    doc = {"jobs": runner.results if runner else [], **extra}
    (run_dir / "summary.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")


def _with_cast_line(prompt: str, line: str) -> str:
    """`line` inserted after the prompt's first sentence (the establishing one)."""
    if not line:
        return prompt
    i = prompt.find(". ")
    return f"{prompt} {line}" if i < 0 else f"{prompt[:i + 1]} {line}{prompt[i + 1:]}"


def ltx_gate_spec(chain_id: str, smoke: bool, cast_line: str = "",
                  extra_refs: dict | None = None, seed_shift: int = 0) -> ce.LtxChainSpec:
    base = baseline_links("chain_bcb2e4a7")
    refs = {**base[0]["refs"], **(extra_refs or {})}
    for slot, name in refs.items():
        if not (COMFY_IN / name).is_file():
            raise SystemExit(f"reference {slot}={name} is missing from {COMFY_IN}")
    links = []
    for i, b in enumerate(base[:2] if smoke else base):
        new = b["frames"] - (17 if i else 0)               # new frames of the baseline segment
        frames = (49 if i == 0 else 49) if smoke else (new if i == 0 else new + cg.LTX_CARRY_FRAMES)
        # the baseline carried 17 frames; its prompts say so in both phrasings
        prompt = (b["prompt"].replace("17-frame chain overlap", f"{cg.LTX_CARRY_FRAMES}-frame chain overlap")
                  .replace("17-frame overlap", f"{cg.LTX_CARRY_FRAMES}-frame overlap"))
        prompt = _with_cast_line(prompt, cast_line)
        links.append(ce.Link(prompt=prompt, seed=int(b["seed"]) + seed_shift, frames=frames,
                             negative=b.get("negative", "")))
    return ce.LtxChainSpec(chain_id=chain_id, links=links, refs=refs)


def _stage_input(p: Path) -> str:
    """A reference copied into the ComfyUI input dir under a content name, so a re-run
    of the same request builds the same graphs and reuses its finished steps."""
    p = Path(p)
    name = f"lab_{hashlib.sha256(p.read_bytes()).hexdigest()[:16]}{p.suffix.lower() or '.png'}"
    if not (COMFY_IN / name).is_file():
        shutil.copyfile(p, COMFY_IN / name)
    return name


def _load_request(path: Path, kind: str) -> tuple[dict, dict]:
    """A Video Studio chain request (what the studio's chain form posts) and its
    registry entry:

    {"chain": id, "model": registry id, "prompts": [...], "seed": n, "frames": n,
     "negative": "", "start_image": path, "cast": [{"slot", "path", "type", "name", "description"}]}"""
    import video_models
    req = json.loads(Path(path).read_text(encoding="utf-8"))
    entry = video_models.VIDEO_MODELS[req["model"]]
    if video_models.seamless_kind(entry) != kind:
        raise SystemExit(f"{req['model']} does not render seamless {kind} chains")
    req["prompts"] = [str(p) for p in req["prompts"]]
    req["start_image"] = Path(req["start_image"]) if req.get("start_image") else None
    req["cast"] = list(req.get("cast") or [])
    return req, entry


def ltx_request_spec(path: Path) -> tuple[ce.LtxChainSpec, cg.LtxModels]:
    """The spec chain_studio.run_chain builds from an LTX request: link frames, seeds,
    reference slots, face close-ups and the cast line, exactly as the studio does."""
    import chain_studio as cs
    req, entry = _load_request(path, "ltx")
    prompts, seed, cast = req["prompts"], int(req["seed"]), req["cast"]
    slots = cs.ltx_reference_slots(req["start_image"], cast)
    if cs.LTX_FACE_REFS:
        slots = cs.ltx_face_slots(slots, cast, LAB / "face_refs")
    descs = cs.cast_descriptions(cast) if cs.LTX_CAST_LINE else []
    lf = cs.ltx_link_frames(int(req["frames"]), len(prompts))
    spec = ce.LtxChainSpec(chain_id=req["chain"],
                           links=[ce.Link(prompt=cs.with_cast_line(p, descs), seed=seed + i, frames=lf[i],
                                          negative=str(req.get("negative") or ""))
                                  for i, p in enumerate(prompts)],
                           refs={s: _stage_input(p) for s, p in slots.items()})
    return spec, cg.LtxModels.from_registry(entry)


def h3_request_spec(path: Path) -> tuple[ce.H3ChainSpec, cg.H3Models]:
    """The spec chain_studio.run_chain builds from an H3 request, with the tier's own
    recipe (UNet, steps, shift, attention, 1080p refine) read from the single-clip
    graph video_studio builds for that tier, as video_studio._run_chain_body does."""
    import chain_studio as cs
    import video_studio as vs
    req, entry = _load_request(path, "h3")
    prompts, seed, frames, cast, start = (req["prompts"], int(req["seed"]), int(req["frames"]),
                                          req["cast"], req["start_image"])
    n = len(prompts)
    order = cs.h3_reference_order(cast)
    anchor = start or (order[0] if order else None)
    tier_refs = cast or ([{"slot": "pic1", "path": str(anchor)}] if anchor else [])
    d = entry.get("defaults") or {}
    graph, _tpl, _te = vs._build(req["model"], "t2v", prompts[0], "", seed, int(d.get("width", 960)),
                                 int(d.get("height", 544)), frames, int(d.get("steps", 32)), float(d.get("cfg", 1.0)),
                                 loras=[], start_image=None, teacache_thresh=0.1, teacache_end=0.6, no_cache=False,
                                 shot_id="lab_tier", references=tier_refs, fps_override=int(entry.get("fps") or 24))
    models, link_over, refine_over = cs.h3_tier_params(graph)
    lf = cs.h3_link_frames(frames, n)
    spec = ce.H3ChainSpec(chain_id=req["chain"],
                          links=[ce.Link(prompt=p, seed=seed + i, frames=lf[i]) for i, p in enumerate(prompts)],
                          refs=[_stage_input(p) for p in order],
                          start_image=_stage_input(anchor) if anchor else None,
                          link={**link_over, "decode_draft": False}, refine=dict(refine_over or {}))
    return spec, models


def cmd_ltx(args, smoke: bool) -> None:
    models: cg.LtxModels | None = None
    if args.request:
        run_dir = new_run(f"ltx_req_{Path(args.request).stem}")
        spec, models = ltx_request_spec(Path(args.request))
        chain_id = spec.chain_id
    else:
        run_dir = new_run("ltx_smoke" if smoke else f"ltx_gate_{args.variant}")
        chain_id = args.chain or ("lab_ltx_smoke_" + time.strftime("%m%d_%H%M") if smoke else "lab_ltx_gate_bcb2e4a7")
        spec = ltx_gate_spec(chain_id, smoke, cast_line=args.cast_line,
                             extra_refs=dict(r.split("=", 1) for r in args.ref), seed_shift=args.seed_shift)
    if args.face_refs and not args.request:
        # the studio's face close-ups (chain_studio.ltx_face_slots): the gate's pic1 is
        # the scene key image, its cast portraits are pic2.., crops land in the input dir
        import chain_studio as cs
        cast = [{"slot": s, "path": str(COMFY_IN / spec.refs[s]), "type": "face"}
                for s in ("pic2", "pic3", "pic4") if s in spec.refs]
        slots = cs.ltx_face_slots({s: COMFY_IN / n for s, n in spec.refs.items()}, cast, COMFY_IN)
        spec.refs = {s: Path(p).name for s, p in slots.items()}
        say(f"face close-ups: {({s: n for s, n in spec.refs.items() if n.startswith('face_')})}")
    if args.draft_set:
        spec.draft.update(_parse_set(args.draft_set))
    man_path = COMFY_OUT / "studio_chain" / chain_id / "manifest.json"
    rec1 = ((json.loads(man_path.read_text(encoding="utf-8")).get("steps") or {}).get("draft_l1")
            if man_path.is_file() else None)
    if rec1 and rec1.get("ok") and "cq_strength" not in spec.draft:
        # an existing chain keeps the CQ strength its drafts were sampled with (the
        # finish refines with the draft's model): the one whose link-1 graph matches
        probe = ce.LtxChain(dataclasses.replace(spec), None, run_dir / "probe")  # type: ignore[arg-type]
        for cq in (spec.cq_strength, 0.6, 0.0):
            probe.spec.cq_strength = cq
            if ce.digest_matches(rec1, probe.draft_graph(0)):
                if cq != spec.cq_strength:
                    say(f"existing chain: its drafts used cq_strength {cq}; keeping it")
                spec.cq_strength = cq
                break
    finish_set = _parse_set(args.set)

    def finish_over(i: int) -> dict:
        # a stage-2-only cast line: the drafts (and their digests) stay as they are
        if not args.finish_cast_line:
            return finish_set
        return {**finish_set, "prompt": _with_cast_line(spec.links[i].prompt, args.finish_cast_line)}

    (run_dir / "spec.json").write_text(json.dumps({"chain": chain_id, "links": [vars(lk) for lk in spec.links],
                                                   "refs": spec.refs, "draft": spec.draft,
                                                   "finish": finish_set, "finish_cast_line": args.finish_cast_line,
                                                   "variant": args.variant, "request": args.request,
                                                   "models": dataclasses.asdict(models) if models else None},
                                                  indent=1), encoding="utf-8")
    inst = Instance("ltx", run_dir, args.comfy_arg)
    runner = LabRunner(inst, run_dir, stall_s=args.stall, hard_stall_s=args.hard_stall)
    chain = ce.LtxChain(spec, runner, COMFY_OUT, models=models)
    if args.seed_from and not chain.manifest.done("draft_l1", chain.draft_graph(0)):
        seed_link1(chain, args.seed_from)
    steps = set(args.steps.split(","))
    n_draft = min(args.draft_links or len(spec.links), len(spec.links))
    n_finish = min(args.finish_links or len(spec.links), len(spec.links))
    whole = n_finish == len(spec.links)            # a final take needs every link finished
    drafted = n_draft == len(spec.links)           # so do the audio take and the draft take
    if not drafted:
        steps -= {"audio", "take", "assemble"}
        n_finish = min(n_finish, n_draft)
        whole = False
    extra: dict = {"chain": chain_id}
    try:
        # validate every graph of the plan before any GPU time is spent
        if steps & {"draft", "audio", "finish"}:
            inst.start()
            plan = []
            if "draft" in steps:
                plan += [(f"draft_l{i + 1}", chain.draft_graph(i)) for i in range(n_draft)]
            if "audio" in steps and len(spec.links) > 1:
                plan.append(("audio_take", chain.audio_take_graph()))
            if "finish" in steps:
                plan += [(f"finish_l{i + 1}", chain.finish_graph(i, args.variant, finish_over(i)))
                         for i in range(n_finish)]
            if "take" in steps and spec.take_decode:
                plan.append(("take_draft", chain.take_graph("draft")))
                if args.variant != "-" and whole:
                    plan.append(("take_final", chain.take_graph("final", args.variant)))
            bad = {n: validate(g, inst.object_info) for n, g in plan}
            bad = {n: e for n, e in bad.items() if e}
            if bad:
                (run_dir / "validation.json").write_text(json.dumps(bad, indent=1), encoding="utf-8")
                raise SystemExit("plan failed validation: " + json.dumps(bad, indent=1)[:4000])
            say(f"plan validated: {len(plan)} graphs")
        if "draft" in steps:
            for i in range(n_draft):
                info = chain.draft(i, force=args.force)
                say(f"draft {i + 1}: {'reused' if info.get('skipped') else 'rendered'} {info.get('seconds')} s")
            if not drafted:
                say(f"drafted links 1..{n_draft} of {len(spec.links)}: no audio take, take or assemble")
        if "audio" in steps and len(spec.links) > 1:
            info = chain.audio_take(force=args.force)
            say(f"audio take: {'reused' if info.get('skipped') else 'decoded'} {info.get('audio')} "
                f"joins {info.get('join', {}).get('joins')}")
        if "take" in steps and spec.take_decode:
            info = chain.take("draft", force=args.force)
            say(f"draft take: {'reused' if info.get('skipped') else 'decoded'} {info.get('seconds')} s, "
                f"junctions {[(j['take_frame'], j['step_ratio']) for j in info['joins']]}")
        if "finish" in steps:
            for i in range(n_finish):
                info = chain.finish(i, args.variant, force=args.force, **finish_over(i))
                say(f"finish {i + 1}: {'reused' if info.get('skipped') else 'rendered'} {info.get('seconds')} s")
        if not whole:
            say(f"finished links 1..{n_finish} of {len(spec.links)}: no final take or final assemble")
        if "take" in steps and spec.take_decode and args.variant != "-" and whole:
            info = chain.take("final", args.variant, force=args.force)
            say(f"final take: {'reused' if info.get('skipped') else 'decoded'} {info.get('seconds')} s, "
                f"junctions {[(j['take_frame'], j['step_ratio']) for j in info['joins']]}")
        inst.stop()
        ff = ffmpeg_exe()
        if "assemble" in steps:
            for stage in (["draft"] if args.variant == "-" or not whole else ["draft", "final"]):
                for xf in ([None] if spec.take_decode else args.crossfade):
                    tag = f"{stage}_{args.variant}_" + ("take" if xf is None else f"x{xf}")
                    mp4 = run_dir / f"{chain_id}_{tag}.mp4"
                    res = chain.assemble(mp4, ff, stage=stage, variant=args.variant, crossfade=xf,
                                         qa_dir=run_dir / f"qa_{tag}", master=stage == "final")
                    print(f"--- {tag}", flush=True)
                    print(res["qa_summary"], flush=True)
                    extra[tag] = {k: v for k, v in res.items() if k != "qa"}
    finally:
        inst.stop()
        _write_summary(run_dir, runner, extra)


_LINK_FILES = ("_draft.mkv", "_draft.json", "_draft.audio.safetensors", "_draft.wav", "_draft_tail.safetensors",
               "_s1_video.safetensors", "_s1_kf.safetensors", "_s1_audio.safetensors")


def seed_link1(chain: ce.LtxChain, src_chain: str) -> bool:
    """Reuse another chain's link-1 draft when its graph is identical (link 1 has no
    carry, so the carry settings do not touch it): copy the files and record the
    step under this chain's digest. Returns False (nothing copied) on a mismatch."""
    src_dir = COMFY_OUT / "studio_chain" / src_chain
    src_man = json.loads((src_dir / "manifest.json").read_text(encoding="utf-8"))
    graph = chain.draft_graph(0)
    digest = ce.graph_digest(graph)
    # the graph names its output files under the chain id; compare it as the source built it
    as_src = json.loads(json.dumps(graph).replace(f'"{chain.base}/', f'"studio_chain/{src_chain}/'))
    rec = (src_man.get("steps") or {}).get("draft_l1") or {}
    if not rec.get("ok") or not ce.digest_matches(rec, as_src):
        say(f"seed: {src_chain} link 1 was drafted from a different graph; rendering it")
        return False
    dst = COMFY_OUT / chain.base
    dst.mkdir(parents=True, exist_ok=True)
    for suf in _LINK_FILES:
        if (src_dir / f"link1{suf}").is_file():
            shutil.copyfile(src_dir / f"link1{suf}", dst / f"link1{suf}")
    chain.manifest.record("draft_l1", **{k: v for k, v in rec.items() if k not in ("seeded_from", "digest", "digest_v")},
                          digest=digest, seeded_from=src_chain)
    say(f"seed: link 1 draft copied from {src_chain} (same graph digest)")
    return True


def _draft_media(chain_dir: Path, links: list[ce.Link], carry: int) -> list:
    import chain_assemble as ca
    return [ca.LinkMedia(video=chain_dir / f"link{i + 1}_draft.mkv",
                         audio=chain_dir / f"link{i + 1}_draft.audio.safetensors",
                         frames=lk.frames, carry=carry if i else 0) for i, lk in enumerate(links)]


def cmd_ltx_seam_ab(args) -> None:
    """Same-seed A/B of the carry at draft level: link 1 is taken from the base chain
    (an identical render), only links 2..N are re-drafted per variant; every variant
    (and the base chain as tag 'base') is assembled at each crossfade and seam-QA'd."""
    import chain_assemble as ca
    import dataclasses
    run_dir = new_run("ltx_seam_ab")
    base_spec = ltx_gate_spec(args.base, smoke=False)
    n = int(args.links)
    links = base_spec.links[:n]
    variants = dict(v.split("=", 1) for v in args.variant)
    base_dir = COMFY_OUT / "studio_chain" / args.base
    inst = Instance("ltx", run_dir, args.comfy_arg)
    runner = LabRunner(inst, run_dir, stall_s=args.stall, hard_stall_s=args.hard_stall)
    chains: dict[str, ce.LtxChain] = {}
    table: dict = {}
    try:
        inst.start()
        for tag, value in variants.items():
            # tag=strengths[|lc=1][|cr=<factor>][|sa=<factor>][|ramp=<latents>]
            parts = value.split("|")
            opts = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            spec = dataclasses.replace(base_spec, chain_id=f"{args.base}_ab_{tag}", links=list(links),
                                       pin_strengths=parts[0].strip(), carry_restore=float(opts.get("cr", 0.0)),
                                       latent_carry=opts.get("lc", "0") not in ("0", "", "false"),
                                       seam_adain=float(opts.get("sa", 0.0)), seam_ramp=int(opts.get("ramp", 6)))
            ch = ce.LtxChain(spec, runner, COMFY_OUT)
            dst = COMFY_OUT / ch.base
            dst.mkdir(parents=True, exist_ok=True)
            for suf in _LINK_FILES:
                src = base_dir / f"link1{suf}"
                if src.is_file():
                    shutil.copyfile(src, dst / f"link1{suf}")
            ch.manifest.record("draft_l1", ok=True, digest=ce.graph_digest(ch.draft_graph(0)), seeded_from=args.base)
            bad = {i: validate(ch.draft_graph(i), inst.object_info) for i in range(1, n)}
            if any(bad.values()):
                raise SystemExit(f"variant {tag}: graph failed validation: {bad}")
            chains[tag] = ch
        say(f"A/B plan validated: {list(variants)} x links 2..{n}")
        for tag, ch in chains.items():
            for i in range(1, n):
                info = ch.draft(i, force=args.force)
                say(f"{tag} draft {i + 1}: {'reused' if info.get('skipped') else 'rendered'} {info.get('seconds')} s")
        inst.stop()
        ff = ffmpeg_exe()
        media = {"base": _draft_media(base_dir, links, base_spec.carry_frames)}
        media.update({tag: _draft_media(COMFY_OUT / ch.base, links, base_spec.carry_frames)
                      for tag, ch in chains.items()})
        for tag, lm in media.items():
            for xf in args.crossfade:
                mp4 = run_dir / f"ab_{tag}_x{xf}.mp4"
                res = ca.assemble_ltx(lm, mp4, ff, crossfade=xf, crf=12)
                rep = seam_qa.analyze_take(mp4, res["seams"], out_dir=run_dir / f"qa_{tag}_x{xf}",
                                           overlaps=[{**seam_qa.overlap_fidelity(lm[i - 1].video, lm[i].video,
                                                                                lm[i].carry), "link": i + 1}
                                                     for i in range(1, len(lm))])
                print(f"--- {tag} (pin {variants.get(tag, 'hard 1.0') or 'hard 1.0'}), crossfade {xf}", flush=True)
                print(seam_qa.summarize(rep), flush=True)
                table[f"{tag}_x{xf}"] = rep.to_json()
    finally:
        inst.stop()
        _write_summary(run_dir, runner, {"ab": table, "variants": variants})


def cmd_ltx_restore_check(args) -> None:
    """Decode-only check of the carry restore on already-sampled A/B variants: the
    restore runs after the sampler, so applying it to a variant's stored stage-1
    latent (reference: the base chain's hard-pinned link, whose carried latents are
    the encoded carry bit for bit) and decoding with the draft's decode settings
    gives exactly what the in-graph node would. Assembles and seam-QAs each restored
    variant next to its unrestored take."""
    import chain_assemble as ca
    run_dir = new_run("ltx_restore_check")
    base_spec = ltx_gate_spec(args.base, smoke=False)
    links = base_spec.links[:2]
    carry_lat = (base_spec.carry_frames - 1) // 8 + 1
    m = cg.LtxModels()
    base_rel = f"studio_chain/{args.base}"
    inst = Instance("ltx", run_dir, args.comfy_arg)
    runner = LabRunner(inst, run_dir, stall_s=args.stall, hard_stall_s=args.hard_stall)
    table: dict = {}
    try:
        graphs = {}
        for tag in args.variants.split(","):
            rel = f"studio_chain/{args.base}_ab_{tag}"
            g = cg._G()
            vae = g.add("VAELoader", vae_name=m.vae)
            lat = g.add("StudioLoadLatent", relpath=f"{rel}/link2_s1_video")
            refl = g.add("StudioLoadLatent", relpath=f"{base_rel}/link2_s1_video")
            rest = g.add("StudioCarryRestore", latent=cg.ref(lat), reference=cg.ref(refl),
                         carry_latents=carry_lat, factor=float(args.factor))
            g.add("StudioSaveLatent", latent=cg.ref(rest, 0), relpath=f"{rel}/link2_s1_video_cr",
                  save_noise_mask=False)
            frames = g.add("VAEDecodeTiled", samples=cg.ref(rest, 0), vae=cg.ref(vae), tile_size=512, overlap=64,
                           temporal_size=64, temporal_overlap=16)
            g.add("StudioSaveVideoLossless", images=cg.ref(frames), fps=float(base_spec.fps),
                  relpath=f"{rel}/link2_draft_cr")
            graphs[tag] = g.nodes
        inst.start()
        bad = {t: e for t, e in ((t, validate(g, inst.object_info)) for t, g in graphs.items()) if e}
        if bad:
            raise SystemExit(f"graphs failed validation: {bad}")
        for tag, g in graphs.items():
            runner.run(f"restore_{tag}", g)          # raises JobFailed unless it succeeded
        inst.stop()
        ff = ffmpeg_exe()
        for tag in graphs:
            vdir = COMFY_OUT / "studio_chain" / f"{args.base}_ab_{tag}"
            for label, video in ((tag, vdir / "link2_draft.mkv"), (f"{tag}cr", vdir / "link2_draft_cr.mkv")):
                lm = _draft_media(vdir, links, base_spec.carry_frames)
                lm[1] = ca.LinkMedia(video=video, audio=lm[1].audio, frames=lm[1].frames, carry=lm[1].carry)
                for xf in args.crossfade:
                    mp4 = run_dir / f"rc_{label}_x{xf}.mp4"
                    res = ca.assemble_ltx(lm, mp4, ff, crossfade=xf, crf=12)
                    rep = seam_qa.analyze_take(mp4, res["seams"], out_dir=run_dir / f"qa_{label}_x{xf}", span=xf,
                                               overlaps=[{**seam_qa.overlap_fidelity(lm[0].video, lm[1].video,
                                                                                    lm[1].carry), "link": 2}])
                    print(f"--- {label}, crossfade {xf}", flush=True)
                    print(seam_qa.summarize(rep), flush=True)
                    table[f"{label}_x{xf}"] = rep.to_json()
    finally:
        inst.stop()
        _write_summary(run_dir, runner, {"restore_check": table})


def cmd_ltx_take_check(args) -> None:
    """Decode-once takes of already rendered chains (StudioLtxDecodeTake on the
    links' stage-1 latents, or with --stage final on a finish variant's stage-2
    latents), encoded and seam-QA'd at the junction and at the carry end. A chain
    whose files sit elsewhere is given as tag=<chain dir>; a draft's link 1 may come
    from the base chain."""
    run_dir = new_run(f"ltx_take_check_{args.stage}")
    base_spec = ltx_gate_spec(args.base, smoke=False)
    links = base_spec.links[:int(args.links)]
    carry = base_spec.carry_frames
    final = args.stage == "final"
    inst = Instance("ltx", run_dir, args.comfy_arg)
    runner = LabRunner(inst, run_dir, stall_s=args.stall, hard_stall_s=args.hard_stall)
    table: dict = {}
    try:
        graphs = {}
        for item in args.chains:
            tag, _, cid = item.partition("=")
            rels = []
            for i in range(len(links)):
                if final:
                    rel = f"studio_chain/{cid}/{args.variant}/link{i + 1}_s2_video"
                else:
                    rel = f"studio_chain/{cid}/link{i + 1}_s1_video"
                    if not (COMFY_OUT / f"{rel}.safetensors").is_file() and i == 0:
                        rel = f"studio_chain/{args.base}/link1_s1_video"
                if not (COMFY_OUT / f"{rel}.safetensors").is_file():
                    raise SystemExit(f"{tag}: missing latent {rel}")
                rels.append(rel)
            out_dir = f"studio_chain/{cid}/{args.variant}" if final else f"studio_chain/{cid}"
            for join in args.join:
                for blend in args.blend:
                    if blend > join - 1:
                        continue
                    name = f"take_{args.stage}_j{join}" + (f"b{blend}" if blend else "")
                    graphs[f"{tag}_j{join}" + (f"b{blend}" if blend else "")] = (
                        cg.ltx_decode_take_graph(rels, f"{out_dir}/{name}", carry_frames=carry,
                                                 join=join, blend=blend),
                        COMFY_OUT / Path(out_dir) / f"{name}.mkv", join)
        inst.start()
        bad = {t: e for t, e in ((t, validate(g, inst.object_info)) for t, (g, _, _) in graphs.items()) if e}
        if bad:
            raise SystemExit(f"graphs failed validation: {bad}")
        for tag, (g, _, _) in graphs.items():
            runner.run(f"take_{tag}", g)
        inst.stop()
        import chain_assemble as ca
        ff = ffmpeg_exe()
        tl = ca.plan_timeline([ca.LinkMedia(Path("-"), None, lk.frames, carry if i else 0)
                               for i, lk in enumerate(links)], 0)
        for tag, (_, mkv, join) in graphs.items():
            side = json.loads(mkv.with_suffix(".json").read_text(encoding="utf-8"))
            junctions = [j["take_frame"] for j in side["take"]["joins"]]
            mp4 = run_dir / f"take_{tag}.mp4"
            ca.encode_take(mkv, mp4, ff, crf=12)
            rep = seam_qa.analyze_take(mp4, sorted(set(junctions + tl["seams"])), out_dir=run_dir / f"qa_{tag}")
            print(f"--- {tag}: junctions {junctions} carry ends {tl['seams']} "
                  f"latent steps {[(j['take_frame'], j['step_ratio']) for j in side['take']['joins']]}", flush=True)
            print(seam_qa.summarize(rep), flush=True)
            table[tag] = {"take": side["take"], "qa": rep.to_json()}
    finally:
        inst.stop()
        _write_summary(run_dir, runner, {"take_check": table})


def cmd_h3_seam_check(args) -> None:
    """The H3 seam look match on an already rendered chain, without re-sampling: links
    2..N's saved AV latents re-matched across their carry end (StudioSeamAdain path,
    phase period 5) per ramp, then the take decoded once and seam-QA'd next to the
    unmatched decode. Only the last link's own carry end is exact: an earlier link's
    tail went into the next link's carry unmatched (2-link chains are exact)."""
    run_dir = new_run("h3_seam_check")
    chain_dir = COMFY_OUT / "studio_chain" / args.chain
    man = json.loads((chain_dir / "manifest.json").read_text(encoding="utf-8"))
    spec_d = dict(man["spec"])
    spec_d.pop("kind", None)
    spec_d["links"] = [ce.Link(**lk) for lk in spec_d["links"]]
    spec = ce.H3ChainSpec(**spec_d)
    n = len(spec.links)
    base = f"studio_chain/{args.chain}"
    inst = Instance("h3", run_dir, args.comfy_arg)
    runner = LabRunner(inst, run_dir, stall_s=args.stall, hard_stall_s=args.hard_stall)
    variants = {"none": [f"{base}/link{i + 1}_av" for i in range(n)]}
    graphs: list[tuple[str, dict]] = []
    for ramp in args.ramp:
        tag = f"r{ramp}f{args.factor:g}"
        rels = [f"{base}/link1_av"]
        for i in range(1, n):
            g = cg._G()
            src = g.add("StudioLoadLatent", relpath=f"{base}/link{i + 1}_av")
            m = cg.h3_seam_adain(g, cg.ref(src), spec.carry_frames, ramp, args.factor)
            out = f"{base}/seam_{tag}/link{i + 1}_av"
            g.add("StudioSaveLatent", latent=m, relpath=out, save_noise_mask=False)
            graphs.append((f"match_{tag}_l{i + 1}", g.nodes))
            rels.append(out)
        variants[tag] = rels
    takes = {tag: (cg.h3_decode_take_graph(rels, f"{base}/seam_{tag}/check", carry_frames=spec.carry_frames),
                   COMFY_OUT / f"{base}/seam_{tag}/check_take_final.mkv") for tag, rels in variants.items()}
    table: dict = {}
    try:
        inst.start()
        bad = {t: e for t, e in ((t, validate(g, inst.object_info))
                                 for t, g in graphs + [(k, v[0]) for k, v in takes.items()]) if e}
        if bad:
            raise SystemExit(f"graphs failed validation: {bad}")
        for tag, g in graphs:
            runner.run(tag, g)
        for tag, (g, _) in takes.items():
            runner.run(f"take_{tag}", g)
        inst.stop()
        import chain_assemble as ca
        ff = ffmpeg_exe()
        for tag, (_, mkv) in takes.items():
            mp4 = run_dir / f"take_{tag}.mp4"
            ca.encode_take(mkv, mp4, ff, crf=12)
            rep = seam_qa.analyze_take(mp4, spec.seam_frames(), out_dir=run_dir / f"qa_{tag}")
            print(f"--- {tag}", flush=True)
            print(seam_qa.summarize(rep), flush=True)
            table[tag] = rep.to_json()
    finally:
        inst.stop()
        _write_summary(run_dir, runner, {"seam_check": table})


def h3_gate_spec(chain_id: str, smoke: bool, window: int, carry: int) -> ce.H3ChainSpec:
    base = baseline_links("chain_5c9b1e8b")
    refs = base[0]["refs"]
    anchor = base[0].get("anchor")
    for name in refs + ([anchor] if anchor else []):
        if not (COMFY_IN / name).is_file():
            raise SystemExit(f"reference {name} is missing from {COMFY_IN}")
    # smoke: two 90-frame windows (17*5+5, divisible by 3), 51 new frames after a 39-frame carry
    rows = base[:2] if smoke else base
    links = [ce.Link(prompt=b["prompt"], seed=int(b["seed"]), frames=90 if smoke else window) for b in rows]
    # per-link decodes only in the smoke (decode-once vs per-link comparison); the gate
    # decodes the joined take once instead
    return ce.H3ChainSpec(chain_id=chain_id, links=links, refs=refs, start_image=anchor, carry_frames=carry,
                          link={"decode_draft": smoke})


def cmd_h3(args, smoke: bool) -> None:
    models: cg.H3Models | None = None
    if args.request:
        run_dir = new_run(f"h3_req_{Path(args.request).stem}_{args.variant}")
        spec, models = h3_request_spec(Path(args.request))
        chain_id = spec.chain_id
    else:
        run_dir = new_run("h3_smoke" if smoke else f"h3_gate_{args.variant}")
        chain_id = args.chain or ("lab_h3_smoke_" + time.strftime("%m%d_%H%M") if smoke else "lab_h3_gate_5c9b1e8b")
        spec = h3_gate_spec(chain_id, smoke, args.window, args.carry)
    if args.link_set:
        spec.link.update(_parse_set(args.link_set))
    refine_set = _parse_set(args.set)
    if smoke:
        refine_set.setdefault("chunk_frames", 73)
        refine_set.setdefault("overlap_frames", 22)
    (run_dir / "spec.json").write_text(json.dumps({"chain": chain_id, "links": [vars(lk) for lk in spec.links],
                                                   "refs": spec.refs, "start_image": spec.start_image,
                                                   "link": spec.link, "refine": {**spec.refine, **refine_set},
                                                   "variant": args.variant, "seams": spec.seam_frames(),
                                                   "request": args.request,
                                                   "models": dataclasses.asdict(models) if models else None},
                                                  indent=1), encoding="utf-8")
    inst = Instance("h3", run_dir, args.comfy_arg)
    runner = LabRunner(inst, run_dir, stall_s=args.stall, hard_stall_s=args.hard_stall)
    chain = ce.H3Chain(spec, runner, COMFY_OUT, models=models)
    steps = set(args.steps.split(","))
    extra: dict = {"chain": chain_id, "seams": spec.seam_frames(), "take_frames": spec.take_frames()}
    try:
        if steps & {"link", "decode", "refine"}:
            inst.start()
            plan = []
            if "link" in steps:
                plan += [(f"link_l{i + 1}", chain.link_graph(i)) for i in range(len(spec.links))]
            if "decode" in steps:
                plan.append(("decode", cg.h3_decode_take_graph([f"{chain.link_rel(i)}_av"
                                                                for i in range(len(spec.links))], "x")))
            if "refine" in steps:
                plan.append(("refcond", chain.refine_cond_graph(args.variant, refine_set)))
                plan.append(("refine", chain.refine_graph(args.variant, refine_set)))
            bad = {n: e for n, e in ((n, validate(g, inst.object_info)) for n, g in plan) if e}
            if bad:
                (run_dir / "validation.json").write_text(json.dumps(bad, indent=1), encoding="utf-8")
                raise SystemExit("plan failed validation: " + json.dumps(bad, indent=1)[:4000])
            say(f"plan validated: {len(plan)} graphs")
        if "link" in steps:
            for i in range(len(spec.links)):
                info = chain.link(i, force=args.force)
                say(f"link {i + 1}: {'reused' if info.get('skipped') else 'rendered'} {info.get('seconds')} s")
        ff = ffmpeg_exe()
        if "link" in steps and spec.link.get("decode_draft", True):
            # per-link decodes joined with a hard cut: shows what decoding the links
            # separately costs at the seams (compare with the decode-once take)
            import chain_assemble as ca
            mp4 = run_dir / f"{chain_id}_base_perlink.mp4"
            res = ca.assemble_ltx(chain.draft_take_links(), mp4, ff, crossfade=0)
            rep = seam_qa.analyze_take(mp4, res["seams"], audio=mp4.with_suffix(".wav") if res["audio"] else None,
                                       out_dir=run_dir / "qa_base_perlink")
            print(seam_qa.summarize(rep), flush=True)
            extra["base_perlink"] = {k: v for k, v in res.items() if k != "qa"}
        if "decode" in steps:
            info = chain.decode_take("B0", force=args.force)
            say(f"decode take: {'reused' if info.get('skipped') else 'decoded'} {info.get('seconds')} s")
            mp4 = run_dir / f"{chain_id}_B0.mp4"
            res = chain.finish_take("B0", mp4, ff, qa_dir=run_dir / "qa_B0")
            print(res["qa_summary"], flush=True)
            extra["base_take"] = {k: v for k, v in res.items() if k != "qa"}
        if "refine" in steps:
            info = chain.refine(args.variant, force=args.force, **refine_set)
            say(f"refine: {'reused' if info.get('skipped') else 'rendered'} {info.get('seconds')} s")
        inst.stop()
        if "take" in steps:
            mp4 = run_dir / f"{chain_id}_{args.variant}.mp4"
            res = chain.finish_take(args.variant, mp4, ff, qa_dir=run_dir / f"qa_{args.variant}")
            print(res["qa_summary"], flush=True)
            extra["take"] = {k: v for k, v in res.items() if k != "qa"}
    finally:
        inst.stop()
        _write_summary(run_dir, runner, extra)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("prefix")
    for name in ("ltx-smoke", "ltx-gate", "h3-smoke", "h3-gate"):
        p = sub.add_parser(name)
        p.add_argument("--chain", default=None, help="chain id (reuse renders across invocations)")
        p.add_argument("--variant", default="F0" if name.startswith("ltx") else "R0")
        p.add_argument("--set", action="append", default=[], help="finish/refine field override k=v")
        p.add_argument("--force", action="store_true")
        p.add_argument("--stall", type=float, default=600)
        p.add_argument("--hard-stall", type=float, default=900)
        p.add_argument("--comfy-arg", action="append", default=[], help="extra ComfyUI flag")
        p.add_argument("--request", default=None,
                       help="Video Studio chain request JSON (see _load_request) instead of the gate chain")
        if name.startswith("ltx"):
            p.add_argument("--steps", default="draft,audio,take,finish,assemble")
            p.add_argument("--finish-links", type=int, default=0, help="finish only the first N links (A/B)")
            p.add_argument("--draft-links", type=int, default=0, help="draft only the first N links (A/B)")
            p.add_argument("--cast-line", default="", help="sentence inserted after each prompt's first one")
            p.add_argument("--finish-cast-line", default="", help="the same, in the stage-2 prompts only")
            p.add_argument("--ref", action="append", default=[], help="extra reference slot=input image")
            p.add_argument("--face-refs", action="store_true", help="the studio's face close-ups in free slots")
            p.add_argument("--seed-from", default=None, help="chain id whose identical link-1 draft to reuse")
            p.add_argument("--seed-shift", type=int, default=0, help="added to every link seed (seed spread)")
            p.add_argument("--draft-set", action="append", default=[])
            p.add_argument("--crossfade", type=int, nargs="+", default=[8])
        else:
            p.add_argument("--steps", default="link,decode,refine,take")
            p.add_argument("--link-set", action="append", default=[])
            p.add_argument("--window", type=int, default=192)
            p.add_argument("--carry", type=int, default=cg.H3_CARRY_FRAMES)
    ab = sub.add_parser("ltx-seam-ab")
    ab.add_argument("--base", default="lab_ltx_gate_bcb2e4a7")
    ab.add_argument("--links", type=int, default=2)
    ab.add_argument("--variant", action="append", required=True, help="tag=strengths ('' = hard pin)")
    ab.add_argument("--crossfade", type=int, nargs="+", default=[8, 24])
    ab.add_argument("--force", action="store_true")
    ab.add_argument("--stall", type=float, default=600)
    ab.add_argument("--hard-stall", type=float, default=900)
    ab.add_argument("--comfy-arg", action="append", default=[])
    rc = sub.add_parser("ltx-restore-check")
    rc.add_argument("--base", default="lab_ltx_gate_bcb2e4a7")
    rc.add_argument("--variants", default="R1,L5", help="comma list of ltx-seam-ab variant tags")
    rc.add_argument("--factor", type=float, default=1.0)
    rc.add_argument("--crossfade", type=int, nargs="+", default=[8, 24])
    rc.add_argument("--stall", type=float, default=600)
    rc.add_argument("--hard-stall", type=float, default=900)
    rc.add_argument("--comfy-arg", action="append", default=[])
    tc = sub.add_parser("ltx-take-check")
    tc.add_argument("--base", default="lab_ltx_gate_bcb2e4a7")
    tc.add_argument("--links", type=int, default=2)
    tc.add_argument("--chains", nargs="+", required=True, help="tag=<chain id under studio_chain/>")
    tc.add_argument("--join", type=int, nargs="+", default=[cg.LTX_TAKE_JOIN])
    tc.add_argument("--blend", type=int, nargs="+", default=[cg.LTX_TAKE_BLEND])
    tc.add_argument("--stage", choices=("draft", "final"), default="draft")
    tc.add_argument("--variant", default="F0", help="finish variant (--stage final)")
    tc.add_argument("--stall", type=float, default=600)
    tc.add_argument("--hard-stall", type=float, default=900)
    tc.add_argument("--comfy-arg", action="append", default=[])
    hs = sub.add_parser("h3-seam-check")
    hs.add_argument("--chain", required=True, help="rendered H3 chain id under studio_chain/")
    hs.add_argument("--ramp", type=int, nargs="+", default=[10])
    hs.add_argument("--factor", type=float, default=1.0)
    hs.add_argument("--stall", type=float, default=600)
    hs.add_argument("--hard-stall", type=float, default=900)
    hs.add_argument("--comfy-arg", action="append", default=[])
    q = sub.add_parser("qa")
    q.add_argument("video")
    q.add_argument("--seams", required=True)
    q.add_argument("--audio", default=None)
    q.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.cmd == "baseline":
        print(json.dumps(baseline_links(args.prefix), indent=1))
    elif args.cmd in ("ltx-smoke", "ltx-gate"):
        cmd_ltx(args, smoke=args.cmd == "ltx-smoke")
    elif args.cmd in ("h3-smoke", "h3-gate"):
        cmd_h3(args, smoke=args.cmd == "h3-smoke")
    elif args.cmd == "ltx-seam-ab":
        cmd_ltx_seam_ab(args)
    elif args.cmd == "ltx-restore-check":
        cmd_ltx_restore_check(args)
    elif args.cmd == "ltx-take-check":
        cmd_ltx_take_check(args)
    elif args.cmd == "h3-seam-check":
        cmd_h3_seam_check(args)
    else:
        seams = [int(x) for x in args.seams.split(",") if x.strip()]
        rep = seam_qa.analyze_take(args.video, seams, audio=args.audio, out_dir=Path(args.out) if args.out else None)
        print(seam_qa.summarize(rep))


if __name__ == "__main__":
    main()
