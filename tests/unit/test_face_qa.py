import pytest
from unittest.mock import MagicMock, patch
from face_qa import FaceQA, DEFAULT_FACE_THRESHOLDS


def test_default_face_thresholds():
    """Verify default face similarity thresholds per character tier."""
    assert DEFAULT_FACE_THRESHOLDS["main"] == 0.75
    assert DEFAULT_FACE_THRESHOLDS["supporting"] == 0.65
    assert DEFAULT_FACE_THRESHOLDS["recurring"] == 0.55


def test_face_qa_evaluate_passing():
    """Shot with face similarity above tier threshold and valid motion passes."""
    qa = FaceQA(cfg={
        "qa": {
            "motion_score_range": [0.1, 5.0],
            "clip_score_min": 0.25,
        }
    })

    with patch.object(qa, "face_similarity", return_value=0.82), \
         patch.object(qa, "clip_alignment", return_value=0.35), \
         patch.object(qa, "motion_score", return_value=1.5):

        shot = {
            "prompt": "Hero walks into the frame",
            "characters": [{"id": "c_hero", "tier": "main"}],
        }
        res = qa.evaluate(shot, "dummy/video.mp4")

        assert res["passed"] is True
        assert res["clip"] == 0.35
        assert res["face"] is None  # no failing face score recorded
        assert res["motion"] == 1.5


def test_face_qa_evaluate_failing_face_similarity():
    """Shot with face similarity below tier threshold fails evaluation."""
    qa = FaceQA()

    # 0.70 is below main tier threshold (0.75)
    with patch.object(qa, "face_similarity", return_value=0.70), \
         patch.object(qa, "clip_alignment", return_value=0.35), \
         patch.object(qa, "motion_score", return_value=1.5):

        shot = {
            "prompt": "Hero close up",
            "characters": [{"id": "c_hero", "tier": "main"}],
        }
        res = qa.evaluate(shot, "dummy/video.mp4")

        assert res["passed"] is False
        assert res["face"] == 0.70


def test_face_qa_crowd_tier_exempt():
    """Crowd tier characters are exempt from face similarity checks."""
    qa = FaceQA()

    with patch.object(qa, "face_similarity") as mock_sim, \
         patch.object(qa, "clip_alignment", return_value=0.30), \
         patch.object(qa, "motion_score", return_value=1.0):

        shot = {
            "prompt": "Crowd gathering in marketplace",
            "characters": [{"id": "pedestrian_1", "tier": "crowd"}],
        }
        res = qa.evaluate(shot, "dummy/video.mp4")

        mock_sim.assert_not_called()
        assert res["passed"] is True


def test_face_qa_motion_fallback_on_missing_file():
    """motion_score returns default fallback when video cannot be decoded."""
    qa = FaceQA()
    # Non-existent video returns default 0.5 or handles gracefully
    score = qa.motion_score("non_existent_file_xyz123.mp4")
    assert isinstance(score, float)
    assert score >= 0.0


def test_face_qa_delegation_to_qa_director():
    """clip_alignment and face_similarity delegate to qa_director.QADirector."""
    qa = FaceQA()
    with patch("qa_director.QADirector") as mock_qd_cls:
        mock_qd_instance = MagicMock()
        mock_qd_instance.clip_alignment.return_value = 0.42
        mock_qd_cls.return_value = mock_qd_instance

        score = qa.clip_alignment("test.mp4", "A cinematic sunrise")
        assert score == 0.42
        mock_qd_instance.clip_alignment.assert_called_once_with("test.mp4", "A cinematic sunrise")
