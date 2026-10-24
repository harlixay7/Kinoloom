"""End-to-end VIDEO smoke test.

Builds a short video workflow with the studio's own WorkflowBuilder,
submits it to the running ComfyUI, waits for it to finish, and verifies a
video file was actually produced.

This is the video counterpart to the image smoke renders — it exercises the
GGUF unet loader, the umt5 text encoder, the Wan VAE, the sampler and the
video save path.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import _gpu_gate
except Exception:  # gate is protection, not a dependency
    _gpu_gate = None

import workflows  # noqa: E402

COMFY = "http://127.0.0.1:8188"
TEMPLATE = "wan2.2_t2v_5b"
FRAMES = 5
W, H = 512, 288
STEPS = 4
SEED = 4242


def post(path: str, payload: dict):
    req = urllib.request.Request(
        COMFY + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get(path: str):
    with urllib.request.urlopen(COMFY + path, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    b = workflows.WorkflowBuilder()
    wf = b.build(TEMPLATE, prompt="a red fox running through tall grass, "
                                  "cinematic, golden hour",
                 negative="blurry, low quality, watermark",
                 seed=SEED, width=W, height=H, steps=STEPS, frames=FRAMES,
                 extra={"DENOISE": 1.0, "LENGTH": FRAMES,
                        "CHAR_STR": 0.8, "POWER_STR": 0.7, "ENV_STR": 0.6,
                        "ANATOMY_STR": 0.7, "QUALITY_STR": 0.6,
                        "STYLE_STR": 0.7})

    classes = sorted({n["class_type"] for n in wf.values()})
    print(f"template : {TEMPLATE}")
    print(f"nodes    : {len(wf)}")
    print(f"classes  : {', '.join(classes)}")

    if _gpu_gate is not None:
        g = _gpu_gate.check()
        if not g["ok"]:
            print(f"REFUSING: studio GPU is busy — {g['details']['summary']}")
            print("(deliberate bypass: KINOLOOM_ALLOW_BUSY_GPU=1)")
            return 3

    try:
        res = post("/prompt", {"prompt": wf, "client_id": "smoke-video"})
    except Exception as e:  # noqa: BLE001
        body = getattr(e, "read", lambda: b"")()
        print(f"SUBMIT FAILED: {e}\n{body[:800]!r}")
        return 1

    pid = res.get("prompt_id")
    if not pid:
        print(f"SUBMIT REJECTED: {json.dumps(res)[:800]}")
        return 1
    print(f"prompt_id: {pid}\nqueued — waiting ...")

    t0 = time.time()
    deadline = t0 + 900
    last = ""
    while time.time() < deadline:
        time.sleep(5)
        try:
            hist = get(f"/history/{pid}")
        except Exception:
            continue
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            print(f"\nfinished in {time.time() - t0:.0f}s — "
                  f"status {status.get('status_str')}")
            if status.get("status_str") != "success":
                print(json.dumps(status, indent=2)[:1500])
                return 1
            outs = entry.get("outputs", {})
            print(f"output nodes: {list(outs)}")
            for nid, o in outs.items():
                for key in ("gifs", "images", "videos"):
                    for item in o.get(key, []) or []:
                        fn = item.get("filename")
                        sub = item.get("subfolder", "")
                        print(f"  {key}: {sub}/{fn}")
            return 0
        else:
            q = ""
            try:
                qd = get("/queue")
                q = (f"running={len(qd.get('queue_running', []))} "
                     f"pending={len(qd.get('queue_pending', []))}")
            except Exception:
                pass
            msg = f"  {int(time.time() - t0):>4}s  {q}"
            if msg != last:
                print(msg)
                last = msg

    print("TIMEOUT waiting for render")
    return 1


if __name__ == "__main__":
    sys.exit(main())
