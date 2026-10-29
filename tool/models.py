"""Canonical model registry — the single source of truth for every
generation feature (Images tab, Training wizard, Generate Photo).

Architecture:
    Model Registry -> Model Adapter -> Workflow Builder -> Generation Job

Adding a model = one registry entry + an adapter. The Training UI never hardcodes models.

Prompt handling uses model-aware adapters: a neutral base prompt is built
first, then the selected model's adapter transforms it (quality tags,
style suffixes, ordering, negatives). Style suffixes are ONLY applied in
"images" context — training datasets stay clean so style traits don't
leak into trained LoRAs.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from pathlib import Path
from typing import Any

import gpu_support
import model_files
from core import ROOT, load_config, log_setup

log = log_setup("models")

PERF_FILE = ROOT / "tool" / "perf_records.json"
_PERF_LOCK = threading.Lock()
PERF_WINDOW = 20          # keep last N measured durations per key
EST_FALLBACK = {          # seconds per 1024x1024 image, per model (start)
    "realvis": 150.0, "wan": 110.0,
    "qwen21": 215.0, "qwen21_fast": 80.0,
}

# ------------------------------------------------------------------ models
# Negative prompt per model family (SDXL base).
NEG_SDXL = ("ugly, deformed, bad anatomy, bad hands, extra fingers, "
            "blurry, low quality, watermark, text, jpeg artifacts")
NEG_WAN = ("blurry, deformed, distorted, warped, folded, overlapping "
           "features, low quality, soft, foggy, smear, extra person, "
           "watermark, text")
# Qwen-Image-2.1: the anti-paint/anti-CGI list that measured best on this
# model (docs/research/PROVEN_CONFIGS_QWEN21.md) — identity terms included because the
# native multi-reference path locks faces from roster stills.
NEG_QWEN = ("painting, illustration, cartoon, brush strokes, plastic texture, "
            "waxy skin, airbrushed, blurry, out of focus, low contrast, "
            "washed out, oversaturated, HDR halos, watermark, text, logo, "
            "extra fingers, deformed hands, distorted face, altered facial "
            "structure")

MODELS: dict[str, dict] = {
    "realvis": {
        "id": "realvis",
        "name": "RealVisXL V5",
        "description": "The photorealism specialist. Trained on photographs "
                       "— natural skin, believable faces, clean lighting.",
        "best_for": "Realistic people, portraits, any scene needing "
                    "photographic realism",
        "family": "sdxl",
        "architecture": "sdxl",
        "loader": "gguf",          # bare GGUF unet -> UnetLoaderGGUF
        "template": "sdxl_t2i",
        "files": {"unet": "RealVisXL_V5.0-Q5_K_M.gguf",
                  "clip_l": "clip_l.safetensors",
                  "clip_g": "clip_g.safetensors",
                  "vae": "xlVAEC_c91.safetensors"},
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "832x1216", "1216x832", "768x1344", "1344x768"],
        "steps": 28, "cfg": 6.5,
        "sampler": "dpmpp_2m", "scheduler": "karras",
        "quality_prefix": "", "style_suffix": "",
        "negative": NEG_SDXL,
        "capabilities": {"loras": True, "hires": True, "refs": True,
                         "sampler_override": True, "negative_override": True},
        "hint": "Strong photorealism out of the box. Detail boosters "
                "(Skin Texture V4, Add Details) stack well.",
    },

    # ---------------------------------------------------------------- Z-Image
    # Z-Image Turbo is a 6B single-stream DiT (Alibaba Tongyi). ComfyUI sees it
    # as image_model "lumina2" and the text encoder is a Qwen3-4B.
    #
    # ⚠ RTX 3070 / sm_86 constraints that shaped these entries — all verified
    # against the local ComfyUI source and the upstream file lists:
    #   * `ZImage.supported_inference_dtypes` is [bfloat16, float32] and fp16 is
    #     only added when `extended_fp16_support()` is true, which is FALSE on
    #     Ampere. So Z-Image runs bf16/fp32 here, never fp16.
    #   * The bf16 unet is 11.5 GB and the bf16 text encoder is 7.5 GB — neither
    #     fits 8 GB VRAM. The GGUF route (ComfyUI-GGUF lists `lumina2` in
    #     IMG_ARCH_LIST and `qwen3` in TXT_ARCH_LIST) is the only viable one.
    #   * The Civitai v8.0 `nvfp4` (4.2 GB) and `mxfp8` (6.6 GB) builds are
    #     Blackwell-class formats and are NOT runnable on sm_86. `fp8Mixed`
    #     (7.4 GB) has no native FP8 path on Ampere and gets upcast to bf16
    #     in memory, i.e. ~12 GB resident. Do not "fix" these entries to use them.
    #
    # Z-Image Turbo is distilled for 8 NFE and samples at cfg 1.0, so the
    # negative prompt is mathematically unused — ComfyUI's own template feeds
    # ConditioningZeroOut to KSampler. Hence negative_override: False, and the
    # UI must hide the negative-prompt control for this family.
    "zimage": {
        "id": "zimage",
        "name": "Z-Image Turbo",
        "description": "Tongyi-MAI's Z-Image Turbo (Q4_K_M GGUF), a 6B "
                       "single-stream DiT. Distilled to 8 steps, so it is the "
                       "fastest photoreal option here by a wide margin.",
        "best_for": "Fast photoreal keyframes, storyboards, rapid "
                    "regeneration, high-throughput stills",
        "family": "zimage",
        "architecture": "zimage",
        "loader": "gguf",          # lumina2 arch -> UnetLoaderGGUF
        "template": "z_image_t2i",
        "files": {"unet": "z_image_turbo-Q4_K_M.gguf",
                  "clip": "qwen_3_4b.safetensors",
                  "vae": "z_image_ae.safetensors"},
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "832x1216", "1216x832", "768x1344", "1344x768"],
        "steps": 8, "cfg": 1.0,
        "sampler": "res_multistep", "scheduler": "simple", "shift": 3.0,
        "quality_prefix": "", "style_suffix": "",
        "negative": "",
        "capabilities": {"loras": False, "hires": False, "refs": False,
                         "sampler_override": True, "negative_override": False},
        "hint": "8 steps at cfg 1.0 — no negative prompt (it is ignored at "
                "cfg 1). Long descriptive prompts work best; there is no "
                "quality-tag convention to follow.",
    },

    # ------------------------------------------------- CyberRealistic XL v10
    # A NATIVE SDXL checkpoint: UNet + CLIP + VAE in one 6.46 GB safetensors.
    # That is a genuinely different loader from every other SDXL entry here
    # (those are bare GGUF unets with separate CLIP/VAE), which is why this
    # entry declares `loader: "checkpoint"` and uses its own template.
    # Routing a checkpoint through UnetLoaderGGUF — or a bare unet through
    # CheckpointLoaderSimple — does not work; the registry tracks the
    # distinction explicitly rather than assuming one loader fits all.
    #
    # Provenance: v10.0 is the current release (Civitai model 312530, version
    # 2840768). Sourced from HuggingFace because civitai.com/api/download
    # requires an API token; the HF artefact's LFS oid
    # fd5e870b5bbce4bddeb64f4bb8e49c57f84ab793c0262a503f0123be435e667d is
    # byte-identical to Civitai's published SHA256.
    #
    # Recommended settings come from the model's own card (steps 25-30,
    # CFG 7-8, DPM++ 3M SDE Karras) — NOT recycled SDXL defaults.
    "cyberrealistic_xl": {
        "id": "cyberrealistic_xl",
        "name": "CyberRealistic XL v10",
        "description": "Native SDXL 1.0 checkpoint tuned for photorealistic "
                       "portraits, fashion and editorial framing. Ships its "
                       "own CLIP and VAE.",
        "best_for": "Cinematic keyframes, realistic people and environments, "
                    "editorial/fashion looks",
        "family": "sdxl",
        "architecture": "sdxl",
        "loader": "checkpoint",
        "template": "sdxl_ckpt_t2i",
        "files": {"ckpt": "cyberrealisticXL_v100_fp16.safetensors"},
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "832x1216", "1216x832", "768x1344", "1344x768"],
        "steps": 28, "cfg": 7.5,
        "sampler": "dpmpp_3m_sde", "scheduler": "karras",
        "quality_prefix": "", "style_suffix": "",
        "negative": NEG_SDXL,
        "capabilities": {"loras": True, "hires": False, "refs": True,
                         "sampler_override": True, "negative_override": True},
        "hint": "Short prompts work — it does not need the long tag soup SDXL "
                "usually wants. 25-30 steps at CFG 7-8. Pairs with the same "
                "detail LoRAs as RealVisXL. Hires is off for this entry: the "
                "existing sdxl_hires template is wired for a bare GGUF unet "
                "and would need a checkpoint variant before it can be used.",
    },

    # ---------------------------------------------------------- Qwen-Image-2.1
    # 7.1B single-stream DiT + Qwen3-VL 8B ConvRot INT8 text encoder + native
    # 64-channel RGBA VAE (16x compression). Runs on the dedicated
    # ComfyUI_Qwen21 instance (ComfyUI v0.37.4, port 8190) — the production
    # v0.30 farm cannot load the 2.1 VAE/text encoder, and the farm is pinned
    # by hard rule. Proven settings were measured on this exact stack
    # (docs/research/PROVEN_CONFIGS_QWEN21.md): quality mode 40 steps @ CFG 3.5 with a REAL
    # negative (the model is NOT guidance-distilled at base — community and
    # lab evidence both show CFG 1.0 leaves it flat), euler/simple, and a
    # 4x-UltraSharp + lanczos finish instead of a latent re-denoise pass
    # (the repaint pass measured -13% foreground sharpness and a painted look).
    # References are NATIVE identity latents (core TextEncodeQwenImage21),
    # which outperforms IP-Adapter for this family — faces lock to roster
    # stills. Both entries run on their own instance via comfy_port.
    "qwen21": {
        "id": "qwen21",
        "name": "Qwen-Image 2.1 (quality)",
        "description": "Photoreal film-still generator: 7.1B DiT at Q5_K_M, "
                       "Qwen3-VL 8B text encoder, native RGBA output. Best "
                       "prompt fidelity and native multi-reference identity "
                       "in the studio.",
        "best_for": "Photoreal keyframes, character-accurate scenes from "
                    "roster stills, cinematic 16:9 frames for video refs",
        "family": "qwen21",
        "architecture": "qwen21",
        "loader": "gguf",
        "template": "qwen21_t2i",
        "files": {"unet": "qwen_image_2.1_Q5_K_M.gguf",
                  "clip": "qwen3vl_8b_int8_convrot.safetensors",
                  "vae": "qwen_image_2.1_vae_bf16_compact.safetensors",
                  "upscale": "4x-UltraSharp.pth"},
        "comfy_port": 8190,
        "default_size": "1344x768",
        "sizes": ["1024x1024", "1344x768"],
        "steps": 40, "cfg": 3.5,
        "sampler": "euler", "scheduler": "simple",
        "quality_prefix": "", "style_suffix": "",
        "negative": NEG_QWEN,
        "capabilities": {"loras": False, "hires": False, "refs": True,
                         "sampler_override": True, "negative_override": True},
        "hint": "Long photographic prompts work best: subject, environment, "
                "lighting, optics, then exclusions in prose — no tag soup. "
                "Native reference images lock faces to roster stills (1-4 "
                "refs, auto-downscaled to 512 px). ~215 s per 2K image "
                "(measured on the RTX 3070).",
    },
    "qwen21_fast": {
        "id": "qwen21_fast",
        "name": "Qwen-Image 2.1 (fast, DMD 8-step)",
        "description": "Same Qwen-Image-2.1 base with the Pruna DMD 8-step "
                       "adapter baked in as a LoRA. Measured equal foreground "
                       "sharpness to the 40-step mode; slightly flatter "
                       "sky/mood drama.",
        "best_for": "Rapid iteration, drafts, batch stills — 2K in under "
                    "90 seconds",
        "family": "qwen21",
        "architecture": "qwen21",
        "loader": "gguf",
        "template": "qwen21_t2i",
        "files": {"unet": "qwen_image_2.1_Q5_K_M.gguf",
                  "clip": "qwen3vl_8b_int8_convrot.safetensors",
                  "vae": "qwen_image_2.1_vae_bf16_compact.safetensors",
                  "lora": "p_qwen_image_2.1_8step_v0.1.safetensors",
                  "upscale": "4x-UltraSharp.pth"},
        "comfy_port": 8190,
        "default_size": "1344x768",
        "sizes": ["1024x1024", "1344x768"],
        "steps": 8, "cfg": 1.0,
        "sampler": "euler", "scheduler": "simple",
        "quality_prefix": "", "style_suffix": "",
        "negative": "",
        "variant_of": "qwen21",
        "capabilities": {"loras": False, "hires": False, "refs": True,
                         "sampler_override": True, "negative_override": False},
        "hint": "8 steps at cfg 1.0 — negative prompt is ignored. Same "
                "native reference support as the quality mode. ~50-90 s per "
                "2K image (measured).",
    },

    # ------------------------------------------------------------ Wan (legacy)
    # Wan 2.2 TI2V-5B rendering a single frame: the studio's first image and
    # video model, kept for old datasets and projects and listed last. Weaker
    # than every other model here. The dataset generator still maps the old
    # "standard"/"fastwan" stack names to it (dataset_gen.start), so it must
    # stay registered.
    "wan": {
        "id": "wan",
        "name": "Wan 2.2 (legacy)",
        "description": "The studio's first generator, kept for old datasets. "
                       "Not as good as the other models: softer faces and "
                       "detail, not photoreal.",
        "best_for": "Nothing new: pick RealVisXL, Z-Image or Qwen-Image instead",
        "family": "wan",
        "architecture": "wan",
        "loader": "gguf",
        "template": "wan2.2_t2i_5b",
        "files": {"unet": "Wan2.2-TI2V-5B-Q4_K_M.gguf", "lora": "T2I_LORA"},
        "default_size": "1024x1024",
        "sizes": ["1024x1024", "832x1216", "1216x832", "768x768"],
        "steps": 20, "cfg": 1.0,
        "sampler": "euler", "scheduler": "simple",
        "quality_prefix": "", "style_suffix": "",
        "negative": NEG_WAN,
        "capabilities": {"loras": False, "hires": False,
                         "sampler_override": False, "negative_override": True},
        "hint": "Legacy, not recommended: RealVisXL, Z-Image and Qwen-Image "
                "give clearly better results. LoRAs and hires do not apply "
                "to Wan.",
    },
}

# generation LoRAs (user-dropped files in models/loras)
GEN_LORAS: dict[str, dict] = {
    "skin_texture_v4.safetensors": {
        "label": "Skin Texture V4",
        "primary": "skin texture style",
        "trigger": "skin texture style, detailed skin, detailed skin pore, "
                   "realistic skin",
        "strength": 0.7,
        "note": "THE anti-smooth-skin fix: pores, grain, photographic skin "
                "texture. Use 0.5-0.9. Works on any SDXL model.",
    },
    "add_details_xl.safetensors": {
        "label": "Add Details XL",
        "primary": "",
        "trigger": "",
        "strength": 0.5,
        "note": "General detail/definition booster — no trigger needed. "
                "0.3-0.7, stack with Skin Texture.",
    },
    "perfection_style.safetensors": {
        "label": "Detailed Perfection",
        "primary": "perfection style",
        "trigger": "perfection style",
        "strength": 0.5,
        "note": "Hands, feet, face, body anatomy detail. 0.4-0.7.",
    },
}

LORA_TRIGGER_EXTRA = ("watermark, web address, artist name, signature, "
                      "username, patreon")


def model_ids() -> list[str]:
    return list(MODELS.keys())


def get_model(mid: str) -> dict | None:
    return MODELS.get(mid)


def gpu_problem(mid: str) -> str:
    """Why this PC's card or driver cannot run the model's ComfyUI build
    (gpu_support: Qwen-Image 2.1 on 8190 needs CUDA 13.0, the rest of the
    image models run on the render farm), or ""."""
    m = MODELS.get(mid) or {}
    return gpu_support.port_problem(m.get("comfy_port"))


def has_weights(mid: str) -> bool:
    """Is the model's primary weight file actually on disk?

    The check depends on the declared loader, because a checkpoint and a bare
    unet live in different directories and are loaded by different nodes:
      * loader "gguf"       -> models/diffusion_models/<unet>
      * loader "checkpoint" -> models/checkpoints/<ckpt>
    The file counts in the studio's own models/ folder and, as for the video
    models, in a model folder of settings paths.extra_model_dirs
    (tool/model_files.py), which every ComfyUI instance mounts.
    """
    m = MODELS.get(mid)
    if not m:
        return False
    files = m["files"]
    cfg = load_config()
    if m.get("loader") == "checkpoint":
        ck = files.get("ckpt")
        return bool(ck) and model_files.exists(f"models/checkpoints/{ck}", cfg, ROOT)
    for key in ("unet",):
        if key in files:
            if not model_files.exists(f"models/diffusion_models/{files[key]}", cfg, ROOT):
                return False
    return True


def is_available(mid: str) -> bool:
    """The model can render here: its weights are installed and this PC's
    card and driver run its ComfyUI build."""
    return has_weights(mid) and not gpu_problem(mid)


def model_info(mid: str) -> dict:
    """Serializable info for the UI (registry consumed from the backend —
    no duplicated model lists in the frontend). `gpu`: why this PC's card
    cannot run it, when that is what makes it unavailable."""
    m = MODELS.get(mid)
    if not m:
        return {}
    info = dict(m)
    info["gpu"] = gpu_problem(mid)
    info["available"] = not info["gpu"] and has_weights(mid)
    info["estimate_s"] = estimate_seconds(mid)
    return info


def all_model_info() -> list[dict]:
    return [model_info(mid) for mid in model_ids()]


# The model the start-picture pickers open on (the Shots page's Start
# pictures and a shot's picture studio): Qwen-Image 2.1 fast, the image model
# the installer sets up and the one the opening frames use (it never
# downloads RealVisXL). When it is not installed, the first installed model
# in this order.
STILL_MODELS = ("qwen21_fast", "qwen21", "realvis", "zimage", "cyberrealistic_xl")


def default_still_model() -> str:
    return next((mid for mid in STILL_MODELS if is_available(mid)), STILL_MODELS[0])


# ----------------------------------------------------------- prompt adapter
def adapt_prompt(mid: str, base_prompt: str, context: str = "images",
                 user_prompt: str | None = None,
                 user_negative: str | None = None) -> tuple[str, str]:
    """Build the final (prompt, negative) for a model.

    context: 'images' (studio tab — style suffixes allowed) or 'training'
    (datasets — style suffixes excluded so traits don't leak into LoRAs).
    user_prompt: full override — used as-is, with the quality prefix
    deduped-in (never force style into user text).
    """
    m = MODELS.get(mid) or MODELS["realvis"]
    neg = user_negative if user_negative is not None else m["negative"]
    if user_prompt:
        prompt = user_prompt
        if m["quality_prefix"] and m["quality_prefix"].strip().rstrip(", ") \
                not in prompt:
            prompt = m["quality_prefix"] + prompt
        return prompt, neg
    parts = []
    if m["quality_prefix"]:
        parts.append(m["quality_prefix"].rstrip(", "))
    parts.append(base_prompt)
    if context == "images" and m["style_suffix"]:
        parts.append(m["style_suffix"])
    return ", ".join(p for p in parts if p), neg


# ------------------------------------------------------ workflow building
def build_workflow(mid: str, prompt: str, negative: str, seed: int,
                   width: int, height: int, steps: int, cfg: float,
                   loras: list | None = None, hires: bool = False,
                   shot_id: str = "img") -> dict:
    """One code path for every model. Returns the built
    ComfyUI API workflow dict for the primary render (hires handled by
    the job engine via build_hires_workflow)."""
    from workflows import WorkflowBuilder
    m = MODELS.get(mid)
    if not m:
        raise ValueError(f"unknown model {mid}")
    b = WorkflowBuilder()
    extra = {"SHOT_ID": shot_id, "CFG": cfg}
    files = m["files"]
    loader = m.get("loader", "gguf")

    # --- weights: the loader decides which node and which tokens are used ---
    if m["family"] == "sdxl" and loader == "checkpoint":
        # one file carries UNet + CLIP + VAE -> CheckpointLoaderSimple
        extra["MODEL_CKPT"] = files["ckpt"]
    elif m["family"] == "sdxl":
        # bare GGUF unet + separate CLIP pair + separate VAE
        extra["MODEL_SDXL"] = files["unet"]
        extra["CLIP_L"] = files["clip_l"]
        extra["CLIP_G"] = files["clip_g"]
        extra["VAE_SDXL"] = files["vae"]

    if m["family"] == "sdxl":
        # Shared by BOTH SDXL loaders: the GGUF-unet and the native-checkpoint
        # templates expose the same six XL_LORA slots and the same sampler
        # tokens, so this block must not be duplicated per loader.
        if m.get("sampler"):
            extra["SAMPLER"] = m["sampler"]
        if m.get("scheduler"):
            extra["SCHEDULER"] = m["scheduler"]
        for slot, l in enumerate((loras or [])[:6], start=1):
            extra[f"XL_LORA{slot}"] = l["file"]
            try:
                extra[f"XL_LORA{slot}_STR"] = float(l.get("strength", 0.7))
            except (TypeError, ValueError):
                log.warning(f"bad LoRA strength {l.get('strength')!r} "
                            f"for {l.get('file', '?')} — using 0.7")
                extra[f"XL_LORA{slot}_STR"] = 0.7
    elif m["family"] == "zimage":
        # Z-Image Turbo: 6B lumina2-arch DiT, Qwen3-4B text encoder.
        # Sampling recipe is the model's own, not an SDXL leftover — see the
        # comment above MODELS["zimage"] for why these values and not others.
        extra["Z_UNET"] = files["unet"]
        extra["Z_CLIP"] = files["clip"]
        extra["Z_VAE"] = files["vae"]
        if m.get("sampler"):
            extra["Z_SAMPLER"] = m["sampler"]
        if m.get("scheduler"):
            extra["Z_SCHEDULER"] = m["scheduler"]
        if m.get("shift") is not None:
            extra["Z_SHIFT"] = float(m["shift"])
    elif m["family"] == "qwen21":
        # Qwen-Image-2.1 on the dedicated ComfyUI_Qwen21 instance (port 8190).
        # The registry entry carries comfy_port; images.py routes the job.
        # QW21_LORA "none" (quality mode) is stripped by the builder's
        # "none"-gate; fast mode passes the Pruna DMD 8-step adapter.
        extra["QW21_UNET"] = files["unet"]
        extra["QW21_CLIP"] = files["clip"]
        extra["QW21_VAE"] = files["vae"]
        extra["QW21_UPSCALE"] = files.get("upscale", "4x-UltraSharp.pth")
        extra["QW21_LORA"] = files.get("lora", "none")
        if m.get("sampler"):
            extra["QW21_SAMPLER"] = m["sampler"]
        if m.get("scheduler"):
            extra["QW21_SCHEDULER"] = m["scheduler"]
    elif m["family"] == "wan":
        extra["T2I_LORA"] = "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors"
    wf = b.build(m["template"], prompt=prompt, negative=negative,
                 seed=seed, width=width, height=height, steps=steps,
                 frames=1, extra=extra)
    if m["family"] == "qwen21":
        try:
            if float(cfg) <= 1.0:
                _prune_inert_negative(wf)
        except (TypeError, ValueError):
            pass
        # The text-encode nodes only consume the VAE for reference latents
        # (build_ref_workflow re-wires it). Without this, the 0.64 GB VAE is
        # resident during the whole denoise loop and squeezes the DiT out of
        # VRAM — which flips ComfyUI-GGUF into per-step CPU dequantization.
        for node in wf.values():
            if node.get("class_type", "").startswith("TextEncodeQwen"):
                (node.get("inputs") or {}).pop("vae", None)
    return wf


def _prune_inert_negative(wf: dict) -> dict:
    """At CFG <= 1.0 the negative branch is never evaluated, but the CPU text
    encode that feeds it still runs — ~40-55 s of AVX-512 work per cold Qwen
    generation (measured). Rewire KSampler to the positive conditioning and
    drop the dead encode node; never remove a node the positive branch uses."""
    neg_ids: set[str] = set()
    pos_ids: set[str] = set()
    for node in wf.values():
        if node.get("class_type") != "KSampler":
            continue
        for key, into in (("negative", neg_ids), ("positive", pos_ids)):
            link = (node.get("inputs") or {}).get(key)
            if isinstance(link, (list, tuple)) and link:
                into.add(str(link[0]))
    dead = neg_ids - pos_ids
    changed = False
    for nid in list(dead):
        node = wf.get(nid) or {}
        if str(node.get("class_type", "")).startswith("TextEncode"):
            wf.pop(nid, None)
            changed = True
    if not changed:
        return wf
    for node in wf.values():
        if node.get("class_type") != "KSampler":
            continue
        inputs = node.get("inputs") or {}
        link = inputs.get("negative")
        if isinstance(link, (list, tuple)) and link and str(link[0]) in dead:
            inputs["negative"] = inputs.get("positive")
    log.info("qwen21: dropped inert negative encode (cfg<=1.0)")
    return wf


def build_hires_workflow(mid: str, prompt: str, negative: str, seed: int,
                         cfg: float, src_w: int, src_h: int,
                         image_file: str, shot_id: str = "hr") -> dict:
    """RealESRGAN upscale + low-denoise refine for SDXL-family models."""
    from workflows import WorkflowBuilder
    m = MODELS.get(mid)
    if not m or m["family"] != "sdxl":
        raise ValueError("hires requires an SDXL-family model")
    if m.get("loader") == "checkpoint":
        # The sdxl_hires template is built around a bare GGUF unet plus
        # separate CLIP/VAE tokens. A monolithic checkpoint has none of those,
        # so refuse loudly instead of emitting a graph with unset tokens.
        # capabilities.hires is False for these entries so the UI hides the
        # control; this is the backstop if it is ever called directly.
        raise ValueError(
            f"hires is not wired for checkpoint-loader models ({mid}); "
            f"it needs a sdxl_ckpt_hires template first")
    scale = 1.375
    w2 = int(round(src_w * scale / 64) * 64)
    h2 = int(round(src_h * scale / 64) * 64)
    mx = max(w2, h2)
    if mx > 1408:
        f = 1408 / mx
        w2 = int(round(w2 * f / 64) * 64)
        h2 = int(round(h2 * f / 64) * 64)
    b = WorkflowBuilder()
    files = m["files"]
    extra = {"SHOT_ID": shot_id, "CFG": cfg, "HIRES_IMAGE": image_file,
             "HIRES_W": w2, "HIRES_H": h2,
             "MODEL_SDXL": files["unet"], "CLIP_L": files["clip_l"],
             "CLIP_G": files["clip_g"], "VAE_SDXL": files["vae"]}
    if m.get("sampler"):
        extra["SAMPLER"] = m["sampler"]
    if m.get("scheduler"):
        extra["SCHEDULER"] = m["scheduler"]
    return b.build("sdxl_hires", prompt=prompt, negative=negative,
                   seed=seed, width=0, height=0, steps=0, frames=1,
                   extra=extra)


IPADAPTER_FILE = "ip-adapter-plus_sdxl_vit-h.safetensors"
CLIP_VISION_FILE = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
IPADAPTER_PRESET = "PLUS (high strength)"
REF_MAX = 5


def build_ref_workflow(mid: str, prompt: str, negative: str, seed: int,
                       width: int, height: int, steps: int, cfg: float,
                       loras: list | None, ref_images: list[str],
                       ref_strength: float, anchor: bool,
                       shot_id: str = "img") -> dict:
    """Identity-anchored batch generation (IP-Adapter).

    Builds the standard SDXL workflow, then appends:
      CLIPVisionLoader -> IPAdapterUnifiedLoader(PLUS) -> ImageBatch chain
      (all reference images) -> IPAdapterBatch (weight=ref_strength)
    and optionally 'anchor mode': ref #1 as the img2img start latent at
    denoise 0.45 instead of an empty latent (preserves the ref's base
    composition and double-locks identity).
    """
    from workflows import WorkflowBuilder
    m = MODELS.get(mid)
    if m and m["family"] == "qwen21":
        # Qwen-Image-2.1: NATIVE identity-latent references (no IP-Adapter).
        # The pack's TextEncodeQwenImage21CPU node (positive prompt) splices
        # the uploaded refs into the conditioning as VAE latents (inputs
        # image1..image4, resized to about 512x512 pixels of area). Proven
        # on the Sara/Anna/palace arena composition.
        return _build_qwen21_ref_workflow(
            m, prompt, negative, seed, width, height, steps, cfg,
            ref_images, shot_id)
    m = MODELS.get(mid)
    if not m or m["family"] != "sdxl":
        raise ValueError("reference images require an SDXL-family model")
    if not ref_images or len(ref_images) > REF_MAX:
        raise ValueError(f"need 1-{REF_MAX} reference images")
    b = WorkflowBuilder()
    base = build_workflow(mid, prompt, negative, seed, width, height,
                          steps, cfg, loras, hires=False, shot_id=shot_id)
    # locate the sampler + clip encode nodes
    kid = next(nid for nid, n in base.items()
               if n["class_type"] == "KSampler")
    nid = max(int(x) for x in base.keys())
    wf = base
    model_in = wf[kid]["inputs"]["model"]
    # IP adapter stack
    nid += 1
    wf[str(nid)] = {"class_type": "CLIPVisionLoader",
                    "inputs": {"clip_name": CLIP_VISION_FILE}}
    vision_id = str(nid)
    nid += 1
    wf[str(nid)] = {"class_type": "IPAdapterUnifiedLoader",
                    "inputs": {"model": model_in,
                               "preset": IPADAPTER_PRESET}}
    unified_id = str(nid)
    # image batch chain
    img_src = None
    for i, ref in enumerate(ref_images):
        nid += 1
        wf[str(nid)] = {"class_type": "LoadImage",
                        "inputs": {"image": ref}}
        cur = str(nid)
        if img_src is None:
            img_src = [cur, 0]
        else:
            nid += 1
            wf[str(nid)] = {"class_type": "ImageBatch",
                            "inputs": {"image1": img_src,
                                       "image2": [cur, 0]}}
            img_src = [str(nid), 0]
    nid += 1
    wf[str(nid)] = {"class_type": "IPAdapterBatch",
                    "inputs": {"model": [unified_id, 0],
                               "ipadapter": [unified_id, 1],
                               "image": img_src,
                               "weight": float(ref_strength),
                               "weight_type": "linear",
                               "start_at": 0.0, "end_at": 1.0,
                               "embeds_scaling": "V only",
                               "encode_batch_size": 0,
                               "clip_vision": [vision_id, 0]}}
    ipa_id = str(nid)
    wf[kid]["inputs"]["model"] = [ipa_id, 0]
    if anchor:
        # ref #1 becomes the start latent at low denoise (scaled to target aspect ratio)
        nid += 1
        scale_id = str(nid)
        first_img = next(n for n, nd in wf.items() if nd["class_type"] == "LoadImage")
        wf[scale_id] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": [first_img, 0],
                "upscale_method": "bicubic",
                "width": width,
                "height": height,
                "crop": "center",
            }
        }
        nid += 1
        wf[str(nid)] = {"class_type": "VAEEncode",
                        "inputs": {"pixels": [scale_id, 0],
                                   "vae": [next(
                                       n for n, nd in wf.items()
                                       if nd["class_type"] == "VAELoader"), 0]}}
        wf[kid]["inputs"]["latent_image"] = [str(nid), 0]
        wf[kid]["inputs"]["denoise"] = 0.45
    return wf


def _build_qwen21_ref_workflow(m: dict, prompt: str, negative: str,
                               seed: int, width: int, height: int,
                               steps: int, cfg: float,
                               ref_images: list[str],
                               shot_id: str = "img") -> dict:
    """Qwen-Image-2.1 native multi-reference graph.

    Builds the standard qwen21 workflow, then wires the already-uploaded
    reference images into the positive conditioning node as identity
    latents (the pack's TextEncodeQwenImage21CPU, inputs image1..image4,
    resized to about 512x512 pixels of area).
    1-4 references.
    """
    if not ref_images or len(ref_images) > 4:
        raise ValueError("Qwen-Image-2.1 accepts 1-4 reference images")
    wf = build_workflow(m["id"], prompt, negative, seed, width, height,
                        steps, cfg, loras=None, hires=False, shot_id=shot_id)
    # the positive conditioning node is the only TextEncodeQwenImage21CPU
    # with measure_dma enabled (the negative encode runs with it off)
    te = next(nid for nid, n in wf.items()
              if n["class_type"] == "TextEncodeQwenImage21CPU"
              and n["inputs"].get("measure_dma"))
    # references need the VAE again (build_workflow strips it for the
    # no-reference path so the VAE does not sit in VRAM during sampling)
    vae_id = next((nid for nid, n in wf.items()
                   if n["class_type"] == "VAELoader"), None)
    if vae_id is not None:
        wf[te]["inputs"]["vae"] = [str(vae_id), 0]
    nid = max(int(x) for x in wf.keys())
    for i, ref in enumerate(ref_images[:4], start=1):
        nid += 1
        wf[str(nid)] = {"class_type": "LoadImage",
                        "inputs": {"image": ref}}
        wf[te]["inputs"][f"image{i}"] = [str(nid), 0]
    return wf


# ------------------------------------------------------- performance store
def _load_perf() -> dict:
    if PERF_FILE.exists():
        try:
            data = json.loads(PERF_FILE.read_text(encoding="utf-8"))
            # guard against corrupted keys (a bug once stored full workflow
            # dicts as keys) — drop anything that isn't a sane perf key.
            # hires keys no longer carry a steps component; fold any legacy
            # step-bearing hires samples into the canonical key.
            import re as _re
            sane = _re.compile(r"^[a-z0-9_]+:\d+x\d+:(?:\d+:)?[hn]$")
            old_hires = _re.compile(r"^[a-z0-9_]+:\d+x\d+:\d+:h$")
            out: dict[str, list] = {}
            for k, v in data.items():
                if not isinstance(v, list):
                    continue
                if old_hires.match(k):
                    k = k.rsplit(":", 2)[0] + ":h"
                if sane.match(k):
                    out.setdefault(k, []).extend(v)
            return {k: v[-PERF_WINDOW:] for k, v in out.items()}
        except Exception:
            pass
    return {}


def _save_perf(data: dict) -> None:
    tmp = PERF_FILE.with_name(PERF_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, PERF_FILE)


def perf_key(mid: str, res: str, steps: int, hires: bool) -> str:
    if hires:
        return f"{mid}:{res}:h"
    return f"{mid}:{res}:{steps}:n"


def record_performance(mid: str, res: str, steps: int, hires: bool,
                       seconds: float) -> None:
    if seconds <= 0:
        return
    with _PERF_LOCK:
        data = _load_perf()
        key = perf_key(mid, res, steps, hires)
        times = data.get(key, [])
        times.append(round(seconds, 1))
        data[key] = times[-PERF_WINDOW:]
        _save_perf(data)


def estimate_seconds(mid: str, res: str = "1024x1024", steps: int | None = None,
                     hires: bool = False) -> float:
    """Rolling median of recent real generations; static fallback only
    when nothing has been measured yet."""
    m = MODELS.get(mid)
    if not m:
        return 120.0
    key = perf_key(mid, res, steps if steps is not None else m["steps"], hires)
    times = _load_perf().get(key, [])
    if times:
        return float(statistics.median(times))
    # resolution-scaled fallback
    try:
        w, h = map(int, res.lower().split("x"))
    except (ValueError, AttributeError, TypeError):
        return float(EST_FALLBACK.get(mid, 120.0))
    base = EST_FALLBACK.get(mid, 120.0)
    return round(base * (w * h) / (1024 * 1024), 1)


def estimate_batch(models: list[dict]) -> float:
    """models: [{model, count, steps?, hires?}] -> total seconds."""
    total = 0.0
    for row in models:
        total += estimate_seconds(row["model"], row.get("res", "1024x1024"),
                                  row.get("steps"), bool(row.get("hires"))) \
            * int(row.get("count", 0))
    return total
