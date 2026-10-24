"""QA verdict semantics + the adjust_lora retry step."""
from unittest.mock import patch

from lora_video import soften_effect_loras
from qa_director import QADirector


def _qa(motion):
    d = QADirector(cfg={"qa": {"motion_score_range": [0.00005, 6.0]}})
    with patch.object(QADirector, "motion_score", return_value=motion), \
         patch.object(QADirector, "clip_alignment", return_value=None):
        return d.evaluate({"characters": []}, "x.mp4", "a prompt")


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
