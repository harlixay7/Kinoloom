"""Story imports from any AI (chat copy/paste or a connected one): a reply that
spells an id another way keeps the cast as it is, and one scene's import is
judged on its own shots."""
import copy
from types import SimpleNamespace

import storygen_llm
from package_norm import normalize_package
from storygen import StoryArchitect


def _sara():
    return {"id": "Sara", "name": "Sara", "tier": "main", "body": "old body",
            "emotion_palette": ["stoic", "calm", "cold"],
            "powers": {"IcePower5": {"descriptor": "frost",
                                     "reference_images": [{"file": "p_000.png", "level": 3}]}},
            "weapons": {"IceKatana": {"type": "katana", "descriptor": "ice blade"}},
            "loadouts": {"sorceress gown + IceKatana": {"outfit": "sorceress gown", "weapon": "IceKatana"}},
            "reference_images": [{"type": "face", "name": "Sara", "file": "stills/v_001.png", "role": "primary"}],
            "enabled": True}


def _palace():
    return {"id": "Palace", "descriptor": "old text", "lighting": "cold",
            "reference_images": [{"type": "environment", "file": "stills/e_000.png", "tag": "throne room"}]}


class _Bible:
    def __init__(self):
        self.chars = {"Sara": _sara()}
        self.locs = {"Palace": _palace()}
        self.crowd = {"guards": {"tier": "crowd", "descriptor": "palace guards"}}
        self.saved: list = []

    def characters(self):
        return copy.deepcopy(self.chars)

    def locations(self):
        return copy.deepcopy(self.locs)

    def crowd_types(self):
        return copy.deepcopy(self.crowd)

    def scenes(self):
        return {}

    def save_character(self, c):
        self.saved.append(("character", c["id"]))
        self.chars = {k: v for k, v in self.chars.items() if k.lower() != c["id"].lower()}
        self.chars[c["id"]] = c

    def save_location(self, loc):
        self.saved.append(("location", loc["id"]))
        self.locs = {k: v for k, v in self.locs.items() if k.lower() != loc["id"].lower()}
        self.locs[loc["id"]] = loc

    def save_crowd_types(self, ct):
        self.crowd = ct

    def universe(self):
        return {}

    def save_universe(self, u):
        pass


def test_a_reply_that_spells_a_character_another_way_keeps_its_record():
    # the cast's own text wins (the story bible cannot change a look): a
    # reply about "sara" writes nothing over "Sara", under either spelling
    b = _Bible()
    out = StoryArchitect._write_bible(SimpleNamespace(bible=b), {  # type: ignore[arg-type]
        "characters": [{"id": "sara", "body": "new body", "powers": {"icepower5": {"descriptor": "new frost"}}}],
        "locations": [{"id": "palace", "descriptor": "new text"}]})
    assert b.saved == [] and b.chars == {"Sara": _sara()} and b.locs == {"Palace": _palace()}
    assert out["characters"][0]["id"] == "Sara" and out["characters"][0]["body"] == "old body"
    assert out["locations"] == [{"id": "Palace", "descriptor": "old text", "lighting": "cold"}]


def test_the_story_bible_adds_the_story_and_never_changes_the_cast():
    b = _Bible()
    b.chars["Kael"] = {"id": "Kael", "name": "Kael", "tier": "supporting", "body": "", "voice": {"engine": "moss-tts"}}
    out = StoryArchitect._write_bible(SimpleNamespace(bible=b), {  # type: ignore[arg-type]
        "characters": [
            {"id": "Sara", "name": "Queen Sara", "tier": "supporting", "body": "weathered exile in her thirties",
             "outfit": "charred mantle", "emotion_palette": ["rage"], "arc_summary": "takes the throne back",
             "voice": {"style_tag": "low"}, "powers": {"fire": {"descriptor": "hot"}},
             "weapons": {"axe": {}}},
            {"id": "Kael", "body": "a tall man", "emotion_palette": ["calm", "wary"],
             "voice": {"style_tag": "gravel"}},
            {"id": "Morgana", "body": "a witch"}],
        "locations": [{"id": "Palace", "lighting": "warm"}, {"id": "cave", "descriptor": "a cave"}],
        "crowd_types": {"guards": {"tier": "crowd", "descriptor": "new guards"},
                        "rebels": {"tier": "crowd", "descriptor": "rebels in rags"}}})
    sara, kael = b.chars["Sara"], b.chars["Kael"]
    # the story, and a voice where she had none; never her name, look, powers or weapons
    assert sara == {**_sara(), "arc_summary": "takes the throne back",
                    "voice": {"engine": "moss-tts", "params": {}, "style_tag": "low"}}
    assert kael["body"] == "" and kael["emotion_palette"] == ["calm", "wary"]  # a palette where none was
    assert kael["voice"] == {"engine": "moss-tts", "params": {}, "style_tag": "gravel"}
    assert "Morgana" not in b.chars and "cave" not in b.locs and b.locs == {"Palace": _palace()}
    assert b.crowd == {"guards": {"tier": "crowd", "descriptor": "palace guards"},
                       "rebels": {"tier": "crowd", "descriptor": "rebels in rags"}}
    # the Story page shows the cast as it is
    assert [c["id"] for c in out["characters"]] == ["Sara", "Kael"]
    assert out["characters"][0]["body"] == "old body" and out["characters"][0]["arc_summary"] == "takes the throne back"
    assert out["characters"][0]["powers"] == {"IcePower5": {"descriptor": "frost"}}
    assert [x["id"] for x in out["locations"]] == ["Palace"] and set(out["crowd_types"]) == {"guards", "rebels"}


def test_a_project_without_a_cast_takes_the_story_bibles_cast():
    b = _Bible()
    b.chars, b.locs = {}, {}
    StoryArchitect._write_bible(SimpleNamespace(bible=b), {  # type: ignore[arg-type]
        "characters": [{"id": "mira", "name": "Mira", "tier": "main", "body": "a young archer",
                        "emotion_palette": ["calm"]}],
        "locations": [{"id": "forest", "descriptor": "a pine forest", "lighting": "dawn"}]})
    assert b.chars["mira"]["body"] == "a young archer" and b.chars["mira"]["enabled"] is True
    assert b.locs["forest"] == {"id": "forest", "descriptor": "a pine forest", "lighting": "dawn", "enabled": True}


def test_a_character_the_cast_does_not_have_fails_the_shot_check():
    from linter import Linter
    b = _Bible()
    StoryArchitect._write_bible(SimpleNamespace(bible=b), {  # type: ignore[arg-type]
        "characters": [{"id": "Morgana", "tier": "main", "emotion_palette": ["calm"]}]})
    shots = normalize_package({"shots": {"shots": _scene("s01", cid="Morgana")}}, b)["shots"]["shots"]
    rep = Linter(b).lint_package(shots)
    assert not rep.ok and any("Morgana" in e and "not registered" in e for e in rep.errors)


def test_every_reference_in_a_reply_takes_the_bibles_spelling():
    reply = {
        "bible": {"characters": [{"id": "SARA", "powers": {"icepower5": {"descriptor": "x"}}}],
                  "locations": [{"id": "palace"}]},
        "beats": {"beats": [{"scene": "s01", "location": "PALACE"}]},
        "shots": {"shots": [{"scene": "s01", "location": "palace", "time": "dusk", "shots": [
            ["Close_Up", "Slow_Push_In", "sara", "Calm", "draws the blade", "ICEPOWER5", "Hero",
             {"with": ["Guards"], "weapon": "icekatana", "loadout": "Sorceress Gown + IceKatana",
              "place": "Throne Room", "level": 3}],
            ["D", "SARA", "Cold", "Kneel."]]}]},
    }
    out = normalize_package(reply, _Bible())
    assert out["bible"]["characters"][0]["id"] == "Sara"
    assert list(out["bible"]["characters"][0]["powers"]) == ["IcePower5"]
    assert out["bible"]["locations"][0]["id"] == "Palace"
    assert out["beats"]["beats"][0]["location"] == "Palace"
    s = out["shots"]["shots"][0]
    assert (s["location"], s["location_tag"], s["power"]) == ("Palace", "throne room", "IcePower5")
    assert (s["shot_size"], s["camera"], s["tier"]) == ("close_up", "slow_push_in", "hero")
    lead = s["characters"][0]
    assert (lead["id"], lead["emotion"], lead["weapon"], lead["loadout"]) == \
        ("Sara", "calm", "IceKatana", "sorceress gown + IceKatana")
    assert s["characters"][1]["id"] == "guards" and s["expected_visible"] == ["Sara", "guards"]
    assert s["dialogue"][0]["char"] == "Sara" and s["dialogue"][0]["emotion"] == "cold"


def test_ids_the_bible_does_not_know_are_left_for_the_check_to_name():
    out = normalize_package({"shots": {"shots": [{"scene": "s01", "location": "cave", "shots": [
        ["wide", "static", "morgana", "calm", "waits", None, "standard"]]}]}}, _Bible())
    s = out["shots"]["shots"][0]
    assert s["location"] == "cave" and s["characters"][0]["id"] == "morgana"


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def all(self, project=None, limit=None):
        return [dict(r) for r in self.rows]

    def delete_shot(self, sid, pid):
        self.rows = [r for r in self.rows if r["shot_id"] != sid]

    def upsert_shots(self, shots):
        for s in shots:
            self.rows.append({"shot_id": s["shot_id"], "scene": s["scene"], "json": s})


def _scene(scene, cid="Sara", n=2):
    return [{"scene": scene, "location": "Palace", "time": "dusk", "shots": [
        ["wide", "static", cid, "calm", f"step {k}", None, "volume" if k == 1 else "standard"]
        for k in range(1, n + 1)]}]


def _store():
    st = SimpleNamespace(stage=None)
    st.get = lambda pid: {"id": pid, "video_family": "ltx25_s6_cq", "story": {"beats": {"beats": [
        {"scene": "s01", "location": "Palace", "time": "dusk"},
        {"scene": "s02", "location": "Palace", "time": "dusk"}]}}}
    st.set_story_stage = lambda pid, stage, content: setattr(st, "stage", (stage, content))
    return st


def test_a_scene_imports_although_an_earlier_scene_has_problems():
    b = _Bible()
    old = normalize_package({"shots": {"shots": _scene("s01", cid="Morgana")}}, b)["shots"]["shots"]
    db = _DB([{"shot_id": s["shot_id"], "scene": "s01", "json": s} for s in old])
    res = storygen_llm.merge_scene_shots(_store(), db, b, "p", "s02", _scene("s02"))
    assert res["ok"] and res["added"] == 2
    assert any("[s01_001]" in e and "Morgana" in e for e in res["elsewhere"])
    assert sorted(r["shot_id"] for r in db.rows) == ["s01_001", "s01_002", "s02_001", "s02_002"]


def test_a_scene_with_problems_of_its_own_is_refused_and_says_which():
    b = _Bible()
    db = _DB([])
    res = storygen_llm.merge_scene_shots(_store(), db, b, "p", "s02", _scene("s02", cid="Morgana"))
    assert not res["ok"] and db.rows == []
    assert res["errors"] and all(e.startswith("[s02") for e in res["errors"])
    assert "Morgana" in res["error"] and res["report"]["errors"] == res["errors"]


def test_an_unsafe_spelling_of_a_known_id_takes_the_bibles_spelling():
    b = _Bible()
    b.chars["Dr_Kim"] = {"id": "Dr_Kim", "tier": "supporting", "emotion_palette": ["calm"]}
    out = normalize_package({"shots": {"shots": [{"scene": "s01", "location": "Palace", "shots": [
        ["wide", "static", "dr.kim", "calm", "waits", None, "standard"]]}]}}, b)
    assert out["shots"]["shots"][0]["characters"][0]["id"] == "Dr_Kim"


def test_a_character_named_twice_in_other_capitals_is_on_screen_once():
    out = normalize_package({"shots": {"shots": [{"scene": "s01", "location": "Palace", "shots": [
        ["wide", "static", "Sara", "calm", "waits", None, "standard", {"with": ["sara", "Guards"]}]]}]}},
        _Bible())
    s = out["shots"]["shots"][0]
    assert [c["id"] for c in s["characters"]] == ["Sara", "guards"]
    assert s["expected_visible"] == ["Sara", "guards"]


def test_scene_ids_and_the_packages_own_names_keep_one_spelling():
    b = _Bible()
    b.scenes = lambda: {"s01": {"id": "s01"}}
    out = normalize_package({
        "bible": {"characters": [{"id": "Mira", "location_binding": ["palace"]}]},
        "beats": {"beats": [{"scene": "S01", "location": "palace"}, {"scene": "s02", "location": "palace"}]},
        "shots": {"shots": [
            {"shot_id": "S01_001", "prev_shot": None, "scene": "S01", "location": "palace",
             "characters": [{"id": "mira"}], "state_mutations": [{"target": "sara", "key": "wound", "value": "x"}]},
            {"shot_id": "S01_002", "prev_shot": "S01_001", "scene": "S01", "location": "palace",
             "characters": [{"id": "MIRA"}]}]}}, b)
    assert [x["scene"] for x in out["beats"]["beats"]] == ["s01", "s02"]
    assert out["bible"]["characters"][0]["location_binding"] == ["Palace"]
    s1, s2 = out["shots"]["shots"]
    assert (s1["shot_id"], s1["scene"], s2["prev_shot"]) == ("s01_001", "s01", "s01_001")
    assert s1["characters"][0]["id"] == "Mira" and s2["characters"][0]["id"] == "Mira"
    assert s1["state_mutations"][0]["target"] == "Sara"


def test_a_broken_chain_in_the_new_scene_is_found_behind_an_older_scenes_error():
    b = _Bible()
    old = normalize_package({"shots": {"shots": _scene("s01")}}, b)["shots"]["shots"]
    old[0]["camera"] = "zoom"                       # an error in s01
    db = _DB([{"shot_id": s["shot_id"], "scene": "s01", "json": s} for s in old])
    new = normalize_package({"shots": {"shots": _scene("s02", n=3)}}, b)["shots"]["shots"]
    new[2]["prev_shot"] = "s02_003"                 # s02_003 points at itself
    new[1]["prev_shot"] = "s02_003"
    res = storygen_llm.merge_scene_shots(_store(), db, b, "p", "s02", new)
    assert not res["ok"] and any(e.startswith("[s02") for e in res["errors"])
    assert all(r["scene"] == "s01" for r in db.rows)


def test_imported_shots_without_an_id_or_with_one_twice_are_refused():
    b = _Bible()
    new = normalize_package({"shots": {"shots": _scene("s02")}}, b)["shots"]["shots"]
    new[1]["shot_id"] = new[0]["shot_id"]
    res = storygen_llm.merge_scene_shots(_store(), _DB([]), b, "p", "s02", new)
    assert not res["ok"] and "used twice" in res["error"]
    new[1]["shot_id"] = ""
    res = storygen_llm.merge_scene_shots(_store(), _DB([]), b, "p", "s02", new)
    assert not res["ok"] and "no shot_id" in res["error"]


def test_the_bible_refuses_an_id_that_differs_from_another_only_in_capitals():
    import pytest
    from bible import Bible
    from core import InvalidInput
    b = Bible("zz_case_guard")
    b.save_character({"id": "Sara", "tier": "main", "weapons": {"IceKatana": {}}})
    with pytest.raises(InvalidInput, match="only in capitals"):
        b.save_character({"id": "sara", "tier": "main"})
    assert b.characters()["Sara"]["weapons"] == {"IceKatana": {}}
    b.save_character({"id": "Sara", "tier": "supporting"})     # its own spelling still saves
    assert b.characters()["Sara"]["tier"] == "supporting"


def test_a_scene_row_imports_only_its_own_scene_from_a_reply_with_more():
    b = _Bible()
    old = normalize_package({"shots": {"shots": _scene("s03")}}, b)["shots"]["shots"]
    db = _DB([{"shot_id": s["shot_id"], "scene": "s03", "json": s} for s in old])
    before = {r["shot_id"]: dict(r["json"]) for r in db.rows}
    res = storygen_llm.merge_scene_shots(_store(), db, b, "p", "s02", _scene("s02") + _scene("s03", n=3))
    assert res["ok"] and res["added"] == 2 and res["left_out"] == ["s03"]
    assert {r["shot_id"]: r["json"] for r in db.rows if r["scene"] == "s03"} == before
    res = storygen_llm.merge_scene_shots(_store(), _DB([]), b, "p", "s04", _scene("s03") + _scene("s05"))
    assert not res["ok"] and "no shots for s04 (it has s03, s05)" in res["error"]


def test_a_file_left_by_the_capitals_bug_can_still_be_saved():
    import pytest
    from bible import Bible
    from core import InvalidInput
    b = Bible("zz_case_legacy")
    (b.dir / "characters" / "Sara.yaml").write_text("id: sara\ntier: main\n", encoding="utf-8")
    b.save_character({"id": "sara", "tier": "supporting"})          # the record that file holds
    assert [p.name for p in (b.dir / "characters").glob("*.yaml")] == ["Sara.yaml"]
    assert b.characters()["sara"]["tier"] == "supporting"
    with pytest.raises(InvalidInput):
        b.save_character({"id": "SARA", "tier": "main"})             # another record: refused


def test_scene_ids_take_the_projects_beats_spelling_and_new_characters_their_palette():
    out = normalize_package({
        "bible": {"characters": [{"id": "Mira", "emotion_palette": ["calm", "wary"]}]},
        "shots": {"shots": [{"shot_id": "S02_001", "prev_shot": None, "scene": "S02", "location": "Palace",
                             "characters": [{"id": "mira", "emotion": "Wary"}]}]}}, _Bible(), scenes=["s01", "s02"])
    s = out["shots"]["shots"][0]
    assert (s["scene"], s["shot_id"]) == ("s02", "s02_001")
    assert s["characters"][0] == {"id": "Mira", "emotion": "wary"}


def test_a_compact_shot_gets_no_made_up_staging():
    out = normalize_package({"shots": {"shots": _scene("s01")}}, _Bible())
    lead = out["shots"]["shots"][0]["characters"][0]
    assert "position" not in lead and "facing" not in lead
