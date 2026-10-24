"""Tensor math for Video Studio chain continuations. Pure torch, no ComfyUI imports,
so every function here is unit-tested on CPU (tests/unit/test_studio_chain_core.py).

Grids
-----
MiniMax H3: pixel frames come in groups of 17, encoded as the token pattern
(1, 4, 4, 4, 4). A clip of 17k+5 frames has 5k+2 video tokens, and audio latents run
at 40 Hz, i.e. 5/3 per pixel frame at 24 fps. A carried prefix of 17n+5 frames is
exactly 5n+2 tokens, and the last 5n+2 tokens of any clip have the same token
pattern as the first 5n+2 tokens of a new one, so a latent tail can be copied into
the head of the next window without a VAE round trip. Audio lines up with video
only when the frame count is divisible by 3 (39, 90, 141, 192, ...).

LTX-2.5: 1 + 8k pixel frames, the first latent frame encodes a single pixel frame,
audio at 25 latents per second.
"""
from __future__ import annotations

import math
import statistics
from fractions import Fraction

import torch

H3_FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
H3_AUDIO_PER_FRAME = Fraction(5, 3)          # 40 Hz audio latents / 24 fps video
H3_VIDEO_CHANNELS = 24
H3_AUDIO_CHANNELS = 32


# --------------------------------------------------------------------- H3 grid
def h3_frames_for_tokens(n: int) -> int:
    return sum(H3_FRAME_PER_TOKEN[i % 5] for i in range(int(n)))


def h3_is_clip_frames(frames: int) -> bool:
    return frames >= 5 and (frames - 5) % 17 == 0


def h3_tokens_for_clip(frames: int) -> int:
    if not h3_is_clip_frames(frames):
        raise ValueError(f"{frames} frames is not on the H3 17k+5 grid")
    return (frames - 5) // 17 * 5 + 2


def h3_audio_ticks(frames: int) -> int:
    """Audio latent count for `frames` pixel frames, the same rounding the empty
    H3 latent uses (round(duration * 40))."""
    return round(Fraction(int(frames)) * H3_AUDIO_PER_FRAME)


def h3_audio_aligned(frames: int) -> bool:
    return (int(frames) * H3_AUDIO_PER_FRAME).denominator == 1


def h3_window_frames(prefix_frames: int, new_frames: int) -> int:
    """Smallest window >= prefix + new that is on the 17k+5 grid AND audio-aligned
    (divisible by 3), so per-link audio never drifts against the video."""
    f = max(5, int(prefix_frames) + int(new_frames))
    while not (h3_is_clip_frames(f) and h3_audio_aligned(f)):
        f += 1
    return f


def _check_h3_video(v: torch.Tensor, name: str) -> int:
    if v.ndim != 5 or v.shape[1] != H3_VIDEO_CHANNELS:
        raise ValueError(f"{name}: expected an H3 video latent [B,24,T,H,W], got {list(v.shape)}")
    frames = h3_frames_for_tokens(v.shape[2])
    if not h3_is_clip_frames(frames):
        raise ValueError(f"{name}: {v.shape[2]} tokens = {frames} frames, not a 17k+5 clip")
    return frames


def h3_carry_prefix(dst_video: torch.Tensor, dst_audio: torch.Tensor,
                    src_video: torch.Tensor, src_audio: torch.Tensor,
                    carry_frames: int) -> dict:
    """Copy the last `carry_frames` of a previous H3 clip (video tokens + audio ticks)
    into the head of a new window and build the matching noise masks (0 = keep).

    Returns {"video", "audio", "video_mask", "audio_mask", "tokens", "ticks",
    "audio_offset_ms"}; masks are [B,1,T,H,W] and [B,1,2,Ta]."""
    src_frames = _check_h3_video(src_video, "source")
    dst_frames = _check_h3_video(dst_video, "target")
    if src_video.shape[0] != dst_video.shape[0] or src_video.shape[3:] != dst_video.shape[3:]:
        raise ValueError(f"source {list(src_video.shape)} and target {list(dst_video.shape)} "
                         "differ in batch or spatial size")
    if src_audio.ndim != 4 or dst_audio.ndim != 4 or src_audio.shape[1] != H3_AUDIO_CHANNELS:
        raise ValueError("expected H3 audio latents [B,32,2,T]")
    tc = h3_tokens_for_clip(int(carry_frames))
    if carry_frames >= dst_frames:
        raise ValueError(f"carry of {carry_frames} frames leaves nothing new in a "
                         f"{dst_frames}-frame window")
    if tc > src_video.shape[2]:
        raise ValueError(f"carry of {carry_frames} frames is longer than the source clip")

    video = dst_video.clone()
    video[:, :, :tc] = src_video[:, :, src_video.shape[2] - tc:].to(video.dtype)

    ac = h3_audio_ticks(carry_frames)
    start_exact = Fraction(src_frames - int(carry_frames)) * H3_AUDIO_PER_FRAME
    start = round(start_exact)
    start = max(0, min(start, src_audio.shape[-1] - ac))
    offset_ms = float((Fraction(start) - start_exact) / 40 * 1000)
    if ac > dst_audio.shape[-1] or start + ac > src_audio.shape[-1]:
        raise ValueError("carried audio does not fit the source/target audio latents")
    audio = dst_audio.clone()
    audio[..., :ac] = src_audio[..., start:start + ac].to(audio.dtype)

    b, _, t, h, w = video.shape
    video_mask = torch.ones((b, 1, t, h, w), dtype=torch.float32)
    video_mask[:, :, :tc] = 0.0
    audio_mask = torch.ones((b, 1, dst_audio.shape[2], dst_audio.shape[3]), dtype=torch.float32)
    audio_mask[..., :ac] = 0.0
    return {"video": video, "audio": audio, "video_mask": video_mask, "audio_mask": audio_mask,
            "tokens": tc, "ticks": ac, "audio_offset_ms": offset_ms}


def h3_concat_take(links: list[tuple[torch.Tensor, torch.Tensor, int]]) -> dict:
    """Join chain links into one take: link 1 whole, every later link without its
    carried prefix. links = [(video, audio, prefix_frames)], prefix 0 for link 1.

    Returns {"video", "audio", "frames", "seam_frames", "seam_tokens"}; seam_frames
    are the first pixel frame of each later link's new content."""
    if not links:
        raise ValueError("no links")
    vids, auds, seams, seam_tokens = [], [], [], []
    frames_total = 0
    tokens_total = 0
    for i, (v, a, prefix) in enumerate(links):
        frames = _check_h3_video(v, f"link {i + 1}")
        prefix = int(prefix or 0)
        if i == 0 and prefix:
            raise ValueError("link 1 has no carried prefix")
        if i > 0 and prefix <= 0:
            raise ValueError(f"link {i + 1} has no carried prefix; it cannot join seamlessly")
        tp = h3_tokens_for_clip(prefix) if prefix else 0
        ap = h3_audio_ticks(prefix) if prefix else 0
        if i > 0:
            seams.append(frames_total)
            seam_tokens.append(tokens_total)
        vids.append(v[:, :, tp:])
        auds.append(a[..., ap:])
        frames_total += frames - prefix
        tokens_total += v.shape[2] - tp
    video = torch.cat(vids, dim=2)
    audio = torch.cat(auds, dim=-1)
    if h3_frames_for_tokens(video.shape[2]) != frames_total:
        raise ValueError("joined take is off the token grid (a link was not group-aligned)")
    return {"video": video, "audio": audio, "frames": frames_total,
            "seam_frames": seams, "seam_tokens": seam_tokens}


def h3_plan_chunks(total_tokens: int, chunk_tokens: int, overlap_tokens: int,
                   protected_tokens: list[int] | None = None,
                   margin_tokens: int = 5) -> list[tuple[int, int]]:
    """Temporal refine chunks [k0, k1) for a whole take, with in-place overlap.

    Chunk starts are group-aligned (multiples of 5); chunk and overlap lengths are
    5m+2 so every overlap is a valid clip prefix. Chunk i+1 re-uses (pins) tokens
    [k0_{i+1}, k1_i) from the refined chunk i, so the junction where fresh refine
    output begins is k1_i. A junction is never placed within `margin_tokens` of a
    protected token (a chain seam): each seam is refined inside one chunk."""
    T, tc, to = int(total_tokens), int(chunk_tokens), int(overlap_tokens)
    for name, n in (("chunk", tc), ("overlap", to)):
        if n < 2 or (n - 2) % 5:
            raise ValueError(f"{name} tokens must be 5m+2, got {n}")
    if (T - 2) % 5:
        raise ValueError(f"take of {T} tokens is not a 17k+5 clip")
    if to >= tc:
        raise ValueError("overlap must be shorter than a chunk")
    protected = sorted(int(s) for s in (protected_tokens or []))
    min_len = to + 10                      # every chunk adds >= 10 fresh tokens
    bounds: list[tuple[int, int]] = []
    k0 = 0
    while T - k0 > tc:
        k1 = k0 + tc
        while (k1 - k0 > min_len
               and any(abs(k1 - s) < margin_tokens for s in protected)):
            k1 -= 5
        bounds.append((k0, k1))
        k0 = k1 - to
    bounds.append((k0, T))
    return bounds


def h3_junction_conflicts(bounds: list[tuple[int, int]], protected_tokens: list[int],
                          margin_tokens: int = 5) -> list[int]:
    """Protected tokens that a chunk junction still lands next to (for logging)."""
    junctions = [k1 for _, k1 in bounds[:-1]]
    return sorted({int(s) for s in protected_tokens or []
                   for j in junctions if abs(j - int(s)) < margin_tokens})


# ------------------------------------------------------------------ LTX audio
def ltx_audio_carry(dst_audio: torch.Tensor, src_audio: torch.Tensor,
                    src_frames: int, carry_frames: int,
                    fps: float = 24.0, latents_per_second: float = 25.0) -> dict:
    """Copy the previous LTX clip's audio over the carried video frames into the head
    of the new clip's audio latent ([B,8,T,16]) and build the noise mask."""
    if dst_audio.ndim != 4 or src_audio.ndim != 4 or dst_audio.shape[1] != src_audio.shape[1]:
        raise ValueError(f"expected LTX audio latents [B,C,T,F], got {list(src_audio.shape)} "
                         f"and {list(dst_audio.shape)}")
    start_exact = (int(src_frames) - int(carry_frames)) / float(fps) * float(latents_per_second)
    k = int(round(int(carry_frames) / float(fps) * float(latents_per_second)))
    start = int(round(start_exact))
    start = max(0, min(start, src_audio.shape[2] - k))
    if k <= 0 or k >= dst_audio.shape[2]:
        raise ValueError(f"audio carry of {k} latents does not fit a {dst_audio.shape[2]}-latent clip")
    audio = dst_audio.clone()
    audio[:, :, :k] = src_audio[:, :, start:start + k].to(audio.dtype)
    mask = torch.ones((audio.shape[0], 1, audio.shape[2], audio.shape[3]), dtype=torch.float32)
    mask[:, :, :k] = 0.0
    return {"audio": audio, "mask": mask, "latents": k,
            "offset_ms": (start - start_exact) / float(latents_per_second) * 1000.0}


def ltx_audio_join(latents: list[torch.Tensor], frames: list[int], carry_frames: int,
                   fps: float = 24.0, latents_per_second: float = 25.0) -> tuple[torch.Tensor, dict]:
    """Join the links' audio latents ([B,8,T,16]) into one take stream.

    Link i opens with k latents copied from link i-1's [start, start+k)
    (ltx_audio_carry), so the take keeps link i-1 up to start+k and continues with
    link i after its k carried latents: one continuous latent stream that decodes
    in one piece. (Decoding the links separately and cross-fading the carried span
    does not work: the audio decoder renders the same latents with a different
    phase in a different context, and the two decodes are uncorrelated.)"""
    if len(latents) != len(frames) or not latents:
        raise ValueError("one frame count per audio latent")
    k = int(round(int(carry_frames) / float(fps) * float(latents_per_second)))
    out = latents[0]
    link0 = 0                      # take latent index of the current link's latent 0
    take_frames = int(frames[0])
    joins = []
    for i in range(1, len(latents)):
        src, dst = latents[i - 1], latents[i]
        if dst.ndim != 4 or dst.shape[1:2] != src.shape[1:2] or dst.shape[3:] != src.shape[3:]:
            raise ValueError(f"link {i + 1}: audio latent {list(dst.shape)} does not match {list(src.shape)}")
        start_exact = (int(frames[i - 1]) - int(carry_frames)) / float(fps) * float(latents_per_second)
        start = max(0, min(int(round(start_exact)), src.shape[2] - k))
        if k <= 0 or k >= dst.shape[2]:
            raise ValueError(f"link {i + 1}: a {k}-latent carry does not fit {dst.shape[2]} latents")
        carried = dst[:, :, :k].float()
        copy_err = float((carried - src[:, :, start:start + k].float().to(carried.device)).abs().max())
        cut = link0 + start + k
        out = torch.cat([out[:, :, :cut], dst[:, :, k:].to(out.device, out.dtype)], dim=2)
        link0 += start
        link_frame0 = take_frames - int(carry_frames)          # take frame of link i's frame 0
        drift_ms = (link0 - link_frame0 / float(fps) * float(latents_per_second)) / float(latents_per_second) * 1e3
        joins.append({"link": i + 1, "cut_latent": cut, "carry_latents": k, "carry_exact": copy_err == 0.0,
                      "carry_max_err": round(copy_err, 6), "drift_ms": round(drift_ms, 3)})
        take_frames += int(frames[i]) - int(carry_frames)
    info = {"latents": int(out.shape[2]), "take_frames": take_frames,
            "expected_latents": int(round(take_frames / float(fps) * float(latents_per_second))),
            "joins": joins}
    return out, info


def is_text_encoder_name(name: str) -> bool:
    """Class name of a loaded ComfyUI model that is a text encoder (MiniMaxH3TEModel_,
    the LTX Gemma TE model, CLIP models)."""
    return "TE" in name or "TextEncoder" in name or "CLIP" in name


def eviction_keep(names: list[str], keep_text_encoder: bool, text_encoder_only: bool) -> list[bool] | None:
    """Which loaded models an LTX25EvictModels call keeps, by class name; None means
    unload everything. keep_text_encoder: drop all but the text encoder (before a
    decode). text_encoder_only: drop only the text encoder (after the last encode),
    which takes precedence."""
    if not (keep_text_encoder or text_encoder_only):
        return None
    return [is_text_encoder_name(n) != bool(text_encoder_only) for n in names]


SEAM_MODES = ("window", "path")


def seam_adain(x: torch.Tensor, carry: int, ramp: int, factor: float = 1.0, ref_latents: int = 2,
               max_ratio: float = 1.5, mode: str = "window", period: int = 1) -> tuple[torch.Tensor, dict]:
    """Ease a video latent's look across the end of its carried prefix.

    mode "window": the newest carried latents [carry-ref_latents, carry) give
    per-channel mean/std; the first `ramp` generated latents, as one block, are
    matched to them with a weight that falls linearly from `factor` at the first
    new latent to 0, so exposure, white balance and contrast continue from the
    carry and drift to the new content's own look over the ramp.

    mode "path": every ramp latent is matched on its own to a point on the straight
    line (per-channel mean and log-std) from the last carried latent to the first
    latent after the ramp. One block transform cannot remove an excursion of a
    single latent, and the first generated latent is one: on the gate chain it
    left the look of the carry and came straight back on the next latent (a
    whole-frame +15 saturation flash for 8 frames). On the path the look leaves
    the carry continuously, any excursion inside the ramp is removed, the content's
    real look change is spread over the ramp instead of resisted, and the ramp
    ends on the next latent's own look, so its end is no step either. Needs a
    latent after the ramp (a shorter ramp is used near the end of the link).

    period (path only): latents whose statistics repeat with this period are
    matched phase by phase. An H3 latent is groups of 5 tokens of 1, 4, 4, 4, 4
    frames, and its single-frame tokens differ in mean and std from the others
    by more than a seam step; a line through neighbouring tokens would flatten
    that structure. With period P each ramp latent follows the line between the
    nearest latents of its own phase: the last one inside the carry and the
    first one after the ramp (P latents after the ramp are needed). P = 1 is
    the plain path.

    The carried latents are left untouched. x: [B, C, T, H, W]."""
    if x.ndim != 5:
        raise ValueError(f"expected a 5-D video latent, got {list(x.shape)}")
    if mode not in SEAM_MODES:
        raise ValueError(f"seam mode {mode!r} is not one of {SEAM_MODES}")
    period = int(period)
    if period < 1 or (period > 1 and mode != "path"):
        raise ValueError(f"period {period} needs mode 'path' and must be >= 1")
    t = x.shape[2]
    carry, ramp = int(carry), int(ramp)
    factor = float(max(0.0, min(1.0, factor)))
    if factor <= 0.0 or ramp <= 0 or carry <= 0 or carry >= t:
        return x, {"applied": False}
    if mode == "path":
        if carry < period:
            raise ValueError(f"a carry of {carry} latents holds no full period of {period}")
        return _seam_path(x, carry, min(ramp, t - period - carry), factor, max_ratio, period)
    ref_latents = max(1, min(int(ref_latents), carry))
    end = min(t, carry + ramp)
    xf = x.float()
    ref = xf[:, :, carry - ref_latents:carry]
    new = xf[:, :, carry:end]
    dims = (0, 2, 3, 4)
    mu_r, sd_r = ref.mean(dim=dims, keepdim=True), ref.std(dim=dims, keepdim=True)
    mu_n, sd_n = new.mean(dim=dims, keepdim=True), new.std(dim=dims, keepdim=True)
    ratio = (sd_r / sd_n.clamp_min(1e-6)).clamp(1.0 / max_ratio, max_ratio)
    matched = (new - mu_n) * ratio + mu_r
    k = torch.arange(end - carry, dtype=torch.float32, device=x.device)
    w = (factor * (1.0 - k / float(ramp))).view(1, 1, -1, 1, 1)
    out = xf.clone()
    out[:, :, carry:end] = new + w * (matched - new)
    info = {"applied": True, "carry": carry, "ramp": ramp, "factor": factor,
            "mean_shift": round(float((mu_r - mu_n).abs().mean()), 5),
            "std_ratio_mean": round(float(ratio.mean()), 5)}
    return out.to(x.dtype), info


def _seam_path(x: torch.Tensor, carry: int, n: int, factor: float,
               max_ratio: float, period: int = 1) -> tuple[torch.Tensor, dict]:
    """seam_adain mode "path" over the n latents [carry, carry+n)."""
    if n <= 0:
        return x, {"applied": False, "mode": "path", "reason": "no latent after the carry end"}
    xf = x.float()
    dims = (0, 3, 4)                                     # per channel and latent frame

    def stats(i: int) -> tuple[torch.Tensor, torch.Tensor]:
        s = xf[:, :, i:i + 1]
        return s.mean(dim=dims, keepdim=True), s.std(dim=dims, keepdim=True).clamp_min(1e-6)

    out = xf.clone()
    dev = []
    for k in range(n):
        i = carry + k
        # the nearest latents of i's phase: the last carried one (the look the
        # viewer last saw) and the first after the ramp (the look the content settles to)
        ia = carry - 1 - (carry - 1 - i) % period
        ib = carry + n + (i - carry - n) % period
        mu_a, sd_a = stats(ia)
        mu_b, sd_b = stats(ib)
        a = (i - ia) / (ib - ia)
        mu_t = (1 - a) * mu_a + a * mu_b
        sd_t = torch.exp((1 - a) * sd_a.log() + a * sd_b.log())
        mu, sd = stats(i)
        dev.append(round(float((mu - mu_t).norm()), 4))
        ratio = (sd_t / sd).clamp(1.0 / max_ratio, max_ratio)
        cur = xf[:, :, i:i + 1]
        out[:, :, i:i + 1] = cur + factor * ((cur - mu) * ratio + mu_t - cur)
    info = {"applied": True, "mode": "path", "carry": carry, "ramp": n, "factor": factor,
            "path_deviation": dev}
    if period > 1:
        info["period"] = period
    return out.to(x.dtype), info


def ltx_latent_carry(samples: torch.Tensor, first: torch.Tensor, source: torch.Tensor,
                     carry: int) -> torch.Tensor:
    """Head of a new LTX link built from the previous link's latents.

    The causal video VAE makes latent 0 a single frame and every later latent 8
    frames, so a carry of 1+8k frames is latent 0 = the first carried frame on its
    own (`first`, encoded from that one frame) followed by the previous link's
    last k sampled latents, copied exactly. Re-encoding the decoded tail instead
    (LTXV Img To Video Inplace) costs a decode/encode round trip per link that
    the encoder, seeing only the tail, cannot reproduce (measured once: the
    re-encoded latents differ from the originals by rmse 0.24-0.42 at std 0.92,
    decoding +1.7 saturation and sharper), and it repeats at every link (whether
    the shift accumulates was not isolated).
    samples, source: [B, C, T, H, W]; first: [B, C, 1, H, W]."""
    carry = int(carry)
    if samples.ndim != 5 or source.ndim != 5 or first.ndim != 5:
        raise ValueError("expected 5-D video latents")
    if carry < 1 or carry > samples.shape[2] or carry - 1 > source.shape[2] - 1:
        raise ValueError(f"carry of {carry} latents does not fit a {samples.shape[2]}-latent link "
                         f"continuing a {source.shape[2]}-latent one")
    want = (samples.shape[0], samples.shape[1], 1, samples.shape[3], samples.shape[4])
    if tuple(first.shape) != want:
        raise ValueError(f"first-frame latent {list(first.shape)} does not match {list(want)}")
    if source.shape[:2] != samples.shape[:2] or source.shape[3:] != samples.shape[3:]:
        raise ValueError(f"previous link's latent {list(source.shape)} does not match {list(samples.shape)}")
    out = samples.clone()
    out[:, :, :1] = first.to(out)
    if carry > 1:
        out[:, :, 1:carry] = source[:, :, source.shape[2] - (carry - 1):].to(out)
    return out


def ltx_take_latents(latents: list[torch.Tensor], carry: int, join: int = 1,
                     blend: int = 0) -> tuple[torch.Tensor, dict]:
    """Join LTX links into one take latent, so the take is decoded as one piece.

    Link i+1's latents 0..carry-1 cover link i's last 1+8(carry-1) frames; latent 0
    is only the single-frame anchor of the carry. The take keeps link i up to the
    latent before its replaced span and continues with link i+1 from latent
    `join`, decoded with real context on both sides (no pixel dissolve). With
    `blend` > 0 the `blend` carried latents before `join` are a linear crossfade
    of the two links (weights 1/(blend+1) .. blend/(blend+1) toward the new one):
    the new link re-rendered its soft-pinned carry, so a hard switch can still
    step in fine detail; the crossfade spreads it over 8*blend frames, and since
    the two versions of each latent differ only slightly it does not ghost.
    Each video latent: [B, C, T, H, W]."""
    carry, join, blend = int(carry), int(join), int(blend)
    if not latents:
        raise ValueError("no links")
    if carry < 1 or not 1 <= join <= carry:
        raise ValueError(f"join latent {join} must lie in 1..{carry} (the carried latents after the anchor)")
    if not 0 <= blend <= join - 1:
        raise ValueError(f"a {blend}-latent blend before join latent {join} would reach the anchor")
    ref = latents[0]
    for i, x in enumerate(latents):
        if x.ndim != 5 or x.shape[:2] != ref.shape[:2] or x.shape[3:] != ref.shape[3:]:
            raise ValueError(f"link {i + 1}: latent {list(x.shape)} does not match link 1 {list(ref.shape)}")
        if i and x.shape[2] <= carry:
            raise ValueError(f"link {i + 1}: {x.shape[2]} latents leave nothing after a {carry}-latent carry")
    pieces, spans, joins = [], [], []
    pos = 0
    for i, x in enumerate(latents):
        lo = join if i else 0
        # the last `blend` latents before the next link's join are crossfaded below
        hi = x.shape[2] - carry + join - blend if i + 1 < len(latents) else x.shape[2]
        if i:
            prev = latents[i - 1]
            p0 = prev.shape[2] - carry                       # prev latent the anchor sits on
            # how far the new link bent the carried latents away from the old ones
            bend = [round(float((x[:, :, j].float() - prev[:, :, p0 + j].float())
                                .pow(2).mean().sqrt()), 4) for j in range(1, carry)]
            if blend:
                w = torch.arange(1, blend + 1, dtype=torch.float32) / float(blend + 1)
                j0 = join - blend
                old = prev[:, :, p0 + j0:p0 + join].float()
                new = x[:, :, j0:join].float()
                pieces.append((old + (new - old) * w.view(1, 1, -1, 1, 1).to(new.device)).to(x.dtype))
                pos += blend
            joins.append({"link": i + 1, "take_latent": pos, "take_frame": 8 * pos - 7,
                          "blend_from_frame": 8 * (pos - blend) - 7, "bend_rmse": bend})
        pieces.append(x[:, :, lo:hi])
        spans.append({"link": i + 1, "latents": [lo, hi], "take_latents": [pos, pos + hi - lo]})
        pos += hi - lo
    take = torch.cat(pieces, dim=2)
    steps = (take[:, :, 1:].float() - take[:, :, :-1].float()).pow(2).mean(dim=(0, 1, 3, 4)).sqrt()
    for j in joins:
        s = j["take_latent"]
        near = [float(steps[t - 1]) for t in range(max(2, s - 6), min(take.shape[2], s + 7)) if t != s]
        med = float(statistics.median(near)) if near else 0.0
        j["step"] = round(float(steps[s - 1]), 4)
        j["step_ratio"] = round(float(steps[s - 1]) / med, 3) if med > 0 else None
    return take, {"latents": int(take.shape[2]), "frames": 8 * (int(take.shape[2]) - 1) + 1,
                  "carry": carry, "join": join, "blend": blend, "spans": spans, "joins": joins}


def decode_windows(total: int, window: int = 24, context: int = 2, overlap: int = 2,
                   temporal_scale: int = 8) -> list[dict]:
    """Outer temporal windows for decoding a long LTX latent piece by piece.

    The video decoder is not causal, so a frame needs latents on both sides. Each
    window [a, b) decodes to frames [8a, 8(b-1)+1) (ComfyUI's tiled-decode
    convention); the `context` latents at an inner window edge are decoded only as
    context and dropped, and consecutive windows' used frames overlap by
    8(overlap-1)+1 frames, which are cross-faded. A latent that fits one window
    decodes exactly like VAE Decode Tiled. use: the take frames a window writes;
    blend: how many of those it fades in over the previous window's."""
    total, window, context, overlap = int(total), int(window), int(context), int(overlap)
    if total < 1:
        raise ValueError("nothing to decode")
    if context < 1 or overlap < 1:
        raise ValueError("context and overlap must be at least one latent")
    s = int(temporal_scale)
    end = s * (total - 1) + 1
    window = max(window, 2 * context + overlap + 1)
    if total <= window:
        return [{"a": 0, "b": total, "use": [0, end], "blend": 0}]
    step = window - 2 * context - overlap
    plan: list[dict] = []
    a = 0
    while True:
        b = min(total, a + window)
        if b == total and plan:
            # a short last window still gets its full context, but never reaches back
            # past the frames the window before the previous one wrote
            floor = -(-plan[-2]["use"][1] // s) - context if len(plan) > 1 else 1
            a = max(min(a, total - window), floor)
        u0 = 0 if a == 0 else s * (a + context)
        u1 = end if b == total else s * (b - 1 - context) + 1
        blend = plan[-1]["use"][1] - u0 if plan else 0
        if blend < 1 and plan:
            raise AssertionError("decode windows do not overlap")
        plan.append({"a": a, "b": b, "use": [u0, u1], "blend": blend})
        if b == total:
            return plan
        a += step


def carry_restore(x: torch.Tensor, ref: torch.Tensor, carry: int, factor: float = 1.0,
                  eps: float = 1e-8) -> tuple[torch.Tensor, dict]:
    """Undo the gain a soft pin puts on the carried latents.

    A carried latent conditioned below strength 1 comes out of the sampler as
    gain * encoded + shift + residual per channel: the model re-renders an
    already-clean frame and amplifies it (measured on the LTX 2.5 draft: gain
    1.06-1.09 on every channel, spread <0.02, which decodes as +10-13% saturation
    over the carry and falls back on the first free latent). The residual is the
    model bending the carry toward the new content, which is what the soft pin is
    for. Per channel and latent frame this regresses the output on the encoded
    carry and keeps encoded + residual, so the look is the previous link's again
    and the motion adaptation stays. Hard-pinned latents (output == encoded) are
    returned unchanged. x, ref: [B, C, T, H, W]; ref's first `carry` latents are
    the encoded carry (the I2V-pinned latent before sampling)."""
    if x.ndim != 5 or ref.ndim != 5:
        raise ValueError(f"expected 5-D video latents, got {list(x.shape)} and {list(ref.shape)}")
    carry = int(carry)
    factor = float(max(0.0, min(1.0, factor)))
    if x.shape[:2] != ref.shape[:2] or x.shape[3:] != ref.shape[3:]:
        raise ValueError(f"latent {list(x.shape)} and reference {list(ref.shape)} differ outside time")
    if factor <= 0.0 or carry <= 0:
        return x, {"applied": False}
    if carry > min(x.shape[2], ref.shape[2]):
        raise ValueError(f"carry {carry} exceeds the latent ({x.shape[2]}) or reference ({ref.shape[2]}) length")
    xf, rf = x.float(), ref.to(device=x.device).float()
    out = xf.clone()
    rows = []
    for t in range(carry):
        s, e = xf[:, :, t], rf[:, :, t]
        if torch.equal(s, e):
            rows.append({"t": t, "pinned": True})
            continue
        sm, em = s.mean(dim=(-2, -1), keepdim=True), e.mean(dim=(-2, -1), keepdim=True)
        sc, ec = s - sm, e - em
        gain = (sc * ec).sum(dim=(-2, -1), keepdim=True) / (ec * ec).sum(dim=(-2, -1), keepdim=True).clamp_min(eps)
        # s = gain*ec + sm + resid  ->  restored = e + resid
        correction = (gain - 1.0) * ec + (sm - em)
        out[:, :, t] = s - factor * correction
        rows.append({"t": t, "gain_mean": round(float(gain.mean()), 4), "gain_sd": round(float(gain.std()), 4),
                     "shift_rms": round(float((sm - em).pow(2).mean().sqrt()), 4),
                     "rmse_before": round(float((s - e).pow(2).mean().sqrt()), 4),
                     "rmse_after": round(float((out[:, :, t] - e).pow(2).mean().sqrt()), 4)})
    return out.to(x.dtype), {"applied": True, "carry": carry, "factor": factor, "latents": rows}


# ------------------------------------------------------------------ drift guard
def adain_to_reference(x: torch.Tensor, ref: torch.Tensor, factor: float,
                       skip_frames: int = 0, ramp_frames: int = 0,
                       max_ratio: float = 1.5) -> tuple[torch.Tensor, dict]:
    """Pull a video latent's per-channel mean/std toward a reference latent's.

    Global per-channel statistics are the lowest-frequency description of a clip's
    look (exposure, white balance, contrast), so matching them corrects colour and
    contrast drift without touching texture. The first `skip_frames` latent frames
    (a carried, already-correct prefix) are excluded from the statistics and left
    untouched; the correction then ramps in over `ramp_frames` so it can never
    create a step of its own. The std ratio is clamped to [1/max_ratio, max_ratio]."""
    if x.ndim != 5 or ref.ndim != 5 or x.shape[1] != ref.shape[1]:
        raise ValueError(f"expected matching 5-D video latents, got {list(x.shape)} / {list(ref.shape)}")
    factor = float(max(0.0, min(1.0, factor)))
    t = x.shape[2]
    skip = int(max(0, min(skip_frames, t)))
    if factor <= 0.0 or skip >= t:
        return x, {"applied": False}
    xs = x[:, :, skip:].float()
    rf = ref.float()
    dims = (0, 2, 3, 4)
    mu_x, sd_x = xs.mean(dim=dims), xs.std(dim=dims)
    mu_r, sd_r = rf.mean(dim=dims), rf.std(dim=dims)
    ratio = (sd_r / sd_x.clamp(min=1e-6)).clamp(1.0 / max_ratio, max_ratio)
    shape = (1, -1, 1, 1, 1)
    target = (x.float() - mu_x.view(shape)) * ratio.view(shape) + mu_r.view(shape)
    w = torch.zeros(t, dtype=torch.float32)
    ramp = int(max(0, ramp_frames))
    for i in range(skip, t):
        w[i] = factor * (min(1.0, (i - skip + 1) / ramp) if ramp > 0 else 1.0)
    out = x.float() + (target - x.float()) * w.view(1, 1, t, 1, 1)
    info = {"applied": True, "factor": factor, "skip": skip, "ramp": ramp,
            "mean_shift": float((mu_r - mu_x).abs().mean()),
            "std_ratio_mean": float(ratio.mean()),
            "std_ratio_min": float(ratio.min()), "std_ratio_max": float(ratio.max())}
    return out.to(x.dtype), info


def ltx_frames_ok(frames: int) -> bool:
    return frames >= 1 and (frames - 1) % 8 == 0


def ltx_latent_frames(frames: int) -> int:
    if not ltx_frames_ok(frames):
        raise ValueError(f"{frames} frames is not on the LTX 1+8k grid")
    return (frames - 1) // 8 + 1


def ceil_to_grid(value: int, base: int, step: int) -> int:
    """Smallest base + step*k >= value."""
    k = max(0, math.ceil((int(value) - base) / step))
    return base + step * k
