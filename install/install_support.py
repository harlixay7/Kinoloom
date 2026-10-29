"""What the install steps share: the install log, pip runs with a log of their
own, a plain report when a step fails, and a PC that stays awake meanwhile.

INSTALL-MAIN.bat runs it for the web app's venv (step 3), with that venv's
own python; install_components.py imports it for the ComfyUI venvs. Standard
library only: it runs before any package is installed.

  python install/install_support.py venv NAME REQUIREMENTS [--index URL]
         [--check-import a,b,c] [--step TEXT] [--venv DIR]
      Pins pip, installs the list (lines marked [second-pass] after the rest),
      tries once more after a network error, and imports the modules
      given. Exit 0 when all went well, 1 after a failure, which it has printed
      and saved by then. With --venv, the venv is marked unfinished until all of
      that went well, and a venv finished from the same list before is only
      checked, so a second run needs no network.
  python install/install_support.py finish
      Deletes pip's download cache and temporary files under temp\\ once an
      install has finished.

Everything is written under <studio>/logs/install/:
  install.log       every run's steps, and what went wrong when one failed
  pip-<name>.log    pip's own verbose log of the last build of that venv, less
                    its line for every file on an index page (the run before
                    it is kept as pip-<name>.prev.log)
  LAST-FAILURE.txt  the failure that stopped the last run; INSTALL-MAIN.bat
                    deletes it when a run starts, start.bat shows it
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG_DIR = ROOT / "logs" / "install"
INSTALL_LOG = LOG_DIR / "install.log"
LAST_FAILURE = LOG_DIR / "LAST-FAILURE.txt"
# pip downloads into the studio folder's drive, not C: (often the small one),
# and a finished install deletes both (finish)
PIP_CACHE = ROOT / "temp" / "pip-cache"
PIP_TMP = ROOT / "temp" / "pip-tmp"
ISSUES = "https://github.com/harlixay7/Kinoloom/issues"

# pip itself in every venv the installer builds; the one a new venv brings is
# older (install/requirements-frozen/base-python.txt has the same pin)
PIP = "pip==26.2.2"
# pip's defaults, 15 s and 5 retries, give up early on a slow or unstable line.
# A setting of the same name on the PC wins.
PIP_DEFAULTS = {"PIP_TIMEOUT": "60", "PIP_RETRIES": "10", "PIP_RESUME_RETRIES": "10"}
# Nothing may wait for an answer nobody sees (a git login, a pip prompt).
# UTF-8 mode: a package's setup.py that reads its README without an encoding
# fails on PCs whose code page cannot decode it.
FIXED_ENV = {"PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
             "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "PYTHONUTF8": "1"}
# settings from the PC that would change what pip installs, or where
CLEARED = ("PYTHONPATH", "PYTHONHOME", "PIP_USER", "PIP_TARGET", "PIP_PREFIX", "PIP_ROOT",
           "PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT", "PIP_NO_BUILD_ISOLATION",
           "PIP_NO_BINARY", "PIP_ONLY_BINARY", "PIP_NO_DEPS", "PIP_REQUIRE_HASHES")
RETRY_WAIT_S = 30
# A requirement line with this in its comment is installed after the rest of
# its list, built inside the venv (--no-build-isolation) against the packages
# the list pins. SAM-2's build asks for "torch>=2.5.1": built in a temporary
# environment of its own, it downloads and unpacks a second, newer torch there.
SECOND_PASS = "[second-pass]"
# A venv is unfinished while it holds MARKER: start.bat and the launcher do not
# start it, and the installers build it again. DONE holds the list it was
# finished from (see build_venv).
MARKER = ".kinoloom-incomplete"
MARKER_TEXT = "pip has not finished this venv; the installer builds it again\n"
DONE = ".kinoloom-installed"


# ------------------------------------------------------------------ the log
def log(*lines: str) -> None:
    """Append lines to install.log. A log that cannot be written never stops
    the install."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(INSTALL_LOG, "a", encoding="utf-8", errors="replace") as fh:
            for line in lines:
                fh.write(line.rstrip("\r\n") + "\n")
    except OSError:
        pass


def say(text: str) -> None:
    """Print a line and log it."""
    print(text, flush=True)
    if not isinstance(sys.stdout, Tee):
        log(text)


class Tee:
    """sys.stdout that also appends what is printed to install.log (pip's
    output goes to the console directly and to its own log)."""

    def __init__(self, stream, path: Path):
        self.stream = stream
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self.fh = None

    def write(self, s: str) -> int:
        n = self.stream.write(s)
        if self.fh is not None:
            try:
                self.fh.write(s)
                self.fh.flush()
            except (OSError, ValueError):
                self.fh = None
        return n

    def flush(self) -> None:
        self.stream.flush()

    def close_log(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None

    def __getattr__(self, name):
        return getattr(self.stream, name)


# ------------------------------------------------------------------ the PC
def keep_awake() -> None:
    """Ask Windows not to sleep while this process runs: a long download on a
    laptop otherwise stops at the sleep timer. It ends with the process."""
    if os.name != "nt":
        return
    try:
        es_continuous, es_system_required = 0x80000000, 0x00000001
        k = ctypes.WinDLL("kernel32")
        k.SetThreadExecutionState.argtypes = (ctypes.c_uint32,)
        k.SetThreadExecutionState.restype = ctypes.c_uint32
        k.SetThreadExecutionState(es_continuous | es_system_required)
    except Exception:  # noqa: BLE001 - a PC that sleeps anyway is no reason to stop
        pass


def no_quick_edit() -> None:
    """A click into a Windows 10 console window starts a text selection, and
    every program writing to the window waits until a key is pressed: an
    install that looks stuck. Turn that off while this process runs."""
    if os.name != "nt":
        return
    try:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        k.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        # the console's input, even when stdin is redirected
        h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            return
        mode = wintypes.DWORD()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return
        quick_edit, extended_flags = 0x40, 0x80
        if mode.value & quick_edit:
            k.SetConsoleMode(h, (mode.value | extended_flags) & ~quick_edit)
            atexit.register(k.SetConsoleMode, h, mode.value)
    except Exception:  # noqa: BLE001 - without a console there is nothing to change
        pass


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
_git_proxy: dict | None = None      # worked out once per run (git_proxy_env)


def windows_proxies() -> dict:
    """The proxy in the Windows settings ({'https': 'http://host:port', ...})."""
    get = getattr(urllib.request, "getproxies_registry", None)
    try:
        return dict(get()) if get else {}
    except OSError:
        return {}


def _without_login(url: str) -> str:
    return re.sub(r"//[^/@]*@", "//***@", url)


def git_proxy_env(env: dict) -> dict:
    """The proxy in the Windows settings, for git alone. pip reads it there
    itself, with its list of addresses that bypass it; git, which pip runs
    for the packages that install from git, does not. It gets it as its own
    http.proxy setting (GIT_CONFIG_*), unless the PC sets a proxy variable
    or git has a proxy of its own."""
    global _git_proxy
    if _git_proxy is not None:
        return _git_proxy
    _git_proxy = {}
    names = {k.upper() for k in env}
    proxies = windows_proxies()
    proxy = proxies.get("https") or proxies.get("http")
    if not proxy or names & set(_PROXY_VARS) or "GIT_CONFIG_COUNT" in names:
        return _git_proxy
    try:
        own = subprocess.run(["git", "config", "--get-urlmatch", "http.proxy", "https://github.com"],
                             capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return _git_proxy                   # no git: nothing to pass it to
    if own.returncode == 0 and own.stdout.strip():
        return _git_proxy
    _git_proxy = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.proxy",
                  "GIT_CONFIG_VALUE_0": proxy}
    log(f"  git uses the proxy of the Windows settings: {_without_login(proxy)}")
    return _git_proxy


def install_env() -> dict:
    """The environment pip and git run in during an install."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in CLEARED}
    for k, v in PIP_DEFAULTS.items():
        env.setdefault(k, v)
    env.update(FIXED_ENV)
    env.update(git_proxy_env(env))
    if "PIP_CACHE_DIR" not in env and "PIP_NO_CACHE_DIR" not in env:
        env["PIP_CACHE_DIR"] = str(PIP_CACHE)
    try:
        PIP_TMP.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(PIP_TMP)
    except OSError:
        pass        # the PC's own temp folder then
    return env


# ------------------------------------------------------------------ failures
@dataclass
class Failure:
    step: str                       # what was being installed
    cause: str                      # a key of CAUSES
    lines: list[str] = field(default_factory=list)  # the lines that show it
    logs: list[Path] = field(default_factory=list)  # where the whole story is
    what: str = ""                  # overrides CAUSES' "what happened"


# what happened, what to do; {rerun} is "run INSTALL.bat again" (INSTALL-MAIN.bat
# passes its own wording in KINOLOOM_RERUN)
CAUSES = {
    "network": ("A download failed: the connection timed out, was refused or broke off.",
                "Check that this PC is online. A VPN, proxy, firewall or antivirus web "
                "filter can block downloads. Then {rerun}: what finished downloading is "
                "kept, so it goes on from there."),
    "dns": ("This PC could not look up the address of a download server (DNS).",
            "Check that this PC is online. A VPN or a DNS filter can cause this. "
            "Then {rerun}."),
    "ssl": ("A secure (HTTPS) connection failed its certificate check.",
            "An antivirus that scans HTTPS traffic, or a company network that inspects "
            "it, causes this. Turn that scan off while the install runs, or ask whoever "
            "runs the network, then {rerun}."),
    "proxy": ("The proxy server refused the connection or broke it off.",
              "Check the proxy settings, then {rerun}."),
    "disk": ("The disk is full.",
             "Free up space on {drive}, then {rerun}."),
    "virus": ("An antivirus program blocked a file as unsafe (Windows error 225).",
              "Allow the Kinoloom folder in the antivirus, then {rerun}."),
    "locked": ("Windows refused access to a file, or another program had it open.",
               "Close Kinoloom if it is running. An antivirus (Windows Security's "
               "'Controlled folder access' among them) or OneDrive can block or hold files "
               "in the Desktop and Documents folders: allow the Kinoloom folder, or move it "
               "to C:\\Kinoloom, then {rerun}."),
    "longpath": ("A file path got longer than Windows allows.",
                 "Move the Kinoloom folder to a short path, like C:\\Kinoloom, then {rerun}."),
    "hash": ("A downloaded file was damaged: its checksum did not match.",
             "{Rerun}. If it happens again, a proxy or an antivirus may be changing "
             "downloads."),
    "damaged": ("A downloaded archive could not be unpacked: it is damaged, or the "
                "server sent something else (a sign-in page of a hotel or guest "
                "network, for example).",
                "{Rerun}. If it happens again, sign in to the network in a browser "
                "first; a proxy or an antivirus may also be changing downloads."),
    "media": ("OpenCV could not load a Windows media library. The 'N' editions of "
              "Windows leave it out.",
              "Install the Media Feature Pack (Settings > Apps > Optional features > "
              "Add a feature), restart Windows, then {rerun}."),
    "dll": ("A package could not load one of its DLL files.",
            "An antivirus may have removed a file, or the Microsoft Visual C++ runtime "
            "is damaged. Restart Windows, allow the Kinoloom folder in the antivirus, "
            "then {rerun}."),
    "incomplete": ("A package pip installed cannot be imported: some of its files are "
                   "missing.",
                   "An antivirus may have removed them. Allow the Kinoloom folder in the "
                   "antivirus, then {rerun}: it builds this environment again."),
    "build": ("A package that is built on this PC did not build.",
              "{Rerun}. If it fails the same way, please open an issue at " + ISSUES
              + " and attach the logs below."),
    "notfound": ("pip could not find a package version the list asks for.",
                 "{Rerun}. If it fails the same way, please open an issue at " + ISSUES
                 + " and attach the logs below."),
    "stopped": ("The install was stopped (Ctrl+C or the window was closed).",
                "{Rerun} when you are ready: the finished steps are skipped."),
    "unknown": ("The step stopped with an error.",
                "{Rerun}. If it fails the same way, please open an issue at " + ISSUES
                + " and attach the logs below."),
}

# first match wins; every pattern is matched ignoring case
_PATTERNS = [
    ("disk", r"No space left on device|\[Errno 28\]|WinError 112\b|not enough space on the disk"),
    ("virus", r"WinError 225\b|contains a virus|potentially unwanted software"),
    ("longpath", r"WinError 206\b|filename or extension is too long|Long Path support"),
    ("locked", r"WinError 5\]|WinError 32\]|Access is denied|being used by another process"
               r"|PermissionError"),
    ("media", r"DLL load failed while importing cv2"),
    ("dll", r"DLL load failed|WinError 126\b|WinError 1114\b"),
    ("ssl", r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSL certificate problem"
            r"|SSLError|SSLCertVerificationError|local issuer certificate|schannel"),
    ("proxy", r"ProxyError|Tunnel connection failed|407 Proxy"),
    ("dns", r"getaddrinfo failed|NameResolutionError|Could not resolve host|Failed to resolve"
            r"|Errno 1100[12]\b"),
    ("network", r"Read ?timed out|ReadTimeoutError|ConnectTimeoutError|Connection timed out"
                r"|Operation timed out|NewConnectionError|Failed to establish a new connection"
                r"|Could not connect to server|Failed to connect to|Connection refused"
                r"|actively refused|Connection reset|ConnectionResetError|Connection aborted"
                r"|RemoteDisconnected|IncompleteRead|ProtocolError|connection broken by"
                r"|Max retries exceeded|unable to access 'http|RPC failed|early EOF"
                r"|remote end hung up|WinError 100[5-6]\d\b|HTTP error 5\d\d|5\d\d Server Error"
                r"|Service Unavailable|Bad Gateway|timed out"),
    ("hash", r"DO NOT MATCH THE HASHES|hash mismatch"),
    ("damaged", r"BadZipFile|not a zip file|Bad CRC-32|TarError|ReadError|not a gzip file"
                r"|unexpected end of data|Compressed file ended"),
    ("build", r"subprocess-exited-with-error|Failed to build|Failed building wheel"
              r"|metadata-generation-failed|Microsoft Visual C\+\+ 14\.0 or greater"),
    ("notfound", r"No matching distribution found|Could not find a version that satisfies"
                 r"|ResolutionImpossible|conflicting dependencies"),
]
_REACH = ("ssl", "proxy", "dns", "network")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d,\d{3} ?")
# pip's --log has a line for every file it sees on an index page: 70.0 of the
# 70.7 MB its log of tool\.venv had, and nothing a report needs
_LINK_NOISE = re.compile(r"^\S+\s+(Skipping link: |Found link )")
_RETRY = re.compile(r"Retry\(total=\d+[^)]*\)\)?")
_OBJECT = re.compile(r"<[\w.]+ object at 0x[0-9A-Fa-f]+>: ")


def _match(text: str, causes=None) -> str | None:
    for cause, pattern in _PATTERNS:
        if (causes is None or cause in causes) and re.search(pattern, text, re.I):
            return cause
    return None


def evidence(text: str, limit: int = 14) -> list[str]:
    """The lines of a pip log (or any error output) that show what went wrong:
    pip's ERROR lines, git's fatal ones, the last retries, and the end of a
    failed build's output."""
    picked: list[str] = []
    retries: list[str] = []
    in_output = in_info = in_trace = False
    output: list[str] = []
    for raw in text.splitlines():
        bare = _OBJECT.sub("", _STAMP.sub("", raw))
        line = bare.strip()
        if not line:
            continue
        if re.search(r"\[\d+ lines? of output\]", line):
            in_output, output = True, []
            continue
        if in_output:       # a build's own output, its traceback included
            if "[end of output]" in line:
                in_output = False
                picked += output[-8:]
            else:
                output.append(line.lstrip("\u2502 ").strip() or line)
            continue
        # The traceback pip logs after "Exception information:" (chained ones
        # included) belongs to an error its ERROR lines already gave: its
        # internals, not the cause. After "ERROR: Exception:" the traceback's
        # last line is the cause, so other tracebacks are read as usual.
        if line == "Exception information:":
            in_info, in_trace = True, False
            continue
        if in_info:
            if in_trace:
                in_trace = bare[:1].isspace()   # its last line, the exception, is not indented
                continue
            if line.startswith(("Traceback (most recent call last)", "During handling of the above",
                                "The above exception was the direct cause")):
                in_trace = line.startswith("Traceback")
                continue
            in_info = False
        if line.startswith("WARNING: Retrying"):
            retries.append(line)
        elif line.startswith(("ERROR:", "HINT:", "fatal:", "error:", "Could not fetch URL")) or (
                re.match(r"^(\w+\.)*\w*(Error|Exception)\b.*:", line)):
            picked.append(line)
    if retries:
        picked = retries[-2:] + picked
    seen: set[str] = set()
    out = []
    for line in picked:
        key = _RETRY.sub("", line)
        if key not in seen:
            seen.add(key)
            out.append(line if len(line) <= 300 else line[:297] + "...")
    return out[-limit:]


def classify(text: str) -> tuple[str, list[str]]:
    """(cause, the lines that show it) for a failed step's log or output."""
    lines = evidence(text)
    if not lines:       # not pip's log: an error message of our own, or a program's output
        lines = [_STAMP.sub("", x).strip() for x in text.splitlines() if x.strip()][-6:]
    cause = _match("\n".join(lines))
    if cause in (None, "notfound"):
        # an index or a server that could not be reached shows up as a missing
        # version; the retries and the fetch errors before it say why
        reach = _match("\n".join(_STAMP.sub("", x) for x in text.splitlines()
                                 if re.search(r"Retrying|Could not fetch URL|fatal:", x)), _REACH)
        cause = reach or cause
    return cause or "unknown", lines


def _rerun() -> str:
    return os.environ.get("KINOLOOM_RERUN") or "run INSTALL.bat again"


def _wrap(text: str, indent: str = " ") -> list[str]:
    return textwrap.wrap(text, 74, initial_indent=indent, subsequent_indent=indent) or [indent]


def failure_text(fails: list[Failure], title: str) -> list[str]:
    """The report's lines, the same on the screen and in the files."""
    rerun = _rerun()
    out = [f" {title}", ""]
    for i, f in enumerate(fails):
        what, todo = CAUSES.get(f.cause, CAUSES["unknown"])
        todo = todo.format(rerun=rerun, Rerun=rerun[:1].upper() + rerun[1:],
                           drive=ROOT.anchor.rstrip("\\") or str(ROOT))
        if i:
            out.append("")
        out.append(f" Step: {f.step}")
        out += _wrap("What happened: " + (f.what or what))
        out += _wrap("What to do: " + todo)
        if f.lines:
            out.append(" The lines that show it:")
            out += [f"   {x}" for x in f.lines]
    logs: list[Path] = []
    for f in fails:
        logs += [p for p in f.logs if p not in logs]
    out += ["", f" Saved to:  {LAST_FAILURE}"]
    for i, p in enumerate(logs):
        out.append((" Full logs: " if i == 0 else "            ") + str(p))
    return out


def report(fails: list[Failure], title: str = "THE INSTALL STOPPED") -> None:
    """Print a failure where it cannot be missed, add it to install.log and
    save it as LAST-FAILURE.txt."""
    body = failure_text(fails, title)
    rule = " " + "=" * 74
    # past a Tee, so the box is not logged twice
    screen = sys.stdout.stream if isinstance(sys.stdout, Tee) else sys.stdout
    print("\n".join(["", rule, *body, rule, ""]), file=screen, flush=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log(f"==== FAILED {stamp} ====", *body, "==== end of failure ====")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_FAILURE.write_text("\n".join([f"Kinoloom install, {stamp}", *body, ""]),
                                encoding="utf-8", errors="replace")
    except OSError:
        pass


# ------------------------------------------------------------------ pip
def run_pip(py: Path, args: list[str], log_path: Path) -> tuple[int, str]:
    """`py -m pip <args>`, pip's verbose log appended to log_path. Returns the
    exit code and what this run added to the log."""
    start = log_path.stat().st_size if log_path.exists() else 0
    cmd = [str(py), "-m", "pip", *args, "--log", str(log_path)]
    log("  $ " + " ".join(f'"{a}"' if " " in a else a for a in cmd))
    t0 = time.monotonic()
    try:
        rc = subprocess.run(cmd, env=install_env()).returncode
    except OSError as e:
        return 1, f"ERROR: could not run {py}: {e}"
    log(f"  pip exit code {rc} after {int(time.monotonic() - t0)} s")
    prune(log_path, start)
    try:
        with open(log_path, "rb") as fh:
            fh.seek(start)
            return rc, fh.read().decode("utf-8", "replace")
    except OSError:
        return rc, ""


def prune(log_path: Path, start: int = 0) -> None:
    """Drop pip's line-per-index-file entries from what a run added to its log."""
    tmp = log_path.with_name(log_path.name + ".tmp")
    try:
        with open(log_path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read(start))
            for line in src:
                if not _LINK_NOISE.match(line.decode("utf-8", "replace")):
                    dst.write(line)
        tmp.replace(log_path)
    except OSError:
        tmp.unlink(missing_ok=True)


def pip_install(py: Path, args: list[str], log_path: Path, step: str) -> Failure | None:
    """pip install with one more try after a network error: what
    finished downloading is in pip's cache by then. Network errors stop pip
    before it installs anything, so the second run starts clean."""
    for attempt in (1, 2):
        rc, text = run_pip(py, ["install", *args], log_path)
        if rc == 0:
            return None
        cause, lines = classify(text)
        if cause == "network" and attempt == 1:
            why = next((x for x in reversed(lines) if _match(x, ("network",))),
                       lines[-1] if lines else "a network error")
            say(f"  A download failed ({why[:150]}).")
            say(f"  Trying once more in {RETRY_WAIT_S} s; what finished downloading is kept.")
            time.sleep(RETRY_WAIT_S)
            continue
        return Failure(step, cause, lines, [INSTALL_LOG, log_path])
    return None     # not reached


def check_imports(py: Path, modules: list[str], step: str) -> Failure | None:
    """Import the modules in the new venv: a file an antivirus removed, or a
    DLL Windows cannot load, shows up here instead of when Kinoloom starts."""
    if not modules:
        return None
    code = ("import importlib, sys\n"
            "for m in sys.argv[1:]:\n"
            "    importlib.import_module(m)\n")
    try:
        r = subprocess.run([str(py), "-c", code, *modules], capture_output=True,
                           encoding="utf-8", errors="replace", env=install_env(), timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Failure(step, "unknown", [f"{type(e).__name__}: {e}"], [INSTALL_LOG])
    if r.returncode == 0:
        log(f"  imports OK: {', '.join(modules)}")
        return None
    out = (r.stderr or r.stdout or "").strip()
    log("  import check failed:", *out.splitlines()[-30:])
    cause = _match(out) or "unknown"
    if cause not in ("media", "dll", "incomplete", "virus", "locked"):
        cause = "incomplete"
    lines = [x.strip() for x in out.splitlines() if x.strip()][-6:]
    return Failure(step, cause, lines, [INSTALL_LOG])


def rotate(log_path: Path) -> None:
    """Keep the previous run's pip log as <name>.prev.log (trimmed too: a run
    that was stopped did not get to trim it)."""
    if log_path.exists():
        prune(log_path)
        try:
            log_path.replace(log_path.with_name(log_path.stem + ".prev" + log_path.suffix))
        except OSError:
            pass


def split_second_pass(requirements: Path, work: Path) -> tuple[Path, list[str]]:
    """The list without its [second-pass] lines (a copy in work, or the list
    itself when it has none), and the requirements on those lines."""
    first: list[str] = []
    later: list[str] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if SECOND_PASS in line:
            later.append(re.sub(r"(^|\s+)#.*$", "", line).strip())
        else:
            first.append(line)
    if not later:
        return requirements, []
    work.mkdir(parents=True, exist_ok=True)
    copy = work / f"{requirements.stem}.first-pass.txt"
    copy.write_text("\n".join(first) + "\n", encoding="utf-8")
    return copy, later


def install_venv_packages(py: Path, requirements: Path, name: str, *, step: str = "",
                          index: str | None = None,
                          imports: list[str] | tuple[str, ...] = ()) -> Failure | None:
    """Fill a fresh venv: pip pinned, then the list, then the import check.
    None when all went well, else what went wrong (not yet reported)."""
    step = step or f"{name}, {requirements.name}"
    keep_awake()
    no_quick_edit()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pip_log = LOG_DIR / f"pip-{name}.log"
    rotate(pip_log)
    log(f"  {step}: pip logs to {pip_log}")
    fail = pip_install(py, ["--upgrade", PIP], pip_log, step)
    if fail:
        return fail
    reqs, later = split_second_pass(requirements, PIP_TMP)
    args = ["-r", str(reqs)]
    if index:
        args = ["--extra-index-url", index, *args]
    fail = pip_install(py, args, pip_log, step)
    if not fail and later:
        # their own dependencies are pinned in the list and installed by now,
        # so pip adds these and changes nothing else
        log(f"  second pass, built in the venv: {', '.join(later)}")
        fail = pip_install(py, ["--no-build-isolation", "--no-deps", *later], pip_log, step)
    return fail or check_imports(py, list(imports), step)


def list_stamp(requirements: Path) -> str:
    """What a venv was finished from: the list and the pip pin."""
    return hashlib.sha256(PIP.encode() + b"\n" + requirements.read_bytes()).hexdigest()


def build_venv(py: Path, requirements: Path, name: str, venv: Path, *, step: str = "",
               index: str | None = None,
               imports: list[str] | tuple[str, ...] = ()) -> Failure | None:
    """install_venv_packages for a venv the installer owns. It is marked
    unfinished until pip and the import check went well. A venv finished from
    the same list is only checked: a second run of the installer needs no
    network for it, and a failed second run cannot leave a working venv
    marked unfinished."""
    step = step or f"{name}, {requirements.name}"
    marker, done = venv / MARKER, venv / DONE
    stamp = list_stamp(requirements)
    was_finished = not marker.exists()      # from another list, or before stamps
    try:
        finished = was_finished and done.read_text(encoding="utf-8").strip() == stamp
    except OSError:
        finished = False
    if finished:
        try:
            shown = str(venv.resolve().relative_to(ROOT.resolve()))
        except ValueError:
            shown = str(venv)
        log(f"  {shown} was finished from this {requirements.name}; checking that it imports")
        fail = check_imports(py, list(imports), step)
        if fail is None:
            say(f"  OK   {shown} is up to date ({requirements.name} has not changed)")
            return None
        # a file is gone from a finished venv: the next run builds it again
        marker.write_text(MARKER_TEXT, encoding="utf-8")
        return fail
    marker.write_text(MARKER_TEXT, encoding="utf-8")
    done.unlink(missing_ok=True)
    fail = install_venv_packages(py, requirements, name, step=step, index=index, imports=imports)
    if fail is None:
        done.write_text(stamp + "\n", encoding="utf-8")
        marker.unlink()
    elif was_finished and fail.cause in _REACH:
        # pip downloads everything before it changes anything, so a run that
        # could not reach a server left the finished venv as it was: it keeps
        # working, and the next run tries the list again (no stamp)
        marker.unlink(missing_ok=True)
        log(f"  {venv} works as before; the next run installs {requirements.name} again")
    return fail


# ------------------------------------------------------------------ cleanup
def long_path(target: str) -> str:
    """The \\\\?\\ form of an absolute Windows path, which reaches past 260
    characters. A mapped network drive resolves to \\\\server\\share\\...,
    whose long form is \\\\?\\UNC\\server\\share\\...."""
    if target.startswith("\\\\?\\"):
        return target
    return "\\\\?\\UNC\\" + target[2:] if target.startswith("\\\\") else "\\\\?\\" + target


def remove_tree(path: Path) -> None:
    """rmtree that also reaches files past 260 characters and read-only ones."""
    def unlock(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    target = str(path.resolve())
    shutil.rmtree(long_path(target) if os.name == "nt" else target, onexc=unlock)


def remove_venv(venv: Path) -> None:
    """Delete a venv so that a removal which stops half-way (a file another
    program holds open) leaves a venv still marked unfinished, never one that
    looks whole: the marker goes in first and comes out last."""
    if not venv.exists():
        return
    marker = venv / MARKER
    marker.write_text(MARKER_TEXT, encoding="utf-8")
    for child in venv.iterdir():
        if child.name == MARKER:
            continue
        if child.is_dir() and not child.is_symlink():
            remove_tree(child)
        else:
            os.chmod(child, stat.S_IWRITE)
            child.unlink()
    marker.unlink()
    venv.rmdir()


def finish() -> None:
    """Delete pip's download cache and temporary files of this install."""
    for p in (PIP_CACHE, PIP_TMP):
        if p.exists():
            try:
                remove_tree(p)
                log(f"  removed {p}")
            except OSError as e:
                log(f"  could not remove {p}: {e}")


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Shared install steps of Kinoloom.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("venv", help="fill a fresh venv (run with that venv's python)")
    v.add_argument("name", help="the venv's name in the log file names, e.g. tool-venv")
    v.add_argument("requirements", type=Path)
    v.add_argument("--index", help="an extra package index (the CUDA builds of torch)")
    v.add_argument("--check-import", default="", help="modules to import afterwards, a,b,c")
    v.add_argument("--step", default="", help="how the report names this step")
    v.add_argument("--venv", type=Path, help="the venv's folder: mark it unfinished until done, "
                                             "and only check it when its list is unchanged")
    sub.add_parser("finish", help="delete pip's cache and temporary files")
    sub.add_parser("remove-venv", help="delete a venv, its marker last").add_argument(
        "venv", type=Path)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:     # a path or an error line pip printed never stops the report
            reconfigure(errors="replace", line_buffering=True)
    if args.cmd == "finish":
        finish()
        return 0
    if args.cmd == "remove-venv":
        try:
            remove_venv(args.venv)
        except OSError as e:
            log(f"  could not remove {args.venv}: {e}")
            print(f"  could not remove {args.venv}: {e}")
            return 1
        return 0
    step = args.step or f"{args.name}, {args.requirements.name}"
    imports = [m.strip() for m in args.check_import.split(",") if m.strip()]
    try:
        if args.venv:
            fail = build_venv(Path(sys.executable), args.requirements, args.name, args.venv,
                              step=step, index=args.index, imports=imports)
        else:
            fail = install_venv_packages(Path(sys.executable), args.requirements, args.name,
                                         step=step, index=args.index, imports=imports)
    except KeyboardInterrupt:
        fail = Failure(step, "stopped")
    except Exception as e:  # noqa: BLE001 - every failure is reported, never a bare traceback
        fail = Failure(step, "unknown", [f"{type(e).__name__}: {e}"], [INSTALL_LOG],
                       what="The installer's own helper failed.")
    if fail:
        report([fail])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
