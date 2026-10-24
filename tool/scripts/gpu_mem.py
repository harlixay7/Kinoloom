"""Device-wide dedicated VRAM, read directly from the Windows PDH counters.

WHY THIS EXISTS (measured on this machine, 2026-09-16)
------------------------------------------------------
There is no reliable *in-process* VRAM probe on this box, and I got this wrong
once already:

* `nvidia-smi` is dead — `Failed to initialize NVML: Unknown Error`.
* `torch.cuda.mem_get_info()` **is per-process here, not device-wide.**
  Proven with a controlled test: one process allocated a known 2 GB (its own
  view went 1078 -> 3152 MB) while a second process reading the same call
  stayed flat at **1078 MB**. So a benchmark harness in a separate process
  CANNOT see ComfyUI's VRAM through torch. I briefly "corrected" the harness
  to use it and that was a regression — it reported 1737 MB for a run that
  really peaks near 7 GB.
* ComfyUI's `/system_stats` `torch_vram_total` is ComfyUI's own per-process
  allocator figure, so it is low for the same reason.

That leaves the Windows performance counter
`\\GPU Adapter Memory(*)\\Dedicated Usage`, which IS device-wide (verified: it
rose to 4425 MB while the 2 GB was held). This module reads it via the PDH API
with ctypes so the benchmark harness can do it in-process, with no PowerShell
subprocess and no dependency on the shell being able to invoke PowerShell.

NOTE the absolute value includes non-CUDA consumers (desktop compositor,
driver reservations) — measured idle baseline is ~1300-1500 MB. For a
"workflow footprint" figure subtract that baseline; for "does it fit in 8 GB"
use the absolute value.

Usage
-----
    from gpu_mem import dedicated_used_mb, dedicated_peak_mb
    with peak_tracker() as p:      # samples in a background thread
        ... run the workflow ...
    print(p.peak)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

_pdh = ctypes.WinDLL("pdh")

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0

COUNTER_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD),
                ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p),
                ("FmtValue", PDH_FMT_COUNTERVALUE)]


_pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t,
                               ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                       ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]

# ⚠ restype MUST be set. ctypes defaults to a SIGNED C int, so PDH_MORE_DATA
# (0x800007D2) comes back as -2147481646 and never compares equal to the
# unsigned constant — which silently made every read return None.
for _fn in (_pdh.PdhOpenQueryW, _pdh.PdhAddEnglishCounterW,
            _pdh.PdhCollectQueryData, _pdh.PdhGetFormattedCounterArrayW,
            _pdh.PdhCloseQuery):
    _fn.restype = ctypes.c_uint32


class _Query:
    """One PDH query holding the wildcard GPU-memory counter."""

    def __init__(self) -> None:
        self.h = ctypes.c_void_p()
        if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.h)) != ERROR_SUCCESS:
            raise OSError("PdhOpenQueryW failed")
        self.c = ctypes.c_void_p()
        rc = _pdh.PdhAddEnglishCounterW(self.h, COUNTER_PATH, 0,
                                        ctypes.byref(self.c))
        if rc != ERROR_SUCCESS:
            self.close()
            raise OSError(f"PdhAddEnglishCounterW failed rc=0x{rc:08X}")
        # a first collect primes the counters; the first sample is unreliable
        _pdh.PdhCollectQueryData(self.h)

    def read_mb(self) -> float | None:
        if _pdh.PdhCollectQueryData(self.h) != ERROR_SUCCESS:
            return None
        size = wt.DWORD(0)
        count = wt.DWORD(0)
        rc = _pdh.PdhGetFormattedCounterArrayW(
            self.c, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
            ctypes.byref(size), ctypes.byref(count), None)
        if rc not in (ERROR_SUCCESS, PDH_MORE_DATA) or size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        rc = _pdh.PdhGetFormattedCounterArrayW(
            self.c, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
            ctypes.byref(size), ctypes.byref(count), buf)
        if rc != ERROR_SUCCESS:
            return None
        arr = ctypes.cast(
            buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W * count.value))
        total = 0.0
        for i in range(count.value):
            item = arr.contents[i]
            if item.FmtValue.CStatus == ERROR_SUCCESS:
                total += item.FmtValue.doubleValue
        return total / (1024 * 1024)

    def close(self) -> None:
        if self.h:
            _pdh.PdhCloseQuery(self.h)
            self.h = ctypes.c_void_p()


def dedicated_used_mb() -> float | None:
    """Device-wide dedicated VRAM in MB, or None if unavailable."""
    try:
        q = _Query()
        try:
            return q.read_mb()
        finally:
            q.close()
    except Exception:
        return None


class peak_tracker:
    """Context manager sampling device VRAM in a background thread."""

    def __init__(self, interval: float = 0.4) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self.peak: float | None = None
        self.baseline: float | None = None
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def __enter__(self) -> "peak_tracker":
        try:
            q = _Query()
        except Exception:
            return self                      # degrade to no-op, never raise
        self.baseline = q.read_mb()
        if self.baseline is not None:
            self.samples.append(self.baseline)

        def loop() -> None:
            while not self._stop.wait(self.interval):
                v = q.read_mb()
                if v is not None:
                    self.samples.append(v)
            q.close()

        self._th = threading.Thread(target=loop, daemon=True)
        self._th.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._th:
            self._th.join(timeout=5)
        if self.samples:
            self.peak = max(self.samples)

    @property
    def delta(self) -> float | None:
        if self.peak is None or self.baseline is None:
            return None
        return self.peak - self.baseline


if __name__ == "__main__":
    print("device-wide dedicated VRAM via PDH")
    for i in range(3):
        print(f"  sample {i}: {dedicated_used_mb():.0f} MB")
        time.sleep(0.6)
