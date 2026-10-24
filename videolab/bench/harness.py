"""videolab telemetry harness and ComfyUI run orchestration.

The NVML monitor is a direct port of the laboratory in-process NVML asynchronous
monitor routines used for every archived run: 0.5 s sampling of device VRAM,
utilization, power, temperature, PCIe TX/RX throughput and link generation,
plus host RAM and swap when psutil is available. Field names and
aggregations intentionally match the lab records so old and new records can be
compared without translation.

NVML is accessed directly through ctypes against the driver library
(nvml.dll on Windows, libnvidia-ml.so.1 on Linux). The deprecated pynvml
package is not used; on the lab machine nvidia-smi itself failed while
in-process NVML worked, which is why this path exists.

Paths: nothing in this module is hardcoded. Callers pass URLs, directories, and
workflow dictionaries.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # optional; RAM/swap fields degrade to None without it
    import psutil
except ImportError:  # pragma: no cover - environment dependent
    psutil = None

_NVML_LIB_NAMES = ("nvml.dll", "libnvidia-ml.so.1", "libnvidia-ml.so")


class _nvmlMemory_t(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class _nvmlUtilization_t(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _load_nvml():
    for name in _NVML_LIB_NAMES:
        try:
            lib = ctypes.WinDLL(name) if os.name == "nt" else ctypes.CDLL(name)
        except OSError:
            continue
        try:
            lib.nvmlInit_v2()
        except Exception:  # noqa: BLE001 - driver mismatch or no device
            continue
        return lib
    return None


def nvml_probe() -> dict:
    """Return a one-shot summary for `videolab.py doctor`. Never raises."""
    out = {"nvml_available": False}
    lib = _load_nvml()
    if lib is None:
        return out
    try:
        out["nvml_available"] = True
        handle = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            return out
        name = ctypes.create_string_buffer(96)
        if lib.nvmlDeviceGetName(handle, name, len(name)) == 0:
            out["gpu_name"] = name.value.decode(errors="replace")
        mem = _nvmlMemory_t()
        if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
            out["vram_total_gib"] = round(mem.total / 2**30, 2)
            out["vram_free_gib"] = round(mem.free / 2**30, 2)
        ver = ctypes.create_string_buffer(80)
        if lib.nvmlSystemGetDriverVersion(ver, len(ver)) == 0:
            out["driver_version"] = ver.value.decode(errors="replace")
        power = ctypes.c_uint()
        if lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(power)) == 0:
            out["power_w"] = round(power.value / 1000.0, 1)
        temp = ctypes.c_uint()
        if lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temp)) == 0:
            out["temp_c"] = temp.value
        gen = ctypes.c_uint()
        width = ctypes.c_uint()
        lib.nvmlDeviceGetCurrPcieLinkGeneration(handle, ctypes.byref(gen))
        lib.nvmlDeviceGetCurrPcieLinkWidth(handle, ctypes.byref(width))
        out["pcie"] = f"Gen{gen.value} x{width.value}"
    except Exception as exc:  # noqa: BLE001
        out["nvml_error"] = f"{type(exc).__name__}: {exc}"
    return out


class NvmlMonitor:
    """Background sampler with the same aggregation fields as the lab harness."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.samples: list[dict] = []
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.nvml = _load_nvml()
        self.handle = ctypes.c_void_p()
        if self.nvml is not None:
            try:
                self.nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self.handle))
            except Exception:  # noqa: BLE001
                self.nvml = None

    def available(self) -> bool:
        return self.nvml is not None

    def _sample(self) -> dict:
        s: dict = {"t": time.time()}
        if self.nvml is not None:
            try:
                m = _nvmlMemory_t()
                if self.nvml.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(m)) == 0:
                    s["vram_used"] = m.used
                    s["vram_free"] = m.free
                else:
                    s["vram_used"] = None
                    s["vram_free"] = None
            except Exception:
                s["vram_used"] = None
                s["vram_free"] = None

            try:
                u = _nvmlUtilization_t()
                if self.nvml.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(u)) == 0:
                    s["gpu_util"] = u.gpu
                    s["mem_util"] = u.memory
                else:
                    s["gpu_util"] = None
                    s["mem_util"] = None
            except Exception:
                s["gpu_util"] = None
                s["mem_util"] = None

            try:
                p = ctypes.c_uint()
                if self.nvml.nvmlDeviceGetPowerUsage(self.handle, ctypes.byref(p)) == 0:
                    s["power_w"] = p.value / 1000.0
                else:
                    s["power_w"] = None
            except Exception:
                s["power_w"] = None

            try:
                t = ctypes.c_uint()
                if self.nvml.nvmlDeviceGetTemperature(self.handle, 0, ctypes.byref(t)) == 0:
                    s["temp_c"] = t.value
                else:
                    s["temp_c"] = None
            except Exception:
                s["temp_c"] = None

            try:
                tx = ctypes.c_uint()
                rx = ctypes.c_uint()
                s["pcie_tx_kbs"] = tx.value if self.nvml.nvmlDeviceGetPcieThroughput(self.handle, 0, ctypes.byref(tx)) == 0 else None
                s["pcie_rx_kbs"] = rx.value if self.nvml.nvmlDeviceGetPcieThroughput(self.handle, 1, ctypes.byref(rx)) == 0 else None
            except Exception:
                s["pcie_tx_kbs"] = None
                s["pcie_rx_kbs"] = None

            try:
                gen = ctypes.c_uint()
                s["pcie_gen"] = gen.value if self.nvml.nvmlDeviceGetCurrPcieLinkGeneration(self.handle, ctypes.byref(gen)) == 0 else None
            except Exception:
                s["pcie_gen"] = None
        if psutil is not None:
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            s.update(ram_used=vm.used, ram_available=vm.available, swap_used=sm.used)
        return s

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self.samples.append(self._sample())
            except Exception:  # noqa: BLE001 - sampling must never kill a run
                pass
            self.stop.wait(self.interval)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def finish(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def stats(self) -> dict:
        if not self.samples:
            return {"samples": 0}

        def mx(key):
            vals = [s[key] for s in self.samples if s.get(key) is not None]
            return max(vals) if vals else None

        def mn(key):
            vals = [s[key] for s in self.samples if s.get(key) is not None]
            return min(vals) if vals else None

        def _gib(value):
            return None if value is None else round(value / 2**30, 3)

        def _sum_gib(key):
            vals = [s[key] for s in self.samples if s.get(key) is not None]
            if not vals:
                return None
            return round(sum(vals) * self.interval / 1024 / 1024, 2)

        out = {
            "samples": len(self.samples),
            "peak_vram_gib": _gib(mx("vram_used")),
            "min_vram_free_gib": _gib(mn("vram_free")),
            "peak_ram_used_gib": _gib(mx("ram_used")),
            "min_ram_available_gib": _gib(mn("ram_available")),
            "peak_swap_used_gib": _gib(mx("swap_used")),
            "peak_gpu_util": mx("gpu_util"),
            "peak_power_w": mx("power_w"),
            "peak_temp_c": mx("temp_c"),
            "pcie_gen_observed": sorted({s["pcie_gen"] for s in self.samples if s.get("pcie_gen") is not None}),
            "pcie_tx_total_gib": _sum_gib("pcie_tx_kbs"),
            "pcie_rx_total_gib": _sum_gib("pcie_rx_kbs"),
        }
        return {k: v for k, v in out.items() if v is not None}


# ComfyUI API Client

class ComfyClient:
    """Minimal API client for a running ComfyUI instance (v0.36+/v0.37)."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _json(self, path: str, payload: dict | None = None, timeout: float | None = None):
        url = self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data,
            method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode())

    def reachable(self) -> bool:
        try:
            self._json("/system_stats", timeout=5.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def submit(self, workflow: dict, client_id: str = "videolab") -> str:
        resp = self._json("/prompt", {"prompt": workflow, "client_id": client_id}, timeout=60.0)
        if "prompt_id" not in resp:
            raise RuntimeError(f"submission rejected: {str(resp)[:400]}")
        return resp["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        data = self._json(f"/history/{prompt_id}", timeout=15.0)
        return data.get(prompt_id)

    def fetch_image(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
        with urllib.request.urlopen(f"{self.base_url}/view?{params}", timeout=120) as resp:
            return resp.read()


# Workflow helpers

def strip_note_keys(workflow: dict) -> dict:
    """Drop _-prefixed documentation keys and non-node values before submission."""
    return {k: v for k, v in workflow.items()
            if not str(k).startswith("_") and isinstance(v, dict) and "class_type" in v}


def placeholder_names(workflow: dict) -> set[str]:
    """Every {TOKEN} found in string inputs of the workflow."""
    import re
    tokens: set[str] = set()
    token_re = re.compile(r"\{([A-Z0-9_]+)\}")
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for value in node.get("inputs", {}).values():
            if isinstance(value, str):
                tokens.update(token_re.findall(value))
    return tokens


def apply_params(workflow: dict, params: dict) -> dict:
    """Substitute {TOKEN} placeholders. Values are stringified. Mutates a copy."""
    import copy
    import re

    wf = copy.deepcopy(workflow)
    token_re = re.compile(r"\{([A-Z0-9_]+)\}")

    def sub(value):
        if isinstance(value, str):
            return token_re.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params
                                else m.group(0), value)
        if isinstance(value, dict):
            return {k: sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [sub(v) for v in value]
        return value

    return sub(wf)


def run_workflow(client: ComfyClient, workflow: dict, *, timeout_s: float = 7200.0,
                 poll_s: float = 2.0, monitor_interval: float = 0.5,
                 on_tick=None) -> dict:
    """Submit, monitor with NVML, and return a run record.

    The record shape follows the lab run JSONs: wall_clock_s, status, monitor,
    outputs. The caller owns persistence.
    """
    mon = NvmlMonitor(interval=monitor_interval)
    mon.start()
    t0 = time.time()
    record: dict = {"submitted_at": t0}
    try:
        prompt_id = client.submit(strip_note_keys(workflow))
        record["prompt_id"] = prompt_id
        status = None
        while True:
            time.sleep(poll_s)
            if time.time() - t0 > timeout_s:
                record["status"] = {"status_str": "timeout",
                                    "messages": [["timeout", {"timeout_s": timeout_s}]]}
                break
            try:
                entry = client.history(prompt_id)
            except Exception:  # noqa: BLE001 - transient socket errors are retried
                entry = None
            if entry is not None:
                status = entry.get("status", {})
                record["outputs"] = entry.get("outputs", {})
                record["status"] = status
                break
            if on_tick is not None:
                on_tick(time.time() - t0, mon.samples[-1] if mon.samples else {})
    finally:
        wall = time.time() - t0
        mon.finish()
        record["wall_clock_s"] = round(wall, 3)
        record["monitor"] = mon.stats()
    return record


def write_record(runs_dir: str, tag: str, record: dict) -> str:
    os.makedirs(runs_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in tag)
    path = os.path.join(runs_dir, f"{safe}_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return path
