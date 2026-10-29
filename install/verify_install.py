"""Acceptance gate for an install (worker box or rebuilt main machine).

  python install/verify_install.py                # full: manifest sizes + sha256 + REQUIRED
  python install/verify_install.py --quick        # sizes only, missing assets are warnings
  python install/verify_install.py --manifest X   # default install/manifest.json
  python install/verify_install.py --log FILE     # also append the output to FILE

Checks, in order:
  1. layout      the core repo files exist; optional folders are reported
  2. manifest    every recorded asset (models/, external_loras/, bible/luts/)
                 exists with the exact size, and the exact sha256 when the
                 manifest was built in full mode (make_manifest.py without
                 --quick). --quick reports missing assets as warnings, so a
                 fresh clone without weights still passes. The default
                 manifest describes one reference machine and is not in git:
                 without it this check is skipped with a warning. A manifest
                 named with --manifest must exist.
  3. REQUIRED    runs tool/scripts/verify_restore.py as a subprocess (the
                 canonical per-file gate the stack already trusts) when a
                 venv python is available; skipped with --quick

Read-only: it writes nothing but the --log file (INSTALL-MAIN.bat passes
logs/install/install.log).
Exit code 0 only when no hard failure remains.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORE_FILES = ("tool/launcher.py", "tool/api.py", "tool/requirements.txt",
              "tool/settings.example.yaml", "start.bat")
ASSET_DIRS = ("models/", "external_loras/", "bible/luts/")


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    """Streamed: model files are up to ~13 GB."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


class Tee:
    """sys.stdout that also appends to the log; a log that cannot be written
    never stops the check."""

    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, s: str) -> int:
        n = self.stream.write(s)
        try:
            self.fh.write(s)
        except (OSError, ValueError):
            pass
        return n

    def flush(self) -> None:
        self.stream.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--manifest", default=None,
                    help="asset manifest to compare with (default: install/manifest.json)")
    ap.add_argument("--log", default=None, help="also append what is printed to this file")
    args = ap.parse_args(argv)
    if not args.log:
        return check(args)
    try:
        fh = open(args.log, "a", encoding="utf-8", errors="replace")
    except OSError:
        return check(args)
    out = sys.stdout
    sys.stdout = Tee(out, fh)
    try:
        return check(args)
    finally:
        sys.stdout = out
        fh.close()


def check(args: argparse.Namespace) -> int:
    fails: list[str] = []
    warns: list[str] = []

    print("== 1. layout ==")
    missing_core = [c for c in CORE_FILES if not (ROOT / c).is_file()]
    for c in missing_core:
        fails.append(f"core repo file missing: {c}")
    if not missing_core:
        print(f"  OK  {len(CORE_FILES)} core files present under {ROOT}")
    for d, why in (("ComfyUI/main.py", "render farm ComfyUI (port 8188)"),
                   ("ComfyUI_LTX/main.py", "LTX-2.5 / MiniMax H3 ComfyUI (8189/8199)"),
                   ("ComfyUI_Qwen21/main.py", "Qwen-Image 2.1 ComfyUI (8190)")):
        state = "OK " if (ROOT / d).is_file() else "-- "
        print(f"  {state} {d}  ({why})")
        if state != "OK ":
            warns.append(f"{d} not present: the {why} will not start "
                         "(see docs/setup/RESTORE_INSTRUCTIONS.md)")
    ext = ROOT / "external_loras" / "VideoGeneration"
    print(f"  {'OK ' if ext.exists() else '-- '} external_loras/VideoGeneration "
          "(optional external video LoRAs)")

    print("== 2. manifest ==")
    mpath = Path(args.manifest) if args.manifest else HERE / "manifest.json"
    if not mpath.exists() and args.manifest:
        fails.append(f"manifest not found: {mpath}")
    elif not mpath.exists():
        # machine-specific and gitignored: a clone has none until one is built
        print("  skipped: no install/manifest.json")
        warns.append("no install/manifest.json, so the model files were not compared with a "
                     "reference machine (build one there with install/make_manifest.py)")
    else:
        doc = json.loads(mpath.read_text(encoding="utf-8"))
        missing = wrong_size = wrong_hash = hashed = 0
        files = doc.get("files", {})
        for rel, ent in files.items():
            f = ROOT / rel
            if not f.is_file():
                missing += 1
                is_asset = rel.startswith(ASSET_DIRS)
                if args.quick and is_asset:
                    warns.append(f"asset not present yet: {rel}")
                else:
                    fails.append(f"missing: {rel}")
                continue
            size = f.stat().st_size
            if size != ent.get("size"):
                wrong_size += 1
                fails.append(f"size mismatch: {rel} ({size} != {ent.get('size')})")
                continue
            if not args.quick and ent.get("sha256"):
                hashed += 1
                if sha256_of(f) != ent["sha256"]:
                    wrong_hash += 1
                    fails.append(f"sha256 mismatch: {rel}")
        print(f"  checked {len(files)} files: missing={missing} "
              f"size-mismatch={wrong_size} hash-mismatch={wrong_hash} "
              f"(hashed {hashed}; manifest mode={doc.get('mode', '?')})")
        if not files:
            warns.append("manifest is empty - generate it on the golden machine")
        elif not args.quick and doc.get("mode") == "quick":
            warns.append("manifest was built with --quick: sizes verified, "
                         "hashes not (run make_manifest.py without --quick)")

    print("== 3. REQUIRED (tool/scripts/verify_restore.py) ==")
    if args.quick:
        print("  skipped (--quick)")
    else:
        vr = ROOT / "tool" / "scripts" / "verify_restore.py"
        py = ROOT / "tool" / ".venv" / "Scripts" / "python.exe"
        exe = str(py if py.exists() else sys.executable)
        if vr.exists():
            env = {"PYTHONPATH": "", "PATH": os.environ.get("PATH", ""),
                   "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
            r = subprocess.run([exe, str(vr)], capture_output=True,
                               text=True, env=env, timeout=600)
            tail = (r.stdout or "").strip().splitlines()[-12:]
            print("\n".join("  " + ln for ln in tail))
            if r.returncode != 0:
                fails.append("verify_restore reported failures (see above)")
        else:
            warns.append(f"{vr} not found - skipped")

    print()
    for w in warns:
        print(f"[WARN] {w}")
    if fails:
        print(f"RESULT: {len(fails)} hard failure(s)")
        for f in fails:
            print(f"  FAIL {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
