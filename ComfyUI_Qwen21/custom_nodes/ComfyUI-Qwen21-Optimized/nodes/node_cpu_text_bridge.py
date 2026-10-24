# node_cpu_text_bridge.py — TextEncodeQwenImage21CPU
#
# Text-encode stage of the pipeline:
#   * Qwen3-VL INT8 stays pinned in host DDR4 (load it via CLIPLoader with
#     device="cpu"); this node executes the conditioning forward pass on the
#     i9-10980XE's AVX-512 VNNI cores, so no encoder weights need to sit in
#     VRAM (the node counts any that do and logs a warning).
#   * The resulting ~4.2 MB conditioning tensor is pushed to the GPU over PCIe
#     with pinned-memory DMA on a dedicated non-blocking CUDA stream. A recorded
#     cudaEvent fences the compute stream, so the DiT forward cannot begin
#     before the transfer retires while the host thread proceeds immediately.
#     Transfer time is measured with CUDA events and logged to the metrics
#     JSONL (budget: <= 1 ms).
#   * Multi-reference inputs are capped at 4 images, each resized to about
#     512x512 pixels of area (aspect kept, so small images are scaled up too).
#     A warning is logged when the encoded sequence exceeds token_budget
#     (4,096 by default); the sequence is never cut.

import logging
import time

import torch

logger = logging.getLogger("qwen21")

from ..qwen21_common import METRICS, free_vram_mb

MAX_REFERENCES = 4
REFERENCE_MAX_PX = 512
CONDITIONING_BUDGET_MB = 32.0     # sanity ceiling; expected ~4.2 MB


def _move_conditioning_to_device(conditioning, device, moved=None):
    """Recursively relocate every tensor in a conditioning structure.

    Conditioning is a list of [tensor, options_dict] entries; options may nest
    tensors (pooled output, masks, ...). Host tensors go through pinned staging
    and are copied non-blocking on the CURRENT stream (the caller enters the
    DMA stream). Every new device tensor is appended to ``moved`` so the caller
    can hand it over to the compute stream.
    """
    def _to_device(obj):
        if torch.is_tensor(obj):
            if obj.device == device:
                return obj
            src = obj.detach()
            if src.device.type == "cpu":
                src = src.pin_memory()        # already a copy: no extra clone
            out_t = src.to(device, non_blocking=True)
            if moved is not None:
                moved.append(out_t)
            return out_t
        if isinstance(obj, dict):
            return {k: _to_device(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            t = [_to_device(v) for v in obj]
            return type(obj)(t) if isinstance(obj, tuple) else t
        return obj

    out = []
    for entry in conditioning:
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
            cond = _to_device(entry[0])
            kwargs = [_to_device(v) for v in entry[1:]]
            out.append([cond] + kwargs)
        else:
            out.append(_to_device(entry))
    return out


def _encode_positive(clip, prompt, vae, refs, resolution):
    """The positive half of core TextEncodeQwenImage21.execute
    (comfy_extras/nodes_qwen.py, v0.37): the same reference resize for the
    vision tower and the VAE, the same token flags. The core node also encodes
    a negative prompt together with every reference. Here each instance
    encodes one prompt: a graph that needs a negative (CFG above 1.0) adds a
    second instance, and a CFG 1.0 graph skips that wasted CPU encode."""
    import math

    import comfy.utils
    import node_helpers

    images_vl, ref_latents = [], []
    for image in refs:
        samples = image[:1].movedim(-1, 1)
        ratio = samples.shape[3] / samples.shape[2]
        width = max(32, round(math.sqrt(resolution * resolution * ratio) / 32) * 32)
        height = max(32, round(math.sqrt(resolution * resolution / ratio) / 32) * 32)
        if (width, height) == (samples.shape[3], samples.shape[2]):
            s = image[:1]
        else:
            s = comfy.utils.common_upscale(samples, width, height, "lanczos",
                                           "disabled").movedim(1, -1)
        rgb = s[:, :, :, :3]
        if s.shape[-1] > 3:
            # the vision tower sees alpha over white, the VAE keeps all four
            rgb = rgb * s[:, :, :, 3:] + (1.0 - s[:, :, :, 3:])
        images_vl.append(rgb)
        if vae is not None:
            ref_latents.append(vae.encode(s))

    tokens = clip.tokenize(prompt, images=images_vl,
                           keep_vision=len(ref_latents) == 0,
                           prevent_empty_text=True)
    positive = clip.encode_from_tokens_scheduled(tokens)
    if ref_latents:
        positive = node_helpers.conditioning_set_values(
            positive, {"reference_latents": ref_latents}, append=True)
    return positive


def _sequence_tokens(conditioning):
    """Longest token sequence in a conditioning list ([1, seq, dim] tensors)."""
    n = 0
    for entry in conditioning:
        t = entry[0] if isinstance(entry, (list, tuple)) and entry else entry
        if torch.is_tensor(t) and t.dim() >= 2:
            n = max(n, int(t.shape[1]))
    return n


def _count_conditioning_bytes(conditioning):
    total = 0
    for entry in conditioning:
        stack = [entry]
        while stack:
            obj = stack.pop()
            if torch.is_tensor(obj):
                total += obj.numel() * obj.element_size()
            elif isinstance(obj, dict):
                stack.extend(obj.values())
            elif isinstance(obj, (list, tuple)):
                stack.extend(obj)
    return total


class TextEncodeQwenImage21CPU:
    """Host-CPU Qwen3-VL conditioning with pinned-memory asynchronous GPU push."""

    CATEGORY = "qwen21/conditioning"
    FUNCTION = "encode"
    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("positive",)
    DESCRIPTION = (
        "Encodes the photographic prompt on the host CPU (Qwen3-VL pinned in DDR4, "
        "AVX-512 VNNI — 0 MB VRAM), then DMA-pushes the conditioning tensor to the "
        "GPU via pinned memory + non-blocking CUDA stream with event fencing. "
        "Accepts up to 4 reference images, each resized to about 512x512 px of area."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "prompt": ("STRING", {
                    "multiline": True, "dynamic_prompts": True,
                    "default": "",
                    "tooltip": "Descriptive photographic prompt. Each node encodes "
                               "one prompt; for a negative, use a second instance. "
                               "At CFG 1.0 a negative has no effect, so put exclusions here."}),
                "token_budget": ("INT", {
                    "default": 4096, "min": 512, "max": 16384, "step": 128,
                    "tooltip": "Reference-context boundary: a "
                               "warning is logged when the encoded sequence "
                               "is longer."}),
                "measure_dma": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "vae": ("VAE", {"tooltip": "Provide to splice reference images into "
                                           "the conditioning as identity latents "
                                           "(multi-reference generation)."}),
                "image1": ("IMAGE",),
                "image2": ("IMAGE",),
                "image3": ("IMAGE",),
                "image4": ("IMAGE",),
            }
        }

    def encode(self, clip, prompt, token_budget, measure_dma, vae=None,
               image1=None, image2=None, image3=None, image4=None):
        refs = [img for img in (image1, image2, image3, image4) if img is not None]
        if len(refs) > MAX_REFERENCES:
            raise ValueError(
                f"{len(refs)} reference images exceed the contract cap of "
                f"{MAX_REFERENCES} (cross-attention saturation boundary).")

        # Ground-truth residency audit: walk the encoder's parameters and count
        # bytes physically resident on CUDA. This is the residency metric
        # (expected: exactly 0) — a free-VRAM delta would
        # conflate ambient DynamicVRAM movement with encoder residency.
        encoder_vram_used = 0
        try:
            for tensor in clip.patcher.model.parameters():
                if tensor.is_cuda:
                    encoder_vram_used += tensor.numel() * tensor.element_size()
        except Exception:
            for _key, (tensor, _pad) in clip.patcher.get_key_patches().items():
                if torch.is_tensor(tensor) and tensor.is_cuda:
                    encoder_vram_used += tensor.numel() * tensor.element_size()
        if encoder_vram_used:
            logger.warning(
                "[qwen21] %d bytes of encoder weights are CUDA-resident; load the "
                "encoder with CLIPLoader device='cpu'.", encoder_vram_used)

        t_encode = time.perf_counter()
        try:
            # Canonical encode semantics (ComfyUI v0.37+): every reference is
            # normalized to <= resolution px (lanczos, aspect preserved), vision
            # tokens are spliced and tokens are built with prevent_empty_text.
            # This node keeps its own caps (<= 4 refs, 512 px) and adds the
            # pinned-memory DMA GPU push below.
            from comfy_extras.nodes_qwen import TextEncodeQwenImage21
        except ImportError:
            # Older core without the canonical node: generic encode path.
            tokens = clip.tokenize(prompt, images=refs)
            conditioning = clip.encode_from_tokens_scheduled(tokens)
        else:
            try:
                conditioning = _encode_positive(clip, prompt, vae, refs,
                                                REFERENCE_MAX_PX)
            except TypeError as exc:
                # core tokenizer API moved: fall back to the core node (which
                # also encodes a negative prompt we then discard)
                logger.warning("[qwen21] positive-only encode unavailable (%s); "
                               "using core TextEncodeQwenImage21.", exc)
                images_dict = {f"image_{i}": img for i, img in enumerate(refs, start=1)}
                conditioning, _negative, _empty_latent = TextEncodeQwenImage21.execute(
                    clip=clip, prompt=prompt, negative_prompt="", vae=vae,
                    resolution=REFERENCE_MAX_PX, images=images_dict)
        encode_s = time.perf_counter() - t_encode

        # Reference-context boundary: reported, never silently
        # truncated (cutting the sequence without its attention mask would
        # corrupt the conditioning).
        seq_tokens = _sequence_tokens(conditioning)
        if seq_tokens > token_budget:
            logger.warning(
                "[qwen21] conditioning holds %d tokens, above token_budget %d: "
                "shorten the prompt or use fewer references.",
                seq_tokens, token_budget)

        payload_bytes = _count_conditioning_bytes(conditioning)
        if payload_bytes > CONDITIONING_BUDGET_MB * 1e6:
            raise ValueError(
                f"conditioning payload {payload_bytes/1e6:.1f} MB exceeds the "
                f"{CONDITIONING_BUDGET_MB:.0f} MB budget — check token budget "
                f"({token_budget}) and reference count ({len(refs)}).")

        dma_ms = 0.0
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            device = torch.device("cuda", torch.cuda.current_device())
            compute = torch.cuda.current_stream()
            stream = torch.cuda.Stream()
            start_ev = torch.cuda.Event(enable_timing=measure_dma)
            stop_ev = torch.cuda.Event(enable_timing=measure_dma)
            moved_tensors = []
            with torch.cuda.stream(stream):
                # the events bracket the copies: start BEFORE they are queued
                start_ev.record(stream)
                moved = _move_conditioning_to_device(conditioning, device,
                                                     moved_tensors)
                stop_ev.record(stream)
            # Fence: the DiT's compute stream waits until the DMA stream retires.
            compute.wait_event(stop_ev)
            # The tensors were allocated on the DMA stream but are consumed on
            # the compute stream: tell the caching allocator, or a block freed
            # while the DiT still reads it could be handed back to a later DMA
            # stream (CUDA streams are pooled and reused).
            for t in moved_tensors:
                t.record_stream(compute)
            if measure_dma:
                stop_ev.synchronize()      # host barrier only to read the timer
                dma_ms = start_ev.elapsed_time(stop_ev)
            conditioning = moved
            del stream

        vram_after = free_vram_mb() if torch.cuda.is_available() else 0.0
        METRICS.emit(node="TextEncodeQwenImage21CPU",
                      event="encode",
                      refs=len(refs),
                      tokens=seq_tokens,
                      encode_s=round(encode_s, 3),
                      dma_ms=round(dma_ms, 3),
                      payload_mb=round(payload_bytes / 1e6, 3),
                      encoder_vram_used_mb=round(encoder_vram_used / 1e6, 6),
                      free_vram_after_mb=round(vram_after, 1))
        if encode_s > 4.5:
            logger.warning("[qwen21] CPU conditioning %.2f s exceeds the 4.50 s "
                           "target.", encode_s)
        return (conditioning,)
