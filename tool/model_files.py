"""Kinoloom's weight files: where each one is, and the copies a user already
has (from ComfyUI or another program, under any name) used instead of a
download.

install/components.json names every weight file the studio loads, with its
size and sha256. The studio finds a file

  * in its own folder, at the catalog path (models/... or ComfyUI/...);
  * for a models/ file, also in the model folders of settings
    paths.extra_model_dirs, at the same place below them. ComfyUI's older
    folder names count there too (unet/ for diffusion_models/, clip/ for
    text_encoders/: ComfyUI maps them onto the new ones). A file of another
    size in such a folder is another build of the model and does not count.

Copies kept anywhere else, under any name, are found by their size and
checked by their sha256; a file that also has the catalog name and size is
taken without the hash, as the installer always has. The user's file is
never renamed, moved or changed. Each one is then

  * linked: a hard link at the catalog path, when the file is on the drive
    the link goes to. That is the same file under a second name: it takes no
    extra space, and deleting either name keeps the other;
  * used where it is: its model folder goes into paths.extra_model_dirs, when
    the file sits there under its catalog name and place (another drive), in
    a folder that is a model folder (named models, or with the subfolders);
  * linked on its own drive (Windows, not a network share): a hard link in
    the Kinoloom-models folder at the top of that drive (a hard link cannot
    cross drives), which becomes a model folder, first in the list;
  * linked by a symbolic link at the catalog path, when no hard link can be
    made (an exFAT drive, say) and symbolic links are allowed (on Windows
    with Developer Mode, or to an administrator);
  * or copied, when none of these works and the user agrees.

install/install_components.py and the Settings page (routers/model_folders.py)
use this module. It imports no studio module: callers pass the studio root
and the settings, so the installer can use it before the studio runs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
GB = 1e9

# Model folders can also come from these (development setups); they come
# before the settings' folders, as they always have in tool/video_models.py.
ENV_VARS = ("KINOLOOM_EXTRA_MODEL_PATHS", "H3_LAB_MODELS", "LTX25_LAB_MODELS")
# ComfyUI's older folder names (folder_paths.map_legacy in all three trees): a
# model folder laid out the old way still has its files where ComfyUI looks.
LEGACY = {"diffusion_models": ("unet",), "text_encoders": ("clip",),
          "clip": ("text_encoders",)}
# The subfolders of each model folder that tool/launcher.py mounts on every
# ComfyUI instance: every folder a models/ entry of the catalog uses, and the
# older names above.
MOUNTED = ("checkpoints", "diffusion_models", "text_encoders", "vae", "loras",
           "upscale_models", "latent_upscale_models", "clip_vision",
           "ipadapter", "unet", "clip")

# where the search keeps what it learned, inside the (gitignored) weights folder
HASHES = Path("models") / ".kinoloom-hashes.json"
SOURCES = Path("models") / ".kinoloom-sources.json"
# folders the search never goes into: code, caches of packages, and the parts
# of a drive that hold Windows itself
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "site-packages"}
SKIP_AT_DRIVE_ROOT = {"windows", "program files", "program files (x86)", "programdata",
                      "system volume information", "$recycle.bin", "recovery"}
# files a download or copy leaves while it runs (pip, Hugging Face, browsers)
PARTIAL = (".part", ".incomplete", ".tmp", ".crdownload", ".download", ".partial")
# a catalog file without a size (RealVisXL) found by its name must be at least
# this big: a Git LFS pointer or an empty file under that name is not the model
MIN_BYTES = 1_000_000
# the folder at the top of another drive that holds Kinoloom's hard links to
# the user's files on that drive, and the note it keeps there
LINK_DIR = "Kinoloom-models"
# Only on Windows: there symbolic links need Developer Mode, and a hard link
# also outlives the file being moved or renamed on its drive. Elsewhere a
# symbolic link is the usual way, and every path's "drive" is /.
DRIVE_LINKS = os.name == "nt"
LINK_NOTE = """Kinoloom made this folder.

Each file in it is a hard link: the same file as one of your own model files
on this drive, under the name Kinoloom loads. A hard link takes no extra
space, and your own files keep their names and places.

Explorer shows these files at their full size, but the drive's free space
does not change. Deleting this folder does not delete your files: Kinoloom
then lists those models as missing until it looks for them again (Settings >
Model files, or install\\INSTALL-MAIN.bat mode [3]). The space of a file is
freed only when both names are deleted, yours and Kinoloom's.
"""

Say = Callable[[str], None]


# ------------------------------------------------------------------ folders
def clean(text: object) -> str:
    """A folder as typed or pasted: no surrounding spaces or quotes (Explorer's
    "Copy as path" adds them). Back- and forward slashes both work."""
    s = str(text or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def listed_dirs(raw: object) -> list[str]:
    """paths.extra_model_dirs as a list, however it was written: a list, a
    single folder as one string, or nothing."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [clean(d) for d in raw if isinstance(d, str) and clean(d)]


def resolve_dir(d: str | Path, root: Path = ROOT) -> Path:
    """A configured folder as an absolute path (relative = the studio root)."""
    p = Path(clean(d)).expanduser()
    return p if p.is_absolute() else root / p


def same_dir(a: str | Path, b: str | Path, root: Path = ROOT) -> bool:
    def norm(p: str | Path) -> str:
        return os.path.normcase(os.path.normpath(str(resolve_dir(p, root))))
    return norm(a) == norm(b)


def is_drive_root(p: Path) -> bool:
    """C:\\ or D:\\ itself (never a model folder: a whole drive would then be
    one, for every ComfyUI and for the files the API may read)."""
    p = Path(os.path.abspath(p))
    return p == Path(p.anchor)


def is_network_path(text: str) -> bool:
    return clean(text).replace("/", "\\").startswith("\\\\")


def on_network_drive(p: Path) -> bool:
    """p is on a network share: a \\\\server\\share path, or a drive letter
    mapped to one."""
    if is_network_path(str(p)):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(Path(p).anchor) == 4      # DRIVE_REMOTE
    except (AttributeError, OSError):
        return False


def extra_roots(cfg: dict | None, root: Path = ROOT) -> list[Path]:
    """Every model folder, in order: the environment variables, then settings
    paths.extra_model_dirs. Missing folders stay in the list; looking in them
    finds nothing."""
    dirs: list[Path] = []
    for var in ENV_VARS:
        for p in (os.environ.get(var) or "").split(os.pathsep):
            if p.strip() and Path(p.strip()) not in dirs:
                dirs.append(Path(p.strip()))
    for d in listed_dirs(((cfg or {}).get("paths") or {}).get("extra_model_dirs")):
        p = resolve_dir(d, root)
        if p not in dirs:
            dirs.append(p)
    return dirs


def tails(rel: str) -> list[str]:
    """Where a models/ file can sit below a model folder: its own place, and
    below ComfyUI's older name for that folder, in the order ComfyUI looks
    there (the order of MOUNTED). Nothing for other files."""
    if not rel.startswith("models/"):
        return []
    tail = rel[len("models/"):]
    first, _, rest = tail.partition("/")
    if not rest:
        return [tail]
    names = sorted([first, *LEGACY.get(first, ())],
                   key=lambda n: MOUNTED.index(n) if n in MOUNTED else len(MOUNTED))
    return [f"{n}/{rest}" for n in names]


def _exact_name(q: Path) -> bool:
    """q is there under exactly that name. ComfyUI lists file names as they
    are on disk, so a model saved in another letter case is not in its list."""
    try:
        return q.name in os.listdir(q.parent)
    except OSError:
        return False


def find(rel: str, extra: Iterable[Path], root: Path = ROOT,
         size: int | None = None) -> Path | None:
    """Where a catalog file is for ComfyUI, the way ComfyUI resolves it: it
    loads the first file that opens under that name (<root>/<rel>, then, a
    models/ file, the model folders in order and the subfolders of each in the
    order of MOUNTED; on Windows in any letter case), and its graph check wants
    the exact name in its list of files. So the first such file counts, with
    the catalog's size when it is in a model folder (another size is another
    build of the model, which ComfyUI would load), and only when some place
    has the file under its exact name. The studio's own copy counts whatever
    its size (the installer reports that one)."""
    own = root / rel
    places = [own] + [r / t for r in extra for t in tails(rel)]
    there = []
    for q in places:
        try:
            if q.is_file():
                there.append(q)
        except OSError:
            continue
    if not there:
        return None
    first = there[0]
    try:
        if first != own and size is not None and first.stat().st_size != size:
            return None
    except OSError:
        return None
    return first if any(_exact_name(q) for q in there) else None


def case_mismatch(rel: str, root: Path = ROOT) -> str | None:
    """The name the studio's own copy of a catalog file has on disk when it
    differs from the catalog name only in letter case (None otherwise)."""
    p = root / rel
    try:
        if not p.is_file() or _exact_name(p):
            return None
        return next((n for n in os.listdir(p.parent) if n.lower() == p.name.lower()), None)
    except OSError:
        return None


def repair_case(root: Path, cat: dict, say: Say | None = None) -> list[Path]:
    """Give the studio's own copy of a catalog file the catalog name when it
    was saved under another letter case: ComfyUI lists names as they are on
    disk, so the render graphs would not find it. Only inside Kinoloom's own
    folders (a user's files elsewhere are never renamed). Returns what it
    renamed."""
    fixed = []
    for e in cat.get("models", {}).values():
        p = root / e["path"]
        actual = case_mismatch(e["path"], root)
        if actual is None:
            continue
        try:
            os.rename(p.parent / actual, p)
            fixed.append(p)
            if say:
                say(f"renamed {p.parent / actual} to {p.name}, the letter case Kinoloom loads")
        except OSError as err:
            if say:
                say(f"could not rename {p.parent / actual} to {p.name}: {err.strerror or err}")
    return fixed


def other_builds(rel: str, extra: Iterable[Path], size: int | None) -> list[Path]:
    """Files in the model folders with the catalog name but another size."""
    out = []
    if not size:
        return out
    for r in extra:
        for t in tails(rel):
            q = r / t
            try:
                if q.is_file() and q.stat().st_size != size:
                    out.append(q)
            except OSError:
                continue
    return out


# ------------------------------------------------------------------ catalog
def load_catalog(root: Path = ROOT) -> dict:
    return json.loads((root / "install" / "components.json").read_text(encoding="utf-8"))


_SIZES: dict[str, dict[str, int]] = {}


def catalog_size(rel: str, root: Path = ROOT) -> int | None:
    """The catalog's size of a file, by its path (None when it has none)."""
    key = str(root)
    if key not in _SIZES:
        try:
            models = load_catalog(root)["models"].values()
            _SIZES[key] = {e["path"]: e["size"] for e in models if e.get("size")}
        except (OSError, ValueError, KeyError):
            _SIZES[key] = {}
    return _SIZES[key].get(rel)


def exists(rel: str, cfg: dict | None, root: Path = ROOT) -> bool:
    """Is a file there for the studio: its own copy, or the right build in a
    model folder."""
    rel = rel.replace("\\", "/")
    return find(rel, extra_roots(cfg, root), root, catalog_size(rel, root)) is not None


def looks_like_model_folder(folder: Path) -> bool:
    """A folder named models, or one with at least two of the subfolders
    ComfyUI reads (diffusion_models, vae, ...). Only such a folder is made a
    model folder by itself: a file at E:\\Users\\me\\vae\\x would otherwise
    make all of E:\\Users\\me one."""
    if folder.name.lower() == "models":
        return True
    try:
        with os.scandir(folder) as it:
            subs = {e.name.lower() for e in it if e.is_dir()}
    except OSError:
        return False
    return len(subs & set(MOUNTED)) >= 2


def mount_root(src: Path, rel: str) -> Path | None:
    """The model folder src sits in under its catalog name (in its letter case
    too: ComfyUI lists names as they are) and place (or the older ComfyUI name
    of that place), so that folder can be used as it is; None when it does
    not, when that would be a whole drive, or when the folder does not look
    like a model folder (looks_like_model_folder)."""
    if src.name != Path(rel).name:
        return None
    for t in tails(rel):
        parts = t.split("/")
        if len(src.parts) <= len(parts):
            continue
        if [p.lower() for p in src.parts[-len(parts):]] == [p.lower() for p in parts]:
            folder = Path(*src.parts[:-len(parts)])
            if not is_drive_root(folder) and looks_like_model_folder(folder):
                return folder
    return None


# ------------------------------------------------------------------ hashes
def duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s} s" if m else f"{s} s"


class HashCache:
    """sha256 of files already checked, by path, size and modification time,
    so a second search does not read them again. Also by the file's id where
    its drive gives one, so its other names (Kinoloom's hard links to it) are
    not read again either."""

    def __init__(self, path: Path | None):
        self.path, self.data, self.dirty = path, {}, False
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.data = {}

    @staticmethod
    def keys(p: Path) -> list[str]:
        try:
            st = os.stat(p)
        except OSError:
            return []
        tail = f"{st.st_size}|{st.st_mtime_ns}"
        keys = [f"{os.path.normcase(os.path.realpath(p))}|{tail}"]
        if st.st_ino:
            keys.append(f"id:{st.st_dev}:{st.st_ino}|{tail}")
        return keys

    def get(self, p: Path) -> str | None:
        return next((self.data[k] for k in self.keys(p) if k in self.data), None)

    def put(self, p: Path, sha: str) -> None:
        for k in self.keys(p):
            self.data[k] = sha
            self.dirty = True

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=0), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass                                   # only a cache


def sha256_file(p: Path, say: Say | None = None, label: str = "") -> str:
    """The sha256 of a file, with a progress line every few seconds."""
    h = hashlib.sha256()
    total = os.stat(p).st_size
    done, shown = 0, time.monotonic()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 23), b""):
            h.update(block)
            done += len(block)
            if say and total and time.monotonic() - shown > 5:
                say(f"      {label or p.name}: {done / total:6.1%}")
                shown = time.monotonic()
    return h.hexdigest()


# ------------------------------------------------------------------ search
LINKED = ("link", "drivelink", "symlink")       # the ways that take no extra space


def drive_link_dir(src: Path) -> Path:
    """The folder that holds Kinoloom's hard links to the files on src's drive:
    Kinoloom-models at the top of that drive."""
    return Path(Path(src).anchor) / LINK_DIR


@dataclass
class Found:
    """A user's file that is a catalog file, and how the studio will use it."""
    mid: str
    rel: str                      # the catalog path
    src: Path                     # the user's file
    size: int
    how: str                      # "link" | "folder" | "drivelink" | "symlink" | "copy"
    checked: str                  # "sha256" or "name and size"
    folder: Path | None = None    # "folder", "drivelink": the model folder to add

    def target(self, root: Path) -> Path:
        """Where Kinoloom's name for the file goes."""
        if self.how == "drivelink" and self.folder is not None:
            return self.folder / self.rel[len("models/"):]
        return root / self.rel

    def describe(self) -> str:
        return {"link": "linked: the same file under Kinoloom's name, no extra space",
                "folder": f"used where it is (model folder {self.folder})",
                "drivelink": f"linked on its own drive, in {self.folder}: the same file "
                             "under Kinoloom's name, no extra space",
                "symlink": "linked by a symbolic link: no extra space",
                "copy": f"copied ({self.size / GB:.1f} GB): "
                        + ("its drive cannot hold a link to it (an exFAT or network drive, say)"
                           if self.rel.startswith("models/") else
                           "ComfyUI reads it only from Kinoloom's own folder, and a link "
                           "cannot cross drives")}[self.how]


def _walk(top: Path, skip: set[str]) -> Iterator[tuple[str, int]]:
    """(path, size) of every file below top. Folder links and junctions are
    followed, each real folder once."""
    seen: set = set()
    stack = [str(top)]
    while stack:
        d = stack.pop()
        try:
            st = os.stat(d)
            key = (st.st_dev, st.st_ino) if st.st_ino else os.path.normcase(os.path.realpath(d))
            if key in seen:
                continue
            seen.add(key)
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        at_drive_root = is_drive_root(Path(d))
        for e in entries:
            try:
                if e.is_dir():
                    low = e.name.lower()
                    if low in SKIP_DIRS or (at_drive_root and low in SKIP_AT_DRIVE_ROOT) \
                            or os.path.normcase(e.path) in skip:
                        continue
                    stack.append(e.path)
                elif e.is_file() and not e.name.lower().endswith(PARTIAL):
                    yield e.path, e.stat().st_size
            except OSError:
                continue


class Finder:
    """Finds the catalog files `wanted` in the folders it is given."""

    def __init__(self, cat: dict, wanted: Iterable[str], root: Path = ROOT,
                 say: Say = print, remember: bool = True):
        self.root, self.say = root, say
        self.entries = {m: cat["models"][m] for m in wanted
                        if m in cat["models"] and not cat["models"][m].get("derive")}
        # remember=False (a dry run) reads the hash cache but writes nothing
        self.cache = HashCache(root / HASHES)
        if not remember:
            self.cache.path = None
        self.found: dict[str, Found] = {}
        self.searched: list[Path] = []
        self._sha: dict[str, str] = {}
        self._links: dict[tuple, bool] = {}
        self._symlinks: dict[int, bool] = {}
        # never searched: the installer's own download staging, and the files
        # at a catalog path (each is that catalog file, not another one)
        self._skip = {os.path.normcase(str(root / "models" / ".downloads"))}
        self._own = {os.path.normcase(str(root / e["path"])) for e in cat["models"].values()}

    def missing(self) -> list[str]:
        return [m for m in self.entries if m not in self.found]

    def search(self, folders: Iterable[Path | str]) -> list[Found]:
        """Look through the folders (and everything below them) for the files
        not found yet. Returns what this search found."""
        todo = {m: e for m, e in self.entries.items() if m not in self.found}
        if not todo:
            return []
        by_size: dict[int, list[str]] = {}
        by_name: dict[str, list[str]] = {}
        for m, e in todo.items():
            if e.get("size"):
                by_size.setdefault(int(e["size"]), []).append(m)
            else:                         # no size to go by: only its own name
                by_name.setdefault(Path(e["path"]).name.lower(), []).append(m)
        cands: dict[str, list[tuple[str, int]]] = {}
        for folder in folders:
            folder = Path(clean(folder))
            if not folder.is_dir():
                self.say(f"  {folder} is not a folder on this PC")
                continue
            if any(same_dir(folder, s, self.root) for s in self.searched):
                continue
            self.searched.append(folder)
            t0 = shown = time.monotonic()
            n = 0
            for path, size in _walk(folder, self._skip):
                n += 1
                if time.monotonic() - shown > 5:
                    self.say(f"      looked at {n:,} files so far")
                    shown = time.monotonic()
                if os.path.normcase(path) in self._own:
                    continue
                for m in by_size.get(size, ()):
                    cands.setdefault(m, []).append((path, size))
                for m in by_name.get(os.path.basename(path).lower(), ()):
                    cands.setdefault(m, []).append((path, size))
            self.say(f"  looked at {n:,} files in {folder} ({duration(time.monotonic() - t0)})")
        new = []
        for m, e in todo.items():
            pick = self._pick(e, cands.get(m, []))
            if pick:
                f = self._decide(m, e, *pick)
                self.found[m] = f
                new.append(f)
        self.cache.save()
        return new

    def _pick(self, e: dict, cands: list[tuple[str, int]]) -> tuple[str, int, str] | None:
        name = Path(e["path"]).name.lower()
        for path, size in cands:                  # its own name and size
            if os.path.basename(path).lower() == name and (e.get("size") or size >= MIN_BYTES):
                return path, size, "name and size" if e.get("size") else "name"
        if not e.get("sha256"):
            return None
        for path, size in cands:                  # any name: the sha256 decides
            try:
                if self._sha256(Path(path)) == e["sha256"]:
                    return path, size, "sha256"
            except OSError as err:                # locked, not allowed, or gone
                self._sha[os.path.normcase(path)] = ""
                self.say(f"    could not read {path} ({err.strerror or err}); skipped")
        return None

    def _sha256(self, p: Path) -> str:
        k = os.path.normcase(str(p))
        if k not in self._sha:
            got = self.cache.get(p)
            if got is None:
                size = os.stat(p).st_size
                self.say(f"    checking {p} ({size / GB:.2f} GB)...")
                t0 = time.monotonic()
                got = sha256_file(p, self.say, p.name)
                if time.monotonic() - t0 >= 30:
                    self.say(f"      took {duration(time.monotonic() - t0)}")
                self.cache.put(p, got)
            self._sha[k] = got
        return self._sha[k]

    def _decide(self, m: str, e: dict, path: str, size: int, checked: str) -> Found:
        # the file as it was found (resolving it all would turn a mapped network
        # drive into \\server\share); a symbolic link: the file it points to
        src = Path(os.path.abspath(path))
        if os.path.islink(path):
            try:
                src = Path(os.path.realpath(path))
            except OSError:
                pass
        rel = e["path"]
        if self.can_link(src, self.root / rel):
            return Found(m, rel, src, size, "link", checked)
        folder = mount_root(src, rel)
        if folder is not None:
            return Found(m, rel, src, size, "folder", checked, folder)
        # never a folder made at the top of a network share
        if DRIVE_LINKS and rel.startswith("models/") and not on_network_drive(src):
            ddir = drive_link_dir(src)
            if self.can_link(src, ddir / rel[len("models/"):]):
                return Found(m, rel, src, size, "drivelink", checked, ddir)
        if self.can_symlink(src, self.root / rel):
            return Found(m, rel, src, size, "symlink", checked)
        return Found(m, rel, src, size, "copy", checked)

    def can_link(self, src: Path, dst: Path) -> bool:
        """Can src get a hard link at dst? Tried once per pair of drives, with
        a link that is removed again, in the nearest folder of dst that exists.
        At the top of a drive (where Windows lets a user make folders, not
        files) the test runs in dst's first folder there, made for the test
        and removed again."""
        probe_dir = dst.parent
        while not probe_dir.is_dir() and probe_dir != probe_dir.parent:
            probe_dir = probe_dir.parent
        made = None
        if is_drive_root(probe_dir) and dst.parent != probe_dir:
            made = probe_dir / dst.relative_to(probe_dir).parts[0]
        try:
            key = (os.stat(src).st_dev, os.stat(probe_dir).st_dev, made is not None)
        except OSError:
            return False
        if key not in self._links:
            ok = False
            probe = (made or probe_dir) / f".kinoloom-link-test-{os.getpid()}-{len(self._links)}"
            try:
                if made is not None:
                    made.mkdir()
                os.link(src, probe)
                ok = True
            except OSError:
                pass
            finally:
                if ok:
                    try:
                        remove_name(probe)
                    except OSError as err:
                        self.say(f"    could not remove the link test {probe} ({err.strerror or err})")
                if made is not None:
                    try:
                        os.rmdir(made)                 # only when empty: never a folder in use
                    except OSError:
                        pass
            self._links[key] = ok
        return self._links[key]

    def can_symlink(self, src: Path, dst: Path) -> bool:
        """Can a symbolic link at dst point to src? Windows allows them with
        Developer Mode on, or to an administrator. Tried once per drive of dst,
        with a link that is removed again."""
        probe_dir = dst.parent
        while not probe_dir.is_dir() and probe_dir != probe_dir.parent:
            probe_dir = probe_dir.parent
        try:
            key = os.stat(probe_dir).st_dev
        except OSError:
            return False
        if key not in self._symlinks:
            probe = probe_dir / f".kinoloom-symlink-test-{os.getpid()}"
            ok = False
            try:
                os.symlink(src, probe)
                ok = True
                os.unlink(probe)
            except (OSError, NotImplementedError):
                pass
            self._symlinks[key] = ok
        return self._symlinks[key]


# ------------------------------------------------------------------ using them
def remove_name(p: Path) -> None:
    """Delete one name of a file, a hard link Kinoloom made. On Windows the
    read-only attribute is the file's, shared by all its names, and it stops
    any of them being deleted; this name goes without the attribute being
    touched, so the user's own file stays read-only."""
    try:
        os.unlink(p)
        return
    except PermissionError:
        if os.name != "nt":
            raise
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD]
    k32.SetFileInformationByHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # DELETE access, shared with everyone, the name itself (never a link's target)
    h = k32.CreateFileW(str(p), 0x00010000, 0x7, None, 3, 0x00200000, None)
    if h is None or h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # FileDispositionInfoEx: delete, POSIX semantics, ignore the read-only attribute
        flags = wintypes.DWORD(0x1 | 0x2 | 0x10)
        if not k32.SetFileInformationByHandle(h, 21, ctypes.byref(flags), ctypes.sizeof(flags)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


def link(src: Path, dst: Path) -> None:
    """A hard link at dst to src: the same file under a second name."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)


def copy_checked(src: Path, dst: Path, size: int | None, sha256: str | None,
                 say: Say | None = None) -> None:
    """Copy src to dst through dst.part, hashing what it reads; a copy of
    another size or sha256 is deleted, never used."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    total = os.stat(src).st_size
    done, shown = 0, time.monotonic()
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            for block in iter(lambda: fi.read(1 << 23), b""):
                fo.write(block)
                h.update(block)
                done += len(block)
                if say and total and time.monotonic() - shown > 5:
                    say(f"      {done / total:6.1%}  {done / GB:.2f} / {total / GB:.2f} GB")
                    shown = time.monotonic()
        if size and part.stat().st_size != size:
            raise OSError(f"the copy has {part.stat().st_size:,} bytes, not {size:,}")
        if sha256 and h.hexdigest() != sha256:
            raise OSError(f"the copy's sha256 {h.hexdigest()[:16]}... is not {sha256[:16]}...")
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


def sources(root: Path = ROOT) -> dict:
    """{catalog path: {"from", "how", "at"}} for the files linked or copied
    from a user's folder."""
    try:
        data = json.loads((root / SOURCES).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(root: Path, rel: str, src: Path, how: str, where: Path | None = None) -> None:
    data = sources(root)
    data[rel] = {"from": str(src), "how": how, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if where is not None:
        data[rel]["where"] = str(where)
    p = root / SOURCES
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass                                       # a note for the Settings page only


def _already(dst: Path, src: Path, size: int) -> bool:
    """dst is there already as src itself, or as a file of its size (a link or
    copy an earlier run made)."""
    try:
        return os.path.samefile(dst, src) or os.stat(dst).st_size == size
    except OSError:
        return False


def note_link_dir(folder: Path) -> None:
    """The note that says what a Kinoloom-models folder is (once)."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        note = folder / "README.txt"
        if not note.exists():
            note.write_text(LINK_NOTE, encoding="utf-8")
    except OSError:
        pass                                       # only a note


def use(found: Found, root: Path, cat: dict, say: Say | None = None) -> None:
    """Link or copy one found file to Kinoloom's name for it ("folder" ones
    are used through the settings, and the model folder of a "drivelink" is
    added there too: add_dir_to_settings / the Settings page). The user's file
    itself is never changed. A file already there as that file, or one of its
    size, is kept."""
    dst = found.target(root)
    e = cat["models"][found.mid]
    if os.path.islink(dst) and not os.path.exists(dst):
        dst.unlink()                              # Kinoloom's symbolic link to a file since moved
    if found.how in ("link", "drivelink"):
        if found.how == "drivelink" and found.folder is not None:
            note_link_dir(found.folder)
        try:
            link(found.src, dst)
        except FileExistsError:
            if not _already(dst, found.src, found.size):
                raise
    elif found.how == "symlink":
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(found.src, dst)
        except FileExistsError:
            if not _already(dst, found.src, found.size):
                raise
    elif found.how == "copy":
        if dst.exists():                          # put there since the search
            if _already(dst, found.src, found.size):
                record(root, found.rel, found.src, found.how, dst)
                return
            raise FileExistsError(f"{dst} is there already; it is not replaced")
        copy_checked(found.src, dst, e.get("size"), e.get("sha256"), say)
    else:
        return
    record(root, found.rel, found.src, found.how, dst)


# ------------------------------------------------------------------ settings
_PATHS = re.compile(r"^paths[ \t]*:[ \t]*(#.*)?$")
_KEY = re.compile(r"^(?P<indent>[ \t]+)extra_model_dirs[ \t]*:")


def _quoted(d: str) -> str:
    return "'" + d.replace("'", "''") + "'"


def _with_dirs(text: str, dirs: list[str]) -> str | None:
    """settings.yaml text with paths.extra_model_dirs set to dirs, every other
    line (comments too) as it was; None when the file is laid out in a way
    this does not handle."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if _PATHS.match(l.rstrip("\r\n"))), None)
    if start is None:
        return None
    nl = "\r\n" if lines[start].endswith("\r\n") else "\n"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
    for i in range(start + 1, end):
        m = _KEY.match(lines[i])
        if not m:
            continue
        ind = m.group("indent")
        j = i + 1
        while j < end:                            # the old value's lines
            s = lines[j].rstrip("\r\n")
            body = s.lstrip(" \t")
            lead = len(s) - len(body)
            if body and (lead > len(ind) or (lead == len(ind) and (body == "-" or body.startswith("- ")))):
                j += 1
                continue
            break
        new = ([f"{ind}extra_model_dirs:{nl}"] + [f"{ind}  - {_quoted(d)}{nl}" for d in dirs]
               if dirs else [f"{ind}extra_model_dirs: []{nl}"])
        return "".join(lines[:i] + new + lines[j:])
    child = next((l for l in lines[start + 1:end]
                  if l.strip() and not l.lstrip().startswith("#")), None)
    ind = child[:len(child) - len(child.lstrip(" \t"))] if child else "  "
    if not ind:
        return None
    new = ([f"{ind}extra_model_dirs:{nl}"] + [f"{ind}  - {_quoted(d)}{nl}" for d in dirs]
           if dirs else [f"{ind}extra_model_dirs: []{nl}"])
    return "".join(lines[:start + 1] + new + lines[start + 1:])


def _read_settings(root: Path) -> tuple[Path, str, dict]:
    """tool/settings.yaml (made from the example when there is none yet): its
    path, its text with its own line endings, and what it holds."""
    import yaml
    sp = root / "tool" / "settings.yaml"
    if not sp.exists():
        shutil.copy2(root / "tool" / "settings.example.yaml", sp)
    with open(sp, encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    cfg = yaml.safe_load(text) or {}
    if not isinstance(cfg, dict) or not isinstance(cfg.get("paths"), dict):
        raise ValueError(f"{sp} has no paths: section")
    return sp, text, cfg


def _write_dirs(sp: Path, text: str, cfg: dict, dirs: list[str]) -> None:
    """Write paths.extra_model_dirs as dirs, every other line as it was; the
    result is read back and must hold exactly the old settings with the new
    list (else the file is written out plainly, without its comments)."""
    import yaml
    want = copy.deepcopy(cfg)
    want["paths"]["extra_model_dirs"] = list(dirs)
    kept = _with_dirs(text, list(dirs))
    try:
        ok = kept is not None and yaml.safe_load(kept) == want
    except yaml.YAMLError:
        ok = False
    new = kept if ok and kept is not None else yaml.safe_dump(want, sort_keys=False,
                                                               allow_unicode=True)
    tmp = sp.with_name(sp.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
        fh.flush()
        os.fsync(fh.fileno())                     # on disk before it replaces the old file
    os.replace(tmp, sp)


def add_dir_to_settings(root: Path, folder: Path, first: bool = False) -> str:
    """Add a folder to tool/settings.yaml paths.extra_model_dirs, keeping the
    file's comments and layout; first puts it before the others (a
    Kinoloom-models folder: ComfyUI loads the first file of a name, and the
    files there are checked). Returns what happened."""
    sp, text, cfg = _read_settings(root)
    have = listed_dirs(cfg["paths"].get("extra_model_dirs"))
    entry = Path(os.path.abspath(folder)).as_posix()
    if any(same_dir(h, entry, root) for h in have):
        return f"{entry} is already one of the model folders"
    _write_dirs(sp, text, cfg, [entry] + have if first else have + [entry])
    return f"added {entry} to the model folders (tool/settings.yaml paths.extra_model_dirs)"


def set_dirs_in_settings(root: Path, dirs: list[str]) -> None:
    """Set tool/settings.yaml paths.extra_model_dirs to dirs, keeping the
    file's comments and layout (the Settings page adds and removes folders
    this way)."""
    sp, text, cfg = _read_settings(root)
    _write_dirs(sp, text, cfg, dirs)


# ------------------------------------------------------------------ report
def status(cat: dict, cfg: dict | None, root: Path = ROOT) -> list[dict]:
    """One row per catalog file: where it is and how the studio has it."""
    extra = extra_roots(cfg, root)
    linked = sources(root)
    rows: dict[str, dict] = {}
    for comp in cat["components"].values():
        for m in comp.get("models", []) + comp.get("manual", []):
            if m in rows:
                rows[m]["parts"].append(comp["title"])
                continue
            e = cat["models"][m]
            rel = e["path"]
            row = {"id": m, "file": Path(rel).name, "path": rel, "size": e.get("size"),
                   "parts": [comp["title"]], "manual": bool(e.get("manual")),
                   "made_here": bool(e.get("derive")), "state": "missing",
                   "where": None, "how": None, "from": None, "other_builds": []}
            own = root / rel
            p = find(rel, extra, root, e.get("size"))
            src = linked.get(rel) or {}
            if p is not None and p == own:
                row["where"] = str(own)
                if e.get("size") and own.stat().st_size != e["size"]:
                    row["state"] = "other build"
                else:
                    row["state"], row["how"] = "ok", "own"
                    still = (src.get("how") == "copy"
                             or (src.get("how") == "symlink" and os.path.islink(own))
                             or (src.get("how") == "link" and os.stat(own).st_nlink > 1))
                    if still:
                        row["how"], row["from"] = src["how"], src.get("from")
            elif p is not None:
                row.update(state="ok", how="folder", where=str(p))
                if src.get("how") == "drivelink" and _same_file(p, src.get("where")):
                    row["how"], row["from"] = "drivelink", src.get("from")
            elif (wrong := case_mismatch(rel, root)) is not None:
                row.update(state="letter case", where=str(own.parent / wrong))
            else:
                row["other_builds"] = [str(q) for q in other_builds(rel, extra, e.get("size"))]
            rows[m] = row
    return list(rows.values())


def _same_file(a: Path | str | None, b: Path | str | None) -> bool:
    try:
        return a is not None and b is not None and os.path.samefile(a, b)
    except OSError:
        return False
