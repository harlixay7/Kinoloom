# node_premul_vae_tiled.py — VAEDecodeTiledPremulAlpha
#
# Tiled VAE decoding of 64-channel RGBA latents with
# premultiplied-alpha composition. Pixel-domain windows of `tile_size` px with
# `overlap` px overlap (defaults: 512 / 64) are decoded independently, then
# composited with
#
#     num = sum_i W_i * a_i * C_i     den = sum_i W_i * a_i     wsum = sum_i W_i
#     C~  = num / max(den, eps)                                 a~  = den / wsum
#
# so RGB is premultiplied by alpha *before* spatial blending and de-multiplied
# after canvas accumulation — identical true colors across a seam reproduce
# exactly, which avoids the dark fringes produced by straight RGB cross-fades.
#
# The tile convention matches core VAEDecodeTiled: tile_size/overlap are PIXELS,
# converted to latent units via vae.spacial_compression_decode().

import logging
import time

import torch

logger = logging.getLogger("qwen21")

from ..qwen21_common import (
    BLEND_EPS,
    METRICS,
    assert_vram_bound,
    ensure_vram_headroom,
    estimate_vae_scale,
    free_vram_mb,
    window_positions,
)


class VAEDecodeTiledPremulAlpha:
    """Tiled RGBA VAE decoder with premultiplied-alpha composition."""

    CATEGORY = "qwen21/vae"
    FUNCTION = "decode"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("rgba_image",)
    DESCRIPTION = (
        "Tiled VAE decode for 64-channel RGBA latents. Decodes 512 px windows with "
        "64 px overlap, premultiplies RGB by alpha before blending and de-multiplies "
        "after accumulation, which aims to avoid the dark edges a straight RGB "
        "cross-fade leaves on transparent cutouts."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT",),
                "vae": ("VAE",),
                "tile_size": ("INT", {
                    "default": 512, "min": 64, "max": 4096, "step": 32,
                    "tooltip": "Pixel-domain decode window (default: 512)."}),
                "overlap": ("INT", {
                    "default": 64, "min": 0, "max": 4096, "step": 32,
                    "tooltip": "Pixel-domain window overlap (default: 64)."}),
                "premultiply": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Premultiplied-alpha composition (the intended mode). "
                               "Disable only for A/B seam debugging; disabled mode uses "
                               "the artifact-prone straight cross-fade."}),
                "vram_gate_mb": ("INT", {
                    "default": 7000, "min": 0, "max": 8192, "step": 8,
                    "tooltip": "Pre-decode free-VRAM gate."}),
            }
        }

    def decode(self, samples, vae, tile_size, overlap, premultiply, vram_gate_mb):
        latent = samples["samples"]
        if latent.is_nested:                       # same guard as core VAEDecodeTiled
            latent = latent.unbind()[0]
        if latent.dim() != 4:
            raise ValueError(f"expected [B,C,H,W] latent, got {tuple(latent.shape)}")

        batch, _channels, lat_h, lat_w = latent.shape
        ratio, compression = estimate_vae_scale(vae)

        # Pixel windows -> latent windows (same conversion as core VAEDecodeTiled).
        if tile_size < overlap * 4:
            overlap = tile_size // 4
        tile_lat = max(1, tile_size // compression)
        overlap_lat = min(max(1, overlap // compression), tile_lat - 1)
        # a window longer than the axis is sliced down to it below, so a small
        # canvas is simply one window per axis
        pos_y = window_positions(lat_h, tile_lat, overlap_lat)
        pos_x = window_positions(lat_w, tile_lat, overlap_lat)

        out_h, out_w = lat_h * ratio, lat_w * ratio
        # measured AFTER the gate: the pre-eviction reading would be counted
        # as decode usage and could fail a decode that stayed in budget
        free_before = ensure_vram_headroom(min_free_mb=vram_gate_mb)
        device = latent.device
        acc = torch.zeros((batch, out_h, out_w, 4), dtype=torch.float32, device=device)
        weights = torch.zeros((out_h, out_w, 1), dtype=torch.float32, device=device)

        min_free_observed = free_before
        t0 = time.perf_counter()
        n_windows = len(pos_y) * len(pos_x)
        window_px = tile_lat * ratio
        logger.info("[qwen21] PremulAlpha decode: %dx%d latent -> %dx%d px, %d windows "
                    "(%d px window, %d px overlap, compression x%d, batch %d)",
                    lat_w, lat_h, out_w, out_h, n_windows, window_px,
                    overlap_lat * ratio, compression, batch)

        with torch.no_grad():
            for wi, oy in enumerate(pos_y):
                for xi, ox in enumerate(pos_x):
                    window = latent[:, :, oy:oy + tile_lat, ox:ox + tile_lat].contiguous()
                    tile_img = vae.decode_tiled(
                        window,
                        tile_x=max(1, tile_size // compression),
                        tile_y=max(1, tile_size // compression),
                        overlap=max(0, overlap // compression),
                    )
                    if tile_img.dim() == 5:            # merge frame axis of video VAEs
                        tile_img = tile_img.reshape(-1, *tile_img.shape[-3:])
                    if tile_img.shape[0] != batch:
                        raise RuntimeError(
                            f"VAE returned batch {tile_img.shape[0]} for input batch {batch}")
                    if tile_img.shape[-1] == 3:
                        # VAE emitted straight RGB: synthesize opaque alpha so the
                        # RGBA composition still holds (alpha = 1 everywhere).
                        tile_img = torch.cat(
                            [tile_img, torch.ones((*tile_img.shape[:-1], 1),
                                                  dtype=tile_img.dtype, device=device)],
                            dim=-1)
                    if tile_img.shape[-1] != 4:
                        raise ValueError(f"expected 4-channel RGBA tile, got {tuple(tile_img.shape)}")

                    px_y = oy * ratio
                    px_x = ox * ratio
                    ph, pw = tile_img.shape[1], tile_img.shape[2]
                    # Ratio-preserving normalization: sole-cover pixels get weight
                    # exactly 1.0; overlap-band blend ratios are unchanged.
                    region_w = sinusoidal_region(ph, pw, device)
                    region_w = (region_w / region_w.amax().clamp_min(BLEND_EPS)).unsqueeze(-1)
                    tile32 = tile_img.to(torch.float32)
                    dst_w = weights[px_y:px_y + ph, px_x:px_x + pw]
                    if premultiply:
                        for b in range(batch):
                            rgb, alpha = tile32[b, ..., :3], tile32[b, ..., 3:4]
                            acc[b, px_y:px_y + ph, px_x:px_x + pw, :3] += region_w * (rgb * alpha)
                            acc[b, px_y:px_y + ph, px_x:px_x + pw, 3:4] += region_w * alpha
                    else:
                        for b in range(batch):
                            acc[b, px_y:px_y + ph, px_x:px_x + pw, :] += region_w * tile32[b]
                    dst_w += region_w
                    min_free_observed = min(min_free_observed, free_vram_mb())

        if premultiply:
            alpha_canvas = (acc[..., 3:4] / weights.clamp_min(BLEND_EPS)).clamp(0.0, 1.0)
            color_canvas = (acc[..., :3] / acc[..., 3:4].clamp_min(BLEND_EPS)).clamp(0.0, 1.0)
            out = torch.cat([color_canvas, alpha_canvas], dim=-1)
        else:
            out = (acc / weights.clamp_min(BLEND_EPS).unsqueeze(0)).clamp(0.0, 1.0)

        elapsed = time.perf_counter() - t0
        peak_used = assert_vram_bound(min_free_observed, "VAEDecodeTiledPremulAlpha",
                                      f"premul_tiled_decode_{out_h}x{out_w}")
        METRICS.emit(node="VAEDecodeTiledPremulAlpha",
                      event="decode",
                      out_h=out_h, out_w=out_w, windows=n_windows,
                      premultiply=bool(premultiply),
                      free_before_mb=round(free_before, 1),
                      min_free_mb=round(min_free_observed, 1),
                      peak_used_mb=round(peak_used, 1),
                      elapsed_s=round(elapsed, 3))
        # ComfyUI IMAGE layout: [B, H, W, C] on the VAE output device.
        return (out.to(dtype=torch.float32),)


def sinusoidal_region(h, w, device):
    """Per-window sinusoidal weight in *pixel* space (sin x sin, pixel centers)."""
    import math
    y = (torch.arange(h, device=device, dtype=torch.float32) + 0.5) / h
    x = (torch.arange(w, device=device, dtype=torch.float32) + 0.5) / w
    return torch.sin(math.pi * y)[:, None] * torch.sin(math.pi * x)[None, :]
