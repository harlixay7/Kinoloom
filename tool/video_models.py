"""Centralised VIDEO model registry — the single source of truth for the
Video Studio.

The movie pipeline reads it too: `orchestrator._video_family` resolves every
project shot's model here, and a project with no usable model renders on
`default_model_id()` (LTX-2.5 s6 CQ). Wan 2.2 is a legacy entry: it renders
only when a project or shot names it explicitly, never as a fallback.

Design notes
------------
* Every entry declares what it can actually do (`supported_modes`,
  `supported_loras`, `supported_controls`). The UI must read these and hide
  controls a model does not support, rather than showing dead widgets.
* `workflow` names a real template in `tool/workflows/`. An entry whose
  template does not exist is reported as `status: "missing"` rather than
  being silently offered: a model is never claimed as available while its
  workflow is missing.
* `vram_estimate_mb` values are *estimates for planning*, not measurements.
  Anything actually measured on this machine is recorded in
  `temp/bench/results.jsonl` by `tool/scripts/bench.py` and must be quoted
  from there, not from this file.
"""
from __future__ import annotations

import re
from pathlib import Path

import gpu_support
import model_files
from core import ROOT, load_config

WF_DIR = ROOT / "tool" / "workflows"

# ---------------------------------------------------------------- defaults
# Wan 2.2 5B sampling. 8 steps @ cfg 1.0 is the established fast recipe this
# studio already uses (settings.yaml tiers.steps = 8 for every tier).
WAN_DEFAULTS = {
    "steps": 8, "cfg": 1.0,
    "sampler": "euler", "scheduler": "simple",
    "shift": 8.0,                 # ModelSamplingSD3 shift, matches SUBST
}

VIDEO_MODELS: dict[str, dict] = {
    # ==================================================== Wan 2.2 I2V-A14B
    # The OFFICIAL Alibaba 14B image-to-video model (not a community merge).
    # Weights live in models/_wan14b/ so the whole experiment can be removed
    # by deleting that folder + its block in ComfyUI/extra_model_paths.yaml.
    #
    # ⚠ MoE: two experts at 8.15 GB each = 16.3 GB against 8 GB VRAM. Runs only
    #   by streaming blocks from system RAM. The single biggest optimisation is
    #   the T5 encoder on CPU (forced below) — ~9 GB freed for a cost paid ONCE
    #   per prompt, not per step.
    "wan22_i2v_a14b": {
        "id": "wan22_i2v_a14b",
        "display_name": "Wan 2.2 I2V-A14B (Q4_K_S, MoE)",
        "catalog": {"group": "wan", "label": "I2V-A14B · Q4_K_S (MoE)", "detail": "RAM offload, slow", "rank": 40},
        "family": "wan_a14b",
        "version": "2.2",
        "path": "models/_wan14b/diffusion_models/Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf",
        "format": "gguf",
        "quantization": "Q4_K_S",
        "moe": True,
        "unet_high": "models/_wan14b/diffusion_models/Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf",
        "unet_low": "models/_wan14b/diffusion_models/Wan2.2-I2V-A14B-LowNoise-Q4_K_S.gguf",
        "studio_workflow": "wan2.2_i2v_a14b",
        "workflow": "wan2.2_i2v_a14b",
        "workflow_version": 1,
        "text_encoder": "models/text_encoders/umt5-xxl-encoder-Q4_K_M.gguf",
        "vae": "models/vae/Wan2.1_VAE.safetensors",
        "supported_modes": ["i2v"],
        "supported_loras": ["wan22_a14b"],
        "supported_controls": ["prompt", "negative", "seed", "resolution",
                               "frames", "steps", "sampler", "cfg", "start_image"],
        "defaults": {"steps": 8, "cfg": 1.0, "sampler": "euler",
                     "scheduler": "simple", "shift": 5.0},
        "resolution_limits": {"min": [256, 256], "max": [1280, 720],
                              "step": 16,
                              "native": ["832x480", "512x288",
                                         "1024x576", "1280x720"]},
        "frame_limits": {"min": 5, "max": 121, "step": 4},
        "recommended_frames": 81,
        "fps": 24,
        "vram_estimate_mb": None,
        "vram_note": "16.3 GB of weights on 8 GB VRAM; runs only via RAM offload. NOT measured here.",
        "source": "models/_wan14b (isolated; delete folder to remove)",
        "experimental": True,
        "declared_status": "candidate",
        "note": "Official Wan 2.2 I2V-A14B. 4-step LightX2V LoRAs. Expect several minutes per clip.",
    },

    # ================================================================ legacy
    # Wan 2.2 TI2V-5B: the studio's first video model, kept for old projects.
    # Weaker than LTX-2.5 and MiniMax H3 (832x480 before upscaling, softer
    # faces and detail). The movie pipeline renders it only when a project or
    # shot picks this entry: through the wan2.2_*_dynamic templates and
    # workflows.SUBST, the same weights this entry gives the Video Studio.
    "wan22_5b_q4": {
        "id": "wan22_5b_q4",
        "display_name": "Wan 2.2 TI2V-5B (Q4_K_M) — legacy, not recommended",
        "catalog": {"group": "wan", "label": "TI2V-5B · Q4_K_M (legacy)", "detail": "legacy · 832x480, weaker than LTX-2.5 / H3", "rank": 10},
        "family": "wan22_ti2v_5b",
        "version": "2.2",
        "path": "models/diffusion_models/Wan2.2-TI2V-5B-Q4_K_M.gguf",
        "format": "gguf",
        "quantization": "Q4_K_M",
        "workflow": "wan2.2_ti2v_5b",
        "workflow_version": 1,
        "text_encoder": "models/text_encoders/umt5-xxl-encoder-Q4_K_M.gguf",
        "vae": "models/vae/wan2.2_vae.safetensors",
        "supported_modes": ["t2v", "i2v"],
        "supported_loras": ["wan22_5b"],
        "supported_controls": ["prompt", "negative", "seed", "resolution",
                               "frames", "steps", "sampler", "cfg", "loras",
                               "start_image", "cache"],
        "defaults": dict(WAN_DEFAULTS),
        "resolution_limits": {"min": [256, 256], "max": [1280, 720],
                              "step": 16,
                              "native": ["832x480", "512x288", "768x432"]},
        "frame_limits": {"min": 5, "max": 121, "step": 4},
        "vram_estimate_mb": 7000,
        "experimental": False,
        "status": "legacy",
        "source": "QuantStack/Wan2.2-TI2V-5B-GGUF",
        "note": "Legacy: the studio's first video model, kept for old "
                "projects. Not as good as LTX-2.5 or MiniMax H3 (832x480, "
                "softer faces and detail) and never chosen automatically. "
                "Same unet the Images tab's `wan` entry uses.",
    },
    "wan22_5b_q5": {
        "id": "wan22_5b_q5",
        "display_name": "Wan 2.2 TI2V-5B (Q5_K_M)",
        "catalog": {"group": "wan", "label": "TI2V-5B · Q5_K_M", "detail": "832x480 + post upscale", "rank": 20},
        "family": "wan22_ti2v_5b",
        "version": "2.2",
        "path": "models/diffusion_models/Wan2.2-TI2V-5B-Q5_K_M.gguf",
        "format": "gguf",
        "quantization": "Q5_K_M",
        "workflow": "wan2.2_ti2v_5b",
        "workflow_version": 1,
        "text_encoder": "models/text_encoders/umt5-xxl-encoder-Q4_K_M.gguf",
        "vae": "models/vae/wan2.2_vae.safetensors",
        "supported_modes": ["t2v", "i2v"],
        "supported_loras": ["wan22_5b"],
        "supported_controls": ["prompt", "negative", "seed", "resolution",
                               "frames", "steps", "sampler", "cfg", "loras",
                               "start_image", "cache"],
        "defaults": dict(WAN_DEFAULTS),
        "resolution_limits": {"min": [256, 256], "max": [1280, 720],
                              "step": 16,
                              "native": ["832x480", "512x288", "768x432"]},
        "frame_limits": {"min": 5, "max": 121, "step": 4},
        "vram_estimate_mb": 7200,
        "experimental": True,
        "status": "candidate",
        "source": "QuantStack/Wan2.2-TI2V-5B-GGUF",
        "note": "Same graph as Q4_K_M, higher-precision weights. Only worth "
                "the extra VRAM if a measured A/B shows a real quality gain.",
    },
    "wan22_5b_q6": {
        "id": "wan22_5b_q6",
        "display_name": "Wan 2.2 TI2V-5B (Q6_K)",
        "catalog": {"group": "wan", "label": "TI2V-5B · Q6_K", "detail": "832x480 + post upscale", "rank": 30},
        "family": "wan22_ti2v_5b",
        "version": "2.2",
        "path": "models/diffusion_models/Wan2.2-TI2V-5B-Q6_K.gguf",
        "format": "gguf",
        "quantization": "Q6_K",
        "workflow": "wan2.2_ti2v_5b",
        "workflow_version": 1,
        "text_encoder": "models/text_encoders/umt5-xxl-encoder-Q4_K_M.gguf",
        "vae": "models/vae/wan2.2_vae.safetensors",
        "supported_modes": ["t2v", "i2v"],
        "supported_loras": ["wan22_5b"],
        "supported_controls": ["prompt", "negative", "seed", "resolution",
                               "frames", "steps", "sampler", "cfg", "loras",
                               "start_image", "cache"],
        "defaults": dict(WAN_DEFAULTS),
        "resolution_limits": {"min": [256, 256], "max": [1280, 720],
                              "step": 16,
                              "native": ["832x480", "512x288", "768x432"]},
        "frame_limits": {"min": 5, "max": 121, "step": 4},
        "vram_estimate_mb": 7600,
        "experimental": True,
        "status": "candidate",
        "source": "QuantStack/Wan2.2-TI2V-5B-GGUF",
        "note": "Highest practical quant for 8 GB. Watch for offload: at "
                "3.92 GB of weights plus activations this is the tightest of "
                "the three on a 3070.",
    },

    # ============================================================ legacy
    "ltx_video_2b": {
        "id": "ltx_video_2b",
        "display_name": "LTX-Video 2B v0.9 (legacy)",
        "catalog": {"group": "legacy", "label": "LTX-Video 2B v0.9", "detail": "dormant template", "rank": 10},
        "family": "ltx",
        "version": "0.9",
        "path": "models/diffusion_models/ltx-video-2b-v0.9-Q4_K_M.gguf",
        "format": "gguf",
        "quantization": "Q4_K_M",
        "workflow": "ltx_video_2b",
        "workflow_version": 1,
        "text_encoder": "models/text_encoders/t5-v1_1-xxl-encoder-Q4_K_M.gguf",
        "vae": "models/vae/LTX-Video-VAE-BF16.safetensors",
        "supported_modes": ["t2v"],
        "supported_loras": ["ltx"],
        "supported_controls": ["prompt", "seed", "resolution", "frames",
                               "steps", "sampler", "cfg"],
        "defaults": {"steps": 8, "cfg": 3.0, "sampler": "euler",
                     "scheduler": "normal"},
        "resolution_limits": {"min": [256, 256], "max": [768, 512],
                              "step": 32, "native": ["512x288", "768x512"]},
        "frame_limits": {"min": 9, "max": 97, "step": 8},
        "vram_estimate_mb": 6000,
        "experimental": True,
        "status": "legacy",
        "source": "dormant studio template",
        "note": "The studio's pre-existing LTX template. Dormant — nothing "
                "in tool/*.py reaches it. Exposed here so it can be assessed "
                "as a distinct capability instead of being "
                "replaced on reputation.",
    },
}

# --------------------------------------------------------------------------
# USER registry merge.
# tool/video_models_user.json lets you add optimized/distinct models without
# touching code. Entries with the same id as a built-in OVERWRITE that
# entry only if they set "override": true; otherwise they are added.
# Set "disabled": true to retire an entry without deleting it.
# --------------------------------------------------------------------------
USER_REGISTRY_ERROR: str | None = None


def _merge_user_registry() -> None:
    """Merge tool/video_models_user.json. A malformed file must never break
    the studio, but it must not fail silently either: the error is logged
    and exposed as USER_REGISTRY_ERROR (surfaced by /api/videostudio/models),
    and a single bad entry only drops that entry."""
    global USER_REGISTRY_ERROR
    import json as _json
    from core import log_setup
    log = log_setup("video_models")
    path = ROOT / "tool" / "video_models_user.json"
    if not path.exists():
        return
    try:
        doc = _json.loads(path.read_text(encoding="utf-8"))
        models = doc.get("models") if isinstance(doc, dict) else None
        if not isinstance(models, dict):
            raise ValueError('top level must be {"models": {id: entry, ...}}')
    except Exception as e:  # noqa: BLE001
        USER_REGISTRY_ERROR = f"{path.name} is unreadable: {e}"
        log.error("%s — only the built-in video models are available",
                  USER_REGISTRY_ERROR)
        return
    for uid, uent in models.items():
        if not isinstance(uent, dict):
            log.warning("%s: entry %r is not an object — skipped", path.name, uid)
            continue
        if uent.get("disabled"):
            # Retire an entry without deleting it. This must also apply to
            # BUILT-IN entries: the documented contract says so, and the
            # legacy `ltx_video_2b` proved the gap — it reported available
            # while silently routing through the Wan templates, so the UI
            # offered a model that could not render correctly.
            VIDEO_MODELS.pop(uid, None)
            continue
        uent.setdefault("id", uid)
        if uent.get("override") and uid in VIDEO_MODELS:
            VIDEO_MODELS[uid] = {**VIDEO_MODELS[uid], **uent}
        else:
            VIDEO_MODELS[uid] = uent


_merge_user_registry()

# Every entry must name these; model_status reports an entry that lacks one
# as unavailable instead of raising KeyError.
_REQUIRED_FIELDS = ("path", "text_encoder", "vae", "workflow")


def _get_extra_model_dirs() -> list[Path]:
    """Extra model roots, in order: env vars (os.pathsep-separated), then
    settings.yaml paths.extra_model_dirs (tool/model_files.py, which the
    launcher's generated extra_model_paths config follows too)."""
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    return model_files.extra_roots(cfg, ROOT)


def _exists(rel: str) -> bool:
    """True when the file is under the studio root, or in a model folder in
    its place (ComfyUI's older unet/ and clip/ names too) with the catalog's
    size: another build under the same name there does not count."""
    if (ROOT / rel).exists():
        return True
    rel = rel.replace("\\", "/")
    return model_files.find(rel, _get_extra_model_dirs(), ROOT,
                            model_files.catalog_size(rel, ROOT)) is not None


def workflow_exists(name: str) -> bool:
    return (WF_DIR / f"{name}.json").exists()


# Reference-guide nodes: a graph carrying one needs real reference images
# (its LoadImage nodes are placeholders until the injector fills them).
REFERENCE_GUIDES = frozenset({"ComfyUILTX25MSRMultiReferenceGuide",
                              "MiniMaxH3ReferenceToVideo"})


_CLASS_TYPE = re.compile(r'"class_type"\s*:\s*"([^"]+)"')


def workflow_classes(name: str | None) -> set[str]:
    """The node class_types of a workflow template (empty when missing).

    Scanned from the raw text: templates carry unquoted {TOKENS}
    ("steps": {STEPS}) and are not valid JSON before substitution."""
    if not name:
        return set()
    try:
        text = (WF_DIR / f"{name}.json").read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(_CLASS_TYPE.findall(text))


def t2v_graph_problem(mid: str, template: str | None,
                      refs_template: str | None) -> str | None:
    """Why `template` cannot render text-to-video for `mid`, or None.

    * a reference graph (LTX MSR / H3 Ref2VA) needs real reference images —
      without them it fails validation, or renders whatever stale file its
      placeholder LoadImage nodes still name;
    * a distilled tier (turbo / few-step) wires its adapter LoRA into the
      refs graph only; the shared t2v graph would run the base model at the
      tier's 4-8 steps without it — unusable output."""
    t2v = workflow_classes(template)
    if t2v & REFERENCE_GUIDES:
        return (f"{mid}: text-to-video is not available — its t2v graph "
                f"'{template}' is a reference graph. Attach a reference image "
                f"(Cast tab / references) or choose another tier.")
    dropped = {c for c in workflow_classes(refs_template)
               if c.startswith("Lora")} - t2v
    if dropped:
        return (f"{mid}: text-to-video would drop this tier's adapter LoRA "
                f"('{template}' has no {', '.join(sorted(dropped))}; "
                f"'{refs_template}' does). Attach a reference image or use a "
                f"full-step tier for text-to-video.")
    return None


def t2v_problem(mid: str) -> str | None:
    """t2v_graph_problem for the graph a reference-less render of `mid`
    would use (entries with a `workflows` map: LTX-2.5 and MiniMax H3)."""
    m = VIDEO_MODELS.get(mid) or {}
    wf = m.get("workflows") or {}
    if not wf:
        return None
    if str(m.get("family", "")).startswith("h3"):
        t, r = "t2v", "refs"
    elif bool((m.get("defaults") or {}).get("dfr", True)):
        t, r = "t2v_dfr", "refs_dfr"
    else:
        t, r = "t2v_plain", "refs_plain"
    return t2v_graph_problem(mid, wf.get(t), wf.get(r)) if wf.get(t) else None


def takes_reference_images(mid: str) -> bool:
    """Whether `mid` can carry an image at all. LTX-2.5 and MiniMax H3 have no
    START_IMAGE slot: a start image or continuation frame rides their
    reference graph (LTX MSR pic1 via `ref_nodes`, H3 Ref2VA frame-0 anchor).
    Wan has native image-to-video templates."""
    m = VIDEO_MODELS.get(mid) or {}
    fam = str(m.get("family", ""))
    wf = m.get("workflows") or {}
    if fam.startswith("h3"):
        return bool(wf.get("refs"))
    if fam.startswith("ltx"):
        return bool(m.get("ref_nodes")) and any(k.startswith("refs") for k in wf)
    return True


def i2v_problem(mid: str) -> str | None:
    """Why `mid` cannot render image-to-video, or None."""
    if takes_reference_images(mid):
        return None
    return (f"{mid}: image-to-video is not available - this tier has no "
            f"reference graph to carry a start image. Choose a reference "
            f"(refs) tier of the same family.")


def seamless_kind(entry: dict) -> str | None:
    """'ltx' / 'h3' when the model renders chains as one seamless take
    (chain_studio), else None (tail-frame continuation)."""
    fam = str(entry.get("family") or "")
    wfs = entry.get("workflows") or {}
    # the LTX-2.5 entries that ship a chain template (22B AV stack: audio VAE,
    # x2 latent upscaler, MSR + CQ LoRAs) are the ones chain_graphs targets
    if fam.startswith("ltx") and wfs.get("chain") and str(entry.get("comfy_root") or "") == "ComfyUI_LTX":
        return "ltx"
    # every H3 tier: the chain reads the tier's own recipe from its built graph
    if fam.startswith("h3") and "refs" in wfs:
        return "h3"
    return None


def chain_has_finish(entry: dict) -> bool:
    """Whether a seamless chain of this entry renders a finish after its draft
    take, so it can stop at the draft preview (draft_only): LTX always (the
    1080p stage 2), H3 on the 1080p tiers (the take refine). On a base-only
    H3 tier the decode-once take is the delivery."""
    kind = seamless_kind(entry)
    return kind == "ltx" or (kind == "h3" and bool(entry.get("refine_target")))


# ------------------------------------------------------------------ catalog
# How the model pickers group and order the registry. An entry's optional
# `catalog` block ({group, label, detail, rank, recommended}) places it; an
# entry without one falls back to its family's group and its display_name.
CATALOG_GROUPS: tuple[tuple[str, str], ...] = (
    ("ltx25", "LTX-2.5 · 22B"),
    ("h3_1080p", "MiniMax H3 · 1080p"),
    ("h3_draft", "MiniMax H3 · drafts"),
    ("wan", "Wan 2.2 · legacy, not recommended"),
    ("legacy", "Legacy"),
)
_GROUP_RANK = {g: i for i, (g, _) in enumerate(CATALOG_GROUPS)}


def _family_group(m: dict) -> str:
    fam = str(m.get("family") or "")
    if fam.startswith("ltx2"):
        return "ltx25"
    if fam.startswith("h3"):
        return "h3_1080p" if m.get("refine_target") else "h3_draft"
    if fam.startswith("wan"):
        return "wan"
    return "legacy"


def catalog_entry(mid: str, m: dict) -> dict:
    """The picker placement of an entry: group, short label, detail line,
    rank inside the group and the recommended flag."""
    c = m.get("catalog")
    c = c if isinstance(c, dict) else {}
    group = str(c.get("group") or "")
    if group not in _GROUP_RANK:
        group = _family_group(m)
    return {"group": group,
            "label": str(c.get("label") or m.get("display_name") or mid),
            "detail": str(c.get("detail") or ""),
            "rank": int(c.get("rank") or 99),
            "recommended": bool(c.get("recommended", False))}


def catalog() -> dict:
    """The picker payload: the groups in display order and every model's
    status, sorted by group, recommended first, then rank and label."""
    models = all_video_models()
    models.sort(key=lambda s: (_GROUP_RANK.get(s["catalog"]["group"], len(_GROUP_RANK)),
                               not s["catalog"]["recommended"], s["catalog"]["rank"],
                               s["catalog"]["label"].lower()))
    used = {s["catalog"]["group"] for s in models}
    return {"groups": [{"id": g, "label": lab} for g, lab in CATALOG_GROUPS if g in used],
            "models": models}


def model_status(mid: str) -> dict:
    """Resolve an entry to its real availability.

    `status` is deliberately *computed*, not trusted from the table: a model
    whose unet or workflow is absent reports `missing` and the UI must not
    offer it.
    """
    m = VIDEO_MODELS.get(mid)
    if not m:
        return {"id": mid, "available": False, "reason": "unknown id",
                "missing": []}
    missing = []
    absent = [k for k in _REQUIRED_FIELDS if not m.get(k)]
    for key in absent:
        missing.append(f"registry field '{key}'")
    for key in ("path", "text_encoder", "vae"):
        if m.get(key) and not _exists(str(m[key])):
            missing.append(str(m[key]))
    if m.get("workflow") and not workflow_exists(str(m["workflow"])):
        missing.append(f"tool/workflows/{m['workflow']}.json")
    # A model whose availability check looks only at `workflow` can be offered
    # as available and then fail at start, because the Studio renders
    # `studio_workflow` when it is set. Validate both, plus every template in
    # the `workflows` map used by LTX-2.5 (refs/t2v x dfr/plain).
    for key in ("studio_workflow",):
        wf_name = m.get(key)
        if wf_name and not workflow_exists(wf_name):
            missing.append(f"tool/workflows/{wf_name}.json")
    for key, wf_name in (m.get("workflows") or {}).items():
        if wf_name and not workflow_exists(wf_name):
            missing.append(f"tool/workflows/{wf_name}.json ({key})")
    # the UI builds its mode selector from supported_modes: drop t2v where
    # the mapped graph cannot do it (the registry entry itself is untouched)
    modes = list(m.get("supported_modes", []))
    t2v_note = t2v_problem(mid) if "t2v" in modes else None
    if t2v_note:
        modes.remove("t2v")
    i2v_note = i2v_problem(mid) if "i2v" in modes else None
    if i2v_note:
        modes.remove("i2v")
    # LTX-2.5 and MiniMax H3 run on ComfyUI_LTX's CUDA 13.0 build, the legacy
    # Wan 2.2 on the render farm's CUDA 12.6 one (gpu_support)
    fam = str(m.get("family") or "")
    gpu = gpu_support.problem("cu126", "Wan 2.2") if fam.startswith("wan") else gpu_support.problem("cu130")
    return {
        "id": mid,
        # --- display fields. These were MISSING, so the UI could only show
        # raw ids like "wan22_5b_q4" and the operator could not tell what any
        # of them were. Everything the UI needs to label a model lives here.
        "display_name": m.get("display_name") or mid,
        "catalog": catalog_entry(mid, m),
        # chains / CONTINUE runs render as one seamless take ('ltx' / 'h3')
        "seamless": seamless_kind(m),
        "chain_finish": chain_has_finish(m),     # a draft preview, then Finish
        "family": m.get("family"),
        "quantization": m.get("quantization"),
        "fps": m.get("fps"),
        "moe": m.get("moe", False),
        "source": m.get("source", "studio"),
        "vram_note": m.get("vram_note"),
        # --- availability: installed, and this PC's card and driver run it
        "available": not missing and not gpu,
        "missing": missing,
        "gpu": gpu,
        **({"reason": gpu} if gpu else {}),
        "declared_status": m.get("status"),
        "workflow": m.get("workflow"),
        "supported_modes": modes,
        "t2v_note": t2v_note,
        "i2v_note": i2v_note,
        "supported_loras": m.get("supported_loras", []),
        "supported_controls": m.get("supported_controls", []),
        "experimental": m.get("experimental", True),
        "vram_estimate_mb": m.get("vram_estimate_mb"),
        "defaults": m.get("defaults", {}),
        "resolution_limits": m.get("resolution_limits", {}),
        "frame_limits": m.get("frame_limits", {}),
        # frame_snap tells the movie pipeline HOW to land on the grid
        # ("nearest" for MiniMax H3: 121 -> 124 = its 5.17 s clip, not the
        # floor value 107). Without this passthrough the orchestrator could
        # only floor-snap and rendered H3 shots 14 frames short.
        "frame_snap": m.get("frame_snap", "floor"),
        "note": m.get("note", ""),
    }


def all_video_models() -> list[dict]:
    return [model_status(mid) for mid in VIDEO_MODELS]


def loras_for(mid: str) -> list[str]:
    """Which LoRA families a model accepts. The Video Studio must intersect
    this with each LoRA's own family before offering it — an
    incompatible adapter must never be selectable, let alone silently applied.
    See `video_studio.loras_for_model()` for the matching rule.
    """
    m = VIDEO_MODELS.get(mid)
    return list(m.get("supported_loras", [])) if m else []


# The movie pipeline's default video model: what a project renders when it has
# no model, an unknown one or an unavailable one. New projects start on it too
# (routers/projects.py).
DEFAULT_MODEL = "ltx25_s6_cq"


def default_model_id() -> str | None:
    """DEFAULT_MODEL when it is installed, else the first available LTX-2.5 /
    MiniMax H3 tier in picker order; None when none is installed. Never a
    Wan 2.2 entry: Wan is legacy and renders only when picked explicitly."""
    if model_status(DEFAULT_MODEL).get("available"):
        return DEFAULT_MODEL
    for m in catalog()["models"]:
        fam = str(m.get("family") or "")
        if (m.get("available") and m["catalog"]["group"] != "legacy"
                and (fam.startswith("ltx") or fam.startswith("h3"))):
            return m["id"]
    return None


def production_model_id() -> str:
    """The model the UI should present as the default: default_model_id(),
    or DEFAULT_MODEL by name when nothing is installed (the picker then
    shows it as unavailable)."""
    return default_model_id() or DEFAULT_MODEL
