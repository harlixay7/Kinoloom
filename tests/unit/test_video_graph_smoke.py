"""Every registered video model builds a complete graph for the modes it
offers - without a ComfyUI. Catches templates whose {TOKENS} are left
unfilled and registry entries that point at missing templates."""
import json
import re

import pytest

import video_models
import video_studio

_TOKEN = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def _png(tmp_path):
    p = tmp_path / "ref.png"
    # 1x1 PNG
    p.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
    return str(p)


def _cases():
    out = []
    for mid in sorted(video_models.VIDEO_MODELS):
        st = video_models.model_status(mid)
        modes = st.get("supported_modes") or []
        if "t2v" in modes:
            out.append((mid, "t2v"))
        fam = str(video_models.VIDEO_MODELS[mid].get("family", ""))
        if fam.startswith(("ltx", "h3")) and video_models.takes_reference_images(mid):
            out.append((mid, "refs"))
    return out


@pytest.mark.parametrize("mid,kind", _cases())
def test_graph_builds_without_unfilled_tokens(tmp_path, monkeypatch, mid, kind):
    # the Wan path refuses to build without its Turbo LoRA on disk
    lora = tmp_path / "models" / "loras" / video_studio.TURBO_LORA
    lora.parent.mkdir(parents=True)
    lora.write_bytes(b"0")
    monkeypatch.setattr(video_studio, "ROOT", tmp_path)
    m = video_models.VIDEO_MODELS[mid]
    lim = m.get("frame_limits") or {}
    frames = int(lim.get("min") or 25)
    w, h = 768, 512
    refs = [{"slot": "pic1", "path": _png(tmp_path)}] if kind == "refs" else None
    mode = "t2v" if kind == "t2v" else ("i2v" if "i2v" in (m.get("supported_modes") or []) else "t2v")
    wf, template, _te = video_studio._build(
        mid, mode, "a quiet street at dusk", "", 1, w, h, frames, 8, 1.0,
        loras=[], start_image=None, teacache_thresh=0.1, teacache_end=0.6,
        no_cache=False, shot_id="smoke", references=refs)
    assert isinstance(wf, dict) and wf, (mid, template)
    for nid, node in wf.items():
        assert isinstance(node, dict) and node.get("class_type"), (mid, template, nid)
    leftover = _TOKEN.findall(json.dumps(wf))
    assert not leftover, (mid, template, sorted(set(leftover)))


@pytest.mark.parametrize("mid", sorted(m for m, e in video_models.VIDEO_MODELS.items()
                                       if e.get("strict_dims")))
def test_resolution_limits_sit_on_the_output_grid(mid):
    """Every size the page can snap to renders at exactly that size. LTX-2.5
    and MiniMax H3 latents sit on a 32 px grid (H3 failed at a height of 272),
    and a template that samples stage 1 at half size ({STAGE1_W}) doubles it:
    LTX at 512x288 rendered a 512x256 clip."""
    import workflows
    m = video_models.VIDEO_MODELS[mid]
    names = {t for t in [m.get("workflow"), *(m.get("workflows") or {}).values()]
             if t and video_models.workflow_exists(t)}
    half = any("{STAGE1_W}" in workflows.load_template(t) for t in names)
    rl = m["resolution_limits"]
    step = int(rl["step"])
    assert step % (64 if half else 32) == 0, (mid, step)
    sizes = [rl["min"], rl["max"], *([int(v) for v in s.split("x")] for s in rl.get("native") or [])]
    if (m.get("defaults") or {}).get("width"):
        sizes.append([m["defaults"]["width"], m["defaults"]["height"]])
    for w, h in sizes:
        assert w % step == 0 and h % step == 0, (mid, w, h, step)


def test_tier_without_reference_graph_does_not_offer_i2v():
    """An LTX tier with only t2v graphs has nowhere to put a start image."""
    mid = "ltx25_w4a8_t2v"
    if mid not in video_models.VIDEO_MODELS:
        pytest.skip(f"{mid} not in this registry")
    st = video_models.model_status(mid)
    assert "i2v" not in st["supported_modes"] and st["i2v_note"]
    assert video_models.i2v_problem("wan22_5b_q4") is None
