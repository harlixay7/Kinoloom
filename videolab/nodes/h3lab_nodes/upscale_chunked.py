"""Lab-only: chunked model-upscale + downscale-to-target (A2 delivery chain).

Failure modes I measured on 2026-09-23 that this node avoids:
  - ImageUpscaleWithModel whole-batch: 49.7 GB CPU allocation (124f x 4x 1080p) -> OOM.
  - Naive chunking: 4x intermediate assembled for all frames = same 49.7 GB; and GPU
    holds the decoded frames (3.1 GB) + chunk activations -> CUDA OOM at chunk 4.

This node fuses upscale + downscale-to-target PER CHUNK: the 4x intermediate exists
only for one chunk on GPU; input stays on CPU between chunks; output is target-sized
(3.1 GB for 124f 1080p) and never exceeds it.

First-party lab node, packaged in videolab/nodes/h3lab_nodes.
"""
import torch


class H3LabUpscaleRefine:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "upscale_model": ("UPSCALE_MODEL",),
                "target_width": ("INT", {"default": 1920, "min": 64, "max": 8192, "step": 2}),
                "target_height": ("INT", {"default": 1088, "min": 64, "max": 8192, "step": 2}),
                "chunk_size": ("INT", {"default": 2, "min": 1, "max": 32, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "H3_LAB"

    def run(self, image, upscale_model, target_width, target_height, chunk_size=2):
        import comfy.model_management as mm
        device = mm.get_torch_device()
        m = upscale_model.model if hasattr(upscale_model, "model") else upscale_model
        m.half().to(device)  # fp32 RRDB intermediates OOM 8 GB at 1080p (measured)
        m.eval()
        image = image.cpu()  # keep the full input off the GPU between chunks
        n = image.shape[0]
        outs = []
        with torch.no_grad():
            for s in range(0, n, chunk_size):
                chunk = image[s:s + chunk_size].movedim(-1, 1).to(device, torch.float16)
                y = m(chunk)                                    # 4x intermediate (GPU only)
                y = y.clamp(0, 1)
                y = torch.nn.functional.interpolate(
                    y, size=(target_height, target_width), mode="bicubic",
                    align_corners=False, antialias=True)
                outs.append(y.float().movedim(1, -1).cpu())
                del chunk, y
                torch.cuda.empty_cache()
                print(f"[H3 lab] refine chunk {min(s + chunk_size, n)}/{n}", flush=True)
        m.to("cpu").float()
        return (torch.cat(outs, 0),)


NODE_CLASS_MAPPINGS = {"H3LabUpscaleRefine": H3LabUpscaleRefine}
NODE_DISPLAY_NAME_MAPPINGS = {"H3LabUpscaleRefine": "H3 Lab Upscale Refine (chunked)"}
