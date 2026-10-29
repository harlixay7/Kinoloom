"""The movie renders every LTX-2.5 / H3 unit with the Video Studio's own
request: a run of CONTINUE shots as one seamless take (one chain, one prompt
per shot written for it, cut back into one clip per shot), any other shot as
one clip. A unit without its prompts gets them written, or waits; a failed
unit goes back to the queue whole with its seeds."""
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import llm
import movie_prompts as mp
import movie_takes
import prompt_check
import video_studio
from moviekit import MID, SIDS, Bible, FakeLLM, shot_rows, take_reply
from orchestrator import REROLL_SEED, Orchestrator
from projects import ProjectStore

CONT = prompt_check._CONT_LINE["ltx"]


def _orch(db, cfg):
    o = Orchestrator.__new__(Orchestrator)
    o.db, o.cfg = db, {**cfg, "qa": {"max_retries": {"standard": 2, "hero": 3}}}
    o.running, o.paused = True, False
    o._lock = threading.Lock()
    o._post_queue = queue.Queue()
    o._post_thread = o._thread = None
    o._transient, o.job_callbacks, o.last_error = {}, [], None
    o.project_filter = None
    return o


@pytest.fixture
def env(movie, tmp_path, monkeypatch):
    """(orchestrator, db, pid, ai, calls): the Video Studio replaced by
    recorders, the AI by a FakeLLM that writes the take and the last CUT."""
    import continuity
    import orchestrator as orch_mod
    db, pid, cfg, _ = movie
    ai = FakeLLM([take_reply(), take_reply(["s01_004"])])
    monkeypatch.setattr(llm, "LLMClient", lambda cfg=None: ai)
    monkeypatch.setattr(orch_mod, "ffmpeg_exe", lambda cfg=None: "ffmpeg")
    monkeypatch.setattr(continuity, "extract_tail", lambda clip, sid, pid=None: None)
    calls: dict = {"chain": [], "clip": []}
    take = tmp_path / "take_final.mp4"
    take.write_bytes(b"mp4")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"mp4")
    calls["rec"] = {"status": "completed", "final_video": str(take), "seam_frames": [121, 241],
                    "engine": "seamless-ltx", "qa": {"passed": True}}
    calls["job"] = {"status": "completed", "outputs": [clip.name], "output_dir": str(tmp_path),
                    "seconds": 1}
    monkeypatch.setattr(video_studio, "chain_running", lambda cid: False)
    monkeypatch.setattr(video_studio, "get_chain", lambda cid: calls["rec"])
    monkeypatch.setattr(video_studio, "job", lambda jid: calls["job"])

    def start_chain(mid, prompts, **kw):
        calls["chain"].append({"mid": mid, "prompts": prompts, **kw})
        return {"id": kw["chain_id"]}

    def start(mid, prompt, **kw):
        calls["clip"].append({"mid": mid, "prompt": prompt, **kw})
        return {"id": f"job{len(calls['clip'])}"}
    monkeypatch.setattr(video_studio, "start_chain", start_chain)
    monkeypatch.setattr(video_studio, "start", start)

    def split(take_path, seams, outs, ff):
        calls["split"] = (Path(take_path), list(seams), [Path(p) for p in outs])
        for p in outs:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_bytes(b"clip")
        return outs
    monkeypatch.setattr(movie_takes, "split_take", split)
    return _orch(db, cfg), db, pid, ai, calls


def _render(o, db, pid, sid=SIDS[0]):
    db.set_status(sid, "queued", project=pid)
    o._render_shot(db.get(sid, pid))


def test_a_continue_run_renders_as_one_take_with_its_written_prompts(env):
    o, db, pid, ai, calls = env
    _render(o, db, pid)
    (req,) = calls["chain"]
    sids = SIDS[:3]
    assert req["mid"] == MID and req["negative"] == "" and "steps" not in req
    assert req["chain_id"] == movie_takes.take_id(pid, MID, sids)
    assert req["origin"] == {"project": pid, "shots": sids}
    assert req["start_image"] is None and req["mode"] == "t2v"   # the cast rides the reference graph
    # the AI's prompts, with the check's continuity line on the later segments
    assert len(ai.calls) == 1
    assert req["prompts"][0].startswith("Medium shot") and "s01_001" in req["prompts"][0]
    assert all(p.startswith(CONT) for p in req["prompts"][1:])
    (ref,) = req["references"]
    assert (ref["slot"], ref["type"], ref["name"]) == ("pic1", "face", "Mara") and "auburn" in ref["description"]

    _, seams, outs = calls["split"]
    assert seams == [121, 241] and len(outs) == 3
    # each shot its own seed (a fresh take's are consecutive)
    assert req["seeds"] == [req["seed"] + i for i in range(3)]
    for i, sid in enumerate(sids):
        row = db.get(sid, pid)
        assert row["status"] == "post" and Path(row["video_path"]) == outs[i]
        assert row["seed"] == req["seeds"][i] and row["prompt"] == req["prompts"][i]
        assert row["json"]["take"]["id"] == req["chain_id"] and row["json"]["take"]["index"] == i + 1
        assert row["json"]["applied_model"] == MID
    assert db.get("s01_001", pid)["json"]["request"]["shots"] == sids
    assert db.get("s01_002", pid)["json"]["continuity_applied"] == "CONTINUE (seamless take)"
    assert db.get("s01_004", pid)["status"] == "pending"     # the CUT after the take renders alone
    assert [o._post_queue.get_nowait()[0]["shot_id"] for _ in range(3)] == sids


def test_a_cut_renders_as_one_clip_with_the_page_request(env):
    o, db, pid, ai, calls = env
    ai.replies = [take_reply(["s01_004"])]
    _render(o, db, pid, "s01_004")
    (req,) = calls["clip"]
    assert req["mid"] == MID and req["negative"] == "" and req["worker"] == "local"
    assert req["start_image"] is None and req["continuation"] is None and req["frames"] == 121
    assert req["references"][0]["name"] == "Mara"
    assert req["seed"] == db.stable_seed(db.get("s01_004", pid))
    row = db.get("s01_004", pid)
    assert row["status"] == "post" and row["json"]["applied_model"] == MID
    assert row["json"]["request"]["opening"] == "cut"
    assert "A cut: a new shot" in ai.calls[0][1]["content"]


def test_a_continue_clip_continues_the_last_frame_of_the_shot_before(env, sandbox_root):
    o, db, pid, ai, calls = env
    ProjectStore(db).update(pid, seamless_takes=0)
    tail = sandbox_root / "output" / "movie_refs" / "s01_001_tail.png"
    tail.write_bytes(b"png tail")
    db.set_status("s01_001", "passed", tail_frame=str(tail), project=pid)
    ai.replies = [take_reply(["s01_002"])]
    _render(o, db, pid, "s01_002")
    (req,) = calls["clip"]
    assert req["continuation"] == str(tail) and req["start_image"] is None
    assert req["prompt"].startswith(CONT)
    assert db.get("s01_002", pid)["json"]["continuity_applied"] == "CONTINUE"
    assert "No cut: the same camera take carries on" in ai.calls[0][1]["content"]


def test_a_unit_without_prompts_and_no_ai_waits_until_they_are_written(env, monkeypatch):
    o, db, pid, _, calls = env

    class NoAI(FakeLLM):
        def config_ok(self):
            return False
    monkeypatch.setattr(llm, "LLMClient", lambda cfg=None: NoAI())
    _render(o, db, pid)
    assert not calls["chain"]
    rows = [db.get(s, pid) for s in SIDS[:3]]
    assert [r["status"] for r in rows] == [mp.WAITING] * 3
    assert rows[0]["error"] == "needs its prompt: no prompt yet"
    assert o._next_shot()["shot_id"] == "s01_004"            # the waiting take is skipped
    for sid, text in mp.parse_reply(take_reply(), SIDS[:3])[0].items():
        assert mp.save_hand(db, Bible(pid), pid, sid, text, cfg=o.cfg)["ok"]
    assert [db.get(s, pid)["status"] for s in SIDS[:3]] == ["queued"] * 3
    o._render_shot(db.get("s01_001", pid))
    assert len(calls["chain"]) == 1


def test_the_render_stops_where_the_pasted_prompts_end_and_goes_on_with_the_next(env, monkeypatch):
    # the chat AI's reply holds the first take's prompts only: the take renders,
    # production stops at the next shot without a prompt (and the match cut that
    # opens on it), and goes on when the next reply is pasted
    from director import Director
    o, db, pid, _, calls = env

    class NoAI(FakeLLM):
        def config_ok(self):
            return False
    monkeypatch.setattr(llm, "LLMClient", lambda cfg=None: NoAI())
    db.upsert_shots([shot_rows(pid)[3] | {"shot_id": "s01_005", "continuity": "MATCH_ACTION",
                                          "prev_shot": "s01_004",
                                          "primary_action": "Mara turns to face the doors"}])
    store = ProjectStore(db)
    orch = MagicMock()
    director = Director(cfg=o.cfg, db=db, store=store, orch=orch, writer=MagicMock())
    assert mp.import_reply(db, Bible(pid), store.get(pid), take_reply(), o.cfg)["saved"] == SIDS[:3]

    def render_what_can():
        while (shot := o._next_shot()) is not None:
            o._render_shot(shot)
            for r in db.all(project=pid):               # the post stage and the QA pass them
                if r["status"] == "post":
                    db.set_status(r["shot_id"], "passed", project=pid)

    def states():
        return {r["shot_id"]: r["status"] for r in db.all(project=pid)}

    render_what_can()
    assert len(calls["chain"]) == 1 and not calls["clip"]
    assert states() == {"s01_001": "passed", "s01_002": "passed", "s01_003": "passed",
                        "s01_004": mp.WAITING, "s01_005": "pending"}
    assert director._stage_render(pid, store.get(pid)) == "gate"
    orch.stop.assert_called_once()
    p = store.get(pid)
    assert (p["status"], p["stage"]) == ("gate_pending", "prompts")
    assert p["error"].startswith("3 of 5 shots are rendered. 1 shot(s) need their video prompt (s01_004), "
                                 "and 1 more open on them: paste them from any chat AI on the Shots page")

    res = mp.import_reply(db, Bible(pid), store.get(pid), take_reply(["s01_004"]), o.cfg)
    assert res["saved"] == ["s01_004"] and states()["s01_004"] == "queued"
    assert director._held_by_prompts(db.all(project=pid)) is None      # it renders on Continue
    render_what_can()
    assert len(calls["clip"]) == 1 and calls["clip"][0]["prompt"].startswith("Medium shot")
    assert states() == {"s01_001": "passed", "s01_002": "passed", "s01_003": "passed",
                        "s01_004": "passed", "s01_005": mp.WAITING}
    assert director._stage_render(pid, store.get(pid)) == "gate"
    assert store.get(pid)["error"].startswith("4 of 5 shots are rendered. 1 shot(s) need their video "
                                              "prompt (s01_005): paste them")


def test_a_failed_take_requeues_whole_with_its_seeds_then_goes_to_review(env):
    o, db, pid, ai, calls = env
    calls["rec"] = {"status": "failed", "error": "decode_B0: CUDA error"}
    _render(o, db, pid)
    rows = [db.get(s, pid) for s in SIDS[:3]]
    assert [r["status"] for r in rows] == ["queued"] * 3
    assert all(r["attempts"] == 1 for r in rows)
    assert "CUDA error" in o.last_error
    # the retry renders the same take with the same seeds (the engine reuses
    # its finished steps) and its prompts, without asking the AI again
    o._render_shot(db.get("s01_001", pid))
    assert len(ai.calls) == 1
    first, second = calls["chain"]
    assert (second["seeds"], second["chain_id"], second["prompts"]) == \
        (first["seeds"], first["chain_id"], first["prompts"])
    # the tightest budget among the take's shots (standard: 2) is spent -> review
    assert [db.get(s, pid)["status"] for s in SIDS[:3]] == ["review"] * 3


def test_a_retake_inside_a_take_renders_the_take_again_with_only_its_seed_moved(env):
    import chain_engine
    o, db, pid, ai, calls = env
    _render(o, db, pid)
    first = calls["chain"][0]
    for sid in SIDS[:3]:
        db.set_status(sid, "passed", project=pid)
    # Re-render on the Shots page (or a QA retake): the next seed, for that shot only
    row = db.get("s01_002", pid)
    db.set_status("s01_002", "queued", seed=row["seed"] + REROLL_SEED, project=pid)
    o._render_shot(db.get("s01_002", pid))
    second = calls["chain"][1]
    # the same chain id, start and prompts: the engine reuses s01_001's steps and
    # carries its latents into the retaken shot
    assert (second["chain_id"], second["start_image"], second["prompts"]) == \
        (first["chain_id"], first["start_image"], first["prompts"])
    assert second["seeds"] == [first["seeds"][0], first["seeds"][1] + REROLL_SEED, first["seeds"][2]]
    assert len(ai.calls) == 1                                  # its prompts were kept
    # the retake step never lands on a seed the engine's seam re-roll tries
    assert REROLL_SEED % chain_engine.REROLL_SEED_OFFSET
    # every shot is delivered again: the shot before ends in the joined decode
    assert [db.get(s, pid)["status"] for s in SIDS[:3]] == ["post"] * 3
    assert len(calls["split"][2]) == 3


def test_a_failed_retake_leaves_the_rendered_shots_of_the_take_as_they_were(env):
    o, db, pid, _, calls = env
    _render(o, db, pid)
    for sid in SIDS[:3]:
        db.set_status(sid, "passed", project=pid)
    calls["rec"] = {"status": "failed", "error": "decode: CUDA error"}
    db.set_status("s01_003", "queued", project=pid)
    o._render_shot(db.get("s01_003", pid))
    rows = [db.get(s, pid) for s in SIDS[:3]]
    assert [r["status"] for r in rows] == ["passed", "passed", "queued"]
    assert [r["attempts"] for r in rows] == [0, 0, 1]


def test_a_retake_waiting_for_an_earlier_prompt_keeps_the_rendered_shots(env, monkeypatch):
    o, db, pid, _, calls = env
    _render(o, db, pid)
    for sid in SIDS[:3]:
        db.set_status(sid, "passed", project=pid)
    db.delete_prompt("s01_001", pid)

    class NoAI(FakeLLM):
        def config_ok(self):
            return False
    monkeypatch.setattr(llm, "LLMClient", lambda cfg=None: NoAI())
    db.set_status("s01_003", "queued", project=pid)
    o._render_shot(db.get("s01_003", pid))
    assert len(calls["chain"]) == 1
    assert [db.get(s, pid)["status"] for s in SIDS[:3]] == ["passed", "passed", mp.WAITING]
    assert "in one take with s01_001" in db.get("s01_003", pid)["error"]
    # the director's prompts stage sees the prompt the take waits for
    assert "s01_001" in mp.needing(db, Bible(pid), pid, o.cfg)
    text = mp.parse_reply(take_reply(), SIDS[:3])[0]["s01_001"]
    assert mp.save_hand(db, Bible(pid), pid, "s01_001", text, cfg=o.cfg)["ok"]
    assert db.get("s01_003", pid)["status"] == "queued"


def test_a_transient_failure_retries_with_its_seeds_and_no_attempt(env, monkeypatch):
    o, db, pid, _, calls = env
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    done = calls["rec"]
    calls["rec"] = {"status": "failed", "error": "[WinError 10061] connection refused"}
    _render(o, db, pid)
    assert [(db.get(s, pid)["status"], db.get(s, pid)["attempts"]) for s in SIDS[:3]] == [("queued", 0)] * 3
    calls["rec"] = done
    o._render_shot(db.get("s01_001", pid))
    assert calls["chain"][1]["seed"] == calls["chain"][0]["seed"]
    assert db.get("s01_002", pid)["status"] == "post"


def test_a_shot_rendered_again_renders_the_continue_after_it_again(env, sandbox_root):
    o, db, pid, ai, calls = env
    ProjectStore(db).update(pid, seamless_takes=0)
    for sid in SIDS[:3]:
        db.set_status(sid, "passed", project=pid)
    ai.replies = [take_reply(["s01_001"])]
    _render(o, db, pid, "s01_001")
    assert db.get("s01_002", pid)["status"] == "queued"
    assert "s01_001, the shot it follows on from, was rendered again" in db.get("s01_002", pid)["error"]
    assert db.get("s01_003", pid)["status"] == "passed"     # it continues s01_002, still as it was


def test_a_wan_render_drops_the_record_of_an_earlier_ltx_render(env, monkeypatch):
    # the QA and the retakes read it: a Wan shot is judged as a Wan shot
    import orchestrator as orch_mod
    o, db, pid, _, _ = env
    seen: dict = {}

    class Stop(Exception):
        pass

    def assign_seed(sj):
        seen.update(sj)
        raise Stop
    monkeypatch.setattr(o, "_video_family", lambda shot, sj, pid: "")
    monkeypatch.setattr(db, "assign_seed", assign_seed)
    row = db.get("s01_001", pid)
    row["json"] = {**row["json"], "applied_model": MID, "applied_references": [], "take": {"id": "x"}}
    with pytest.raises(Stop):
        o._render_shot(row)
    assert set(seen) == set(row["json"]) - set(orch_mod._MOVIE_RECORD) and "scene" in seen


def test_next_shot_follows_story_order_and_a_continue_waits_for_its_shot(env):
    o, db, pid, _, _ = env
    assert o._next_shot()["shot_id"] == "s01_001"
    db.set_status("s01_001", "rendering", project=pid)
    # s01_002 continues s01_001, which has not rendered: the CUT after them goes first
    assert o._next_shot()["shot_id"] == "s01_004"
    db.set_status("s01_004", "failed", project=pid)
    db.set_status("s01_001", "passed", project=pid)
    assert o._next_shot()["shot_id"] == "s01_002"            # queued before failed


def test_a_match_cut_waits_for_the_shot_before_unless_it_is_in_another_place(env):
    # its opening frame is made from that shot's last frame
    o, db, pid, _, _ = env

    def last(**over):
        db.upsert_shots([shot_rows(pid, s01_004={"continuity": "MATCH_ACTION", **over})[3]])
    last()
    for sid in SIDS[:3]:
        db.set_status(sid, "rendering", project=pid)
    assert o._next_shot() is None
    last(location="street")
    assert o._next_shot()["shot_id"] == "s01_004"            # a cut: it does not wait
    last()
    db.set_status("s01_004", "queued", project=pid)
    db.set_status("s01_003", "review", project=pid)
    assert o._next_shot()["shot_id"] == "s01_004"


def test_wait_take_follows_the_chain_not_a_fixed_deadline(env, monkeypatch):
    o, _, _, _, calls = env
    final = calls["rec"]["final_video"]
    states = iter([{"status": "linking"}, {"status": "refining"},
                   {"status": "completed", "final_video": final}])
    monkeypatch.setattr(video_studio, "chain_running", lambda _cid: True)
    monkeypatch.setattr(video_studio, "get_chain", lambda _cid: next(states))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    assert o._wait_take("ab" * 8, timeout_s=3600)["final_video"] == final
    # a record nobody renders any more (left by an earlier process) fails at once
    monkeypatch.setattr(video_studio, "chain_running", lambda _cid: False)
    monkeypatch.setattr(video_studio, "get_chain", lambda _cid: {"status": "running"})
    with pytest.raises(RuntimeError, match="not rendering"):
        o._wait_take("ab" * 8, timeout_s=3600)


def test_takes_follow_the_project_switch_and_the_seamless_engine(env, monkeypatch):
    o, db, pid, _, _ = env
    assert [u.sids for u in mp.plan_units(db, pid, o.cfg)][0] == SIDS[:3]
    ProjectStore(db).update(pid, seamless_takes=0)
    assert [u.sids for u in mp.plan_units(db, pid, o.cfg)][0] == ["s01_001"]
    ProjectStore(db).update(pid, seamless_takes=1)
    # with the engine off, chains fall back to the tail-frame continuation and
    # deliver no seams to cut at: shots render alone
    monkeypatch.setattr(video_studio, "load_config", lambda: {"video_studio": {"seamless_chains": False}})
    assert video_studio.seamless_engine(MID) is None
    assert [u.sids for u in mp.plan_units(db, pid, o.cfg)][0] == ["s01_001"]


def test_a_qa_retake_keeps_the_written_prompt_and_takes_a_new_seed(env, monkeypatch):
    import post
    import qa_director
    o, db, pid, _, _ = env
    db.set_status("s01_004", "post", seed=1234, project=pid)
    row = db.get("s01_004", pid)
    sjson = {**row["json"], "applied_model": MID}

    class Post:
        def __init__(self, cfg):
            pass

        def process(self, sj, video, metrics, worker=""):
            return video

    class QA:
        def __init__(self, cfg):
            pass

        def evaluate(self, sj, video, prompt, refs=None):
            return {"passed": False, "motion_fail": True}
    monkeypatch.setattr(post, "PostPipeline", Post)
    monkeypatch.setattr(qa_director, "QADirector", QA)
    monkeypatch.setattr(o, "_project_post_on", lambda p: True)
    o._post_and_qa(row, sjson, Path("clip.mp4"), "the written prompt")
    row = db.get("s01_004", pid)
    assert row["status"] == "queued" and row["seed"] == 1234 + REROLL_SEED and row["qa_stage"] == 1


def test_a_take_shot_keeps_its_faces_and_one_that_lost_the_cast_goes_to_review(env, monkeypatch):
    import post
    import qa_director
    o, db, pid, _, calls = env
    seen = {"Mara": {"n": 4, "sim": 0.44, "age": 29.0}}
    calls["rec"]["cast"] = {"links": [{"link": 1, **seen}, {"link": 2}, {"link": 3, "Mara": {"n": 2}}]}
    _render(o, db, pid)
    takes = [db.get(s, pid)["json"]["take"] for s in SIDS[:3]]
    assert takes[0]["cast"] == seen and "cast" not in takes[1] and takes[2]["cast"] == {"Mara": {"n": 2}}
    row = db.get("s01_002", pid)
    ref = row["json"]["applied_references"][0]["path"]
    assert o._cast_portraits(row["json"]) == [ref]
    assert o._cast_portraits({**row["json"], "applied_model": None}) == []
    asked = []

    class Post:
        def __init__(self, cfg):
            pass

        def process(self, sj, video, metrics, worker=""):
            return video

    class QA:
        def __init__(self, cfg):
            pass

        def evaluate(self, sj, video, prompt, refs=None):
            asked.append(refs)
            return {"passed": False, "face": 0.12, "face_fail": True, "face_min": 0.25}
    monkeypatch.setattr(post, "PostPipeline", Post)
    monkeypatch.setattr(qa_director, "QADirector", QA)
    for post_on in (False, True):
        monkeypatch.setattr(o, "_project_post_on", lambda p, on=post_on: on)
        db.set_status("s01_002", "post", seed=77, project=pid)
        o._post_and_qa(db.get("s01_002", pid), row["json"], Path("clip.mp4"), "the written prompt")
        r = db.get("s01_002", pid)
        # no retake with a new seed: the shots after it in the take would render again
        assert r["status"] == "review" and r["seed"] == 77 and "0.12" in r["qa_notes"]
    assert asked == [[ref], [ref]]
