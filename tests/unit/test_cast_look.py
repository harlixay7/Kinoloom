"""A cast member's look text against the picture it was picked from."""
import json
from types import SimpleNamespace

import pytest

import cast_look
import datasets_db

SARA = {"age": "young adult", "presentation": "woman", "hair_style": "long", "hair_color": "dark brown",
        "eye_color": "violet", "eye_shape": "almond", "silhouette": "fitted dress", "wardrobe_color": "silver"}


@pytest.fixture
def record(monkeypatch):
    """A Casting record for face/Sara/stills/v_002.png made with `params`."""
    params = dict(SARA)
    rows = [{"file": "stills/v_002.png", "run_id": 7, "variations": {"outfit": "base outfit"}}]
    images, run = datasets_db.list_images, datasets_db.get_run     # another fixture's records stay
    monkeypatch.setattr(datasets_db, "list_images",
                        lambda t, n, limit=0: list(rows) if n == "Sara" else images(t, n, limit=limit))
    monkeypatch.setattr(datasets_db, "get_run", lambda rid: {"params": json.dumps(params)} if rid == 7 else run(rid))
    cast_look._LOOKS.clear()
    yield params
    cast_look._LOOKS.clear()


def _sara(**kw):
    char = {"id": "sara", "name": "Sara", "reference_images": [
        {"type": "face", "name": "Sara", "file": "stills/v_002.png", "role": "primary"}]}
    char.update(kw)
    return char


def test_the_picture_record_gives_the_autofill_text(record):
    look = cast_look.picture_look({"type": "face", "name": "Sara", "file": "stills/v_002.png"})
    assert look["body"] == "young adult woman, long dark brown hair, violet almond eyes"
    assert look["outfit"] == "fitted dress, silver"
    assert look["presentation"] == "woman" and look["from"] == "face/Sara/stills/v_002.png"
    # a still without a record (an upload, a deleted batch) has no look
    assert cast_look.picture_look({"type": "face", "name": "Other", "file": "x.png"}) is None
    assert cast_look.picture_look({"file": "upload.png"}) is None


def test_a_story_look_without_a_gender_is_flagged_and_the_picture_text_offered(record):
    # the measured failure: a story AI's guess, no gender, another age and clothes
    c = cast_look.check(_sara(body="a weathered exile in their thirties, sharp eyes",
                              outfit="charred leathers and a travel cloak"))
    assert not c["ok"]
    assert any("does not say this is a woman" in p for p in c["problems"])
    assert any("another age" in p for p in c["problems"])
    assert any("other clothes" in p for p in c["problems"])
    assert c["body"].startswith("young adult woman") and c["outfit"] == "fitted dress, silver"


def test_a_text_that_describes_the_picture_passes(record):
    assert cast_look.check(_sara(body="young adult woman, long dark brown hair, violet eyes",
                                 outfit="a fitted silver dress"))["ok"]
    # other words for the same person are fine
    assert cast_look.check(_sara(body="a slender lady in her early twenties", outfit="silver gown, dress"))["ok"]


def test_the_other_gender_is_named_for_what_it_is(record):
    c = cast_look.check(_sara(body="a tall man with a beard", outfit="fitted dress"))
    assert c["problems"][0] == "it describes a man, the picture is of a woman"


def test_ages_in_years_and_older_gender_fields_are_read(record):
    record.update(presentation="", gender="male", age="40s", silhouette="long coat")
    assert cast_look.check(_sara(body="a 45-year-old man", outfit="a long grey coat"))["ok"]
    c = cast_look.check(_sara(body="a 25 year old man", outfit="a long grey coat"))
    assert c["problems"] == ["it gives another age than the picture (40s)"]


def test_text_typed_by_hand_is_never_flagged(record):
    assert cast_look.check(_sara(body="a tall man", outfit="armour", look_source="hand")) is None


def test_nothing_to_compare_is_no_warning(record):
    assert cast_look.check({"id": "x", "body": "a man"}) is None            # no picture
    upload = {"id": "x", "body": "a man", "reference_images": [{"file": "ref_1.png"}]}
    assert cast_look.check(upload) is None                                   # a picture without a record
    assert cast_look.check(_sara(body="", outfit=""))["problems"] == ["it is empty"]


def test_an_outfit_for_scenes_never_decides_the_look_but_an_identity_plate_does(record):
    char = {"reference_images": [
        {"type": "face", "name": "Sara", "file": "stills/armour.png", "tag": "SaraArmored", "role": "secondary"},
        {"type": "face", "name": "Sara", "file": "stills/v_002.png", "tag": "plate:medium", "role": "primary"}]}
    assert cast_look.primary_face(char)["file"] == "stills/v_002.png"
    assert cast_look.primary_face({"reference_images": char["reference_images"][:1]}) is None


def test_a_new_main_look_rewrites_text_from_casting_or_a_story_but_never_typed_text(record):
    entry = _sara()["reference_images"][0]
    for src in ("story", "casting:face/Sara/stills/v_001.png"):
        out, changed = cast_look.refresh(_sara(body="old", outfit="old", look_source=src), entry)
        assert changed == {"body": out["body"], "outfit": out["outfit"]}
        assert out["look_source"] == "casting:face/Sara/stills/v_002.png"
    for src in ("hand", ""):
        out, changed = cast_look.refresh(_sara(body="mine", outfit="mine", look_source=src), entry)
        assert changed == {} and out["body"] == "mine"
    # an empty field is filled whoever wrote the other one
    out, changed = cast_look.refresh(_sara(body="mine", outfit="", look_source="hand"), entry)
    assert changed == {"outfit": "fitted dress, silver"} and out["look_source"] == "hand"


def test_every_cast_sentence_names_the_gender_of_the_picture(record):
    assert cast_look.body_text(_sara(body="long dark brown hair")) == "a woman, long dark brown hair"
    assert cast_look.body_text(_sara(body="")) == "a woman"
    # a text that names one keeps it, even the other one (the user may know better)
    assert cast_look.body_text(_sara(body="a man with long hair")) == "a man with long hair"
    # no record: the text as written
    assert cast_look.body_text({"body": "long hair"}) == "long hair"


def test_checks_leave_out_benched_characters_and_never_fail(record, monkeypatch):
    chars = {"sara": _sara(body="a man", outfit="dress"),
             "benched": _sara(body="a man", outfit="dress", enabled=False),
             "fine": _sara(body="a young woman", outfit="dress")}
    assert list(cast_look.checks(chars)) == ["sara"]
    monkeypatch.setattr(cast_look, "check", lambda c: 1 / 0)
    assert cast_look.checks(chars) == {}


def test_the_record_is_read_once_a_minute_at_most(record, monkeypatch):
    calls = []
    real = datasets_db.list_images
    monkeypatch.setattr(datasets_db, "list_images", lambda *a, **k: calls.append(1) or real(*a, **k))
    entry = _sara()["reference_images"][0]
    cast_look.picture_look(entry)
    cast_look.picture_look(entry)
    assert len(calls) == 1
    monkeypatch.setattr(cast_look.time, "monotonic", lambda: 10 ** 9)
    cast_look.picture_look(entry)
    assert len(calls) == 2


# ------------------------------------------------------------------ places
COURTYARD = {"location": "the grand courtyard", "place_kind": "exterior", "time": "night",
             "place_desc": "a vast paved courtyard ringed by colonnades and a central fountain"}
PALACE = {"world": "royal palace", "lighting": "overcast daylight", "materials": ["marble"]}


@pytest.fixture
def places(monkeypatch):
    rows = [{"file": "stills/e_022.png", "run_id": 3, "variations": dict(COURTYARD)},
            {"file": "stills/e_000.png", "run_id": 3, "variations": json.dumps(
                {"location": "a gilded corridor", "place_kind": "interior"})}]
    monkeypatch.setattr(datasets_db, "list_images",
                        lambda t, n, limit=0: list(rows) if (t, n) == ("environment", "Palace1") else [])
    monkeypatch.setattr(datasets_db, "get_run", lambda rid: {"params": json.dumps(PALACE)})
    cast_look._LOOKS.clear()
    yield
    cast_look._LOOKS.clear()


PALACE_LOC = {"id": "palace", "descriptor": "a fantasy city", "reference_images": [
    {"type": "environment", "name": "Palace1", "file": "stills/e_000.png", "copied": "ref_e_000.png", "tag": ""},
    {"type": "environment", "name": "Palace1", "file": "stills/e_022.png", "copied": "ref_e_022.png",
     "tag": "palace_waterfront"},
    {"type": "environment", "name": "Gone", "file": "x.png", "tag": "no_record"}]}


def test_a_place_picture_is_told_by_what_it_was_made_with(places):
    # the batch's lighting is what the picture shows, never the still's own "night"
    assert cast_look.place_text(PALACE_LOC["reference_images"][1]) == (
        "a vast paved courtyard ringed by colonnades and a central fountain "
        "(the grand courtyard, royal palace, exterior, overcast daylight)")
    assert cast_look.place_text(PALACE_LOC["reference_images"][0]) == (
        "a gilded corridor (royal palace, interior, overcast daylight)")
    assert cast_look.place_text(PALACE_LOC["reference_images"][2]) == ""
    assert cast_look.place_text(None) == ""


def test_the_view_a_shot_renders_with_is_found_by_its_file_its_tag_or_as_the_main_one(places):
    pick = cast_look.place_entry
    assert pick(PALACE_LOC, "", r"C:\bible\refs\locs\palace\ref_e_022.png")["tag"] == "palace_waterfront"
    assert pick(PALACE_LOC, "Palace_Waterfront")["file"] == "stills/e_022.png"
    assert pick(PALACE_LOC)["file"] == "stills/e_000.png"
    assert pick(PALACE_LOC, "nowhere") is None and pick({}) is None


def test_the_story_writer_reads_what_each_view_shows(places):
    from storygen_llm import loc_lines
    out = loc_lines(SimpleNamespace(locations=lambda: {"palace": PALACE_LOC}))
    assert "its picture shows: a gilded corridor (royal palace, interior, overcast daylight)" in out
    assert "palace_waterfront = a vast paved courtyard ringed by colonnades" in out
    assert out.endswith("; no_record")          # a view without a record keeps its name


def test_a_take_tells_the_prompt_writer_the_set_its_background_shows(places, record):
    from movie_takes import take_cast
    bible = SimpleNamespace(characters=lambda: {"sara": _sara(body="long dark hair", outfit="silver dress")},
                            locations=lambda: {"palace": PALACE_LOC})
    refs = [{"slot": "pic1", "path": "C:/refs/chars/sara/ref.png", "source": "character:sara"},
            {"slot": "background", "path": "C:/refs/locs/palace/ref_e_022.png",
             "source": "location:palace:palace_waterfront"}]
    cast = take_cast(refs, bible, {"characters": [{"id": "sara"}]})
    assert cast[0]["description"].startswith("a woman, long dark hair")
    assert cast[1]["type"] == "environment"
    assert cast[1]["description"].startswith("a vast paved courtyard")


# ------------------------------------------------------------ the Cast router
def _router(monkeypatch, tmp_path, chars):
    from routers import cast
    b = SimpleNamespace(dir=tmp_path, characters=lambda: {k: dict(v) for k, v in chars.items()},
                        save_character=lambda c: chars.update({c["id"]: c}))
    monkeypatch.setattr(cast, "Bible", lambda pid: b)
    monkeypatch.setattr(cast, "_proj_guard", lambda pid: ({"id": pid}, None))
    return cast


def test_a_save_marks_who_wrote_the_look(record, monkeypatch, tmp_path):
    chars = {"sara": _sara(body="old", outfit="old", look_source="story")}
    cast = _router(monkeypatch, tmp_path, chars)
    # the Autofill text, saved as it came: the picture's
    cast.cast_character_update("p", "sara", {"body": "b1", "outfit": "o1",
                                             "look_source": "casting:face/Sara/stills/v_002.png"})
    assert chars["sara"]["look_source"] == "casting:face/Sara/stills/v_002.png"
    # a save that leaves the text as it is keeps its source
    cast.cast_character_update("p", "sara", {"body": "b1", "outfit": "o1", "tier": "main"})
    assert chars["sara"]["look_source"].startswith("casting:")
    # typed text is the user's
    cast.cast_character_update("p", "sara", {"body": "b2", "outfit": "o1"})
    assert chars["sara"]["look_source"] == "hand"
    # anything else a client sends is not a source
    cast.cast_character_update("p", "sara", {"body": "b3", "look_source": "story"})
    assert chars["sara"]["look_source"] == "hand"


def test_apply_and_keep_answer_the_warning(record, monkeypatch, tmp_path):
    chars = {"sara": _sara(body="a weathered exile", outfit="leathers")}
    cast = _router(monkeypatch, tmp_path, chars)
    r = cast.cast_look_apply("p", "sara")
    assert r["ok"] and chars["sara"]["body"].startswith("young adult woman")
    assert cast_look.check(chars["sara"])["ok"]
    chars["sara"].update(body="a weathered exile", outfit="leathers")
    assert cast.cast_look_keep("p", "sara")["ok"] and cast_look.check(chars["sara"]) is None
    # a main look without a record cannot be applied
    chars["sara"]["reference_images"] = [{"file": "ref_1.png"}]
    assert not cast.cast_look_apply("p", "sara")["ok"]


def test_picking_a_main_look_rewrites_a_story_look(record, monkeypatch, tmp_path):
    src = tmp_path / "v_002.png"
    src.write_bytes(b"png")
    chars = {"sara": {"id": "sara", "name": "Sara", "body": "a weathered exile", "outfit": "leathers",
                      "look_source": "story"}}
    cast = _router(monkeypatch, tmp_path, chars)
    monkeypatch.setattr(cast, "_batch_image_path", lambda t, n, f: src)
    r = cast.cast_heroref_select("p", "sara", cast.PickSelectBody(type="face", name="Sara",
                                                                 file="stills/v_002.png"))
    assert r["look_filled"]["body"].startswith("young adult woman")
    assert chars["sara"]["look_source"] == "casting:face/Sara/stills/v_002.png"
    # an extra look leaves the text alone
    chars["sara"]["body"] = "kept"
    r = cast.cast_heroref_select("p", "sara", cast.PickSelectBody(
        type="face", name="Sara", file="stills/v_002.png", role="secondary", append=True))
    assert r["look_filled"] == {} and chars["sara"]["body"] == "kept"


# ------------------------------------------------------------- the Director
def test_the_film_waits_at_the_cast_while_a_look_contradicts_its_picture(record, monkeypatch):
    import bible as bible_mod
    from director import Director
    chars = {"sara": _sara(body="a weathered exile", outfit="leathers")}
    monkeypatch.setattr(bible_mod, "Bible", lambda pid: SimpleNamespace(characters=lambda: chars))
    statuses = []
    d = Director.__new__(Director)
    d.store = SimpleNamespace(set_status=lambda pid, st, **kw: statuses.append((st, kw)))
    assert not d._stage_done("p", "cast", {})
    assert d._stage_cast("p") == "gate"
    st, kw = statuses[-1]
    assert st == "gate_pending" and kw["stage"] == "cast"
    assert "Sara" in kw["error"] and "does not say this is a woman" in kw["error"]
    chars["sara"]["look_source"] = "hand"
    assert d._stage_done("p", "cast", {}) and d._stage_cast("p") is None
