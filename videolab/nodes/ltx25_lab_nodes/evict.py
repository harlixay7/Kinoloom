"""Lab-only helper: explicitly evict models before the diffusion VAE decoder is constructed.

ComfyUI issue #15606: on Windows, constructing comfy/ldm/lightricks/vae/na_diffusion_decoder.py
while the DiT is resident raises "Windows fatal exception: access violation" (no Python
traceback, process dies). Reproduced on a 32 GB card, so it is size-dependent, not VRAM-class
dependent. The reporter's bisected workaround is unload_all_models + empty_cache between the
sampler and the decode. This node performs exactly that, as a no-op passthrough.

First-party lab node, shipped in videolab/nodes/ltx25_lab_nodes.
"""
import gc

import torch


class LTX25LabEvictModels:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",),
                             "enabled": ("BOOLEAN", {"default": True})},
                "optional": {"keep_text_encoder": ("BOOLEAN", {"default": False,
                                "tooltip": "unload everything except the text encoder (keeps its RAM staging; "
                                           "a new prompt then costs the encode, not the 50-75 s re-stage)"})}}

    RETURN_TYPES = ("*",)
    RETURN_NAMES = ("value",)
    FUNCTION = "evict"
    CATEGORY = "LTX25_LAB"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def evict(self, value, enabled=True, keep_text_encoder=False):
        if not enabled:
            print("[LTX25 lab] eviction DISABLED (A/B control)", flush=True)
            return (value,)
        import comfy.model_management as mm
        before = torch.cuda.memory_allocated() if torch.cuda.is_available() else 0
        if keep_text_encoder:
            keep = []
            for lm in list(mm.current_loaded_models):
                try:
                    name = type(lm.model.model).__name__
                except Exception:  # noqa: BLE001
                    name = ""
                if "TE" in name or "TextEncoder" in name or "CLIP" in name:
                    keep.append(lm)
            print(f"[LTX25 lab] selective eviction: keeping {len(keep)} text-encoder model(s): "
                  f"{[type(lm.model.model).__name__ for lm in keep]}", flush=True)
            for device in mm.get_all_torch_devices():
                mm.free_memory(1e30, device, keep_loaded=keep)
        else:
            mm.unload_all_models()
        mm.soft_empty_cache()
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        after = torch.cuda.memory_allocated() if torch.cuda.is_available() else 0
        print(f"[LTX25 lab] explicit eviction before decoder: allocated "
              f"{before/2**30:.2f} GiB -> {after/2**30:.2f} GiB, reserved "
              f"{torch.cuda.memory_reserved()/2**30:.2f} GiB", flush=True)
        return (value,)


NODE_CLASS_MAPPINGS = {"LTX25LabEvictModels": LTX25LabEvictModels}
NODE_DISPLAY_NAME_MAPPINGS = {"LTX25LabEvictModels": "LTX25 Lab Evict Models"}
