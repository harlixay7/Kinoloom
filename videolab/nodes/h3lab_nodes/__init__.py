from .upscale_chunked import H3LabUpscaleRefine

NODE_CLASS_MAPPINGS = {"H3LabUpscaleRefine": H3LabUpscaleRefine}
NODE_DISPLAY_NAME_MAPPINGS = {"H3LabUpscaleRefine": "H3 Lab Upscale Refine (chunked)"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
