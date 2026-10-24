import hashlib
import time
from datetime import datetime, timedelta
from unittest.mock import patch
import pytest
import task_queue
import workers


@pytest.fixture
def temp_queue_db(tmp_path, monkeypatch):
    """Isolate queue.db in a temporary directory for worker fleet tests."""
    test_db = tmp_path / "test_queue.db"
    monkeypatch.setattr(task_queue, "DB_PATH", test_db)
    task_queue._initialized = False
    task_queue.init()
    yield test_db


def test_worker_token_auth_check():
    """_check_token enforces shared secret and per-worker registration token hash."""
    fake_config = {"workers": {"shared_token": "secret_fleet_token_123"}}

    with patch("workers.load_config", return_value=fake_config):
        # Correct token passes
        workers._check_token("secret_fleet_token_123")

        # Bad token raises PermissionError
        with pytest.raises(PermissionError, match="bad worker token"):
            workers._check_token("wrong_token")

        # Registered worker with mismatched token hash raises PermissionError
        expected_hash = hashlib.sha256(b"secret_fleet_token_123").hexdigest()
        bad_hash = hashlib.sha256(b"other_token").hexdigest()

        # Matching row token_hash passes
        workers._check_token("secret_fleet_token_123", {"token_hash": expected_hash})

        # Mismatched row token_hash fails
        with pytest.raises(PermissionError, match="token does not match this worker's registration"):
            workers._check_token("secret_fleet_token_123", {"token_hash": bad_hash})


def test_worker_registration_and_heartbeat(temp_queue_db):
    """Worker registers with SHA-256 token hash and heartbeats telemetry."""
    wid = "gpu-node-01"
    token = "node_token_abc"
    expected_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    with patch("workers.load_config", return_value={"workers": {"shared_token": token}}):
        reg_res = workers.register(
            name=wid,
            host="192.168.1.50",
            port=8188,
            token=token,
            gpu={"name": "RTX 4090", "vram_free_mb": 24000},
            models=["realvis", "ltx"],
            agent_version="1.2.0",
        )
        assert reg_res["id"] == wid

        # Verify database entry
        worker = workers.get(wid)
        assert worker is not None
        assert worker["host"] == "192.168.1.50"
        assert worker["port"] == 8188
        assert worker["token_hash"] == expected_hash
        assert worker["status"] == "online"
        assert worker["gpu"]["name"] == "RTX 4090"

        # Send heartbeat
        hb_res = workers.heartbeat(
            wid=wid,
            token=token,
            gpu={"name": "RTX 4090", "vram_free_mb": 21000},
            agent_version="1.2.1",
        )
        assert hb_res["ok"] is True

        updated_worker = workers.get(wid)
        assert updated_worker["gpu"]["vram_free_mb"] == 21000
        assert updated_worker["agent_version"] == "1.2.1"


def test_worker_auth_fails_closed_without_configured_token():
    with patch("workers.load_config", return_value={"workers": {"shared_token": ""}}):
        with pytest.raises(PermissionError, match="not configured"):
            workers._check_token("")
        with pytest.raises(PermissionError, match="not configured"):
            workers._check_token("anything")


@pytest.mark.parametrize("host", [
    "169.254.169.254",                 # cloud metadata (link-local)
    "http://169.254.169.254:80",
    "[fe80::1]",
    "0.0.0.0",
    "224.0.0.1",                       # multicast
    "file:///etc/passwd",
    "ftp://box.lan",
    "http://user:pw@box.lan",
    "http://box.lan/?x=1",
    "bad host name",
    "",
])
def test_worker_host_validation_rejects(host):
    with pytest.raises(ValueError):
        workers.validate_worker_host(host)


@pytest.mark.parametrize("host", [
    "192.168.1.20", "10.0.0.5", "100.101.102.103", "127.0.0.1",
    "box.lan", "https://box.tailnet.ts.net", "http://100.64.1.2:8120",
])
def test_worker_host_validation_accepts(host):
    assert workers.validate_worker_host(host) == host.rstrip("/")


def test_register_rejects_bad_host_and_port(temp_queue_db):
    with patch("workers.load_config", return_value={"workers": {"shared_token": "t"}}):
        with pytest.raises(ValueError):
            workers.register(name="w", host="169.254.169.254", port=8120, token="t")
        with pytest.raises(ValueError):
            workers.register(name="w", host="192.168.1.2", port=70000, token="t")


def test_store_result_streams_and_caps(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(workers, "_result_dir", tmp_path / "results")
    res = workers.store_result("job1", "out.mp4", io.BytesIO(b"x" * 1000), max_bytes=4096)
    assert res["bytes"] == 1000
    assert (tmp_path / "results" / "job1" / "out.mp4").read_bytes() == b"x" * 1000

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        workers.store_result("job1", "big.mp4", io.BytesIO(b"x" * 5000), max_bytes=4096)
    assert exc.value.status_code == 413
    # nothing partial left behind
    assert sorted(p.name for p in (tmp_path / "results" / "job1").iterdir()) == ["out.mp4"]


@pytest.mark.parametrize("job,name", [("..", "x"), ("j", "../x"), ("j", "C:x"),
                                      ("a/b", "x"), ("j", "")])
def test_store_result_rejects_traversal(tmp_path, monkeypatch, job, name):
    from fastapi import HTTPException
    monkeypatch.setattr(workers, "_result_dir", tmp_path / "results")
    with pytest.raises(HTTPException):
        workers.store_result(job, name, b"x")


def test_worker_offline_reaper(temp_queue_db):
    """reap_offline transitions silent workers to offline status."""
    wid = "silent-worker"
    with patch("workers.load_config", return_value={"workers": {"shared_token": "t", "offline_after_s": 30}}):
        workers.register(name=wid, host="192.168.1.99", port=8188, token="t")

        w = workers.get(wid)
        assert w["status"] == "online"

        # Manually backdate the worker's last_heartbeat by 60 seconds
        old_time = (datetime.now() - timedelta(seconds=60)).isoformat()
        with task_queue._lock, task_queue.connect() as c:
            c.execute("UPDATE workers SET last_heartbeat=? WHERE id=?", (old_time, wid))

        # Reaper runs with 30s threshold
        workers.reap_offline()

        reaped_worker = workers.get(wid)
        assert reaped_worker["status"] == "offline"
