"""Launcher helpers that decide what is ours, what ComfyUI can see, and how
the user finds the web app."""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    lz.host, lz.port, lz.not_installed = "127.0.0.1", 8080, []
    spawned = []
    lz.spawn = lambda *a, **k: spawned.append(a)
    monkeypatch.setattr(launcher, "QWEN_PY", tmp_path / "nope" / "python.exe")
    monkeypatch.setattr(launcher, "_settings", lambda: {})
    lz.launch("qwen-render")
    assert spawned == [] and lz.not_installed == ["qwen-render"]


def test_missing_comfyui_tree_skips_component(monkeypatch, tmp_path):
    import launcher
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.host, lz.port, lz.not_installed = "127.0.0.1", 8080, []
    spawned = []
    lz.spawn = lambda *a, **k: spawned.append(a)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)      # a clone: no ComfyUI/main.py
    monkeypatch.setattr(launcher, "_settings", lambda: {})
    lz.launch("comfy-ui")
    lz.launch("comfy-ui")
    assert spawned == [] and lz.not_installed == ["comfy-ui"]


# ------------------------------------------------- where the web app is
def test_browser_url_opens_a_wildcard_bind_on_loopback():
    from launcher import browser_url
    assert browser_url("127.0.0.1", 8080) == "http://127.0.0.1:8080/"
    assert browser_url("0.0.0.0", 8080) == "http://127.0.0.1:8080/"
    assert browser_url("::", 9000) == "http://127.0.0.1:9000/"
    assert browser_url("", 8080) == "http://127.0.0.1:8080/"
    assert browser_url("192.168.1.20", 8080) == "http://192.168.1.20:8080/"
    assert browser_url("fe80::1", 8080) == "http://[fe80::1]:8080/"


def test_web_app_answers_only_when_health_says_200():
    from launcher import web_app_answers
    status = {"code": 503}

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status["code"] if self.path == "/api/health" else 404)
            self.end_headers()

        def log_message(self, format, *args):               # quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        assert not web_app_answers(url)
        status["code"] = 200
        assert web_app_answers(url)
    finally:
        server.shutdown()
        server.server_close()
    assert not web_app_answers(url, timeout=1)              # nothing listens now


def test_has_weights_looks_below_every_folder(tmp_path):
    from launcher import has_weights
    (tmp_path / "models" / "vae").mkdir(parents=True)
    (tmp_path / "models" / "vae" / "README.txt").write_text("x")
    assert not has_weights([tmp_path / "missing", tmp_path / "models"])
    (tmp_path / "models" / "vae" / "v.SafeTensors").write_bytes(b"")
    assert has_weights([tmp_path / "missing", tmp_path / "models"])


def test_open_browser_setting_defaults_to_on(monkeypatch):
    import launcher
    for ui, want in (({}, True), ({"open_browser": False}, False),
                     ({"open_browser": "off"}, False), ({"open_browser": True}, True)):
        monkeypatch.setattr(launcher, "_settings", lambda ui=ui: {"ui": ui})
        assert launcher.Launcher._open_browser_setting() is want


def _ready_launcher(launcher, open_browser):
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.stop, lz.open_browser, lz.not_installed = threading.Event(), open_browser, []
    return lz


def test_ready_banner_title_and_browser(monkeypatch, tmp_path, capsys):
    import launcher
    polls = []
    monkeypatch.setattr(launcher, "web_app_answers", lambda url: polls.append(url) or len(polls) > 1)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    titles, opened = [], []
    monkeypatch.setattr(launcher, "set_title", titles.append)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)                  # no weights
    monkeypatch.setattr(launcher, "extra_model_dirs", lambda cfg=None: [])
    lz = _ready_launcher(launcher, True)
    lz.not_installed = ["qwen-render"]
    lz._announce_ready("http://127.0.0.1:8080/")
    out = capsys.readouterr().out
    assert len(polls) == 2                                           # waited for it
    assert "Kinoloom is ready" in out and "http://127.0.0.1:8080/" in out
    assert "qwen-render" in out and "No model weights yet" in out
    assert "http://127.0.0.1:8080/" in titles[-1] and opened == ["http://127.0.0.1:8080/"]

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.gguf").write_bytes(b"")
    _ready_launcher(launcher, False)._announce_ready("http://127.0.0.1:8080/")
    out = capsys.readouterr().out
    assert "Open it in your browser" in out and "No model weights yet" not in out
    assert opened == ["http://127.0.0.1:8080/"]                       # not opened again


def test_ready_gives_up_after_the_timeout(monkeypatch, capsys):
    import launcher
    monkeypatch.setattr(launcher, "web_app_answers", lambda url: False)
    monkeypatch.setattr(launcher, "READY_TIMEOUT_S", -1)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: pytest.fail("opened"))
    _ready_launcher(launcher, True)._announce_ready("http://127.0.0.1:8080/")
    assert "did not answer" in capsys.readouterr().out
