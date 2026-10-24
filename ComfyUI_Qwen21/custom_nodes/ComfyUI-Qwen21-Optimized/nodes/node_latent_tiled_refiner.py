# node_latent_tiled_refiner.py — LatentTiledRefiner
#
# Latent-refine stage of the pipeline: low-denoise detail refinement of a 2K
# latent in the latent domain, partitioned into overlapping windows to keep
# each pass's activation memory small. The design target was peak VRAM
# <= 5.2 GB; the hard check afterwards is the 7,392 MB ceiling of an 8 GB card
# (the pack README records a run that went over it).
#
# Window closure is solved, not assumed: n·T - (n-1)·O = H  =>  T = (H+O)/n.
# For a 2048 px canvas (128x128 latent, 16x compression) with
# O = 16 tokens (256 px) this yields T = 72, a 2x2 grid, stride 56 (4 tiles
# with 16 tokens of overlap). The node generalizes: any canvas closes exactly,
# and a per-window token ceiling is enforced before sampling starts.
#
# Blending uses the separable sinusoidal window
#     W(x, y) = sin(pi x / L) * sin(pi y / L)
# accumulated with normalized partition-of-unity division sum W_i f_i / sum W_i.

import logging
import math
import time

import torch

logger = logging.getLogger("qwen21")

from ..qwen21_common import (
    METRICS,
    assert_vram_bound,
    clamp_tile,
    ensure_vram_headroom,
    free_vram_mb,
    window_positions,
)

import comfy.sample
import comfy.samplers
import comfy.utils


class LatentTiledRefiner:
    """Tiled low-denoise latent refinement with sinusoidal window blending."""

    CATEGORY = "qwen21/sampling"
    FUNCTION = "refine"
    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("refined_latent",)
    DESCRIPTION = (
        "Partitions a 128x128 latent into overlapping 72x72 windows (2x2 grid, "
        "16-token / 256 px overlap), denoises each at 0.25 / 8 steps with res_multistep "
        "+ beta (CFG 1.0) and reassembles with sinusoidal blend weights. The windows "
        "aim to keep peak VRAM low enough to avoid WDDM paging; the node gates free "
        "VRAM before sampling and checks the peak against the card's ceiling afterwards."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "latent": ("LATENT",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff,
                                       "control_after_generate": True}),
                "tile_size": ("INT", {
                    "default": 72, "min": 32, "max": 128, "step": 8,
                    "tooltip": "Latent-token window side. 72 solves the "
                               "closure (H+O)/n for 128/16/2x2."}),
                "overlap": ("INT", {
                    "default": 16, "min": 0, "max": 32, "step": 1,
                    "tooltip": "Window overlap in latent tokens (default: 16 = 256 px)."}),
                "max_window_tokens": ("INT", {
                    "default": 6400, "min": 1024, "max": 16384, "step": 256,
                    "tooltip": "Hard per-window S bound protecting the activation budget."}),
                "steps": ("INT", {"default": 8, "min": 1, "max": 100}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.01,
                                  "tooltip": "1.0 (guidance distillation); values above 1.0 raise an error."}),
                "denoise": ("FLOAT", {"default": 0.25, "min": 0.01, "max": 1.0, "step": 0.01,
                                      "tooltip": "Intended range: 0.20-0.30 detail refinement."}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": "res_multistep"}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "beta"}),
                "vram_gate_mb": ("INT", {"default": 7000, "min": 0, "max": 8192, "step": 8}),
            }
        }

    def refine(self, model, latent, positive, negative, noise_seed, tile_size, overlap,
               max_window_tokens, steps, cfg, denoise, sampler_name, scheduler, vram_gate_mb):
        if cfg > 1.0:
            # Hard enforcement, not a warning: guidance-distilled latents burn
            # (highlight clipping, contrast blowout) under CFG > 1.0.
            raise ValueError(
                f"CFG {cfg} violates the design constraints (strictly 1.0, guidance "
                f"distillation). Negative constraints belong in the positive prompt.")

        samples = latent["samples"]
        if samples.dim() != 4:
            raise ValueError(f"expected [B,C,H,W] latent, got {tuple(samples.shape)}")
        batch, channels, lat_h, lat_w = samples.shape

        # per-axis window: an axis shorter than tile_size is one window
        tile_h, ov_h = clamp_tile(lat_h, tile_size, overlap)
        tile_w, ov_w = clamp_tile(lat_w, tile_size, overlap)
        pos_y = window_positions(lat_h, tile_h, ov_h)
        pos_x = window_positions(lat_w, tile_w, ov_w)
        windows = [(oy, ox) for oy in pos_y for ox in pos_x]
        s_per_window = tile_h * tile_w
        if s_per_window > max_window_tokens:
            raise ValueError(
                f"window S={s_per_window} exceeds the {max_window_tokens}-token budget; "
                f"reduce tile_size or raise max_window_tokens knowingly.")

        # Grid sanity: a 2K refinement should land on exactly 4 windows; log the grid.
        logger.info("[qwen21] LatentTiledRefiner: latent %dx%d, grid %dx%d = %d windows "
                    "(tile %d, overlap %d, S/window %d, denoise %.2f, steps %d)",
                    lat_w, lat_h, len(pos_x), len(pos_y), len(windows),
                    max(tile_h, tile_w), max(ov_h, ov_w), s_per_window, denoise, steps)

        # Free-VRAM gate: the strict 7,000 MB requirement applies to
        # the clean pre-sampling state. During the window loop the DiT is
        # legitimately resident (free ~3,300 MB by the base-sampling budget), so each
        # window carries only a WDDM paging guard at ~900 MB free, with eviction
        # as the last resort.
        ensure_vram_headroom(min_free_mb=vram_gate_mb)
        PAGING_GUARD_MB = 900
        device = samples.device
        work_dtype = samples.dtype

        acc = torch.zeros((batch, channels, lat_h, lat_w), dtype=torch.float32, device=device)
        wsum = torch.zeros((lat_h, lat_w), dtype=torch.float32, device=device)

        # One raw sinusoidal tile weight (sin x sin, pixel-center sampled), scaled
        # so its peak is exactly 1.0: sole-cover pixels then blend with weight 1 and
        # overlap-band ratios are untouched (ratio-preserving normalization).
        y_axis = (torch.arange(tile_h, device=device, dtype=torch.float32) + 0.5) / tile_h
        x_axis = (torch.arange(tile_w, device=device, dtype=torch.float32) + 0.5) / tile_w
        raw_tile = torch.sin(math.pi * y_axis)[:, None] * torch.sin(math.pi * x_axis)[None, :]
        raw_tile = raw_tile / raw_tile.amax().clamp_min(1e-12)

        min_free_observed = free_vram_mb()
        t0 = time.perf_counter()
        per_window_s = []

        with torch.no_grad():
            for idx, (oy, ox) in enumerate(windows):
                if free_vram_mb() < PAGING_GUARD_MB:
                    ensure_vram_headroom(min_free_mb=PAGING_GUARD_MB, max_attempts=2,
                                         unload_models=True)
                window_latent = samples[:, :, oy:oy + tile_h, ox:ox + tile_w].contiguous()
                # Deterministic per-window noise: same seed schedule replays exactly.
                window_seed = (noise_seed + idx * 1_000_003) % (1 << 64)
                noise = comfy.sample.prepare_noise(window_latent, window_seed)

                t_w = time.perf_counter()
                denoised = comfy.sample.sample(
                    model=model,
                    noise=noise,
                    steps=steps,
                    cfg=cfg,
                    sampler_name=sampler_name,
                    scheduler=scheduler,
                    positive=positive,
                    negative=negative,
                    latent_image=window_latent,
                    denoise=denoise,
                    disable_pbar=False,
                    seed=window_seed,
                )
                per_window_s.append(time.perf_counter() - t_w)

                w2d = raw_tile
                acc[:, :, oy:oy + tile_h, ox:ox + tile_w] += w2d * denoised.to(torch.float32)
                wsum[oy:oy + tile_h, ox:ox + tile_w] += w2d
                min_free_observed = min(min_free_observed, free_vram_mb())
                logger.info("[qwen21]   window %d/%d @(%d,%d) done in %.2f s",
                            idx + 1, len(windows), ox, oy, per_window_s[-1])

        refined = (acc / wsum.clamp_min(1e-6)).to(dtype=work_dtype)
        elapsed = time.perf_counter() - t0
        peak_used = assert_vram_bound(min_free_observed, "LatentTiledRefiner",
                                      f"tiled_refine_{lat_w}x{lat_h}")
        METRICS.emit(node="LatentTiledRefiner",
                      event="refine",
                      windows=len(windows), tile=tile_size, overlap=overlap,
                      s_per_window=s_per_window, steps=steps, cfg=cfg, denoise=denoise,
                      sampler=sampler_name, scheduler=scheduler,
                      mean_window_s=round(sum(per_window_s) / max(1, len(per_window_s)), 3),
                      min_free_mb=round(min_free_observed, 1),
                      peak_used_mb=round(peak_used, 1),
                      elapsed_s=round(elapsed, 3))

        # Preserve any auxiliary latent dict entries (e.g. batch indices); only the
        # sample tensor is replaced by the blended canvas.
        out = dict(latent)
        out["samples"] = refined
        return (out,)
