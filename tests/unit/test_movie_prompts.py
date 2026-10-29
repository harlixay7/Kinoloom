"""The movie's video prompts (movie_prompts): which shots render together, what
each prompt is written for, the brief an AI writes from, reading its reply,
writing with an AI (with and without tools), and hand or pasted prompts."""
import time

import pytest

import movie_prompts as mp
import prompt_check
from moviekit import H3, MID, SIDS, Bible, FakeLLM, shot_rows, take_reply, tool_call

CONT = prompt_check._CONT_LINE["ltx"]


def _units(db, pid, cfg):
    return [u.sids for u in mp.plan_units(db, pid, cfg)]


def _req(db, pid, cfg, sid=SIDS[0], **kw):
    unit = mp.unit_for(db, pid, sid, cfg)
    return mp.request_for(db, Bible(pid), unit, cfg=cfg, **kw)


def _write(db, pid, cfg, client, sid=SIDS[0], **kw):
    from projects import ProjectStore
    unit = mp.unit_for(db, pid, sid, cfg)
    return mp.write_unit(db, Bible(pid), ProjectStore(db).get(pid), unit, client=client, cfg=cfg, **kw)


# ------------------------------------------------------------------- units
def test_a_run_of_continue_shots_is_one_take_whatever_its_status(movie):
    db, pid, cfg, _ = movie
    assert _units(db, pid, cfg) == [SIDS[:3], SIDS[3:]]
    db.set_status("s01_001", "passed", project=pid)
    assert _units(db, pid, cfg) == [SIDS[:3], SIDS[3:]]


@pytest.mark.parametrize("change", [
    {"continuity": "MATCH_ACTION"},          # a cut to a new angle: an edit
    {"continuity": "CUT"},
    {"location": "street"},
    {"scene": "s02"},
    {"t2v": True},
])
def test_a_take_ends_at_anything_but_a_continue_in_the_same_place(movie, change):
    db, pid, cfg, _ = movie
    db.upsert_shots(shot_rows(pid, s01_002=change))
    assert _units(db, pid, cfg)[0] == ["s01_001"]


def test_a_continuity_change_names_the_rendered_shots_its_take_renders_again(movie):
    import movie_takes
    db, pid, cfg, _ = movie
    tid = movie_takes.take_id(pid, MID, SIDS[:3])
    for s in SIDS:
        row = db.get(s, pid)
        db.set_json(s, {**row["json"], **({"take": {"id": tid}} if s != "s01_004" else {})}, project=pid)
        db.set_status(s, "passed", project=pid)

    def after(sid, **change):
        return mp.rerenders(db, pid, sid, {**db.get(sid, pid)["json"], **change}, cfg)
    # the CUT after the take joins it: the take renders anew, four shots long
    assert after("s01_004", continuity="CONTINUE") == SIDS[:3]
    # a cut inside the take: the shot after it goes on in a new take
    assert after("s01_002", continuity="CUT") == ["s01_003"]
    # the same take: the shots after the changed one render anew
    assert after("s01_002", continuity="CONTINUE") == ["s01_003"]
    assert after("s01_003", continuity="CONTINUE") == []
    assert after("s01_004", continuity="CUT") == []               # a shot of its own
    db.set_status("s01_003", "queued", project=pid)
    assert after("s01_004", continuity="CONTINUE") == SIDS[:2]    # a shot without its clip is not named
    assert db.get("s01_004", pid)["json"]["continuity"] == "CUT"   # nothing is written


def test_a_take_stays_on_one_model_ends_at_a_fork_and_is_capped(movie):
    db, pid, cfg, models = movie
    models["s01_003"] = "ltx25_w4a8_refs"
    assert _units(db, pid, cfg)[0] == ["s01_001", "s01_002"]
    models.clear()
    fork = shot_rows(pid)[1] | {"shot_id": "s01_002b"}
    db.upsert_shots([fork])
    assert _units(db, pid, cfg)[0] == ["s01_001"]
    db.delete_shot("s01_002b", project=pid)
    assert _units(db, pid, {**cfg, "movie": {"seamless_takes": False}}) == [[s] for s in SIDS]
    # each model's take length: the H3 setting caps H3 takes only
    capped = {**cfg, "movie": {"h3_take_max_shots": 2}}
    assert _units(db, pid, capped)[0] == SIDS[:3]
    models.update({s: H3 for s in SIDS})
    assert _units(db, pid, capped)[:2] == [SIDS[:2], ["s01_003"]]


def test_a_long_continue_run_goes_on_in_takes_of_the_take_length(movie):
    db, pid, cfg, models = movie
    run = [f"s01_{i:03d}" for i in range(10, 17)]
    rows = shot_rows(pid)[1:2] * len(run)
    db.upsert_shots([r | {"shot_id": sid, "prev_shot": prev} for r, sid, prev
                     in zip(rows, run, ["s01_004", *run[:-1]])])
    # LTX-2.5: takes of 6 (the measured default), then the rest
    assert _units(db, pid, cfg)[-2:] == [["s01_004", *run[:5]], run[5:]]
    assert [u.split for u in mp.plan_units(db, pid, cfg)] == [False, False, True]
    # the settings and the project change it
    assert _units(db, pid, {**cfg, "movie": {"ltx_take_max_shots": 8}})[-1] == ["s01_004", *run]
    from projects import ProjectStore
    store = ProjectStore(db)
    store.update(pid, take_max_shots=3)
    assert _units(db, pid, cfg)[-3:] == [["s01_004", *run[:2]], run[2:5], run[5:]]
    store.update(pid, take_max_shots=0)
    models.update({s: H3 for s in ["s01_004", *run]})
    assert _units(db, pid, cfg)[-2:] == [["s01_004", *run[:3]], run[3:7]]


def test_a_shot_that_is_not_on_ltx_or_h3_is_in_no_unit(movie):
    db, pid, cfg, models = movie
    models["s01_004"] = ""                       # the legacy Wan model
    assert _units(db, pid, cfg) == [SIDS[:3]]
    assert mp.unit_for(db, pid, "s01_004", cfg) is None


def test_a_retake_inside_a_take_renders_the_whole_take(movie):
    db, pid, cfg, _ = movie
    for sid in SIDS[:3]:
        db.set_status(sid, "passed", project=pid)
    db.set_status("s01_002", "queued", project=pid)
    # one piece under one chain id: the shot before it reuses its steps
    for sid in SIDS[:3]:
        assert mp.unit_for(db, pid, sid, cfg).sids == SIDS[:3]
    take = mp.plan_units(db, pid, cfg)[0]
    assert mp.remaining(take).sids == SIDS[:3] and mp.to_render(take) == ["s01_002"]
    db.set_status("s01_003", "failed", project=pid)
    assert mp.to_render(mp.plan_units(db, pid, cfg)[0]) == ["s01_002", "s01_003"]
    for sid in SIDS[:3]:
        db.set_status(sid, "passed", project=pid)
    assert mp.remaining(mp.plan_units(db, pid, cfg)[0]) is None


# ----------------------------------------------------------------- replies
@pytest.mark.parametrize("reply", [
    "Shot s01_001: one.\n\nShot s01_002: two.",
    "**Shot s01_001:** one.\n**Shot s01_002:** two.",
    "## Shot s01_001 (the cut)\none.\n## Shot s01_002 -\ntwo.",
    "Segment 1: one.\nSegment 2: two.",
    '```json\n{"shots": {"s01_001": "one.", "s01_002": "two."}}\n```',
    '[{"shot_id": "s01_001", "prompt": "one."}, {"shot_id": "s01_002", "prompt": "two."}]',
])
def test_a_reply_is_read_in_every_format_writers_use(reply):
    got, images = mp.parse_reply(reply, ["s01_001", "s01_002"])
    assert got == {"s01_001": "one.", "s01_002": "two."} and images == {}


def test_a_reply_brings_the_opening_image_for_its_shot():
    got, images = mp.parse_reply("Opening image: a still.\n\nShot s01_001: moving.", ["s01_001"])
    assert got == {"s01_001": "moving."} and images == {"s01_001": "a still."}
    got, images = mp.parse_reply("Opening image (for Shot s01_004): the doors.\nShot s01_004: they open.\n"
                                 "Shot s01_001: walks.", ["s01_001", "s01_004"])
    assert images == {"s01_004": "the doors."} and got["s01_004"] == "they open."
    # one shot: the bare prompt, quotes removed
    assert mp.parse_reply('"Mara walks in."', ["s01_001"])[0] == {"s01_001": "Mara walks in."}
    # an unknown shot id is not a prompt, and a label that names no shot is prose
    assert mp.parse_reply("Shot s09_001: no.", ["s01_001", "s01_002"])[0] == {}
    assert mp.parse_reply("Shot s09_001: no.", ["s01_001"])[0] == {}
    got, _ = mp.parse_reply("Shot s01_001: Close-up.\nShot reverse: her face.", ["s01_001"])
    assert got == {"s01_001": "Close-up.\nShot reverse: her face."}
    assert mp.labelled_ids("Shot s01_001: a\n**Shot s09_002:** b") == ["s01_001", "s09_002"]


# ------------------------------------------------------------- signatures
def test_a_prompt_is_ready_until_what_it_was_written_for_changes(movie):
    db, pid, cfg, _ = movie
    client = FakeLLM([take_reply()])
    assert _write(db, pid, cfg, client)["written"] == SIDS[:3]
    req = _req(db, pid, cfg)
    assert [s["state"] for s in mp.states(db, req)] == ["ready"] * 3
    # what a render records about itself never makes a prompt stale
    db.set_json("s01_002", {**db.get("s01_002", pid)["json"], "applied_model": MID, "dfr": True},
                project=pid)
    assert [s["state"] for s in mp.states(db, _req(db, pid, cfg))] == ["ready"] * 3
    # the shot itself does, and so does the shot a continuation carries on
    db.upsert_shots(shot_rows(pid, s01_001={"primary_action": "Mara runs into the hall"}))
    st = mp.states(db, _req(db, pid, cfg))
    assert [s["state"] for s in st] == ["stale", "stale", "ready"]
    assert "written again" in st[0]["why"]


def test_the_check_fixes_are_applied_and_errors_keep_a_unit_from_rendering(movie):
    db, pid, cfg, _ = movie
    _write(db, pid, cfg, FakeLLM([take_reply()]))
    texts, why = mp.prompts_for(db, _req(db, pid, cfg, planning=False))
    assert why == {} and len(texts) == 3
    # a seamless take says it continues in every later segment
    assert not texts[0].startswith(CONT) and texts[1].startswith(CONT) and texts[2].startswith(CONT)
    db.delete_prompt("s01_002", pid)
    texts, why = mp.prompts_for(db, _req(db, pid, cfg, planning=False))
    assert texts is None and why == {"s01_002": "no prompt yet"}


# ------------------------------------------------------------------- the brief
def test_the_brief_holds_the_story_the_shots_the_cast_and_what_follows(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    db.upsert_shots(shot_rows(pid, s01_001={"end_state": {"mara": "at the table"},
                                            "dialogue": [{"char": "mara", "line": "Who sent this?"}]}))
    req = _req(db, pid, cfg)
    brief = mp.unit_brief(db, Bible(pid), ProjectStore(db).get(pid), req)
    assert brief.startswith('BRIEF: the video prompts of one seamless take of "The Letter"')
    assert "This is the first shot of the movie." in brief
    assert "Scene s01" in brief and "the great hall" in brief and "torchlight" in brief
    assert "Segment 2 = Shot s01_002" in brief and "lifts a letter" in brief
    assert 'Mara says: "Who sent this?"' in brief and "It must end on: mara: at the table" in brief
    assert "WHAT COMES NEXT" in brief and "The doors of the hall swing open" in brief
    assert "Mara" in brief.split("THE SHOTS TO WRITE")[1]           # the cast sheet
    assert brief.rstrip().endswith("Shot s01_003: ...")
    # the next unit is told where the take left off, with the end of its prompt
    # (s01_001 speaks its line: a prompt that leaves out a shot's dialogue goes back to the AI)
    _write(db, pid, cfg, FakeLLM([take_reply().replace("action of s01_001.", 'action of s01_001: "Who sent this?"')]))
    nxt = mp.unit_brief(db, Bible(pid), ProjectStore(db).get(pid), _req(db, pid, cfg, "s01_004"))
    assert "Earlier in this scene:" in nxt and "s01_003 [CONTINUE, medium]" in nxt
    assert 'The prompt of the shot before (s01_003) ended: "...' in nxt
    assert "A cut: a new shot" in nxt and "WHAT COMES NEXT" not in nxt


def test_a_match_cut_is_written_as_a_new_angle_on_the_running_action(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    db.upsert_shots(shot_rows(pid, s01_004={"continuity": "MATCH_ACTION"}))
    req = _req(db, pid, cfg, "s01_004")
    assert req.opening == "match_action" and req.continuation is None and req.start_image is None
    brief = mp.unit_brief(db, Bible(pid), ProjectStore(db).get(pid), req)
    assert "cuts to a new angle in the middle of the action" in brief


# ------------------------------------------------------------- writing with an AI
def test_an_ai_writes_a_take_and_its_errors_go_back_to_it(movie):
    db, pid, cfg, _ = movie
    client = FakeLLM([take_reply(SIDS[:2]), take_reply()])
    res = _write(db, pid, cfg, client)
    assert res["ok"] and res["written"] == SIDS[:3]
    assert len(client.calls) == 2
    feedback = client.calls[1][-1]["content"]
    assert "Shot s01_003: the prompt is missing." in feedback
    stored = db.prompts(pid)
    assert {stored[s]["source"] for s in SIDS[:3]} == {"ai"}
    assert stored["s01_002"]["unit"] == SIDS[:3] and stored["s01_002"]["model"] == MID
    assert client.released == 1                      # a local model is asked to unload
    # ready prompts are not written again; `force` writes them again
    assert _write(db, pid, cfg, FakeLLM())["written"] == []
    assert _write(db, pid, cfg, FakeLLM([take_reply()]), force=True)["written"] == SIDS[:3]


def test_an_ai_that_cannot_fix_its_prompts_leaves_them_unwritten(movie):
    db, pid, cfg, _ = movie
    res = _write(db, pid, cfg, FakeLLM([take_reply(SIDS[:1])] * 3))
    assert not res["ok"] and "s01_002" in res["error"]
    assert db.prompts(pid) == {}


def test_only_the_prompts_that_need_it_are_written_around_the_kept_ones(movie):
    db, pid, cfg, _ = movie
    _write(db, pid, cfg, FakeLLM([take_reply()]))
    hand = "Medium shot. Mara reads the letter by the torch; it crackles. Diegetic audio: paper rustles."
    assert mp.save_hand(db, Bible(pid), pid, "s01_002", hand, cfg=cfg)["ok"]
    db.upsert_shots(shot_rows(pid, s01_003={"primary_action": "Mara burns the letter"}))
    client = FakeLLM([take_reply(["s01_003"])])
    res = _write(db, pid, cfg, client)
    assert res["written"] == ["s01_003"] and set(res["kept"]) == {"s01_001", "s01_002"}
    brief = client.calls[0][1]["content"]
    assert "(its prompt is written: keep it exactly" in brief and hand in brief
    assert brief.rstrip().endswith("Shot s01_003: ...")
    assert db.prompt("s01_002", pid)["text"].endswith(hand) and db.prompt("s01_002", pid)["source"] == "hand"


def test_an_ai_with_tools_looks_things_up_checks_and_hands_in(movie):
    db, pid, cfg, _ = movie
    good = {s: f"Mara in the torchlit hall, shot {s}. Diegetic audio: a crackling torch." for s in SIDS[:3]}
    client = FakeLLM([tool_call("get_character", {"character_id": "mara"}),
                      tool_call("check_prompts", {"prompts": {"s01_001": ""}}),
                      tool_call("submit_prompts", {"prompts": {"s01_001": good["s01_001"]}}),
                      tool_call("submit_prompts", {"prompts": good})], tools=True)
    res = _write(db, pid, cfg, client)
    assert res["ok"] and res["written"] == SIDS[:3]
    tool_out = [m for m in client.calls[-1] if m["role"] == "tool"]
    assert '"name": "Mara"' in tool_out[0]["content"]
    assert "empty" in tool_out[1]["content"] or "Write a prompt" in tool_out[1]["content"]
    assert '"accepted": false' in tool_out[2]["content"] and "s01_002" in tool_out[2]["content"]
    assert db.prompt("s01_003", pid)["text"].endswith(good["s01_003"])


class _NothingThere(FakeLLM):
    """An endpoint nothing listens on (Ollama not started): refused."""

    def chat(self, messages, tools=None, max_tokens=None, temperature=0.7):
        self.calls.append([dict(m) for m in messages])
        return {"ok": False, "unreachable": True,
                "error": "the AI endpoint did not answer ([WinError 10061] actively refused)"}


def test_an_ai_writer_nothing_answers_for_stops_the_writer_at_once(movie, monkeypatch):
    import llm
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    monkeypatch.setattr(mp, "_DOWN", {})
    down = _NothingThere()
    with pytest.raises(mp.Unreachable):
        _write(db, pid, cfg, down)
    assert len(down.calls) == 1 and down.released == 0      # nothing to unload there
    # for a minute after, the next unit fails at once, without waiting out retries
    with pytest.raises(mp.Unreachable, match="a moment ago"):
        _write(db, pid, cfg, down, sid="s01_004")
    assert len(down.calls) == 1
    # the writer stops at the first unit instead of trying every one
    monkeypatch.setattr(mp, "_DOWN", {})
    monkeypatch.setattr(llm, "LLMClient", lambda cfg=None: down)
    res = mp.PromptWriter(cfg, db, ProjectStore(db)).run(pid)
    assert res["unreachable"] and "https://api.example.test/v1 did not answer" in res["message"]
    assert sorted(res["failed"]) == SIDS[:3] and len(down.calls) == 2
    # a minute later it is asked again, and an answer clears it
    monkeypatch.setattr(mp, "_DOWN", {cfg["architect"]["base_url"]: time.time() - mp.DOWN_S - 1})
    assert _write(db, pid, cfg, FakeLLM([take_reply()]))["ok"] and mp._DOWN == {}


def test_a_refused_connection_is_told_apart_from_a_slow_model(monkeypatch):
    import httpx
    import llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def client_raising(exc):
        class C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise exc
        return C
    ai = llm.LLMClient({"architect": {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "retries": 0}})
    monkeypatch.setattr(httpx, "Client", client_raising(httpx.ConnectError("refused")))
    res = ai.chat([{"role": "user", "content": "hi"}])
    assert not res["ok"] and res["unreachable"]
    monkeypatch.setattr(httpx, "Client", client_raising(httpx.ReadTimeout("slow")))
    res = ai.chat([{"role": "user", "content": "hi"}])
    assert not res["ok"] and not res.get("unreachable")


def test_a_local_ai_waits_while_a_render_holds_the_gpu(movie, monkeypatch):
    import gpu_broker
    import llm as llm_mod
    import time
    db, pid, cfg, _ = movie
    cfg = {**cfg, "architect": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3"}}
    busy = iter([True, True, False])
    monkeypatch.setattr(gpu_broker, "busy", lambda: next(busy))
    monkeypatch.setattr(llm_mod, "reachable", lambda base, probe=None: True)       # it runs
    monkeypatch.setattr(time, "sleep", lambda s: None)
    seen = []
    res = _write(db, pid, cfg, FakeLLM([take_reply()]), waiting=lambda: seen.append(1))
    assert res["ok"] and len(seen) == 2
    # stopped while waiting: nothing is asked
    monkeypatch.setattr(gpu_broker, "busy", lambda: True)
    res = _write(db, pid, cfg, FakeLLM(), force=True, stop=lambda: True)
    assert res == {"ok": False, "error": "stopped"}


# ------------------------------------------------------------ hand and pasted
def test_a_hand_prompt_is_checked_fixed_and_releases_its_waiting_shot(movie):
    db, pid, cfg, _ = movie
    db.set_status("s01_004", mp.WAITING, project=pid)
    res = mp.save_hand(db, Bible(pid), pid, "s01_004", "The doors swing open; wind howls in.", cfg=cfg)
    assert res["ok"] and db.get("s01_004", pid)["status"] == "queued"
    assert db.prompt("s01_004", pid)["source"] == "hand"
    texts, why = mp.prompts_for(db, _req(db, pid, cfg, "s01_004", planning=False))
    # Mara is in the shot: her look, word for word, follows the first sentence
    assert why == {} and texts == ["The doors swing open; wind howls in. Cast, the same age and look in "
                                   "every shot: a woman in her late twenties, auburn hair, green eyes."]
    assert mp.save_hand(db, Bible(pid), pid, "s01_004", "", cfg=cfg) == {"ok": True, "cleared": True}
    assert db.prompt("s01_004", pid) is None


def test_a_hand_prompt_with_an_error_is_refused(movie):
    db, pid, cfg, models = movie
    models.update({s: H3 for s in SIDS})
    res = mp.save_hand(db, Bible(pid), pid, "s01_002", "<Picture 4> waves. Footsteps echo.", cfg=cfg)
    assert not res["ok"] and "Picture 4" in res["error"]
    assert db.prompt("s01_002", pid) is None
    models["s01_004"] = ""
    assert "legacy Wan" in mp.save_hand(db, Bible(pid), pid, "s01_004", "x", cfg=cfg)["error"]


def test_a_stale_hand_prompt_waits_for_its_author_and_never_blocks_the_others(movie):
    db, pid, cfg, _ = movie
    mp.save_hand(db, Bible(pid), pid, "s01_002", "Mara reads it. Paper rustles.", cfg=cfg)
    db.upsert_shots(shot_rows(pid, s01_002={"primary_action": "Mara reads the letter twice"}))
    st = {s["sid"]: s for s in mp.states(db, _req(db, pid, cfg))}
    assert st["s01_002"]["state"] == "stale" and "save it again" in st["s01_002"]["why"]
    res = _write(db, pid, cfg, FakeLLM([take_reply(["s01_001", "s01_003"])]))
    assert res["ok"] and res["written"] == ["s01_001", "s01_003"]
    # kept as saved (with the check's continuity line)
    assert db.prompt("s01_002", pid)["text"] == CONT + "Mara reads it. Paper rustles."
    texts, why = mp.prompts_for(db, _req(db, pid, cfg, planning=False))
    assert texts is None and list(why) == ["s01_002"]


def test_a_chat_brief_and_its_pasted_reply(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    proj = ProjectStore(db).get(pid)
    brief = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    assert brief["units"] == 2 and brief["shots"] == SIDS
    assert "===== PART 1 of 2 =====" in brief["text"] and brief["text"].rstrip().endswith("Shot s01_004: ...")
    reply = take_reply() + "\n\nShot s01_004: The doors open; wind howls.\n\nShot s09_001: nothing."
    res = mp.import_reply(db, Bible(pid), proj, reply, cfg=cfg)
    assert res["ok"] and res["saved"] == SIDS and res["unknown"] == ["s09_001"]
    assert {p["source"] for p in db.prompts(pid).values()} == {"chat"}
    assert mp.chat_brief(db, Bible(pid), proj, cfg=cfg)["units"] == 0
    # one shot of a take is checked with the prompts its take mates have
    res = mp.import_reply(db, Bible(pid), proj, "Shot s01_002: only this one, in silence.", cfg=cfg)
    assert res["ok"] and res["saved"] == ["s01_002"]
    assert db.prompt("s01_001", pid)["text"].startswith("Medium shot")


def test_the_shots_page_view_and_what_still_needs_a_prompt(movie):
    db, pid, cfg, _ = movie
    view = mp.project_view(db, Bible(pid), pid, cfg)
    assert view["counts"] == {"missing": 4} and view["shots"]["s01_002"]["unit"] == SIDS[:3]
    assert view["shots"]["s01_001"]["opening"] == "cut" and view["shots"]["s01_002"]["opening"] == "continue"
    assert (view["shots"]["s01_001"]["start"], view["shots"]["s01_002"]["start"]) == ("", "take")
    _write(db, pid, cfg, FakeLLM([take_reply()]))
    assert mp.needing(db, Bible(pid), pid, cfg) == ["s01_004"]
    db.set_status("s01_004", "passed", project=pid)
    assert mp.needing(db, Bible(pid), pid, cfg) == []


# ------------------------------------------------------------ opening frames
@pytest.fixture
def frames_on(movie, monkeypatch):
    import face_drift
    import opening_frames
    monkeypatch.setattr(opening_frames, "enabled", lambda cfg=None: True)
    monkeypatch.setattr(face_drift, "_APP", False)          # no face model: one frame, not scored
    opening_frames._FAILED.clear()
    return movie


def test_only_a_fresh_opening_without_a_start_image_gets_an_opening_frame(frames_on, sandbox_root):
    import opening_frames as of
    db, pid, cfg, _ = frames_on
    units = mp.plan_units(db, pid, cfg)
    assert of.planned(db, units[0], cfg=cfg) and of.planned(db, units[1], cfg=cfg)
    # written by hand without an opening image: it renders from its references
    assert not of.planned(db, units[1], {"source": "hand", "image": ""}, cfg)
    assert of.planned(db, units[1], {"source": "hand", "image": "the doors, closed"}, cfg)
    # a shot with its own start image
    own = sandbox_root / "output" / "movie_refs" / "own.png"
    own.write_bytes(b"png own")
    db.set_status("s01_004", "pending", ref_image=str(own), project=pid)
    assert not of.planned(db, mp.unit_for(db, pid, "s01_004", cfg), cfg=cfg)
    # a continuation opens on the shot before (a clip of its own: takes off)
    from projects import ProjectStore
    ProjectStore(db).update(pid, seamless_takes=0)
    tail = sandbox_root / "output" / "movie_refs" / "tail.png"
    tail.write_bytes(b"png tail")
    db.set_status("s01_001", "passed", tail_frame=str(tail), project=pid)
    db.set_status("s01_002", "queued", project=pid)
    unit = mp.unit_for(db, pid, "s01_002", cfg)
    assert unit.sids == ["s01_002"] and not of.planned(db, unit, cfg=cfg)


def test_the_ai_writes_the_opening_image_and_the_frame_keeps_the_prompts_ready(frames_on):
    import opening_frames as of
    db, pid, cfg, _ = frames_on
    req = _req(db, pid, cfg)
    assert req.frame_planned and req.start_source == "opening frame"
    client = FakeLLM([take_reply(), take_reply(image="Medium shot of Mara at the hall doors.")])
    assert _write(db, pid, cfg, client)["ok"]
    assert "the opening image's prompt is missing" in client.calls[1][-1]["content"]
    assert "OPENING IMAGE" in client.calls[0][1]["content"]
    assert db.prompt("s01_001", pid)["image"] == "Medium shot of Mara at the hall doors."
    unit = mp.unit_for(db, pid, "s01_001", cfg)
    # not made yet: the render request has no frame and the prompts wait for it
    assert of.frame_for(db, unit, cfg=cfg) is None
    assert mp.prompts_for(db, _req(db, pid, cfg, planning=False))[0] is None
    # made: the prompts written for the planned frame render with it
    path = of.frame_path(db, unit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png frame" * 200)
    final = _req(db, pid, cfg, planning=False)
    assert final.start_source == "opening frame" and not final.frame_planned
    texts, why = mp.prompts_for(db, final)
    assert why == {} and len(texts) == 3


def test_the_frames_are_made_with_qwen_from_the_cast_in_one_lease(frames_on, monkeypatch):
    import gpu_broker
    import opening_frames as of
    db, pid, cfg, _ = frames_on
    db.save_prompt("s01_001", pid, text="x", source="ai", image="Mara at the doors, torchlight.")
    db.save_prompt("s01_004", pid, text="y", source="ai", image="The closed doors.")
    leases = []
    monkeypatch.setattr(gpu_broker, "acquire", lambda **kw: leases.append(kw) or "lease")
    monkeypatch.setattr(gpu_broker, "release", lambda lease: leases.append("released"))

    class Comfy:
        port = 8190
        graphs: list = []

        def __init__(self, port):
            assert port == 8190

        def system_stats(self):
            return {}

        def upload_image(self, p):
            return "up_" + p.name

        def queue_prompt(self, wf):
            Comfy.graphs.append(wf)
            return f"p{len(Comfy.graphs)}"

        def wait_for(self, pid, timeout_s=None):
            return {"status": {"status_str": "success"},
                    "outputs": {"9": {"images": [{"filename": "o.png", "subfolder": ""}]}}}

        def download(self, sub, name, dest):
            dest.write_bytes(b"\x89PNG" + b"0" * 2000)
            return dest
    import renderer
    monkeypatch.setattr(renderer, "ComfyClient", Comfy)
    unit = mp.unit_for(db, pid, "s01_001", cfg)
    assert of.ensure(db, Bible(pid), unit, cfg) == of.frame_path(db, unit)
    # both frames still to make, under one lease, each from the real graph builder
    assert leases[0]["port"] == 8190 and leases[0]["model"] == of.MODEL and leases[-1] == "released"
    assert len(Comfy.graphs) == 2
    text = str(Comfy.graphs[0])
    assert "Mara at the doors, torchlight." in text and "up_mara.png" in text
    assert of.frame_for(db, mp.unit_for(db, pid, "s01_004", cfg), cfg=cfg) is not None
    # made once: the same image prompt is never made again
    assert of.ensure(db, Bible(pid), unit, cfg) is not None and len(Comfy.graphs) == 2


@pytest.fixture
def stills(frames_on, monkeypatch, sandbox_root):
    """Mara's pictures (her portrait, its face close-up, her loadout still)
    and the hall's, as the references of every shot."""
    import face_ref
    import ltx_refs
    db, pid, cfg, _ = frames_on
    d = sandbox_root / "output" / "movie_refs"
    pics = {n: d / f"{n}.png" for n in ("portrait", "face", "loadout", "hall", "tail")}
    for p in pics.values():
        p.write_bytes(b"png " + p.stem.encode())
    monkeypatch.setattr(ltx_refs, "resolve_reference_set", lambda shot, b, family=None: ([
        {"slot": "pic1", "path": str(pics["loadout"]), "source": "character:mara:armed"},
        {"slot": "background", "path": str(pics["hall"]), "source": "location:hall"}], []))
    monkeypatch.setattr(ltx_refs, "character_refs",
                        lambda pid, cid, char, loadout=None, shot=None: [pics["portrait"]])
    monkeypatch.setattr(face_ref, "face_closeup",
                        lambda src, cache, **kw: pics["face"] if src == pics["portrait"] else None)
    return db, pid, cfg, pics


def test_a_frame_is_made_from_the_faces_the_set_and_the_outfit(stills):
    import opening_frames as of
    db, pid, cfg, pics = stills
    unit = mp.unit_for(db, pid, "s01_004", cfg)
    refs = of._refs(db, Bible(pid), unit)
    # one picture per person (the one the video model gets): two of one person drew them twice
    assert refs["images"] == [pics["loadout"], pics["hall"]]
    assert refs["faces"] == {"Mara": pics["face"]} and refs["last"] is None
    assert refs["looks"] == ["a woman in her late twenties, auburn hair, green eyes"]
    # a match cut: the set is the last frame of the shot before, the place goes last
    db.upsert_shots([shot_rows(pid, s01_004={"continuity": "MATCH_ACTION"})[3]])
    db.set_status("s01_003", "passed", tail_frame=str(pics["tail"]), project=pid)
    unit = mp.unit_for(db, pid, "s01_004", cfg)
    assert of.match_opening(db, unit)
    refs = of._refs(db, Bible(pid), unit)
    assert refs["images"] == [pics["loadout"], pics["tail"], pics["hall"]]
    assert of.with_looks("A wide shot of the hall. She turns.", refs["looks"]) == (
        "A wide shot of the hall. The people: a woman in her late twenties, auburn hair, green eyes. She turns.")


class _Upload:
    def upload_image(self, p):
        return "up_" + p.name


def _frames(monkeypatch, scores, available=True):
    """Render candidates as files; their likeness comes from `scores`."""
    import face_drift
    import opening_frames as of
    made = []

    def render(client, unit, prompt, uploaded, seed, m, dest):
        made.append((prompt, uploaded, seed))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png" + bytes(2000))
    it = iter(scores)
    monkeypatch.setattr(of, "_render", render)
    monkeypatch.setattr(face_drift, "available", lambda: available)
    monkeypatch.setattr(face_drift, "likeness", lambda image, cast, **kw: {"Mara": next(it)})
    return made


@pytest.mark.parametrize("scores, best, weak, tried", [
    ([0.6, 0.4], 0.6, False, 2),
    ([0.2, 0.31], 0.31, False, 2),                 # a medium-wide frame of the key-image tests
    ([0.1, 0.2, 0.5, 0.1], 0.5, False, 4),         # under the floor: two more
    ([0.1, 0.2, 0.15, 0.22], 0.22, True, 4),       # still under it: the best, marked weak
])
def test_the_most_alike_candidate_is_kept(stills, monkeypatch, scores, best, weak, tried):
    import opening_frames as of
    db, pid, cfg, _ = stills
    db.save_prompt("s01_004", pid, text="y", source="ai", image="The closed doors. Mara waits.")
    unit = mp.unit_for(db, pid, "s01_004", cfg)
    dest = of.frame_path(db, unit)
    made = _frames(monkeypatch, scores)
    assert of._make_one(_Upload(), db, Bible(pid), unit, dest, "The closed doors. Mara waits.", {}, cfg) == dest
    info = of.frame_info(db, unit)
    assert (info["likeness"], info["weak"], len(info["candidates"])) == (best, weak, tried)
    assert info["people"] == {"Mara": best} and info["min_likeness"] == of.MIN_LIKENESS
    assert len({s for _, _, s in made}) == tried                  # each candidate its own seed
    assert "The people: a woman in her late twenties" in made[0][0]
    assert made[0][1] == ["up_loadout.png", "up_hall.png"]
    mine = sorted(p.name for p in dest.parent.iterdir() if p.name.startswith(dest.stem))
    assert mine == sorted([dest.name, dest.stem + ".json"])          # the other candidates are gone


def test_without_a_face_model_one_frame_is_made_and_not_scored(stills, monkeypatch):
    import opening_frames as of
    db, pid, cfg, _ = stills
    db.save_prompt("s01_004", pid, text="y", source="ai", image="The doors.")
    unit = mp.unit_for(db, pid, "s01_004", cfg)
    made = _frames(monkeypatch, [], available=False)
    of._make_one(_Upload(), db, Bible(pid), unit, of.frame_path(db, unit), "The doors.", {}, cfg)
    info = of.frame_info(db, unit)
    assert len(made) == 1 and (info["scored"], info["likeness"], info["weak"]) == (False, None, False)
    # the settings change how many are tried and the floor
    assert of.candidates({"movie": {"key_image_candidates": 4}}) == 4 and of.candidates({}) == of.CANDIDATES
    assert of.min_likeness({"movie": {"key_image_min_likeness": 0.5}}) == 0.5
    assert of.min_likeness({"movie": {"key_image_min_likeness": "x"}}) == of.MIN_LIKENESS


def test_a_candidate_that_fails_leaves_the_ones_made_before_it(stills, monkeypatch):
    import opening_frames as of
    db, pid, cfg, _ = stills
    db.save_prompt("s01_004", pid, text="y", source="ai", image="The doors.")
    unit = mp.unit_for(db, pid, "s01_004", cfg)
    made = _frames(monkeypatch, [0.5])
    ok = of._render

    def second_fails(client, unit, prompt, uploaded, seed, m, dest):
        if made:
            raise RuntimeError("the image graph failed")
        ok(client, unit, prompt, uploaded, seed, m, dest)
    monkeypatch.setattr(of, "_render", second_fails)
    dest = of.frame_path(db, unit)
    assert of._make_one(_Upload(), db, Bible(pid), unit, dest, "The doors.", {}, cfg) == dest
    assert of.frame_info(db, unit)["likeness"] == 0.5 and len(of.frame_info(db, unit)["candidates"]) == 1


def test_with_the_keyframes_gate_manual_the_frames_are_made_first_and_looked_at(stills, monkeypatch):
    import json
    import opening_frames as of
    from director import Director
    from projects import ProjectStore
    db, pid, cfg, _ = stills
    for sid in ("s01_001", "s01_004"):
        db.save_prompt(sid, pid, text="x", source="ai", image=f"Mara, {sid}.")
    store = ProjectStore(db)
    d = Director(cfg=cfg, db=db, store=store)

    def make(db_, bible, units, cfg_=None):
        for u in units:
            p = of.frame_path(db_, u)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"png" + bytes(2000))
            weak = u.sids[0] == "s01_004"
            p.with_suffix(".json").write_text(json.dumps({"likeness": 0.2 if weak else 0.5, "weak": weak}))
        return []
    monkeypatch.setattr(of, "make", make)
    proj = store.get(pid)
    assert [u.sids[0] for u in of.pending(db, pid, cfg)] == ["s01_001", "s01_004"]
    assert not d._stage_done(pid, "keyframes", proj)
    assert d._stage_keyframes(pid, proj, "manual") == "gate"
    proj = store.get(pid)
    assert (proj["status"], proj["stage"]) == ("gate_pending", "keyframes")
    assert "2 opening frame(s) to look at on the Shots page; 1 look less like the cast" in proj["error"]
    assert "(s01_004)" in proj["error"]
    assert of.pending(db, pid, cfg) == [] and [s for s, _ in of.made(db, pid, cfg)] == ["s01_001", "s01_004"]
    assert not d._stage_done(pid, "keyframes", proj)               # waits for the operator
    of.looked_at(db, pid, cfg)                                      # Continue production
    store.set_status(pid, "running", stage="render")
    assert d._stage_done(pid, "keyframes", store.get(pid)) and of.to_look_at(db, pid, cfg) == []
    # the frames passed stay passed after a Stop and Start; a frame made again does not
    store.set_status(pid, "stopped", stage="paused")
    assert d._stage_done(pid, "keyframes", store.get(pid))
    make(db, Bible(pid), [mp.unit_for(db, pid, "s01_004", cfg)])
    assert not d._stage_done(pid, "keyframes", store.get(pid))
    assert [s for s, _ in of.to_look_at(db, pid, cfg)] == ["s01_004"]
    # on auto they are made right before each render
    store.update(pid, gates={"story": "auto", "keyframes": "auto", "review": "auto"})
    store.set_status(pid, "running", stage="keyframes")
    assert d._stage_done(pid, "keyframes", store.get(pid))


def test_a_match_cut_frame_follows_the_last_frame_of_the_shot_before(stills, monkeypatch):
    import movie_takes
    import opening_frames as of
    db, pid, cfg, pics = stills
    db.upsert_shots([shot_rows(pid, s01_004={"continuity": "MATCH_ACTION"})[3]])
    db.save_prompt("s01_004", pid, text="y", source="ai", image="Mara, closer.")

    def unit():
        return mp.unit_for(db, pid, "s01_004", cfg)
    # the shot before has no last frame yet: nothing to make the frame from
    assert of.waits(db, unit()) and of.pending(db, pid, cfg) == []
    tid = movie_takes.take_id(pid, MID, SIDS[:3])
    for s in SIDS:
        db.set_json(s, {**db.get(s, pid)["json"], **({"take": {"id": tid}} if s != "s01_004" else {})},
                    project=pid)
        db.set_status(s, "passed", project=pid)
    db.set_status("s01_003", "passed", tail_frame=str(pics["tail"]), project=pid)
    first = of.frame_path(db, unit())
    assert not of.waits(db, unit())
    # rendered: the take rendered again renders the match cut made from its last frame again
    last = db.get("s01_003", pid)
    assert [r["shot_id"] for r in mp.follow_ons(db, last, pid, cfg)] == ["s01_004"]
    change = {**db.get("s01_002", pid)["json"], "continuity": "CONTINUE"}
    assert mp.rerenders(db, pid, "s01_002", change, cfg) == ["s01_003", "s01_004"]
    # the shot before is to render again: its frame is about to change, Make again waits
    db.set_status("s01_003", "queued", project=pid)
    asked = []
    monkeypatch.setattr(of, "make", lambda *a, **kw: asked.append(a) or [])
    assert of.waits(db, unit()) and of.again(db, Bible(pid), unit(), cfg) is None and asked == []
    # rendered again with another last frame: the match cut gets another frame
    pics["tail"].write_bytes(b"png the new last frame")
    db.set_status("s01_003", "passed", project=pid)
    assert of.frame_path(db, unit()) != first
    # written without an opening image, it renders from its references and stays
    db.save_prompt("s01_004", pid, text="y", source="hand", image="")
    assert mp.follow_ons(db, last, pid, cfg) == []


def _split_take(db, pid):
    """The take s01_001-003 at a take length of 2: s01_003 goes on in a new take."""
    from projects import ProjectStore
    ProjectStore(db).update(pid, take_max_shots=2)
    return mp.unit_for(db, pid, "s01_003")


def test_a_take_that_goes_on_past_the_take_length_opens_on_a_new_angle(frames_on):
    import opening_frames as of
    db, pid, cfg, _ = frames_on
    unit = _split_take(db, pid)
    assert unit.sids == ["s01_003"] and unit.split
    # planned: a match cut on an opening frame, its prompt written for that
    req = _req(db, pid, cfg, sid="s01_003")
    assert (req.opening, req.start_source, req.frame_planned) == ("match_action", "opening frame", True)
    assert of.planned(db, unit, cfg=cfg)
    view = mp.project_view(db, Bible(pid), pid, cfg)["shots"]["s01_003"]
    assert view["opening"] == "match_action" and view["wants_image"]
    brief = mp.unit_brief(db, Bible(pid), {"id": pid}, req)
    assert "A match cut: the camera cuts to a new angle" in brief and "OPENING IMAGE" in brief
    # a hand prompt written without an opening image continues from the last frame
    mp.save_hand(db, Bible(pid), pid, "s01_003", "Mara crushes the letter. Paper crackles.", cfg=cfg)
    assert _req(db, pid, cfg, sid="s01_003").opening == "continue"
    # its frame failed: the take carries on from the last frame for the hour
    db.delete_prompt("s01_003", pid)
    db.save_prompt("s01_003", pid, text="x", source="ai", image="Mara, closer, the letter in her fist.")
    of._FAILED[of._key(db, unit, "Mara, closer, the letter in her fist.")] = time.time()
    assert _req(db, pid, cfg, sid="s01_003").opening == "continue"


def test_without_opening_frames_a_take_past_the_take_length_carries_on_from_the_last_frame(movie):
    db, pid, cfg, _ = movie
    unit = _split_take(db, pid)
    assert unit.split
    req = _req(db, pid, cfg, sid="s01_003")
    assert (req.opening, req.start_source, req.frame_planned) == ("continue", "previous clip", False)
    # the take before it, not cut by the take length, opens as it always did
    assert not mp.unit_for(db, pid, "s01_001").split


def test_a_frame_that_failed_is_not_planned_for_an_hour(frames_on):
    import time
    import opening_frames as of
    db, pid, cfg, _ = frames_on
    db.save_prompt("s01_004", pid, text="x", source="ai", image="the doors")
    unit = mp.unit_for(db, pid, "s01_004", cfg)
    assert of.planned(db, unit, cfg=cfg)
    of._FAILED[of._key(db, unit, "the doors")] = time.time()
    assert not of.planned(db, unit, cfg=cfg)
    of._FAILED[of._key(db, unit, "the doors")] = time.time() - of._RETRY_S - 1
    assert of.planned(db, unit, cfg=cfg)


# ------------------------------------------------------- chat briefs in batches
def _scene2(pid, n):
    """A second scene of `n` cuts at the street."""
    rows = []
    for k in range(1, n + 1):
        rows.append({"shot_id": f"s02_{k:03d}", "project_id": pid, "scene": "s02", "location": "hall",
                     "continuity": "CUT", "tier": "standard", "shot_size": "wide",
                     "prev_shot": f"s02_{k - 1:03d}" if k > 1 else None,
                     "primary_action": f"Mara crosses the hall, step {k}",
                     "characters": [{"id": "mara", "tier": "main"}]})
    return rows


def _chat_reply(sids, extra=""):
    return "\n\n".join(f"Shot {s}: A wide shot in the torchlit hall. Mara, a woman in her late twenties, "
                         f"auburn hair, green eyes, moves through {s}{extra}. Diegetic audio: a crackling torch."
                         for s in sids)


def _per_reply(cfg, n):
    return {**cfg, "movie": {**(cfg.get("movie") or {}), "prompt_segments_per_reply": n}}


def test_a_chat_brief_is_one_scene_at_a_time_and_up_to_the_prompts_one_reply_writes(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    cfg = _per_reply(cfg, 8)
    db.upsert_shots(_scene2(pid, 10))
    store = ProjectStore(db)
    store.set_story_stage(pid, "beats", {"beats": [
        {"scene": "s01", "goal": "Mara finds the letter", "hook": "the doors open"},
        {"scene": "s02", "goal": "Mara follows the messenger"}]})
    proj = store.get(pid)
    first = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    # s01 is its own batch, although s02 would fit beside it
    assert first["shots"] == SIDS and first["scene"] == "s01" and first["units"] == 2
    assert first["progress"] == {"ready": 0, "total": 14, "batches": 3}
    text = first["text"]
    assert "THE FILM (>> = the scene of these shots)" in text and ">> s01: Mara finds the letter" in text
    assert text.count("BRIEF:") == 1 and "Write the parts in story order" in text
    assert mp.import_reply(db, Bible(pid), proj, take_reply() + "\n\n" + _chat_reply(["s01_004"]),
                           cfg=cfg)["saved"] == SIDS
    # the next batch is made from what was saved: the prompt of the shot before
    nxt = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    assert nxt["shots"] == [f"s02_{k:03d}" for k in range(1, 9)] and nxt["scene"] == "s02"
    assert 'The prompt of the shot before (s01_004) ended: "...' in nxt["text"]
    assert nxt["progress"] == {"ready": 4, "total": 14, "batches": 2}
    # the rules its eight clips share are said once, at the top
    assert nxt["text"].count("HOW LTX-2.5 READS THE PROMPT") == 1 and "RULES OF THE RENDER MODEL" in nxt["text"]
    assert nxt["text"].count("===== PART") == 8
    mp.import_reply(db, Bible(pid), proj, _chat_reply(nxt["shots"]), cfg=cfg)
    last = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    assert last["shots"] == ["s02_009", "s02_010"] and last["progress"]["batches"] == 1
    assert "PART" not in last["text"] or "s02_008" in last["text"]


# ----------------------------------------------------- a take written in parts
def test_a_take_is_split_into_replies_of_even_sizes():
    assert mp.parts(list("abcdef"), 4) == [list("abc"), list("def")]
    assert mp.parts(list("abcde"), 4) == [list("abc"), list("de")]
    assert mp.parts(list("abcdefghij"), 4) == [list("abcd"), list("efg"), list("hij")]
    assert mp.parts(list("abc"), 4) == [list("abc")] and mp.parts([], 4) == []
    assert mp.parts(list("ab"), 1) == [["a"], ["b"]] and mp.parts(list("ab"), 0) == [["a"], ["b"]]
    assert mp.segments_per_reply({}) == mp.SEGMENTS_PER_REPLY == 4
    assert mp.segments_per_reply({"movie": {"prompt_segments_per_reply": 6}}) == 6
    assert mp.segments_per_reply({"movie": {"prompt_segments_per_reply": "x"}}) == 4
    assert mp.segments_per_reply({"movie": {"prompt_segments_per_reply": 999}}) == 50
    assert mp.segments_per_reply({"movie": {"prompt_segments_per_reply": -2}}) == 1


def test_the_ai_writes_a_long_take_part_by_part_with_the_prompts_before_kept(movie):
    db, pid, cfg, _ = movie
    cfg = _per_reply(cfg, 2)
    # part 1 writes all three anyway: the later one is not taken from it
    client = FakeLLM([take_reply().replace("action of s01_003", "early draft of s01_003"),
                      take_reply(["s01_003"])])
    res = _write(db, pid, cfg, client)
    assert res["ok"] and res["written"] == SIDS[:3] and len(client.calls) == 2
    first, second = (c[1]["content"] for c in client.calls)
    assert "This reply writes segments 1 and 2 of 3" in first
    assert "Segment 3 = Shot s01_003 (later: written in the next reply, not now)" in first
    assert "Shot s01_002: ..." in first and "Shot s01_003: ..." not in first
    # part 2: the whole brief again, with the prompts part 1 wrote
    assert "THE STORY SO FAR" in second and "This reply writes segment 3 of 3" in second
    assert "Segment 1 = Shot s01_001 (its prompt is written: keep it exactly" in second
    assert 'Its prompt: "' + db.prompt("s01_001", pid)["text"] in second
    assert second.rstrip().endswith("Shot s01_003: ...") and "Shot s01_001: ..." not in second
    assert "action of s01_003" in db.prompt("s01_003", pid)["text"]
    assert {db.prompt(s, pid)["source"] for s in SIDS[:3]} == {"ai"}
    texts, why = mp.prompts_for(db, _req(db, pid, cfg, planning=False))
    assert why == {} and len(texts) == 3


def test_a_hand_prompt_inside_a_take_written_in_parts_is_kept_and_written_around(movie):
    db, pid, cfg, _ = movie
    assert mp.save_hand(db, Bible(pid), pid, "s01_002", "Mara reads it. Paper rustles.", cfg=cfg)["ok"]
    hand = db.prompt("s01_002", pid)["text"]
    client = FakeLLM([take_reply(["s01_001"]), take_reply(["s01_003"])])
    res = _write(db, pid, _per_reply(cfg, 1), client)
    assert res["ok"] and res["written"] == ["s01_001", "s01_003"] and res["kept"] == ["s01_002"]
    first, second = (c[1]["content"] for c in client.calls)
    assert "This reply writes segment 1 of 3" in first and "Segment 3 = Shot s01_003 (later" in first
    assert "This reply writes segment 3 of 3" in second and "(later" not in second
    assert db.prompt("s01_002", pid)["text"] == hand and db.prompt("s01_002", pid)["source"] == "hand"


def test_a_part_gets_its_own_fixes_and_the_opening_image_belongs_to_the_first(frames_on):
    db, pid, cfg, _ = frames_on
    cfg = _per_reply(cfg, 2)
    client = FakeLLM([take_reply(["s01_001"], image="Mara at the doors."),
                      take_reply(SIDS[:2], image="Mara at the doors."),
                      take_reply(["s01_003"])])
    assert _write(db, pid, cfg, client)["ok"]
    assert "Shot s01_002: the prompt is missing." in client.calls[1][-1]["content"]
    assert "OPENING IMAGE" in client.calls[0][1]["content"] and "OPENING IMAGE" not in client.calls[2][1]["content"]
    assert db.prompt("s01_001", pid)["image"] == "Mara at the doors."


def test_the_whole_take_is_checked_and_only_the_segments_it_names_are_written_again(movie, monkeypatch):
    from prompt_check import Issue
    db, pid, cfg, _ = movie
    cfg = _per_reply(cfg, 2)
    real = mp.story_checks

    def whole_take(sjsons, texts, names, strict=frozenset()):
        out = real(sjsons, texts, names, strict)
        if all(t.strip() for t in texts) and "early" in texts[0]:      # seen only with the whole take
            out.append(Issue("error", "test", "it does not match how the take ends.", 1))
        return out
    monkeypatch.setattr(mp, "story_checks", whole_take)
    client = FakeLLM([take_reply(SIDS[:2]).replace("action of s01_001", "early action"),
                      take_reply(["s01_003"]), take_reply(["s01_001"])])
    res = _write(db, pid, cfg, client)
    assert res["ok"] and len(client.calls) == 3
    fix = client.calls[2][1]["content"]
    assert "This reply writes segment 1 of 3" in fix and fix.rstrip().endswith("Shot s01_001: ...")
    assert "early" not in db.prompt("s01_001", pid)["text"]
    assert "action of s01_003" in db.prompt("s01_003", pid)["text"]


def test_the_tools_check_a_part_without_the_segments_written_later(movie):
    import json
    db, pid, cfg, _ = movie
    cfg = _per_reply(cfg, 2)
    got, _ = mp.parse_reply(take_reply(), SIDS[:3])
    client = FakeLLM([tool_call("check_prompts", {"prompts": {s: got[s] for s in SIDS[:2]}}),
                      tool_call("submit_prompts", {"prompts": {s: got[s] for s in SIDS[:2]}}, 1),
                      tool_call("submit_prompts", {"prompts": {"s01_003": got["s01_003"]}}, 2)], tools=True)
    res = _write(db, pid, cfg, client)
    assert res["ok"] and res["written"] == SIDS[:3]
    checked = json.loads(client.calls[1][-1]["content"])
    assert checked["errors"] == []


def test_a_chat_brief_writes_a_long_take_in_parts_and_its_pasted_parts_wait_as_one(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    cfg = _per_reply(cfg, 2)
    proj = ProjectStore(db).get(pid)
    for sid in SIDS[:3]:
        db.set_status(sid, mp.WAITING, project=pid)
    first = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    assert first["shots"] == SIDS[:2] and first["units"] == 1
    assert first["progress"]["batches"] == 2
    text = first["text"]
    assert "(later: written in the next reply, not now)" in text and "Shot s01_003: ..." not in text
    assert "WHAT COMES NEXT" not in text
    res = mp.import_reply(db, Bible(pid), proj, take_reply(SIDS[:2]), cfg=cfg)
    assert res["ok"] and res["saved"] == SIDS[:2] and res["waiting"] == ["s01_003"]
    assert {db.get(s, pid)["status"] for s in SIDS[:3]} == {mp.WAITING}     # the take waits whole
    nxt = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    assert nxt["shots"] == ["s01_003", "s01_004"] and nxt["units"] == 2
    assert nxt["progress"] == {"ready": 2, "total": 4, "batches": 1}
    assert "(its prompt is written: keep it exactly" in nxt["text"]
    res = mp.import_reply(db, Bible(pid), proj, take_reply(["s01_003"]) + "\n\n" + _chat_reply(["s01_004"]),
                          cfg=cfg)
    assert res["saved"] == ["s01_003", "s01_004"] and res["waiting"] == []
    assert {db.get(s, pid)["status"] for s in SIDS[:3]} == {"queued"}
    # one take from its prompt editor: its parts one after another
    redo = mp.chat_brief(db, Bible(pid), proj, sids=SIDS[:3], cfg=cfg)
    assert redo["shots"] == SIDS[:2]


def test_a_chat_brief_holds_one_replys_worth_of_prompts_by_default(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    db.upsert_shots(_scene2(pid, 6))
    proj = ProjectStore(db).get(pid)
    assert mp.chat_brief(db, Bible(pid), proj, cfg=cfg)["shots"] == SIDS
    mp.import_reply(db, Bible(pid), proj, take_reply() + "\n\n" + _chat_reply(["s01_004"]), cfg=cfg)
    nxt = mp.chat_brief(db, Bible(pid), proj, cfg=cfg)
    assert nxt["shots"] == [f"s02_{k:03d}" for k in range(1, 5)] and nxt["progress"]["batches"] == 2
    assert mp.chat_brief(db, Bible(pid), proj, limit=1, cfg=cfg)["shots"] == ["s02_001"]


def test_a_shot_asked_for_by_name_is_written_again(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    proj = ProjectStore(db).get(pid)
    mp.import_reply(db, Bible(pid), proj, _chat_reply(["s01_004"]), cfg=cfg)
    redo = mp.chat_brief(db, Bible(pid), proj, sids=["s01_004"], cfg=cfg)
    assert redo["shots"] == ["s01_004"] and redo["text"].rstrip().endswith("Shot s01_004: ...")
    assert "(its prompt is written: keep it exactly" not in redo["text"]


def test_a_pasted_prompt_keeps_the_shots_dialogue_and_names_no_one_else(movie):
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    db.upsert_shots(shot_rows(pid, s01_004={"dialogue": [{"char": "mara", "line": "Who sent this?"}]}))
    proj = ProjectStore(db).get(pid)

    class WithKael(Bible):
        def characters(self):
            return {**super().characters(), "kael": {"id": "kael", "name": "Kael", "tier": "main"}}
    res = mp.import_reply(db, WithKael(pid), proj, _chat_reply(["s01_004"]), cfg=cfg)
    assert not res["ok"] and 'The line "Who sent this?" is not spoken' in res["refused"]["s01_004"]
    res = mp.import_reply(db, WithKael(pid), proj,
                          _chat_reply(["s01_004"], ' and asks, "Who sent this?", as Kael watches'), cfg=cfg)
    assert res["ok"] and res["saved"] == ["s01_004"]
    assert any("Kael is named in the prompt but is not in this shot" in w for w in res["warnings"]["s01_004"])
    # a hand prompt is its author's: a missing line is a warning there
    hand = mp.save_hand(db, Bible(pid), pid, "s01_004", "The doors swing open. Diegetic audio: wind.", cfg=cfg)
    assert hand["ok"] and any(i["code"] == "dialogue" and i["level"] == "warning" for i in hand["issues"])


def test_a_prompt_with_the_cast_look_word_for_word_is_left_as_written(movie):
    db, pid, cfg, _ = movie
    text = ("A wide shot of a woman in her late twenties, auburn hair, green eyes, at the doors. "
            "Diegetic audio: wind.")
    assert mp.save_hand(db, Bible(pid), pid, "s01_004", text, cfg=cfg)["text"] == text


def test_a_local_ai_that_is_not_running_is_not_waited_for_behind_a_render(movie, monkeypatch):
    # Ollama set up but not started, a render on the GPU: the writer used to
    # wait for the GPU before it found out nothing answers
    import gpu_broker
    import llm as llm_mod
    db, pid, cfg, _ = movie
    cfg = {**cfg, "architect": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3"}}
    monkeypatch.setattr(mp, "_DOWN", {})
    monkeypatch.setattr(gpu_broker, "busy", lambda: True)
    monkeypatch.setattr(llm_mod, "reachable", lambda base, probe=None: False)
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("waited for the GPU"))
    client = FakeLLM()
    with pytest.raises(mp.Unreachable):
        _write(db, pid, cfg, client)
    assert client.calls == [] and "http://127.0.0.1:11434/v1" in mp._DOWN


def test_the_writer_asks_once_whether_the_ai_answers_before_it_writes(movie, monkeypatch):
    # nothing listening: found out at once, not after the request's retries
    import llm
    from projects import ProjectStore
    db, pid, cfg, _ = movie
    monkeypatch.setattr(mp, "_DOWN", {})
    monkeypatch.setattr(llm, "reachable", lambda base, probe=None: False)
    client = FakeLLM()
    monkeypatch.setattr(llm, "LLMClient", lambda cfg=None: client)
    res = mp.PromptWriter(cfg, db, ProjectStore(db)).run(pid)
    assert res["unreachable"] and client.calls == [] and sorted(res["failed"]) == SIDS[:3]
