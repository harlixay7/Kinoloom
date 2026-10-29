"""Orchestrator + queue. Tier routing, pipelined post
(post of shot N runs while shot N+1 renders), retries, resume-safe.

Project shots on LTX-2.5 / MiniMax H3 render unit by unit (movie_prompts:
one shot, or a seamless take of CONTINUE shots) with exactly the Video
Studio's request (movie_request) and the prompts written for it by an AI or
by hand (movie_prompts), in story order. Wan 2.2 (legacy) and shots without
a project keep their own path."""
from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core import ROOT, ffmpeg_exe, load_config, log_setup
from linter import Linter
from movie_request import NoVideoModel
from prompts import PromptBuilder
from renderer import Renderer
from shots_db import ShotsDB

log = log_setup("orchestrator")

__all__ = ["NoVideoModel", "Orchestrator", "waits_for"]
# A retake's seed step. Never a multiple of the chain engine's seam re-roll step
# (chain_engine.REROLL_SEED_OFFSET, 7919): a retaken shot of a take renders under
# the take's chain id, and must not land on a seed its re-roll already tried.
REROLL_SEED = 104729
_DONE = ("rendered", "post", "qa", "passed", "review", "assembled")
# the record of an LTX-2.5 / H3 render (_provenance, _render_chain)
_MOVIE_RECORD = ("applied_model", "applied_references", "take", "request", "opening_frame",
                 "reference_warnings", "continuation_frame")


def waits_for(r: dict, by_key: dict) -> dict | None:
    """The shot `r` opens on and so renders after, or None when it opens on
    its own. A CONTINUE shot opens on the last frame of the shot before it;
    so does a MATCH_ACTION in the same place (its opening frame is made from
    that frame). A shot before it in another scene or place is a cut
    (movie_request). `by_key`: the rows by (project_id, shot_id)."""
    j = r.get("json")
    sj: dict = j if isinstance(j, dict) else {}
    mode = str(sj.get("continuity") or "").upper()
    if mode not in ("CONTINUE", "MATCH_ACTION") or not sj.get("prev_shot"):
        return None
    prev = by_key.get((r.get("project_id"), str(sj["prev_shot"])))
    if prev is None or prev.get("scene") != r.get("scene"):
        return None
    pj = prev.get("json")
    locs = {str((pj if isinstance(pj, dict) else {}).get("location") or ""),
            str(sj.get("location") or "")}
    if mode == "MATCH_ACTION" and len(locs - {""}) > 1:
        return None
    return prev


class Orchestrator:
    def __init__(self, bible, db: ShotsDB, renderer: Renderer,
                 builder: PromptBuilder, linter: Linter) -> None:
        self.bible = bible
        self.db = db
        self.renderer = renderer
        self.builder = builder
        self.linter = linter
        self.cfg = load_config()
        self.running = False
        self.paused = False
        self._thermal_paused = False
        self._lock = threading.Lock()
        self._post_queue: queue.Queue = queue.Queue()
        self._post_thread: threading.Thread | None = None
        self._thread: threading.Thread | None = None
        self._transient: dict[str, int] = {}
        self.project_filter: str | None = None   # render only this project
        self.job_callbacks: list[Callable[[dict], None]] = []
        self.last_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    def _reset_orphaned_rendering(self) -> int:
        """Orphaned "rendering" rows -> pending. Returns how many.

        A crash, a closed terminal or a stop mid-shot leaves rows in
        'rendering' that the loop never re-picks (it only looks at
        queued/pending/failed) — the shot would hang forever. Called by
        start() BEFORE the loop runs (never while running).
        """
        try:
            n = self.db.con.execute(
                "UPDATE shots SET status='pending' WHERE status='rendering'"
            ).rowcount
            if n:
                self.db.con.commit()
                log.info("orchestrator: reset %d orphaned 'rendering' shot(s) "
                         "to pending", n)
            return int(n or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("orchestrator: could not reset rendering shots: %s", e)
            return 0

    def _recover_post_backlog(self) -> int:
        """Shots a crash/restart left between render and QA ('rendered' /
        'post'): their post-queue entries lived in memory only, and the loop
        never re-picks those statuses — they hung forever. Re-queue them for
        post+QA when the raw render still exists, else back to pending.
        Called once per process, before the post worker starts."""
        n = 0
        try:
            rows = [r for st in ("rendered", "post")
                    for r in self.db.all(status=st)]
        except Exception as e:  # noqa: BLE001
            log.warning("orchestrator: post backlog scan failed: %s", e)
            return 0
        for r in rows:
            vp = r.get("video_path")
            sjson = r.get("json") or {}
            if vp and Path(vp).is_file():
                self._post_queue.put((r, sjson, Path(vp),
                                      str(r.get("prompt") or
                                          sjson.get("prompt") or "")))
            else:
                self.db.set_status(r["shot_id"], "pending",
                                   project=r.get("project_id"))
            n += 1
        if n:
            log.info("orchestrator: recovered %d shot(s) stranded between "
                     "render and QA", n)
        return n

    def start(self) -> None:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if self.running and alive:
                return
            self.running = True
            self.paused = False
            if alive:
                # stop() only clears the flag; that loop is still finishing
                # its in-flight shot and simply carries on. Starting a second
                # loop here rendered two shots at once (and the reset below
                # re-queued the in-flight one).
                log.info("orchestrator resumed (in-flight loop kept)")
                return
            self._reset_orphaned_rendering()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="orchestrator-loop")
            self._thread.start()
            if self._post_thread is None:
                self._recover_post_backlog()
            if self._post_thread is None or not self._post_thread.is_alive():
                self._post_thread = threading.Thread(target=self._post_worker,
                                                     daemon=True,
                                                     name="orchestrator-post")
                self._post_thread.start()
        log.info("orchestrator started")

    def stop(self) -> None:
        self.running = False
        log.info("orchestrator stopped")

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def status(self) -> dict:
        stats = self.db.stats()
        return {
            "running": self.running,
            "paused": self.paused,
            "project": self.project_filter,   # scope: ""/None = every project
            "stats": stats,
            "last_error": self.last_error,
        }

    # -------------------------------------------------------------- ingest
    def ingest(self, shots: list[dict]) -> dict:
        rep = self.linter.lint_package(shots)
        result = {"lint": rep.to_dict(), "stored": None}
        if rep.ok:
            result["stored"] = self.db.upsert_shots(shots)
            log.info(f"ingested {len(shots)} shots")
        else:
            log.warning(f"ingest blocked: {len(rep.errors)} lint errors")
        return result

    def lint_only(self, shots: list[dict]) -> dict:
        return self.linter.lint_package(shots).to_dict()

    # --------------------------------------------------------------- order
    def _next_shot(self) -> dict | None:
        """The next shot to render, in story order (scenes in order, each in
        its prev_shot order): queued and pending shots first, then failed
        ones. A CONTINUE shot waits until the shot before it has rendered: it
        opens on that shot's last frame. So does a MATCH_ACTION: its opening
        frame is made from that last frame (opening_frames)."""
        from shots_db import story_order
        rows = self.db.all(project=self.project_filter)
        by_key = {(r.get("project_id"), r["shot_id"]): r for r in rows}
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            groups.setdefault((str(r.get("project_id") or ""), str(r.get("scene") or "")), []).append(r)
        ordered = [r for k in sorted(groups) for r in story_order(groups[k])]

        def ready(r: dict) -> bool:
            prev = waits_for(r, by_key)
            return prev is None or prev.get("status") in _DONE

        for wanted in (("queued", "pending"), ("failed",)):
            for r in ordered:
                if r.get("status") in wanted and ready(r):
                    if r["status"] == "pending":
                        self.db.set_status(r["shot_id"], "queued", project=r.get("project_id"))
                        return self.db.get(r["shot_id"], r.get("project_id"))
                    return r
        return None

    # ----------------------------------------------------------------- loop
    def _thermal_gate(self) -> None:
        """Pause rendering while any GPU exceeds 85C,
        resume under 75C. Reads nvidia-smi once per gate."""
        import subprocess
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            temps = [int(line.split(",")[1].strip())
                     for line in out.stdout.strip().splitlines()
                     if "," in line]
        except Exception:
            return
        if not temps:
            return
        if any(t > 85 for t in temps) or getattr(self, "_thermal_paused", False):
            self._thermal_paused = True
            while any(t > 75 for t in temps) and self.running:
                log.warning(f"thermal gate: GPU temps {temps} > 75C — "
                            f"pausing until <= 75C")
                self._emit({"event": "thermal", "temps": temps})
                time.sleep(30)
                try:
                    out = subprocess.run(
                        ["nvidia-smi", "--query-gpu=index,temperature.gpu",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=10)
                    temps = [int(line.split(",")[1].strip())
                             for line in out.stdout.strip().splitlines()
                             if "," in line]
                except Exception:
                    break
            if all(t <= 75 for t in temps):
                self._thermal_paused = False

    def _loop(self) -> None:
        while self.running:
            if self.paused:
                time.sleep(2)
                continue
            try:
                self._thermal_gate()
                shot = self._next_shot()
                if shot is None:
                    time.sleep(5)
                    continue
                self._render_shot(shot)
            except Exception as e:
                self.last_error = str(e)
                log.exception("orchestrator loop error")
                time.sleep(10)

    # --------------------------------------------------------------- render
    def _render_shot(self, shot: dict) -> None:
        sid = shot["shot_id"]
        cfg = self.cfg
        tier = shot["tier"] or "standard"
        sjson = shot["json"] or {}
        pid = shot.get("project_id") or sjson.get("project_id")
        is_project = bool(pid)
        if is_project:
            # the LTX-2.5 / H3 model id; "" = the legacy Wan 2.2 path, which
            # only a project that picks Wan and the API-only shots without a
            # project (/api/shots/ingest) still render on
            try:
                mid = self._video_family(shot, sjson, pid)
            except NoVideoModel as e:
                self.last_error = str(e)
                self.db.set_status(sid, "review", error=str(e), project=pid)
                self._emit({"shot_id": sid, "event": "error", "error": str(e),
                            "attempts": 0})
                log.error(f"{sid}: {e} - sent to review")
                return
            if mid:
                self._render_unit(shot, pid)
                return
            # rendered on LTX-2.5 / H3 before the project moved to Wan: the
            # movie's record goes, the QA and the retakes read it
            sjson = {k: v for k, v in sjson.items() if k not in _MOVIE_RECORD}
        seed = self.db.assign_seed(sjson)
        state = self._scene_state(sid, sjson, pid)

        # ---------------------------------------------------------- routing
        if tier == "volume":
            has_people = bool(sjson.get("characters")) or bool(
                sjson.get("power"))
            tier = "volume_people" if has_people else "volume_env"
        res = cfg["tiers"]["resolution"].get(tier, "832x480")
        steps = cfg["tiers"]["steps"].get(tier, 6)
        fps = cfg["tiers"]["fps"].get(tier, 24)

        force_t2v = bool(sjson.get("t2v"))

        # ---- project path (Wan 2.2): continuity keyframes, LoRA chain, profiles
        lora_names: list[str] = []
        profile = "production_fast"
        p_bible = None
        if is_project:
            from bible import Bible
            from continuity import start_image_for
            from lora_video import pick_profile
            p_bible = Bible(pid)
            prev_row = None
            prev_id = sjson.get("prev_shot")
            if prev_id:
                prev_row = self.db.get(prev_id, pid)
            start = None
            if not force_t2v:
                ref = shot.get("ref_image")
                if ref and Path(ref).exists():
                    start = Path(ref)
                else:
                    # user setting: allow the roster identity portrait as
                    # a last-resort start image (default yes; off -> T2V)
                    allow_roster = True
                    try:
                        prow = self.db.con.execute(
                            "SELECT ref_fallback FROM projects WHERE id=?",
                            (pid,)).fetchone()
                        if prow is not None and prow[0] is not None:
                            allow_roster = int(prow[0]) != 0
                    except Exception:
                        allow_roster = True
                    start, _mode = start_image_for(
                        sjson, prev_row, self.db, p_bible,
                        allow_roster=allow_roster)
            chain = self._lora_chain(shot, sjson, p_bible, is_i2v=start is not None)
            profile = pick_profile(sjson, chain, cfg)
            pcfg = cfg.get("render_profiles", {}).get(profile, {})
            if pcfg.get("res"):
                res = pcfg["res"]
            if pcfg.get("steps"):
                steps = int(pcfg["steps"])
            if pcfg.get("fps"):
                fps = int(pcfg["fps"])
            lora_names = [c["name"] for c in chain if c.get("enabled", True)]
        else:
            # ---- shots without a project (API-only /api/shots/ingest): the
            # legacy Wan 2.2 path, unchanged I2V keyframe downgrade logic ----
            start = None
            if force_t2v:
                tier = "t2v_fallback"
            elif tier in ("standard", "volume_people"):
                start = self._pick_keyframe(sjson)
                if start is None:
                    log.warning(f"{sid}: no keyframe for I2V — rendering as "
                                f"T2V fallback")
                    tier = "t2v_fallback"
            chain = []

        w, h = map(int, res.split("x"))
        frames = 5 * fps + 1

        # ----------------------------------------------------------- prompt
        if is_project:
            prompt, negative = self._project_prompt(
                shot, sjson, state, p_bible,
                is_i2v=start is not None and not force_t2v, lora_names=lora_names)
        else:
            prompt = self.builder.build(sjson, state)
            negative = self.builder.negative(sjson)

        # --------------------------------------------------------- workflow
        # A project that picks the legacy Wan 2.2 model keeps its templates
        # byte-identically; shots without a project use the renderer's tiers.
        if is_project:
            from workflows import WorkflowBuilder
            from lora_video import chain_to_extra
            base = "wan2.2_ti2v_dynamic" if (start is not None
                                             and not force_t2v) \
                else "wan2.2_t2v_dynamic"
            wb = WorkflowBuilder()
            workflow = wb.build(base, prompt, negative, seed, w, h, steps,
                                frames, cfg,
                                extra={"SHOT_ID": sid,
                                       "LORA_CHAIN": chain_to_extra(chain)})
        else:
            workflow = self.renderer.build_workflow(
                tier=tier, prompt=prompt, negative=negative, seed=seed,
                width=w, height=h, steps=steps, frames=frames, shot_id=sid,
                sjson=sjson)

        # ------------------------------------------------- worker routing
        # The pipeline stays strictly sequential (each
        # shot chains from the previous one's tail), but WHICH machine
        # renders a shot is assignable — project default plus per-shot
        # override, read from queue.db. The start image is uploaded to the
        # chosen worker and the finished video is pulled back, so the next
        # link in the chain is unaffected even if it hops machines.
        shot_worker = "local"
        try:
            import task_queue as _tq
            shot_worker = _tq.shot_worker(pid if is_project else "", sid)
        except Exception:
            pass
        if shot_worker and shot_worker != "local":
            try:
                import workers as _workers_mod
                shot_client = _workers_mod.client_for(shot_worker)
            except Exception as e:
                log.warning(f"{sid}: worker {shot_worker} unavailable ({e})"
                            " — rendering locally")
                shot_worker = "local"
                shot_client = self.renderer.client
        else:
            shot_client = self.renderer.client

        if start is not None:
            resized = self._resize_keyframe(start, w, h)
            fname = shot_client.upload_image(resized)
            workflow = self._set_start_image(workflow, fname)

        self.db.set_status(sid, "rendering", seed=seed, prompt=prompt,
                           negative=negative,
                           render_profile=profile if is_project else None,
                           project=pid if is_project else None)
        if is_project:
            # provenance: record the EXACT LoRA chain this render uses
            from lora_video import chain_to_extra
            prov = dict(sjson)
            prov["applied_loras"] = chain_to_extra(chain)
            self.db.set_json(sid, prov, project=pid)
        t0 = time.time()
        try:
            prompt_id = shot_client.queue_prompt(workflow)
            log.info(f"render {sid} ({tier}{'/' + profile if is_project else ''})"
                     f" submitted: {prompt_id}"
                     + (f" @ {shot_worker}" if shot_worker != "local" else ""))
            result = shot_client.wait_for(prompt_id, timeout_s=2400)
            if result is None:
                raise TimeoutError(f"{sid} render timeout")
            video = self._fetch_output(result, client=shot_client)
            render_s = time.time() - t0
            self.db.record_render(sid, render_s, str(video),
                                  project=pid if is_project else None)
            self._transient.pop(sid, None)
            self._emit({"shot_id": sid, "event": "rendered", "time_s": render_s,
                        "video": str(video)})
            # CONTINUITY FIRST: extract the tail frame from the RAW render now,
            # before the clip is handed to post. With post ON and a worker PC
            # upscaling, the tail used to exist only after the worker finished
            # — the next shot then had no start frame and the chain stalled.
            try:
                from continuity import extract_tail
                tail = extract_tail(video, sid, pid=pid)
                if tail is not None:
                    self.db.set_status(sid, "rendered",
                                       tail_frame=str(tail), project=pid)
            except Exception as e:  # noqa: BLE001
                log.warning(f"{sid}: early tail extraction failed: {e}")
            # pipelining: post+QA for this shot runs on the post worker
            # while the loop picks up the next render immediately
            self._post_queue.put((shot, sjson, video, prompt))
        except Exception as e:
            self.last_error = str(e)
            transient = "10061" in str(e) or "refused" in str(e) or \
                isinstance(e, TimeoutError)
            if transient:
                # comfy is booting/restarting — wait and retry WITHOUT
                # consuming the retry budget, but cap the storm at 60 tries
                n = self._transient.get(sid, 0) + 1
                self._transient[sid] = n
                if n > 60:
                    self._transient.pop(sid, None)
                    msg = f"comfy unreachable after {n} transient retries"
                    self.db.set_status(sid, "review", error=msg,
                                       project=pid if is_project else None)
                    self._emit({"shot_id": sid, "event": "error",
                                "error": msg, "attempts": 0})
                    log.error(f"{sid}: {msg} — sent to review")
                    return
                log.warning(f"{sid}: transient failure ({e}) — retrying in "
                            f"30s without consuming budget ({n}/60)")
                time.sleep(30)
                self.db.set_status(sid, "queued",
                                   project=pid if is_project else None)
                return
            attempts = self.db.increment_attempts(
                sid, project=pid if is_project else None)
            budget = cfg["qa"]["max_retries"].get(
                shot.get("tier", "standard"), 2)
            status = "failed" if attempts >= budget else "queued"
            if status == "failed":
                self.db.set_status(sid, "review", error=str(e),
                                   project=pid if is_project else None)
            else:
                self.db.set_status(
                    sid, "queued", seed=self.db.retry_seed(sjson, attempts),
                    project=pid if is_project else None)
            self._emit({"shot_id": sid, "event": "error", "error": str(e),
                        "attempts": attempts})
            log.error(f"{sid} failed: {e}")

    # ----------------------------------------------------- shot preparation
    def _scene_state(self, sid: str, sjson: dict, pid: str | None) -> dict[str, dict[str, Any]]:
        """Scene state accumulated before `sid` (persisting state mutations of
        the earlier shots) — follows the actual prev_shot chain,
        falling back to shot-id order for orphan shots."""
        state: dict[str, dict[str, Any]] = {}
        try:
            for r in self.db.scene_order(sjson.get("scene", ""), project=pid):
                if r["shot_id"] == sid:
                    break
                for mut in (r.get("json") or {}).get("state_mutations", []):
                    t, k = mut.get("target"), mut.get("key")
                    if mut.get("persist") and t and k:
                        state.setdefault(t, {})[k] = mut["value"]
        except Exception as e:  # noqa: BLE001
            log.warning(f"{sid}: scene state accumulation failed: {e}")
        return state

    def _lora_chain(self, shot: dict, sjson: dict, p_bible, is_i2v: bool) -> list[dict]:
        """The shot's video LoRA chain (overrides, then the bible), softened
        once the QA ladder reaches its adjust stage."""
        from lora_video import (ADJUST_LORA_STAGE, VideoLoraRegistry,
                                resolve_chain, soften_effect_loras)
        reg = VideoLoraRegistry(self.cfg)
        reg.scan()
        chain = resolve_chain(sjson, p_bible, reg,
                              overrides=sjson.get("selected_loras") or None,
                              is_i2v=is_i2v)
        if int(shot.get("qa_stage") or 0) >= ADJUST_LORA_STAGE:
            soften_effect_loras(chain)
        return chain

    def _project_prompt(self, shot: dict, sjson: dict, state: dict, p_bible,
                        is_i2v: bool, lora_names: list[str]) -> tuple[str, str]:
        """(prompt, negative) of a Wan 2.2 project shot."""
        from videoprompt import VideoPromptBuilder
        vb = VideoPromptBuilder(p_bible, self.db)
        if shot.get("prompt") and shot.get("qa_stage", 0) > 0:
            # QA ladder override (simplified prompt / manual edit)
            return shot["prompt"], shot.get("negative") or vb.negative(sjson)
        return vb.build(sjson, state, is_i2v, lora_names), vb.negative(sjson)

    # ---------------------------------------------------- LTX-2.5 / H3 units
    def _render_unit(self, shot: dict, pid: str) -> None:
        """Render the unit `shot` starts (movie_prompts.unit_for): one clip, or
        a seamless take of CONTINUE shots, with exactly the request the Video
        Studio page sends (movie_request: the cast as references with names
        and looks, the start image or the previous clip's last frame, the
        page's size and frames, no negative, the model's own steps), refused
        by the same prompt check, with the prompts written for it
        (movie_prompts). A take is cut back into one clip per shot, and
        renders whole: a retake inside it renders the take again under its
        chain id, where the shots before the retaken one reuse their steps.

        A unit without ready prompts gets them written now when an AI is set
        up; otherwise, or when the AI cannot write them, its shots still to
        render wait (status "prompt") with the reason. A failure retries like
        any shot: a transient one without using the budget, any other uses
        one attempt and keeps the seeds, so a take reuses its finished steps."""
        import movie_prompts
        import movie_takes
        import opening_frames
        from bible import Bible
        bible = Bible(pid)
        unit = movie_prompts.unit_for(self.db, pid, shot["shot_id"], self.cfg)
        if unit is None:
            msg = "the shot's model changed while it was queued: queue it again"
            self.db.set_status(shot["shot_id"], "review", error=msg, project=pid)
            return
        tag = (f"take {movie_takes.take_id(pid, unit.model, unit.sids)[:8]} "
               f"({' -> '.join(unit.sids)})" if unit.chain else unit.sids[0])
        try:
            opening_frames.ensure(self.db, bible, unit, self.cfg)
            req = movie_prompts.request_for(self.db, bible, unit, planning=False, cfg=self.cfg)
            prompts, why = movie_prompts.prompts_for(self.db, req)
            if prompts is None and movie_prompts.ai_ready(self.cfg):
                log.info(f"{tag}: writing its prompts ({next(iter(why.values()), '')})")
                # written for the request with its opening frame (the AI
                # writes the frame's prompt too), which is made next
                res = movie_prompts.write_unit(self.db, bible, self._project(pid), unit,
                                               cfg=self.cfg, stop=lambda: not self.running)
                if not self.running:
                    return                    # stopped while writing: the unit stays queued
                opening_frames.ensure(self.db, bible, unit, self.cfg)
                req = movie_prompts.request_for(self.db, bible, unit, planning=False, cfg=self.cfg)
                prompts, why = movie_prompts.prompts_for(self.db, req)
                if prompts is None and not res.get("ok"):
                    why = {s: f"the AI could not write it ({res.get('error')})" for s in why}
            if prompts is None:
                self._wait_for_prompts(unit, why)
                return
            refused = req.refusal(prompts)
            if refused:
                self._wait_for_prompts(unit, {s: refused["error"] for s in unit.sids})
                return
        except movie_prompts.Unreachable as e:
            self._wait_for_prompts(unit, {s: f"{movie_prompts.UNREACHED} ({e})" for s in unit.sids})
            return
        except movie_prompts.WriteError as e:
            self._wait_for_prompts(unit, {s: f"the AI could not write it ({e})" for s in unit.sids})
            return
        except Exception as e:  # noqa: BLE001
            self._unit_failed(unit, tag, e)
            return
        # every shot keeps its own seed: a retried unit (failed or transient)
        # renders with the same ones, so the chain engine reuses every finished
        # step; a retake moves only its shot's (the rerender endpoint and the
        # QA ladder), so in a take the steps before it are reused too
        seeds = [int(r["seed"]) if r.get("seed") is not None else self.db.stable_seed(r)
                 for r in unit.rows]
        if unit.chain:
            self._render_chain(unit, req, prompts, seeds, tag)
        else:
            self._render_clip(unit, req, prompts[0], seeds[0], tag)

    def _project(self, pid: str) -> dict:
        from projects import ProjectStore
        return ProjectStore(self.db).get(pid) or {"id": pid}

    @staticmethod
    def _mode(req) -> str:
        """The page's mode: Start from Image when there is a start image, else
        text (references still ride the model's reference graph)."""
        import video_models
        modes = (video_models.VIDEO_MODELS.get(req.model) or {}).get("supported_modes") or []
        return "i2v" if req.start_image or "t2v" not in modes else "t2v"

    def _wait_for_prompts(self, unit, why: dict[str, str]) -> None:
        """The unit's shots still to render wait (status "prompt") until the
        prompts are written or fixed (movie_prompts.release_waiting queues them
        again); its rendered shots keep their clips meanwhile."""
        import movie_prompts
        first_sid, first = next(iter(why.items()), (unit.sids[0], "no prompt yet"))
        for sid in movie_prompts.to_render(unit):
            msg = why.get(sid) or f"it renders in one take with {first_sid}, whose prompt is not ready"
            self.db.set_status(sid, movie_prompts.WAITING, error=f"needs its prompt: {msg}"[:500],
                               project=unit.pid)
        self._emit({"shot_id": unit.sids[0], "event": "prompt", "shots": unit.sids, "why": first})
        log.info(f"{' -> '.join(unit.sids)}: waiting for prompts ({first_sid}: {first})")

    def _render_clip(self, unit, req, prompt: str, seed: int, tag: str) -> None:
        """One shot: Video Studio Generate with one prompt."""
        import video_models
        import video_studio
        pid, row, sjson = unit.pid, unit.head, unit.sjsons()[0]
        sid = row["shot_id"]
        entry = video_models.VIDEO_MODELS.get(req.model) or {}
        self.db.set_status(sid, "rendering", seed=seed, prompt=prompt, negative="", project=pid)
        t0 = time.time()
        try:
            dfr = sjson.get("dfr")
            rec = video_studio.start(
                req.model, prompt, mode=self._mode(req), negative="", seed=seed,
                width=req.width, height=req.height, frames=req.frames,
                references=req.references, start_image=req.start_image,
                continuation=req.continuation, dfr=bool(dfr) if dfr is not None else None,
                worker="local")
            log.info(f"{tag}: rendering on {req.model}, {req.opening}"
                     + (f" from the {req.start_source}" if req.start_source else ""))
            job = self._wait_job(rec["id"], int(entry.get("timeout_s") or 3600))
            if job.get("status") != "completed":
                raise RuntimeError(f"render failed: {job.get('error') or 'unknown error'}")
            outs = job.get("outputs") or []
            path = Path(job.get("output_dir") or (ROOT / "output" / "videostudio")) / outs[0] if outs else None
            if path is None or not path.is_file():
                raise RuntimeError(f"the render completed without its video ({outs[:1]})")
        except Exception as e:  # noqa: BLE001
            self._unit_failed(unit, tag, e)
            return
        prov = self._provenance(req, sjson, 0)
        if job.get("continuation"):
            prov["continuation_frame"] = job["continuation"]
        self.db.set_json(sid, prov, project=pid)
        self._delivered(row, prov, path, prompt, time.time() - t0)
        self._cascade(unit)
        log.info(f"{tag}: rendered in {job.get('seconds')}s")

    def _render_chain(self, unit, req, prompts: list[str], seeds: list[int], tag: str) -> None:
        """A seamless take: Video Studio Generate with one prompt per shot (one
        chain under one broker lease, each shot's own seed), cut back at its
        seams into one clip per shot. The take id is the shots', so a re-run
        (a retry, a retake inside it) reuses every step whose seed, prompt and
        inputs are unchanged; every shot is delivered again, because the
        shot before a retaken one ends in the joined decode of both."""
        import movie_prompts
        import movie_takes
        import video_models
        import video_studio
        pid, sids, sjsons = unit.pid, unit.sids, unit.sjsons()
        entry = video_models.VIDEO_MODELS.get(req.model) or {}
        cid = movie_takes.take_id(pid, req.model, sids)
        todo = movie_prompts.to_render(unit)
        t0 = time.time()
        try:
            for i, sid in enumerate(sids):
                if sid in todo:
                    self.db.set_status(sid, "rendering", seed=seeds[i], prompt=prompts[i],
                                       negative="", project=pid)
            if video_studio.chain_running(cid):
                # an earlier wait gave up while the take still renders: wait for it
                log.info(f"{tag}: already rendering - waiting for it")
            else:
                again = [s for s in sids if s not in todo]
                log.info(f"{tag}: rendering {len(sids)} shots as one seamless {req.model} take, "
                         f"{req.opening}" + (f" from the {req.start_source}" if req.start_source else "")
                         + (f"; {', '.join(again)} reuse their finished steps" if again else ""))
                video_studio.start_chain(
                    req.model, prompts, mode=self._mode(req), negative="", seed=seeds[0],
                    width=req.width, height=req.height, frames=req.frames,
                    start_image=req.start_image, references=req.references, chain_id=cid,
                    origin={"project": pid, "shots": sids}, seeds=seeds)
            rec = self._wait_take(cid, 8 * len(sids) * int(entry.get("timeout_s") or 2700))
            out_dir = ROOT / "output" / "videostudio"
            clips = movie_takes.split_take(
                Path(rec["final_video"]), list(rec.get("seam_frames") or []),
                [out_dir / f"take_{cid[:8]}_{i + 1}_{re.sub(r'[^A-Za-z0-9_-]', '_', s)}.mp4"
                 for i, s in enumerate(sids)], ffmpeg_exe(self.cfg))
        except Exception as e:  # noqa: BLE001
            self._unit_failed(unit, tag, e)
            return
        render_s = (time.time() - t0) / len(sids)
        qa = rec.get("qa") or {}
        # the cast's identity and apparent age per link of the take (face_drift)
        links = list(((rec.get("cast") or {}).get("links")) or [])
        for i, (row, sj, clip) in enumerate(zip(unit.rows, sjsons, clips)):
            prov = self._provenance(req, sj, i)
            prov["take"] = {"id": cid, "index": i + 1, "shots": sids, "engine": rec.get("engine"),
                            "video": rec.get("final_video"), "seam_qa_passed": qa.get("passed")}
            faces = {k: v for k, v in next((lk for lk in links if lk.get("link") == i + 1), {}).items()
                     if k != "link"}
            if faces:
                prov["take"]["cast"] = faces
            self.db.set_json(row["shot_id"], prov, project=pid)
            self._delivered(row, prov, clip, prompts[i], render_s, take=cid)
        self._cascade(unit)
        log.info(f"{tag}: done in {render_s * len(sids):.0f}s, seam QA "
                 f"{'PASS' if qa.get('passed') else 'FAIL'}")

    @staticmethod
    def _provenance(req, sjson: dict, i: int) -> dict:
        """The shot record of a render: the model, the references, the request
        and how the shot opened (earlier renders' entries are replaced)."""
        prov = {k: v for k, v in sjson.items()
                if k not in ("take", "continuity_applied", "continuation_frame", "opening_frame",
                             "reference_warnings", "request", "applied_loras")}
        prov.update(applied_model=req.model, applied_references=req.references)
        if i:
            prov["continuity_applied"] = "CONTINUE (seamless take)"
            return prov
        prov["request"] = req.summary()
        if req.opening != "cut":
            prov["continuity_applied"] = {"continue": "CONTINUE",
                                          "match_action": "MATCH_ACTION (a new angle)"}[req.opening]
        if req.start_source == "opening frame":
            prov["opening_frame"] = req.start_image
        if req.warnings:
            prov["reference_warnings"] = list(req.warnings)
        return prov

    def _delivered(self, row: dict, prov: dict, clip: Path, prompt: str, render_s: float,
                   take: str | None = None) -> None:
        """A shot's clip is in: its render record, its last frame (the next
        CONTINUE opens on it), then post + QA on the post worker."""
        sid, pid = row["shot_id"], row.get("project_id")
        self.db.record_render(sid, render_s, str(clip), project=pid)
        self._transient.pop(sid, None)
        self._emit({"shot_id": sid, "event": "rendered", "time_s": render_s, "video": str(clip),
                    **({"take": take} if take else {})})
        try:
            from continuity import extract_tail
            tail = extract_tail(clip, sid, pid=pid)
            if tail is not None:
                self.db.set_status(sid, "rendered", tail_frame=str(tail), project=pid)
        except Exception as e:  # noqa: BLE001
            log.warning(f"{sid}: tail extraction failed: {e}")
        self._post_queue.put((row, prov, clip, prompt))

    def _cascade(self, unit) -> None:
        """A shot that rendered before the unit and opens on its last frame (it
        CONTINUEs the unit's last shot, or is a match cut made from that
        frame) opens on a frame that no longer exists: it renders again."""
        import movie_prompts
        last = self.db.get(unit.sids[-1], unit.pid) or unit.rows[-1]
        for r in movie_prompts.follow_ons(self.db, last, unit.pid, self.cfg):
            if r.get("status") in _DONE:
                self.db.set_status(r["shot_id"], "queued", project=unit.pid,
                                   error=f"renders again: {last['shot_id']}, the shot it follows on "
                                         f"from, was rendered again")
                log.info(f"{r['shot_id']}: queued again (it follows on from {last['shot_id']})")

    def _wait_job(self, jid: str, timeout_s: int) -> dict:
        """Block until a Video Studio job ends. The job waits for the GPU (up
        to an hour) and then renders within its own deadline, so it always
        ends; the deadline here only guards against a hung job thread."""
        import video_studio
        deadline = time.time() + 3600 + int(timeout_s) + 900
        while True:
            job = video_studio.job(jid) or {}
            if job.get("status") in ("completed", "failed"):
                return job
            if time.time() > deadline:
                raise RuntimeError(f"render {jid} did not end (status {job.get('status')!r})")
            time.sleep(5)

    def _wait_take(self, cid: str, timeout_s: int) -> dict:
        """Block until the take's chain completes; raises with its error.

        Waits as long as this process renders the chain: every chain step has
        its own budget (chain_studio.StudioRunner.step_timeout), so a running
        chain ends by itself, and a fixed deadline would send a slow but
        healthy take to review with its delivery never cut. `timeout_s` only
        guards against a hung chain thread (the caller passes the chain's
        worst case: drafts with a re-roll, finishes and the whole-take steps)."""
        import video_studio
        deadline = time.time() + timeout_s
        while True:
            running = video_studio.chain_running(cid)     # before the record: no race
            rec = video_studio.get_chain(cid) or {}
            if rec.get("status") == "completed":
                if not rec.get("final_video") or not Path(rec["final_video"]).is_file():
                    raise RuntimeError("the take completed without a delivery video")
                return rec
            if rec.get("status") == "failed":
                raise RuntimeError(f"take failed: {rec.get('error') or 'unknown error'}")
            if not running:
                raise RuntimeError(f"take {cid[:8]} is not rendering (status {rec.get('status')!r})")
            if time.time() > deadline:
                raise RuntimeError(f"take {cid[:8]} still rendering after {timeout_s}s")
            time.sleep(10)

    def _unit_failed(self, unit, tag: str, e: Exception) -> None:
        """A failed unit's shots still to render go back to the queue (same
        seeds: the retry resumes a take), or to review once the tightest
        budget among them is spent; its rendered shots keep their clips.
        Transient failures (a server restarting) do not use the budget."""
        import movie_prompts
        todo = set(movie_prompts.to_render(unit)) or set(unit.sids)
        run = [r for r in unit.rows if r["shot_id"] in todo]
        pid, sids = unit.pid, [r["shot_id"] for r in run]
        self.last_error = str(e)
        transient = "10061" in str(e) or "refused" in str(e) or isinstance(e, TimeoutError)
        if transient:
            n = self._transient.get(sids[0], 0) + 1
            self._transient[sids[0]] = n
            if n <= 60:
                log.warning(f"{tag}: transient failure ({e}) - retrying in 30s ({n}/60)")
                time.sleep(30)
                for sid in sids:
                    self.db.set_status(sid, "queued", project=pid)
                return
            self._transient.pop(sids[0], None)
            e = RuntimeError(f"comfy unreachable after {n} transient retries")
        attempts = max(self.db.increment_attempts(sid, project=pid) for sid in sids)
        budget = min(self.cfg["qa"]["max_retries"].get(r.get("tier") or "standard", 2) for r in run)
        status = "review" if attempts >= budget else "queued"
        for sid in sids:
            self.db.set_status(sid, status, error=str(e) if status == "review" else None, project=pid)
            self._emit({"shot_id": sid, "event": "error", "error": str(e), "attempts": attempts})
        log.error(f"{tag} failed ({attempts}/{budget}): {e}")

    def _video_family(self, shot: dict, sjson: dict, pid: str) -> str:
        """The LTX-2.5 / MiniMax H3 model id this project shot renders with,
        or "" for the legacy Wan 2.2 path (movie_request.shot_model). Raises
        NoVideoModel when no LTX-2.5 / H3 model is installed."""
        from movie_request import shot_model
        return shot_model(self.db, shot, sjson, pid)

    def _project_post_on(self, pid: str) -> bool:
        """Per-project post-processing switch (upscale / face-fix / RIFE /
        grade / temporal). Defaults OFF: shots are delivered exactly as the
        model rendered them — the agreed LTX-2.5 workflow. Projects opt in
        through the Generate tab toggle (projects.post_processing)."""
        try:
            row = self.db.con.execute(
                "SELECT post_processing FROM projects WHERE id=?",
                (pid,)).fetchone()
            return bool(int(row[0])) if row is not None and row[0] is not None \
                else False
        except Exception:
            return False

    def _project_post_worker(self, pid: str) -> str:
        """Which worker PC runs this project's upscale/post stage ("" = local).

        When set, the post pipeline sends the rendered clip to that worker
        (RIFE + RealESRGAN x4 -> mp4 back) and continues the remaining local
        steps. A missing/offline worker falls back to the local chain inside
        the post pipeline — a worker can never block a render.
        """
        try:
            row = self.db.con.execute(
                "SELECT post_worker FROM projects WHERE id=?",
                (pid,)).fetchone()
            return str((row[0] if row else "") or "").strip()
        except Exception:
            return ""

    def _pick_keyframe(self, shot: dict):
        """Keyframe for I2V: main character's roster keyframe, else location still."""
        from pathlib import Path
        from core import ROOT
        for c in shot.get("characters", []):
            if c.get("tier") == "crowd":
                continue
            for cand in (ROOT / "roster" / "keyframes" / c["id"]).glob("*.png"):
                return cand
            break
        loc = shot.get("location")
        if loc:
            for cand in (ROOT / "roster" / "locations" / loc).glob("*.png"):
                return cand
            for cand in (ROOT / "bible" / "locations").glob(f"{loc}*.png"):
                return cand
        return None

    def _set_start_image(self, workflow: dict, fname: str) -> dict:
        import copy
        wf = copy.deepcopy(workflow)
        for n in wf.values():
            if n["class_type"] == "LoadImage":
                n["inputs"]["image"] = fname
        return wf

    def _resize_keyframe(self, src, w: int, h: int):
        """Scale a keyframe to the exact target resolution (I2V latent match)."""
        from core import ROOT
        import subprocess
        import uuid
        dst = ROOT / "temp" / f"kf_{uuid.uuid4().hex[:8]}_{w}x{h}.png"
        ff = ffmpeg_exe(self.cfg)
        subprocess.run([ff, "-y", "-v", "error", "-i", str(src),
                        "-vf", f"scale={w}:{h}:force_original_aspect_ratio="
                               f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                        str(dst)], capture_output=True)
        return dst if dst.exists() else src

    def _harvest_keyframe(self, sjson: dict, video: Any) -> None:
        """Extract frame 0 of a good render as the character's I2V keyframe.
        Only for identity-locked characters, first come
        first served — enables standard-tier I2V for later shots."""
        from core import ROOT
        try:
            char = next((c for c in sjson.get("characters", [])
                         if c.get("tier") in ("main", "supporting")), None)
            if char is None:
                return
            kf_dir = ROOT / "roster" / "keyframes" / char["id"]
            kf_dir.mkdir(parents=True, exist_ok=True)
            dest = kf_dir / "front.png"
            if dest.exists():
                return
            ff = ffmpeg_exe(self.cfg)
            import subprocess
            subprocess.run([ff, "-y", "-v", "error", "-i", str(video),
                            "-frames:v", "1", str(dest)],
                           capture_output=True)
            if dest.exists() and dest.stat().st_size > 1000:
                log.info(f"keyframe harvested: {dest}")
        except Exception as e:
            log.warning(f"keyframe harvest failed: {e}")

    def _post_worker(self) -> None:
        """Pipelined post+QA: processes completed renders in the background
        while the render loop continues."""
        while True:
            try:
                shot, sjson, video, prompt = self._post_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.db.set_status(shot["shot_id"], "post",
                                   project=shot.get("project_id"))
                if not (shot.get("project_id")
                        or sjson.get("project_id")):
                    # legacy shots only: harvested frames must never become
                    # canonical roster identity
                    self._harvest_keyframe(sjson, video)
                self._post_and_qa(shot, sjson, video, prompt)
            except Exception as e:
                log.error(f"post worker error for {shot['shot_id']}: {e}")

    def _post_and_qa(self, shot: dict, sjson: dict, video: Any,
                     prompt: str = "") -> None:
        """Automation (settings.automation.auto_post): post pipeline -> QA ->
        pass/retry/review. Gated behind config so the bake-off can disable it."""
        cfg = self.cfg
        if not cfg.get("automation", {}).get("auto_post", True):
            self.db.set_status(shot["shot_id"], "passed",
                               project=shot.get("project_id"))
            return
        sid = shot["shot_id"]
        pid = shot.get("project_id") or sjson.get("project_id")
        # per-project post-processing switch: default OFF — the raw render is
        # the deliverable (LTX-2.5 native output is final; the upscale chain
        # belongs to the 832x480 Wan profile and is opt-in per project).
        if pid and not self._project_post_on(pid):
            self._finalize_raw(shot, sjson, Path(str(video)), prompt)
            return
        try:
            from post import PostPipeline
            post_worker = self._project_post_worker(pid) if pid else ""
            final = PostPipeline(cfg).process(sjson, Path(str(video)), {},
                                              worker=post_worker)
            # store the finished (upscaled/graded) clip the moment it exists —
            # the shot card shows the RAW render immediately and swaps the
            # main box to this final while QA runs
            try:
                self.db.set_status(sid, "post", final_path=str(final),
                                   project=pid)
            except Exception as e:  # noqa: BLE001
                log.warning(f"{sid}: final_path not stored: {e}")
            # tail continuity frame — extracted by post.py
            # into a per-project namespace (same shot_ids exist across
            # projects and must never collide)
            from continuity import _ns
            tail = ROOT / "shots" / "tails" / _ns(pid) / \
                f"{sid}_tail.png"
            tail_frame = str(tail) if tail.exists() else None

            if pid:
                from qa_director import QADirector
                qa = QADirector(cfg).evaluate(sjson, str(final), prompt, refs=self._cast_portraits(sjson))
            else:
                from face_qa import FaceQA
                qa_shot = {**sjson, "prompt": prompt}
                qa = FaceQA(cfg).evaluate(qa_shot, str(final))
            self._emit({"shot_id": sid, "event": "qa",
                        "passed": qa["passed"],
                        "metrics": {k: v for k, v in qa.items()
                                    if isinstance(v, (int, float, bool))}})
            if qa["passed"]:
                self.db.record_qa(sid, True, qa.get("clip"), qa.get("face"),
                                  qa.get("motion"), project=pid)
                if tail_frame:
                    self.db.set_status(sid, "passed", tail_frame=tail_frame,
                                       project=pid)
                return
            if pid and sjson.get("applied_model") and qa.get("face_fail"):
                # a shot that lost the cast goes to review with its score, not
                # into retakes (in a take, a retake renders the shots after it)
                self.db.set_status(sid, "review", qa_notes=str(qa),
                                   tail_frame=tail_frame, project=pid)
                return
            # QA failed -> intelligent retry ladder (project shots) or
            # plain next-seed retry (legacy)
            attempts = self.db.increment_attempts(sid, project=pid)
            budget = cfg["qa"]["max_retries"].get(shot.get("tier", "standard"),
                                                  2)
            if attempts >= budget:
                self.db.set_status(sid, "review", qa_notes=str(qa),
                                   tail_frame=tail_frame, project=pid)
                return
            if pid and sjson.get("applied_model"):
                # LTX-2.5 / H3: the prompt is the one written for the shot,
                # never a template; a retake takes the next seed (with a take,
                # the shots after it in the take render again with it)
                row = self.db.get(sid, pid) or shot
                seed = int(row["seed"]) if row.get("seed") is not None else self.db.stable_seed(row)
                self.db.set_status(sid, "queued", project=pid, seed=(seed + REROLL_SEED) % (2 ** 31),
                                   qa_stage=int(shot.get("qa_stage") or 0) + 1)
            elif pid:
                from qa_director import QADirector
                from keyframe_gen import KeyframeGen
                stage = int(shot.get("qa_stage") or 0)
                action = QADirector(cfg).next_retry_action(
                    {**sjson, "qa_stage": stage}, qa)
                if action["action"] == "review":
                    # ladder exhausted before the retry budget
                    self.db.set_status(sid, "review", qa_notes=str(qa),
                                       tail_frame=tail_frame, project=pid)
                    return
                new_stage = stage + 1
                kw: dict[str, Any] = {}
                if action["action"] == "simplify_prompt" and \
                        action.get("prompt_hint"):
                    kw["prompt"] = action["prompt_hint"]
                if action["action"] == "regen_keyframe":
                    try:
                        from bible import Bible
                        from lora_video import VideoLoraRegistry
                        reg = VideoLoraRegistry(cfg)
                        reg.scan()
                        from lora_video import resolve_chain
                        chain = resolve_chain(sjson, Bible(pid), reg,
                                              is_i2v=True)
                        KeyframeGen(cfg).generate_candidates(
                            sjson, Bible(pid), chain, count=1)
                    except Exception as e:
                        log.warning(f"{sid}: keyframe regen failed: {e}")
                kw["qa_stage"] = new_stage
                kw["seed"] = self.db.retry_seed(sjson, attempts)
                self.db.set_status(sid, "queued", project=pid, **kw)
            else:
                self.db.set_status(sid, "queued",
                                   seed=self.db.retry_seed(sjson, attempts))
        except Exception as e:
            log.warning(f"{sid} post/qa failed, keeping raw: {e}")
            # the raw render is kept as the deliverable; say so on the card
            self.db.set_status(sid, "passed", project=pid,
                               qa_notes=f"post/QA skipped: {e}"[:500])

    def _finalize_raw(self, shot: dict, sjson: dict, video: Path,
                      prompt: str = "") -> None:
        """No-post completion path: QA the raw render, pass or flag it.

        Nothing is re-encoded, upscaled or graded — the video the model
        produced is the deliverable (the agreed LTX-2.5 workflow). A failed
        QA never triggers a re-render here: the shot is flagged for review
        and the operator decides.
        """
        cfg = self.cfg
        sid = shot["shot_id"]
        pid = shot.get("project_id") or sjson.get("project_id")
        # Continuity tail: one extracted frame, no re-encode. Every family
        # gets one — an LTX/H3 CONTINUE shot opens on the previous shot's
        # final frame (movie_request).
        tail_frame = None
        try:
            from continuity import extract_tail
            tail = extract_tail(video, sid, pid=pid)
            tail_frame = str(tail) if tail else None
        except Exception as e:
            log.warning(f"{sid}: tail extraction failed: {e}")
        try:
            from qa_director import QADirector
            qa = QADirector(cfg).evaluate(sjson, str(video), prompt, refs=self._cast_portraits(sjson))
        except Exception as e:
            log.warning(f"{sid} raw QA failed, keeping raw: {e}")
            self.db.set_status(sid, "passed", project=pid)
            return
        self._emit({"shot_id": sid, "event": "qa",
                    "passed": qa["passed"],
                    "metrics": {k: v for k, v in qa.items()
                                if isinstance(v, (int, float, bool))}})
        if qa["passed"]:
            self.db.record_qa(sid, True, qa.get("clip"), qa.get("face"),
                              qa.get("motion"), project=pid)
            if tail_frame:
                self.db.set_status(sid, "passed", tail_frame=tail_frame,
                                   project=pid)
            log.info(f"{sid}: raw render passed QA (post-processing off)")
        else:
            self.db.set_status(sid, "review", qa_notes=str(qa),
                               tail_frame=tail_frame, project=pid)
            log.info(f"{sid}: raw render flagged for review (post-processing off)")

    @staticmethod
    def _cast_portraits(sjson: dict) -> list[str]:
        """The identity stills of the people an LTX-2.5 / H3 shot rendered
        with (its "character:" references), for the QA's likeness check;
        empty for other shots."""
        if not sjson.get("applied_model"):
            return []
        pid = str(sjson.get("project_id") or "")
        try:
            from bible import Bible
            chars = Bible(pid).characters() or {} if pid else {}
        except Exception:  # noqa: BLE001 - the request's own still is used then
            chars = {}
        from ltx_refs import character_refs
        out: list[str] = []
        for r in sjson.get("applied_references") or []:
            parts = str(r.get("source") or "").split(":")
            if parts[0] != "character" or len(parts) < 2:
                continue
            # the identity still, not a loadout still where the face is small
            own = character_refs(pid, parts[1], chars.get(parts[1])) if chars.get(parts[1]) else []
            p = str(own[0] if own else r.get("path") or "")
            if p and p not in out and Path(p).is_file():
                out.append(p)
        return out

    def _fetch_output(self, result: dict, client=None) -> Any:
        from core import ROOT
        out = result.get("outputs", {})
        for node_id, node_out in out.items():
            videos = (node_out.get("images") or node_out.get("gifs")
                      or node_out.get("videos") or [])
            if not videos:
                continue
            v = videos[0]
            sub = v.get("subfolder", "")
            name = v["filename"]
            dest = ROOT / "shots" / "video" / name
            return (client or self.renderer.client).download(sub, name, dest)
        raise RuntimeError("no video output in workflow result")

    def _emit(self, evt: dict) -> None:
        for cb in self.job_callbacks:
            try:
                cb(evt)
            except Exception:
                pass

