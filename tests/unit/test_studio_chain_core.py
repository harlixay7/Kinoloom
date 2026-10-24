"""Chain-continuation tensor math (ComfyUI-LTX25-Studio/studio_chain_core.py).

Pure torch on CPU: exact token/tick bookkeeping for the H3 latent carry, the take
join, the seam-protected refine chunk plan, the LTX audio carry and the drift guard.
"""
import random
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

_NODE_DIR = (Path(__file__).resolve().parents[2] / "ComfyUI_LTX" / "custom_nodes"
             / "ComfyUI-LTX25-Studio")
sys.path.insert(0, str(_NODE_DIR))
import studio_chain_core as sc  # noqa: E402


def _h3_link(frames, h=4, w=6, base=0.0):
    t = sc.h3_tokens_for_clip(frames)
    v = torch.zeros((1, 24, t, h, w))
    for k in range(t):
        v[:, :, k] = base + k
    ticks = sc.h3_audio_ticks(frames)
    a = torch.arange(ticks, dtype=torch.float32).view(1, 1, 1, ticks).expand(1, 32, 2, ticks).clone() + base
    return v, a


# ------------------------------------------------------------------ H3 grid
@pytest.mark.parametrize("frames,tokens", [(5, 2), (22, 7), (39, 12), (124, 37), (192, 57), (345, 102)])
def test_h3_tokens_for_clip(frames, tokens):
    assert sc.h3_tokens_for_clip(frames) == tokens
    assert sc.h3_frames_for_tokens(tokens) == frames


def test_h3_rejects_off_grid():
    with pytest.raises(ValueError):
        sc.h3_tokens_for_clip(40)


def test_h3_window_is_grid_and_audio_aligned():
    assert sc.h3_window_frames(39, 124) == 192
    assert sc.h3_window_frames(0, 124) == 141       # 124 is on the grid but drifts audio
    for prefix in (0, 39):
        for new in range(20, 400, 7):
            f = sc.h3_window_frames(prefix, new)
            assert f >= prefix + new and sc.h3_is_clip_frames(f) and sc.h3_audio_aligned(f)


def test_h3_audio_ticks_match_empty_latent_rounding():
    for frames in (124, 141, 158, 192):
        assert sc.h3_audio_ticks(frames) == round(frames / 24 * 40)
    assert sc.h3_audio_ticks(39) == 65


# ------------------------------------------------------------- H3 carry
def test_h3_carry_copies_tail_tokens_and_ticks_exactly():
    src_v, src_a = _h3_link(192)
    dst_v, dst_a = torch.zeros_like(src_v), torch.zeros_like(src_a)
    out = sc.h3_carry_prefix(dst_v, dst_a, src_v, src_a, 39)
    assert out["tokens"] == 12 and out["ticks"] == 65 and out["audio_offset_ms"] == 0.0
    # tokens 45..56 of the source become tokens 0..11 of the window, same (1,4,4,4,4) phase
    assert torch.equal(out["video"][:, :, :12], src_v[:, :, 45:57])
    assert all((45 + j) % 5 == j % 5 for j in range(12))
    assert torch.count_nonzero(out["video"][:, :, 12:]) == 0
    assert torch.equal(out["audio"][..., :65], src_a[..., 255:320])
    assert out["video_mask"].shape == (1, 1, 57, 4, 6)
    assert out["video_mask"][:, :, :12].sum() == 0 and out["video_mask"][:, :, 12:].min() == 1
    assert out["audio_mask"].shape == (1, 1, 2, 320)
    assert out["audio_mask"][..., :65].sum() == 0 and out["audio_mask"][..., 65:].min() == 1
    # inputs untouched
    assert torch.count_nonzero(dst_v) == 0


def test_h3_carry_reports_misaligned_source():
    src_v, src_a = _h3_link(158)                    # 158 % 3 != 0
    dst_v, dst_a = _h3_link(192)
    out = sc.h3_carry_prefix(dst_v, dst_a, src_v, src_a, 39)
    assert 0 < abs(out["audio_offset_ms"]) <= 12.5


def test_h3_carry_validates_shapes():
    src_v, src_a = _h3_link(192)
    with pytest.raises(ValueError):
        sc.h3_carry_prefix(torch.zeros((1, 24, 57, 4, 8)), src_a.clone(), src_v, src_a, 39)
    with pytest.raises(ValueError):
        sc.h3_carry_prefix(src_v.clone(), src_a.clone(), src_v, src_a, 40)


# --------------------------------------------------------------- H3 join
def test_h3_concat_take_drops_prefixes_and_stays_on_grid():
    v1, a1 = _h3_link(192)
    v2, a2 = _h3_link(192, base=100.0)
    carry = sc.h3_carry_prefix(v2, a2, v1, a1, 39)
    v2, a2 = carry["video"], carry["audio"]
    v3, a3 = _h3_link(192, base=200.0)
    carry = sc.h3_carry_prefix(v3, a3, v2, a2, 39)
    v3, a3 = carry["video"], carry["audio"]
    take = sc.h3_concat_take([(v1, a1, 0), (v2, a2, 39), (v3, a3, 39)])
    assert take["frames"] == 192 + 153 + 153 == 498
    assert sc.h3_is_clip_frames(take["frames"])
    assert take["video"].shape[2] == sc.h3_tokens_for_clip(498) == 57 + 45 + 45
    assert take["audio"].shape[-1] == sc.h3_audio_ticks(498) == 320 + 255 + 255
    assert take["seam_frames"] == [192, 345] and take["seam_tokens"] == [57, 102]
    # link 2's first fresh token (its token 12) directly follows link 1's last token
    assert torch.equal(take["video"][:, :, 57], v2[:, :, 12])
    assert torch.equal(take["video"][:, :, 56], v1[:, :, 56])


def test_h3_concat_take_requires_prefix_on_later_links():
    v1, a1 = _h3_link(192)
    with pytest.raises(ValueError):
        sc.h3_concat_take([(v1, a1, 0), (v1, a1, 0)])


# ----------------------------------------------------- refine chunk plan
def _check_plan(bounds, T, tc, to):
    assert bounds[0][0] == 0 and bounds[-1][1] == T
    for (a0, a1), (b0, b1) in zip(bounds, bounds[1:]):
        assert b0 == a1 - to, (bounds,)
    for k0, k1 in bounds:
        assert k0 % 5 == 0 and (k1 - k0 - 2) % 5 == 0 and k1 - k0 <= tc


def test_plan_chunks_covers_take_without_protection():
    bounds = sc.h3_plan_chunks(102, 37, 7)
    _check_plan(bounds, 102, 37, 7)
    assert bounds == [(0, 37), (30, 67), (60, 97), (90, 102)]


def test_plan_chunks_moves_junctions_off_seams():
    bounds = sc.h3_plan_chunks(102, 37, 7, protected_tokens=[66], margin_tokens=5)
    _check_plan(bounds, 102, 37, 7)
    assert sc.h3_junction_conflicts(bounds, [66], 5) == []


def test_plan_chunks_random_properties():
    rng = random.Random(7)
    for _ in range(300):
        T = 5 * rng.randint(3, 120) + 2
        tc = 5 * rng.randint(4, 12) + 2
        to = 5 * rng.randint(0, 2) + 2
        if to >= tc - 10:
            continue
        seams = sorted(rng.sample(range(5, T), k=min(4, T - 6)))
        bounds = sc.h3_plan_chunks(T, tc, to, seams, margin_tokens=3)
        _check_plan(bounds, T, tc, to)


# --------------------------------------------------------------- LTX audio
@pytest.mark.parametrize("src_frames,start", [(145, 125), (121, 100)])
def test_ltx_audio_carry_is_time_aligned(src_frames, start):
    n_src = round(src_frames / 24 * 25)
    src = torch.arange(n_src, dtype=torch.float32).view(1, 1, n_src, 1).expand(1, 8, n_src, 16).clone()
    dst = torch.zeros((1, 8, 151, 16))
    out = sc.ltx_audio_carry(dst, src, src_frames, 25)
    assert out["latents"] == 26 and abs(out["offset_ms"]) < 1e-9
    assert torch.equal(out["audio"][:, :, :26], src[:, :, start:start + 26])
    assert out["mask"].shape == (1, 1, 151, 16)
    assert out["mask"][:, :, :26].sum() == 0 and out["mask"][:, :, 26:].min() == 1


def _carried_chain(frames, carry=25):
    """Audio latents of a chain whose links carry exactly (a global time ramp, so any
    misplaced latent shows up as a wrong value)."""
    lats = []
    for i, f in enumerate(frames):
        n = round(f / 24 * 25)
        if i == 0:
            lats.append(torch.arange(n, dtype=torch.float32).view(1, 1, n, 1).expand(1, 8, n, 16).clone())
            continue
        src = lats[-1]
        out = sc.ltx_audio_carry(torch.full((1, 8, n, 16), -1.0), src, frames[i - 1], carry)
        k, lat = out["latents"], out["audio"]
        # new content continues the take's time ramp from the carried span's first value
        base = float(lat[0, 0, 0, 0])
        lat[:, :, k:] = (base + torch.arange(k, n, dtype=torch.float32)).view(1, 1, -1, 1)
        lats.append(lat)
    return lats


@pytest.mark.parametrize("frames", [[121, 145, 145, 145], [49, 49], [121, 145]])
def test_ltx_audio_join_is_one_continuous_stream(frames):
    lats = _carried_chain(frames)
    out, info = sc.ltx_audio_join(lats, frames, 25)
    total = frames[0] + sum(f - 25 for f in frames[1:])
    assert info["take_frames"] == total
    assert info["latents"] == info["expected_latents"] == round(total / 24 * 25)
    # the global time ramp comes through without a gap or a repeat
    assert torch.equal(out[0, 0, :, 0], torch.arange(out.shape[2], dtype=torch.float32))
    assert all(j["carry_exact"] and abs(j["drift_ms"]) < 1e-6 for j in info["joins"])


def test_ltx_audio_join_flags_a_carry_that_was_not_pinned():
    lats = _carried_chain([121, 145])
    lats[1][:, :, 3] += 0.5
    _, info = sc.ltx_audio_join(lats, [121, 145], 25)
    assert not info["joins"][0]["carry_exact"] and info["joins"][0]["carry_max_err"] == 0.5


# --------------------------------------------------------------- seam adain
def test_seam_adain_matches_the_first_new_latent_and_fades_out():
    g = torch.Generator().manual_seed(4)
    x = torch.randn((1, 8, 16, 4, 4), generator=g)
    x[:, :, 4:] = x[:, :, 4:] * 0.6 - 0.8          # the new content steps in look
    out, info = sc.seam_adain(x, carry=4, ramp=4, factor=1.0)
    assert info["applied"] and torch.equal(out[:, :, :4], x[:, :, :4])
    assert torch.equal(out[:, :, 8:], x[:, :, 8:])           # untouched after the ramp
    ref_mu = x[:, :, 2:4].mean()
    gaps = [abs(float(out[:, :, t].mean() - ref_mu)) for t in range(4, 8)]
    assert gaps[0] < abs(float(x[:, :, 4].mean() - ref_mu)) * 0.35 and gaps == sorted(gaps)
    same, info0 = sc.seam_adain(x, carry=4, ramp=4, factor=0.0)
    assert same is x and not info0["applied"]
    assert not sc.seam_adain(x, carry=0, ramp=4)[1]["applied"]


def _flash_latent(t=16):
    g = torch.Generator().manual_seed(5)
    # 16x16 per latent: enough positions that per-latent std stays inside the ratio clamp
    x = torch.randn((1, 8, t, 16, 16), generator=g)
    for k in range(t):                                   # the content's look drifts steadily
        x[:, :, k] = x[:, :, k] * (1 - 0.03 * k) + 0.1 * k
    x[:, :, 4] += 1.5                                    # and the first new latent flashes
    return x


def _flash(y, t=4):
    """How far latent t's mean sits off the line through its neighbours."""
    m = [float(y[:, :, k].mean()) for k in (t - 1, t, t + 1)]
    return abs(m[1] - (m[0] + m[2]) / 2)


def test_seam_adain_path_removes_a_one_latent_flash_and_lands_on_the_next_look():
    x = _flash_latent()
    out, info = sc.seam_adain(x, carry=4, ramp=4, factor=1.0, mode="path")
    assert info["applied"] and info["mode"] == "path" and info["ramp"] == 4
    assert torch.equal(out[:, :, :4], x[:, :, :4]) and torch.equal(out[:, :, 8:], x[:, :, 8:])
    # per channel, the look walks in equal steps from the last carried latent to latent 8
    mu = out.mean(dim=(0, 3, 4))
    steps = mu[:, 4:9] - mu[:, 3:8]
    assert torch.allclose(steps, steps[:, :1].expand_as(steps), atol=1e-5)
    sd = out.std(dim=(0, 3, 4)).log()
    assert torch.allclose(sd[:, 4:9] - sd[:, 3:8], (sd[:, 4:5] - sd[:, 3:4]).expand(-1, 5), atol=1e-4)
    assert _flash(out) < 1e-4
    window, _ = sc.seam_adain(x, carry=4, ramp=4, factor=1.0)
    assert _flash(window) > 0.5                          # one block transform keeps the flash
    half, _ = sc.seam_adain(x, carry=4, ramp=4, factor=0.5, mode="path")
    assert abs(_flash(half) - _flash(x) / 2) < 0.05


def test_seam_adain_path_needs_a_latent_after_the_ramp():
    x = _flash_latent(6)                                 # carry 4, then only 2 new latents
    out, info = sc.seam_adain(x, carry=4, ramp=4, factor=1.0, mode="path")
    assert info["ramp"] == 1 and torch.equal(out[:, :, 5], x[:, :, 5])
    short = _flash_latent(5)
    same, info = sc.seam_adain(short, carry=4, ramp=4, factor=1.0, mode="path")
    assert same is short and not info["applied"]
    with pytest.raises(ValueError):
        sc.seam_adain(x, carry=4, ramp=4, mode="smooth")


def _h3_phased_latent(t=27, carry=12):
    """An H3-like video latent: every 5th token (the 1-frame one) sits far off the
    others, the look drifts slowly, and the new content steps up at the carry end."""
    g = torch.Generator().manual_seed(6)
    x = torch.randn((1, 6, t, 16, 16), generator=g)
    for k in range(t):
        x[:, :, k] += 0.02 * k + (2.0 if k % 5 == 0 else 0.0)
        if k % 5 == 0:
            x[:, :, k] *= 1.4
    x[:, :, carry:] += 0.8
    return x


def test_seam_adain_path_period_matches_each_phase_on_its_own_line():
    x = _h3_phased_latent()
    out, info = sc.seam_adain(x, carry=12, ramp=10, factor=1.0, mode="path", period=5)
    assert info["applied"] and info["ramp"] == 10 and info["period"] == 5
    assert torch.equal(out[:, :, :12], x[:, :, :12]) and torch.equal(out[:, :, 22:], x[:, :, 22:])
    mu = out.mean(dim=(0, 3, 4))
    for p in range(5):
        # each phase walks in equal steps from its last carried token to its first after the ramp
        idx = [k for k in range(7, 27) if k % 5 == p]
        seg = mu[:, idx]
        steps = seg[:, 1:] - seg[:, :-1]
        assert torch.allclose(steps, steps[:, :1].expand_as(steps), atol=1e-4)
    # the 1-frame tokens keep their own level: the phase structure survives
    single = mu[:, [15, 20]].mean() - mu[:, [14, 16, 19, 21]].mean()
    assert float(single) > 1.5
    # a plain path through neighbouring tokens flattens it
    flat, _ = sc.seam_adain(x, carry=12, ramp=10, factor=1.0, mode="path")
    fm = flat.mean(dim=(0, 3, 4))
    assert float(fm[:, [15, 20]].mean() - fm[:, [14, 16, 19, 21]].mean()) < 0.5


def test_seam_adain_period_keeps_a_full_period_after_the_ramp_and_validates():
    x = _h3_phased_latent(t=22)
    out, info = sc.seam_adain(x, carry=12, ramp=10, factor=1.0, mode="path", period=5)
    assert info["ramp"] == 5 and torch.equal(out[:, :, 17:], x[:, :, 17:])
    _, info1 = sc.seam_adain(x, carry=12, ramp=10, factor=1.0, mode="path", period=1)
    assert "period" not in info1 and info1["ramp"] == 9
    with pytest.raises(ValueError):
        sc.seam_adain(x, carry=12, ramp=4, mode="window", period=5)
    with pytest.raises(ValueError):
        sc.seam_adain(x, carry=3, ramp=4, mode="path", period=5)
    with pytest.raises(ValueError):
        sc.seam_adain(x, carry=12, ramp=4, mode="path", period=0)


# ---------------------------------------------------------------- eviction
def test_eviction_keep_selects_by_role():
    names = ["MiniMaxH3", "MiniMaxH3TEModel_", "MiniMaxH3VideoVAE", "LTXAVTEModel", "SD1ClipModel_CLIP"]
    assert sc.eviction_keep(names, False, False) is None                                   # unload all
    assert sc.eviction_keep(names, True, False) == [False, True, False, True, True]       # before a decode
    assert sc.eviction_keep(names, False, True) == [True, False, True, False, False]      # after the encode
    assert sc.eviction_keep(names, True, True) == sc.eviction_keep(names, False, True)
    assert sc.eviction_keep([], False, True) == []


# ------------------------------------------------------------- latent carry
def test_ltx_latent_carry_anchor_plus_exact_tail():
    g = torch.Generator().manual_seed(11)
    src = torch.randn((1, 8, 16, 3, 5), generator=g)
    tgt = torch.zeros((1, 8, 19, 3, 5))
    first = torch.randn((1, 8, 1, 3, 5), generator=g)
    out = sc.ltx_latent_carry(tgt, first, src, 4)
    assert torch.equal(out[:, :, :1], first) and torch.equal(out[:, :, 1:4], src[:, :, 13:16])
    assert torch.equal(out[:, :, 4:], tgt[:, :, 4:]) and not torch.equal(tgt[:, :, :1], first)   # input untouched
    with pytest.raises(ValueError):
        sc.ltx_latent_carry(tgt, first[:, :, :, :2], src, 4)
    with pytest.raises(ValueError):
        sc.ltx_latent_carry(tgt, first, src[:, :, :, :2], 4)
    with pytest.raises(ValueError):
        sc.ltx_latent_carry(tgt, first, src, 20)


def _ltx_chain_latents(ts, carry=4, seed=3):
    """Links whose carried latents 1..carry-1 repeat the previous link's last ones."""
    g = torch.Generator().manual_seed(seed)
    out = []
    for i, t in enumerate(ts):
        x = torch.randn((1, 4, t, 2, 3), generator=g)
        if i:
            x[:, :, 1:carry] = out[-1][:, :, -(carry - 1):]
        out.append(x)
    return out


@pytest.mark.parametrize("join", [1, 2, 4])
def test_ltx_take_latents_joins_on_one_latent(join):
    lats = _ltx_chain_latents([16, 19, 19])
    take, info = sc.ltx_take_latents(lats, carry=4, join=join)
    # the take has every pixel frame once: 121 + 2 * (145 - 25)
    assert take.shape[2] == 16 + 2 * 15 and info["frames"] == 121 + 2 * 120 == 8 * (take.shape[2] - 1) + 1
    a = 16 - 4 + join
    assert torch.equal(take[:, :, :a], lats[0][:, :, :a])
    assert torch.equal(take[:, :, a:a + 15], lats[1][:, :, join:join + 15])
    assert torch.equal(take[:, :, -(19 - join):], lats[2][:, :, join:])
    assert [j["take_latent"] for j in info["joins"]] == [a, a + 15]
    assert info["joins"][0]["bend_rmse"] == [0.0, 0.0, 0.0]       # exact carry


def test_ltx_take_latents_blend_crossfades_the_overlap():
    lats = _ltx_chain_latents([16, 19, 19])
    lats[1][:, :, 1:4] += 1.0                    # the new link re-rendered its carry
    hard, _ = sc.ltx_take_latents(lats, carry=4, join=4)
    take, info = sc.ltx_take_latents(lats, carry=4, join=4, blend=3)
    assert take.shape == hard.shape and info["frames"] == 361
    # latents 13..15: link 1's, moved 1/4, 2/4, 3/4 of the way to link 2's re-render
    for j, w in zip(range(13, 16), (0.25, 0.5, 0.75)):
        assert torch.allclose(take[:, :, j], lats[0][:, :, j] + w, atol=1e-6)
    assert torch.equal(take[:, :, :13], lats[0][:, :, :13])
    assert torch.equal(take[:, :, 16:28], lats[1][:, :, 4:16])          # until link 3's blend starts
    assert torch.allclose(take[:, :, 28], lats[1][:, :, 16] * 0.75 + lats[2][:, :, 1] * 0.25, atol=1e-6)
    assert torch.equal(take[:, :, 31:], lats[2][:, :, 4:])
    assert info["joins"][0]["take_latent"] == 16 and info["joins"][0]["blend_from_frame"] == 8 * 13 - 7
    with pytest.raises(ValueError):
        sc.ltx_take_latents(lats, carry=4, join=2, blend=2)     # would reach the anchor


def test_ltx_take_latents_validates():
    lats = _ltx_chain_latents([16, 19])
    with pytest.raises(ValueError):
        sc.ltx_take_latents(lats, carry=4, join=0)
    with pytest.raises(ValueError):
        sc.ltx_take_latents([lats[0], lats[1][:, :, :, :1]], carry=4)
    with pytest.raises(ValueError):
        sc.ltx_take_latents([lats[0], lats[1][:, :, :4]], carry=4)


def test_decode_windows_single_window_is_a_plain_decode():
    assert sc.decode_windows(19, window=24) == [{"a": 0, "b": 19, "use": [0, 145], "blend": 0}]


@pytest.mark.parametrize("total", list(range(20, 140, 7)) + [25, 26, 61])
@pytest.mark.parametrize("window,context,overlap", [(24, 2, 2), (12, 2, 1), (8, 1, 1), (30, 3, 2)])
def test_decode_windows_cover_the_take_once_with_blends(total, window, context, overlap):
    plan = sc.decode_windows(total, window, context, overlap)
    end = 8 * (total - 1) + 1
    assert plan[0]["use"][0] == 0 and plan[-1]["use"][1] == end and plan[-1]["b"] == total
    for prev, cur in zip(plan, plan[1:]):
        assert cur["blend"] == prev["use"][1] - cur["use"][0] >= 1           # overlapping, cross-faded
        assert cur["use"][0] > prev["use"][0] and cur["use"][1] > prev["use"][1]
        assert cur["a"] > 0 and cur["use"][0] >= 8 * (cur["a"] + context)    # left context dropped
        assert prev["use"][1] <= 8 * (prev["b"] - 1 - context) + 1           # right context dropped
    for a, c in zip(plan, plan[2:]):
        assert c["use"][0] >= a["use"][1]                                    # never three windows at once
    for w in plan:
        assert 0 <= w["a"] < w["b"] <= total and w["b"] - w["a"] <= max(window, 2 * context + overlap + 1)
    # every frame is written exactly once: the used spans minus the blended overlaps
    assert sum(w["use"][1] - w["use"][0] - w["blend"] for w in plan) == end


# ------------------------------------------------------------ carry restore
def _soft_pinned(seed=7):
    """An encoded carry, and a sampled latent whose carry is gain*enc + shift +
    residual (latent 0 hard-pinned, i.e. exact), the way a soft pin comes out."""
    g = torch.Generator().manual_seed(seed)
    enc = torch.randn((1, 8, 10, 6, 6), generator=g)
    x = torch.randn((1, 8, 10, 6, 6), generator=g)          # free latents: anything
    resid = 0.05 * torch.randn((1, 8, 4, 6, 6), generator=g)
    resid -= resid.mean(dim=(-2, -1), keepdim=True)
    gain = 1.0 + 0.08 * torch.rand((1, 8, 1, 1, 1), generator=g)
    shift = 0.04 * torch.randn((1, 8, 1, 1, 1), generator=g)
    x[:, :, :4] = gain * enc[:, :, :4] + shift + resid
    x[:, :, 0] = enc[:, :, 0]
    return x, enc, resid


def test_carry_restore_removes_gain_and_shift_keeps_residual():
    x, enc, resid = _soft_pinned()
    out, info = sc.carry_restore(x, enc, carry=4)
    assert info["applied"] and info["latents"][0] == {"t": 0, "pinned": True}
    assert torch.equal(out[:, :, 4:], x[:, :, 4:])            # free latents untouched
    assert torch.equal(out[:, :, 0], x[:, :, 0])              # hard pin untouched
    # what is left is encoded + the part of the residual orthogonal to it
    for t in range(1, 4):
        dev = out[:, :, t] - enc[:, :, t]
        assert float(dev.mean(dim=(-2, -1)).abs().max()) < 1e-5
        assert float(dev.pow(2).mean().sqrt()) <= float(resid[:, :, t].pow(2).mean().sqrt()) + 1e-6
        assert info["latents"][t]["gain_mean"] > 1.0 and info["latents"][t]["rmse_after"] < info["latents"][t]["rmse_before"]


def test_carry_restore_is_idempotent_and_factor_scales():
    x, enc, _ = _soft_pinned(8)
    once, _ = sc.carry_restore(x, enc, carry=4)
    twice, info = sc.carry_restore(once, enc, carry=4)
    assert torch.allclose(once, twice, atol=1e-5)
    assert all(abs(r["gain_mean"] - 1.0) < 1e-4 for r in info["latents"][1:])
    half, _ = sc.carry_restore(x, enc, carry=4, factor=0.5)
    assert torch.allclose(half, (x + once) / 2, atol=1e-5)
    same, info0 = sc.carry_restore(x, enc, carry=4, factor=0.0)
    assert same is x and not info0["applied"]


def test_carry_restore_validates_shapes():
    x, enc, _ = _soft_pinned()
    with pytest.raises(ValueError):
        sc.carry_restore(x, enc[:, :, :, :5], carry=4)
    with pytest.raises(ValueError):
        sc.carry_restore(x, enc[:, :, :3], carry=4)


# ------------------------------------------------------------- drift guard
def test_adain_matches_reference_stats_and_keeps_prefix():
    g = torch.Generator().manual_seed(0)
    ref = torch.randn((1, 8, 12, 6, 6), generator=g) * 0.8 + 0.3
    x = torch.randn((1, 8, 12, 6, 6), generator=g) * 0.95 - 0.2
    out, info = sc.adain_to_reference(x, ref, 1.0, skip_frames=4, ramp_frames=0)
    assert info["applied"]
    assert torch.equal(out[:, :, :4], x[:, :, :4])
    tail = out[:, :, 4:]
    assert torch.allclose(tail.mean(dim=(0, 2, 3, 4)), ref.mean(dim=(0, 2, 3, 4)), atol=1e-4)
    assert torch.allclose(tail.std(dim=(0, 2, 3, 4)), ref.std(dim=(0, 2, 3, 4)), atol=1e-3)


def test_adain_ramp_is_monotonic_and_factor_zero_is_identity():
    g = torch.Generator().manual_seed(1)
    ref = torch.randn((1, 4, 10, 4, 4), generator=g) + 1.0
    # identical content in every frame -> the per-frame shift is proportional to the weight
    x = torch.randn((1, 4, 1, 4, 4), generator=g).expand(1, 4, 10, 4, 4).clone()
    same, info = sc.adain_to_reference(x, ref, 0.0, skip_frames=2)
    assert same is x and not info["applied"]
    out, _ = sc.adain_to_reference(x, ref, 0.5, skip_frames=2, ramp_frames=4)
    shift = (out - x).abs().mean(dim=(0, 1, 3, 4))
    assert torch.all(shift[:2] == 0)
    assert all(shift[i] <= shift[i + 1] + 1e-6 for i in range(2, 9))
    assert torch.allclose(shift[2] * 4, shift[5], rtol=1e-4)     # ramp 1/4 -> full
    assert torch.allclose(shift[5], shift[9], rtol=1e-4)


def test_adain_clamps_contrast_ratio():
    ref = torch.randn((1, 2, 6, 4, 4)) * 10
    x = torch.randn((1, 2, 6, 4, 4))
    _, info = sc.adain_to_reference(x, ref, 1.0, max_ratio=1.5)
    assert info["std_ratio_max"] <= 1.5 + 1e-6


def test_ltx_grid_helpers():
    assert sc.ltx_latent_frames(145) == 19 and sc.ltx_latent_frames(25) == 4
    assert sc.ceil_to_grid(130, 1, 8) == 137
    with pytest.raises(ValueError):
        sc.ltx_latent_frames(144)
