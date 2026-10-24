"""Start ComfyUI with an on-demand stack dump (chain_lab's hang diagnostics).

Windows has no SIGUSR1, so a hung server cannot be asked for its Python stacks.
This shim starts a watcher thread, then runs ComfyUI's main.py unchanged. Creating
the file named by LAB_DUMP_TRIGGER makes the watcher append every thread's stack
to LAB_DUMP_FILE (the GIL is released inside CUDA/IO calls, so it answers even
while a node is stuck in native code). A trigger file containing "mem" appends
the CUDA memory state instead (torch allocated/reserved and their peaks since the
previous "mem" dump, device free/total) and resets the peaks.

usage: python _comfy_lab_boot.py <ComfyUI main.py> [ComfyUI args...]
"""
import faulthandler
import os
import runpy
import sys
import threading
import time

main_py = os.path.abspath(sys.argv[1])
trigger = os.environ.get("LAB_DUMP_TRIGGER", "")
dump_file = os.environ.get("LAB_DUMP_FILE", "")


def _memory_state() -> str:
    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return "torch/CUDA not loaded\n"
    mb = 2 ** 20
    free, total = torch.cuda.mem_get_info()
    out = (f"device free {free / mb:.0f} MB of {total / mb:.0f} MB\n"
           f"torch allocated {torch.cuda.memory_allocated() / mb:.0f} MB (peak {torch.cuda.max_memory_allocated() / mb:.0f})"
           f", reserved {torch.cuda.memory_reserved() / mb:.0f} MB (peak {torch.cuda.max_memory_reserved() / mb:.0f})\n")
    torch.cuda.reset_peak_memory_stats()
    return out


def _watch() -> None:
    while True:
        time.sleep(1.0)
        if not (trigger and os.path.exists(trigger)):
            continue
        try:
            with open(trigger, encoding="utf-8") as t:
                what = t.read().strip()
            os.remove(trigger)
        except OSError:
            what = ""
        with open(dump_file, "a", encoding="utf-8") as f:
            if what == "mem":
                f.write(f"\n==== memory {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                try:
                    f.write(_memory_state())
                except Exception as e:  # noqa: BLE001
                    f.write(f"memory state failed: {e}\n")
                continue
            f.write(f"\n==== stack dump {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)


if trigger and dump_file:
    threading.Thread(target=_watch, name="lab-stack-dump", daemon=True).start()

# behave exactly like `python main.py ...`: ComfyUI's own directory first on sys.path
sys.path[0] = os.path.dirname(main_py)
sys.argv = [main_py] + sys.argv[2:]
runpy.run_path(main_py, run_name="__main__")
