"""Project Director — full automation with selectable
control level.

Three project modes over the SAME underlying state:
  autopilot     : idea -> story -> assets -> shots -> keyframes -> videos ->
                  QA/retries -> post -> scenes -> film. No stops.
  checkpoints   : automatically work until configurable approval gates
                  (story / keyframes / failed QA).
  manual        : nothing advances without an explicit user action; the
                  Director only performs the single requested step.

Switching modes never loses work: every decision lives in the project row,
the shot DB, and the project Bible (all resume/pause/crash-safe).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from core import ROOT, load_config, log_setup

log = log_setup("director")

GATES = ("story", "keyframes", "review")
STAGE_FLOW = ("story", "cast", "scenes", "shots", "keyframes", "render",
              "review", "assemble")
TERMINAL = {"passed", "review", "failed", "assembled"}


class Director:
    """Walks one project through the production stages, honoring gates."""

    def __init__(self, cfg: dict | None = None, db=None, store=None,
                 orch=None, assembler=None) -> None:
        self.cfg = cfg or load_config()
        self.db = db
        self.store = store
        self.orch = orch
        self.assembler = assembler
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- control
    def start(self, pid: str) -> dict:
        with self._lock:
            self._stop.discard(pid)
            t = self._threads.get(pid)
            if t is not None and t.is_alive():
                return {"ok": True, "running": True}
            t = threading.Thread(target=self._run, args=(pid,),
                                 daemon=True)
            self._threads[pid] = t
            t.start()
        return {"ok": True, "running": True}

    def stop(self, pid: str) -> dict:
        with self._lock:
            self._stop.add(pid)
        if self.orch is not None:
            try:
                self.orch.stop()
            except Exception:
                pass
        if self.store is not None:
            self.store.set_status(pid, "stopped", stage="paused")
        return {"ok": True}

    def resume(self, pid: str) -> dict:
        """Manual-mode / gate approval: advance one step past the gate."""
        self.start(pid)
        return {"ok": True}

    def status(self, pid: str) -> dict:
        proj = self.store.get(pid) if self.store else None
        stats = self.db.project_stats(pid) if self.db else {}
        running = self._threads.get(pid) is not None and \
            self._threads[pid].is_alive()
        return {"running": running, "project": proj, "stats": stats}

    def _stopped(self, pid: str) -> bool:
        with self._lock:
            return pid in self._stop

    # ------------------------------------------------------------ machine
    def _run(self, pid: str) -> None:
        try:
            proj = self.store.get(pid)
            if proj is None:
                return
            if proj.get("status") == "done":
                return
            # a stopped/errored project RESUMES from wherever it left off
            # (completed stages are skipped by _stage_done)
            self.store.set_status(pid, "running", stage=proj.get("stage")
                                  or "story")
            for stage in STAGE_FLOW:
                if self._stopped(pid):
                    return
                proj = self.store.get(pid)
                if proj.get("status") == "done":
                    return
                if self._stage_done(pid, stage, proj):
                    continue
                result = self._run_stage(pid, stage, proj)
                if result == "gate":
                    return  # waiting for user approval (manual gate)
            self.store.set_status(pid, "done", stage="assemble")
        except Exception as e:  # noqa: BLE001
            log.exception("director failed for %s", pid)
            if self.store is not None:
                self.store.set_status(pid, "error", error=str(e))
        finally:
            if self.orch is not None:
                # keep the filter: clearing it here let the STILL-RUNNING
                # orchestrator wander into other projects' queues
                pass

    def _stage_done(self, pid: str, stage: str, proj: dict) -> bool:
        story = proj.get("story") or {}
        if stage == "story":
            return bool(story.get("treatment"))
        if stage == "cast":
            from bible import Bible
            return bool(Bible(pid).characters())
        if stage == "scenes":
            from bible import Bible
            return bool(Bible(pid).scenes())
        if stage == "shots":
            return (self.db.all(project=pid, limit=1) if self.db else []) \
                and True or False
        if stage == "keyframes":
            return True  # handled inside render (candidates on demand)
        if stage == "render":
            rows = self.db.all(project=pid) if self.db else []
            active = sum(1 for r in rows if r["status"] not in TERMINAL)
            return len(rows) > 0 and active == 0
        if stage == "review":
            # the review gate is a stop condition: it is passed once the
            # operator approved it (POST /resume stores stage=assemble) or
            # autopilot moved on; otherwise every resume re-gated forever
            # while any shot stayed flagged
            return (proj.get("stage") or "") in ("assemble", "film")
        return False

    def _run_stage(self, pid: str, stage: str, proj: dict) -> str | None:
        mode = self.store.mode(pid)
        gate = self.store.gate(pid, stage) \
            if stage in GATES else ("manual" if mode == "manual" else "auto")

        if stage == "story":
            return self._stage_story(pid, proj, gate)
        if stage == "cast":
            self.store.set_status(pid, "running", stage="scenes")
            return None
        if stage == "scenes":
            self.store.set_status(pid, "running", stage="shots")
            return None
        if stage == "shots":
            return self._stage_shots(pid, proj, gate)
        if stage == "keyframes":
            return self._stage_keyframes(pid, proj, gate)
        if stage == "render":
            return self._stage_render(pid, proj)
        if stage == "review":
            return self._stage_review(pid, proj, gate)
        if stage == "assemble":
            return self._stage_assemble(pid, proj)
        return None

    # ------------------------------------------------------------ stages
    def _stage_story(self, pid: str, proj: dict, gate: str) -> str | None:
        story = proj.get("story") or {}
        if not story.get("treatment"):
            from bible import Bible
            from storygen import StoryArchitect
            p_bible = Bible(pid)
            if not (p_bible.dir / "universe.yaml").exists():
                p_bible.snapshot_from_global(pid)
            res = StoryArchitect(proj, self.store, p_bible).generate()
            if not res.get("ok"):
                raise RuntimeError(f"story generation failed: "
                                   f"{res.get('error')}")
        if gate == "manual":
            self.store.set_status(pid, "gate_pending", stage="story")
            return "gate"
        self.store.set_status(pid, "running", stage="cast")
        return None

    def _stage_shots(self, pid: str, proj: dict, gate: str) -> str | None:
        rows = self.db.all(project=pid) if self.db else []
        if not rows:
            # regenerate the shots stage (story exists but queue was cleared)
            from bible import Bible
            from storygen import StoryArchitect
            res = StoryArchitect(proj, self.store, Bible(pid)) \
                .generate(stages=["beats", "scenes", "shots"])
            if not res.get("ok"):
                raise RuntimeError(f"shot regeneration failed: "
                                   f"{res.get('error')}")
        if gate == "manual":
            self.store.set_status(pid, "gate_pending", stage="shots")
            return "gate"
        self.store.set_status(pid, "running", stage="keyframes")
        return None

    def _stage_keyframes(self, pid: str, proj: dict,
                         gate: str) -> str | None:
        """Generate + auto-approve keyframes for hero/important shots."""
        from bible import Bible
        from keyframe_gen import KeyframeGen, resolve_project_ref
        from lora_video import VideoLoraRegistry, resolve_chain, pick_profile
        kcfg = self.cfg.get("keyframes", {})
        auto = bool(kcfg.get("auto_approve", True)) or gate == "auto"
        p_bible = Bible(pid)
        reg = VideoLoraRegistry(self.cfg)
        reg.scan()
        gen = KeyframeGen(self.cfg)
        ref = resolve_project_ref(self.store, pid)
        rows = self.db.all(project=pid) if self.db else []
        made = 0
        for r in rows:
            sjson = r.get("json") or {}
            important = (sjson.get("tier") == "hero"
                         or sjson.get("keyframe_source") == "generated"
                         or (sjson.get("characters") and
                             sjson.get("shot_size") in
                             ("close_up", "extreme_close_up",
                              "medium_close_up")))
            if not important:
                continue
            from keyframe_gen import _kf_dir
            if r.get("keyframe_path") or \
                    _kf_dir(r["shot_id"], pid).exists():
                continue
            chain = resolve_chain(sjson, p_bible, reg, is_i2v=True)
            res = gen.generate_candidates(
                sjson, p_bible, chain,
                count=int(kcfg.get("candidates", 2)),
                base_seed=self.db.assign_seed(sjson) + 500,
                ref_image=ref)
            if res.get("ok"):
                made += 1
                if auto and res.get("approved"):
                    self.db.set_status(r["shot_id"], "queued",
                                       keyframe_path=res["approved"],
                                       project=pid)
        if gate == "manual" and made:
            self.store.set_status(pid, "gate_pending", stage="keyframes")
            return "gate"
        self.store.set_status(pid, "running", stage="render")
        return None

    def _stage_render(self, pid: str, proj: dict) -> str | None:
        if self.orch is None:
            return None
        self.orch.project_filter = pid
        self.orch.start()
        self.store.set_status(pid, "rendering", stage="render")
        while True:
            if self._stopped(pid):
                # keep the filter: clearing it here let the STILL-RUNNING
                # orchestrator wander into other projects' queues
                pass
                return None
            rows = self.db.all(project=pid)
            active = sum(1 for r in rows
                         if r["status"] not in TERMINAL)
            if active == 0:
                break
            time.sleep(4)
        # the filter stays on this project — the orchestrator keeps running
        # and idles (nothing pending) instead of picking another movie's queue
        self.store.set_status(pid, "rendered", stage="review")
        return None

    def _stage_review(self, pid: str, proj: dict, gate: str) -> str | None:
        rows = self.db.all(project=pid) if self.db else []
        flagged = [r for r in rows if r["status"] == "review"]
        if flagged and gate == "manual":
            self.store.set_status(pid, "gate_pending", stage="review")
            return "gate"
        # autopilot: proceed with passed shots (review shots stay flagged)
        self.store.set_status(pid, "running", stage="assemble")
        return None

    def _stage_assemble(self, pid: str, proj: dict) -> str | None:
        if self.assembler is None:
            return None
        # this project's PASSED shots only, in story order (assembler.project_clips)
        from assembler import project_clips
        clips = project_clips(self.db.all(project=pid) if self.db else [])
        scenes = sorted(clips)
        if not scenes:
            log.warning("director: no passed scenes to assemble for %s", pid)
            self.store.set_status(pid, "done", stage="assemble")
            return None
        title = (proj.get("title") or "movie").replace(" ", "_")
        try:
            master = self.assembler.assemble_movie(scenes, title, clips=clips,
                                                   ns=pid)
            self.store.update(pid, stage="film", status="done")
            log.info("director: assembled %s -> %s", pid, master)
        except Exception as e:  # noqa: BLE001
            log.error("director: assembly failed for %s: %s", pid, e)
            self.store.set_status(pid, "error", stage="assemble",
                                  error=str(e))
        return None
