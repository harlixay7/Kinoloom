"""ImageGen.stop is scoped to its own run; wait_for honours cancellation."""
import threading
import time
from unittest.mock import MagicMock

from images import ImageGen
from renderer import ComfyClient


def test_stop_before_any_run_is_safe():
    ImageGen().stop()


def test_stop_only_touches_its_own_prompt(monkeypatch):
    g = ImageGen()
    c = ComfyClient(8190)
    posted = []
    monkeypatch.setattr(c, "post", lambda path, data=None: posted.append((path, data)))
    monkeypatch.setattr(c, "queue_position", lambda pid: 0)
    c.interrupt = MagicMock()
    g._client = c
    g.active.update(running=True, prompt_id="p-123")
    g.stop()
    assert posted == [("/queue", {"delete": ["p-123"]})]   # never {"clear": True}
    c.interrupt.assert_called_once()


def test_stop_does_not_interrupt_when_our_prompt_is_not_running(monkeypatch):
    g = ImageGen()
    c = ComfyClient(8188)
    monkeypatch.setattr(c, "post", lambda *a, **k: None)
    monkeypatch.setattr(c, "queue_position", lambda pid: None)
    c.interrupt = MagicMock()
    g._client = c
    g.active.update(running=True, prompt_id="p-9")
    g.stop()
    c.interrupt.assert_not_called()       # someone else's render keeps going


def test_wait_for_returns_promptly_on_cancel(monkeypatch):
    c = ComfyClient(8188)
    monkeypatch.setattr(c, "history", lambda pid: {})
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    t0 = time.time()
    assert c.wait_for("x", timeout_s=60, cancel=cancel) is None
    assert time.time() - t0 < 3
