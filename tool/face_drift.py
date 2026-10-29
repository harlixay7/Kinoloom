"""Cast identity and apparent age through a chained take, link by link (reported
with the seam QA, not a gate).

Per sampled frame every detected face is matched to the cast by ArcFace cosine
against each cast portrait (InsightFace buffalo_l: det_10g, w600k_r50, genderage
on the CPU) and logged with its apparent age. Per link and cast member: the
mean similarity and the mean apparent age. The estimator reads expression as age
(crying faces score 5-10 years older) and its absolute ages are off by years, so
only the change of the same person between links of one take means something;
that is why this is reported and not gated. Faces are judged at one analysis
size (the longer side at most the draft's 960) so a 1080p final and its draft
compare, in either orientation.

Without insightface or its models (~/.insightface/models/buffalo_l) the report
is None.
"""
from __future__ import annotations

from pathlib import Path

from core import log_setup

log = log_setup("face_drift")

_APP = None


def _app():
    global _APP
    if _APP is None:
        try:
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition", "genderage"],
                               providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            _APP = app
        except Exception as e:  # noqa: BLE001 - optional dependency / missing models
            log.warning(f"face drift unavailable: {e}")
            _APP = False
    return _APP or None


def _norm(v):
    import numpy as np
    return v / (np.linalg.norm(v) + 1e-9)


def _scaled(bgr, width: int):
    import cv2
    if max(bgr.shape[:2]) > width:
        k = width / max(bgr.shape[:2])
        bgr = cv2.resize(bgr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    return bgr


def cast_drift(video: Path, link_starts: list[int], cast: dict[str, Path], *, every: int = 6,
               width: int = 960, min_face: int = 40, min_sim: float = 0.15) -> dict | None:
    """link_starts: first take frame of every later link (the take's seams)."""
    app = _app()
    if app is None or not cast:
        return None
    import av
    import cv2
    import numpy as np
    refs = {}
    for name, p in cast.items():
        img = cv2.imread(str(p))
        faces = app.get(img) if img is not None else []
        if faces:
            f = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            refs[name] = {"emb": _norm(f.embedding), "age": int(f.age)}
    if not refs:
        return None
    edges = [0] + sorted(int(s) for s in link_starts)
    rows = []
    with av.open(str(video)) as ct:
        for i, fr in enumerate(ct.decode(video=0)):
            if i % every:
                continue
            bgr = _scaled(cv2.cvtColor(fr.to_ndarray(format="rgb24"), cv2.COLOR_RGB2BGR), width)
            for f in app.get(bgr):
                if f.bbox[3] - f.bbox[1] < min_face:
                    continue
                e = _norm(f.embedding)
                sims = {k: float(e @ v["emb"]) for k, v in refs.items()}
                who = max(sims, key=lambda k: sims[k])
                if sims[who] >= min_sim:
                    link = sum(1 for s in edges if i >= s)
                    rows.append((link, who, sims[who], int(f.age)))
    links = []
    for li in range(1, len(edges) + 1):
        entry: dict = {"link": li}
        for name in refs:
            r = [x for x in rows if x[0] == li and x[1] == name]
            if r:
                entry[name] = {"n": len(r), "sim": round(float(np.mean([x[2] for x in r])), 3),
                               "age": round(float(np.mean([x[3] for x in r])), 1)}
        links.append(entry)
    summary = {}
    for name in refs:
        seen = [lk[name] for lk in links if name in lk]
        if seen:
            summary[name] = {"ref_age": refs[name]["age"], "sim_first": seen[0]["sim"], "sim_last": seen[-1]["sim"],
                             "age_first": seen[0]["age"], "age_last": seen[-1]["age"],
                             "age_change": round(seen[-1]["age"] - seen[0]["age"], 1)}
    return {"every": every, "width": width, "links": links, "summary": summary}


_REFS: dict[tuple[str, int, int], object] = {}    # (path, size, mtime) -> embedding or None


def _ref_embedding(app, p: Path):
    import cv2
    p = Path(p)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_size, int(st.st_mtime))
    if key not in _REFS:
        img = cv2.imread(str(p))
        faces = app.get(img) if img is not None else []
        _REFS[key] = (_norm(max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])).embedding)
                      if faces else None)
    return _REFS[key]


def available() -> bool:
    """InsightFace and its buffalo_l models load (on the CPU)."""
    return _app() is not None


def likeness(image: Path, cast: dict[str, Path], *, width: int = 1344) -> dict[str, float | None] | None:
    """How much each cast member looks like themselves in a still: the ArcFace
    cosine of their reference picture to the best-matching face in `image`.
    A member whose picture shows no face is left out; one with a face but
    none in the still scores None. None when InsightFace is not available."""
    app = _app()
    if app is None:
        return None
    import cv2
    refs = {name: e for name, p in cast.items() if (e := _ref_embedding(app, Path(p))) is not None}
    if not refs:
        return {}
    img = cv2.imread(str(image))
    faces = [_norm(f.embedding) for f in app.get(_scaled(img, width))] if img is not None else []
    return {name: (round(max(float(f @ e) for f in faces), 3) if faces else None)
            for name, e in refs.items()}


def summarize(d: dict | None) -> str:
    if not d:
        return ""
    lines = []
    for name, s in d["summary"].items():
        per = "  ".join(f"L{lk['link']} {lk[name]['sim']:.2f}/{lk[name]['age']:.0f}"
                        for lk in d["links"] if name in lk)
        lines.append(f"  cast {name}: identity {s['sim_first']:.2f} -> {s['sim_last']:.2f}, apparent age "
                     f"{s['age_first']:.0f} -> {s['age_last']:.0f} ({s['age_change']:+.0f}) | {per}")
    return "\n".join(lines)
