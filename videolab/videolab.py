#!/usr/bin/env python3
"""videolab: reproducible benchmarks for 20B-33B video diffusion transformers on 8 GB GPUs.

Subcommands:
  doctor    inspect the host GPU (NVML), PyTorch CUDA readiness, and run the
            fused W4A8 kernel parity gate (bench/gate_w4a8.py)
  download  fetch and checksum-verify model assets for a declared profile
  selftest  run offline unit checks (tiling closure, premultiplied alpha,
            token/FLOP derivations, config sanity), the W4A8 gate when the GPU
            stack is present, and an optional micro-render; --lint adds the
            developer-only repository path-hygiene scan
  bench     submit a proven workflow to a running ComfyUI instance and record
            NVML telemetry for the run

Nothing here hardcodes drive paths. Model storage and run records default to
`models/` and `runs/` next to this file and can be relocated with the
VIDEOLAB_MODELS_DIR and VIDEOLAB_RUNS_DIR environment variables.

See README.md for the hardware context and docs/ for the measurement record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = REPO_ROOT / "configs"
WORKFLOW_DIR = REPO_ROOT / "workflows"
BENCH_DIR = REPO_ROOT / "bench"
MODELS_DIR = Path(os.environ.get("VIDEOLAB_MODELS_DIR", REPO_ROOT / "models"))
RUNS_DIR = Path(os.environ.get("VIDEOLAB_RUNS_DIR", REPO_ROOT / "runs"))
DEFAULT_COMFY = os.environ.get("VIDEOLAB_COMFY_URL", "http://127.0.0.1:8188")

sys.path.insert(0, str(BENCH_DIR))
import harness  # noqa: E402

DRIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![\\/])")
SUPPORTED_MODELS = ["h3", "ltx25", "qwen21"]
# CLI parameters that must be non-empty when present: ComfyUI LoadImage treats an
# empty name as invalid, and a wrong-but-empty default is exactly the silent
# misconfiguration the placeholder guard exists to prevent.
NONEMPTY_PARAMS = {"CHARACTER_IMAGE", "POWER_IMAGE", "ENVIRONMENT_IMAGE"}

MODEL_DEFAULTS: dict[str, dict[str, object]] = {
    "ltx25": {
        "FPS": 24,
        "FRAMES": 121,
        "STAGE1_W": 960,
        "STAGE1_H": 544,
        "NEGATIVE": "",
        "SEED": 519438276610342,
        "SEED2": 519438276610342,
        "SHOT_ID": "bench",
    },
    "h3": {
        "SEED": 519438276610342,
        "SEED_REFINE": 519438276610342,
        # Reference images have no default on purpose: an empty LoadImage value
        # fails server-side, so the CLI requires them to be supplied explicitly.
    },
    "qwen21": {
        "NEGATIVE": "",
        "REFINE_PROMPT": "",
        "SEED": 12345,
        "REFINE_SEED": 12345,
    },
}


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def sha256_of(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def human_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000.0
    return f"{n} B"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# Doctor subcommand

def cmd_doctor(args) -> int:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "failures": [],
        "warnings": [],
    }

    print(f"[doctor] python {report['python']} on {report['platform']}")
    if sys.version_info < (3, 12):
        report["failures"].append(f"python {report['python']} < 3.12")

    # PyTorch CUDA readiness
    torch_state = {"installed": False}
    try:
        import torch
        torch_state = {
            "installed": True,
            "version": torch.__version__,
            "cuda_runtime": torch.version.cuda,
            "cuda_available": torch.cuda.is_available(),
        }
        if torch.cuda.is_available():
            torch_state["device"] = torch.cuda.get_device_name(0)
            torch_state["compute_capability"] = list(torch.cuda.get_device_capability(0))
            torch_state["vram_total_gib"] = round(
                torch.cuda.get_device_properties(0).total_memory / 2**30, 2)
        else:
            report["failures"].append("torch reports cuda_available=False")
    except ImportError:
        report["failures"].append("torch is not installed (install the cuda extra)")
    report["torch"] = torch_state
    print(f"[doctor] torch: {json.dumps(torch_state)}")

    # comfy-kitchen
    ck_state = {"installed": False}
    try:
        import importlib.metadata as md
        ck_state = {"installed": True, "version": md.version("comfy-kitchen")}
    except Exception:  # noqa: BLE001
        report["failures"].append("comfy-kitchen is not installed (fused W4A8 kernels unavailable)")
    report["comfy_kitchen"] = ck_state
    print(f"[doctor] comfy-kitchen: {json.dumps(ck_state)}")

    # NVML
    nvml = harness.nvml_probe()
    report["nvml"] = nvml
    print(f"[doctor] NVML: {json.dumps(nvml)}")
    if not nvml.get("nvml_available"):
        report["failures"].append("NVML is not accessible; telemetry would be empty")

    # W4A8 gate
    gate_out = RUNS_DIR / "doctor_gate_w4a8.json"
    gate_result = {"status": "NOT RUN"}
    if not args.no_gate:
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "gate_w4a8.py"), "--out", str(gate_out)],
            capture_output=True, text=True)
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
        if proc.returncode != 0 and (proc.stderr or "").strip():
            err_tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
            tail += f"\n--- stderr tail ---\n{err_tail}"
        print(f"[doctor] W4A8 gate output (tail):\n{tail}")
        if proc.returncode == 0:
            gate_result = {"status": "PASS", "record": _rel(gate_out)}
        elif proc.returncode == 3:
            gate_result = {"status": "SKIP (environment)", "record": _rel(gate_out)}
            report["warnings"].append("W4A8 gate skipped: torch/comfy-kitchen/CUDA unavailable")
        else:
            gate_result = {"status": "FAIL", "record": _rel(gate_out)}
            report["failures"].append(f"W4A8 gate FAILED (exit {proc.returncode})")
    report["w4a8_gate"] = gate_result
    print(f"[doctor] W4A8 gate: {gate_result['status']}")

    report_path = RUNS_DIR / f"doctor_{int(time.time())}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[doctor] report -> {_rel(report_path)}")

    if report["failures"]:
        for failure in report["failures"]:
            print(f"[doctor] FAIL: {failure}")
        return 3 if any("gate SKIPPED" in f or "not installed" in f or "cuda_available" in f
                        for f in report["failures"]) else 2
    print("[doctor] environment OK")
    return 0


# Download subcommand

class DownloadError(RuntimeError):
    """Download failure with per-attempt detail (source, URL, error)."""

    def __init__(self, message: str, attempts: list[tuple[str, str]]):
        super().__init__(message)
        self.attempts = attempts


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Best-effort extraction of the Hugging Face JSON error message."""
    try:
        body = json.loads(exc.read().decode(errors="replace"))
        message = body.get("error") or body.get("message") or ""
        return str(message)[:180]
    except Exception:  # noqa: BLE001
        return str(getattr(exc, "reason", "") or "")[:180]


def _http_download(url: str, dest: Path, token: str | None, attempts: int = 3) -> str:
    """Streaming download with retries. 401/403/404 fail immediately."""
    problems: list[tuple[str, str]] = []
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 ** attempt)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as resp, open(part, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            part.replace(dest)
            return "http"
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            entry = (url, f"HTTP {exc.code} {detail}".strip())
            problems.append(entry)
            if exc.code in (401, 403, 404):
                part.unlink(missing_ok=True)
                raise DownloadError(entry[1], problems)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            problems.append((url, f"{type(exc).__name__}: {str(exc)[:160]}"))
        part.unlink(missing_ok=True)
    raise DownloadError(problems[-1][1] if problems else "download failed", problems)


def _fetch_from_source(repo: str, repo_path: str, dest: Path, token: str | None):
    """Try huggingface_hub first, then direct HTTP. Returns (method, url)."""
    url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(repo_path)}"
    problems: list[tuple[str, str]] = []
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from huggingface_hub import hf_hub_download
        cached = hf_hub_download(repo_id=repo, filename=repo_path, token=token)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, dest)
        return "huggingface_hub", url
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - fall through to direct HTTP
        problems.append((url, f"huggingface_hub: {type(exc).__name__}: {str(exc)[:160]}"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        method = _http_download(url, dest, token)
        return method, url
    except DownloadError as exc:
        raise DownloadError(str(exc), problems + exc.attempts)


def _sources(asset: dict) -> list[tuple[str, str]]:
    """Primary source first, then verified mirrors."""
    repo_path = asset.get("repo_path") or Path(asset["filename"]).name
    out = [(asset["repo"], repo_path)]
    for mirror in asset.get("mirrors") or []:
        out.append((mirror["repo"], mirror.get("repo_path") or repo_path))
    return out


def _verify(path: Path, asset: dict) -> tuple[bool, str]:
    """Verify size, then sha256 (lab-recorded) or sha256_source (served-file pin)."""
    expected_size = asset.get("size_bytes")
    if expected_size is not None and path.stat().st_size != expected_size:
        return False, f"size {path.stat().st_size} != {expected_size}"
    digest = asset.get("sha256")
    kind = "sha256 (lab-recorded)"
    if not digest:
        digest = asset.get("sha256_source")
        kind = "sha256_source (served-file pin; lab digest not archived for this asset)"
    if digest:
        actual = sha256_of(path)
        if actual != digest:
            return False, f"digest mismatch: {actual[:16]}... != {digest[:16]}... [{kind}]"
        return True, f"verified against {kind}"
    return True, "size-only check (no digest recorded)"


def _selected_assets(profile: dict, only: list[str] | None) -> list[str]:
    ids = list(profile["assets"])
    if only:
        unknown = [asset_id for asset_id in only if asset_id not in ids]
        if unknown:
            raise SystemExit(f"[download] --only asset(s) not in this profile: {unknown}")
        ids = [asset_id for asset_id in ids if asset_id in only]
    return ids


def _check_sources(ids: list[str], manifest: dict, token: str | None) -> int:
    """Reachability + digest check for every recorded source, no downloads."""
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
    except ImportError:
        api = None
        print("[download] huggingface_hub not installed; using HTTP HEAD checks (no digest info)")
    failures = 0
    report: list[dict] = []
    for asset_id in ids:
        asset = manifest["assets"][asset_id]
        if not asset.get("repo"):
            print(f"  {asset_id} [no source] manual/BYOW asset; nothing to check")
            report.append({"asset": asset_id, "status": "no source"})
            continue
        for index, (repo, repo_path) in enumerate(_sources(asset)):
            label = "primary" if index == 0 else f"mirror {index}"
            if api is not None:
                try:
                    info = api.model_info(repo_id=repo, files_metadata=True)
                    entry = next((s for s in info.siblings if s.rfilename == repo_path), None)
                    if entry is None:
                        body = f"MISSING FILE at {repo}::{repo_path}"
                        failures += 1
                    else:
                        lfs = getattr(entry, "lfs", None)
                        sha = lfs.get("sha256") if isinstance(lfs, dict) else (lfs.sha256 if lfs else None)
                        expected = asset.get("sha256") or asset.get("sha256_source")
                        if sha and expected:
                            verdict = "digest MATCH" if sha == expected else "digest DIFFERS"
                            if verdict == "digest DIFFERS":
                                failures += 1
                        else:
                            verdict = "no digest pin"
                        body = f"OK {entry.size} bytes ({verdict})"
                except Exception as exc:  # noqa: BLE001
                    body = f"ERROR {type(exc).__name__}: {str(exc)[:140]}"
                    failures += 1
            else:
                url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(repo_path)}"
                try:
                    request = urllib.request.Request(url, method="HEAD")
                    with urllib.request.urlopen(request, timeout=30) as resp:
                        body = f"OK (HTTP {resp.status})"
                except urllib.error.HTTPError as exc:
                    body = f"HTTP {exc.code}"
                    failures += 1
                except Exception as exc:  # noqa: BLE001
                    body = f"ERROR {type(exc).__name__}"
                    failures += 1
            print(f"  {asset_id} [{label}] {repo}::{repo_path}\n      {body}")
            report.append({"asset": asset_id, "source": label, "repo": repo,
                           "repo_path": repo_path, "status": body})
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"source_check_{int(time.time())}.json"
    out.write_text(json.dumps({"assets": report}, indent=2), encoding="utf-8")
    print(f"[download] source check report -> {_rel(out)}")
    return 0 if failures == 0 else 1


def cmd_download(args) -> int:
    manifest = load_json(CONFIG_DIR / "models.manifest.json")
    profiles = load_json(CONFIG_DIR / "profiles.json")["profiles"]
    if args.profile not in profiles:
        print(f"[download] unknown profile {args.profile!r}; choose from: {', '.join(profiles)}")
        return 2
    profile = profiles[args.profile]
    token = os.environ.get("HF_TOKEN")
    ids = _selected_assets(profile, args.only)

    if args.check_sources:
        print(f"[download] checking every recorded source for profile {args.profile!r} "
              f"({len(ids)} assets); no files will be written")
        return _check_sources(ids, manifest, token)

    report: list[dict] = []
    failures = 0

    if not profile.get("downloads", True):
        if not args.acknowledge_license:
            print("[download] h3-byow requires --acknowledge-license.")
            print("  MiniMax H3 weights are governed by the MiniMax H3 Community License, which")
            print("  carries territorial restrictions. This tool does not download them. The flag")
            print("  confirms you have obtained the weights lawfully and accept those terms.")
            return 2
        print(f"[download] h3-byow: validating {len(ids)} user-supplied assets under {MODELS_DIR}")
        for asset_id in ids:
            asset = manifest["assets"][asset_id]
            path = MODELS_DIR / asset["filename"]
            if not path.exists():
                print(f"  MISSING  {asset_id}: expected at {_rel(path)}")
                print(f"           role: {asset['role']}")
                failures += 1
                report.append({"asset": asset_id, "status": "missing", "expected": _rel(path)})
                continue
            if args.verify_hashes:
                ok, detail = _verify(path, asset)
            else:
                expected = asset.get("size_bytes")
                ok = expected is None or path.stat().st_size == expected
                detail = ("size ok" if expected is not None and ok else
                          ("size unknown; re-run with --verify-hashes for a digest check"
                           if expected is None else f"size {path.stat().st_size} != {expected}"))
            print(f"  {'OK' if ok else 'BAD':8} {asset_id}: {path.stat().st_size} bytes ({detail})")
            report.append({"asset": asset_id, "status": "ok" if ok else "bad",
                           "detail": detail, "bytes": path.stat().st_size})
            if not ok:
                failures += 1
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        out = RUNS_DIR / f"manifest_check_h3_byow_{int(time.time())}.json"
        out.write_text(json.dumps({"profile": "h3-byow", "rows": report}, indent=2), encoding="utf-8")
        print(f"[download] validation record -> {_rel(out)}")
        print(f"[download] done, {failures} failure(s)")
        return 0 if failures == 0 else 1

    print(f"[download] profile {args.profile!r} -> {MODELS_DIR}")
    for asset_id in ids:
        asset = manifest["assets"][asset_id]
        filename = asset["filename"]
        dest = MODELS_DIR / filename
        if asset.get("downloadable") is False:
            reason = asset.get("note") or asset.get("license_note") or "not downloadable by design"
            print(f"  SKIP     {asset_id}: {reason}")
            report.append({"asset": asset_id, "status": "skipped", "reason": reason})
            continue
        if not asset.get("repo"):
            reason = asset.get("verification", "no repository recorded; provision manually")
            print(f"  MANUAL   {asset_id}: {reason}")
            report.append({"asset": asset_id, "status": "manual", "reason": reason})
            continue
        if dest.exists():
            ok, detail = _verify(dest, asset)
            if ok:
                print(f"  PRESENT  {asset_id}: {_rel(dest)} ({detail})")
                report.append({"asset": asset_id, "status": "present", "detail": detail})
                continue
            print(f"  STALE    {asset_id}: {detail}; re-downloading")
            dest.unlink(missing_ok=True)
        sources = _sources(asset)
        if args.dry_run:
            plan = ", ".join(f"{repo}::{repo_path}" for repo, repo_path in sources)
            print(f"  DRY-RUN  {asset_id}: {plan} -> {_rel(dest)}")
            report.append({"asset": asset_id, "status": "dry-run", "sources": plan})
            continue
        entry = {"asset": asset_id, "role": asset.get("role"), "filename": filename,
                 "destination": _rel(dest), "attempts": []}
        done = False
        for index, (repo, repo_path) in enumerate(sources):
            label = "primary" if index == 0 else f"mirror {index}"
            try:
                method, url = _fetch_from_source(repo, repo_path, dest, token)
                entry["source_used"] = f"{label}: {repo}::{repo_path}"
                entry["method"] = method
                done = True
                break
            except DownloadError as exc:
                attempts = exc.attempts or [(f"{repo}::{repo_path}", str(exc))]
                for url, why in attempts:
                    entry["attempts"].append({"source": label, "repo": repo, "repo_path": repo_path,
                                              "url": url, "error": why})
                print(f"  RETRY    {asset_id} [{label}] {repo}::{repo_path}")
                print(f"           {str(exc)[:160]}")
        if not done:
            failures += 1
            entry["status"] = "failed"
            print(f"  FAILED   {asset_id}")
            print(f"           role: {asset.get('role')}")
            print(f"           file: {filename}")
            print(f"           expected at: {_rel(dest)}")
            for attempt in entry["attempts"]:
                print(f"           - {attempt['source']}: {attempt['repo']}::{attempt['repo_path']}")
                print(f"             url: {attempt['url']}")
                print(f"             error: {attempt['error']}")
            report.append(entry)
            continue
        ok, detail = _verify(dest, asset)
        entry["status"] = "downloaded" if ok else "verification-failed"
        entry["bytes"] = dest.stat().st_size
        entry["verification"] = detail
        if ok:
            print(f"  OK       {asset_id}: {human_bytes(dest.stat().st_size)} "
                  f"via {entry['source_used']} ({detail})")
        else:
            failures += 1
            print(f"  BAD      {asset_id}: {detail}")
            print(f"           file kept at {_rel(dest)} for inspection; delete it to retry")
        report.append(entry)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"download_report_{int(time.time())}.json"
    out.write_text(json.dumps({"profile": args.profile, "assets": report}, indent=2), encoding="utf-8")
    print(f"[download] report -> {_rel(out)}")
    print(f"[download] done, {failures} failure(s)")
    return 0 if failures == 0 else 1


# Selftest subcommand

def _check(results: list, name: str, ok: bool, detail: str = "", skip: bool = False) -> None:
    tag = "SKIP" if skip else ("PASS" if ok else "FAIL")
    print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""))
    results.append({"name": name, "status": tag, "detail": detail})


def _token_checks() -> list[tuple[str, bool, str]]:
    h3_seq = 31 * (30 * 17)
    ltx_s1 = 16 * 17 * 30
    ltx_s2 = 16 * 34 * 60
    flops = 2 * 33e9 * h3_seq
    return [
        ("H3 sequence length == 15,810", h3_seq == 15810, f"31*510={h3_seq}"),
        ("LTX stage-1 sequence == 8,160", ltx_s1 == 8160, f"16*17*30={ltx_s1}"),
        ("LTX stage-2 sequence == 32,640", ltx_s2 == 32640, f"16*34*60={ltx_s2}"),
        ("H3 linear-term FLOPs ~= 1.043e15", abs(flops - 1.043e15) / 1.043e15 < 0.01,
         f"2*33e9*15810={flops:.4e}"),
    ]


def _tiling_checks() -> list[tuple[str, bool, str]]:
    n, height, overlap, tokens = 2, 128, 16, 72
    window = (height + overlap) // n
    closure = n * window - (n - 1) * overlap
    return [
        ("tiling window T=72 from closure", window == tokens, f"(128+16)/2={window}"),
        ("tiling closure 2*72-16 == 128", closure == height, f"2*72-16={closure}"),
        ("window tokens <= 6400 ceiling", tokens * tokens <= 6400, f"72*72={tokens*tokens}"),
    ]


def _premul_check() -> list[tuple[str, bool, str]]:
    color, alpha = 0.8, 0.5
    num = sum(1.0 * alpha * color for _ in range(2))
    den = sum(1.0 * alpha for _ in range(2))
    reconstructed = num / den
    # Naive linear_rgb_composite straight cross-fade (un-demultiplied premultiplied RGB):
    # sum(W_i * (alpha * color)) / sum(W_i) darkens semi-transparent edges
    naive = sum(1.0 * (alpha * color) for _ in range(2)) / sum(1.0 for _ in range(2))
    return [
        ("premultiplied reconstruct equals true color", abs(reconstructed - color) < 1e-9,
         f"{reconstructed:.3f} vs {color}"),
        ("naive alpha blend darkens (negative control)", naive < color - 0.1,
         f"naive={naive:.3f}"),
    ]


def _config_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    manifest = load_json(CONFIG_DIR / "models.manifest.json")
    profiles = load_json(CONFIG_DIR / "profiles.json")["profiles"]
    results.append(("manifest and profiles parse", True, ""))
    missing = [f"{p}:{a}" for p, spec in profiles.items()
               for a in spec["assets"] if a not in manifest["assets"]]
    results.append(("every profile asset exists in the manifest", not missing,
                    ",".join(missing) if missing else f"{sum(len(p['assets']) for p in profiles.values())} references"))
    h3_base = {"PROMPT", "SEED", "CHARACTER_IMAGE", "POWER_IMAGE", "ENVIRONMENT_IMAGE"}
    expected = {
        "h3_ulta_8gb_production.json": h3_base | {"STEPS", "FRAMES", "SEED_REFINE"},
        "h3_c3_8gb_speed.json": h3_base | {"STEPS", "FRAMES", "SEED_REFINE"},
        "h3_flagship_8gb.json": h3_base | {"STEPS", "FRAMES", "SEED_REFINE"},
        "h3_t2_243f_8gb.json": h3_base | {"STEPS", "FRAMES"},
        "ltx25_w4a8_production.json": {"PROMPT", "NEGATIVE", "FPS", "STAGE1_W", "STAGE1_H",
                                       "FRAMES", "SEED", "SEED2", "SHOT_ID",
                                       "REF_IMAGE_1", "REF_IMAGE_2", "REF_BACKGROUND"},
        "qwen_2k_tiled_refine.json": {"PROMPT", "REFINE_PROMPT", "NEGATIVE", "SEED", "REFINE_SEED"},
    }
    for name, allowed in expected.items():
        path = WORKFLOW_DIR / name
        if not path.exists():
            results.append((f"workflow {name} exists", False, "missing"))
            continue
        wf = load_json(path)
        found = harness.placeholder_names(wf)
        missing_tokens = sorted(allowed - found)
        unknown = sorted(found - allowed)
        results.append((f"workflow {name} placeholders", not missing_tokens and not unknown,
                        f"missing={missing_tokens} unknown={unknown}" if (missing_tokens or unknown) else
                        f"{len(found)} tokens"))
    # Proven per-workflow defaults must reference an existing workflow and names
    # that actually appear as placeholders in it, or bench would silently drop them.
    for proven_name in ("h3.proven.json", "ltx25.proven.json", "qwen21.proven.json"):
        proven = load_json(CONFIG_DIR / proven_name)
        for workflow_rel, defaults in (proven.get("bench_defaults") or {}).items():
            label = f"{proven_name} bench_defaults for {Path(workflow_rel).name}"
            path = REPO_ROOT / workflow_rel
            if not path.exists():
                results.append((label, False, f"workflow missing: {workflow_rel}"))
                continue
            tokens = harness.placeholder_names(load_json(path))
            unknown = sorted(set(defaults) - tokens)
            results.append((label, not unknown,
                            f"keys that are not placeholders: {unknown}" if unknown else
                            f"{len(defaults)} defaults"))
    return results


def _path_hygiene() -> list[tuple[str, bool, str]]:
    skip_dirs = {".venv", "venv", ".git", "models", "runs", "__pycache__", ".idea", ".vscode"}
    hits: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in (".py", ".md", ".json", ".toml", ".txt", ".ps1", ""):
                continue
            if filename == ".gitignore" or path.suffix == "":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            scanned += 1
            for match in DRIVE_PATH_RE.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{_rel(path)}:{line}")
    return [("no absolute drive paths in tracked text files", not hits,
             f"{scanned} files scanned; hits: {hits[:8]}" if hits else f"{scanned} files scanned")]
def cmd_selftest(args) -> int:
    results: list[dict] = []
    print("[selftest] offline checks")
    checks = _tiling_checks() + _premul_check() + _token_checks() + _config_checks()
    if args.lint:
        checks += _path_hygiene()
    for name, ok, detail in checks:
        _check(results, name, ok, detail)

    print("[selftest] W4A8 gate")
    if args.skip_gpu:
        _check(results, "W4A8 parity gate", True, "skipped by flag", skip=True)
    else:
        gate_modules = True
        try:
            import torch  # noqa: F401
            import comfy_kitchen  # noqa: F401
        except ImportError:
            gate_modules = False
        if not gate_modules:
            _check(results, "W4A8 parity gate", True,
                   "SKIP: torch/comfy-kitchen not installed in this interpreter", skip=True)
        else:
            gate_out = RUNS_DIR / "selftest_gate_w4a8.json"
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [sys.executable, str(BENCH_DIR / "gate_w4a8.py"), "--out", str(gate_out)],
                capture_output=True, text=True)
            ok = proc.returncode == 0
            _check(results, "W4A8 parity gate", ok,
                   f"exit={proc.returncode}, record -> {_rel(gate_out)}")

    print("[selftest] micro-render")
    if not args.comfy:
        _check(results, "deterministic micro-render", True,
               "SKIP: pass --comfy URL and --checkpoint NAME to run it", skip=True)
    elif not args.checkpoint:
        _check(results, "deterministic micro-render", False,
               "--comfy given without --checkpoint")
    else:
        ok, detail = _micro_render(args.comfy, args.checkpoint)
        _check(results, "deterministic micro-render", ok, detail)

    failed = [r for r in results if r["status"] == "FAIL"]
    skipped = [r for r in results if r["status"] == "SKIP"]
    print(f"[selftest] {len(results) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    record = RUNS_DIR / f"selftest_{int(time.time())}.json"
    record.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    print(f"[selftest] record -> {_rel(record)}")
    return 0 if not failed else 2


def _micro_render(comfy_url: str, checkpoint: str) -> tuple[bool, str]:
    """One 64x64, one-step render through the full submit->history->artifact path."""
    client = harness.ComfyClient(comfy_url)
    if not client.reachable():
        return False, f"ComfyUI not reachable at {comfy_url}"
    workflow = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 64, "height": 64, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "seed": 12345, "steps": 1, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["2", 0], "denoise": 1.0}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "videolab selftest", "clip": ["1", 1]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": "videolab_selftest"}},
    }
    record = harness.run_workflow(client, workflow, timeout_s=600, poll_s=1.0)
    status = (record.get("status") or {}).get("status_str")
    if status != "success":
        return False, f"status={status}"
    images = [img for node in (record.get("outputs") or {}).values()
              for img in node.get("images", [])]
    if not images:
        return False, "no image in history outputs"
    try:
        from PIL import Image
        import io
        payload = client.fetch_image(images[0]["filename"], images[0].get("subfolder", ""),
                                     images[0].get("type", "output"))
        img = Image.open(io.BytesIO(payload))
        lo, hi = img.convert("L").getextrema()
        if img.size != (64, 64):
            return False, f"unexpected size {img.size}"
        if hi - lo < 2:
            return False, f"degenerate output (extrema {lo}..{hi})"
        return True, f"64x64 artifact, extrema {lo}..{hi}, wall {record['wall_clock_s']}s"
    except ImportError:
        return True, f"artifact present (Pillow unavailable for pixel validation), wall {record['wall_clock_s']}s"


# Bench subcommand

def _parse_set(pairs: list[str] | None) -> dict:
    params: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--set expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        params[key.strip()] = value
    return params


def _referenced_model_files(workflow: dict) -> list[str]:
    """Model-ish filenames a workflow loads, for a local preflight warning."""
    keys = ("ckpt_name", "unet_name", "clip_name", "vae_name", "lora_name", "model_name")
    found: set[str] = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for key, value in node.get("inputs", {}).items():
            if key in keys and isinstance(value, str) and not value.startswith("{"):
                found.add(value)
    return sorted(found)


def _model_present(name: str) -> bool:
    """Best-effort local presence check across ComfyUI's standard model folders."""
    if (MODELS_DIR / name).exists():
        return True
    for sub in ("diffusion_models", "text_encoders", "vae", "loras", "checkpoints",
                "latent_upscale_models", "upscale_models", "unet", "clip", "prompt_generator"):
        if (MODELS_DIR / sub / Path(name).name).exists():
            return True
    return False


def cmd_bench(args) -> int:
    try:
        proven = load_json(CONFIG_DIR / f"{args.model}.proven.json")
    except Exception as exc:  # noqa: BLE001
        print(f"[bench] cannot read configs/{args.model}.proven.json: {type(exc).__name__}: {exc}")
        return 2
    workflow_rel = args.workflow or proven["workflow"]
    workflow_path = Path(workflow_rel) if Path(workflow_rel).is_absolute() else REPO_ROOT / workflow_rel
    if not workflow_path.exists():
        print(f"[bench] workflow not found: {workflow_path}")
        return 2
    try:
        workflow = load_json(workflow_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[bench] workflow is not valid JSON: {workflow_path}: {type(exc).__name__}: {exc}")
        return 2
    placeholders = harness.placeholder_names(workflow)

    model_defaults = dict(MODEL_DEFAULTS.get(args.model, {}))
    # Proven configs may pin per-workflow defaults (for example the H3 step count
    # and frame length); they take precedence over the model-wide defaults.
    workflow_defaults = (proven.get("bench_defaults") or {}).get(workflow_rel, {})
    model_defaults.update(workflow_defaults)
    params = {}
    applied_defaults = []
    for k, v in model_defaults.items():
        if k in placeholders:
            params[k] = str(v)
            applied_defaults.append(f"{k}={v}")

    if args.params_file:
        try:
            params.update(load_json(Path(args.params_file)))
        except Exception as exc:  # noqa: BLE001
            print(f"[bench] cannot read params file {args.params_file}: {type(exc).__name__}: {exc}")
            return 2
    user_set = _parse_set(args.set)
    params.update(user_set)
    for k in user_set:
        applied_defaults = [d for d in applied_defaults if not d.startswith(f"{k}=")]

    missing = sorted(placeholders - set(params))
    extra = sorted(set(params) - placeholders)
    if extra and not args.allow_extra_params:
        print(f"[bench] parameters that match no placeholder: {extra}")
        print("        (pass --allow-extra-params to ignore)")
        return 2
    if missing:
        print(f"[bench] missing placeholder values: {missing}")
        print("        supply them with --set KEY=VALUE or --params-file params.json")
        print("        example: --set PROMPT=\"...\" --set SEED=12345")
        if set(missing) & NONEMPTY_PARAMS:
            print("        H3 image-to-video requires valid image paths, e.g.:")
            print("        --set CHARACTER_IMAGE=path/to/img.png "
                  "--set POWER_IMAGE=path/to/ref.png --set ENVIRONMENT_IMAGE=path/to/bg.png")
        return 2
    empty_required = sorted(k for k in NONEMPTY_PARAMS if k in placeholders
                            and k in params and not str(params[k]).strip())
    if empty_required:
        print(f"[bench] H3 image parameters passed empty: {empty_required}")
        print("        H3 image-to-video requires valid image paths, e.g.:")
        print("        --set CHARACTER_IMAGE=path/to/img.png "
              "--set POWER_IMAGE=path/to/ref.png --set ENVIRONMENT_IMAGE=path/to/bg.png")
        return 2
    if applied_defaults:
        print(f"[bench] applied proven defaults: {', '.join(applied_defaults)}")

    referenced = _referenced_model_files(workflow)
    if referenced:
        absent = [name for name in referenced if not _model_present(name)]
        print(f"[bench] workflow loads {len(referenced)} model file(s); "
              f"{len(referenced) - len(absent)} found under {_rel(MODELS_DIR)}")
        if absent:
            print("[bench] WARNING: not found locally. If ComfyUI uses a different models root this "
                  "is expected; server-side presence is checked by ComfyUI itself when the job runs:")
            for name in absent:
                print(f"  - {name}")

    client = harness.ComfyClient(args.comfy)
    if not client.reachable():
        print(f"[bench] ComfyUI not reachable at {args.comfy}")
        return 3

    filled = harness.apply_params(workflow, params)
    remaining = harness.placeholder_names(filled)
    if remaining:
        print(f"[bench] unfilled placeholders after substitution: {sorted(remaining)}")
        return 2

    print(f"[bench] model={args.model} workflow={_rel(workflow_path)} comfy={args.comfy}")
    print(f"[bench] seed={params.get('SEED', '?')} (N=1 run per invocation; "
          f"repeat the command for repeat measurements)")

    def on_tick(elapsed: float, sample: dict) -> None:
        if int(elapsed) % 15 < 1:
            vram = sample.get("vram_used")
            vram_gib = f"{vram / 2**30:.2f}GiB" if vram else "?"
            print(f"  [{elapsed:7.1f}s] vram={vram_gib} gpu={sample.get('gpu_util', '?')}% "
                  f"pcie_tx={sample.get('pcie_tx_kbs', 0)/1024:.0f}MB/s", flush=True)

    try:
        record = harness.run_workflow(client, filled, timeout_s=args.timeout, on_tick=on_tick)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        print(f"[bench] ComfyUI rejected the submission: HTTP {exc.code} {body}")
        return 4
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"[bench] run failed before completion: {type(exc).__name__}: {exc}")
        return 4
    record["model"] = args.model
    record["workflow"] = _rel(workflow_path)
    record["params"] = params
    record["note"] = ("single run; to claim a difference, repeat this command with "
                      "different seeds and compare distributions, not single numbers")
    status = (record.get("status") or {}).get("status_str", "unknown")
    tag = f"{args.model}_{args.tag}" if args.tag else args.model
    path = harness.write_record(str(RUNS_DIR), tag, record)
    monitor = record.get("monitor", {})
    print(f"[bench] status={status} wall={record['wall_clock_s']}s "
          f"peak_vram={monitor.get('peak_vram_gib', '?')}GiB "
          f"pcie_rx={monitor.get('pcie_rx_total_gib', '?')}GiB")
    print(f"[bench] record -> {_rel(Path(path))}")
    if status != "success":
        print("[bench] non-success status; inspect the record for the ComfyUI error messages")
    return 0 if status == "success" else 4


# CLI entry point

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="videolab",
        description="Reproducible benchmarks for large video diffusion transformers on 8 GB GPUs.",
        epilog="Examples:\n"
               "  python videolab.py doctor\n"
               "  python videolab.py download --profile smoke\n"
               "  python videolab.py selftest --comfy http://127.0.0.1:8188 --checkpoint my_model.safetensors\n"
               "  python videolab.py bench --model ltx25 --set PROMPT=\"a weathered wooden pier at blue hour, low camera push-in\" --set REF_IMAGE_1=character.png --set REF_IMAGE_2=prop.png --set REF_BACKGROUND=environment.png --set SHOT_ID=demo\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="GPU/PyTorch/NVML check + W4A8 parity gate")
    p_doc.add_argument("--no-gate", action="store_true", help="skip the W4A8 gate")
    p_doc.set_defaults(func=cmd_doctor)

    p_dl = sub.add_parser("download", help="fetch and verify model assets for a profile")
    p_dl.add_argument("--profile", required=True,
                      choices=["smoke", "standard", "full", "h3-byow"])
    p_dl.add_argument("--dry-run", action="store_true", help="print the plan without downloading")
    p_dl.add_argument("--acknowledge-license", action="store_true",
                      help="required for h3-byow; acknowledges the MiniMax H3 license terms")
    p_dl.add_argument("--verify-hashes", action="store_true",
                      help="hash local files (slow) and report digests")
    p_dl.add_argument("--only", action="append", metavar="ASSET_ID",
                      help="limit to specific asset id(s); repeatable")
    p_dl.add_argument("--check-sources", action="store_true",
                      help="verify that every recorded source is reachable and matches its "
                           "digest pin; writes a report and downloads nothing")
    p_dl.set_defaults(func=cmd_download)

    p_st = sub.add_parser("selftest", help="offline unit checks + optional gate and micro-render")
    p_st.add_argument("--skip-gpu", action="store_true", help="skip GPU-dependent checks")
    p_st.add_argument("--lint", action="store_true",
                      help="also run the developer-only path-hygiene scan "
                           "(flags absolute drive paths in tracked text files)")
    p_st.add_argument("--comfy", default=None, help="ComfyUI base URL for the micro-render")
    p_st.add_argument("--checkpoint", default=None,
                      help="checkpoint name as ComfyUI sees it (required for the micro-render)")
    p_st.set_defaults(func=cmd_selftest)

    p_b = sub.add_parser("bench", help="run a proven workflow with NVML telemetry")
    p_b.add_argument("--model", required=True, choices=SUPPORTED_MODELS)
    p_b.add_argument("--workflow", default=None, help="override the workflow template path")
    p_b.add_argument("--set", action="append", metavar="KEY=VALUE",
                     help="placeholder value; repeatable")
    p_b.add_argument("--params-file", default=None, help="JSON file of placeholder values")
    p_b.add_argument("--comfy", default=DEFAULT_COMFY, help="ComfyUI base URL")
    p_b.add_argument("--timeout", type=float, default=7200.0, help="run timeout in seconds")
    p_b.add_argument("--tag", default=None, help="suffix for the run record filename")
    p_b.add_argument("--allow-extra-params", action="store_true",
                     help="do not fail when a --set key matches no placeholder")
    p_b.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
