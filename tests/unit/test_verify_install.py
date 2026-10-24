"""install/verify_install.py: the asset manifest describes one reference machine and
is not in git, so a tree without one is checked without it, while a manifest named
on the command line must exist. verify_restore.py is not run here (it imports torch)."""
import importlib.util
import json
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def vi(sandbox_root, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_install", sandbox_root / "install" / "verify_install.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for rel in (*mod.CORE_FILES, "tool/scripts/verify_restore.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    (tmp_path / "install").mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "HERE", tmp_path / "install")
    done = subprocess.CompletedProcess([], 0, stdout="REQUIRED: all present\n", stderr="")
    monkeypatch.setattr(mod, "subprocess", SimpleNamespace(run=lambda *a, **kw: done))
    return mod


def test_a_clone_without_a_manifest_passes_the_full_check(vi, capsys):
    assert vi.main([]) == 0
    out = capsys.readouterr().out
    assert "skipped: no install/manifest.json" in out
    assert "REQUIRED: all present" in out and "RESULT: PASS" in out


def test_a_named_manifest_must_exist(vi, capsys):
    assert vi.main(["--manifest", str(vi.HERE / "other.json")]) == 1
    assert "manifest not found" in capsys.readouterr().out


def test_the_default_manifest_is_checked_when_it_is_there(vi, capsys):
    (vi.ROOT / "models").mkdir()
    (vi.ROOT / "models" / "a.safetensors").write_bytes(b"1234")
    files = {"models/a.safetensors": {"size": 4}, "models/b.safetensors": {"size": 9}}
    (vi.HERE / "manifest.json").write_text(json.dumps({"mode": "quick", "files": files}), encoding="utf-8")
    assert vi.main([]) == 1
    assert "FAIL missing: models/b.safetensors" in capsys.readouterr().out
    assert vi.main(["--quick"]) == 0                     # --quick: a missing asset is a warning
