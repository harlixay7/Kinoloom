"""Seam QA for chained video takes: measures every join and the drift across a take.

Gates (all must pass for a take to count as seamless):

    pop_ratio      frame difference at the seam / median local frame difference   <= 1.30
    d_sat          6-frame mean saturation step across the seam (HSV S, 0-255)    |..| <= 2.0
    d_L            6-frame mean lightness step (CIELAB L*, 0-100)                 |..| <= 1.5
    d_contrast     6-frame mean L* standard deviation step                         |..| <= 1.5
    detail_ratio   Laplacian variance of the 4 frames after / 4 frames before      0.85 .. 1.15
    audio_step_db  RMS level step across the seam (100 ms windows)                 <= 1.5 dB
                   (only judged when the local level is above -45 dBFS and both
                   sides are sound: a sound starting out of silence, or ending
                   into it, below -60 dBFS, is content and reported as an onset)
    audio_click    largest sample jump at the seam / 99.9th pct of the take        <= 3.0
    drift          relative change link 1 -> last link of L*, contrast,
                   saturation and hf texture (Laplacian variance: reported only)   <= 5 %
    overlap_psnr   (LTX) carried frames re-rendered vs the originals               >= 32 dB

The look steps (d_sat, d_L, d_contrast) fail only above both their gate and
LOCAL_FACTOR x the same step measured nearby (added 2026-10-23). The thresholds were
set on 2026-10-22 from an analysis of the earlier chains, which measured pop
1.75-4.27x and saturation steps of +4.2..+22.9.
"""
from __future__ import annotations

import json
import math
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Look steps are judged against the same step measured at these frame offsets from
# the seam (outside its +-6 window): a seam fails only on a step LOCAL_FACTOR x the
# local median and above the absolute gate.
LOCAL_OFFSETS = (-24, -18, -12, 12, 18, 24)
LOCAL_FACTOR = 2.5

GATES = {
    "pop_ratio": 1.30,
    "d_sat": 2.0,
    "d_L": 1.5,
    "d_contrast": 1.5,
    "detail_lo": 0.85,
    "detail_hi": 1.15,
    "audio_step_db": 1.5,
    "audio_click": 3.0,
    "drift_pct": 5.0,
    "overlap_psnr": 32.0,
}
ANALYSIS_W, ANALYSIS_H = 960, 544
# Below this a 100 ms window is silence: a level step from or into it is a sound
# starting or ending (v4 gate: -75 dBFS -> -40 dBFS where a breath begins), which a
# join cannot cause when the audio is joined in latent space; a join defect is a
# step between two sounds, or a click.
SILENCE_DBFS = -60.0


# ----------------------------------------------------------------- frames
def iter_frames(path: str | Path):
    """RGB uint8 frames of any container PyAV/FFmpeg can read (FFV1 mkv, mp4)."""
    import av
    with av.open(str(path)) as ct:
        for fr in ct.decode(video=0):
            yield fr.to_ndarray(format="rgb24")


def frame_count(path: str | Path) -> int:
    return sum(1 for _ in iter_frames(path))


def fit(rgb: np.ndarray, w: int, h: int) -> tuple[int, int]:
    """cv2 (width, height) of a landscape analysis size in the frame's orientation,
    so a portrait take is measured at the same scale instead of squashed."""
    return (w, h) if rgb.shape[1] >= rgb.shape[0] else (h, w)


def _stats(rgb: np.ndarray) -> dict:
    import cv2
    small = cv2.resize(rgb, fit(rgb, ANALYSIS_W, ANALYSIS_H), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB).astype(np.float32)
    L = lab[..., 0] * (100.0 / 255.0)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    g = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lap = float(cv2.Laplacian(g, cv2.CV_32F).var())
    hf = float(np.abs(g - cv2.GaussianBlur(g, (0, 0), 3)).mean())
    return {"L": float(L.mean()), "contrast": float(L.std()),
            "L_p02": float(np.percentile(L, 2)), "L_p98": float(np.percentile(L, 98)),
            "a": float(lab[..., 1].mean() - 128), "b": float(lab[..., 2].mean() - 128),
            "sat": float(hsv[..., 1].mean()), "lap": lap, "hf": hf}


def analyze_frames(path: str | Path) -> tuple[list[dict], np.ndarray]:
    """Per-frame statistics plus the mean absolute difference to the previous frame
    (on a 480x272 RGB thumbnail, all channels, so codec noise does not dominate)."""
    import cv2
    rows, diffs, prev = [], [], None
    for rgb in iter_frames(path):
        rows.append(_stats(rgb))
        th = cv2.resize(rgb, fit(rgb, 480, 272), interpolation=cv2.INTER_AREA).astype(np.float32)
        diffs.append(0.0 if prev is None else float(np.abs(th - prev).mean()))
        prev = th
    return rows, np.array(diffs, dtype=np.float64)


# ------------------------------------------------------------------- audio
def load_audio(path: str | Path) -> tuple[np.ndarray, int] | None:
    """Mono float audio from a WAV or from a video's first audio stream."""
    p = Path(path)
    if p.suffix.lower() == ".wav":
        with wave.open(str(p), "rb") as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        return data.reshape(-1, ch).mean(axis=1), sr
    import av
    with av.open(str(p)) as ct:
        if not ct.streams.audio:
            return None
        chunks, sr = [], None
        for fr in ct.decode(audio=0):
            arr = fr.to_ndarray().astype(np.float32)
            sr = fr.sample_rate
            if arr.ndim == 2 and fr.format.is_planar:
                arr = arr.mean(axis=0)
            elif arr.ndim == 2:
                arr = arr.reshape(-1, len(fr.layout.channels)).mean(axis=1)
            chunks.append(arr.reshape(-1))
        if not chunks or sr is None:
            return None
        return np.concatenate(chunks), int(sr)


def _rms_db(x: np.ndarray) -> float:
    return 20.0 * math.log10(max(1e-6, float(np.sqrt(np.mean(np.square(x)))) if x.size else 1e-6))


def audio_seam(audio: np.ndarray, sr: int, t: float) -> dict:
    """Level step and click at time t (seconds)."""
    w = int(0.1 * sr)
    i = int(round(t * sr))
    before, after = audio[max(0, i - w):i], audio[i:i + w]
    level = max(_rms_db(before), _rms_db(after))
    step = abs(_rms_db(after) - _rms_db(before))
    # local variability: steps between neighbouring windows away from the seam
    ref_steps = []
    for k in (-4, -3, -2, 2, 3, 4):
        j = i + k * w
        a, b = audio[max(0, j - w):j], audio[j:j + w]
        if a.size == w and b.size == w:
            ref_steps.append(abs(_rms_db(b) - _rms_db(a)))
    d = np.abs(np.diff(audio))
    p999 = float(np.percentile(d, 99.9)) if d.size else 0.0
    m = int(0.005 * sr)
    seam_jump = float(d[max(0, i - m):i + m].max()) if d.size else 0.0
    return {"level_dbfs": round(level, 2), "step_db": round(step, 3),
            "local_step_db": round(float(np.median(ref_steps)) if ref_steps else 0.0, 3),
            "onset": min(_rms_db(before), _rms_db(after)) < SILENCE_DBFS <= level,
            "click": round(seam_jump / p999, 3) if p999 > 0 else 0.0}


# ------------------------------------------------------------------ seams
def _win(rows: list[dict], a: int, b: int, key: str) -> float:
    seg = [r[key] for r in rows[max(0, a):max(0, b)]]
    return float(np.mean(seg)) if seg else float("nan")


def seam_metrics(rows: list[dict], diffs: np.ndarray, b: int, span: int = 0) -> dict:
    """span: frames before the seam over which the join is spread (a dissolve across
    a carried overlap). window_pop = the largest frame step anywhere in
    [b-span, b+2) over the median motion outside that window."""
    local = np.r_[diffs[max(1, b - 12):b - 1], diffs[b + 2:b + 13]]
    med = float(np.median(local)) if local.size else 0.0
    out = {"frame": int(b), "diff": round(float(diffs[b]), 3), "local_median_diff": round(med, 3),
           "pop_ratio": round(float(diffs[b]) / med, 3) if med > 1e-6 else None}
    if span > 0:
        lo = max(1, b - span)
        outside = np.r_[diffs[max(1, lo - 12):lo], diffs[b + 2:b + 14]]
        omed = float(np.median(outside)) if outside.size else 0.0
        wmax = float(diffs[lo:b + 2].max()) if b + 2 > lo else 0.0
        out["window_pop"] = round(wmax / omed, 3) if omed > 1e-6 else None
        out["window_peak_frame"] = int(lo + int(np.argmax(diffs[lo:b + 2]))) if b + 2 > lo else None
    def step(key: str, at: int) -> float:
        return _win(rows, at, at + 6, key) - _win(rows, at - 6, at, key)

    def detail(at: int) -> float | None:
        a, bf = _win(rows, at, at + 4, "lap"), _win(rows, at - 4, at, "lap")
        return a / bf if bf > 1e-6 else None

    # the same measurement at neighbouring offsets: how fast the look changes here
    # anyway (a camera move into warm light changes saturation on any frame, a
    # seam defect is a change the moment does not otherwise have)
    near = [b + k for k in LOCAL_OFFSETS if 6 <= b + k <= len(rows) - 6]
    for key, name in (("sat", "d_sat"), ("L", "d_L"), ("contrast", "d_contrast"),
                      ("lap", "d_lap"), ("hf", "d_hf"), ("a", "d_a"), ("b", "d_b")):
        out[name] = round(step(key, b), 3)
        if name in ("d_sat", "d_L", "d_contrast") and near:
            out[name + "_local"] = round(float(np.median([abs(step(key, k)) for k in near])), 3)
    dr = detail(b)
    out["detail_ratio"] = round(dr, 3) if dr is not None else None
    loc = [abs(np.log(d)) for d in (detail(k) for k in near) if d]
    if loc:
        out["detail_local"] = round(float(np.exp(np.median(loc))), 3)    # typical ratio, as >= 1
    return out


def motion_profile(video: str | Path, seams: list[int], span: int = 8) -> dict[int, dict]:
    """Optical-flow speed (Farneback, 480x272 grey) over `span` frames before and
    after each seam. motion_ratio = after/before: an abrupt start or stop of
    motion at the join (a velocity step) reads as a jolt even without a pop.
    Reported, not gated: an action that starts on the cut legitimately changes it."""
    import cv2
    want = {k for b in seams for k in range(b - span - 1, b + span + 1)}
    grey = {}
    for i, f in enumerate(iter_frames(video)):
        if i in want:
            grey[i] = cv2.cvtColor(cv2.resize(f, fit(f, 480, 272), interpolation=cv2.INTER_AREA),
                                   cv2.COLOR_RGB2GRAY)
    out = {}
    for b in seams:
        mags = {}
        for k in range(b - span, b + span):
            if k - 1 in grey and k in grey:
                fl = cv2.calcOpticalFlowFarneback(grey[k - 1], grey[k], None,  # type: ignore[call-overload]
                                                  0.5, 3, 15, 3, 5, 1.1, 0)
                mags[k] = float(np.linalg.norm(fl, axis=2).mean())
        before = [mags[k] for k in range(b - span, b) if k in mags]
        after = [mags[k] for k in range(b, b + span) if k in mags]
        if before and after:
            mb, ma = float(np.mean(before)), float(np.mean(after))
            out[b] = {"flow_before": round(mb, 3), "flow_after": round(ma, 3), "flow_at": round(mags.get(b, 0.0), 3),
                      "motion_ratio": round(max(ma, mb) / max(1e-3, min(ma, mb)), 3)}
    return out


def judge_seam(s: dict) -> dict:
    fails = []
    if s.get("pop_ratio") is not None and s["pop_ratio"] > GATES["pop_ratio"]:
        fails.append("pop")
    if s.get("window_pop") is not None and s["window_pop"] > GATES["pop_ratio"]:
        fails.append("window_pop")
    for name, label in (("d_sat", "saturation"), ("d_L", "lightness"), ("d_contrast", "contrast")):
        # a step fails when it is both above the absolute gate and unusual for the moment
        if abs(s[name]) > max(GATES[name], LOCAL_FACTOR * s.get(name + "_local", 0.0)):
            fails.append(label)
    dr = s.get("detail_ratio")
    if dr is not None:
        lo, hi = GATES["detail_lo"], GATES["detail_hi"]
        loc = s.get("detail_local")
        if loc:
            lo, hi = min(lo, loc ** -LOCAL_FACTOR), max(hi, loc ** LOCAL_FACTOR)
        if not lo <= dr <= hi:
            fails.append("detail")
    a = s.get("audio")
    if (a and a["level_dbfs"] > -45 and not a.get("onset")
            and a["step_db"] > max(GATES["audio_step_db"], 2 * a["local_step_db"])):
        fails.append("audio_level")
    if a and a["click"] > GATES["audio_click"]:
        fails.append("audio_click")
    return {"pass": not fails, "fails": fails}


CARRY_END_CHECKS = ("pop", "saturation", "lightness", "contrast")


def carry_end_check(video: str | Path, carry: int) -> dict:
    """A new link's own seam, in its own decode: the first generated frame after
    the `carry` carried ones, judged only on what the link's sampling decides (a
    pop and the look steps), not on detail, which content changes legitimately
    (a blink, a lean-in). score: how far the worst of those checks is past its
    gate (1.0 = at the gate), to pick the better of two attempts."""
    rows, diffs = analyze_frames(video)
    if not 6 <= int(carry) <= len(rows) - 6:
        return {"frame": int(carry), "pass": True, "skipped": True, "score": 0.0}
    s = seam_metrics(rows, diffs, int(carry))
    fails = [f for f in judge_seam(s)["fails"] if f in CARRY_END_CHECKS]
    excess = [s["pop_ratio"] / GATES["pop_ratio"]] if s.get("pop_ratio") is not None else []
    for name in ("d_sat", "d_L", "d_contrast"):
        excess.append(abs(s[name]) / max(GATES[name], LOCAL_FACTOR * s.get(name + "_local", 0.0)))
    return {"frame": int(carry), "pass": not fails, "fails": fails, "score": round(max(excess or [0.0]), 3),
            **{k: s.get(k) for k in ("pop_ratio", "d_sat", "d_L", "d_contrast")}}


def drift(rows: list[dict], bounds: list[int]) -> dict:
    """Relative change of the take's look from link 1 to the last link."""
    edges = [0] + list(bounds) + [len(rows)]
    links = []
    for a, b in zip(edges, edges[1:]):
        seg = rows[a:b]
        links.append({k: float(np.mean([r[k] for r in seg])) for k in ("L", "contrast", "sat", "lap", "hf")})
    first, last = links[0], links[-1]
    pct = {k: round(100.0 * (last[k] - first[k]) / max(1e-6, abs(first[k])), 2) for k in first}
    worst = max(abs(pct[k]) for k in ("L", "contrast", "sat", "hf"))
    return {"links": [{k: round(v, 3) for k, v in d.items()} for d in links], "pct": pct,
            "worst_pct": round(worst, 2), "pass": worst <= GATES["drift_pct"]}


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2))
    return 99.0 if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)


def overlap_fidelity(prev_video: str | Path, cur_video: str | Path, carry: int) -> dict:
    """PSNR of the carried frames: the new link's first `carry` frames against the
    previous link's last `carry` frames (both lossless)."""
    prev = list(iter_frames(prev_video))[-carry:]
    cur = []
    for i, f in enumerate(iter_frames(cur_video)):
        if i >= carry:
            break
        cur.append(f)
    vals = [round(psnr(a, b), 2) for a, b in zip(prev, cur)]
    return {"psnr": vals, "min": min(vals) if vals else None, "mean": round(float(np.mean(vals)), 2) if vals else None,
            "pass": bool(vals) and min(vals) >= GATES["overlap_psnr"]}


def audio_overlap_fidelity(prev_audio: str | Path, cur_audio: str | Path, carry: int,
                           fps: float = 24.0) -> dict:
    """SNR of the carried audio: the new link's first `carry` frames of sound against
    the previous link's last `carry` frames (float waveforms from *.audio.safetensors).
    Reported, not gated: the audio decoder sees different context at a clip start."""
    from safetensors import safe_open

    def _load(p):
        with safe_open(str(p), framework="np") as f:
            return f.get_tensor("waveform").astype(np.float32), int((f.metadata() or {}).get("sample_rate") or 0)
    (pa, sr), (ca, sr2) = _load(prev_audio), _load(cur_audio)
    if sr <= 0 or sr != sr2:
        return {"snr_db": None, "error": "sample rates differ or missing"}
    n = int(round(carry / fps * sr))
    a, b = pa[..., -n:].mean(axis=0), ca[..., :n].mean(axis=0)
    m = min(a.shape[-1], b.shape[-1])
    a, b = a[-m:], b[:m]
    sig, err = float(np.mean(a ** 2)), float(np.mean((a - b) ** 2))
    # the middle half avoids the decoder's start-of-clip context
    q = m // 4
    sig_m, err_m = float(np.mean(a[q:m - q] ** 2)), float(np.mean((a[q:m - q] - b[q:m - q]) ** 2))
    db = lambda s, e: round(10 * math.log10(s / e), 2) if e > 0 and s > 0 else 99.0  # noqa: E731
    return {"snr_db": db(sig, err), "snr_mid_db": db(sig_m, err_m), "level_dbfs": round(_rms_db(a), 2)}


@dataclass
class TakeReport:
    video: str
    seams: list[dict] = field(default_factory=list)
    drift: dict = field(default_factory=dict)
    overlaps: list[dict] = field(default_factory=list)
    frames: int = 0
    passed: bool = False

    def to_json(self) -> dict:
        return {"video": self.video, "frames": self.frames, "passed": self.passed,
                "seams": self.seams, "drift": self.drift, "overlaps": self.overlaps, "gates": GATES}


def analyze_take(video: str | Path, seam_frames: list[int], fps: float = 24.0,
                 audio: str | Path | None = None, out_dir: str | Path | None = None,
                 overlaps: list[dict] | None = None, span: int = 0) -> TakeReport:
    """span: frames before each seam the join is spread over (0 = a cut at the seam)."""
    rows, diffs = analyze_frames(video)
    rep = TakeReport(video=str(video), frames=len(rows))
    au = load_audio(audio or video)
    motion = motion_profile(video, [int(b) for b in seam_frames if 10 <= b < len(rows) - 10])
    for b in seam_frames:
        if not (2 <= b < len(rows) - 2):
            continue
        s = seam_metrics(rows, diffs, int(b), span=int(span))
        if au is not None:
            s["audio"] = audio_seam(au[0], au[1], b / float(fps))
        if int(b) in motion:
            s["motion"] = motion[int(b)]
        s.update(judge_seam(s))
        rep.seams.append(s)
    rep.drift = drift(rows, [int(b) for b in seam_frames]) if seam_frames else {"pass": True}
    rep.overlaps = list(overlaps or [])
    rep.passed = (all(s["pass"] for s in rep.seams) and bool(rep.drift.get("pass"))
                  and all(o.get("pass", True) for o in rep.overlaps))
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "seam_qa.json").write_text(json.dumps(rep.to_json(), indent=1), encoding="utf-8")
        curve = [{"f": i, **{k: round(r[k], 3) for k in ("L", "contrast", "sat", "lap", "hf")},
                  "diff": round(float(diffs[i]), 3)} for i, r in enumerate(rows)]
        (out / "frame_curve.json").write_text(json.dumps(curve), encoding="utf-8")
        write_seam_strips(video, seam_frames, out)
    return rep


def write_seam_strips(video: str | Path, seam_frames: list[int], out_dir: Path) -> None:
    """Frames b-2..b+1 of every seam side by side, plus a centre crop at full res."""
    import cv2
    want = {k for b in seam_frames for k in range(b - 2, b + 2)}
    keep = {}
    for i, f in enumerate(iter_frames(video)):
        if i in want:
            keep[i] = f
    for b in seam_frames:
        row = [cv2.resize(keep[k], fit(keep[k], 640, 360)) for k in range(b - 2, b + 2) if k in keep]
        if len(row) == 4:
            strip = cv2.cvtColor(np.hstack(row), cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(out_dir / f"seam_{b:05d}_strip.jpg"), strip, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if b - 1 in keep and b in keep:
            h, w = keep[b].shape[:2]
            crops = [keep[k][h // 4:3 * h // 4, w // 3:2 * w // 3] for k in (b - 1, b)]
            pair = cv2.cvtColor(np.hstack(crops), cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(out_dir / f"seam_{b:05d}_crop.jpg"), pair, [cv2.IMWRITE_JPEG_QUALITY, 92])


def summarize(rep: TakeReport) -> str:
    lines = [f"take {Path(rep.video).name}: {rep.frames} frames, {'PASS' if rep.passed else 'FAIL'}"]
    for s in rep.seams:
        a = s.get("audio") or {}
        mo = s.get("motion") or {}
        wp = f" window {s['window_pop']}x@{s.get('window_peak_frame')}" if s.get("window_pop") is not None else ""
        lines.append(f"  seam @{s['frame']}: pop {s['pop_ratio']}x{wp}  dSat {s['d_sat']:+.2f}  dL {s['d_L']:+.2f}  "
                     f"dContrast {s['d_contrast']:+.2f}  detail {s['detail_ratio']}  "
                     f"motion {mo.get('flow_before', '-')}->{mo.get('flow_after', '-')}px ({mo.get('motion_ratio', '-')}x)  "
                     f"audio step {a.get('step_db', '-')} dB{' (onset)' if a.get('onset') else ''} "
                     f"click {a.get('click', '-')}  "
                     f"{'ok' if s['pass'] else 'FAIL ' + ','.join(s['fails'])}")
    d = rep.drift
    if d.get("pct"):
        lines.append(f"  drift link1->last: {d['pct']}  worst {d['worst_pct']}%  {'ok' if d['pass'] else 'FAIL'}")
    for o in rep.overlaps:
        au = o.get("audio") or {}
        lines.append(f"  overlap {o.get('link')}: min {o.get('min')} dB mean {o.get('mean')} dB "
                     f"{'ok' if o.get('pass') else 'FAIL'}"
                     + (f" | audio SNR {au.get('snr_db')} dB (mid {au.get('snr_mid_db')} dB)" if au else ""))
    return "\n".join(lines)
