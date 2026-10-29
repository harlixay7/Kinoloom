"""tool/model_files.py: where the studio finds each model file, and the
copies a user already has, under any name, used instead of a download."""
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

import model_files as mf


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cat(**entries) -> dict:
    """A catalog of small files: name=(path, content) or (path, content, extra)."""
    models = {}
    for mid, spec in entries.items():
        path, data, *more = spec
        e = {"path": path, "size": len(data), "sha256": _sha(data), "sources": [{"url": "x"}]}
        if more:
            e.update(more[0])
        models[mid] = e
    return {"models": models,
            "components": {"part": {"title": "A part", "models": list(models)}}}


def _put(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture(autouse=True)
def _drives(tmp_path, monkeypatch):
    """Links on other drives as on Windows (on any system), never a folder at
    the top of a real drive, and no symbolic links unless a test allows them
    (Windows allows them only in Developer Mode)."""
    monkeypatch.setattr(mf, "DRIVE_LINKS", True)
    monkeypatch.setattr(mf, "drive_link_dir", lambda src: tmp_path / "drive-top" / mf.LINK_DIR)
    monkeypatch.setattr(mf.Finder, "can_symlink", lambda self, src, dst: False)


def _other_drive(monkeypatch, tmp_path, drive: Path, links: bool = True) -> Path:
    """Files under `drive` are on another drive, whose top is drive/top; hard
    links work only within one drive (and nowhere on it with links=False).
    Returns that drive's Kinoloom-models folder."""
    on = (lambda p: drive in Path(p).parents)
    monkeypatch.setattr(mf, "drive_link_dir", lambda src: (drive / "top" if on(src) else
                                                          tmp_path / "drive-top") / mf.LINK_DIR)
    monkeypatch.setattr(mf.Finder, "can_link",
                        lambda self, src, dst: on(src) == on(dst) and (links or not on(src)))
    return drive / "top" / mf.LINK_DIR


# ------------------------------------------------------------------ folders
def test_a_folder_is_read_as_typed_or_pasted():
    assert mf.clean('  "C:\\ComfyUI\\models"  ') == "C:\\ComfyUI\\models"
    assert mf.clean("'D:/AI stuff/models'") == "D:/AI stuff/models"
    assert mf.clean(None) == ""
    assert mf.listed_dirs("C:/ComfyUI/models") == ["C:/ComfyUI/models"]     # one string = one folder
    assert mf.listed_dirs(['"C:\\a"', "", None, 3, "D:/b"]) == ["C:\\a", "D:/b"]
    assert mf.listed_dirs(None) == [] and mf.listed_dirs({"x": 1}) == []
    assert mf.is_network_path("\\\\server\\share\\models") and mf.is_network_path("//nas/m")
    assert not mf.is_network_path("C:\\models")


def test_model_folders_come_from_the_environment_then_the_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KINOLOOM_EXTRA_MODEL_PATHS", str(tmp_path / "env"))
    cfg = {"paths": {"extra_model_dirs": ["rel/models", str(tmp_path / "abs")]}}
    assert mf.extra_roots(cfg, tmp_path) == [tmp_path / "env", tmp_path / "rel" / "models",
                                            tmp_path / "abs"]
    monkeypatch.delenv("KINOLOOM_EXTRA_MODEL_PATHS")
    # a single folder written as a plain string is that one folder
    assert mf.extra_roots({"paths": {"extra_model_dirs": str(tmp_path / "x")}}, tmp_path) == [
        tmp_path / "x"]


def test_a_drive_is_never_a_model_folder(tmp_path):
    assert mf.is_drive_root(Path(tmp_path.anchor))
    assert not mf.is_drive_root(tmp_path)


# ------------------------------------------------------------------ finding
def test_a_file_counts_in_a_model_folder_in_its_place_with_its_size(tmp_path):
    root, extra = tmp_path / "studio", tmp_path / "comfy"
    rel = "models/diffusion_models/m.gguf"
    assert mf.tails(rel) == ["diffusion_models/m.gguf", "unet/m.gguf"]
    # in the order ComfyUI looks: text_encoders/ before the older clip/
    assert mf.tails("models/clip/c.safetensors") == ["text_encoders/c.safetensors", "clip/c.safetensors"]
    assert mf.tails("ComfyUI/models/SEEDVR2/s.gguf") == []
    old = _put(extra / "unet" / "m.gguf", b"abc")              # ComfyUI's older name
    assert mf.find(rel, [extra], root, size=3) == old
    assert mf.find(rel, [extra], root, size=4) is None          # another build
    assert mf.other_builds(rel, [extra], 4) == [old]
    own = _put(root / rel, b"abcd")                             # the studio's own copy wins
    assert mf.find(rel, [extra], root, size=3) == own
    assert mf.find(rel, [tmp_path / "missing"], tmp_path / "nowhere") is None


def test_a_file_counts_only_where_comfyui_loads_it_from(tmp_path):
    """ComfyUI loads the first file of that name, in the order of the model
    folders and of their subfolders: another build there is what it loads."""
    root, a, b = tmp_path / "studio", tmp_path / "a", tmp_path / "b"
    rel = "models/vae/v.st"
    _put(a / "vae" / "v.st", b"abcd")                           # another build, first
    good = _put(b / "vae" / "v.st", b"abc")
    assert mf.find(rel, [a, b], root, size=3) is None
    assert mf.find(rel, [b, a], root, size=3) == good
    one = tmp_path / "one"
    _put(one / "diffusion_models" / "d.gguf", b"abcd")
    _put(one / "unet" / "d.gguf", b"abc")
    assert mf.find("models/diffusion_models/d.gguf", [one], root, size=3) is None
    # another letter case: ComfyUI's list of names does not have it
    _put(tmp_path / "c" / "loras" / "L.st", b"abc")
    assert mf.find("models/loras/l.st", [tmp_path / "c"], root, size=3) is None
    assert mf.find("models/loras/L.st", [tmp_path / "c"], root, size=3) == tmp_path / "c" / "loras" / "L.st"


def test_mount_root_is_the_folder_a_file_sits_in_by_its_catalog_place(tmp_path):
    rel = "models/loras/l.safetensors"
    assert mf.mount_root(tmp_path / "comfy" / "models" / "loras" / "l.safetensors", rel) == \
        tmp_path / "comfy" / "models"
    assert mf.mount_root(tmp_path / "C" / "Models" / "LORAS" / "l.safetensors", rel) == \
        tmp_path / "C" / "Models"                                # folder names: any case
    assert mf.mount_root(tmp_path / "C" / "models" / "loras" / "L.safetensors", rel) is None
    assert mf.mount_root(tmp_path / "m" / "loras" / "ltx" / "l.safetensors", rel) is None
    assert mf.mount_root(tmp_path / "models" / "loras" / "other.safetensors", rel) is None
    # a folder not named models counts with two of the subfolders ComfyUI reads
    (tmp_path / "h3" / "unet").mkdir(parents=True)
    assert mf.mount_root(tmp_path / "h3" / "unet" / "d.gguf", "models/diffusion_models/d.gguf") is None
    (tmp_path / "h3" / "vae").mkdir()
    assert mf.mount_root(tmp_path / "h3" / "unet" / "d.gguf", "models/diffusion_models/d.gguf") == \
        tmp_path / "h3"
    drive = Path(tmp_path.anchor)
    assert mf.mount_root(drive / "loras" / "l.safetensors", rel) is None   # never a whole drive


# ------------------------------------------------------------------ search
def test_a_file_under_another_name_is_found_by_its_sha256(tmp_path):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(vae=("models/vae/kino_vae.safetensors", b"V" * 40))
    mine = _put(tmp_path / "comfy" / "stuff" / "my vae (old).safetensors", b"V" * 40)
    _put(tmp_path / "comfy" / "same size, other bytes.bin", b"W" * 40)
    f = mf.Finder(cat, ["vae"], root, say=lambda s: None)
    found = f.search([tmp_path / "comfy"])
    assert [(x.mid, x.src, x.checked) for x in found] == [("vae", mine.resolve(), "sha256")]
    assert f.missing() == []


def test_the_catalog_name_and_size_are_enough_without_reading_the_file(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(vae=("models/vae/kino_vae.safetensors", b"V" * 40))
    _put(tmp_path / "comfy" / "vae" / "kino_vae.safetensors", b"V" * 40)
    monkeypatch.setattr(mf, "sha256_file", lambda *a, **k: pytest.fail("hashed"))
    found = mf.Finder(cat, ["vae"], root, say=lambda s: None).search([tmp_path / "comfy"])
    assert found[0].checked == "name and size"


def test_two_files_of_one_size_are_told_apart_by_their_sha256(tmp_path):
    """MiniMax H3's ref2va and fl2va transformers have exactly the same size."""
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(a=("models/diffusion_models/a.st", b"A" * 64),
               b=("models/diffusion_models/b.st", b"B" * 64))
    fa = _put(tmp_path / "mine" / "x1.st", b"B" * 64)
    fb = _put(tmp_path / "mine" / "x2.st", b"A" * 64)
    found = {x.mid: x.src for x in
             mf.Finder(cat, ["a", "b"], root, say=lambda s: None).search([tmp_path / "mine"])}
    assert found == {"a": fb.resolve(), "b": fa.resolve()}


def test_a_second_search_reads_no_file_again(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(vae=("models/vae/v.st", b"V" * 40))
    _put(tmp_path / "mine" / "renamed.st", b"V" * 40)
    reads = []
    real = mf.sha256_file
    monkeypatch.setattr(mf, "sha256_file", lambda p, *a, **k: reads.append(p) or real(p))
    mf.Finder(cat, ["vae"], root, say=lambda s: None).search([tmp_path / "mine"])
    assert len(reads) == 1 and (root / mf.HASHES).is_file()
    mf.Finder(cat, ["vae"], root, say=lambda s: None).search([tmp_path / "mine"])
    assert len(reads) == 1                                     # from the cache
    # another name of the same file (a hard link, as Kinoloom makes) is not read either
    os.link(tmp_path / "mine" / "renamed.st", _put(tmp_path / "other" / "x", b"").with_name("again.st"))
    mf.Finder(cat, ["vae"], root, say=lambda s: None).search([tmp_path / "other"])
    assert len(reads) == 1
    # a dry run reads the cache and writes nothing
    (root / mf.HASHES).unlink()
    mf.Finder(cat, ["vae"], root, say=lambda s: None, remember=False).search([tmp_path / "mine"])
    assert not (root / mf.HASHES).exists()


def test_a_file_without_a_size_in_the_catalog_is_found_by_its_name_only(tmp_path):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = {"models": {"rv": {"path": "models/clip/clip_l.safetensors", "manual": True},
                      "onnx": {"path": "ComfyUI/onnx/f.onnx", "derive": {"from": "x"}}},
           "components": {}}
    _put(tmp_path / "mine" / "other.safetensors", b"x")
    _put(tmp_path / "mine" / "lfs" / "clip_l.safetensors", b"version https://git-lfs")  # a pointer
    src = _put(tmp_path / "mine" / "deep" / "CLIP_L.safetensors", b"x" * mf.MIN_BYTES)
    _put(tmp_path / "mine" / "f.onnx", b"o")
    f = mf.Finder(cat, ["rv", "onnx"], root, say=lambda s: None)
    assert list(f.entries) == ["rv"]                           # a derived file is made, not found
    found = f.search([tmp_path / "mine"])
    assert [(x.mid, x.src, x.checked) for x in found] == [("rv", src.resolve(), "name")]


def test_a_file_that_cannot_be_read_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(a=("models/loras/a.st", b"A" * 40), b=("models/vae/b.st", b"B" * 30))
    busy = _put(tmp_path / "mine" / "busy.bin", b"Z" * 30)          # b's size, locked
    lora = _put(tmp_path / "mine" / "renamed lora.st", b"A" * 40)
    real = mf.sha256_file

    def locked(p, *a, **k):
        if Path(p).name == busy.name:
            raise PermissionError(13, "Permission denied")
        return real(p, *a, **k)

    monkeypatch.setattr(mf, "sha256_file", locked)
    said: list[str] = []
    found = mf.Finder(cat, ["a", "b"], root, say=said.append).search([tmp_path / "mine"])
    assert [(x.mid, x.src) for x in found] == [("a", lora.resolve())]
    assert any("could not read" in s and "busy.bin" in s for s in said)


def test_the_search_skips_partial_files_and_code_folders(tmp_path):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(vae=("models/vae/v.st", b"V" * 40))
    _put(tmp_path / "mine" / "v.st.part", b"V" * 40)
    _put(tmp_path / "mine" / "v.st.crdownload", b"V" * 40)
    _put(tmp_path / "mine" / "venv" / "Lib" / "site-packages" / "v.st", b"V" * 40)
    _put(tmp_path / "mine" / ".git" / "v.st", b"V" * 40)
    assert mf.Finder(cat, ["vae"], root, say=lambda s: None).search([tmp_path / "mine"]) == []


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_a_junction_that_loops_is_walked_once(tmp_path):
    import _winapi
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(vae=("models/vae/v.st", b"V" * 40))
    src = _put(tmp_path / "mine" / "sub" / "renamed.st", b"V" * 40)
    loop = tmp_path / "mine" / "sub" / "loop"
    _winapi.CreateJunction(str(tmp_path / "mine"), str(loop))
    try:
        seen = list(mf._walk(tmp_path / "mine", set()))
        assert [os.path.basename(p) for p, _ in seen] == ["renamed.st"]
        found = mf.Finder(cat, ["vae"], root, say=lambda s: None).search([tmp_path / "mine"])
        assert found[0].src == src.resolve()
    finally:
        os.rmdir(loop)                                         # the junction, not its target


def test_how_a_found_file_is_used(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    (root / "models").mkdir(parents=True)
    cat = _cat(l1=("models/loras/kino.st", b"L" * 40), l2=("models/loras/other.st", b"M" * 40),
               h3=("models/diffusion_models/h.st", b"H" * 40),
               rife=("ComfyUI/custom_nodes/F/ckpts/rife/rife47.pth", b"R" * 40))
    comfy = tmp_path / "B" / "comfy"
    _put(comfy / "models" / "loras" / "kino.st", b"L" * 40)
    _put(comfy / "renamed.st", b"M" * 40)
    _put(tmp_path / "B" / "home" / "loras" / "h.st", b"H" * 40)     # its name, not a model folder
    _put(tmp_path / "B" / "post" / "rife47.pth", b"R" * 40)

    def found():
        f = mf.Finder(cat, list(cat["models"]), root, say=lambda s: None)
        return {x.mid: x for x in f.search([tmp_path / "B"])}

    # on Kinoloom's drive: every one linked into models/, whatever its name or place
    assert {m: x.how for m, x in found().items()} == dict.fromkeys(cat["models"], "link")
    # on another drive: in a model folder under its own name it is used there;
    # otherwise it is linked on its own drive, in that drive's Kinoloom-models
    links = _other_drive(monkeypatch, tmp_path, tmp_path / "B")
    got = found()
    assert got["l1"].how == "folder" and got["l1"].folder == (comfy / "models").resolve()
    assert (got["l2"].how, got["l2"].folder) == ("drivelink", links)
    assert (got["h3"].how, got["h3"].folder) == ("drivelink", links)
    assert got["l2"].target(root) == links / "loras" / "other.st"
    assert "no extra space" in got["l2"].describe() and str(links) in got["l2"].describe()
    # a file ComfyUI reads only from inside Kinoloom's folder: no model folder can hold it
    assert got["rife"].how == "copy" and "only from Kinoloom's own folder" in got["rife"].describe()
    # a drive without hard links (exFAT, say): a symbolic link where Windows
    # allows one, else a copy
    _other_drive(monkeypatch, tmp_path, tmp_path / "B", links=False)
    monkeypatch.setattr(mf.Finder, "can_symlink", lambda self, src, dst: True)
    assert found()["l2"].how == "symlink" and found()["l1"].how == "folder"
    monkeypatch.setattr(mf.Finder, "can_symlink", lambda self, src, dst: False)
    assert found()["l2"].how == "copy" and "cannot hold a link" in found()["l2"].describe()
    # a network share gets no folder made at its top
    _other_drive(monkeypatch, tmp_path, tmp_path / "B")
    monkeypatch.setattr(mf, "on_network_drive", lambda p: True)
    assert found()["l2"].how == "copy" and found()["l1"].how == "folder"
    # elsewhere than on Windows a symbolic link is the usual way, and no folder
    # is made at / either
    monkeypatch.setattr(mf, "on_network_drive", lambda p: False)
    monkeypatch.setattr(mf, "DRIVE_LINKS", False)
    monkeypatch.setattr(mf.Finder, "can_symlink", lambda self, src, dst: True)
    assert found()["l2"].how == "symlink"


def test_a_network_share_is_told_apart_from_a_local_folder(tmp_path):
    assert mf.on_network_drive(Path("\\\\nas\\share\\models\\x.st"))
    assert not mf.on_network_drive(tmp_path / "x.st")


def test_the_link_test_leaves_nothing_behind(tmp_path, monkeypatch):
    # read-only files, as users keep them: on Windows that attribute is shared
    # by every name of a file and stops any of them being deleted
    src = _put(tmp_path / "mine" / "w.st", b"w")
    other = _put(tmp_path / "mine" / "x.st", b"x")
    for p in (src, other):
        os.chmod(p, stat.S_IREAD)
    said: list[str] = []
    f = mf.Finder({"models": {}, "components": {}}, [], tmp_path, say=said.append)
    assert f.can_link(src, tmp_path / "studio" / "models" / "vae" / "w.st")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mine"]     # no folder made, no probe left
    # at the top of a drive, where Windows lets a user make a folder but not a
    # file, the test runs in the folder the links would go in, made and removed
    top = tmp_path / "top"
    top.mkdir()
    monkeypatch.setattr(mf, "is_drive_root", lambda p: Path(p) == top)
    made = []
    real_mkdir = Path.mkdir
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: made.append(self) or real_mkdir(self, *a, **k))
    assert f.can_link(src, top / mf.LINK_DIR / "vae" / "w.st")
    assert made == [top / mf.LINK_DIR] and list(top.iterdir()) == []
    # the next file is tested the same way, and comes out the same
    f2 = mf.Finder({"models": {}, "components": {}}, [], tmp_path, say=said.append)
    assert f2.can_link(src, top / mf.LINK_DIR / "vae" / "w.st")
    assert f2.can_link(other, top / mf.LINK_DIR / "loras" / "x.st") and list(top.iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "mine").iterdir()) == ["w.st", "x.st"] and not said
    for p in (src, other):                                     # the user's files as they were
        assert not os.access(p, os.W_OK) and os.stat(p).st_nlink == 1


def test_a_link_kinoloom_made_to_a_read_only_file_can_be_removed(tmp_path):
    mine = _put(tmp_path / "mine" / "w.st", b"w")
    os.chmod(mine, stat.S_IREAD)
    os.link(mine, tmp_path / "kinoloom name.st")
    mf.remove_name(tmp_path / "kinoloom name.st")
    assert not (tmp_path / "kinoloom name.st").exists()
    assert mine.read_bytes() == b"w" and not os.access(mine, os.W_OK) and os.stat(mine).st_nlink == 1


def test_the_symbolic_link_test_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.undo()                                          # the real test
    src = _put(tmp_path / "mine" / "w.st", b"w")
    (tmp_path / "studio" / "models").mkdir(parents=True)
    f = mf.Finder({"models": {}, "components": {}}, [], tmp_path / "studio", say=lambda s: None)
    ok = f.can_symlink(src, tmp_path / "studio" / "models" / "vae" / "w.st")
    assert isinstance(ok, bool) and list((tmp_path / "studio" / "models").iterdir()) == []


# ------------------------------------------------------------------ using
def test_a_link_is_the_same_file_and_deleting_it_keeps_the_users_file(tmp_path):
    root = tmp_path / "studio"
    cat = _cat(vae=("models/vae/v.st", b"V" * 40))
    mine = _put(tmp_path / "mine" / "renamed.st", b"V" * 40)
    found = mf.Found("vae", "models/vae/v.st", mine, 40, "link", "sha256")
    mf.use(found, root, cat)
    dst = root / "models" / "vae" / "v.st"
    assert os.path.samefile(dst, mine) and os.stat(mine).st_nlink == 2
    assert mf.sources(root)["models/vae/v.st"]["from"] == str(mine)
    assert mf.status(cat, {}, root)[0]["how"] == "link"
    dst.unlink()
    assert mine.read_bytes() == b"V" * 40


def test_a_copy_is_checked_and_a_bad_one_never_used(tmp_path):
    root = tmp_path / "studio"
    cat = _cat(vae=("models/vae/v.st", b"V" * 40))
    mine = _put(tmp_path / "mine" / "renamed.st", b"V" * 40)
    mf.use(mf.Found("vae", "models/vae/v.st", mine, 40, "copy", "sha256"), root, cat)
    dst = root / "models" / "vae" / "v.st"
    assert dst.read_bytes() == b"V" * 40 and not os.path.samefile(dst, mine)
    assert mf.sources(root)["models/vae/v.st"]["how"] == "copy"
    dst.unlink()
    mine.write_bytes(b"X" * 40)                                # changed since the search
    with pytest.raises(OSError, match="sha256"):
        mf.use(mf.Found("vae", "models/vae/v.st", mine, 40, "copy", "sha256"), root, cat)
    assert not dst.exists() and not dst.with_name("v.st.part").exists()
    # a file put at the catalog path since the search is never replaced
    mine.write_bytes(b"V" * 40)
    dst.write_bytes(b"theirs")
    with pytest.raises(FileExistsError):
        mf.use(mf.Found("vae", "models/vae/v.st", mine, 40, "copy", "sha256"), root, cat)
    assert dst.read_bytes() == b"theirs"


def test_a_link_on_its_own_drive_is_the_users_file_under_kinoloom_s_name(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    rel = "models/text_encoders/te.st"
    cat = _cat(te=(rel, b"T" * 40))
    mine = _put(tmp_path / "B" / "AI stuff" / "my text encoder (v2).st", b"T" * 40)
    links = _other_drive(monkeypatch, tmp_path, tmp_path / "B")
    (f,) = mf.Finder(cat, ["te"], root, say=lambda s: None).search([tmp_path / "B"])
    assert f.how == "drivelink"
    mf.use(f, root, cat)
    dst = links / "text_encoders" / "te.st"
    assert os.path.samefile(dst, mine) and os.stat(mine).st_nlink == 2
    assert "takes no extra space" in " ".join((links / "README.txt").read_text(encoding="utf-8").split())
    assert not (root / rel).exists()                            # nothing in Kinoloom's own folder
    assert mf.sources(root)[rel]["where"] == str(dst)
    cfg = {"paths": {"extra_model_dirs": [str(links)]}}
    row = mf.status(cat, cfg, root)[0]
    assert (row["state"], row["how"], row["from"]) == ("ok", "drivelink", str(mine))
    mf.use(f, root, cat)                                         # again: nothing to do, no error
    assert mine.read_bytes() == b"T" * 40 and mine.name == "my text encoder (v2).st"
    # the folder deleted: the user's file stays, and the model is missing again
    for p in sorted(links.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    links.rmdir()
    assert mine.read_bytes() == b"T" * 40
    assert mf.status(cat, cfg, root)[0]["state"] == "missing"


def test_a_symbolic_link_points_at_the_users_file(tmp_path):
    root = tmp_path / "studio"
    mine = _put(tmp_path / "usb" / "renamed.st", b"S" * 40)
    try:
        os.symlink(mine, tmp_path / "probe")
    except (OSError, NotImplementedError):
        pytest.skip("this Windows does not allow symbolic links (no Developer Mode)")
    cat = _cat(s=("models/vae/s.st", b"S" * 40))
    mf.use(mf.Found("s", "models/vae/s.st", mine, 40, "symlink", "sha256"), root, cat)
    dst = root / "models" / "vae" / "s.st"
    assert os.path.islink(dst) and os.path.samefile(dst, mine)
    row = mf.status(cat, {}, root)[0]
    assert (row["state"], row["how"], row["from"]) == ("ok", "symlink", str(mine))
    # the user moved the file: the link to where it was is replaced, no error
    moved = tmp_path / "usb" / "moved.st"
    os.rename(mine, moved)
    assert mf.status(cat, {}, root)[0]["state"] == "missing"
    mf.use(mf.Found("s", "models/vae/s.st", moved, 40, "symlink", "sha256"), root, cat)
    assert os.path.samefile(dst, moved) and mf.status(cat, {}, root)[0]["state"] == "ok"


def test_linking_again_what_is_already_linked_is_no_error(tmp_path):
    root = tmp_path / "studio"
    cat = _cat(vae=("models/vae/v.st", b"V" * 40))
    mine = _put(tmp_path / "mine" / "renamed.st", b"V" * 40)
    f = mf.Found("vae", "models/vae/v.st", mine, 40, "link", "sha256")
    mf.use(f, root, cat)
    mf.use(f, root, cat)
    # a copy an earlier run made, of the right size, is kept as it is
    other = _put(tmp_path / "other" / "same.st", b"V" * 40)
    mf.use(mf.Found("vae", "models/vae/v.st", other, 40, "copy", "sha256"), root, cat)
    assert os.path.samefile(root / "models" / "vae" / "v.st", mine)


def test_a_file_already_in_a_kinoloom_models_folder_is_found_there_again(tmp_path, monkeypatch):
    """The folder taken off the model folders by hand: the next search finds
    the links in it again, and uses them as they are."""
    root = tmp_path / "studio"
    cat = _cat(te=("models/text_encoders/te.st", b"T" * 40), v=("models/vae/v.st", b"V" * 40))
    links = _other_drive(monkeypatch, tmp_path, tmp_path / "B")
    a = _put(tmp_path / "B" / "x" / "renamed te.st", b"T" * 40)
    b = _put(tmp_path / "B" / "x" / "renamed v.st", b"V" * 40)
    for f in mf.Finder(cat, ["te", "v"], root, say=lambda s: None).search([tmp_path / "B" / "x"]):
        mf.use(f, root, cat)
    again = mf.Finder(cat, ["te", "v"], root, say=lambda s: None).search([links])
    assert {f.how for f in again} == {"folder"} and {f.folder for f in again} == {links}
    assert os.path.samefile(links / "text_encoders" / "te.st", a)
    assert os.path.samefile(links / "vae" / "v.st", b)


# ------------------------------------------------------------------ letter case
def test_comfyui_loads_a_file_in_another_letter_case_when_its_name_is_listed(tmp_path):
    """On Windows ComfyUI opens the first file of a name in any letter case;
    its graph check wants the exact name in its list, from any folder."""
    root, a, b = tmp_path / "studio", tmp_path / "a", tmp_path / "b"
    _put(a / "vae" / "V.st", b"abc")                            # the same file, other case
    _put(b / "vae" / "v.st", b"abc")
    assert mf.find("models/vae/v.st", [a, b], root, size=3) is not None
    assert mf.find("models/vae/v.st", [a], root, size=3) is None


@pytest.mark.skipif(sys.platform != "win32", reason="a file system that ignores letter case")
def test_kinoloom_s_own_copy_in_another_letter_case_is_renamed(tmp_path):
    root = tmp_path / "studio"
    cat = _cat(vae=("models/vae/ltx-vae.st", b"V" * 40), other=("models/vae/o.st", b"O" * 40))
    _put(root / "models" / "vae" / "LTX-VAE.st", b"V" * 40)
    rows = {r["id"]: r for r in mf.status(cat, {}, root)}
    assert rows["vae"]["state"] == "letter case" and rows["vae"]["where"].endswith("LTX-VAE.st")
    assert mf.find("models/vae/ltx-vae.st", [], root, size=40) is None   # ComfyUI would not list it
    said: list[str] = []
    assert mf.repair_case(root, cat, said.append) == [root / "models" / "vae" / "ltx-vae.st"]
    assert os.listdir(root / "models" / "vae") == ["ltx-vae.st"] and "renamed" in said[0]
    assert mf.status(cat, {}, root)[0]["state"] == "ok" and mf.repair_case(root, cat) == []


# ------------------------------------------------------------------ settings
EXAMPLE = """# Kinoloom settings
paths:
  root: .
  # weights outside the tree
  extra_model_dirs: []
  shots: shots
ui:
  host: 127.0.0.1
"""


def _settings(tmp_path, text: str, crlf: bool = False) -> Path:
    sp = tmp_path / "tool" / "settings.yaml"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return sp


@pytest.mark.parametrize("value", [
    "  extra_model_dirs: []\n",
    "  extra_model_dirs:\n  - C:/old\n",                         # how the web app saves a list
    "  extra_model_dirs:\n    - C:/old\n    - 'D:/x'\n",
    "  extra_model_dirs: [C:/old]\n",
    "  extra_model_dirs: C:/old\n",                              # one folder as a string
    "",                                                         # no key at all
])
def test_a_model_folder_is_added_whatever_the_list_looks_like(tmp_path, value):
    text = EXAMPLE.replace("  extra_model_dirs: []\n", value)
    sp = _settings(tmp_path, text)
    before = yaml.safe_load(text)
    folder = tmp_path / "AI, video's #2" / "models"               # a comma, a quote, a hash
    assert "added" in mf.add_dir_to_settings(tmp_path, folder)
    after = yaml.safe_load(sp.read_text(encoding="utf-8"))
    old = mf.listed_dirs(before["paths"].get("extra_model_dirs"))
    assert after["paths"]["extra_model_dirs"] == old + [folder.as_posix()]
    assert {k: v for k, v in after["paths"].items() if k != "extra_model_dirs"} == \
        {k: v for k, v in before["paths"].items() if k != "extra_model_dirs"}
    assert after["ui"] == before["ui"]
    assert "# weights outside the tree" in sp.read_text(encoding="utf-8")
    assert "already" in mf.add_dir_to_settings(tmp_path, folder)


@pytest.mark.parametrize("crlf", [True, False])
def test_adding_a_folder_keeps_the_files_line_endings(tmp_path, crlf):
    sp = _settings(tmp_path, EXAMPLE, crlf=crlf)
    mf.add_dir_to_settings(tmp_path, tmp_path / "m")
    raw = sp.read_bytes()
    assert raw.count(b"\r\n") == (raw.count(b"\n") if crlf else 0)
    assert yaml.safe_load(raw)["paths"]["extra_model_dirs"] == [(tmp_path / "m").as_posix()]


def test_a_kinoloom_models_folder_goes_first(tmp_path):
    sp = _settings(tmp_path, EXAMPLE.replace("[]", "\n  - 'C:/ComfyUI/models'"))
    mf.add_dir_to_settings(tmp_path, tmp_path / "B" / mf.LINK_DIR, first=True)
    assert yaml.safe_load(sp.read_text(encoding="utf-8"))["paths"]["extra_model_dirs"] == [
        (tmp_path / "B" / mf.LINK_DIR).as_posix(), "C:/ComfyUI/models"]


def test_model_folders_are_set_as_a_whole_list(tmp_path):
    sp = _settings(tmp_path, EXAMPLE.replace("[]", "\n  - 'C:/a'\n  - D:/b"))
    mf.set_dirs_in_settings(tmp_path, ["D:/b"])
    assert yaml.safe_load(sp.read_text(encoding="utf-8"))["paths"]["extra_model_dirs"] == ["D:/b"]
    mf.set_dirs_in_settings(tmp_path, [])
    text = sp.read_text(encoding="utf-8")
    assert "  extra_model_dirs: []\n" in text and "# weights outside the tree" in text
    assert yaml.safe_load(text) == yaml.safe_load(EXAMPLE)


def test_a_settings_file_this_cannot_edit_is_written_plainly_and_stays_valid(tmp_path):
    odd = "paths: {root: ., extra_model_dirs: [], shots: shots}\nui: {host: 127.0.0.1}\n"
    sp = _settings(tmp_path, odd)
    mf.add_dir_to_settings(tmp_path, tmp_path / "m")
    cfg = yaml.safe_load(sp.read_text(encoding="utf-8"))
    assert cfg == {"paths": {"root": ".", "extra_model_dirs": [(tmp_path / "m").as_posix()],
                             "shots": "shots"}, "ui": {"host": "127.0.0.1"}}


# ------------------------------------------------------------------ report
def test_the_report_says_where_each_file_is(tmp_path):
    root, extra = tmp_path / "studio", tmp_path / "comfy"
    cat = _cat(own=("models/vae/own.st", b"O" * 4), folder=("models/vae/f.st", b"F" * 4),
               gone=("models/vae/g.st", b"G" * 4), bad=("models/vae/b.st", b"B" * 4))
    _put(root / "models" / "vae" / "own.st", b"O" * 4)
    _put(extra / "vae" / "f.st", b"F" * 4)
    _put(extra / "vae" / "g.st", b"G" * 9)                      # another build
    _put(root / "models" / "vae" / "b.st", b"B" * 5)
    rows = {r["id"]: r for r in mf.status(cat, {"paths": {"extra_model_dirs": [str(extra)]}}, root)}
    assert (rows["own"]["state"], rows["own"]["how"]) == ("ok", "own")
    assert (rows["folder"]["state"], rows["folder"]["how"]) == ("ok", "folder")
    assert rows["gone"]["state"] == "missing"
    assert rows["gone"]["other_builds"] == [str(extra / "vae" / "g.st")]
    assert rows["bad"]["state"] == "other build"
