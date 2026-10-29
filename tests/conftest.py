"""Hermetic pytest harness for the Kinoloom control room.

Every data path the studio uses (studio.db, queue.db, datasets.db, bible/,
temp/, output/, shots/, logs/, the video-studio job store) is derived from the
location of ``tool/core.py``. Running the suite against the real ``tool/`` tree
would therefore read and write a live studio's databases and job records.

To make that impossible, the ``tool/`` and ``install/`` trees are copied into a
throwaway sandbox before any studio module is imported, and the modules are
imported from there. Everything the code creates lands in the sandbox, which
is deleted when the session ends.

Background services are disabled as well:

* ``TestClient`` is used WITHOUT its context manager, so the application
  lifespan (GPU broker reconcile, queue dispatcher, dataset worker, VRAM idle
  watchdog, job-store recovery) never starts.
* The sandbox settings disable the GPU broker and the VRAM idle watchdog, so
  no request handler can reach a live ComfyUI instance through them.

Clients: ``test_client`` talks to the app as the local operator
(``Host: 127.0.0.1:8080`` from a loopback peer). ``make_client`` builds a
client for any Host header / peer address, e.g. a remote LAN browser.
"""
from __future__ import annotations

import atexit
import gc
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

_SKIP_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", "node_modules"}
_SKIP_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log",
                  ".pyc", ".pyo"}


def _ignore(dirpath: str, names: list[str]) -> list[str]:
    here = Path(dirpath)
    skipped = []
    for name in names:
        if name in _SKIP_DIRS or Path(name).suffix.lower() in _SKIP_SUFFIXES:
            skipped.append(name)
        elif name == "settings.yaml" and here.name == "tool":
            skipped.append(name)          # never copy real secrets/config
        elif name == "decision_labels" and here.name == "tool":
            skipped.append(name)          # a local, gitignored data folder
    return skipped


def _build_sandbox() -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="kinoloom-tests-")).resolve()
    for sub in ("tool", "install"):
        src = REPO_ROOT / sub
        if src.is_dir():
            shutil.copytree(src, sandbox / sub, ignore=_ignore)

    example = sandbox / "tool" / "settings.example.yaml"
    cfg = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    cfg.setdefault("gpu_broker", {})["enabled"] = False
    cfg.setdefault("vram", {})["auto_offload"] = False
    cfg.setdefault("workers", {})["shared_token"] = "test-worker-token"
    cfg.setdefault("security", {})["api_token"] = ""
    (sandbox / "tool" / "settings.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return sandbox


def _remove_sandbox(path: Path) -> None:
    """Delete the sandbox, read-only files too: git makes the objects of the
    tests' project repositories read-only, and rmtree alone left them behind
    (a kinoloom-tests-* folder in %TEMP% after every run). The studio's log
    files and databases are closed first (Windows deletes no open file); this
    runs before logging's own exit hook."""
    logging.shutdown()
    for obj in gc.get_objects():        # a module's ShotsDB keeps studio.db open
        if type(obj) is sqlite3.Connection:     # (isinstance wakes torch's deprecated proxies)
            try:
                obj.close()
            except sqlite3.Error:
                pass

    def writable_again(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=writable_again)


SANDBOX = _build_sandbox()
atexit.register(_remove_sandbox, SANDBOX)

os.environ["KINOLOOM_TESTING"] = "1"
# the suite's model availability must not depend on the card of the PC it runs
# on (tool/gpu_support.py; tests/unit/test_gpu_support.py checks the rules)
os.environ["KINOLOOM_GPU_CHECK"] = "0"
# the last three add model folders of a development PC (tool/model_files.py)
for _var in ("KINOLOOM_API_TOKEN", "KINOLOOM_BROKER_URL", "KINOLOOM_EXTRA_MODEL_PATHS",
             "H3_LAB_MODELS", "LTX25_LAB_MODELS"):
    os.environ.pop(_var, None)

# The sandbox copy must win over any path that points at the real tree
# (pytest.ini, an editable install, or a stale PYTHONPATH).
_real_tool = str((REPO_ROOT / "tool").resolve())
sys.path[:] = [p for p in sys.path if os.path.abspath(p) != _real_tool]
sys.path.insert(0, str(SANDBOX / "tool"))
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

import core  # noqa: E402  (must come after the sys.path setup above)

if Path(core.ROOT).resolve() != SANDBOX:
    pytest.exit(f"test sandbox not active: core.ROOT={core.ROOT} "
                f"(expected {SANDBOX}); refusing to run against a live studio",
                returncode=3)


@pytest.fixture(scope="session")
def sandbox_root() -> Path:
    """Root of the throwaway studio tree the suite runs against."""
    return SANDBOX


LOCAL_BASE_URL = "http://127.0.0.1:8080"


def _client(base_url: str = LOCAL_BASE_URL, peer: str = "127.0.0.1"):
    from fastapi.testclient import TestClient
    import api
    # no context manager: the lifespan (background services) never starts
    return TestClient(api.app, base_url=base_url, client=(peer, 50000))


@pytest.fixture
def test_client():
    """The local operator: loopback peer, loopback Host header."""
    return _client()


@pytest.fixture
def make_client():
    """Factory: make_client(base_url=..., peer=...) for remote/odd clients."""
    return _client


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Isolated ShotsDB backed by a per-test temporary database."""
    import shots_db
    monkeypatch.setattr(shots_db, "DB", tmp_path / "test_studio.db")
    monkeypatch.setattr(shots_db, "SHOT_DIR", tmp_path / "test_shots")

    db = shots_db.ShotsDB()
    yield db
    try:
        db.con.close()
    except Exception:
        pass
