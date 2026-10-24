"""LLM Story Architect: reply shapes, id normalization, partial scripts."""
import json
from unittest.mock import MagicMock

import storygen_llm
from llm import extract_json
from package_norm import normalize_ids, normalize_package
from storygen_llm import validate_scene_package

ENTRY = ["wide", "dolly", "kael", "focused", "walks across the roof", None,
         "standard"]


def test_prompt_envelope_validates():
    """The scene prompt's documented {"shots": {"shots": [...]}} reply."""
    reply = "Here you go:\n```json\n" + json.dumps(
        {"shots": {"shots": [{"scene": "s01", "location": "roof",
                              "time": "night", "shots": [ENTRY]}]}}) + "\n```"
    shots, errs = validate_scene_package(extract_json(reply))
    assert errs == [] and shots and shots[0]["scene"] == "s01"


def test_bare_inner_object_still_validates():
    shots, errs = validate_scene_package(
        {"shots": [{"scene": "s01", "shots": [ENTRY]}]})
    assert errs == [] and shots


def test_unknown_top_level_key_still_rejected():
    _, errs = validate_scene_package(
        {"shots": [{"scene": "s01", "shots": [ENTRY]}], "notes": "x"})
    assert any("unexpected top-level keys" in e for e in errs)


def test_normalize_ids_rewrites_unsafe_ids_everywhere():
    pkg = {
        "bible": {"characters": [{"id": "dr.kim"}, {"id": "kael"}],
                  "locations": [{"id": "loc:alley"}]},
        "beats": {"beats": [{"scene": "s/01", "location": "loc:alley"}]},
        "scenes": {"scenes": [{"id": "s/01", "location": "loc:alley"}]},
        "shots": {"shots": [{"shot_id": "s/01_001", "prev_shot": None,
                             "scene": "s/01", "location": "loc:alley",
                             "characters": [{"id": "dr.kim"}],
                             "expected_visible": ["dr.kim"],
                             "dialogue": [{"char": "dr.kim", "line": "hi"}]}]},
    }
    remap = normalize_ids(pkg)
    assert remap == {"dr.kim": "dr_kim", "loc:alley": "loc_alley", "s/01": "s_01"}
    assert pkg["bible"]["characters"][0]["id"] == "dr_kim"
    assert pkg["bible"]["characters"][1]["id"] == "kael"          # untouched
    shot = pkg["shots"]["shots"][0]
    assert shot["shot_id"] == "s_01_001" and shot["scene"] == "s_01"
    assert shot["characters"][0]["id"] == "dr_kim"
    assert shot["expected_visible"] == ["dr_kim"]
    assert shot["dialogue"][0]["char"] == "dr_kim"
    assert pkg["beats"]["beats"][0] == {"scene": "s_01", "location": "loc_alley"}


def test_normalize_ids_avoids_collisions_and_keeps_valid_packages():
    pkg = {"bible": {"characters": [{"id": "a.b"}, {"id": "a_b"}]}}
    assert normalize_ids(pkg) == {"a.b": "a_b_2"}
    clean = {"shots": {"shots": [{"shot_id": "s01_001", "scene": "s01"}]}}
    assert normalize_package(clean, None)["shots"]["shots"][0]["scene"] == "s01"


def test_failed_scene_is_reported_not_silently_skipped(monkeypatch):
    beats = [{"scene": "s01"}, {"scene": "s02"}]
    store = MagicMock()
    store.get.return_value = {"id": "p", "story": {"beats": {"beats": beats}}}
    scene_reply = json.dumps({"shots": {"shots": [
        {"scene": "sX", "shots": [ENTRY]}]}})
    client = MagicMock()
    client.config_ok.return_value = True
    client.complete.side_effect = (
        [json.dumps({"treatment": {"t": 1}})] + [scene_reply] * 20)
    monkeypatch.setattr("llm.LLMClient", lambda cfg=None: client)
    monkeypatch.setattr("bible.Bible", MagicMock())
    arch = MagicMock()
    arch.import_stage.return_value = {"ok": True}
    monkeypatch.setattr("storygen.StoryArchitect", lambda *a, **k: arch)
    monkeypatch.setattr(storygen_llm, "build_spine_prompt", lambda *a: "spine")
    monkeypatch.setattr(storygen_llm, "build_scene_prompt", lambda *a: "scene")
    monkeypatch.setattr(storygen_llm, "_system_prompt", lambda: "sys")

    def merge(store_, db_, bible_, pid, scene, shots):
        if scene == "s01":
            return {"ok": True, "added": 1}
        return {"ok": False, "report": {"errors": ["[s02_001] bad camera"]}}

    monkeypatch.setattr(storygen_llm, "merge_scene_shots", merge)
    job = storygen_llm.ArchitectJob(cfg={"architect": {"retries": 1}},
                                    db=MagicMock(), store=store)
    job._jobs["p"] = {"running": True, "stage": "", "done": 0, "total": 0,
                      "message": "", "log": []}
    job._run("p")
    st = job._jobs["p"]
    assert st["running"] is False
    assert st["stage"] == "partial" and st["failed_scenes"] == ["s02"]
    assert "s02" in st["message"]
