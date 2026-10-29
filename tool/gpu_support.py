"""What this PC's NVIDIA card and driver can run.

The ComfyUI engines run on two PyTorch builds:

  cu126  ComfyUI\\, the render farm on port 8188: the SDXL image models,
         Z-Image, post-processing and the legacy Wan 2.2. Kernels for compute
         capability 5.0 to 9.0 (GTX 900 to RTX 40 series; none for the RTX 50
         series), NVIDIA driver R560 or newer.
  cu130  ComfyUI_LTX\\ (LTX-2.5 on 8189, MiniMax H3 on 8199). Kernels for
         compute capability 7.5 and newer (GTX 16 and RTX 20 series on: none for GTX 10
         and older cards), driver R580 or newer.

ComfyUI_Qwen21\\ (Qwen-Image 2.1 on 8190) runs on whichever of the two its
venv was built with: the installer builds it on cu130, or on cu126 for a card
or driver that runs only that one (qwen_pick, install/components.json).

nvidia-smi names the card and the driver; the studio renders on the first
card (CUDA_DEVICE_ORDER=PCI_BUS_ID, --default-device 0), the first line it
prints. Where it cannot answer (no NVIDIA card, a driver without NVML, CI)
nothing is known and nothing is refused. KINOLOOM_GPU_CHECK=0 turns the
check off. Standard library only: the launcher imports it before any venv.

  python tool\\gpu_support.py [--log FILE]   prints the card and what it runs
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIMES = {
    "cu126": {"cuda": "12.6", "cc": (5, 0), "cc_max": 9, "driver": 560,
              "cards": "a GTX 900- to RTX 40-series card",
              "runs": "the render farm (the SDXL image models, Z-Image, post-processing)"},
    "cu130": {"cuda": "13.0", "cc": (7, 5), "cc_max": None, "driver": 580,
              "cards": "a GTX 16- or RTX 20-series card or newer",
              "runs": "LTX-2.5 and MiniMax H3"},
}
# the build each ComfyUI port runs on (launcher.py); Qwen-Image 2.1's is the
# one its venv holds (qwen_runtime)
PORT_RUNTIME = {8188: "cu126", 8189: "cu130", 8199: "cu130"}
QWEN_PORT = 8190
QWEN_TORCH = ROOT / "ComfyUI_Qwen21" / "venv" / "Lib" / "site-packages" / "torch" / "version.py"


def _smi(field: str) -> str | None:
    # the one on the PATH, as INSTALL-MAIN.bat finds it (a bare name would find
    # System32's first, whatever the PATH says)
    exe = shutil.which("nvidia-smi") or "nvidia-smi"
    try:
        r = subprocess.run([exe, f"--query-gpu={field}", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    line = (r.stdout or "").strip().splitlines()[0].strip() if r.returncode == 0 and r.stdout.strip() else ""
    return line or None


def _version(text: str, parts: int) -> tuple[int, ...] | None:
    try:
        nums = tuple(int(p) for p in text.strip().split(".")[:parts])
    except ValueError:
        return None
    return nums if len(nums) == parts else None


@functools.lru_cache(maxsize=1)
def card() -> dict | None:
    """{"name", "driver", "cc"} of the card the studio renders on: the name,
    the driver as text ("616.64") and the compute capability as a pair, or
    None for what nvidia-smi did not say (compute_cap needs driver R510 or
    newer). None when there is no answer at all."""
    if os.environ.get("KINOLOOM_GPU_CHECK", "1") == "0":
        return None
    line = _smi("name,driver_version")
    if not line or "," not in line:
        return None
    name, driver = (p.strip() for p in line.rsplit(",", 1))
    cc = _version(_smi("compute_cap") or "", 2)
    if not name or _version(driver, 1) is None:
        return None
    return {"name": name, "driver": driver, "cc": cc}


def problem(runtime: str, runs: str = "") -> str:
    """Why this PC cannot run `runtime` ("cu126" / "cu130"), or "" when it
    can or nothing is known. `runs`: what to call the thing that runs on it
    (the runtime's own list when empty)."""
    need, c = RUNTIMES[runtime], card()
    if c is None:
        return ""
    runs = runs or need["runs"]
    built = f"built on CUDA {need['cuda']} for {need['cards']}"
    if c["cc"] is not None and (c["cc"] < need["cc"]
                                or (need["cc_max"] is not None and c["cc"][0] > need["cc_max"])):
        return f"the {c['name']} (compute capability {c['cc'][0]}.{c['cc'][1]}) cannot run {runs}, {built}"
    major = (_version(c["driver"], 1) or (0,))[0]
    if major < need["driver"]:
        return (f"NVIDIA driver {c['driver']} is too old for {runs}, built on CUDA {need['cuda']}: "
                f"it needs R{need['driver']} or newer (update it from nvidia.com/drivers)")
    return ""


def pick(main: str, fallback: str) -> str:
    """The build to install: `main`, or `fallback` when this PC runs only
    that one."""
    return fallback if problem(main) and not problem(fallback) else main


def qwen_pick() -> str:
    """The build the installer gives Qwen-Image 2.1 on this PC."""
    return pick("cu130", "cu126")


def qwen_runtime() -> str:
    """The build Qwen-Image 2.1's venv holds (its torch's CUDA version), or
    the one the installer would give it when there is none yet."""
    try:
        m = re.search(r"^cuda\b[^=\n]*=\s*['\"](\d+)\.(\d+)", QWEN_TORCH.read_text(encoding="utf-8"), re.M)
    except OSError:
        m = None
    rt = f"cu{m.group(1)}{m.group(2)}" if m else ""
    return rt if rt in RUNTIMES else qwen_pick()


def port_problem(port: int | None) -> str:
    """problem() for the ComfyUI instance on `port` (8188 when None)."""
    port = int(port or 8188)
    if port != QWEN_PORT:
        return problem(PORT_RUNTIME.get(port, "cu130"))
    rt = qwen_runtime()
    why = problem(rt, "Qwen-Image 2.1")
    if why and rt != qwen_pick():
        why += "; run INSTALL.bat again to build the one this card runs"
    return why


def report() -> list[str]:
    """The card and, for each build it cannot run, why."""
    c = card()
    if c is None:
        return ["  Card: nvidia-smi did not answer, so the card and driver were not checked."]
    cc = f", compute capability {c['cc'][0]}.{c['cc'][1]}" if c["cc"] else ""
    out = [f"  Card: {c['name']}{cc}, driver {c['driver']}."]
    for rt in RUNTIMES:
        why = problem(rt)
        out.append(f"  [WARN] {why[0].upper()}{why[1:]}." if why else f"  OK: it runs {RUNTIMES[rt]['runs']}.")
    why = port_problem(QWEN_PORT)
    out.append(f"  [WARN] {why[0].upper()}{why[1:]}." if why else
               f"  OK: it runs Qwen-Image 2.1 (on CUDA {RUNTIMES[qwen_runtime()]['cuda']}).")
    return out


if __name__ == "__main__":
    lines = report()
    print("\n".join(lines))
    if "--log" in sys.argv[1:-1]:
        try:
            with open(sys.argv[sys.argv.index("--log") + 1], "a", encoding="utf-8") as fh:
                fh.write("[driver] " + "\n".join(lines).strip() + "\n")
        except OSError:
            pass
