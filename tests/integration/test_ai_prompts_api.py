"""The endpoints of the movie's video prompts and of the AI settings
(routers/ai_prompts.py, and the shot prompt / re-render endpoints of
routers/project_shots.py), on the small movie of tests/unit/moviekit.py."""
import copy

import pytest

from moviekit import H3, SIDS, Bible, take_reply


@pytest.fixture
def api_movie(movie, monkeypatch):
    """The `movie` fixture behind the app's routers."""
    from projects import ProjectStore
    from routers import ai_prompts, project_shots
    db, pid, _, models = movie
    store = ProjectStore(db)
    for mod in (ai_prompts, project_shots):
        monkeypatch.setattr(mod, "db", db)
        monkeypatch.setattr(mod, "Bible", Bible)
    monkeypatch.setattr(ai_prompts, "store", store)
    return db, pid, models


@pytest.fixture
def settings_kept():
    """The live settings, restored after the test (the AI settings save them)."""
    from core import load_config, save_config
    live = load_config()
    before = copy.deepcopy(live.get("architect"))
    yield live
    if before is None:
        live.pop("architect", None)
    else:
        live["architect"] = before
    save_config(live)


def test_the_ai_settings_keep_the_key_for_its_endpoint_only(test_client, settings_kept):
    provs = test_client.get("/api/ai/providers").json()["providers"]
    assert {"openai", "gemini", "ollama", "custom"} <= {p["id"] for p in provs}
    r = test_client.post("/api/ai/config", json={"provider": "openai", "model": "gpt-5-mini",
                                                 "api_key": "sk-test-1234567890abcd"}).json()
    assert r["ok"] and r["base_url"] == "https://api.openai.com/v1" and r["ready"]
    cfg = test_client.get("/api/ai/config").json()
    assert cfg["api_key"] == "********abcd" and cfg["has_key"] and cfg["provider"] == "openai"
    assert "sk-test" not in str(cfg)
    # the masked key sent back keeps the key
    r = test_client.post("/api/ai/config", json={"provider": "openai", "model": "gpt-5",
                                                 "api_key": cfg["api_key"]}).json()
    assert r["has_key"] and settings_kept["architect"]["api_key"] == "sk-test-1234567890abcd"
    # another host: the key was issued for the old one
    r = test_client.post("/api/ai/config", json={"provider": "groq", "model": "llama"}).json()
    assert r["api_key_cleared"] and not r["has_key"]
    r = test_client.post("/api/ai/config", json={"base_url": "file:///etc/passwd", "model": "x"}).json()
    assert not r["ok"]
    short = test_client.post("/api/ai/config", json={"provider": "custom", "base_url": "http://127.0.0.1:9/v1",
                                                     "model": "m", "api_key": "abcd"}).json()
    assert short["api_key"] == "********"


def test_an_opening_frame_is_shown_with_its_likeness_and_made_again(test_client, api_movie, monkeypatch,
                                                                   tmp_path):
    import json
    import face_drift
    import opening_frames as of
    import movie_prompts as mp
    db, pid, _ = api_movie
    monkeypatch.setattr(of, "enabled", lambda cfg=None: True)
    monkeypatch.setattr(of, "FRAME_DIR", tmp_path / "opening")        # its fake frame stays here
    monkeypatch.setattr(face_drift, "_APP", False)
    of._FAILED.clear()
    db.save_prompt("s01_004", pid, text="x", source="ai", image="The closed doors.")
    url = f"/api/projects/{pid}/shots/s01_004/opening-frame"
    assert test_client.get(url).status_code == 404                     # not made yet
    unit = mp.unit_for(db, pid, "s01_004")
    p = of.frame_path(db, unit)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG" + bytes(2000))
    p.with_suffix(".json").write_text(json.dumps({"likeness": 0.21, "weak": True, "scored": True,
                                                  "min_likeness": 0.35, "people": {"Mara": 0.21}}))
    view = test_client.get(f"/api/projects/{pid}/prompts").json()["shots"]
    f = view["s01_004"]["frame"]
    assert (f["likeness"], f["weak"], f["people"]) == (0.21, True, {"Mara": 0.21}) and "frame" not in view["s01_002"]
    r = test_client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert test_client.get(f"/api/projects/{pid}/shots/s01_002/opening-frame").status_code == 404   # inside a take
    made = []
    monkeypatch.setattr(of, "again", lambda db_, bible, u, cfg=None: made.append(u.sids))
    assert test_client.post(url + "/again").json() == {"ok": True, "making": True}
    import time
    for _ in range(50):
        if made:
            break
        time.sleep(0.05)
    assert made == [["s01_004"]]
    db.save_prompt("s01_004", pid, text="x", source="hand", image="")    # a hand prompt without one
    assert not test_client.post(url + "/again").json()["ok"]


def test_the_render_page_shows_the_take_length_and_sets_the_projects(test_client, api_movie, monkeypatch,
                                                                     tmp_path):
    import video_studio
    from projects import ProjectStore
    from routers import projects as project_router
    db, pid, _ = api_movie

    class ProjectBible(Bible):
        dir = tmp_path

        def all(self):
            return {"characters": self.characters()}
    store = ProjectStore(db)
    monkeypatch.setattr(project_router, "store", store)
    monkeypatch.setattr(project_router, "db", db)
    monkeypatch.setattr(__import__("bible"), "Bible", ProjectBible)
    monkeypatch.setattr(project_router, "load_config", lambda: {"movie": {"ltx_take_max_shots": 7}})
    url = f"/api/projects/{pid}"
    assert test_client.get(url).json()["take_length"] == {"studio": 7, "now": 7,
                                                          "top": video_studio.CHAIN_MAX_SEGMENTS}
    assert test_client.post(url, json={"take_max_shots": 3}).json()["ok"]
    assert test_client.get(url).json()["take_length"] == {"studio": 7, "now": 3,
                                                          "top": video_studio.CHAIN_MAX_SEGMENTS}
    assert not test_client.post(url, json={"take_max_shots": video_studio.CHAIN_MAX_SEGMENTS + 1}).json()["ok"]
    assert test_client.post(url, json={"take_max_shots": 0}).json()["ok"] and store.get(pid)["take_max_shots"] == 0


def test_a_continuity_change_first_says_which_clips_it_renders_again(test_client, api_movie):
    db, pid, _ = api_movie
    for s in SIDS[:3]:
        db.set_status(s, "passed", project=pid)
    url = f"/api/projects/{pid}/shots/s01_004"
    assert test_client.post(url + "/continue", json={"dry_run": True}).json() == {"ok": True, "rerenders": SIDS[:3]}
    r = test_client.post(url + "/continuity", json={"continuity": "CONTINUE", "dry_run": True}).json()
    assert r["ok"] and r["rerenders"] == SIDS[:3]
    row = db.get("s01_004", pid)
    assert row["json"]["continuity"] == "CUT" and row["status"] != "queued"     # a dry run writes nothing
    assert test_client.post(url + "/continue").json()["ok"]                  # without a body, as before
    row = db.get("s01_004", pid)
    assert row["json"]["continuity"] == "CONTINUE" and row["status"] == "queued"


def test_the_shots_page_view_brief_and_import(test_client, api_movie):
    db, pid, _ = api_movie
    view = test_client.get(f"/api/projects/{pid}/prompts").json()
    assert view["ok"] and set(view["shots"]) == set(SIDS) and view["counts"] == {"missing": 4}
    one = test_client.post(f"/api/projects/{pid}/prompts/brief", json={"limit": 1}).json()
    assert one["shots"] == SIDS[:1] and "(later: written in the next reply" in one["text"]
    brief = test_client.post(f"/api/projects/{pid}/prompts/brief", json={}).json()
    assert brief["shots"] == SIDS and "Shot s01_004: ..." in brief["text"]
    reply = take_reply() + "\n\nShot s01_004: The doors open; wind howls."
    res = test_client.post(f"/api/projects/{pid}/prompts/import", json={"text": reply}).json()
    assert res["saved"] == SIDS
    assert test_client.get(f"/api/projects/{pid}/prompts").json()["counts"] == {"ready": 4}
    assert test_client.get("/api/projects/nope/prompts").status_code == 404


def test_writing_needs_an_ai(test_client, api_movie, settings_kept):
    _, pid, _ = api_movie
    settings_kept["architect"] = {}
    r = test_client.post(f"/api/projects/{pid}/prompts/write", json={}).json()
    assert not r["ok"] and "no AI is set up" in r["error"]


def test_a_shot_prompt_is_checked_and_a_rerender_takes_the_next_seed(test_client, api_movie):
    from orchestrator import REROLL_SEED
    db, pid, models = api_movie
    url = f"/api/projects/{pid}/shots/s01_004"
    r = test_client.post(url + "/prompt", json={"prompt": "The doors open. Wind howls."}).json()
    assert r["ok"] and db.prompt("s01_004", pid)["source"] == "hand"
    assert db.get("s01_004", pid)["status"] == "queued" and db.get("s01_004", pid)["qa_stage"] == 0
    db.set_status("s01_004", "passed", seed=500, project=pid)
    assert test_client.post(url + "/rerender", json={}).json()["ok"]
    assert db.get("s01_004", pid)["seed"] == 500 + REROLL_SEED
    models.update({s: H3 for s in SIDS})
    r = test_client.post(f"/api/projects/{pid}/shots/s01_002/prompt",
                         json={"prompt": "<Picture 7> waves. Footsteps."}).json()
    assert not r["ok"] and "Picture 7" in r["error"]


def test_whether_the_ai_answers_is_asked_apart_from_the_settings(test_client, settings_kept, monkeypatch):
    import llm
    settings_kept["architect"] = {}
    r = test_client.get("/api/ai/reachable").json()
    assert not r["ready"] and not r["reachable"]
    settings_kept["architect"] = {"base_url": "http://127.0.0.1:9/v1", "model": "m"}
    monkeypatch.setattr(llm, "reachable", lambda base, probe=None: base.endswith(":9/v1"))
    r = test_client.get("/api/ai/reachable").json()
    assert r["ready"] and r["reachable"] and r["local"]
    assert "reachable" not in test_client.get("/api/ai/config").json()   # every page loads that one



def test_saving_the_ai_settings_lets_the_writers_ask_again(test_client, settings_kept):
    import movie_prompts
    movie_prompts._DOWN["http://127.0.0.1:9/v1"] = __import__("time").time()
    test_client.post("/api/ai/config", json={"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m"})
    assert movie_prompts._DOWN == {}
