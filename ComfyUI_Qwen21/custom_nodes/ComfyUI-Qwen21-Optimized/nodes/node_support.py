# node_support.py — pipeline support nodes for Qwen-Image-2.1.
#
# EmptyQwen21LatentImage : latent geometry initializer — 64 latent channels,
#                          16x spatial compression (1024x1024 -> 64x64x64, S=4096).
# PromptEnhancerHostPE   : prompt expansion with the PE-T2I INT8 checkpoint
#                          (models/prompt_generator/). Its weights are cached in
#                          host RAM, but the forward pass runs on the GPU (see the
#                          class docstring). With fallback_passthrough on, missing
#                          weights or transformers degrade to a logged pass-through,
#                          never to a silent failure.

import logging
import time

import torch

logger = logging.getLogger("qwen21")

from ..qwen21_common import LATENT_CHANNELS, METRICS, SPATIAL_COMPRESSION

import comfy.model_management

# Residency cache: the 9.47 GB PE-T2I CLIP stays pinned in host DDR4
# between runs instead of being re-read from NVMe on every workflow execution.
_PE_CLIP_CACHE = {}


class EmptyQwen21LatentImage:
    """64-channel RGBA latent initializer with 16x spatial compression."""

    CATEGORY = "qwen21/latent"
    FUNCTION = "generate"
    RETURN_TYPES = ("LATENT",)
    DESCRIPTION = (
        "Empty latent matching the Qwen-Image-2.1 VAE geometry: 64 channels, 16x "
        "spatial compression (1024x1024 -> 64x64x64, S = 4,096 tokens)."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "width": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 32}),
                "height": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 32}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 16}),
            }
        }

    def generate(self, width, height, batch_size):
        if width % SPATIAL_COMPRESSION or height % SPATIAL_COMPRESSION:
            raise ValueError(
                f"canvas {width}x{height} is not a multiple of the {SPATIAL_COMPRESSION}x "
                f"latent compression; snap dimensions to multiples of 32.")
        latent = torch.zeros(
            [batch_size, LATENT_CHANNELS, height // SPATIAL_COMPRESSION,
             width // SPATIAL_COMPRESSION],
            device=comfy.model_management.intermediate_device(),
            dtype=torch.float32,
        )
        METRICS.emit(node="EmptyQwen21LatentImage", event="init",
                     width=width, height=height, batch=batch_size,
                     latent_shape=list(latent.shape),
                     tokens=(height // SPATIAL_COMPRESSION) * (width // SPATIAL_COMPRESSION))
        return ({"samples": latent},)


class SplitImageRGBA:
    """Separate an RGBA (or RGB) IMAGE into RGB and single-channel alpha planes.

    Required because ESRGAN archives such as 4x-UltraSharp accept exactly three
    channels: the upscale step routes RGB through the model while
    alpha is resized geometrically, then MergeImageRGBA recombines.
    """

    CATEGORY = "qwen21/image"
    FUNCTION = "split"
    RETURN_TYPES = ("IMAGE", "IMAGE")
    RETURN_NAMES = ("rgb", "alpha")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",)}}

    def split(self, image):
        channels = image.shape[-1]
        if channels >= 4:
            rgb, alpha = image[..., :3], image[..., 3:4]
        elif channels == 3:
            rgb = image
            alpha = torch.ones(image.shape[:-1] + (1,), dtype=image.dtype,
                               device=image.device)
        else:
            raise ValueError(f"expected 3- or 4-channel IMAGE, got {channels} channels")
        return (rgb.contiguous(), alpha.contiguous())


class MergeImageRGBA:
    """Recombine an RGB IMAGE and an alpha plane ([B,H,W,1] IMAGE or [B,H,W] mask)."""

    CATEGORY = "qwen21/image"
    FUNCTION = "merge"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("rgba",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "rgb": ("IMAGE",),
                "alpha": ("IMAGE",),
            }
        }

    def merge(self, rgb, alpha):
        if alpha.dim() == 3:                      # squeezed grayscale [B,H,W]
            alpha = alpha.unsqueeze(-1)
        # Defensive: core common_upscale(lanczos) squeezes single-channel input
        # and the following movedim can hand back a transposed mask at
        # non-square sizes; restore [B,H,W,1] orientation to match rgb.
        if alpha.shape[:3] != rgb.shape[:3] and \
                alpha.shape[1:3] == rgb.shape[2:4] and \
                alpha.shape[2:4] == rgb.shape[1:3]:
            alpha = alpha.transpose(1, 2)
        if alpha.shape[-1] != 1:
            raise ValueError(f"alpha must be single-channel, got {alpha.shape[-1]}")
        if alpha.shape[:3] != rgb.shape[:3]:
            raise ValueError(f"rgb {tuple(rgb.shape)} and alpha {tuple(alpha.shape)} "
                             f"spatial layouts differ; resize alpha first.")
        rgba = torch.cat([rgb[..., :3], alpha.clamp(0.0, 1.0).to(rgb.dtype)], dim=-1)
        return (rgba.contiguous(),)


class PromptEnhancerHostPE:
    """PE-T2I prompt expansion (MTP-aware budget).

    Behaviour:
      * With `models/prompt_generator/PE-T2I-Qwen3.5-VL-9B-INT8.safetensors`
        provisioned, the ConvRot INT8 checkpoint runs through ComfyUI's native
        CLIP machinery (comfy.sd.load_clip with CLIPType.QWEN_IMAGE) and a
        greedy generation pass expands the terse prompt into a full
        photographic description. Measured: despite "Host" in the name, the
        ConvRot dequant kernels require CUDA compute, so this auxiliary model
        pages through VRAM instead of executing on the host CPU; its weights
        stay cached in host DDR4 between runs.
      * With the checkpoint absent, the input prompt is returned unchanged with
        a loud warning when `fallback_passthrough` is enabled (observable
        degradation in the metrics JSONL); with it disabled, missing
        prerequisites raise.
    """

    CATEGORY = "qwen21/conditioning"
    FUNCTION = "enhance"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("expanded_prompt",)
    DESCRIPTION = (
        "Prompt expansion with a PE-T2I INT8 checkpoint (weights cached in host RAM; "
        "its int8 kernels need CUDA, so it runs through VRAM paging). The expanded "
        "prompt ends with explicit exclusions, for graphs at CFG 1.0, where a "
        "negative prompt has no effect."
    )

    @classmethod
    def INPUT_TYPES(cls):
        try:
            import folder_paths
            names = folder_paths.get_filename_list("prompt_generator")
        except Exception:
            names = []
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "model_name": (names or ["PE-T2I-Qwen3.5-VL-9B-INT8.safetensors"],),
                "max_new_tokens": ("INT", {"default": 256, "min": 64, "max": 1024, "step": 32}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff,
                                 "control_after_generate": True}),
                "fallback_passthrough": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Return the input prompt unchanged (with a warning) when "
                               "the enhancer checkpoint or transformers is unavailable."}),
            }
        }

    def enhance(self, prompt, model_name, max_new_tokens, seed, fallback_passthrough):
        expanded, enhanced = self._expand(prompt, model_name, max_new_tokens, seed,
                                          fallback_passthrough)
        METRICS.emit(node="PromptEnhancerHostPE", event="enhance",
                     enhanced=enhanced, model=model_name, out_chars=len(expanded))
        return (expanded,)

    def _expand(self, prompt, model_name, max_new_tokens, seed, fallback_passthrough):
        import folder_paths

        path = folder_paths.get_full_path("prompt_generator", model_name)
        if path is None:
            return self._degrade(f"checkpoint '{model_name}' not provisioned",
                                 prompt, fallback_passthrough)
        try:
            import comfy.sd
            from comfy.sd import CLIPType

            # ConvRot INT8 checkpoints are ComfyUI-native quantizations: they must
            # go through comfy.sd.load_clip (transformers cannot read them), and
            # the rotated-int8 dequant kernels require CUDA compute. Measured:
            # this auxiliary model cannot execute on the host CPU in this
            # runtime; it runs through ComfyUI's dynamic VRAM paging with
            # weights resident in host DDR4 between runs (the same memory
            # regime as the DiT).
            # The Qwen3-VL *conditioning encoder* remains pinned to CPU (it is,
            # verified by the encoder-residency metric).
            clip = _PE_CLIP_CACHE.get(path)
            if clip is None:
                embed_dirs = folder_paths.get_folder_paths("embeddings")
                clip = comfy.sd.load_clip(
                    ckpt_paths=[path],
                    embedding_directory=embed_dirs[0] if embed_dirs else None,
                    clip_type=CLIPType.QWEN_IMAGE)
                _PE_CLIP_CACHE[path] = clip   # host-RAM residency between runs

            t0 = time.perf_counter()
            instruction = (
                "Expand the following idea into one single-paragraph photographic "
                "scene description covering subject and pose, environment, lighting "
                "geometry and camera optics, ending with explicit exclusions. "
                "Natural prose only, no tags: ")
            torch.manual_seed(seed)
            tokens = clip.tokenize(instruction + prompt)
            generated = clip.generate(tokens, do_sample=False,
                                      max_length=max_new_tokens,
                                      repetition_penalty=1.05, seed=seed)
            expanded = clip.decode(generated).strip()
            elapsed = time.perf_counter() - t0
            # runs on CUDA through dynamic VRAM paging (see above), not the CPU
            logger.info("[qwen21] prompt enhanced in %.2f s (%d chars, GPU-paged).",
                        elapsed, len(expanded))
            return (expanded or prompt, bool(expanded))
        except Exception as exc:
            return self._degrade(f"enhancer forward failed: {exc}",
                                 prompt, fallback_passthrough)

    @staticmethod
    def _degrade(reason, prompt, fallback_passthrough):
        if not fallback_passthrough:
            raise RuntimeError(f"PromptEnhancerHostPE: {reason} and fallback_passthrough "
                               f"is disabled (strict production posture).")
        logger.warning("[qwen21] PromptEnhancerHostPE degraded to pass-through: %s", reason)
        return prompt, False
