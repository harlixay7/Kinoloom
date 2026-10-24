"""Clip-to-clip continuity: tail frames + CUT/CONTINUE/MATCH_ACTION
handling. Handles extraction exceptions internally and routes
fallbacks to alternative image sources."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import ROOT, ffmpeg_exe, load_config, log_setup

log = log_setup("continuity")

TAIL_DIR = ROOT / "shots" / "tails"
KF_DIR = ROOT / "shots" / "keyframes"


def _ns(pid) -> str:
    from core import is_safe_component
    return str(pid) if pid and is_safe_component(str(pid)) else "_"


def _frame_score(path: Path) -> float:
    """Laplacian variance + edge density — picks the sharpest tail frame."""
    import math
    import cv2
    import numpy as np
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None or img.size == 0:
        return 0.0
    var = float(cv2.Laplacian(img, cv2.CV_64F).var())
    edges = float(np.mean(cv2.Canny(img, 50, 150) > 0))
    return 0.7 * min(1.0, math.log1p(max(var, 0.0)) / math.log1p(5000.0)) \
        + 0.3 * edges


def extract_tail(video_path, shot_id: str, n: int = 5,
                 pid: str | None = None) -> Path | None:
    """Extract the LAST n frames, score each (sharpness + edge density),
    copy the best to shots/tails/<pid>/<shot_id>_tail.png. Returns the
    path or None — never raises."""
    import glob
    import shutil
    import subprocess
    import uuid
    try:
        cfg = load_config()
        ff = ffmpeg_exe(cfg)
        video = Path(video_path)
        if not video.exists():
            return None
        tmp = ROOT / "temp" / f"tail_{shot_id}_{uuid.uuid4().hex[:6]}"
        tmp.mkdir(parents=True, exist_ok=True)
        subprocess.run([ff, "-y", "-v", "error", "-i", str(video),
                        str(tmp / "f_%05d.png")], capture_output=True)
        frames = sorted(glob.glob(str(tmp / "f_*.png")))
        if not frames:
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        tail_frames = frames[-max(1, int(n)):]
        best = max(tail_frames, key=_frame_score)
        tail_dir = TAIL_DIR / _ns(pid)
        tail_dir.mkdir(parents=True, exist_ok=True)
        dest = tail_dir / f"{shot_id}_tail.png"
        shutil.copy2(best, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        if dest.exists() and dest.stat().st_size > 100:
            log.info(f"tail extracted: {dest}")
            return dest
        return None
    except Exception as e:
        log.warning(f"tail extraction failed for {shot_id}: {e}")
        return None


def _row_keyframe_path(shot: dict, db) -> str | None:
    sid = shot.get("shot_id", "")
    kp = shot.get("keyframe_path")
    if not kp and db is not None and sid:
        try:
            kp = (db.get(sid, shot.get("project_id")) or {}).get(
                "keyframe_path")
        except Exception:
            kp = None
    return kp


def _best_generated(shot_id: str, pid: str | None = None) -> Path | None:
    p = KF_DIR / _ns(pid) / shot_id / "candidates.json"
    if not p.exists():
        return None
    try:
        from core import jload
        data = jload(p)
        files = data.get("files") or []
        approved = data.get("approved")
        if approved:
            cand = ROOT / str(approved)
            if cand.exists():
                return cand
        for f in sorted(files, key=lambda d: d.get("score", 0.0),
                        reverse=True):
            cand = ROOT / str(f.get("file", ""))
            if cand.exists():
                return cand
    except Exception as e:
        log.warning(f"generated keyframe lookup failed: {e}")
    return None


def _roster_keyframe(shot: dict) -> Path | None:
    """Mirror orchestrator._pick_keyframe: roster character keyframe,
    else location still."""
    for c in shot.get("characters", []) or []:
        if c.get("tier") == "crowd":
            continue
        for cand in (ROOT / "roster" / "keyframes" / c["id"]).glob("*.png"):
            return cand
        break
    loc = shot.get("location") or ""
    if loc:
        for cand in (ROOT / "roster" / "locations" / loc).glob("*.png"):
            return cand
        for cand in (ROOT / "bible" / "locations").glob(f"{loc}*.png"):
            return cand
    return None


def start_image_for(shot: dict, prev_shot_row: dict | None, db,
                    bible, allow_roster: bool = True) -> tuple[Path | None, str]:
    """Continuity routing: the I2V start image and where it came from.
    Mode is one of tail | keyframe | roster | none. allow_roster=False
    disables the identity-portrait fallback — shots with no ref/keyframe
    then return (None, "none") so the renderer uses T2V instead of
    starting from the character's frozen portrait."""
    from shotspec import continuity_of
    mode = continuity_of(shot)
    sid = shot.get("shot_id", "")
    pid = shot.get("project_id")

    tail = prev_shot_row.get("tail_frame") if prev_shot_row else None
    if mode in ("CONTINUE", "MATCH_ACTION") and tail:
        tp = Path(tail)
        if tp.exists():
            return tp, "tail"

    kp = _row_keyframe_path(shot, db)
    if kp:
        kpp = Path(kp)
        if kpp.exists():
            return kpp, "keyframe"

    if shot.get("keyframe_source", "auto") in ("generated", "auto"):
        gen = _best_generated(sid, pid)
        if gen is not None:
            return gen, "keyframe"

    if not allow_roster:
        return None, "none"

    roster = _roster_keyframe(shot)
    if roster is not None:
        return roster, "roster"

    return None, "none"


def continuity_notes(shot: dict, prev_shot_row: dict | None) -> str:
    """Human-readable continuity line for the prompt/UI."""
    from shotspec import continuity_of
    mode = continuity_of(shot)
    prev_id = (prev_shot_row or {}).get("shot_id", "")
    if mode == "CONTINUE":
        return (f"CONTINUE from {prev_id} tail frame"
                if prev_id else "CONTINUE from previous shot tail frame")
    if mode == "MATCH_ACTION":
        return (f"MATCH_ACTION — match {prev_id} tail composition"
                if prev_id else "MATCH_ACTION — match previous shot "
                                "tail composition")
    return "CUT — fresh composition"
