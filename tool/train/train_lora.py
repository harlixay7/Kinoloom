"""Character/power/environment/anatomy/quality LoRA training
via kohya sd-scripts. CLI entry; the web UI uses tool/training.py instead.

Usage:
  python tool/train/train_lora.py --type face --dataset kael --id_name kael
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))
from core import is_safe_component

SD = ROOT / "tools" / "sd-scripts"          # verified: train_network.py at root
VENV_PY = ROOT / "tool" / ".venv" / "Scripts" / "python.exe"
DATASETS = ROOT / "roster" / "datasets"

PRESETS = {
    "face": {"steps": 1800, "rank": 16, "lr": 1e-4},
    "power": {"steps": 4000, "rank": 24, "lr": 5e-5},
    "environment": {"steps": 4500, "rank": 24, "lr": 5e-5},
    "anatomy": {"steps": 7000, "rank": 32, "lr": 5e-5},
    "quality": {"steps": 4500, "rank": 24, "lr": 5e-5},
}
PREFIX = {"face": "char", "power": "power", "environment": "env",
          "anatomy": "anatomy", "quality": "quality"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", required=True,
                    choices=list(PRESETS.keys()))
    ap.add_argument("--dataset", required=True)   # folder in roster/datasets/<type>/
    ap.add_argument("--id_name", default=None)    # registry id (kael/lightning/...)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--rank", type=int, default=None)
    ap.add_argument("--lr", type=float, default=None)
    ap.add_argument("--resolution", default="512,512")
    ap.add_argument("--resume", action="store_true",
                    help="resume from a training-state checkpoint — requires "
                         "a previous run that saved state (--save_state); "
                         "the final .safetensors is NOT a valid resume point")
    args = ap.parse_args()

    if not is_safe_component(args.dataset):
        print(f"ABORT: unsafe dataset name: {args.dataset!r}")
        sys.exit(1)
    if args.id_name and not is_safe_component(args.id_name):
        print(f"ABORT: unsafe id_name: {args.id_name!r}")
        sys.exit(1)

    preset = PRESETS[args.type]
    steps = args.steps or preset["steps"]
    rank = args.rank or preset["rank"]
    lr = args.lr or preset["lr"]
    ident = args.id_name or args.dataset
    out_name = f"{PREFIX[args.type]}_{ident}"

    data_dir = DATASETS / args.type / args.dataset
    if not data_dir.is_dir():
        print(f"ABORT: dataset not found: {data_dir}")
        sys.exit(1)

    base = ROOT / "models" / "train_models" / "wan2.2_ti2v_5B_fp16.safetensors"
    if not base.exists():
        print(f"ABORT: training base missing — download to {base}")
        sys.exit(1)

    cmd = [
        str(VENV_PY), str(SD / "train_network.py"),
        "--pretrained_model_name_or_path", str(base),
        "--network_module", "networks.lora",
        "--network_dim", str(rank),
        "--network_alpha", str(max(1, rank // 2)),
        "--learning_rate", str(lr),
        "--scheduler", "cosine",
        "--optimizer_type", "adafactor",
        "--optimizer_args", "relative_step=False",
        "--optimizer_args", "scale_parameter=False",
        "--optimizer_args", "warmup_init=False",
        "--train_data_dir", str(data_dir),
        "--output_dir", str(ROOT / "models" / "loras"),
        "--output_name", out_name,
        "--max_train_epochs", "1",
        "--max_train_steps", str(steps),
        "--resolution", args.resolution,
        "--bucket_reso_steps", "64",
        "--bucket_no_upscale",
        "--save_every_n_epochs", "5",
        "--save_model_as", "safetensors",
        "--cache_latents",
        "--cache_latents_to_disk",
        "--fp8_base",
        "--gradient_checkpointing",
        "--mixed_precision", "fp16",
        "--network_train_unet_only",
        "--sdpa",
        "--seed", "42",
    ]
    out = ROOT / "models" / "loras" / f"{out_name}.safetensors"
    if args.resume:
        # sd-scripts --resume expects a training-state checkpoint (saved via
        # --save_state), never the final LoRA .safetensors.
        state_dir = ROOT / "models" / "loras"
        states = [p for p in state_dir.glob(f"{out_name}*.safetensors")
                  if re.search(r"state", p.stem)]
        if not states:
            print("ABORT: --resume needs a training-state checkpoint "
                  "(sd-scripts saves one only with --save_state). The final "
                  f"{out_name}.safetensors is not a valid resume point — "
                  "start a fresh run instead.")
            sys.exit(1)
        states.sort(key=lambda p: p.stat().st_mtime)
        cmd += ["--resume", str(states[-1])]
    print(f"training {args.type}:{ident} steps={steps} rank={rank} lr={lr}")
    r = subprocess.run(cmd, cwd=str(SD))
    if r.returncode != 0:
        print("TRAINING FAILED — see output above")
        sys.exit(r.returncode)
    if out.exists():
        roster = ROOT / "roster" / "loras"
        roster.mkdir(parents=True, exist_ok=True)
        shutil.copy(out, roster / out.name)
        print(f"DONE: {out} (+ roster copy) — register via the web UI")
    else:
        print("output missing — check sd-scripts logs")
        sys.exit(1)


if __name__ == "__main__":
    main()
