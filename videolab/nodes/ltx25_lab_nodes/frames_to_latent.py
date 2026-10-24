"""Lab-only: encode a directory of PNG frames as an LTX video latent.

Why: the LTX-as-upscaler probe (A3) conditions the LTX-2.5 stage-2 sampler on an EXTERNAL video (H3 hero output
decoded to frames). Stock nodes have no image-sequence -> latent path without VHS.
This node loads sorted PNGs, resizes, trims the count to the LTX frame rule
(N % 8 == 1), and encodes with the video VAE.

First-party lab node, shipped in videolab/nodes/ltx25_lab_nodes.
Registered in __init__.py; none of the shipped workflow templates uses it.
"""
import numpy as np
import torch
from PIL import Image


class LTX25LabFramesToLatent:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "frames_dir": ("STRING", {"default": "", "forceInput": False}),
                "vae": ("VAE",),
                "width": ("INT", {"default": 1920, "min": 64, "max": 4096, "step": 32}),
                "height": ("INT", {"default": 1088, "min": 64, "max": 4096, "step": 32}),
                "max_frames": ("INT", {"default": 0, "min": 0, "max": 10000,
                                       "tooltip": "0 = all frames (trimmed to N % 8 == 1)"}),
            }
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("video_latent",)
    FUNCTION = "encode"
    CATEGORY = "LTX25_LAB"

    def encode(self, frames_dir, vae, width, height, max_frames=0):
        from pathlib import Path
        paths = sorted(Path(frames_dir).glob("*.png"))
        if not paths:
            raise ValueError(f"no PNG frames in {frames_dir}")
        if max_frames and max_frames > 0:
            paths = paths[:max_frames]
        n = len(paths)
        n -= (n - 1) % 8  # LTX frame rule N % 8 == 1
        paths = paths[:n]
        frames = []
        for p in paths:
            im = Image.open(p).convert("RGB")
            if im.size != (width, height):
                im = im.resize((width, height), Image.LANCZOS)
            frames.append(np.asarray(im, dtype=np.float32))
        pixels = torch.from_numpy(np.stack(frames))  # [T,H,W,C] in 0..1
        pixels = pixels.unsqueeze(0)                 # [1,T,H,W,C]
        # Same contract as LTXVImgToVideoInplace: channels-last 0..1 video,
        # vae.encode handles device/dtype/scale and returns [B,C,T',H',W'].
        print(f"[LTX25 lab] encoding {pixels.shape[1]} frames @ {width}x{height}", flush=True)
        out = vae.encode(pixels[:, :, :, :, :3])
        if isinstance(out, dict):
            out = out["samples"]
        print(f"[LTX25 lab] latent {tuple(out.shape)}", flush=True)
        return ({"samples": out},)


NODE_CLASS_MAPPINGS = {"LTX25LabFramesToLatent": LTX25LabFramesToLatent}
NODE_DISPLAY_NAME_MAPPINGS = {"LTX25LabFramesToLatent": "LTX25 Lab Frames To Latent"}
