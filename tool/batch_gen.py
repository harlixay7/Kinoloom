"""Batch reference generation (user-requested feature): select scenes or
shots, pick a model + LoRAs + steps/CFG/hires once, and generate cinematic
reference images for ALL of them automatically — then auto-assign the best
result to each shot (becomes its I2V start image). Stop-safe and progress-
visible. Reuses the per-shot image studio under the hood."""
from __future__ import annotations

import threading
import time
from typing import Any

from core import load_config, log_setup

log = log_setup("refbatch")


class BatchGen:
    """One background worker per project. Iterates the selected shots,
    runs the per-shot studio for each, ranks the results and auto-assigns
    the sharpest image as the shot's reference (ref_image)."""

    def __init__(self, cfg: dict | None = None, db=None) -> None:
        self.cfg = cfg or load_config()
        self.db = db
        self._jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ control
    def start(self, pid: str, shot_ids: list[str], config: dict) -> dict:
        if not pid or not shot_ids:
            return {"ok": False, "error": "select at least one shot"}
        with self._lock:
            job = self._jobs.get(pid)
            if job is not None and job.get("running"):
                return {"ok": False, "error": "a batch is already running "
                                               "for this project"}
            self._stop.discard(pid)
            self._jobs[pid] = {
                "running": True, "done": 0, "total": len(shot_ids),
                "current": "", "message": "starting", "results": {},
                "started": time.time(), "config": config,
            }
            t = threading.Thread(target=self._run,
                                 args=(pid, list(shot_ids), dict(config)),
                                 daemon=True)
            self._threads[pid] = t
            t.start()
        return {"ok": True, "total": len(shot_ids)}

    def status(self, pid: str) -> dict:
        with self._lock:
            job = self._jobs.get(pid)
        return {"active": dict(job) if job else None}

    def stop(self, pid: str) -> dict:
        with self._lock:
            self._stop.add(pid)
        return {"ok": True}

    # ---------------------------------------------------------------- run
    def _run(self, pid: str, shot_ids: list[str], config: dict) -> None:
        from shot_images import ShotImageGen, KF_ROOT, _ns
        job = self._jobs.get(pid) or {}
        try:
            gen = ShotImageGen(self.cfg)
            for sid in shot_ids:
                if pid in self._stop:
                    job["message"] = "stopped"
                    break
                row = self.db.get(sid, pid) if self.db else None
                if row is None:
                    job["results"][sid] = {"error": "shot not found"}
                    job["done"] += 1
                    continue
                job["current"] = sid
                job["message"] = f"generating {sid}…"
                shot = dict(row.get("json") or {})
                shot["shot_id"] = sid
                shot["project_id"] = pid
                from bible import Bible
                bible = Bible(pid)
                params = dict(config)
                res = gen.start(shot, bible, params)
                if not res.get("ok"):
                    job["results"][sid] = {"error": res.get("error",
                                                            "start failed")}
                    job["done"] += 1
                    continue
                # poll the per-shot job until done
                while pid not in self._stop:
                    st = gen.status(sid, pid)
                    act = st.get("active")
                    if act is None or not act.get("running"):
                        break
                    time.sleep(2)
                if pid in self._stop:
                    job["results"][sid] = {"error": "stopped"}
                    job["done"] += 1
                    continue
                files = st.get("files") or []
                if not files:
                    job["results"][sid] = {
                        "error": act.get("message") or "no images"}
                    job["done"] += 1
                    continue
                # rank by sharpness; auto-assign the best
                best = self._rank_best(pid, sid, files)
                assigned = None
                if best is not None:
                    p = KF_ROOT / _ns(pid) / sid / best
                    self.db.set_status(sid, "queued", ref_image=str(p),
                                       project=pid)
                    assigned = best
                job["results"][sid] = {"files": files,
                                       "assigned": assigned}
                job["done"] += 1
                job["message"] = f"{sid} done ({len(files)} images)"
            job["running"] = False
            if not job.get("message") or job["message"] == "starting":
                job["message"] = "batch complete"
        except Exception as e:  # noqa: BLE001
            log.exception("refbatch failed for %s", pid)
            job["message"] = f"failed: {e}"
            job["running"] = False
        finally:
            with self._lock:
                self._stop.discard(pid)

    @staticmethod
    def _rank_best(pid: str, sid: str, files: list[str]) -> str | None:
        """Pick the sharpest generated image (Laplacian variance). Falls
        back to the first file when ranking is impossible."""
        import cv2
        import numpy as np
        from shot_images import KF_ROOT, _ns
        best, best_v = None, -1.0
        for f in files:
            p = KF_ROOT / _ns(pid) / sid / f
            try:
                img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    continue
                v = float(cv2.Laplacian(img, cv2.CV_64F).var())
                if v > best_v:
                    best_v, best = v, f
            except Exception:
                continue
        return best if best is not None and files else \
            (files[0] if files else None)
