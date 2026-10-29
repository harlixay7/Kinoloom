"""Continuous takes in the movie pipeline: which shot carries the one before
it on, the take's one cast, and cutting the delivered take back into
frame-exact shot clips. Which shots form a take: test_movie_prompts."""
import shutil
import subprocess

import pytest

import movie_takes as mt


def _row(sid, cont="CONTINUE", prev=None, scene="s01", loc="hall"):
    return {"shot_id": sid, "scene": scene,
            "json": {"shot_id": sid, "scene": scene, "location": loc, "continuity": cont, "prev_shot": prev}}


@pytest.mark.parametrize("row, joins", [
    (_row("b", prev="a"), True),
    (_row("b", prev="a", loc=""), True),                 # no location given: the same place
    (_row("b", cont="MATCH_ACTION", prev="a"), False),   # a cut to a new angle
    (_row("b", cont="CUT", prev="a"), False),
    (_row("b", prev="x"), False),                        # continues another shot
    (_row("b", prev="a", loc="street"), False),
    (_row("b", prev="a", scene="s02"), False),
])
def test_continues_is_a_continue_of_that_shot_in_the_same_scene_and_place(row, joins):
    assert mt.continues(_row("a", cont="CUT"), row) is joins


def test_takes_default_on_with_measured_take_lengths_that_settings_and_projects_change():
    import video_studio
    assert mt.settings({}) is True and mt.settings({"movie": {"seamless_takes": False}}) is False
    ltx, h3 = "ltx25_s6_cq", "minimax_h3_hero_v2"
    assert mt.max_shots({}, ltx) == mt.LTX_TAKE_MAX_SHOTS == 6
    assert mt.max_shots({}, h3) == mt.H3_TAKE_MAX_SHOTS == 4
    assert mt.max_shots({"movie": {"ltx_take_max_shots": 9}}, ltx) == 9
    assert mt.max_shots({"movie": {"ltx_take_max_shots": 999}}, ltx) == video_studio.CHAIN_MAX_SEGMENTS
    # the older key keeps its meaning for H3 only; the new one wins
    old = {"movie": {"take_max_shots": 2}}
    assert mt.max_shots(old, h3) == 2 and mt.max_shots(old, ltx) == 6
    assert mt.max_shots({"movie": {"h3_take_max_shots": 3, "take_max_shots": 2}}, h3) == 3
    assert mt.max_shots({"movie": {"h3_take_max_shots": 0}}, h3) == 4
    assert mt.max_shots({"movie": {"ltx_take_max_shots": "x"}}, ltx) == 6
    # a project's take length: any length on LTX, only a shorter one on H3
    # (its refine holds the whole take in memory)
    assert mt.max_shots({}, ltx, 12) == 12 and mt.max_shots({}, ltx, 1) == 1
    assert mt.max_shots({}, h3, 2) == 2 and mt.max_shots({}, h3, 9) == 4
    assert mt.max_shots({}, ltx, 0) == 6


def test_the_story_is_written_for_the_shortest_take_of_the_projects_models(monkeypatch):
    import video_models
    monkeypatch.setattr(video_models, "default_model_id", lambda: "ltx25_s6_cq")
    assert mt.story_length({}, {}) == 6
    assert mt.story_length({}, {"video_models": {"hero": "minimax_h3_hero_v2"}}) == 4
    assert mt.story_length({}, {"take_max_shots": 8}) == 8
    assert mt.story_length({}, {"take_max_shots": 8, "video_family": "minimax_h3_hero_v2"}) == 4


def test_a_projects_take_length_is_read_from_its_row(isolated_db):
    from projects import ProjectStore
    db = isolated_db
    store = ProjectStore(db)
    pid = store.create("Take Length")["id"]
    assert mt.project_length(db, pid) == 0 and store.get(pid)["take_max_shots"] == 0
    store.update(pid, take_max_shots=3)
    assert mt.project_length(db, pid) == 3 and store.get(pid)["take_max_shots"] == 3
    assert mt.project_length(db, "nobody") == 0


def test_take_id_is_stable_hex_and_depends_on_the_shots():
    a = mt.take_id("proj", "ltx25_s6_cq", ["s01_001", "s01_002"])
    assert a == mt.take_id("proj", "ltx25_s6_cq", ["s01_001", "s01_002"])
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)
    assert a != mt.take_id("proj", "ltx25_s6_cq", ["s01_001", "s01_003"])
    assert a != mt.take_id("other", "ltx25_s6_cq", ["s01_001", "s01_002"])


def test_take_shot_unites_the_cast_and_the_first_power():
    s1 = {"scene": "s01", "characters": [{"id": "mara", "tier": "supporting"}]}
    s2 = {"characters": [{"id": "mara", "tier": "main"}, {"id": "ivo", "tier": "main"}],
          "power": "frost", "power_level": 3}
    t = mt.take_shot([s1, s2])
    assert [c["id"] for c in t["characters"]] == ["mara", "ivo"]
    assert t["characters"][0]["tier"] == "main"
    assert (t["power"], t["power_level"]) == ("frost", 3)
    assert t["scene"] == "s01"


class _Bible:
    def characters(self):
        return {"mara": {"name": "Mara", "body": "a woman in her late twenties, auburn hair", "outfit": "grey coat"}}


def test_take_cast_types_names_and_looks_in_slot_order(monkeypatch):
    import ltx_refs
    monkeypatch.setattr(ltx_refs, "resolve_presentation", lambda char, sc, shot: {"outfit": char.get("outfit")})
    refs = [{"slot": "background", "path": "bg.png", "source": "location:hall"},
            {"slot": "pic2", "path": "p.png", "source": "power:mara:frost"},
            {"slot": "pic1", "path": "m.png", "source": "character:mara"}]
    cast = mt.take_cast(refs, _Bible(), {"characters": [{"id": "mara"}]})
    assert [c["slot"] for c in cast] == ["pic1", "pic2", "background"]
    assert [c["type"] for c in cast] == ["face", "power", "environment"]
    assert cast[0]["name"] == "Mara"
    assert cast[0]["description"] == "a woman in her late twenties, auburn hair, wearing grey coat"


def test_shot_bounds():
    assert mt.shot_bounds([121, 241], 361) == [(0, 121), (121, 241), (241, 361)]
    assert mt.shot_bounds([], 121) == [(0, 121)]
    with pytest.raises(ValueError):
        mt.shot_bounds([121, 121], 361)
    with pytest.raises(ValueError):
        mt.shot_bounds([400], 361)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_split_take_cuts_frame_exact_clips_with_their_sound(tmp_path):
    pytest.importorskip("av")
    ff = shutil.which("ffmpeg")
    assert ff
    take = tmp_path / "take.mp4"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=96x64:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-frames:v", "57",
                    "-t", f"{57 / 24}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(take)],
                   check=True)
    outs = [tmp_path / f"shot{i}.mp4" for i in range(3)]
    mt.split_take(take, [17, 41], outs, ff)
    import av
    for out, n in zip(outs, (17, 24, 16)):
        with av.open(str(out)) as ct:
            assert sum(1 for p in ct.demux(ct.streams.video[0]) if p.size) == n
            a = ct.streams.audio[0]
            assert abs(float(a.duration * a.time_base) - n / 24) < 0.03
    with pytest.raises(ValueError):
        mt.split_take(take, [17, 41], outs[:2], ff)
