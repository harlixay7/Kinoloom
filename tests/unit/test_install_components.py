"""install/install_components.py and its catalog, install/components.json.

No network: the catalog checks are structural, and the install steps run
against local fixture archives."""
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
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


def test_model_state_looks_in_the_model_folders_as_the_studio_does(ic, tmp_path):
    entry = {"path": "models/vae/x.safetensors", "size": 3}
    extra = tmp_path / "elsewhere"
    (extra / "vae").mkdir(parents=True)
    (extra / "vae" / "x.safetensors").write_bytes(b"abc")
    assert ic.model_state(tmp_path, entry, [extra]) == ("ok", extra / "vae" / "x.safetensors")
    # another build under the same name in a model folder is not used: the
    # file is still to get, and the user's file stays as it is
    (extra / "vae" / "x.safetensors").write_bytes(b"abcd")
    assert ic.model_state(tmp_path, entry, [extra])[0] == "missing"
    # the studio's own copy of another size is reported, never replaced
    (tmp_path / "models" / "vae").mkdir(parents=True)
    (tmp_path / "models" / "vae" / "x.safetensors").write_bytes(b"abcd")
    assert ic.model_state(tmp_path, entry, [extra])[0] == "wrong size"
    # ComfyUI's older folder names count in a model folder
    (extra / "unet").mkdir()
    (extra / "unet" / "d.gguf").write_bytes(b"abc")
    assert ic.model_state(tmp_path, {"path": "models/diffusion_models/d.gguf", "size": 3},
                          [extra])[0] == "ok"
    # a file the node packs keep inside ComfyUI/ is only looked for there
    (extra / "ultralytics").mkdir()
    (extra / "ultralytics" / "f.pt").write_bytes(b"abc")
    assert ic.model_state(tmp_path, {"path": "ComfyUI/models/ultralytics/f.pt", "size": 3},
                          [extra])[0] == "missing"


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


@pytest.mark.parametrize("relative", [False, True], ids=["full-path-back", "name-back"])
def test_a_derived_file_is_exported_and_its_shapes_checked(ic, tmp_path, monkeypatch, relative):
    root = tmp_path / "O'Neil's studio"                # ultralytics drops a ' from a model path
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
        assert cmd[1] == "-I" and cmd[4] == "face.pt"                   # the copy, by its name only
        assert (Path(cwd) / cmd[4]).read_bytes() == b"pt" and Path(cwd).parent == root / "temp"
        assert kw["env"]["CUDA_VISIBLE_DEVICES"] == "-1" and kw["env"]["YOLO_AUTOINSTALL"] == "false"
        (Path(cwd) / "face.onnx").write_bytes(b"onnx")
        info = {"path": "face.onnx" if relative else str(Path(cwd) / "face.onnx"),
                "input": [1, 3, 640, 640], **shapes}
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


def test_a_failed_export_names_its_error_and_logs_the_rest(ic, tmp_path, monkeypatch):
    root = tmp_path / "studio"
    (root / "temp").mkdir(parents=True)
    (root / "ComfyUI" / "bbox").mkdir(parents=True)
    (root / "ComfyUI" / "bbox" / "face.pt").write_bytes(b"pt")
    cat = {"models": {
        "pt": {"path": "ComfyUI/bbox/face.pt", "size": 2, "sha256": None, "sources": []},
        "onnx": {"path": "ComfyUI/onnx/face.onnx", "size": None, "sha256": None,
                 "derive": {"from": "pt", "export": {"format": "onnx"}}}}}
    trace = ('Traceback (most recent call last):\n  File "x.py", line 1, in <module>\n'
             "    super().__init__(open(name, mode))\nModuleNotFoundError: No module named 'onnx'\n")
    monkeypatch.setattr(ic.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stdout": "", "stderr": trace})())
    logged = []
    monkeypatch.setattr(ic.support, "log", lambda *lines: logged.extend(lines))
    with pytest.raises(ic.InstallError) as err:
        ic.Installer(root, cat).model("onnx", [])
    assert str(err.value) == "the export failed: ModuleNotFoundError: No module named 'onnx'"
    assert any("Traceback" in line for line in logged)
    assert not (root / "ComfyUI" / "onnx").exists()


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


def test_a_download_that_breaks_off_goes_on_where_it_stopped(ic, tmp_path, monkeypatch):
    """The first response stops a third of the way in (the server closes the
    connection); the next try asks for the rest with Range, and the file comes
    out whole. A server that ignores Range sends it all again."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    data = bytes(range(256)) * 4096
    ranges: list[str | None] = []
    honour_range = [True]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            ranges.append(rng)
            if rng is None or not honour_range[0]:
                cut = len(data) if len(ranges) > 1 else len(data) // 3
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:cut])
                self.close_connection = True
                return
            start = int(rng.split("=")[1].rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])

        def log_message(self, format, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    waits: list[float] = []
    monkeypatch.setattr(ic.time, "sleep", waits.append)
    monkeypatch.setattr(ic.support, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ic.support, "INSTALL_LOG", tmp_path / "logs" / "install.log")
    url = f"http://127.0.0.1:{srv.server_port}/f.bin"
    try:
        ic.http_download(url, tmp_path / "a.bin", size=len(data))
        assert (tmp_path / "a.bin").read_bytes() == data
        assert ranges == [None, f"bytes={len(data) // 3}-"] and waits == [5]
        ranges.clear()
        honour_range[0] = False
        ic.http_download(url, tmp_path / "b.bin", size=len(data))
        assert (tmp_path / "b.bin").read_bytes() == data
        assert not (tmp_path / "b.bin.part").exists()
        logged = (tmp_path / "logs" / "install.log").read_text(encoding="utf-8")
        assert logged.count(f"downloaded {url}: {len(data):,} bytes in 2 tries") == 2
    finally:
        srv.shutdown()


def test_a_failed_step_keeps_its_cause_for_the_report(ic, tmp_path):
    def unreachable():
        raise ic.InstallError("download failed: https://codeload.github.com/o/r/zip/abc "
                              "(URLError: <urlopen error [Errno 11001] getaddrinfo failed>)")

    inst = ic.Installer(tmp_path, {})
    assert not inst.step("ComfyUI_LTX", unreachable)
    assert [(f.step, f.cause) for f in inst.failures] == [("ComfyUI_LTX", "dns")]
    assert inst.fails == ["ComfyUI_LTX: " + str(inst.failures[0].lines[0])]


REFUSED = "No connection could be made because the target machine actively refused it"


def test_a_server_that_does_not_answer_is_tried_once_per_run(ic, tmp_path, monkeypatch):
    """Offline, every download went through all its tries (3 to 5 minutes a
    file, with its second source): now only the first from a server does, and
    the next ones from it fail at once, with the same cause in the report."""
    monkeypatch.setattr(ic, "UNREACHABLE", {})
    waits: list[float] = []
    monkeypatch.setattr(ic.time, "sleep", waits.append)
    calls: list[str] = []

    def refused(req, timeout):
        calls.append(req.full_url)
        raise ic.urllib.error.URLError(ConnectionRefusedError(10061, REFUSED))

    monkeypatch.setattr(ic.urllib.request, "urlopen", refused)
    with pytest.raises(ic.InstallError, match="actively refused"):
        ic.http_download("https://github.com/o/r/releases/download/v1/a.pth", tmp_path / "a.pth")
    assert len(calls) == 6 and waits == [5, 10, 20, 40, 60]
    inst = ic.Installer(tmp_path, {})
    assert not inst.step("b.pth", ic.http_download,
                         "https://github.com/o/r/releases/download/v1/b.pth", tmp_path / "b.pth")
    assert len(calls) == 6 and len(waits) == 5                      # not tried again
    assert inst.fails[0].startswith("b.pth: not tried: github.com could not be reached a moment ago")
    assert inst.failures[0].cause == "network"
    with pytest.raises(ic.InstallError):                            # another server still is
        ic.http_download("https://example.org/c.pth", tmp_path / "c.pth", tries=1)
    assert calls[-1] == "https://example.org/c.pth"
    # a name that does not resolve counts too, with its own cause
    monkeypatch.setattr(ic.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(
        ic.urllib.error.URLError(ic.socket.gaierror(11001, "getaddrinfo failed"))))
    with pytest.raises(ic.InstallError):
        ic.http_download("https://codeload.github.com/o/r/zip/abc", tmp_path / "d.zip", tries=1)
    assert not inst.step("e.zip", ic.http_download, "https://codeload.github.com/o/r/zip/def", tmp_path / "e.zip")
    assert inst.failures[-1].cause == "dns"


def test_a_server_that_answers_is_tried_again(ic, tmp_path, monkeypatch):
    """An HTTP error, or a transfer that stalls, comes from a server that is
    there: the next download from it goes ahead."""
    monkeypatch.setattr(ic, "UNREACHABLE", {})
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)

    def unavailable(req, timeout):
        raise ic.urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(ic.urllib.request, "urlopen", unavailable)
    for name in "ab":
        with pytest.raises(ic.InstallError, match="HTTP 503"):
            ic.http_download(f"https://github.com/o/r/{name}", tmp_path / name, tries=2)

    class Stalls:
        status, headers = 200, {"Content-Length": "10"}
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def read(self, n):
            raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(ic.urllib.request, "urlopen", lambda req, timeout: Stalls())
    with pytest.raises(ic.InstallError, match="timed out"):
        ic.http_download("https://github.com/o/r/c", tmp_path / "c", tries=2)
    assert ic.UNREACHABLE == {}


def test_the_hub_that_does_not_answer_is_named_and_not_asked_again(ic, tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "UNREACHABLE", {})
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    asked = []

    class ConnectError(Exception):          # httpx's, which the hub chains to its own error
        pass

    class LocalEntryNotFoundError(Exception):
        pass

    def hub(repo_id, filename, local_dir):
        asked.append(filename)
        try:
            raise ConnectError(f"[WinError 10061] {REFUSED}")
        except ConnectError as e:
            raise LocalEntryNotFoundError("An error happened while trying to locate the file on the Hub") from e

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=hub))
    with pytest.raises(ic.InstallError, match="huggingface.co could not be reached .*actively refused"):
        ic.hf_download("o/r", "a.pt", tmp_path / "a.pt", tmp_path / "staging")
    inst = ic.Installer(tmp_path, {})
    assert not inst.step("b.pt", ic.hf_download, "o/r", "b.pt", tmp_path / "b.pt", tmp_path / "staging")
    assert asked == ["a.pt"] and "not tried: huggingface.co" in inst.fails[0]
    assert inst.failures[0].cause == "network"
    # a file with two sources on that server says so once
    cat = {"models": {"c": {"path": "models/vae/c.pt", "size": 2, "sha256": None,
                            "sources": [{"hf": "o/r", "file": "c.pt"}, {"hf": "o/mirror", "file": "c.pt"}]}}}
    with pytest.raises(ic.InstallError) as err:
        ic.Installer(tmp_path, cat).model("c", [])
    assert str(err.value).count("not tried") == 1 and asked == ["a.pt"]


def test_a_damaged_archive_fails_its_part_and_the_others_go_on(ic, tmp_path, monkeypatch):
    """A guest network's sign-in page instead of the zip: zipfile raises
    BadZipFile, which is neither an OSError nor an InstallError."""
    monkeypatch.setattr(ic.support, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ic.support, "INSTALL_LOG", tmp_path / "logs" / "install.log")
    page = tmp_path / "ComfyUI.zip"
    page.write_text("<html>Sign in to the guest network</html>", encoding="utf-8")
    inst = ic.Installer(tmp_path, {})
    assert not inst.step("ComfyUI", ic.safe_extract, page, tmp_path / "x")
    assert inst.step("ComfyUI_LTX", lambda: None)
    assert [(f.step, f.cause) for f in inst.failures] == [("ComfyUI", "damaged")]
    assert "BadZipFile" in (tmp_path / "logs" / "install.log").read_text(encoding="utf-8")


def test_an_error_nobody_planned_for_is_reported(ic, tmp_path, monkeypatch, capsys):
    logs = tmp_path / "logs"
    for name, value in {"LOG_DIR": logs, "INSTALL_LOG": logs / "install.log",
                        "LAST_FAILURE": logs / "LAST-FAILURE.txt"}.items():
        monkeypatch.setattr(ic.support, name, value)
    monkeypatch.setattr(ic.support, "keep_awake", lambda: None)
    monkeypatch.setattr(ic.support, "no_quick_edit", lambda: None)

    def boom(args):
        raise ValueError("a bug")

    monkeypatch.setattr(ic, "run", boom)
    assert ic.main(["--components", "post", "--yes"]) == 1
    assert "THE INSTALL STOPPED" in capsys.readouterr().out
    assert "ValueError: a bug" in (logs / "LAST-FAILURE.txt").read_text(encoding="utf-8")
    assert "Traceback" in (logs / "install.log").read_text(encoding="utf-8")


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
    assert "MiniMax H3" in out and "RealVisXL V5" in out and "--use-folder" in out
    for mid in plan.manual:
        assert str(tmp_path / cat["models"][mid]["path"]) in out
    assert out.count("published at https://huggingface.co/") >= len(cat["components"]["h3"]["manual"])


# ---------------------------------------------------------------- files you already have
OWN = {"a": ("models/vae/a.st", b"A" * 40), "b": ("models/loras/b.st", b"B" * 40),
       "h": ("models/diffusion_models/h.st", b"H" * 40)}           # h: you bring it


@pytest.fixture
def own_files(ic, tmp_path, monkeypatch):
    """A studio with one part (a and b downloadable, h brought by the user)
    and a folder of the user's own copies of a and h, renamed."""
    import argparse
    import hashlib
    root = tmp_path / "studio"
    (root / "tool").mkdir(parents=True)
    settings = "paths:\n  root: .\n  # weights outside the tree\n  extra_model_dirs: []\n"
    (root / "tool" / "settings.yaml").write_text(settings, encoding="utf-8")
    models = {m: {"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest(),
                  "sources": [{"url": f"https://example.invalid/{m}"}]} for m, (p, d) in OWN.items()}
    models["h"].update(sources=[], manual=True, needed_by="a test")
    cat = {"trees": {}, "nodes": {}, "tools": {}, "models": models,
           "components": {"part": {"title": "A part", "summary": "for the test",
                                   "models": ["a", "b"], "manual": ["h"]},
                          "brought": {"title": "Brought", "summary": "like h3: no downloads",
                                      "models": [], "manual": ["h"]}}}
    downloads: list[str] = []

    def fake_http(url, dest, size=None, tries=6):
        downloads.append(url.rsplit("/", 1)[1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(OWN[url.rsplit("/", 1)[1]][1])

    monkeypatch.setattr(ic, "ROOT", root)
    monkeypatch.setattr(ic, "load_catalog", lambda path=None: cat)
    monkeypatch.setattr(ic, "http_download", fake_http)
    monkeypatch.setattr(ic, "find_ffmpeg", lambda r: r / "tools" / "ffmpeg.exe")
    monkeypatch.setattr(ic, "studio_report", lambda r: None)
    monkeypatch.setattr(ic, "set_title", lambda text: None)
    monkeypatch.setattr(ic.support, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ic.support, "INSTALL_LOG", tmp_path / "logs" / "install.log")
    # links on other drives as on Windows, never a folder at the top of a
    # real drive, and no symbolic links
    monkeypatch.setattr(ic.mf, "DRIVE_LINKS", True)
    monkeypatch.setattr(ic.mf, "drive_link_dir", lambda src: tmp_path / "drive-top" / ic.mf.LINK_DIR)
    monkeypatch.setattr(ic.mf.Finder, "can_symlink", lambda self, src, dst: False)
    mine = tmp_path / "my models"
    for name, m in (("renamed a.st", "a"), ("sub/H-3 copy.st", "h")):
        (mine / name).parent.mkdir(parents=True, exist_ok=True)
        (mine / name).write_bytes(OWN[m][1])
        os.chmod(mine / name, stat.S_IREAD)         # read-only, as users often keep them

    def args(**kw):
        base = dict(components="part", list=False, check=False, dry_run=False, yes=True,
                    use_folder=None, h3_dir=None, skip_models=False)
        return argparse.Namespace(**{**base, **kw})

    return root, mine, downloads, args


def _answers(monkeypatch, *answers):
    asked: list[str] = []
    left = list(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or left.pop(0))
    return asked


def _other_drive(ic, monkeypatch, drive, links=True):
    """The files under `drive` are on another drive than the studio, whose top
    is drive/top; hard links work only within one drive (and not at all on
    that one with links=False). Returns its Kinoloom-models folder."""
    on = (lambda p: drive in Path(p).parents)
    top = drive / "top" / ic.mf.LINK_DIR
    monkeypatch.setattr(ic.mf, "drive_link_dir", lambda src: top)
    monkeypatch.setattr(ic.mf.Finder, "can_link",
                        lambda self, src, dst: on(src) == on(dst) and links)
    return top


def test_files_you_already_have_are_linked_instead_of_downloaded(ic, own_files, capsys):
    root, mine, downloads, args = own_files
    assert ic.run(args(use_folder=[str(mine)])) == 0
    out = capsys.readouterr().out
    import os
    assert os.path.samefile(root / "models" / "vae" / "a.st", mine / "renamed a.st")
    assert os.path.samefile(root / "models" / "diffusion_models" / "h.st", mine / "sub" / "H-3 copy.st")
    assert downloads == ["b"]
    assert "your files:    2 found on this PC: 2 linked (no extra space)" in out
    assert "LINK models/vae/a.st" in out and "your files keep their names and places" in out
    assert (mine / "renamed a.st").read_bytes() == OWN["a"][1]           # the user's file as it was
    assert not os.access(mine / "renamed a.st", os.W_OK)                  # still read-only
    assert not list(root.rglob(".kinoloom-link-test-*"))                  # no link test left behind


def test_renamed_files_on_another_drive_are_linked_on_that_drive(ic, own_files, monkeypatch, capsys):
    """The weights on B:, Kinoloom on C:, under other names and in any
    folders: hard links in B:\\Kinoloom-models, nothing copied or downloaded,
    the user's files and folders as they were."""
    import os
    import yaml
    root, mine, downloads, args = own_files
    (root / "tool" / "settings.yaml").write_text(
        "paths:\n  root: .\n  # weights outside the tree\n  extra_model_dirs:\n  - D:/ComfyUI/models\n",
        encoding="utf-8")
    before = sorted((p.relative_to(mine), p.stat().st_size) for p in mine.rglob("*") if p.is_file())
    links = _other_drive(ic, monkeypatch, mine)
    assert ic.run(args(use_folder=[str(mine)])) == 0
    out = capsys.readouterr().out
    assert os.path.samefile(links / "vae" / "a.st", mine / "renamed a.st")
    assert os.path.samefile(links / "diffusion_models" / "h.st", mine / "sub" / "H-3 copy.st")
    assert not (root / "models" / "vae" / "a.st").exists() and downloads == ["b"]
    assert "2 found on this PC: 2 linked (no extra space)" in out and "copied" not in out
    assert f"links on other drives go in {links}" in out and f"LINK models/vae/a.st  (in {links})" in out
    cfg = yaml.safe_load((root / "tool" / "settings.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["extra_model_dirs"] == [links.as_posix(), "D:/ComfyUI/models"]   # first
    assert "# weights outside the tree" in (root / "tool" / "settings.yaml").read_text(encoding="utf-8")
    assert (links / "README.txt").is_file() and "close it and start it" in out
    assert sorted((p.relative_to(mine), p.stat().st_size) for p in mine.rglob("*")
                  if p.is_file() and links not in p.parents) == before
    # a second run finds everything in place: nothing to search, link or download
    assert ic.run(args(use_folder=[str(mine)])) == 0
    out = capsys.readouterr().out
    assert f"OK   models/vae/a.st  (in {links / 'vae' / 'a.st'})" in out
    assert "FOUND" not in out and downloads == ["b"]


def test_the_installer_asks_for_a_folder(ic, own_files, monkeypatch, capsys):
    root, mine, downloads, args = own_files
    asked = _answers(monkeypatch, "\\\\nas\\models", str(mine / "nope"), f'"{mine}"', "", "")
    assert ic.run(args(yes=False)) == 0
    out = capsys.readouterr().out
    assert asked == ["Folder: ", "Another folder (Enter: go on): ",
                     "Another folder (Enter: go on): ", "Another folder (Enter: go on): ",
                     "\nGo ahead? [Y/n] "]
    assert "A network folder" in out and "is not a folder on this PC" in out
    assert "Kinoloom still needs 3 model file(s)" in out
    assert (root / "models" / "vae" / "a.st").is_file() and downloads == ["b"]


def test_a_copy_you_decline_is_downloaded_instead(ic, own_files, monkeypatch, capsys):
    """A drive that cannot hold links (exFAT, say): a copy is offered."""
    root, mine, downloads, args = own_files
    _other_drive(ic, monkeypatch, mine, links=False)
    _answers(monkeypatch, str(mine), "", "n", "")
    assert ic.run(args(yes=False)) == 0
    out = capsys.readouterr().out
    assert "2 files found cannot be linked from where they are" in out
    assert "its drive cannot hold a link to it" in out
    assert ("Developer Mode" in out) == (sys.platform == "win32") and "Not copied" in out
    assert sorted(downloads) == ["a", "b"]
    assert not (root / "models" / "diffusion_models" / "h.st").exists()  # brought, not downloaded
    assert "--  " + str(root / OWN["h"][0]) in out


def test_a_model_folder_of_your_own_is_used_where_it_is(ic, own_files, monkeypatch, capsys):
    """Files under their own names in a ComfyUI models folder on another drive."""
    import yaml
    root, mine, downloads, args = own_files
    comfy = mine / "ComfyUI" / "models"
    (comfy / "vae").mkdir(parents=True)
    (comfy / "vae" / "a.st").write_bytes(OWN["a"][1])
    monkeypatch.setattr(ic.mf.Finder, "can_link", lambda self, src, dst: False)
    assert ic.run(args(use_folder=[str(comfy)])) == 0
    out = capsys.readouterr().out
    cfg = yaml.safe_load((root / "tool" / "settings.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["extra_model_dirs"] == [comfy.resolve().as_posix()]
    assert "# weights outside the tree" in (root / "tool" / "settings.yaml").read_text(encoding="utf-8")
    assert not (root / "models" / "vae" / "a.st").exists() and downloads == ["b"]
    assert f"OK   models/vae/a.st  (in {comfy.resolve() / 'vae' / 'a.st'})" in out
    assert "close it and start it" in out


def test_a_dry_run_finds_your_files_and_changes_nothing(ic, own_files, capsys):
    root, mine, downloads, args = own_files
    before = (root / "tool" / "settings.yaml").read_bytes()
    assert ic.run(args(dry_run=True, yes=False, use_folder=[str(mine)])) == 0
    out = capsys.readouterr().out
    assert "FOUND models/vae/a.st" in out and "LINK models/vae/a.st" in out
    assert downloads == [] and not (root / "models").exists()
    assert (root / "tool" / "settings.yaml").read_bytes() == before
    assert sorted(p.name for p in mine.rglob("*")) == ["H-3 copy.st", "renamed a.st", "sub"]


def test_a_dry_run_that_would_link_on_another_drive_changes_nothing(ic, own_files, monkeypatch, capsys):
    root, mine, downloads, args = own_files
    links = _other_drive(ic, monkeypatch, mine)
    before = (root / "tool" / "settings.yaml").read_bytes()
    assert ic.run(args(dry_run=True, yes=False, use_folder=[str(mine)])) == 0
    out = capsys.readouterr().out
    assert f"LINK models/vae/a.st  (in {links})" in out and f"{links} would become a model folder" in out
    assert not links.exists() and downloads == [] and not (root / "models").exists()
    assert (root / "tool" / "settings.yaml").read_bytes() == before


@pytest.mark.skipif(sys.platform != "win32", reason="a file system that ignores letter case")
def test_an_install_gives_kinoloom_s_own_file_its_letter_case(ic, own_files, capsys):
    import os
    root, _, downloads, args = own_files
    wrong = root / "models" / "vae" / "A.ST"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(OWN["a"][1])
    assert ic.model_state(root, ic.load_catalog()["models"]["a"], [])[0] == "letter case"
    assert ic.run(args(check=True)) == 0
    assert "(saved as A.ST: an install or the next start fixes the letter case)" in capsys.readouterr().out
    assert ic.run(args()) == 0
    out = capsys.readouterr().out
    assert "renamed" in out and os.listdir(root / "models" / "vae") == ["a.st"]
    assert "OK   models/vae/a.st" in out and downloads == ["b"]


def test_a_part_of_files_you_bring_gets_its_model_folder(ic, own_files, monkeypatch, capsys):
    """Like h3: no downloads at all, and every file found in a model folder of
    another drive under its own name."""
    import yaml
    root, mine, downloads, args = own_files
    comfy = mine / "H3"
    for sub in ("diffusion_models", "vae"):
        (comfy / sub).mkdir(parents=True)
    (comfy / "diffusion_models" / "h.st").write_bytes(OWN["h"][1])
    monkeypatch.setattr(ic.mf.Finder, "can_link", lambda self, src, dst: False)
    assert ic.run(args(components="brought", use_folder=[str(comfy)])) == 0
    out = capsys.readouterr().out
    cfg = yaml.safe_load((root / "tool" / "settings.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["extra_model_dirs"] == [comfy.resolve().as_posix()]
    assert f"OK  {root / OWN['h'][0]}" in out and "close it and start it" in out
    assert downloads == []


def test_a_link_that_fails_uses_the_folder_or_the_download(ic, own_files, monkeypatch, capsys):
    import yaml
    root, mine, downloads, args = own_files
    comfy = mine / "ComfyUI" / "models"
    (comfy / "diffusion_models").mkdir(parents=True)
    (comfy / "diffusion_models" / "h.st").write_bytes(OWN["h"][1])
    os.chmod(mine / "sub" / "H-3 copy.st", stat.S_IWRITE)
    (mine / "sub" / "H-3 copy.st").unlink()

    def no_link(src, dst):
        raise OSError(1, "Incorrect function")

    monkeypatch.setattr(ic.mf, "link", no_link)
    assert ic.run(args(use_folder=[str(mine)])) == 0
    out = capsys.readouterr().out
    assert "could not link it" in out
    assert sorted(downloads) == ["a", "b"]                  # a: renamed, so downloaded
    cfg = yaml.safe_load((root / "tool" / "settings.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["extra_model_dirs"] == [comfy.resolve().as_posix()]     # h: in place
    assert f"OK  {root / OWN['h'][0]}" in out and "close it and start it" in out


def test_a_quoted_folder_ending_in_a_backslash_keeps_the_arguments_after_it(ic):
    # Windows hands  --use-folder "D:\ComfyUI\models\" --yes  over like this
    assert ic.windows_argv(["--use-folder", 'D:\\ComfyUI\\models" --yes']) == \
        ["--use-folder", "D:\\ComfyUI\\models", "--yes"]
    assert ic.windows_argv(["--use-folder", 'D:" --dry-run', "--yes"]) == \
        ["--use-folder", "D:\\", "--dry-run", "--yes"]
    assert ic.windows_argv(["--components", "ltx25"]) == ["--components", "ltx25"]


def test_h3_dir_is_the_older_name_of_use_folder(ic, own_files, capsys):
    root, mine, downloads, args = own_files
    assert ic.main(["--components", "part", "--dry-run", "--h3-dir", str(mine / "sub")]) == 0
    assert "FOUND models/diffusion_models/h.st" in capsys.readouterr().out
    assert ic.main(["--components", "part", "--dry-run", "--use-folder", str(mine / "nope")]) == 2
    assert "is not a folder on this PC" in capsys.readouterr().out


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
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path}\n", encoding="utf-8")
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
    assert ic.venv_state(tmp_path, tree) == "outdated"            # not the pinned torch
    (site / "torch-2.14.1+cu126.dist-info").rename(site / "torch-2.13.0+cu126.dist-info")
    assert ic.venv_state(tmp_path, tree) == "ok"
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'moved-away'}\n", encoding="utf-8")
    assert ic.venv_state(tmp_path, tree) == "moved"               # the folder was moved
    (venv / ic.INCOMPLETE).write_text("", encoding="utf-8")
    assert ic.venv_state(tmp_path, tree) == "incomplete"          # the marker says more


def test_a_moved_venv_is_named_as_such(ic, tmp_path, capsys):
    tree = {"dir": "T", "venv": {"requirements": "req.txt", "index": "cu126"}}
    (tmp_path / "T" / "venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "T" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
    (tmp_path / "T" / "venv" / "pyvenv.cfg").write_text(f"home = {tmp_path / 'gone'}\n", encoding="utf-8")
    ic.Installer(tmp_path, {"trees": {"t": tree}}, dry_run=True).venv("t")
    out = capsys.readouterr().out
    assert "T\\venv no longer starts (was the Kinoloom folder moved?)" in out, out
    assert "not finished" not in out


def test_a_pin_without_a_local_label_matches_the_cuda_build_pip_installed(ic, tmp_path):
    """qwen21-venv.txt pins torchaudio==2.11.0, and the CUDA index installs a
    build with a local label (2.11.0+cu130): every second run counted that
    venv as unfinished and built it again."""
    (tmp_path / "req.txt").write_text("torchaudio==2.11.0\ntorch==2.13.0+cu126\n", encoding="utf-8")
    tree = {"dir": "T", "venv": {"requirements": "req.txt"}}
    venv = tmp_path / "T" / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path}\n", encoding="utf-8")
    site = venv / "Lib" / "site-packages"
    for d in ("torchaudio-2.11.0+cu126.dist-info", "torch-2.13.0+cu126.dist-info"):
        (site / d).mkdir(parents=True)
        (site / d / "RECORD").write_text("", encoding="utf-8")
    assert ic.venv_state(tmp_path, tree) == "ok"
    (site / "torch-2.13.0+cu126.dist-info").rename(site / "torch-2.13.0.dist-info")
    assert ic.venv_state(tmp_path, tree) == "outdated"            # a pinned label must be there
    (site / "torch-2.13.0.dist-info").rename(site / "torch-2.13.0+cu126.dist-info")
    (site / "torchaudio-2.11.0+cu126.dist-info").rename(site / "torchaudio-2.11.1+cu126.dist-info")
    assert ic.venv_state(tmp_path, tree) == "outdated"


def _venv(root: Path, tree: dict, *dists: str) -> None:
    venv = root / tree["dir"] / "venv"
    (venv / "Scripts").mkdir(parents=True, exist_ok=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(f"home = {root}\n", encoding="utf-8")
    site = venv / "Lib" / "site-packages"
    for d in site.glob("*.dist-info") if site.is_dir() else ():
        shutil.rmtree(d)
    for d in dists:
        (site / f"{d}.dist-info").mkdir(parents=True)
        (site / f"{d}.dist-info" / "RECORD").write_text("", encoding="utf-8")


@pytest.mark.parametrize("card, build", [
    (None, "cu130"),                                   # nvidia-smi did not answer
    (("NVIDIA GeForce RTX 3070", "616.64", (8, 6)), "cu130"),
    (("NVIDIA GeForce RTX 5090", "616.64", (12, 0)), "cu130"),
    (("NVIDIA GeForce GTX 1080", "560.94", (6, 1)), "cu126"),
    (("NVIDIA GeForce RTX 3070", "566.36", (8, 6)), "cu126"),    # a driver older than R580
    (("NVIDIA GeForce GTX 1080", "550.12", (6, 1)), "cu130"),    # runs neither: nothing to win
])
def test_qwen_gets_the_build_the_card_runs(ic, cat, monkeypatch, card, build):
    """Qwen-Image 2.1 is built on CUDA 13.0, or on the CUDA 12.6 list of 0.1.0
    where only that one runs (a GTX 900 or 10 card, a driver before R580)."""
    import gpu_support
    monkeypatch.setattr(gpu_support, "card", lambda: card and dict(zip(("name", "driver", "cc"), card)))
    spec = ic.venv_spec(cat["trees"]["qwen"])
    assert spec["cuda"] == build
    assert spec["requirements"].endswith("qwen21-venv-cu126.txt" if build == "cu126" else "qwen21-venv.txt")
    assert ic.venv_spec(cat["trees"]["ltx"]) is cat["trees"]["ltx"]["venv"]     # no other build


def test_the_two_qwen_lists_differ_in_the_torch_build_only(cat):
    spec = cat["trees"]["qwen"]["venv"]

    def pins(rel: str) -> dict[str, str]:
        lines = (REPO / rel).read_text(encoding="utf-8").splitlines()
        return dict(x.split("==", 1) for x in lines if "==" in x and not x.startswith("#"))
    main, old = pins(spec["requirements"]), pins(spec["fallback"]["requirements"])
    assert main.keys() == old.keys()
    assert {k for k in main if main[k] != old[k]} == {"torch", "torchvision"}
    assert old["torch"].endswith("+cu126") and main["torch"].endswith("+cu130")
    assert spec["fallback"]["index"].endswith("/cu126") and spec["index"].endswith("/cu130")


def test_a_venv_of_the_other_build_is_built_again_with_the_reason(ic, monkeypatch, tmp_path, capsys):
    """A Qwen venv from 0.1.0 (cu126) on an RTX card, or one built for a GTX
    1080 after the card was changed, no longer matches its list: the installer
    says why and builds the one this card runs."""
    import gpu_support
    (tmp_path / "new.txt").write_text("torch==2.13.0+cu130\n", encoding="utf-8")
    (tmp_path / "old.txt").write_text("torch==2.13.0+cu126\n", encoding="utf-8")
    tree = {"dir": "Q", "title": "Qwen-Image 2.1 (port 8190)",
            "venv": {"requirements": "new.txt", "index": "cu130-index", "cuda": "cu130",
                     "fallback": {"requirements": "old.txt", "index": "cu126-index", "cuda": "cu126"}}}
    _venv(tmp_path, tree, "torch-2.13.0+cu126")
    monkeypatch.setattr(gpu_support, "card", lambda: {"name": "NVIDIA GeForce RTX 3070", "driver": "616.64",
                                                      "cc": (8, 6)})
    assert ic.venv_state(tmp_path, tree) == "outdated"
    ic.Installer(tmp_path, {"trees": {"q": tree}}, dry_run=True).venv("q")
    out = capsys.readouterr().out
    assert "Q\\venv has other packages than new.txt (from an older Kinoloom, or for another graphics card)" in out
    assert "(Python 3.12, new.txt, cu130-index)" in out and "instead" not in out

    monkeypatch.setattr(gpu_support, "card", lambda: {"name": "NVIDIA GeForce GTX 1080", "driver": "560.94",
                                                      "cc": (6, 1)})
    assert ic.venv_state(tmp_path, tree) == "ok"                  # the build this card runs
    _venv(tmp_path, tree, "torch-2.13.0+cu130")
    ic.Installer(tmp_path, {"trees": {"q": tree}}, dry_run=True).venv("q")
    out = capsys.readouterr().out
    assert "(Python 3.12, old.txt, cu126-index)" in out, out
    assert ("The NVIDIA GeForce GTX 1080 (compute capability 6.1) cannot run Qwen-Image 2.1 (port 8190), "
            "built on CUDA 13.0 for a GTX 16- or RTX 20-series card or newer: it gets the CUDA 12.6 build instead") in out


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
            assert (Path(args[3]) / ic.INCOMPLETE).exists()     # marked before it is made
            (Path(args[3]) / "Scripts").mkdir(parents=True)
            (Path(args[3]) / "Scripts" / "python.exe").write_bytes(b"")
            (Path(args[3]) / "pyvenv.cfg").write_text(f"home = {tmp_path}\n", encoding="utf-8")
        elif "-r" in args and pip_rc[0] == 0:
            d = venv / "Lib" / "site-packages" / "torch-2.13.0+cu126.dist-info"
            d.mkdir(parents=True)
            (d / "RECORD").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args, pip_rc[0] if "-r" in args else 0, "", "")

    fake = types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(ic, "subprocess", fake)
    monkeypatch.setattr(ic.support, "subprocess", fake)             # pip runs from the helper
    monkeypatch.setattr(ic.support, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ic.support, "INSTALL_LOG", tmp_path / "logs" / "install.log")
    monkeypatch.setattr(ic.support, "keep_awake", lambda: None)
    monkeypatch.setattr(ic.support, "no_quick_edit", lambda: None)
    inst = ic.Installer(tmp_path, cat)
    with pytest.raises(ic.InstallError, match="builds this venv again"):
        inst.venv("t")
    assert [c[c.index("-r") + 1] for c in calls if "-r" in c] == [str(tmp_path / "req.txt")]
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
    ic.support.remove_tree(venv)
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
function Stop-Bootstrap([string]$t, [switch]$Calm) { Write-Host "[FAIL] $t"; exit 1 }
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
        for spec in filter(None, [t.get("venv"), (t.get("venv") or {}).get("fallback")]):
            assert f'file = "{spec["requirements"]}"; index = "{spec["index"]}"' in wf, t["dir"]
