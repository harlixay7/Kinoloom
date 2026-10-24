"""Cast drift report: faces matched to the cast by identity, per-link means, summary."""
from types import SimpleNamespace

import numpy as np
import pytest

import face_drift

A, B = np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])


def _video(path, frames):
    av = pytest.importorskip("av")      # not in tests/requirements-ci.txt
    with av.open(str(path), "w") as ct:
        st = ct.add_stream("ffv1", rate=24)
        st.width, st.height, st.pix_fmt = 64, 48, "yuv444p"
        for i in range(frames):
            img = np.full((48, 64, 3), i % 256, np.uint8)
            for pkt in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                ct.mux(pkt)
        for pkt in st.encode():
            ct.mux(pkt)


class _App:
    """A reference portrait (tag 1 or 2 in its first pixel) shows that cast member.
    Video frame i (grey level i) shows A, aging a year per 12 frames, from frame 24
    on also B, and always a face too small to judge."""

    def get(self, img):
        if img.shape[0] > 100:
            return [SimpleNamespace(bbox=(0, 0, 80, 80), embedding=A if img[0, 0, 0] == 1 else B, age=25)]
        level = int(img[0, 0, 0])
        out = [SimpleNamespace(bbox=(0, 0, 45, 45), embedding=A + 0.1 * B, age=25 + level // 12)]
        if level >= 24:
            out.append(SimpleNamespace(bbox=(0, 0, 45, 45), embedding=B, age=33))
        out.append(SimpleNamespace(bbox=(0, 0, 10, 10), embedding=A, age=90))
        return out


def test_cast_drift_reports_identity_and_age_per_link(tmp_path, monkeypatch):
    import cv2
    monkeypatch.setattr(face_drift, "_APP", _App())
    for name, tag in (("a.png", 1), ("b.png", 2)):
        ref = np.zeros((120, 120, 3), np.uint8)
        ref[0, 0, 0] = tag
        cv2.imwrite(str(tmp_path / name), ref)
    v = tmp_path / "take.mkv"
    _video(v, 48)
    d = face_drift.cast_drift(v, [24], {"A": tmp_path / "a.png", "B": tmp_path / "b.png"}, every=6)
    assert [lk["link"] for lk in d["links"]] == [1, 2]
    a1, a2 = d["links"][0]["A"], d["links"][1]["A"]
    assert a1["n"] == 4 and a2["n"] == 4 and a1["sim"] > 0.99
    assert a1["age"] < a2["age"]                                  # A ages through the take
    assert "B" not in d["links"][0] and d["links"][1]["B"]["age"] == 33
    s = d["summary"]["A"]
    assert s["age_change"] == round(a2["age"] - a1["age"], 1) and s["ref_age"] == 25
    text = face_drift.summarize(d)
    assert "cast A: identity" in text and "L1 " in text and "L2 " in text
    assert face_drift.summarize(None) == ""


def test_cast_drift_is_none_without_the_face_model(monkeypatch, tmp_path):
    monkeypatch.setattr(face_drift, "_APP", False)
    assert face_drift.cast_drift(tmp_path / "x.mkv", [], {"A": tmp_path / "a.png"}) is None
