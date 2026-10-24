"""Orchestrator + queue. Tier routing, pipelined post
(post of shot N runs while shot N+1 renders), retries, resume-safe."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core import ROOT, ffmpeg_exe, load_config, log_setup
from linter import Linter
from prompts import PromptBuilder
from renderer import Renderer
from shots_db import ShotsDB

log = log_setup("orchestrator")


class NoVideoModel(RuntimeError):
    """A project shot has no installed model to render with."""


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
        rows = self.db.all(status="queued", project=self.project_filter)
        if rows:
            rows.sort(key=lambda r: (r["scene"], r["shot_id"]))
            return rows[0]
        rows = self.db.all(status="pending", project=self.project_filter)
        if rows:
            rows.sort(key=lambda r: (r["scene"], r["shot_id"]))
            s = rows[0]
            self.db.set_status(s["shot_id"], "queued",
                               project=s.get("project_id"))
            return self.db.get(s["shot_id"], s.get("project_id"))
        # retries
        rows = self.db.all(status="failed", project=self.project_filter)
        if rows:
            rows.sort(key=lambda r: r["attempts"])
            return rows[0]
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

        # ---- project path: continuity keyframes, LoRA chain, profiles ----
        lora_names: list[str] = []
        profile = "production_fast"
        p_bible = None
        # the LTX-2.5 / H3 model id; "" = the legacy Wan 2.2 path, which only
        # a project that picks Wan and the API-only shots without a project
        # (/api/shots/ingest) still render on
        family = ""
        if is_project:
            from bible import Bible
            from continuity import start_image_for
            from lora_video import pick_profile
            p_bible = Bible(pid)
            # LTX-2.5 shots are reference-driven: the Wan continuity tail is
            # deliberately NOT resolved or injected for them (different model,
            # different graph, and the tail would fight the cast references).
            try:
                family = self._video_family(shot, sjson, pid)
            except NoVideoModel as e:
                self.last_error = str(e)
                self.db.set_status(sid, "review", error=str(e), project=pid)
                self._emit({"shot_id": sid, "event": "error", "error": str(e),
                            "attempts": 0})
                log.error(f"{sid}: {e} - sent to review")
                return
            prev_row = None
            prev_id = sjson.get("prev_shot")
            if prev_id:
                prev_row = self.db.get(prev_id, pid)
            start = None
            if not force_t2v and not family:
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

        # LTX-2.5 / MiniMax H3 shots render at the model's own native
        # resolution on the model's own frame grid. The movie pipeline's Wan
        # profile ladder (832x480 stage-1 + AI upscale in post) must NOT apply
        # to them: LTX renders 1920x1088 directly (refs at stage 1, DFR), and
        # H3 hero shots render 1920x1088 via the two-phase latent-upscale
        # pipeline (H3 lab record PROVEN_CONFIGS_H3) — output is final for both.
        if is_project and family:
            nw, nh = self._family_dims(family, w, h)
            if (nw, nh) != (w, h):
                log.info(f"{sid}: {family} render resolution {w}x{h} -> "
                         f"{nw}x{nh} (profile '{profile}')")
            w, h = nw, nh
            nframes = self._family_frames(family, frames)
            if nframes != frames:
                log.info(f"{sid}: {family} frames {frames} -> {nframes}")
            frames = nframes
            # a run of CONTINUE shots on a seamless model is ONE camera take:
            # it renders as a seamless chain and every shot gets its own cut
            run = self._take_run(shot, pid, family)
            if len(run) > 1:
                self._render_take(run, pid, family, w, h, frames)
                return

        # ----------------------------------------------------------- prompt
        if is_project:
            prompt, negative = self._project_prompt(
                shot, sjson, state, p_bible, family,
                is_i2v=start is not None and not force_t2v, lora_names=lora_names)
        else:
            prompt = self.builder.build(sjson, state)
            negative = self.builder.negative(sjson)

        # --------------------------------------------------------- workflow
        # LTX-2.5 shots take a different render path (second ComfyUI instance,
        # cast reference images, no start-image/tail chaining). A project that
        # picks the legacy Wan 2.2 model keeps its templates byte-identically.
        # family was resolved with the project block; shots without a project
        # always use the legacy Wan path.
        if is_project and not family:
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
        elif not is_project:
            workflow = self.renderer.build_workflow(
                tier=tier, prompt=prompt, negative=negative, seed=seed,
                width=w, height=h, steps=steps, frames=frames, shot_id=sid,
                sjson=sjson)
        else:
            workflow = None      # LTX: video_studio builds its own graph

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

        if start is not None and not family:
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
            if is_project and family:
                video = self._render_family_shot(
                    sid, sjson, prompt, negative, w, h, frames, steps, seed,
                    family, pid)
            else:
                prompt_id = shot_client.queue_prompt(workflow)
                log.info(f"render {sid} ({tier}{'/' + profile if is_project else ''})"
                         f" submitted: {prompt_id}"
                         + (f" @ {shot_worker}" if shot_worker != "local" else ""))
                result = shot_client.wait_for(prompt_id, timeout_s=2400)
                if result is None:
                    raise TimeoutError(f"{sid} render timeout")
                video = self._fetch_output(result, client=shot_client)
            render_s = time.time() - t0
            self.db.record_render(sid, render_s, video,
                                  project=pid if is_project else None)
            self._transient.pop(sid, None)
            self._emit({"shot_id": sid, "event": "rendered", "time_s": render_s,
                        "video": str(video)})
            # CONTINUITY FIRST: extract the tail frame from the RAW render now,
            # before the clip is handed to post. With post ON and a worker PC
            # upscaling, the tail used to exist only after the worker finished
            # — the next shot then had no start frame and the chain stalled.
            # EVERY family gets a tail now: LTX/H3 shots honour CONTINUE /
            # MATCH_ACTION by opening on the previous shot's final frame
            # (H3 frame-0 anchor / LTX MSR slot), so the tail is their
            # continuity currency too — not just Wan's start image.
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
                        family: str, is_i2v: bool,
                        lora_names: list[str]) -> tuple[str, str]:
        """(prompt, negative) of a project shot before the family recipes."""
        from videoprompt import VideoPromptBuilder
        vb = VideoPromptBuilder(p_bible, self.db)
        if shot.get("prompt") and shot.get("qa_stage", 0) > 0:
            # QA ladder override (simplified prompt / manual edit)
            return shot["prompt"], shot.get("negative") or vb.negative(sjson)
        if self._is_h3(family):
            # MiniMax H3 prompts follow docs/H3_PROMPT_GUIDE.md — a structured
            # text (integrated_multimodal_description + overall_soundscape +
            # non_diegetic_music), NOT the comma prompt the LTX/Wan builders
            # produce. CFG 1.0 (distilled) ignores negatives, so none is passed.
            return vb.build_h3(sjson, state), ""
        return vb.build(sjson, state, is_i2v, lora_names), vb.negative(sjson)

    @staticmethod
    def _is_h3(mid: str) -> bool:
        try:
            import video_models
            return str((video_models.VIDEO_MODELS.get(mid) or {}).get("family", "")).startswith("h3")
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------- continuous takes
    def _take_run(self, shot: dict, pid: str, mid: str) -> list[dict]:
        """`shot` and the CONTINUE shots that render with it as one seamless
        take (movie_takes.plan_take); [shot] when it renders alone."""
        import movie_takes
        import video_studio
        on, max_shots = movie_takes.settings(self.cfg)
        if not (on and self._project_takes_on(pid) and video_studio.seamless_engine(mid)):
            return [shot]
        try:
            rows = self.db.all(scene=shot.get("scene") or None, project=pid)
            return movie_takes.plan_take(
                shot, rows, lambda r: self._video_family(r, r.get("json") or {}, pid), max_shots)
        except Exception as e:  # noqa: BLE001 - planning must never block the shot itself
            log.warning(f"{shot['shot_id']}: take planning failed ({e}) - rendering the shot alone")
            return [shot]

    def _render_take(self, run: list[dict], pid: str, mid: str, w: int, h: int,
                     frames: int) -> None:
        """Render a run of CONTINUE shots as one seamless take (movie_takes):
        one chain under one broker lease (video_studio.start_chain), one
        reference set and opening frame for the take, then the delivered take
        is cut at its seams and every shot continues alone (render record, tail
        frame, post + QA). The engines sample with their own recipes (H3: the
        tier's registry schedule), never the movie tier's step count.

        Failures match a single shot's: a transient one retries without using
        the budget; otherwise every shot of the take uses one attempt and the
        take is re-queued with the same seeds under its stable chain id. The
        retry reuses every finished step: the references are uploaded again
        under content-derived names (renderer.upload_name), so the step
        digests stay the same."""
        import movie_takes
        import video_models
        import video_studio
        from bible import Bible
        from ltx_refs import resolve_reference_set

        sids = [r["shot_id"] for r in run]
        sjsons = [r.get("json") or {} for r in run]
        entry = video_models.VIDEO_MODELS.get(mid) or {}
        fam = str(entry.get("family") or "")
        h3 = fam.startswith("h3")
        bible = Bible(pid)
        cid = movie_takes.take_id(pid, mid, sids)
        tag = f"take {cid[:8]} ({' -> '.join(sids)})"
        t0 = time.time()
        try:
            # one reference set and cast line for the take, and the opening
            # frame: the previous shot's tail when the take itself continues one
            merged = movie_takes.take_shot(sjsons)
            refs, warnings = resolve_reference_set(merged, bible, family=fam)
            refs = movie_takes.slot_sorted(refs)
            opening, tail = self._continuation_tail(sjsons[0], pid, warnings)
            start = Path(tail) if tail else None
            # a retried take (failed or transient) keeps the seeds of its last
            # attempt (stored on the head row), so the chain engine reuses every
            # finished step; assign_seed would drift with the scene's statuses
            head = run[0]
            retry = head.get("attempts") or self._transient.get(head["shot_id"])
            base_seed = (int(head["seed"]) if retry and head.get("seed") is not None
                         else self.db.assign_seed(sjsons[0]))
            prompts, negative = [], ""
            for i, (row, sj) in enumerate(zip(run, sjsons)):
                chain = [] if h3 else self._lora_chain(row, sj, bible, is_i2v=False)
                body, neg = self._project_prompt(
                    row, sj, self._scene_state(row["shot_id"], sj, pid), bible, mid, is_i2v=False,
                    lora_names=[c["name"] for c in chain if c.get("enabled", True)])
                if h3:
                    prompts.append(self._h3_take_prompt(body, refs, sj, bible, continues=i > 0))
                    continue
                legend = self._ltx_reference_legend(
                    ([{"source": "continuation:prev_tail"}] if start else []) + refs)
                body = self._ltx_prompt_recipe(body, sj)
                prompts.append(f"{legend} {body}" if legend else body)
                if i == 0:
                    negative = self._ltx_negative_recipe(neg)
            for i, sid in enumerate(sids):
                self.db.set_status(sid, "rendering", seed=base_seed + i, prompt=prompts[i],
                                   negative=negative, project=pid)
            if video_studio.chain_running(cid):
                # an earlier wait gave up while the take still renders: wait for it
                log.info(f"{tag}: already rendering - waiting for it")
            else:
                log.info(f"{tag}: rendering {len(run)} shots as one seamless {mid} take"
                         + (f", opening on {start.name}" if start else ""))
                video_studio.start_chain(
                    mid, prompts, mode="i2v" if (refs or start) else "t2v", negative=negative,
                    seed=base_seed, width=w, height=h, frames=frames,
                    references=movie_takes.take_cast(refs, bible, merged), chain_id=cid,
                    start_path=start, origin={"project": pid, "shots": sids})
            rec = self._wait_take(cid, 8 * len(run) * int(entry.get("timeout_s") or 2700))
            out_dir = ROOT / "output" / "videostudio"
            clips = movie_takes.split_take(
                Path(rec["final_video"]), list(rec.get("seam_frames") or []),
                [out_dir / f"take_{cid[:8]}_{i + 1}_{re.sub(r'[^A-Za-z0-9_-]', '_', s)}.mp4"
                 for i, s in enumerate(sids)], ffmpeg_exe(self.cfg))
        except Exception as e:  # noqa: BLE001
            self._take_failed(run, pid, tag, e)
            return

        render_s = (time.time() - t0) / len(run)
        qa = rec.get("qa") or {}
        for i, (row, sj, clip) in enumerate(zip(run, sjsons, clips)):
            sid = row["shot_id"]
            prov = dict(sj)
            prov.update(applied_model=mid, applied_references=refs,
                        take={"id": cid, "index": i + 1, "shots": sids, "engine": rec.get("engine"),
                              "video": rec.get("final_video"), "seam_qa_passed": qa.get("passed")})
            if i > 0:
                prov["continuity_applied"] = "CONTINUE (seamless take)"
            elif start is not None:
                prov["continuity_applied"] = opening
                prov["continuation_frame"] = str(start)
            if warnings:
                prov["reference_warnings"] = warnings
            self.db.set_json(sid, prov, project=pid)
            self.db.record_render(sid, render_s, str(clip), project=pid)
            self._transient.pop(sid, None)
            self._emit({"shot_id": sid, "event": "rendered", "time_s": render_s,
                        "video": str(clip), "take": cid})
            try:
                from continuity import extract_tail
                tail_png = extract_tail(clip, sid, pid=pid)
                if tail_png is not None:
                    self.db.set_status(sid, "rendered", tail_frame=str(tail_png), project=pid)
            except Exception as e:  # noqa: BLE001
                log.warning(f"{sid}: tail extraction failed: {e}")
            self._post_queue.put((row, prov, clip, prompts[i]))
        log.info(f"{tag}: done in {render_s * len(run):.0f}s, seam QA "
                 f"{'PASS' if qa.get('passed') else 'FAIL'}")

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

    def _take_failed(self, run: list[dict], pid: str, tag: str, e: Exception) -> None:
        """A failed take goes back to the queue whole (same seeds: the retry
        resumes it), or to review once the tightest budget among its shots is
        spent. Transient failures (server restarting) do not use the budget."""
        sids = [r["shot_id"] for r in run]
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
        or "" for the legacy Wan 2.2 path.

        Selection is per shot override first (`video_model` in the shot json),
        then the project's per-tier override, then its `video_family` column.
        No model, an unknown id or an unavailable one resolves to the studio
        default (video_models.default_model_id: LTX-2.5 s6 CQ) with a logged
        warning, so a stale id can never brick a project. Wan 2.2 is legacy:
        it renders only when the shot or project names an installed Wan
        entry. Raises NoVideoModel when no LTX-2.5 / H3 model is installed.
        """
        mid = str(shot.get("video_model") or sjson.get("video_model") or "")
        if not mid:
            try:
                row = self.db.con.execute(
                    "SELECT video_family, video_models FROM projects WHERE id=?",
                    (pid,)).fetchone()
                mid = str((row[0] if row else "") or "")
                # per-tier override: hero / standard / volume each may run a
                # different model (e.g. H3 for hero shots, LTX for the rest)
                try:
                    per_tier = json.loads((row[1] if row and len(row) > 1
                                           else "") or "{}")
                except Exception:  # noqa: BLE001
                    per_tier = {}
                tier = str(shot.get("tier") or "standard")
                mid = str((per_tier or {}).get(tier) or mid or "")
            except Exception:
                mid = ""
        import video_models
        if mid:
            try:
                st = video_models.model_status(mid)
            except Exception as e:  # noqa: BLE001
                st = {"available": False, "missing": [f"registry error: {e}"]}
            fam = str(st.get("family") or "")
            if st.get("available") and fam.startswith("wan"):
                return ""       # legacy Wan 2.2, picked explicitly
            if st.get("available") and (fam.startswith("ltx") or fam.startswith("h3")):
                return mid
            why = (f"unavailable ({st.get('missing') or st.get('reason')})"
                   if not st.get("available") else "not a movie model")
            log.warning(f"{pid}: video model '{mid}' is {why} - using the "
                        f"default model")
        default = video_models.default_model_id()
        if default:
            return default
        raise NoVideoModel(
            "no LTX-2.5 or MiniMax H3 model is installed (docs/setup/RESTORE_INSTRUCTIONS.md "
            "step 6), and the project does not pick the legacy Wan 2.2 model")

    # ------------------------------------------------- family render helpers
    def _family_dims(self, mid: str, w: int, h: int) -> tuple[int, int]:
        """The RENDER resolution for a non-Wan family.

        `defaults.width/height` wins when declared: MiniMax H3's hero tier runs
        its phase 1 at HALF the output (960x544 -> refine to 1920x1088), so the
        registry carries the phase-1 size while `resolution_limits.native`
        describes the final output. Without defaults, LTX-2.5 uses its native
        resolution (the historical behaviour).
        """
        try:
            import video_models
            st = video_models.model_status(mid)
            d = st.get("defaults") or {}
            dw, dh = d.get("width"), d.get("height")
            if dw and dh:
                return int(dw), int(dh)
        except Exception:
            pass
        return self._family_native_dims(mid, w, h)

    def _family_native_dims(self, mid: str, w: int, h: int) -> tuple[int, int]:
        """Nearest native resolution declared by the model's registry entry.

        The registry declares e.g. native: ["1920x1088"]; a shot whose aspect
        ratio matches within +-10% is rendered at that native size instead of
        the movie pipeline's Wan profile resolution. Anything else (vertical
        shots, panoramas) keeps the incoming dims untouched.
        """
        try:
            import video_models
            st = video_models.model_status(mid)
            names = (st.get("resolution_limits") or {}).get("native") or []
        except Exception:
            names = []
        if not h or not w:
            return w, h
        best = None
        for name in names:
            try:
                nw, nh = (int(x) for x in str(name).lower().split("x"))
            except Exception:
                continue
            if not nh:
                continue
            ratio = (nw / nh) / (w / h)
            if 0.9 <= ratio <= 1.1 and (best is None or abs(ratio - 1) < best[0]):
                best = (abs(ratio - 1), nw, nh)
        return (best[1], best[2]) if best else (w, h)

    def _family_frames(self, mid: str, frames: int) -> int:
        """Snap a frame count to the model's declared frame grid.

        LTX-2.5 uses N = 1 + 8k (floor snap, the historical behaviour). MiniMax
        H3 uses N = 5 + 17k and declares `frame_snap: "nearest"` - the movie
        pipeline's 5 s target (121 frames) sits between H3's 107 and 124, and
        the intended 5.17 s clip is 124, so H3 snaps to the NEAREST grid point.
        """
        try:
            import video_models
            st = video_models.model_status(mid)
            lim = (st.get("frame_limits") or {})
            snap = str(st.get("frame_snap") or (lim.get("snap") or "floor"))
        except Exception:
            lim, snap = {}, "floor"
        mx = int(lim.get("max", 121) or 121)
        stp = int(lim.get("step", 8) or 8)
        lo = int(lim.get("min", 9) or 9)
        n = min(int(frames), mx)
        if (n - lo) % stp:
            if snap == "nearest":
                n = lo + round((n - lo) / stp) * stp
            else:
                n = lo + ((n - lo) // stp) * stp
        return max(n, lo)

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

    def _project_takes_on(self, pid: str) -> bool:
        """Per-project seamless-takes switch (projects.seamless_takes, the
        Generate tab toggle). Defaults ON."""
        try:
            row = self.db.con.execute(
                "SELECT seamless_takes FROM projects WHERE id=?", (pid,)).fetchone()
            return bool(int(row[0])) if row is not None and row[0] is not None else True
        except Exception:  # noqa: BLE001 - a DB without the column predates the switch
            return True

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

    # "high shutter speed" was in the proven phrase, but it strips motion blur
    # and reads as stroboscopic/choppy at 24fps — one of the cues that makes a
    # render look animated. Natural motion blur keeps the real-time intent.
    _LTX_MOTION_PHRASE = ("natural real-time speed, natural motion blur, "
                          "live-action photography")
    # ---- LTX photographic translation (measured 2026-09-22, BENCH_2026-09-22)
    # Identical scene, refs, seed and 1920x1088 config:
    #   fantasy language  -> glossy CG game-art still ("Frozen" look)
    #   photographic rewrite -> real-location film still
    # The model's prior for fantasy nouns (magical city / rune-light / arcane)
    # IS illustration; translating the same scene into real-world equivalents
    # is the fix. LTX-ONLY: H3 renders the fantasy language photoreal already.
    _LTX_PHOTO_SUBS = (
        ("rune-lit", "lamp-lit"),
        ("rune-carved", "carved stone"),
        ("rune-light", "warm carved stone detail"),
        ("glowing runes", "carved stone patterns"),
        ("rune foundry", "stone workshop"),
        ("runes", "carved stone details"),
        ("magical city", "historic stone city"),
        ("magical", "grand"),
        ("royal palace", "grand stone palace"),
        ("arcane", "old stone"),
        ("ethereal", "soft"),
        ("celestial", "bright"),
        ("enchanted", "grand"),
        ("luminous", "bright"),
        ("floating structures", "tall towers"),
        ("crystalline", "clear"),
        ("sorceress", "woman in dark robes"),
        ("aura", "soft glow"),
    )
    _LTX_PHOTO_LEAD = ("live-action film still on a real location, real "
                       "actress, practical set, documentary-style location "
                       "cinematography — ")

    def _ltx_photographic(self, prompt: str) -> str:
        """Translate fantasy vocabulary into real-world photographic language
        and lead with a real-location anchor (LTX only)."""
        p = str(prompt or "")
        for a, b in self._LTX_PHOTO_SUBS:
            if a in p.lower():
                p = re.sub(re.escape(a), b, p, flags=re.IGNORECASE)
        if "real location" not in p.lower():
            p = self._LTX_PHOTO_LEAD + p
        return p
    _LTX_NEG_EXTRA = ("slow motion", "slow-mo", "frozen pose", "static",
                      "motionless", "stiff movement", "un-photorealistic",
                      "unrealistic", "cartoon", "anime", "cgi", "3d render",
                      "illustration", "plastic skin", "game art",
                      "concept art", "stylized", "oversaturated",
                      "doll-like", "wax figure", "video game",
                      "flat lighting", "smooth plastic")

    def _ltx_prompt_recipe(self, prompt: str, shot: dict | None = None) -> str:
        """LTX prompt recipe (LTX lab record PROVEN_CONFIGS, rules 4-5). LTX renders only
        — Wan prompts stay untouched.

        Motion-first: LTX conditions hardest on the prompt's LEADING tokens.
        The shared legacy assembly opens with film-stock/location preamble and
        buries the action behind it (measured: shots drifted to ambient
        posing — "stands at the entrance", "walks past..."). The primary
        action is therefore hoisted to the very front, unless it already
        leads. The identity block still carries it verbatim further in, which
        reinforces rather than contradicts (same words, earlier position).
        """
        p = (prompt or "").strip().rstrip(",")
        try:
            act = str((shot or {}).get("primary_action") or "").strip()
            if act and act.lower() not in p.lower()[:160]:
                p = act.rstrip(".,") + ", " + p
        except Exception:  # noqa: BLE001
            pass
        # photographic translation BEFORE the motion phrase so the real-world
        # anchor lands right after the action (leading tokens weight most)
        p = self._ltx_photographic(p)
        low = p.lower()
        if "real-time speed" not in low and "real time speed" not in low:
            p = (p + ", " + self._LTX_MOTION_PHRASE).strip(", ")
        return p

    def _ltx_negative_recipe(self, negative: str) -> str:
        """Append every missing term of the proven negative set
        (LTX lab record PROVEN_CONFIGS, rule 5). LTX renders only."""
        items = [x.strip() for x in (negative or "").split(",") if x.strip()]
        low = {x.lower() for x in items}
        for term in self._LTX_NEG_EXTRA:
            if term not in low:
                items.append(term)
                low.add(term)
        return ", ".join(items)

    def _h3_reference_legend(self, refs: list[dict]) -> str:
        """One-line <Picture N> role legend for H3 Ref2VA prompts.

        H3's ref_images fill sequentially, so <Picture 1..N> map to the order
        of `refs` — the legend is generated from the RESOLVED set (not from
        assumptions), which ensures the tags match the submission
        order. Roles follow the guide's explicit-role sentences.
        """
        parts: list[str] = []
        lead_done = False
        for i, r in enumerate(refs):
            src = str(r.get("source") or "")
            tag = f"<Picture {i + 1}>"
            if src.startswith("character:"):
                who = src.split(":")[1] if ":" in src else src
                if not lead_done:
                    parts.append(
                        f"{tag} is the lead character ({who}); face, hair, "
                        f"wardrobe and colors preserved exactly as referenced")
                    lead_done = True
                else:
                    parts.append(
                        f"{tag} is another character ({who}), identity "
                        f"preserved exactly")
            elif src.startswith("weapon:"):
                parts.append(f"{tag} shows the weapon's exact design and "
                             f"materials")
            elif src.startswith("power:"):
                parts.append(f"{tag} defines the power's look and VFX style")
            elif src.startswith("location:"):
                parts.append(f"{tag} is the environment")
            else:
                parts.append(f"{tag} is a reference image")
        if not parts:
            return ""
        return "Reference images: " + "; ".join(parts) + "."

    def _h3_ref2va_prompt(self, body: str, refs: list[dict],
                          shot: dict, bible, continues: bool = False) -> str:
        """Official H3 Ref2VA six-section prompt (ref-en guide).

        Field order: subject_definitions -> summary -> retention_analysis ->
        detailed_description -> overall_soundscape -> non_diegetic_music.
        Pictures of ONE character are GROUPED under a single <Subject N> — the
        official guide: several photos of one person grouped = one defended
        identity, ungrouped = lookalikes/drift. The frame-0 anchor is declared
        explicitly ("the shot begins from <Picture 1>") because a reference is
        not the first frame unless the prompt says so; `continues` (a later
        link of a seamless take, opening on the carried frames) says the shot
        carries on instead.
        """
        from ltx_refs import resolve_presentation
        chars = {}
        try:
            chars = bible.characters() or {}
        except Exception:  # noqa: BLE001
            chars = {}
        by_id = {str(c.get("id")): c for c in (shot.get("characters") or [])
                 if isinstance(c, dict)}
        # group refs: one subject per character (identity+outfit+weapon),
        # then power, then environment
        subjects: list[dict] = []
        idx_of: dict[str, int] = {}

        def _subject(key: str, label: str) -> dict:
            if key not in idx_of:
                idx_of[key] = len(subjects)
                subjects.append({"key": key, "label": label, "pics": [],
                                 "retention": ""})
            return subjects[idx_of[key]]

        pic_no = 0
        for r in refs:
            src = str(r.get("source") or "")
            pic_no += 1
            if src.startswith("character:"):
                parts = src.split(":")
                cid = parts[1] if len(parts) > 1 else "?"
                char = chars.get(cid) or {}
                name = str(char.get("name") or cid)
                sc = by_id.get(cid) or {}
                pres = resolve_presentation(char, sc, shot)
                feats = [str(char.get("body") or "").strip()]
                if pres.get("outfit"):
                    feats.append(f"wearing {pres['outfit']}")
                if pres.get("weapon_txt"):
                    feats.append(f"carrying {pres['weapon_txt']}")
                s = _subject(f"char:{cid}", name)
                s["pics"].append(pic_no)
                feats_txt = ", ".join(x for x in feats if x)
                s["text"] = (f"<Subject {idx_of[f'char:{cid}'] + 1}> is "
                             f"{name} in <Picture {pic_no}>"
                             + (f", {feats_txt}" if feats_txt else ""))
                # retention is CONDITIONAL on what the shot actually shows: a
                # weapon-less presentation must not ask the model to preserve
                # a weapon it is not supposed to carry.
                ret = "fully_preserved - face, hair and wardrobe colors"
                if pres.get("weapon_txt"):
                    ret += ", and the weapon's design and materials"
                s["retention"] = ret
            elif src.startswith("weapon:"):
                parts = src.split(":")
                cid = parts[1] if len(parts) > 1 else "?"
                wid = parts[2] if len(parts) > 2 else "weapon"
                char = chars.get(cid) or {}
                w = ((char.get("weapons") or {}).get(wid) or {})
                s = _subject(f"char:{cid}", str(char.get("name") or cid))
                s["pics"].append(pic_no)
                s.setdefault("extra", []).append(
                    f"<Picture {pic_no}> shows the exact design of "
                    f"{wid}: {str(w.get('descriptor') or wid)}")
            elif src.startswith("power:"):
                parts = src.split(":")
                pw = parts[2] if len(parts) > 2 else "power"
                lvl = next((x for x in parts if x.startswith("lv")), "")
                s = _subject(f"power:{pw}", f"the {pw} power")
                s["pics"].append(pic_no)
                s["text"] = (f"<Subject {idx_of[f'power:{pw}'] + 1}> is the "
                             f"{pw} power's manifestation in "
                             f"<Picture {pic_no}>"
                             + (f" at intensity {lvl[2:]}" if lvl else ""))
                s["retention"] = "fully_preserved - colors, materials and VFX look"
            elif src.startswith("location:"):
                parts = src.split(":")
                lid = parts[1] if len(parts) > 1 else "?"
                loc = (bible.locations() or {}).get(lid) or {}
                s = _subject(f"loc:{lid}", str(loc.get("descriptor") or lid))
                s["pics"].append(pic_no)
                s["text"] = (f"<Subject {idx_of[f'loc:{lid}'] + 1}> is the "
                             f"environment in <Picture {pic_no}>: "
                             f"{str(loc.get('descriptor') or lid)}")
                s["retention"] = "fully_preserved - layout, materials and lighting"

        if not subjects:
            return self._h3_reference_legend(refs)

        lines: list[str] = ["subject_definitions:"]
        for i, s in enumerate(subjects):
            base = s.get("text") or (f"<Subject {i + 1}> is defined by "
                                     f"<Picture {s['pics'][0]}>")
            if len(s["pics"]) > 1:
                base += (", further defined by "
                         + ", ".join(f"<Picture {n}>" for n in s["pics"][1:]))
            lines.append(base + ".")
            for extra in s.get("extra") or []:
                lines.append(extra + ".")

        # summary: one line, the task and the reference roles
        lead = subjects[0]["label"] if subjects else "the scene"
        n_char = sum(1 for s in subjects if s["key"].startswith("char:"))
        has_env = any(s["key"].startswith("loc:") for s in subjects)
        summary = (f"[reference generation] The target video follows {lead}"
                   + (f" and {n_char - 1} other character(s)" if n_char > 1 else "")
                   + (" in the referenced environment" if has_env else "")
                   + ".")
        lines += ["", "summary:", summary]

        lines += ["", "retention_analysis:"]
        for i, s in enumerate(subjects):
            if s.get("retention"):
                lines.append(f"<Subject {i + 1}>: {s['retention']}.")

        opening = ("the shot carries on from the previous moment without a cut."
                   if continues else "the shot begins from <Picture 1>.")
        lines += ["", "detailed_description:", opening, body]

        return "\n".join(lines)

    def _ltx_reference_legend(self, refs: list[dict]) -> str:
        """MSR usage-tip reference line for LTX prompts.

        The official MSR guidance: describe each reference in the prompt, use
        consistent labels ("Image 1", "Image 2"), and specify which reference
        provides the character, garment, object or background. LTX has no
        <Picture N> tags — plain "Image N" labels are the convention.
        """
        parts: list[str] = []
        lead_done = False
        for i, r in enumerate(refs):
            src = str(r.get("source") or "")
            tag = f"Image {i + 1}"
            if src.startswith("character:"):
                who = src.split(":")[1] if ":" in src else src
                if not lead_done:
                    parts.append(f"{tag} provides {who}'s face, hair and "
                                 f"outfit exactly as shown")
                    lead_done = True
                else:
                    parts.append(f"{tag} provides {who}'s identity")
            elif src.startswith("weapon:"):
                parts.append(f"{tag} provides the weapon's exact design and "
                             f"materials")
            elif src.startswith("power:"):
                parts.append(f"{tag} provides the power's look and VFX style")
            elif src.startswith("location:"):
                parts.append(f"{tag} provides the environment")
        if not parts:
            return ""
        return "References: " + "; ".join(parts) + "."

    def _continuation_tail(self, sjson: dict, pid: str,
                           warnings: list[str]) -> tuple[str, str | None]:
        """(effective continuity, the previous shot's tail frame or None).

        Continuity bridging is LLM-authored and per-shot overridable: a
        CONTINUE / MATCH_ACTION shot opens on the PREVIOUS shot's final frame.
        A scene/location change is always a hard cut, never a bridge, and a
        bridge without a tail frame renders as a cut; both are noted in
        `warnings`."""
        try:
            from shotspec import continuity_of
            mode = continuity_of(sjson)
        except Exception:  # noqa: BLE001
            mode = str(sjson.get("continuity") or "CUT").upper()
        if mode not in ("CONTINUE", "MATCH_ACTION"):
            return mode, None
        prev_id = str(sjson.get("prev_shot") or "")
        prev_row = self.db.get(prev_id, pid) if prev_id else None
        try:
            pj = (prev_row or {}).get("json")
            if isinstance(pj, str):
                pj = json.loads(pj or "{}")
            prev_loc = str((pj or {}).get("location") or "")
        except Exception:  # noqa: BLE001
            prev_loc = ""
        cur_loc = str(sjson.get("location") or "")
        if prev_loc and cur_loc and prev_loc != cur_loc:
            warnings.append(f"continuity {mode} forced to CUT "
                            f"(location changed {prev_loc} -> {cur_loc})")
            return "CUT", None
        tail = (prev_row or {}).get("tail_frame") if prev_row else None
        if not tail:
            warnings.append(f"continuity {mode} but no tail frame "
                            f"from '{prev_id or '?'}' — rendering as a cut")
            return mode, None
        return mode, str(tail)

    def _h3_take_prompt(self, body: str, refs: list[dict], shot: dict, bible,
                        continues: bool) -> str:
        """The H3 Ref2VA prompt of one link of a take (later links continue
        the take instead of opening on <Picture 1>)."""
        try:
            return self._h3_ref2va_prompt(body, refs, shot, bible, continues=continues)
        except Exception as e:  # noqa: BLE001
            log.warning("H3 ref2va sections failed, using legend: %s", e)
            legend = self._h3_reference_legend(refs)
            return f"{legend}\n\n{body}" if legend else body

    def _render_family_shot(self, sid: str, sjson: dict, prompt: str, negative: str,
                            w: int, h: int, frames: int, steps: int, seed: int,
                            mid: str, pid: str) -> Path:
        """Render one non-Wan shot (LTX-2.5 / MiniMax H3) through its isolated
        ComfyUI runtime.

        Delegates to video_studio - which owns the family's ComfyUI instance
        (registry `comfy_port`), reference uploading, output copying and the
        crash-safe job store - then blocks until the job is terminal and
        returns the finished mp4. Raises RuntimeError on failure or timeout so
        the caller's existing retry/review ladder handles it unchanged.

        References come from the cast (ltx_refs.resolve_reference_set): chosen
        stills first, then the existing selected.png / roster fall backs. A
        CONTINUE / MATCH_ACTION shot rendered on its own also opens on the
        previous shot's tail frame (_continuation_tail); a run of CONTINUE
        shots on a seamless model renders as one take instead (_render_take).

        Family-specific behaviour:
        * LTX: the proven real-time motion + negative prompt recipe is
          appended (LTX lab record PROVEN_CONFIGS, rules 4-5); DFR applies.
        * H3: prompts follow docs/H3_PROMPT_GUIDE.md and are passed through
          UNCHANGED (the guide structure IS the recipe; CFG 1.0 ignores
          negatives, and the distilled model must not inherit LTX's negative
          list). No DFR. The timeout comes from the registry entry
          (`timeout_s`, hero tier 2700 s - measured 18-23 min per clip).
        """
        import video_models
        import video_studio
        from bible import Bible
        from ltx_refs import resolve_reference_set

        fam = str((video_models.VIDEO_MODELS.get(mid) or {}).get("family", ""))
        bible = Bible(pid)
        refs, warnings = resolve_reference_set(sjson, bible, family=fam)
        # continuity bridging: H3 pins the previous shot's tail as the frame-0
        # anchor; LTX takes a free picture slot and the legend names it
        cont_mode, tail = self._continuation_tail(sjson, pid, warnings)
        if tail:
            from ltx_refs import add_continuation
            refs, note = add_continuation(refs, tail, fam)
            if note:
                warnings.append(note)
        if fam.startswith("ltx"):
            # agreed prompt recipe (LTX lab record PROVEN_CONFIGS, rules 4-5): motion-first
            # ordering + explicit real-time motion language + the proven
            # negative set. Applied to LTX renders only; the Wan templates
            # keep their prompts as-is.
            prompt = self._ltx_prompt_recipe(prompt, sjson)
            negative = self._ltx_negative_recipe(negative)
        if fam.startswith("ltx") and refs:
            # MSR usage tips: name each reference and its role in the prompt
            legend = self._ltx_reference_legend(refs)
            if legend:
                prompt = legend + " " + (prompt or "")
        if fam.startswith("h3") and refs:
            # Official Ref2VA six-section format (grouped subjects, retention
            # lines, declared frame-0 anchor). Falls back to the one-line
            # legend if the section builder hits missing data.
            prompt = self._h3_take_prompt(prompt or "", refs, sjson, bible, continues=False)
        dfr_raw = sjson.get("dfr")
        dfr = bool(dfr_raw) if dfr_raw is not None else None
        entry = video_models.VIDEO_MODELS.get(mid) or {}
        if fam.startswith("h3"):
            # The H3 templates run the proven schedule baked into the graph +
            # registry defaults (20 steps phase 1, 5-step refine). The movie
            # pipeline's tier step counts must never override it.
            try:
                steps = int((entry.get("defaults") or {}).get("steps") or 20)
            except Exception:
                steps = 20
        timeout_s = int(entry.get("timeout_s")
                        or (self.cfg.get("ltx", {}) or {}).get("timeout_s", 1800))

        rec = video_studio.start(
            mid, prompt, mode="i2v" if refs else "t2v", negative=negative,
            seed=seed, width=w, height=h, frames=frames, steps=steps,
            references=refs, dfr=dfr, worker="local", timeout=timeout_s)
        deadline = time.time() + timeout_s + 120
        job = None
        while time.time() < deadline:
            job = video_studio.job(rec["id"])
            if job and job.get("status") in ("completed", "failed"):
                break
            time.sleep(5)
        if not job or job.get("status") != "completed":
            err = (job or {}).get("error") or "render did not reach a terminal state"
            raise RuntimeError(f"{fam} render failed: {err}")
        outs = job.get("outputs") or []
        if not outs:
            raise RuntimeError(f"{fam} render completed but produced no output file")
        out_dir = Path(job.get("output_dir") or (ROOT / "output" / "videostudio"))
        path = out_dir / outs[0]
        if not path.exists():
            raise RuntimeError(f"{fam} output missing on disk: {path}")

        # provenance: pin the exact model and reference set into the shot record
        prov = dict(sjson)
        prov["applied_model"] = mid
        prov["applied_references"] = refs
        if cont_mode and cont_mode != "CUT":
            prov["continuity_applied"] = cont_mode
        if (job or {}).get("continuation"):
            prov["continuation_frame"] = job.get("continuation")
        if warnings:
            prov["reference_warnings"] = warnings
        self.db.set_json(sid, prov, project=pid)
        log.info(f"{sid}: {fam} render {mid} finished in {job.get('seconds')}s "
                 f"refs={[r['slot'] for r in refs] or 'none'}")
        return path

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
                qa = QADirector(cfg).evaluate(sjson, str(final), prompt)
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
            # QA failed -> intelligent retry ladder (project shots) or
            # plain next-seed retry (legacy)
            attempts = self.db.increment_attempts(sid, project=pid)
            budget = cfg["qa"]["max_retries"].get(shot.get("tier", "standard"),
                                                  2)
            if attempts >= budget:
                self.db.set_status(sid, "review", qa_notes=str(qa),
                                   tail_frame=tail_frame, project=pid)
                return
            if pid:
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
        # gets one — LTX/H3 shots honour CONTINUE / MATCH_ACTION by opening
        # on the previous shot's final frame.
        tail_frame = None
        try:
            from continuity import extract_tail
            tail = extract_tail(video, sid, pid=pid)
            tail_frame = str(tail) if tail else None
        except Exception as e:
            log.warning(f"{sid}: tail extraction failed: {e}")
        try:
            from qa_director import QADirector
            qa = QADirector(cfg).evaluate(sjson, str(video), prompt)
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

