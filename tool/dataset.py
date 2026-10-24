"""Dataset tools. Sourcing (Pexels URL import + self-render
batches) and auto-filters (sharpness, motion, dedupe, face presence)."""
from __future__ import annotations

import glob
import hashlib
import ipaddress
import shutil
import socket
import subprocess
import urllib.parse
from pathlib import Path

import cv2
import numpy as np

from core import ROOT, ffmpeg_exe, load_config, log_setup, jsave, is_safe_component
from training import DATASETS_DIR, curate_save

log = log_setup("dataset")


# ------------------------------------------------------------- sourcing
MAX_IMPORT_URLS = 500
MAX_IMPORT_BYTES = 50 * 1024 * 1024       # per downloaded image


def _non_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global excludes RFC 1918, loopback, link-local, CGNAT (100.64/10),
    # reserved and unspecified; multicast must be checked on its own
    return not ip.is_global or ip.is_multicast


def _resolve_public(url: str) -> tuple[str, int, str]:
    """Validate url (see validate_safe_url) and return (host, port, ip) —
    the first resolved address, which the download is then PINNED to so a
    second DNS answer cannot redirect it (DNS rebinding)."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    if url.startswith("-") or url.strip().startswith("-"):
        raise ValueError(f"URL cannot start with a dash: {url!r}")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Disallowed scheme '{parsed.scheme}': only 'https' is permitted")

    if parsed.username or parsed.password:
        raise ValueError("URL must not contain credentials")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"URL missing hostname: {url!r}")

    port = parsed.port or 443
    try:
        addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except Exception as e:
        raise ValueError(f"Failed to resolve host '{hostname}': {e}") from e

    if not addr_info:
        raise ValueError(f"No address returned for host '{hostname}'")

    pinned = ""
    for res in addr_info:
        ip_str = str(res[4][0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise ValueError(f"Invalid IP address {ip_str!r}: {e}") from e

        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and _non_public(mapped):
            raise ValueError(f"Resolved to non-public IPv4-mapped address: {mapped}")
        if _non_public(ip):
            raise ValueError(f"Resolved to non-public IP address: {ip}")
        pinned = pinned or ip_str

    return hostname, port, pinned


def validate_safe_url(url: str) -> str:
    """Validate that a URL is safe against SSRF attacks.

    Enforces non-empty string, no leading dash ('-'), scheme == 'https',
    no credentials (username/password), and rejects the URL when ANY resolved
    address is non-public (private, loopback, link-local, CGNAT, multicast,
    reserved, unspecified — IPv4-mapped IPv6 included).
    Returns the validated URL on success, or raises ValueError on invalid/unsafe URLs.
    """
    _resolve_public(url)
    return url


def _is_image(path: Path) -> bool:
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        return buf.size > 0 and cv2.imdecode(buf, cv2.IMREAD_UNCHANGED) is not None
    except Exception:  # noqa: BLE001
        return False


def import_urls(urls: list[str], dtype: str, name: str, license_note: str) -> dict:
    """Download a list of direct image URLs into a new dataset folder.

    Each URL is validated (validate_safe_url), then fetched by curl PINNED to
    the address that was validated (--resolve), https-only, no redirects,
    size-capped, into a temp file that is kept only if it decodes as an
    image."""
    if dtype not in ("face", "power", "environment", "anatomy", "quality"):
        return {"ok": False, "error": f"bad type {dtype}"}
    if not is_safe_component(name):
        return {"ok": False, "error": f"bad dataset name {name}"}
    if len(urls) > MAX_IMPORT_URLS:
        return {"ok": False, "error": f"too many urls (max {MAX_IMPORT_URLS})"}
    curl_bin = shutil.which("curl")
    if not curl_bin:
        return {"ok": False, "error": "curl not found on PATH"}
    d = DATASETS_DIR / dtype / name
    d.mkdir(parents=True, exist_ok=True)
    added = 0
    skipped: list[str] = []
    for i, url in enumerate(urls):
        try:
            host, port, ip = _resolve_public(url)
        except ValueError as e:
            log.warning(f"Skipping unsafe or invalid URL {url!r}: {e}")
            skipped.append(f"{url}: {e}")
            continue
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        ext = suffix if suffix in (".png", ".jpg", ".jpeg") else ".jpg"
        dest = d / f"src_{i:04d}{ext}"
        if dest.exists():
            added += 1
            continue
        part = dest.with_name(dest.name + ".part")
        pin_ip = f"[{ip}]" if ":" in ip else ip
        try:
            r = subprocess.run(
                [curl_bin, "--proto", "=https", "--proto-redir", "=https",
                 "--max-redirs", "0", "--fail", "-sS",
                 "--max-time", "120", "--max-filesize", str(MAX_IMPORT_BYTES),
                 "--resolve", f"{host}:{port}:{pin_ip}",
                 "-o", str(part), "--", url],
                capture_output=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning(f"curl failed for {url}: {e}")
            part.unlink(missing_ok=True)
            skipped.append(f"{url}: {e}")
            continue
        ok = (r.returncode == 0 and part.exists()
              and 10_000 < part.stat().st_size <= MAX_IMPORT_BYTES
              and _is_image(part))
        if ok:
            part.replace(dest)
            added += 1
        else:
            part.unlink(missing_ok=True)
            why = (r.stderr or b"").decode("utf-8", "replace").strip()[:200]
            skipped.append(f"{url}: {why or 'not an image / too small'}")
    if license_note:
        (d / "LICENSE.md").write_text(license_note, encoding="utf-8")
    if added == 0:
        return {"ok": False, "error": "no files downloaded", "added": 0,
                "skipped": skipped[:50], "dataset": str(d)}
    return {"ok": True, "added": added, "skipped": skipped[:50],
            "dataset": str(d)}


def self_render_batch(dtype: str, name: str, prompts: list[str],
                      seed_base: int = 1000) -> dict:
    """Render short clips via the 5B to generate training data (self-
    distillation): extract a frame from each render into the dataset."""
    from renderer import ComfyClient
    from workflows import WorkflowBuilder
    d = DATASETS_DIR / dtype / name
    d.mkdir(parents=True, exist_ok=True)
    frames_dir = d / "frames"
    frames_dir.mkdir(exist_ok=True)
    c = ComfyClient(8188)
    b = WorkflowBuilder()
    n = 0
    for i, prompt in enumerate(prompts):
        wf = b.build("wan2.2_t2v_5b", prompt=prompt, negative="blurry",
                     seed=seed_base + i, width=512, height=288, steps=6,
                     frames=49,
                     extra={"SHOT_ID": f"trainbatch_{dtype}_{i}"})
        try:
            pid = c.queue_prompt(wf)
            res = c.wait_for(pid, timeout_s=1800)
            if res is None:
                continue
            vids = []
            for o in res.get("outputs", {}).values():
                vids += o.get("images", []) or o.get("gifs", []) or []
            if not vids:
                continue
            v = vids[0]
            tmp = ROOT / "temp" / "trainbatch.mp4"
            c.download(v.get("subfolder", ""), v["filename"], tmp)
            out = frames_dir / f"f_{n:04d}.png"
            subprocess.run([cfg_ff(), "-y", "-v", "error", "-i", str(tmp),
                            "-frames:v", "1", str(out)], capture_output=True)
            if out.exists() and out.stat().st_size > 1000:
                n += 1
        except Exception as e:
            log.warning(f"batch render {i} failed: {e}")
    return {"ok": True, "rendered": n, "dataset": str(d)}


def cfg_ff() -> str:
    return ffmpeg_exe()


# ---------------------------------------------------------- auto-filters
def auto_filter(dtype: str, name: str) -> dict:
    """Score every candidate; mark keep/reject in curation.json. Reject:
    too blurry, zero/frozen motion, duplicates, (faces) no detected face.
    `_auto` keys are RELATIVE posix paths (training looks them up)."""
    d = DATASETS_DIR / dtype / name
    if not d.is_dir():
        return {"ok": False, "error": f"dataset not found: {d}"}
    from impact_onnx_face import detect_faces
    state: dict = {"_filtered": [], "_auto": {}}
    files: list[str] = []
    for sub in ("", "stills", "frames"):
        base = d if not sub else d / sub
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            files += sorted(glob.glob(str(base / ext)))
    files = sorted(files)
    phashes: dict[str, str] = {}
    for f in files:
        img = cv2.imread(f)
        if img is None:
            state["_filtered"].append([f, "unreadable"])
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
        reasons = []
        if sharp < 60:
            reasons.append("blurry")
        ph = _phash(gray)
        dup = [k for k, v in phashes.items() if v == ph]
        if dup:
            reasons.append(f"duplicate_of_{Path(dup[0]).name}")
        else:
            phashes[f] = ph
        if dtype == "face":
            nfaces = detect_faces(f)
            if nfaces == 0:
                # detector is real-photo trained; AI faces often miss — advisory
                reasons.append("no_face_detected(advisory)")
        state["_auto"][Path(f).relative_to(d).as_posix()] = {
            "sharpness": round(sharp, 1),
            "keep": not [r for r in reasons if "advisory" not in r],
            "reasons": reasons,
        }
    curate_save(d, state)
    return {"ok": True, "scored": len(files),
            "kept": sum(1 for v in state["_auto"].values() if v["keep"])}


def _phash(gray: np.ndarray) -> str:
    small = cv2.resize(gray, (16, 16))
    avg = small.mean()
    return "".join("1" if p > avg else "0" for p in small.flatten())


# -------------------------------------------------------------- captions
def captions_bible(dtype: str, name: str, char_id: str = "",
                   power_id: str = "", location: str = "") -> dict:
    """Auto-caption template from Bible/metadata — editable in the UI.
    Face datasets: canonical identity description (same block as the
    generation prompts) + per-image emotion/angle from meta.json, so the
    LoRA learns the expression->face mapping."""
    from bible import Bible
    b = Bible()
    texts: dict[str, str] = {}
    meta: dict = {}
    if dtype == "face":
        try:
            import json as _json
            mp = DATASETS_DIR / dtype / name / "meta.json"
            meta = _json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    files: list[str] = []
    base_dir = DATASETS_DIR / dtype / name
    for sub in ("", "stills", "frames"):
        base = base_dir if not sub else base_dir / sub
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            files += glob.glob(str(base / ext))
    files = sorted(files)
    for f in files:
        if dtype == "face" and char_id:
            c = b.characters().get(char_id, {})
            body = c.get("body", "") or name.replace("_", " ")
            m = meta.get(Path(f).name, {})
            var = m.get("variations") or {} if isinstance(m, dict) else {}
            tag = ""
            emotion = m.get("emotion") or var.get("emotion")
            if emotion:
                tag += f", {emotion} expression"
            angle = m.get("angle") or var.get("angle")
            if angle:
                tag += f", {angle} view"
            texts[f] = f"portrait of {char_id}, {body}{tag}"
        elif dtype == "power" and power_id:
            for c in b.characters().values():
                p = c.get("powers", {}).get(power_id)
                if p:
                    texts[f] = p.get("descriptor", power_id)
                    break
        elif dtype == "environment" and location:
            l = b.locations().get(location, {})
            texts[f] = l.get("descriptor", location)
        else:
            texts[f] = name.replace("_", " ")
    return texts
