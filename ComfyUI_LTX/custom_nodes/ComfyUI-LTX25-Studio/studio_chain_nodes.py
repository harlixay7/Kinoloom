"""Video Studio chain-continuation nodes (LTX-2.5 and MiniMax H3).

A chain renders a long take as links. Each link continues the previous one from
saved, lossless state instead of from an encoded mp4:

* latents (including joint audio/video NestedTensor latents) are saved and loaded
  with StudioSaveLatent / StudioLoadLatent;
* carried pixel frames are saved bit-exact as uint8 tensors (StudioSaveImages /
  StudioLoadImages) and every link is also written as lossless FFV1
  (StudioSaveVideoLossless) for the one final encode;
* LTX25CarryAudioLatent and StudioH3CarryPrefix put the previous link's tail into
  the head of the new window under noise mask 0, so the sampler continues those
  frames instead of redrawing them;
* StudioH3ConcatTake joins H3 links into one take latent for a single refine pass;
* StudioAdainLatent is the drift guard (per-channel statistics matched to link 1);
* StudioH3CondSchedule hands MMH3SplitUpscale one conditioning per link;
  StudioSaveConditioning / StudioLoadConditioning let the refine sample in a
  server that never loads the text encoder.

All file access is confined to ComfyUI's output directory. The math lives in
studio_chain_core.py.
"""
from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

import numpy as np
import torch

import comfy.nested_tensor
import folder_paths

from . import studio_chain_core as core

_SAFE_META_TYPES = (int, float, str, bool, type(None))


def _out_root() -> Path:
    return Path(folder_paths.get_output_directory()).resolve()


def _resolve(relpath: str, suffix: str) -> Path:
    """Map a relative name onto a file under ComfyUI's output directory."""
    rel = str(relpath or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ":" in rel or any(p in ("..", "") for p in rel.split("/")):
        raise ValueError(f"invalid studio path {relpath!r}: use a relative path like "
                         "'studio_chain/<id>/<name>'")
    root = _out_root()
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"studio path {relpath!r} escapes the output directory")
    if suffix and not p.name.endswith(suffix):
        p = p.with_name(p.name + suffix)
    return p


def _file_sig(p: Path) -> str:
    try:
        st = p.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "missing"


def _say(msg: str) -> None:
    try:
        print(f"[Studio] {msg}", flush=True)
    except Exception:
        pass


def _json_meta(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k in ("samples", "noise_mask"):
            continue
        if isinstance(v, _SAFE_META_TYPES):
            out[k] = v
        elif isinstance(v, (list, tuple)) and all(isinstance(x, _SAFE_META_TYPES) for x in v):
            out[k] = list(v)
    return out


def _streams(x) -> list:
    if x is None:
        return []
    if getattr(x, "is_nested", False):
        return list(x.unbind())
    return [x]


def _pack(streams):
    if len(streams) == 1:
        return streams[0]
    return comfy.nested_tensor.NestedTensor(tuple(streams))


# ------------------------------------------------------------------ latents
class StudioSaveLatent:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "relpath": ("STRING", {"default": "studio_chain/run/latent"}),
                             "save_noise_mask": ("BOOLEAN", {"default": False})}}

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "path")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "LTX25/chain"

    def save(self, latent, relpath, save_noise_mask=False):
        from safetensors.torch import save_file
        p = _resolve(relpath, ".safetensors")
        p.parent.mkdir(parents=True, exist_ok=True)
        streams = _streams(latent["samples"])
        tensors = {f"stream_{i}": s.detach().cpu().contiguous() for i, s in enumerate(streams)}
        masks = _streams(latent.get("noise_mask")) if save_noise_mask else []
        if masks:
            tensors.update({f"mask_{i}": m.detach().cpu().float().contiguous()
                            for i, m in enumerate(masks)})
        meta = {"studio": json.dumps({"streams": len(streams),
                                      "masks": len(masks),
                                      "shapes": [list(s.shape) for s in streams],
                                      "extra": _json_meta(latent)})}
        tmp = p.with_name(p.name + ".part")
        save_file(tensors, str(tmp), metadata=meta)
        os.replace(tmp, p)
        _say(f"saved latent {p.relative_to(_out_root())} "
             + " ".join(str(list(s.shape)) for s in streams))
        return (latent, str(p.relative_to(_out_root())).replace("\\", "/"))


class StudioLoadLatent:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"relpath": ("STRING", {"default": "studio_chain/run/latent"})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "load"
    CATEGORY = "LTX25/chain"

    @classmethod
    def IS_CHANGED(cls, relpath):
        return _file_sig(_resolve(relpath, ".safetensors"))

    def load(self, relpath):
        from safetensors import safe_open
        p = _resolve(relpath, ".safetensors")
        if not p.is_file():
            raise FileNotFoundError(f"studio latent not found: {relpath}")
        with safe_open(str(p), framework="pt") as f:
            meta = json.loads((f.metadata() or {}).get("studio") or "{}")
            n = int(meta.get("streams") or 1)
            streams = [f.get_tensor(f"stream_{i}") for i in range(n)]
            nm = int(meta.get("masks") or 0)
            masks = [f.get_tensor(f"mask_{i}") for i in range(nm)]
        out = dict(meta.get("extra") or {})
        out["samples"] = _pack(streams)
        if masks:
            out["noise_mask"] = _pack(masks)
        _say(f"loaded latent {relpath} " + " ".join(str(list(s.shape)) for s in streams))
        return (out,)


# ------------------------------------------------------------- conditioning
def _cond_flatten(x, tensors: list):
    """A conditioning as JSON-able structure plus a flat tensor list (no pickle)."""
    if getattr(x, "is_nested", False):
        return {"__nested__": [_cond_flatten(t, tensors) for t in x.unbind()]}
    if isinstance(x, torch.Tensor):
        tensors.append(x.detach().cpu().clone().contiguous())
        return {"__tensor__": len(tensors) - 1}
    if isinstance(x, (list, tuple)):
        return [_cond_flatten(v, tensors) for v in x]
    if isinstance(x, dict):
        bad = [k for k in x if not isinstance(k, str) or k in ("__tensor__", "__nested__")]
        if bad:
            raise ValueError(f"conditioning key {bad[0]!r} cannot be saved")
        return {k: _cond_flatten(v, tensors) for k, v in x.items()}
    if isinstance(x, _SAFE_META_TYPES):
        return x
    raise ValueError(f"a {type(x).__name__} in a conditioning cannot be saved")


def _cond_unflatten(x, tensors: list):
    if isinstance(x, dict):
        if "__tensor__" in x:
            return tensors[int(x["__tensor__"])]
        if "__nested__" in x:
            return comfy.nested_tensor.NestedTensor(tuple(_cond_unflatten(t, tensors) for t in x["__nested__"]))
        return {k: _cond_unflatten(v, tensors) for k, v in x.items()}
    if isinstance(x, list):
        return [_cond_unflatten(v, tensors) for v in x]
    return x


class StudioSaveConditioning:
    """Save a CONDITIONING exactly (tensors in safetensors, the structure as JSON),
    so an H3 refine can sample in a server that never loads the text encoder."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"conditioning": ("CONDITIONING",),
                             "relpath": ("STRING", {"default": "studio_chain/run/cond"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "LTX25/chain"

    def save(self, conditioning, relpath):
        from safetensors.torch import save_file
        p = _resolve(relpath, ".safetensors")
        p.parent.mkdir(parents=True, exist_ok=True)
        tensors: list = []
        tree = _cond_flatten(conditioning, tensors)
        tmp = p.with_name(p.name + ".part")
        save_file({f"t{i}": t for i, t in enumerate(tensors)}, str(tmp),
                  metadata={"studio_conditioning": json.dumps({"tree": tree, "tensors": len(tensors)})})
        os.replace(tmp, p)
        _say(f"saved conditioning {p.relative_to(_out_root())} ({len(tensors)} tensors)")
        return (str(p.relative_to(_out_root())).replace("\\", "/"),)


class StudioLoadConditioning:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"relpath": ("STRING", {"default": "studio_chain/run/cond"})}}

    RETURN_TYPES = ("CONDITIONING",)
    FUNCTION = "load"
    CATEGORY = "LTX25/chain"

    @classmethod
    def IS_CHANGED(cls, relpath):
        return _file_sig(_resolve(relpath, ".safetensors"))

    def load(self, relpath):
        from safetensors import safe_open
        p = _resolve(relpath, ".safetensors")
        if not p.is_file():
            raise FileNotFoundError(f"studio conditioning not found: {relpath}")
        with safe_open(str(p), framework="pt") as f:
            meta = json.loads((f.metadata() or {}).get("studio_conditioning") or "{}")
            if "tree" not in meta:
                raise ValueError(f"{relpath} is not a saved studio conditioning")
            tensors = [f.get_tensor(f"t{i}") for i in range(int(meta["tensors"]))]
        _say(f"loaded conditioning {relpath} ({len(tensors)} tensors)")
        return (_cond_unflatten(meta["tree"], tensors),)


# ------------------------------------------------------------------- images
class StudioSaveImages:
    """Save IMAGE frames bit-exact as uint8 (the precision of the final delivery)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",),
                             "relpath": ("STRING", {"default": "studio_chain/run/tail"}),
                             "last_n": ("INT", {"default": 0, "min": 0, "max": 100000,
                                                "tooltip": "0 = all frames"})}}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "path")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "LTX25/chain"

    def save(self, images, relpath, last_n=0):
        from safetensors.torch import save_file
        p = _resolve(relpath, ".safetensors")
        p.parent.mkdir(parents=True, exist_ok=True)
        sel = images[-int(last_n):] if last_n and last_n < images.shape[0] else images
        u8 = (sel[..., :3].clamp(0, 1) * 255.0).round().to(torch.uint8).cpu().contiguous()
        tmp = p.with_name(p.name + ".part")
        save_file({"frames": u8}, str(tmp),
                  metadata={"studio": json.dumps({"frames": int(u8.shape[0]),
                                                  "source_frames": int(images.shape[0])})})
        os.replace(tmp, p)
        _say(f"saved {u8.shape[0]} frame(s) {list(u8.shape)} -> {p.relative_to(_out_root())}")
        return (images, str(p.relative_to(_out_root())).replace("\\", "/"))


class StudioLoadImages:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"relpath": ("STRING", {"default": "studio_chain/run/tail"})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "load"
    CATEGORY = "LTX25/chain"

    @classmethod
    def IS_CHANGED(cls, relpath):
        return _file_sig(_resolve(relpath, ".safetensors"))

    def load(self, relpath):
        from safetensors.torch import load_file
        p = _resolve(relpath, ".safetensors")
        if not p.is_file():
            raise FileNotFoundError(f"studio frames not found: {relpath}")
        u8 = load_file(str(p))["frames"]
        _say(f"loaded {u8.shape[0]} frame(s) {list(u8.shape)} from {relpath}")
        return (u8.float() / 255.0,)


def _write_audio(p: Path, audio: dict) -> dict:
    """<p>.audio.safetensors (float32 waveform [C, L]) and a 16-bit <p>.wav."""
    import wave
    from safetensors.torch import save_file
    wf = audio["waveform"][0].detach().float().cpu().contiguous()   # [C, L]
    sr = int(audio["sample_rate"])
    save_file({"waveform": wf}, str(p.with_suffix(".audio.safetensors")), metadata={"sample_rate": str(sr)})
    pcm = (wf.clamp(-1, 1) * 32767.0).round().to(torch.int16).numpy().T.copy()
    with wave.open(str(p.with_suffix(".wav")), "wb") as wv:
        wv.setnchannels(int(wf.shape[0]))
        wv.setsampwidth(2)
        wv.setframerate(sr)
        wv.writeframes(pcm.tobytes())
    return {"sample_rate": sr, "channels": int(wf.shape[0]), "samples": int(wf.shape[1]),
            "seconds": wf.shape[1] / sr}


class StudioSaveAudio:
    """Save AUDIO as <relpath>.audio.safetensors + .wav, with a JSON sidecar."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",),
                             "relpath": ("STRING", {"default": "studio_chain/run/take_audio"})},
                "optional": {"info": ("STRING", {"forceInput": True})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "LTX25/chain"

    def save(self, audio, relpath, info=None):
        p = _resolve(relpath, ".mka")
        p.parent.mkdir(parents=True, exist_ok=True)
        side = {"audio": _write_audio(p, audio)}
        if info:
            side["join"] = json.loads(info)
        p.with_suffix(".json").write_text(json.dumps(side, indent=1), encoding="utf-8")
        _say(f"audio {p.with_suffix('.wav').relative_to(_out_root())}: {side['audio']['seconds']:.3f} s")
        return (str(p.relative_to(_out_root())).replace("\\", "/"),)


class StudioSaveVideoLossless:
    """Write frames as FFV1 (lossless RGB) in MKV, plus the audio as float32
    safetensors and a 16-bit WAV, and a JSON sidecar with the exact counts."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",),
                             "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0}),
                             "relpath": ("STRING", {"default": "studio_chain/run/link"})},
                "optional": {"audio": ("AUDIO",)}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "LTX25/chain"

    def save(self, images, fps, relpath, audio=None):
        import av
        p = _resolve(relpath, ".mkv")
        p.parent.mkdir(parents=True, exist_ok=True)
        n, h, w = int(images.shape[0]), int(images.shape[1]), int(images.shape[2])
        tmp = p.with_name(p.name + ".part.mkv")
        with av.open(str(tmp), "w") as ct:
            s = ct.add_stream("ffv1", rate=Fraction(round(float(fps) * 1000), 1000))
            s.width, s.height, s.pix_fmt = w, h, "bgr0"
            for i in range(n):
                fr = (images[i, :, :, :3].clamp(0, 1) * 255.0).round().to(torch.uint8).cpu().numpy()
                for pkt in s.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(fr), format="rgb24")):
                    ct.mux(pkt)
            for pkt in s.encode():
                ct.mux(pkt)
        os.replace(tmp, p)
        side = {"frames": n, "width": w, "height": h, "fps": float(fps), "audio": None}
        if audio is not None and audio.get("waveform") is not None:
            side["audio"] = _write_audio(p, audio)
        p.with_suffix(".json").write_text(json.dumps(side, indent=1), encoding="utf-8")
        _say(f"lossless video {p.relative_to(_out_root())}: {n} frames {w}x{h} @ {fps:g} fps"
             + (f", audio {side['audio']['seconds']:.3f} s" if side["audio"] else ""))
        return {"ui": {"text": [str(p.relative_to(_out_root()))]},
                "result": (str(p.relative_to(_out_root())).replace("\\", "/"),)}


# -------------------------------------------------------------- LTX carries
class LTX25CarryAudioLatent:
    """LTX25ZeroAudioLatent plus an optional audio carry: the previous link's audio
    over the carried video frames goes into the head of the new audio stream under
    noise mask 0 (time-aligned to the pinned pixel frames)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video_latent": ("LATENT",),
                             "frame_rate": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0}),
                             "audio_latents_per_second": ("FLOAT", {"default": 25.0, "min": 1.0, "max": 200.0}),
                             "frames": ("INT", {"default": 121, "min": 1, "max": 100000,
                                                "tooltip": "pixel frames of the video (not latent T)"})},
                "optional": {"source_audio": ("LATENT",),
                             "source_frames": ("INT", {"default": 0, "min": 0, "max": 100000}),
                             "carry_frames": ("INT", {"default": 0, "min": 0, "max": 100000})}}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("av_latent",)
    FUNCTION = "wrap"
    CATEGORY = "LTX25/chain"

    def wrap(self, video_latent, frame_rate, audio_latents_per_second, frames,
             source_audio=None, source_frames=0, carry_frames=0):
        samples = video_latent["samples"]
        if getattr(samples, "is_nested", False):
            raise ValueError("LTX25CarryAudioLatent expects a plain video latent")
        audio_t = max(1, int(round(frames / float(frame_rate) * float(audio_latents_per_second))))
        audio = torch.zeros((samples.shape[0], 8, audio_t, 16), device=samples.device, dtype=samples.dtype)
        a_mask = torch.ones((samples.shape[0], 1, audio_t, 16), dtype=torch.float32)
        info = "silent"
        if source_audio is not None and carry_frames > 0:
            src = source_audio["samples"]
            if getattr(src, "is_nested", False):
                src = src.unbind()[1]
            res = core.ltx_audio_carry(audio, src.to(audio.device), int(source_frames),
                                       int(carry_frames), frame_rate, audio_latents_per_second)
            audio, a_mask = res["audio"], res["mask"]
            info = f"carried {res['latents']} audio latents (offset {res['offset_ms']:+.2f} ms)"
        v_mask = video_latent.get("noise_mask", None)
        if v_mask is None:
            v_mask = torch.ones((samples.shape[0], 1, samples.shape[2], 1, 1), dtype=torch.float32)
        out = dict(video_latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((samples, audio))
        out["noise_mask"] = comfy.nested_tensor.NestedTensor((v_mask, a_mask))
        _say(f"LTX AV latent: video {list(samples.shape)} frames={frames} audio {list(audio.shape)} ({info})")
        return (out,)


class StudioLtxAudioTake:
    """Join the links' stage-1 audio latents into one take stream (decode it once)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"links": ("STRING", {"multiline": True, "default": "",
                                                  "tooltip": "audio latent relpaths, one per line, in order"}),
                             "frames": ("STRING", {"default": "", "tooltip": "pixel frames per link, comma list"}),
                             "carry_frames": ("INT", {"default": 25, "min": 1, "max": 10000}),
                             "frame_rate": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0}),
                             "audio_latents_per_second": ("FLOAT", {"default": 25.0, "min": 1.0, "max": 200.0})}}

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("audio_latent", "info")
    FUNCTION = "join"
    CATEGORY = "LTX25/chain"

    @staticmethod
    def _rels(links):
        return [ln.strip() for ln in str(links).splitlines() if ln.strip()]

    @classmethod
    def IS_CHANGED(cls, links, **_):
        return "|".join(_file_sig(_resolve(r, ".safetensors")) for r in cls._rels(links))

    def join(self, links, frames, carry_frames, frame_rate, audio_latents_per_second):
        rels = self._rels(links)
        counts = [int(x) for x in str(frames).replace(";", ",").split(",") if x.strip()]
        loaded = [StudioLoadLatent().load(r)[0] for r in rels]
        lat = [(d["samples"].unbind()[1] if getattr(d["samples"], "is_nested", False) else d["samples"])
               for d in loaded]
        out, info = core.ltx_audio_join(lat, counts, int(carry_frames), frame_rate, audio_latents_per_second)
        if info["latents"] != info["expected_latents"]:
            _say(f"audio take: {info['latents']} latents vs {info['expected_latents']} for "
                 f"{info['take_frames']} frames")
        bad = [j for j in info["joins"] if not j["carry_exact"]]
        if bad:
            _say(f"audio take: carried latents differ from their source at {bad}")
        _say("audio take: " + json.dumps(info))
        res = dict(loaded[0])
        res["samples"] = out
        res.pop("noise_mask", None)
        return (res, json.dumps(info))


# --------------------------------------------------------------- H3 carries
class StudioH3CarryPrefix:
    """Copy the previous H3 link's last N frames (video tokens + audio ticks) into the
    head of a new Ref2VA window under noise mask 0."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "source": ("LATENT",),
                             "carry_frames": ("INT", {"default": 39, "min": 5, "max": 1000, "step": 17})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "carry"
    CATEGORY = "LTX25/chain"

    def carry(self, latent, source, carry_frames):
        dst, src = latent["samples"], source["samples"]
        if not (getattr(dst, "is_nested", False) and getattr(src, "is_nested", False)):
            raise ValueError("StudioH3CarryPrefix needs H3 AV latents (video + audio)")
        dv, da = dst.unbind()
        sv, sa = src.unbind()
        res = core.h3_carry_prefix(dv, da, sv.to(dv.device), sa.to(da.device), int(carry_frames))
        out = dict(latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((res["video"], res["audio"]))
        out["noise_mask"] = comfy.nested_tensor.NestedTensor((res["video_mask"], res["audio_mask"]))
        _say(f"H3 carry: {carry_frames} frames = {res['tokens']} tokens + {res['ticks']} audio ticks "
             f"(audio offset {res['audio_offset_ms']:+.2f} ms) into window {list(dv.shape)}")
        return (out,)


class StudioH3ConcatTake:
    """Join saved H3 link latents into one take (later links lose their prefix)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"links": ("STRING", {"multiline": True, "default": "",
                                                  "tooltip": "one relpath per line, in order"}),
                             "carry_frames": ("INT", {"default": 39, "min": 5, "max": 1000, "step": 17})}}

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "seams")
    FUNCTION = "concat"
    CATEGORY = "LTX25/chain"

    @classmethod
    def IS_CHANGED(cls, links, carry_frames):
        return "|".join(_file_sig(_resolve(r, ".safetensors"))
                        for r in str(links).splitlines() if r.strip())

    def concat(self, links, carry_frames):
        from safetensors import safe_open
        rels = [r.strip() for r in str(links).splitlines() if r.strip()]
        if len(rels) < 1:
            raise ValueError("no links")
        parts = []
        for i, rel in enumerate(rels):
            p = _resolve(rel, ".safetensors")
            with safe_open(str(p), framework="pt") as f:
                v, a = f.get_tensor("stream_0"), f.get_tensor("stream_1")
            parts.append((v, a, 0 if i == 0 else int(carry_frames)))
        take = core.h3_concat_take(parts)
        info = {"frames": take["frames"], "seam_frames": take["seam_frames"],
                "seam_tokens": take["seam_tokens"], "links": len(rels)}
        _say(f"H3 take: {len(rels)} links -> {take['frames']} frames, video {list(take['video'].shape)}, "
             f"audio {list(take['audio'].shape)}, seams at frames {take['seam_frames']}")
        return ({"samples": comfy.nested_tensor.NestedTensor((take["video"], take["audio"]))},
                json.dumps(info))


class StudioH3CondSchedule:
    """One conditioning per chain link; MMH3SplitUpscale picks the link that owns
    each refine chunk's fresh frames."""

    @classmethod
    def INPUT_TYPES(cls):
        opt = {f"cond_{i}": ("CONDITIONING",) for i in range(1, 9)}
        return {"required": {"start_frames": ("STRING", {"default": "0",
                                                         "tooltip": "comma separated first frame of each link"})},
                "optional": opt}

    RETURN_TYPES = ("H3_COND_SCHEDULE",)
    FUNCTION = "build"
    CATEGORY = "LTX25/chain"

    def build(self, start_frames, **conds):
        starts = [int(s) for s in str(start_frames).replace(" ", "").split(",") if s != ""]
        items = [conds.get(f"cond_{i}") for i in range(1, 9)]
        items = [c for c in items if c is not None]
        if len(items) != len(starts) or not items:
            raise ValueError(f"{len(items)} conditioning(s) for {len(starts)} start frame(s)")
        if starts != sorted(starts) or starts[0] != 0:
            raise ValueError("start_frames must start at 0 and increase")
        return ({"starts": starts, "conds": items},)


# ----------------------------------------------------------------- drift guard
class StudioAdainLatent:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "reference": ("LATENT",),
                             "factor": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05}),
                             "skip_frames": ("INT", {"default": 0, "min": 0, "max": 10000,
                                                     "tooltip": "leading latent frames to leave untouched"}),
                             "ramp_frames": ("INT", {"default": 8, "min": 0, "max": 10000})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "apply"
    CATEGORY = "LTX25/chain"

    def apply(self, latent, reference, factor, skip_frames, ramp_frames):
        x = latent["samples"]
        r = reference["samples"]
        nested = getattr(x, "is_nested", False)
        xs = x.unbind() if nested else [x]
        rv = r.unbind()[0] if getattr(r, "is_nested", False) else r
        v, info = core.adain_to_reference(xs[0], rv.to(xs[0].device), factor, skip_frames, ramp_frames)
        out = dict(latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((v, *xs[1:])) if nested else v
        _say("drift guard: " + json.dumps(info))
        return (out,)


# ----------------------------------------------------------------- noise mask
class StudioPinRamp:
    """Per-latent conditioning strengths for a carried prefix.

    LTXV Img To Video Inplace pins every encoded frame at one strength. A hard
    pin (1.0) on the whole carry freezes it, and the first generated latent then
    starts the new content in one step (measured: exposure, sharpness and motion
    all jump at that frame). Lightricks' extend sampler conditions its overlap at
    0.5; a ramp keeps the oldest carried latents faithful and lets the newest
    ones bend toward the new content, so the change is spread over the overlap
    (the take assembly cross-fades the carried frames to match).
    strengths: comma list, one per carried latent from the first, e.g. "1,0.8,0.6,0.4";
    mask = 1 - strength."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "strengths": ("STRING", {"default": "1.0, 0.8, 0.6, 0.4"})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "apply"
    CATEGORY = "LTX25/chain"

    def apply(self, latent, strengths):
        s = [float(x) for x in str(strengths).replace(";", ",").split(",") if x.strip()]
        x = latent["samples"]
        if getattr(x, "is_nested", False) or x.ndim != 5:
            raise ValueError("StudioPinRamp expects a plain 5D video latent")
        if not s or len(s) > x.shape[2] or any(not 0.0 <= v <= 1.0 for v in s):
            raise ValueError(f"strengths {s} do not fit a {x.shape[2]}-latent clip (values 0..1)")
        mask = latent.get("noise_mask")
        if mask is None:
            mask = torch.ones((x.shape[0], 1, x.shape[2], 1, 1), dtype=torch.float32, device=x.device)
        mask = mask.clone()
        for i, v in enumerate(s):
            mask[:, :, i] = 1.0 - v
        out = dict(latent)
        out["noise_mask"] = mask
        _say(f"pin ramp: carried latent strengths {s}")
        return (out,)


class StudioSetNoiseMask:
    """Give a plain video latent an explicit uniform noise mask.

    LTXV Add Generated Keyframes finds free detailing slots from the latent's
    noise mask, and without one treats every non-zero latent frame as occupied.
    A finish pass starts from a real (non-zero) stage-1 video, so a link with no
    I2V pin needs "nothing occupied" spelled out."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "value": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "apply"
    CATEGORY = "LTX25/chain"

    def apply(self, latent, value):
        x = latent["samples"]
        if getattr(x, "is_nested", False) or x.ndim != 5:
            raise ValueError("StudioSetNoiseMask expects a plain 5D video latent")
        out = dict(latent)
        # [B,1,T,1,1], the shape comfy_extras.nodes_lt.get_noise_mask creates
        out["noise_mask"] = torch.full((x.shape[0], 1, x.shape[2], 1, 1), float(value),
                                       dtype=torch.float32, device=x.device)
        return (out,)


class StudioSeamAdain:
    """Seam-local look matching on a sampled video latent (studio_chain_core.seam_adain).
    window: the first `ramp` latents after the carry take the carried latents'
    channel statistics as one block, fading out linearly. path: each of them is
    matched on its own to the straight line from the last carried latent's look to
    the next latent's, which also removes a one-latent flash at the carry end.
    period 5 matches an H3 latent phase by phase (its token groups of 1,4,4,4,4
    frames). An audio-video latent is matched on its video stream only."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "carry_latents": ("INT", {"default": 4, "min": 0, "max": 1000}),
                             "ramp_latents": ("INT", {"default": 6, "min": 0, "max": 1000}),
                             "factor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05})},
                # optional, so graphs saved before the mode existed keep "window"
                "optional": {"mode": (list(core.SEAM_MODES), {"default": "window"}),
                             "period": ("INT", {"default": 1, "min": 1, "max": 16})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "apply"
    CATEGORY = "LTX25/chain"

    def apply(self, latent, carry_latents, ramp_latents, factor, mode="window", period=1):
        x = latent["samples"]
        nested = getattr(x, "is_nested", False)
        xs = x.unbind() if nested else [x]
        v, info = core.seam_adain(xs[0], carry_latents, ramp_latents, factor, mode=mode, period=period)
        out = dict(latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((v, *xs[1:])) if nested else v
        _say("seam adain: " + json.dumps(info))
        return (out,)


class StudioLtxLatentCarry:
    """Pin a new LTX link's carried head from the previous link's latents
    (studio_chain_core.ltx_latent_carry): latent 0 is encoded from the first
    carried frame alone, latents 1..carry-1 are the previous link's last sampled
    latents, exact. Same output and mask semantics as LTXV Img To Video Inplace
    (mask = 1 - strength over the carry), so StudioPinRamp can soften it."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"vae": ("VAE",),
                             "image": ("IMAGE", {"tooltip": "the carried frames; only the first is encoded"}),
                             "latent": ("LATENT",),
                             "source": ("LATENT", {"tooltip": "the previous link's saved video latent"}),
                             "carry_latents": ("INT", {"default": 4, "min": 1, "max": 1000}),
                             "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "apply"
    CATEGORY = "LTX25/chain"

    def apply(self, vae, image, latent, source, carry_latents, strength):
        import comfy.utils
        from comfy_extras.nodes_lt import get_noise_mask
        samples, src = latent["samples"], source["samples"]
        if getattr(samples, "is_nested", False) or getattr(src, "is_nested", False):
            raise ValueError("StudioLtxLatentCarry expects plain 5D video latents")
        _, hs, ws = vae.downscale_index_formula
        height, width = samples.shape[3] * hs, samples.shape[4] * ws
        px = image[:1]
        if px.shape[1] != height or px.shape[2] != width:
            px = comfy.utils.common_upscale(px.movedim(-1, 1), width, height, "bilinear", "center").movedim(1, -1)
        first = vae.encode(px[:, :, :, :3])
        out = core.ltx_latent_carry(samples, first, src, carry_latents)
        mask = get_noise_mask(latent)
        mask[:, :, :int(carry_latents)] = 1.0 - float(strength)
        _say(f"latent carry: latent 0 encoded from frame 0, latents 1..{int(carry_latents) - 1} "
             f"copied from the previous link's last {int(carry_latents) - 1}")
        return ({"samples": out, "noise_mask": mask},)


class _Ffv1Writer:
    """Append IMAGE frames ([N, H, W, C] floats 0..1) to an FFV1 stream in an open container."""

    def __init__(self, container, fps: float) -> None:
        self.ct, self.fps = container, float(fps)
        self.stream = None
        self.written = self.width = self.height = 0

    def put(self, frames: torch.Tensor) -> None:
        import av
        for k in range(frames.shape[0]):
            fr = (frames[k, :, :, :3].clamp(0, 1) * 255.0).round().to(torch.uint8).cpu().numpy()
            if self.stream is None:
                self.height, self.width = int(fr.shape[0]), int(fr.shape[1])
                self.stream = self.ct.add_stream("ffv1", rate=Fraction(round(self.fps * 1000), 1000))
                self.stream.width, self.stream.height, self.stream.pix_fmt = self.width, self.height, "bgr0"
            for pkt in self.stream.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(fr), format="rgb24")):
                self.ct.mux(pkt)
            self.written += 1

    def close(self) -> None:
        if self.stream is not None:
            for pkt in self.stream.encode():
                self.ct.mux(pkt)


class StudioLtxDecodeTake:
    """Decode an LTX chain as one take (studio_chain_core.ltx_take_latents) and
    write it as FFV1, streaming: the joined latent is decoded in overlapping
    windows (studio_chain_core.decode_windows) with the same tiling as VAE Decode
    Tiled, so a 1080p take never sits in memory whole. Optional audio (the take's
    one-piece decode) is written next to it like StudioSaveVideoLossless."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"vae": ("VAE",),
                             "links": ("STRING", {"multiline": True, "default": "",
                                                  "tooltip": "one saved video latent relpath per line, in order"}),
                             "carry_latents": ("INT", {"default": 4, "min": 1, "max": 1000}),
                             "join_latent": ("INT", {"default": 1, "min": 1, "max": 1000}),
                             "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0}),
                             "relpath": ("STRING", {"default": "studio_chain/run/take"}),
                             "tile_size": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 32}),
                             "overlap": ("INT", {"default": 64, "min": 0, "max": 4096, "step": 32}),
                             "temporal_size": ("INT", {"default": 64, "min": 8, "max": 4096, "step": 4}),
                             "temporal_overlap": ("INT", {"default": 16, "min": 4, "max": 4096, "step": 4}),
                             "window_latents": ("INT", {"default": 24, "min": 6, "max": 1000}),
                             "context_latents": ("INT", {"default": 2, "min": 1, "max": 100}),
                             "overlap_latents": ("INT", {"default": 2, "min": 1, "max": 100})},
                "optional": {"audio": ("AUDIO",),
                             "blend_latents": ("INT", {"default": 0, "min": 0, "max": 1000,
                                                       "tooltip": "carried latents before the join crossfaded "
                                                                  "between the links (0 = hard switch)"})}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("path", "info")
    FUNCTION = "decode"
    OUTPUT_NODE = True
    CATEGORY = "LTX25/chain"

    @classmethod
    def IS_CHANGED(cls, links, **_):
        return "|".join(_file_sig(_resolve(r, ".safetensors")) for r in str(links).splitlines() if r.strip())

    def decode(self, vae, links, carry_latents, join_latent, fps, relpath, tile_size, overlap, temporal_size,
               temporal_overlap, window_latents, context_latents, overlap_latents, audio=None, blend_latents=0):
        import av
        import nodes as comfy_nodes
        rels = [r.strip() for r in str(links).splitlines() if r.strip()]
        lats = [StudioLoadLatent().load(r)[0]["samples"] for r in rels]
        lats = [x.unbind()[0] if getattr(x, "is_nested", False) else x for x in lats]
        take, info = core.ltx_take_latents(lats, carry_latents, join_latent, blend_latents)
        plan = core.decode_windows(take.shape[2], window_latents, context_latents, overlap_latents)
        info["windows"] = plan
        p = _resolve(relpath, ".mkv")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part.mkv")
        dec = comfy_nodes.VAEDecodeTiled()
        held = None
        with av.open(str(tmp), "w") as ct:
            out = _Ffv1Writer(ct, fps)
            put = out.put
            for k, win in enumerate(plan):
                imgs = dec.decode(vae, {"samples": take[:, :, win["a"]:win["b"]]}, tile_size, overlap,
                                  temporal_size, temporal_overlap)[0]
                base = 8 * win["a"]
                seg = imgs[win["use"][0] - base:win["use"][1] - base].float()
                if seg.shape[0] != win["use"][1] - win["use"][0]:
                    raise RuntimeError(f"window {k} decoded {imgs.shape[0]} frames, expected frames "
                                       f"{win['use']} from offset {base}")
                nxt = plan[k + 1] if k + 1 < len(plan) else None
                hold = win["use"][1] - nxt["use"][0] if nxt else 0
                if held is not None:
                    n = held.shape[0]
                    wts = (torch.arange(1, n + 1, dtype=torch.float32) / float(n + 1)).view(-1, 1, 1, 1)
                    put(held * (1.0 - wts) + seg[:n] * wts)
                    seg = seg[n:]
                    hold = min(hold, seg.shape[0])
                put(seg[:seg.shape[0] - hold])
                held = seg[seg.shape[0] - hold:].clone() if hold else None
                _say(f"take decode: window {k + 1}/{len(plan)} latents [{win['a']}, {win['b']}) "
                     f"-> frames {win['use']}, {out.written} written")
            out.close()
        written, w, h = out.written, out.width, out.height
        if written != info["frames"]:
            raise RuntimeError(f"take decode wrote {written} frames, the joined latent has {info['frames']}")
        os.replace(tmp, p)
        side = {"frames": written, "width": w, "height": h, "fps": float(fps), "audio": None, "take": info}
        if audio is not None and audio.get("waveform") is not None:
            side["audio"] = _write_audio(p, audio)
        p.with_suffix(".json").write_text(json.dumps(side, indent=1), encoding="utf-8")
        _say(f"take {p.relative_to(_out_root())}: {written} frames {w}x{h}, joins "
             + json.dumps([{k: j[k] for k in ("take_frame", "step_ratio")} for j in info["joins"]]))
        rel = str(p.relative_to(_out_root())).replace("\\", "/")
        return {"ui": {"text": [rel]}, "result": (rel, json.dumps(info))}


class StudioCarryRestore:
    """Remove the soft-pin gain from a sampled latent's carried prefix
    (studio_chain_core.carry_restore). reference: the I2V-pinned latent the sampler
    started from; its first `carry_latents` frames are the encoded carry."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",),
                             "reference": ("LATENT",),
                             "carry_latents": ("INT", {"default": 4, "min": 0, "max": 1000}),
                             "factor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05})}}

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "info")
    FUNCTION = "apply"
    CATEGORY = "LTX25/chain"

    def apply(self, latent, reference, carry_latents, factor):
        x, r = latent["samples"], reference["samples"]
        nested = getattr(x, "is_nested", False)
        xs = x.unbind() if nested else [x]
        rv = r.unbind()[0] if getattr(r, "is_nested", False) else r
        v, info = core.carry_restore(xs[0], rv, carry_latents, factor)
        out = dict(latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((v, *xs[1:])) if nested else v
        text = json.dumps(info)
        _say("carry restore: " + text)
        return (out, text)


NODE_CLASS_MAPPINGS = {
    "StudioLtxLatentCarry": StudioLtxLatentCarry,
    "StudioLtxDecodeTake": StudioLtxDecodeTake,
    "StudioCarryRestore": StudioCarryRestore,
    "StudioSeamAdain": StudioSeamAdain,
    "StudioPinRamp": StudioPinRamp,
    "StudioSetNoiseMask": StudioSetNoiseMask,
    "StudioSaveLatent": StudioSaveLatent,
    "StudioLoadLatent": StudioLoadLatent,
    "StudioSaveConditioning": StudioSaveConditioning,
    "StudioLoadConditioning": StudioLoadConditioning,
    "StudioSaveImages": StudioSaveImages,
    "StudioLoadImages": StudioLoadImages,
    "StudioSaveVideoLossless": StudioSaveVideoLossless,
    "StudioSaveAudio": StudioSaveAudio,
    "LTX25CarryAudioLatent": LTX25CarryAudioLatent,
    "StudioLtxAudioTake": StudioLtxAudioTake,
    "StudioH3CarryPrefix": StudioH3CarryPrefix,
    "StudioH3ConcatTake": StudioH3ConcatTake,
    "StudioH3CondSchedule": StudioH3CondSchedule,
    "StudioAdainLatent": StudioAdainLatent,
}
NODE_DISPLAY_NAME_MAPPINGS = {k: "Studio " + k.removeprefix("Studio").removeprefix("LTX25")
                              for k in NODE_CLASS_MAPPINGS}
