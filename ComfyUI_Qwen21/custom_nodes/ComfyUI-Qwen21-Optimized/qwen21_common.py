# qwen21_common.py — shared engineering primitives for ComfyUI-Qwen21-Optimized.
#
# Implements the numeric core the nodes share:
#   * exact-coverage overlapping window partitioning (closure T = (H + O)/n family)
#   * 2D separable sinusoidal blending window W(x,y) = sin(pi x/L) sin(pi y/L)
#   * ratio-preserving per-pixel weight normalization (numerically hardened, blend-identical)
#   * premultiplied-alpha RGBA tile accumulation
#   * VRAM headroom gate (gc -> empty_cache -> unload -> re-poll)
#   * JSONL per-node telemetry (qwen21_metrics.jsonl)
#
# Hard constants (MiB, see free_vram_mb) from the design constraints:
#   physical 8192 / OS buffer 800 -> usable ceiling 7392; pre-sample gate 7000.

import json
import logging
import math
import os
import time

logger = logging.getLogger("qwen21")

PHYSICAL_VRAM_MB = 8192
OS_DISPLAY_BUFFER_MB = 800
VRAM_CEILING_MB = PHYSICAL_VRAM_MB - OS_DISPLAY_BUFFER_MB   # 7392 usable ceiling
VRAM_GATE_MB = 7000                                         # pre-sample free-VRAM gate
LATENT_CHANNELS = 64
SPATIAL_COMPRESSION = 16
BLEND_EPS = 1e-6


def free_vram_mb():
    """Free physical VRAM in MiB via torch.cuda.mem_get_info(); 0.0 when CUDA absent.

    MiB (2**20) is the unit the constants above are written in
    (8,192 MB physical / 7,392 MB ceiling), so all comparisons stay consistent.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        free_bytes, _total = torch.cuda.mem_get_info()
        return free_bytes / (1024 ** 2)
    except Exception:
        return 0.0


def ensure_vram_headroom(min_free_mb=VRAM_GATE_MB, max_attempts=5, unload_models=True):
    """Free-VRAM gate: pause, collect, evict and re-poll until headroom is restored.

    A 1.5% tolerance absorbs desktop-compositor variance: the machine idles at
    ~7,114 MiB free, so a strict 7,000 MiB gate fails randomly when the display
    manager drifts a few dozen MiB (observed: 6,964 MiB free after full eviction).
    Acceptance inside the tolerance logs a warning instead of aborting the run.

    Returns the observed free MB. Raises RuntimeError when headroom cannot be
    restored: the design aims to keep sampling out of Windows WDDM
    shared-memory paging, so the gate aborts before sampling instead.
    """
    import gc

    import torch
    accept_floor = min_free_mb * 0.985
    last_free = free_vram_mb()
    if last_free >= min_free_mb:
        return last_free
    for attempt in range(1, max_attempts + 1):
        logger.warning("[qwen21] VRAM gate %.0f MB < %.0f MB free (attempt %d/%d): collecting.",
                       last_free, min_free_mb, attempt, max_attempts)
        gc.collect()
        try:
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
        except Exception:
            pass
        if unload_models:
            try:
                import comfy.model_management as mm
                mm.unload_all_models()
                mm.soft_empty_cache()
            except Exception:
                pass
        time.sleep(0.5 * attempt)
        last_free = free_vram_mb()
        if last_free >= min_free_mb:
            logger.info("[qwen21] VRAM gate restored: %.0f MB free.", last_free)
            return last_free
        if last_free >= accept_floor:
            logger.warning("[qwen21] VRAM gate accepted at %.0f MB free "
                           "(>= 98.5%% of %.0f MB gate) within compositor tolerance.",
                           last_free, min_free_mb)
            return last_free
    raise RuntimeError(
        f"VRAM gate failed: only {last_free:.0f} MB VRAM free after {max_attempts} "
        f"recovery attempts (gate {min_free_mb} MB). Aborting before sampling to avoid "
        f"Windows WDDM shared-memory paging.")


class MetricsLog:
    """Append-only JSONL telemetry (qwen21_metrics.jsonl) for benchmark scripts.

    Written next to the ComfyUI user directory when available so one shared log serves
    the whole server process; falls back to %TEMP% when executed outside ComfyUI
    (e.g. unit self-tests).
    """

    def __init__(self):
        self.path = self._resolve_path()
        self._lock_held = False

    @staticmethod
    def _resolve_path():
        candidates = []
        try:
            import folder_paths
            candidates.append(os.path.join(folder_paths.get_user_directory(), "qwen21_metrics.jsonl"))
        except Exception:
            pass
        candidates.append(os.path.join(os.environ.get("TEMP", os.getcwd()), "qwen21_metrics.jsonl"))
        for path in candidates:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                probe = open(path, "a", encoding="utf-8")
                probe.close()
                return path
            except OSError:
                continue
        return None

    def emit(self, node, **fields):
        record = {"ts": time.time(), "node": node}
        record.update(fields)
        if self.path is None:
            logger.info("[qwen21:metrics] %s", json.dumps(record))
            return record
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            logger.info("[qwen21:metrics] %s", json.dumps(record))
        return record


METRICS = MetricsLog()


def window_positions(size, tile, overlap):
    """Monotonic window origins giving *exact* coverage of [0, size).

    For the 2K refinement (size=128, tile=72, overlap=16) this yields
    [0, 56] -> two windows per axis, overlap exactly 16 tokens = 256 px. Spacing is
    equalized across [0, size-tile] so consecutive windows always overlap by >= 0 and
    the union always covers the axis; equal spacing also keeps blend weights symmetric.

    An axis no longer than the tile is a single window at 0; callers clamp the
    window to the axis (see clamp_tile) so it covers exactly [0, size).
    """
    if tile <= 0 or size <= 0:
        raise ValueError(f"tile {tile} invalid for axis size {size}")
    if size <= tile:
        return [0]
    if overlap >= tile:
        raise ValueError(f"overlap {overlap} must be < tile {tile}")
    n = math.ceil((size - tile) / (tile - overlap)) + 1
    if n == 1:
        return [0]
    last = size - tile
    return [round(i * last / (n - 1)) for i in range(n)]


def clamp_tile(size, tile, overlap):
    """(tile, overlap) for one axis: the window never exceeds the axis, and the
    overlap stays below the window. A canvas smaller than the default window
    (e.g. under 1152 px for the refiner's 72-token tile) is one window."""
    t = max(1, min(int(tile), int(size)))
    return t, max(0, min(int(overlap), t - 1))


def sinusoidal_window_2d(tile_h, tile_w, device="cpu", dtype=None):
    """Separable window W(x, y) = sin(pi x / L) * sin(pi y / L), sampled at pixel centers.

    Pixel-center sampling (x + 0.5) keeps every weight strictly positive so that a
    window which solely covers a border pixel still contributes (a raw sin window has
    zeros exactly on its edge rows/cols and would punch black holes into the canvas
    border). Blend ratios are unaffected by any per-pixel positive rescaling, which
    blend_weights_at() exploits.
    """
    import torch
    x = (torch.arange(tile_w, device=device, dtype=torch.float32) + 0.5) / tile_w
    y = (torch.arange(tile_h, device=device, dtype=torch.float32) + 0.5) / tile_h
    wy = torch.sin(math.pi * y)
    wx = torch.sin(math.pi * x)
    w = wy[:, None] * wx[None, :]
    if dtype is not None:
        w = w.to(dtype)
    return w


def blend_weights_at(weight_stack):
    """Ratio-preserving per-pixel normalization of a stack of window weights.

    weight_stack: [N, H, W] tensor of raw window weights (>= 0). Returns [N, H, W]
    where, per pixel, every weight is divided by the largest weight at that pixel.
    Dividing all candidates at a pixel by the same positive constant leaves the
    normalized blend sum W_i f_i / sum W_i unchanged — identical to the plain
    sinusoidal formulation — while sole-cover pixels get weight exactly 1.0, which
    removes the float32 underflow risk of tiny border weights.
    """
    peak = weight_stack.amax(dim=0, keepdim=True).clamp_min(BLEND_EPS)
    return weight_stack / peak


def assemble_weight_grid(grid_h, grid_w, tile, positions_y, positions_x, device="cpu"):
    """Build [N, H_lat, W_lat] raw sinusoidal weights for every tile window in the grid."""
    import torch
    n = len(positions_y) * len(positions_x)
    canvas = torch.zeros((n, grid_h, grid_w), dtype=torch.float32, device=device)
    base = sinusoidal_window_2d(tile, tile, device=device)
    idx = 0
    for oy in positions_y:
        for ox in positions_x:
            canvas[idx, oy:oy + tile, ox:ox + tile] = base
            idx += 1
    return canvas


def premul_alpha_composite(tiles, weights):
    """Premultiplied-alpha composition: accumulation then de-multiplication.

    tiles:    [N, H, W, 4] RGBA in [0, 1] (RGB straight, channel 3 = alpha)
    weights:  [N, H, W] normalized blending weights (sum over N may exceed 1 in overlaps)
    returns   [H, W, 4] composited RGBA, float32, on the same device.

        num  = sum_i W_i * a_i * C_i        den  = sum_i W_i * a_i        wsum = sum_i W_i
        C~   = num / max(den, eps)          a~   = den / wsum

    Identical true colors across a seam reproduce exactly (num = C * den), so the
    blend itself adds no dark edge where the tiles agree; fully transparent pixels
    collapse to RGB 0 under alpha 0 (numerically irrelevant).
    """
    import torch
    if tiles.dim() != 4 or tiles.shape[-1] != 4:
        raise ValueError(f"expected [N,H,W,4] RGBA tiles, got {tuple(tiles.shape)}")
    w = weights.to(tiles.dtype).unsqueeze(-1)              # [N,H,W,1]
    rgb, alpha = tiles[..., :3], tiles[..., 3:4]
    premul = rgb * alpha                                   # C * alpha (premultiplication)
    num = (w * premul).sum(dim=0)                          # sum W a C
    den = (w * alpha).sum(dim=0)                           # sum W a
    wsum = w.sum(dim=0)                                    # sum W
    color = num / den.clamp_min(BLEND_EPS)
    out_alpha = den / wsum.clamp_min(BLEND_EPS)
    return torch.cat([color, out_alpha], dim=-1)


def linear_rgb_composite(tiles, weights):
    """Naive straight-RGB cross-fade kept ONLY as the benchmark's negative control:
    it is the artifact-producing blend the design avoids (dark fringing where
    alpha < 1). Never used in production paths."""
    import torch
    w = weights.to(tiles.dtype).unsqueeze(-1)              # [N,H,W,1]
    wsum = w.sum(dim=0).clamp_min(BLEND_EPS)               # [H,W,1]
    color = (w * tiles[..., :3]).sum(dim=0) / wsum
    alpha = (w * tiles[..., 3:4]).sum(dim=0) / wsum
    return torch.cat([color, alpha], dim=-1)


def latent_blend(tiles, weights):
    """Alpha-free latent-domain blend: L~ = sum W_i L_i / sum W_i in float32."""
    w = weights.to(tiles.dtype).unsqueeze(1)               # [N,1,H,W] for [N,C,H,W] tiles
    return (w * tiles).sum(dim=0) / w.sum(dim=0).clamp_min(BLEND_EPS)


def estimate_vae_scale(vae):
    """Return (upscale_ratio, compression) tolerating scalar or per-axis ratios."""
    comp = vae.spacial_compression_decode()
    ratio = getattr(vae, "upscale_ratio", comp)
    if isinstance(ratio, (list, tuple)):
        ratio = ratio[-1]
    return int(ratio), int(comp)


def vram_snapshot(tag, node):
    free = free_vram_mb()
    METRICS.emit(node=node, event="vram", tag=tag, free_mb=round(free, 1),
                 ceiling_mb=VRAM_CEILING_MB)
    return free


def total_vram_mb():
    """Physical VRAM of the current device in MiB; None when CUDA is absent."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        _free, total = torch.cuda.mem_get_info()
        return total / (1024 ** 2)
    except Exception:
        return None


def assert_vram_bound(observed_free_min_mb, node, phase):
    """Post-hoc ceiling check: minimum observed free VRAM must imply usage within
    the card's physical VRAM minus the OS/display buffer (7392 MB on an
    8 GB card). The card's real size is used, so a worker with a
    different GPU is neither falsely failed nor unchecked."""
    physical = total_vram_mb()
    if physical is None:
        return 0.0                  # no CUDA: nothing was placed on a GPU
    ceiling = physical - OS_DISPLAY_BUFFER_MB
    peak_used = physical - observed_free_min_mb
    METRICS.emit(node=node, event="peak_bound", phase=phase,
                 peak_used_mb=round(peak_used, 1), ceiling_mb=round(ceiling, 1))
    if peak_used > ceiling:
        raise RuntimeError(
            f"VRAM ceiling exceeded during {phase}: peak VRAM {peak_used:.0f} MB exceeds "
            f"the {ceiling:.0f} MB usable ceiling (physical {physical:.0f} MB - "
            f"{OS_DISPLAY_BUFFER_MB} MB OS/display buffer).")
    return peak_used
