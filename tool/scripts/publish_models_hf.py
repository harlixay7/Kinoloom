"""Publish the Colab model groups to the private HuggingFace repo.

One-time (and re-runnable) bridge between this PC's model tree and cloud
workers: walks install/colab_model_groups.json, fills in byte sizes +
sha256 (streaming, cached back into the manifest), and uploads each file to
the repo with the SAME relative path, so colab/fetch_models.py on the worker
can mirror the layout exactly.

Usage (from the studio venv):
    set HF_TOKEN=hf_...                      (write token, fine-grained)
    python tool/scripts/publish_models_hf.py --groups h3_shared,h3_dit_singularity
    python tool/scripts/publish_models_hf.py --groups all --repo datasets/you/kinoloom-models

Re-runnable: a file whose path already exists in the repo is skipped (by
path only; sizes and hashes are not compared), so an interrupted run resumes
where it stopped. Each upload is one Hub commit, so a listed path is a
complete file. To replace a file whose local copy changed, delete it from the
repo first. Hashes are computed once and cached in the manifest, which is
rewritten after every hashed file and once at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "install" / "colab_model_groups.json"
CHUNK = 8 * 2**20


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def source_path(f: dict) -> Path:
    """A manifest source path; relative ones are relative to the studio root."""
    src = Path(str(f.get("source") or ""))
    return src if src.is_absolute() else ROOT / src


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--groups", default="all",
                    help="comma-separated group names, or 'all'")
    ap.add_argument("--repo", default="",
                    help="override hf_repo, e.g. datasets/you/kinoloom-models")
    ap.add_argument("--revision", default="", help="override hf_revision")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve + measure only; no uploads, no writes")
    args = ap.parse_args()

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.repo:
        data["hf_repo"] = args.repo
    if args.revision:
        data["hf_revision"] = args.revision
    repo = str(data.get("hf_repo") or "")
    if not repo or "OWNER" in repo:
        print("manifest hf_repo is not set yet — pass --repo "
              "datasets/<user>/<name> once; it is remembered in the manifest")
        return 2

    names = list((data.get("groups") or {}).keys())
    if args.groups != "all":
        names = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in names if g not in (data.get("groups") or {})]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}")
        return 2

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub missing — pip install huggingface_hub hf_transfer "
              "into the studio venv first")
        return 2
    import os
    token = os.environ.get("HF_TOKEN") or ""
    if not token:
        print("HF_TOKEN env var not set — needed for a private repo upload")
        return 2
    repo_type = "dataset" if repo.startswith("datasets/") else "model"
    repo_id = repo.split("/", 1)[1] if repo.startswith("datasets/") else repo

    api = HfApi(token=token)
    if not args.dry_run:
        api.create_repo(repo_id=repo_id, repo_type=repo_type, private=True,
                        exist_ok=True)
        # exist_ok keeps a repo that already exists as it is, public or not
        if not api.repo_info(repo_id=repo_id, repo_type=repo_type).private:
            print(f"{repo} is public — refusing to upload: several model "
                  "licenses do not allow re-publishing the weights "
                  "(THIRD_PARTY_NOTICES.md section 3)")
            return 2
        print(f"repo ready: {repo} (private {repo_type})")

    total_b = 0
    todo = []
    for gname in names:
        for f in (data["groups"][gname].get("files") or []):
            todo.append((gname, f))

    for i, (gname, f) in enumerate(todo, 1):
        rel = str(f.get("path") or "")
        src = source_path(f)
        if not src.is_file():
            print(f"[{i}/{len(todo)}] MISSING on this PC: {src}")
            return 2
        if not f.get("bytes"):
            f["bytes"] = src.stat().st_size
        total_b += int(f["bytes"])
        print(f"[{i}/{len(todo)}] {gname}/{rel} "
              f"({int(f['bytes']) / 2**30:.2f}GB)")

    print(f"selected: {len(todo)} file(s), {total_b / 2**30:.1f}GB total")
    if args.dry_run:
        for missing_hash in [f for _, f in todo if not f.get("sha256")]:
            print(f"  needs sha256 at publish time: {missing_hash['path']}")
        return 0

    ok = True
    try:
        in_repo = set(api.list_repo_files(repo_id=repo_id, repo_type=repo_type))
    except Exception:  # noqa: BLE001
        in_repo = set()
    for i, (gname, f) in enumerate(todo, 1):
        rel = str(f["path"])
        src = source_path(f)
        if not f.get("sha256"):
            print(f"[{i}/{len(todo)}] hashing {rel} …", flush=True)
            t0 = time.time()
            f["sha256"] = sha256_file(src)
            print(f"    sha256 in {time.time() - t0:.0f}s — cached in manifest")
            save_manifest(data)
        if rel in in_repo:
            print(f"[{i}/{len(todo)}] already in repo: {rel} — skipped")
            continue
        print(f"[{i}/{len(todo)}] uploading {rel} "
              f"({int(f['bytes']) / 2**30:.2f}GB) …", flush=True)
        t0 = time.time()
        try:
            api.upload_file(path_or_fileobj=str(src), path_in_repo=rel,
                            repo_id=repo_id, repo_type=repo_type)
        except Exception as e:  # noqa: BLE001
            print(f"    UPLOAD FAILED: {e}")
            ok = False
            continue
        dt = max(time.time() - t0, 0.001)
        print(f"    done in {dt / 60:.1f} min "
              f"({int(f['bytes']) / dt / 2**20:.0f}MB/s)")
    save_manifest(data)
    print("manifest updated with sizes + hashes" if ok
          else "finished WITH ERRORS — re-run to resume")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
