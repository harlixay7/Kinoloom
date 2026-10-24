"""Validate that every workflow template in tool/workflows/ builds and that
every model file it references actually exists on disk.

This exercises the *video* path (wan2.2_*, ltx_*, post_*) that the image
smoke renders don't cover, without running a 15-minute render.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))

import workflows  # noqa: E402

MODEL_EXT = (".gguf", ".safetensors", ".pth", ".pt", ".ckpt", ".bin")

# Where ComfyUI looks for each folder (mirrors extra_model_paths.yaml, plus
# the custom-node-local ckpt dirs some nodes use).
SEARCH = {
    "diffusion_models": [ROOT / "models" / "diffusion_models"],
    "unet":             [ROOT / "models" / "diffusion_models"],
    "checkpoints":      [ROOT / "models" / "checkpoints"],
    "clip":             [ROOT / "models" / "clip",
                         ROOT / "models" / "text_encoders"],
    "text_encoders":    [ROOT / "models" / "text_encoders",
                         ROOT / "models" / "clip"],
    "vae":              [ROOT / "models" / "vae"],
    "loras":            [ROOT / "models" / "loras",
                         ROOT / "external_loras" / "VideoGeneration"],
    "upscale_models":   [ROOT / "models" / "upscale_models"],
    "clip_vision":      [ROOT / "models" / "clip_vision"],
    "ipadapter":        [ROOT / "models" / "ipadapter"],
    # SeedVR2 upscaler weights live under ComfyUI/models/SEEDVR2
    "seedvr2":          [ROOT / "ComfyUI" / "models" / "SEEDVR2"],
    # RIFE frame interpolation ships its own ckpts inside the custom node
    "rife":             [ROOT / "ComfyUI" / "custom_nodes"
                         / "ComfyUI-Frame-Interpolation" / "ckpts" / "rife"],
}
ALL_DIRS = [d for dirs in SEARCH.values() for d in dirs]

# Tokens the pipeline supplies at call time (not part of SUBST). Supplying
# them here lets every template build in a standalone check.
CALLER_TOKENS = {
    "DENOISE": 0.5, "LENGTH": 5,
    "CHAR_STR": 0.8, "POWER_STR": 0.7, "ENV_STR": 0.6,
    "ANATOMY_STR": 0.7, "QUALITY_STR": 0.6, "STYLE_STR": 0.7,
}


def find(name: str) -> Path | None:
    for d in ALL_DIRS:
        p = d / name
        if p.exists():
            return p
    return None


def collect_refs(wf: dict) -> set[str]:
    out = set()
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        for key, val in (node.get("inputs") or {}).items():
            if isinstance(val, str) and val.lower().endswith(MODEL_EXT):
                out.add(val)
            # list-valued (e.g. LORA_CHAIN) — scan one level
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        for v in item.values():
                            if isinstance(v, str) and \
                                    v.lower().endswith(MODEL_EXT):
                                out.add(v)
    return out


def main() -> int:
    templates = workflows.list_templates()
    print(f"found {len(templates)} templates: {templates}\n")

    total_missing = 0
    for name in templates:
        b = workflows.WorkflowBuilder()
        try:
            wf = b.build(name, prompt="test prompt", negative="test neg",
                         seed=1, width=512, height=512, steps=4, frames=5,
                         extra=dict(CALLER_TOKENS))
        except Exception as e:  # noqa: BLE001
            print(f"[BUILD-FAIL] {name}: {type(e).__name__}: {e}")
            total_missing += 1
            continue
        refs = collect_refs(wf)
        missing = sorted(r for r in refs if find(r) is None)
        if missing:
            total_missing += len(missing)
            print(f"[MISS] {name}  ({len(wf)} nodes, {len(refs)} model refs)")
            for m in missing:
                print(f"         !! {m}")
        else:
            print(f"[OK  ] {name}  ({len(wf)} nodes, {len(refs)} model refs, "
                  f"all present)")

    print("\n" + "=" * 68)
    if total_missing == 0:
        print("RESULT: every template builds and every referenced model exists.")
    else:
        print(f"RESULT: {total_missing} missing reference(s).")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
