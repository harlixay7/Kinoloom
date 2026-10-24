"""Warm the studio's default image + video models on launch.

`start.bat` -> `launcher.py` spawns this once ComfyUI (8188) answers, so the
first real render is not a cold start. ComfyUI keeps the loaded weights in
system RAM/VRAM; closing the studio window kills ComfyUI and releases them.

Toggle + model list live in settings.yaml:

    ensemble:
      warm_models: true          # enables this script
      warm_image_models: [zimage]
      warm_video: false          # true = also warm the legacy Wan 2.2 graph

Both are real (tiny) renders: a 2-step 512x512 image, and 5 frames at
512x288 for video. The video warm-up loads only the legacy Wan 2.2 model
(tiers.model) on 8188, so it is off unless settings turn it on; the LTX-2.5
and MiniMax H3 instances are never warmed here. Failures are logged to
logs/warm_studio.log, never fatal - a broken warm-up must not stop the studio
from starting.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))

import yaml  # noqa: E402
from renderer import ComfyClient  # noqa: E402
from workflows import WorkflowBuilder  # noqa: E402
import models  # noqa: E402

LOG = ROOT / "logs" / "warm_studio.log"
WARM_SEED = 4242


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_workflow(c: ComfyClient, wf: dict, label: str) -> bool:
    try:
        pid = c.queue_prompt(wf, client_id="studio-warm")
    except Exception as e:  # noqa: BLE001
        log(f"{label}: submit failed: {e}")
        return False
    t0 = time.time()
    res = c.wait_for(pid, timeout_s=1800)
    if res is None:
        log(f"{label}: timed out")
        return False
    status = res.get("status") or {}
    ok = status.get("status_str") == "success"
    log(f"{label}: {'OK' if ok else 'FAILED'} in {time.time() - t0:.1f}s")
    if not ok:
        for m in status.get("messages") or []:
            if m and m[0] in ("execution_error", "execution_interrupted"):
                log(f"{label}: {str(m[1])[:300]}")
    return ok


def warm_image(c: ComfyClient, mid: str) -> bool:
    m = models.get_model(mid)
    if m is None:
        log(f"image {mid}: unknown model id")
        return False
    if not models.is_available(mid):
        log(f"image {mid}: weights not on disk - skipped")
        return False
    wf = models.build_workflow(mid, "warmup render", "", WARM_SEED,
                               512, 512, 2, m["cfg"], None, False,
                               shot_id=f"warm_{mid}_{int(time.time())}")
    return run_workflow(c, wf, f"image {mid}")


def warm_video(c: ComfyClient, cfg: dict) -> bool:
    lora = cfg.get("lora") or {}
    extra = {
        "SHOT_ID": f"warm_video_{int(time.time())}",
        "MODEL_5B": cfg["tiers"]["model"],
        "CHAR_STR": float(lora.get("char_strength", 0.7)),
        "POWER_STR": float(lora.get("power_strength", 0.7)),
        "ENV_STR": float(lora.get("env_strength", 0.6)),
        "ANATOMY_STR": float(lora.get("anatomy_strength", 0.7)),
        "QUALITY_STR": float(lora.get("quality_strength", 0.6)),
        "STYLE_STR": float(lora.get("style_strength", 0.7)),
    }
    wf = WorkflowBuilder().build(
        "wan2.2_t2v_5b", "a warm-up render, soft light", "", WARM_SEED,
        512, 288, 2, 5, cfg, extra=extra)
    return run_workflow(c, wf, f"video {cfg['tiers']['model']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image-model", action="append", default=None,
                    help="override the image model list (repeatable)")
    ap.add_argument("--no-image", action="store_true")
    ap.add_argument("--no-video", action="store_true")
    a = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "tool" / "settings.yaml")
                         .read_text(encoding="utf-8")) or {}
    ens = cfg.get("ensemble") or {}
    if not ens.get("warm_models", True):
        log("warm-up disabled (settings.yaml: ensemble.warm_models=false)")
        return 0

    image_models = a.image_model if a.image_model else \
        (ens.get("warm_image_models") or ["zimage"])
    if a.no_image:
        image_models = []

    c = ComfyClient(int(ens.get("comfyui_port") or 8188))
    ok = True
    for mid in image_models:
        ok = warm_image(c, str(mid)) and ok
    if not a.no_video and ens.get("warm_video", False):
        ok = warm_video(c, cfg) and ok
    log("warm-up complete" if ok else "warm-up finished with failures")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
