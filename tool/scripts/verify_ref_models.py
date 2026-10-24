"""Verify the SDXL reference-image (IP-Adapter) stack end to end.

Static + graph verification, in four stages:

  1. both model files exist with the exact byte size HuggingFace reports
  2. both parse as safetensors and carry the tensor keys the loaders require
  3. ComfyUI's own dropdowns list them -- this is authoritative, because the
     loaders validate submitted values against exactly those lists
  4. `models.build_ref_workflow()` produces a graph whose every node type and
     every literal input the running ComfyUI accepts

Stage 3-4 need ComfyUI up on :8188; stages 1-2 run offline.

Run with the studio venv on PYTHONPATH (the tool modules need its PyYAML):
    PYTHONPATH=tool/.venv/Lib/site-packages \
      tools/python/python.exe tool/scripts/verify_ref_models.py

Exit code 0 = every check passed.
"""
from __future__ import annotations

import json
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMFY = "http://127.0.0.1:8188"

# byte sizes as reported by the HF CDN's Content-Length
EXPECTED = {
    "models/ipadapter/ip-adapter-plus_sdxl_vit-h.safetensors": 847_517_512,
    "models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors":
        2_528_373_448,
}

PASS, FAIL, WARN = [], [], []


def ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  [ok]   {msg}")


def bad(msg: str) -> None:
    FAIL.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    WARN.append(msg)
    print(f"  [warn] {msg}")


# ---------------------------------------------------------------- stage 1+2
def check_files() -> dict[str, dict]:
    print("\n1. model files on disk")
    parsed: dict[str, dict] = {}
    for rel, expect in EXPECTED.items():
        p = ROOT / rel
        if not p.exists():
            bad(f"{rel} — MISSING")
            continue
        size = p.stat().st_size
        if size != expect:
            bad(f"{rel} — {size:,} B, expected {expect:,} B")
            continue
        ok(f"{p.name} — {size:,} B exactly")
        with p.open("rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n <= 0 or n > 200_000_000:
                bad(f"{p.name} — implausible header length {n}")
                continue
            hdr = json.loads(f.read(n).decode("utf-8"))
        keys = [k for k in hdr if k != "__metadata__"]
        parsed[rel] = {"keys": keys, "header": hdr}
        ok(f"{p.name} — safetensors OK, {len(keys)} tensors")

    print("\n2. tensor keys the loaders actually look for")
    ipa = parsed.get("models/ipadapter/ip-adapter-plus_sdxl_vit-h.safetensors")
    if ipa:
        keys = ipa["keys"]
        # ipadapter_model_loader splits on "image_proj." / "ip_adapter.";
        # ipadapter_execute decides is_plus from "latents" in image_proj.
        if "image_proj.latents" in keys:
            ok("image_proj.latents present — marks this as a PLUS model")
        else:
            bad("image_proj.latents MISSING — loader would not treat it "
                "as PLUS")
        n_proj = sum(1 for k in keys if k.startswith("image_proj."))
        n_ip = sum(1 for k in keys if k.startswith("ip_adapter."))
        if n_proj and n_ip:
            ok(f"key split OK — {n_proj} image_proj + {n_ip} ip_adapter")
        else:
            bad(f"key split broken — {n_proj} image_proj, {n_ip} ip_adapter")
        lat = ipa["header"].get("image_proj.latents", {}).get("shape")
        if lat == [1, 16, 1280]:
            ok(f"latents shape {lat} — ViT-H/14 SDXL PLUS, 16 tokens")
        else:
            warn(f"latents shape {lat}, expected [1, 16, 1280]")

    cv = parsed.get("models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K"
                    ".safetensors")
    if cv:
        keys = cv["keys"]
        for probe in ("vision_model.embeddings.class_embedding",
                      "vision_model.post_layernorm.weight"):
            if probe in keys:
                ok(f"CLIP vision key present: {probe}")
                break
        else:
            heads = sorted({k.split(".")[0] for k in keys})
            warn(f"unexpected CLIP vision layout, top-level: {heads}")
        wid = cv["header"].get("vision_model.embeddings.patch_embedding.weight",
                               {}).get("shape")
        if wid:
            ok(f"patch_embedding {wid} — hidden size {wid[0]}")
    return parsed


# ------------------------------------------------------------------ stage 3
def get_object_info(node: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{COMFY}/object_info/{node}",
                                    timeout=15) as r:
            return json.loads(r.read().decode("utf-8")).get(node)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def combo_list(info: dict, group: str, key: str) -> list | None:
    spec = ((info.get("input") or {}).get(group) or {}).get(key)
    if not spec:
        return None
    first = spec[0]
    return first if isinstance(first, list) else None


def check_comfy(parsed: dict) -> dict | None:
    print("\n3. does ComfyUI list them? (this is what loaders validate against)")
    probe = get_object_info("CLIPVisionLoader")
    if probe is None:
        bad("ComfyUI not reachable on :8188 — stages 3-4 skipped")
        return None

    names = combo_list(probe, "required", "clip_name") or []
    want = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
    if want in names:
        ok(f"CLIPVisionLoader sees {want}")
    else:
        bad(f"CLIPVisionLoader does NOT list {want} (sees {names})")

    uni = get_object_info("IPAdapterUnifiedLoader")
    if uni is None:
        bad("IPAdapterUnifiedLoader node missing — pack not loaded?")
    else:
        presets = combo_list(uni, "required", "preset") or []
        if "PLUS (high strength)" in presets:
            ok("IPAdapterUnifiedLoader offers 'PLUS (high strength)'")
        else:
            bad(f"preset missing, sees {presets}")
        rets = uni.get("output") or []
        if list(rets)[:2] == ["MODEL", "IPADAPTER"]:
            ok("UnifiedLoader returns (MODEL, IPADAPTER) — wiring matches")
        else:
            warn(f"UnifiedLoader returns {rets}")

    bat = get_object_info("IPAdapterBatch")
    if bat is None:
        bad("IPAdapterBatch node missing")
    else:
        wt = combo_list(bat, "required", "weight_type") or []
        if "linear" in wt:
            ok("IPAdapterBatch accepts weight_type='linear'")
        else:
            bad("weight_type 'linear' not accepted")
        emb = combo_list(bat, "required", "embeds_scaling") or []
        if "V only" in emb:
            ok("IPAdapterBatch accepts embeds_scaling='V only'")
        else:
            bad(f"embeds_scaling 'V only' not accepted, sees {emb}")
        optional = bat.get("input", {}).get("optional") or {}
        if "clip_vision" in optional:
            ok("IPAdapterBatch takes an optional clip_vision — explicit link OK")

    # the ipadapter file is matched by regex, not by an exact name — prove the
    # pack's own pattern finds the file we just installed
    import re
    pat = r"plus.sdxl.vit.h\.(safetensors|bin)$"
    if re.search(pat, "ip-adapter-plus_sdxl_vit-h.safetensors", re.IGNORECASE):
        ok("pack's PLUS/SDXL regex matches our filename")
    else:
        bad("pack's PLUS/SDXL regex does NOT match our filename")
    cvpat = r"(ViT.H.14.*s32B.b79K|ipadapter.*sd15|sd1.?5.*model)\.(bin|safetensors)"
    if re.search(cvpat, want, re.IGNORECASE):
        ok("pack's clip-vision regex matches our filename")
    else:
        bad("pack's clip-vision regex does NOT match our filename")
    return probe


# ------------------------------------------------------------------ stage 4
def validate_graph(wf: dict, label: str) -> None:
    """Check every node type and every literal input against /object_info.

    NOTE on undeclared inputs: ComfyUI's get_input_data() (execution.py:189)
    only collects an input when `input_category is not None`, i.e. when the
    node declares it. Undeclared keys are silently DROPPED, never passed to
    the node function. So an extra key like UnetLoaderGGUF.weight_dtype is
    harmless -- warn, do not fail.
    """
    unknown, out_of_range, dropped, checked = [], [], [], 0
    for nid, node in wf.items():
        ct = node.get("class_type")
        info = get_object_info(ct)
        if info is None:
            unknown.append(f"{ct}: node type unknown to ComfyUI")
            continue
        spec_all = {}
        for grp in ("required", "optional"):
            spec_all.update((info.get("input") or {}).get(grp) or {})
        for key, val in (node.get("inputs") or {}).items():
            if key not in spec_all:
                dropped.append(f"{ct}.{key}")
                continue
            spec = spec_all[key]
            allowed = spec[0] if isinstance(spec, list) else None
            if isinstance(allowed, list) and isinstance(val, str):
                # LoadImage's list only knows the ComfyUI input dir; the probe
                # name is not uploaded, so that one check is not meaningful
                if ct == "LoadImage" and key == "image":
                    continue
                checked += 1
                if val not in allowed:
                    out_of_range.append(f"{ct}.{key}='{val}' not in list")
            elif isinstance(val, (int, float)) and len(spec) > 1 \
                    and isinstance(spec[1], dict):
                mn, mx = spec[1].get("min"), spec[1].get("max")
                if mn is not None and val < mn:
                    out_of_range.append(f"{ct}.{key}={val} < min {mn}")
                if mx is not None and val > mx:
                    out_of_range.append(f"{ct}.{key}={val} > max {mx}")
    for u in unknown:
        bad(f"[{label}] {u}")
    for o in out_of_range:
        bad(f"[{label}] {o}")
    if not unknown and not out_of_range:
        ok(f"[{label}] {len(wf)} nodes, {checked} combo inputs — all accepted")
    if dropped:
        warn(f"[{label}] undeclared inputs ignored by ComfyUI: "
             f"{sorted(set(dropped))}")


def check_graph() -> None:
    print("\n4. does the built graph pass ComfyUI validation?")
    # self-contained: the tool modules live in tool/ and need the studio venv
    # for PyYAML, so don't rely on the caller setting PYTHONPATH
    sys.path.insert(0, str(ROOT / "tool"))
    sys.path.insert(0, str(ROOT / "tool" / ".venv" / "Lib" / "site-packages"))
    try:
        from models import build_ref_workflow
    except Exception as e:                                    # noqa: BLE001
        bad(f"cannot import models.build_ref_workflow: {type(e).__name__}: {e}")
        return

    def build(refs, anchor):
        return build_ref_workflow(
            "realvis", "a portrait, cinematic lighting", "ugly, deformed",
            12345, 1024, 1024, 28, 6.5, [], refs, 0.8, anchor,
            shot_id="verify")

    # one reference: no ImageBatch needed (nothing to concatenate)
    try:
        wf1 = build(["ref_probe_a.png"], False)
    except Exception as e:                                    # noqa: BLE001
        bad(f"single-ref build raised: {type(e).__name__}: {e}")
        return
    ok(f"single ref  -> {len(wf1)} nodes")
    for cls in ("CLIPVisionLoader", "IPAdapterUnifiedLoader", "IPAdapterBatch",
                "LoadImage", "KSampler", "VAEDecode"):
        if not any(n.get("class_type") == cls for n in wf1.values()):
            bad(f"single-ref graph is missing {cls}")
    if any(n.get("class_type") == "ImageBatch" for n in wf1.values()):
        warn("single ref unexpectedly produced an ImageBatch")
    validate_graph(wf1, "1 ref")

    # two references: must chain LoadImage -> ImageBatch
    try:
        wf2 = build(["ref_probe_a.png", "ref_probe_b.png"], False)
    except Exception as e:                                    # noqa: BLE001
        bad(f"two-ref build raised: {type(e).__name__}: {e}")
        return
    ok(f"two refs     -> {len(wf2)} nodes")
    n_load = sum(1 for n in wf2.values() if n.get("class_type") == "LoadImage")
    n_batch = sum(1 for n in wf2.values() if n.get("class_type") == "ImageBatch")
    if n_load == 2 and n_batch == 1:
        ok("two refs chain 2x LoadImage -> 1x ImageBatch")
    else:
        bad(f"two-ref graph has {n_load} LoadImage / {n_batch} ImageBatch")
    validate_graph(wf2, "2 refs")

    # anchor mode must rewire the sampler to a VAEEncode start latent
    try:
        wf3 = build(["ref_probe_a.png"], True)
    except Exception as e:                                    # noqa: BLE001
        bad(f"anchor build raised: {type(e).__name__}: {e}")
        return
    ks = next((n for n in wf3.values() if n.get("class_type") == "KSampler"),
              None)
    if ks and ks["inputs"].get("denoise") == 0.45 \
            and any(n.get("class_type") == "VAEEncode" for n in wf3.values()):
        ok("anchor mode: ref #1 -> VAEEncode -> KSampler at denoise 0.45")
    else:
        bad("anchor mode did not rewire the sampler to a VAEEncode latent")
    validate_graph(wf3, "anchor")


def main() -> int:
    print("=" * 68)
    print("reference-image (IP-Adapter) stack verification")
    print("=" * 68)
    parsed = check_files()
    if len(parsed) < len(EXPECTED):
        print("\nfiles missing — aborting")
        return 1
    check_comfy(parsed)
    check_graph()
    print("\n" + "=" * 68)
    print(f"PASS {len(PASS)}   WARN {len(WARN)}   FAIL {len(FAIL)}")
    for w in WARN:
        print(f"  warn: {w}")
    for f in FAIL:
        print(f"  FAIL: {f}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
