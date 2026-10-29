"""Picking pictures and adding places on the Cast & World page (routers/cast.py):
a picture copied into the project while Windows still holds the old copy, and a
new place named in the page."""
import os
import shutil
import time

from routers import cast


def test_a_picked_picture_is_copied_once_and_waits_for_a_held_file(tmp_path, monkeypatch):
    src = tmp_path / "e_011.png"
    src.write_bytes(b"one")
    dst = tmp_path / "refs" / "selected.png"
    dst.parent.mkdir()
    assert cast._copy_ref(src, dst) is None and dst.read_bytes() == b"one"

    # the same picture again (the second click of a double-click): left as it is
    def held(*a, **k):
        raise PermissionError(32, "The process cannot access the file")
    monkeypatch.setattr(shutil, "copy2", held)
    assert cast._copy_ref(src, dst) is None
    monkeypatch.undo()

    # another picture while a program holds the old copy: tried a few times,
    # then a plain error, and no half-written file is left beside it
    other = tmp_path / "e_012.png"
    other.write_bytes(b"two")
    tries = []
    real_replace = os.replace

    def locked(a, b):
        tries.append(1)
        raise PermissionError(32, "The process cannot access the file")
    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    err = cast._copy_ref(other, dst)
    assert "open in another program" in err and len(tries) == 6
    assert dst.read_bytes() == b"one" and not list(dst.parent.glob("*.part"))

    # it lets go a moment later: the next try goes through
    tries.clear()

    def once(a, b):
        if not tries:
            tries.append(1)
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(a, b)
    monkeypatch.setattr(os, "replace", once)
    assert cast._copy_ref(other, dst) is None and dst.read_bytes() == b"two"
    assert not list(dst.parent.glob("*.part"))


def test_another_picture_of_the_same_size_and_time_is_still_copied(tmp_path):
    # Windows stamps two files written within one clock tick with the same
    # time: only their bytes tell them apart
    src = tmp_path / "e_011.png"
    src.write_bytes(b"one")
    dst = tmp_path / "selected.png"
    dst.write_bytes(b"two")
    st = src.stat()
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert cast._copy_ref(src, dst) is None and dst.read_bytes() == b"one"


class _Bible:
    def __init__(self, locs):
        self.locs = locs

    def locations(self):
        return self.locs

    def save_location(self, loc):
        self.locs[loc["id"]] = loc


def test_a_new_place_takes_its_typed_name_and_never_clashes(monkeypatch):
    locs: dict = {}
    monkeypatch.setattr(cast, "_proj_guard", lambda pid: ({"id": pid}, None))
    monkeypatch.setattr(cast, "Bible", lambda pid: _Bible(locs))
    r = cast.cast_location_create("p", {"name": "Palace lobby", "enabled": True})
    assert r["ok"] and r["location"]["id"] == "palace_lobby"
    # the same name again is numbered, where it used to fail on "already exists"
    assert cast.cast_location_create("p", {"name": "palace lobby"})["location"]["id"] == "palace_lobby_2"
    # an explicit id still refuses a clash
    assert not cast.cast_location_create("p", {"id": "palace_lobby"})["ok"]
    assert "letters" in cast.cast_location_create("p", {"name": "日本"})["error"]
    assert cast.cast_location_create("p", {"name": "!!!"})["location"]["id"] == "place"


def test_a_new_place_or_character_in_other_capitals_does_not_write_over_one(monkeypatch):
    locs: dict = {"Palace": {"id": "Palace"}}
    monkeypatch.setattr(cast, "_proj_guard", lambda pid: ({"id": pid}, None))
    monkeypatch.setattr(cast, "Bible", lambda pid: _Bible(locs))
    assert cast.cast_location_create("p", {"name": "palace"})["location"]["id"] == "palace_2"
    assert not cast.cast_location_create("p", {"id": "PALACE"})["ok"]


def test_a_picture_that_cannot_be_copied_is_told_for_what_it_is(tmp_path, monkeypatch):
    gone = tmp_path / "e_404.png"
    dst = tmp_path / "selected.png"
    assert "no longer in its batch" in cast._copy_ref(gone, dst)
    src = tmp_path / "e_001.png"
    src.write_bytes(b"one")

    def full(a, b):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(shutil, "copy2", full)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    err = cast._copy_ref(src, dst)
    assert "could not be written" in err and "open in another program" not in err


def test_deleting_a_scene_drops_its_beat_so_the_film_does_not_wait_for_it(monkeypatch, tmp_path):
    from types import SimpleNamespace
    saved = {}
    proj = {"id": "p", "story": {"beats": {"beats": [{"scene": "s01"}, {"scene": "s02"}]}}}
    monkeypatch.setattr(cast, "store", SimpleNamespace(
        get=lambda pid: proj, set_story_stage=lambda pid, stage, content: saved.update({stage: content})))
    rows = [{"shot_id": "s02_001", "scene": "s02"}]
    monkeypatch.setattr(cast, "db", SimpleNamespace(all=lambda project=None: list(rows),
                                                    delete_shot=lambda sid, pid: rows.clear()))
    monkeypatch.setattr(cast, "Bible", lambda pid: SimpleNamespace(dir=tmp_path))
    r = cast.cast_scene_delete("p", "s02")
    assert r["ok"] and r["removed_shots"] == 1 and saved["beats"]["beats"] == [{"scene": "s01"}]


def test_a_picked_look_writes_an_empty_body_and_outfit_from_its_batch_prompt(tmp_path, monkeypatch):
    # the story AI cannot see the picture: the look comes from what the still
    # was made with, and text already written stays as it is
    import json
    from types import SimpleNamespace
    src = tmp_path / "v_002.png"
    src.write_bytes(b"png")
    chars = {"new_character": {"id": "new_character", "name": "New Character", "body": "", "outfit": ""}}
    b = SimpleNamespace(dir=tmp_path, characters=lambda: {k: dict(v) for k, v in chars.items()},
                        save_character=lambda c: chars.update({c["id"]: c}))
    monkeypatch.setattr(cast, "Bible", lambda pid: b)
    monkeypatch.setattr(cast, "_batch_image_path", lambda t, n, f: src)
    params = {"age": "early 20s", "presentation": "woman", "hair_style": "long wavy", "hair_color": "dark",
              "silhouette": "fitted gown", "wardrobe_color": "silver"}
    monkeypatch.setattr(cast.datasets_db, "list_images", lambda t, n, limit=0: [
        {"file": "stills/v_002.png", "run_id": 7, "variations": json.dumps({"outfit": "base outfit"})}])
    monkeypatch.setattr(cast.datasets_db, "get_run", lambda rid: {"params": json.dumps(params)})
    body = cast.PickSelectBody(type="face", name="Sara", file="stills/v_002.png")
    r = cast.cast_heroref_select("p", "new_character", body)
    assert r["ok"] and r["look_filled"] == {"body": "early 20s woman, long wavy dark hair",
                                            "outfit": "fitted gown, silver"}
    assert chars["new_character"]["body"] == "early 20s woman, long wavy dark hair"
    # a look already written is the operator's: another pick leaves it
    chars["new_character"]["outfit"] = "charred mantle"
    r = cast.cast_heroref_select("p", "new_character", cast.PickSelectBody(
        type="face", name="Sara", file="stills/v_002.png", role="secondary", append=True))
    assert r["look_filled"] == {} and chars["new_character"]["outfit"] == "charred mantle"
    # an outfit picture (a loadout composite) never writes the base look
    chars["new_character"].update(body="", outfit="")
    r = cast.cast_heroref_select("p", "new_character", cast.PickSelectBody(
        type="face", name="Sara", file="stills/v_002.png", tag="gown", append=True))
    assert r["look_filled"] == {} and chars["new_character"]["body"] == ""
