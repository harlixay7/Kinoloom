"""The movie pipeline renders a run of CONTINUE shots as one seamless take:
one chain request with every shot's prompt, every shot gets its own cut of the
delivered take, and a failed take goes back to the queue whole with its seeds."""
import queue
import threading
from pathlib import Path

import pytest

import movie_takes
import video_studio
from orchestrator import Orchestrator
from projects import ProjectStore

MID = "ltx25_s6_cq"


class _Bible:
    def __init__(self, pid):
        self.pid = pid

    def characters(self):
        return {"mara": {"name": "Mara", "body": "a woman in her late twenties"}}

    def locations(self):
        return {}


def _orch(db):
    o = Orchestrator.__new__(Orchestrator)
    o.db, o.cfg = db, {"qa": {"max_retries": {"standard": 2, "hero": 3}}}
    o.running = o.paused = False
    o._lock = threading.Lock()
    o._post_queue = queue.Queue()
    o._post_thread = o._thread = None
    o._transient, o.job_callbacks, o.last_error = {}, [], None
    return o


def _shots(db, pid):
    shots: list[dict] = [{"shot_id": "s01_001", "scene": "s01", "location": "hall", "continuity": "CUT", "tier": "standard"},
             {"shot_id": "s01_002", "scene": "s01", "location": "hall", "continuity": "CONTINUE",
              "prev_shot": "s01_001", "tier": "standard"},
             {"shot_id": "s01_003", "scene": "s01", "location": "hall", "continuity": "CONTINUE",
              "prev_shot": "s01_002", "tier": "hero"},
             {"shot_id": "s01_004", "scene": "s01", "location": "hall", "continuity": "CUT",
              "prev_shot": "s01_003", "tier": "standard"}]
    for s in shots:
        s.update({"project_id": pid, "characters": [{"id": "mara", "tier": "main"}]})
    db.upsert_shots(shots)
    db.set_status("s01_001", "queued", project=pid)
    return db.get("s01_001", pid)


@pytest.fixture
def take_env(isolated_db, tmp_path, monkeypatch):
    import bible
    import ltx_refs
    import orchestrator as orch_mod
    store = ProjectStore(isolated_db)
    pid = store.create(title="Takes")["id"]
    head = _shots(isolated_db, pid)
    o = _orch(isolated_db)
    ref = tmp_path / "mara.png"
    ref.write_bytes(b"png")
    monkeypatch.setattr(bible, "Bible", _Bible)
    monkeypatch.setattr(ltx_refs, "resolve_reference_set",
                        lambda shot, b, family=None: ([{"slot": "pic1", "path": str(ref),
                                                        "source": "character:mara"}], []))
    monkeypatch.setattr(o, "_video_family", lambda shot, sjson, p: MID)
    monkeypatch.setattr(o, "_lora_chain", lambda *a, **k: [])
    monkeypatch.setattr(o, "_scene_state", lambda *a, **k: {})
    monkeypatch.setattr(o, "_project_prompt",
                        lambda shot, sj, *a, **k: (f"action of {shot['shot_id']}. it moves", "blurry"))
    monkeypatch.setattr(orch_mod, "ffmpeg_exe", lambda cfg=None: "ffmpeg")
    import continuity
    monkeypatch.setattr(continuity, "extract_tail", lambda clip, sid, pid=None: None)
    calls: dict = {"start": []}
    monkeypatch.setattr(video_studio, "chain_running", lambda cid: False)

    def start_chain(mid, prompts, **kw):
        calls["start"].append({"mid": mid, "prompts": prompts, **kw})
        return {"id": kw["chain_id"]}
    monkeypatch.setattr(video_studio, "start_chain", start_chain)
    take = tmp_path / "take_final.mp4"
    take.write_bytes(b"mp4")
    calls["rec"] = {"status": "completed", "final_video": str(take), "seam_frames": [121, 241],
                    "engine": "seamless-ltx", "qa": {"passed": True}}
    monkeypatch.setattr(video_studio, "get_chain", lambda cid: calls["rec"])

    def split(take_path, seams, outs, ff):
        calls["split"] = (Path(take_path), list(seams), [Path(p) for p in outs])
        for p in outs:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_bytes(b"clip")
        return outs
    monkeypatch.setattr(movie_takes, "split_take", split)
    return o, isolated_db, pid, head, calls


def test_a_continue_run_renders_as_one_take_and_every_shot_gets_its_cut(take_env):
    o, db, pid, head, calls = take_env
    run = o._take_run(head, pid, MID)
    assert [r["shot_id"] for r in run] == ["s01_001", "s01_002", "s01_003"]
    o._render_take(run, pid, MID, 1920, 1088, 121)

    (req,) = calls["start"]
    sids = ["s01_001", "s01_002", "s01_003"]
    assert req["mid"] == MID and req["frames"] == 121 and "steps" not in req
    assert req["chain_id"] == movie_takes.take_id(pid, MID, sids)
    assert req["origin"] == {"project": pid, "shots": sids}
    assert len(req["prompts"]) == 3 and "action of s01_002" in req["prompts"][1]
    assert all(p.startswith("References: Image 1 provides mara") for p in req["prompts"])
    assert req["references"][0]["type"] == "face" and req["references"][0]["description"]
    assert req["start_path"] is None          # the take opens on a CUT: no tail to continue

    take, seams, outs = calls["split"]
    assert seams == [121, 241] and len(outs) == 3
    for i, sid in enumerate(sids):
        row = db.get(sid, pid)
        assert row["status"] == "post" and Path(row["video_path"]) == outs[i]
        assert row["seed"] == req["seed"] + i
        assert row["json"]["take"]["id"] == req["chain_id"] and row["json"]["take"]["index"] == i + 1
        assert row["json"]["applied_model"] == MID
    assert db.get("s01_004", pid)["status"] == "pending"     # the CUT after the take renders alone
    posted = [o._post_queue.get_nowait()[0]["shot_id"] for _ in range(3)]
    assert posted == sids


def test_a_failed_take_requeues_whole_with_its_seeds_then_goes_to_review(take_env):
    o, db, pid, head, calls = take_env
    calls["rec"] = {"status": "failed", "error": "decode_B0: CUDA error"}
    run = o._take_run(head, pid, MID)
    o._render_take(run, pid, MID, 960, 544, 124)
    first_seed = calls["start"][0]["seed"]
    rows = [db.get(s, pid) for s in ("s01_001", "s01_002", "s01_003")]
    assert [r["status"] for r in rows] == ["queued"] * 3
    assert all(r["attempts"] == 1 for r in rows)
    assert "CUDA error" in o.last_error

    # the retry plans the same take and keeps its seeds (the engine reuses its steps)
    head = db.get("s01_001", pid)
    run = o._take_run(head, pid, MID)
    assert len(run) == 3
    o._render_take(run, pid, MID, 960, 544, 124)
    assert calls["start"][1]["seed"] == first_seed
    assert calls["start"][1]["chain_id"] == calls["start"][0]["chain_id"]
    # the tightest budget among the take's shots (standard: 2) is spent -> review
    assert [db.get(s, pid)["status"] for s in ("s01_001", "s01_002", "s01_003")] == ["review"] * 3


def test_a_transient_failure_retries_the_take_with_its_seeds_and_no_attempt(take_env, monkeypatch):
    import time
    o, db, pid, head, calls = take_env
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    done = calls["rec"]
    calls["rec"] = {"status": "failed", "error": "[WinError 10061] connection refused"}
    o._render_take(o._take_run(head, pid, MID), pid, MID, 960, 544, 124)
    rows = [db.get(s, pid) for s in ("s01_001", "s01_002", "s01_003")]
    assert [(r["status"], r["attempts"]) for r in rows] == [("queued", 0)] * 3
    # the take's own shots now count as started in assign_seed: the retry must
    # not take a new seed, or the engine re-renders every finished step
    calls["rec"] = done
    o._render_take(o._take_run(db.get("s01_001", pid), pid, MID), pid, MID, 960, 544, 124)
    assert calls["start"][1]["seed"] == calls["start"][0]["seed"]
    assert db.get("s01_002", pid)["status"] == "post"


def test_wait_take_follows_the_chain_not_a_fixed_deadline(take_env, monkeypatch):
    import time
    o, _, _, _, calls = take_env
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


def test_render_shot_hands_a_continue_run_to_the_take_path(take_env, monkeypatch):
    from core import load_config
    o, db, pid, head, _ = take_env
    o.cfg = {**load_config(), "qa": o.cfg["qa"]}
    seen = {}
    monkeypatch.setattr(o, "_render_take", lambda run, p, mid, w, h, frames: seen.update(
        run=[r["shot_id"] for r in run], mid=mid, size=(w, h), frames=frames))
    o._render_shot(head)
    assert seen == {"run": ["s01_001", "s01_002", "s01_003"], "mid": MID, "size": (1920, 1088), "frames": 121}


def test_the_project_switch_and_a_non_seamless_model_render_shots_alone(take_env):
    o, db, pid, head, _ = take_env
    ProjectStore(db).update(pid, seamless_takes=0)
    assert [r["shot_id"] for r in o._take_run(head, pid, MID)] == ["s01_001"]
    ProjectStore(db).update(pid, seamless_takes=1)
    assert [r["shot_id"] for r in o._take_run(head, pid, "ltx25_w4a8_refs")] == ["s01_001"]
    o.cfg["movie"] = {"seamless_takes": False}
    assert [r["shot_id"] for r in o._take_run(head, pid, MID)] == ["s01_001"]


def test_takes_follow_the_seamless_chains_switch(take_env, monkeypatch):
    # with the engine off, chains fall back to the tail-frame continuation and
    # deliver no seams to cut at: shots render alone
    o, _, pid, head, _ = take_env
    assert len(o._take_run(head, pid, MID)) == 3
    monkeypatch.setattr(video_studio, "load_config", lambda: {"video_studio": {"seamless_chains": False}})
    assert video_studio.seamless_engine(MID) is None
    assert [r["shot_id"] for r in o._take_run(head, pid, MID)] == ["s01_001"]
