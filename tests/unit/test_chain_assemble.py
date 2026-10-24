"""Joining chain links: timeline bookkeeping, the in-overlap blend, the equal-gain
audio fade and one end-to-end assembly of synthetic lossless links."""
import json
import shutil
from fractions import Fraction

import numpy as np
import pytest

import chain_assemble as ca

av = pytest.importorskip("av")


def _links(frames, carry):
    return [ca.LinkMedia(video=None, audio=None, frames=f, carry=(0 if i == 0 else carry))
            for i, f in enumerate(frames)]


def test_plan_timeline_counts_and_seams():
    tl = ca.plan_timeline(_links([121, 145, 145, 145], 25), 8)
    assert tl["total"] == 121 + 3 * 120 == 481
    assert tl["starts"] == [0, 96, 216, 336]
    assert tl["seams"] == [121, 241, 361]


def test_plan_timeline_rejects_bad_overlap():
    with pytest.raises(ValueError):
        ca.plan_timeline(_links([121, 145], 25), 30)
    with pytest.raises(ValueError):
        ca.plan_timeline([ca.LinkMedia(None, None, 121, 0), ca.LinkMedia(None, None, 145, 0)], 8)
    with pytest.raises(ValueError):
        ca.plan_timeline(_links([121, 145], 25), 8, blend_start=18)     # window past the carry end


def test_plan_timeline_blend_windows():
    links = _links([121, 145, 145], 25)
    assert ca.plan_timeline(links, 8)["blends"] == [[113, 121], [233, 241]]          # ends on the seam
    assert ca.plan_timeline(links, 4, blend_start=0)["blends"] == [[96, 100], [216, 220]]
    assert ca.plan_timeline(links, 4, blend_start=0)["seams"] == [121, 241]


def test_blend_weights_are_strictly_inside_0_1():
    w = ca.blend_weights(8)
    assert w[0] == pytest.approx(1 / 9) and w[-1] == pytest.approx(8 / 9)
    assert np.all(np.diff(w) > 0)


@pytest.mark.parametrize("sr", [24000, 32000, 48000])
def test_audio_gains_sum_to_one_across_each_join(sr):
    links = _links([121, 145, 145], 25)
    gains = ca.link_gains(links, 8, 24.0, sr)
    tl = ca.plan_timeline(links, 8)
    total = int(round(tl["total"] / 24.0 * sr))
    acc = np.zeros(total + 4, dtype=np.float64)
    for i, g in enumerate(gains):
        s0 = int(round(tl["starts"][i] / 24.0 * sr))
        acc[s0:s0 + g.shape[0]] += g
    body = acc[:total]
    # every sample of the take is covered with total gain exactly 1
    assert np.max(np.abs(body - 1.0)) < 1e-6, np.nonzero(np.abs(body - 1.0) > 1e-6)[0][:20]
    # each fade ends AUDIO_GUARD_FRAMES before the outgoing link's last frame
    g0 = gains[0]
    tail = int(round(ca.AUDIO_GUARD_FRAMES / 24.0 * sr))
    assert np.all(g0[-tail + 1:] == 0.0) and g0[-tail - int(round(8 / 24.0 * sr)) - 2] == 1.0


def test_audio_gains_hard_cut_without_crossfade():
    links = _links([121, 145], 25)
    g = ca.link_gains(links, 0, 24.0, 48000)
    assert set(np.unique(g[0])) <= {0.0, 1.0} and set(np.unique(g[1])) <= {0.0, 1.0}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_short_decoded_audio_does_not_dip(tmp_path):
    # the audio decoder returns ~30 ms less than the video; the fade must not hit the gap
    l1, l2 = tmp_path / "a.mkv", tmp_path / "b.mkv"
    _write_link(l1, _solid(100, 49), tone=0.3, short_s=0.03)
    _write_link(l2, _solid(100, 49), tone=0.3, short_s=0.03)
    links = [ca.LinkMedia(l1, l1.with_suffix(".audio.safetensors"), 49, 0),
             ca.LinkMedia(l2, l2.with_suffix(".audio.safetensors"), 49, 25)]
    au, sr = ca.assemble_audio(links, 8, 24.0)
    body = au[:, : int(round((49 + 24 - 1) / 24.0 * sr))]
    assert np.max(np.abs(body - 0.3)) < 1e-3


def _write_link(path, frames, sr=24000, tone=0.0, short_s=0.0):
    h, w = 16, 24
    with av.open(str(path), "w") as ct:
        s = ct.add_stream("ffv1", rate=Fraction(24, 1))
        s.width, s.height, s.pix_fmt = w, h, "bgr0"
        for f in frames:
            for pkt in s.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
                ct.mux(pkt)
        for pkt in s.encode():
            ct.mux(pkt)
    path.with_suffix(".json").write_text(json.dumps({"frames": len(frames), "width": w, "height": h,
                                                     "fps": 24.0}), encoding="utf-8")
    from safetensors.numpy import save_file
    n = int(round((len(frames) / 24.0 - short_s) * sr))
    wf = np.full((2, n), tone, dtype=np.float32)
    save_file({"waveform": wf}, str(path.with_suffix(".audio.safetensors")), metadata={"sample_rate": str(sr)})


def _solid(v, n):
    return [np.full((16, 24, 3), v, dtype=np.uint8) for _ in range(n)]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_end_to_end_assembly(tmp_path):
    # link 1: grey 100 for 33 frames; link 2: its 9 carried frames show grey 100 (the same
    # content), then grey 200 for 24 new frames. crossfade 4.
    l1 = tmp_path / "l1.mkv"
    l2 = tmp_path / "l2.mkv"
    _write_link(l1, _solid(100, 33), tone=0.25)
    _write_link(l2, _solid(100, 9) + _solid(200, 24), tone=0.25)
    links = [ca.LinkMedia(l1, l1.with_suffix(".audio.safetensors"), 33, 0),
             ca.LinkMedia(l2, l2.with_suffix(".audio.safetensors"), 33, 9)]
    frames = list(ca.assemble_frames(links, 4))
    assert len(frames) == 33 + 24
    vals = [int(f[0, 0, 0]) for f in frames]
    assert vals[:33] == [100] * 33          # blend of identical content stays identical
    assert vals[33:] == [200] * 24
    au, sr = ca.assemble_audio(links, 4, 24.0)
    assert au.shape[1] == int(round(57 / 24.0 * sr))
    # a constant tone stays constant through the equal-gain fade (no dip, no bump)
    assert np.max(np.abs(au - 0.25)) < 1e-3
    res = ca.assemble_ltx(links, tmp_path / "take.mp4", shutil.which("ffmpeg"), crossfade=4, crf=10)
    assert res["frames"] == 57 and res["seams"] == [33]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_link_preview_keeps_only_the_links_own_frames_and_sound(tmp_path):
    # 9 carried frames (grey 100, a replay of the previous link), then 24 of its own (grey 200)
    link = tmp_path / "link2_draft.mkv"
    _write_link(link, _solid(100, 9) + _solid(200, 24), tone=0.25)
    out = tmp_path / "preview" / "link2_draft.mp4"
    res = ca.link_preview(link, 9, 24.0, out, shutil.which("ffmpeg"))
    assert res["frames"] == 24 and (res["width"], res["height"]) == (24, 16)
    with av.open(str(out)) as ct:
        frames = [f.to_ndarray(format="rgb24") for f in ct.decode(video=0)]
    with av.open(str(out)) as ct:
        assert ct.streams.audio and abs(float(ct.streams.audio[0].duration * ct.streams.audio[0].time_base)
                                        - 24 / 24.0) < 0.1
    assert len(frames) == 24 and abs(int(frames[0].mean()) - 200) <= 4
    assert sorted(p.name for p in out.parent.iterdir()) == ["link2_draft.mp4"]     # no .part / .wav left


def _pan_clip(path, brightness, n=96, h=112, w=192):
    """A textured pan (1 px per frame); brightness[i] is added to frame i."""
    cv2 = pytest.importorskip("cv2")
    rng = np.random.default_rng(0)
    tex = cv2.resize(rng.integers(40, 200, size=(h // 4, w // 2, 3)).astype(np.float32), (w * 2, h),
                     interpolation=cv2.INTER_NEAREST)
    return _write_frames(path, [np.clip(tex[:, i:i + w] + brightness[i], 0, 255).astype(np.uint8)
                                for i in range(n)])


def _write_frames(path, frames):
    h, w = frames[0].shape[:2]
    with av.open(str(path), "w") as ct:
        s = ct.add_stream("ffv1", rate=Fraction(24, 1))
        s.width, s.height, s.pix_fmt = w, h, "bgr0"
        for f in frames:
            for pkt in s.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(f), format="rgb24")):
                ct.mux(pkt)
        for pkt in s.encode():
            ct.mux(pkt)
    return path


def test_look_path_eases_a_look_step_at_a_carry_end_and_leaves_clean_ones(tmp_path):
    import seam_qa
    seam = 48
    step = _pan_clip(tmp_path / "step.mkv", np.where(np.arange(96) >= seam, 16.0, 0.0))
    plan = ca.look_path_plan(step, [seam, 80])               # 80: no room after the ramp
    assert list(plan) == [seam] and "lightness" in plan[seam]["fails"]
    assert sorted(plan[seam]["frames"]) == list(range(seam, seam + ca.LOOK_PATH_RAMP))
    assert plan[seam]["first_shift_L"] < 0                   # the first new frame is pulled back down
    out = list(ca.apply_look_path(ca._iter_rgb(step), plan))
    src = list(ca._iter_rgb(step))
    assert all(np.array_equal(out[i], src[i]) for i in range(96) if not seam <= i < seam + ca.LOOK_PATH_RAMP)
    rows0, diffs0 = seam_qa.analyze_frames(step)
    s0 = seam_qa.seam_metrics(rows0, diffs0, seam)
    rows, diffs = seam_qa.analyze_frames(_write_frames(tmp_path / "fixed.mkv", out))
    s = seam_qa.seam_metrics(rows, diffs, seam)
    # the step becomes a 16-frame line: its 6-frame step is ~a fifth of the jump
    assert "lightness" not in seam_qa.judge_seam(s)["fails"]
    assert abs(s["d_L"]) < 0.3 * abs(s0["d_L"]) and abs(s["d_sat"]) < 0.3 * abs(s0["d_sat"])
    assert [round(rows[i]["sat"]) for i in (seam - 1, seam + 8, seam + 16)] == \
        sorted([round(rows[i]["sat"]) for i in (seam - 1, seam + 8, seam + 16)], reverse=True)
    # a clean carry end is left exactly as it is
    smooth = _pan_clip(tmp_path / "smooth.mkv", np.zeros(96))
    assert ca.look_path_plan(smooth, [seam]) == {}


def test_early_switch_plays_the_incoming_carry(tmp_path):
    # link 2's carry (9 frames) differs from link 1's tail (120 vs 100): with the blend
    # at the carry start the take dissolves over its first 2 frames, then plays link 2's
    # own carried frames instead of link 1's
    l1, l2 = tmp_path / "l1.mkv", tmp_path / "l2.mkv"
    _write_link(l1, _solid(100, 33))
    _write_link(l2, _solid(120, 9) + _solid(200, 24))
    links = [ca.LinkMedia(l1, None, 33, 0), ca.LinkMedia(l2, None, 33, 9)]
    vals = [int(f[0, 0, 0]) for f in ca.assemble_frames(links, 2, blend_start=0)]
    assert len(vals) == 57
    assert vals[:24] == [100] * 24                        # link 1 up to the carry start
    assert vals[24:26] == [107, 113]                      # 100 + 20 * (1/3, 2/3)
    assert vals[26:33] == [120] * 7 and vals[33:] == [200] * 24
    # hard cut at the carry start
    cut = [int(f[0, 0, 0]) for f in ca.assemble_frames(links, 0, blend_start=0)]
    assert cut[:24] == [100] * 24 and cut[24:33] == [120] * 9
