"""Reproducible benchmark harness for the generation stack.

Every run is fully specified and fully recorded, so a result can be reproduced
or refuted later. Fixed by construction: prompt, seed, resolution, frame count
and output format. Everything that could explain a difference is captured.

VRAM measurement note
---------------------
`nvidia-smi` is broken on this machine (`Failed to initialize NVML`) even
though CUDA works. So VRAM is sampled from ComfyUI's own `/system_stats`,
which reports `torch_vram_total` (bytes reserved by torch's caching allocator)
and `vram_free`. The peak of `torch_vram_total` over the run is the figure
reported as `vram_peak_mb`; `vram_free` is used for headroom. These are
ComfyUI-reported numbers, not driver numbers — label them as such.

Usage
-----
    PYTHONPATH= tool/.venv/Scripts/python.exe tool/scripts/bench.py image \
        --model zimage --steps 8 --res 1024x1024 --seed 4242 --label zimg-q4-cold
    ... tool/scripts/bench.py show            # summarise results.jsonl
    ... tool/scripts/bench.py show --json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import _gpu_gate
except Exception:  # gate is protection, not a dependency — never block import
    _gpu_gate = None

COMFY = "http://127.0.0.1:8188"
RESULTS = ROOT / "temp" / "bench" / "results.jsonl"
SHOTS = ROOT / "temp" / "bench" / "shots"

# ⚠ When the shell exports HTTP_PROXY/HTTPS_PROXY, urllib honours them by
# default — so requests to 127.0.0.1:8188 get sent to a proxy that can answer
# 502 Bad Gateway, which looks exactly like "ComfyUI is down". Bypass proxies
# entirely for the local API; there is nothing to proxy on loopback.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(path: str, timeout: float = 15) -> dict:
    with _OPENER.open(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, payload: dict, timeout: float = 60) -> dict:
    req = urllib.request.Request(
        COMFY + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def device_vram() -> float | None:
    """Device-wide dedicated VRAM in MB (all processes), or None.

    Delegates to `gpu_mem.py`, which reads the Windows PDH counter
    `\\GPU Adapter Memory(*)\\Dedicated Usage` via ctypes.

    ⚠ Do NOT "simplify" this to `torch.cuda.mem_get_info()`. That call is
    **per-process on this machine** — proven by a controlled test: one process
    held a known 2 GB (its own view 1078 -> 3152 MB) while a second process
    reading the same call stayed flat at 1078 MB. A harness in its own process
    therefore cannot see ComfyUI's VRAM through torch at all; an attempt to
    "fix" it that way reported 1737 MB for a run that truly peaks near 7 GB.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gpu_mem
        return gpu_mem.dedicated_used_mb()
    except Exception:
        return None


def stats() -> dict:
    d = _get("/system_stats")
    dev = (d.get("devices") or [{}])[0]
    sysd = d.get("system", {})
    return {
        "torch_vram_total": dev.get("torch_vram_total", 0),
        "vram_total": dev.get("vram_total", 0),
        "vram_free": dev.get("vram_free", 0),
        "ram_free": sysd.get("ram_free", 0),
        "ram_total": sysd.get("ram_total", 0),
    }


def free_vram() -> None:
    """Ask ComfyUI to unload models and release cached memory.

    This is what makes a 'cold' run cold. Without it, run 2 of the same
    workflow measures a different thing than run 1.

    NOTE: ComfyUI's /free returns an EMPTY body on success, so this must not
    try to parse JSON from the response — doing so raises
    "Expecting value: line 1 column 1" and silently skips the unload, which
    is exactly the bug that made the first harness run non-cold.
    """
    try:
        req = urllib.request.Request(
            COMFY + "/free",
            data=json.dumps({"unload_models": True,
                             "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()          # drain; body is empty on success
        time.sleep(3.0)
    except Exception as e:
        print(f"  ! /free failed: {e}")


def hardware() -> dict:
    try:
        d = _get("/system_stats")
        dev = (d.get("devices") or [{}])[0]
        s = d.get("system", {})
        return {
            "gpu": dev.get("name"),
            "vram_total_mb": round(dev.get("vram_total", 0) / 2**20),
            "ram_total_gb": round(s.get("ram_total", 0) / 2**30, 1),
            "comfyui_version": s.get("comfyui_version"),
        }
    except Exception:
        return {}


def override_weights(wf: dict, unet: str | None, clip: str | None,
                     ckpt: str | None) -> list[str]:
    """Patch the loader filenames inside an already-built graph.

    This is how quant variants are A/B'd without adding a registry entry per
    quant — the registry describes *models*, the benchmark varies one
    dimension of one model. Returns a list of human-readable changes so the
    result row records exactly what was swapped.
    """
    changes = []
    want = {"UnetLoaderGGUF": ("unet_name", unet),
            "CLIPLoaderGGUF": ("clip_name", clip),
            "CheckpointLoaderSimple": ("ckpt_name", ckpt)}
    for nid, node in wf.items():
        ct = node.get("class_type")
        if ct in want:
            key, val = want[ct]
            if val:
                old = node["inputs"].get(key)
                node["inputs"][key] = val
                changes.append(f"{ct}.{key}: {old} -> {val}")
    return changes


def run_workflow(wf: dict, timeout_s: int = 3600, poll_s: float = 1.0,
                 gate: bool = True) -> dict:
    """Submit a built API workflow and wait for it, sampling VRAM throughout.

    Refuses to queue while the studio is rendering: the studio's ComfyUI is
    user-owned, and a benchmark that lands in the user's queue (or ahead of
    it) does not measure what it claims to measure. Bypass deliberately with
    gate=False, --allow-busy, or KINOLOOM_ALLOW_BUSY_GPU=1.
    """
    if gate and _gpu_gate is not None:
        g = _gpu_gate.check()
        if not g["ok"]:
            return {"ok": False, "seconds": 0.0, "outputs": [],
                    "error": f"gpu gate: {g['details']['summary']}",
                    "gpu_gate": g}
    t0 = time.time()
    base = stats()
    peak_ram_used = base["ram_total"] - base["ram_free"]

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import gpu_mem
        tracker = gpu_mem.peak_tracker(interval=0.4)
    except Exception:
        tracker = None
    if tracker is not None:
        tracker.__enter__()
    base_used = tracker.baseline if tracker else None
    peak_used = base_used

    try:
        res = _post("/prompt", {"prompt": wf})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        return {"ok": False, "error": f"HTTP {e.code}: {body}",
                "seconds": time.time() - t0}

    pid = res.get("prompt_id")
    if not pid:
        return {"ok": False, "error": f"no prompt_id: {res}", "seconds": 0}

    while time.time() - t0 < timeout_s:
        try:
            s = stats()
            peak_ram_used = max(peak_ram_used,
                                s["ram_total"] - s["ram_free"])
        except Exception:
            pass
        try:
            h = _get(f"/history/{pid}")
        except Exception:
            time.sleep(poll_s)
            continue
        if pid in h:
            entry = h[pid]
            status = (entry.get("status") or {})
            secs = time.time() - t0
            ok = status.get("status_str") == "success" and not status.get("error")
            outs = []
            for node_out in (entry.get("outputs") or {}).values():
                for img in node_out.get("images", []) or []:
                    outs.append({
                        "filename": img.get("filename"),
                        "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"),
                    })
            err = ""
            if not ok:
                for m in status.get("messages", []) or []:
                    if m and m[0] in ("execution_error", "execution_interrupted"):
                        err = json.dumps(m[1])[:800]
            if tracker is not None:
                tracker.__exit__()
                peak_used, base_used = tracker.peak, tracker.baseline
            return {
                "ok": ok, "seconds": secs, "outputs": outs, "error": err,
                "vram_peak_mb": round(peak_used) if peak_used else None,
                "vram_baseline_mb": round(base_used) if base_used else None,
                "vram_delta_mb": round(tracker.delta) if (
                    tracker is not None and tracker.delta is not None) else None,
                "vram_probe": "windows-pdh device-wide",
                "ram_peak_gb": round(peak_ram_used / 2**30, 2),
                "status": status.get("status_str"),
            }
        time.sleep(poll_s)

    if tracker is not None:
        tracker.__exit__()
        peak_used, base_used = tracker.peak, tracker.baseline
    return {"ok": False, "error": f"timeout after {timeout_s}s",
            "seconds": time.time() - t0,
            "vram_peak_mb": round(peak_used) if peak_used else None,
            "vram_baseline_mb": round(base_used) if base_used else None}


def record(row: dict) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# ------------------------------------------------------------------ commands
def cmd_image(a) -> int:
    import models

    mid = a.model
    m = models.get_model(mid)
    if not m:
        print(f"!! unknown model {mid}; known: {models.model_ids()}")
        return 2
    if not models.is_available(mid):
        print(f"!! {mid} is registered but its unet file is not on disk — "
              f"refusing to benchmark a model that cannot load.")
        return 2

    w, h = (int(x) for x in a.res.lower().split("x"))
    steps = a.steps if a.steps is not None else m["steps"]
    cfg = a.cfg if a.cfg is not None else m["cfg"]
    prompt, negative = models.adapt_prompt(mid, a.prompt, "images")

    wf = models.build_workflow(mid, prompt, negative, a.seed, w, h,
                               steps, cfg, None, False, a.label or "bench")

    overrides = override_weights(wf, a.unet, a.clip, a.ckpt)
    for c in overrides:
        print(f"  override: {c}")

    if a.free:
        print("  freeing VRAM for a cold run...")
        free_vram()

    print(f"  {mid}  {w}x{h}  steps={steps} cfg={cfg} seed={a.seed}")
    r = run_workflow(wf, timeout_s=a.timeout, gate=not a.allow_busy)

    # ComfyUI caches node outputs across prompt submissions: re-submitting an
    # identical graph returns the previous result in ~1s without running the
    # sampler. A "generation" that finishes in a couple of seconds at >=4
    # steps did not generate anything — flag it so it can never be quoted as
    # a speed-up. (Measured on this machine: an identical repeat of an 8-step
    # Z-Image run came back in 1.1s versus 47.2s for the real thing.)
    r["suspected_cache_hit"] = bool(r.get("ok") and r.get("seconds", 999) < 3.0
                                    and steps >= 4)

    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "image",
        "label": a.label,
        "model": mid,
        "model_name": m["name"],
        "family": m["family"],
        "template": m["template"],
        "quant": Path(m["files"].get("unet")
                      or m["files"].get("ckpt", "?")).stem,
        "overrides": overrides,        "files": m["files"],
        "prompt": prompt,
        "negative": negative,
        "seed": a.seed, "width": w, "height": h,
        "steps": steps, "cfg": cfg,
        "sampler": m.get("sampler"), "scheduler": m.get("scheduler"),
        "loras": [], "attention": a.attention, "cache": a.cache,
        "memory_mode": a.memory_mode, "cold": bool(a.free),
        "hardware": hardware(),
        **r,
    }
    record(row)

    print(f"  -> {'OK' if r.get('ok') else 'FAIL'}  "
          f"{r.get('seconds', 0):.1f}s  "
          f"vram_peak={r.get('vram_peak_mb', '?')}MB  "
          f"ram_peak={r.get('ram_peak_gb', '?')}GB")
    if r.get("suspected_cache_hit"):
        print("     ⚠ SUSPECTED CACHE HIT — ComfyUI reuses node outputs when an "
              "identical graph is re-submitted. This timing is NOT a real "
              "generation. Vary the seed to force recomputation.")
    if not r.get("ok"):
        print(f"     error: {r.get('error', '')[:400]}")
    for o in r.get("outputs", [])[:4]:
        print(f"     out: {o['subfolder']}/{o['filename']}")
    return 0 if r.get("ok") else 1


def strip_node_type(wf: dict, ctype: str) -> list[str]:
    """Remove every node of `ctype` and rewire its consumers to its source.

    Used to A/B an optimisation by *removing* it rather than by editing a
    template: the production video graph ships with `WanVideoTeaCacheKJ`
    already wired in, so "no cache" has to be produced by taking that node out
    and reconnecting KSampler.model to whatever fed the cache node.
    """
    removed = []
    for nid, node in list(wf.items()):
        if node.get("class_type") != ctype:
            continue
        src = node["inputs"].get("model") or node["inputs"].get("samples")
        if not isinstance(src, list):
            continue
        for other in wf.values():
            for k, v in other["inputs"].items():
                if isinstance(v, list) and len(v) == 2 and v[0] == nid:
                    other["inputs"][k] = src
        del wf[nid]
        removed.append(f"{ctype}({nid})")
    return removed


def cmd_video(a) -> int:
    """Benchmark the production video path.

    Mirrors `renderer.Renderer.build_workflow()` exactly — same template, same
    WorkflowBuilder call, same token set — so the numbers describe the real
    pipeline and not a bespoke graph.
    """
    sys.path.insert(0, str(ROOT / "tool"))
    from workflows import WorkflowBuilder

    cfg_path = ROOT / "tool" / "settings.yaml"
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    w, h = (int(x) for x in a.res.lower().split("x"))
    quant = a.quant if a.quant.endswith(".gguf") else \
        f"Wan2.2-TI2V-5B-{a.quant}.gguf"

    lora = cfg.get("lora", {}) or {}
    extra = {
        "SHOT_ID": a.label or "bench",
        "MODEL_5B": quant,
        # The production video templates carry a six-slot LoRA chain and need
        # the strength tokens even when every slot is "none" — the builder
        # substitutes before parsing, so an unset token is a hard error.
        # Values come from settings.yaml so the graph matches production.
        "CHAR_STR": float(lora.get("char_strength", 0.8)),
        "POWER_STR": float(lora.get("power_strength", 0.7)),
        "ENV_STR": float(lora.get("env_strength", 0.6)),
        "ANATOMY_STR": float(lora.get("anatomy_strength", 0.7)),
        "QUALITY_STR": float(lora.get("quality_strength", 0.6)),
        "STYLE_STR": float(lora.get("style_strength", 0.7)),
        "TEA_THRESH": a.teacache_thresh,
        "TEA_END": a.teacache_end,
    }
    wf = WorkflowBuilder().build(a.template, a.prompt, a.negative, a.seed,
                                 w, h, a.steps, a.frames, cfg, extra=extra)

    if a.no_cache:
        removed = strip_node_type(wf, "WanVideoTeaCacheKJ")
        print(f"  cache: stripped {removed or 'nothing'} (no-cache run)")
    else:
        print(f"  cache: WanVideoTeaCacheKJ thresh={a.teacache_thresh} "
              f"end={a.teacache_end}")

    if a.free:
        print("  freeing VRAM for a cold run...")
        free_vram()

    print(f"  {a.template}  {w}x{h}  frames={a.frames} steps={a.steps} "
          f"seed={a.seed}  unet={quant}")

    r = run_workflow(wf, timeout_s=a.timeout, gate=not a.allow_busy)
    secs = r.get("seconds", 0) or 0
    r["suspected_cache_hit"] = bool(r.get("ok") and secs < 3.0 and a.steps >= 4)
    r["seconds_per_frame"] = round(secs / a.frames, 3) if a.frames else None

    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "video",
        "label": a.label,
        "template": a.template,
        "unet": quant,
        "quant": a.quant,
        "prompt": a.prompt,
        "negative": a.negative,
        "seed": a.seed, "width": w, "height": h,
        "frames": a.frames, "steps": a.steps, "cfg": 1.0,
        "sampler": "euler", "scheduler": "simple",
        "attention": a.attention,
        "cache": "none" if a.no_cache else f"teacache:{a.teacache_thresh}",
        "memory_mode": a.memory_mode, "cold": bool(a.free),
        "hardware": hardware(),
        **r,
    }
    record(row)

    print(f"  -> {'OK' if r.get('ok') else 'FAIL'}  {secs:.1f}s  "
          f"{r.get('seconds_per_frame')}s/frame  "
          f"vram_peak={r.get('vram_peak_mb', '?')}MB")
    if r.get("suspected_cache_hit"):
        print("     ⚠ SUSPECTED CACHE HIT — not a real generation.")
    if not r.get("ok"):
        print(f"     error: {r.get('error', '')[:400]}")
    for o in r.get("outputs", [])[:4]:
        print(f"     out: {o['subfolder']}/{o['filename']}")
    return 0 if r.get("ok") else 1


def cmd_show(a) -> int:
    if not RESULTS.exists():
        print("no results yet")
        return 0
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    print(f"{'label':<26}{'model':<16}{'res':<12}{'stp':>4}{'sec':>8}"
          f"{'vramMB':>8}{'ramGB':>7}  ok")
    print("-" * 92)
    for r in rows:
        res = f"{r.get('width')}x{r.get('height')}"
        print(f"{(r.get('label') or '-'):<26}{r.get('model',''):<16}{res:<12}"
              f"{r.get('steps',0):>4}{r.get('seconds',0):>8.1f}"
              f"{r.get('vram_peak_mb',0):>8}{r.get('ram_peak_gb',0):>7}"
              f"  {'OK' if r.get('ok') else 'FAIL'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("image")
    i.add_argument("--model", required=True)
    i.add_argument("--steps", type=int, default=None)
    i.add_argument("--cfg", type=float, default=None)
    i.add_argument("--res", default="1024x1024")
    i.add_argument("--seed", type=int, default=4242)
    i.add_argument("--prompt", default=(
        "cinematic close-up portrait of a woman, natural window light, "
        "shallow depth of field, detailed skin texture, 35mm photograph"))
    i.add_argument("--label", default=None)
    i.add_argument("--free", action="store_true",
                   help="unload models first for a cold measurement")
    i.add_argument("--timeout", type=int, default=1800)
    i.add_argument("--attention", default="pytorch-sdpa")
    i.add_argument("--cache", default="none")
    i.add_argument("--memory_mode", default="default")
    i.add_argument("--unet", default=None,
                   help="override the unet filename in the built graph")
    i.add_argument("--clip", default=None,
                   help="override the text-encoder filename")
    i.add_argument("--ckpt", default=None,
                   help="override the checkpoint filename")
    i.add_argument("--allow-busy", action="store_true",
                   help="bypass the pre-flight GPU gate (studio may be "
                        "rendering — only when you know why)")
    i.set_defaults(fn=cmd_image)

    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    v = sub.add_parser("video")
    v.add_argument("--template", default="wan2.2_t2v_5b",
                   help="production template to mirror")
    v.add_argument("--quant", default="Q4_K_M",
                   help="Q4_K_M | Q5_K_M | Q6_K | a full .gguf filename")
    v.add_argument("--res", default="832x480")
    v.add_argument("--frames", type=int, default=25)
    v.add_argument("--steps", type=int, default=8)
    v.add_argument("--seed", type=int, default=4242)
    v.add_argument("--prompt", default=(
        "cinematic medium shot, a woman walking through a rain-slick city "
        "street at dusk, neon reflections, slow camera push in, 35mm film"))
    v.add_argument("--negative", default="")
    v.add_argument("--label", default=None)
    v.add_argument("--no-cache", action="store_true",
                   help="strip WanVideoTeaCacheKJ to measure the uncached path")
    v.add_argument("--teacache-thresh", type=float, default=0.1)
    v.add_argument("--teacache-end", type=float, default=0.6)
    v.add_argument("--free", action="store_true")
    v.add_argument("--timeout", type=int, default=3600)
    v.add_argument("--attention", default="pytorch-sdpa")
    v.add_argument("--memory_mode", default="default")
    v.add_argument("--allow-busy", action="store_true",
                   help="bypass the pre-flight GPU gate (studio may be "
                        "rendering — only when you know why)")
    v.set_defaults(fn=cmd_video)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
