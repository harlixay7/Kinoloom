"""Orchestrator start/stop: one render loop, and crash recovery of shots
stranded between render and QA."""
import threading
from unittest.mock import MagicMock

from orchestrator import Orchestrator


def _orch(db=None):
    o = Orchestrator.__new__(Orchestrator)
    import queue
    o.db = db or MagicMock()
    o.running = o.paused = False
    o._lock = threading.Lock()
    o._post_queue = queue.Queue()
    o._post_thread = None
    o._thread = None
    o.cfg = {}
    return o


def test_restart_while_draining_keeps_a_single_loop(monkeypatch):
    o = _orch()
    gate = threading.Event()
    loops = []

    def fake_loop():
        loops.append(threading.current_thread())
        gate.wait(5)

    monkeypatch.setattr(o, "_loop", fake_loop)
    monkeypatch.setattr(o, "_post_worker", lambda: gate.wait(5))
    monkeypatch.setattr(o, "_reset_orphaned_rendering", MagicMock())
    monkeypatch.setattr(o, "_recover_post_backlog", MagicMock())
    o.start()
    o.stop()                         # in-flight loop still alive
    o.start()                        # must NOT spawn a second loop
    gate.set()
    o._thread.join(2)
    assert len(loops) == 1
    assert o._reset_orphaned_rendering.call_count == 1
    assert o.running is True


def test_recover_post_backlog(tmp_path):
    vid = tmp_path / "s01_001.mp4"
    vid.write_bytes(b"x")
    rows = {"rendered": [{"shot_id": "s01_001", "project_id": "p",
                          "video_path": str(vid), "json": {"prompt": "hi"},
                          "prompt": None}],
            "post": [{"shot_id": "s01_002", "project_id": "p",
                      "video_path": str(tmp_path / "gone.mp4"), "json": {}}]}
    db = MagicMock()
    db.all.side_effect = lambda status=None, **k: rows.get(status, [])
    o = _orch(db)
    assert o._recover_post_backlog() == 2
    shot, sjson, video, prompt = o._post_queue.get_nowait()
    assert shot["shot_id"] == "s01_001" and video == vid and prompt == "hi"
    db.set_status.assert_called_once_with("s01_002", "pending", project="p")
