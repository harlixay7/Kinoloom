"""Simple "Generate photo" box (Training tab) — routed through the shared
job engine so every model in the registry is available."""
from __future__ import annotations

import threading
from pathlib import Path

from core import ROOT, log_setup, is_safe_component

log = log_setup("photogen")


class PhotoGen:
    def __init__(self) -> None:
        self._run_id: int | None = None
        self.active: dict = {"running": False, "done": 0, "total": 0,
                             "message": ""}

    def start(self, dtype: str, name: str, prompt: str, count: int = 4,
              model: str = "realvis") -> dict:
        if self._run_id:
            import datasets_db as db
            s = db.run_stats(self._run_id)
            if not s or s.get("status") not in ("queued", "active"):
                self._run_id = None
                self.active["running"] = False
        if self.active["running"]:
            return {"ok": False, "error": "a generation is already active"}
        if not is_safe_component(dtype) or dtype not in (
                "face", "power", "environment", "anatomy", "quality"):
            return {"ok": False, "error": f"bad dataset type {dtype}"}
        if not name.strip():
            return {"ok": False, "error": "dataset name required"}
        if not is_safe_component(name):
            return {"ok": False, "error": f"bad dataset name {name}"}
        if not prompt or not prompt.strip():
            return {"ok": False, "error": "prompt is empty"}
        import jobs
        from models import get_model
        if get_model(model) is None:
            return {"ok": False, "error": f"unknown model '{model}'"}
        d = ROOT / "roster" / "datasets" / dtype / name
        d.mkdir(parents=True, exist_ok=True)
        res = jobs.submit({
            "mode": "photo", "prompt": prompt.strip(),
            "models": [{"model": model, "count": count}],
        }, dtype, name)
        if not res.get("ok"):
            return res
        self._run_id = res["run_id"]
        self.active.update({"running": True, "done": 0, "total": res["total"],
                            "message": "queued"})
        return {"ok": True, "run_id": res["run_id"], "total": res["total"]}

    def stop(self) -> None:
        import jobs
        if self._run_id:
            jobs.cancel(self._run_id)
            self.active.update({"running": False, "message": "canceled"})

    def status(self) -> dict:
        import datasets_db as db
        if self._run_id:
            s = db.run_stats(self._run_id)
            if s:
                total = s.get("total") or 0
                done = s.get("done") or 0
                self.active.update({
                    "running": s.get("status") in ("queued", "active"),
                    "done": done, "total": total,
                    "message": f"{done}/{total} · {s.get('status')}"})
                if not self.active["running"]:
                    self._run_id = None
        return {"gen": self.active}
