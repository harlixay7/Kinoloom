"""Join chain links into one take and encode it once.

LTX links overlap: link i+1 opens with `carry` frames that are re-renders of link
i's last `carry` frames (pinned, same content). The join blends inside that overlap
— the last `crossfade` frames of link i into link i+1's matching carried frames —
so the switch to link i+1's own stream happens on identical content; the audio is
cross-faded over the same window with equal GAIN (the carried audio is the same
signal, an equal-power fade would bump it +3 dB mid-fade).

H3 takes are refined and decoded as one piece, so they only need the encode.

The single delivery encode converts RGB -> YUV with the BT.709 matrix and tags the
stream accordingly (ffmpeg's RGB->YUV default is BT.601, which shifts colours).
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class LinkMedia:
    video: Path                 # lossless FFV1 mkv
    audio: Path | None          # *.audio.safetensors (float waveform) or None
    frames: int
    carry: int                  # leading frames that repeat the previous link (0 for link 1)


def read_link_audio(path: Path | None) -> tuple[np.ndarray, int] | None:
    if path is None or not Path(path).is_file():
        return None
    from safetensors import safe_open
    with safe_open(str(path), framework="np") as f:
        wf = f.get_tensor("waveform").astype(np.float32)        # [C, L]
        sr = int((f.metadata() or {}).get("sample_rate") or 0)
    if sr <= 0:
        raise ValueError(f"{path}: no sample_rate in metadata")
    return wf, sr


def _iter_rgb(path: Path):
    import av
    with av.open(str(path)) as ct:
        for fr in ct.decode(video=0):
            yield fr.to_ndarray(format="rgb24")


def _blend_offset(carry: int, crossfade: int, blend_start: int | None) -> int:
    """Carried frame at which the blend window opens: by default the window ends on
    the seam (offset carry - crossfade); 0 switches to the incoming link at the
    start of the carry, where a soft pin still holds it to the previous link."""
    off = carry - crossfade if blend_start is None else int(blend_start)
    if not 0 <= off <= carry - crossfade:
        raise ValueError(f"blend window [{off}, {off + crossfade}) does not fit a {carry}-frame carry")
    return off


def plan_timeline(links: list[LinkMedia], crossfade: int, blend_start: int | None = None) -> dict:
    """Frame bookkeeping of the joined take (pure; unit-tested). seams: the first
    frame of each link's own content; blends: the timeline window each join's
    dissolve occupies."""
    if not links:
        raise ValueError("no links")
    if links[0].carry:
        raise ValueError("link 1 has no carry")
    starts, total = [], 0
    seams, blends = [], []
    for i, lk in enumerate(links):
        if i > 0:
            if lk.carry <= 0:
                raise ValueError(f"link {i + 1} has no carried frames")
            if crossfade > lk.carry:
                raise ValueError("crossfade longer than the carried overlap")
        start = total - lk.carry if i > 0 else 0      # timeline frame of this link's frame 0
        starts.append(start)
        if i > 0:
            seams.append(total)
            off = _blend_offset(lk.carry, crossfade, blend_start)
            blends.append([start + off, start + off + crossfade])
        total = start + lk.frames
    return {"starts": starts, "total": total, "seams": seams, "blends": blends, "crossfade": crossfade}


def blend_weights(n: int) -> np.ndarray:
    """Weights of the incoming link over an n-frame overlap: 1/(n+1) .. n/(n+1)."""
    return np.arange(1, n + 1, dtype=np.float32) / float(n + 1)


def assemble_frames(links: list[LinkMedia], crossfade: int, blend_start: int | None = None):
    """Yield the joined take's RGB frames (uint8), streaming one link at a time.
    Over each carried overlap: the outgoing link up to the blend window, a linear
    dissolve across it, then the incoming link's own frames."""
    prev_tail: list[np.ndarray] = []
    for i, lk in enumerate(links):
        nxt = links[i + 1] if i + 1 < len(links) else None
        # frames of this link's end that the next join replaces: from its blend window on
        keep_tail = (nxt.carry - _blend_offset(nxt.carry, crossfade, blend_start)) if nxt else 0
        frames = _iter_rgb(lk.video)
        idx = 0
        if i > 0:
            off = _blend_offset(lk.carry, crossfade, blend_start)
            # skip the carried frames before the blend window, blend across it
            for _ in range(off):
                next(frames)
                idx += 1
            w = blend_weights(crossfade)
            for j in range(crossfade):
                cur = next(frames).astype(np.float32)
                idx += 1
                out = prev_tail[j].astype(np.float32) * (1.0 - w[j]) + cur * w[j]
                yield np.clip(np.rint(out), 0, 255).astype(np.uint8)
        tail: list[np.ndarray] = []
        for f in frames:
            if keep_tail and idx >= lk.frames - keep_tail:
                tail.append(f)
            else:
                yield f
            idx += 1
        if idx != lk.frames:
            raise ValueError(f"{lk.video.name}: {idx} frames, expected {lk.frames}")
        prev_tail = tail


AUDIO_GUARD_FRAMES = 2


def link_gains(links: list[LinkMedia], crossfade: int, fps: float, sr: int,
               guard: int = AUDIO_GUARD_FRAMES) -> list[np.ndarray]:
    """Per-link audio gain curves (pure; unit-tested), in samples from each link's
    placement at round(start/fps*sr).

    Inside a carried overlap both links play the same (pinned) sound, so the fade can
    sit anywhere in it; it ends `guard` frames before the carry does because the audio
    decoder returns ~30 ms less than the video length, and the outgoing link must be
    fully faded out before its audio runs out. Both curves are cut from one timeline
    window, so they sum to exactly 1 sample for sample."""
    tl = plan_timeline(links, crossfade)
    starts = [int(round(s / fps * sr)) for s in tl["starts"]]
    gains = [np.ones(int(round(lk.frames / fps * sr)), dtype=np.float32) for lk in links]
    for i in range(1, len(links)):
        lk = links[i]
        g = max(0, min(int(guard), lk.carry - crossfade))
        t1 = int(round((tl["starts"][i] + lk.carry - g) / fps * sr))
        t0 = int(round((tl["starts"][i] + lk.carry - g - crossfade) / fps * sr))
        ramp = (np.arange(t1 - t0, dtype=np.float32) + 0.5) / float(max(1, t1 - t0))
        inc, out = gains[i], gains[i - 1]
        a0, a1 = t0 - starts[i], t1 - starts[i]
        inc[:a0] = 0.0
        inc[a0:a1] *= ramp
        b0, b1 = t0 - starts[i - 1], t1 - starts[i - 1]
        out[b0:b1] *= 1.0 - ramp
        out[b1:] = 0.0
    return gains


def assemble_audio(links: list[LinkMedia], crossfade: int, fps: float) -> tuple[np.ndarray, int] | None:
    tl = plan_timeline(links, crossfade)
    tracks = [read_link_audio(lk.audio) for lk in links]
    if any(t is None for t in tracks):
        return None
    loaded = [t for t in tracks if t is not None]
    sr = loaded[0][1]
    if any(t[1] != sr for t in loaded):
        raise ValueError("links have different audio sample rates")
    ch = max(t[0].shape[0] for t in loaded)
    total = int(round(tl["total"] / fps * sr))
    out = np.zeros((ch, total), dtype=np.float32)
    gains = link_gains(links, crossfade, fps, sr)
    for i, ((wf, _), g) in enumerate(zip(loaded, gains)):
        if wf.shape[0] < ch:
            wf = np.repeat(wf, ch, axis=0)[:ch]
        n = g.shape[0]
        wf = wf[:, :n] if wf.shape[1] >= n else np.pad(wf, ((0, 0), (0, n - wf.shape[1])))
        s0 = int(round(tl["starts"][i] / fps * sr))
        e = min(total, s0 + n)
        out[:, s0:e] += (wf * g[None, :])[:, :e - s0]
    return out, sr


def write_wav(path: Path, wf: np.ndarray, sr: int) -> None:
    pcm = (np.clip(wf, -1.0, 1.0) * 32767.0).round().astype(np.int16).T.copy()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(int(wf.shape[0]))
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def encode(frames_iter, width: int, height: int, fps: float, out_mp4: Path, ffmpeg: str,
           wav: Path | None = None, crf: int = 16, preset: str = "slow",
           master_mkv: Path | None = None, low_priority: bool = False) -> dict:
    """One delivery encode (x264, BT.709) from RGB frames; optional FFV1 master.
    low_priority: run ffmpeg below normal CPU priority (an encode beside a render)."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    vf = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", f"{float(fps):g}", "-i", "-"]
    if wav is not None:
        cmd += ["-i", str(wav)]
    cmd += ["-map", "0:v:0"] + (["-map", "1:a:0"] if wav is not None else []) + [
        "-vf", vf, "-c:v", "libx264", "-crf", str(int(crf)), "-preset", preset,
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-movflags", "+faststart"]
    if wav is not None:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
    cmd += [str(out_mp4)]
    import av
    from fractions import Fraction
    flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS if low_priority and os.name == "nt" else 0
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    stdin, stderr = proc.stdin, proc.stderr
    assert stdin is not None and stderr is not None
    master = ms = None
    if master_mkv is not None:
        master = av.open(str(master_mkv), "w")
        ms = master.add_stream("ffv1", rate=Fraction(round(fps * 1000), 1000))
        ms.width, ms.height, ms.pix_fmt = width, height, "bgr0"
    n = 0
    try:
        for f in frames_iter:
            if f.shape[0] != height or f.shape[1] != width:
                raise ValueError(f"frame {n} is {f.shape[1]}x{f.shape[0]}, expected {width}x{height}")
            stdin.write(np.ascontiguousarray(f).tobytes())
            if master is not None and ms is not None:
                for pkt in ms.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
                    master.mux(pkt)
            n += 1
    finally:
        stdin.close()
        err = stderr.read().decode(errors="replace")
        rc = proc.wait()
        if master is not None and ms is not None:
            for pkt in ms.encode():
                master.mux(pkt)
            master.close()
    if rc != 0:
        raise RuntimeError(f"ffmpeg encode failed ({rc}): {err[-800:]}")
    return {"frames": n, "mp4": str(out_mp4), "master": str(master_mkv) if master_mkv else None}


def link_preview(video: Path, carry: int, fps: float, out_mp4: Path, ffmpeg: str, crf: int = 20) -> dict:
    """A quick look at one rendered link: its own frames (after the `carry` frames
    it opens on, which repeat the previous link) with their sound, encoded fast and
    at a lower CPU priority, so it can run beside the next link's render. Written
    under a temporary name and moved into place, so a reader never sees half a file."""
    frames = _iter_rgb(video)
    for _ in range(int(carry)):
        next(frames)
    first = next(frames)
    h, w = first.shape[:2]
    part = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    wav = None
    got = read_link_audio(video.with_suffix(".audio.safetensors"))
    if got is not None:
        wf, sr = got
        wav = out_mp4.with_suffix(".wav")
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        write_wav(wav, wf[:, int(round(carry / fps * sr)):], sr)
    try:
        res = encode(itertools.chain([first], frames), w, h, fps, part, ffmpeg, wav=wav, crf=crf,
                     preset="veryfast", low_priority=True)
        os.replace(part, out_mp4)
    finally:
        part.unlink(missing_ok=True)
        if wav is not None:
            wav.unlink(missing_ok=True)
    return {**res, "mp4": str(out_mp4), "width": w, "height": h}


def fit_take_audio(path: Path, total_frames: int, fps: float) -> tuple[np.ndarray, int]:
    """A take's one-piece audio decode, padded/trimmed to the video length (the
    decoder returns ~30 ms short; the pad is silence at the very end)."""
    got = read_link_audio(path)
    if got is None:
        raise FileNotFoundError(path)
    wf, sr = got
    n = int(round(total_frames / fps * sr))
    wf = wf[:, :n] if wf.shape[1] >= n else np.pad(wf, ((0, 0), (0, n - wf.shape[1])))
    return wf, sr


def assemble_ltx(links: list[LinkMedia], out_mp4: Path, ffmpeg: str, fps: float = 24.0,
                 crossfade: int = 8, crf: int = 16, master: bool = False,
                 take_audio: Path | None = None, blend_start: int | None = None) -> dict:
    """take_audio: the whole take's sound decoded once from the joined audio latents
    (preferred); without it the links' own decodes are cross-faded.
    blend_start: carried frame the video dissolve opens at (None = ending on the seam)."""
    tl = plan_timeline(links, crossfade, blend_start)
    side = json.loads(Path(links[0].video).with_suffix(".json").read_text(encoding="utf-8"))
    w, h = int(side["width"]), int(side["height"])
    wav = None
    au = fit_take_audio(take_audio, tl["total"], fps) if take_audio else assemble_audio(links, crossfade, fps)
    if au is not None:
        wav = out_mp4.with_suffix(".wav")
        write_wav(wav, au[0], au[1])
    res = encode(assemble_frames(links, crossfade, blend_start), w, h, fps, out_mp4, ffmpeg, wav=wav, crf=crf,
                 master_mkv=out_mp4.with_suffix(".master.mkv") if master else None)
    if res["frames"] != tl["total"]:
        raise RuntimeError(f"assembled {res['frames']} frames, planned {tl['total']}")
    res.update({"seams": tl["seams"], "blends": tl["blends"], "total": tl["total"], "crossfade": crossfade,
                "blend_start": blend_start,
                "audio": "take" if (au is not None and take_audio) else ("crossfaded" if au is not None else None)})
    return res


LOOK_PATH_RAMP = 16                 # frames after a carry end the pixel look path spans
LOOK_PATH_CHECKS = ("saturation", "lightness", "contrast")


def look_path_plan(video: Path, carry_ends: list[int], ramp: int = LOOK_PATH_RAMP,
                   pre: int = 1, post: int = 3) -> dict[int, dict]:
    """The take's look path in pixel space, for the carry ends whose look still
    steps (seam_qa saturation / lightness / contrast) after the latent look match.

    The latent match (StudioSeamAdain) adjusts each 8-frame latent as a whole, so a
    change inside the first free latent survives it: on gate v4 link 3 the previous
    link ended on a growing sun glare and the new content dropped it within 2 frames
    (L -4.4; both seeds). Here frames b..b+ramp-1 are matched one by one, in Lab, to
    the straight line from the last carried frame's look to the look at b+ramp:
    lightness in mean and spread (spread on a log line), the chroma channels in mean
    only (matching their spread overshot saturation on dark frames), then the
    chroma scaled so the frame's mean HSV saturation lands on its line too
    (brightening a frame lowers its HSV saturation). Passing seams are left alone.
    Returns {b: {"fails", "ramp", "L_from", "L_to", "frames": {i: (L gain, L target
    mean, L mean, a shift, b shift, saturation target)}}}, Lab in OpenCV 8-bit units."""
    import seam_qa
    rows, diffs = seam_qa.analyze_frames(video)
    plan: dict[int, dict] = {}
    for b in sorted(int(x) for x in carry_ends):
        if b < pre or b + ramp + post > len(rows):
            continue
        fails = [f for f in seam_qa.judge_seam(seam_qa.seam_metrics(rows, diffs, b))["fails"]
                 if f in LOOK_PATH_CHECKS]
        if not fails:
            continue

        def look(seg):
            return (float(np.mean([r["L"] for r in seg])),
                    float(np.exp(np.mean([np.log(r["contrast"] + 1e-3) for r in seg]))),
                    float(np.mean([r["a"] for r in seg])), float(np.mean([r["b"] for r in seg])),
                    float(np.mean([r["sat"] for r in seg])))
        la, lb = look(rows[b - pre:b]), look(rows[b + ramp:b + ramp + post])
        frames = {}
        for i in range(b, b + ramp):
            t = (i - (b - 1)) / (ramp + 1)
            sd_t = float(np.exp(np.log(la[1]) + t * (np.log(lb[1]) - np.log(la[1]))))
            r = rows[i]
            frames[i] = (sd_t / (r["contrast"] + 1e-3), (la[0] + t * (lb[0] - la[0])) * 2.55, r["L"] * 2.55,
                         la[2] + t * (lb[2] - la[2]) - r["a"], la[3] + t * (lb[3] - la[3]) - r["b"],
                         la[4] + t * (lb[4] - la[4]))
        plan[b] = {"fails": fails, "ramp": ramp, "L_from": round(la[0], 2), "L_to": round(lb[0], 2),
                   "first_shift_L": round((frames[b][1] - frames[b][2]) / 2.55, 2), "frames": frames}
    return plan


def apply_look_path(frames_iter, plan: dict[int, dict]):
    """Yield the frames with the planned per-frame look corrections applied."""
    import cv2
    corr = {i: c for p in plan.values() for i, c in p["frames"].items()}
    for i, rgb in enumerate(frames_iter):
        c = corr.get(i)
        if c is None:
            yield rgb
            continue
        gain, mu_t, mu_i, da, db, sat_t = c
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        lab[..., 0] = (lab[..., 0] - mu_i) * gain + mu_t
        lab[..., 1] += da
        lab[..., 2] += db
        out = cv2.cvtColor(np.clip(lab + 0.5, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
        sat = _mean_sat(out)
        if sat > 1.0:
            # chroma about neutral, by the ratio still missing (bounded: a look path
            # eases a step, it does not regrade the frame)
            k = float(np.clip(sat_t / sat, 0.8, 1.25))
            lab[..., 1:] = (lab[..., 1:] - 128.0) * k + 128.0
            out = cv2.cvtColor(np.clip(lab + 0.5, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
        yield out


def _mean_sat(rgb: np.ndarray) -> float:
    """Mean HSV saturation at the seam QA's analysis size (seam_qa._stats)."""
    import cv2
    import seam_qa
    small = cv2.resize(rgb, seam_qa.fit(rgb, seam_qa.ANALYSIS_W, seam_qa.ANALYSIS_H),
                       interpolation=cv2.INTER_AREA)
    return float(cv2.cvtColor(small, cv2.COLOR_RGB2HSV)[..., 1].mean())


def encode_take(video_mkv: Path, out_mp4: Path, ffmpeg: str, fps: float = 24.0, crf: int = 16,
                take_audio: Path | None = None, master: bool = False,
                look_path: list[int] | None = None) -> dict:
    """Encode an already-joined take (an H3 take, or an LTX take decoded once) once.
    take_audio: the take's one-piece audio decode (*.audio.safetensors), fitted to
    the video length; otherwise a WAV next to the mkv is used if there is one.
    look_path: carry ends to give a pixel look path where their look still steps
    (look_path_plan); the lossless take itself is left as it is."""
    side = json.loads(Path(video_mkv).with_suffix(".json").read_text(encoding="utf-8"))
    frames = int(side["frames"])
    wav = Path(video_mkv).with_suffix(".wav")
    if take_audio is not None:
        wf, sr = fit_take_audio(Path(take_audio), frames, fps)
        wav = Path(out_mp4).with_suffix(".wav")
        write_wav(wav, wf, sr)
    plan = look_path_plan(Path(video_mkv), look_path) if look_path else {}
    src = _iter_rgb(Path(video_mkv))
    res = encode(apply_look_path(src, plan) if plan else src, int(side["width"]), int(side["height"]), fps,
                 out_mp4, ffmpeg, wav=wav if wav.is_file() else None, crf=crf,
                 master_mkv=Path(out_mp4).with_suffix(".master.mkv") if master else None)
    res["look_path"] = {str(b): {k: v for k, v in p.items() if k != "frames"} for b, p in plan.items()}
    if res["frames"] != frames:
        raise RuntimeError(f"encoded {res['frames']} frames, the take has {frames}")
    res["audio"] = "take" if take_audio is not None else ("muxed" if wav.is_file() else None)
    return res
