"""ComfyUI-Qwen21-Optimized: ComfyUI nodes for running the
Qwen-Image-2.1 pipeline on 8 GB VRAM workstations (RTX 3070 class).

Pipeline mapping:
  Text conditioning: TextEncodeQwenImage21CPU (host CPU + DDR4, 0 MB VRAM) / PromptEnhancerHostPE (CPU orchestration, CUDA ConvRot paging)
  Base sampling: KSampler base (DiT Q4_K_M in VRAM)
  Intermediate decode: VAEDecodeTiledPremulAlpha (tiled RGBA decode)
  2K refinement: LatentTiledRefiner (2x2 / 72-token windows)
  Final decode and export: VAEDecodeTiledPremulAlpha + SaveImageAdvanced (32-bit RGBA PNG export)
"""

from .nodes import (
    EmptyQwen21LatentImage,
    LatentTiledRefiner,
    MergeImageRGBA,
    PromptEnhancerHostPE,
    SplitImageRGBA,
    TextEncodeQwenImage21CPU,
    VAEDecodeTiledPremulAlpha,
)

# Register the pack's custom model folder (models/prompt_generator) so
# folder_paths.get_full_path resolves PE-T2I checkpoints placed there.
try:
    import os

    import folder_paths

    if "prompt_generator" not in folder_paths.folder_names_and_paths:
        folder_paths.add_model_folder_path(
            "prompt_generator",
            os.path.join(folder_paths.base_path, "models", "prompt_generator"))
except Exception:  # noqa: BLE001 - registration must never block node loading
    pass


NODE_CLASS_MAPPINGS = {
    "TextEncodeQwenImage21CPU": TextEncodeQwenImage21CPU,
    "LatentTiledRefiner": LatentTiledRefiner,
    "VAEDecodeTiledPremulAlpha": VAEDecodeTiledPremulAlpha,
    "EmptyQwen21LatentImage": EmptyQwen21LatentImage,
    "PromptEnhancerHostPE": PromptEnhancerHostPE,
    "SplitImageRGBA": SplitImageRGBA,
    "MergeImageRGBA": MergeImageRGBA,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TextEncodeQwenImage21CPU": "Qwen Image 2.1 Text Encode (Host CPU + DMA)",
    "LatentTiledRefiner": "Qwen Image 2.1 Latent Tiled Refiner (2K)",
    "VAEDecodeTiledPremulAlpha": "Qwen Image 2.1 VAE Decode Tiled (Premul Alpha)",
    "EmptyQwen21LatentImage": "Empty Qwen Image 2.1 Latent (64ch RGBA)",
    "PromptEnhancerHostPE": "Qwen Image 2.1 Prompt Enhancer (Host PE-T2I)",
    "SplitImageRGBA": "Qwen Image 2.1 Split RGBA",
    "MergeImageRGBA": "Qwen Image 2.1 Merge RGBA",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
