"""Start pictures (the Shots page's Start pictures and a shot's picture
studio) open on Qwen-Image 2.1 fast, and a Qwen picture renders on Qwen's own
server, under one GPU lease, from each person's still and the place."""
import json
from pathlib import Path

import gpu_broker
import ltx_refs
import models
import pytest
import renderer
import shot_images


@pytest.fixture
def installed(monkeypatch):
    """The image models whose weights are on disk (add ids to the set)."""
    have: set[str] = set()
    monkeypatch.setattr(models, "is_available", lambda mid: mid in have)
    return have


def _refs(tmp_path, *sources):
    """resolve_reference_set rows for these sources, each with its file."""
    out = []
    for src in sources:
        p = tmp_path / (src.replace(":", "_") + ".png")
        p.write_bytes(b"png")
        out.append({"slot": "background" if src.startswith("location") else "pic1",
                    "path": str(p), "source": src})
    return out


def _resolves(monkeypatch, refs):
    monkeypatch.setattr(ltx_refs, "resolve_reference_set",
                        lambda shot, bible, family=None: (refs, []))


class FakeComfy:
    """A ComfyUI that renders every graph into a small PNG."""
    made: list["FakeComfy"] = []

    def __init__(self, port):
        self.port, self.uploads, self.graphs = port, [], []
        FakeComfy.made.append(self)

    def system_stats(self):
        return {}

    def upload_image(self, path):
        self.uploads.append(Path(path).name)
        return Path(path).name

    def queue_prompt(self, wf):
        self.graphs.append(wf)
        return f"p{len(self.graphs)}"

    def wait_for(self, prompt_id, timeout_s=600):
        return {"status": {"status_str": "success"},
                "outputs": {"9": {"images": [{"filename": "out.png", "subfolder": ""}]}}}

    def download(self, subfolder, filename, dest):
        Path(dest).write_bytes(b"\x89PNG" + b"0" * 200)


@pytest.fixture
def farm(tmp_path, monkeypatch):
    """(gen, leases, released): a picture studio on FakeComfy servers, the
    GPU leases it takes and the ones it gives back."""
    FakeComfy.made.clear()
    monkeypatch.setattr(renderer, "ComfyClient", FakeComfy)
    monkeypatch.setattr(shot_images, "KF_ROOT", tmp_path / "kf")
    leases, released = [], []
    monkeypatch.setattr(gpu_broker, "acquire", lambda **kw: leases.append(kw) or f"lease{len(leases)}")
    monkeypatch.setattr(gpu_broker, "release", released.append)
    return shot_images.ShotImageGen({"ensemble": {"comfyui_port": 8188}}), leases, released


def _render(gen, params, count=1):
    shot = {"shot_id": "s01_001", "project_id": "p1"}
    gen._jobs["s01_001"] = {"total": count, "running": True}
    gen._run(shot, None, {"prompt": "Mara in the hall", "negative": "blurry", **params})


def test_the_pickers_open_on_qwen_fast_and_fall_back_to_an_installed_model(installed):
    installed.update({"qwen21_fast", "qwen21", "realvis"})
    assert models.default_still_model() == "qwen21_fast"
    installed.discard("qwen21_fast")
    assert models.default_still_model() == "qwen21"
    installed.discard("qwen21")
    assert models.default_still_model() == "realvis"
    installed.clear()
    assert models.default_still_model() == "qwen21_fast"        # listed, not installed


def test_qwen_gets_each_persons_still_and_the_place_lead_first(tmp_path, monkeypatch):
    _resolves(monkeypatch, _refs(tmp_path, "character:mara:Armed", "character:tom",
                                 "power:mara:fire", "location:hall"))
    got = shot_images.native_refs({"shot_id": "s01_001"}, None)
    assert [(r["kind"], r["id"], r["path"].name) for r in got] == [
        ("character", "mara", "character_mara_Armed.png"),
        ("location", "hall", "location_hall.png"),
        ("character", "tom", "character_tom.png")]


def test_qwen_gets_at_most_four_pictures_and_none_that_is_missing(tmp_path, monkeypatch):
    refs = _refs(tmp_path, "character:a", "character:b", "character:c", "character:d",
                 "location:hall")
    refs.insert(1, {"slot": "pic2", "path": str(tmp_path / "gone.png"), "source": "character:x"})
    _resolves(monkeypatch, refs)
    got = shot_images.native_refs({"shot_id": "s01_001"}, None)
    assert [r["id"] for r in got] == ["a", "hall", "b", "c"]

    def broken(shot, bible, family=None):
        raise RuntimeError("bible unreadable")
    monkeypatch.setattr(ltx_refs, "resolve_reference_set", broken)
    assert shot_images.native_refs({"shot_id": "s01_001"}, None) == []


def test_the_plan_names_the_default_and_where_its_pictures_are_shown(sandbox_root, monkeypatch, installed):
    from bible import BIBLE_DIR
    installed.add("qwen21_fast")
    still = BIBLE_DIR / "projects" / "p1" / "refs" / "chars" / "mara" / "mara_still.png"
    place = BIBLE_DIR / "projects" / "p1" / "refs" / "locs" / "hall" / "hall.png"
    elsewhere = sandbox_root / "output" / "batch" / "tom.png"
    for p in (still, place, elsewhere):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")
    _resolves(monkeypatch, [
        {"slot": "pic1", "path": str(still), "source": "character:mara"},
        {"slot": "pic2", "path": str(elsewhere), "source": "character:tom"},
        {"slot": "background", "path": str(place), "source": "location:hall"}])
    monkeypatch.setattr(shot_images, "build_auto_prompt", lambda shot, bible: ("a hall", ""))

    class Names:
        def characters(self):
            return {"mara": {"name": "Mara"}}

        def locations(self):
            return {"hall": {"name": "the great hall"}}
    plan = shot_images.ShotImageGen({}).plan({"shot_id": "s01_001", "project_id": "p1"}, Names())
    assert plan["default_model"] == "qwen21_fast"
    assert [(r["label"], r["url"]) for r in plan["native_refs"]] == [
        ("Mara", "/api/herorefs/p1/mara/mara_still.png"),
        ("the great hall", "/api/locrefs/p1/hall/hall.png"),
        ("tom", None)]                                          # shown by name only


def test_a_qwen_picture_takes_its_references_natively():
    gen = shot_images.ShotImageGen({"ensemble": {"comfyui_port": 8188}})
    wf = gen._build_workflow("qwen21_fast", models.MODELS["qwen21_fast"], "qwen21", "a hall",
                             "", 7, 1344, 768, 8, 1.0, [], ["mara.png", "hall.png"], "s01",
                             {}, None)
    te = next(n for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21CPU"
              and n["inputs"].get("measure_dma"))
    assert [wf[te["inputs"][f"image{i}"][0]]["inputs"]["image"] for i in (1, 2)] == \
        ["mara.png", "hall.png"]
    assert "IPAdapter" not in json.dumps(wf)


def test_a_qwen_start_picture_renders_on_qwens_server_under_one_lease(tmp_path, monkeypatch,
                                                                      installed, farm):
    gen, leases, released = farm
    installed.add("qwen21_fast")
    _resolves(monkeypatch, _refs(tmp_path, "character:mara", "location:hall"))
    monkeypatch.setattr(shot_images, "char_identity_refs",
                        lambda shot, bible: pytest.fail("Qwen takes the stills, not face picks"))
    _render(gen, {}, count=2)                                   # no model: the default
    (comfy,) = FakeComfy.made
    assert comfy.port == 8190
    assert [(kw["model"], kw["port"]) for kw in leases] == [("qwen21_fast", 8190)]
    assert released == ["lease1"]
    assert comfy.uploads == ["character_mara.png", "location_hall.png"]
    assert len(comfy.graphs) == 2
    assert all("TextEncodeQwenImage21CPU" in json.dumps(g) for g in comfy.graphs)
    assert sorted(p.name for p in (tmp_path / "kf" / "p1" / "s01_001").glob("img_*.png")) == \
        ["img_0.png", "img_1.png"]


def test_an_sdxl_start_picture_stays_on_the_farm_with_its_face_lock(tmp_path, monkeypatch,
                                                                   installed, farm):
    gen, leases, released = farm
    installed.add("realvis")
    face = tmp_path / "face.png"
    face.write_bytes(b"png")
    monkeypatch.setattr(shot_images, "char_identity_refs", lambda shot, bible: {"mara": face})
    monkeypatch.setattr(shot_images, "location_ref", lambda shot: None)
    _render(gen, {"model": "realvis"})
    (comfy,) = FakeComfy.made
    assert comfy.port == 8188 and leases[0]["port"] == 8188 and released == ["lease1"]
    assert comfy.uploads == ["face.png"]
    assert "IPAdapter" in json.dumps(comfy.graphs[0])


def test_the_lease_is_given_back_when_the_render_fails(tmp_path, monkeypatch, installed, farm):
    gen, leases, released = farm
    installed.add("qwen21_fast")
    _resolves(monkeypatch, [])
    monkeypatch.setattr(FakeComfy, "wait_for", lambda self, pid, timeout_s=600: None)
    _render(gen, {})
    assert len(leases) == 1 and released == ["lease1"]
    assert not list((tmp_path / "kf").rglob("img_*.png"))


def test_start_uses_the_default_model_and_refuses_an_unknown_one(monkeypatch, installed):
    installed.add("qwen21_fast")
    gen = shot_images.ShotImageGen({"ensemble": {"comfyui_port": 8188}})
    monkeypatch.setattr(gen, "_run", lambda shot, bible, params: None)
    shot = {"shot_id": "s01_001", "project_id": "p1"}
    assert gen.start(shot, None, {"model": "no_such"}) == \
        {"ok": False, "error": "unknown model 'no_such'"}
    refused = gen.start(shot, None, {"hires": True})            # Qwen has no hires pass
    assert not refused["ok"] and "Qwen-Image 2.1 (fast" in refused["error"]
    assert gen.start(shot, None, {})["ok"]


def test_the_batch_window_offers_and_uses_the_default(test_client, monkeypatch, installed):
    from routers import cast, common
    installed.add("qwen21_fast")
    pid = common.store.create(title="Start Pictures")["id"]
    opts = test_client.get(f"/api/projects/{pid}/refbatch/options").json()
    assert opts["default_model"] == "qwen21_fast"
    assert "qwen21_fast" in [m["id"] for m in opts["models"]]
    sent = []
    monkeypatch.setattr(cast.batch_gen, "start",
                        lambda p, shots, cfg: sent.append(cfg) or {"ok": True, "total": len(shots)})
    r = test_client.post(f"/api/projects/{pid}/refbatch/start",
                         json={"shots": ["s01_001"], "config": {"model": ""}}).json()
    assert r["ok"] and sent[0]["model"] == "qwen21_fast"
