"""The studio's side of the model folders: image and video models count in
them, and Settings > Model files (routers/model_folders.py) adds and removes
them and finds the files a user already has."""
import copy
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

import model_files as mf


def _put(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------ the studio's checks
def test_image_models_count_in_a_model_folder(tmp_path, monkeypatch):
    import models
    ck = next(m for m in models.MODELS.values() if m.get("loader") == "checkpoint")
    gg = next(m for m in models.MODELS.values() if "unet" in m["files"])
    ck_rel = f"models/checkpoints/{ck['files']['ckpt']}"
    gg_rel = f"models/diffusion_models/{gg['files']['unet']}"
    folder = tmp_path / "ComfyUI" / "models"
    cfg = {"paths": {"extra_model_dirs": [str(folder)]}}
    monkeypatch.setattr(models, "load_config", lambda: cfg)
    monkeypatch.setattr(mf, "catalog_size", lambda rel, root=mf.ROOT: {ck_rel: 3, gg_rel: 3}.get(rel))
    assert not models.is_available(ck["id"]) and not models.is_available(gg["id"])
    _put(folder / "checkpoints" / ck["files"]["ckpt"], b"abcd")      # another build
    assert not models.is_available(ck["id"])
    _put(folder / "checkpoints" / ck["files"]["ckpt"], b"abc")
    _put(folder / "unet" / gg["files"]["unet"], b"abc")              # ComfyUI's older name
    assert models.is_available(ck["id"]) and models.is_available(gg["id"])


def test_video_model_files_count_in_a_model_folder(tmp_path, monkeypatch):
    import video_models
    folder = tmp_path / "comfy models"
    monkeypatch.setattr(video_models, "load_config",
                        lambda: {"paths": {"extra_model_dirs": str(folder)}})
    rel = "models/text_encoders/te.safetensors"
    monkeypatch.setattr(mf, "catalog_size", lambda r, root=mf.ROOT: {rel: 3}.get(r))
    assert not video_models._exists(rel)
    _put(folder / "clip" / "te.safetensors", b"abc")
    assert video_models._exists(rel)
    _put(folder / "SEEDVR2" / "s.gguf", b"abc")
    assert not video_models._exists("ComfyUI/models/SEEDVR2/s.gguf")


def test_media_roots_take_a_model_folder_written_as_one_string(tmp_path):
    import core
    d = tmp_path / "m"
    d.mkdir()
    roots = core.media_roots({"paths": {"extra_model_dirs": str(d)}})
    assert d.resolve() in roots
    assert Path(d.anchor).resolve() not in roots


# ------------------------------------------------------------------ Settings > Model files
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


FILES = {"a": ("models/vae/a.st", b"A" * 40), "b": ("models/loras/b.st", b"B" * 40),
         "c": ("models/vae/c.st", b"C" * 40), "d": ("models/vae/d.st", b"D" * 40),
         "e": ("models/vae/e.st", b"E" * 40)}


@pytest.fixture
def studio(tmp_path, monkeypatch, sandbox_root):
    """The router on a studio of its own (a small catalog, the example
    settings with their comments); the live settings' model folders are put
    back afterwards. Links on other drives as on Windows, never a folder at
    the top of a real drive, and no symbolic links."""
    import core
    from routers import model_folders as r
    monkeypatch.setattr(mf, "DRIVE_LINKS", True)
    monkeypatch.setattr(mf, "drive_link_dir", lambda src: tmp_path / "drive-top" / mf.LINK_DIR)
    monkeypatch.setattr(mf.Finder, "can_symlink", lambda self, src, dst: False)
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = {"models": {m: {"path": p, "size": len(d), "sha256": _sha(d),
                          "sources": [{"url": "x"}]} for m, (p, d) in FILES.items()},
           "components": {"part": {"title": "A part", "models": list(FILES)}}}
    _put(root / "install" / "components.json", json.dumps(cat).encode())
    _put(root / "tool" / "settings.yaml",
         (sandbox_root / "tool" / "settings.example.yaml").read_bytes())
    monkeypatch.setattr(r, "ROOT", root)
    live = core.load_config()
    before = copy.deepcopy((live.get("paths") or {}).get("extra_model_dirs"))
    r._reset(None)
    r._COPIES.clear()
    yield r, root
    live.setdefault("paths", {})["extra_model_dirs"] = before
    r._reset(None)
    r._COPIES.clear()


def _disk_dirs(root: Path, sandbox_root: Path) -> list:
    """paths.extra_model_dirs of the studio's settings file, whose comments
    (those of the example it was made from) must all still be there."""
    import yaml
    text = (root / "tool" / "settings.yaml").read_text(encoding="utf-8")
    example = (sandbox_root / "tool" / "settings.example.yaml").read_text(encoding="utf-8")
    comments = [line for line in example.splitlines() if line.lstrip().startswith("#")]
    assert comments and all(c in text.splitlines() for c in comments)
    return yaml.safe_load(text)["paths"]["extra_model_dirs"]


def _wait(client) -> dict:
    end = time.monotonic() + 30
    while time.monotonic() < end:
        job = client.get("/api/model-files/job").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    pytest.fail("the search or copy did not finish")


def _rows(client) -> dict:
    return {f["id"]: f for f in client.get("/api/model-files").json()["files"]}


def test_model_folders_are_added_and_removed(test_client, studio, tmp_path, sandbox_root):
    import core
    _, root = studio
    folder = tmp_path / "ComfyUI" / "models"
    folder.mkdir(parents=True)
    res = test_client.post("/api/model-files/folders", json={"folder": f'"{folder}"'})
    assert res.status_code == 200 and res.json()["restart"] is True
    assert core.load_config()["paths"]["extra_model_dirs"][-1] == folder.as_posix()
    assert _disk_dirs(root, sandbox_root) == [folder.as_posix()]
    listed = test_client.get("/api/model-files").json()["folders"]
    assert [f["exists"] for f in listed if f["path"] == str(folder)] == [True]
    again = test_client.post("/api/model-files/folders", json={"folder": str(folder) + os.sep})
    assert "already" in again.json()["message"]
    res = test_client.post("/api/model-files/folders", json={"folder": str(folder), "action": "remove"})
    assert res.status_code == 200
    assert folder.as_posix() not in core.load_config()["paths"]["extra_model_dirs"]
    assert _disk_dirs(root, sandbox_root) == []
    assert test_client.post("/api/model-files/folders",
                            json={"folder": str(folder), "action": "remove"}).status_code == 404


def test_a_listed_network_folder_can_be_removed(test_client, studio, sandbox_root):
    _, root = studio
    mf.set_dirs_in_settings(root, ["//nas/share/models"])         # written by hand
    assert [f["listed"] for f in test_client.get("/api/model-files").json()["folders"]] == \
        ["//nas/share/models"]
    res = test_client.post("/api/model-files/folders",
                           json={"folder": "//nas/share/models", "action": "remove"})
    assert res.status_code == 200 and _disk_dirs(root, sandbox_root) == []


@pytest.mark.parametrize("folder, why", [
    ("", "type or paste"),
    ("relative/models", "not a full path"),
    ("\\\\nas\\share\\models", "network folder"),
    ("MISSING", "not a folder"),
    ("DRIVE", "whole drive"),
])
def test_a_model_folder_that_cannot_be_one_is_refused(test_client, studio, tmp_path, folder, why):
    folder = {"MISSING": str(tmp_path / "nope"), "DRIVE": tmp_path.anchor}.get(folder, folder)
    res = test_client.post("/api/model-files/folders", json={"folder": folder})
    assert res.status_code == 400 and why in res.json()["detail"]


def test_the_model_files_api_is_not_open_to_the_lan(make_client, studio, tmp_path, monkeypatch):
    from routers import common
    monkeypatch.setitem(common.cfg, "security", dict(common.cfg.get("security") or {}, api_token=""))
    lan = make_client(base_url="http://192.168.1.10:8080", peer="192.168.1.50")
    assert lan.get("/api/model-files").status_code == 401
    assert lan.post("/api/model-files/folders", json={"folder": str(tmp_path)}).status_code == 401
    assert lan.post("/api/model-files/find", json={}).status_code == 401


def test_a_search_links_uses_in_place_and_offers_copies(test_client, studio, tmp_path, monkeypatch,
                                                        sandbox_root):
    """a: renamed, on Kinoloom's drive -> linked into models/; b: in a ComfyUI
    models folder under its own name on another drive -> that folder is used;
    c: renamed on another drive -> linked on that drive, in its
    Kinoloom-models folder; d: on a drive that cannot hold links -> copied on
    request; e: nowhere -> still missing."""
    import core
    r, root = studio
    mine = tmp_path / "mine"
    other, nolinks = mine / "otherdrive", mine / "usbstick"
    _put(mine / "renamed a.st", FILES["a"][1])
    _put(other / "ComfyUI" / "models" / "loras" / "b.st", FILES["b"][1])
    _put(other / "old" / "renamed c.st", FILES["c"][1])
    _put(nolinks / "renamed d.st", FILES["d"][1])

    def drive(p):
        parts = Path(p).parts
        return "B" if "otherdrive" in parts else ("X" if "usbstick" in parts else "C")
    links = other / "top" / mf.LINK_DIR
    monkeypatch.setattr(mf, "drive_link_dir", lambda src: (other if drive(src) == "B" else nolinks)
                        / "top" / mf.LINK_DIR)
    monkeypatch.setattr(mf.Finder, "can_link",
                        lambda self, src, dst: drive(src) == drive(dst) != "X")
    assert {m: x["state"] for m, x in _rows(test_client).items()} == dict.fromkeys(FILES, "missing")

    res = test_client.post("/api/model-files/find", json={"folder": str(mine)})
    assert res.status_code == 200
    job = _wait(test_client)
    assert job["state"] == "done", job
    assert {f["path"]: f["how"] for f in job["found"]} == {
        "models/vae/a.st": "link", "models/loras/b.st": "folder", "models/vae/c.st": "drivelink"}
    assert [(c["path"], c["size"]) for c in job["copy"]] == [("models/vae/d.st", 40)]
    used = (other / "ComfyUI" / "models").resolve()
    assert sorted(job["added"]) == sorted([str(used), str(links)])
    assert any(mf.same_dir(d, used) for d in core.load_config()["paths"]["extra_model_dirs"])
    assert _disk_dirs(root, sandbox_root) == [links.as_posix(), used.as_posix()]   # links first
    assert os.path.samefile(root / "models" / "vae" / "a.st", mine / "renamed a.st")
    assert os.path.samefile(links / "vae" / "c.st", other / "old" / "renamed c.st")
    assert not (root / "models" / "vae" / "c.st").exists()
    rows = _rows(test_client)
    assert (rows["a"]["state"], rows["a"]["how"]) == ("ok", "link")
    assert (rows["b"]["state"], rows["b"]["how"]) == ("ok", "folder")
    assert (rows["c"]["state"], rows["c"]["how"], rows["c"]["from"]) == (
        "ok", "drivelink", str(other / "old" / "renamed c.st"))
    assert rows["d"]["state"] == rows["e"]["state"] == "missing"

    assert test_client.post("/api/model-files/copy").status_code == 200
    job = _wait(test_client)
    assert job["state"] == "done", job
    assert (root / "models" / "vae" / "d.st").read_bytes() == FILES["d"][1]
    assert not os.path.samefile(root / "models" / "vae" / "d.st", nolinks / "renamed d.st")
    assert (_rows(test_client)["d"]["state"], _rows(test_client)["d"]["how"]) == ("ok", "copy")
    assert test_client.post("/api/model-files/copy").status_code == 400     # nothing left

    # a second search: only e is missing, and nothing is read again
    monkeypatch.setattr(mf, "sha256_file", lambda *a, **k: pytest.fail("hashed again"))
    test_client.post("/api/model-files/find", json={"folder": str(mine)})
    job = _wait(test_client)
    assert job["state"] == "done" and job["found"] == [] and "1 still missing" in job["lines"][-1]
    # the user's files as they were
    for m, name in (("a", mine / "renamed a.st"), ("c", other / "old" / "renamed c.st"),
                    ("d", nolinks / "renamed d.st")):
        assert name.read_bytes() == FILES[m][1]


def test_one_search_or_copy_at_a_time(test_client, studio):
    r, _ = studio
    r._JOB["state"] = "running"
    assert test_client.post("/api/model-files/find", json={}).status_code == 409
    r._reset(None)
    assert test_client.post("/api/model-files/find", json={"folder": "C:/no/such/folder"}).status_code == 400
