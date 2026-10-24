"""Support helpers: RenderMode (GPU health and freeing VRAM) and versions
(the historical pins in tool/versions.txt)."""
from __future__ import annotations

import subprocess

from core import ROOT, load_config, log_setup

log = log_setup("support")


class RenderMode:
    """Frees the display card's VRAM before render batches."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()

    def free_vram(self) -> dict:
        rc = self.cfg.get("render_mode", {})
        killed = []
        if not rc.get("enabled"):
            return {"killed": []}
        for app in rc.get("kill_gpu_apps", []):
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"$p = Get-Process {app} -ErrorAction SilentlyContinue; "
                 f"if ($p) {{ $p | Stop-Process -Force "
                 f"-ErrorAction SilentlyContinue; 'stopped' }}"],
                capture_output=True, text=True)
            if (out.stdout or "").strip():
                killed.append(app)
        log.info(f"render mode: apps stopped {killed}")
        return {"killed": killed}

    def health(self) -> list[dict]:
        # no GPU or no nvidia-smi on PATH: report no GPUs instead of failing
        # /api/status
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,"
                                  "memory.used,memory.total,temperature.gpu,"
                                  "utilization.gpu", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        rows = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 6:
                rows.append({"index": parts[0], "name": parts[1],
                             "mem_used": parts[2], "mem_total": parts[3],
                             "temp": parts[4], "util": parts[5]})
        return rows


def versions() -> dict:
    """Read the historical version pins in tool/versions.txt."""
    p = ROOT / "tool" / "versions.txt"
    if not p.exists():
        return {}
    out = {}
    text = p.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
