import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import renderer
import video_models
import video_studio


def _run_ltx_chain(tmp_path, monkeypatch, frames, tail_ok=True):
    """Drive the LEGACY tail-frame path of _run_chain_body for a 3-segment LTX
    chain (a chain graph is registered; the seamless engine switched off, as
    video_studio.seamless_chains: false does) and record what each segment is
    built with and what the assembly is told to trim."""
    monkeypatch.setattr(video_models, "seamless_kind", lambda _entry: None)
    mid = "ltx_test_chain"
    monkeypatch.setitem(video_models.VIDEO_MODELS, mid, {
        "id": mid, "family": "ltx25", "fps": 24, "comfy_port": 8189,
        "comfy_root": "ComfyUI_LTX", "workflows": {"chain": "chain_tpl"}})
    cid = "test_ltx_overlap"
    video_studio._CHAINS[cid] = {"id": cid, "type": "chain", "status": "running",
                                 "outputs": [], "total": 3, "current": 0}
    seg_src = tmp_path / "seg.mp4"
    seg_src.write_bytes(b"x" * 2000)
    client = MagicMock()
    client.queue_prompt.return_value = "pid"
    client.wait_for.return_value = {
        "status": {"status_str": "success"},
        "outputs": {"9": {"images": [{"filename": "seg.mp4", "type": "output"}]}}}
    built, assembled = [], {}

    def fake_build(*args, **kw):
        built.append({"frames": args[7], "chain_tail": kw.get("chain_tail")})
        return {"1": {"class_type": "SaveVideo", "inputs": {}}}, "tpl", "n"

    def fake_png(_src, dst):
        Path(dst).write_bytes(b"png")
        return True

    def fake_tail(_src, dst, n):
        assert n == 17
        Path(dst).write_bytes(b"mp4")
        return tail_ok

    def fake_assemble(segs, final_path, trims, fps=24):
        assembled["trims"] = list(trims)
        Path(final_path).write_bytes(b"x" * 2000)
        return True

    monkeypatch.setattr(video_studio, "ROOT", tmp_path)
    monkeypatch.setattr(renderer, "ComfyClient", lambda *_a, **_k: client)
    monkeypatch.setattr(video_studio, "_build", fake_build)
    monkeypatch.setattr(video_studio, "_apply_references", lambda wf, *_a, **_k: wf)
    monkeypatch.setattr(video_studio, "_find_output", lambda *_a, **_k: seg_src)
    monkeypatch.setattr(video_studio, "_extract_last_frame", fake_png)
    monkeypatch.setattr(video_studio, "_extract_tail_video", fake_tail)
    monkeypatch.setattr(video_studio, "_assemble_chain", fake_assemble)
    video_studio._run_chain_body(
        cid=cid, mid=mid, prompts=["a", "b", "c"], mode="t2v", negative="",
        seed=1, width=768, height=512, frames=frames, steps=8, cfg=1.0,
        start_image="", worker="")
    return video_studio._CHAINS[cid], built, assembled


@pytest.mark.parametrize("frames", [121, 97, 25])
def test_ltx_chain_overlap_pins_and_trims(tmp_path, monkeypatch, frames):
    """Segments 2+ replay the previous 17-frame tail: they render
    frames + 16 (still on the LTX 1+8k grid) and the assembly trims 17
    leading frames, so every continuation adds frames - 1 new frames."""
    rec, built, assembled = _run_ltx_chain(tmp_path, monkeypatch, frames)
    assert rec["status"] == "completed", rec.get("error")
    assert [b["frames"] for b in built] == [frames, frames + 16, frames + 16]
    assert all((b["frames"] - 1) % 8 == 0 for b in built)
    assert built[0]["chain_tail"] is None
    assert all(b["chain_tail"] for b in built[1:])
    assert assembled["trims"] == [0, 17, 17]


def _seamless_call(tmp_path, monkeypatch, cfg):
    import chain_studio
    mid = "ltx_test_seamless"
    monkeypatch.setitem(video_models.VIDEO_MODELS, mid, {
        "id": mid, "family": "ltx25", "fps": 24, "comfy_port": 8189,
        "comfy_root": "ComfyUI_LTX", "workflows": {"chain": "chain_tpl"}})
    cid = "test_seamless"
    video_studio._CHAINS[cid] = {"id": cid, "type": "chain", "status": "running", "outputs": [],
                                 "total": 2, "current": 0, "draft_only": True}
    calls = []
    monkeypatch.setattr(video_studio, "load_config", lambda: cfg)
    monkeypatch.setattr(renderer, "ComfyClient", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(chain_studio, "run_chain", lambda rec, entry, client, prompts, **kw:
                        calls.append({"prompts": prompts, **kw}))
    # the legacy path builds per-segment graphs; reaching _build marks it
    monkeypatch.setattr(video_studio, "_build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("legacy")))
    video_studio._run_chain_body(cid=cid, mid=mid, prompts=["a", "b"], mode="t2v", negative="n",
                                 seed=7, width=768, height=512, frames=121, steps=8, cfg=1.0,
                                 start_image="", worker="")
    return calls, video_studio._CHAINS[cid]


def test_ltx_chain_model_runs_the_seamless_engine(tmp_path, monkeypatch):
    calls, rec = _seamless_call(tmp_path, monkeypatch, {"ensemble": {"comfyui_port": 8188}})
    assert calls and calls[0]["prompts"] == ["a", "b"] and rec.get("error", "") != "legacy"
    assert calls[0]["base_seed"] == 7 and calls[0]["frames"] == 121 and calls[0]["negative"] == "n"
    assert calls[0]["draft_only"] is True and calls[0]["tier_graph"] is None


def test_seamless_chains_switch_falls_back_to_the_legacy_path(tmp_path, monkeypatch):
    cfg = {"ensemble": {"comfyui_port": 8188}, "video_studio": {"seamless_chains": False}}
    calls, rec = _seamless_call(tmp_path, monkeypatch, cfg)
    assert calls == [] and rec["error"] == "legacy"


def test_ltx_chain_without_tail_video_does_not_pin(tmp_path, monkeypatch):
    """If the 17-frame tail cannot be extracted the next segment falls back
    to the still-image path: normal length, nothing trimmed."""
    rec, built, assembled = _run_ltx_chain(tmp_path, monkeypatch, 121,
                                           tail_ok=False)
    assert rec["status"] == "completed", rec.get("error")
    assert [b["frames"] for b in built] == [121, 121, 121]
    assert assembled["trims"] == [0, 0, 0]


def _run_mock_chain(tmp_path: Path, prompts: list[str], urlopen_side_effect=None):
    """Helper to execute _run_chain_body under hermetic mocks and record requests."""
    cid = f"test_chain_{len(prompts)}seg"
    video_studio._CHAINS[cid] = {
        "id": cid,
        "type": "chain",
        "status": "running",
        "outputs": [],
        "total": len(prompts),
        "current": 0,
    }

    dummy_out = tmp_path / "dummy_seg.mp4"
    dummy_out.write_bytes(b"x" * 2000)

    mock_client = MagicMock()
    mock_client.queue_prompt.return_value = "prompt-pid-1"
    mock_client.wait_for.return_value = {
        "status": {"status_str": "success"},
        "outputs": {
            "SaveVideo": {
                "images": [{"filename": "dummy_seg.mp4", "type": "output"}]
            }
        },
    }

    recorded_requests: list[urllib.request.Request] = []

    def fake_urlopen(req, timeout=None):
        recorded_requests.append(req)
        if callable(urlopen_side_effect):
            return urlopen_side_effect(req, timeout=timeout)
        if isinstance(urlopen_side_effect, Exception):
            raise urlopen_side_effect
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        return resp

    def fake_assemble(seg_paths, final_path, trims, fps=24):
        Path(final_path).write_bytes(b"x" * 2000)
        return True

    def fake_extract_last(vpath, opng):
        Path(opng).write_bytes(b"dummy_png")
        return True

    with patch.object(video_studio, "ROOT", tmp_path), \
         patch.object(renderer, "ComfyClient", return_value=mock_client), \
         patch.object(video_studio, "_build", return_value=({"SaveVideo": {"class_type": "SaveVideo"}}, {}, "node1")), \
         patch.object(video_studio, "_find_output", return_value=dummy_out), \
         patch.object(video_studio, "_extract_last_frame", side_effect=fake_extract_last), \
         patch.object(video_studio, "_assemble_chain", side_effect=fake_assemble), \
         patch("time.sleep", return_value=None), \
         patch("urllib.request.urlopen", side_effect=fake_urlopen):

        video_studio._run_chain_body(
            cid=cid,
            mid="wan-2.1-t2v-14b",
            prompts=prompts,
            mode="t2v",
            negative="",
            seed=42,
            width=832,
            height=480,
            frames=81,
            steps=8,
            cfg=6.0,
            start_image="",
            worker="",
        )

    return video_studio._CHAINS[cid], recorded_requests


def test_chain_keeps_model_warm_between_segments(tmp_path):
    """A chain holds ONE broker lease so its model stays warm: no segment
    may unload the instance (/free) before the next one renders — that
    forced a full model reload per segment, and /free -> reload is the H3
    livelock path the broker exists to avoid."""
    prompts = ["Scene 1: Introduction", "Scene 2: Escalation", "Scene 3: Climax"]
    chain_rec, requests = _run_mock_chain(tmp_path=tmp_path, prompts=prompts)

    assert not [r for r in requests if str(getattr(r, "full_url", r)).endswith("/free")]
    assert chain_rec["status"] == "completed"
    assert not chain_rec.get("error")
    assert len(chain_rec["segment_times"]) == len(prompts)


def test_chain_segment_timeout_fails_cleanly(tmp_path):
    """wait_for() returning None (deadline hit) used to crash with
    AttributeError on None.get; it must interrupt the farm and raise a
    clear error instead."""
    cid = "test_chain_timeout"
    video_studio._CHAINS[cid] = {"id": cid, "type": "chain", "status": "running",
                                 "outputs": [], "total": 1, "current": 0}
    mock_client = MagicMock()
    mock_client.queue_prompt.return_value = "pid"
    mock_client.wait_for.return_value = None
    with patch.object(video_studio, "ROOT", tmp_path), \
         patch.object(renderer, "ComfyClient", return_value=mock_client), \
         patch.object(video_studio, "_build",
                      return_value=({"1": {"class_type": "SaveVideo"}}, {}, "n")):
        video_studio._run_chain_body(
            cid=cid, mid="wan-2.1-t2v-14b", prompts=["only"], mode="t2v",
            negative="", seed=1, width=832, height=480, frames=81,
            steps=8, cfg=6.0, start_image="", worker="")
    rec = video_studio._CHAINS[cid]
    assert rec["status"] == "failed"
    assert "no result" in rec["error"]
    mock_client.interrupt.assert_called_once()
