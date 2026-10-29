import pytest
from unittest.mock import MagicMock
from linter import (
    CAMERA_MOVES,
    SHOT_SIZES,
    POSITIONS,
    FACINGS,
    TIERS,
    CHAR_TIERS,
    HERO_SHOT_SIZES,
    LintReport,
    Linter,
)


def test_linter_enum_sets():
    """Verify all filmmaking enum sets have exact canonical member counts."""
    assert len(CAMERA_MOVES) == 9
    assert CAMERA_MOVES == {
        "static", "slow_push_in", "dolly", "pan", "crane", "handheld",
        "orbit", "pull_back", "tracking",
    }

    assert len(SHOT_SIZES) == 7
    assert SHOT_SIZES == {
        "extreme_close_up", "close_up", "medium_close_up", "medium",
        "medium_wide", "wide", "extreme_wide",
    }

    assert len(POSITIONS) == 9
    assert POSITIONS == {
        "foreground_left", "foreground_center", "foreground_right",
        "midground_left", "midground_center", "midground_right",
        "background_left", "background_center", "background_right",
    }

    assert len(FACINGS) == 4
    assert FACINGS == {
        "camera_left", "camera_right", "camera", "away_from_camera",
    }

    assert len(TIERS) == 3
    assert TIERS == {"hero", "standard", "volume"}

    assert len(CHAR_TIERS) == 4
    assert CHAR_TIERS == {"main", "supporting", "recurring", "crowd"}


def _create_mock_bible():
    bible = MagicMock()
    bible.locations.return_value = {
        "bridge": {"lighting": "cold neon"},
        "reactor_core": {"lighting": "amber warning pulses"},
    }
    bible.characters.return_value = {
        "c_hero": {
            "id": "c_hero",
            "tier": "main",
            "powers": {"teleport_flash": {"descriptor": "blue light flash"}},
            "emotion_palette": ["focused", "determined", "calm"],
            "location_binding": ["bridge", "reactor_core"],
        },
        "c_sidekick": {
            "id": "c_sidekick",
            "tier": "supporting",
            "emotion_palette": ["curious", "nervous"],
            "location_binding": [],
        },
    }
    bible.crowd_types.return_value = {}
    return bible


def test_lint_shot_valid():
    """Valid shot specification passes linter without errors."""
    bible = _create_mock_bible()
    linter = Linter(bible=bible)

    shot = {
        "shot_id": "s01_001",
        "camera": "slow_push_in",
        "shot_size": "close_up",
        "tier": "hero",
        "duration_s": 5,
        "location": "bridge",
        "lighting": "cold neon",
        "continuity": "CUT",
        "characters": [
            {
                "id": "c_hero",
                "tier": "main",
                "emotion": "focused",
                "position": "foreground_center",
                "facing": "camera",
            }
        ],
    }

    report = linter.lint_shot(shot)
    assert report.ok is True
    assert len(report.errors) == 0


def test_lint_shot_invalid_enums_and_unknown_entities():
    """Invalid shot specification generates actionable error messages."""
    bible = _create_mock_bible()
    linter = Linter(bible=bible)

    bad_shot = {
        "shot_id": "s01_002",
        "camera": "360_spin_zoom",
        "shot_size": "microscopic",
        "tier": "legendary",
        "duration_s": 10,
        "location": "mars_crater",
        "continuity": "WARP",
        "characters": [
            {
                "id": "unknown_ghost",
                "tier": "main",
                "emotion": "furious",
                "position": "sky_high",
                "facing": "upside_down",
            }
        ],
    }

    report = linter.lint_shot(bad_shot)
    assert report.ok is False
    assert len(report.errors) >= 5

    error_blob = " ".join(report.errors)
    assert "camera '360_spin_zoom' not in enum" in error_blob
    assert "shot_size 'microscopic' not in enum" in error_blob
    assert "tier 'legendary' not in {hero,standard,volume}" in error_blob
    assert "location 'mars_crater' not in Bible" in error_blob
    assert "continuity 'WARP' not in {CUT,CONTINUE,MATCH_ACTION}" in error_blob
    assert "character 'unknown_ghost' not registered in Bible" in error_blob


def test_lint_hero_tier_gating():
    """Hero tier requires close-up range shot_size or a declared power."""
    bible = _create_mock_bible()
    linter = Linter(bible=bible)

    wide_hero_shot = {
        "shot_id": "s01_003",
        "camera": "static",
        "shot_size": "extreme_wide",
        "tier": "hero",
        "duration_s": 5,
        "location": "bridge",
        "lighting": "cold neon",
        "characters": [
            {
                "id": "c_hero",
                "tier": "main",
                "emotion": "calm",
                "position": "midground_center",
                "facing": "camera",
            }
        ],
    }

    report = linter.lint_shot(wide_hero_shot)
    assert not report.ok
    assert any("hero tier requires shot_size in close-up range or a power" in e for e in report.errors)

    # Adding a power satisfies hero tier even on wide shots
    wide_hero_shot["power"] = "teleport_flash"
    report2 = linter.lint_shot(wide_hero_shot)
    assert report2.ok


def _chain(*conts, sizes=None):
    shots, prev = [], ""
    for i, cont in enumerate(conts, 1):
        sid = f"s01_{i:03d}"
        shots.append({"shot_id": sid, "scene": "s01", "location": "bridge", "continuity": cont,
                      "prev_shot": prev, "shot_size": (sizes or {}).get(i, "medium"), "camera": "static"})
        prev = sid
    return shots


@pytest.mark.parametrize("family, take_len, conts, warned", [
    ("", 0, ("CUT",) + ("CONTINUE",) * 5, []),                    # one LTX take of 6, the measured default
    ("", 0, ("CUT",) + ("CONTINUE",) * 6, ["s01_007"]),           # the 7th goes on in a new take
    ("h3", 0, ("CUT",) + ("CONTINUE",) * 3, []),                  # one H3 take of 4
    ("h3", 0, ("CUT",) + ("CONTINUE",) * 4, ["s01_005"]),
    ("", 2, ("CUT",) + ("CONTINUE",) * 4, ["s01_003", "s01_005"]),  # the project's take length
    ("", 1, ("CUT", "CONTINUE", "CONTINUE"), ["s01_002", "s01_003"]),
    ("", 3, ("CUT", "CONTINUE", "CONTINUE", "MATCH_ACTION", "CONTINUE", "CONTINUE"), []),  # a cut opens fresh
])
def test_a_take_longer_than_the_take_length_says_where_it_goes_on(family, take_len, conts, warned):
    rep = LintReport()
    Linter(bible=_create_mock_bible(), family=family, take_len=take_len)._lint_continuity_order(
        _chain(*conts, sizes={i: "close_up" for i, c in enumerate(conts, 1) if c == "MATCH_ACTION"}), rep)
    hits = [w for w in rep.warnings if "the take length" in w]
    assert [w.split("]")[0].strip("[") for w in hits] == warned


def test_a_face_that_matters_is_never_written_wide():
    linter = Linter(bible=_create_mock_bible())
    shot = {"shot_id": "s01_001", "shot_size": "wide", "tier": "standard", "characters": [{"id": "x"}],
            "dialogue": [{"char": "x", "line": "hi"}]}
    assert any("dialogue at wide" in w for w in linter.lint_shot(shot).warnings)
    shot.update(dialogue=[], tier="hero", power="p")
    assert any("a hero moment at wide" in w for w in linter.lint_shot(shot).warnings)
    shot.update(shot_size="medium")
    assert not any("identity and detail" in w for w in linter.lint_shot(shot).warnings)
    # an establishing shot of the place is what wide is for
    shot.update(shot_size="extreme_wide", tier="volume", power=None)
    assert not any("identity and detail" in w for w in linter.lint_shot(shot).warnings)


def test_a_match_cut_needs_a_new_angle():
    rep = LintReport()
    Linter(bible=_create_mock_bible())._lint_continuity_order(_chain("CUT", "MATCH_ACTION"), rep)
    assert any("needs a new angle" in w for w in rep.warnings)
    rep = LintReport()
    Linter(bible=_create_mock_bible())._lint_continuity_order(
        _chain("CUT", "MATCH_ACTION", sizes={2: "wide"}), rep)
    assert not rep.warnings
