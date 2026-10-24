"""install/install_components.py and its catalog, install/components.json.

No network: the catalog checks are structural, and the install steps run
against local fixture archives."""
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def ic(sandbox_root):
    spec = importlib.util.spec_from_file_location(
        "install_components", sandbox_root / "install" / "install_components.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_components"] = mod             # dataclasses look the module up
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("install_components", None)


@pytest.fixture(scope="module")
def cat(ic):
    return ic.load_catalog()


# ---------------------------------------------------------------- catalog
def test_every_reference_in_the_catalog_resolves(cat):
    for cid, c in cat["components"].items():
        assert c["title"] and c["summary"], cid
        for t in c.get("trees", []):
            assert t in cat["trees"], (cid, t)
        for n in c.get("nodes", []):
            assert n in cat["nodes"], (cid, n)
            assert cat["nodes"][n]["tree"] in c["trees"], (cid, n)
        for m in c.get("models", []):
            assert m in cat["models"] and not cat["models"][m].get("manual"), (cid, m)
        for m in c.get("manual", []):
            assert cat["models"][m].get("manual"), (cid, m)


def test_every_download_is_pinned(cat):
    for t in cat["trees"].values():
        assert re.fullmatch(r"[0-9a-f]{40}", t["core"]["commit"]), t["dir"]
    for nid, n in cat["nodes"].items():
        src = n["source"]
        if src["type"] == "git":
            assert re.fullmatch(r"[0-9a-f]{40}", src["commit"]), nid
        else:
            assert src["type"] == "archive" and HEX64.match(src["sha256"]), nid
    for mid, m in cat["models"].items():
        assert m["path"].count("/") >= 2, mid
        if m.get("derive"):
            assert cat["models"][m["derive"]["from"]]["sources"] and not m.get("sources"), mid
        elif m.get("manual"):
            assert not m.get("sources") and m["needed_by"], mid
            assert m["size"] is None or (m["size"] > 0 and HEX64.match(m["sha256"])), mid
        else:
            assert m["size"] > 0 and HEX64.match(m["sha256"]) and m["sources"], mid
    ff = cat["tools"]["ffmpeg"]
    assert HEX64.match(ff["sha256"]) and ff["url"].startswith("https://")


def test_a_derived_file_comes_after_its_source_in_the_same_component(cat):
    for cid, c in cat["components"].items():
        ms = c.get("models", [])
        for i, mid in enumerate(ms):
            d = cat["models"][mid].get("derive")
            if d:
                assert d["from"] in ms[:i], (cid, mid)


def test_h3_weights_are_never_downloaded(cat):
    h3 = cat["components"]["h3"]
    assert not h3.get("models") and h3["manual"]
    for mid, m in cat["models"].items():
        if "minimax_h3" in m["path"].lower() or "minimax-h3" in m["path"].lower() \
                or mid in h3["manual"]:
            assert m.get("manual") and not m.get("sources"), mid
            assert m["published_at"].startswith("https://huggingface.co/"), mid
    assert "EU" in " ".join(h3["licenses"])


def test_ltx_and_qwen_entries_match_the_videolab_manifest(cat):
    vl = json.loads((REPO / "videolab" / "configs" / "models.manifest.json")
                    .read_text(encoding="utf-8"))["assets"]
    shared = [m for m in cat["models"] if m in vl]
    assert len(shared) >= 12
    for mid in shared:
        a, m = vl[mid], cat["models"][mid]
        assert m["size"] == a["size_bytes"], mid
        if not m.get("manual"):
            assert m["sha256"] == (a.get("sha256") or a.get("sha256_source")), mid
            assert m["sources"][0] == {"hf": a["repo"], "file": a["repo_path"]}, mid


def _catalog_files(cat, cids):
    return {Path(cat["models"][m]["path"]).name
            for c in cids for m in cat["components"][c].get("models", [])
            + cat["components"][c].get("manual", [])}


WEIGHT = re.compile(r'"([^"{}\\/]+\.(?:safetensors|gguf|pth|onnx|pt))"')


def test_the_catalog_covers_every_weight_the_studio_loads(cat, sandbox_root):
    import video_models

    import models
    have = _catalog_files(cat, cat["components"])
    for mid, m in models.MODELS.items():
        for key, fname in m["files"].items():
            if re.fullmatch(r"[A-Z0-9_]+", fname):        # a workflow placeholder
                continue
            assert fname in have, f"image model {mid}: {key} {fname}"
    for name in (models.IPADAPTER_FILE, models.CLIP_VISION_FILE):
        assert name in have
    wf_dir = sandbox_root / "tool" / "workflows"
    for mid, v in video_models.VIDEO_MODELS.items():
        if v.get("status") == "legacy" and v.get("family") != "wan22_ti2v_5b":
            continue                                        # retired entries
        if "_wan14b" in str(v.get("path")) or "ltx-video-2b" in str(v.get("path")):
            continue
        for key in ("path", "text_encoder", "vae", "vae_audio", "latent_upscaler"):
            if v.get(key):
                assert Path(v[key]).name in have, f"video tier {mid}: {v[key]}"
        names = {v.get("workflow"), v.get("studio_workflow"), *(v.get("workflows") or {}).values()}
        for wf in filter(None, names):
            p = wf_dir / f"{wf}.json"
            for fname in WEIGHT.findall(p.read_text(encoding="utf-8")) if p.is_file() else []:
                assert fname in have, f"video tier {mid}: {wf}.json loads {fname}"
    chains = (sandbox_root / "tool" / "chain_graphs.py").read_text(encoding="utf-8")
    for fname in WEIGHT.findall(chains):
        assert fname in have, f"chain_graphs.py loads {fname}"


# ---------------------------------------------------------------- planning
def test_resolve_expands_components_in_order_without_duplicates(ic, cat):
    plan = ic.resolve(cat, ["h3", "ltx25"])
    assert plan.components == ["ltx25", "h3"]               # catalog order
    assert plan.trees == ["ltx"]
    assert len(plan.nodes) == len(set(plan.nodes))
    assert plan.manual and not any(cat["models"][m].get("manual") for m in plan.models)
    farm = ic.resolve(cat, ["zimage"])
    assert farm.trees == ["farm"] and "farm:ComfyUI-GGUF" in farm.nodes
    assert set(ic.resolve(cat, ["ltx25", "qwen21", "post"]).components) == {
        c for c, v in cat["components"].items() if v.get("default")}
    with pytest.raises(ic.InstallError):
        ic.resolve(cat, ["nope"])


def test_model_state_uses_extra_roots_only_where_the_studio_does(ic, tmp_path):
    entry = {"path": "models/vae/x.safetensors", "size": 3, "extra_roots_ok": True}
    extra = tmp_path / "elsewhere"
    (extra / "vae").mkdir(parents=True)
    (extra / "vae" / "x.safetensors").write_bytes(b"abc")
    assert ic.model_state(tmp_path, entry, [extra])[0] == "ok"
    assert ic.model_state(tmp_path, {**entry, "extra_roots_ok": False}, [extra])[0] == "missing"
    (extra / "vae" / "x.safetensors").write_bytes(b"abcd")
    assert ic.model_state(tmp_path, entry, [extra])[0] == "wrong size"


# ---------------------------------------------------------------- file steps
def test_copy_missing_never_overwrites(ic, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "a").mkdir(parents=True)
    (src / "a" / "new.py").write_text("upstream")
    (src / "tracked.yaml").write_text("upstream")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("x")
    dst.mkdir()
    (dst / "tracked.yaml").write_text("ours")
    assert ic.copy_missing(src, dst) == (1, 1)
    assert (dst / "tracked.yaml").read_text() == "ours"
    assert (dst / "a" / "new.py").read_text() == "upstream"
    assert not (dst / ".git").exists()


def test_safe_extract_refuses_paths_outside_the_target(ic, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ic.InstallError):
        ic.safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("pack-abc/__init__.py", "")
    assert ic.safe_extract(good, tmp_path / "out2").name == "pack-abc"


def test_register_extra_dir_keeps_comments_and_is_idempotent(ic, tmp_path):
    (tmp_path / "tool").mkdir()
    sp = tmp_path / "tool" / "settings.yaml"
    sp.write_text("paths:\n  root: .\n  # weights outside the tree\n  extra_model_dirs: []\n"
                  "  shots: shots\n", encoding="utf-8")
    folder = tmp_path / "H3 models"
    assert "added" in ic.register_extra_dir(tmp_path, folder)
    assert "already" in ic.register_extra_dir(tmp_path, folder)
    other = tmp_path / "more"
    ic.register_extra_dir(tmp_path, other)
    import yaml
    cfg = yaml.safe_load(sp.read_text(encoding="utf-8"))
    assert cfg["paths"]["extra_model_dirs"] == [folder.as_posix(), other.as_posix()]
    assert cfg["paths"]["shots"] == "shots"
    assert "# weights outside the tree" in sp.read_text(encoding="utf-8")
    assert ic.extra_model_roots(tmp_path)[-2:] == [folder, other]


def test_installer_installs_a_tree_pack_and_model_from_pinned_sources(ic, tmp_path, monkeypatch):
    """The whole flow on a fake catalog: archives come from local files."""
    root = tmp_path / "studio"
    (root / "temp").mkdir(parents=True)
    (root / "Tree" / "custom_nodes" / "Pack").mkdir(parents=True)
    (root / "Tree" / "extra_model_paths.yaml").write_text("ours")      # a tracked file
    core_zip = tmp_path / "core.zip"
    with zipfile.ZipFile(core_zip, "w") as z:
        z.writestr("ComfyUI-abc/main.py", "")
        z.writestr("ComfyUI-abc/comfyui_version.py", '__version__ = "9.9.9"\n')
        z.writestr("ComfyUI-abc/extra_model_paths.yaml", "upstream")
    pack_zip = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack_zip, "w") as z:
        z.writestr("__init__.py", "")
    weight = b"weights!"
    import hashlib

    def fake_http(url, dest, size=None, tries=3):
        data = {"core": core_zip.read_bytes(), "pack": pack_zip.read_bytes(), "w": weight}
        key = "core" if "codeload" in url else "pack" if url.endswith(".zip") else "w"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data[key])

    monkeypatch.setattr(ic, "http_download", fake_http)
    cat = {
        "trees": {"t": {"dir": "Tree", "core": {"repo": "o/r", "commit": "a" * 40, "version": "9.9.9"}}},
        "nodes": {"t:Pack": {"tree": "t", "dir": "Pack", "source": {
            "type": "archive", "url": "https://example.invalid/pack.zip",
            "sha256": hashlib.sha256(pack_zip.read_bytes()).hexdigest()}}},
        "models": {"w": {"path": "models/vae/w.safetensors", "size": len(weight),
                         "sha256": hashlib.sha256(weight).hexdigest(),
                         "sources": [{"url": "https://example.invalid/w"}]}},
        "components": {}, "tools": {},
    }
    inst = ic.Installer(root, cat)
    inst.core("t")
    inst.node("t:Pack")
    inst.model("w", [])
    assert ic.tree_state(root, cat["trees"]["t"]) == "ok"
    assert (root / "Tree" / "extra_model_paths.yaml").read_text() == "ours"
    assert ic.node_state(root, cat, "t:Pack") == "ok"
    assert (root / "models" / "vae" / "w.safetensors").read_bytes() == weight
    # a corrupted download is deleted, not kept
    cat["models"]["bad"] = {**cat["models"]["w"], "path": "models/vae/bad.safetensors",
                            "sha256": "0" * 64}
    with pytest.raises(ic.InstallError):
        inst.model("bad", [])
    assert not (root / "models" / "vae" / "bad.safetensors").exists()


def test_a_derived_file_is_exported_and_its_shapes_checked(ic, tmp_path, monkeypatch):
    root = tmp_path / "studio"
    (root / "temp").mkdir(parents=True)
    (root / "ComfyUI" / "bbox").mkdir(parents=True)
    (root / "ComfyUI" / "bbox" / "face.pt").write_bytes(b"pt")
    cat = {"models": {
        "pt": {"path": "ComfyUI/bbox/face.pt", "size": 2, "sha256": None, "sources": []},
        "onnx": {"path": "ComfyUI/onnx/face.onnx", "size": None, "sha256": None,
                 "derive": {"from": "pt", "export": {"format": "onnx"},
                            "expect": {"input": [1, 3, 640, 640], "output": [1, 5, 8400]}}}}}
    shapes = {"output": [[1, 5, 8400]]}

    def fake_run(cmd, cwd, **kw):
        assert cmd[1] == "-I" and Path(cmd[4]).parent == Path(cwd)     # works on a copy
        assert kw["env"]["CUDA_VISIBLE_DEVICES"] == "-1" and kw["env"]["YOLO_AUTOINSTALL"] == "false"
        out = Path(cwd) / "face.onnx"
        out.write_bytes(b"onnx")
        info = {"path": str(out), "input": [1, 3, 640, 640], **shapes}
        return type("R", (), {"returncode": 0, "stdout": json.dumps(info), "stderr": ""})()

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    ic.Installer(root, cat).model("onnx", [])
    assert (root / "ComfyUI" / "onnx" / "face.onnx").read_bytes() == b"onnx"
    assert (root / "ComfyUI" / "bbox" / "face.pt").read_bytes() == b"pt"
    (root / "ComfyUI" / "onnx" / "face.onnx").unlink()
    shapes["output"] = [[1, 84, 8400]]
    with pytest.raises(ic.InstallError, match="expected"):
        ic.Installer(root, cat).model("onnx", [])
    assert not (root / "ComfyUI" / "onnx" / "face.onnx").exists()


def test_the_manual_files_are_listed_with_where_they_go(ic, cat, tmp_path, capsys):
    plan = ic.resolve(cat, ["h3", "realvis"])
    ic.print_manual(tmp_path, cat, plan, [])
    out = capsys.readouterr().out
    assert "MiniMax H3" in out and "RealVisXL V5" in out and "--h3-dir" in out
    for mid in plan.manual:
        assert str(tmp_path / cat["models"][mid]["path"]) in out
    assert out.count("published at https://huggingface.co/") >= len(cat["components"]["h3"]["manual"])
