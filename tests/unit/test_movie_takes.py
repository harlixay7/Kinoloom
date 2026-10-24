"""Continuous takes in the movie pipeline: which shots join a take, the take's
one cast, and cutting the delivered take back into frame-exact shot clips."""
import shutil
import subprocess

import pytest

import movie_takes as mt


def _row(sid, cont="CONTINUE", prev=None, status="pending", scene="s01", loc="hall", **extra):
    j = {"shot_id": sid, "scene": scene, "location": loc, "continuity": cont, "prev_shot": prev,
         **extra.pop("json", {})}
    return {"shot_id": sid, "scene": scene, "status": status, "qa_stage": extra.pop("qa_stage", 0),
            "json": j, **extra}


def _scene(*rows):
    return list(rows)


def _ids(run):
    return [r["shot_id"] for r in run]


def same_model(_row):
    return "ltx25_s6_cq"


def test_a_run_of_continue_shots_is_one_take():
    a = _row("a", cont="CUT", status="queued")
    rows = _scene(a, _row("b", prev="a"), _row("c", prev="b"), _row("d", cont="CUT", prev="c"))
    assert _ids(mt.plan_take(a, rows, same_model)) == ["a", "b", "c"]


@pytest.mark.parametrize("breaker", [
    _row("b", cont="MATCH_ACTION", prev="a"),          # a new camera set-up: an edit
    _row("b", cont="CUT", prev="a"),
    _row("b", prev="a", loc="street"),                  # location change
    _row("b", prev="a", scene="s02"),                   # scene change
    _row("b", prev="a", status="passed"),               # already rendered
    _row("b", prev="a", qa_stage=1),                    # a QA retake renders alone
    _row("b", prev="a", json={"t2v": True}),
])
def test_a_take_ends_at_anything_but_a_fresh_continue(breaker):
    a = _row("a", cont="CUT", status="queued")
    assert _ids(mt.plan_take(a, _scene(a, breaker), same_model)) == ["a"]


def test_a_take_stays_on_one_model_ends_at_a_fork_and_is_capped():
    a = _row("a", cont="CUT", status="queued")
    rows = _scene(a, _row("b", prev="a"), _row("c", prev="b"))
    assert _ids(mt.plan_take(a, rows, lambda r: "h3" if r["shot_id"] == "c" else "ltx")) == ["a", "b"]
    fork = _scene(a, _row("b", prev="a"), _row("b2", prev="a"))
    assert _ids(mt.plan_take(a, fork, same_model)) == ["a"]
    long = [a] + [_row(f"x{i}", prev=("a" if i == 0 else f"x{i - 1}")) for i in range(8)]
    assert len(mt.plan_take(a, long, same_model, max_shots=4)) == 4
    retake = _row("a", cont="CUT", status="queued", qa_stage=2)
    assert _ids(mt.plan_take(retake, _scene(retake, _row("b", prev="a")), same_model)) == ["a"]


def test_settings_default_on_and_capped_to_at_least_one_shot():
    assert mt.settings({}) == (True, mt.TAKE_MAX_SHOTS)
    assert mt.settings({"movie": {"seamless_takes": False, "take_max_shots": 0}}) == (False, mt.TAKE_MAX_SHOTS)
    assert mt.settings({"movie": {"take_max_shots": 2}}) == (True, 2)


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
