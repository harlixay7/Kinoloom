"""INSTALL-WORKER.bat: placeholder filling and an end-to-end dry install.

The served installer is the template with its placeholders replaced. The
template's "was it filled in?" checks must survive that replace pass (they
used to be rewritten too, which discarded the embedded token and URL), and
the generated script must actually install from a golden tree.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import core
from routers import fleet

TEMPLATE = Path(core.ROOT) / "install" / "INSTALL-WORKER.bat"
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")


def _render(token="tok-123_ABC", url="http://192.168.1.10:8080",
            ui_host="0.0.0.0"):
    return fleet.render_installer(TEMPLATE.read_text(encoding="utf-8"),
                                  url, token, ui_host)


def test_template_is_ascii_crlf():
    raw = TEMPLATE.read_bytes()
    assert raw.isascii()
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_render_fills_every_placeholder_and_keeps_the_checks():
    out = _render()
    assert PLACEHOLDER.findall(out) == []
    assert 'set "WTOKEN=tok-123_ABC"' in out
    assert 'set "MURL=http://192.168.1.10:8080"' in out
    # the sentinel comparisons are untouched by the replace pass
    for name in ("TOKEN", "MAIN_URL", "WORKER_NAME", "BIND_WARNING"):
        assert f'"%_P%{name}%_P%"' in out


def test_bind_warning_only_for_loopback_studio():
    assert "WARNING: the studio listens on 127.0.0.1" in _render(ui_host="127.0.0.1")
    assert "WARNING: the studio" not in _render(ui_host="0.0.0.0")


@pytest.mark.parametrize("bad", ['x" & calc', "a%PATH%", "a|b", "a\r\nb"])
def test_batch_metacharacters_are_refused(bad):
    with pytest.raises(ValueError):
        _render(token=bad)


def test_installer_endpoint_serves_a_filled_script(test_client):
    r = test_client.get("/api/workers/installer")
    assert r.status_code == 200
    assert 'set "WTOKEN=test-worker-token"' in r.text
    assert PLACEHOLDER.findall(r.text) == []


# ------------------------------------------------------------ dry install
def _golden_tree(root: Path) -> Path:
    src = root / "golden"
    (src / "ComfyUI").mkdir(parents=True)
    (src / "ComfyUI" / "main.py").write_text("# stub\n")
    for d in ("tools/python", "tool/.venv", "tool/worker"):
        (src / d).mkdir(parents=True)
        (src / d / "stub.txt").write_text("x")
    shutil.copytree(Path(core.ROOT) / "install", src / "install")
    sdxl = src / "models" / "diffusion_models" / "RealVisXL_V5.0-Q5_K_M.gguf"
    sdxl.parent.mkdir(parents=True)
    sdxl.write_bytes(b"\0" * 4096)
    return src


def _run(script: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KINOLOOM_")}
    env.update(env_extra)
    return subprocess.run(["cmd.exe", "/d", "/c", str(script)], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=180)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch installer")
@pytest.mark.skipif(shutil.which("robocopy") is None, reason="needs robocopy")
def test_served_installer_installs_with_embedded_values(tmp_path):
    src = _golden_tree(tmp_path)
    dst = tmp_path / "worker install"            # a space in the path on purpose
    script = tmp_path / "INSTALL-WORKER.bat"
    script.write_bytes(_render().encode("ascii"))
    p = _run(script, {"KINOLOOM_SRC": str(src), "KINOLOOM_DST": str(dst),
                      "KINOLOOM_WORKER_NAME": "rig2",
                      "KINOLOOM_PACKAGES": "1"})
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    cfg = (dst / "worker" / "worker_config.yaml").read_text()
    assert 'token: "tok-123_ABC"' in cfg
    assert 'main_url: "http://192.168.1.10:8080"' in cfg
    assert 'name: "rig2"' in cfg
    # manifest copy keeps the folder layout; absent files are reported
    assert (dst / "models" / "diffusion_models" /
            "RealVisXL_V5.0-Q5_K_M.gguf").stat().st_size == 4096
    assert "missing on the source" in p.stdout
    launcher = (dst / "WORKER.bat").read_text()
    assert f'cd /d "{dst}\\worker"' in launcher


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch installer")
@pytest.mark.skipif(shutil.which("robocopy") is None, reason="needs robocopy")
def test_raw_template_requires_a_token(tmp_path):
    src = _golden_tree(tmp_path)
    script = src / "install" / "INSTALL-WORKER.bat"
    p = _run(script, {"KINOLOOM_DST": str(tmp_path / "w"),
                      "KINOLOOM_MAIN_URL": "http://10.0.0.2:8080",
                      "KINOLOOM_PACKAGES": "5"})
    assert p.returncode == 1
    assert "fleet token required" in p.stdout
    assert not (tmp_path / "w").exists()

    p = _run(script, {"KINOLOOM_DST": str(tmp_path / "w"),
                      "KINOLOOM_MAIN_URL": "http://10.0.0.2:8080",
                      "KINOLOOM_WORKER_TOKEN": "envtok",
                      "KINOLOOM_PACKAGES": "5"})
    assert p.returncode == 0, p.stdout[-2000:]
    cfg = (tmp_path / "w" / "worker" / "worker_config.yaml").read_text()
    assert 'token: "envtok"' in cfg and "10.0.0.2" in cfg
