"""Kinoloom - single-window launcher.

One console window shows component logs in a unified stream.
Closing the window or pressing Ctrl+C initiates shutdown across four complementary
layers:
  1. children are spawned with the BASE python (no venv shim chain)
  2. children are assigned to a Windows Job Object (KILL_ON_JOB_CLOSE)
     including their descendant process tree
  3. a console-control handler triggers graceful,
     pattern-based shutdown on Ctrl+C / window close
  4. shutdown() cleans up child processes matching known command-line patterns

Usage:  start.bat   (or: python tool/launcher.py)
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_PY = (ROOT / "tools" / "python" / "python.exe") if (ROOT / "tools" / "python" / "python.exe").exists() else Path(sys.executable)
VENV_SITE = ROOT / "tool" / ".venv" / "Lib" / "site-packages"
# start.bat runs this on the base Python, which a new install keeps bare: read
# settings.yaml with the studio venv's PyYAML, the packages the children get
# through PYTHONPATH (_base_env). Appended, so the base's own packages win.
if VENV_SITE.is_dir() and str(VENV_SITE) not in sys.path:
    sys.path.append(str(VENV_SITE))
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

COMFY_PORT = 8188
UI_PORT = 8080
# LTX-2.5 runs on its OWN ComfyUI instance: it needs ComfyUI >= v0.32 plus the
# LTX node pack, which the production v0.30 farm cannot provide. Its weighted
# runtime lives beside the farm and is launched from its own venv.
LTX_PORT = 8189
# No fallback to BASE_PY: the studio python lacks the LTX/Qwen node-pack
# dependencies, so a missing venv must skip the component with a clear
# message (launch()) instead of crash-looping it three times.
LTX_PY = ROOT / "ComfyUI_LTX" / "venv" / "Scripts" / "python.exe"
# MiniMax H3 (hero shots) runs on a THIRD instance of the ComfyUI_LTX tree:
# same v0.36 code and node pack, but WITHOUT --bf16-vae - the H3 video VAE is
# fp16 and the proven 1080p two-phase config was measured without the bf16 VAE
# override (the H3 tiers' notes in tool/video_models_user.json and
# docs/research/HARDWARE_BENCHMARKS_AND_LIMITS.md). Weights are mounted zero-copy from the
# settings paths.extra_model_dirs roots (see _extra_paths_args).
H3_PORT = 8199
# Qwen-Image-2.1 runs on a FOURTH instance: it needs ComfyUI >= v0.37 (the
# production v0.30 farm cannot load the 2.1 VAE/text encoder), its own venv and
# the Qwen21 node pack. Proven configs + evidence: docs/research/PROVEN_CONFIGS_QWEN21.md.
QWEN_PORT = 8190
QWEN_PY = ROOT / "ComfyUI_Qwen21" / "venv" / "Scripts" / "python.exe"
COMFY_PORTS = (COMFY_PORT, LTX_PORT, H3_PORT, QWEN_PORT)
RESTART_CAP = 3          # consecutive fast crashes before giving up
RESTART_WINDOW_S = 45    # a crash is "fast" if it dies within this window
READY_TIMEOUT_S = 300    # how long to wait for the web app before saying so
WEIGHT_SUFFIXES = (".safetensors", ".gguf", ".pth", ".ckpt")

# Generated from settings paths.extra_model_dirs and passed to EVERY ComfyUI
# instance, so the farms see exactly the model roots the studio's
# availability checks (video_models) see.
EXTRA_PATHS_YAML = ROOT / "temp" / "extra_model_paths.studio.yaml"
_MODEL_SUBDIRS = ("checkpoints", "diffusion_models", "text_encoders", "vae",
                  "loras", "upscale_models", "latent_upscale_models",
                  "clip_vision")


def _settings() -> dict:
    cfg_file = ROOT / "tool" / "settings.yaml"
    if not cfg_file.exists():
        cfg_file = ROOT / "tool" / "settings.example.yaml"
    try:
        import yaml
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extra_model_dirs(cfg: dict | None = None) -> list[str]:
    """Existing paths.extra_model_dirs roots (relative = studio root)."""
    raw = ((cfg if cfg is not None else _settings()).get("paths") or {}) \
        .get("extra_model_dirs") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for d in raw:
        if not isinstance(d, str) or not d.strip():
            continue
        p = Path(d.strip())
        p = p if p.is_absolute() else ROOT / p
        if p.is_dir() and p.as_posix() not in out:
            out.append(p.as_posix())
    return out


def is_studio_cmd(cmd: str, ui_port: int) -> bool:
    """Is this command line one of OUR components? A ComfyUI on one of the
    studio's ports, or the control room on the studio's UI port. (Matching
    any "uvicorn ... api:app" killed unrelated FastAPI apps on shutdown.)"""
    cmd = cmd or ""
    if any(f"main.py --port {p}" in cmd for p in COMFY_PORTS):
        return True
    return ("uvicorn" in cmd and "api:app" in cmd
            and f"--port {ui_port}" in cmd)


# ---------------------------------------------------------------- console
def enable_vt() -> None:
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if k.GetConsoleMode(h, ctypes.byref(mode)):
        k.SetConsoleMode(h, mode.value | 0x0004)


C = {"reset": "\x1b[0m", "dim": "\x1b[2m", "bold": "\x1b[1m",
     "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
     "blue": "\x1b[34m", "cyan": "\x1b[36m", "magenta": "\x1b[35m",
     "white": "\x1b[37m"}


def say(tag: str, color: str, line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{C['dim']}{ts}{C['reset']} {C.get(color, '')}[{tag}]{C['reset']} {line}")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def browser_url(host: str, port: int) -> str:
    """The web app's address on this PC. A wildcard bind answers on loopback,
    where the studio also skips the API token (security.local_bypass)."""
    h = (host or "").strip()
    if h in ("", "0.0.0.0", "::", "[::]"):
        h = "127.0.0.1"
    elif ":" in h and not h.startswith("["):
        h = f"[{h}]"                        # an IPv6 address in a URL
    return f"http://{h}:{port}/"


def set_title(text: str) -> None:
    """The console window's title stays in view however far the logs scroll."""
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except (AttributeError, OSError):
        pass


def web_app_answers(url: str, timeout: float = 3.0) -> bool:
    """Does the control room's unauthenticated /api/health answer?"""
    # no proxy: a system-wide proxy must not stand between us and 127.0.0.1
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/api/health", timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError, http.client.HTTPException):
        return False


def has_weights(dirs: list[Path]) -> bool:
    """Is there a model weight file under any of these folders?"""
    for d in dirs:
        try:
            for p in d.rglob("*"):
                if p.suffix.lower() in WEIGHT_SUFFIXES and p.is_file():
                    return True
        except OSError:
            continue
    return False


# ------------------------------------------------------------ job object
class JobKiller:
    """Job object: when the launcher exits (any way), everything dies."""

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        self.k32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        # explicit prototypes: the default c_int restype truncates 64-bit
        # HANDLEs, and AssignProcessToJobObject takes a process HANDLE
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD)
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        self.job = k.CreateJobObjectW(None, None)
        if not self.job:
            self.job = None
            return

        class LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", LIMIT), ("Io", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.k32.SetInformationJobObject(self.job, 9, ctypes.byref(info),
                                         ctypes.sizeof(info))

    def _assign_handle(self, handle: int) -> bool:
        try:
            return bool(self.k32.AssignProcessToJobObject(self.job, handle))
        except Exception:
            return False

    def _assign_pid(self, pid: int) -> None:
        h = self.k32.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE,
                                 False, int(pid))
        if not h:
            return
        try:
            self._assign_handle(h)   # already in the job (inherited) = no-op fail
        finally:
            self.k32.CloseHandle(h)

    def add(self, proc: subprocess.Popen) -> None:
        if self.job:
            self._assign_handle(int(proc._handle))  # type: ignore[attr-defined]

    def add_tree(self, root_pid: int) -> None:
        """Assign a process and ALL descendants to the job (best effort)."""
        if not self.job:
            return
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Select-Object ProcessId,ParentProcessId | "
                 "ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=30)
            procs = {p["ProcessId"]: p["ParentProcessId"]
                     for p in json.loads(out.stdout)}
            found = {root_pid}
            changed = True
            while changed:
                changed = False
                for pid, parent in procs.items():
                    if parent in found and pid not in found:
                        found.add(pid)
                        changed = True
            for pid in found:
                self._assign_pid(pid)
        except Exception:
            pass

    def close(self) -> None:
        if self.job:
            self.k32.CloseHandle(self.job)
            self.job = None


# -------------------------------------------------------------- launcher
class Launcher:
    def __init__(self, host: str | None = None, port: int | None = None,
                 open_browser: bool | None = None) -> None:
        self.job = JobKiller()
        self.procs: dict[str, subprocess.Popen] = {}
        self.stop = threading.Event()
        self._born: dict[str, float] = {}
        self._fast_crashes: dict[str, int] = {}
        self._gave_up: set[str] = set()
        self._shutting_down = False
        self.host = host
        self.port = port
        self.open_browser = open_browser    # None = ui.open_browser
        self.not_installed: list[str] = []  # components launch() skipped

    @staticmethod
    def _load_ui_settings() -> tuple[str, int]:
        host = "127.0.0.1"
        port = UI_PORT
        ui_cfg = _settings().get("ui") or {}
        try:
            if ui_cfg.get("host") is not None:
                host = str(ui_cfg["host"])
            if ui_cfg.get("port") is not None:
                port = int(ui_cfg["port"])
        except (TypeError, ValueError):
            pass
        return host, port

    @staticmethod
    def _open_browser_setting() -> bool:
        value = (_settings().get("ui") or {}).get("open_browser", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("false", "no", "off", "0")

    def _resolve_host_port(self, host: str | None = None, port: int | None = None) -> tuple[str, int]:
        cfg_host, cfg_port = self._load_ui_settings()
        final_host = host if host is not None else (self.host if self.host is not None else cfg_host)
        final_port = port if port is not None else (self.port if self.port is not None else cfg_port)
        self.host = final_host
        self.port = final_port
        return final_host, final_port

    def _base_env(self, extra: dict | None = None, own_env: bool = False) -> dict:
        # NOTE: **os.environ MUST come first. It used to be expanded last, which
        # silently clobbered the PYTHONPATH below whenever the parent shell had
        # PYTHONPATH set — the children then never saw the venv's site-packages.
        e = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PATH": str(ROOT / "tools" / "git" / "cmd")
                    + os.pathsep + os.environ.get("PATH", ""),
        }
        if own_env:
            # Components with their own dependency stack (the LTX farm venv)
            # must NOT see the studio venv's site-packages: PYTHONPATH wins over
            # the venv's own site-packages, which made the LTX farm import the
            # studio's older comfy-kitchen and crash on a missing symbol.
            e.pop("PYTHONPATH", None)
        else:
            e["PYTHONPATH"] = str(VENV_SITE)

        # ComfyUI_LTX/extra_model_paths.yaml mounts ${H3_LAB_MODELS} and
        # ${LTX25_LAB_MODELS}; fill them from the configured extra model roots
        # when the shell did not. (Every root is ALSO mounted on every farm
        # through the generated config — _extra_paths_args — so a root whose
        # name matches neither still reaches ComfyUI.)
        h3_path = os.environ.get("H3_LAB_MODELS")
        ltx_path = os.environ.get("LTX25_LAB_MODELS")
        for d in extra_model_dirs():
            low = d.lower()
            if not h3_path and "h3" in low:
                h3_path = d
            if not ltx_path and "ltx" in low:
                ltx_path = d
        if h3_path:
            e["H3_LAB_MODELS"] = str(h3_path)
        if ltx_path:
            e["LTX25_LAB_MODELS"] = str(ltx_path)

        if extra:
            e.update(extra)
        return e

    def spawn(self, name: str, args: list[str], logfile: str,
              env: dict | None = None, cwd: Path | None = None,
              python: Path | None = None) -> None:
        path = LOG_DIR / logfile
        if path.exists():
            try:
                path.replace(LOG_DIR / f"{path.stem}.old{path.suffix}")
            except Exception:
                pass
        # direct child of the launcher: base python, no venv shim in between.
        # Components with their own dependency stack (LTX farm) pass `python`.
        exe = python or BASE_PY
        if not Path(exe).exists():
            say(name, "red", f"!! python not found: {exe} — component not started")
            return
        lf = open(path, "a", encoding="utf-8", buffering=1)
        p = subprocess.Popen([str(exe), *args], stdout=lf,
                             stderr=subprocess.STDOUT,
                             env=self._base_env(env, own_env=python is not None),
                             cwd=str(cwd) if cwd else None)
        self.job.add(p)
        self.procs[name] = p
        self._born[name] = time.time()
        say(name, "cyan", f"started (pid {p.pid})")
        threading.Timer(4.0, lambda: self.job.add_tree(p.pid)).start()

    # --------------------------------------------------------------- tail
    def tail(self, name: str, logfile: str, color: str) -> None:
        path = LOG_DIR / logfile
        size = path.stat().st_size if path.exists() else 0
        while not self.stop.is_set():
            try:
                if path.exists():
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as f:
                        f.seek(size)
                        for line in f:
                            line = line.rstrip()
                            if line:
                                say(name, color, line[:220])
                        size = f.tell()
            except Exception:
                pass
            time.sleep(0.5)

    @staticmethod
    def _extra_paths_args() -> list[str]:
        """--extra-model-paths-config for the paths.extra_model_dirs roots
        (regenerated per launch; nothing passed when none are configured —
        ComfyUI cannot load an empty config)."""
        dirs = extra_model_dirs()
        if not dirs:
            return []
        import yaml
        doc = {f"studio_extra_{i}": {"base_path": d,
                                     **{s: s for s in _MODEL_SUBDIRS}}
               for i, d in enumerate(dirs)}
        try:
            EXTRA_PATHS_YAML.parent.mkdir(parents=True, exist_ok=True)
            EXTRA_PATHS_YAML.write_text(
                "# generated by tool/launcher.py from settings "
                "paths.extra_model_dirs — do not edit\n"
                + yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        except OSError as e:
            say("studio", "yellow", f"extra model paths not written: {e}")
            return []
        return ["--extra-model-paths-config", str(EXTRA_PATHS_YAML)]

    def _spec(self, name: str):
        """(args, logfile, env, cwd, python) for a named component."""
        if name == "comfy-ui":
            return ([str(ROOT / "ComfyUI" / "main.py"), "--port",
                     str(COMFY_PORT), "--default-device", "0",
                     "--disable-auto-launch", *self._extra_paths_args()],
                    "comfyui_8188.log",
                    {"NCCL_P2P_DISABLE": "1",
                     "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                     "OMP_NUM_THREADS": str(os.cpu_count() or 1),
                     "KMP_AFFINITY": "granularity=fine,compact,1,0"},
                    ROOT / "ComfyUI", None)
        if name == "control-room":
            host = self.host or "127.0.0.1"
            port = self.port or UI_PORT
            return (["-m", "uvicorn", "api:app", "--host", host,
                     "--port", str(port)],
                    "ui.log",
                    {"PYTHONPATH": str(ROOT / "tool") + os.pathsep +
                     str(VENV_SITE)},
                    None, None)
        if name == "ltx-render":
            return ([str(ROOT / "ComfyUI_LTX" / "main.py"), "--port",
                     str(LTX_PORT), "--listen", "127.0.0.1",
                     "--enable-dynamic-vram", "--bf16-vae",
                     "--disable-auto-launch", *self._extra_paths_args()],
                    "ltx_8189.log", {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"},
                    ROOT / "ComfyUI_LTX", LTX_PY)
        if name == "h3-render":
            # H3 shares the LTX tree: without a separate database both
            # servers fight over one SQLite file (observed lock errors).
            h3_db = (ROOT / "ComfyUI_LTX" / "user" /
                     "comfyui_h3.db").as_posix()
            return ([str(ROOT / "ComfyUI_LTX" / "main.py"), "--port",
                     str(H3_PORT), "--listen", "127.0.0.1",
                     "--enable-dynamic-vram",
                     "--database-url", f"sqlite:///{h3_db}",
                     "--disable-auto-launch", *self._extra_paths_args()],
                    "h3_8199.log", {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"},
                    ROOT / "ComfyUI_LTX", LTX_PY)
        if name == "qwen-render":
            # NVML pressure stays ON: measured 2026-10-06 — disabling it made
            # aimdo overcommit the full card and thrash (60-104 s/step, 487-704
            # s runs) because CUDA's WDDM view ignores other processes' VRAM.
            # --highvram: measured 2026-10-06 — without it the Q5_K DiT keeps
            # falling into ComfyUI-GGUF cast mode (per-module CPU dequantize
            # every step: 100 W, one core, 20-100 s/step). --highvram disables
            # DynamicVRAM and skips the LOW/NORMAL lowvram branch in
            # load_models_gpu, so the 4.97 GB model materializes on the card.
            return ([str(ROOT / "ComfyUI_Qwen21" / "main.py"), "--port",
                     str(QWEN_PORT), "--listen", "127.0.0.1",
                     "--highvram", "--disable-auto-launch",
                     *self._extra_paths_args()],
                    "qwen_8190.log", {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"},
                    ROOT / "ComfyUI_Qwen21", QWEN_PY)
        return None

    def launch(self, name: str) -> None:
        """Spawn a component from its single source-of-truth spec."""
        spec = self._spec(name)
        if spec is None:
            say(name, "yellow", f"!! no launch spec for '{name}' — not started")
            return
        args, logfile, env, cwd, python = spec
        script = Path(args[0]) if args and args[0].endswith(".py") else None
        if script is not None and not script.exists():
            # a clone has no ComfyUI trees until they are set up
            say(name, "yellow",
                f"!! {script} not found — component not started "
                "(INSTALL.bat installs it)")
            if name not in self.not_installed:
                self.not_installed.append(name)
            return
        if python is not None and not Path(python).exists():
            # install/install_components.py builds the LTX/H3 and Qwen venvs
            say(name, "yellow",
                f"!! venv not found: {python} — component not started "
                "(INSTALL.bat installs it)")
            if name not in self.not_installed:
                self.not_installed.append(name)
            return
        self.spawn(name, args, logfile, env, cwd=cwd, python=python)

    def _health(self) -> None:
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                continue
            self.procs.pop(name, None)
            born = self._born.get(name, 0)
            fast = time.time() - born < RESTART_WINDOW_S
            if fast:
                self._fast_crashes[name] = self._fast_crashes.get(name, 0) + 1
            else:
                self._fast_crashes[name] = 0
            if self._fast_crashes.get(name, 0) >= RESTART_CAP:
                if name not in self._gave_up:
                    self._gave_up.add(name)
                    say(name, "red",
                        f"!! crashed {RESTART_CAP}x in a row — NOT restarting.")
                continue
            say(name, "red", f"!! DIED (exit {p.returncode}) — restarting "
                             f"(crash {self._fast_crashes.get(name, 0)}/{RESTART_CAP})")
            time.sleep(3)
            self.launch(name)

    def _listener_pids(self, port: int) -> list[int]:
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True)
            pids = []
            for line in out.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    pids.append(int(line.split()[-1]))
            return pids
        except Exception:
            return []

    def _is_ours(self, pid: int) -> bool:
        """Is this PID one of OUR studio processes (by command line)?"""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter "
                 "\"ProcessId={}\" | Select-Object -ExpandProperty "
                 "CommandLine".format(pid)],
                capture_output=True, text=True, timeout=15)
            return is_studio_cmd(out.stdout or "", self.port or UI_PORT)
        except Exception:
            return False

    def _warm_default_models(self) -> None:
        """Spawn the warm-up script once ComfyUI answers (see the thread in
        run()). Failures are logged, never fatal."""
        for _ in range(90):
            if self.stop.is_set():
                return
            if port_in_use(COMFY_PORT):
                break
            time.sleep(2)
        else:
            return
        try:
            # stdout goes to a separate file so the script's own structured
            # log (logs/warm_studio.log) is not doubled by the redirect
            logf = open(LOG_DIR / "warm_studio.out.log", "ab")
            subprocess.Popen(
                [str(BASE_PY), str(ROOT / "tool" / "scripts" /
                                   "warm_studio.py")],
                stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=self._base_env(),
                creationflags=subprocess.CREATE_NO_WINDOW)
            say("warm", "cyan", "default image + video models warming...")
        except Exception as e:  # noqa: BLE001
            say("warm", "yellow", f"warm-up failed to start: {e}")

    def _announce_ready(self, url: str) -> None:
        """Once the web app answers, show where it is where nobody misses it:
        the window title, a banner below the startup logs, and the default
        browser (ui.open_browser; --no-browser)."""
        deadline = time.time() + READY_TIMEOUT_S
        while not web_app_answers(url):
            if self.stop.is_set():
                return
            if time.time() > deadline:
                say("studio", "yellow", "!! the web app did not answer within "
                                        f"{READY_TIMEOUT_S} s — see logs\\ui.log")
                return
            time.sleep(1)
        if self.stop.is_set():
            return
        set_title(f"Kinoloom - open {url} - closing this window stops Kinoloom")
        rule = f" {C['green']}{'━' * 66}{C['reset']}"
        where = ("It opens in your browser." if self.open_browser
                 else "Open it in your browser.")
        lines = ["", rule,
                 f"   {C['bold']}{C['green']}Kinoloom is ready:{C['reset']}  "
                 f"{C['bold']}{url}{C['reset']}",
                 f"   {where} Keep this window open: closing it stops Kinoloom."]
        if self.not_installed:
            lines.append(f"   {C['yellow']}Not installed, so not started: "
                         f"{', '.join(self.not_installed)}. INSTALL.bat installs "
                         f"them.{C['reset']}")
        model_dirs = [ROOT / "models", *map(Path, extra_model_dirs())]
        if not has_weights(model_dirs):
            lines.append(f"   {C['yellow']}No model weights yet: "
                         "install\\INSTALL-MAIN.bat, mode [3], downloads "
                         f"them.{C['reset']}")
        lines += [rule, ""]
        print("\n".join(lines), flush=True)
        if self.open_browser:
            try:
                webbrowser.open(url)
            except Exception as e:  # noqa: BLE001 - no browser is not fatal
                say("studio", "yellow", f"could not open a browser ({e}); "
                                        f"open {url} yourself")

    def run(self, host: str | None = None, port: int | None = None) -> None:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--host", default=None)
        parser.add_argument("--port", type=int, default=None)
        cli_args, _ = parser.parse_known_args()

        if host is None:
            host = cli_args.host
        if port is None:
            port = cli_args.port

        host, port = self._resolve_host_port(host, port)
        url = browser_url(host, port)
        if self.open_browser is None:
            self.open_browser = self._open_browser_setting()

        enable_vt()
        set_title(f"Kinoloom - starting - {url}")
        conflicts = [(p, n) for p, n in ((COMFY_PORT, "render farm"),
                                         (port, "control room"),
                                         (LTX_PORT, "ltx render farm"),
                                         (H3_PORT, "h3 render farm"),
                                         (QWEN_PORT, "qwen render farm"))
                     if port_in_use(p)]
        if conflicts:
            holders = {pid for cport, _ in conflicts
                       for pid in self._listener_pids(cport)}
            ours = all(self._is_ours(pid) for pid in holders) if holders \
                else False
            if ours:
                say("studio", "yellow",
                    "!! A Kinoloom instance is ALREADY RUNNING "
                    f"(pids {sorted(holders)}).")
                say("studio", "yellow",
                    "    [R]estart it (kills the old instance, starts fresh)")
                say("studio", "yellow",
                    "    [E]xit (keep it running and use the open window)")
                try:
                    choice = input("    > ").strip().lower() or "r"
                except EOFError:
                    # started without an interactive console: restart rather
                    # than wedge forever waiting for a keypress
                    choice = "r"
                if choice != "r":
                    say("studio", "green",
                        f"keeping the running instance — open {url}")
                    return
                say("studio", "yellow",
                    "restarting: killing old instance (in-progress renders "
                    "are resume-safe)...")
                try:
                    out = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter "
                         "\"Name='python.exe'\" | "
                         "Select-Object ProcessId,CommandLine | "
                         "ConvertTo-Json -Compress"],
                        capture_output=True, text=True, timeout=20)
                    rows = json.loads(out.stdout or "[]")
                    if isinstance(rows, dict):
                        rows = [rows]
                    for p in rows:
                        if is_studio_cmd(p.get("CommandLine") or "", port):
                            subprocess.run(
                                ["taskkill", "/PID",
                                 str(p["ProcessId"]), "/T", "/F"],
                                capture_output=True)
                except Exception:
                    pass
                time.sleep(4)
            else:
                say("studio", "red", "!! CANNOT START — ports already in "
                                     "use:")
                for cport, name in conflicts:
                    say("studio", "red", f"   port {cport} ({name}) is taken.")
                    for pid in self._listener_pids(cport):
                        say("studio", "yellow",
                            f"   held by PID {pid} — run: "
                            f"taskkill /PID {pid} /F")
                say("studio", "yellow",
                    "   These are NOT our processes — close whatever holds "
                    "the ports, then start again.")
                return
        print(f"""
 {C['cyan']}◈{C['reset']}  {C['bold']}KINOLOOM{C['reset']} — control room + render farm
 {C['dim']}──────────────────────────────────────────────────────────{C['reset']}
 {C['cyan']}[comfy-ui]{C['reset']}     render farm (RTX 3070)                          → http://127.0.0.1:{COMFY_PORT}
 {C['magenta']}[ltx-render]{C['reset']}  LTX-2.5 farm (RTX 3070, isolated runtime)        → http://127.0.0.1:{LTX_PORT}
 {C['yellow']}[h3-render]{C['reset']}   MiniMax H3 farm (RTX 3070, isolated runtime)     → http://127.0.0.1:{H3_PORT}
 {C['cyan']}[qwen-render]{C['reset']} Qwen-Image-2.1 farm (RTX 3070, isolated runtime) → http://127.0.0.1:{QWEN_PORT}
 {C['green']}[control-room]{C['reset']} web UI                                          → {url}
 {C['yellow']}Stop:{C['reset']} press {C['bold']}Ctrl+C{C['reset']} or close this window — everything shuts down.
 {C['dim']}──────────────────────────────────────────────────────────{C['reset']}""")

        # One source of truth for every component's launch spec: the health
        # loop restarts from the same table, so a crashed H3/Qwen instance is
        # respawned correctly instead of falling through to the UI branch.
        for name in ("comfy-ui", "control-room", "ltx-render", "h3-render",
                     "qwen-render"):
            self.launch(name)

        for name, logfile, color in (("comfy-ui", "comfyui_8188.log", "cyan"),
                                     ("control-room", "ui.log", "green"),
                                     ("ltx-render", "ltx_8189.log", "magenta"),
                                     ("h3-render", "h3_8199.log", "yellow"),
                                     ("qwen-render", "qwen_8190.log", "blue")):
            threading.Thread(target=self.tail,
                             args=(name, logfile, color), daemon=True).start()

        # Warm the default image + video models once the render farm answers,
        # so the first real render is not a cold start. The script reads
        # settings.yaml (ensemble.warm_models / warm_image_models / warm_video)
        # and self-disables; closing this window kills ComfyUI, which releases
        # everything it cached.
        threading.Thread(target=self._warm_default_models,
                         daemon=True).start()
        threading.Thread(target=self._announce_ready, args=(url,),
                         daemon=True).start()

        while not self.stop.is_set():
            self._health()
            time.sleep(4)

    def shutdown(self, why: str) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        say("studio", "yellow", f"shutting down ({why}) ...")
        self.stop.set()
        # layer 1: kill recorded children recursively (fast path)
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    subprocess.run(["taskkill", "/PID", str(p.pid),
                                    "/T", "/F"], capture_output=True,
                                   timeout=15)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass
        # layer 2: pattern-based sweep — immune to any parent/child race.
        # Kills ONLY processes that are unmistakably ours (is_studio_cmd).
        port = self.port or UI_PORT
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                 "| Select-Object ProcessId,CommandLine | "
                 "ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20)
            rows = json.loads(out.stdout or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            for p in rows:
                if is_studio_cmd(p.get("CommandLine") or "", port):
                    subprocess.run(["taskkill", "/PID",
                                    str(p["ProcessId"]), "/T", "/F"],
                                   capture_output=True)
        except Exception:
            pass
        time.sleep(1)
        self.job.close()  # layer 3: job object backstop
        say("studio", "green", "all components stopped. goodbye.")


# ------------------------------------------------------------ console hook
# Keep a module-level reference so the callback is never garbage collected.
_HANDLER = None


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Kinoloom launcher")
    parser.add_argument("--host", default=None, help="Host to bind control room web UI")
    parser.add_argument("--port", type=int, default=None, help="Port to bind control room web UI")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open the web UI in the browser (ui.open_browser)")
    args, _ = parser.parse_known_args()

    launcher = Launcher(host=args.host, port=args.port,
                        open_browser=False if args.no_browser else None)
    global _HANDLER

    PHANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint32)
    CTRL_C_EVENT, CTRL_CLOSE_EVENT = 0, 2

    def console_handler(ctrl_type: int) -> bool:
        launcher.shutdown("console closed" if ctrl_type == CTRL_CLOSE_EVENT
                          else "Ctrl+C")
        # For Ctrl+C we handle it fully. For window-close we return False so
        # the default termination also fires as a final backstop.
        return ctrl_type == CTRL_C_EVENT

    _HANDLER = PHANDLER(console_handler)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_HANDLER, True)
    except Exception:
        pass

    def sigint(*_):
        launcher.shutdown("Ctrl+C")

    signal.signal(signal.SIGINT, sigint)
    signal.signal(signal.SIGTERM, sigint)
    try:
        launcher.run(host=args.host, port=args.port)
    except KeyboardInterrupt:
        launcher.shutdown("Ctrl+C")
    finally:
        launcher.job.close()
        sys.exit(0)


if __name__ == "__main__":
    main()

