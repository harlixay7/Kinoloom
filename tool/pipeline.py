"""Full-pipeline automation:
render every shot -> post -> QA -> assemble. Automated end-to-end execution loop
with its status exposed through the /api/pipeline endpoints."""
from __future__ import annotations

import threading
import time

from core import load_config, log_setup

log = log_setup("pipeline")

TERMINAL = {"passed", "review", "failed", "assembled"}


class Pipeline:
    def __init__(self, orch, db, assembler) -> None:
        self.orch = orch
        self.db = db
        self.assembler = assembler
        self.cfg = load_config()
        self._state: dict = {"status": "idle", "message": "", "started": None,
                             "eta_min": None, "progress": 0.0,
                             "done": 0, "total": 0}
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- control
    def start(self) -> dict:
        if self._state["status"] in ("running",):
            return {"ok": False, "error": "pipeline already running"}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self) -> dict:
        """Stop the orchestrator AND interrupt any in-flight ComfyUI render —
        otherwise the GPU keeps working for up to an hour."""
        self.orch.stop()
        try:
            from renderer import ComfyClient
            ComfyClient(self.cfg["ensemble"]["comfyui_port"]).interrupt()
        except Exception as e:
            log.warning(f"interrupt failed: {e}")
        self._state["status"] = "stopped"
        self._state["message"] = "stopped by user"
        return {"ok": True}

    def status(self) -> dict:
        return dict(self._state)

    # ---------------------------------------------------------------- run
    def _run(self) -> None:
        total = self.db.stats().get("total", 0)
        if total == 0:
            self._state = {"status": "idle",
                           "message": "nothing to render — ingest shots on "
                                      "the Shots page first",
                           "started": None, "eta_min": None,
                           "progress": 0.0, "done": 0, "total": 0}
            log.info("pipeline run requested with an empty queue")
            return
        self._state = {"status": "running", "message": "rendering shots",
                       "started": time.time(), "eta_min": None,
                       "progress": 0.0, "done": 0, "total": 0}
        self.orch.start()
        try:
            self._wait_for_renders()
            if self._state["status"] != "running":
                return
            if self.cfg.get("automation", {}).get("auto_assemble", True):
                self._state["message"] = "assembling movie"
                self._assemble()
                if self._state["status"] != "running":
                    return
            self._state["status"] = "done"
            self._state["message"] = "pipeline complete"
        except Exception as e:  # noqa: BLE001
            self._state["status"] = "error"
            self._state["message"] = str(e)
            log.exception("pipeline failed")

    def _wait_for_renders(self) -> None:
        t0 = time.time()
        while self._state["status"] == "running":
            stats = self.db.stats()
            total = stats.get("total", 0)
            done = sum(stats.get(s, 0) for s in TERMINAL)
            active = sum(stats.get(s, 0)
                         for s in ("queued", "rendering", "post", "qa"))
            self._state["done"] = done
            self._state["total"] = total
            if total:
                self._state["progress"] = round(done / total, 3)
            if done + active >= total and total > 0:
                # wait for any in-flight work to settle
                if active == 0:
                    return
            if done > 0 and total:
                self._state["eta_min"] = self._estimate_eta(total - done)
            time.sleep(4)

    def _estimate_eta(self, remaining: int) -> int | None:
        rows = self.db.all()
        times = [r["render_time_s"] for r in rows if r.get("render_time_s")]
        if not times:
            return None
        avg = sum(times) / len(times)
        return int(avg * remaining / 60)

    def _assemble(self) -> None:
        scenes = sorted({s["scene"] for s in self.db.all()
                         if s["status"] == "passed"})
        if not scenes:
            log.warning("no passed scenes to assemble")
            return
        title = time.strftime("movie_%Y%m%d_%H%M")
        try:
            master = self.assembler.assemble_movie(scenes, title)
            self._state["message"] = f"assembled: {master}"
        except Exception as e:  # noqa: BLE001
            self._state["status"] = "error"
            self._state["message"] = \
                f"movie assembly failed (ffmpeg/assembler error): {e}"
            log.exception("movie assembly failed")
