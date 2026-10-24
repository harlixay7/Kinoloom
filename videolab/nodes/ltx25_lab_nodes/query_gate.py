"""Lab-only: query-gated self-attention (Dynamic query routing gate, optimized).

Skipping *query rows* for low-saliency tokens while keeping K/V dense saves
attention FLOPs row-linearly (measured on this machine at the real shapes:
-39..44% of attention time at rho=0.55) without removing context tokens.

How it works
------------
1. A DIFFUSION_MODEL wrapper (same hook easycache/hidream use) sees the latent
   each step. It derives a per-token saliency map from temporal frame-difference
   energy (coarse 4x4 blocks + light feather blur, so the mask boundary is not a
   per-token feature discontinuity), thresholds it at the (1-rho) quantile and
   stores the keep-index list in transformer_options for the current step.
2. A module-level wrapper around comfy.ldm.modules.attention.optimized_attention
   reads that plan and computes attention only for the kept query rows; dropped
   rows keep the residual path (attention output = 0 for them). Guide/reference
   rows are always exempt: only the target video rows [0:guide_start) are gated,
   and the tracked-guide attention group is never touched.
3. The mask is computed once at `warmup_steps` (the first steps are pure noise -
   no structure to measure) and reused for the rest of that sampling run.

First-party lab node, shipped in videolab/nodes/ltx25_lab_nodes. Off by default; the measured result is negative.
"""
import math

import torch

import comfy.ldm.modules.attention as attention_module
import comfy.patcher_extension

_ORIG = attention_module.optimized_attention

# plan for the current step, written by the model wrapper, read by attention
_GATE: dict = {}


def _to_video_latent(x):
    if getattr(x, "is_nested", False):
        return x.tensors[0]
    if isinstance(x, list):
        return x[0]
    return x


def _saliency_scores(vx, t_target, rho, block, feather):
    """Temporal frame-difference energy per target token, block-aligned + feathered.

    vx: (B, C, T, H, W) combined latent (target frames first). Returns a 1-D
    tensor of `t_target * H * W` scores in the model's (f, h, w) token order
    (LTX-2.5 uses SymmetricPatchifier(1): one token per latent cell).
    """
    v = vx[0, :, :t_target]                       # (C, T, H, W)
    d = (v[:, 1:] - v[:, :-1]).abs().mean(dim=0)  # (T-1, H, W)
    if d.shape[0] < t_target:                     # keep T by repeating the last diff
        d = torch.cat([d, d[-1:]], dim=0)
    d = d.unsqueeze(0).unsqueeze(0)               # (1,1,T,H,W)
    # coarse blocks -> per-block mean, then feather with a box blur
    if block > 1:
        d = torch.nn.functional.avg_pool3d(d, kernel_size=(1, block, block),
                                           stride=(1, block, block))
        d = torch.nn.functional.interpolate(d, size=(t_target, vx.shape[3], vx.shape[4]),
                                            mode="nearest")
    if feather > 1:
        pad = feather // 2
        d = torch.nn.functional.avg_pool3d(
            torch.nn.functional.pad(d, (pad, pad, pad, pad, 0, 0), mode="replicate"),
            kernel_size=(1, feather, feather), stride=1)
    return d.reshape(-1)


def _gated_attention(q, k, v, heads, mask=None, attn_precision=None,
                     transformer_options=None, **kwargs):
    plan = None
    if isinstance(transformer_options, dict):
        plan = transformer_options.get("ltx25_query_gate")
    if plan is None:
        return _ORIG(q, k, v, heads, mask=mask, attn_precision=attn_precision,
                     transformer_options=transformer_options, **kwargs)
    gs = plan["guide_start"]
    total = k.shape[1]
    is_noisy_group = mask is None or (hasattr(mask, "shape") and mask.ndim == 4 and mask.shape[2] == 1)
    if not (total >= gs and is_noisy_group and (q.shape[1] == gs or q.shape[1] == total)):
        return _ORIG(q, k, v, heads, mask=mask, attn_precision=attn_precision,
                     transformer_options=transformer_options, **kwargs)
    keep = plan["keep"]
    if keep.numel() >= q.shape[1]:
        return _ORIG(q, k, v, heads, mask=mask, attn_precision=attn_precision,
                     transformer_options=transformer_options, **kwargs)
    if plan.get("debug") and not plan.get("_logged"):
        print(f"[LTX25 query-gate] gated call: q={tuple(q.shape)} k={tuple(k.shape)} "
              f"keep={keep.numel()}/{gs}", flush=True)
        plan["_logged"] = True
    out = torch.zeros_like(q)
    out[:, keep] = _ORIG(q[:, keep].contiguous(), k, v, heads, mask=mask,
                         attn_precision=attn_precision,
                         transformer_options=transformer_options, **kwargs)
    return out


def install_gated_attention():
    attention_module.optimized_attention = _gated_attention


def uninstall_gated_attention():
    attention_module.optimized_attention = _ORIG


class LTX25LabQueryGate:
    """Attach a query-row gate to a MODEL (lab experiment; see module docstring)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "rho": ("FLOAT", {"default": 0.55, "min": 0.05, "max": 1.0, "step": 0.05,
                                  "tooltip": "fraction of query rows kept per step"}),
                "warmup_steps": ("INT", {"default": 2, "min": 0, "max": 20,
                                         "tooltip": "compute the saliency mask at this step index and freeze it"}),
                "block": ("INT", {"default": 4, "min": 1, "max": 16,
                                  "tooltip": "coarse saliency block size (latent tokens)"}),
                "feather": ("INT", {"default": 3, "min": 1, "max": 9,
                                    "tooltip": "box blur width for the mask boundary"}),
                "enabled": ("BOOLEAN", {"default": True}),
                "debug": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply"
    CATEGORY = "LTX25_LAB"

    def apply(self, model, rho=0.55, warmup_steps=2, block=4, feather=3,
              enabled=True, debug=False):
        if not enabled or rho >= 1.0:
            print("[LTX25 query-gate] disabled", flush=True)
            return (model,)
        install_gated_attention()
        m = model.clone()
        state = {"last_t": None, "step": -1, "keep": None, "gs": None, "logged": False}
        dbg = debug

        def wrapper(executor, *args, **kwargs):
            x = args[0]
            t = args[1]
            t_val = float(t.flatten()[0]) if torch.is_tensor(t) else float(t)
            # LTX passes transformer_options positionally (args[5]); other models use
            # kwargs. Find the first dict in the positional args.
            topts = None
            for cand in args:
                if isinstance(cand, dict):
                    topts = cand
                    break
            if topts is None:
                topts = kwargs.get("transformer_options")
            if not isinstance(topts, dict):
                return executor(*args, **kwargs)
            # new run when sigma increases (sampler walks sigma down)
            if state["last_t"] is None or t_val > state["last_t"]:
                state["step"] = -1
                state["keep"] = None
            state["last_t"] = t_val
            state["step"] += 1
            vx = _to_video_latent(x)
            if vx.dim() == 5:
                b, c, tt, h, w = vx.shape
                total_tokens = tt * h * w
                # Guide/reference frames are appended at the end of the sequence;
                # their token count is keyframe_idxs.shape[2] (LTX passes it as the
                # 7th positional arg). Everything before that is gatable video.
                kf = args[6] if len(args) > 6 and torch.is_tensor(args[6]) else None
                guide_tokens = int(kf.shape[2]) if (kf is not None and kf.dim() >= 3) else 0
                if guide_tokens >= total_tokens:
                    guide_tokens = 0
                gs = total_tokens - guide_tokens
                t_target = max(1, gs // (h * w))
                if dbg and not state.get("shape_logged"):
                    print(f"[LTX25 query-gate] x={tuple(vx.shape)} "
                          f"keyframe_tokens={guide_tokens} total={total_tokens} "
                          f"gs={gs} t_target={t_target}", flush=True)
                    state["shape_logged"] = True
                # new run also when the sequence shape changes (stage 1 -> stage 2)
                if state["gs"] is not None and state["gs"] != gs:
                    state["keep"] = None
                    state["step"] = -1
                    state["logged"] = False
                state["gs"] = gs
                if state["keep"] is None and state["step"] >= warmup_steps:
                    scores = _saliency_scores(vx, t_target, rho, block, feather)
                    thr = torch.quantile(scores.float(), 1.0 - rho)
                    keep = torch.nonzero(scores >= thr, as_tuple=False).flatten()
                    if keep.numel() == 0:
                        keep = torch.arange(gs, device=scores.device)
                    state["keep"] = keep
                    state["gs"] = gs
                    if dbg:
                        print(f"[LTX25 query-gate] step={state['step']} t={t_val:.3f} "
                              f"vx={tuple(vx.shape)} t_target={t_target} gs={gs} "
                              f"keep={keep.numel()}/{gs} ({keep.numel()/max(gs,1):.2f})", flush=True)
                if state["keep"] is not None:
                    topts["ltx25_query_gate"] = {"guide_start": gs, "keep": state["keep"],
                                                 "debug": dbg}
                    if dbg and not state["logged"]:
                        print(f"[LTX25 query-gate] active: gs={gs} keep={state['keep'].numel()} "
                              f"rho_target={rho}", flush=True)
                        state["logged"] = True
                else:
                    topts.pop("ltx25_query_gate", None)
            return executor(*args, **kwargs)

        m.add_wrapper_with_key(comfy.patcher_extension.WrappersMP.DIFFUSION_MODEL,
                               "ltx25_query_gate", wrapper)
        print(f"[LTX25 query-gate] attached rho={rho} warmup={warmup_steps} "
              f"block={block} feather={feather} debug={debug}", flush=True)
        return (m,)


NODE_CLASS_MAPPINGS = {"LTX25LabQueryGate": LTX25LabQueryGate}
NODE_DISPLAY_NAME_MAPPINGS = {"LTX25LabQueryGate": "LTX25 Lab Query Gate"}
