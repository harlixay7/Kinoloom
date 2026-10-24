"""Edge-variance and motion-profile metrics used by the archived benchmarks.

CAVEAT, stated once here and repeated wherever the numbers are quoted: edge_* is
a Laplacian/FIND_EDGES texture-energy indicator, not perceptual quality. On the
lab machine its ordering reversed across resolutions (the DFR "richest" clip had
the lowest 1920x1088 mean, 36 vs 1232 for the no-DFR clip), and same-seed
spread on the sister pipeline ranged from -56% to +40% for edge and -46% to
+108% for motion. Use it for same-configuration, same-resolution comparisons
only. See docs/03_RETRACTIONS_FAILURES_AND_LESSONS.md.

Implementation note: the temporal profiles use Pillow only (no numpy). The
original lab script filtered with ImageFilter.FIND_EDGES and took the variance
of the filtered array; ImageStat.Stat(...).var[0] computes exactly that for an
"L" image. Percentiles use linear interpolation, matching numpy's default.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (numpy default method)."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def edge_variance(image: Image.Image) -> float:
    """Variance of the FIND_EDGES response, the lab's texture-energy proxy."""
    gray = image.convert("L").filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(gray).var[0])


def frame_motion(a: Image.Image, b: Image.Image, size: tuple[int, int] = (240, 136)) -> float:
    """Mean absolute per-channel difference of two consecutive frames."""
    ia = a.convert("RGB").resize(size)
    ib = b.convert("RGB").resize(size)
    diff = ImageChops.difference(ia, ib)
    return float(sum(ImageStat.Stat(diff).mean) / 3.0)


def profile_frames(frames: list[Image.Image]) -> dict:
    edge = [edge_variance(f) for f in frames]
    motion = [frame_motion(a, b) for a, b in zip(frames, frames[1:])]
    motion_mean = round(sum(motion) / len(motion), 2) if motion else None
    motion_p90 = round(_percentile(motion, 90), 2) if motion else None
    if motion:
        motion.append(0.0)  # Preserved solely for per-frame timeline alignment
    out = {
        "frames_sampled": len(frames),
        "edge_mean": round(sum(edge) / len(edge), 1) if edge else None,
        "edge_min": round(min(edge), 1) if edge else None,
        "edge_p10": round(_percentile(edge, 10), 1) if edge else None,
        "edge_p90": round(_percentile(edge, 90), 1) if edge else None,
        "edge_ratio_p10_over_p90": (
            round(_percentile(edge, 10) / max(_percentile(edge, 90), 1e-9), 3) if edge else None),
        "motion_mean": motion_mean,
        "motion_p90": motion_p90,
    }
    return out


def extract_frames(video_path: str, fps: int = 4, scale: int = 480) -> list[Image.Image]:
    """Decode `fps` frames per second at `scale` px wide via ffmpeg into a temp dir."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH; required for video profiling")
    with tempfile.TemporaryDirectory() as td:
        pattern = str(Path(td) / "f%05d.png")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-vf", f"fps={fps},scale={scale}:-2", pattern]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:300]}")
        frames = [Image.open(p).convert("RGB") for p in sorted(Path(td).glob("f*.png"))]
    return frames


def video_profile(video_path: str, fps: int = 4, scale: int = 480) -> dict:
    return profile_frames(extract_frames(video_path, fps=fps, scale=scale))


def image_metrics(image_path: str) -> dict:
    img = Image.open(image_path)
    rgba = img.convert("RGBA")
    gray = rgba.convert("L")
    stat = ImageStat.Stat(gray)
    out = {
        "path": str(image_path),
        "size": list(img.size),
        "mode": img.mode,
        "has_alpha": img.mode in ("RGBA", "LA"),
        "edge_variance": round(edge_variance(img), 2),
        "rms_contrast": round(stat.stddev[0] / 255.0, 4),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="videolab edge/motion metrics")
    ap.add_argument("path", type=str, help="video file or image file")
    ap.add_argument("--fps", type=int, default=4, help="sampling rate for video profiling")
    ap.add_argument("--scale", type=int, default=480, help="frame width for video profiling")
    args = ap.parse_args()
    suffix = Path(args.path).suffix.lower()
    try:
        if suffix in (".mp4", ".mkv", ".mov", ".webm"):
            result = video_profile(args.path, fps=args.fps, scale=args.scale)
        else:
            result = image_metrics(args.path)
    except (RuntimeError, OSError) as exc:
        print(f"[eval_metrics] {exc}")
        return 3
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
