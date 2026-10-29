"""A story written in takes: the edit follows from the takes, never from
markers, and the story prompt states the take length."""
import pytest

from package_norm import _force_cut_on_scene_change, expand_compact
from storygen_llm import shot_rules, validate_scene_package


def _entry(lead="sara", action="walks", tier="standard", extra=None):
    e = ["medium", "tracking", lead, "calm", action, None, tier]
    return e + [extra] if extra is not None else e


def _expand(*blocks):
    return expand_compact({"shots": {"shots": list(blocks)}})["shots"]["shots"]


def _scene(*takes, scene="s01", location="palace"):
    return {"scene": scene, "location": location, "time": "day", "takes": list(takes)}


def test_the_edit_follows_from_the_takes():
    shots = _expand(_scene(
        {"place": "the courtyard", "shots": [_entry(), _entry(), _entry()]},
        {"place": "the courtyard", "shots": [_entry(), _entry()]},          # same place: a match cut
        {"place": "the corridor", "shots": [_entry()]},                     # another place: a cut
        {"location": "harbour", "shots": [_entry(), _entry()]}))            # another location: a cut
    assert [s["continuity"] for s in shots] == [
        "CUT", "CONTINUE", "CONTINUE", "MATCH_ACTION", "CONTINUE", "CUT", "CUT", "CONTINUE"]
    assert [s["shot_id"] for s in shots] == [f"s01_{i:03d}" for i in range(1, 9)]
    assert [s["prev_shot"] for s in shots][:3] == [None, "s01_001", "s01_002"]
    assert [s.get("location_tag") for s in shots][:6] == ["the courtyard"] * 5 + ["the corridor"]
    assert "location_tag" not in shots[6] and shots[6]["location"] == "harbour"
    assert shots[0]["location"] == "palace"


def test_a_place_differs_only_in_spelling_is_the_same_place():
    shots = _expand(_scene({"place": "The_Courtyard", "shots": [_entry()]},
                           {"place": "the courtyard", "shots": [_entry()]}))
    assert shots[1]["continuity"] == "MATCH_ACTION"


def test_markers_and_places_on_single_shots_inside_a_take_are_left_out_with_a_note():
    shots = _expand(_scene({"place": "the courtyard", "shots": [
        _entry(), ["medium", "static", "sara", "calm", "stops", None, "cut"],
        _entry(extra={"continuity": "match_action", "place": "the chapel"})]}))
    assert [s["continuity"] for s in shots] == ["CUT", "CONTINUE", "CONTINUE"]
    assert shots[2]["location_tag"] == "the courtyard"
    assert "continuity CUT inside a take" in shots[1]["_norm_notes"][0]
    assert any("'the chapel'" in n for n in shots[2]["_norm_notes"])
    # the tier still reads from the 7th element and the extra object
    assert _expand(_scene({"shots": [_entry(tier="hero")]}))[0]["tier"] == "hero"


def test_dialogue_belongs_to_the_shot_before_it_and_empty_takes_change_nothing():
    shots = _expand(_scene(
        {"place": "a", "shots": [_entry(), ["D", "sara", "calm", "Hello."]]},
        {"place": "b", "shots": []},                                         # empty: skipped
        {"place": "b", "shots": [["D", "sara", "calm", "Still me."]]},       # only a line: the shot before
        {"place": "a", "shots": [_entry()]}))
    assert len(shots) == 2
    assert [d["line"] for d in shots[0]["dialogue"]] == ["Hello.", "Still me."]
    assert shots[1]["continuity"] == "MATCH_ACTION"      # the take before it was in "a" too


def test_takes_and_older_flat_blocks_import_side_by_side():
    shots = _expand(
        _scene({"shots": [_entry(), _entry()]}),
        {"scene": "s02", "location": "palace", "time": "day",
         "shots": [_entry(), ["medium", "static", "sara", "calm", "stops", None, "continue"]]})
    assert [(s["shot_id"], s["continuity"]) for s in shots] == [
        ("s01_001", "CUT"), ("s01_002", "CONTINUE"), ("s02_001", "CUT"), ("s02_002", "CONTINUE")]


def test_a_continue_into_another_view_becomes_a_match_cut():
    shots = [{"shot_id": "a", "scene": "s01", "location": "palace", "location_tag": "corridor",
              "continuity": "CUT"},
             {"shot_id": "b", "scene": "s01", "location": "palace", "location_tag": "courtyard",
              "continuity": "CONTINUE", "prev_shot": "a"},
             {"shot_id": "c", "scene": "s01", "location": "palace", "location_tag": "courtyard",
              "continuity": "CONTINUE", "prev_shot": "b"},
             {"shot_id": "d", "scene": "s01", "location": "palace", "continuity": "CONTINUE",
              "prev_shot": "c"}]                     # no view: the scene's default, left as it is
    _force_cut_on_scene_change(shots)
    assert [s["continuity"] for s in shots] == ["CUT", "MATCH_ACTION", "CONTINUE", "CONTINUE"]
    assert "made a MATCH_ACTION" in shots[1]["_norm_notes"][0]


@pytest.mark.parametrize("block, error", [
    (_scene({"place": "a", "shots": [_entry()]}), None),
    (_scene(), "takes must be a non-empty list"),
    (_scene({"place": "a", "shots": []}), "shots must be a non-empty list"),
    (_scene({"place": 3, "shots": [_entry()]}), "place must be a name or null"),
    (_scene({"place": "a", "camera": "x", "shots": [_entry()]}), "unknown keys ['camera']"),
    (_scene({"shots": [["D", "sara", "calm", "Hi."]]}), "only dialogue"),
    (_scene({"shots": [["medium", "static"]]}), "must have 5-8 elements"),
    (_scene("nope"), "is not an object"),
])
def test_a_reply_in_takes_is_checked_like_any_reply(block, error):
    shots, errs = validate_scene_package({"shots": {"shots": [block]}})
    if error is None:
        assert errs == [] and shots
    else:
        assert shots is None and any(error in e for e in errs), errs


def test_the_rules_state_the_take_length_and_never_ask_for_cuts():
    rules = shot_rules(6)
    assert "A take holds 1 to 6 shots" in rules
    assert "CUT is the default" not in rules and "medium or closer" in rules
    assert "THE PLACE PICTURE IS THE SET" in rules
    assert "1 to 1 shots" in shot_rules(0)
    # what the video model does well and badly: the same words as the cast sheet
    from prompt_check import FACES, SHOWS_BADLY, SHOWS_WELL
    assert FACES in rules and SHOWS_WELL in rules and SHOWS_BADLY in rules
