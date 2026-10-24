"""ComfyUI-LTX25-Studio — production helper nodes for the LTX-2.5 two-stage video pipeline.

Both nodes were developed and measured in the isolated LTX-2.5 laboratory (the videolab
project) on an RTX 3070 8 GB (SM86) and are ported here unchanged in behaviour. They exist
because two architectural facts make them necessary:

1. LTX-2.5 is a joint audio-video model. Every shipped ComfyUI graph builds the sampler input
   with LTXVConcatAVLatent, which requires an audio latent from LTXVEmptyLatentAudio, which
   requires the audio VAE. LTX25ZeroAudioLatent produces the same joint latent with a correctly
   sized zero audio stream, for graphs that run video-only without the audio VAE (the studio's
   ltx25_msr_dfr_s6_cq_chain graph; its other ltx25_*.json graphs load the audio VAE and use
   LTXVEmptyLatentAudio). A *zero-length* audio stream does NOT work: the model then returns
   video-only output, comfy/model_base.py:254 skips packing and calculate_denoised compares a
   5-D output against the packed 3-D latent. Measured failure:
   "size of tensor a (40960) must match tensor b (8) at non-singleton dimension 4".

2. On Windows, constructing the LTX diffusion decoder while the DiT is resident raises
   "Windows fatal exception: access violation" inside na_diffusion_decoder.py (ComfyUI issue
   #15606, reproduced upstream on a 32 GB card). Explicit eviction between the sampler and the
   decoder is the documented workaround. LTX25EvictModels performs it as a passthrough.
   With this guard, 30+ consecutive generations completed without a crash on the lab machine.

No external dependencies and no network access. The chain-continuation nodes
(studio_chain_nodes.py) read and write files, but only under ComfyUI's output directory.
"""
import gc

import torch
import comfy.nested_tensor

# Constants taken from the LTX-2.5 audio VAE defaults
# (comfy/ldm/lightricks/vae/causal_audio_autoencoder.py: sampling_rate 16000, mel_hop_length 160,
#  mel_bins 64, LATENT_DOWNSAMPLE_FACTOR 4 -> 25 latents/second, 16 frequency bins).
# These match LTXAVModel.num_audio_channels (8) and .audio_frequency_bins (16).
AUDIO_CHANNELS = 8
AUDIO_FREQ_BINS = 16
DEFAULT_FPS = 24.0
DEFAULT_LATENTS_PER_SECOND = 25.0
VIDEO_TEMPORAL_COMPRESSION = 8  # vae_scale_factors[0]; LTX frame rule is N = 1 + 8k


class LTX25ZeroAudioLatent:
    """Wrap a plain video latent as a joint AV latent with a silent audio stream of correct length."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_latent": ("LATENT",),
                "frame_rate": ("FLOAT", {"default": DEFAULT_FPS, "min": 1.0, "max": 240.0, "step": 1.0}),
                "audio_latents_per_second": ("FLOAT", {"default": DEFAULT_LATENTS_PER_SECOND,
                                                       "min": 1.0, "max": 200.0, "step": 1.0}),
                "frames_override": ("INT", {"default": 0, "min": 0, "max": 100000,
                                            "tooltip": "0 = derive from latent T. Set explicitly when "
                                                       "reference tokens extend T (e.g. MSR guides)."}),
            }
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("av_latent",)
    FUNCTION = "wrap"
    CATEGORY = "LTX25"

    def wrap(self, video_latent, frame_rate=DEFAULT_FPS, audio_latents_per_second=DEFAULT_LATENTS_PER_SECOND,
             frames_override=0):
        samples = video_latent["samples"]
        if getattr(samples, "is_nested", False):
            return (video_latent,)

        temporal_latents = None
        frames = int(frames_override) if frames_override and frames_override > 0 else 0
        if not frames:
            if samples.dim() == 5:
                temporal_latents = samples.shape[2]
            elif samples.dim() == 4:
                temporal_latents = samples.shape[1]
            else:
                raise ValueError(f"LTX25ZeroAudioLatent: unexpected video latent shape {tuple(samples.shape)}")
            frames = VIDEO_TEMPORAL_COMPRESSION * (temporal_latents - 1) + 1

        audio_t = max(1, int(round((frames / float(frame_rate)) * float(audio_latents_per_second))))
        audio = torch.zeros(
            (samples.shape[0], AUDIO_CHANNELS, audio_t, AUDIO_FREQ_BINS),
            device=samples.device, dtype=samples.dtype,
        )
        out = dict(video_latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((samples, audio))
        noise_mask = video_latent.get("noise_mask", None)
        if noise_mask is not None:
            out["noise_mask"] = comfy.nested_tensor.NestedTensor((noise_mask, torch.ones_like(audio)))
        print(f"[LTX25] zero-audio AV latent: video {tuple(samples.shape)} frames={frames} "
              f"audio {tuple(audio.shape)}", flush=True)
        return (out,)


def _model_name(lm) -> str:
    try:
        return type(lm.model.model).__name__
    except Exception:
        return ""


class LTX25EvictModels:
    """Free GPU model memory as a passthrough, for use between the last sampler and the decoder.

    With keep_text_encoder=True the text encoder is excluded from the eviction.
    Measured on this machine (LTX25_LAB, 2026-09-22, one session, 73 frames 960x544):
    keeping the 10.6 GB text encoder staged made a different-prompt shot 26.2 s
    faster from a cold start (150.8 -> 124.6 s) and 1.9 s faster warm (72.4 -> 70.5 s),
    with bit-identical output statistics (150.8 -> 70.5 s is the whole cold-to-warm
    difference, not the flag alone).

    With text_encoder_only=True it does the opposite: only the text encoder is
    unloaded (its VRAM and RAM staging), the DiT and VAEs stay staged. That is the
    release after the last prompt encode: it frees the encoder's RAM for the
    sampler without throwing away a DiT the previous link left warm (a full
    eviction there re-read the 11 GB H3 DiT from disk on every link, ~80 s).
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",),
                             "enabled": ("BOOLEAN", {"default": True})},
                "optional": {"keep_text_encoder": ("BOOLEAN", {"default": False,
                                "tooltip": "unload everything except the text encoder (keeps its RAM staging; "
                                           "a new prompt then costs the encode, not the re-stage)"}),
                             "text_encoder_only": ("BOOLEAN", {"default": False,
                                "tooltip": "unload only the text encoder; the DiT and VAEs stay staged"})}}

    RETURN_TYPES = ("*",)
    RETURN_NAMES = ("value",)
    FUNCTION = "evict"
    CATEGORY = "LTX25"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # always execute; this node exists for its side effect

    def evict(self, value, enabled=True, keep_text_encoder=False, text_encoder_only=False):
        if not enabled:
            return (value,)
        import comfy.model_management as mm
        from .studio_chain_core import eviction_keep
        before = torch.cuda.memory_allocated() if torch.cuda.is_available() else 0
        loaded = list(mm.current_loaded_models)
        flags = eviction_keep([_model_name(lm) for lm in loaded], keep_text_encoder, text_encoder_only)
        if flags is not None:
            # free_memory unloads every loaded model not in keep_loaded
            keep = [lm for lm, k in zip(loaded, flags) if k]
            for device in mm.get_all_torch_devices():
                mm.free_memory(1e30, device, keep_loaded=keep)
            kept_names = [_model_name(lm) for lm in keep]
        else:
            mm.unload_all_models()
            kept_names = []
        what = "text encoder release" if text_encoder_only else "eviction before decoder"
        mm.soft_empty_cache()
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        after = torch.cuda.memory_allocated() if torch.cuda.is_available() else 0
        kept = f", kept {kept_names}" if kept_names else ""
        print(f"[LTX25] {what}: allocated {before/2**30:.2f} -> {after/2**30:.2f} GiB, "
              f"reserved {torch.cuda.memory_reserved()/2**30:.2f} GiB{kept}", flush=True)
        return (value,)


NODE_CLASS_MAPPINGS = {
    "LTX25ZeroAudioLatent": LTX25ZeroAudioLatent,
    "LTX25EvictModels": LTX25EvictModels,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LTX25ZeroAudioLatent": "LTX25 Zero Audio Latent",
    "LTX25EvictModels": "LTX25 Evict Models",
}

# Video Studio chain continuations (lossless state between links, latent carries,
# the H3 take join and the drift guard) — see studio_chain_nodes.py.
from .studio_chain_nodes import NODE_CLASS_MAPPINGS as _CHAIN_NODES  # noqa: E402
from .studio_chain_nodes import NODE_DISPLAY_NAME_MAPPINGS as _CHAIN_NAMES  # noqa: E402

NODE_CLASS_MAPPINGS.update(_CHAIN_NODES)
NODE_DISPLAY_NAME_MAPPINGS.update(_CHAIN_NAMES)
