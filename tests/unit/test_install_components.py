"""install/install_components.py and its catalog, install/components.json.

No network: the catalog checks are structural, and the install steps run
against local fixture archives."""
import importlib.util
import json
import re
import subprocess
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
    assert {"python", "git", "ffmpeg"} <= set(cat["tools"])
    for tid, t in cat["tools"].items():
        assert t["url"].startswith("https://") and t["size"] > 0 and HEX64.match(t["sha256"]), tid


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


def test_skip_models_installs_runtimes_without_listing_weights(ic, capsys):
    """INSTALL.bat's plan (--components all --skip-models --yes): no weights,
    and no 'bring your own' lists to confuse a first install."""
    assert ic.main(["--components", "all", "--skip-models", "--yes", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "0 file(s) to download" in out and "== Weights ==" not in out
    assert "Weights you bring yourself" not in out
    assert "(--check lists them)" in out and "listed at the end" not in out
    assert ic.main(["--components", "h3", "--yes", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Weights you bring yourself" in out and "(listed at the end)" in out


def test_steps_show_their_progress_and_slow_ones_their_time(ic, cat, tmp_path, monkeypatch,
                                                            capsys):
    """The window title counts the steps, so a long install visibly moves; a
    step of 30 s or more says how long it took."""
    import types
    titles: list[str] = []
    monkeypatch.setattr(ic, "set_title", titles.append)
    clock = iter([0.0, 95.0, 100.0, 101.0])
    monkeypatch.setattr(ic, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    def boom():
        raise ic.InstallError("no")

    inst = ic.Installer(tmp_path, cat)
    inst.total = 2
    assert inst.step("ComfyUI_LTX\\venv", lambda: None)
    assert not inst.step("ffmpeg", boom)
    out = capsys.readouterr().out
    assert titles == ["Kinoloom install - 1 of 2: ComfyUI_LTX\\venv - keep this window open",
                      "Kinoloom install - 2 of 2: ffmpeg - keep this window open"]
    assert "took 1 min 35 s" in out and out.count("took") == 1
    assert "FAIL ffmpeg: no" in out
    assert ic.duration(12.4) == "12 s"


def test_a_pinned_archive_of_the_wrong_size_is_deleted(ic, tmp_path, monkeypatch):
    """tools.ffmpeg's zip is checked by size as well as SHA-256."""
    import hashlib
    data = b"not the pinned zip"

    def fake_http(url, dest, size=None, tries=3):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    monkeypatch.setattr(ic, "http_download", fake_http)
    src = {"type": "archive", "url": "https://example.invalid/ffmpeg.zip",
           "size": len(data) + 1, "sha256": hashlib.sha256(data).hexdigest()}
    with pytest.raises(ic.InstallError, match="size"):
        ic.fetch_source(src, tmp_path)
    assert not (tmp_path / "ffmpeg.zip").exists()


def test_the_manual_files_are_listed_with_where_they_go(ic, cat, tmp_path, capsys):
    plan = ic.resolve(cat, ["h3", "realvis"])
    ic.print_manual(tmp_path, cat, plan, [])
    out = capsys.readouterr().out
    assert "MiniMax H3" in out and "RealVisXL V5" in out and "--h3-dir" in out
    for mid in plan.manual:
        assert str(tmp_path / cat["models"][mid]["path"]) in out
    assert out.count("published at https://huggingface.co/") >= len(cat["components"]["h3"]["manual"])


# ---------------------------------------------------------------- scripts
def test_a_venv_counts_as_built_only_when_pip_finished_it(ic, tmp_path):
    """No marker left, every package with its RECORD, every pin installed."""
    (tmp_path / "req.txt").write_text("# pins\nTorch==2.13.0+cu126\npy-yaml==6.0\n",
                                      encoding="utf-8")
    tree = {"dir": "T", "venv": {"requirements": "req.txt"}}
    assert ic.venv_state(tmp_path, tree) == "missing"
    venv = tmp_path / "T" / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    site = venv / "Lib" / "site-packages"
    for d in ("torch-2.13.0+cu126.dist-info", "PY_yaml-6.0.dist-info"):
        (site / d).mkdir(parents=True)
        (site / d / "RECORD").write_text("", encoding="utf-8")
    assert ic.venv_state(tmp_path, tree) == "ok"
    (venv / ic.INCOMPLETE).write_text("", encoding="utf-8")
    assert ic.venv_state(tmp_path, tree) == "incomplete"          # pip did not finish
    (venv / ic.INCOMPLETE).unlink()
    (site / "torch-2.13.0+cu126.dist-info" / "RECORD").unlink()
    assert ic.venv_state(tmp_path, tree) == "incomplete"          # torch broke off part-way
    (site / "torch-2.13.0+cu126.dist-info").rename(site / "torch-2.14.1+cu126.dist-info")
    (site / "torch-2.14.1+cu126.dist-info" / "RECORD").write_text("", encoding="utf-8")
    assert ic.venv_state(tmp_path, tree) == "incomplete"          # not the pinned torch


def test_a_venv_pip_did_not_finish_is_built_again(ic, tmp_path, monkeypatch, capsys):
    """A failed pip run leaves the marker and fails the step, with no unpinned
    second try on top of the half-installed packages. The next run deletes the
    venv and builds it from the pinned list again."""
    import types
    (tmp_path / "req.txt").write_text("torch==2.13.0+cu126\n", encoding="utf-8")
    cat = {"trees": {"t": {"dir": "T", "venv": {"requirements": "req.txt",
                                                "index": "https://example.invalid/whl"}}}}
    venv = tmp_path / "T" / "venv"
    calls: list[list[str]] = []
    pip_rc = [1]

    def fake_run(args, check=False, **kw):
        args = [str(a) for a in args]
        calls.append(args)
        if args[1:3] == ["-m", "venv"]:
            (Path(args[3]) / "Scripts").mkdir(parents=True)
            (Path(args[3]) / "Scripts" / "python.exe").write_bytes(b"")
        elif "-r" in args and pip_rc[0] == 0:
            d = venv / "Lib" / "site-packages" / "torch-2.13.0+cu126.dist-info"
            d.mkdir(parents=True)
            (d / "RECORD").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args, pip_rc[0] if "-r" in args else 0)

    monkeypatch.setattr(ic, "subprocess", types.SimpleNamespace(run=fake_run))
    inst = ic.Installer(tmp_path, cat)
    with pytest.raises(ic.InstallError, match="builds this venv again"):
        inst.venv("t")
    assert [c[-1] for c in calls if "-r" in c] == [str(tmp_path / "req.txt")]
    assert ic.venv_state(tmp_path, cat["trees"]["t"]) == "incomplete"

    (venv / "Lib").mkdir()
    (venv / "Lib" / "left-over.txt").write_text("x", encoding="utf-8")
    pip_rc[0] = 0
    inst.venv("t")
    assert "was not finished last time; building it again" in capsys.readouterr().out
    assert not (venv / "Lib" / "left-over.txt").exists()
    assert ic.venv_state(tmp_path, cat["trees"]["t"]) == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="the 260-character limit is Windows'")
def test_remove_tree_reaches_files_past_260_characters(ic, tmp_path):
    import os
    import stat
    venv = tmp_path / "venv"
    deep = "\\\\?\\" + str(venv / ("d" * 120) / ("e" * 120))
    os.makedirs(deep)
    f = deep + "\\LICENSE.txt"
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("x")
    os.chmod(f, stat.S_IREAD)
    assert len(f) - len("\\\\?\\") > 260
    ic.remove_tree(venv)
    assert not venv.exists()


INSTALL_SCRIPTS = ("INSTALL.bat", "start.bat", "install/INSTALL-MAIN.bat", "install/bootstrap.ps1")


def test_the_install_scripts_are_plain_ascii():
    """cmd.exe and Windows PowerShell 5.1 read a file without a byte-order mark
    in the PC's ANSI code page, where any other character can change meaning."""
    for rel in INSTALL_SCRIPTS:
        assert (REPO / rel).read_bytes().isascii(), rel


def test_bootstrap_takes_its_pins_from_the_catalog():
    text = (REPO / "install" / "bootstrap.ps1").read_text(encoding="ascii")
    assert "$Cat.tools.python" in text and "$Cat.tools.git" in text
    assert not re.search(r"[0-9a-fA-F]{64}", text)          # no hash of its own


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_bootstrap_parses_in_windows_powershell():
    script = REPO / "install" / "bootstrap.ps1"
    check = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
             f"'{script}', [ref]$null, [ref]$errors); $errors.Count")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", check],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "0", r.stdout + r.stderr


LONG_PATH_HARNESS = r"""
param([string]$Script, [string]$Root, [string]$Answer, [string]$Works)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
$fn = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                     $a.Name -eq 'Assert-Machine' }, $true)[0].Extent.Text
$MaxShortRoot = 45
function Say([string]$t) { Write-Host $t }
function Stop-Bootstrap([string]$t) { Write-Host "[FAIL] $t"; exit 1 }
function Confirm-Step([string]$q, [bool]$d) { Write-Host "ASKED $q $d"; return $Answer -eq 'y' }
function Test-LongPaths { return $false }
function Enable-LongPaths { return $Works -eq 'y' }
. ([scriptblock]::Create($fn))
Assert-Machine
Write-Host 'CONTINUES'
"""


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
@pytest.mark.parametrize("root, answer, works, code, shows", [
    ("C:\\Users\\someone\\Desktop\\Kinoloom-main\\Kinoloom-main", "y", "y", 0, "long paths are on"),
    ("C:\\Users\\someone\\Desktop\\Kinoloom-main\\Kinoloom-main", "n", "y", 1, "like C:\\Kinoloom"),
    ("C:\\Users\\someone\\Desktop\\Kinoloom-main\\Kinoloom-main", "y", "n", 1, "was not changed"),
    ("C:\\Kinoloom", "n", "n", 0, "CONTINUES"),
])
def test_a_long_folder_path_asks_to_turn_on_long_paths(tmp_path, root, answer, works, code, shows):
    """bootstrap.ps1's own check, with the prompt and the registry stubbed: a long
    path asks (default yes) and goes on once long paths are on; declined, or not
    allowed, it stops with the move advice; a short path asks nothing."""
    harness = tmp_path / "harness.ps1"
    harness.write_text(LONG_PATH_HARNESS, encoding="ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(harness), "-Script", str(REPO / "install" / "bootstrap.ps1"),
                        "-Root", root, "-Answer", answer, "-Works", works],
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == code and shows in out, out
    assert ("ASKED Turn on long paths? True" in out) == (root != "C:\\Kinoloom"), out
    assert ("CONTINUES" in out) == (code == 0), out


def test_the_resolve_job_uses_the_catalog_indexes(cat):
    """dependency-audit.yml resolves each venv list with the index its tree uses."""
    wf = (REPO / ".github" / "workflows" / "dependency-audit.yml").read_text(encoding="utf-8")
    assert '"tool/requirements.txt"' in wf
    for t in cat["trees"].values():
        if t.get("venv"):
            spec = t["venv"]
            assert f'file = "{spec["requirements"]}"; index = "{spec["index"]}"' in wf, t["dir"]