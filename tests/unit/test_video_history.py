"""Video Studio history: one list of single clips (the job store) and chains (their
records, the movie's continuous takes included), plus chains rendered before chain
records existed, rebuilt from their files."""
import json
import time

import pytest

import chain_studio
import video_studio as vs


@pytest.fixture
def studio(tmp_path, monkeypatch):
    out = tmp_path / "videostudio"
    out.mkdir()
    monkeypatch.setattr(vs, "OUT_DIR", out)
    monkeypatch.setattr(vs, "JOBS_FILE", tmp_path / "videostudio_jobs.json")
    monkeypatch.setattr(vs, "_JOBS", {})
    monkeypatch.setattr(vs, "_CHAINS", {})
    monkeypatch.setattr(chain_studio, "CHAIN_DIR", out / "chains")
    return out


def _jobs(*jobs):
    vs.JOBS_FILE.write_text(json.dumps({j["id"]: j for j in jobs}), encoding="utf-8")


def _touch(studio, *names):
    for n in names:
        (studio / n).write_bytes(b"")


def test_clips_and_chain_records_list_newest_first(studio):
    _jobs({"id": "c1", "ts": "2026-10-01T10:00:00+00:00", "status": "completed",
           "outputs": ["c1_00001_.mp4"], "workflow": {"1": {"class_type": "X"}}},
          {"id": "c2", "ts": "2026-10-03T10:00:00+00:00", "status": "queued", "outputs": []})
    chain_studio.save_record({
        "id": "abcdef123456", "ts": "2026-10-02T10:00:00+00:00", "status": "completed",
        "model": "ltx25_s6_cq", "prompts": ["one", "two"],
        "final_video": str(studio / "chain_abcdef12_final.mp4"),
        "origin": {"project": "film", "shots": ["s01_001", "s01_002"]}})
    _touch(studio, "chain_abcdef12_final.mp4", "chain_abcdef12_seg1.mp4")

    items = vs.history()

    assert [(it["kind"], it["id"]) for it in items] == [
        ("clip", "c2"), ("chain", "abcdef123456"), ("clip", "c1")]
    clip = items[2]
    assert clip["video"] == "c1_00001_.mp4" and "workflow" not in clip
    assert items[0]["video"] is None
    chain = items[1]
    assert chain["video"] == "chain_abcdef12_final.mp4"
    assert chain["segments"] == 2
    assert chain["origin"] == {"project": "film", "shots": ["s01_001", "s01_002"]}
    assert not chain.get("legacy")             # its files belong to the record


def test_chains_without_a_record_are_rebuilt_from_their_files(studio):
    _touch(studio, "chain_0011aabb_seg2.mp4", "chain_0011aabb_seg1.mp4", "chain_0011aabb_final.mp4",
           "chain_0011aabb_tail17_1.mp4", "chain_99887766_seg1.mp4", "notachain.mp4")

    items = {it["id"]: it for it in vs.history()}

    assert set(items) == {"0011aabb", "99887766"}
    done = items["0011aabb"]
    assert done["legacy"] and done["status"] == "completed"
    assert done["video"] == "chain_0011aabb_final.mp4"
    assert done["segments"] == 2
    assert done["outputs"] == ["chain_0011aabb_seg1.mp4", "chain_0011aabb_seg2.mp4"]
    partial = items["99887766"]
    assert partial["status"] == "partial" and partial["video"] == "chain_99887766_seg1.mp4"


def test_a_record_left_mid_run_reads_interrupted_a_live_chain_keeps_its_stage(studio):
    chain_studio.save_record({"id": "aaaa11112222", "ts": "2026-10-01T00:00:00+00:00",
                              "status": "rendering segment 2/3", "prompts": ["a", "b", "c"]})
    live = {"id": "bbbb33334444", "ts": "2026-10-02T00:00:00+00:00",
            "status": "rendering segment 1/2", "prompts": ["a", "b"]}
    chain_studio.save_record(live)
    vs._CHAINS["bbbb33334444"] = live
    chain_studio.save_record({"id": "cccc55556666", "ts": "2026-10-03T00:00:00+00:00",
                              "status": "draft_ready", "prompts": ["a", "b"],
                              "draft_video": str(studio / "chain_cccc5555_draft.mp4")})

    items = {it["id"]: it for it in vs.history()}

    assert items["aaaa11112222"]["status"] == "interrupted"
    assert items["bbbb33334444"]["status"] == "rendering segment 1/2"
    assert items["cccc55556666"]["status"] == "draft_ready"
    assert items["cccc55556666"]["video"] == "chain_cccc5555_draft.mp4"


@pytest.mark.usefixtures("studio")
def test_history_is_limited_to_the_newest():
    _jobs(*[{"id": f"j{i}", "ts": f"2026-10-0{i}T00:00:00+00:00", "status": "completed",
                     "outputs": []} for i in range(1, 6)])
    assert [it["id"] for it in vs.history(limit=2)] == ["j5", "j4"]


def test_history_route_clamps_the_limit(test_client, monkeypatch):
    seen = []
    monkeypatch.setattr(vs, "history", lambda limit=60: seen.append(limit) or [{"kind": "clip", "id": "x"}])
    t0 = time.time()
    res = test_client.get("/api/videostudio/history?limit=1000")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == [{"kind": "clip", "id": "x"}]
    assert t0 - 1 <= body["now"] <= time.time() + 1      # the server clock, for the UI's step timers
    assert seen == [300]


def test_chain_preview_route_serves_only_segment_previews(test_client, monkeypatch, tmp_path):
    monkeypatch.setattr(chain_studio, "CHAIN_DIR", tmp_path)
    d = chain_studio.preview_dir("abcdef123456")
    d.mkdir(parents=True)
    (d / "link2_final.mp4").write_bytes(b"mp4")
    (tmp_path / "abcdef123456.json").write_text("{}", encoding="utf-8")
    base = "/api/videostudio/chain/abcdef123456/preview/"
    assert test_client.get(base + "link2_final.mp4").content == b"mp4"
    for bad in ("link3_final.mp4", "link2_final.mkv", "..%2Fabcdef123456.json", "notes.mp4"):
        assert test_client.get(base + bad).status_code == 404, bad
    assert test_client.get("/api/videostudio/chain/ABC/preview/link1_draft.mp4").status_code == 404


def test_gpu_health_route_reports_the_gpus_or_none(test_client, monkeypatch):
    from routers import common
    row = {"index": "0", "name": "RTX", "mem_used": "1024 MiB", "mem_total": "8192 MiB",
           "temp": "50", "util": "3 %"}
    monkeypatch.setattr(common.render_mode, "health", lambda: [row])
    assert test_client.get("/api/gpu/health").json() == {"gpu": [row]}

    def missing():
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(common.render_mode, "health", missing)
    assert test_client.get("/api/gpu/health").json() == {"gpu": []}
