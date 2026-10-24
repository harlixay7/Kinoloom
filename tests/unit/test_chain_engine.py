"""Chain orchestration: link wiring between steps, resumable manifest, output checks,
H3 take bookkeeping, and the lab's static graph validator."""
import json
import sys
from pathlib import Path

import pytest

import chain_engine as ce
import chain_graphs as cg
import core

sys.path.insert(0, str(Path(core.ROOT) / "tool" / "scripts"))
import chain_lab  # noqa: E402


def _ltx_spec(n=3):
    links = [ce.Link(prompt=f"p{i}", seed=100 + i, frames=121 if i == 0 else 145) for i in range(n)]
    return ce.LtxChainSpec(chain_id="t", links=links, refs={"pic1": "a.png", "pic2": "b.png"})


def _nodes(graph, ct):
    return [n for n in graph.values() if n["class_type"] == ct]


class FakeRunner:
    """Writes the files a real step would leave behind (sidecar frame count from the graph)."""

    def __init__(self, out_dir: Path, frames_override=None):
        self.out, self.calls, self.frames_override = out_dir, [], frames_override

    def run(self, name, graph):
        self.calls.append(name)
        for n in graph.values():
            ct, inp = n["class_type"], n["inputs"]
            if ct in ("StudioSaveLatent", "StudioSaveImages", "StudioSaveConditioning"):
                p = self.out / (inp["relpath"] + ".safetensors")
            elif ct == "StudioSaveVideoLossless":
                p = self.out / (inp["relpath"] + ".mkv")
                frames = self.frames_override or _frames_of(graph)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.with_suffix(".json").write_text(json.dumps({"frames": frames}), encoding="utf-8")
            elif ct == "StudioLtxDecodeTake":
                p = self.out / (inp["relpath"] + ".mkv")
                p.parent.mkdir(parents=True, exist_ok=True)
                n = len(inp["links"].splitlines())
                p.with_suffix(".json").write_text(json.dumps(
                    {"frames": self.frames_override or 121 + 120 * (n - 1),
                     "take": {"joins": [{"take_frame": 105 + 120 * j, "step_ratio": 0.9} for j in range(n - 1)]}}),
                    encoding="utf-8")
            else:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * len(self.calls))      # every render leaves a different file
        return {"status": "success"}


def _frames_of(graph):
    for ct, key in (("EmptyLTXVLatentVideo", "length"), ("MiniMaxH3ReferenceToVideo", "length")):
        hit = _nodes(graph, ct)
        if hit:
            return int(hit[0]["inputs"][key])
    st = _nodes(graph, "StudioLoadLatent")
    return 145 if st else 121


def test_ltx_spec_rejects_off_grid_links():
    s = _ltx_spec()
    s.links[1].frames = 140
    with pytest.raises(ValueError):
        s.check()
    s = _ltx_spec()
    s.links[1].frames = 33                    # carry 25 + 8 leaves nothing new
    with pytest.raises(ValueError):
        s.check()


def test_ltx_link_wiring(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    d1, d2 = ch.draft_graph(0), ch.draft_graph(1)
    assert not _nodes(d1, "StudioLoadImages")
    tails = [n["inputs"]["relpath"] for n in _nodes(d2, "StudioLoadImages")]
    assert tails == ["studio_chain/t/link1_draft_tail"]
    carry = _nodes(d2, "LTX25CarryAudioLatent")[0]["inputs"]
    assert carry["source_frames"] == 121 and carry["carry_frames"] == cg.LTX_CARRY_FRAMES
    # the original reference set on every link
    assert sorted(n["inputs"]["image"] for n in _nodes(d2, "LoadImage")) == ["a.png", "b.png"]
    f2 = ch.finish_graph(1, "FX", {"cq_strength": 0.6})
    loads = sorted(n["inputs"]["relpath"] for n in _nodes(f2, "StudioLoadImages"))
    assert loads == ["studio_chain/t/FX/link1_final_tail"]
    assert _nodes(f2, "RandomNoise")[0]["inputs"]["noise_seed"] == 102
    assert _nodes(f2, "LoraLoaderModelOnly")[0]["inputs"]["strength_model"] == 0.6
    saves = {n["inputs"]["relpath"] for n in _nodes(f2, "StudioSaveVideoLossless")}
    assert saves == {"studio_chain/t/FX/link2_final"}


def test_ltx_finish_stage2_references_are_opt_in_and_link1_gets_a_full_mask(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    f1, f2 = ch.finish_graph(0, "F0", {}), ch.finish_graph(1, "F0", {})
    for f in (f1, f2):                        # default: no reference tokens in stage 2
        assert not _nodes(f, "ComfyUILTX25MSRMultiReferenceGuide") and not _nodes(f, "LTXVCropGuides")
    assert _nodes(f1, "StudioSetNoiseMask") and not _nodes(f2, "StudioSetNoiseMask")
    assert _nodes(f2, "StudioLtxLatentCarry") and not _nodes(f2, "LTXVImgToVideoInplace")
    on = ch.finish_graph(1, "F1", {"msr_stage2": True})
    assert _nodes(on, "ComfyUILTX25MSRICLoRALoader") and _nodes(on, "LTXVCropGuides")
    assert {"pic1", "pic2"} <= set(_nodes(on, "ComfyUILTX25MSRMultiReferenceGuide")[0]["inputs"])


def _chain_of(g, node_id):
    """Class names upstream of node_id along each node's first LATENT-ish input."""
    out = []
    while node_id in g:
        n = g[node_id]
        out.append(n["class_type"])
        nxt = next((v[0] for k, v in n["inputs"].items() if k in ("latent", "samples", "value", "video_latent")
                    and isinstance(v, list)), None)
        node_id = nxt
    return out


def test_default_carry_recipe_is_wired_in_order_on_both_stages(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    for g, saved, src in ((ch.draft_graph(1), "_s1_video", "studio_chain/t/link1_s1_video"),
                          (ch.finish_graph(1, "F0", {}), "_s2_video", "studio_chain/t/F0/link1_s2_video")):
        assert _nodes(g, "StudioPinRamp")[0]["inputs"]["strengths"] == cg.LTX_PIN_RAMP
        (cid, cnode), = [(k, n) for k, n in g.items() if n["class_type"] == "StudioLtxLatentCarry"]
        assert g[cnode["inputs"]["source"][0]]["inputs"]["relpath"] == src      # the previous link's latents
        (rid, rnode), = [(k, n) for k, n in g.items() if n["class_type"] == "StudioCarryRestore"]
        assert rnode["inputs"]["reference"] == [cid, 0] and rnode["inputs"]["carry_latents"] == 4
        sa = _nodes(g, "StudioSeamAdain")[0]["inputs"]
        assert sa["latent"] == [rid, 0] and sa["ramp_latents"] == 4 and sa["factor"] == 1.0
        assert sa["mode"] == "path"
        save_id = [k for k, n in g.items() if n["class_type"] == "StudioSaveLatent"
                   and n["inputs"]["relpath"].endswith(saved)][0]
        up = _chain_of(g, save_id)
        assert up[:3] == ["StudioSaveLatent", "StudioSeamAdain", "StudioCarryRestore"]
    for g in (ch.draft_graph(0), ch.finish_graph(0, "F0", {})):    # link 1 has no carry
        for ct in ("StudioPinRamp", "StudioLtxLatentCarry", "StudioCarryRestore", "StudioSeamAdain"):
            assert not _nodes(g, ct)


def test_a_rerendered_step_reruns_exactly_the_steps_that_read_its_files(tmp_path):
    run = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(), run, tmp_path)

    def at(frames, fn, *a, **kw):
        run.frames_override = frames
        return fn(*a, **kw)

    def everything():
        for i in range(3):
            at(ch.spec.links[i].frames, ch.draft, i)
        at(ch.take_frames(), ch.take, "draft")
        for i in range(3):
            at(ch.spec.links[i].frames, ch.finish, i, "F0")
        at(ch.take_frames(), ch.take, "final", "F0")
    everything()
    assert len(run.calls) == 8
    run.calls.clear()
    everything()
    assert run.calls == []                              # nothing changed, nothing re-runs
    at(145, ch.draft, 1, force=True)                    # link 2's draft is re-rendered: its graph is
    everything()                                        # unchanged, so only the files tell
    assert run.calls == ["draft_l2", "draft_l3", "take_draft", "finish_F0_l2", "finish_F0_l3", "take_final_F0"]
    rec = ch.manifest.data["steps"]["finish_F0_l2"]["inputs"]
    assert "studio_chain/t/link2_s1_video" in rec and "studio_chain/t/F0/link1_s2_video" in rec


def test_a_record_from_before_input_tracking_is_kept_and_learns_its_inputs(tmp_path):
    run = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(2), run, tmp_path)
    ch.draft(0)
    ch.draft(1)
    rec = ch.manifest.data["steps"]["draft_l2"]
    sig = rec.pop("inputs")
    ch.manifest.save()
    run.calls.clear()
    assert ch.draft(1)["skipped"] and run.calls == []
    assert ch.manifest.data["steps"]["draft_l2"]["inputs"] == sig
    assert sorted(sig) == ["studio_chain/t/link1_draft_tail", "studio_chain/t/link1_s1_audio",
                           "studio_chain/t/link1_s1_video"]
    # ...unless an input was written after it: an upstream re-render it never saw
    rec = ch.manifest.data["steps"]["draft_l2"]
    rec.pop("inputs")
    rec["at"] = "2020-01-01T00:00:00"
    ch.manifest.save()
    run.frames_override = 145
    assert not ch.draft(1).get("skipped") and run.calls == ["draft_l2"]


def test_seam_mode_reaches_both_stages_and_window_graphs_keep_their_shape(tmp_path):
    s = _ltx_spec()
    ch = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    path = [ch.draft_graph(1), ch.finish_graph(1, "F0", {})]              # the default
    s.seam_mode = "window"
    window = [ch.draft_graph(1), ch.finish_graph(1, "F0", {})]
    for w, p in zip(window, path):
        (wn,), (pn,) = _nodes(w, "StudioSeamAdain"), _nodes(p, "StudioSeamAdain")
        assert "mode" not in wn["inputs"] and pn["inputs"]["mode"] == "path"
        assert {k: v for k, v in pn["inputs"].items() if k != "mode"} == wn["inputs"]
    s.seam_mode = "smooth"
    with pytest.raises(ValueError):
        s.check()


def test_carry_recipe_parts_switch_off(tmp_path):
    s = _ltx_spec()
    s.latent_carry, s.carry_restore, s.seam_adain = False, 0.0, 0.0
    ch = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    for g in (ch.draft_graph(1), ch.finish_graph(1, "F0", {})):
        assert _nodes(g, "LTXVImgToVideoInplace") and not _nodes(g, "StudioLtxLatentCarry")
        assert not _nodes(g, "StudioCarryRestore") and not _nodes(g, "StudioSeamAdain")
    s.pin_strengths, s.carry_restore = "", 1.0                     # a hard pin has no gain to undo
    assert not _nodes(ce.LtxChain(s, FakeRunner(tmp_path), tmp_path).draft_graph(1), "StudioCarryRestore")


def test_stage2_keyframes_are_opt_in_and_switch_cleanly(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    for i in (0, 1):
        on, off = ch.finish_graph(i, "F1", {"stage2_keyframes": True}), ch.finish_graph(i, "F0", {})
        assert _nodes(on, "LTXVAddGeneratedKeyframes") and _nodes(on, "LTXVSeparateGeneratedKeyframes")
        for ct in ("LTXVAddGeneratedKeyframes", "LTXVSeparateGeneratedKeyframes"):
            assert not _nodes(off, ct)
        assert not [n for n in _nodes(off, "StudioLoadLatent") if n["inputs"]["relpath"].endswith("_s1_kf")]
        # the sampler still sees the carried (or masked) stage-1 latent, and the saved
        # video comes straight from the AV split, through the carry recipe on later links
        (av,) = _nodes(off, "LTXVConcatAVLatent")
        src = off[av["inputs"]["video_latent"][0]]["class_type"]
        assert src == ("StudioSetNoiseMask" if i == 0 else "StudioPinRamp")
        save_id = [k for k, n in off.items() if n["class_type"] == "StudioSaveLatent"][0]
        up = _chain_of(off, save_id)
        assert "LTXVSeparateAVLatent" in up
        if i:
            assert up[:3] == ["StudioSaveLatent", "StudioSeamAdain", "StudioCarryRestore"]
    msr_off = ch.finish_graph(1, "F2", {"stage2_keyframes": False, "msr_stage2": True})
    (guide,) = _nodes(msr_off, "ComfyUILTX25MSRMultiReferenceGuide")
    assert msr_off[guide["inputs"]["latent"][0]]["class_type"] == "StudioPinRamp"
    assert _nodes(msr_off, "LTXVCropGuides")


def _model_chain(g):
    (guider,) = _nodes(g, "LTXVDualCFGGuider")
    out, nid = [], guider["inputs"]["model"][0]
    while nid:
        out.append(g[nid]["class_type"])
        nid = (g[nid]["inputs"].get("model") or [None])[0]
    return out


def test_finish_refines_with_the_drafts_model_without_the_reference_guide(tmp_path):
    s = _ltx_spec()
    s.draft["cq_strength"] = 0.45
    ch = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    for i in (0, 1):
        d, f = ch.draft_graph(i), ch.finish_graph(i, "F0", {})
        assert _model_chain(f) == _model_chain(d) == ["ComfyUILTX25MSRICLoRALoader", "LoraLoaderModelOnly",
                                                      "UNETLoader"]
        for ct in ("LoraLoaderModelOnly", "ComfyUILTX25MSRICLoRALoader"):
            assert _nodes(f, ct)[0]["inputs"] == _nodes(d, ct)[0]["inputs"]    # same LoRAs, same strengths
        assert _nodes(f, "LoraLoaderModelOnly")[0]["inputs"]["strength_model"] == 0.45
        # no reference tokens: nothing to append, nothing to crop
        for ct in ("ComfyUILTX25MSRMultiReferenceGuide", "LTXVCropGuides", "LoadImage"):
            assert not _nodes(f, ct)
        assert _nodes(f, "ManualSigmas")[0]["inputs"]["sigmas"] == cg.LTX_STAGE2_SIGMAS
        assert _nodes(f, "KSamplerSelect")[0]["inputs"]["sampler_name"] == "euler_ancestral"
    bare = ch.finish_graph(0, "FB", {"msr_lora": False, "cq_strength": 0.0})
    assert _model_chain(bare) == ["UNETLoader"]
    s.refs = {}                               # text-to-video: the draft has no MSR LoRA either
    t2v = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    assert _model_chain(t2v.finish_graph(0, "F0", {})) == _model_chain(t2v.draft_graph(0)) == [
        "LoraLoaderModelOnly", "UNETLoader"]


def test_chains_draft_and_finish_without_the_cq_enhancer_unless_asked(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    for i in (0, 1):
        assert _model_chain(ch.draft_graph(i)) == _model_chain(ch.finish_graph(i, "F0", {})) == [
            "ComfyUILTX25MSRICLoRALoader", "UNETLoader"]
    s = _ltx_spec()
    s.cq_strength = 0.6                        # a chain drafted with it is finished with it
    ch = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    for g in (ch.draft_graph(1), ch.finish_graph(1, "F0", {})):
        assert _nodes(g, "LoraLoaderModelOnly")[0]["inputs"]["strength_model"] == 0.6


def _upstream(g, node_id):
    """Every node id node_id depends on."""
    seen, todo = set(), [node_id]
    while todo:
        for v in g[todo.pop()]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and v[0] in g and v[0] not in seen:
                seen.add(v[0])
                todo.append(v[0])
    return seen


def _released_before(g, sampler_class, encoder_class, encoder_only=False):
    """The text-encoder release sits between the last text encode and the sampler;
    H3 links release the encoder alone (a full eviction there would also drop the
    DiT the previous link left warm)."""
    (rid, rel), = [(k, n) for k, n in g.items() if n["class_type"] == "LTX25EvictModels"
                   and n["inputs"]["keep_text_encoder"] is False]
    assert rel["inputs"].get("text_encoder_only", False) is encoder_only
    assert g[rel["inputs"]["value"][0]]["class_type"] == encoder_class
    (sid,) = [k for k, n in g.items() if n["class_type"] == sampler_class]
    return rid in _upstream(g, sid)


def test_text_encoder_is_released_before_every_sampler(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    for g in (ch.draft_graph(0), ch.draft_graph(1), ch.finish_graph(0, "F0", {}), ch.finish_graph(1, "F0", {})):
        assert _released_before(g, "SamplerCustomAdvanced", "LTXVConditioning")
        # the decoder-side eviction still keeps the encoder staged for the next prompt
        assert [n["inputs"]["keep_text_encoder"] for n in _nodes(g, "LTX25EvictModels")] == [False, True]
    off = ch.finish_graph(1, "F0", {"release_text_encoder": False})
    assert [n["inputs"]["keep_text_encoder"] for n in _nodes(off, "LTX25EvictModels")] == [True]
    # H3 graphs unload no model, as the studio's own H3 workflows (a model unload at
    # an H3 job hand-over preceded both blue screens of 2026-10-23)
    h3 = ce.H3Chain(_h3_spec(), FakeRunner(tmp_path), tmp_path)
    for g in (h3.link_graph(0), h3.link_graph(1), h3.refine_graph("R0", {})):
        assert not _nodes(g, "LTX25EvictModels")
    # the encoder-only release is still available, and the digest ignores the release,
    # so H3 links linked with it are reused
    h3.spec.link["release_text_encoder"] = True
    g = h3.link_graph(1)
    assert _released_before(g, "SamplerCustomAdvanced", "MiniMaxH3ReferenceToVideo", encoder_only=True)
    del h3.spec.link["release_text_encoder"]
    assert ce.graph_digest(g) == ce.graph_digest(h3.link_graph(1))


def test_step_digest_ignores_memory_passthroughs_but_not_real_changes(tmp_path):
    ch = ce.LtxChain(_ltx_spec(), FakeRunner(tmp_path), tmp_path)
    new, old = ch.finish_graph(1, "F0", {}), ch.finish_graph(1, "F0", {"release_text_encoder": False})
    assert ce.graph_digest(new) == ce.graph_digest(old)
    assert ce.graph_digest(ch.finish_graph(1, "F0", {"seed": 7})) != ce.graph_digest(new)
    assert ce.graph_digest(ch.finish_graph(1, "F0", {"carry_restore": 0.5})) != ce.graph_digest(new)
    # a record written before the release existed hashed the raw graph without it
    legacy = {"ok": True, "digest": ce._sha(old)}
    assert ce.digest_matches(legacy, new)
    assert not ce.digest_matches({**legacy, "digest_v": ce.DIGEST_VERSION}, new)   # no fallback for v2 records
    assert not ce.digest_matches({"ok": True, "digest": ce._sha(ch.finish_graph(1, "F0", {"seed": 7,
                                  "release_text_encoder": False}))}, new)


def test_manifest_migrates_a_legacy_record_once(tmp_path):
    r = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(), r, tmp_path)
    ch.draft(0)
    rec = ch.manifest.data["steps"]["draft_l1"]
    assert rec["digest_v"] == ce.DIGEST_VERSION
    ch.spec.draft["release_text_encoder"] = False             # the graph as built before the release
    legacy = ce._sha(ch.draft_graph(0))
    del ch.spec.draft["release_text_encoder"]
    rec["digest"] = legacy
    del rec["digest_v"]
    assert ch.draft(0).get("skipped") and r.calls == ["draft_l1"]            # reused, not re-rendered
    assert ch.manifest.data["steps"]["draft_l1"]["digest_v"] == ce.DIGEST_VERSION
    assert ch.manifest.data["steps"]["draft_l1"]["digest"] == ce.graph_digest(ch.draft_graph(0))


def test_ltx_take_decodes_the_stage_latents_once(tmp_path):
    r = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(), r, tmp_path)
    for stage, want in (("draft", [f"studio_chain/t/link{i}_s1_video" for i in (1, 2, 3)]),
                        ("final", [f"studio_chain/t/F0/link{i}_s2_video" for i in (1, 2, 3)])):
        node = _nodes(ch.take_graph(stage, "F0"), "StudioLtxDecodeTake")[0]["inputs"]
        assert node["links"].splitlines() == want
        assert node["join_latent"] == cg.LTX_TAKE_JOIN and node["carry_latents"] == 4
        assert node["blend_latents"] == cg.LTX_TAKE_BLEND
    assert ch.take_frames() == 121 + 2 * 120
    # the new link enters where its crossfade starts: carried latent 1 (join 4 - blend 3)
    assert ch.junction_frames() == [97, 217]
    info = ch.take("final", "F0")
    assert r.calls == ["take_final_F0"] and info["frames"] == 361 and len(info["joins"]) == 2
    assert ch.take("final", "F0").get("skipped")
    s = _ltx_spec()
    s.take_join = 5                                                # past the 4 carried latents
    with pytest.raises(ValueError):
        s.check()


def test_pin_strengths_are_checked_against_the_carry(tmp_path):
    s = _ltx_spec()
    s.pin_strengths = "1,0.8,0.6,0.4,0.2"                          # 5 values, 4 carried latents
    with pytest.raises(ValueError):
        ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    s.pin_strengths = "1,1.2"
    with pytest.raises(ValueError):
        ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)


def test_ltx_chain_without_references_renders_text_to_video(tmp_path):
    s = _ltx_spec()
    s.refs = {}
    ch = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    for g in (ch.draft_graph(0), ch.draft_graph(1), ch.finish_graph(1, "F0", {})):
        assert not _nodes(g, "ComfyUILTX25MSRMultiReferenceGuide")
        assert not _nodes(g, "ComfyUILTX25MSRICLoRALoader") and not _nodes(g, "LTXVCropGuides")
    assert _nodes(ch.draft_graph(1), "LTX25CarryAudioLatent")


def test_ltx_steps_resume_and_check_outputs(tmp_path):
    r = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(2), r, tmp_path)
    ch.draft(0)
    ch.draft(1)
    assert r.calls == ["draft_l1", "draft_l2"]
    again = ce.LtxChain(_ltx_spec(2), r, tmp_path)            # a new process picks the manifest up
    assert again.draft(0).get("skipped")
    assert r.calls == ["draft_l1", "draft_l2"]
    assert not again.draft(0, force=True).get("skipped")
    # a different graph (other prompt) is not mistaken for the finished step
    s = _ltx_spec(2)
    s.links[0].prompt = "changed"
    ce.LtxChain(s, r, tmp_path).draft(0)
    assert r.calls[-1] == "draft_l1"
    man = json.loads((tmp_path / "studio_chain/t/manifest.json").read_text(encoding="utf-8"))
    assert man["steps"]["draft_l1"]["ok"] and man["spec"]["links"][0]["prompt"] == "changed"


def test_ltx_step_fails_on_wrong_frame_count(tmp_path):
    ch = ce.LtxChain(_ltx_spec(1), FakeRunner(tmp_path, frames_override=97), tmp_path)
    with pytest.raises(RuntimeError, match="97 frames"):
        ch.draft(0)
    man = json.loads((tmp_path / "studio_chain/t/manifest.json").read_text(encoding="utf-8"))
    assert man["steps"]["draft_l1"]["ok"] is False


def test_ltx_step_fails_on_missing_output(tmp_path):
    class Lazy(FakeRunner):
        def run(self, name, graph):
            return {"status": "success"}
    with pytest.raises(RuntimeError, match="no output"):
        ce.LtxChain(_ltx_spec(1), Lazy(tmp_path), tmp_path).draft(0)


def _h3_spec():
    return ce.H3ChainSpec(chain_id="h", refs=["r1.png", "r2.png"], start_image="s.png",
                          links=[ce.Link("a", 1, 192), ce.Link("b", 2, 192), ce.Link("c", 3, 192)])


def test_h3_spec_grid_and_take_bookkeeping():
    s = _h3_spec()
    s.check()
    assert s.seam_frames() == [192, 345]
    assert s.take_frames() == 192 + 2 * 153
    bad = _h3_spec()
    bad.links[1].frames = 124                 # on the token grid, but not on whole audio ticks
    with pytest.raises(ValueError, match="40 Hz"):
        bad.check()
    bad.links[1].frames = 100
    with pytest.raises(ValueError, match="17k"):
        bad.check()


def test_h3_wiring(tmp_path):
    ch = ce.H3Chain(_h3_spec(), FakeRunner(tmp_path), tmp_path)
    g1, g2 = ch.link_graph(0), ch.link_graph(1)
    assert _nodes(g1, "MiniMaxH3AddGuide") and not _nodes(g1, "StudioH3CarryPrefix")
    assert not _nodes(g2, "MiniMaxH3AddGuide")
    assert _nodes(g2, "StudioLoadLatent")[0]["inputs"]["relpath"] == "studio_chain/h/link1_av"
    rg = ch.refine_graph("R1", {"sigmas": "0.3, 0.0"})
    sched = _nodes(rg, "StudioH3CondSchedule")[0]["inputs"]
    assert sched["start_frames"] == "0,192,345"
    up = _nodes(rg, "MMH3SplitUpscale")[0]["inputs"]
    assert up["temporal_mode"] == "pin" and up["protected_frames"] == "192,345"
    assert _nodes(rg, "ManualSigmas")[0]["inputs"]["sigmas"] == "0.3, 0.0"
    take = _nodes(rg, "StudioH3ConcatTake")[0]["inputs"]["links"].splitlines()
    assert take == [f"studio_chain/h/link{i}_av" for i in (1, 2, 3)]


def test_h3_seam_match_is_phase_aware_on_later_links_and_feeds_save_and_decode(tmp_path):
    off = _h3_spec()
    off.seam_adain = 0.0
    assert not _nodes(ce.H3Chain(off, FakeRunner(tmp_path), tmp_path).link_graph(1), "StudioSeamAdain")
    ch = ce.H3Chain(_h3_spec(), FakeRunner(tmp_path), tmp_path)          # on by default
    assert not _nodes(ch.link_graph(0), "StudioSeamAdain")               # link 1 has no carry
    g = ch.link_graph(1)
    (sa_id, sa), = [(k, n) for k, n in g.items() if n["class_type"] == "StudioSeamAdain"]
    assert sa["inputs"] == {"latent": sa["inputs"]["latent"], "carry_latents": 12, "ramp_latents": 5,
                            "factor": 1.0, "mode": "path", "period": 5}
    spec = _h3_spec()
    spec.link = {"seam_ramp": 10}                                        # link overrides still win
    assert _nodes(ce.H3Chain(spec, FakeRunner(tmp_path), tmp_path).link_graph(1),
                  "StudioSeamAdain")[0]["inputs"]["ramp_latents"] == 10
    assert g[sa["inputs"]["latent"][0]]["class_type"] == "SamplerCustomAdvanced"
    for cls in ("StudioSaveLatent", "VAEDecode", "VAEDecodeAudio"):
        node = _nodes(g, cls)[0]["inputs"]
        assert (node.get("latent") or node.get("samples")) == [sa_id, 0]
    assert cg.h3_tokens(39) == 12 and cg.h3_tokens(192) == 57


def _seed_of(graph):
    return int(_nodes(graph, "RandomNoise")[0]["inputs"]["noise_seed"])


def _checks(monkeypatch, scores):
    """seam_qa.carry_end_check answering from `scores` in call order (score > 1 fails)."""
    import seam_qa
    calls = []

    def check(video, carry):
        sc = scores[len(calls)]
        calls.append((Path(video).name, Path(video).read_bytes()))
        return {"frame": carry, "pass": sc <= 1.0, "fails": [] if sc <= 1.0 else ["saturation"], "score": sc}
    monkeypatch.setattr(seam_qa, "carry_end_check", check)
    return calls


@pytest.mark.parametrize("second, keep_second", [(0.6, True), (3.0, False)])
def test_a_link_failing_its_carry_end_is_rerolled_once_and_the_better_kept(tmp_path, monkeypatch,
                                                                           second, keep_second):
    calls = _checks(monkeypatch, [2.0, second])
    run = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(), run, tmp_path)
    ch.draft(0)                                        # link 1 has no carry: never checked
    seed = ch.spec.links[1].seed
    info = ch.draft(1)
    assert run.calls == ["draft_l1", "draft_l2", "draft_l2"] and len(calls) == 2
    first_bytes, second_bytes = calls[0][1], calls[1][1]
    kept = seed + ce.REROLL_SEED_OFFSET if keep_second else seed
    assert info["reroll"]["kept"] == kept and [t["seed"] for t in info["reroll"]["tried"]] == [
        seed, seed + ce.REROLL_SEED_OFFSET]
    assert _seed_of(ch.draft_graph(1)) == kept
    base = tmp_path / ch.base
    assert (base / "link2_draft.mkv").read_bytes() == (second_bytes if keep_second else first_bytes)
    rejected = base / "rejected" / f"link2_seed{seed if keep_second else kept + ce.REROLL_SEED_OFFSET}"
    assert (rejected / "link2_draft.mkv").read_bytes() == (first_bytes if keep_second else second_bytes)
    assert not list(base.glob("rejected/*/link1_*"))   # only link 2's files moved
    # a resumed chain reuses the kept attempt: no render, no check
    again = ce.LtxChain(_ltx_spec(), run, tmp_path)
    assert again.draft(1)["skipped"] and run.calls.count("draft_l2") == 2 and len(calls) == 2
    # the next link carries from the kept attempt's files
    assert _nodes(again.draft_graph(2), "StudioLoadLatent")[0]["inputs"]["relpath"] == f"{ch.base}/link2_s1_video"


def test_a_passing_carry_end_or_reroll_off_drafts_once(tmp_path, monkeypatch):
    calls = _checks(monkeypatch, [0.4])
    run = FakeRunner(tmp_path)
    ch = ce.LtxChain(_ltx_spec(), run, tmp_path)
    ch.draft(0)
    assert ch.draft(1)["carry_end"]["score"] == 0.4 and run.calls == ["draft_l1", "draft_l2"]
    assert "seeds" not in ch.manifest.data and len(calls) == 1
    s = _ltx_spec()
    s.chain_id, s.reroll_seams = "noreroll", False
    off = ce.LtxChain(s, FakeRunner(tmp_path), tmp_path)
    off.draft(0)
    off.draft(1)
    assert len(calls) == 1


class _FreshRunner(FakeRunner):
    """FakeRunner that can restart its server; take steps leave a take-length decode."""

    def run(self, name, graph):
        self.frames_override = 192 + 2 * 153 if name.startswith(("decode_", "refine_")) else None
        return super().run(name, graph)

    def fresh_process(self, name):
        self.calls.append(f"fresh:{name}")


def test_h3_server_is_restarted_before_a_graph_that_would_drop_its_models(tmp_path):
    run = _FreshRunner(tmp_path)
    ch = ce.H3Chain(_h3_spec(), run, tmp_path)
    for i in range(3):
        ch.link(i)
    assert ch.decode_take("B0")["fresh_process"] is True
    r = ch.refine("R0")
    assert not r["conditioning"].get("fresh_process") and r["fresh_process"] is True
    assert r["decode"]["fresh_process"] is True
    # the links hold the DiT and the encoder, the refine's encode step the encoder, the
    # refine the DiT, the take decodes neither: a restart before each graph that would
    # drop one, none before the encode step (after a decode the server holds nothing)
    assert run.calls == ["link_l1", "link_l2", "link_l3", "fresh:decode_B0", "decode_B0",
                         "refcond_R0", "fresh:refine_R0", "refine_R0", "fresh:decode_R0", "decode_R0"]
    held = {c for c, _ in ce.held_models(ch.link_graph(0))}
    assert held == {"UNETLoader", "CLIPLoader"}
    assert ce.held_models(ch.refine_graph("R0", {})) == {m for m in ce.held_models(ch.link_graph(0))
                                                        if m[0] == "UNETLoader"}
    assert {c for c, _ in ce.held_models(ch.refine_cond_graph("R0", {}))} == {"CLIPLoader"}
    # a fresh engine does not know what the server holds: a model-less graph restarts it,
    # a graph that loads models does not
    again = ce.H3Chain(_h3_spec(), run, tmp_path)
    again.refine("R0", force=True)
    again.decode_take("B0", force=True)
    # after a decode the server holds nothing: the B0 decode runs on without a restart
    assert run.calls[-6:] == ["refcond_R0", "fresh:refine_R0", "refine_R0", "fresh:decode_R0", "decode_R0",
                              "decode_B0"]
    other = ce.H3Chain(_h3_spec(), run, tmp_path)
    other.decode_take("B0", force=True)
    assert run.calls[-2:] == ["fresh:decode_B0", "decode_B0"]
    # a runner that cannot restart still runs every step
    plain = ce.H3Chain(_h3_spec(), FakeRunner(tmp_path, frames_override=498), tmp_path)
    plain.decode_take("B0", force=True)


def test_lab_restarts_a_used_h3_server_and_never_frees_it(tmp_path, monkeypatch):
    class Proc:
        pid = 2 ** 31 - 1                                 # no such process: kill() finds nothing

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(chain_lab.time, "sleep", lambda s: None)
    monkeypatch.setattr(chain_lab, "gpu_sample", lambda: {"vram_mb": 900})
    log = []
    inst = chain_lab.Instance("h3", tmp_path)
    monkeypatch.setattr(inst, "free", lambda: log.append("free"))
    monkeypatch.setattr(inst, "start", lambda: (log.append("start"), setattr(inst, "proc", Proc())))
    inst.proc, inst.vram_baseline_mb = Proc(), 800      # back at boot level: no wait
    run = chain_lab.LabRunner(inst, tmp_path)
    run.fresh_process("decode_B0")                        # nothing run since the start
    assert log == []
    inst.jobs = 4
    run.fresh_process("decode_B0")
    assert log == ["start"]                               # killed and started, no /free
    ltx = chain_lab.Instance("ltx", tmp_path)
    monkeypatch.setattr(ltx, "free", lambda: log.append("free ltx"))
    ltx.proc = Proc()
    ltx.stop()
    assert log[-1] == "free ltx"


def test_lab_request_builds_the_studio_spec_and_stages_refs_by_content(tmp_path, monkeypatch):
    import chain_studio as cs
    comfy_in = tmp_path / "in"
    comfy_in.mkdir()
    monkeypatch.setattr(chain_lab, "COMFY_IN", comfy_in)
    monkeypatch.setattr(cs, "LTX_FACE_REFS", False)       # face crops need InsightFace
    (tmp_path / "open.png").write_bytes(b"opening frame")
    (tmp_path / "lead.png").write_bytes(b"portrait")
    cast = [{"slot": "pic1", "path": str(tmp_path / "lead.png"), "type": "face", "name": "Lead",
             "description": "a woman in her late twenties with short platinum hair"}]
    prompts = ["She walks the path. The camera follows.", "She stops at the post.", "She looks out.", "She turns."]
    req = {"chain": "lab_req", "model": "ltx25_s6_cq", "seed": 50, "frames": 97, "negative": "",
           "start_image": str(tmp_path / "open.png"), "cast": cast, "prompts": prompts}
    path = tmp_path / "req.json"
    path.write_text(json.dumps(req), encoding="utf-8")
    spec, models = chain_lab.ltx_request_spec(path)
    assert spec.chain_id == "lab_req" and isinstance(models, cg.LtxModels)
    assert [lk.frames for lk in spec.links] == cs.ltx_link_frames(97, 4) == [97, 121, 121, 121]
    assert [lk.seed for lk in spec.links] == [50, 51, 52, 53]
    descs = cs.cast_descriptions(cast) if cs.LTX_CAST_LINE else []
    assert [lk.prompt for lk in spec.links] == [cs.with_cast_line(p, descs) for p in prompts]
    # the studio's slots (start image first, then the cast), each staged under a
    # content name, so a re-run of the request builds the same graphs
    assert set(spec.refs) == set(cs.ltx_reference_slots(Path(req["start_image"]), cast)) == {"pic1", "pic2"}
    assert all(n.startswith("lab_") and (comfy_in / n).read_bytes() for n in spec.refs.values())
    assert chain_lab.ltx_request_spec(path)[0].refs == spec.refs
    assert len(list(comfy_in.iterdir())) == 2
    path.write_text(json.dumps({**req, "model": "minimax_h3_hero_v2"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="does not render seamless ltx chains"):
        chain_lab.ltx_request_spec(path)


def test_h3_refine_encodes_refines_and_decodes_in_separate_steps(tmp_path):
    class Take(FakeRunner):
        def run(self, name, graph):
            self.frames_override = 192 + 2 * 153
            return super().run(name, graph)
    run = Take(tmp_path)
    ch = ce.H3Chain(_h3_spec(), run, tmp_path)
    # encode step: the encoder, the VAEs and the references; one saved conditioning per
    # link, encoded as the link sampled it (its prompt and window, at the base size)
    cg_ = ch.refine_cond_graph("R0", {})
    r2v = _nodes(cg_, "MiniMaxH3ReferenceToVideo")
    assert [n["inputs"]["prompt"] for n in r2v] == [lk.prompt for lk in ch.spec.links]
    assert [n["inputs"]["length"] for n in r2v] == [lk.frames for lk in ch.spec.links]
    assert {(n["inputs"]["width"], n["inputs"]["height"]) for n in r2v} == {(960, 544)}
    saves = [n["inputs"] for n in _nodes(cg_, "StudioSaveConditioning")]
    assert [s["relpath"] for s in saves] == [f"studio_chain/h/R0/take_cond{j}" for j in (1, 2, 3)]
    assert all(cg_[s["conditioning"][0]]["class_type"] == "MiniMaxH3ReferenceToVideo" for s in saves)
    assert not _nodes(cg_, "UNETLoader")
    # refine step: the DiT and the upscaler; the conditionings come from disk, in link order
    rg = ch.refine_graph("R0", {})
    for cls in ("CLIPLoader", "VAELoader", "LoadImage", "MiniMaxH3ReferenceToVideo", "VAEDecode",
                "StudioSaveVideoLossless", "LTX25EvictModels"):
        assert not _nodes(rg, cls), cls
    sched = _nodes(rg, "StudioH3CondSchedule")[0]["inputs"]
    assert [rg[sched[f"cond_{j}"][0]]["inputs"]["relpath"] for j in (1, 2, 3)] == [
        f"studio_chain/h/R0/take_cond{j}" for j in (1, 2, 3)]
    assert _nodes(rg, "MMH3SplitUpscale")[0]["inputs"]["conditioning"] == sched["cond_1"]
    assert _nodes(rg, "StudioSaveLatent")[0]["inputs"]["relpath"] == "studio_chain/h/R0/take_take_av"
    info = ch.refine("R0")
    assert info["decode"]["frames"] == 498 and run.calls == ["refcond_R0", "refine_R0", "decode_R0"]
    dec = json.loads((tmp_path / "studio_chain/h/R0/take_take_final.json").read_text(encoding="utf-8"))
    assert dec["frames"] == 498
    # each step records the files it reads: new conditionings invalidate the refine, a
    # new refined latent the decode; a resume reuses all three
    steps = ch.manifest.data["steps"]
    assert set(steps["refine_R0"]["inputs"]) >= {f"studio_chain/h/R0/take_cond{j}" for j in (1, 2, 3)}
    assert set(steps["decode_R0"]["inputs"]) == {"studio_chain/h/R0/take_take_av"}
    again = ce.H3Chain(_h3_spec(), run, tmp_path).refine("R0")
    assert again["skipped"] and again["conditioning"]["skipped"] and again["decode"]["skipped"]
    assert run.calls == ["refcond_R0", "refine_R0", "decode_R0"]
    with pytest.raises(RuntimeError, match="decoded 121 frames, expected 498"):
        ce.H3Chain(_h3_spec(), FakeRunner(tmp_path, frames_override=121), tmp_path).refine("R0", force=True)


OBJECT_INFO = {
    "Loader": {"input": {"required": {"name": [["m1.safetensors", "m2.safetensors"]]}}, "output": ["MODEL"]},
    "Use": {"input": {"required": {"model": ["MODEL"], "steps": ["INT", {"default": 4}]},
                      "optional": {"extra": ["LATENT"],
                                   "mode": ["COMFY_DYNAMICCOMBO_V3", {"options": []}]}},
            "output": ["LATENT"]},
    "Pick": {"input": {"required": {"choice": ["COMBO", {"options": ["a", "b"]}]}}, "output": []},
}


def test_validator_accepts_a_good_graph():
    g = {"1": {"class_type": "Loader", "inputs": {"name": "m1.safetensors"}},
         "2": {"class_type": "Use", "inputs": {"model": ["1", 0], "steps": 6, "mode.width": 3}},
         "3": {"class_type": "Pick", "inputs": {"choice": "b"}}}
    assert chain_lab.validate(g, OBJECT_INFO) == []


def test_validator_catches_every_class_of_mistake():
    g = {"1": {"class_type": "Loader", "inputs": {"name": "missing.safetensors"}},
         "2": {"class_type": "Use", "inputs": {"model": ["9", 0], "typo": 1}},
         "3": {"class_type": "Use", "inputs": {"model": ["2", 0], "steps": 1}},
         "4": {"class_type": "Use", "inputs": {"model": ["1", 3], "steps": 1}},
         "5": {"class_type": "Nope", "inputs": {}},
         "6": {"class_type": "Pick", "inputs": {"choice": "c"}}}
    errs = "\n".join(chain_lab.validate(g, OBJECT_INFO))
    for needle in ("'missing.safetensors' is not an available option", "missing node #9", "unknown input 'typo'",
                   "missing required input 'steps'", "expects MODEL, gets LATENT", "slot 3 out of range",
                   "unknown node class Nope", "'c' is not an available option"):
        assert needle in errs, needle
