"""Seam QA on synthetic clips with known answers: a step at the seam fails, the
same change spread over the take (a lighting move) passes, a jump hidden inside a
carry window is caught by the window pop, and the overlap PSNR is exact."""
from fractions import Fraction

import numpy as np
import pytest

import seam_qa

av = pytest.importorskip("av")
cv2 = pytest.importorskip("cv2")

H, W, N, SEAM = 112, 192, 96, 48


def _texture(seed=0):
    rng = np.random.default_rng(seed)
    t = rng.integers(40, 200, size=(H // 4, W // 4, 3)).astype(np.float32)
    return cv2.resize(t, (W * 2, H), interpolation=cv2.INTER_NEAREST)     # wide, to pan across


def _clip(path, brightness, shift):
    """brightness[i]: offset added to frame i; shift[i]: horizontal pan in px."""
    tex = _texture()
    with av.open(str(path), "w") as ct:
        s = ct.add_stream("ffv1", rate=Fraction(24, 1))
        s.width, s.height, s.pix_fmt = W, H, "bgr0"
        for i in range(N):
            x = int(shift[i])
            f = np.clip(tex[:, x:x + W] + brightness[i], 0, 255).astype(np.uint8)
            for pkt in s.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(f), format="rgb24")):
                ct.mux(pkt)
        for pkt in s.encode():
            ct.mux(pkt)
    return path


def _seam(tmp_path, name, brightness, shift, span=0):
    rep = seam_qa.analyze_take(_clip(tmp_path / f"{name}.mkv", brightness, shift), [SEAM], span=span)
    return rep.seams[0]


PAN = np.arange(N) * 1.0                      # steady motion: 1 px per frame


def test_smooth_take_passes(tmp_path):
    s = _seam(tmp_path, "smooth", np.zeros(N), PAN)
    assert s["pass"], s
    assert 0.7 < s["pop_ratio"] < 1.3


def test_a_lightness_and_motion_step_at_the_seam_fails(tmp_path):
    b = np.where(np.arange(N) >= SEAM, 14.0, 0.0)
    shift = PAN + np.where(np.arange(N) >= SEAM, 12, 0)              # the content jumps too
    s = _seam(tmp_path, "step", b, shift)
    assert not s["pass"] and {"pop", "lightness"} <= set(s["fails"])


def test_a_lighting_move_through_the_seam_is_not_a_seam_defect(tmp_path):
    # the same total change, but the take changes at that rate everywhere
    ramp = np.arange(N) * 1.2
    s = _seam(tmp_path, "ramp", ramp, PAN)
    assert abs(s["d_L"]) > seam_qa.GATES["d_L"]                     # above the absolute gate ...
    assert s["d_L_local"] > 0 and s["pass"], s                       # ... but usual for the moment


def test_a_step_on_top_of_a_lighting_move_still_fails(tmp_path):
    b = np.arange(N) * 1.2 + np.where(np.arange(N) >= SEAM, 25.0, 0.0)
    s = _seam(tmp_path, "ramp_step", b, PAN)
    assert "lightness" in s["fails"]


def test_carry_end_check_judges_only_what_the_new_link_decides(tmp_path):
    step = seam_qa.carry_end_check(_clip(tmp_path / "step.mkv", np.where(np.arange(N) >= SEAM, 14.0, 0.0),
                                         PAN + np.where(np.arange(N) >= SEAM, 12, 0)), SEAM)
    assert not step["pass"] and {"pop", "lightness"} <= set(step["fails"]) and step["score"] > 1.0
    smooth = seam_qa.carry_end_check(_clip(tmp_path / "smooth.mkv", np.zeros(N), PAN), SEAM)
    assert smooth["pass"] and smooth["score"] < step["score"]
    # a new texture from the carry end on (a content change: detail) is not judged
    tex2 = _texture(1)

    def detail_clip(path):
        tex = _texture()
        with av.open(str(path), "w") as ct:
            s = ct.add_stream("ffv1", rate=Fraction(24, 1))
            s.width, s.height, s.pix_fmt = W, H, "bgr0"
            for i in range(N):
                t = tex if i < SEAM else (0.5 * tex + 0.5 * cv2.GaussianBlur(tex2, (0, 0), 4))
                f = np.clip(t[:, i:i + W], 0, 255).astype(np.uint8)
                for pkt in s.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(f), format="rgb24")):
                    ct.mux(pkt)
            for pkt in s.encode():
                ct.mux(pkt)
        return path
    full = seam_qa.analyze_take(detail_clip(tmp_path / "detail.mkv"), [SEAM]).seams[0]
    own = seam_qa.carry_end_check(tmp_path / "detail.mkv", SEAM)
    assert set(own["fails"]) == set(full["fails"]) & set(seam_qa.CARRY_END_CHECKS)
    assert "detail" not in own["fails"]
    assert seam_qa.carry_end_check(tmp_path / "smooth.mkv", 3)["skipped"]     # no room to judge


def test_a_sound_starting_out_of_silence_is_an_onset_not_a_level_step():
    sr = 48000
    t = np.arange(sr) / sr
    tone = 0.3 * np.sin(2 * np.pi * 220 * t)
    onset = np.where(t >= 0.5, tone, 1e-5 * tone)                  # silence, then a breath
    a = seam_qa.audio_seam(onset, sr, 0.5)
    assert a["onset"] and a["step_db"] > 30
    s = {"d_sat": 0.0, "d_L": 0.0, "d_contrast": 0.0, "pop_ratio": 1.0, "audio": a}
    assert "audio_level" not in seam_qa.judge_seam(s)["fails"]
    jump = np.where(t >= 0.5, tone, 0.1 * tone)                    # two sounds at different levels
    b = seam_qa.audio_seam(jump, sr, 0.5)
    assert not b["onset"] and "audio_level" in seam_qa.judge_seam({**s, "audio": b})["fails"]
    assert not seam_qa.audio_seam(1e-5 * tone, sr, 0.5)["onset"]   # silence on both sides


def test_window_pop_finds_a_jump_inside_the_carry(tmp_path):
    shift = PAN + np.where(np.arange(N) >= SEAM - 10, 12, 0)         # jump 10 frames before the seam
    s = _seam(tmp_path, "inner", np.zeros(N), shift, span=25)
    assert s["pop_ratio"] < 1.3                                       # the seam frame itself is clean
    assert s["window_pop"] > 3 and s["window_peak_frame"] == SEAM - 10 and "window_pop" in s["fails"]


def test_overlap_fidelity_compares_the_carried_frames(tmp_path):
    a = _clip(tmp_path / "a.mkv", np.zeros(N), PAN)
    # link b opens with a's last 10 frames, then pans on
    shift_b = np.concatenate([PAN[N - 10:], PAN[-1] + 1 + np.arange(N - 10)])
    b = _clip(tmp_path / "b.mkv", np.zeros(N), shift_b)
    o = seam_qa.overlap_fidelity(a, b, 10)
    assert o["pass"] and o["min"] == 99.0 and len(o["psnr"]) == 10
    c = _clip(tmp_path / "c.mkv", np.full(N, 9.0), shift_b)           # same frames, brighter
    oc = seam_qa.overlap_fidelity(a, c, 10)
    assert not oc["pass"] and oc["min"] < seam_qa.GATES["overlap_psnr"]
