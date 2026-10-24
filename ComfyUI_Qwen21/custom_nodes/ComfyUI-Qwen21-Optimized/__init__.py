# ComfyUI-Qwen21-Optimized — custom nodes for running the Qwen-Image-2.1
# pipeline on 8 GB VRAM workstations (RTX 3070 class).
#
# Stages of the full tiled graph these nodes were designed for:
#   Text encode     TextEncodeQwenImage21CPU / PromptEnhancerHostPE   encoder on host CPU + DDR4 (the PE pages through VRAM)
#   Base sample     KSampler (base)                                   DiT Q4_K_M in VRAM
#   Tiled decode    VAEDecodeTiledPremulAlpha                         tiled RGBA decode
#   Latent refine   LatentTiledRefiner                                2x2 / 72-token windows
#   Final export    VAEDecodeTiledPremulAlpha + SaveImageAdvanced     32-bit RGBA PNG export

from .nodes import (
    EmptyQwen21LatentImage,
    LatentTiledRefiner,
    MergeImageRGBA,
    PromptEnhancerHostPE,
    SplitImageRGBA,
    TextEncodeQwenImage21CPU,
    VAEDecodeTiledPremulAlpha,
)

# Register the custom model folder models/prompt_generator so
# folder_paths.get_full_path resolves PE-T2I checkpoints placed there.
try:
    import os

    import folder_paths

    if "prompt_generator" not in folder_paths.folder_names_and_paths:
        folder_paths.add_model_folder_path(
            "prompt_generator",
            os.path.join(folder_paths.base_path, "models", "prompt_generator"))
except Exception:  # noqa: BLE001 — registration must never block node loading
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
