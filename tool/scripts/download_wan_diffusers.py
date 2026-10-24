"""Fetch the diffusers-format Wan 2.2 TI2V-5B pipeline that ai-toolkit needs
for LoRA training.

tool/settings.yaml expects it at models/train_models/wan22_5b_diffusers.
Only the pipeline components are fetched — the repo's assets/, examples/ and
README are skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

from huggingface_hub import snapshot_download

ROOT = Path(__file__).resolve().parents[2]
REPO = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
DEST = ROOT / "models" / "train_models" / "wan22_5b_diffusers"

# Exactly the pieces ai-toolkit's sd_trainer loads.
ALLOW = [
    "model_index.json",
    "scheduler/*",
    "text_encoder/*",
    "tokenizer/*",
    "transformer/*",
    "vae/*",
]


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"repo : {REPO}")
    print(f"dest : {DEST}")
    print("downloading pipeline components (~34 GB) ...\n", flush=True)

    path = snapshot_download(
        repo_id=REPO,
        local_dir=str(DEST),
        allow_patterns=ALLOW,
        max_workers=4,
    )
    print(f"\ndone -> {path}")

    total = 0
    count = 0
    for f in sorted(Path(path).rglob("*")):
        if f.is_file():
            total += f.stat().st_size
            count += 1
    print(f"files: {count}   total: {total / 1e9:.2f} GB")

    required = [
        "model_index.json",
        "transformer/config.json",
        "transformer/diffusion_pytorch_model.safetensors.index.json",
        "vae/config.json",
        "vae/diffusion_pytorch_model.safetensors",
        "text_encoder/config.json",
        "text_encoder/model.safetensors.index.json",
        "scheduler/scheduler_config.json",
        "tokenizer/tokenizer_config.json",
    ]
    missing = [r for r in required if not (Path(path) / r).exists()]
    if missing:
        print("\nMISSING REQUIRED COMPONENTS:")
        for m in missing:
            print(f"  !! {m}")
        return 1
    print("\nall required pipeline components present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
