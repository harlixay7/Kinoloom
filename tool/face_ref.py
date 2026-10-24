"""Face close-up references cut from cast portraits (LTX-2.5 MSR identity).

The MSR guide fits every reference into the draft frame (960x544): a waist-up
or full-body portrait leaves the face 40-90 px tall, too small for the model to
hold the identity across a chain. A square close-up of the same portrait in a
free slot gives the face ~270 px. Measured on gate link 1 (2026-10-23, same seed):
ArcFace similarity of the lead's face to her portrait 0.205 -> 0.303, her moles
and face shape came back, and the apparent age early in the link fell by 3-5 years.

The crop: the largest frontal (else profile) face found by OpenCV's Haar
cascades, a square of `scale` x the face height centred a little below the face
centre (hair, chin and neck in frame), resized to `size`. Portraits whose face
already fills `max_face_frac` of the height are close-ups already: no crop.
Crops are cached by the source's content hash, so a chain resumed later finds
the same file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from core import log_setup

log = log_setup("face_ref")


def _largest_face(gray):
    import cv2
    h, w = gray.shape
    mn = max(24, min(h, w) // 20)
    eq = cv2.equalizeHist(gray)
    for name, flip in (("haarcascade_frontalface_default.xml", False),
                       ("haarcascade_profileface.xml", False),
                       ("haarcascade_profileface.xml", True)):
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + name)  # type: ignore[attr-defined]
        img = cv2.flip(eq, 1) if flip else eq
        faces = casc.detectMultiScale(img, 1.1, 6, minSize=(mn, mn))
        if len(faces):
            x, y, fw, fh = (int(v) for v in max(faces, key=lambda f: f[2] * f[3]))
            return (w - x - fw if flip else x), y, fw, fh
    return None


def face_box(src: Path) -> tuple[int, int, int, int, int, int] | None:
    """(x, y, w, h, image_w, image_h) of the largest face, or None."""
    import cv2
    img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    box = _largest_face(img)
    return None if box is None else (*box, img.shape[1], img.shape[0])


def face_closeup(src: Path, cache_dir: Path, *, scale: float = 2.0, size: int = 768,
                 max_face_frac: float = 0.4) -> Path | None:
    """A cached square face close-up of `src`, or None (no face found, the
    portrait is a close-up already, or the image cannot be read)."""
    import cv2
    import numpy as np
    src = Path(src)
    try:
        data = src.read_bytes()
    except OSError as e:
        log.warning(f"face close-up: {src.name} unreadable: {e}")
        return None
    key = hashlib.sha256(data + f"|{scale}|{size}".encode()).hexdigest()[:16]
    out = Path(cache_dir) / f"face_{key}.png"
    if out.is_file():
        return out
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    box = _largest_face(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    if box is None:
        log.info(f"face close-up: no face found in {src.name}")
        return None
    x, y, fw, fh = box
    h, w = img.shape[:2]
    if fh >= max_face_frac * h:
        return None
    s = min(int(fh * scale), h, w)
    cx, cy = x + fw / 2, y + fh * 0.55
    x0 = int(np.clip(cx - s / 2, 0, w - s))
    y0 = int(np.clip(cy - s / 2, 0, h - s))
    crop = img[y0:y0 + s, x0:x0 + s]
    crop = cv2.resize(crop, (size, size), interpolation=cv2.INTER_LANCZOS4 if s < size else cv2.INTER_AREA)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    if not cv2.imwrite(str(tmp), crop):
        return None
    tmp.replace(out)
    log.info(f"face close-up: {src.name} face {fw}x{fh} in {w}x{h} -> {out.name}")
    return out
