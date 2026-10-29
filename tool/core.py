"""Core shared infrastructure for the Kinoloom tool."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

import model_files

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
(ROOT / "temp").mkdir(exist_ok=True)
(ROOT / "output").mkdir(exist_ok=True)
(ROOT / "shots").mkdir(exist_ok=True)

_CFG: dict[str, Any] | None = None
_CFG_LOCK = threading.RLock()


class InvalidInput(ValueError):
    """A client-supplied value failed validation. api.py maps it to HTTP 400
    (a bare ValueError stays a 500: it usually means a bug)."""


def log_setup(name: str = "studio") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(LOG_DIR / f"{name}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger


def load_config() -> dict[str, Any]:
    """The process-wide settings dict (tool/settings.yaml, created from
    settings.example.yaml on first run).

    Every caller gets the SAME dict object, and save_config() updates it in
    place, so modules that captured a reference at import time
    (``from routers.common import cfg``) always see the current settings."""
    global _CFG
    with _CFG_LOCK:
        if _CFG is None:
            cfg_path = ROOT / "tool" / "settings.yaml"
            if not cfg_path.exists():
                example_path = ROOT / "tool" / "settings.example.yaml"
                if example_path.exists():
                    try:
                        shutil.copyfile(example_path, cfg_path)
                    except Exception:
                        cfg_path = example_path
                else:
                    cfg_path = example_path
            with open(cfg_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if not isinstance(loaded, dict):
                raise ValueError(f"{cfg_path} must contain a YAML mapping")
            _CFG = loaded
        return _CFG


def save_config(cfg: dict[str, Any]) -> None:
    """Persist ``cfg`` atomically and make it the live configuration.

    If ``cfg`` is not the live dict itself, the live dict is updated in place
    (never rebound) so existing references stay valid."""
    global _CFG
    if not isinstance(cfg, dict):
        raise TypeError("settings must be a mapping")
    path = ROOT / "tool" / "settings.yaml"
    text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    with _CFG_LOCK:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if _CFG is None:
            _CFG = cfg
        elif cfg is not _CFG:
            snapshot = dict(cfg)
            _CFG.clear()
            _CFG.update(snapshot)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def jload(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsave(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_safe_component(s: str | None) -> bool:
    """Validate a single path component (dataset type/name, id_name, ...):
    no separators, no traversal, no control chars."""
    if s is None:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}", s))


def tool_path(value: str | Path | None) -> Path | None:
    """A configured executable/file path (``paths.ffmpeg``, ``paths.python``)
    resolved against the studio root when relative. The server runs with
    cwd = tool/, so a relative value would otherwise resolve to the wrong
    folder."""
    if not value:
        return None
    p = Path(str(value))
    return p if p.is_absolute() else ROOT / p


def ffmpeg_exe(cfg: dict[str, Any] | None = None) -> str:
    """The ffmpeg binary: settings ``paths.ffmpeg`` (root-relative allowed),
    else the bundled tools/ffmpeg build, else ``ffmpeg`` on PATH."""
    cfg = cfg if cfg is not None else load_config()
    p = tool_path((cfg.get("paths") or {}).get("ffmpeg"))
    if p is not None and p.is_file():
        return str(p)
    bundled = ROOT / "tools" / "ffmpeg" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg") or (str(p) if p is not None else "ffmpeg")


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def safe_join(base: Path, rel: str) -> Path:
    """``base / rel`` for a client-supplied relative name, refusing anything
    that would land outside ``base`` (absolute paths, drive letters, UNC,
    ``..``). Raises InvalidInput."""
    s = str(rel or "").replace("\\", "/").strip()
    if not s or "\x00" in s or s.startswith("/") or _DRIVE_RE.match(s):
        raise InvalidInput(f"invalid relative path: {rel!r}")
    b = Path(base).resolve()
    p = (b / s).resolve()
    if p == b or not p.is_relative_to(b):
        raise InvalidInput(f"path escapes its folder: {rel!r}")
    return p


def media_roots(cfg: dict[str, Any] | None = None) -> list[Path]:
    """Folders that client-supplied *absolute* media paths may point into:
    the studio tree, the configured ComfyUI and extra model dirs, and any
    operator-added ``security.media_roots`` (e.g. a footage drive)."""
    cfg = cfg if cfg is not None else load_config()
    roots: list[Path] = [ROOT]
    paths = cfg.get("paths") or {}
    for key in ("comfyui", "shots", "output", "roster", "temp"):
        v = paths.get(key)
        if v:
            roots.append(Path(v) if Path(v).is_absolute() else ROOT / v)
    # a list, or one folder written as a plain string
    for v in model_files.listed_dirs(paths.get("extra_model_dirs")):
        roots.append(model_files.resolve_dir(v, ROOT))
    for v in ((cfg.get("security") or {}).get("media_roots") or []):
        if v:
            roots.append(Path(v))
    out: list[Path] = []
    for r in roots:
        try:
            rr = r.resolve()
        except OSError:
            continue
        if rr not in out:
            out.append(rr)
    return out


def resolve_user_path(path: str | Path, cfg: dict[str, Any] | None = None,
                      must_exist: bool = True) -> Path:
    """Validate a client-supplied local file path (ingest source, reference
    image, continuation clip...).

    Relative paths resolve against the studio root. The result must lie
    inside one of media_roots(). UNC paths are always refused: opening
    ``\\\\host\\share`` makes Windows authenticate to that host with the
    user's NTLM credentials. Raises InvalidInput (or FileNotFoundError when
    ``must_exist`` and the file is missing)."""
    s = str(path or "").strip().strip('"')
    if not s or "\x00" in s:
        raise InvalidInput("empty path")
    if s.startswith(("\\\\", "//")):
        raise InvalidInput("network (UNC) paths are not allowed")
    p = Path(s)
    if not p.is_absolute():
        if _DRIVE_RE.match(s):          # "C:foo" — drive-relative
            raise InvalidInput(f"ambiguous drive-relative path: {s!r}")
        p = ROOT / p
    try:
        p = p.resolve()
    except OSError as e:
        raise InvalidInput(f"unresolvable path {s!r}: {e}") from None
    if not any(p.is_relative_to(r) for r in media_roots(cfg)):
        raise InvalidInput(
            f"{s!r} is outside the studio's media folders; add its folder to "
            "security.media_roots in tool/settings.yaml to allow it")
    if must_exist and not p.is_file():
        raise FileNotFoundError(str(p))
    return p


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj
