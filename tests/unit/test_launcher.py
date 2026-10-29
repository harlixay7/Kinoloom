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
    # ComfyUI's older folder names, and the IP-Adapter folder, are mounted too
    assert block["unet"] == "unet" and block["clip"] == "clip" and block["ipadapter"] == "ipadapter"


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


def test_a_venv_the_installer_did_not_finish_is_not_started(monkeypatch, tmp_path):
    import launcher
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.host, lz.port, lz.not_installed = "127.0.0.1", 8080, []
    spawned = []
    lz.spawn = lambda *a, **k: spawned.append(a)
    (tmp_path / "ComfyUI_Qwen21").mkdir()
    (tmp_path / "ComfyUI_Qwen21" / "main.py").write_text("", encoding="utf-8")
    venv = tmp_path / "ComfyUI_Qwen21" / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / launcher.INSTALL_MARKER).write_text("", encoding="utf-8")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "QWEN_PY", venv / "Scripts" / "python.exe")
    monkeypatch.setattr(launcher, "_settings", lambda: {})
    lz.launch("qwen-render")
    assert spawned == [] and lz.not_installed == ["qwen-render"]
    (venv / launcher.INSTALL_MARKER).unlink()
    lz.launch("qwen-render")
    assert len(spawned) == 1


class _Dead:
    returncode = 1

    def poll(self):
        return 1


def _health_launcher(launcher, monkeypatch, tmp_path):
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.procs, lz._born, lz._fast_crashes, lz._gave_up = {"qwen-render": _Dead()}, {}, {}, set()
    relaunched = []
    lz.launch = relaunched.append
    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    return lz, relaunched


def test_a_restart_the_gpu_broker_made_is_not_reported_as_a_crash(monkeypatch, tmp_path, capsys):
    # the broker ends an instance whose /free did not give the memory back; the
    # launcher printed "!! DIED (exit 1) - restarting (crash 0/3)" in red
    import gpu_broker
    import launcher
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(gpu_broker, "ROOT", tmp_path)
    monkeypatch.setattr(gpu_broker, "_port_pids", lambda port: [])
    monkeypatch.setattr(gpu_broker, "_vram_free_mb", lambda port: 7100)
    monkeypatch.setattr(gpu_broker.time, "sleep", lambda s: None)
    b = gpu_broker._Broker()
    monkeypatch.setattr(b, "_registry", lambda: {8190: {"h3": False, "models": ["qwen21_fast"]}})
    b._restart_instance(b._instance(8190))
    lz, relaunched = _health_launcher(launcher, monkeypatch, tmp_path / "logs")
    lz._health()
    out = capsys.readouterr().out
    assert "restarted it to free the card's memory (planned, not a crash)" in out and "DIED" not in out
    assert relaunched == ["qwen-render"] and lz._fast_crashes["qwen-render"] == 0
    assert not (tmp_path / "logs" / ".planned-restart-8190").exists()       # used once
    lz.procs["qwen-render"] = _Dead()                                      # a real crash after it
    lz._health()
    assert "DIED (exit 1)" in capsys.readouterr().out


def test_an_old_restart_mark_does_not_hide_a_crash(monkeypatch, tmp_path, capsys):
    import os
    import launcher
    mark = tmp_path / ".planned-restart-8190"
    mark.write_text("0", encoding="utf-8")
    os.utime(mark, (1, 1))
    lz, relaunched = _health_launcher(launcher, monkeypatch, tmp_path)
    lz._health()
    assert "DIED (exit 1)" in capsys.readouterr().out and relaunched == ["qwen-render"]
    assert not mark.exists()


def test_an_unfinished_studio_install_stops_the_start(monkeypatch, tmp_path):
    import launcher
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    assert launcher.unfinished_install() == []
    venv = tmp_path / "tool" / ".venv"
    venv.mkdir(parents=True)
    (venv / launcher.INSTALL_MARKER).write_text("", encoding="utf-8")
    lines = launcher.unfinished_install()
    assert len(lines) == 2 and "did not finish" in lines[0] and "INSTALL.bat" in lines[1]
    last = tmp_path / "logs" / "install" / "LAST-FAILURE.txt"
    last.parent.mkdir(parents=True)
    last.write_text("x", encoding="utf-8")
    assert str(last) in launcher.unfinished_install()[-1]


def test_run_checks_the_install_before_it_touches_any_port():
    """run() is never called here: its port check can restart a running
    studio. The order in the source is checked instead."""
    import inspect

    import launcher
    src = inspect.getsource(launcher.Launcher.run)
    assert 0 < src.index("unfinished_install()") < src.index("conflicts = ")


def test_a_crash_on_a_missing_package_points_to_the_installer(tmp_path):
    from launcher import crash_hint
    log = tmp_path / "ui.log"
    log.write_text("INFO: started\nTraceback (most recent call last):\n"
                   "ModuleNotFoundError: No module named 'uvicorn'\n", encoding="utf-8")
    hint = crash_hint(log)
    assert hint and "No module named 'uvicorn'" in hint and "INSTALL.bat" in hint
    log.write_text("torch.OutOfMemoryError: CUDA out of memory\n", encoding="utf-8")
    assert crash_hint(log) is None
    assert crash_hint(tmp_path / "missing.log") is None
    # a venv made before the Kinoloom folder was moved
    log.write_text("No Python at 'C:\\Old\\Kinoloom\\tools\\python\\python.exe'\n", encoding="utf-8")
    hint = crash_hint(log)
    assert hint and "moved" in hint and "INSTALL.bat" in hint


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
    lz.stop, lz.open_browser, lz.not_installed, lz.unsupported = threading.Event(), open_browser, [], {}
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
    lz.unsupported = {"ltx-render": "the GTX 1080 cannot run LTX-2.5"}
    lz._announce_ready("http://127.0.0.1:8080/")
    out = capsys.readouterr().out
    assert len(polls) == 2                                           # waited for it
    assert "Kinoloom is ready" in out and "http://127.0.0.1:8080/" in out
    assert "qwen-render" in out and "No model weights yet" in out
    assert "Not started on this PC: ltx-render: the GTX 1080 cannot run LTX-2.5." in out
    assert "Settings > Model files" in out                           # weights you already have
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
