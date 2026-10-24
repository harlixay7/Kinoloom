"""Model inventory — resolves every workflow template to the concrete model
files it loads, then cross-checks them against what is on disk.

Reads `tool/workflows.py`'s SUBST dict with `ast` (no imports, so `core.py`
and its dependencies are never touched), performs the same {TOKEN}
substitution `WorkflowBuilder.build()` does, then walks each resulting
ComfyUI graph collecting every loader filename input.

Usage (from the studio root, any interpreter — stdlib only):
    python tool/scripts/model_inventory.py
Writes temp/model_inventory.json and temp/model_inventory.txt
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF_DIR = ROOT / "tool" / "workflows"
MODELS = ROOT / "models"
OUT_JSON = ROOT / "temp" / "model_inventory.json"
OUT_TXT = ROOT / "temp" / "model_inventory.txt"

TOKEN = re.compile(r"\{([A-Z0-9_]+)\}")

# inputs on ComfyUI loader nodes that name an on-disk file
FILE_KEYS = {
    "unet_name", "ckpt_name", "lora_name", "vae_name", "clip_name",
    "clip_name1", "clip_name2", "clip_name3", "model_name", "control_net_name",
    "ipadapter_file", "style_model_name", "gligen_name", "photomaker_model_name",
    "upscale_model_name", "model", "tokenizer",
}

DUMMY = {
    "PROMPT": "p", "NEGATIVE": "n", "SEED": 1, "WIDTH": 1024, "HEIGHT": 1024,
    "STEPS": 20, "FRAMES": 49, "FPS": 16, "FPS_F": 16.0, "STEPS_HALF": 10,
}

# Directories worth indexing for on-disk model files (venvs/nodes excluded).
# `ComfyUI/custom_nodes` is needed because node packs keep their own ckpts
# (e.g. Frame-Interpolation's ckpts/rife/rife47.pth).
SCAN_DIRS = ["models", "external_loras", "ComfyUI/models",
             "ComfyUI/custom_nodes", "roster", "tool"]
MODEL_EXT = {".safetensors", ".gguf", ".pth", ".pt", ".onnx", ".bin", ".ckpt",
             ".sft", ".cube"}


def autofill(values: dict, tokens: set) -> dict:
    """Templates reference runtime-only tokens ({CHAR_STR}, {DENOISE} ...) that
    the caller supplies. Give them a plausible dummy so the graph still parses,
    and remember which ones we invented.

    Defaults are NUMERIC on purpose: the substitution runs before JSON parsing
    and strips string quotes, so a bare token like `"denoise": {DENOISE}` is
    only valid JSON if the replacement is a number.
    """
    auto = {}
    for t in tokens:
        if t.endswith("_BOOL") or t.startswith("USE_"):
            v = False
        elif t.endswith("_STR") or t.endswith("_WEIGHT"):
            v = 0.7
        else:
            v = 1.0
        values[t] = v
        auto[t] = v
    return auto


def load_subst() -> dict:
    src = (ROOT / "tool" / "workflows.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SUBST":
                    return ast.literal_eval(node.value)
    raise SystemExit("SUBST not found in workflows.py")


def resolve(template_text: str, values: dict) -> tuple[dict, set]:
    """Same substitution WorkflowBuilder.build() performs, but tolerant:
    any token the caller would have supplied at runtime is auto-filled."""
    unknown = set(TOKEN.findall(template_text)) - set(values)
    auto = autofill(values, unknown)

    def esc(v):
        return json.dumps(v)[1:-1] if isinstance(v, str) else json.dumps(v)

    def repl_in_str(m):
        return m.group(1) + esc(values[m.group(2)]) + m.group(3)

    text = re.sub(r'("[^"]*)\{([A-Z0-9_]+)\}([^"]*")', repl_in_str, template_text)
    text = TOKEN.sub(lambda m: json.dumps(values[m.group(1)]), text)
    return json.loads(text), auto


def strip_disabled_loras(wf: dict) -> dict:
    """Mirror WorkflowBuilder: drop LoraLoader* whose lora_name == 'none'."""
    for nid, node in list(wf.items()):
        if node.get("class_type") in ("LoraLoaderModelOnly", "LoraLoader") \
                and node.get("inputs", {}).get("lora_name") == "none":
            srcs = {0: node["inputs"]["model"]}
            if node["class_type"] == "LoraLoader":
                srcs[1] = node["inputs"]["clip"]
            for other in wf.values():
                for k, v in other.get("inputs", {}).items():
                    if isinstance(v, list) and len(v) == 2 and v[0] == nid:
                        other["inputs"][k] = srcs.get(v[1], srcs[0])
            del wf[nid]
    return wf


def collect(wf: dict) -> list[dict]:
    found = []
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "?")
        for k, v in (node.get("inputs") or {}).items():
            if k in FILE_KEYS and isinstance(v, str) and v and v != "none":
                found.append({"node": nid, "class_type": ct, "slot": k, "file": v})
    return found


def index_disk() -> tuple[dict, list]:
    """Index model files by basename AND by trailing path fragment, so a
    template value like 'bbox\\face_yolov8m.onnx' still resolves.
    Returns (lookup, every_model_file)."""
    lookup: dict[str, list] = {}
    every: list = []
    for sub in SCAN_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in MODEL_EXT:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            rec = {"rel": rel, "bytes": size}
            every.append(rec)
            parts = rel.split("/")
            for i in range(len(parts)):
                lookup.setdefault("/".join(parts[i:]), []).append(rec)
            lookup.setdefault(p.name, []).append(rec)
    return lookup, every


def main() -> int:
    subst = load_subst()
    values = dict(subst)
    values.update(DUMMY)
    disk, every = index_disk()

    templates = sorted(p.stem for p in WF_DIR.glob("*.json"))
    py_src = {}
    for p in (ROOT / "tool").glob("*.py"):
        try:
            py_src[p.name] = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass

    report = []
    for stem in templates:
        raw = (WF_DIR / f"{stem}.json").read_text(encoding="utf-8")
        try:
            wf, auto = resolve(raw, values)
        except Exception as e:                                   # noqa: BLE001
            report.append({"template": stem, "error": f"{type(e).__name__}: {e}"})
            continue
        wf = strip_disabled_loras(wf)
        refs = collect(wf)
        callers = sorted(n for n, s in py_src.items() if stem in s)
        for r in refs:
            key = r["file"].replace("\\", "/")
            hits = disk.get(key, [])
            r["on_disk"] = [h["rel"] for h in hits]
            r["bytes"] = hits[0]["bytes"] if hits else None
        report.append({
            "template": stem,
            "nodes": len(wf),
            "runtime_tokens": sorted(auto),
            "callers": callers,
            "refs": refs,
        })

    OUT_JSON.write_text(json.dumps(report, indent=1), encoding="utf-8")

    lines = []
    for t in report:
        if "error" in t:
            lines.append(f"### {t['template']}  <ERROR> {t['error']}")
            lines.append("")
            continue
        lines.append(f"### {t['template']}   ({t['nodes']} nodes)")
        lines.append(f"    called from: {', '.join(t['callers']) or '(nothing in tool/*.py)'}")
        if t["runtime_tokens"]:
            lines.append(f"    runtime-only tokens: {t['runtime_tokens']}")
        for r in t["refs"]:
            size = f"{r['bytes']/1e6:8.1f} MB" if r["bytes"] else "  MISSING  "
            lines.append(f"    {size}  {r['slot']:<14} {r['file']}   [{r['class_type']}]")
        lines.append("")

    lines.append("=" * 70)
    lines.append("EVERY MODEL FILE ON DISK")
    lines.append("=" * 70)
    for rec in sorted(every, key=lambda x: (-x["bytes"], x["rel"])):
        lines.append(f"{rec['bytes']/1e6:9.1f} MB  {rec['rel']}")
    lines.append(f"\ntotal: {len(every)} files, "
                 f"{sum(r['bytes'] for r in every)/1e9:.2f} GB")

    OUT_TXT.write_text("\n".join(lines), encoding="utf-8")
    print(f"templates: {len(report)}   model files indexed: {len(every)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
