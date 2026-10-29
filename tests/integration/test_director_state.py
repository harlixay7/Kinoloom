import pytest
from unittest.mock import MagicMock, patch
from director import Director, STAGE_FLOW, GATES


def test_director_stage_flow_order():
    """Verify the Director executes stages in the canonical sequence: the video
    prompts are written after the shots, before anything renders."""
    expected_stages = (
        "story",
        "cast",
        "scenes",
        "shots",
        "prompts",
        "keyframes",
        "render",
        "review",
        "assemble",
    )
    assert STAGE_FLOW == expected_stages
    assert GATES == ("story", "keyframes", "review")


def _prompt_director(monkeypatch, ai, needing):
    import movie_prompts
    store, writer = MagicMock(), MagicMock()
    writer.run.side_effect = lambda pid: needing.clear() or {"message": "written"}
    director = Director(cfg={}, db=MagicMock(), store=store, writer=writer)
    monkeypatch.setattr(movie_prompts, "ai_ready", lambda cfg=None: ai)
    monkeypatch.setattr(director, "_needing_prompts", lambda pid: list(needing))
    return director, store, writer


def test_the_prompts_stage_has_the_ai_write_them_then_moves_on(monkeypatch):
    director, store, writer = _prompt_director(monkeypatch, True, ["s01_001"])
    assert director._run_stage("p", "prompts", {"id": "p"}) is None
    writer.run.assert_called_once_with("p")
    store.set_status.assert_called_with("p", "running", stage="keyframes")


def test_without_an_ai_the_project_waits_for_its_prompts_in_every_mode(monkeypatch):
    director, store, writer = _prompt_director(monkeypatch, False, ["s01_001", "s01_002"])
    store.mode.return_value = "autopilot"
    assert director._run_stage("p", "prompts", {"id": "p"}) == "gate"
    writer.run.assert_not_called()
    args, kw = store.set_status.call_args
    assert args == ("p", "gate_pending") and kw["stage"] == "prompts"
    assert "2 shot(s) need their video prompt (s01_001, s01_002)" in kw["error"]


def test_the_shots_with_a_prompt_render_while_the_others_wait_for_theirs(monkeypatch):
    director, store, writer = _prompt_director(monkeypatch, False, ["s01_002"])
    director.db.all.return_value = [{"shot_id": "s01_001", "status": "queued"},
                                    {"shot_id": "s01_002", "status": "queued"}]
    assert director._run_stage("p", "prompts", {"id": "p"}) is None
    store.set_status.assert_called_with("p", "running", stage="keyframes")


def test_an_ai_writer_that_does_not_answer_is_named_when_no_shot_can_render(monkeypatch):
    director, store, writer = _prompt_director(monkeypatch, True, ["s01_001", "s01_002"])
    director.cfg = {"architect": {"base_url": "http://127.0.0.1:11434/v1"}}
    writer.run.side_effect = lambda pid: {"message": "did not answer", "unreachable": True}
    director.db.all.return_value = [{"shot_id": "s01_001", "status": "queued"},
                                    {"shot_id": "s01_002", "status": "prompt"},
                                    {"shot_id": "s01_000", "status": "passed"}]
    assert director._run_stage("p", "prompts", {"id": "p"}) == "gate"
    args, kw = store.set_status.call_args
    assert args == ("p", "gate_pending") and kw["stage"] == "prompts"
    assert "the AI writer at http://127.0.0.1:11434/v1 did not answer" in kw["error"]


@pytest.mark.parametrize("stage, needing, done", [
    ("shots", ["s1"], False), ("shots", [], True),
    ("render", ["s1"], True),                 # moved on: the render stage waits for them
    ("paused", ["s1"], False),
])
def test_the_prompts_stage_is_done_once_written_or_passed(monkeypatch, stage, needing, done):
    director, _, _ = _prompt_director(monkeypatch, True, needing)
    assert director._stage_done("p", "prompts", {"id": "p", "stage": stage}) is done


def test_the_render_stage_stops_when_only_shots_waiting_for_prompts_are_left():
    store, db, orch = MagicMock(), MagicMock(), MagicMock()
    db.all.return_value = [{"shot_id": "s1", "status": "passed"},
                           {"shot_id": "s2", "status": "prompt", "error": "needs its prompt: no prompt yet"}]
    director = Director(cfg={}, db=db, store=store, orch=orch, writer=MagicMock())
    assert director._stage_render("p", {"id": "p"}) == "gate"
    orch.stop.assert_called_once()
    args, kw = store.set_status.call_args
    assert args == ("p", "gate_pending") and kw["stage"] == "prompts" and "(s2)" in kw["error"]
    assert kw["error"].startswith("1 of 2 shots are rendered.")


def _story_rows(db, pid, shots):
    """Store shots (id, scene, continuity, place, status, error) with the shot
    before each in its scene as prev_shot, the way a story stores them."""
    rows, prev = [], {}
    for sid, scene, mode, place, status, *err in shots:
        j = {"shot_id": sid, "scene": scene, "continuity": mode, "location": place, "project_id": pid}
        if prev.get(scene):
            j["prev_shot"] = prev[scene]
        prev[scene] = sid
        rows.append(j)
    db.upsert_shots(rows)
    for sid, scene, mode, place, status, *err in shots:
        db.set_status(sid, status, project=pid, error=err[0] if err else "")


def _render_director(isolated_db, monkeypatch, ai=False, cfg=None):
    import movie_prompts
    store, orch = MagicMock(), MagicMock()
    monkeypatch.setattr(movie_prompts, "ai_ready", lambda cfg=None: ai)
    return Director(cfg=cfg or {}, db=isolated_db, store=store, orch=orch, writer=MagicMock()), store, orch


def test_the_render_stops_where_the_pasted_prompts_end(isolated_db, monkeypatch):
    # three prompts pasted from a chat AI: the first take renders, the next one
    # waits for its prompts, and the shots that open on it stay pending behind
    # it. Production stopped nowhere: it read "rendering" with nothing to do.
    _story_rows(isolated_db, "p", [
        ("s01_001", "s01", "CUT", "palace", "passed"),
        ("s01_002", "s01", "CONTINUE", "palace", "passed"),
        ("s01_003", "s01", "CONTINUE", "palace", "passed"),
        ("s01_004", "s01", "MATCH_ACTION", "palace", "prompt", "needs its prompt: no prompt yet"),
        ("s01_005", "s01", "MATCH_ACTION", "palace", "pending"),
        ("s01_006", "s01", "CONTINUE", "palace", "pending")])
    director, store, orch = _render_director(isolated_db, monkeypatch)
    assert director._stage_render("p", {"id": "p"}) == "gate"
    orch.stop.assert_called_once()            # a prompt pasted now renders on Continue
    args, kw = store.set_status.call_args
    assert args == ("p", "gate_pending") and kw["stage"] == "prompts"
    assert kw["error"].startswith("3 of 6 shots are rendered. 1 shot(s) need their video prompt "
                                  "(s01_004), and 2 more open on them: paste them from any chat AI "
                                  "on the Shots page (Use a chat AI)")
    assert "then press Continue production" in kw["error"]


def test_a_shot_held_behind_one_that_renders_keeps_the_render_going(isolated_db, monkeypatch):
    _story_rows(isolated_db, "p", [
        ("s01_001", "s01", "CUT", "palace", "rendering"),
        ("s01_002", "s01", "CONTINUE", "palace", "pending"),
        ("s02_001", "s02", "CUT", "harbor", "prompt", "needs its prompt: no prompt yet")])
    director, _, _ = _render_director(isolated_db, monkeypatch)
    assert director._held_by_prompts(isolated_db.all(project="p")) is None


def test_a_cut_after_a_waiting_shot_can_still_render(isolated_db, monkeypatch):
    # a match cut to another place, and the next scene's first shot, open on
    # their own: they render while the shot before them waits for its prompt
    for nxt in (("s01_002", "s01", "MATCH_ACTION", "harbor", "pending"),
                ("s02_001", "s02", "CONTINUE", "palace", "pending")):
        _story_rows(isolated_db, "p", [
            ("s01_001", "s01", "CUT", "palace", "prompt", "needs its prompt: no prompt yet"), nxt])
        director, _, _ = _render_director(isolated_db, monkeypatch)
        assert director._held_by_prompts(isolated_db.all(project="p")) is None
        isolated_db.con.execute("DELETE FROM shots")


def test_an_ai_writer_that_does_not_answer_is_named_when_the_render_stops(isolated_db, monkeypatch):
    import movie_prompts
    base = "http://127.0.0.1:11434/v1"
    _story_rows(isolated_db, "p", [
        ("s01_001", "s01", "CUT", "palace", "passed"),
        ("s01_002", "s01", "MATCH_ACTION", "palace", "prompt",
         f"needs its prompt: {movie_prompts.UNREACHED} (the AI endpoint did not answer)"),
        ("s01_003", "s01", "CONTINUE", "palace", "pending")])
    director, store, _ = _render_director(isolated_db, monkeypatch, ai=True,
                                          cfg={"architect": {"base_url": base}})
    assert director._stage_render("p", {"id": "p"}) == "gate"
    err = store.set_status.call_args.kwargs["error"]
    assert f"the AI writer at {base} did not answer: start it or pick another one under AI writer, " \
           "or paste them from any chat AI" in err


def test_an_ai_that_could_not_write_a_take_names_the_shot_and_why(isolated_db, monkeypatch):
    _story_rows(isolated_db, "p", [
        ("s01_001", "s01", "CUT", "palace", "prompt",
         "needs its prompt: the AI could not write it (the AI replied with nothing)")])
    director, store, _ = _render_director(isolated_db, monkeypatch, ai=True)
    assert director._stage_render("p", {"id": "p"}) == "gate"
    err = store.set_status.call_args.kwargs["error"]
    assert "s01_001: the AI could not write it (the AI replied with nothing): paste them" in err


def test_director_gate_pending_in_manual_mode():
    """Manual approval gate pauses execution with gate_pending status."""
    mock_store = MagicMock()
    mock_db = MagicMock()
    director = Director(cfg={}, db=mock_db, store=mock_store)

    pid = "p_test_gate"
    proj = {
        "id": pid,
        "status": "draft",
        "stage": "story",
        "story": {"treatment": "Existing treatment"},
    }

    mock_store.get.return_value = proj
    mock_store.mode.return_value = "manual"
    mock_store.gate.return_value = "manual"

    # Running shots stage under manual gate
    res = director._run_stage(pid, "shots", proj)
    assert res == "gate"
    mock_store.set_status.assert_called_with(pid, "gate_pending", stage="shots")


def test_director_auto_gate_advances_stage():
    """Auto gate completes the stage and advances to next stage without pausing."""
    mock_store = MagicMock()
    mock_db = MagicMock()
    director = Director(cfg={}, db=mock_db, store=mock_store)

    pid = "p_test_auto"
    proj = {
        "id": pid,
        "status": "running",
        "stage": "story",
        "story": {"treatment": "Existing treatment"},
    }

    mock_store.get.return_value = proj
    mock_store.mode.return_value = "autopilot"
    mock_store.gate.return_value = "auto"

    # Running story stage when treatment is already generated
    res = director._run_stage(pid, "story", proj)
    assert res is None
    mock_store.set_status.assert_called_with(pid, "running", stage="cast")


@pytest.mark.parametrize("stage,done", [("review", False), ("assemble", True),
                                        ("film", True)])
def test_review_gate_is_passed_once_approved(stage, done):
    """Approving the review gate stores stage=assemble; the next run must not
    stop at the gate again while flagged shots remain."""
    director = Director(cfg={}, db=MagicMock(), store=MagicMock())
    proj = {"id": "p", "status": "running", "stage": stage}
    assert director._stage_done("p", "review", proj) is done


def test_resume_after_review_approval_reaches_assembly():
    store, db = MagicMock(), MagicMock()
    flagged = [{"shot_id": "s1", "status": "review", "scene": "sc1"}]
    db.all.return_value = flagged
    proj = {"id": "p", "status": "running", "stage": "assemble",
            "story": {"treatment": "t"}}
    store.get.return_value = proj
    store.mode.return_value = "checkpoints"
    store.gate.return_value = "manual"
    director = Director(cfg={}, db=db, store=store)
    with patch.object(director, "_stage_done",
                      side_effect=lambda pid, st, pr: st not in ("review",
                                                                  "assemble")
                      or Director._stage_done(director, pid, st, pr)), \
            patch.object(director, "_stage_assemble",
                         return_value=None) as assemble:
        director._run("p")
    assemble.assert_called_once()
    assert ("p", "gate_pending") not in [c.args[:2] for c in
                                          store.set_status.call_args_list]


def test_director_stop_and_resume():
    """Director stop flags the project as stopped, and resume re-triggers start."""
    mock_store = MagicMock()
    director = Director(cfg={}, store=mock_store)

    pid = "p_stop_resume"
    stop_res = director.stop(pid)
    assert stop_res == {"ok": True}
    mock_store.set_status.assert_called_with(pid, "stopped", stage="paused")

    with patch.object(director, "start", return_value={"ok": True, "running": True}) as mock_start:
        resume_res = director.resume(pid)
        assert resume_res == {"ok": True}
        mock_start.assert_called_once_with(pid)



# ------------------------------------------------ the story and its scenes
BEATS = {"beats": {"beats": [{"scene": "s01"}, {"scene": "s02"}]}}


def _rows_db(rows):
    db = MagicMock()
    db.all.side_effect = lambda project=None, limit=None: list(rows)[:limit] if limit else list(rows)
    return db


def _story_director(monkeypatch, ai, rows=(), story=None, architect=None):
    import movie_prompts
    store = MagicMock()
    proj = {"id": "p", "status": "running", "stage": "story", "story": story or {}}
    store.get.return_value = proj
    store.mode.return_value = "autopilot"
    store.gate.return_value = "auto"
    director = Director(cfg={}, db=_rows_db(rows), store=store, writer=MagicMock(),
                        architect=architect or MagicMock())
    monkeypatch.setattr(movie_prompts, "ai_ready", lambda cfg=None: ai)
    return director, store, proj


def test_without_an_ai_the_story_waits_for_a_chat_ais_reply(monkeypatch):
    director, store, proj = _story_director(monkeypatch, False)
    assert director._run_stage("p", "story", proj) == "gate"
    director.architect.run.assert_not_called()
    args, kw = store.set_status.call_args
    assert args == ("p", "gate_pending") and kw["stage"] == "story"
    assert "copy the story prompt into any chat AI" in kw["error"]


def test_the_ai_writer_writes_the_outline_then_the_story_moves_on(monkeypatch):
    director, store, proj = _story_director(monkeypatch, True)

    def write(pid, outline, scenes, stop):
        proj["story"] = {"treatment": {"logline": "x"}, **BEATS}
        return {"ok": True}
    director.architect.run.side_effect = write
    assert director._run_stage("p", "story", proj) is None
    assert director.architect.run.call_args.kwargs["scenes"] == "none"
    store.set_status.assert_called_with("p", "running", stage="cast")


def test_an_ai_writer_that_does_not_answer_is_named_at_the_story(monkeypatch):
    director, store, proj = _story_director(monkeypatch, True)
    director.architect.run.return_value = {"ok": False, "unreachable": True,
                                           "message": "the AI writer at http://127.0.0.1:11434/v1 did not answer"}
    assert director._run_stage("p", "story", proj) == "gate"
    kw = store.set_status.call_args.kwargs
    assert kw["stage"] == "story" and "11434/v1 did not answer: start it" in kw["error"]


def test_the_ai_writer_writes_the_scenes_that_have_no_shots(monkeypatch):
    rows = [{"shot_id": "s01_001", "scene": "s01", "status": "pending"}]
    director, store, proj = _story_director(monkeypatch, True, rows, {"treatment": "t", **BEATS})
    director.architect.run.return_value = {"ok": True}
    assert director._run_stage("p", "shots", proj) is None
    assert director.architect.run.call_args.kwargs["scenes"] == "missing"
    store.set_status.assert_called_with("p", "running", stage="prompts")


def test_without_an_ai_the_written_scenes_render_and_the_film_waits_for_the_rest(monkeypatch):
    rows = [{"shot_id": "s01_001", "scene": "s01", "status": "passed"}]
    director, store, proj = _story_director(monkeypatch, False, rows, {"treatment": "t", **BEATS})
    assert director._run_stage("p", "shots", proj) is None
    director.architect.run.assert_not_called()
    director.assembler = MagicMock()
    assert director._stage_assemble("p", proj) == "gate"
    director.assembler.assemble_movie.assert_not_called()
    kw = store.set_status.call_args.kwargs
    assert kw["stage"] == "shots" and "1 scene(s) still need their shots (s02)" in kw["error"]


def test_a_story_without_any_shots_waits_for_them(monkeypatch):
    director, store, proj = _story_director(monkeypatch, False, (), {"treatment": "t", **BEATS})
    assert director._run_stage("p", "shots", proj) == "gate"
    kw = store.set_status.call_args.kwargs
    assert kw["stage"] == "shots" and "2 scene(s) need their shots (s01, s02)" in kw["error"]


@pytest.mark.parametrize("stage, rows, done", [
    ("shots", [{"scene": "s01", "status": "pending"}], False),            # s02 has none
    ("shots", [{"scene": "s01", "status": "pending"}, {"scene": "s02", "status": "pending"}], True),
    ("render", [{"scene": "s01", "status": "pending"}], True),            # moved on
])
def test_the_shots_stage_is_done_once_every_scene_has_shots_or_passed(monkeypatch, stage, rows, done):
    director, _, proj = _story_director(monkeypatch, False, rows, {"treatment": "t", **BEATS})
    proj["stage"] = stage
    assert director._stage_done("p", "shots", proj) is done


def test_a_finished_film_runs_again_only_for_new_work(monkeypatch):
    rows = [{"shot_id": "s01_001", "scene": "s01", "status": "passed"},
            {"shot_id": "s02_001", "scene": "s02", "status": "passed"}]
    director, store, proj = _story_director(monkeypatch, False, rows, {"treatment": "t", **BEATS})
    proj.update(status="done", stage="film")
    director._run("p")
    store.set_status.assert_not_called()
    rows.pop()                                                             # s02 lost its shots
    director.db = _rows_db(rows)
    with patch.object(director, "_run_stage", return_value="gate"):
        director._run("p")
    store.set_status.assert_any_call("p", "running", stage="shots")


def test_stopping_the_director_stops_the_ai_writer_too():
    architect = MagicMock()
    director = Director(cfg={}, store=MagicMock(), writer=MagicMock(), architect=architect)
    director.stop("p")
    architect.stop.assert_called_once_with("p")



def test_an_ai_writer_stopped_from_the_story_page_leaves_a_clear_wait(monkeypatch):
    director, store, proj = _story_director(monkeypatch, True)
    director.architect.run.return_value = {"ok": False, "stopped": True, "message": "stopped"}
    assert director._run_stage("p", "story", proj) == "gate"
    kw = store.set_status.call_args.kwargs
    assert kw["stage"] == "story" and "AI writer was stopped" in kw["error"]


def test_start_answers_with_the_reason_when_production_stops_at_once(monkeypatch):
    # pressing Start with every video prompt missing and no AI to write them
    # used to look like nothing happened: the reason is in the answer
    store = MagicMock()
    proj = {"id": "p", "status": "draft", "stage": "story"}
    store.get.side_effect = lambda pid: dict(proj)

    def gate(pid, status, stage=None, **kw):
        proj.update(status=status, stage=stage or proj["stage"], error=kw.get("error", ""))
    store.set_status.side_effect = gate
    director = Director(cfg={}, db=MagicMock(), store=store, writer=MagicMock())
    monkeypatch.setattr(director, "_stage_done", lambda pid, st, pr: st != "prompts")
    monkeypatch.setattr(director, "_run_stage", lambda pid, st, pr: gate(
        pid, "gate_pending", "prompts", error="12 shot(s) need their video prompt") or "gate")
    res = director.start("p", wait=5)
    assert res == {"ok": True, "running": False, "status": "gate_pending", "stage": "prompts",
                   "error": "12 shot(s) need their video prompt"}


def test_start_answers_running_while_production_works(monkeypatch):
    import threading
    release = threading.Event()
    store = MagicMock()
    store.get.return_value = {"id": "p", "status": "draft", "stage": "story"}
    director = Director(cfg={}, db=MagicMock(), store=store, writer=MagicMock())
    monkeypatch.setattr(director, "_stage_done", lambda pid, st, pr: False)
    monkeypatch.setattr(director, "_run_stage", lambda pid, st, pr: release.wait(10) and "gate")
    try:
        assert director.start("p", wait=0.2) == {"ok": True, "running": True}
        assert director.start("p", wait=0.2) == {"ok": True, "running": True}     # already running
    finally:
        release.set()
