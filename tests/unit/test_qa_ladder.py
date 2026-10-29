"""QA verdict semantics + the adjust_lora retry step."""
from unittest.mock import patch

import numpy as np
import pytest

from lora_video import soften_effect_loras
from qa_director import QADirector


def _qa(motion, clip=None):
    d = QADirector(cfg={"qa": {"motion_score_range": [0.00005, 6.0]}})
    with patch.object(QADirector, "motion_score", return_value=motion), \
         patch.object(QADirector, "clip_alignment", return_value=clip):
        return d.evaluate({"characters": []}, "x.mp4", "a prompt")


class _Face:
    def __init__(self, h, sim):
        self.bbox = np.array([0.0, 0.0, h, h])
        self.normed_embedding = np.array([sim, (1 - sim * sim) ** 0.5])   # cosine `sim` to the cast's


class _App:
    """The cast's portrait (8 px tall) holds its face; the shot's frames hold `faces`."""

    def __init__(self, faces):
        self.faces = faces

    def get(self, img):
        return [_Face(8, 1.0)] if img.shape[0] == 8 else self.faces


def _movie_qa(tmp_path, faces, qa=None, clip=None):
    import cv2
    ref = tmp_path / "mara.png"
    cv2.imwrite(str(ref), np.zeros((8, 8, 3), np.uint8))
    d = QADirector(cfg={"qa": {"motion_score_range": [0.00005, 6.0], **(qa or {})}})
    d._face = _App(faces)
    frame = np.zeros((1088, 1920, 3), np.uint8)
    with patch.object(QADirector, "motion_score", return_value=0.5), \
         patch.object(QADirector, "clip_alignment", return_value=clip), \
         patch.object(QADirector, "_read_frames", return_value=[frame] * 5):
        return d.evaluate({"applied_model": "ltx25_s6_cq", "characters": [{"id": "mara", "tier": "main"}]},
                          "x.mp4", "a prompt", refs=[str(ref)])


def test_a_movie_shot_is_judged_on_the_cast_faces_large_enough_to_tell(tmp_path):
    r = _movie_qa(tmp_path, [_Face(300, 0.2)])
    assert r["face_fail"] and not r["passed"] and "qa.cast_face_min 0.25" in r["detail"]
    assert _movie_qa(tmp_path, [_Face(300, 0.4)])["passed"]
    # a 100 px face (under 12 % of 1088 px) is not judged, nor a shot without a face
    for faces in ([_Face(100, 0.05)], []):
        r = _movie_qa(tmp_path, faces)
        assert r["face"] is None and r["passed"]
    # the best face that is judged counts; both limits are settings
    assert _movie_qa(tmp_path, [_Face(100, 0.9), _Face(300, 0.3)])["face"] == pytest.approx(0.3)
    assert _movie_qa(tmp_path, [_Face(300, 0.4)], {"cast_face_min": 0.5})["face_fail"]
    assert _movie_qa(tmp_path, [_Face(100, 0.05)], {"cast_face_size": 0.05})["face_fail"]
    assert _movie_qa(tmp_path, [_Face(300, 0.2)], {"cast_face_min": "x"})["face_min"] == 0.25


def test_clip_is_recorded_but_never_fails_a_movie_shot(tmp_path):
    r = _movie_qa(tmp_path, [], clip=0.1)
    assert r["clip"] == 0.1 and not r["clip_fail"] and r["passed"]
    assert _qa(0.5, clip=0.1)["clip_fail"]                  # the Wan shots keep the check


def test_only_a_movie_shot_cuts_its_prompt_at_clips_77_tokens():
    """A Wan prompt over CLIP's 77 tokens scores nothing, as it always did; a
    movie shot's is cut there (its score is only recorded)."""
    torch = pytest.importorskip("torch")
    calls = []

    def proc(**kw):
        calls.append(kw)
        return {}

    class Out:
        image_embeds = text_embeds = torch.ones(1, 2)
    d = QADirector(cfg={"qa": {"motion_score_range": [0.00005, 6.0]}})
    d._clip = (lambda **kw: Out(), proc)
    with patch.object(QADirector, "_ensure_clip", return_value=True), \
         patch.object(QADirector, "_read_frames", return_value=[np.zeros((8, 8, 3), np.uint8)]):
        d.clip_alignment("x.mp4", "a prompt")
        d.clip_alignment("x.mp4", "a prompt", truncate=True)
    assert "truncation" not in calls[0] and (calls[1]["truncation"], calls[1]["max_length"]) == (True, 77)
    with patch.object(QADirector, "motion_score", return_value=0.5), \
         patch.object(QADirector, "clip_alignment", return_value=None) as clip:
        d.evaluate({"characters": []}, "x.mp4", "a prompt")
        d.evaluate({"applied_model": "ltx25_s6_cq", "characters": []}, "x.mp4", "a prompt")
    assert [c.kwargs.get("truncate") for c in clip.call_args_list] == [False, True]


def test_static_shot_inside_configured_range_passes():
    r = _qa(0.001)                     # under _FROZEN_MOTION, inside the range
    assert r["frozen"] is True and r["passed"] is True
    assert "frozen" in r["detail"]


def test_motion_outside_range_fails():
    assert _qa(0.00001)["passed"] is False
    assert _qa(9.0)["passed"] is False


def test_soften_effect_loras_halves_only_effects():
    chain = [
        {"name": "char_a.safetensors", "strength": 0.8, "category": "Character"},
        {"name": "fire.safetensors", "strength": 0.7, "category": "VFX"},
        {"name": "zoom.safetensors", "strength": 0.6, "category": "Camera"},
        {"name": "turbo.safetensors", "strength": 1.0, "category": "Acceleration"},
        {"name": "off.safetensors", "strength": 0.7, "category": "VFX",
         "enabled": False},
    ]
    soften_effect_loras(chain)
    s = {e["name"]: e["strength"] for e in chain}
    assert s == {"char_a.safetensors": 0.8, "fire.safetensors": 0.35,
                 "zoom.safetensors": 0.3, "turbo.safetensors": 1.0,
                 "off.safetensors": 0.7}
