"""Launcher helpers that decide what is ours and what ComfyUI can see."""
import sys

import pytest
import yaml

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="launcher is Windows-only")


def test_is_studio_cmd_matches_only_our_components():
    from launcher import is_studio_cmd
    assert is_studio_cmd(r"python B:\s\ComfyUI\main.py --port 8188 --x", 8080)
    assert is_studio_cmd(r"python main.py --port 8199 --listen 127.0.0.1", 8080)
    assert is_studio_cmd("python -m uvicorn api:app --host 127.0.0.1 --port 8080", 8080)
    # an unrelated FastAPI app (was killed by the old "uvicorn + api:app" rule)
    assert not is_studio_cmd("python -m uvicorn api:app --port 9000", 8080)
    assert not is_studio_cmd(r"python C:\other\main.py --port 7000", 8080)


def test_extra_model_dirs_and_generated_config(tmp_path, monkeypatch):
    import launcher
    real = tmp_path / "lab"
    (real / "diffusion_models").mkdir(parents=True)
    cfg = {"paths": {"extra_model_dirs": [str(real), str(tmp_path / "missing"),
                                          str(real)]}}
    monkeypatch.setattr(launcher, "_settings", lambda: cfg)
    monkeypatch.setattr(launcher, "EXTRA_PATHS_YAML", tmp_path / "x.yaml")
    assert launcher.extra_model_dirs() == [real.as_posix()]
    args = launcher.Launcher._extra_paths_args()
    assert args == ["--extra-model-paths-config", str(tmp_path / "x.yaml")]
    doc = yaml.safe_load((tmp_path / "x.yaml").read_text(encoding="utf-8"))
    block = doc["studio_extra_0"]
    assert block["base_path"] == real.as_posix()
    assert block["diffusion_models"] == "diffusion_models"


def test_no_extra_dirs_passes_no_flag(monkeypatch):
    import launcher
    monkeypatch.setattr(launcher, "_settings", lambda: {"paths": {}})
    assert launcher.Launcher._extra_paths_args() == []


def test_missing_venv_skips_component(monkeypatch, tmp_path):
    import launcher
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.host, lz.port = "127.0.0.1", 8080
    spawned = []
    lz.spawn = lambda *a, **k: spawned.append(a)
    monkeypatch.setattr(launcher, "QWEN_PY", tmp_path / "nope" / "python.exe")
    monkeypatch.setattr(launcher, "_settings", lambda: {})
    lz.launch("qwen-render")
    assert spawned == []


def test_missing_comfyui_tree_skips_component(monkeypatch, tmp_path):
    import launcher
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.host, lz.port = "127.0.0.1", 8080
    spawned = []
    lz.spawn = lambda *a, **k: spawned.append(a)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)      # a clone: no ComfyUI/main.py
    monkeypatch.setattr(launcher, "_settings", lambda: {})
    lz.launch("comfy-ui")
    assert spawned == []
