"""Video Studio adapter for seamless chains: link lengths, reference policy, the H3
tier recipe read from the studio's own graph, records, and the ComfyUI runner."""
import time
from pathlib import Path

import pytest

import chain_studio as cs


def test_ltx_link_frames_keep_the_old_span_per_link():
    assert cs.ltx_link_frames(121, 4) == [121, 145, 145, 145]
    assert cs.ltx_link_frames(124, 2) == [129, 153]          # snapped up to 1+8k
    assert all((f - 1) % 8 == 0 for f in cs.ltx_link_frames(100, 3))


def test_ltx_draft_size_follows_the_requested_orientation():
    assert cs.ltx_draft_size(1920, 1088) == (960, 544)
    assert cs.ltx_draft_size(1088, 1920) == (544, 960)
    assert cs.ltx_draft_size(1080, 1920) == (544, 960)          # orientation only, never the size
    assert cs.ltx_draft_size(1344, 768) == cs.ltx_draft_size(None, None) == (960, 544)


def test_h3_windows_land_on_the_audio_grid():
    assert [cs.h3_window(f) for f in (5, 39, 40, 124, 141, 150, 500)] == [39, 39, 90, 141, 141, 192, 192]
    assert cs.h3_link_frames(124, 3) == [141, 192, 192]
    for f in cs.h3_link_frames(90, 4):
        assert (f - 5) % 17 == 0 and f % 3 == 0


def test_ltx_reference_policy():
    cast = [{"slot": "pic1", "path": "a.png"}, {"slot": "pic2", "path": "b.png"},
            {"slot": "background", "path": "bg.png"}]
    s = cs.ltx_reference_slots(Path("start.png"), cast)
    assert s == {"pic1": Path("start.png"), "pic2": Path("a.png"), "pic3": Path("b.png"),
                 "background": Path("bg.png")}
    # no start image: subjects fill from pic1 even when the caller skipped slots
    s = cs.ltx_reference_slots(None, [{"slot": "pic3", "path": "c.png"}, {"slot": "pic2", "path": "b.png"}])
    assert s == {"pic1": Path("b.png"), "pic2": Path("c.png")}
    assert cs.ltx_reference_slots(None, []) == {}


def test_ltx_face_slots_fill_free_subject_slots_lead_first(monkeypatch, tmp_path):
    import face_ref
    made = []

    def closeup(src, cache_dir, **kw):
        made.append(Path(src).name)
        return None if Path(src).name == "closeup.png" else Path(cache_dir) / f"face_{Path(src).stem}.png"
    monkeypatch.setattr(face_ref, "face_closeup", closeup)
    cast = [{"slot": "pic2", "path": "man.png", "type": "face"}, {"slot": "pic1", "path": "woman.png"},
            {"slot": "background", "path": "bg.png", "type": "environment"}]
    slots = cs.ltx_reference_slots(Path("start.png"), cast)
    out = cs.ltx_face_slots(slots, cast, tmp_path)
    # start image pic1, cast pic2/pic3: one free slot, the lead (cast pic1) gets it
    assert out == {**slots, "pic4": tmp_path / "face_woman.png"} and made == ["woman.png"]
    # no start image: two free slots, both faces, in cast order
    slots = cs.ltx_reference_slots(None, cast)
    out = cs.ltx_face_slots(slots, cast, tmp_path)
    assert out["pic3"] == tmp_path / "face_woman.png" and out["pic4"] == tmp_path / "face_man.png"
    # a portrait that is a close-up already gives no crop; the next one is tried
    made.clear()
    cast2 = [{"slot": "pic1", "path": "closeup.png"}, {"slot": "pic2", "path": "man.png"}]
    out = cs.ltx_face_slots(cs.ltx_reference_slots(Path("s.png"), cast2), cast2, tmp_path)
    assert out["pic4"] == tmp_path / "face_man.png" and made == ["closeup.png", "man.png"]
    # no free slot, no subject at all, non-face types: untouched
    full = {"pic1": Path("s"), "pic2": Path("a"), "pic3": Path("b"), "pic4": Path("c")}
    assert cs.ltx_face_slots(full, cast, tmp_path) == full
    assert cs.ltx_face_slots({}, cast, tmp_path) == {}
    power = [{"slot": "pic1", "path": "fire.png", "type": "power"}]
    assert cs.ltx_face_slots({"pic1": Path("s")}, power, tmp_path) == {"pic1": Path("s")}


def test_cast_line_comes_from_descriptions_or_the_face_dataset(monkeypatch):
    import cast_import as ci
    monkeypatch.setattr(ci, "_rows_for_import", lambda t, n: ([{"run_id": 1}], False) if n == "mara" else ([], False))
    monkeypatch.setattr(ci, "_run_params", lambda rows: {"age": "mid-20s", "presentation": "woman",
                                                         "hair_color": "brown", "silhouette": "cable-knit turtleneck"})
    cast = [{"slot": "pic2", "path": "t.png", "type": "face", "name": "tom",
             "description": "a man in his early thirties, short dark hair. "},
            {"slot": "pic1", "path": "m.png", "type": "face", "name": "mara"},
            {"slot": "pic3", "path": "x.png", "type": "face", "name": "nobody"},
            {"slot": "background", "path": "bg.png", "type": "environment", "name": "forest"}]
    d = cs.cast_descriptions(cast)
    assert d == ["mid-20s woman, brown hair, cable-knit turtleneck", "a man in his early thirties, short dark hair"]
    p = cs.with_cast_line("A two-shot opens on the path. She stops. He turns.", d)
    assert p == ("A two-shot opens on the path. Cast, the same age and look in every shot: mid-20s woman, "
                 "brown hair, cable-knit turtleneck; a man in his early thirties, short dark hair. She stops. He turns.")
    assert cs.with_cast_line("One sentence only", ["x"]) == "One sentence only Cast, the same age and look in every shot: x."
    assert cs.with_cast_line("Untouched. Prompt.", []) == "Untouched. Prompt."


def test_face_closeup_crops_square_below_the_face_and_caches(monkeypatch, tmp_path):
    import cv2
    import numpy as np

    import face_ref
    img = np.zeros((600, 1000, 3), np.uint8)
    img[:, :, 1] = np.arange(1000, dtype=np.uint16)[None, :] % 256    # x-coded image
    src = tmp_path / "portrait.png"
    cv2.imwrite(str(src), img)
    monkeypatch.setattr(face_ref, "_largest_face", lambda g: (450, 100, 100, 100))
    out = face_ref.face_closeup(src, tmp_path / "cache", size=256)
    crop = cv2.imread(str(out))
    assert crop is not None and crop.shape == (256, 256, 3)
    # 200 px square around x 500 (450..550 face): columns 400..599 of the source
    assert abs(int(crop[0, 0, 1]) - 400 % 256) <= 2 and abs(int(crop[0, -1, 1]) - 599 % 256) <= 3
    assert face_ref.face_closeup(src, tmp_path / "cache", size=256) == out          # cached
    monkeypatch.setattr(face_ref, "_largest_face", lambda g: (300, 50, 300, 300))     # half the height
    assert face_ref.face_closeup(src, tmp_path / "c2") is None
    monkeypatch.setattr(face_ref, "_largest_face", lambda g: None)
    assert face_ref.face_closeup(src, tmp_path / "c3") is None
    assert face_ref.face_closeup(tmp_path / "missing.png", tmp_path / "c3") is None


def test_h3_reference_order_follows_slot_priority():
    cast = [{"slot": "background", "path": "bg"}, {"slot": "pic2", "path": "b"}, {"slot": "pic1", "path": "a"}]
    assert cs.h3_reference_order(cast) == [Path("a"), Path("b"), Path("bg")]


def _tier_graph(refine: bool, lora: str | None = None):
    g = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "models/x/h3.safetensors"}},
         "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "te.safetensors"}},
         "3": {"class_type": "VAELoader", "inputs": {"vae_name": "h3_video_vae.safetensors"}},
         "4": {"class_type": "VAELoader", "inputs": {"vae_name": "h3_audio_vae.safetensors"}},
         "5": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"width": 960, "height": 544}},
         "6": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"shift_video": 10.0, "shift_audio": 2.0}},
         "7": {"class_type": "ModelAttentionBackend", "inputs": {"attention": "sage"}},
         "11": {"class_type": "BasicScheduler", "inputs": {"steps": 4}}}
    if lora:
        g["20"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": lora, "strength_model": 0.8}}
    if refine:
        g["41"] = {"class_type": "MinimaxH3LatentUpscaler3D",
                   "inputs": {"model_name": "up.safetensors", "mode.width": 1920, "mode.height": 1088}}
        g["43"] = {"class_type": "MMH3SpatialSplitParamsV10",
                   "inputs": {"tile_width": 1024, "tile_height": 704, "seam_denoise": 0.7}}
        g["45"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": "0.3, 0.0"}}
        g["46"] = {"class_type": "MMH3SplitUpscale", "inputs": {"sigmas": ["45", 0], "spatial_split_param": ["43", 0]}}
    return g


def test_h3_tier_params_read_the_tier_recipe():
    m, link, refine = cs.h3_tier_params(_tier_graph(True, lora="turbo.safetensors"))
    assert (m.unet, m.vae, m.audio_vae, m.upscaler) == ("h3.safetensors", "h3_video_vae.safetensors",
                                                        "h3_audio_vae.safetensors", "up.safetensors")
    assert link == {"loras": [("turbo.safetensors", 0.8)], "attention": "sage", "shift_video": 10.0,
                    "shift_audio": 2.0, "steps": 4, "width": 960, "height": 544}
    assert refine["sigmas"] == "0.3, 0.0" and refine["seam_denoise"] == 0.7
    assert (refine["width"], refine["height"], refine["tile_width"]) == (1920, 1088, 1024)
    assert refine["loras"] == [("turbo.safetensors", 0.8)]
    assert cs.h3_tier_params(_tier_graph(False))[2] is None


def test_records_survive_a_restart():
    rec = {"id": "abc123def456", "status": "draft_ready", "prompts": ["p"]}
    cs.save_record(rec)
    assert cs.load_record("abc123def456") == rec
    assert cs.load_record("../etc") is None and cs.load_record("") is None


class _Client:
    def __init__(self, hist):
        self.hist, self.interrupted = hist, False

    def queue_prompt(self, graph, client_id=None):
        return "pid"

    def wait_for(self, pid, timeout_s=0):
        return self.hist

    def interrupt(self):
        self.interrupted = True


def test_runner_reports_errors_and_timeouts():
    ok = cs.StudioRunner(_Client({"status": {"status_str": "success"}}), 10).run("draft_l1", {})
    assert ok["status"] == "success"
    err = {"status": {"status_str": "error",
                      "messages": [["execution_error", {"exception_message": "OOM on tile 3"}]]}}
    with pytest.raises(RuntimeError, match="draft_l1: OOM on tile 3"):
        cs.StudioRunner(_Client(err), 10).run("draft_l1", {})
    c = _Client(None)
    with pytest.raises(RuntimeError, match="no result"):
        cs.StudioRunner(c, 10).run("finish_F0_l2", {})
    assert c.interrupted


def test_whole_take_steps_get_the_clip_budget_once_per_link():
    # the H3 gate's 4-link 1080p refine took 3653 s against a 2700 s clip budget
    run = cs.StudioRunner(_Client(None), 2700, links=4)
    for step in ("draft_l1", "finish_F0_l3", "link_l4"):
        assert run.step_timeout(step) == 2700
    for step in ("refcond_R0", "refine_R0", "decode_B0", "decode_R0", "take_draft", "take_final_F0",
                 "audio_take"):
        assert run.step_timeout(step) == 4 * 2700
    seen = []

    class _Timed(_Client):
        def wait_for(self, pid, timeout_s=0):
            seen.append(timeout_s)
            return self.hist
    cs.StudioRunner(_Timed({"status": {"status_str": "success"}}), 100, links=3).run("refine_R0", {})
    assert seen == [300]


def test_runner_restarts_the_server_under_the_chains_lease(monkeypatch):
    import gpu_broker
    lease = gpu_broker.Lease(8199, "h3", "chain")
    restarted = []
    monkeypatch.setattr(gpu_broker, "current_lease", lambda: lease)
    monkeypatch.setattr(gpu_broker, "restart_leased",
                        lambda ls: restarted.append(ls) or {"ok": True, "ms": 25000, "port": 8199})
    run = cs.StudioRunner(_Client(None), 10)
    run.fresh_process("decode_B0")
    assert restarted == [lease]
    monkeypatch.setattr(gpu_broker, "restart_leased", lambda ls: {"ok": False, "error": "did not come back"})
    with pytest.raises(RuntimeError, match="decode_B0: could not restart the server first: did not come back"):
        run.fresh_process("decode_B0")
    monkeypatch.setattr(gpu_broker, "current_lease", lambda: None)       # broker off: render as before
    run.fresh_process("decode_B0")


def test_broker_restarts_only_the_active_lease_and_keeps_it(monkeypatch):
    import gpu_broker
    b = gpu_broker._Broker()
    monkeypatch.setattr(b, "_registry", lambda: {8199: {"h3": True, "models": ["h3"]}})
    killed = []

    def restart(inst):
        killed.append(inst.port)
        inst.state, inst.lease_id = "empty", None
    monkeypatch.setattr(b, "_restart_instance", restart)
    lease = gpu_broker.Lease(8199, "h3", "chain")
    assert not b.restart_leased(lease)["ok"] and not killed           # not the active lease
    b._active = lease
    res = b.restart_leased(lease)
    assert res["ok"] and killed == [8199]
    inst = b._instance(8199)
    assert (inst.state, inst.lease_id, inst.model) == ("busy", lease.id, "h3") and b._active is lease


def test_broker_does_not_wait_for_an_h3_server_that_is_not_running(monkeypatch):
    """A studio without H3 (or before the launcher starts it) has nothing on its
    port: the start-up check must not restart it and wait 180 s for it."""
    import gpu_broker
    b = gpu_broker._Broker()
    monkeypatch.setattr(b, "_registry", lambda: {8199: {"h3": True, "models": {"h3"}}})
    s = dict(b.settings(), h3_mode="restart", verify_timeout_s=60)
    monkeypatch.setattr(b, "settings", lambda: s)
    restarted = []
    monkeypatch.setattr(b, "_restart_instance", lambda inst: restarted.append(inst.port))
    monkeypatch.setattr(gpu_broker, "_queue_empty", lambda port: None)
    monkeypatch.setattr(gpu_broker, "_listening", lambda port: False)
    pids: list = []
    monkeypatch.setattr(gpu_broker, "_port_pids", lambda port: pids)
    t0 = time.time()
    assert b._reconcile_body("startup")["results"] == {"8199": "empty"}
    assert restarted == [] and time.time() - t0 < 5
    pids.append(4242)                       # a server starting up there is restarted
    b._instance(8199).state = "unknown"
    b._reconcile_body("startup")
    assert restarted == [8199]


class _FilesRunner:
    """Stands in for StudioRunner: records step names and leaves the files each
    node would write (with sidecars carrying the frame counts the engine checks)."""
    calls: list = []

    def __init__(self, client, timeout_s, on_step=None, links=1):
        self.out = cs.ROOT / "ComfyUI_LTX" / "output"
        self.on_step = on_step

    def run(self, name, graph):
        import json
        if self.on_step:                                  # as StudioRunner.run does
            self.on_step(name)
        _FilesRunner.calls.append(name)
        frames = next((int(n["inputs"]["length"]) for n in graph.values()
                       if n["class_type"] == "EmptyLTXVLatentVideo"), None)
        for n in graph.values():
            ct, inp = n["class_type"], n["inputs"]
            if ct in ("StudioSaveLatent", "StudioSaveImages", "StudioSaveConditioning"):
                p = self.out / (inp["relpath"] + ".safetensors")
            elif ct in ("StudioSaveVideoLossless", "StudioLtxDecodeTake"):
                p = self.out / (inp["relpath"] + ".mkv")
                if ct == "StudioLtxDecodeTake":
                    k = len(inp["links"].splitlines())
                    side = {"frames": 121 + 120 * (k - 1), "take": {"joins": []}}
                else:
                    side = {"frames": frames or 121}             # a finish reads its length from the draft
                    if "_final" in inp["relpath"]:
                        side["frames"] = 121 if "/link1_" in inp["relpath"] else 145
                p.parent.mkdir(parents=True, exist_ok=True)
                p.with_suffix(".json").write_text(json.dumps(side), encoding="utf-8")
            elif ct == "StudioSaveAudio":
                p = self.out / (inp["relpath"] + ".audio.safetensors")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.with_name(p.name.replace(".audio.safetensors", ".json")).write_text(
                    json.dumps({"audio": {"seconds": 1.0}, "join": {"joins": [{"carry_exact": True}]}}), encoding="utf-8")
            else:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        return {"status": "success"}


@pytest.fixture
def studio_sandbox(tmp_path, monkeypatch):
    import chain_engine as ce
    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "CHAIN_DIR", tmp_path / "chains")
    monkeypatch.setattr(cs, "StudioRunner", _FilesRunner)
    monkeypatch.setattr(cs, "ffmpeg_exe", lambda: "ffmpeg")
    stages = []

    def fake_assemble(self, out_mp4, ffmpeg, stage="final", variant="F0", **kw):
        stages.append((stage, variant))
        return {"mp4": str(out_mp4), "frames": self.take_frames(), "qa": {"passed": True}, "qa_summary": "ok"}
    monkeypatch.setattr(ce.LtxChain, "assemble", fake_assemble)
    _FilesRunner.calls = []

    # segment previews: encoded inline (no thread outliving the test) by a stand-in
    def fake_preview(video, carry, fps, out, ffmpeg):
        PREVIEWS.append((Path(video).relative_to(tmp_path / "ComfyUI_LTX" / "output").as_posix(), carry))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
    monkeypatch.setattr(cs, "_PREVIEWS", _Inline())
    monkeypatch.setattr(cs.ca, "link_preview", fake_preview)
    PREVIEWS.clear()
    return stages


PREVIEWS: list = []


class _Inline:
    def submit(self, fn, *a, **kw):
        fn(*a, **kw)


class _Uploads:
    def upload_image(self, p):
        return Path(p).name


LTX_ENTRY = {"family": "ltx25", "comfy_root": "ComfyUI_LTX", "workflows": {"chain": "x"}}


def test_ltx_chain_runs_drafts_take_then_finishes_and_decodes_once(studio_sandbox):
    rec = {"id": "c0ffee00c0ffee00"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b", "c"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[])
    assert _FilesRunner.calls == ["draft_l1", "draft_l2", "draft_l3", "audio_take", "take_draft",
                                  "finish_F0_l1", "finish_F0_l2", "finish_F0_l3", "take_final_F0"]
    assert studio_sandbox == [("draft", "F0"), ("final", "F0")]
    assert rec["status"] == "completed" and rec["final_video"].endswith("_final.mp4")
    assert rec["draft_size"] == [960, 544] and rec["final_size"] == [1920, 1088]


def test_a_chain_takes_one_seed_per_link(studio_sandbox, monkeypatch):
    import json

    import video_models
    import video_studio
    rec = {"id": "c0ffee00c0ffee09"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[], seeds=[11, 40])
    man = json.loads((Path(rec["chain_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert [lk["seed"] for lk in man["spec"]["links"]] == [11, 40]
    with pytest.raises(ValueError, match="1 seeds for 2 links"):
        cs.run_chain({"id": "c0ffee00c0ffee0a"}, LTX_ENTRY, _Uploads(), ["a", "b"], negative="",
                     base_seed=7, frames=121, start_image=None, cast=[], seeds=[1])
    monkeypatch.setattr(video_models, "model_status", lambda mid: {"available": True})
    with pytest.raises(ValueError, match="2 seeds for 3 segments"):
        video_studio.start_chain("ltx25_s6_cq", ["a", "b", "c"], seeds=[1, 2])


def test_chain_plan_lists_every_step_in_order():
    assert cs.chain_plan("ltx", 3) == ["draft_l1", "draft_l2", "draft_l3", "audio_take", "take_draft",
                                       "assemble_draft", "finish_F0_l1", "finish_F0_l2", "finish_F0_l3",
                                       "take_final_F0", "assemble_final"]
    assert cs.chain_plan("ltx", 1, take_decode=False) == ["draft_l1", "assemble_draft", "finish_F0_l1",
                                                          "assemble_final"]
    assert cs.chain_plan("h3", 2) == ["link_l1", "link_l2", "decode_B0", "assemble_B0",
                                      "refcond_R0", "refine_R0", "decode_R0", "assemble_R0"]
    assert cs.chain_plan("h3", 2, refine=False)[-1] == "assemble_B0"


def test_ltx_chain_times_every_step_and_previews_every_segment(studio_sandbox):
    rec = {"id": "c0ffee00c0ffee01"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[])
    assert [s["id"] for s in rec["plan"]] == cs.chain_plan("ltx", 2)
    # every step was started (by the runner or an assemble mark), so each has its own time
    assert all(s["state"] == "done" and "seconds" in s and not s.get("reused") for s in rec["plan"])
    base = "studio_chain/studio_c0ffee00c0ffee01"
    assert PREVIEWS == [(f"{base}/link1_draft.mkv", 0), (f"{base}/link2_draft.mkv", 25),
                        (f"{base}/F0/link1_final.mkv", 0), (f"{base}/F0/link2_final.mkv", 25)]
    assert rec["link_previews"] == {"1": {"draft": "link1_draft.mp4", "final": "link1_final.mp4"},
                                    "2": {"draft": "link2_draft.mp4", "final": "link2_final.mp4"}}
    assert (cs.preview_dir(rec["id"]) / "link2_final.mp4").is_file()


def test_a_draft_only_chain_leaves_the_1080p_steps_pending_until_it_is_finished(studio_sandbox):
    rec = {"id": "c0ffee00c0ffee02"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[], draft_only=True)
    states = {s["id"]: s["state"] for s in rec["plan"]}
    assert rec["status"] == "draft_ready" and states["assemble_draft"] == "done"
    assert [states[k] for k in ("finish_F0_l1", "finish_F0_l2", "take_final_F0", "assemble_final")] == ["pending"] * 4
    cs.resume_chain(rec, LTX_ENTRY, _Uploads())
    assert rec["status"] == "completed" and all(s["state"] == "done" for s in rec["plan"])


def test_plan_marks_reused_and_redrawn_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CHAIN_DIR", tmp_path)
    rec = {"id": "abcabcabcabc"}
    cs._plan_init(rec, ["draft_l1", "draft_l2", "take_draft", "assemble_draft"])
    cs._plan_mark(rec, "draft_l2")                     # link 1 was rendered by an earlier run
    plan = {s["id"]: s for s in rec["plan"]}
    assert plan["draft_l1"] == {"id": "draft_l1", "state": "done", "reused": True}
    assert plan["draft_l2"]["state"] == "running"
    t0 = plan["draft_l2"]["t0"]
    cs._plan_mark(rec, "draft_l2")                     # the seam check drafts it once more
    assert rec["plan"][1]["t0"] == t0 and rec["plan"][1]["redrawn"] is True
    cs._plan_mark(rec, "assemble_draft", close=True)
    assert [s["state"] for s in rec["plan"]] == ["done"] * 4
    assert "seconds" in rec["plan"][1] and rec["plan"][2].get("reused") is True
    cs._plan_mark(rec, "no_such_step")                 # unknown steps leave the plan alone
    assert len(rec["plan"]) == 4


def test_a_portrait_ltx_chain_drafts_portrait_and_keeps_it_for_the_finish(studio_sandbox, monkeypatch):
    import json

    import chain_graphs as cg
    sizes = []
    real = cg.ltx_draft_graph

    def spy(p):
        sizes.append((p.width, p.height))
        return real(p)
    monkeypatch.setattr(cg, "ltx_draft_graph", spy)
    rec = {"id": "f00df00df00df00d"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[], draft_only=True, width=1088, height=1920)
    assert sizes == [(544, 960), (544, 960)]
    assert rec["draft_size"] == [544, 960] and rec["final_size"] == [1088, 1920]
    man = json.loads((Path(rec["chain_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert (man["spec"]["width"], man["spec"]["height"]) == (544, 960)     # resume_chain rebuilds from it


def test_ltx_chain_adds_the_leads_face_close_up_and_reports_the_cast(studio_sandbox, monkeypatch):
    import cast_import as ci
    import face_drift
    import face_ref
    monkeypatch.setattr(face_ref, "face_closeup", lambda src, cache_dir, **kw: Path(cache_dir) / "face_lead.png")
    monkeypatch.setattr(ci, "_rows_for_import", lambda t, n: ([], False))       # no dataset lookups
    seen = []

    def drift(video, starts, cast, **kw):
        seen.append((Path(video).name, sorted(cast)))
        return {"links": [], "summary": {}}
    monkeypatch.setattr(face_drift, "cast_drift", drift)
    cast = [{"slot": "pic1", "path": "lead.png", "type": "face", "name": "Mara",
             "description": "a woman in her mid-twenties"},
            {"slot": "pic2", "path": "man.png", "type": "face", "name": "Tom"},
            {"slot": "background", "path": "bg.png", "type": "environment"}]
    rec = {"id": "facecafefacecafe"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=Path("start.png"), cast=cast)
    assert rec["references"]["pic4"].endswith("face_lead.png")
    assert rec["cast_faces"] == {"Mara": "lead.png", "Tom": "man.png"}
    assert rec["cast_line"] == ["a woman in her mid-twenties"]
    import json
    man = json.loads((Path(rec["chain_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert all("Cast, the same age and look in every shot: a woman in her mid-twenties." in lk["prompt"]
               for lk in man["spec"]["links"])
    assert seen == [("chain_facecafe_draft.mp4", ["Mara", "Tom"]), ("chain_facecafe_final.mp4", ["Mara", "Tom"])]
    assert "draft_cast" in rec and "cast" in rec and rec["status"] == "completed"


def test_resuming_a_legacy_draft_keeps_its_carry(studio_sandbox):
    import json
    rec = {"id": "0dd0dd0dd0dd0dd0"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[], draft_only=True)
    assert rec["status"] == "draft_ready"
    man_p = Path(rec["chain_dir"]) / "manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8"))
    for k in cs.LTX_LEGACY_CARRY:                     # a manifest written before these existed
        man["spec"].pop(k, None)
    man_p.write_text(json.dumps(man), encoding="utf-8")
    built = []
    import chain_engine as ce
    real = ce.LtxChain.__init__

    def spy(self, spec, *a, **kw):
        built.append(spec)
        real(self, spec, *a, **kw)
    ce.LtxChain.__init__ = spy
    try:
        cs.resume_chain(rec, LTX_ENTRY, _Uploads())
    finally:
        ce.LtxChain.__init__ = real
    s = built[-1]
    assert (s.pin_strengths, s.latent_carry, s.carry_restore, s.seam_adain, s.take_decode) == ("", False, 0.0, 0.0, False)
    assert s.seam_mode == "window"
    assert "take_final_F0" not in _FilesRunner.calls and rec["status"] == "completed"


def test_resuming_a_draft_from_before_the_seam_mode_keeps_window_matching(studio_sandbox):
    import json
    import chain_engine as ce
    rec = {"id": "5ea05ea05ea05ea0"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[], draft_only=True)
    man_p = Path(rec["chain_dir"]) / "manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8"))
    assert man["spec"]["seam_mode"] == "path"         # new chains record it
    man["spec"].pop("seam_mode")
    man_p.write_text(json.dumps(man), encoding="utf-8")
    built = []
    real = ce.LtxChain.__init__

    def spy(self, spec, *a, **kw):
        built.append(spec)
        real(self, spec, *a, **kw)
    ce.LtxChain.__init__ = spy
    try:
        cs.resume_chain(rec, LTX_ENTRY, _Uploads())
    finally:
        ce.LtxChain.__init__ = real
    s = built[-1]
    assert (s.seam_mode, s.seam_adain, s.latent_carry) == ("window", 1.0, True)   # the rest as recorded


def test_resuming_a_draft_from_before_the_cq_default_finishes_with_its_cq(studio_sandbox):
    import json
    import chain_engine as ce
    rec = {"id": "c0c0c0c0c0c0c0c0"}
    cs.run_chain(rec, LTX_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=121,
                 start_image=None, cast=[], draft_only=True)
    man_p = Path(rec["chain_dir"]) / "manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8"))
    assert man["spec"]["cq_strength"] == 0.0           # new chains record it
    man["spec"].pop("cq_strength")
    man_p.write_text(json.dumps(man), encoding="utf-8")
    built = []
    real = ce.LtxChain.__init__

    def spy(self, spec, *a, **kw):
        built.append(spec)
        real(self, spec, *a, **kw)
    ce.LtxChain.__init__ = spy
    try:
        cs.resume_chain(rec, LTX_ENTRY, _Uploads())
    finally:
        ce.LtxChain.__init__ = real
    assert built[-1].cq_strength == 0.6


H3_ENTRY = {"family": "h3", "comfy_root": "ComfyUI_LTX", "workflows": {"refs": "x"}}


@pytest.mark.parametrize("refine", [True, False])
def test_h3_chain_restarts_the_server_before_each_take_decode(studio_sandbox, monkeypatch, refine):
    import json
    import chain_engine as ce
    lf = cs.h3_link_frames(192, 2)
    take = lf[0] + lf[1] - 39

    class Runner(_FilesRunner):
        def run(self, name, graph):
            for n in graph.values():
                if n["class_type"] == "StudioSaveVideoLossless":
                    p = self.out / (n["inputs"]["relpath"] + ".mkv")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"x")
                    p.with_suffix(".json").write_text(json.dumps({"frames": take}), encoding="utf-8")
                    _FilesRunner.calls.append(name)
                    return {"status": "success"}
            return super().run(name, graph)

        def fresh_process(self, name):
            _FilesRunner.calls.append(f"fresh:{name}")
    monkeypatch.setattr(cs, "StudioRunner", Runner)

    def finish_take(self, variant, out_mp4, ff, **kw):
        Path(out_mp4).write_bytes(b"mp4")
        return {"mp4": str(out_mp4), "seams": self.spec.seam_frames(), "qa": {"passed": True}, "qa_summary": "ok"}
    monkeypatch.setattr(ce.H3Chain, "finish_take", finish_take)
    rec = {"id": "4343434343434343"}
    cs.run_chain(rec, H3_ENTRY, _Uploads(), ["a", "b"], negative="", base_seed=7, frames=192,
                 start_image=None, cast=[], tier_graph=_tier_graph(refine))
    steps = ["link_l1", "link_l2", "fresh:decode_B0", "decode_B0"]
    assert _FilesRunner.calls == steps + (["refcond_R0", "fresh:refine_R0", "refine_R0", "fresh:decode_R0",
                                           "decode_R0"] if refine else [])
    assert rec["status"] == "completed"


def test_legacy_defaults_name_real_spec_fields():
    import dataclasses
    import chain_engine as ce
    assert set(cs.LTX_LEGACY_CARRY) <= {f.name for f in dataclasses.fields(ce.LtxChainSpec)}
    assert set(cs.H3_LEGACY_CARRY) <= {f.name for f in dataclasses.fields(ce.H3ChainSpec)}
    assert ce.H3ChainSpec(chain_id="h", links=[], refs=[]).seam_adain == 1.0     # new chains match


@pytest.fixture
def comfy_uploads():
    """ComfyUI's /upload/image as the studio uses it: the file is stored under the name
    in the form (overwrite=true) and that name is returned."""
    import http.server
    import json
    import re
    import threading
    stored: dict[str, bytes] = {}

    class Upload(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
            stored[name] = body
            out = json.dumps({"name": name, "subfolder": "", "type": "input"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, format, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upload)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], stored
    srv.shutdown()
    srv.server_close()


def test_uploads_are_named_after_their_content(comfy_uploads, tmp_path):
    import re

    from renderer import ComfyClient
    port, stored = comfy_uploads
    a, a_again, b = tmp_path / "a.png", tmp_path / "a_again.png", tmp_path / "b.png"
    a.write_bytes(b"one still")
    a_again.write_bytes(b"one still")
    b.write_bytes(b"another still")
    client = ComfyClient(port)
    names = [client.upload_image(p) for p in (a, a_again, a, b)]
    assert names[0] == names[1] == names[2] != names[3]
    assert re.fullmatch(r"kf_[0-9a-f]{16}\.png", names[0])
    assert sorted(stored) == sorted({names[0], names[3]})
    assert b'name="overwrite"\r\n\r\ntrue' in stored[names[0]]


def test_a_worker_upload_is_named_like_a_local_one(comfy_uploads, tmp_path, monkeypatch):
    import workers
    from renderer import ComfyClient
    still = tmp_path / "still.png"
    still.write_bytes(b"one still")
    remote = workers.RemoteWorkerClient("rig2")
    monkeypatch.setattr(remote, "_req", lambda *a, **kw: {})
    assert remote.upload_image(still) == ComfyClient(comfy_uploads[0]).upload_image(still)


def test_a_chain_started_again_reuses_every_finished_step(studio_sandbox, comfy_uploads, tmp_path):
    """What a re-queued or retried movie take does: the same chain id, seeds, prompts and
    stills, uploaded again through the real ComfyUI client. The graphs are unchanged,
    so no step renders a second time."""
    from renderer import ComfyClient
    port, stored = comfy_uploads
    tail = tmp_path / "tail.png"
    tail.write_bytes(b"the previous shot's tail frame")

    def start():
        _FilesRunner.calls = []
        rec = {"id": "7a4e7a4e7a4e7a4e"}
        cs.run_chain(rec, LTX_ENTRY, ComfyClient(port), ["a", "b"], negative="", base_seed=7, frames=121,
                     start_image=tail, cast=[])
        assert rec["status"] == "completed"
        return _FilesRunner.calls
    assert start() == ["draft_l1", "draft_l2", "audio_take", "take_draft",
                       "finish_F0_l1", "finish_F0_l2", "take_final_F0"]
    assert start() == []
    assert len(stored) == 1


def test_a_retake_of_one_link_renders_it_and_the_links_after_it_only(studio_sandbox, comfy_uploads, tmp_path):
    """A movie retake inside a take: the same chain id with only that shot's seed
    moved (orchestrator.REROLL_SEED). The link before it keeps its draft and
    finish, so its latents carry into the retaken link; that link, the one after
    it (it reads the new carry) and the take decodes render again."""
    from orchestrator import REROLL_SEED
    from renderer import ComfyClient
    port, _ = comfy_uploads
    tail = tmp_path / "tail.png"
    tail.write_bytes(b"the previous shot's tail frame")

    def start(seeds):
        _FilesRunner.calls = []
        rec = {"id": "7a4e7a4e7a4e7a4f"}
        cs.run_chain(rec, LTX_ENTRY, ComfyClient(port), ["a", "b", "c"], negative="", base_seed=7, frames=121,
                     start_image=tail, cast=[], seeds=seeds)
        assert rec["status"] == "completed"
        return _FilesRunner.calls
    assert len(start([7, 8, 9])) == 9
    time.sleep(0.05)                       # the re-rendered carry files get a later mtime
    assert start([7, 8 + REROLL_SEED, 9]) == ["draft_l2", "draft_l3", "audio_take", "take_draft",
                                              "finish_F0_l2", "finish_F0_l3", "take_final_F0"]


def test_a_chain_continued_from_a_clip_opens_on_its_staged_tail_frame(test_client, sandbox_root, tmp_path,
                                                                     monkeypatch):
    """↪ stages the clip's tail frame in the ComfyUI input folders; a chain names that
    file as its start image. The absolute path of the thumbnail copy is refused, and so
    is a file name that is in no input folder."""
    import threading

    import continuity
    import video_models
    import video_studio
    tail = tmp_path / "tail.png"
    tail.write_bytes(b"tail frame")
    clip = sandbox_root / "output" / "videostudio" / "chain_0a0a0a0a_final.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"mp4")
    monkeypatch.setattr(continuity, "extract_tail", lambda video, name, n=5: str(tail))
    monkeypatch.setattr(video_models, "model_status", lambda mid: {"available": True})
    monkeypatch.setattr(video_studio, "_CHAINS", {})
    monkeypatch.setattr(cs, "CHAIN_DIR", tmp_path / "chains")
    started, ran = [], threading.Event()

    def run_chain(cid, mid, prompts, mode, negative, seed, width, height, frames, steps, cfg,
                  start_image, worker, references, start_path=None, seeds=None):
        started.append(start_image)
        ran.set()
    monkeypatch.setattr(video_studio, "_run_chain", run_chain)

    cont = test_client.post("/api/videostudio/continue_from", json={"name": clip.name}).json()
    staged = [sandbox_root / d / "input" / cont["input_filename"] for d in ("ComfyUI", "ComfyUI_LTX")]
    try:
        assert cont["ok"] and all(p.is_file() for p in staged)
        body = {"model": "ltx25_s6_cq", "prompts": ["a", "b"], "mode": "i2v", "frames": 121}
        r = test_client.post("/api/videostudio/chain", json={**body, "start_image": cont["input_filename"]}).json()
        assert r["status"] == "running" and ran.wait(5) and started == [cont["input_filename"]]
        r = test_client.post("/api/videostudio/chain", json={**body, "start_image": cont["path"]}).json()
        assert r["ok"] is False and "must be a filename" in r["error"]
        r = test_client.post("/api/videostudio/chain", json={**body, "start_image": "vs_cont_none.png"}).json()
        assert r["ok"] is False and "not in a ComfyUI input folder" in r["error"]
        assert started == [cont["input_filename"]]
    finally:
        for p in (clip, Path(cont["path"]), *staged):
            p.unlink(missing_ok=True)
