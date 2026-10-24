"""Manifest builder - pins the exact bytes of the assets git does not carry
(model weights, external LoRAs, LUTs), so another machine can be checked
against the golden one.

  python install/make_manifest.py              # full: sha256 + size (slow on ~150 GB)
  python install/make_manifest.py --quick      # size only (fast; still catches
                                               #  most drift: wrong/old files)
  python install/make_manifest.py --out X      # default: install/manifest.json

Scope (read-only; ROOT-relative paths): models/**, external_loras/**,
bible/luts/*.cube. Tracked files (workflows, registries, configs) are pinned
by git, and tool/settings.yaml is per-install, so neither belongs here.
Weights kept outside the tree (paths.extra_model_dirs) are not covered.

Additive-only: the manifest is a NEW file; nothing in the tree is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = Path(__file__).resolve().parent / "manifest.json"

TARGETS = [
    ("models", "**/*"),
    ("external_loras", "**/*"),
    ("bible/luts", "*.cube"),
]
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIP_SUFFIXES = {".log", ".tmp"}


def iter_files():
    for base, pattern in TARGETS:
        p = ROOT / base
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (q for q in p.glob(pattern or "*") if q.is_file())


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="record sizes only (no hashing)")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    entries: dict[str, dict] = {}
    t0 = time.time()
    n = 0
    for f in sorted(iter_files()):
        rel = f.relative_to(ROOT).as_posix()
        if f.name.lower() in SKIP_NAMES or f.suffix.lower() in SKIP_SUFFIXES:
            continue
        ent: dict = {"size": f.stat().st_size}
        if not args.quick:
            ent["sha256"] = sha256_of(f)
        entries[rel] = ent
        n += 1
        if n % 25 == 0:
            print(f"  {n} files... ({time.time()-t0:.0f}s)", flush=True)

    doc = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "quick" if args.quick else "full",
        "file_count": n,
        "files": entries,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    total_gb = round(sum(e["size"] for e in entries.values()) / 2**30, 1)
    print(f"manifest: {out} - {n} files, {total_gb} GB, "
          f"{time.time()-t0:.0f}s, mode={doc['mode']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
