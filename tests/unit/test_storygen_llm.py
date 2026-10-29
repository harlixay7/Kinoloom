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


def test_the_extra_object_carries_the_cast_the_end_frame_and_both_markers():
    from package_norm import expand_compact
    ext = {"with": ["vera", ["ozan", "tense", "draws back"], "kael"], "end": "both at the gate",
           "tier": "hero", "continuity": "match_action"}
    entry = ["close_up", "dolly", "kael", "focused", "turns to the gate", None, "standard", ext]
    shots, errs = validate_scene_package({"shots": [{"scene": "s01", "shots": [entry]}]})
    assert errs == []
    s = expand_compact({"shots": {"shots": shots}})["shots"]["shots"][0]
    assert [c["id"] for c in s["characters"]] == ["kael", "vera", "ozan"]      # the lead once
    assert s["characters"][2] == {"id": "ozan", "tier": "main", "emotion": "tense", "action": "draws back"}
    assert s["expected_visible"] == ["kael", "vera", "ozan"]
    assert s["end_state"] == {"last_frame": "both at the gate"}
    assert (s["tier"], s["continuity"]) == ("hero", "MATCH_ACTION")
    for bad in ({"with": "vera"}, {"with": [3]}, {"tier": "epic"}, {"continuity": "dissolve"},
                {"end": ["x"]}, {"level": 11}, {"mood": "x"}):
        _, errs = validate_scene_package({"shots": [{"scene": "s01", "shots": [entry[:7] + [bad]]}]})
        assert errs, bad


def test_a_story_import_keeps_the_power_pictures_of_the_cast():
    # the cast's powers are the Cast tab's (their stills and descriptors): the
    # story bible adds the arc and leaves every power as it is
    from types import SimpleNamespace
    from storygen import StoryArchitect
    stills = [{"file": "lvl3.png", "level": 3}]
    saved = {}
    powers = {"lightning": {"descriptor": "old text", "reference_images": stills},
              "old_text_only": {"descriptor": "from an earlier story"},
              "frost": {"descriptor": "cold", "reference_images": stills}}
    bible = MagicMock()
    bible.characters.return_value = {"kael": {"id": "kael", "weapons": {"blade": {}}, "powers": powers}}
    bible.save_character.side_effect = lambda c: saved.update({c["id"]: c})
    bible.crowd_types.return_value = {}
    bible.locations.return_value = {}
    StoryArchitect._write_bible(SimpleNamespace(bible=bible), {"characters": [  # type: ignore[arg-type]
        {"id": "kael", "arc_summary": "falls", "powers": {"lightning": {"descriptor": "new text"},
                                                          "fire": {"descriptor": "hot"}}}]})
    assert saved["kael"]["powers"] == powers and saved["kael"]["weapons"] == {"blade": {}}
    assert saved["kael"]["arc_summary"] == "falls"


def test_the_reference_budget_follows_the_projects_model_family():
    from linter import Linter
    store = MagicMock()
    for mid, budget in (("minimax_h3_768p", 6), ("ltx25_s6_cq", 5), ("", 5)):
        store.get.return_value = {"video_family": mid}
        fam = storygen_llm._project_family(store, "p")
        assert Linter(MagicMock(), family=fam).ref_budget == budget


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



def _job(story=None):
    store = MagicMock()
    store.get.return_value = {"id": "p", "story": story or {}}
    return storygen_llm.ArchitectJob(cfg={"architect": {"base_url": "http://127.0.0.1:11434/v1",
                                                       "model": "m", "retries": 1}},
                                     db=MagicMock(), store=store)


def test_scenes_only_needs_an_outline_and_takes_scene_ids():
    job = _job()
    assert "write the outline first" in job.start("p", outline=False, scenes="missing")["error"]
    assert "scenes must be" in job.start("p", scenes="some")["error"]
    assert "scenes must be" in job.start("p", scenes=["../x"])["error"]


def test_one_writer_at_a_time_per_project():
    job = _job({"beats": {"beats": [{"scene": "s01"}]}})
    assert job._claim("p", True)
    assert "already being written" in job.start("p", outline=False, scenes="missing")["error"]
    res = job.run("p", outline=False, scenes="missing", stop=lambda: True)    # the director waits, or stops
    assert res["stopped"] and not res["ok"]


def test_an_ai_writer_that_does_not_answer_stops_the_story_at_once(monkeypatch):
    import movie_prompts
    monkeypatch.setattr(movie_prompts, "_DOWN", {})
    client = MagicMock()
    client.complete.return_value = None
    client.last_unreachable = True
    client.last_error = "the AI endpoint did not answer (refused)"
    monkeypatch.setattr("llm.LLMClient", lambda cfg=None: client)
    monkeypatch.setattr("bible.Bible", MagicMock())
    monkeypatch.setattr(movie_prompts, "wait_gpu", lambda *a, **k: True)
    monkeypatch.setattr(movie_prompts, "_release_local", lambda c: None)
    job = _job()
    assert job._claim("p", True)
    res = job._run("p")
    assert res["unreachable"] and "11434/v1 did not answer" in res["message"]
    assert client.complete.call_count == 1                       # no parse-retry loop
    assert "http://127.0.0.1:11434/v1" in movie_prompts._DOWN and not job.status("p")["active"]["running"]
    res = job.run("p")                                           # remembered for a minute
    assert res["unreachable"] and client.complete.call_count == 1


def test_a_local_story_writer_that_is_not_running_is_not_waited_for(monkeypatch):
    import gpu_broker
    import llm
    import movie_prompts
    monkeypatch.setattr(movie_prompts, "_DOWN", {})
    client = MagicMock()
    monkeypatch.setattr("llm.LLMClient", lambda cfg=None: client)
    monkeypatch.setattr("bible.Bible", MagicMock())
    monkeypatch.setattr(gpu_broker, "busy", lambda: True)                 # a render holds the GPU
    monkeypatch.setattr(llm, "reachable", lambda base, probe=None: False)
    job = _job()
    assert job._claim("p", True)
    res = job._run("p")
    assert res["unreachable"] and "11434/v1 did not answer" in res["message"]
    assert client.complete.call_count == 0


def test_the_chat_prompt_is_the_outline_for_a_long_film():
    bible = MagicMock()
    bible.characters.return_value = {}
    bible.locations.return_value = {}
    text, kind = storygen_llm.chat_prompt({"duration_target": 3600, "idea": "x"}, bible)
    assert kind == "spine" and "MASTER PLAN" in text and "720 shots across 26 scenes" in text
    text, kind = storygen_llm.chat_prompt({"duration_target": 60, "idea": "x"}, bible)
    assert kind == "full" and "exactly 12" in text



def test_a_story_prompt_keeps_the_cast_as_it_is():
    bible = MagicMock()
    bible.characters.return_value = {"sara": {"id": "sara", "name": "Sara", "tier": "main",
                                              "body": "early 20s woman", "outfit": "silver gown"}}
    bible.locations.return_value = {"palace": {"id": "palace", "descriptor": "a palace"}}
    for kind in ("full", "spine"):
        text, _ = storygen_llm.chat_prompt({"duration_target": 60, "idea": "x"}, bible, kind)
        assert "early 20s woman; wearing silver gown" in text and "The cast is FIXED" in text
        assert "Never add a character or a location" in text
        bible_part = text.split('"bible": ')[-1].split('"beats"')[0]       # the reply format
        assert '"<cast id>"' in bible_part and '"body"' not in bible_part and '"locations"' not in bible_part
    # a project without a cast: the story writes one, looks included
    bible.characters.return_value = {}
    text, _ = storygen_llm.chat_prompt({"duration_target": 60, "idea": "x"}, bible, "full")
    assert "There is no cast yet" in text and '"body": "..."' in text


def test_the_director_does_not_write_again_an_outline_written_while_it_waited(monkeypatch):
    import threading
    import time
    real_sleep = time.sleep
    job = _job()
    assert job._claim("p", True)                                  # the Story page's run

    def page_finishes():
        real_sleep(0.3)
        job.store.get.return_value = {"id": "p", "story": {"treatment": {"logline": "x"},
                                                          "beats": {"beats": [{"scene": "s01"}]}}}
        with job._lock:
            job._jobs["p"]["running"] = False
    monkeypatch.setattr(storygen_llm.time, "sleep", lambda s: real_sleep(0.05))
    monkeypatch.setattr(job, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote it again")))
    threading.Thread(target=page_finishes).start()
    res = job.run("p", outline=True, scenes="none")
    assert res["ok"] and "meanwhile" in res["message"] and not job.status("p")["active"]["running"]



def test_writing_scenes_never_resets_the_projects_cast(monkeypatch):
    import movie_prompts
    from bible import Bible
    b = Bible("zz_keep_cast")
    b.save_character({"id": "Sara", "tier": "main", "weapons": {"IceKatana": {}}})
    b.save_location({"id": "Palace", "descriptor": "halls"})
    (b.dir / "universe.yaml").unlink(missing_ok=True)            # an older project without one
    monkeypatch.setattr(movie_prompts, "_DOWN", {})
    client = MagicMock()
    client.complete.return_value = None
    client.last_unreachable = True
    client.last_error = "refused"
    monkeypatch.setattr("llm.LLMClient", lambda cfg=None: client)
    monkeypatch.setattr(movie_prompts, "wait_gpu", lambda *a, **k: True)
    monkeypatch.setattr(movie_prompts, "_release_local", lambda c: None)
    store = MagicMock()
    store.get.return_value = {"id": "zz_keep_cast", "story": {"treatment": "t", "beats": {"beats": [{"scene": "s01"}]}}}
    job = storygen_llm.ArchitectJob(cfg={"architect": {"base_url": "http://127.0.0.1:9/v1", "model": "m"}},
                                    db=MagicMock(), store=store)
    job.db.all.return_value = []
    res = job.run("zz_keep_cast", outline=False, scenes="missing")
    assert res["unreachable"]
    assert Bible("zz_keep_cast").characters()["Sara"]["weapons"] == {"IceKatana": {}}
    assert "Palace" in Bible("zz_keep_cast").locations()



def test_writing_scenes_the_outline_does_not_have_says_so(monkeypatch):
    monkeypatch.setattr("llm.LLMClient", lambda cfg=None: MagicMock())
    monkeypatch.setattr("bible.Bible", MagicMock())
    job = _job({"treatment": "t", "beats": {"beats": [{"scene": "s01"}]}})
    assert job._claim("p", False)
    res = job._run("p", outline=False, scenes=["s09"])
    assert not res["ok"] and res["message"] == "no such scene in the outline"
