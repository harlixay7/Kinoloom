"""Install the parts of Kinoloom you choose: the ComfyUI trees, their node
packs and venvs, the model weights, and ffmpeg.

  python install/install_components.py                  # menu
  python install/install_components.py --list           # what can be installed
  python install/install_components.py --check          # report only, change nothing
  python install/install_components.py --components ltx25,qwen21 --yes
  python install/install_components.py --components all --dry-run
  python install/install_components.py --h3-dir D:\\H3\\models   # H3 weights you keep elsewhere

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
bring, where they are published and where they go; --h3-dir registers a
folder that already holds H3 weights (paths.extra_model_dirs).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CATALOG = HERE / "components.json"
UA = {"User-Agent": "kinoloom-installer"}
GB = 1e9


class InstallError(RuntimeError):
    pass


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


def extra_model_roots(root: Path) -> list[Path]:
    """The extra model roots the studio reads (tool/video_models.py
    _get_extra_model_dirs): env vars, then settings paths.extra_model_dirs."""
    roots: list[Path] = []
    for var in ("KINOLOOM_EXTRA_MODEL_PATHS", "H3_LAB_MODELS", "LTX25_LAB_MODELS"):
        for p in (os.environ.get(var) or "").split(os.pathsep):
            if p.strip() and Path(p.strip()) not in roots:
                roots.append(Path(p.strip()))
    sp = _settings_path(root)
    if sp.exists():
        try:
            import yaml
            cfg = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            raw = (cfg.get("paths") or {}).get("extra_model_dirs") or []
            for p in [raw] if isinstance(raw, str) else raw:
                if isinstance(p, str) and p.strip():
                    q = Path(p.strip())
                    q = q if q.is_absolute() else root / q
                    if q not in roots:
                        roots.append(q)
        except Exception as e:  # noqa: BLE001 - a broken settings file is reported, not fatal
            print(f"  [WARN] could not read {sp}: {e}")
    return roots


def find_model(root: Path, entry: dict, extra: list[Path]) -> Path | None:
    """Where a weight file already is: the studio tree first, then (for files
    the video registry also finds there) the extra model roots."""
    p = root / entry["path"]
    if p.is_file():
        return p
    if entry.get("extra_roots_ok") and entry["path"].startswith("models/"):
        tail = entry["path"][len("models/"):]
        for r in extra:
            q = r / tail
            if q.is_file():
                return q
    return None


def model_state(root: Path, entry: dict, extra: list[Path]) -> tuple[str, Path | None]:
    """'ok', 'missing' or 'wrong size'."""
    p = find_model(root, entry, extra)
    if p is None:
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


def http_download(url: str, dest: Path, size: int | None = None, tries: int = 3) -> None:
    """Stream a URL to dest (via dest.part), with a progress line."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as fh:
                total = size or int(r.headers.get("Content-Length") or 0)
                done, shown = 0, time.monotonic()
                for block in iter(lambda: r.read(1 << 20), b""):
                    fh.write(block)
                    done += len(block)
                    if total and time.monotonic() - shown > 5:
                        print(f"      {done / total:6.1%}  {done / GB:.2f} / {total / GB:.2f} GB")
                        shown = time.monotonic()
            part.replace(dest)
            return
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403, 404):
                break
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = f"{type(e).__name__}: {e}"
        part.unlink(missing_ok=True)
        if attempt < tries:
            time.sleep(3 * attempt)
    part.unlink(missing_ok=True)
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
    try:
        got = Path(hf_hub_download(repo_id=repo, filename=repo_file, local_dir=staging))
    except Exception as e:  # noqa: BLE001 - every hub error is a failed source
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
        bad = verify(archive, None, src["sha256"])
        if bad:
            archive.unlink(missing_ok=True)
            raise InstallError(f"{url}: {bad}")
    out = safe_extract(archive, work / "x")
    return out / src["subdir"] if src.get("subdir") else out


# ------------------------------------------------------------------ steps
class Installer:
    def __init__(self, root: Path, cat: dict, dry_run: bool = False):
        self.root, self.cat, self.dry = root, cat, dry_run
        self.fails: list[str] = []
        self.notes: list[str] = []

    def step(self, label: str, fn, *args) -> bool:
        try:
            fn(*args)
            return True
        except (InstallError, OSError, subprocess.CalledProcessError) as e:
            self.fails.append(f"{label}: {e}")
            print(f"  FAIL {label}: {e}")
            return False

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
        spec = tree.get("venv")
        if not spec:
            return
        py = venv_python(self.root, tree)
        if py.is_file():
            print(f"  OK   {tree['dir']}\\venv")
            return
        print(f"  ...  {tree['dir']}\\venv (Python 3.12, {spec['requirements']}, {spec['index']})")
        if self.dry:
            return
        base = Path(getattr(sys, "_base_executable", sys.executable))
        subprocess.run([str(base), "-c", "import sys; assert sys.version_info[:2] == (3, 12)"],
                       check=True)
        subprocess.run([str(base), "-m", "venv", str(py.parent.parent)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        cmd = [str(py), "-m", "pip", "install", "--extra-index-url", spec["index"], "-r"]
        r = subprocess.run(cmd + [str(self.root / spec["requirements"])])
        if r.returncode != 0:
            fallback = self.root / tree["dir"] / spec["fallback"]
            print(f"  [WARN] the pinned list did not install; trying {fallback}")
            self.notes.append(f"{tree['dir']}\\venv: installed from {spec['fallback']}, "
                              "not the pinned list")
            subprocess.run(cmd + [str(fallback)], check=True)
        relay(self.root, py.parent.parent / "Lib" / "site-packages")

    # -- weights
    def model(self, mid: str, extra: list[Path]) -> None:
        e = self.cat["models"][mid]
        state, where = model_state(self.root, e, extra)
        if state == "ok":
            print(f"  OK   {e['path']}" + ("" if where == self.root / e["path"] else f"  (in {where})"))
            return
        if state == "wrong size" and where is not None:
            raise InstallError(f"{where} has the wrong size ({where.stat().st_size:,}, "
                               f"expected {e['size']:,}); delete or move it, then re-run")
        if e.get("derive"):
            self.derive(mid)
            return
        print(f"  ...  {e['path']}  ({e['size'] / GB:.2f} GB)")
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
        raise InstallError("; ".join(problems) or "no source")

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
            r = subprocess.run([sys.executable, "-I", "-c", code, str(work), json.dumps(d["export"])],
                               cwd=td, env=env, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise InstallError(f"the export failed: {(r.stderr or r.stdout).strip()[-400:]}")
            got = json.loads(r.stdout.strip().splitlines()[-1])
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
            files = fetch_source({"type": "archive", "url": t["url"], "sha256": t["sha256"]}, Path(td))
            copy_missing(files, self.root / "tools" / "ffmpeg")
        if not (self.root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe").is_file():
            raise InstallError("tools\\ffmpeg\\bin\\ffmpeg.exe missing after the copy")


def relay(root: Path, site_packages: Path) -> None:
    """Deploy the live-progress page into a venv's ComfyUI frontend package
    (install/overlay/README.md)."""
    static = site_packages / "comfyui_frontend_package" / "static"
    if static.is_dir():
        shutil.copy2(root / "install" / "overlay" / "progress_relay.html", static)


def register_extra_dir(root: Path, folder: Path) -> str:
    """Add a folder to tool/settings.yaml paths.extra_model_dirs, keeping the
    file's comments. Returns what happened."""
    sp = _settings_path(root)
    if not sp.exists():
        shutil.copy2(root / "tool" / "settings.example.yaml", sp)
    text = sp.read_text(encoding="utf-8")
    entry = folder.as_posix()
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(r"^(\s+)extra_model_dirs:\s*(\[\s*\])?\s*(#.*)?$", line.rstrip("\r\n"))
        if not m:
            continue
        indent = m.group(1)
        nl = "\r\n" if line.endswith("\r\n") else "\n"
        j = i + 1
        items = []
        while j < len(lines) and re.match(rf"^{indent}\s+-\s", lines[j]):
            items.append(lines[j].split("-", 1)[1].strip().strip("'\""))
            j += 1
        if any(Path(x) == folder for x in items):
            return f"{entry} is already in paths.extra_model_dirs"
        new_item = f"{indent}  - {entry}{nl}"
        if m.group(2) is not None:
            lines[i] = f"{indent}extra_model_dirs:{nl}"
        lines.insert(j, new_item)
        sp.write_text("".join(lines), encoding="utf-8")
        return f"added {entry} to paths.extra_model_dirs in tool/settings.yaml"
    raise InstallError("tool/settings.yaml has no paths.extra_model_dirs line; add "
                       f"'{entry}' to it by hand")


# ------------------------------------------------------------------ reports
def component_status(root: Path, cat: dict, cid: str, extra: list[Path]) -> tuple[str, list[str]]:
    """('installed' | 'partly installed' | 'not installed', what is missing)."""
    c = cat["components"][cid]
    missing = []
    for tid in c.get("trees", []):
        t = cat["trees"][tid]
        if tree_state(root, t) != "ok":
            missing.append(f"{t['dir']} (ComfyUI {t['core']['version']})")
        elif t.get("venv") and not venv_python(root, t).is_file():
            missing.append(f"{t['dir']}\\venv")
    missing += [f"{cat['nodes'][n]['dir']} node pack" for n in c.get("nodes", [])
                if node_state(root, cat, n) != "ok"]
    for mid in c.get("models", []) + c.get("manual", []):
        state, _ = model_state(root, cat["models"][mid], extra)
        if state != "ok":
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
            "folders shown, or keep them in one folder with the same subfolders and run",
            "the installer again with --h3-dir <that folder>. Sizes are those of the",
            "published files the studio was tested with."]),
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
            elif e.get("published_at"):
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


# ------------------------------------------------------------------ main
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
    ap.add_argument("--h3-dir", help="a folder that already holds MiniMax H3 weights "
                                     "(subfolders diffusion_models, text_encoders, vae, ...)")
    ap.add_argument("--skip-models", action="store_true", help="install everything but the weights")
    args = ap.parse_args(argv)

    cat = load_catalog()
    root = ROOT
    extra = extra_model_roots(root)
    print("== Kinoloom components ==")
    print(f"  studio: {root}")
    print(f"  weights go to {root / 'models'}\\<folder>\\\n")
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

    h3_missing = "h3" in plan.components and any(
        model_state(root, cat["models"][m], extra)[0] != "ok" for m in cat["components"]["h3"]["manual"])
    if h3_missing and not (args.h3_dir or args.yes or args.dry_run):
        print("\nMiniMax H3 weights are never downloaded (see its license). If you already have")
        print("them in a folder with the subfolders diffusion_models, text_encoders, vae,")
        print("latent_upscale_models and loras, type that folder; Enter skips this, and you")
        print(f"can put the files under {root / 'models'} later.")
        args.h3_dir = input("H3 folder: ").strip().strip('"') or None
    if args.h3_dir and not args.dry_run:
        h3 = Path(args.h3_dir).expanduser().resolve()
        if not h3.is_dir():
            print(f"[FAIL] --h3-dir {h3} is not a folder")
            return 2
        print(f"  {register_extra_dir(root, h3)}")
        extra = extra_model_roots(root)

    todo = [] if args.skip_models else [
        m for m in plan.models if model_state(root, cat["models"][m], extra)[0] != "ok"]
    need = _gb(cat, todo) * GB
    print(f"\n== Plan: {', '.join(plan.components)} ==")
    print(f"  ComfyUI trees: {', '.join(cat['trees'][t]['dir'] for t in plan.trees) or '-'}")
    print(f"  node packs:    {len(plan.nodes)}")
    print(f"  weights:       {len(todo)} file(s) to download, {need / GB:.1f} GB, into {root / 'models'}")
    venvs = [cat["trees"][t] for t in plan.trees if cat["trees"][t].get("venv")
             and not venv_python(root, cat["trees"][t]).is_file()]
    venv_gb = sum(t["venv"].get("size_gb", 0) for t in venvs)
    if venvs:
        print(f"  venvs:         {', '.join(t['dir'] for t in venvs)}, about {venv_gb:.1f} GB")
    if plan.manual:
        print(f"  you bring:     {len(plan.manual)} file(s) yourself (listed at the end)")
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
    if free < need * 1.05 + (venv_gb + 5) * GB:
        print(f"  [WARN] {need / GB:.0f} GB of weights and {venv_gb:.0f} GB of venvs may not fit "
              f"in {free / GB:.0f} GB")
    if not args.yes and not args.dry_run:
        if input("\nGo ahead? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("Stopped; nothing was changed.")
            return 0

    (root / "temp").mkdir(exist_ok=True)
    inst = Installer(root, cat, dry_run=args.dry_run)
    if plan.trees:
        print("\n== ComfyUI ==")
        for t in plan.trees:
            if inst.step(cat["trees"][t]["dir"], inst.core, t):
                for n in [n for n in plan.nodes if cat["nodes"][n]["tree"] == t]:
                    inst.step(cat["nodes"][n]["dir"], inst.node, n)
                inst.step(f"{cat['trees'][t]['dir']}\\venv", inst.venv, t)
    print("\n== ffmpeg ==")
    inst.step("ffmpeg", inst.ffmpeg)
    if plan.models and not args.skip_models:
        print("\n== Weights ==")
        for m in plan.models:
            inst.step(cat["models"][m]["path"], inst.model, m, extra)
        staging = root / "models" / ".downloads"
        if not args.dry_run and staging.exists() and not any(
                p.is_file() and not p.name.endswith((".lock", ".metadata"))
                for p in staging.rglob("*") if ".cache" not in p.parts):
            shutil.rmtree(staging, ignore_errors=True)
    print_manual(root, cat, plan, extra)

    print("\n== Result ==")
    if not args.dry_run:
        studio_report(root)
    print(f"  studio:        {root}")
    print(f"  weights:       {root / 'models'}\\<folder>\\  (post-processing files under {root / 'ComfyUI'})")
    print(f"  settings:      {_settings_path(root)}")
    for n in inst.notes:
        print(f"  [NOTE] {n}")
    if inst.fails:
        print(f"  {len(inst.fails)} step(s) failed; re-run to retry them (finished steps are skipped):")
        for f in inst.fails:
            print(f"    FAIL {f}")
        return 1
    print("  dry run: nothing was changed." if args.dry_run else
          "  Done. Start the studio with start.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
