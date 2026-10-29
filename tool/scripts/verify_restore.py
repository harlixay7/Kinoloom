r"""Post-restore verification — run with the venv python and a CLEAN PYTHONPATH.

  set PYTHONPATH= && tool\.venv\Scripts\python.exe ^
      tool\scripts\verify_restore.py

Checks: interpreter + CUDA, that at least one movie model (an LTX-2.5 or
MiniMax H3 tier from the video registry) is installed (hard), the model files
the other workflow templates reference (optional, they warn; the legacy Wan
2.2 files are among them), the IP-Adapter reference-image pair when an SDXL
model is installed, and that the studio's own modules import cleanly.

If a capability is advertised in `models.py` (e.g. `capabilities.refs`), the
files it needs belong in REQUIRED below — that is the only way a missing asset
fails loudly instead of at submit time.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))

OK, BAD, WARN = "OK  ", "FAIL", "WARN"
fails: list[str] = []
warns: list[str] = []


def line(status: str, msg: str) -> None:
    print(f"[{status}] {msg}")
    if status == BAD:
        fails.append(msg)
    elif status == WARN:
        warns.append(msg)


# --------------------------------------------------------------- interpreter
print("=" * 68)
print("1. INTERPRETER + CUDA")
print("=" * 68)
line(OK, f"python {sys.version.split()[0]}  ({sys.executable})")
try:
    import torch
    line(OK, f"torch {torch.__version__}")
    if torch.cuda.is_available():
        line(OK, f"cuda {torch.version.cuda} — {torch.cuda.get_device_name(0)} "
                 f"cap {torch.cuda.get_device_capability(0)}")
        x = torch.randn(512, 512, device="cuda")
        line(OK, f"gpu matmul ok (sum={float((x @ x).sum()):.1f})")
    else:
        line(BAD, "torch.cuda.is_available() is False")
except Exception as e:  # noqa: BLE001
    line(BAD, f"torch: {e!r}")

# -------------------------------------------------------------- model files
print()
print("=" * 68)
print("2. MODEL FILES (tool/workflows.py SUBST)")
print("=" * 68)

# A file counts in models/ and in the model folders of the settings, as it
# does for the studio (tool/model_files.py).
import model_files  # noqa: E402

try:
    from core import load_config
    _EXTRA = model_files.extra_roots(load_config(), ROOT)
except Exception:  # noqa: BLE001 - section 3 reports a studio module that does not import
    _EXTRA = model_files.extra_roots({}, ROOT)


def _where(sub: str, name: str) -> Path | None:
    rel = f"models/{sub}/{name}"
    return model_files.find(rel, _EXTRA, ROOT, model_files.catalog_size(rel, ROOT))


# An SDXL image model on disk turns its IP-Adapter pair into a hard
# requirement (see the reference-image block below).
_SDXL_INSTALLED = any(
    _where(sub, name) is not None
    for sub, name in (("diffusion_models", "RealVisXL_V5.0-Q5_K_M.gguf"),
                      ("checkpoints", "cyberrealisticXL_v100_fp16.safetensors")))

# (folder under models/, file name, hard). A hard entry fails the check; a
# soft one only warns. The movie models (LTX-2.5 / MiniMax H3) are checked
# through the video registry in 2b below. Everything here is optional: the
# studio computes availability from disk and marks a model without its files
# as unavailable instead of failing.
REQUIRED = [
    # --- legacy Wan 2.2 TI2V-5B (workflows.SUBST): renders only when a project
    # picks it; also keyframes.model: wan, face-fix and LoRA training --------
    ("diffusion_models", "Wan2.2-TI2V-5B-Q4_K_M.gguf", False),
    ("text_encoders", "umt5-xxl-encoder-Q4_K_M.gguf", False),
    ("vae", "wan2.2_vae.safetensors", False),
    ("loras", "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors", False),

    # --- optional templates (Wan A14B, legacy LTX-Video 2B, SDXL) ----
    # The A14B and LTX-Video 2B registry entries are disabled or dormant
    # (video_models.py / video_models_user.json); keep them soft.
    ("diffusion_models", "Wan2.2-T2V-A14B-HighNoise-Q3_K_M.gguf", False),
    ("diffusion_models", "Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf", False),
    ("loras", "wan22_t2v_a14b_4step_high.safetensors", False),
    ("loras", "wan22_t2v_a14b_4step_low.safetensors", False),
    ("vae", "Wan2.1_VAE.safetensors", False),
    ("diffusion_models", "ltx-video-2b-v0.9-Q4_K_M.gguf", False),
    ("text_encoders", "t5-v1_1-xxl-encoder-Q4_K_M.gguf", False),
    ("vae", "LTX-Video-VAE-BF16.safetensors", False),
    ("diffusion_models", "RealVisXL_V5.0-Q5_K_M.gguf", False),
    ("vae", "xlVAEC_c91.safetensors", False),
    ("clip", "clip_l.safetensors", False),
    ("clip", "clip_g.safetensors", False),
    ("upscale_models", "RealESRGAN_x4.pth", False),

    # --- reference images (IP-Adapter) -------------------------------------
    # models.py advertises capabilities.refs=True on every SDXL model and
    # build_ref_workflow() hardcodes these two filenames. ComfyUI_IPAdapter_plus
    # has NO auto-download, so without both the feature fails on submit with
    # "clip_name not in list" / "IPAdapter model not found". CLIPVisionLoader
    # looks the clip_vision file up by this EXACT name, so do not rename it.
    # Hard only when an SDXL model is installed.
    ("ipadapter", "ip-adapter-plus_sdxl_vit-h.safetensors", _SDXL_INSTALLED),
    ("clip_vision", "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors", _SDXL_INSTALLED),

    # --- Z-Image Turbo (Images tab, optional) ------------------------------
    ("diffusion_models", "z_image_turbo-Q4_K_M.gguf", False),
    ("vae", "z_image_ae.safetensors", False),
    ("text_encoders", "qwen_3_4b.safetensors", False),

    # --- post-processing additions (2026-09-16) ----------------------------
    # 4xNomos8kDAT is an *alternative* still upscaler, not the default, so it
    # is soft: its absence must not fail a restore.
    ("upscale_models", "4xNomos8kDAT.safetensors", False),
]
for sub, name, hard in REQUIRED:
    p = _where(sub, name)
    if p is not None and p.stat().st_size > 1_000_000:
        line(OK, f"{sub}/{name}  ({p.stat().st_size / 1048576:.0f} MB)"
                 + ("" if p == ROOT / "models" / sub / name else f"  in {p}"))
    else:
        line(BAD if hard else WARN, f"{sub}/{name}  MISSING")

# ------------------------------------------------------------ movie models
print()
print("=" * 68)
print("2b. MOVIE MODELS (tool/video_models.py + video_models_user.json)")
print("=" * 68)
# At least one LTX-2.5 / MiniMax H3 tier must be installed: a project without
# a usable model renders on video_models.default_model_id() and goes to review
# when there is none. Wan 2.2 is legacy and does not count.
try:
    import video_models
    movie = []
    for st in video_models.catalog()["models"]:
        fam = str(st.get("family") or "")
        if st["catalog"]["group"] == "legacy" or not fam.startswith(("ltx", "h3")):
            continue
        movie.append(st["id"])
        if st["available"]:
            line(OK, f"{st['id']}  available")
        elif st.get("missing"):
            line(WARN, f"{st['id']}  missing: {', '.join(st['missing'])}")
        else:
            line(WARN, f"{st['id']}  not on this graphics card: {st.get('gpu') or st.get('reason') or '?'}")
    default = video_models.default_model_id()
    import gpu_support
    if default is None and gpu_support.problem("cu130"):
        line(BAD, f"no movie model runs on this PC: {gpu_support.problem('cu130')}")
    elif default is None:
        line(BAD, f"no movie model installed (checked {len(movie)} LTX-2.5 / "
                  f"H3 tiers) — see docs/setup/RESTORE_INSTRUCTIONS.md step 6")
    elif default != video_models.DEFAULT_MODEL:
        line(WARN, f"default {video_models.DEFAULT_MODEL} is missing; "
                   f"projects without a model render on {default}")
    else:
        line(OK, f"studio default {default} available")
except Exception as e:  # noqa: BLE001
    line(BAD, f"video registry: {type(e).__name__}: {e}")

EXTRA = [
    ROOT / "ComfyUI/models/SEEDVR2/seedvr2_ema_3b-Q8_0.gguf",
    ROOT / "ComfyUI/models/SEEDVR2/ema_vae_fp16.safetensors",
    ROOT / "ComfyUI/custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife/rife47.pth",
    ROOT / "ComfyUI/models/onnx/bbox/face_yolov8m.onnx",
    ROOT / "tools/ffmpeg/bin/ffmpeg.exe",
    ROOT / "tools/git/cmd/git.exe",
    ROOT / "tools/python/python.exe",
    ROOT / "bible/luts/cinematic_teal_amber.cube",
]
print()
for p in EXTRA:
    if p.exists() and p.stat().st_size > 1000:
        line(OK, f"{p.relative_to(ROOT)}  ({p.stat().st_size / 1048576:.1f} MB)")
    else:
        line(WARN, f"{p.relative_to(ROOT)}  MISSING")

# ------------------------------------------------------------------ imports
print()
print("=" * 68)
print("3. STUDIO MODULE IMPORTS")
print("=" * 68)
MODULES = [
    "core", "workflows", "bible", "linter", "renderer", "orchestrator",
    "post", "assembler", "face_qa", "shots_db", "support", "models", "model_files",
    "prompts", "pipeline", "training", "lora_registry", "photo_gen",
    "dataset_gen", "images", "shot_images", "director", "keyframe_gen",
    "lora_video", "projects", "storygen_llm", "batch_gen", "dataset",
    "datasets_db", "jobs", "api",
]
for m in MODULES:
    try:
        __import__(m)
        line(OK, m)
    except Exception as e:  # noqa: BLE001
        line(BAD, f"{m}: {type(e).__name__}: {e}")

# ------------------------------------------------------- comfyui node imports
print()
print("=" * 68)
print("4. COMFYUI CUSTOM NODES (declared deps importable)")
print("=" * 68)
for m in ["gguf", "sentencepiece", "insightface", "onnxruntime", "ultralytics",
          "segment_anything", "sam2", "cv2", "skimage", "piexif", "dill",
          "diffusers", "peft", "rotary_embedding_torch", "einops", "kornia",
          "spandrel", "color_matcher", "mss", "imageio_ffmpeg", "av",
          "transformers", "accelerate", "bitsandbytes", "pyloudnorm", "psutil"]:
    try:
        __import__(m)
        line(OK, m)
    except Exception as e:  # noqa: BLE001
        line(WARN, f"{m}: {type(e).__name__}: {e}")

print()
print("=" * 68)
print(f"RESULT: {len(fails)} failure(s), {len(warns)} warning(s)")
print("=" * 68)
for f in fails:
    print(f"  FAIL {f}")
for w in warns:
    print(f"  WARN {w}")
sys.exit(1 if fails else 0)
