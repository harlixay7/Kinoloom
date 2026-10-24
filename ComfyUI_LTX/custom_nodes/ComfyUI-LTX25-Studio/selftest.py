"""CPU self-test of the chain nodes inside the real ComfyUI package (no GPU, no server).

usage (from the repo root):
    ComfyUI_LTX\\venv\\Scripts\\python.exe ComfyUI_LTX\\custom_nodes\\ComfyUI-LTX25-Studio\\selftest.py

Checks the node classes end to end against ComfyUI's own helpers: save/load round
trips (NestedTensor latents, uint8 frames, FFV1 video), the LTX and H3 carries, that
the noise masks survive comfy.utils.reshape_mask (the sampler's mask preparation)
without interpolation smear, and that MMH3SplitUpscale's pin-mode chunk planner
agrees with the unit-tested twin in studio_chain_core.
"""
import importlib.util
import json
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMFY = HERE.parents[1]
sys.path.insert(0, str(COMFY))

import numpy as np  # noqa: E402
import torch  # noqa: E402

import comfy.nested_tensor  # noqa: E402
import comfy.utils  # noqa: E402
import folder_paths  # noqa: E402


def _load_pack():
    spec = importlib.util.spec_from_file_location(
        "studio_pack", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["studio_pack"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_mmh3_helpers():
    p = COMFY / "custom_nodes" / "Comfyui_Minimax_h3_latent_Upscaler" / "nodes" / "MMH3_Split_Upscale.py"
    spec = importlib.util.spec_from_file_location("mmh3_split", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="studio_selftest_"))
    folder_paths.set_output_directory(str(tmp))
    pack = _load_pack()
    N = pack.NODE_CLASS_MAPPINGS
    core = sys.modules["studio_pack.studio_chain_core"]
    print("nodes:", sorted(k for k in N if k.startswith(("Studio", "LTX25"))))

    # -- path guard
    for bad in ("../x", "/abs", "C:/x", "a/../b", ""):
        try:
            N["StudioSaveLatent"]().save({"samples": torch.zeros(1)}, bad)
            raise AssertionError(f"path {bad!r} accepted")
        except ValueError:
            pass
    print("  ok: path guard rejects traversal/absolute paths")

    # -- nested latent round trip with masks and metadata
    v = torch.randn(1, 24, 57, 4, 6)
    a = torch.randn(1, 32, 2, 320)
    lat = {"samples": comfy.nested_tensor.NestedTensor((v, a)),
           "noise_mask": comfy.nested_tensor.NestedTensor((torch.zeros(1, 1, 57, 4, 6), torch.ones(1, 1, 2, 320))),
           "generated_keyframe_indices": [48, 72], "generated_keyframe_num_frames": 145}
    _, rel = N["StudioSaveLatent"]().save(lat, "studio_chain/t/link01", save_noise_mask=True)
    back = N["StudioLoadLatent"]().load(rel.removesuffix(".safetensors"))[0]
    bv, ba = back["samples"].unbind()
    check(torch.equal(bv, v) and torch.equal(ba, a), "nested latent round trip is exact")
    check(back["generated_keyframe_indices"] == [48, 72] and back["generated_keyframe_num_frames"] == 145,
          "latent metadata survives")
    check(back["noise_mask"].unbind()[0].shape == (1, 1, 57, 4, 6), "noise mask round trip")

    # -- conditioning round trip (an H3 Ref2VA conditioning: text embedding, pooled,
    # reference blocks with latents and ints, a nested tensor, None, floats, bools)
    emb = torch.randn(1, 77, 64, dtype=torch.bfloat16)
    zref = torch.randn(1, 24, 1, 34, 60)
    cond = [[emb, {"pooled_output": None, "attention_mask": torch.ones(1, 77, dtype=torch.int64),
                   "minimax_refs": [{"kind": "image", "latent_h": 34, "latent_w": 60, "latent": zref},
                                    {"kind": "audio", "ref_audio_t": 0, "audio_latent": None}],
                   "strength": 0.30000000000000004, "flag": True,
                   "nested": comfy.nested_tensor.NestedTensor((zref, torch.ones(1, 32, 2, 8)))}],
            [emb * 2, {"pooled_output": torch.zeros(1, 8)}]]
    crel = N["StudioSaveConditioning"]().save(cond, "studio_chain/t/cond1")[0]
    got = N["StudioLoadConditioning"]().load(crel.removesuffix(".safetensors"))[0]
    g0 = got[0][1]
    check(len(got) == 2 and torch.equal(got[0][0], emb) and got[0][0].dtype == torch.bfloat16
          and torch.equal(got[1][0], emb * 2) and torch.equal(got[1][1]["pooled_output"], torch.zeros(1, 8)),
          "conditioning tensors round trip exact (dtype kept)")
    check(g0["pooled_output"] is None and g0["strength"] == 0.30000000000000004 and g0["flag"] is True
          and torch.equal(g0["attention_mask"], cond[0][1]["attention_mask"])
          and g0["minimax_refs"][0]["latent_h"] == 34 and torch.equal(g0["minimax_refs"][0]["latent"], zref)
          and g0["minimax_refs"][1]["audio_latent"] is None,
          "conditioning structure round trips (refs, ints, floats, bools, None)")
    check(g0["nested"].is_nested and torch.equal(g0["nested"].unbind()[0], zref), "nested tensors in a conditioning")
    try:
        N["StudioSaveConditioning"]().save([[emb, {"hook": object()}]], "studio_chain/t/cond_bad")
        raise AssertionError("an unsupported object was saved")
    except ValueError:
        pass
    check(not (tmp / "studio_chain/t/cond_bad.safetensors").exists(), "unsupported conditioning values are refused")

    # -- uint8 frames
    img = torch.rand(30, 16, 24, 3)
    N["StudioSaveImages"]().save(img, "studio_chain/t/tail", last_n=25)
    got = N["StudioLoadImages"]().load("studio_chain/t/tail")[0]
    want = (img[-25:] * 255).round() / 255
    check(got.shape == (25, 16, 24, 3) and torch.allclose(got, want, atol=1e-6), "tail frames exact (uint8)")

    # -- lossless video + audio sidecars
    wave = torch.sin(torch.linspace(0, 200, 32000)).view(1, 1, -1).repeat(1, 2, 1) * 0.3
    N["StudioSaveVideoLossless"]().save(img, 24.0, "studio_chain/t/link01_draft",
                                        audio={"waveform": wave, "sample_rate": 32000})
    import av
    with av.open(str(tmp / "studio_chain/t/link01_draft.mkv")) as ct:
        frames = [f.to_ndarray(format="rgb24") for f in ct.decode(video=0)]
    exp = (img * 255).round().to(torch.uint8).numpy()
    check(len(frames) == 30 and all(np.array_equal(f, e) for f, e in zip(frames, exp)), "FFV1 video bit-exact")
    side = json.loads((tmp / "studio_chain/t/link01_draft.json").read_text())
    check(side["frames"] == 30 and side["audio"]["samples"] == 32000, "sidecar counts")

    # -- LTX audio carry + mask through the sampler's reshape_mask
    src_audio = {"samples": torch.arange(151, dtype=torch.float32).view(1, 1, 151, 1).expand(1, 8, 151, 16).clone()}
    vlat = {"samples": torch.zeros(1, 128, 19, 17, 30),
            "noise_mask": torch.cat([torch.zeros(1, 1, 4, 1, 1), torch.ones(1, 1, 15, 1, 1)], dim=2)}
    out = N["LTX25CarryAudioLatent"]().wrap(vlat, 24.0, 25.0, 145, source_audio=src_audio,
                                           source_frames=145, carry_frames=25)[0]
    sv, sa = out["samples"].unbind()
    mv, ma = out["noise_mask"].unbind()
    check(sa.shape == (1, 8, 151, 16) and torch.equal(sa[:, :, :26], src_audio["samples"][:, :, 125:151]),
          "LTX audio carry copies the time-aligned tail")
    pm = comfy.utils.reshape_mask(ma, sa.shape)
    check(pm.shape == sa.shape and pm[:, :, :26].abs().max() == 0 and pm[:, :, 26:].min() == 1,
          "LTX audio mask survives reshape_mask without smear")
    pv = comfy.utils.reshape_mask(mv, sv.shape)
    check(pv[:, :, :4].abs().max() == 0 and pv[:, :, 4:].min() == 1, "LTX video mask survives reshape_mask")

    # -- full noise mask: the keyframe node must see every frame as free
    from comfy_extras.nodes_lt_keyframes import occupied_pixel_frames, free_detailing_slots
    up = {"samples": torch.randn(1, 128, 19, 4, 4)}
    masked = N["StudioSetNoiseMask"]().apply(up, 1.0)[0]
    check(masked["noise_mask"].shape == (1, 1, 19, 1, 1), "full noise mask has the LTX [B,1,T,1,1] shape")
    occ = occupied_pixel_frames(masked, 8, 145)
    check(occ == set() and free_detailing_slots(145, 24.0, occ) == [24, 48, 72, 96, 120, 144],
          "finish of link 1: stage-1 keyframe slots are free again")
    check(len(occupied_pixel_frames(up, 8, 145)) == 19, "(without the mask every frame reads as occupied)")

    # -- soft carry: per-latent strengths, slots unchanged
    pinned = {"samples": torch.randn(1, 128, 19, 4, 4),
              "noise_mask": torch.cat([torch.zeros(1, 1, 4, 1, 1), torch.ones(1, 1, 15, 1, 1)], dim=2)}
    soft = N["StudioPinRamp"]().apply(pinned, "1, 0.8, 0.6, 0.4")[0]["noise_mask"]
    check(torch.allclose(soft[0, 0, :5, 0, 0], torch.tensor([0.0, 0.2, 0.4, 0.6, 1.0])),
          "pin ramp sets mask = 1 - strength on the carried latents only")
    check(occupied_pixel_frames({"samples": pinned["samples"], "noise_mask": soft}, 8, 145) == {0, 8, 16, 24},
          "soft-pinned latents still count as occupied (keyframe slots unchanged)")

    # -- LTX audio take: one continuous stream from carried links
    l1 = torch.arange(126, dtype=torch.float32).view(1, 1, 126, 1).expand(1, 8, 126, 16).clone()
    l2 = N["LTX25CarryAudioLatent"]().wrap({"samples": torch.zeros(1, 128, 19, 2, 2)}, 24.0, 25.0, 145,
                                           source_audio={"samples": l1}, source_frames=121,
                                           carry_frames=25)[0]["samples"].unbind()[1].clone()
    l2[:, :, 26:] = (100 + torch.arange(26, 151, dtype=torch.float32)).view(1, 1, -1, 1)
    N["StudioSaveLatent"]().save({"samples": l1}, "studio_chain/t/a1")
    N["StudioSaveLatent"]().save({"samples": l2}, "studio_chain/t/a2")
    tk, tinfo = N["StudioLtxAudioTake"]().join("studio_chain/t/a1\nstudio_chain/t/a2", "121,145", 25, 24.0, 25.0)
    tinfo = json.loads(tinfo)
    check(torch.equal(tk["samples"][0, 0, :, 0], torch.arange(251, dtype=torch.float32))
          and tinfo["latents"] == tinfo["expected_latents"] == 251 and tinfo["joins"][0]["carry_exact"],
          "audio take joins carried links into one continuous latent stream")
    N["StudioSaveAudio"]().save({"waveform": wave, "sample_rate": 32000}, "studio_chain/t/take_audio",
                                info=json.dumps(tinfo))
    aside = json.loads((tmp / "studio_chain/t/take_audio.json").read_text())
    check(aside["audio"]["samples"] == 32000 and aside["join"]["latents"] == 251
          and (tmp / "studio_chain/t/take_audio.audio.safetensors").is_file()
          and (tmp / "studio_chain/t/take_audio.wav").is_file(), "take audio saved with its join report")

    # -- LTX latent carry and the decode-once take, against a fake VAE whose decode
    #    writes each frame's absolute take index into the pixels
    class FakeVae:
        downscale_index_formula = (8, 32, 32)

        def temporal_compression_decode(self):
            return 8

        def spacial_compression_decode(self):
            return 32

        def encode(self, px):
            return torch.full((1, 4, 1, px.shape[1] // 32, px.shape[2] // 32), -1.0)

        def decode_tiled(self, samples, tile_x=None, tile_y=None, overlap=None, tile_t=None, overlap_t=None):
            t0 = int(round(float(samples[0, 0, 0, 0, 0])))       # the window's first absolute latent
            n = 8 * (samples.shape[2] - 1) + 1
            idx = torch.arange(8 * t0, 8 * t0 + n, dtype=torch.float32)
            img = torch.zeros(n, samples.shape[3] * 32, samples.shape[4] * 32, 3)
            img[..., 0] = (idx % 256).view(-1, 1, 1) / 255.0
            img[..., 1] = (idx // 256).view(-1, 1, 1) / 255.0
            return img

    fv = FakeVae()
    prev = torch.randn(1, 4, 16, 1, 2)
    lc = N["StudioLtxLatentCarry"]().apply(fv, torch.rand(25, 40, 70, 3), {"samples": torch.zeros(1, 4, 19, 1, 2)},
                                           {"samples": prev}, 4, 1.0)[0]
    check(torch.equal(lc["samples"][:, :, 1:4], prev[:, :, 13:16]) and float(lc["samples"][0, 0, 0, 0, 0]) == -1.0
          and torch.equal(lc["noise_mask"][0, 0, :, 0, 0], torch.cat([torch.zeros(4), torch.ones(15)])),
          "LTX latent carry: anchor encoded, 3 latents copied exactly, hard-pin mask")
    ts = [16, 19, 19, 19]
    absl, lats = 0, []
    for i, t in enumerate(ts):                                   # latents hold their absolute take index
        x = torch.zeros(1, 4, t, 1, 2)
        start = 0 if i == 0 else absl - 4
        x[:, :, :] = torch.arange(start, start + t, dtype=torch.float32).view(1, 1, -1, 1, 1)
        if i:
            x[:, :, 0] = start                                   # (the anchor is never shown)
        absl = start + t
        N["StudioSaveLatent"]().save({"samples": x}, f"studio_chain/t/ltx_l{i + 1}")
        lats.append(x)
    rel, tinfo = N["StudioLtxDecodeTake"]().decode(
        fv, "\n".join(f"studio_chain/t/ltx_l{i + 1}" for i in range(4)), 4, 1, 24.0, "studio_chain/t/take",
        512, 64, 64, 16, 12, 2, 2)["result"]
    tinfo = json.loads(tinfo)
    with av.open(str(tmp / "studio_chain/t/take.mkv")) as ct:
        got_idx = [int(f.to_ndarray(format="rgb24")[0, 0, 0]) + 256 * int(f.to_ndarray(format="rgb24")[0, 0, 1])
                   for f in ct.decode(video=0)]
    check(len(tinfo["windows"]) > 2 and got_idx == list(range(121 + 3 * 120)),
          f"decode-once take: {len(tinfo['windows'])} streamed windows give every frame once, in order")

    # -- H3 carry + masks through reshape_mask
    tv, ta = torch.zeros(1, 24, 57, 4, 6), torch.zeros(1, 32, 2, 320)
    res = N["StudioH3CarryPrefix"]().carry({"samples": comfy.nested_tensor.NestedTensor((tv, ta))},
                                          {"samples": comfy.nested_tensor.NestedTensor((v, a))}, 39)[0]
    hv, ha = res["samples"].unbind()
    hmv, hma = res["noise_mask"].unbind()
    check(torch.equal(hv[:, :, :12], v[:, :, 45:]) and torch.equal(ha[..., :65], a[..., 255:]),
          "H3 carry copies 12 tokens + 65 ticks")
    pv = comfy.utils.reshape_mask(hmv, hv.shape)
    pa = comfy.utils.reshape_mask(hma, ha.shape)
    check(pv[:, :, :12].abs().max() == 0 and pv[:, :, 12:].min() == 1, "H3 video mask exact after reshape_mask")
    check(pa[..., :65].abs().max() == 0 and pa[..., 65:].min() == 1, "H3 audio mask exact after reshape_mask")

    # -- take join from saved links
    N["StudioSaveLatent"]().save({"samples": comfy.nested_tensor.NestedTensor((v, a))}, "studio_chain/t/h3_l1")
    N["StudioSaveLatent"]().save(res, "studio_chain/t/h3_l2")
    take, info = N["StudioH3ConcatTake"]().concat("studio_chain/t/h3_l1\nstudio_chain/t/h3_l2", 39)
    info = json.loads(info)
    tvv, taa = take["samples"].unbind()
    check(info["frames"] == 345 and info["seam_frames"] == [192] and tvv.shape[2] == 102 and taa.shape[-1] == 575,
          "take join: 192 + 153 frames, seam at 192")

    # -- drift guard on nested and plain latents
    ref = {"samples": torch.randn(1, 128, 19, 4, 4) * 0.7 + 0.2}
    x = {"samples": torch.randn(1, 128, 19, 4, 4)}
    y = N["StudioAdainLatent"]().apply(x, ref, 1.0, 4, 0)[0]["samples"]
    check(torch.equal(y[:, :, :4], x["samples"][:, :, :4]), "drift guard leaves the carried frames alone")
    yn = N["StudioAdainLatent"]().apply({"samples": comfy.nested_tensor.NestedTensor((x["samples"], torch.ones(1, 8, 151, 16)))},
                                        ref, 0.5, 4, 8)[0]["samples"]
    check(yn.is_nested and torch.equal(yn.unbind()[1], torch.ones(1, 8, 151, 16)), "drift guard keeps audio untouched")

    # -- conditioning schedule
    sch = N["StudioH3CondSchedule"]().build("0,192", cond_1=[["a", {}]], cond_2=[["b", {}]])[0]
    mm = _load_mmh3_helpers()
    check(mm.pick_conditioning(sch, 10)[1] == 0 and mm.pick_conditioning(sch, 200)[1] == 1, "schedule picks the owning link")

    # -- MMH3 pin-mode planner == tested core planner
    rng = random.Random(3)
    n = 0
    for _ in range(400):
        T = 5 * rng.randint(3, 140) + 2
        cf = 5 + 17 * rng.randint(4, 10)
        of = 5 + 17 * rng.randint(0, 2)
        tc, to = core.h3_tokens_for_clip(cf), core.h3_tokens_for_clip(of)
        if to >= tc - 10:
            continue
        seams_f = sorted({core.h3_frames_for_tokens(5 * rng.randint(1, max(1, (T - 2) // 5))) for _ in range(3)})
        seams_t = [mm.tokens_for_frames(f) for f in seams_f]
        a_ = [(b[0], b[2]) for b in mm.plan_pinned_chunks(T, cf, of, seams_f)]
        b_ = core.h3_plan_chunks(T, tc, to, seams_t, 5)
        assert a_ == b_, (T, cf, of, seams_f, a_, b_)
        n += 1
    check(n > 100, f"MMH3 pin planner matches studio_chain_core on {n} random takes")
    print("SELFTEST PASSED")


if __name__ == "__main__":
    main()
