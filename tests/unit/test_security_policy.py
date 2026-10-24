"""Unit tests for the pure security helpers (tool/security.py, tool/core.py)."""
from pathlib import Path

import pytest

import core
import security


# ------------------------------------------------------------------ host gate
@pytest.mark.parametrize("header,expected", [
    ("127.0.0.1:8080", "127.0.0.1"),
    ("localhost", "localhost"),
    ("[::1]:8080", "::1"),
    ("Studio.LAN:80", "studio.lan"),
    ("", ""),
])
def test_split_host(header, expected):
    assert security.split_host(header) == expected


@pytest.mark.parametrize("header,allowed,ok", [
    ("127.0.0.1:8080", [], True),
    ("192.168.1.10:8080", [], True),          # IP literals always pass
    ("localhost:8080", [], True),
    ("attacker.example", [], False),          # DNS rebinding
    ("studio.lan:8080", ["studio.lan"], True),
    ("box.tailnet.ts.net", ["*.ts.net"], True),
    ("ts.net.evil.example", ["*.ts.net"], False),
    ("anything.example", ["*"], True),
    ("", ["*"], False),
])
def test_host_allowed(header, allowed, ok):
    assert security.host_allowed(header, allowed) is ok


def test_loopback_detection():
    assert security.is_loopback_client("127.0.0.1")
    assert security.is_loopback_client("::1")
    assert security.is_loopback_client("127.5.5.5")
    assert not security.is_loopback_client("192.168.1.5")
    assert not security.is_loopback_client("testclient")
    assert not security.is_loopback_client("")
    assert security.is_loopback_host_header("localhost:8080")
    assert not security.is_loopback_host_header("studio.lan")


# --------------------------------------------------------------- origin gate
HOST = "127.0.0.1:8080"
CORS = ["http://localhost:3000"]


@pytest.mark.parametrize("method,headers,ok", [
    ("GET", {"origin": "http://evil.example"}, True),     # safe method
    ("POST", {}, True),                                   # CLI / agent
    ("POST", {"origin": "http://127.0.0.1:8080"}, True),  # same origin
    ("POST", {"origin": "http://localhost:3000"}, True),  # configured CORS
    ("POST", {"origin": "http://evil.example"}, False),
    ("DELETE", {"origin": "null"}, False),
    ("POST", {"sec-fetch-site": "cross-site"}, False),
    ("POST", {"sec-fetch-site": "same-origin"}, True),
])
def test_origin_allowed(method, headers, ok):
    assert security.origin_allowed(method, headers, HOST, CORS) is ok


# ------------------------------------------------------------- auth helpers
@pytest.mark.parametrize("method,path,ok", [
    ("POST", "/api/workers/register", True),
    ("POST", "/api/workers/gpu-01/heartbeat", True),
    ("POST", "/api/workers/gpu-01/result", True),
    ("GET", "/api/workers/register", False),
    ("POST", "/api/workers/token", False),
    ("POST", "/api/workers/gpu-01/install", False),
    ("POST", "/api/workers/../settings/heartbeat", False),
])
def test_worker_callbacks(method, path, ok):
    assert security.is_worker_callback(method, path) is ok


def test_provided_token_sources():
    assert security.provided_token({"authorization": "Bearer abc"}, {}) == "abc"
    assert security.provided_token({"x-api-token": "def"}, {}) == "def"
    assert security.provided_token({}, {"kinoloom_token": "ghi"}) == "ghi"
    assert security.provided_token({"authorization": "Basic xyz"}, {}) == ""


def test_token_matches():
    assert security.token_matches("a" * 10, "a" * 10)
    assert not security.token_matches("", "")
    assert not security.token_matches("x", "")
    assert not security.token_matches("é", "e")


# ------------------------------------------------------------------ secrets
def test_redact_and_merge_roundtrip():
    cfg = {"security": {"api_token": "s1"}, "workers": {"shared_token": "s2"},
           "architect": {"api_key": ""}, "ui": {"port": 8080}}
    red = security.redact(cfg)
    assert red["security"]["api_token"] == security.REDACTED
    assert red["workers"]["shared_token"] == security.REDACTED
    assert red["architect"]["api_key"] == ""          # empty stays empty
    assert cfg["security"]["api_token"] == "s1"       # original untouched
    merged = security.merge_secrets(red, cfg)
    assert merged["security"]["api_token"] == "s1"
    assert merged["workers"]["shared_token"] == "s2"


def test_merge_accepts_new_secret():
    cur = {"security": {"api_token": "old"}}
    new = {"security": {"api_token": "new"}}
    assert security.merge_secrets(new, cur)["security"]["api_token"] == "new"


def test_ensure_secrets(monkeypatch):
    monkeypatch.delenv("KINOLOOM_API_TOKEN", raising=False)
    cfg = {"workers": {"shared_token": security.PLACEHOLDER_WORKER_TOKEN}}
    notes = security.ensure_secrets(cfg, "127.0.0.1")
    assert len(notes) == 1
    assert cfg["workers"]["shared_token"] not in ("", security.PLACEHOLDER_WORKER_TOKEN)
    assert not cfg["security"].get("api_token")       # loopback: not needed

    notes = security.ensure_secrets(cfg, "0.0.0.0")
    assert len(notes) == 1 and cfg["security"]["api_token"]
    assert security.ensure_secrets(cfg, "0.0.0.0") == []   # idempotent


# --------------------------------------------------------------- path guards
@pytest.mark.parametrize("rel", ["", "/etc/passwd", "C:/Windows/win.ini", "C:x",
                                 "..", "../x", "a/../../x", "x\x00y"])
def test_safe_join_rejects(tmp_path, rel):
    with pytest.raises(core.InvalidInput):
        core.safe_join(tmp_path, rel)


def test_safe_join_accepts_nested(tmp_path):
    assert core.safe_join(tmp_path, "a/b.png") == (tmp_path / "a" / "b.png").resolve()


def test_resolve_user_path(tmp_path):
    inside = Path(core.ROOT) / "temp" / "probe_resolve_user_path.png"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"x")
    try:
        assert core.resolve_user_path(str(inside)) == inside.resolve()
        assert core.resolve_user_path("temp/probe_resolve_user_path.png") == inside.resolve()
    finally:
        inside.unlink()
    for bad in ("\\\\attacker\\share\\x.png", "//attacker/share/x.png", "", "C:rel.png"):
        with pytest.raises(core.InvalidInput):
            core.resolve_user_path(bad)
    outside = tmp_path / "outside.png"        # pytest tmp: not a media root
    outside.write_bytes(b"x")
    with pytest.raises(core.InvalidInput, match="media_roots"):
        core.resolve_user_path(str(outside))
    cfg = {"paths": {}, "security": {"media_roots": [str(tmp_path)]}}
    assert core.resolve_user_path(str(outside), cfg) == outside.resolve()


# ----------------------------------------------------------- fleet installs
@pytest.mark.parametrize("rel", ["C:/Users/x/secret.txt", "/etc/passwd",
                                 "../tool/settings.yaml", "tool/settings.yaml",
                                 "models", "models/../tool/settings.yaml"])
def test_install_source_rejects(rel):
    from routers import fleet
    with pytest.raises(ValueError):
        fleet._install_source(rel)


@pytest.mark.parametrize("rel", ["models/loras/x.safetensors",
                                 "ComfyUI/models/onnx/face.onnx"])
def test_install_source_accepts(rel):
    from routers import fleet
    assert fleet._install_source(rel).is_relative_to(Path(core.ROOT).resolve())
