"""Video Studio — the isolated experimental video-generation backend.

⚠ **Isolation contract.** This module never writes to `settings.yaml`, never
touches `tool/workflows.py:SUBST`, and never changes what the automated movie
pipeline renders. It has its own model registry (`video_models`), its own
output directory, and its own job store. Making a model the movie pipeline's
default is a deliberate manual edit of `video_models.DEFAULT_MODEL`.

Design:
  * Jobs run on a daemon thread; each is fully described in the job record so a
    result can be reproduced later.
  * LoRAs are gated by model family — an incompatible adapter is never
    selectable, let alone silently applied.
  * Output goes to `output/videostudio/`, distinct from the pipeline's `output/`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import ROOT, ffmpeg_exe, load_config, log_setup
import model_files
import video_models

log = log_setup("video_studio")

OUT_DIR = ROOT / "output" / "videostudio"
JOBS_FILE = ROOT / "temp" / "videostudio_jobs.json"

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_CHAINS: dict[str, dict] = {}


# ------------------------------------------------------------------- loras
def _lora_registry() -> dict:
    p = ROOT / "tool" / "video_lora_registry.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"video lora registry unreadable: {e}")
        return {}


def loras_for_model(mid: str) -> dict:
    """LoRAs whose family the selected model actually accepts.

    `video_models.loras_for()` lists the families a model supports (e.g.
    `["wan22_5b"]`); the registry tags each LoRA with a concrete family (e.g.
    `wan22_5b`, `wan22_a14b`, `ltx`, `sdxl`, `unknown`).

    Matching rule — a LoRA family F matches an accepted family A when
    `F == A` or **F is a proper prefix of A**. That lets a generically-tagged
    `wan22` adapter serve a `wan22_5b` model, while correctly rejecting
    `wan22_a14b` for it: the relation is deliberately one-directional, because
    a bidirectional prefix test would offer 14B adapters for 5B models.
    `unknown` never matches.
    """
    accepted = video_models.loras_for(mid)
    out, rejected = {}, []
    for key, e in _lora_registry().items():
        fam = (e.get("family") or "unknown").lower()
        if fam == "unknown":
            rejected.append(key)
            continue
        if any(fam == a or (a.startswith(fam) and len(fam) < len(a))
               for a in accepted):
            out[key] = {
                "id": e.get("id"), "file": e.get("file"),
                "display": e.get("display") or e.get("file"),
                "family": fam, "category": e.get("category"),
                "trigger": e.get("trigger") or "",
                "strength": e.get("strength"),
                "status": e.get("status"),
            }
        else:
            rejected.append(key)
    return {"compatible": out, "incompatible_count": len(rejected),
            "accepted_families": accepted}


# -------------------------------------------------------------------- jobs
def _load_jobs() -> None:
    """Reload the job store **in place**.

    ⚠ This must MUTATE `_JOBS`, never rebind it. An earlier version did
    `_JOBS = json.loads(...)`, which swapped in a brand-new dict — so a running
    job's local `j` reference kept pointing at the *old* dict, and every
    subsequent update (status, seconds, outputs) was silently discarded when
    `_save_jobs()` wrote the freshly-loaded copy. The symptom was a job that
    produced its file correctly but sat at "running" forever.
    """
    if not JOBS_FILE.exists():
        return
    try:
        disk = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    _JOBS.clear()
    _JOBS.update(disk)


def _patch(jid: str, **kw) -> None:
    """Apply an update to a job and persist it, re-fetching under the lock."""
    with _LOCK:
        _load_jobs()
        j = _JOBS.get(jid)
        if j is not None:
            j.update(**kw)
        _save_jobs()


def _save_jobs() -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_FILE.with_name(JOBS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(_JOBS, indent=1), encoding="utf-8")
    tmp.replace(JOBS_FILE)


def jobs(limit: int = 50) -> list[dict]:
    with _LOCK:
        _load_jobs()
        return sorted(_JOBS.values(), key=lambda j: j.get("ts", ""),
                      reverse=True)[:limit]


def job(jid: str) -> dict | None:
    with _LOCK:
        _load_jobs()
        return _JOBS.get(jid)


# --------------------------------------------------------------- generation
STOCK_TE = "umt5-xxl-encoder-Q4_K_M.gguf"

# ⚠ The Turbo LoRA is NOT optional for Wan 5B. Production's `wan2.2_t2v_5b`
# template applies it via a dedicated `LoraLoaderModelOnly` node
# ({LORA_TURBO}); the `_dynamic` templates the Video Studio uses carry only a
# `LoraChain` ({LORA_CHAIN}) and have NO turbo slot. Generating without it at
# 8 steps produces mush. It is injected explicitly in _build so the Studio
# matches production's quality, and so the job record shows it.
TURBO_LORA = "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors"


def _apply_text_encoder(wf: dict, te: str | None,
                        force_cpu: bool = False,
                        clip_type: str = "wan") -> str:
    """Swap the text encoder in a built graph.

    The production templates load the encoder with `CLIPLoaderGGUF` (a GGUF
    file). An encoder that ships only as safetensors needs a different node,
    not just a different filename:
    `CLIPLoaderGGUF` cannot read a .safetensors, and `CLIPLoader` cannot read a
    .gguf. Returning the node type lets the job record which path was used.

    `clip_type` is the ComfyUI CLIPLoader `type` value. Wan uses "wan";
    LTX-2.5 uses "ltxv" (Gemma 4 encoder). Passing the wrong type silently
    produces garbage conditioning, so the value comes from the registry
    entry (`clip_type`) rather than being hardcoded.

    `force_cpu=True` sets `device: "cpu"` so the encoder stays in SYSTEM RAM
    instead of VRAM. The Wan UMT5 encoder is **6.3 GB in fp8** — on an 8 GB
    card that is the difference between fitting and not. It costs almost no
    speed because the encoder runs ONCE per generation, not once per step.
    This mirrors the official Wan guidance for <=24 GB cards (`--t5_cpu`).

    Returns the class_type actually in the graph.
    """
    if not te:
        return "CLIPLoaderGGUF"
    want_gguf = te.lower().endswith(".gguf")
    for nid, node in list(wf.items()):
        ct = node.get("class_type")
        if ct not in ("CLIPLoaderGGUF", "CLIPLoader"):
            continue
        new_type = "CLIPLoaderGGUF" if want_gguf else "CLIPLoader"
        node["class_type"] = new_type
        node["inputs"] = {"clip_name": te, "type": clip_type}
        if want_gguf:
            node["inputs"]["device"] = "default"
        elif force_cpu:
            # only CLIPLoader (safetensors) exposes a device option
            node["inputs"]["device"] = "cpu"
        return new_type
    return "CLIPLoaderGGUF"


def _check_t2v_graph(mid: str, template: str, refs_template: str | None) -> None:
    """Refuse a text-to-video render whose mapped graph cannot do t2v
    (video_models.t2v_graph_problem) — a clear error instead of a failed
    validation or a render with stale placeholder references."""
    problem = video_models.t2v_graph_problem(mid, template, refs_template)
    if problem:
        raise ValueError(problem)


def _build(mid: str, mode: str, prompt: str, negative: str, seed: int,
           w: int, h: int, frames: int, steps: int, cfg: float,
           loras: list[dict], start_image: str | None,
           teacache_thresh: float, teacache_end: float, no_cache: bool,
           shot_id: str, text_encoder: str | None = None,
           references: list[dict] | None = None,
           dfr: bool | None = None,
           chain_tail: str | None = None,
           chain_tail_still: str | None = None,
           fps_override: int | None = None) -> tuple[dict, str, str]:
    from workflows import WorkflowBuilder

    m = video_models.VIDEO_MODELS.get(mid)
    if not m:
        raise ValueError(f"unknown video model {mid}")

    # Pick the workflow.
    #
    # `studio_workflow` is a NEW, optional field: only models that genuinely
    # need a different graph set it (the A14B/MoE pair needs two unets and two
    # KSamplerAdvanced nodes). Everything else falls through to the original
    # mode-based default, so the working 5B path is byte-identical — the 5B
    # entries declare `workflow: wan2.2_ti2v_5b`, but the Studio has always
    # rendered them through the `_dynamic` templates, and that must not change.
    #
    # LTX-2.5 declares a `workflows` map instead:
    # {refs_dfr, refs_plain, t2v_dfr, t2v_plain}. Reference images REPLACE the
    # start image for LTX (the MSR guide appends them to the stage-1 latent),
    # and DFR is the detail-keyframe pass. When references exist the MSR
    # template is mandatory: ComfyUILTX25MSRMultiReferenceGuide requires pic1,
    # so a plain t2v graph cannot carry them.
    refs = [r for r in (references or [])
            if isinstance(r, dict) and r.get("path")]
    wfset = m.get("workflows") or {}
    fam = str(m.get("family", ""))
    # LTX i2v backstop: promote a start image to the pic1 SUBJECT slot when no
    # references were passed (mirrors start()'s promotion; see that comment).
    if fam.startswith("ltx") and not refs and start_image \
            and m.get("ref_nodes"):
        _sp = _resolve_start_image_path(start_image)
        if _sp is not None:
            refs = [{"slot": "pic1", "path": str(_sp),
                     "source": "i2v_start_image"}]
    want_dfr = (bool(dfr) if dfr is not None
                else bool((m.get("defaults") or {}).get("dfr", True)))
    if fam.startswith("h3"):
        # MiniMax H3 family: refs-vs-t2v only (no DFR concept). The templates
        # carry their own CLIPLoader (type "minimax") and the two-phase hero
        # graph embeds the proven refine schedule — see the
        # H3 lab record PROVEN_CONFIGS_H3. Resolution tiering comes from the registry
        # entry the caller selected (hero 1080p / 768p / 544p).
        #
        # H3 declares i2v but has NO separate i2v graph: identity rides the
        # Ref2VA frame-0 anchor, which only the refs template carries. A start
        # image with no references used to select the t2v template and the
        # image was silently dropped. Promote it to reference #1 instead —
        # _apply_references_h3 pins reference #1 as the frame-0 identity
        # anchor, which is exactly the "start image" behavior the operator
        # expects (identity path of the H3 lab record PROVEN_CONFIGS_H3).
        if not refs and start_image:
            p = _resolve_start_image_path(start_image)
            if p is not None:
                log.info(f"{mid}: i2v start image promoted to the H3 frame-0 "
                         f"identity anchor: {p}")
                refs = [{"slot": "pic1", "path": str(p),
                         "source": "i2v_start_image"}]
            else:
                log.warning(f"{mid}: start image '{start_image}' not found in "
                            f"any ComfyUI input dir — rendering text-to-video")
        key = "refs" if refs else "t2v"
        template = wfset.get(key)
        if not template:
            raise ValueError(f"{mid} has no workflow mapped for '{key}'")
        if key == "t2v":
            _check_t2v_graph(mid, template, wfset.get("refs"))
    elif wfset:
        # LTX MSR requires a SUBJECT reference: ComfyUILTX25MSRMultiReference-
        # Guide needs pic1, so a background-only set (environment ref without
        # any character identity) cannot ride the MSR template. It used to
        # select refs_* anyway and the injector raised "LTX reference graph
        # has no pic1 after injection" — killing the render. Degrade to the
        # plain t2v template instead (with a warning the caller surfaces).
        subject_slots = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6")
        has_subject = any(str(r.get("slot") or "") in subject_slots
                          for r in refs)
        if refs and not has_subject:
            log.warning("%s: %d reference(s) but no subject (pic1..) — "
                        "rendering text-to-video; attach character identity "
                        "references in the Cast tab", mid, len(refs))
            refs = []
        key = (("refs_dfr" if want_dfr else "refs_plain") if refs
               else ("t2v_dfr" if want_dfr else "t2v_plain"))
        if chain_tail and wfset.get("chain"):
            # Overlap-extend chain variant: the previous clip's tail frames
            # are pinned IN-PLACE at the leading latent frames (stage 1) and
            # re-pinned after the latent upsampler (stage 2) — measured
            # 33.5-35.1 dB overlap fidelity and seam motion continuity equal
            # to the clip's own natural motion (BENCH chain lab, 2026-10-01).
            # Refs selection does not apply; the tail identity rides the pin
            # and the tail still anchors pic1 in the template.
            key = "chain"
        template = wfset.get(key)
        if not template:
            raise ValueError(f"{mid} has no workflow mapped for '{key}'")
        if key.startswith("t2v"):
            _check_t2v_graph(mid, template,
                             wfset.get("refs_dfr" if want_dfr else "refs_plain"))
    elif mode == "i2v":
        if "i2v" not in (m.get("supported_modes") or []):
            raise ValueError(f"{mid} does not support i2v")
        template = m.get("studio_workflow") or "wan2.2_ti2v_dynamic"
    else:
        if "t2v" not in (m.get("supported_modes") or []):
            raise ValueError(f"{mid} does not support t2v")
        template = m.get("studio_workflow") or "wan2.2_t2v_dynamic"
    if not video_models.workflow_exists(template):
        raise ValueError(f"workflow template {template} is missing")

    # ⚠ TeaCache must be OFF for the MoE/A14B path.
    #
    # The A14B template hard-wires a `WanVideoTeaCacheKJ` node
    # (rel_l1_thresh 0.1, end_percent 0.6). At the 4 steps this model runs,
    # that threshold lets the sampler SKIP most of its steps whenever the
    # latent barely changes — which produces an almost motionless image.
    # Measured symptom: "looks like a still, barely anything visible".
    #
    # The working setup I compared against (another ComfyUI install on this
    # PC) defaults TeaCache OFF, so the working configuration never had it.
    # A caller can still force it on by passing no_cache=False explicitly.
    if m.get("moe") and no_cache is False:
        no_cache = True

    cfg_yaml = load_config()
    lora_cfg = cfg_yaml.get("lora", {}) or {}

    chain = [{"name": l["file"], "strength": float(l.get("strength") or 1.0)}
             for l in loras if l.get("file")]

    # The dynamic templates have no turbo slot, so add it first — before any
    # user LoRA — unless the caller already named it.
    #
    # MoE (A14B) models must NOT get the 5B Turbo LoRA: they use the paired
    # LightX2V 4-step LoRAs wired into the template itself
    # (`{LORA_4STEP_HIGH}` / `{LORA_4STEP_LOW}`). LTX-2.5 and MiniMax H3 use
    # no Wan LoRA at all (their graphs carry their own adapters).
    is_wan = fam.startswith("wan")
    if is_wan and not m.get("moe") and not any(c["name"] == TURBO_LORA for c in chain):
        if not model_files.exists(f"models/loras/{TURBO_LORA}", cfg_yaml, ROOT):
            raise ValueError(
                f"Turbo LoRA missing: {TURBO_LORA}. Generating without it at "
                f"8 steps produces unusable output — install it or raise steps.")
        chain.insert(0, {"name": TURBO_LORA, "strength": 1.0})

    extra = {
        "SHOT_ID": f"vs_{shot_id}",
        "MODEL_5B": Path(m["path"]).name,
        "LORA_CHAIN": chain,
        "TEA_THRESH": teacache_thresh,
        "TEA_END": teacache_end,
        "CHAR_STR": float(lora_cfg.get("char_strength", 0.8)),
        "POWER_STR": float(lora_cfg.get("power_strength", 0.7)),
        "ENV_STR": float(lora_cfg.get("env_strength", 0.6)),
        "ANATOMY_STR": float(lora_cfg.get("anatomy_strength", 0.7)),
        "QUALITY_STR": float(lora_cfg.get("quality_strength", 0.6)),
        "STYLE_STR": float(lora_cfg.get("style_strength", 0.7)),
    }
    # MoE models carry two experts; the A14B template loads both.
    if m.get("moe"):
        if not m.get("unet_high") or not m.get("unet_low"):
            raise ValueError(f"{mid} is marked moe but lacks unet_high/unet_low")
        extra["MODEL_HIGH"] = Path(m["unet_high"]).name
        extra["MODEL_LOW"] = Path(m["unet_low"]).name
        # the template's 4-step LoRA slots default to the t2v pair in SUBST;
        # honour a per-model override when one is declared
        if m.get("lora_4step_high"):
            extra["LORA_4STEP_HIGH"] = Path(m["lora_4step_high"]).name
        if m.get("lora_4step_low"):
            extra["LORA_4STEP_LOW"] = Path(m["lora_4step_low"]).name
    # MiniMax H3 templates take their fixed, proven stack through dedicated
    # tokens (the filenames are frozen in the H3 lab record PROVEN_CONFIGS_H3); the DiT
    # filename comes from the registry entry via MODEL_5B. REFINE_W/H drive
    # the two-phase hero template's latent upscaler target.
    if fam.startswith("h3"):
        extra["H3_TE"] = Path(m["text_encoder"]).name
        extra["H3_VAE_VIDEO"] = Path(m["vae"]).name
        if m.get("vae_audio"):
            extra["H3_VAE_AUDIO"] = Path(m["vae_audio"]).name
        if m.get("latent_upscaler"):
            extra["H3_UPSCALER"] = Path(m["latent_upscaler"]).name
        if m.get("refine_target"):
            rw, rh = str(m["refine_target"]).lower().split("x")
            extra["REFINE_W"], extra["REFINE_H"] = int(rw), int(rh)
    if start_image:
        extra["START_IMAGE"] = start_image
    if chain_tail:
        extra["CHAIN_TAIL"] = chain_tail
    if fps_override:
        # see below — an explicit chain fps wins over everything
        extra["FPS"] = int(fps_override)
        extra["FPS_F"] = float(fps_override)
    elif fam.startswith("ltx") and m.get("fps"):
        # the builder derives FPS as frames//5 (a 5-second-clip heuristic).
        # The lab's PROVEN LTX audio+motion config pins 24 fps explicitly
        # (yield_b2fix): a wrong fps detunes the RoPE time axis AND makes
        # the generated audio track the wrong length. Entries that declare
        # "fps" (the s6_cq family) get the true value instead.
        extra["FPS"] = int(m["fps"])
        extra["FPS_F"] = float(m["fps"])

    wf = WorkflowBuilder().build(template, prompt, negative, seed, w, h,
                                 steps, frames, cfg_yaml, extra=extra)

    if no_cache:
        # rewire KSampler.model past the TeaCache node
        for nid, node in list(wf.items()):
            if node.get("class_type") != "WanVideoTeaCacheKJ":
                continue
            src = node["inputs"].get("model")
            for other in wf.values():
                for k, v in other["inputs"].items():
                    if isinstance(v, list) and len(v) == 2 and v[0] == nid:
                        other["inputs"][k] = src
            del wf[nid]

    # The model's DECLARED text encoder wins over the template default. For
    # the 5B entries this is a no-op (their declared encoder is the same file
    # the template already uses).
    te = text_encoder
    if te is None and m.get("text_encoder"):
        te = Path(m["text_encoder"]).name

    # MoE/A14B: keep the 6.3 GB text encoder in SYSTEM RAM. On 8 GB of VRAM
    # the encoder and the two 14B experts cannot coexist; the encoder runs
    # once per generation so the speed cost is negligible.
    #
    # Models that declare `clip_type` (LTX: "ltxv", Gemma 4 encoder) already
    # carry the correct CLIPLoader in their template. Rewriting it through the
    # Wan path would set type "wan" and silently produce garbage conditioning,
    # so the template's loader is left untouched and only reported.
    if m.get("clip_type"):
        te_node = next((n.get("class_type") for n in wf.values()
                        if n.get("class_type") in ("CLIPLoaderGGUF", "CLIPLoader")),
                       "CLIPLoader")
    else:
        te_node = _apply_text_encoder(wf, te, force_cpu=bool(m.get("moe")))
    return wf, template, te_node


def _find_output(filename: str, output_root: Path | None = None) -> Path | None:
    """Locate a ComfyUI output artefact by filename (searches subfolders).

    `output_root` defaults to the production ComfyUI tree; models that run on a
    second ComfyUI instance (registry `comfy_root`, e.g. ComfyUI_LTX) pass their
    own output directory. Without this, LTX renders would be "not found" by the
    Video Studio copier even though ComfyUI wrote them successfully.
    """
    base = output_root or (ROOT / "ComfyUI" / "output")
    direct = base / filename
    if direct.exists():
        return direct
    for p in base.rglob(filename):
        return p
    return None


def _model_comfy_root(m: dict) -> Path:
    return ROOT / (m.get("comfy_root") or "ComfyUI")


def _is_bare_filename(name: str) -> bool:
    """A single filename component (``Path(x).name == x`` rejects separators
    and Windows drive prefixes such as ``C:x``)."""
    return bool(name) and name not in (".", "..") and Path(name).name == name \
        and ":" not in name


_REF_SLOT_ORDER = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6", "background")


def _slot_rank(r: dict) -> int:
    slot = str((r or {}).get("slot") or "")
    return _REF_SLOT_ORDER.index(slot) if slot in _REF_SLOT_ORDER else len(_REF_SLOT_ORDER)


def _resolve_start_image_path(fn: str) -> Path | None:
    """Resolve an I2V start-image filename to a local absolute path.

    The Video Studio stores uploaded start images in BOTH ComfyUI/input/ and
    ComfyUI_LTX/input/; manually-entered names may live in either. Bare
    filenames only — no separators, no drive prefix, no traversal.
    """
    from core import ROOT
    name = str(fn or "").strip()
    if not _is_bare_filename(name):
        return None
    for base in (ROOT / "ComfyUI" / "input", ROOT / "ComfyUI_LTX" / "input"):
        p = base / name
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _stage_input_image(src: Path, prefix: str) -> str:
    """Copy a local still into the stock ComfyUI input dirs under a unique
    name and return that name (what a START_IMAGE LoadImage expects)."""
    import shutil
    name = f"{prefix}_{uuid.uuid4().hex[:10]}{Path(src).suffix.lower() or '.png'}"
    staged = False
    for base in (ROOT / "ComfyUI" / "input", ROOT / "ComfyUI_LTX" / "input"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, base / name)
            staged = True
        except OSError as e:
            log.warning(f"could not stage {src} into {base}: {e}")
    if not staged:
        raise ValueError(f"could not stage {Path(src).name} into any ComfyUI input dir")
    return name


def _apply_references(wf: dict, m: dict, refs: list[dict], client) -> dict:
    """Upload reference stills and wire them into the MSR guide's LoadImage nodes.

    LTX-2.5 reference slots: pic1..pic4 (subjects) + background (location).
    Rules, all derived from the measured laboratory configuration:

    * Slots with a readable file are uploaded to the target ComfyUI's input
      directory and linked to their declared node id (`registry.ref_nodes`).
    * Slots WITHOUT a file are removed from both the guide's inputs and the
      graph. A LoadImage pointing at a missing file fails ComfyUI validation,
      and leaving an unused optional input linked would still validate the
      orphan node.
    * `pic1` is required by the MSR guide, so the caller must never choose an
      MSR template with zero references (`_build` picks the t2v template in
      that case). This is asserted here as a hard invariant.

    The workflow is deep-copied first: the job record keeps the pristine graph
    so a re-run can inject a different reference set.
    """
    nodes = m.get("ref_nodes") or {}
    if not nodes:
        return wf
    wf = json.loads(json.dumps(wf))
    guide_id = next((nid for nid, n in wf.items()
                     if isinstance(n, dict)
                     and n.get("class_type") == "ComfyUILTX25MSRMultiReferenceGuide"), None)
    if guide_id is None:
        return wf  # t2v graph — nothing to inject
    by_slot: dict[str, str] = {}
    for r in refs or []:
        slot = r.get("slot")
        if slot and slot not in by_slot:
            # Accepts an absolute/local path OR a batch reference
            # ({type,name,file}) so the Video Studio picker does not need to
            # know the server's filesystem layout.
            from ltx_refs import resolve_reference_entry
            p = resolve_reference_entry(r)
            if p is not None:
                by_slot[slot] = str(p)
            else:
                log.warning(f"[LTX refs] slot {slot}: reference not resolvable: {r!r}")
    used, dropped = [], []
    for slot, nid in nodes.items():
        path = by_slot.get(slot)
        if path and Path(path).exists():
            up = client.upload_image(Path(path))
            wf[nid] = {"class_type": "LoadImage", "inputs": {"image": up}}
            wf[guide_id]["inputs"][slot] = [nid, 0]
            used.append(slot)
        else:
            wf[guide_id]["inputs"].pop(slot, None)
            wf.pop(nid, None)
            dropped.append(slot)
    if "pic1" not in wf[guide_id]["inputs"]:
        raise RuntimeError("LTX reference graph has no pic1 after injection")
    log.info(f"[LTX refs] slots used={used} dropped={dropped}")
    return wf


def _apply_references_h3(wf: dict, m: dict, refs: list[dict], client,
                         continuation: str | None = None) -> dict:
    """Upload reference stills and wire them into the H3 Ref2VA guide.

    MiniMax H3 reference mechanics (MiniMaxH3ReferenceToVideo): `ref_images`
    is an ORDERED dict — the submission order defines which `<Picture N>` tag
    each image becomes in the prompt. Slots are therefore filled SEQUENTIALLY
    in priority order (pic1 = lead cast, pic2 = second subject, background =
    location), and absent slots are pruned from both the LoadImage nodes and
    the guide's ref_images dict (an empty LoadImage fails ComfyUI validation,
    and a dangling link would still validate the orphan node).

    The frame-0 anchor (MiniMaxH3AddGuide + its LoadImage, registry
    `anchor_image_node`) is pinned to the FIRST reference — the lead cast
    still — and is re-injected at every denoising step, never denoised. This
    is the measured strongest identity signal (H3 lab record PROVEN_CONFIGS_H3).

    When `continuation` is set (the previous shot's tail frame, a CONTINUE /
    MATCH_ACTION shot), the anchor is pinned to THAT instead: the clip opens
    exactly on the previous shot's final frame while identity still rides the
    subject pictures. This is the scene-bridging mechanism.

    The workflow is deep-copied first: the job record keeps the pristine graph
    so a re-run can inject a different reference set.
    """
    guide_id = m.get("ref_guide_node")
    ref_load = m.get("ref_load_nodes") or []
    anchor_id = m.get("anchor_image_node")
    if not guide_id or guide_id not in wf:
        return wf
    wf = json.loads(json.dumps(wf))
    from ltx_refs import resolve_reference_entry
    ordered: list[str] = []
    # <Picture N> numbering follows submission order, so submit in slot
    # priority order (pic1..pic6, background last) whatever order the caller
    # listed them in; sorted() is stable for unknown slots.
    for r in sorted(refs or [], key=_slot_rank):
        p = resolve_reference_entry(r)
        if p is not None:
            ordered.append(str(p))
    if not ordered:
        raise RuntimeError("H3 reference graph selected but no reference resolved")
    used, dropped = [], []
    for i, nid in enumerate(ref_load):
        if i < len(ordered):
            up = client.upload_image(Path(ordered[i]))
            wf[nid] = {"class_type": "LoadImage", "inputs": {"image": up}}
            used.append(f"ref_image_{i}")
        else:
            wf.pop(nid, None)
            dropped.append(nid)
    # Autogrow inputs are submitted as DOT-FLATTENED keys matching the schema
    # expansion (finalize_prefix joins the autogrow id and template name with
    # '.'): "ref_images.ref_image_0". A nested dict matches no expanded id and
    # is silently dropped at validation, leaving ref_images empty at execute —
    # the model then never sees any reference (measured: 0-ref control render
    # was pixel-identical to the 3-ref render, 2026-10-06).
    # purge ALL autogrow keys (nested + any prior flat keys) — leftover flat
    # keys pointing at popped LoadImage nodes would KeyError at validation
    wf[guide_id]["inputs"] = {
        k: v for k, v in wf[guide_id]["inputs"].items()
        if k != "ref_images" and not k.startswith("ref_images.")
    }
    for i in range(len(used)):
        wf[guide_id]["inputs"][f"ref_images.ref_image_{i}"] = [ref_load[i], 0]
    anchor_src = ordered[0]
    if continuation and Path(str(continuation)).exists():
        anchor_src = str(continuation)
    if anchor_id and anchor_id in wf:
        up = client.upload_image(Path(anchor_src))
        wf[anchor_id] = {"class_type": "LoadImage", "inputs": {"image": up}}
    log.info(f"[H3 refs] used={used} dropped={dropped} "
             f"anchor={'continuation' if anchor_src == str(continuation) and continuation else 'subject'} "
             f"({'yes' if (anchor_id and anchor_id in wf) else 'no'})")
    return wf


def _free_other_instances(keep_port: int) -> None:
    """Best-effort: ask every OTHER ComfyUI instance to release its models.

    8 GB cannot hold two model stacks at once. The studio renders strictly
    sequentially, so before a job starts on its own instance the other one is
    asked to unload (POST /free). Failures are ignored — the lease is an
    optimization, never a correctness gate, and a missing/older instance must
    not block a render.
    """
    import urllib.request
    ports = set()
    try:
        cfg = load_config()
        ports.add(int(cfg.get("ensemble", {}).get("comfyui_port", 8188)))
    except Exception:
        ports.add(8188)
    for m in video_models.VIDEO_MODELS.values():
        p = m.get("comfy_port")
        if p:
            ports.add(int(p))
    # image-registry instances too (Qwen-Image-2.1 on 8190): with one 8 GB
    # card, a Wan/H3/LTX render must be able to reclaim the Qwen instance's
    # resident DiT before starting.
    try:
        import models as _img_models
        for m in _img_models.MODELS.values():
            p = m.get("comfy_port")
            if p:
                ports.add(int(p))
    except Exception:
        pass
    for port in sorted(ports):
        if int(port) == int(keep_port):
            continue
        try:
            # Never disturb an instance that is actively rendering: unloading a
            # model mid-render only forces a reload, but that would slow a
            # concurrent movie-pipeline shot for no reason.
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/queue", timeout=5) as r:
                q = json.loads(r.read().decode())
            if (q.get("queue_running") or []):
                log.info(f"[vram lease] :{port} is rendering — not freeing it")
                continue
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/free",
                data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10):
                pass
            log.info(f"[vram lease] freed models on ComfyUI :{port}")
        except Exception:
            pass    # instance down or endpoint absent — nothing to free


def _run(jid: str) -> None:
    """Hold one GPU-broker lease for the whole local job. The broker evicts
    and verifies every other instance before the workflow is submitted."""
    with _LOCK:
        _load_jobs()
        j = dict(_JOBS.get(jid) or {})
    if not j:
        return
    lease = None
    if not (j.get("worker") and j["worker"] != "local"):
        try:
            import gpu_broker
            entry = video_models.VIDEO_MODELS.get(str(j.get("model") or "")) or {}
            port = int(entry.get("comfy_port")
                       or load_config()["ensemble"]["comfyui_port"])
            # wait up to an hour for the GPU; hold it for the job's own
            # deadline plus margin (the broker extends a lease whose
            # instance is still rendering, so this is a backstop only)
            lease = gpu_broker.acquire(
                model=j.get("model"), port=port, purpose="video",
                timeout=3600, hold_s=int(j.get("timeout") or 3600) + 900)
        except Exception as e:  # noqa: BLE001
            log.exception("video broker acquire failed: %s", e)
            _patch(jid, status="failed",
                   error=f"GPU broker unavailable: {e}")
            return
    try:
        if lease is not None:
            _patch(jid, lease_id=lease.id)
        _run_body(jid)
    finally:
        if lease is not None:
            try:
                import gpu_broker
                gpu_broker.release(lease)
            except Exception:
                pass


def _run_body(jid: str) -> None:
    from renderer import ComfyClient
    with _LOCK:
        _load_jobs()
        j = _JOBS.get(jid)
    if j is None:
        return
    try:
        # Registry entry drives both the ComfyUI instance and the output tree:
        # LTX-2.5 / H3 run on their own instances (registry `comfy_port` /
        # `comfy_root`); Wan keeps the production one.
        entry = video_models.VIDEO_MODELS.get(j.get("model") or "") or {}
        # worker routing: worker jobs execute on the
        # worker's own ComfyUI via its agent; the start image is pushed
        # there first and artefacts are pulled back through the result
        # store — outputs still land in OUT_DIR exactly as local runs.
        wclient = None
        if j.get("worker") and j["worker"] != "local":
            import workers as workers_mod
            wclient = workers_mod.client_for(j["worker"])
            client = wclient
            start_name = str(j.get("start_image") or "")
            src = _resolve_start_image_path(start_name) if start_name else None
            if src is not None:
                up = client.upload_image(src)
                wf = json.loads(json.dumps(j["workflow"]))
                for node in wf.values():
                    if isinstance(node, dict) and \
                            node.get("class_type") == "LoadImage":
                        node.setdefault("inputs", {})["image"] = up
                j["workflow"] = wf
        else:
            port = entry.get("comfy_port") or load_config()["ensemble"]["comfyui_port"]
            client = ComfyClient(port)
        comfy_root = _model_comfy_root(entry)

        # Reference stills are uploaded to the TARGET ComfyUI and wired into
        # the family's guide here (needs a live client for uploads). LTX uses
        # the MSR slot map; MiniMax H3 uses its ordered Ref2VA ref_images dict
        # plus the frame-0 identity anchor.
        if j.get("references"):
            if str(entry.get("family", "")).startswith("h3"):
                j["workflow"] = _apply_references_h3(
                    j["workflow"], entry, j["references"], client,
                    continuation=j.get("continuation"))
            else:
                j["workflow"] = _apply_references(
                    j["workflow"], entry, j["references"], client)
        # The GPU broker has already evicted and verified every other
        # instance before this job acquired its lease (see _run).
        t0 = time.time()
        pid = client.queue_prompt(j["workflow"])
        j["prompt_id"] = pid          # `j` is detached once _patch reloads
        _patch(jid, prompt_id=pid, status="running")

        hist = client.wait_for(pid, timeout_s=j.get("timeout", 3600))
        secs = time.time() - t0
        if not hist:
            # The farm may still be sampling (cold model load + a slow card
            # can run past the deadline). Leaving it running pegs the GPU and
            # produces a clip nobody waits for, and the next attempt queues
            # BEHIND it — stop it before raising.
            try:
                client.interrupt()
                log.warning("render timed out — interrupted the farm to free "
                            "the GPU (prompt %s)", pid)
            except Exception as e:  # noqa: BLE001
                log.warning("timeout interrupt failed: %s", e)
            raise RuntimeError("timeout waiting for ComfyUI")
        j.update(_collect_outputs(j, hist, secs, comfy_root, wclient))
    except Exception as e:
        j.update(status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        # ⚠ `j` is a plain dict reference. Persist through `_patch` so the
        # update lands in the CURRENT `_JOBS` (see `_load_jobs`).
        upd = {k: j.get(k) for k in ("seconds", "seconds_per_frame",
                                     "output_dir", "copy_warnings")
               if j.get(k) is not None}
        if j.get("prompt_id"):
            upd["prompt_id"] = j["prompt_id"]
        _patch(jid, status=j.get("status", "failed"),
               outputs=j.get("outputs", []), error=j.get("error", ""), **upd)


def _collect_outputs(j: dict, hist: dict, secs: float, comfy_root: Path,
                     wclient=None) -> dict:
    """Turn a finished ComfyUI history entry into the job's final fields,
    copying every artefact into OUT_DIR (shared by _run_body and the
    startup adopter)."""
    st = hist.get("status", {}) or {}
    ok = st.get("status_str") == "success" and not st.get("error")

    outs = []
    for node_out in (hist.get("outputs") or {}).values():
        for key in ("images", "gifs", "video", "videos"):
            for f in node_out.get(key, []) or []:
                if f.get("filename"):
                    outs.append(f["filename"])

    # ComfyUI always writes into its own output tree. Copy the artefacts
    # into the Video Studio's own directory so experiments are physically
    # separated from production renders (independent output dir).
    import shutil
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    copy_warnings = []
    # Fallback roots: a registry entry with a stale/absent comfy_root must
    # not silently lose its render (measured: three 1080p entries pointed
    # at the benchmark tree while the farm rendered into ComfyUI_LTX).
    fallback_roots = [ROOT / "ComfyUI_LTX" / "output",
                      ROOT / "ComfyUI" / "output"]
    for name in outs:
        try:
            dst = OUT_DIR / f"{j['model']}_{j['id']}_{Path(name).name}"
            if wclient is not None:
                wclient.download("", name, dst)   # agent-pushed artefact
            else:
                src = _find_output(name, comfy_root / "output")
                if not src:
                    for fb in fallback_roots:
                        src = _find_output(name, fb)
                        if src:
                            log.warning(
                                f"{j['id']}: output '{name}' found under "
                                f"{fb} — comfy_root for '{j['model']}' is "
                                f"stale; fix the registry entry")
                            break
                if src:
                    shutil.copyfile(src, dst)
                else:
                    copy_warnings.append(
                        f"output not found in any instance tree: {name}")
                    continue
            copied.append(dst.name)
        except Exception as e:
            copy_warnings.append(f"could not copy {name}: {e}")
            log.warning(f"could not copy output {name}: {e}")

    return {"status": "completed" if ok else "failed",
            "seconds": round(secs, 1),
            "seconds_per_frame": round(secs / max(int(j.get("frames") or 1), 1), 3),
            "outputs": copied or outs,
            "copy_warnings": copy_warnings,
            "output_dir": str(OUT_DIR),
            "error": "" if ok else json.dumps(st.get("messages", []))[:600]}


def resolve_references(mid: str, references: list[dict] | None, *,
                       start_image: str | None = None,
                       continuation: Path | None = None,
                       log: logging.Logger = log
                       ) -> tuple[list[dict], str | None, list[str]]:
    """The reference set one Video Studio clip of `mid` renders with:
    (references, continuation frame, warnings), exactly what start() builds
    the graph from. `continuation` is the previous clip's tail frame, already
    resolved to a local path. H3 numbers its <Picture N> tags in slot order
    (_apply_references_h3) and pins the continuation frame, else its first
    picture, as frame 0. The prompt checks (prompt_check) plan a clip's
    pictures from this same function, with a silent `log`."""
    m_entry = video_models.VIDEO_MODELS.get(mid) or {}
    references = list(references or [])
    fam_name = str(m_entry.get("family", ""))
    if continuation is not None and fam_name.startswith(("h3", "ltx")):
        from ltx_refs import add_continuation
        references, note = add_continuation(references, str(continuation), fam_name)
        if note:
            # no subject reference yet: hand the tail over as the
            # continuation slot; the promotion below makes it pic1
            references.append({"slot": "continuation", "path": str(continuation),
                               "source": "continuation:prev_tail"})

    # Reference stills for LTX-2.5 (MSR slots) and H3 (ordered ref_images).
    # Only existing files survive; unknown slots and duplicates are dropped
    # with a warning. Slot order is priority order: pic1..pic6 are subjects
    # (LTX uses pic1..pic4 only), background is the location.
    REF_SLOTS = _REF_SLOT_ORDER
    refs_clean: list[dict] = []
    ref_warnings: list[str] = []
    cont_path: str | None = None
    seen_slots: set[str] = set()
    for r in references:
        r = r or {}
        slot = r.get("slot")
        if slot == "continuation":
            # the previous shot's tail frame: NOT a picture slot. H3 pins it
            # as the frame-0 anchor; it is never uploaded as a reference image.
            from ltx_refs import resolve_reference_entry
            p = resolve_reference_entry(r)
            if p is not None:
                cont_path = str(p)
            else:
                ref_warnings.append("continuation frame could not be resolved")
            continue
        if slot not in REF_SLOTS:
            log.warning(f"{mid}: ignoring reference with bad slot: {r!r}")
            ref_warnings.append(f"bad slot: {slot!r}")
            continue
        if slot in seen_slots:
            log.warning(f"{mid}: duplicate reference slot {slot} ignored")
            ref_warnings.append(f"duplicate slot {slot}")
            continue
        # absolute/local path OR batch reference ({type,name,file})
        from ltx_refs import resolve_reference_entry
        p = resolve_reference_entry(r)
        if p is None:
            log.warning(f"{mid}: reference not resolvable, slot {slot} skipped: {r!r}")
            ref_warnings.append(
                f"slot {slot}: could not resolve "
                f"{r.get('type') or '?'}/{r.get('name') or '?'}/"
                f"{r.get('file') or '?'}")
            continue
        entry = {"slot": slot, "path": str(p)}
        for k in ("type", "name", "file"):
            if r.get(k):
                entry[k] = r[k]
        refs_clean.append(entry)
        seen_slots.add(slot)
    if references and not refs_clean:
        ref_warnings.append("NO reference could be resolved — the render "
                            "falls back to text-to-video")
    if refs_clean and not m_entry.get("ref_nodes"):
        log.warning(f"{mid}: references supplied but the model declares no ref_nodes; ignored")
        ref_warnings.append("model declares no reference nodes — ignored")
        refs_clean = []
    # Per-model reference capacity (LTX MSR = 5 slots; H3 = its ref_load_nodes
    # count, 6 after the 2026-09 extension). Never claim more provenance than
    # the graph actually receives: trim with an explicit warning.
    _cap = len(m_entry.get("ref_load_nodes") or []) or 5
    if len(refs_clean) > _cap:
        dropped = refs_clean[_cap:]
        ref_warnings.append(
            f"{len(refs_clean)} references resolved but {mid} accepts {_cap} "
            f"pictures — dropped: "
            + ", ".join(str(d.get("slot")) for d in dropped))
        log.warning(f"{mid}: trimming {len(dropped)} references over capacity {_cap}")
        refs_clean = refs_clean[:_cap]
    # LTX MSR REQUIRES a subject reference (pic1). A background-only set (the
    # shot's characters had no identity ref) cannot ride the MSR guide: drop
    # it here so provenance is honest and the t2v template is used, instead of
    # the injector raising "LTX reference graph has no pic1 after injection".
    if refs_clean and str(m_entry.get("family", "")).startswith("ltx"):
        _subj = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6")
        if not any(str(r.get("slot") or "") in _subj for r in refs_clean):
            ref_warnings.append(
                "no character identity reference resolved — the shot renders "
                "as text-to-video; attach cast refs in the Cast tab")
            log.warning(f"{mid}: background-only references dropped "
                        f"(no pic1) — text-to-video")
            refs_clean = []
    # H3 pins its FIRST reference as frame 0 (identity anchor). An
    # environment-only set would make the clip OPEN on the environment still —
    # drop it and render t2v instead, with the same warning.
    if refs_clean and str(m_entry.get("family", "")).startswith("h3"):
        if not any(str(r.get("slot") or "").startswith("pic") for r in refs_clean):
            ref_warnings.append(
                "no character identity reference resolved — the shot renders "
                "as text-to-video; attach cast refs in the Cast tab")
            log.warning(f"{mid}: environment-only references dropped "
                        f"(frame-0 anchor needs a subject) — text-to-video")
            refs_clean = []
    # (A continuation with no subject reference is promoted to pic1 further
    # down for H3/LTX; families without a reference system drop it there.)

    # H3 i2v: a start image with no references is promoted to reference #1.
    # The Ref2VA graph pins reference #1 as the frame-0 identity anchor, so
    # the picked image drives identity — the t2v template has no START_IMAGE
    # slot and used to drop it silently. _build applies the same promotion as
    # a backstop; populating refs_clean HERE is what makes the render-time
    # injection (_apply_references_h3) actually wire the anchor.
    can_ref = video_models.takes_reference_images(mid)
    if not refs_clean and start_image and not can_ref and \
            str(m_entry.get("family", "")).startswith(("h3", "ltx")):
        ref_warnings.append("start image ignored - this tier has no reference "
                            "graph to carry it (choose a refs tier)")
        log.warning(f"{mid}: start image ignored - no reference graph")
    if not refs_clean and start_image and can_ref and \
            str(m_entry.get("family", "")).startswith("h3"):
        _sp = _resolve_start_image_path(start_image)
        if _sp is not None:
            refs_clean = [{"slot": "pic1", "path": str(_sp),
                           "source": "i2v_start_image"}]
            log.info(f"{mid}: i2v start image promoted to the H3 frame-0 "
                     f"identity anchor: {_sp}")
        else:
            log.warning(f"{mid}: start image '{start_image}' not found in any "
                        f"ComfyUI input dir — rendering text-to-video")
    # LTX i2v: same gap, LTX-native mechanism. The LTX workflow map has no i2v
    # graph (refs_*/t2v_* only) and its t2v templates carry no START_IMAGE
    # slot, so a start image was silently dropped. The MSR guide treats
    # pic1 as the SUBJECT identity reference applied at stage 1 — promote the
    # start image there for refs-capable LTX entries.
    if not refs_clean and start_image and \
            str(m_entry.get("family", "")).startswith("ltx") and \
            "refs" in (m_entry.get("supported_controls") or []) and \
            m_entry.get("ref_nodes"):
        _sp = _resolve_start_image_path(start_image)
        if _sp is not None:
            refs_clean = [{"slot": "pic1", "path": str(_sp),
                           "source": "i2v_start_image"}]
            log.info(f"{mid}: i2v start image promoted to the LTX MSR "
                     f"pic1 subject reference: {_sp}")
        else:
            log.warning(f"{mid}: start image '{start_image}' not found in any "
                        f"ComfyUI input dir — rendering text-to-video")

    # H3/LTX i2v: a continuation path with no subject reference yet gets the
    # tail promoted to pic1 as well (bench chain config — the tail doubles as
    # the pic1 identity reference AND the frame-0 anchor). This ensures the
    # two-phase refs graph is selected, not the t2v fallback.
    if cont_path and not refs_clean and can_ref and \
            (str(m_entry.get("family", "")).startswith("h3") or
             str(m_entry.get("family", "")).startswith("ltx")):
        refs_clean = [{"slot": "pic1", "path": cont_path,
                       "source": "continuation:prev_tail"}]
        log.info(f"{mid}: continuation tail promoted to pic1 (no other "
                 f"subject refs)")
    if cont_path and not refs_clean:
        # no reference system to carry it (the H3 t2v graph has no guide
        # node, and the LTX MSR guide hard-requires pic1)
        ref_warnings.append("continuity frame dropped — no subject reference "
                            "for the anchor (renders as a plain cut)")
        cont_path = None

    # LTX i2v + cast references: the opening frame OWNS pic1 (the lead
    # subject slot — strongest identity/composition weight, the same
    # treatment the chain pin gives the previous tail) and the cast subjects
    # shift one slot down (pic1->pic2, pic2->pic3, pic3->pic4). Without this
    # the start image was silently dropped whenever references were attached
    # (the refs templates carry no START_IMAGE slot).
    if start_image and refs_clean and not cont_path and \
            str(m_entry.get("family", "")).startswith("ltx") and \
            m_entry.get("ref_nodes"):
        _sp = _resolve_start_image_path(start_image)
        if _sp is not None:
            shifted = [{"slot": "pic1", "path": str(_sp),
                        "source": "i2v_start_image"}]
            remap = {"pic1": "pic2", "pic2": "pic3", "pic3": "pic4"}
            for r in refs_clean:
                ns = remap.get(r.get("slot"),
                               "background" if r.get("slot") == "background"
                               else None)
                if ns:
                    shifted.append({**r, "slot": ns})
            dropped = sum(1 for r in refs_clean
                          if remap.get(r.get("slot")) is None
                          and r.get("slot") != "background")
            refs_clean = shifted
            if dropped:
                ref_warnings.append(
                    f"start image took pic1 — {dropped} cast subject(s) "
                    f"beyond pic4 dropped")
            log.info(f"{mid}: i2v start image owns pic1; cast shifted "
                     f"({len(refs_clean)} refs total)")

    # H3 i2v + cast references: the start image must still anchor frame 0
    # (the clip opens exactly on the picked frame) while the cast rides the
    # Picture slots. Without this the Browse image lost the anchor to the
    # lead cast reference whenever cast refs were attached.
    if start_image and refs_clean and not cont_path and \
            str(m_entry.get("family", "")).startswith("h3"):
        _sp = _resolve_start_image_path(start_image)
        if _sp is not None:
            cont_path = str(_sp)
            log.info(f"{mid}: i2v start image pinned as the frame-0 anchor "
                     f"alongside {len(refs_clean)} cast reference(s): {_sp}")
    return refs_clean, cont_path, ref_warnings


def start(mid: str, prompt: str, *, mode: str = "t2v", negative: str = "",
          seed: int = -1, width: int = 832, height: int = 480, frames: int = 25,
          steps: int | None = None, cfg: float | None = None,
          loras: list[dict] | None = None, start_image: str | None = None,
          teacache_thresh: float = 0.1, teacache_end: float = 0.6,
          no_cache: bool = False, timeout: int | None = None,
          text_encoder: str | None = None,
          worker: str | None = None,
          references: list[dict] | None = None,
          dfr: bool | None = None,
          continuation: str | None = None) -> dict:
    """Validate, build and launch one Video Studio render (background thread).

    ``continuation`` is the previous clip's tail frame (a local image path);
    it is translated per model family: H3 pins it as the frame-0 anchor, LTX
    gives it a free subject slot, Wan uses it as the native first frame.
    Raises ValueError for anything the caller must fix."""
    st = video_models.model_status(mid)
    if not st["available"]:
        # unknown ids carry "reason" instead of "missing" (queueing garbage
        # used to surface as a bare KeyError)
        raise ValueError(
            f"{mid} is not available: {st.get('reason') or 'missing ' + str(st.get('missing'))}")

    d = st.get("defaults", {})
    steps = steps if steps is not None else d.get("steps", 8)
    cfg = cfg if cfg is not None else d.get("cfg", 1.0)
    if seed is None or seed < 0:
        seed = int(time.time() * 1000) % 2_147_483_647

    lim = st.get("frame_limits", {})
    if lim and not (lim.get("min", 0) <= frames <= lim.get("max", 10**9)):
        raise ValueError(f"frames {frames} outside {lim}")

    # LTX-2.5 has a HARD grid: N = 1 + 8k frames and W/H multiples of 64 (the
    # latent is downsampled 32x spatially and 8x temporally, and the two-stage
    # templates sample stage 1 at half size: 512x288 would floor to a 512x256
    # clip). Wan tolerates other values, so the grid is only enforced for
    # entries that declare `strict_frames` / `strict_dims` (registry `step`).
    m_entry = video_models.VIDEO_MODELS.get(mid) or {}
    if m_entry.get("strict_frames"):
        lo = lim.get("min", 9) if lim else 9
        stp = lim.get("step", 8) if lim else 8
        if (frames - lo) % stp:
            raise ValueError(
                f"{mid}: frames must satisfy (N - {lo}) % {stp} == 0 "
                f"(this model's frame grid); got {frames}")
    if m_entry.get("strict_dims"):
        rl = st.get("resolution_limits", {}) or {}
        rstep = int(rl.get("step", 32))
        if width % rstep or height % rstep:
            raise ValueError(
                f"{mid}: width/height must be multiples of {rstep}; got {width}x{height}")
        mx, mn = rl.get("max"), rl.get("min")
        if mx and (width > mx[0] or height > mx[1]):
            raise ValueError(f"{mid}: resolution above {mx}; got {width}x{height}")
        if mn and (width < mn[0] or height < mn[1]):
            raise ValueError(f"{mid}: resolution below {mn}; got {width}x{height}")

    if start_image and not _is_bare_filename(str(start_image)):
        raise ValueError("start_image must be a filename in a ComfyUI input "
                         "folder (use the upload button)")

    # Continuity: a previous clip's tail frame, validated as a local media
    # path. Wan opens on it natively (staged as the start image); H3 and LTX
    # carry it in their reference graphs (resolve_references).
    references = list(references or [])
    cont_file = None
    if continuation:
        from core import InvalidInput, resolve_user_path
        try:
            cont_file = resolve_user_path(continuation)
        except (InvalidInput, FileNotFoundError) as e:
            raise ValueError(f"continuation frame unusable: {e}") from None
        if str(m_entry.get("family", "")).startswith("wan") and not start_image:
            start_image = _stage_input_image(cont_file, "vs_cont")
    refs_clean, cont_path, ref_warnings = resolve_references(
        mid, references, start_image=start_image, continuation=cont_file)

    # only accept LoRAs this model's family actually supports
    compat = loras_for_model(mid)["compatible"]
    clean, rejected = [], []
    for l in (loras or []):
        if l.get("file") in compat:
            clean.append(l)
        else:
            rejected.append(l.get("file"))

    jid = uuid.uuid4().hex[:12]

    # An explicit `text_encoder` filename overrides the model's own encoder.
    te = text_encoder
    # Models that own their encoder in the template (LTX: Gemma 4, loaded with
    # CLIPLoader type "ltxv") report the registry's declared encoder instead of
    # the Wan stock name.
    model_te_label = None
    if m_entry.get("clip_type") and m_entry.get("text_encoder"):
        model_te_label = Path(m_entry["text_encoder"]).name

    if te:
        if Path(te).name != te or te in (".", ".."):
            raise ValueError(f"invalid text encoder name: {te!r}")
        if not model_files.exists(f"models/text_encoders/{te}", load_config(), ROOT):
            raise ValueError(f"text encoder not found: {te}")

    wf, template, te_node = _build(mid, mode, prompt, negative, seed, width,
                                   height, frames, steps, cfg, clean,
                                   start_image, teacache_thresh, teacache_end,
                                   no_cache, jid, te,
                                   references=refs_clean, dfr=dfr)

    # Per-model renders duration cap. Measured worst-case legitimate LTX run is
    # ~9 minutes (1280x704x121, 3 refs); 20 minutes bounds any silent stall
    # without ever killing a healthy render. Wan entries declare no timeout_s
    # and keep the historical 3600 s default.
    if timeout is None:
        timeout = int(m_entry.get("timeout_s") or 3600)

    want_dfr = (bool(dfr) if dfr is not None
                else bool((d or {}).get("dfr", True)))

    rec = {
        "id": jid,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "queued",
        "worker": worker or "local",
        "model": mid, "model_name": st.get("note", ""), "mode": mode,
        "template": template, "workflow": wf,
        "prompt": prompt, "negative": negative, "seed": seed,
        "width": width, "height": height, "frames": frames,
        "steps": steps, "cfg": cfg,
        "loras": clean, "rejected_loras": rejected,
        "start_image": start_image,
        "references": refs_clean,
        "continuation": cont_path,
        "reference_warnings": ref_warnings,
        "dfr": want_dfr,
        "text_encoder": model_te_label or te or STOCK_TE,
        "text_encoder_node": te_node,
        "cache": "none" if no_cache else f"teacache:{teacache_thresh}",
        "teacache_end": teacache_end,
        # Per-model deadline: the registry carries the measured wall time
        # (H3 hero 2700 s, LTX 1200 s). The VS endpoint used to leave this
        # unset, so slow-but-healthy hero renders could be abandoned by the
        # caller while the farm kept working.
        "timeout": int(timeout or st.get("timeout_s") or 3600),
        "outputs": [], "error": "",
        "experimental": True,
    }
    with _LOCK:
        _load_jobs()
        _JOBS[jid] = rec
        _save_jobs()

    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {k: v for k, v in rec.items() if k != "workflow"}


_load_jobs()


# ---------------------------------------------------------- crash reaper
def reap_stale_running(in_flight_killed: bool | None = None) -> dict:
    """Startup pass for jobs a previous control-room process left `running`
    or `queued`. Called from the API lifespan only — never at import, so a
    script or test that imports this module cannot touch live job records.

    * `queued` jobs never reached ComfyUI (the record is written before the
      worker thread submits) — no thread will ever run them: failed.
    * `running` jobs: when the GPU broker reconciles at startup
      (gpu_broker.reconcile_on_start, the default) every local instance is
      reset, so their prompts are gone: failed. Otherwise the target instance
      (registry `comfy_port`) is probed; a prompt that is still queued or
      already finished is ADOPTED — a thread waits for it and collects the
      outputs exactly like a normal run. Worker-PC jobs cannot be re-attached
      and are failed.
    """
    fixed = {"failed": 0, "adopted": 0, "orphaned_queued": 0}
    if in_flight_killed is None:
        try:
            gb = load_config().get("gpu_broker") or {}
            in_flight_killed = bool(gb.get("reconcile_on_start", True))
        except Exception:  # noqa: BLE001
            in_flight_killed = True
    try:
        from renderer import ComfyClient
        default_port = load_config()["ensemble"]["comfyui_port"]
        with _LOCK:
            _load_jobs()
            snapshot = [(jid, dict(j)) for jid, j in _JOBS.items()]
        for jid, j in snapshot:
            status = j.get("status")
            if status not in ("running", "queued"):
                continue
            pid = str(j.get("prompt_id") or "")
            if status == "queued" or not pid:
                fixed["orphaned_queued"] += 1
                fixed["failed"] += 1
                _patch(jid, status="failed",
                       error="control room restarted before this render was "
                             "submitted — run it again")
                continue
            remote = bool(j.get("worker") and j["worker"] != "local")
            if in_flight_killed or remote:
                fixed["failed"] += 1
                _patch(jid, status="failed",
                       error=("control room restarted mid-render (the worker "
                              "job can no longer be tracked)" if remote else
                              "control room restarted mid-render (the GPU "
                              "broker resets every ComfyUI instance at startup)"))
                continue
            entry = video_models.VIDEO_MODELS.get(str(j.get("model") or "")) or {}
            port = int(entry.get("comfy_port") or default_port)
            client = ComfyClient(port)
            try:
                q = client.queue_info() or {}
                live = {e[1] for e in (q.get("queue_running") or [])
                        + (q.get("queue_pending") or [])
                        if isinstance(e, (list, tuple)) and len(e) >= 2}
                done = pid in (client.history(pid) or {})
            except Exception:  # noqa: BLE001
                live, done = set(), False
            if pid in live or done:
                fixed["adopted"] += 1
                threading.Thread(target=_adopt, args=(jid, port, pid, entry),
                                 daemon=True, name=f"vs-adopt-{jid}").start()
            else:
                fixed["failed"] += 1
                _patch(jid, status="failed",
                       error="control room restarted and the render is no "
                             f"longer known to ComfyUI :{port}")
    except Exception as e:
        log.error("reap_stale_running failed: %s", e)
    if fixed["failed"] or fixed["adopted"]:
        log.info("video studio reaper: %s", fixed)
    return fixed


def _adopt(jid: str, port: int, pid: str, entry: dict) -> None:
    """Finish a render that outlived the control room that submitted it."""
    from renderer import ComfyClient
    with _LOCK:
        _load_jobs()
        j = dict(_JOBS.get(jid) or {})
    if not j:
        return
    t0 = time.time()
    try:
        hist = ComfyClient(port).wait_for(pid, timeout_s=int(j.get("timeout") or 3600))
        if not hist:
            raise RuntimeError("timeout waiting for the adopted render")
        upd = _collect_outputs(j, hist, time.time() - t0, _model_comfy_root(entry))
        upd["note"] = "adopted after a control-room restart (timing partial)"
    except Exception as e:  # noqa: BLE001
        upd = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    _patch(jid, **upd)
    log.info(f"video studio: adopted job {jid} -> {upd.get('status')}")


# ------------------------------------------------------- chain runner
CHAIN_MAX_SEGMENTS = 50        # the most segments one chain is written for (the cast sheet)


def start_chain(mid: str, prompts: list, *, mode: str = "t2v",
                negative: str = "", seed: int = -1,
                width: int = 1344, height: int = 768,
                frames: int = 124, steps: int | None = None,
                cfg: float | None = None, start_image: str | None = None,
                worker: str | None = None,
                references: list | None = None,
                draft_only: bool = False,
                chain_id: str | None = None,
                start_path: Path | None = None,
                origin: dict | None = None,
                seeds: list | None = None) -> dict:
    """Multi-segment chain rendered as one take. Runs in a background thread;
    check get_chain() for progress. `references` carries the cast (character /
    power / environment stills), applied to EVERY segment with the model
    family's own reference system.

    LTX-2.5 chain models and MiniMax H3 run the seamless engine (chain_studio:
    pinned per-stage carries, one reference set, one-piece audio, QA'd take);
    `draft_only` stops those after the low-resolution preview (status
    "draft_ready") until finish_chain(). Other families keep the tail-frame
    continuation.

    In-process callers (the movie pipeline) may pass:
      chain_id    a stable id (hex, 12-32): a re-run with the same id, seeds,
                  prompts and stills reuses the steps already rendered (uploads
                  are named after their content, renderer.upload_name);
      start_path  the opening still as a local path (a previous shot's tail),
                  instead of a ComfyUI input filename;
      origin      provenance stored on the record (e.g. project + shot ids);
      seeds       one seed per segment instead of seed, seed+1, ... (a movie
                  take keeps each shot's own: a retake moves only its shot's,
                  and the segments before it reuse their finished steps)."""
    st = video_models.model_status(mid)
    if not st["available"]:
        raise ValueError(
            f"{mid} is not available: {st.get('reason') or 'missing ' + str(st.get('missing'))}")
    if seeds is not None:
        if len(seeds) != len(prompts):
            raise ValueError(f"{len(seeds)} seeds for {len(prompts)} segments")
        seeds = [int(s) for s in seeds]
        if any(s < 0 for s in seeds):
            raise ValueError("segment seeds must not be negative")
    if start_image and not _is_bare_filename(str(start_image)):
        raise ValueError("start_image must be a filename in a ComfyUI input folder")
    # refused here, not warned about in the chain thread: a chain whose opening
    # frame is missing would render as if none had been chosen
    if start_image and _resolve_start_image_path(str(start_image)) is None:
        raise ValueError(f"start image {start_image!r} is not in a ComfyUI input folder")
    if start_path is not None and not Path(start_path).is_file():
        raise ValueError(f"start image {start_path} does not exist")
    if chain_id is not None:
        if not re.fullmatch(r"[a-f0-9]{12,32}", chain_id):
            raise ValueError("chain_id must be 12-32 lowercase hex characters")
        if chain_running(chain_id):
            raise ValueError(f"chain {chain_id} is already {_CHAINS[chain_id].get('status')!r}")
    cid = chain_id or uuid.uuid4().hex[:12]
    rec = {
        "id": cid, "type": "chain", "model": mid,
        "prompts": list(prompts), "mode": mode,
        "status": "running", "segments": [],
        "current": 0, "total": len(prompts),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed, "error": "", "outputs": [],
        "references": list(references or []),
        "draft_only": bool(draft_only), "frames": int(frames),
        "seamless": seamless_engine(mid),
        **({"origin": dict(origin)} if origin else {}),
        **({"seeds": list(seeds)} if seeds is not None else {}),
    }
    _CHAINS[cid] = rec
    import chain_studio
    chain_studio.save_record(rec)
    threading.Thread(target=_run_chain, args=(cid, mid, prompts, mode,
                      negative, seed, width, height, frames, steps, cfg,
                      start_image, worker, list(references or [])),
                     kwargs={"start_path": start_path, "seeds": seeds}, daemon=True).start()
    return rec


def seamless_engine(mid: str) -> str | None:
    """The seamless engine chains of `mid` render with ('ltx' / 'h3'), or None:
    the model has none, or settings `video_studio.seamless_chains` is off and
    chains keep the tail-frame continuation. The one place that decides it,
    for Video Studio chains, the movie's continuous takes and the pickers' ⛓."""
    if not (load_config().get("video_studio") or {}).get("seamless_chains", True):
        return None
    return video_models.seamless_kind(video_models.VIDEO_MODELS.get(mid) or {})


def chain_running(cid: str) -> bool:
    """Whether this process is rendering the chain right now (a persisted
    record left 'running' by an earlier process is not)."""
    rec = _CHAINS.get(cid)
    return bool(rec) and rec.get("status") not in ("completed", "failed", "draft_ready")


def get_chain(cid: str):
    """The chain record (in memory, or persisted by a previous studio run)."""
    rec = _CHAINS.get(cid)
    if rec is None:
        import chain_studio
        rec = chain_studio.load_record(cid)
    return rec


# chain artefacts in OUT_DIR: chain_<cid[:8]>_<final|draft|segN|tail17_N>.mp4
_CHAIN_FILE = re.compile(r"chain_([0-9a-f]{8})_(final|draft|seg(\d+))\.mp4$")
_CHAIN_SETTLED = ("completed", "failed", "draft_ready")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def history(limit: int = 60) -> list[dict]:
    """Everything the studio rendered, newest first, as one list: single clips
    (the job store, kind "clip") and chains (kind "chain": their records, the
    movie's continuous takes included). Chains rendered before chain records
    existed are rebuilt from their files. `video` names the playable result
    in OUT_DIR (the final take, else the draft, else the newest segment). A
    persisted chain left mid-run by an earlier studio process reads
    "interrupted". Workflow graphs stay out of the list."""
    import chain_studio
    with _LOCK:
        _load_jobs()
        clips = [{k: v for k, v in j.items() if k != "workflow"} for j in _JOBS.values()]
    items: list[dict] = []
    for j in clips:
        vid = next((o for o in j.get("outputs") or [] if str(o).lower().endswith((".mp4", ".webm"))), None)
        items.append({**j, "kind": "clip", "video": Path(vid).name if vid else None})

    records: dict[str, dict] = {}
    if chain_studio.CHAIN_DIR.is_dir():
        for p in chain_studio.CHAIN_DIR.glob("*.json"):
            rec = chain_studio.load_record(p.stem)
            if rec and rec.get("id"):
                if rec.get("status") not in _CHAIN_SETTLED:
                    rec = {**rec, "status": "interrupted"}
                records[rec["id"]] = rec
    records.update({cid: dict(r) for cid, r in list(_CHAINS.items())})   # live state wins

    files: dict[str, dict] = {}
    if OUT_DIR.is_dir():
        for p in OUT_DIR.glob("chain_*.mp4"):
            m = _CHAIN_FILE.match(p.name)
            if not m:
                continue
            f = files.setdefault(m.group(1), {"segs": [], "mtime": 0.0})
            f["mtime"] = max(f["mtime"], p.stat().st_mtime)
            if m.group(3):
                f["segs"].append((int(m.group(3)), p.name))
            else:
                f[m.group(2)] = p.name

    def _name(path) -> str | None:
        return Path(str(path)).name if path else None

    for cid, rec in records.items():
        f = files.pop(cid[:8], {})
        video = (_name(rec.get("final_video")) or _name(rec.get("draft_video"))
                 or f.get("final") or f.get("draft"))
        items.append({**rec, "kind": "chain", "video": video,
                      "segments": len(rec.get("prompts") or []) or len(f.get("segs") or [])})
    for short, f in files.items():           # chains from before chain records
        segs = sorted(f["segs"])
        items.append({"kind": "chain", "id": short, "ts": _iso(f["mtime"]), "legacy": True,
                      "status": "completed" if f.get("final") else "partial",
                      "video": f.get("final") or f.get("draft") or (segs[-1][1] if segs else None),
                      "segments": len(segs), "outputs": [n for _, n in segs]})
    items.sort(key=lambda it: str(it.get("ts") or ""), reverse=True)
    return items[:max(1, int(limit))]


def finish_chain(cid: str) -> dict:
    """Finish a seamless chain that stopped at its draft preview: renders the
    1080p pass from the saved drafts under one broker lease."""
    import chain_studio
    rec = get_chain(cid)
    if rec is None:
        raise ValueError("chain not found")
    if rec.get("status") != "draft_ready":
        raise ValueError(f"chain is {rec.get('status')!r}, not draft_ready")
    entry = video_models.VIDEO_MODELS.get(rec["model"]) or {}
    if video_models.seamless_kind(entry) is None:
        raise ValueError(f"{rec['model']} has no seamless chain support")
    _CHAINS[cid] = rec
    rec["status"] = "finishing"
    chain_studio.save_record(rec)

    def _go() -> None:
        lease = None
        try:
            import gpu_broker
            from renderer import ComfyClient
            port = int(entry.get("comfy_port") or load_config()["ensemble"]["comfyui_port"])
            lease = gpu_broker.acquire(model=rec["model"], port=port, purpose="chain", timeout=3600,
                                       hold_s=len(rec.get("prompts") or []) * int(entry.get("timeout_s") or 2700)
                                       + 1800)
            chain_studio.resume_chain(rec, entry, ComfyClient(port))
        except Exception as e:  # noqa: BLE001
            rec["status"], rec["error"] = "failed", str(e)[:500]
            chain_studio.save_record(rec)
            log.error(f"[chain {cid[:8]}] finish failed: {e}")
        finally:
            if lease is not None:
                try:
                    import gpu_broker
                    gpu_broker.release(lease)
                except Exception:
                    pass

    threading.Thread(target=_go, daemon=True).start()
    return rec


def _count_frames(video_path: str, ff: str) -> int | None:
    """Exact decoded frame count of a video file."""
    import subprocess
    import re as _re
    try:
        o = subprocess.run([ff, "-v", "info", "-i", str(video_path),
                            "-map", "0:v:0", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        m = _re.findall(r"frame=\s*(\d+)", o.stderr or "")
        return int(m[-1]) if m else None
    except Exception:
        return None


def _anchor_identity_score(png_path: str) -> dict:
    """Identity-usability of a candidate H3 anchor frame (pure OpenCV).

    H3's frame-0 anchor is the model's DOMINANT identity signal — when it is
    a dark / backlit / face-less silhouette the model improvises appearance
    from training priors (measured 2026-10-03: dark-haired lead drifted to
    white-haired after a 'glowing silhouette' terminal frame). Score = face
    presence/size + sharpness + exposure so the chain runner can pick a
    better anchor and WARN when none exists."""
    import cv2
    import numpy as _np
    import os as _os
    img = cv2.imread(str(png_path))
    if img is None:
        return {"score": 0.0, "face_frac": 0.0, "sharp": 0.0, "dark": 1.0}
    h, w = img.shape[:2]
    scale = 480.0 / max(w, 1)
    small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale)))) \
        if scale < 1 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    face_frac = 0.0
    cascades = []
    # cv2.data.haarcascades points at the cascade directory itself
    base = cv2.data.haarcascades
    for name in ("haarcascade_frontalface_default.xml",
                 "haarcascade_profileface.xml"):
        p = _os.path.join(base, name)
        if _os.path.isfile(p):
            c = cv2.CascadeClassifier(p)
            if not c.empty():
                cascades.append(c)
    if cascades:
        area_small = small.shape[0] * small.shape[1]
        best = 0
        for c in cascades:
            faces = c.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5,
                                       minSize=(36, 36))
            for (fx, fy, fw, fh) in (faces if faces is not None else []):
                best = max(best, fw * fh)
            if best:
                break
            # profile faces: try the mirror too
            faces = c.detectMultiScale(cv2.flip(gray, 1), scaleFactor=1.12,
                                       minNeighbors=5, minSize=(36, 36))
            for (fx, fy, fw, fh) in (faces if faces is not None else []):
                best = max(best, fw * fh)
            if best:
                break
        face_frac = best / max(1, area_small)

    sharp = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 300.0)
    dark = float((gray < 38).mean())
    exposure = 1.0 - min(1.0, dark / 0.5)
    score = (0.60 * min(1.0, face_frac / 0.05) + 0.20 * sharp
             + 0.20 * exposure)
    return {"score": round(score, 3), "face_frac": round(face_frac, 4),
            "sharp": round(sharp, 3), "dark": round(dark, 3)}


def _extract_anchor_frame(video_path: str, out_png: str, lookback: int = 36,
                          seg_label: str = "") -> dict | None:
    """Choose the H3 chain anchor from the clip's final seconds.

    Returns None when the identity scorer is unavailable (caller falls back
    to the exact-last-frame extraction). Otherwise: extracts the last
    `lookback` frames, scores each with _anchor_identity_score, and picks the
    LATEST frame within 90% of the best score (near-ties keep motion
    continuity). When the best score is still poor the exact last frame is
    kept AND a warning is raised for the chain record / UI."""
    import subprocess
    import tempfile
    import shutil as _shutil
    import glob as _glob
    import os as _os
    try:
        import cv2  # noqa: F401 — availability probe
    except ImportError:
        return None
    try:
        cfg = load_config()
        ff = ffmpeg_exe(cfg)
        total = _count_frames(video_path, ff)
        if not total or total < 6:
            return None
        n = max(2, min(lookback, total))
        start = total - n
        tmp = tempfile.mkdtemp(prefix="anchor_scan_")
        try:
            subprocess.run([ff, "-y", "-v", "error", "-i", str(video_path),
                            "-vf", f"select=gte(n\\,{start}),scale=480:-2,"
                            f"setpts=N/FRAME_RATE/TB",
                            _os.path.join(tmp, "f%04d.png")],
                           capture_output=True, timeout=300)
            frames = sorted(_glob.glob(_os.path.join(tmp, "f*.png")))
            if not frames:
                return None
            scores = [_anchor_identity_score(f) for f in frames]
            best_score = max(s["score"] for s in scores)
            last = len(frames) - 1
            # Deviate from the exact last frame ONLY when a meaningfully
            # better identity carrier exists nearby (>= 0.45). If the whole
            # tail is identity-poor, keep the last frame — a jump back buys
            # nothing and costs motion continuity — and just warn.
            if best_score >= 0.45:
                chosen = max(i for i, s in enumerate(scores)
                             if s["score"] >= 0.9 * best_score)
            else:
                chosen = last
            idx = start + chosen
            subprocess.run([ff, "-y", "-v", "error", "-i", str(video_path),
                            "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1",
                            str(out_png)], capture_output=True, timeout=120)
            ok = (_os.path.isfile(out_png) and _os.path.getsize(out_png) > 100)
            warning = None
            if scores[chosen]["score"] < 0.35:
                warning = (f"{seg_label}: anchor frame {idx}/{total - 1} is "
                           f"identity-poor (score "
                           f"{scores[chosen]['score']:.2f}, face "
                           f"{scores[chosen]['face_frac']:.0%}, dark "
                           f"{scores[chosen]['dark']:.0%}) — the next clip "
                           f"may drift in appearance; end clips on a "
                           f"well-lit, face-visible framing of the lead")
            return {"ok": ok, "frame_idx": idx, "total": total,
                    "score": scores[chosen]["score"],
                    "last_score": scores[-1]["score"],
                    "warning": warning}
        finally:
            _shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        log.warning(f"anchor-frame selection failed, using last frame: {e}")
        return None


def _extract_last_frame(video_path: str, out_png: str) -> bool:
    """Extract the very last frame of a video as a PNG start image.

    Frame-index exact (the tail is the continuation anchor — a seek-rounded
    frame measurably weakened it); falls back to a seek when the count is
    unavailable."""
    import subprocess
    try:
        cfg = load_config()
        ff = ffmpeg_exe(cfg)
        total = _count_frames(video_path, ff)
        if total:
            subprocess.run([ff, "-y", "-v", "error", "-i", str(video_path),
                            "-vf", f"select=eq(n\\,{total - 1})",
                            "-frames:v", "1", "-update", "1",
                            str(out_png)], capture_output=True, timeout=60)
        else:
            subprocess.run([ff, "-y", "-v", "error",
                            "-sseof", "-0.1", "-i", str(video_path),
                            "-frames:v", "1", "-update", "1",
                            str(out_png)], capture_output=True, timeout=30)
        import os as _os
        return _os.path.isfile(out_png) and _os.path.getsize(out_png) > 100
    except Exception:
        return False


def _extract_tail_video(video_path: str, out_mp4: str, frames_n: int = 17) -> bool:
    """Extract the last N frames of a video as a compact all-keyframe mp4.

    This is the LTX overlap-extend payload: the next chain segment pins these
    exact frames into its leading latent (stage 1) and again after the latent
    upsampler (stage 2), so the clip literally opens on the previous clip's
    final motion. All-keyframe + crf 12 keeps the payload lossless-ish and
    trivially decodable by ComfyUI's LoadVideo."""
    import subprocess
    try:
        cfg = load_config()
        ff = ffmpeg_exe(cfg)
        total = _count_frames(video_path, ff)
        if not total or total <= frames_n:
            return False
        start = total - frames_n
        subprocess.run([ff, "-y", "-v", "error", "-i", str(video_path),
                        "-vf", f"select=gte(n\\,{start})",
                        "-frames:v", str(frames_n), "-g", "1", "-crf", "12",
                        "-an", str(out_mp4)], capture_output=True, timeout=120)
        import os as _os
        return (_os.path.isfile(out_mp4)
                and _os.path.getsize(out_mp4) > 1000
                and (_count_frames(out_mp4, ff) or 0) == frames_n)
    except Exception:
        return False


def _assemble_chain(seg_paths: list, final_path: str, trims: list,
                    fps: float = 24.0, fade_s: float = 0.08) -> bool:
    """Concatenate chain segments into ONE video with trimmed overlap junctions.

    trims[i] leading frames are dropped from segment i — those are the
    overlap-replay frames the NEXT segment's render was pinned to, so after
    the trim the previous segment's original frames flow straight into the
    first genuinely-generated frame (the seam is exact by construction).

    Audio gets a short fade in/out at every junction instead of a hard cut
    (kills the click without breaking A/V sync — unlike a crossfade, which
    shortens the track and drifts it out of phase with the video).

    When every trim is 0 (H3/Wan chains) the video streams are kept
    bit-exact: audio is rebuilt once and muxed onto a concat-demuxer copy.
    Re-encode is visually lossless (crf 15). Any failure returns False and
    the caller falls back to plain stream-copy concat."""
    import subprocess
    try:
        cfg = load_config()
        ff = ffmpeg_exe(cfg)
        n = len(seg_paths)
        if n < 2:
            return False
        # audio presence per input (LTX renders are video-only; H3 carries
        # aac) — a [i:a] bind on an audio-less input kills the whole graph.
        # Default verbosity: -v error prints no stream info at all.
        has_audio = []
        for p in seg_paths:
            o = subprocess.run([ff, "-i", str(p)],
                               capture_output=True, text=True, timeout=120)
            has_audio.append("Audio:" in (o.stderr or ""))
        uniform_audio = all(has_audio)

        if any(t > 0 for t in trims) or not uniform_audio:
            # filter pass. Video: trim the overlap replay, then re-stamp
            # timestamps by frame index — the pinned segments' container fps
            # metadata can be wrong, and setpts=N/(fps*TB) makes duration
            # exact regardless of what the muxer wrote.
            f = []
            labels = []
            for i in range(n):
                trim = (f"trim=start_frame={int(trims[i])},"
                        f"setpts=PTS-STARTPTS,") if trims[i] else ""
                f.append(f"[{i}:v]{trim}"
                         f"setpts=N/({float(fps):g}*TB),format=yuv420p[v{i}]")
                if uniform_audio:
                    atrim = (f"atrim=start={trims[i] / fps:.6f},"
                             f"asetpts=PTS-STARTPTS,") if trims[i] else ""
                    f.append(f"[{i}:a]{atrim}"
                             f"areverse,afade=t=in:d={fade_s},"
                             f"areverse,afade=t=in:d={fade_s}[a{i}]")
                    labels.append(f"[v{i}][a{i}]")
                else:
                    labels.append(f"[v{i}]")
            if uniform_audio:
                f.append("".join(labels)
                         + f"concat=n={n}:v=1:a=1[vout][aout]")
                amap = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
            else:
                f.append("".join(labels) + f"concat=n={n}:v=1:a=0[vout]")
                amap = ["-an"]
            cmd = [ff, "-y", "-v", "error"]
            for p in seg_paths:
                cmd += ["-i", str(p)]
            cmd += ["-filter_complex", ";".join(f),
                    "-map", "[vout]"] + amap + [
                    "-r", f"{float(fps):g}",
                    "-c:v", "libx264", "-crf", "15", "-preset", "medium",
                    str(final_path)]
            subprocess.run(cmd, capture_output=True, timeout=900)
        else:
            # video stays bit-exact: rebuild audio with junction fades, then
            # mux it onto a concat-demuxer stream copy of the segments
            f = []
            alabels = []
            for i in range(n):
                f.append(f"[{i}:a]areverse,afade=t=in:d={fade_s},"
                         f"areverse,afade=t=in:d={fade_s}[a{i}]")
                alabels.append(f"[a{i}]")
            f.append("".join(alabels) + f"concat=n={n}:v=1:a=1[aout]")
            tmp_audio = str(final_path) + ".m4a"
            cmd = [ff, "-y", "-v", "error"]
            for p in seg_paths:
                cmd += ["-i", str(p)]
            cmd += ["-filter_complex", ";".join(f),
                    "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
                    tmp_audio]
            subprocess.run(cmd, capture_output=True, timeout=300)
            concat_list = str(final_path) + ".txt"
            with open(concat_list, "w", encoding="utf-8") as fh:
                for p in seg_paths:
                    fh.write(f"file '{p}'\n")
            subprocess.run([ff, "-y", "-v", "error", "-f", "concat",
                            "-safe", "0", "-i", concat_list,
                            "-i", tmp_audio, "-map", "0:v:0", "-map", "1:a:0",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-shortest", str(final_path)],
                           capture_output=True, timeout=300)
            try:
                import os as _os
                _os.remove(tmp_audio)
                _os.remove(concat_list)
            except OSError:
                pass
        import os as _os
        return _os.path.isfile(final_path) and _os.path.getsize(final_path) > 1000
    except Exception as e:
        log.warning(f"chain assembly failed: {e}")
        return False


def _run_chain(cid: str, mid: str, prompts: list, mode: str,
               negative: str, seed: int, width: int, height: int,
               frames: int, steps: int, cfg: float,
               start_image: str, worker: str, references: list = None,
               start_path: Path | None = None, seeds: list | None = None) -> None:
    """One broker lease for the whole chain so segments stay warm (chains
    always run on the local instance — _run_chain_body binds it directly).
    The lease belongs to this thread: the H3 engine restarts the server under
    it (chain_studio.StudioRunner.fresh_process)."""
    lease = None
    try:
        import gpu_broker
        entry = video_models.VIDEO_MODELS.get(mid) or {}
        port = int(entry.get("comfy_port")
                   or load_config()["ensemble"]["comfyui_port"])
        # the lease must cover the WHOLE chain, not one render: an expiry
        # mid-chain auto-releases the port, the warm TTL lapses while the
        # next segment is still rendering, and the broker's H3 eviction mode
        # (restart) then kills the instance under the render (measured
        # 2026-10-02: 5-seg hero chain died at segment 3, one hour in).
        per_seg = int(entry.get("timeout_s") or 2700)
        lease = gpu_broker.acquire(
            model=mid, port=port, purpose="chain", timeout=3600,
            hold_s=len(prompts) * per_seg + 1800)
    except Exception as e:  # noqa: BLE001
        _CHAINS[cid]["status"] = "failed"
        _CHAINS[cid]["error"] = f"GPU broker unavailable: {e}"
        return
    try:
        _run_chain_body(cid, mid, prompts, mode, negative, seed, width,
                        height, frames, steps, cfg, start_image, worker,
                        references, start_path=start_path, seeds=seeds)
    except Exception as e:  # noqa: BLE001
        # a dead body must never strand the chain as "running" — the UI
        # polls this record and the user watches it
        _CHAINS[cid]["status"] = "failed"
        _CHAINS[cid]["error"] = str(e)[:500]
        log.error(f"[chain {cid[:8]}] failed: {e}")
    finally:
        try:
            import chain_studio
            chain_studio.save_record(_CHAINS[cid])
        except Exception:  # noqa: BLE001
            pass
        if lease is not None:
            try:
                import gpu_broker
                gpu_broker.release(lease)
            except Exception:
                pass


def chain_cast(mid: str, references: list | None, tag: str = "[chain]",
               log: logging.Logger = log) -> list[dict]:
    """The cast every segment of a chain of `mid` carries, resolved with the
    same contract as start(): valid slots, resolvable files, no duplicate
    slots. Sorted so H3's <Picture N> order is deterministic (pic1..picN,
    background last); empty for a model without a reference graph. The prompt
    checks (prompt_check) plan a chain's pictures from this function, with a
    silent `log`."""
    entry = video_models.VIDEO_MODELS.get(mid) or {}
    fam = str(entry.get("family", ""))
    can_ref = video_models.takes_reference_images(mid)
    from ltx_refs import resolve_reference_entry as _rre
    ref_slots = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6", "background")
    cast_refs: list[dict] = []
    seen_slots: set[str] = set()
    for r in (references or []):
        r = r or {}
        slot = r.get("slot")
        p = _rre(r) if slot in ref_slots else None
        if p is None or slot in seen_slots:
            log.warning(f"{tag} cast reference skipped: "
                        f"slot={slot!r} resolved={p is not None}")
            continue
        seen_slots.add(slot)
        cast_refs.append({"slot": slot, "path": str(p),
                          "type": r.get("type") or "",
                          "name": r.get("name") or "",
                          # optional identity text (age, look, outfit) for the
                          # seamless chains' cast line (chain_studio.with_cast_line)
                          "description": str(r.get("description") or "")[:400]})
    if cast_refs:
        cast_refs.sort(key=lambda r: 9 if r["slot"] == "background"
                       else int(str(r["slot"]).removeprefix("pic") or 9))
    if cast_refs and fam.startswith("wan"):
        log.info(f"{tag} {len(cast_refs)} cast reference(s) "
                 f"ignored — Wan carries identity in the frame itself "
                 f"(no reference system)")
        cast_refs = []
    elif cast_refs and not can_ref:
        log.warning(f"{tag} {len(cast_refs)} cast reference(s) "
                    f"ignored — {mid} has no reference graph")
        cast_refs = []
    elif cast_refs:
        log.info(f"{tag} cast: "
                 + ", ".join(f"{r['slot']}={r['name'] or r['path']}"
                             for r in cast_refs))
    return cast_refs


def _run_chain_body(cid: str, mid: str, prompts: list, mode: str,
                    negative: str, seed: int, width: int, height: int,
                    frames: int, steps: int, cfg: float,
                    start_image: str, worker: str,
                    references: list = None,
                    start_path: Path | None = None,
                    seeds: list | None = None) -> None:
    """Sequential chain: each clip continues from the previous tail frame.
    start_path: a trusted local opening still (seamless engine only);
    seeds: one per segment (default: the base seed + the segment index)."""
    from renderer import ComfyClient
    from workflows import WorkflowBuilder
    import shutil as _shutil
    import os as _os

    entry = video_models.VIDEO_MODELS.get(mid) or {}
    port = entry.get("comfy_port") or load_config()["ensemble"]["comfyui_port"]
    client = ComfyClient(port)
    comfy_root = ROOT / (entry.get("comfy_root") or "ComfyUI")
    out_dir = ROOT / "output" / "videostudio"
    out_dir.mkdir(parents=True, exist_ok=True)
    tail_png = start_image  # first segment may use a user-provided image
    entry_defaults = entry.get("defaults") or {}
    real_steps = steps if steps else (entry_defaults.get("steps") or 8)
    real_cfg = cfg if cfg else (entry_defaults.get("cfg") or 1.0)
    fam = str(entry.get("family", ""))
    # per-segment deadline: the registry's measured wall time (H3 hero
    # 2700 s) or the historical 2700 s default
    seg_timeout = int(entry.get("timeout_s") or 2700)
    import random as _random
    base_seed = int(seed) if seed is not None and int(seed) >= 0 \
        else _random.randrange(1 << 31)
    seg_seeds = [int(s) for s in seeds] if seeds else [base_seed + i for i in range(len(prompts))]
    # LTX overlap-extend: segments 2+ pin the previous clip's last 17 frames
    # in-place (see _build's "chain" branch). Those frames are replayed, so
    # each continuation segment renders OVERLAP-1 extra frames (grid-safe:
    # 1+8k+16 is again 1+8k) and the assembly trims the OVERLAP replay frames.
    use_ltx_chain = (fam.startswith("ltx")
                     and bool((entry.get("workflows") or {}).get("chain")))
    can_ref = video_models.takes_reference_images(mid)
    OVERLAP = 17
    fps = float(entry.get("fps") or 24)
    tail_vid = None      # staged 17-frame tail mp4 (input-dir filename)
    trims: list[int] = []  # per-segment leading frames dropped in assembly

    # Cast references — resolved ONCE, applied to every segment
    cast_refs = chain_cast(mid, references, f"[chain {cid[:8]}]")

    import chain_studio
    seamless = seamless_engine(mid)
    if seamless:
        # one take with pinned per-stage carries (LTX-2.5) / an exact latent
        # carry and a single refine (H3), QA'd at every join — see chain_studio
        if start_path is None and start_image:
            start_path = _resolve_start_image_path(start_image)
            if start_path is None:
                cand = comfy_root / "input" / str(start_image)
                start_path = cand if cand.is_file() else None
            if start_path is None:
                log.warning(f"[chain {cid[:8]}] start image '{start_image}' not resolvable")
        tier_graph = None
        if seamless == "h3":
            tier_refs = cast_refs or ([{"slot": "pic1", "path": str(start_path)}] if start_path else [])
            tier_graph, _tpl, _te = _build(
                mid, mode, str(prompts[0]), negative, base_seed, width, height, frames, real_steps,
                real_cfg, loras=[], start_image=start_image, teacache_thresh=0.1, teacache_end=0.6,
                no_cache=False, shot_id=f"chain_{cid[:8]}_tier", references=tier_refs,
                fps_override=int(entry.get("fps") or 24))
        rec = _CHAINS[cid]
        rec["seed"] = base_seed
        chain_studio.run_chain(rec, entry, client, [str(p) for p in prompts], negative=negative,
                               base_seed=base_seed, frames=int(frames), start_image=start_path,
                               cast=cast_refs, draft_only=bool(rec.get("draft_only")),
                               tier_graph=tier_graph, width=width, height=height, seeds=seg_seeds)
        return

    try:
        for i, prompt in enumerate(prompts):
            _CHAINS[cid]["current"] = i + 1
            _CHAINS[cid]["status"] = f"rendering segment {i+1}/{len(prompts)}"

            # Continuity still: the previous tail (segment 1: the caller's
            # start image). The tail is staged in the target instance's input
            # dir and the stock resolver only searches the two stock input
            # dirs, so fall back to comfy_root/input before giving up.
            si = tail_png
            refs = []
            opening_path = None  # absolute frame this segment opens on
            chain_tail = chain_tail_still = None
            seg_frames = frames
            pin = use_ltx_chain and bool(tail_vid) and i > 0
            if pin:
                chain_tail = tail_vid
                seg_frames = frames + (OVERLAP - 1)
                trims.append(OVERLAP)
                log.info(f"[chain {cid[:8]}] segment {i+1}: overlap-extend pin "
                         f"({OVERLAP} tail frames), rendering {seg_frames}f")
                if si:
                    p = _resolve_start_image_path(si)
                    if p is None:
                        cand = comfy_root / "input" / str(si)
                        p = cand if cand.is_file() else None
                    if p is not None:
                        opening_path = str(p)
            else:
                trims.append(0)
                if si:
                    p = _resolve_start_image_path(si)
                    if p is None:
                        cand = comfy_root / "input" / str(si)
                        p = cand if cand.is_file() else None
                    if p is not None:
                        opening_path = str(p)
                        if not cast_refs and can_ref:
                            refs = [{"slot": "pic1", "path": str(p),
                                     "source": ("chain_continuation" if i
                                                else "i2v_start_image")}]
                        elif not cast_refs:
                            log.warning(f"[chain {cid[:8]}] segment {i+1}: "
                                        f"{mid} has no reference graph - the "
                                        f"continuity image is not used")
                    else:
                        log.warning(f"[chain {cid[:8]}] segment {i+1}: continuity "
                                    f"image '{si}' not resolvable — this segment "
                                    f"renders text-to-video")
            if cast_refs and not (pin and fam.startswith("ltx")):
                # cast identity rides the family's reference system on every
                # segment; the opening frame stays the continuity anchor.
                refs = list(cast_refs)
                if fam.startswith("ltx") and opening_path:
                    # LTX: the opening frame owns pic1 (the lead slot — same
                    # treatment the pin gives the previous tail on segments
                    # 2+); cast subjects shift one slot down.
                    shifted = [{"slot": "pic1", "path": opening_path,
                                "source": ("i2v_start_image" if i == 0
                                           else "chain_continuation")}]
                    remap = {"pic1": "pic2", "pic2": "pic3", "pic3": "pic4"}
                    for r in refs:
                        ns = remap.get(r["slot"],
                                       "background" if r["slot"] == "background"
                                       else None)
                        if ns:
                            shifted.append({**r, "slot": ns})
                    refs = shifted
            # Wan carries the frame natively: the i2v template's first-frame
            # slot (H3/LTX instead ride their reference guides below).
            seg_mode = "i2v" if (refs and fam.startswith("wan")) else mode
            wf, template, te_node = _build(
                mid, seg_mode, prompt, negative, seg_seeds[i], width, height,
                seg_frames, real_steps, real_cfg, loras=[], start_image=si,
                teacache_thresh=0.1, teacache_end=0.6, no_cache=False,
                shot_id=f"chain_{cid[:8]}_seg{i+1}", references=refs,
                dfr=None, chain_tail=chain_tail,
                fps_override=int(entry.get("fps") or 24))

            # Render-time injection — the same dispatch _run uses for
            # single-clip jobs: upload the stills to the target instance and
            # wire the family's guide nodes. Without this the refs template
            # queues with empty LoadImage inputs and ComfyUI rejects the graph.
            if pin and fam.startswith("ltx"):
                # LTX pin segments: the tail still owns pic1 (scene identity
                # from the actual previous frame); cast subjects remap to
                # pic2..pic4 and the environment keeps background.
                inj = []
                if opening_path:
                    inj.append({"slot": "pic1", "path": opening_path,
                                "source": "chain_tail_still"})
                remap = {"pic1": "pic2", "pic2": "pic3", "pic3": "pic4"}
                for r in cast_refs:
                    ns = remap.get(r["slot"],
                                   "background" if r["slot"] == "background"
                                   else None)
                    if ns:
                        inj.append({**r, "slot": ns})
                wf = _apply_references(wf, entry, inj, client)
            elif refs and fam.startswith("h3"):
                # the opening frame (tail — or the user's start image on
                # segment 1) is the frame-0 anchor; cast refs ride the
                # Picture slots for identity that survives occlusion and
                # single-character framings.
                wf = _apply_references_h3(wf, entry, refs, client,
                                          continuation=opening_path)
            elif refs:
                wf = _apply_references(wf, entry, refs, client)

            prefix = f"chain_{cid[:8]}_seg{i+1}"
            for node in wf.values():
                if node.get("class_type") == "SaveVideo":
                    node.setdefault("inputs", {})[
                        "filename_prefix"] = prefix

            t0 = time.time()
            pid = client.queue_prompt(wf, client_id="studio-chain")
            hist = client.wait_for(pid, timeout_s=seg_timeout)
            if not hist:
                # stop the farm before giving up, or the next attempt queues
                # behind a render nobody is waiting for
                try:
                    client.interrupt()
                except Exception as e:  # noqa: BLE001
                    log.warning(f"[chain {cid[:8]}] timeout interrupt failed: {e}")
                raise RuntimeError(f"segment {i+1}: no result after "
                                   f"{seg_timeout}s (render interrupted)")
            st = hist.get("status", {}) or {}
            if st.get("status_str") == "error":
                for msg in st.get("messages", []):
                    if msg[0] == "execution_error":
                        raise RuntimeError(
                            msg[1].get("exception_message", "render failed"))
                raise RuntimeError("render failed")

            # find and copy the output
            seg_path = None
            for _nid, payload in (hist.get("outputs") or {}).items():
                for img in payload.get("images", []):
                    if img.get("type") != "output":
                        continue
                    src = _find_output(img["filename"],
                                       comfy_root / "output")
                    if not src:
                        for fb in (ROOT / "ComfyUI_LTX" / "output",
                                   ROOT / "ComfyUI" / "output"):
                            src = _find_output(img["filename"], fb)
                            if src:
                                break
                    if src:
                        seg_path = out_dir / f"{prefix}.mp4"
                        _shutil.copy2(src, seg_path)
                        break
            if seg_path is None:
                raise RuntimeError(f"segment {i+1}: output not found")

            _CHAINS[cid].setdefault("outputs", []).append(str(seg_path))

            # extract the tail for the NEXT segment's continuation, staged
            # into the target instance's input dir: Wan consumes the still
            # through the template's LoadImage (input-dir-relative name), H3's
            # injection re-uploads it, and LTX chains additionally get the
            # last 17 frames as an all-keyframe mp4 for the overlap pin.
            in_dir = comfy_root / "input"
            in_dir.mkdir(parents=True, exist_ok=True)
            tail_abs = out_dir / f"chain_{cid[:8]}_tail_{i+1}.png"
            # H3: the tail becomes the next clip's frame-0 IDENTITY anchor —
            # prefer a face-bearing, well-exposed frame from the final
            # seconds over a possibly backlit/blurry exact-last frame, and
            # WARN when nothing identity-bearing exists near the end.
            anchor_info = None
            if fam.startswith("h3"):
                anchor_info = _extract_anchor_frame(
                    str(seg_path), str(tail_abs),
                    seg_label=f"segment {i + 1}")
            if anchor_info is not None:
                ok = bool(anchor_info.get("ok"))
            else:
                ok = _extract_last_frame(str(seg_path), str(tail_abs))
            if ok:
                _shutil.copy2(tail_abs, in_dir / tail_abs.name)
                tail_png = tail_abs.name
                if anchor_info is not None:
                    _CHAINS[cid].setdefault("anchor_choices", []).append({
                        "segment": i + 1, "frame_idx": anchor_info["frame_idx"],
                        "total": anchor_info["total"],
                        "score": anchor_info["score"],
                        "last_score": anchor_info["last_score"]})
                    if anchor_info.get("warning"):
                        _CHAINS[cid].setdefault("warnings", []).append(
                            anchor_info["warning"])
                        log.warning(f"[chain {cid[:8]}] "
                                    f"{anchor_info['warning']}")
            else:
                tail_png = None  # next segment starts fresh
            tail_vid = None
            if use_ltx_chain and i + 1 < len(prompts):
                vid_abs = out_dir / f"chain_{cid[:8]}_tail17_{i+1}.mp4"
                if _extract_tail_video(str(seg_path), str(vid_abs), OVERLAP):
                    _shutil.copy2(vid_abs, in_dir / vid_abs.name)
                    tail_vid = vid_abs.name
                else:
                    log.warning(f"[chain {cid[:8]}] tail-video extraction "
                                f"failed — segment {i+2} falls back to the "
                                f"pic1 continuity path")

            elapsed = time.time() - t0
            _CHAINS[cid].setdefault("segment_times", []).append(
                round(elapsed, 1))
            log.info(f"[chain {cid[:8]}] segment {i+1}/{len(prompts)} "
                     f"done in {elapsed:.0f}s -> {seg_path.name}")
            # No /free between segments: the chain holds ONE broker lease so
            # the model stays warm for the next segment. Unloading here would
            # force a full reload per segment (minutes for H3/LTX) and, for
            # H3, /free -> reload is the livelock path the broker avoids.

        # ── assemble all segments into ONE final video ─────────────────────
        seg_outs = _CHAINS[cid].get("outputs") or []
        if len(seg_outs) > 1:
            _CHAINS[cid]["status"] = "concatenating"
            final_path = out_dir / f"chain_{cid[:8]}_final.mp4"
            trim_list = (trims + [0] * len(seg_outs))[:len(seg_outs)]
            assembled = False
            if any(trim_list):
                assembled = _assemble_chain(seg_outs, str(final_path),
                                            trim_list, fps=fps)
            else:
                # no overlap replay: keep the video bit-exact, still smooth
                # the audio junctions
                assembled = _assemble_chain(seg_outs, str(final_path),
                                            [0] * len(seg_outs), fps=fps)
            if not assembled:
                # fallback: the historical stream-copy concat
                concat_list = out_dir / f"chain_{cid[:8]}_concat.txt"
                with open(concat_list, "w", encoding="utf-8") as fh:
                    for p in seg_outs:
                        # concat-demuxer quoting: ' -> '\''
                        safe = str(p).replace("\\", "/").replace("'", "'\\''")
                        fh.write(f"file '{safe}'\n")
                import subprocess as _sp
                ff = ffmpeg_exe()
                _sp.run([ff, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                         "-i", str(concat_list), "-c", "copy", str(final_path)],
                        capture_output=True, timeout=300)
            if final_path.is_file() and final_path.stat().st_size > 1000:
                _CHAINS[cid]["outputs"] = [str(final_path)]
                _CHAINS[cid]["final_video"] = str(final_path)
                log.info(f"[chain {cid[:8]}] assembled {len(seg_outs)} "
                         f"segments -> {final_path.name}")
            else:
                _CHAINS[cid]["outputs"] = seg_outs
                log.warning(f"[chain {cid[:8]}] assembly failed, returning "
                            f"individual segments")
        else:
            _CHAINS[cid]["outputs"] = seg_outs

        _CHAINS[cid]["status"] = "completed"
        log.info(f"[chain {cid[:8]}] completed: {len(seg_outs)} segments")
    except Exception as e:
        _CHAINS[cid]["status"] = "failed"
        _CHAINS[cid]["error"] = str(e)[:500]
        log.error(f"[chain {cid[:8]}] failed: {e}")
