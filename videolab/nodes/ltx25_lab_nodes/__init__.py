"""Lab-only helper: wrap a plain video latent as a joint AV latent with a zero audio stream.

Why this exists
---------------
LTX-2.5 is a joint audio-video architecture. Every shipped graph builds the sampler input with
LTXVConcatAVLatent, which needs an audio latent from LTXVEmptyLatentAudio, which needs the
audio VAE. Two facts measured on this machine:

1. A *zero-length* audio stream does not work. The model returns video-only output, so
   comfy/model_base.py:254 (`len(model_output) > 1`) skips packing and
   `model_sampling.calculate_denoised` then compares a 5-D video output against the packed
   3-D latent -> RuntimeError. Measured: "size of tensor a (40960) must match tensor b (8)
   at non-singleton dimension 4".
2. LTXVEmptyLatentAudio fills the audio latent with plain zeros; only its length matters,
   and that length comes from constants (sampling_rate / mel_hop_length / LATENT_DOWNSAMPLE_FACTOR)
   that ComfyUI defaults to 16000 / 160 / 4 in comfy/ldm/lightricks/vae/causal_audio_autoencoder.py:849.

So this node produces the same zeros tensor LTXVEmptyLatentAudio would, without loading the
audio VAE. Audio is never decoded or saved; only the video stream is measured.

First-party lab node, shipped in videolab/nodes/ltx25_lab_nodes.
"""
import torch
import comfy.nested_tensor

AUDIO_CHANNELS = 8       # LTXAVModel.num_audio_channels; audio VAE z_channels = 8
AUDIO_FREQ_BINS = 16     # LTXAVModel.audio_frequency_bins; mel_bins 64 // LATENT_DOWNSAMPLE_FACTOR 4
VIDEO_TEMPORAL_COMPRESSION = 8  # vae_scale_factors[0]; frame rule N % 8 == 1
DEFAULT_FPS = 24.0
DEFAULT_LATENTS_PER_SECOND = 25.0  # 16000 / 160 / 4


class LTX25LabEmptyAVLatent:
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
    CATEGORY = "LTX25_LAB"

    def wrap(self, video_latent, frame_rate=DEFAULT_FPS, audio_latents_per_second=DEFAULT_LATENTS_PER_SECOND,
             frames_override=0):
        samples = video_latent["samples"]
        if getattr(samples, "is_nested", False):
            return (video_latent,)
        if frames_override and frames_override > 0:
            frames = int(frames_override)
            temporal_latents = None
        elif samples.dim() == 5:
            temporal_latents = samples.shape[2]
            frames = 0
        elif samples.dim() == 4:
            temporal_latents = samples.shape[1]
            frames = 0
        else:
            raise ValueError(f"unexpected video latent shape {tuple(samples.shape)}")
        if not frames:
            frames = VIDEO_TEMPORAL_COMPRESSION * (temporal_latents - 1) + 1
        audio_t = max(1, int(round((frames / float(frame_rate)) * float(audio_latents_per_second))))
        audio = torch.zeros(
            (samples.shape[0], AUDIO_CHANNELS, audio_t, AUDIO_FREQ_BINS),
            device=samples.device, dtype=samples.dtype,
        )
        print(f"[LTX25 lab] video latent {tuple(samples.shape)} -> frames={frames}, "
              f"zero audio latent {tuple(audio.shape)}", flush=True)
        out = dict(video_latent)
        out["samples"] = comfy.nested_tensor.NestedTensor((samples, audio))
        noise_mask = video_latent.get("noise_mask", None)
        if noise_mask is not None:
            out["noise_mask"] = comfy.nested_tensor.NestedTensor((noise_mask, torch.ones_like(audio)))
        return (out,)


from .evict import LTX25LabEvictModels
from .frames_to_latent import LTX25LabFramesToLatent
from .preflight import LTX25LabPreflight
from .query_gate import LTX25LabQueryGate

NODE_CLASS_MAPPINGS = {
    "LTX25LabEmptyAVLatent": LTX25LabEmptyAVLatent,
    "LTX25LabEvictModels": LTX25LabEvictModels,
    "LTX25LabFramesToLatent": LTX25LabFramesToLatent,
    "LTX25LabPreflight": LTX25LabPreflight,
    "LTX25LabQueryGate": LTX25LabQueryGate,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LTX25LabEmptyAVLatent": "LTX25 Lab Empty AV Latent",
    "LTX25LabEvictModels": "LTX25 Lab Evict Models",
    "LTX25LabFramesToLatent": "LTX25 Lab Frames To Latent",
    "LTX25LabPreflight": "LTX25 Lab Preflight Sink",
    "LTX25LabQueryGate": "LTX25 Lab Query Gate",
}
