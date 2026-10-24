"""Engine adapters — how the unified queue executes and polls each kind.

Registered into task_queue at api startup (see `_queue_boot` in
routers/common.py, which api.py calls on startup). Each adapter's execute()
runs an existing engine entry point UNMODIFIED, with at most the additive
`worker` routing parameter, and returns an opaque engine_ref that status()
polls later. Engines keep their own persistence (gen_runs,
videostudio_jobs.json, ImageGen.active) — the queue merely mirrors state for
the unified view.

Worker lanes:
  * video — video_studio.start(**payload, worker=lane)
  * image — ImageGen.start(**payload, worker=lane); ImageGen is
    single-flight, and a remote lane routes through that worker's agent.
  * dataset — jobs.submit(...) run; polled via datasets_db.get_run.
  * refbatch — BatchGen per project; polled via BatchGen.status(pid).
  * shot  — never dispatched here (orchestrator owns chain order); queue
    rows for shots are status mirrors + worker assignments only.
"""
from __future__ import annotations

from typing import Any

import task_queue


def _engines() -> dict:
    return task_queue._engines


# ------------------------------------------------------------------ video
def exec_video(it: dict, lane: str) -> str:
    import video_studio
    payload = dict(it.get("payload") or {})
    payload["worker"] = lane              # lane is authoritative at dispatch
    # the payload is VideoStudioBody-shaped (what the UI submits); the api
    # route maps model/prompt positionally — mirror it exactly.
    mid = payload.pop("model")
    prompt = payload.pop("prompt")
    rec = video_studio.start(mid, prompt, **payload)
    if not rec.get("id"):
        raise RuntimeError(rec.get("error", "video_studio.start returned no id"))
    return str(rec["id"])


def stat_video(ref: str, it: dict) -> dict:
    import video_studio
    j = video_studio.job(ref)
    if not j:
        return {"status": "failed", "error": "video job vanished from the store"}
    s = j.get("status")
    total = int(j.get("frames") or 0)
    if s == "queued":
        return {"status": "running", "done": 0, "total": total}
    if s == "running":
        return {"status": "running", "done": 0, "total": total}
    if s == "completed":
        return {"status": "done", "done": total, "total": total}
    return {"status": "failed", "error": (j.get("error") or "failed")[:400]}


# ------------------------------------------------------------------ image
def exec_image(it: dict, lane: str) -> str:
    """Image Studio job. The lane is authoritative: 'local' runs on the
    production farm, any other lane routes through that worker's agent
    (ImageGen.start(worker=...) — same workflows, same gallery outputs)."""
    gen = _engines().get("image_gen")
    payload = dict(it.get("payload") or {})
    payload["worker"] = lane
    res = gen.start(**payload)
    if isinstance(res, dict) and res.get("ok") is False:
        raise RuntimeError(res.get("error", "ImageGen.start refused"))
    return "imagegen"


def stat_image(ref: str, it: dict) -> dict:
    gen = _engines().get("image_gen")
    a = gen.active or {}
    total = int(a.get("total") or 0)
    done = int(a.get("done") or 0)
    if a.get("running"):
        return {"status": "running", "done": done, "total": total}
    # terminal: ImageGen only runs one at a time and the local lane is
    # single-flight, so a non-running ImageGen means OUR run ended.
    # ANY failure makes the item failed — the engine counts the failed
    # attempt in `done` (done=i+1 on break), so `failed and not done` never
    # matched and a crashed run was reported as "done" with zero images.
    failed = int(a.get("failed") or 0)
    if failed:
        return {"status": "failed",
                "error": f"{failed} image(s) failed: "
                         f"{str(a.get('message') or '')[:200]}",
                "done": done, "total": total}
    return {"status": "done", "done": done or total, "total": total}


# ---------------------------------------------------------------- dataset
def exec_dataset(it: dict, lane: str) -> str:
    """Dataset batch. jobs.py already honours params.worker per task (and a
    per-run worker map); the lane fills it so a whole batch can be assigned
    to a worker PC from the queue."""
    import jobs
    p = dict(it.get("payload") or {})
    params = dict(p.get("params") or {})
    if lane and lane != "local":
        params.setdefault("worker", lane)
    res = jobs.submit(params, p.get("dtype") or "face", p.get("name") or "")
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "jobs.submit refused"))
    return str(res["run_id"])


def stat_dataset(ref: str, it: dict) -> dict:
    import datasets_db
    try:
        r = datasets_db.get_run(int(ref))
    except (TypeError, ValueError):
        r = None
    if not r:
        return {"status": "failed", "error": "run vanished from datasets.db"}
    s = r["status"]
    st = datasets_db.run_stats(int(ref)) or {}
    done = int(st.get("done", r.get("done", 0)) or 0)
    total = int(st.get("total", r.get("total", 0)) or 0)
    if s in ("queued", "active"):
        return {"status": "running", "done": done, "total": total}
    if s in ("done", "partial"):
        return {"status": "done", "done": done, "total": total}
    if s == "canceled":
        return {"status": "canceled", "done": done, "total": total}
    return {"status": "failed", "error": (r.get("error") or "failed")[:400],
            "done": done, "total": total}


# --------------------------------------------------------------- refbatch
def exec_refbatch(it: dict, lane: str) -> str:
    """Reference-batch generation (per-shot candidate images). The lane goes
    into the config; ShotImageGen routes its ComfyUI calls through the
    worker's agent when config.worker is set."""
    bg = _engines().get("batch_gen")
    p = dict(it.get("payload") or {})
    cfg = dict(p.get("config") or {})
    if lane and lane != "local":
        cfg.setdefault("worker", lane)
    res = bg.start(p.get("pid") or "", p.get("shot_ids") or [], cfg)
    if isinstance(res, dict) and res.get("ok") is False:
        raise RuntimeError(res.get("error", "BatchGen.start refused"))
    return p.get("pid") or ""


def stat_refbatch(ref: str, it: dict) -> dict:
    bg = _engines().get("batch_gen")
    st = bg.status(ref) or {}
    total = int(st.get("total") or 0)
    done = int(st.get("done") or st.get("processed") or 0)
    if st.get("running"):
        return {"status": "running", "done": done, "total": total}
    if st.get("error"):
        return {"status": "failed", "error": str(st["error"])[:400],
                "done": done, "total": total}
    return {"status": "done", "done": done or total, "total": total}


# --------------------------------------------------------------- register
def register_all() -> None:
    task_queue.register_adapter("video", exec_video, stat_video)
    task_queue.register_adapter("image", exec_image, stat_image)
    task_queue.register_adapter("dataset", exec_dataset, stat_dataset)
    task_queue.register_adapter("refbatch", exec_refbatch, stat_refbatch)


def make_worker_cancel() -> Any:
    """task_queue.cancel_running calls this for remote-lane items.
    engine_ref for worker items is 'wid:agent_job_id'."""
    def _cancel(it: dict) -> None:
        ref = it.get("engine_ref") or ""
        if ":" not in ref:
            return
        wid, agent_job = ref.split(":", 1)
        import workers as workers_mod
        client = workers_mod.RemoteWorkerClient(wid)
        old = client.current_job
        client._job_id = agent_job
        client.interrupt()
        client._job_id = old
    return _cancel
