"""Workflow builder — loads ComfyUI API-format JSON templates and
substitutes shot values. Templates live in tool/workflows/*.json.
Placeholders are {TOKEN} tokens substituted in the RAW TEXT before JSON
parsing (each value json-encoded, so numbers stay numbers)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core import ROOT, log_setup

log = log_setup("workflows")
WF_DIR = ROOT / "tool" / "workflows"

_TOKEN = re.compile(r"\{([A-Z0-9_]+)\}")


class WorkflowError(Exception):
    pass


def list_templates() -> list[str]:
    if not WF_DIR.exists():
        return []
    return sorted(p.stem for p in WF_DIR.glob("*.json"))


def load_template(name: str) -> str:
    p = WF_DIR / f"{name}.json"
    if not p.exists():
        raise WorkflowError(f"workflow template '{name}' not found in {WF_DIR}")
    return p.read_text(encoding="utf-8")


SUBST = {
    # ---- Wan 2.2 (legacy): projects/shots that pick it, shots without a
    # project, face-fix, V2V and the Images tab's `wan` entry. Every
    # wan2.2_*.json template reads these tokens (and the H3 templates reuse
    # MODEL_5B for their DiT) — removing them breaks those renders.
    "MODEL_HIGH": "Wan2.2-T2V-A14B-HighNoise-Q3_K_M.gguf",
    "MODEL_LOW": "Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf",
    "MODEL_5B": "Wan2.2-TI2V-5B-Q4_K_M.gguf",
    "VAE_WAN": "Wan2.1_VAE.safetensors",
    "VAE_WAN22": "wan2.2_vae.safetensors",   # 5B I2V needs the 16x VAE
    "LORA_TURBO": "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors",
    "LORA_FASTWAN": "Wan2_2_5B_FastWanFullAttn_lora_rank_128_bf16.safetensors",
    "T2I_LORA": "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors",
    "LORA_4STEP_HIGH": "wan22_t2v_a14b_4step_high.safetensors",
    "LORA_4STEP_LOW": "wan22_t2v_a14b_4step_low.safetensors",
    "LORA_I2V_HIGH": "wan2.2_i2v_A14b_high_noise_lora_rank64_lightx2v_4step_1022.safetensors",
    "LORA_I2V_LOW": "wan2.2_i2v_A14b_low_noise_lora_rank64_lightx2v_4step_1022.safetensors",
    "MODEL_LTX": "ltx-video-2b-v0.9-Q4_K_M.gguf",
    "MODEL_SDXL": "RealVisXL_V5.0-Q5_K_M.gguf",
    # Native SDXL *checkpoint* (UNet + CLIP + VAE in one file) — a different
    # loader from MODEL_SDXL, which is a bare GGUF unet. The registry tracks
    # this with its `loader` field; do not route a checkpoint through
    # UnetLoaderGGUF or a bare unet through CheckpointLoaderSimple.
    "MODEL_CKPT": "cyberrealisticXL_v100_fp16.safetensors",
    "CLIP_L": "clip_l.safetensors",
    "CLIP_G": "clip_g.safetensors",
    "VAE_SDXL": "xlVAEC_c91.safetensors",
    "T5": "umt5-xxl-encoder-Q4_K_M.gguf",
    "T5_LTX": "t5-v1_1-xxl-encoder-Q4_K_M.gguf",
    "VAE_LTX": "LTX-Video-VAE-BF16.safetensors",
    "T5_LOADER": "CLIPLoaderGGUF",
    "T5_FILE": "umt5-xxl-encoder-Q4_K_M.gguf",
    "LORA_CHAIN": [],               # dynamic LoRA chain — [{name, strength}]
    "START_IMAGE": "keyframe.png",
    "SHOT_ID": "shot",
    "TEA_THRESH": 0.1,
    "TEA_DEVICE": "main_device",
    "TEA_END": 0.6,
    "CHAR_LORA": "none",           # per-character face LoRA — "none" = skip
    "POWER_LORA": "none",          # per-power VFX LoRA
    "ENV_LORA": "none",            # per-location environment LoRA
    "ANATOMY_LORA": "none",        # global human anatomy/motion LoRA
    "QUALITY_LORA": "none",        # global filmic quality LoRA
    "STYLE_LORA": "none",          # per-movie style LoRA
    "SEEDVR2_DIT": "seedvr2_ema_3b-Q8_0.gguf",
    "ESRGAN_MODEL": "RealESRGAN_x4plus.pth",
    "SEEDVR2_VAE": "ema_vae_fp16.safetensors",
    # ---- Z-Image Turbo (Tongyi / Alibaba, 6B S3-DiT). Arch = lumina2 in
    # ComfyUI, text encoder = Qwen3-4B. On sm_86 (RTX 3070) the bf16 unet is
    # 11.5 GB and will not fit 8 GB VRAM, and the Civitai nvfp4/mxfp8 builds
    # are Blackwell-only, so the GGUF route is the only viable one.
    # Defaults mirror ComfyUI's own image_z_image_turbo template exactly:
    # steps 8, cfg 1.0, res_multistep/simple, ModelSamplingAuraFlow shift 3.0.
    "Z_UNET": "z_image_turbo-Q4_K_M.gguf",
    "Z_CLIP": "qwen_3_4b.safetensors",
    "Z_VAE": "z_image_ae.safetensors",
    "Z_SAMPLER": "res_multistep",
    "Z_SCHEDULER": "simple",
    "Z_SHIFT": 3.0,
    # ---- Qwen-Image-2.1 (7.1B single-stream DiT + Qwen3-VL 8B ConvRot INT8
    # text encoder + 64-ch RGBA VAE). Runs on the dedicated ComfyUI_Qwen21
    # instance (v0.37.4, port 8190) — the production v0.30 farm cannot load
    # the 2.1 VAE/TE. Proven settings + evidence: docs/research/PROVEN_CONFIGS_QWEN21.md.
    # QW21_LORA "none" = quality mode (40 steps, CFG 3.5); set to the Pruna
    # DMD 8-step adapter for fast mode (8 steps, CFG 1.0) — the builder
    # strips "none" LoRA nodes automatically.
    "QW21_UNET": "qwen_image_2.1_Q5_K_M.gguf",
    "QW21_CLIP": "qwen3vl_8b_int8_convrot.safetensors",
    "QW21_VAE": "qwen_image_2.1_vae_bf16_compact.safetensors",
    "QW21_UPSCALE": "4x-UltraSharp.pth",
    "QW21_SAMPLER": "euler",
    "QW21_SCHEDULER": "simple",
    "QW21_LORA": "none",
    "FRAMES_DIR": str(ROOT / "temp" / "frames"),
    "OUT_PREFIX": "kinoloom/post",
    "FF_STEPS": 4,
    "CFG": 5.0,                    # standard Wan 2.2 I2V guidance (no turbo)
    "SAMPLER": "dpmpp_2m",
    "SCHEDULER": "karras",
    "XL_LORA1": "none",          # SDXL image-studio LoRA slot 1 — "none" = skipped
    "XL_LORA1_STR": 0.7,
    "XL_LORA2": "none",          # SDXL image-studio LoRA slot 2
    "XL_LORA2_STR": 0.7,
    "XL_LORA3": "none",          # SDXL image-studio LoRA slot 3
    "XL_LORA3_STR": 0.7,
    "XL_LORA4": "none",          # SDXL image-studio LoRA slot 4
    "XL_LORA4_STR": 0.7,
    "XL_LORA5": "none",          # SDXL image-studio LoRA slot 5
    "XL_LORA5_STR": 0.7,
    "XL_LORA6": "none",          # SDXL image-studio LoRA slot 6
    "XL_LORA6_STR": 0.7,
    "UPSCALE_MODEL": "RealESRGAN_x4.pth",
    "HIRES_IMAGE": "keyframe.png",
    "HIRES_W": 1408,
    "HIRES_H": 1408,
    "HIRES_STEPS": 8,
    "HIRES_DENOISE": 0.45,
}


class WorkflowBuilder:
    # Template text is cached per process. The cache is keyed by file mtime so
    # editing a template (or adding a new one) takes effect without restarting
    # the control room — the previous unbounded cache meant a fixed template
    # kept rendering the old graph until the app was restarted, which is
    # impossible to notice from the UI.
    _cache: dict[str, tuple[float, str]] = {}

    def _raw(self, template: str) -> str:
        p = WF_DIR / f"{template}.json"
        mtime = p.stat().st_mtime if p.exists() else 0.0
        hit = self._cache.get(template)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        text = load_template(template)
        self._cache[template] = (mtime, text)
        return text

    def build(self, template: str, prompt: str, negative: str, seed: int,
              width: int, height: int, steps: int, frames: int,
              cfg: dict | None = None, extra: dict | None = None) -> dict:
        values: dict[str, Any] = dict(SUBST)
        values.update({
            "PROMPT": prompt, "NEGATIVE": negative, "SEED": seed,
            "WIDTH": width, "HEIGHT": height, "STEPS": steps, "FRAMES": frames,
            "FPS": max(1, frames // 5), "FPS_F": float(frames) / 5.0,
            "STEPS_HALF": max(1, steps // 2),
            # LTX-2.5 two-stage runs stage 1 at HALF the target resolution and
            # lets LTXVLatentUpsampler double it; stage 2 uses a second noise
            # seed. Both are derived here so orchestrator and video_studio get
            # them without duplication. Wan templates do not reference them.
            "STAGE1_W": max(1, width // 2), "STAGE1_H": max(1, height // 2),
            "SEED2": seed + 1,
        })
        if extra:
            values.update(extra)

        text = self._raw(template)
        missing: set[str] = set()

        def esc(v: Any) -> str:
            if isinstance(v, str):
                return json.dumps(v)[1:-1]  # JSON-escaped, no outer quotes
            return json.dumps(v)  # lists/numbers (e.g. LORA_CHAIN) keep shape

        # 1) tokens embedded inside existing JSON strings ("kinoloom/{SHOT_ID}")
        def repl_in_str(m: re.Match) -> str:
            k = m.group(2)
            return (m.group(1) + esc(values[k]) + m.group(3)) if k in values \
                else (missing.add(k) or m.group(0))

        text = re.sub(r'("[^"]*)\{([A-Z0-9_]+)\}([^"]*")', repl_in_str, text)

        # 2) bare tokens (numbers etc.)
        def repl_bare(m: re.Match) -> str:
            k = m.group(1)
            return json.dumps(values[k]) if k in values \
                else (missing.add(k) or m.group(0))

        text = _TOKEN.sub(repl_bare, text)
        if missing:
            raise WorkflowError(f"template '{template}' has unset tokens: "
                                f"{sorted(missing)}")
        try:
            wf = json.loads(text)
        except json.JSONDecodeError as e:
            raise WorkflowError(f"template '{template}' invalid JSON: {e}") from e

        # optional LoRA chains: any LoraLoader/LoraLoaderModelOnly whose
        # lora_name is "none" is removed and its consumers rewired to its
        # source model (and clip for full LoraLoader)
        for nid, node in list(wf.items()):
            if node["class_type"] in ("LoraLoaderModelOnly", "LoraLoader") \
                    and node["inputs"].get("lora_name") == "none":
                srcs = {0: node["inputs"]["model"]}
                if node["class_type"] == "LoraLoader":
                    srcs[1] = node["inputs"]["clip"]
                for other in wf.values():
                    for k, v in other["inputs"].items():
                        if isinstance(v, list) and len(v) == 2 \
                                and v[0] == nid:
                            other["inputs"][k] = srcs.get(v[1], srcs[0])
                del wf[nid]

        # dynamic LoRA chains: LoraChain placeholder nodes
        # expand into N chained LoraLoaderModelOnly nodes
        for nid, node in list(wf.items()):
            if node.get("class_type") != "LoraChain":
                continue
            inputs = node.get("inputs") or {}
            src = inputs.get("model")
            loras = inputs.get("loras")
            if not isinstance(loras, list):
                loras = []
            prev = src
            last = None
            for i, lora in enumerate(loras):
                if not isinstance(lora, dict) or not lora.get("name"):
                    continue
                lid = f"{nid}_lora_{i}"
                wf[lid] = {
                    "class_type": "LoraLoaderModelOnly",
                    "inputs": {
                        "model": prev,
                        "lora_name": lora["name"],
                        "strength_model": float(lora.get("strength", 1.0)),
                    },
                }
                prev = [lid, 0]
                last = lid
            target = [last, 0] if last else src
            for other in wf.values():
                for k, v in other["inputs"].items():
                    if isinstance(v, list) and len(v) == 2 \
                            and v[0] == nid:
                        other["inputs"][k] = target
            del wf[nid]
        return wf
