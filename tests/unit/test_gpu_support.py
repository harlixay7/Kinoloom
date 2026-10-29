"""What the PC's card and driver run (tool/gpu_support.py): LTX-2.5 and
MiniMax H3 run on a CUDA 13.0 build (GTX 16 or RTX 20 series and newer, driver R580),
the render farm on CUDA 12.6 (GTX 900 to RTX 40 series, R560), and
Qwen-Image 2.1 on whichever its venv was built with. A model this PC cannot
run is unavailable with the reason; nothing is refused when nvidia-smi does not
answer."""
import subprocess

import pytest

import gpu_support


class Smi:
    """nvidia-smi's answers by query field; None = the query fails."""

    def __init__(self, **fields):
        self.fields, self.calls = fields, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        field = cmd[1].split("=", 1)[1]
        out = self.fields.get(field)
        if out is None:
            return subprocess.CompletedProcess(cmd, 6, "", "Field is not a valid field to query.")
        return subprocess.CompletedProcess(cmd, 0, out + "\n", "")


@pytest.fixture
def smi(monkeypatch, tmp_path):
    """Set the card: smi(name="...", driver="...", cc="8.6" or None). No Qwen
    venv unless a test writes one (qwen_venv)."""
    monkeypatch.setenv("KINOLOOM_GPU_CHECK", "1")
    monkeypatch.setattr(gpu_support, "QWEN_TORCH", tmp_path / "torch" / "version.py")

    def put(name="NVIDIA GeForce RTX 3070", driver="616.64", cc="8.6", missing=False):
        gpu_support.card.cache_clear()
        if missing:
            def run(*a, **k):
                raise FileNotFoundError("nvidia-smi")
            monkeypatch.setattr(subprocess, "run", run)
            return None
        fake = Smi(**{"name,driver_version": f"{name}, {driver}", "compute_cap": cc})
        monkeypatch.setattr(subprocess, "run", fake)
        return fake
    yield put
    gpu_support.card.cache_clear()


def qwen_venv(cuda: str | None) -> None:
    """The Qwen venv's torch, as torch writes its version.py."""
    gpu_support.QWEN_TORCH.parent.mkdir(parents=True, exist_ok=True)
    gpu_support.QWEN_TORCH.write_text(
        "from typing import Optional\n\n__version__ = '2.13.0'\ndebug = False\n"
        f"cuda: Optional[str] = {repr(cuda)}\ngit_version = 'x'\n", encoding="utf-8")


GTX_1080 = {"name": "NVIDIA GeForce GTX 1080", "driver": "560.94", "cc": "6.1"}
RTX_5090 = {"name": "NVIDIA GeForce RTX 5090", "driver": "616.64", "cc": "12.0"}


def test_a_recent_card_runs_everything(smi):
    smi()
    assert gpu_support.card() == {"name": "NVIDIA GeForce RTX 3070", "driver": "616.64", "cc": (8, 6)}
    assert gpu_support.problem("cu130") == "" and gpu_support.problem("cu126") == ""
    assert gpu_support.qwen_pick() == "cu130"
    assert [gpu_support.port_problem(p) for p in (8188, 8189, 8190, 8199)] == ["", "", "", ""]


def test_a_gtx_10_card_runs_the_render_farm_and_qwen_on_cuda_12_6(smi):
    smi(**GTX_1080)
    assert gpu_support.problem("cu130") == (
        "the NVIDIA GeForce GTX 1080 (compute capability 6.1) cannot run LTX-2.5 and MiniMax H3, "
        "built on CUDA 13.0 for a GTX 16- or RTX 20-series card or newer")
    assert gpu_support.problem("cu126") == ""
    # the installer builds Qwen-Image 2.1 on CUDA 12.6 here, and that build runs
    assert gpu_support.qwen_pick() == "cu126"
    qwen_venv("12.6")
    assert gpu_support.qwen_runtime() == "cu126"
    assert [gpu_support.port_problem(p) == "" for p in (8188, 8189, 8190, 8199)] == [True, False, True, False]


def test_a_qwen_venv_built_for_another_card_says_how_to_fix_it(smi):
    smi(**GTX_1080)
    qwen_venv("13.0")                       # built on an RTX card, the folder copied here
    assert gpu_support.port_problem(8190) == (
        "the NVIDIA GeForce GTX 1080 (compute capability 6.1) cannot run Qwen-Image 2.1, built on "
        "CUDA 13.0 for a GTX 16- or RTX 20-series card or newer; run INSTALL.bat again to build the one this "
        "card runs")
    smi(**RTX_5090)
    qwen_venv("12.6")                       # a 0.1.0 venv, or one built for a GTX 1080
    assert "RTX 5090 (compute capability 12.0) cannot run Qwen-Image 2.1, built on CUDA 12.6" \
        in gpu_support.port_problem(8190)
    assert gpu_support.port_problem(8190).endswith("run INSTALL.bat again to build the one this card runs")
    qwen_venv(None)                         # a CPU build of torch: what the installer would build
    assert gpu_support.qwen_runtime() == "cu130" and gpu_support.port_problem(8190) == ""


def test_an_rtx_50_card_cannot_run_the_render_farm(smi):
    """The CUDA 12.6 wheels hold kernels for sm_50 to sm_90 and no PTX: none for
    the RTX 50 series (sm_120)."""
    smi(**RTX_5090)
    assert gpu_support.problem("cu126") == (
        "the NVIDIA GeForce RTX 5090 (compute capability 12.0) cannot run the render farm (the SDXL "
        "image models, Z-Image, post-processing), built on CUDA 12.6 for a GTX 900- to RTX 40-series card")
    assert gpu_support.problem("cu130") == "" and gpu_support.qwen_pick() == "cu130"
    assert gpu_support.problem("cu126", "Wan 2.2").startswith(
        "the NVIDIA GeForce RTX 5090 (compute capability 12.0) cannot run Wan 2.2, built on CUDA 12.6")


def test_a_driver_older_than_r580_is_named_with_what_to_do(smi):
    smi(driver="566.36")
    assert gpu_support.problem("cu130") == (
        "NVIDIA driver 566.36 is too old for LTX-2.5 and MiniMax H3, built on CUDA 13.0: it needs "
        "R580 or newer (update it from nvidia.com/drivers)")
    assert gpu_support.problem("cu126") == ""
    assert gpu_support.qwen_pick() == "cu126"


def test_a_driver_too_old_to_report_the_compute_capability(smi):
    smi(driver="472.12", cc=None)            # compute_cap is a query of R510 and newer
    assert gpu_support.card()["cc"] is None
    assert "driver 472.12 is too old" in gpu_support.problem("cu130")
    assert "R560 or newer" in gpu_support.problem("cu126")
    assert gpu_support.qwen_pick() == "cu130"    # neither runs: the installer builds its own


@pytest.mark.parametrize("how", ["missing", "off", "garbage"])
def test_no_answer_refuses_nothing(smi, monkeypatch, how):
    if how == "missing":
        smi(missing=True)
    elif how == "off":
        smi(name="NVIDIA GeForce GTX 1080", cc="6.1")
        monkeypatch.setenv("KINOLOOM_GPU_CHECK", "0")
        gpu_support.card.cache_clear()
    else:
        smi(name="", driver="[N/A]", cc="[N/A]")
    assert gpu_support.card() is None
    assert gpu_support.problem("cu130") == "" and gpu_support.problem("cu126") == ""
    assert gpu_support.qwen_pick() == "cu130" and gpu_support.port_problem(8190) == ""
    assert "not checked" in gpu_support.report()[0]


def test_it_asks_nvidia_smi_once(smi):
    fake = smi()
    for _ in range(3):
        gpu_support.problem("cu130")
    assert len(fake.calls) == 2                # the name and driver, then the compute capability


def test_the_installer_report(smi):
    smi(**GTX_1080)
    qwen_venv("12.6")
    lines = gpu_support.report()
    assert lines[0] == "  Card: NVIDIA GeForce GTX 1080, compute capability 6.1, driver 560.94."
    assert lines[1].startswith("  OK: it runs the render farm")
    assert lines[2].startswith("  [WARN] The NVIDIA GeForce GTX 1080 (compute capability 6.1) cannot run "
                               "LTX-2.5 and MiniMax H3")
    assert lines[3] == "  OK: it runs Qwen-Image 2.1 (on CUDA 12.6)."
    smi()
    qwen_venv("13.0")
    assert gpu_support.report()[1:] == [
        "  OK: it runs the render farm (the SDXL image models, Z-Image, post-processing).",
        "  OK: it runs LTX-2.5 and MiniMax H3.", "  OK: it runs Qwen-Image 2.1 (on CUDA 13.0)."]


# ---------------------------------------------------------------- the studio
def test_an_image_model_the_card_cannot_run_is_unavailable_with_the_reason(smi, monkeypatch):
    import models
    monkeypatch.setattr(models, "has_weights", lambda mid: True)
    smi(**GTX_1080)
    qwen_venv("13.0")                       # Qwen built for another card
    info = models.model_info("qwen21_fast")
    assert info["available"] is False and "RTX 20-series" in info["gpu"]
    assert models.model_info("realvis")["available"] is True and models.model_info("realvis")["gpu"] == ""
    qwen_venv("12.6")                       # the build the installer gives this card
    assert models.model_info("qwen21_fast")["available"] is True
    smi(**RTX_5090)
    assert models.model_info("realvis")["available"] is False
    assert "built on CUDA 12.6" in models.model_info("realvis")["gpu"]


def test_a_video_model_the_card_cannot_run_is_unavailable_with_the_reason(smi, monkeypatch):
    import video_models
    monkeypatch.setattr(video_models, "_exists", lambda rel: True)
    monkeypatch.setattr(video_models, "workflow_exists", lambda name: True)
    smi(driver="566.36")
    st = video_models.model_status("ltx25_s6_cq")
    assert st["available"] is False and st["missing"] == [] and "R580" in st["gpu"] == st["reason"]
    assert video_models.default_model_id() is None
    smi()
    st = video_models.model_status("ltx25_s6_cq")
    assert st["available"] is True and st["gpu"] == "" and "reason" not in st


def test_the_launcher_does_not_start_an_engine_the_card_cannot_run(monkeypatch, tmp_path):
    import launcher
    lz = launcher.Launcher.__new__(launcher.Launcher)
    lz.host, lz.port, lz.not_installed, lz.unsupported = "127.0.0.1", 8080, [], {}
    spawned = []
    lz.spawn = lambda name, *a, **k: spawned.append(name)
    for tree in ("ComfyUI", "ComfyUI_Qwen21"):
        (tmp_path / tree).mkdir()
        (tmp_path / tree / "main.py").write_text("", encoding="utf-8")
    venv = tmp_path / "ComfyUI_Qwen21" / "venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_bytes(b"")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "QWEN_PY", venv / "python.exe")
    monkeypatch.setattr(launcher, "_settings", lambda: {})
    monkeypatch.setattr(gpu_support, "port_problem",
                        lambda port: "it needs an RTX 20-series card" if port != 8188 else "")
    lz.launch("qwen-render")
    lz.launch("comfy-ui")
    assert spawned == ["comfy-ui"] and lz.unsupported == {"qwen-render": "it needs an RTX 20-series card"}
    assert lz.not_installed == []
