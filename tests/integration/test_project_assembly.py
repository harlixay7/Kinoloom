"""Project error state + per-project assembly (director -> assembler)."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assembler import _concat_list, safe_title
from director import Director
from projects import ProjectStore


def test_set_status_error_is_recorded_and_cleared(isolated_db):
    store = ProjectStore(isolated_db)
    pid = store.create(title="Broken Film")["id"]
    # was: ValueError("Disallowed SQL column identifier: 'error'") raised
    # from inside the director's own exception handler
    store.set_status(pid, "error", stage="assemble", error="ffmpeg exploded")
    p = store.get(pid)
    assert p["status"] == "error" and p["error"] == "ffmpeg exploded"
    store.set_status(pid, "running", stage="render")
    assert store.get(pid)["error"] == ""


def test_list_and_get_return_the_same_shape(isolated_db):
    store = ProjectStore(isolated_db)
    pid = store.create(title="Shape")["id"]
    store.update(pid, video_models={"hero": "x", "standard": "", "volume": ""})
    listed = next(p for p in store.list() if p["id"] == pid)
    assert listed == store.get(pid)
    assert isinstance(listed["video_models"], dict)


def test_migrations_apply_to_every_database(tmp_path, monkeypatch):
    """A second database in the same process gets the added columns too."""
    import shots_db
    for name in ("a.db", "b.db"):
        monkeypatch.setattr(shots_db, "DB", tmp_path / name)
        db = shots_db.ShotsDB()
        try:
            store = ProjectStore(db)
            pid = store.create(title="Twice")["id"]
            store.set_status(pid, "error", error="x")
            assert store.get(pid)["post_worker"] == ""
        finally:
            db.con.close()


def test_director_failure_marks_project_error(isolated_db):
    store = ProjectStore(isolated_db)
    pid = store.create(title="Crashy")["id"]
    d = Director(cfg={}, db=isolated_db, store=store)
    d._run_stage = MagicMock(side_effect=RuntimeError("stage blew up"))
    d._run(pid)
    p = store.get(pid)
    assert p["status"] == "error" and "stage blew up" in p["error"]


def test_director_assembles_only_this_projects_passed_shots(tmp_path):
    good = tmp_path / "s01_001.mp4"
    good.write_bytes(b"x")
    raw = tmp_path / "s02_001_raw.mp4"
    raw.write_bytes(b"x")
    rows = [
        {"scene": "s01", "shot_id": "s01_001", "status": "passed",
         "final_path": str(good), "video_path": ""},
        {"scene": "s01", "shot_id": "s01_002", "status": "review",
         "final_path": str(good), "video_path": ""},
        {"scene": "s02", "shot_id": "s02_001", "status": "passed",
         "final_path": "", "video_path": str(raw)},          # post off
        {"scene": "s03", "shot_id": "s03_001", "status": "passed",
         "final_path": str(tmp_path / "missing.mp4"), "video_path": ""},
    ]
    db = MagicMock()
    db.all.return_value = rows
    asm = MagicMock()
    asm.assemble_movie.return_value = tmp_path / "film.mp4"
    d = Director(cfg={}, db=db, store=MagicMock(), assembler=asm)
    d._stage_assemble("proj_a", {"title": "My Film"})
    db.all.assert_called_with(project="proj_a")
    args, kw = asm.assemble_movie.call_args
    assert args[0] == ["s01", "s02"]
    assert kw["clips"] == {"s01": [good], "s02": [raw]}
    assert kw["ns"] == "proj_a"


def test_project_clips_follow_the_story_order_of_each_scene(tmp_path):
    # shot ids out of story order: the prev_shot chain decides, so a seamless
    # take's cuts concatenate back in take order
    from assembler import project_clips
    clips = {s: tmp_path / f"{s}.mp4" for s in ("a", "b", "c", "d")}
    for p in clips.values():
        p.write_bytes(b"x")
    rows = [
        {"scene": "s01", "shot_id": "c", "status": "passed", "video_path": str(clips["c"]),
         "json": {"prev_shot": "a"}},
        {"scene": "s01", "shot_id": "a", "status": "passed", "video_path": str(clips["a"]), "json": {}},
        {"scene": "s01", "shot_id": "b", "status": "passed", "video_path": str(clips["b"]),
         "json": {"prev_shot": "c"}},
        {"scene": "s02", "shot_id": "d", "status": "passed", "video_path": str(clips["d"]), "json": {}},
    ]
    assert project_clips(rows) == {"s01": [clips["a"], clips["c"], clips["b"]], "s02": [clips["d"]]}
    assert project_clips(rows, ["s02"]) == {"s02": [clips["d"]]}


def test_film_tab_assembles_the_projects_own_clips(test_client, monkeypatch, tmp_path):
    from routers import common
    clip = tmp_path / "s01_001.mp4"
    clip.write_bytes(b"x")
    rows = [{"scene": "s01", "shot_id": "s01_001", "status": "passed", "video_path": str(clip), "json": {}}]
    seen = {}
    monkeypatch.setattr(common.db, "all", lambda **kw: seen.setdefault("all", kw) and rows)

    def assemble(scenes, title, clips=None, ns=""):
        seen["call"] = (scenes, title, clips, ns)
        return tmp_path / "film.mp4"
    monkeypatch.setattr(common.assembler, "assemble_movie", assemble)
    r = test_client.post("/api/assembly/run", json={"scenes": ["s01"], "title": "Film", "project": "proj_a"})
    assert r.json() == {"ok": True, "master": str(tmp_path / "film.mp4")}
    assert seen["all"] == {"project": "proj_a"}
    assert seen["call"] == (["s01"], "Film", {"s01": [clip]}, "proj_a")
    bad = test_client.post("/api/assembly/run", json={"title": "Film", "project": "../x"}).json()
    assert bad["ok"] is False


@pytest.mark.parametrize("raw,expected", [
    ("My Film", "My Film"),
    ("C:evil", "Cevil"),
    ("../../etc/passwd", "etcpasswd"),
    ("...", "movie"),
    ("a\\b/c", "abc"),
    ("", "movie"),
])
def test_safe_title(raw, expected):
    assert safe_title(raw) == expected


def test_concat_list_escapes_quotes():
    line = _concat_list([Path("C:/x/it's.mp4")])
    assert line == "file 'C:/x/it'\\''s.mp4'\n"


def test_assemble_scene_rejects_unsafe_scene_id():
    from assembler import Assembler
    a = Assembler.__new__(Assembler)
    a.cfg, a.ff = {}, "ffmpeg"
    with pytest.raises(ValueError):
        a.assemble_scene("*")
    with pytest.raises(ValueError):
        a.assemble_scene("../x")
