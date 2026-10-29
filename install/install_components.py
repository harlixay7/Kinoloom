"""Install the parts of Kinoloom you choose: the ComfyUI trees, their node
packs and venvs, the model weights, and ffmpeg.

  python install/install_components.py                  # menu
  python install/install_components.py --list           # what can be installed
  python install/install_components.py --check          # report only, change nothing
  python install/install_components.py --components ltx25,qwen21 --yes
  python install/install_components.py --components all --dry-run
  python install/install_components.py --use-folder D:\\ComfyUI\\models   # files you already have

Everything it fetches is pinned in install/components.json: the ComfyUI
release commits, the node packs by git commit, the venv requirements, and
every weight file by repository, size and sha256. A downloaded file that does
not match is deleted and reported, never used. Re-running is safe: finished
steps are skipped, and files that already exist are never overwritten (the
repository's own files inside the ComfyUI trees stay as they are).

Weights go to <studio>/models/<folder>/, where every ComfyUI tree and the
studio look for them (post-processing files go under ComfyUI/). Two sets are
never downloaded: MiniMax H3, because the MiniMax H3 Community License does
not cover every country, and RealVisXL, because I have no public copy to
check against. Their components install the runtime and list the files to
bring, where they are published and where they go.

Weight files already on the PC are used instead of a download, whatever
their name or folder (tool/model_files.py): the model folders of the settings
and Kinoloom's own models folder are searched by themselves, and the installer
asks for more folders (or takes them with --use-folder). A file found by size
and sha256 is hard-linked into place when it is on the same drive (no extra
space), used where it is when it sits in a model folder under its own name and
place (the folder goes into paths.extra_model_dirs), or copied, if the user
agrees.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:       # loaded by file path (the tests), not run as a script
    sys.path.insert(0, str(HERE))
# the studio's own lookup of model files (tool/model_files.py, standard library
# only); appended, so nothing in tool/ can stand in for an installed package
if str(ROOT / "tool") not in sys.path:
    sys.path.append(str(ROOT / "tool"))
import gpu_support  # noqa: E402
import install_support as support  # noqa: E402
import model_files as mf  # noqa: E402

CATALOG = HERE / "components.json"
UA = {"User-Agent": "kinoloom-installer"}
GB = 1e9
# the servers no connection could be made to in this run, with why: a later
# download from one of them fails at once instead of through all its tries again
UNREACHABLE: dict[str, str] = {}


class InstallError(RuntimeError):
    def __init__(self, message: str = "", failure: support.Failure | None = None):
        super().__init__(message)
        self.failure = failure      # a pip failure's details, for the report


# ------------------------------------------------------------------ catalog
def load_catalog(path: Path = CATALOG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Plan:
    components: list[str]
    trees: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


def resolve(cat: dict, selected: list[str]) -> Plan:
    """Expand component ids into the trees, node packs and files they need,
    in install order and without duplicates."""
    comps = cat["components"]
    unknown = [c for c in selected if c not in comps]
    if unknown:
        raise InstallError(f"unknown component(s): {', '.join(unknown)} "
                           f"(choose from {', '.join(comps)})")
    plan = Plan(components=[c for c in comps if c in selected])

    def add(seq: list[str], items):
        for it in items:
            if it not in seq:
                seq.append(it)

    for cid in plan.components:
        c = comps[cid]
        add(plan.trees, c.get("trees", []))
        add(plan.nodes, c.get("nodes", []))
        add(plan.models, c.get("models", []))
        add(plan.manual, c.get("manual", []))
    plan.trees = [t for t in cat["trees"] if t in plan.trees]
    if plan.components:
        plan.tools.append("ffmpeg")
    return plan


# ------------------------------------------------------------------ paths
def _settings_path(root: Path) -> Path:
    return root / "tool" / "settings.yaml"


def read_settings(root: Path) -> dict:
    """tool/settings.yaml, or {} when there is none yet (or it is broken,
    which is reported)."""
    sp = _settings_path(root)
    if not sp.exists():
        return {}
    try:
        import yaml
        cfg = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:  # noqa: BLE001 - a broken settings file is reported, not fatal
        print(f"  [WARN] could not read {sp}: {e}")
        return {}


def extra_model_roots(root: Path) -> list[Path]:
    """The model folders the studio reads (tool/model_files.py): env vars,
    then settings paths.extra_model_dirs."""
    return mf.extra_roots(read_settings(root), root)


def find_model(root: Path, entry: dict, extra: list[Path]) -> Path | None:
    """Where a weight file already is: the studio tree first, then (a models/
    file) the same place in a model folder, where only a file of the
    catalog's size counts: the studio looks there the same way."""
    return mf.find(entry["path"], extra, root, entry.get("size"))


def model_state(root: Path, entry: dict, extra: list[Path]) -> tuple[str, Path | None]:
    """'ok', 'missing', 'wrong size' (the studio's own copy has another size)
    or 'letter case' (the studio's own copy is saved under another letter
    case, which an install fixes)."""
    p = find_model(root, entry, extra)
    if p is None:
        wrong = mf.case_mismatch(entry["path"], root)
        if wrong is not None:
            return "letter case", (root / entry["path"]).parent / wrong
        return "missing", None
    if entry.get("size") and p.stat().st_size != entry["size"]:
        return "wrong size", p
    return "ok", p


def tree_state(root: Path, tree: dict) -> str:
    main = root / tree["dir"] / "main.py"
    if not main.is_file():
        return "missing"
    ver = root / tree["dir"] / "comfyui_version.py"
    want = tree["core"]["version"]
    got = ""
    if ver.is_file():
        m = re.search(r'__version__\s*=\s*"([^"]+)"', ver.read_text(encoding="utf-8"))
        got = m.group(1) if m else ""
    return "ok" if got == want else f"version {got or '?'} (expected {want})"


def node_state(root: Path, cat: dict, nid: str) -> str:
    n = cat["nodes"][nid]
    d = root / cat["trees"][n["tree"]]["dir"] / "custom_nodes" / n["dir"]
    return "ok" if (d / n.get("marker", "__init__.py")).is_file() else "missing"


def venv_python(root: Path, tree: dict) -> Path:
    return root / tree["dir"] / "venv" / "Scripts" / "python.exe"


# Sits in a venv while pip fills it, so a run that stopped part-way is seen as
# unfinished (INSTALL-MAIN.bat uses the same name for tool\.venv).
INCOMPLETE = support.MARKER    # a venv pip has not finished (install_support.py)


def _dist_key(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def venv_base_exists(venv: Path) -> bool:
    """A venv runs the python named in its pyvenv.cfg ("home ="); once the
    Kinoloom folder has moved, that one is gone and the venv cannot start."""
    try:
        cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    m = re.search(r"^\s*home\s*=\s*(.+?)\s*$", cfg, re.M)
    return bool(m) and Path(m.group(1)).is_dir()


def venv_spec(tree: dict) -> dict:
    """The tree's venv list: its own, or its fallback build when this PC's card
    or driver cannot run its own but can run the fallback's (Qwen-Image 2.1:
    CUDA 13.0, else CUDA 12.6 for a GTX 900 or 10 card or a driver older than
    R580; tool/gpu_support.py)."""
    spec = tree.get("venv") or {}
    alt = spec.get("fallback")
    if alt and spec.get("cuda") and gpu_support.pick(spec["cuda"], alt["cuda"]) == alt["cuda"]:
        return alt
    return spec


def venv_state(root: Path, tree: dict) -> str:
    """'ok', 'missing', 'incomplete', 'outdated' or 'moved'. Incomplete: the
    marker is still there, or a package lacks its RECORD (pip writes it last,
    so an install that broke off leaves none). Outdated: a pin of the tree's
    list (venv_spec) is not installed: the list changed since (an update), or
    it was built for another card. Moved: made in another folder, so its
    python no longer starts."""
    venv = root / tree["dir"] / "venv"
    if not venv_python(root, tree).is_file():
        return "missing"
    if (venv / INCOMPLETE).exists():
        return "incomplete"
    if not venv_base_exists(venv):
        return "moved"
    have: dict[str, set[str]] = {}
    for d in (venv / "Lib" / "site-packages").glob("*.dist-info"):
        if not (d / "RECORD").is_file():
            return "incomplete"
        name, _, version = d.name[:-len(".dist-info")].partition("-")
        have.setdefault(_dist_key(name), set()).add(version)
    req = root / venv_spec(tree).get("requirements", "")
    if req.is_file():
        for line in req.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)==([^\s;#]+)", line)
            if not m:
                continue
            pin, got = m.group(2), have.get(_dist_key(m.group(1)), set())
            # a pin without a local label matches a build with one (PEP 440):
            # torchaudio==2.11.0 from the cu130 index installs 2.11.0+cu130
            if pin not in got and ("+" in pin or pin not in {v.split("+", 1)[0] for v in got}):
                return "outdated"
    return "ok"


def find_ffmpeg(root: Path) -> Path | None:
    p = root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if p.is_file():
        return p
    w = shutil.which("ffmpeg")
    return Path(w) if w else None


# ------------------------------------------------------------------ fetching
def sha256_of(path: Path, chunk: int = 1 << 23) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify(path: Path, size: int | None, sha256: str | None) -> str | None:
    """None when the file matches, else the reason."""
    if size and path.stat().st_size != size:
        return f"size {path.stat().st_size:,} != {size:,}"
    if sha256:
        got = sha256_of(path)
        if got != sha256:
            return f"sha256 {got[:16]}... != {sha256[:16]}..."
    return None


def no_answer(err: BaseException | None) -> BaseException | None:
    """The error in err's chain that says no server answered at all (the
    connection was refused, the name not found, no route, or it timed out),
    or None when one did, with an error of its own."""
    seen = 0
    while err is not None and seen < 10:
        if isinstance(err, urllib.error.HTTPError):
            return None
        e = err.reason if isinstance(err, urllib.error.URLError) else err
        if isinstance(e, BaseException) and (
                isinstance(e, (ConnectionRefusedError, socket.gaierror))
                or (isinstance(e, TimeoutError) and e is not err)   # urlopen's, not a read's
                or getattr(e, "winerror", None) in (10051, 10060, 10061, 10065)
                or type(e).__name__ in ("ConnectError", "ConnectTimeout")):     # httpx
            return e
        err, seen = err.__cause__ or err.__context__, seen + 1
    return None


def not_reached(host: str | None) -> None:
    """Refuse a download from a server that could not be reached earlier in
    this run: a PC that is offline gets its report in minutes, not hours."""
    if host and host in UNREACHABLE:
        raise InstallError(f"not tried: {host} could not be reached a moment ago ({UNREACHABLE[host]})")


def http_download(url: str, dest: Path, size: int | None = None, tries: int = 6) -> None:
    """Stream a URL to dest (via dest.part), with a progress line. A transfer
    that breaks off goes on where it stopped when the server allows it (HTTP
    Range), and each new try waits longer: 5, 10, 20, 40, then 60 s."""
    host = urllib.parse.urlparse(url).hostname
    not_reached(host)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    last = err = None
    for attempt in range(1, tries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {**UA, "Range": f"bytes={have}-"} if have else UA
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:        # the server sends the whole file again
                    have = 0
                length = r.headers.get("Content-Length")
                expected = have + int(length) if length else None
                total = size or expected or 0
                with open(part, "ab" if have else "wb") as fh:
                    done, shown = have, time.monotonic()
                    for block in iter(lambda: r.read(1 << 20), b""):
                        fh.write(block)
                        done += len(block)
                        if total and time.monotonic() - shown > 5:
                            print(f"      {done / total:6.1%}  {done / GB:.2f} / {total / GB:.2f} GB")
                            shown = time.monotonic()
                if expected is not None and done < expected:
                    # http.client ends a body the server cut short without an error
                    raise http.client.IncompleteRead(b"", expected - done)
            part.replace(dest)
            support.log(f"  downloaded {url}: {done:,} bytes"
                        + (f" in {attempt} tries" if attempt > 1 else ""))
            return
        except urllib.error.HTTPError as e:
            last, err = f"HTTP {e.code}", e
            if e.code == 416:                       # nothing left to resume: start over
                part.unlink(missing_ok=True)
            elif e.code in (401, 403, 404):
                break
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            last, err = f"{type(e).__name__}: {e}", e
        if attempt < tries:
            wait = min(60, 5 * 2 ** (attempt - 1))
            print(f"      the download failed ({last}); trying again in {wait} s")
            time.sleep(wait)
    part.unlink(missing_ok=True)
    if host and no_answer(err) is not None:
        UNREACHABLE[host] = last or "no answer"
    raise InstallError(f"download failed: {url} ({last})")


def hf_download(repo: str, repo_file: str, dest: Path, staging: Path) -> None:
    """A Hugging Face file straight into dest (staged on the same drive, so
    no second copy lands in the user cache); resumable."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(repo_file)}"
        http_download(url, dest)
        return
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    host = urllib.parse.urlparse(os.environ.get("HF_ENDPOINT") or "https://huggingface.co").hostname
    not_reached(host)
    try:
        got = Path(hf_hub_download(repo_id=repo, filename=repo_file, local_dir=staging))
    except Exception as e:  # noqa: BLE001 - every hub error is a failed source
        why = no_answer(e)
        if host and why is not None:    # the hub's own words say only "check your connection"
            UNREACHABLE[host] = f"{type(why).__name__}: {why}"
            raise InstallError(f"Hugging Face {repo}/{repo_file}: {host} could not be reached "
                               f"({UNREACHABLE[host]})") from e
        raise InstallError(f"Hugging Face {repo}/{repo_file}: {type(e).__name__}: {str(e)[:200]}") from e
    dest.parent.mkdir(parents=True, exist_ok=True)
    got.replace(dest)


def safe_extract(archive: Path, dest: Path) -> Path:
    """Extract a zip or tar archive into dest, refusing entries that would land
    outside it; returns the single top-level folder when there is one."""
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                target = (base / info.filename).resolve()
                if target != base and not target.is_relative_to(base):
                    raise InstallError(f"{archive.name}: unsafe path {info.filename!r}")
            z.extractall(base)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive) as t:
            t.extractall(base, filter="data")
    else:
        raise InstallError(f"unknown archive type: {archive.name}")
    entries = [p for p in base.iterdir() if p.name != "__MACOSX"]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else base


def copy_missing(src: Path, dst: Path) -> tuple[int, int]:
    """Copy every file from src that dst does not have yet; never overwrite.
    Returns (copied, kept)."""
    copied = kept = 0
    for f in src.rglob("*"):
        if f.is_dir() or ".git" in f.relative_to(src).parts:
            continue
        target = dst / f.relative_to(src)
        if target.exists():
            kept += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied += 1
    return copied, kept


def fetch_source(src: dict, work: Path) -> Path:
    """Download and unpack a pinned source; returns the folder with its files."""
    work.mkdir(parents=True, exist_ok=True)
    if src["type"] == "git":
        owner_repo = src["repo"]
        url = f"https://codeload.github.com/{owner_repo}/zip/{src['commit']}"
        archive = work / f"{owner_repo.replace('/', '_')}-{src['commit'][:12]}.zip"
    elif src["type"] == "archive":
        url = src["url"]
        archive = work / Path(urllib.parse.urlparse(url).path).name
    else:
        raise InstallError(f"unknown source type {src['type']!r}")
    http_download(url, archive)
    if src.get("sha256"):
        bad = verify(archive, src.get("size"), src["sha256"])
        if bad:
            archive.unlink(missing_ok=True)
            raise InstallError(f"{url}: {bad}")
    out = safe_extract(archive, work / "x")
    return out / src["subdir"] if src.get("subdir") else out


# ------------------------------------------------------------------ steps
def set_title(text: str) -> None:
    """The console window's title, which stays in view while pip scrolls."""
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except (AttributeError, OSError):
            pass


def duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s} s" if m else f"{s} s"


class Installer:
    def __init__(self, root: Path, cat: dict, dry_run: bool = False):
        self.root, self.cat, self.dry = root, cat, dry_run
        self.fails: list[str] = []
        self.failures: list[support.Failure] = []   # the same, for the report
        self.notes: list[str] = []
        self.added: list[Path] = []     # model folders a failed link was used through
        self.total = 0      # steps in the plan, for the progress in the title
        self.done = 0

    def step(self, label: str, fn, *args) -> bool:
        self.done += 1
        if self.total:
            set_title(f"Kinoloom install - {self.done} of {self.total}: {label}"
                      " - keep this window open")
        t0 = time.monotonic()
        try:
            fn(*args)
            return True
        except Exception as e:  # noqa: BLE001 - a failed part is reported, and the others go on
            expected = isinstance(e, (InstallError, OSError, subprocess.CalledProcessError,
                                      http.client.HTTPException))
            text = str(e) if expected else f"{type(e).__name__}: {e}"
            if not expected:    # a damaged archive, or a bug: its traceback is in the log
                support.log(f"  {label} stopped with an error:",
                            *traceback.format_exc().splitlines())
            self.fails.append(f"{label}: {text}")
            failure = getattr(e, "failure", None)
            if failure is None:
                cause, lines = support.classify(text)
                failure = support.Failure(label, cause, lines, [support.INSTALL_LOG])
            self.failures.append(failure)
            print(f"  FAIL {label}: {text}")
            return False
        finally:
            took = time.monotonic() - t0
            if took >= 30:      # say so after a long step, so the pace shows
                print(f"       took {duration(took)}")

    # -- ComfyUI cores
    def core(self, tid: str) -> None:
        tree = self.cat["trees"][tid]
        state = tree_state(self.root, tree)
        if state == "ok":
            print(f"  OK   {tree['dir']}: ComfyUI {tree['core']['version']}")
            return
        if state != "missing":
            # someone's own tree: report, never overwrite it
            self.notes.append(f"{tree['dir']}: {state}; left as it is")
            print(f"  --   {tree['dir']}: {state}; left as it is")
            return
        src = {"type": "git", "repo": tree["core"]["repo"], "commit": tree["core"]["commit"]}
        print(f"  ...  {tree['dir']}: ComfyUI {tree['core']['version']} "
              f"({tree['core']['repo']} {tree['core']['commit'][:12]})")
        if self.dry:
            return
        with tempfile.TemporaryDirectory(dir=self.root / "temp") as td:
            files = fetch_source(src, Path(td))
            copied, kept = copy_missing(files, self.root / tree["dir"])
        print(f"       {copied} files copied, {kept} of the repository's own files kept")
        if tree_state(self.root, tree) != "ok":
            raise InstallError(f"{tree['dir']}: version check failed after the copy")

    # -- custom node packs
    def node(self, nid: str) -> None:
        n = self.cat["nodes"][nid]
        tree = self.cat["trees"][n["tree"]]
        if node_state(self.root, self.cat, nid) == "ok":
            print(f"  OK   {tree['dir']}/custom_nodes/{n['dir']}")
            return
        src = n["source"]
        pin = src.get("version") or src.get("commit", "")[:12]
        print(f"  ...  {tree['dir']}/custom_nodes/{n['dir']} ({pin})")
        if self.dry:
            return
        with tempfile.TemporaryDirectory(dir=self.root / "temp") as td:
            files = fetch_source(src, Path(td))
            copy_missing(files, self.root / tree["dir"] / "custom_nodes" / n["dir"])
        if node_state(self.root, self.cat, nid) != "ok":
            raise InstallError(f"{n['dir']}: {n.get('marker', '__init__.py')} missing after the copy")

    # -- venvs
    def venv(self, tid: str) -> None:
        tree = self.cat["trees"][tid]
        if not tree.get("venv"):
            return
        spec = venv_spec(tree)
        py = venv_python(self.root, tree)
        venv = py.parent.parent
        state = venv_state(self.root, tree)
        if state == "ok":
            print(f"  OK   {tree['dir']}\\venv")
            return
        if state == "incomplete":
            print(f"  ...  {tree['dir']}\\venv was not finished last time; building it again")
        elif state == "outdated":
            print(f"  ...  {tree['dir']}\\venv has other packages than {Path(spec['requirements']).name} "
                  "(from an older Kinoloom, or for another graphics card); building it again")
        elif state == "moved":
            print(f"  ...  {tree['dir']}\\venv no longer starts (was the Kinoloom folder moved?); "
                  "building it again")
        print(f"  ...  {tree['dir']}\\venv (Python 3.12, {spec['requirements']}, {spec['index']})")
        if spec is not tree["venv"]:
            why = gpu_support.problem(tree["venv"]["cuda"], tree["title"])
            print(f"       {why[:1].upper()}{why[1:]}: it gets the CUDA "
                  f"{gpu_support.RUNTIMES[spec['cuda']]['cuda']} build instead")
        if self.dry:
            return
        print(f"       about {spec.get('size_gb', 0):.1f} GB of packages, torch among them: this "
              "takes several minutes, and pip's bars show its progress")
        # its marker goes in first and comes out last, so a run that stops while it
        # removes, creates or fills the venv leaves one marked unfinished
        support.remove_venv(venv)
        base = Path(getattr(sys, "_base_executable", sys.executable))
        subprocess.run([str(base), "-c", "import sys; assert sys.version_info[:2] == (3, 12)"],
                       check=True)
        venv.mkdir(parents=True)
        (venv / INCOMPLETE).write_text(support.MARKER_TEXT, encoding="utf-8")
        subprocess.run([str(base), "-m", "venv", str(venv)], check=True)
        # pip pinned, the list, one more try after a broken download, then torch
        # must import; pip's own log is logs/install/pip-<tree>.log
        fail = support.install_venv_packages(
            py, self.root / spec["requirements"], tree["dir"], step=f"{tree['dir']}\\venv",
            index=spec["index"], imports=("torch",))
        if fail:
            what = (fail.what or support.CAUSES[fail.cause][0]).rstrip(".")
            raise InstallError(f"{what} ({spec['requirements']}); the next run builds this "
                               "venv again", failure=fail)
        (venv / INCOMPLETE).unlink()
        relay(self.root, venv / "Lib" / "site-packages")

    # -- weights
    def model(self, mid: str, extra: list[Path]) -> None:
        e = self.cat["models"][mid]
        state, where = model_state(self.root, e, extra)
        if state == "letter case" and where is not None and not self.dry:
            mf.repair_case(self.root, {"models": {mid: e}}, lambda s: print(f"       {s}"))
            state, where = model_state(self.root, e, extra)
        if state == "ok":
            print(f"  OK   {e['path']}" + ("" if where == self.root / e["path"] else f"  (in {where})"))
            return
        if state == "letter case" and where is not None:
            if self.dry:
                print(f"  OK   {e['path']}  (saved as {where.name}: the install fixes the letter case)")
                return
            raise InstallError(f"{where} must be named {Path(e['path']).name} (the letter case "
                               "Kinoloom loads), and it could not be renamed: rename it, then re-run")
        if state == "wrong size" and where is not None:
            raise InstallError(f"{where} has the wrong size ({where.stat().st_size:,}, "
                               f"expected {e['size']:,}); delete or move it, then re-run")
        if e.get("derive"):
            self.derive(mid)
            return
        print(f"  ...  {e['path']}  ({e['size'] / GB:.2f} GB)")
        for other in mf.other_builds(e["path"], extra, e.get("size")):
            print(f"       {other} has this name but another size (another build of the "
                  "model): it stays as it is, and Kinoloom downloads its own copy")
        if self.dry:
            return
        dest = self.root / e["path"]
        problems = []
        for src in e["sources"]:
            try:
                if "hf" in src:
                    hf_download(src["hf"], src["file"], dest, self.root / "models" / ".downloads")
                else:
                    http_download(src["url"], dest, e["size"])
            except InstallError as err:
                problems.append(str(err))
                continue
            print("       checking sha256...")
            bad = verify(dest, e["size"], e.get("sha256"))
            if bad is None:
                return
            dest.unlink(missing_ok=True)
            problems.append(f"{src.get('hf') or src.get('url')}: {bad}")
        raise InstallError("; ".join(dict.fromkeys(problems)) or "no source")     # a server once

    def use_found(self, found: mf.Found, extra: list[Path]) -> None:
        """A file the user already has, linked (or, where no link works,
        copied) under Kinoloom's name; the user's file stays as it is. A link
        that does not work after all falls back to the file's model folder,
        else the download."""
        e = self.cat["models"][found.mid]
        if found.how in mf.LINKED:
            where = f"  (in {found.folder})" if found.how == "drivelink" else ""
            print(f"  LINK {found.rel}{where}\n       = {found.src}")
        else:
            print(f"  COPY {found.rel}  ({found.size / GB:.2f} GB)\n       from {found.src}")
        if self.dry:
            return
        try:
            mf.use(found, self.root, self.cat, print)
        except OSError as err:
            if found.how not in mf.LINKED:
                raise InstallError(f"could not copy {found.src}: {err}") from err
            # a link that did not work after all: its model folder used as it
            # is, else the download
            folder = mf.mount_root(found.src, found.rel)
            if folder is not None:
                print(f"       could not link it ({err}); using it where it is")
                print(f"       {register_extra_dir(self.root, folder)}")
                self.added.append(folder)
                return
            if e.get("sources"):
                print(f"       could not link it ({err}); downloading it instead")
                self.model(found.mid, extra)
                return
            raise InstallError(f"could not link {found.src} ({err}): put a copy of it at "
                               f"{self.root / found.rel}, or re-run") from err
        support.log(f"  {found.how}: {found.rel} <- {found.src} ({found.checked})")

    def derive(self, mid: str) -> None:
        """Make a file from another one (the face detector's ONNX export from
        its .pt), in a temporary folder, on the CPU, without auto-installs."""
        e = self.cat["models"][mid]
        d = e["derive"]
        src = self.cat["models"][d["from"]]
        print(f"  ...  {e['path']}  (exported from {Path(src['path']).name})")
        if self.dry:
            return
        src_path = self.root / src["path"]
        if not src_path.is_file():
            raise InstallError(f"needs {src['path']} first")
        dest = self.root / e["path"]
        code = ("import json, sys\n"
                "from ultralytics import YOLO\n"
                "out = YOLO(sys.argv[1]).export(device='cpu', **json.loads(sys.argv[2]))\n"
                "import onnxruntime as ort\n"
                "s = ort.InferenceSession(str(out), providers=['CPUExecutionProvider'])\n"
                "print(json.dumps({'path': str(out), 'input': s.get_inputs()[0].shape,"
                " 'output': [o.shape for o in s.get_outputs()]}))\n")
        env = {**os.environ, "YOLO_AUTOINSTALL": "false", "YOLO_OFFLINE": "true",
               "CUDA_VISIBLE_DEVICES": "-1"}
        with tempfile.TemporaryDirectory(dir=self.root / "temp") as td:
            work = Path(td) / src_path.name
            shutil.copy2(src_path, work)
            # the copy by its name in the work folder: ultralytics drops every '
            # from a model path, so a full path would break in a folder like D:\O'Neil
            r = subprocess.run([sys.executable, "-I", "-c", code, work.name, json.dumps(d["export"])],
                               cwd=td, env=env, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                said = (r.stderr or r.stdout).strip()
                support.log(f"  the export of {e['path']} failed (exit code {r.returncode}):", said)
                raise InstallError("the export failed: "
                                   + (said.splitlines()[-1] if said else f"exit code {r.returncode}"))
            got = json.loads(r.stdout.strip().splitlines()[-1])
            got["path"] = str(Path(td, got["path"]))
            want = d.get("expect") or {}
            if want and (got["input"] != want["input"] or got["output"] != [want["output"]]):
                raise InstallError(f"the export has input {got['input']} and output {got['output']}, "
                                   f"expected {want['input']} and {want['output']}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(got["path"], dest)
        print(f"       exported, input {got['input']}, output {got['output'][0]}")

    # -- ffmpeg
    def ffmpeg(self) -> None:
        found = find_ffmpeg(self.root)
        if found:
            print(f"  OK   ffmpeg ({found})")
            return
        t = self.cat["tools"]["ffmpeg"]
        print(f"  ...  ffmpeg {t['version']} -> tools\\ffmpeg ({t['size'] / GB * 1000:.0f} MB)")
        if self.dry:
            return
        with tempfile.TemporaryDirectory(dir=self.root / "temp") as td:
            files = fetch_source({"type": "archive", "url": t["url"], "size": t["size"],
                                  "sha256": t["sha256"]}, Path(td))
            copy_missing(files, self.root / "tools" / "ffmpeg")
        if not (self.root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe").is_file():
            raise InstallError("tools\\ffmpeg\\bin\\ffmpeg.exe missing after the copy")


def relay(root: Path, site_packages: Path) -> None:
    """Deploy the live-progress page into a venv's ComfyUI frontend package
    (install/overlay/README.md)."""
    static = site_packages / "comfyui_frontend_package" / "static"
    if static.is_dir():
        shutil.copy2(root / "install" / "overlay" / "progress_relay.html", static)


def register_extra_dir(root: Path, folder: Path, first: bool = False) -> str:
    """Add a folder to tool/settings.yaml paths.extra_model_dirs, keeping the
    file's comments (tool/model_files.py); first: before the others. Returns
    what happened."""
    try:
        return mf.add_dir_to_settings(root, folder, first)
    except Exception as e:  # noqa: BLE001 - a settings file that is not valid YAML, or locked
        raise InstallError(f"could not add {folder} to tool/settings.yaml: {e}") from e


# ------------------------------------------------------------------ reports
def component_status(root: Path, cat: dict, cid: str, extra: list[Path]) -> tuple[str, list[str]]:
    """('installed' | 'partly installed' | 'not installed', what is missing)."""
    c = cat["components"][cid]
    missing = []
    for tid in c.get("trees", []):
        t = cat["trees"][tid]
        if tree_state(root, t) != "ok":
            missing.append(f"{t['dir']} (ComfyUI {t['core']['version']})")
        elif t.get("venv") and (vs := venv_state(root, t)) != "ok":
            missing.append(f"{t['dir']}\\venv" + {"incomplete": " (not finished)",
                                                   "outdated": " (other packages than its list)",
                                                   "moved": " (made in another folder)"}.get(vs, ""))
    missing += [f"{cat['nodes'][n]['dir']} node pack" for n in c.get("nodes", [])
                if node_state(root, cat, n) != "ok"]
    for mid in c.get("models", []) + c.get("manual", []):
        state, where = model_state(root, cat["models"][mid], extra)
        if state == "letter case" and where is not None:
            missing.append(f"{cat['models'][mid]['path']} (saved as {where.name}: "
                           "an install or the next start fixes the letter case)")
        elif state != "ok":
            missing.append(f"{cat['models'][mid]['path']} ({state})")
    total = (len(c.get("trees", [])) + len(c.get("nodes", [])) + len(c.get("models", []))
             + len(c.get("manual", [])))
    if not missing:
        return "installed", []
    return ("not installed" if len(missing) >= total else "partly installed"), missing


def _gb(cat: dict, mids: list[str]) -> float:
    return sum(cat["models"][m].get("size") or 0 for m in mids) / GB


def print_components(root: Path, cat: dict, extra: list[Path]) -> None:
    for i, (cid, c) in enumerate(cat["components"].items(), 1):
        state, _ = component_status(root, cat, cid, extra)
        size = _gb(cat, c.get("models", []))
        dl = f"{size:5.1f} GB" if size else "    -   "
        mark = "*" if c.get("default") else " "
        print(f"  {i}{mark} {cid:15} {dl}  {c['title']}  [{state}]")
        print(f"       {c['summary']}")
        if c.get("manual"):
            bring = _gb(cat, c["manual"])
            print(f"       You bring {len(c['manual'])} file(s) yourself"
                  + (f" ({bring:.1f} GB)." if bring else "."))


MANUAL_WHY = {
    "h3": ("MiniMax H3",
           ["The MiniMax H3 Community License grants no use in the EU, the UK, South Korea",
            "or the USA, so the installer never downloads these. Where the license lets you",
            "use H3, get them from where they are published (below) and put them in the",
            "folders shown, or keep them where they are, under any name, and run the",
            "installer again: give it their folder when it asks (or --use-folder <folder>).",
            "Sizes are those of the published files the studio was tested with."]),
    "realvis": ("RealVisXL V5",
                ["I have no public copy of these four files to check against mine, so the",
                 "installer does not download them. Any RealVisXL V5 Q5_K_M GGUF, the SDXL",
                 "CLIP-L and CLIP-G encoders and an SDXL VAE saved under these names work;",
                 "until then the studio lists RealVisXL as not installed."]),
}


def print_manual(root: Path, cat: dict, plan: Plan, extra: list[Path]) -> None:
    if not plan.manual:
        return
    for cid in plan.components:
        mids = [m for m in cat["components"][cid].get("manual", []) if m in plan.manual]
        if not mids:
            continue
        name, why = MANUAL_WHY.get(cid, (cat["components"][cid]["title"], []))
        print(f"\n== Weights you bring yourself: {name} ==")
        for line in why:
            print(f"  {line}")
        for mid in mids:
            e = cat["models"][mid]
            state, where = model_state(root, e, extra)
            tag = "OK  " if state == "ok" else "--  "
            size = f"{e['size'] / GB:.2f} GB, " if e.get("size") else ""
            print(f"  {tag}{root / e['path']}")
            print(f"        ({size}{e['needed_by']})")
            if state == "ok" and where != root / e["path"]:
                print(f"        found in {where}")
            elif state == "wrong size":
                print(f"        found {where} with the wrong size")
            elif state == "letter case" and where is not None:
                print(f"        saved as {where.name}: an install or the next start fixes the letter case")
            else:
                for other in mf.other_builds(e["path"], extra, e.get("size")):
                    print(f"        {other} has another size: another build, not used")
                if e.get("published_at"):
                    print(f"        published at {e['published_at']}")


def studio_report(root: Path) -> None:
    """Which video tiers and image models the studio itself now sees."""
    tool = str(root / "tool")
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "import video_models as v, models as m;"
            "vs=[k for k in v.VIDEO_MODELS if v.model_status(k)['available']];"
            "ims=[k for k in m.MODELS if m.is_available(k)];"
            "print('  video tiers available: %d of %d' % (len(vs), len(v.VIDEO_MODELS)), ', '.join(vs));"
            "print('  image models available: %d of %d' % (len(ims), len(m.MODELS)), ', '.join(ims))")
    try:
        r = subprocess.run([sys.executable, "-c", code, tool], capture_output=True,
                           text=True, timeout=120, cwd=root)
        print(r.stdout.rstrip() or f"  (studio check failed: {r.stderr.strip()[-300:]})")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  (studio check failed: {e})")


# ------------------------------------------------------------------ your own files
CHECKED = {"sha256": "sha256 checked", "name and size": "same name and size",
           "name": "same name"}


def print_found(found: list[mf.Found]) -> bool:
    for f in found:
        print(f"  FOUND {f.rel}")
        print(f"        {f.src}  ({CHECKED[f.checked]})")
        print(f"        -> {f.describe()}")
    return bool(found)


def find_own_files(root: Path, cat: dict, finder: mf.Finder, extra: list[Path],
                   folders: list[str], ask: bool) -> None:
    """Look for the files the plan still needs among the files already on this
    PC, under any name: in the model folders, in Kinoloom's own models folder,
    in the folders given with --use-folder and, when ask is set, in the
    folders the user types."""
    auto = [p for p in [*extra, root / "models"] if p.is_dir()]
    if not (auto or folders or ask):
        return
    print("\n== Model files already on this PC ==")
    if auto:
        print_found(finder.search(auto))
    for f in folders:
        print_found(finder.search([Path(f).expanduser()]))
    if not ask or not finder.missing():
        return
    missing = [cat["models"][m] for m in finder.missing()]
    gb = sum(e.get("size") or 0 for e in missing) / GB
    print(f"\n  Kinoloom still needs {len(missing)} model file(s)"
          + (f" ({gb:.1f} GB)." if gb else "."))
    print("  If ComfyUI or another program already has some of them, under any name,")
    print("  Kinoloom can use those instead of downloading them, with no extra space:")
    print("  your files stay where they are, under their own names. Type or paste the")
    print("  folder they are in (its subfolders are searched too); Enter alone goes on.")
    if any(e.get("manual") for e in missing):
        print("  The files you bring yourself (MiniMax H3, RealVisXL) are found the same way.")
    prompt = "Folder: "
    while finder.missing():
        raw = mf.clean(input(prompt))
        prompt = "Another folder (Enter: go on): "
        if not raw:
            return
        if mf.is_network_path(raw):
            print("  A network folder (\\\\server\\share) is not searched: map it to a drive")
            print("  letter, or copy the files to this PC.")
            continue
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            print(f"  {folder} is not a folder on this PC")
            continue
        if not print_found(finder.search([folder])):
            print("  None of the files Kinoloom still needs are in that folder.")


def confirm_copies(finder: mf.Finder, root: Path, ask: bool) -> None:
    """Found files that no link can reach (a drive that cannot hold one, or a
    file ComfyUI reads only from Kinoloom's own folder, on another drive) get
    copied, if the user agrees; otherwise they are downloaded like the rest
    (or, the ones you bring yourself, stay missing)."""
    copies = [f for f in finder.found.values() if f.how == "copy"]
    if not copies:
        return
    gb = sum(f.size for f in copies) / GB
    if len(copies) == 1:
        print("\n  1 file found cannot be linked from where it is (the reason is above), so")
        print(f"  Kinoloom can use it only as a copy; copying it takes {gb:.1f} GB on {root.anchor}")
        q = "Copy it? (n: download it instead) [Y/n] "
    else:
        print(f"\n  {len(copies)} files found cannot be linked from where they are (the reasons are")
        print("  above), so Kinoloom can use them only as copies; copying them takes")
        print(f"  {gb:.1f} GB on {root.anchor}")
        q = "Copy them? (n: download them instead) [Y/n] "
    if os.name == "nt":
        it = "it" if len(copies) == 1 else "them"
        print(f"  (With Windows Developer Mode on, Kinoloom links {it} instead, with no extra")
        print("  space: Settings > System > For developers, then run this again.)")
    if ask and input(q).strip().lower() not in ("", "y", "yes"):
        for f in copies:
            del finder.found[f.mid]
        print("  Not copied: " + ("it is downloaded instead (a file you bring yourself stays missing)."
                                  if len(copies) == 1 else
                                  "they are downloaded instead (the files you bring yourself stay missing)."))


def add_model_folder(root: Path, folder: Path, dry_run: bool, first: bool = False) -> None:
    if dry_run:
        print(f"  ...  {folder} would become a model folder")
        return
    print(f"  {register_extra_dir(root, folder, first)}")


# ------------------------------------------------------------------ main
def windows_argv(argv: list[str]) -> list[str]:
    """A folder given in quotes with a backslash at the end ("D:\\models\\")
    reaches Python with the closing quote kept and the arguments after it run
    into it (Windows reads \\" as a quote): split them out again."""
    out: list[str] = []
    for a in argv:
        head, quote, rest = a.partition('"')
        if not quote:
            out.append(a)
            continue
        out.append(head + "\\" if head.endswith(":") else head)
        out.extend(rest.split())
    return out


def choose(cat: dict) -> list[str]:
    ids = list(cat["components"])
    default = [c for c in ids if cat["components"][c].get("default")]
    print("\nType the numbers or names of the parts to install, separated by commas.")
    print(f"Enter alone installs the ones marked * ({', '.join(default)}); 'all' installs everything.")
    raw = input("Install: ").strip().lower()
    if not raw:
        return default
    if raw == "all":
        return ids
    out = []
    for tok in re.split(r"[,\s]+", raw):
        if tok.isdigit() and 1 <= int(tok) <= len(ids):
            out.append(ids[int(tok) - 1])
        elif tok in ids:
            out.append(tok)
        elif tok:
            raise InstallError(f"not a component: {tok}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install Kinoloom components.")
    ap.add_argument("--components", help="comma-separated ids, or 'all' (default: ask)")
    ap.add_argument("--list", action="store_true", help="list the components and exit")
    ap.add_argument("--check", action="store_true", help="report what is installed; change nothing")
    ap.add_argument("--dry-run", action="store_true", help="show what would be installed")
    ap.add_argument("--yes", action="store_true",
                    help="no questions: accept the plan and the model licenses it lists")
    ap.add_argument("--use-folder", action="append", metavar="FOLDER",
                    help="a folder with model files you already have, under any name "
                         "(ComfyUI's models folder, say); give it more than once for more folders")
    ap.add_argument("--h3-dir", help="the same as --use-folder (older name, for MiniMax H3 weights)")
    ap.add_argument("--skip-models", action="store_true", help="install everything but the weights")
    args = ap.parse_args(windows_argv(sys.argv[1:]) if argv is None else argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:     # print each line as it comes, in step with pip's own output
        reconfigure(line_buffering=True, errors="replace")
    if args.list or args.check or args.dry_run:
        return run(args)
    # an install run: everything it prints also goes to logs/install/install.log
    support.keep_awake()
    support.no_quick_edit()
    tee = support.Tee(sys.stdout, support.INSTALL_LOG)
    sys.stdout = tee
    try:
        print(f"---- install_components.py {' '.join(sys.argv[1:] if argv is None else argv)}"
              f"  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
        return run(args)
    except KeyboardInterrupt:
        support.report([support.Failure("the parts of Kinoloom", "stopped")])
        return 1
    except Exception as e:  # noqa: BLE001 - every failure is reported, never a bare traceback
        support.log("  install_components.py stopped with an error:",
                    *traceback.format_exc().splitlines())
        cause, lines = support.classify(f"{type(e).__name__}: {e}")
        support.report([support.Failure("the parts of Kinoloom", cause, lines,
                                        [support.INSTALL_LOG])])
        return 1
    finally:
        sys.stdout = tee.stream
        tee.close_log()


def run(args: argparse.Namespace) -> int:
    cat = load_catalog()
    root = ROOT
    extra = extra_model_roots(root)
    print("== Kinoloom components ==")
    print(f"  studio: {root}")
    print(f"  weights go to {root / 'models'}\\<folder>\\\n")
    if not (args.list or args.check or args.dry_run):
        # a model file of Kinoloom's own saved under another letter case
        mf.repair_case(root, cat, lambda s: print(f"  {s}"))
    print_components(root, cat, extra)
    if args.list:
        return 0
    if args.check:
        for cid in cat["components"]:
            state, missing = component_status(root, cat, cid, extra)
            if missing:
                print(f"\n  {cid} [{state}] missing:")
                for m in missing:
                    print(f"    - {m}")
        print_manual(root, cat, resolve(cat, [c for c, v in cat["components"].items()
                                              if v.get("manual")]), extra)
        print()
        studio_report(root)
        return 0

    try:
        if args.components:
            sel = list(cat["components"]) if args.components.strip().lower() == "all" \
                else [c.strip() for c in args.components.split(",") if c.strip()]
        elif args.yes:
            sel = [c for c in cat["components"] if cat["components"][c].get("default")]
        else:
            sel = choose(cat)
        plan = resolve(cat, sel)
    except InstallError as e:
        print(f"[FAIL] {e}")
        return 2
    if not plan.components:
        print("Nothing selected.")
        return 0

    # files the user already has, anywhere on this PC and under any name
    folders = [mf.clean(f) for f in (args.use_folder or []) + ([args.h3_dir] if args.h3_dir else [])]
    for f in folders:
        if mf.is_network_path(f) or not Path(f).expanduser().is_dir():
            print(f"[FAIL] {f} is not a folder on this PC")
            return 2
    finder = mf.Finder(cat, [] if args.skip_models else [
        m for m in plan.models + plan.manual
        if model_state(root, cat["models"][m], extra)[0] == "missing"], root, say=print,
        remember=not args.dry_run)
    if finder.entries:
        ask = not (args.yes or args.dry_run)
        find_own_files(root, cat, finder, extra, folders, ask)
        confirm_copies(finder, root, ask)
    found = finder.found

    todo = [] if args.skip_models else [
        m for m in plan.models
        if m not in found and model_state(root, cat["models"][m], extra)[0] != "ok"]
    need = _gb(cat, todo) * GB
    copy_bytes = sum(f.size for f in found.values() if f.how == "copy")
    print(f"\n== Plan: {', '.join(plan.components)} ==")
    print(f"  ComfyUI trees: {', '.join(cat['trees'][t]['dir'] for t in plan.trees) or '-'}")
    print(f"  node packs:    {len(plan.nodes)}")
    print(f"  weights:       {len(todo)} file(s) to download, {need / GB:.1f} GB, into {root / 'models'}")
    link_dirs = sorted({str(f.folder) for f in found.values() if f.how == "drivelink"})
    if found:
        hows = [f.how for f in found.values()]
        linked = sum(h in mf.LINKED for h in hows)
        kinds = [f"{n} {word}" for n, word in (
            (linked, "linked (no extra space)"), (hows.count("folder"), "used where they are"),
            (hows.count("copy"), "copied")) if n]
        print(f"  your files:    {len(found)} found on this PC: {', '.join(kinds)}"
              + (f" ({copy_bytes / GB:.1f} GB)" if copy_bytes else ""))
        if link_dirs:
            print(f"                 links on other drives go in {', '.join(link_dirs)}")
    venvs = [cat["trees"][t] for t in plan.trees if cat["trees"][t].get("venv")
             and venv_state(root, cat["trees"][t]) != "ok"]
    venv_gb = sum(venv_spec(t).get("size_gb", 0) for t in venvs)
    if venvs:
        print(f"  venvs:         {', '.join(t['dir'] for t in venvs)}, about {venv_gb:.1f} GB")
    if plan.manual:
        where = "--check lists them" if args.skip_models else "listed at the end"
        print(f"  you bring:     {len(plan.manual)} file(s) yourself ({where})")
    free = shutil.disk_usage(root).free
    print(f"  free space:    {free / GB:.1f} GB on {root.anchor}")
    licenses = []
    for cid in plan.components:
        for lic in cat["components"][cid].get("licenses", []):
            if lic not in licenses:
                licenses.append(lic)
    if licenses:
        print("  The weights come under these licenses; read them before you use the models:")
        for lic in licenses:
            print(f"    - {lic}")
    if free < need * 1.05 + copy_bytes + (venv_gb + 5) * GB:
        print(f"  [WARN] {(need + copy_bytes) / GB:.0f} GB of weights and {venv_gb:.0f} GB of "
              f"venvs may not fit in {free / GB:.0f} GB")
    if not args.yes and not args.dry_run:
        if input("\nGo ahead? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("Stopped; nothing was changed.")
            return 0

    (root / "temp").mkdir(exist_ok=True)
    inst = Installer(root, cat, dry_run=args.dry_run)
    # the model folders that hold found files where they are, and the
    # Kinoloom-models folders with the links on other drives (those first:
    # their files are checked, and ComfyUI loads the first file of a name)
    added: list[Path] = []
    first: list[Path] = []
    for f in found.values():
        if f.how in ("folder", "drivelink") and f.folder is not None \
                and not any(mf.same_dir(f.folder, d, root) for d in added):
            added.append(f.folder)
            if f.how == "drivelink":
                first.append(f.folder)
    own_manual = [m for m in plan.manual if m in found and found[m].how != "folder"]
    # one step per tree, node pack, venv (a tree without one returns at once),
    # ffmpeg, weight file, model folder added and file you bring yourself
    inst.total = (2 * len(plan.trees) + len(plan.nodes) + 1
                  + (0 if args.skip_models else len(plan.models))
                  + len(added) + len(own_manual))
    if plan.trees:
        print("\n== ComfyUI ==")
        for t in plan.trees:
            if inst.step(cat["trees"][t]["dir"], inst.core, t):
                for n in [n for n in plan.nodes if cat["nodes"][n]["tree"] == t]:
                    inst.step(cat["nodes"][n]["dir"], inst.node, n)
                inst.step(f"{cat['trees'][t]['dir']}\\venv", inst.venv, t)
    print("\n== ffmpeg ==")
    inst.step("ffmpeg", inst.ffmpeg)
    added_ok: list[Path] = []
    if (plan.models or own_manual or added) and not args.skip_models:
        print("\n== Weights ==")
        for d in added:
            if inst.step(f"model folder {d}", add_model_folder, root, d, args.dry_run, d in first):
                added_ok.append(d)
        extra = extra_model_roots(root) + (added if args.dry_run else [])
        for m in plan.models:
            if m in found and found[m].how != "folder":
                inst.step(cat["models"][m]["path"], inst.use_found, found[m], extra)
            else:
                inst.step(cat["models"][m]["path"], inst.model, m, extra)
        for m in own_manual:
            inst.step(cat["models"][m]["path"], inst.use_found, found[m], extra)
        if inst.added:                          # a failed link used through its folder
            extra = extra_model_roots(root)
        staging = root / "models" / ".downloads"
        if not args.dry_run and staging.exists() and not any(
                p.is_file() and not p.name.endswith((".lock", ".metadata"))
                for p in staging.rglob("*") if ".cache" not in p.parts):
            shutil.rmtree(staging, ignore_errors=True)
    if not args.skip_models:                    # --check lists them on request
        print_manual(root, cat, plan, extra)

    print("\n== Result ==")
    if not args.dry_run:
        studio_report(root)
    print(f"  studio:        {root}")
    print(f"  weights:       {root / 'models'}\\<folder>\\  (post-processing files under {root / 'ComfyUI'})")
    print(f"  settings:      {_settings_path(root)}")
    for n in inst.notes:
        print(f"  [NOTE] {n}")
    if any(f.how in mf.LINKED for f in found.values()):
        print("  Linked files are your own files under a second name, Kinoloom's: they take")
        print("  no extra space, and your files keep their names and places.")
        if link_dirs:
            print(f"  The links on other drives are in {', '.join(link_dirs)}; deleting that")
            print("  folder never deletes your files.")
    if (added_ok or inst.added) and not args.dry_run:
        print("  Model folders were added: if Kinoloom is running, close it and start it")
        print("  again, so its render engines see them.")
    if inst.fails:
        print(f"  {len(inst.fails)} step(s) failed; re-run to retry them (finished steps are skipped):")
        for f in inst.fails:
            print(f"    FAIL {f}")
        support.report(inst.failures, "SOME PARTS DID NOT INSTALL")
        return 1
    print("  dry run: nothing was changed." if args.dry_run else
          "  Done. Start the studio with start.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
