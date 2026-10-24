import pytest
from unittest.mock import MagicMock
from storygen_llm import cast_lines, loc_lines, power_lines


def test_cast_lines_populated():
    """cast_lines formats character identity, tier, powers, weapons, and loadouts."""
    bible = MagicMock()
    bible.characters.return_value = {
        "c_dr_valen": {
            "id": "c_dr_valen",
            "name": "Dr. Valen",
            "tier": "main",
            "powers": {"chronoshift": {"descriptor": "temporal distortion"}},
            "lora": {"file": "valen_v2.safetensors"},
            "emotion_palette": ["stoic", "alarmed", "determined"],
            "weapons": {
                "quantum_disruptor": {"descriptor": "silver handheld pulse emitter"}
            },
            "loadouts": {
                "field_suit": {
                    "outfit": "heavy_containment_armor",
                    "weapon": "quantum_disruptor",
                }
            },
            "scene_tags": {"s01": "breach_alert"},
        }
    }

    out = cast_lines(bible)
    assert "- c_dr_valen (Dr. Valen, tier main)" in out
    assert "powers: chronoshift" in out
    assert "| LoRA: valen_v2.safetensors" in out
    assert "emotions: stoic, alarmed, determined" in out
    assert "weapons: quantum_disruptor (silver handheld pulse emitter)" in out
    assert "loadouts: field_suit [outfit: heavy_containment_armor, weapon: quantum_disruptor]" in out
    assert "default per scene: s01=breach_alert" in out


def test_cast_lines_empty_fallback():
    """Empty cast returns the canonical anti-hallucination fallback instruction."""
    bible = MagicMock()
    bible.characters.return_value = {}
    assert cast_lines(bible) == "- none yet — create your own in the bible stage"


def test_loc_lines_populated():
    """loc_lines formats location descriptor and referenced place tags."""
    bible = MagicMock()
    bible.locations.return_value = {
        "sub_hangar": {
            "id": "sub_hangar",
            "descriptor": "Flooded subterranean submarine docking bay",
            "reference_images": [
                {"tag": "gantry_crane"},
                {"tag": "water_dock"},
            ],
            "scene_tags": {"s02": "night_floods"},
        }
    }

    out = loc_lines(bible)
    assert "- sub_hangar: Flooded subterranean submarine docking bay" in out
    assert "places (use as place): gantry_crane; water_dock" in out
    assert "default per scene: s02=night_floods" in out


def test_loc_lines_empty_fallback():
    """Empty locations returns the canonical anti-hallucination fallback instruction."""
    bible = MagicMock()
    bible.locations.return_value = {}
    assert loc_lines(bible) == "- none yet — create your own in the bible stage"


def test_power_lines_formatting():
    """power_lines synthesizes descriptive descriptors when missing."""
    bible = MagicMock()
    bible.characters.return_value = {
        "c_mage": {
            "id": "c_mage",
            "powers": {
                "pyro_blast": {
                    "descriptor": "pyro_blast",  # identical to id -> triggers vfx synthesis
                    "color": "crimson",
                    "material": "plasma",
                    "motion": "swirling",
                    "glow": "bright",
                }
            },
        }
    }
    out = power_lines(bible)
    assert "- pyro_blast (on c_mage): crimson plasma energy, swirling, bright" in out
