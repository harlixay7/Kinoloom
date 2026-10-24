"""Fetch Kinoloom model groups onto a cloud worker (Colab).

Standalone + stdlib-only by design: this runs in the bootstrap BEFORE any
venv or the worker agent exists. Reads install/colab_model_groups.json from
the cloned repo, resolves the union of the requested groups, and streams
each file from the private HF dataset repo into models/<path> with the same
relative layout the studio uses. .part + Range resume means an interrupted
bootstrap (or a flaky HF connection) continues where it stopped; files that
already exist with a matching size are skipped, so re-running after a
session death only pulls deltas.

Usage:
    HF_TOKEN=hf_... python3 colab/fetch_models.py \
        --groups h3_shared,h3_dit_singularity --models-dir /content/kinoloom/models

Exit code is non-zero if any file failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 8 * 2**20
REPO_ROOT = Path(__file__).resolve().parents[1]


def hf_url(repo: str, rev: str, fname: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/{rev}/{fname}"


def headers_for(url: str, token: str) -> dict:
    h = {"User-Agent": "kinoloom-colab-fetch/1.0"}
    if token and "huggingface.co" in urllib.parse.urlparse(url).netloc:
        h["Authorization"] = f"Bearer {token}"
    return h


def stream_to(dest: Path, url: str, token: str, want_size: int,
              want_sha: str) -> tuple[float, bool]:
    """Returns (avg MB/s, resumed?). Raises on hard failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    done = tmp.stat().st_size if tmp.exists() else 0
    hasher = hashlib.sha256()
    if done:
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                hasher.update(chunk)
        if want_size and done > want_size:
            done, hasher = 0, hashlib.sha256()
            tmp.unlink()
    req = urllib.request.Request(url, headers=headers_for(url, token))
    if done:
        req.add_header("Range", f"bytes={done}-")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as r:
        resumed = bool(done) and getattr(r, "status", 200) == 206
        if done and not resumed:
            done, hasher = 0, hashlib.sha256()
        got = done
        with open(tmp, "ab" if done else "wb") as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                got += len(chunk)
    total = tmp.stat().st_size
    if want_size and total != want_size:
        raise IOError(f"size mismatch: got {total}, want {want_size}")
    if want_sha and hasher.hexdigest().lower() != want_sha.lower():
        tmp.unlink(missing_ok=True)
        raise IOError("sha256 mismatch")
    tmp.replace(dest)
    dt = max(time.time() - t0, 0.001)
    return total / dt / 2**20, (done > 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--groups", required=True,
                    help="comma-separated group names")
    ap.add_argument("--models-dir", required=True,
                    help="worker models dir (becomes the ComfyUI tree root)")
    ap.add_argument("--manifest", default=str(REPO_ROOT / "install"
                                               / "colab_model_groups.json"))
    ap.add_argument("--budget-gb", type=float, default=0,
                    help="refuse if selected bytes exceed this (0 = manifest "
                         "disk_budget_gb)")
    args = ap.parse_args()

    import os
    token = os.environ.get("HF_TOKEN") or ""
    if not token:
        print("WARNING: HF_TOKEN not set — private repos will 401")

    data = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    repo = str(data.get("hf_repo") or "")
    rev = str(data.get("hf_revision") or "main")
    if not repo or "OWNER" in repo:
        print("manifest hf_repo not published yet — run "
              "publish_models_hf.py on the PC first")
        return 2

    seen: dict[str, dict] = {}
    for gname in [g.strip() for g in args.groups.split(",") if g.strip()]:
        g = (data.get("groups") or {}).get(gname)
        if not g:
            print(f"unknown group: {gname} "
                  f"(have: {', '.join(data.get('groups') or {})})")
            return 2
        for f in (g.get("files") or []):
            seen.setdefault(str(f["path"]), f)

    want = sum(int(f.get("bytes") or 0) for f in seen.values())
    budget = args.budget_gb or float(data.get("disk_budget_gb") or 0)
    free = shutil.disk_usage(args.models_dir
                             if Path(args.models_dir).exists()
                             else "/").free
    print(f"{len(seen)} file(s), {want / 2**30:.1f}GB selected; "
          f"{free / 2**30:.1f}GB free"
          + (f"; budget {budget:.0f}GB" if budget else ""))
    if budget and want > budget * 2**30:
        print(f"REFUSING: selection exceeds the {budget:.0f}GB disk budget — "
              "drop a group (e.g. a second DiT) and re-run")
        return 2
    if want and free < want + 3 * 2**30:
        print(f"REFUSING: not enough disk ({free / 2**30:.1f}GB free for "
              f"{want / 2**30:.1f}GB + margin)")
        return 2

    failures = []
    for i, (rel, f) in enumerate(seen.items(), 1):
        dest = Path(args.models_dir) / rel
        gb = int(f.get("bytes") or 0) / 2**30
        if dest.is_file() and (not f.get("bytes")
                               or dest.stat().st_size == int(f["bytes"])):
            print(f"[{i}/{len(seen)}] have {rel} — skipped")
            continue
        url = hf_url(repo, rev, rel)
        print(f"[{i}/{len(seen)}] {rel} ({gb:.2f}GB) …", flush=True)
        try:
            speed, resumed = stream_to(dest, url, token,
                                       int(f.get("bytes") or 0),
                                       str(f.get("sha256") or ""))
            tag = "resumed" if resumed else "fetched"
            print(f"    {tag} at {speed:.0f}MB/s avg")
        except urllib.error.HTTPError as e:
            hint = (" (HF_TOKEN wrong/missing for a private repo?)"
                    if e.code in (401, 403) else "")
            print(f"    FAILED: HTTP {e.code}{hint}")
            failures.append(rel)
        except Exception as e:  # noqa: BLE001
            print(f"    FAILED: {e}")
            failures.append(rel)

    if failures:
        print(f"{len(failures)} file(s) FAILED — fix and re-run (resume "
              "continues from the .part files):")
        for r in failures:
            print(f"  - {r}")
        return 1
    print("all groups present — model state satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
