import pytest
from unittest.mock import MagicMock, patch
from director import Director, STAGE_FLOW, GATES


def test_director_8_stage_flow_order():
    """Verify the Director executes stages in the exact canonical 8-stage sequence."""
    expected_stages = (
        "story",
        "cast",
        "scenes",
        "shots",
        "keyframes",
        "render",
        "review",
        "assemble",
    )
    assert STAGE_FLOW == expected_stages
    assert GATES == ("story", "keyframes", "review")


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
