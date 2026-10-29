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
# "prompts": the video prompts of the LTX-2.5 / H3 shots, written by the AI
# (movie_prompts) or by hand; without an AI the project waits for them
STAGE_FLOW = ("story", "cast", "scenes", "shots", "prompts", "keyframes", "render",
              "review", "assemble")
TERMINAL = {"passed", "review", "failed", "assembled"}


class Director:
    """Walks one project through the production stages, honoring gates."""

    def __init__(self, cfg: dict | None = None, db=None, store=None,
                 orch=None, assembler=None, writer=None, architect=None) -> None:
        self.cfg = cfg or load_config()
        self.db = db
        self.store = store
        self.orch = orch
        self.assembler = assembler
        # the AI writer of the story (storygen_llm.ArchitectJob), shared with
        # the Story page so one run at a time writes a project
        self.architect = architect
        if writer is None:
            from movie_prompts import PromptWriter
            writer = PromptWriter(self.cfg, db, store)
        self.writer = writer
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- control
    def start(self, pid: str, wait: float = 0.0) -> dict:
        """Run the project's production in the background. `wait`: seconds to
        wait for it to settle; a run that stops within them (a step waits for
        something, or an error) answers with the project's status and the
        reason, so the operator is told at once instead of seeing nothing."""
        with self._lock:
            self._stop.discard(pid)
            t = self._threads.get(pid)
            if t is not None and t.is_alive():
                return {"ok": True, "running": True}
            t = threading.Thread(target=self._run, args=(pid,),
                                 daemon=True)
            self._threads[pid] = t
            t.start()
        if wait > 0:
            t.join(wait)
        if t.is_alive() or self.store is None:
            return {"ok": True, "running": True}
        proj = self.store.get(pid) or {}
        return {"ok": True, "running": False, "status": proj.get("status") or "",
                "stage": proj.get("stage") or "", "error": proj.get("error") or ""}

    def stop(self, pid: str) -> dict:
        with self._lock:
            self._stop.add(pid)
        self.writer.stop(pid)
        if self.architect is not None:
            self.architect.stop(pid)
        if self.orch is not None:
            try:
                self.orch.stop()
            except Exception:
                pass
        if self.store is not None:
            self.store.set_status(pid, "stopped", stage="paused")
        return {"ok": True}

    def resume(self, pid: str, wait: float = 0.0) -> dict:
        """Manual-mode / gate approval: advance one step past the gate."""
        if not wait:
            self.start(pid)
            return {"ok": True}
        return self.start(pid, wait)

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
            stage = proj.get("stage") or "story"
            if proj.get("status") == "done":
                # a finished film runs again only for new work: scenes that
                # got their shots since, or shots still to render
                if self._missing_scenes(pid, proj):
                    stage = "shots"
                elif self._unfinished(pid):
                    stage = "prompts"
                else:
                    return
            # a stopped/errored project RESUMES from wherever it left off
            # (completed stages are skipped by _stage_done)
            self.store.set_status(pid, "running", stage=stage)
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
            # the outline: a treatment and its scenes (beats), or the shots
            # of a story written before beats existed
            return bool(story.get("treatment")) and bool(self._beats(proj) or self._has_rows(pid))
        if stage == "cast":
            import cast_look
            from bible import Bible
            chars = Bible(pid).characters()
            return bool(chars) and not cast_look.checks(chars)
        if stage == "scenes":
            from bible import Bible
            return bool(Bible(pid).scenes())
        if stage == "shots":
            # every scene has its shots, or the operator moved on (approved
            # the gate, or the written scenes render while others wait)
            return self._past(proj, "shots") or \
                (self._has_rows(pid) and not self._missing_scenes(pid, proj))
        if stage == "prompts":
            # passed once every shot still to render has its prompt, or once
            # the operator moved on (a shot that still has none waits for it
            # in the render stage)
            past = STAGE_FLOW.index(proj.get("stage")) > STAGE_FLOW.index("prompts") \
                if proj.get("stage") in STAGE_FLOW else False
            return past or not self._needing_prompts(pid)
        if stage == "keyframes":
            # the LTX-2.5 / H3 opening frames are looked at here when the
            # keyframes gate is manual, until the operator moves on (the
            # frames passed stay passed after a Stop, opening_frames.looked_at);
            # the Wan path's keyframes are made inside render (candidates on demand)
            if self._past(proj, "keyframes") or self.store.gate(pid, "keyframes") != "manual":
                return True
            import opening_frames
            return not (opening_frames.enabled(self.cfg) and self.db is not None
                        and (opening_frames.pending(self.db, pid, self.cfg)
                             or opening_frames.to_look_at(self.db, pid, self.cfg)))
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
            return self._stage_cast(pid)
        if stage == "scenes":
            self.store.set_status(pid, "running", stage="shots")
            return None
        if stage == "shots":
            return self._stage_shots(pid, proj, gate)
        if stage == "prompts":
            return self._stage_prompts(pid, proj)
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
    # ----------------------------------------------------------- helpers
    def _beats(self, proj: dict) -> list[dict]:
        beats = (((proj.get("story") or {}).get("beats") or {}).get("beats") or [])
        return [b for b in beats if isinstance(b, dict) and b.get("scene")]

    def _has_rows(self, pid: str) -> bool:
        return bool(self.db.all(project=pid, limit=1)) if self.db else False

    def _missing_scenes(self, pid: str, proj: dict) -> list[str]:
        """The outline's scenes that have no shots yet."""
        beats = self._beats(proj)
        if not beats or not self.db:
            return []
        written = {str(r.get("scene")).lower() for r in self.db.all(project=pid)}
        return [str(b["scene"]) for b in beats if str(b["scene"]).lower() not in written]

    def _unfinished(self, pid: str) -> bool:
        return any(r["status"] not in TERMINAL for r in (self.db.all(project=pid) if self.db else []))

    def _past(self, proj: dict, stage: str) -> bool:
        st = proj.get("stage")
        return st in STAGE_FLOW and STAGE_FLOW.index(st) > STAGE_FLOW.index(stage)

    def _ai(self) -> bool:
        import movie_prompts
        return self.architect is not None and movie_prompts.ai_ready(self.cfg)

    def _writer_stopped(self, pid: str, stage: str) -> str:
        """The AI writer stopped: by the Director's Stop (the status says so
        already), or from the Story page, where the project waits for a new
        Start instead of reading as running."""
        if not self._stopped(pid):
            self.store.set_status(pid, "gate_pending", stage=stage,
                                  error="The AI writer was stopped: press Continue production to go on")
        return "gate"

    @staticmethod
    def _ids(ids: list[str]) -> str:
        return ", ".join(ids[:6]) + ("..." if len(ids) > 6 else "")

    # ------------------------------------------------------------ stages
    def _stage_story(self, pid: str, proj: dict, gate: str) -> str | None:
        """The outline (treatment, story bible, beats): written by the AI
        writer when one is connected, else on the Story page with any chat AI
        (copy the prompt, paste the reply). The scenes' shots follow in the
        shots stage. Never a template story."""
        if not self._stage_done(pid, "story", proj):
            why = ("write it on the Story page: copy the story prompt into any chat AI and paste "
                   "its reply, or connect an AI writer")
            if self._ai():
                self.store.set_status(pid, "running", stage="story")
                res = self.architect.run(pid, outline=True, scenes="none",
                                         stop=lambda: self._stopped(pid))
                if res.get("stopped"):
                    return self._writer_stopped(pid, "story")
                if res.get("unreachable"):
                    why = (f"{res.get('message')}: start it, or pick another one under AI writer, "
                           "or write the story with a chat AI on the Story page")
                elif not res.get("ok"):
                    why = (f"the AI writer could not write the outline ({res.get('message')}): "
                           "try again, or write it with a chat AI on the Story page")
                proj = self.store.get(pid) or proj
            if not self._stage_done(pid, "story", proj):
                self.store.set_status(pid, "gate_pending", stage="story",
                                      error=f"The story is not written yet: {why}")
                return "gate"
        if gate == "manual":
            self.store.set_status(pid, "gate_pending", stage="story")
            return "gate"
        self.store.set_status(pid, "running", stage="cast")
        return None

    def _stage_cast(self, pid: str) -> str | None:
        """Every prompt describes a cast member with their Body and Outfit
        text, beside the picture the video model sees. A text that says
        another gender, age or outfit than the picture pulls each shot away
        from it, so the film waits until it is put right on Cast & World."""
        import cast_look
        from bible import Bible
        chars = Bible(pid).characters()
        bad = cast_look.checks(chars)
        if bad:
            names = [str((chars.get(c) or {}).get("name") or c) for c in bad]
            first = next(iter(bad.values()))
            self.store.set_status(
                pid, "gate_pending", stage="cast",
                error=(f"The look text of {self._ids(names)} does not match the main picture "
                       f"({first['problems'][0]}): on Cast & World, use the picture's look "
                       "or keep yours, then press Approve & continue"))
            return "gate"
        self.store.set_status(pid, "running", stage="scenes")
        return None

    def _stage_shots(self, pid: str, proj: dict, gate: str) -> str | None:
        """The scenes' shots: the AI writer writes the scenes that have none;
        without one they are written on the Story page (Shots scene by scene)
        and the written scenes render meanwhile. The film is cut once every
        scene has its shots."""
        missing = self._missing_scenes(pid, proj)
        if missing and self._ai():
            self.store.set_status(pid, "running", stage="shots")
            res = self.architect.run(pid, outline=False, scenes="missing",
                                     stop=lambda: self._stopped(pid))
            if res.get("stopped"):
                return self._writer_stopped(pid, stage="shots")
            if not res.get("ok"):
                log.warning("director: %s: the AI writer did not write every scene: %s",
                            pid, res.get("message"))
            proj = self.store.get(pid) or proj
            missing = self._missing_scenes(pid, proj)
        if not self._has_rows(pid):
            why = (f"{len(missing)} scene(s) need their shots ({self._ids(missing)}): write them on the "
                   "Story page (Shots scene by scene) with any chat AI, or connect an AI writer"
                   if missing else "the story has no shots yet: write them on the Story page")
            self.store.set_status(pid, "gate_pending", stage="shots", error=why)
            return "gate"
        if missing:
            log.info("director: %s renders its written scenes; %d scene(s) still need their shots",
                     pid, len(missing))
        if gate == "manual":
            self.store.set_status(pid, "gate_pending", stage="shots")
            return "gate"
        self.store.set_status(pid, "running", stage="prompts")
        return None

    def _needing_prompts(self, pid: str) -> list[str]:
        import movie_prompts
        from bible import Bible
        return movie_prompts.needing(self.db, Bible(pid), pid, self.cfg) if self.db else []

    def _stage_prompts(self, pid: str, proj: dict) -> str | None:
        """The video prompts of the LTX-2.5 / H3 shots: written by the AI when
        one is set up (one unit at a time, in story order). The shots that
        have theirs then render; the others wait for theirs in the render
        stage (the Shots page: write them there, or with a chat AI). The
        project waits here only when no shot could render yet."""
        import movie_prompts
        res: dict = {}
        if movie_prompts.ai_ready(self.cfg) and self._needing_prompts(pid):
            self.store.set_status(pid, "running", stage="prompts")
            res = self.writer.run(pid)
            log.info("director: %s prompts - %s", pid, res.get("message"))
            if self._stopped(pid):
                return None
        missing = self._needing_prompts(pid)
        if missing:
            rows = self.db.all(project=pid) if self.db else []
            if any(r["shot_id"] not in missing for r in rows if r["status"] not in TERMINAL):
                log.info("director: %s renders the shots with a prompt; %d wait for theirs",
                         pid, len(missing))
                self.store.set_status(pid, "running", stage="keyframes")
                return None
            why = self._prompts_why(bool(res.get("unreachable")), "")
            self.store.set_status(pid, "gate_pending", stage="prompts",
                                  error=f"{len(missing)} shot(s) need their video prompt "
                                        f"({self._ids(missing)}): {why}")
            return "gate"
        self.store.set_status(pid, "running", stage="keyframes")
        return None

    def _prompts_why(self, unreachable: bool, reason: str) -> str:
        """What moves a production that waits for video prompts on: the AI
        writer when one is set up and answers, else the Shots page, with any
        chat AI. `reason`: why the AI did not write them, when it was asked."""
        import movie_prompts
        chat = ("paste them from any chat AI on the Shots page (Use a chat AI) or write them there, "
                "then press Continue production")
        if not movie_prompts.ai_ready(self.cfg):
            return f"{chat}; or connect an AI writer"
        if unreachable:
            base = str((self.cfg.get("architect") or {}).get("base_url") or "")
            return (f"the AI writer at {base} did not answer: start it or pick another one "
                    f"under AI writer, or {chat}")
        return f"{reason or 'the AI could not write them (see the Shots page)'}: {chat}"

    def _held_by_prompts(self, rows: list[dict]) -> tuple[list[str], list[str]] | None:
        """(waiting, held) when nothing still to render can render before more
        video prompts are in: every such shot waits for its prompt (status
        "prompt"), or opens on one that does (orchestrator.waits_for: a
        CONTINUE, or a match cut in the same place), so it stays pending
        behind it. None while any shot can still render or renders."""
        import movie_prompts
        from orchestrator import waits_for
        by_key = {(r.get("project_id"), r["shot_id"]): r for r in rows}
        active = [r for r in rows if r["status"] not in TERMINAL]
        waiting = [r["shot_id"] for r in active if r["status"] == movie_prompts.WAITING]
        if not waiting:
            return None
        held = []
        for r in active:
            if r["status"] == movie_prompts.WAITING:
                continue
            prev, seen = waits_for(r, by_key), {r["shot_id"]}
            while prev is not None and prev["status"] != movie_prompts.WAITING \
                    and prev["status"] not in TERMINAL and prev["shot_id"] not in seen:
                seen.add(prev["shot_id"])
                prev = waits_for(prev, by_key)
            if prev is None or prev["status"] != movie_prompts.WAITING:
                return None
            held.append(r["shot_id"])
        return waiting, held

    def _stage_keyframes(self, pid: str, proj: dict,
                         gate: str) -> str | None:
        """LTX-2.5 / H3: with the keyframes gate manual (Checkpoints), the
        opening frames of the cuts still to render are made here, before the
        render, and the project waits at the gate while they are looked at
        on the Shots page (their likeness to the cast, Make again). A match
        cut's frame is made from the last frame of the shot before, so at
        render time; with the gate on auto every frame is made right before
        its render (opening_frames).

        Wan 2.2: generate + auto-approve keyframes for hero/important shots.
        LTX-2.5 / H3 shots open on their own start image, the previous clip
        or their opening frame (movie_request), never on these keyframes."""
        from bible import Bible
        if gate == "manual" and self.db is not None:
            import opening_frames
            todo = opening_frames.pending(self.db, pid, self.cfg)
            for i in range(0, len(todo), opening_frames.BATCH):
                if self._stopped(pid):
                    return None
                self.store.set_status(pid, "running", stage="keyframes")
                opening_frames.make(self.db, Bible(pid), todo[i:i + opening_frames.BATCH], self.cfg)
            ready = opening_frames.to_look_at(self.db, pid, self.cfg)
            if ready:
                weak = [sid for sid, info in ready if info.get("weak")]
                self.store.set_status(
                    pid, "gate_pending", stage="keyframes",
                    error=f"{len(ready)} opening frame(s) to look at on the Shots page"
                          + (f"; {len(weak)} look less like the cast than "
                             f"movie.key_image_min_likeness ({', '.join(weak[:6])}"
                             f"{'...' if len(weak) > 6 else ''}): make them again there"
                             if weak else "") + ". Then press Approve & continue to render.")
                return "gate"
        from keyframe_gen import KeyframeGen, resolve_project_ref
        from lora_video import VideoLoraRegistry, resolve_chain, pick_profile
        from movie_request import NoVideoModel, shot_model
        kcfg = self.cfg.get("keyframes", {})
        auto = bool(kcfg.get("auto_approve", True)) or gate == "auto"
        rows = self.db.all(project=pid) if self.db else []

        def wan(r: dict) -> bool:
            j = r.get("json")
            try:
                return shot_model(self.db, r, j if isinstance(j, dict) else {}, pid) == ""
            except NoVideoModel:
                return False
        rows = [r for r in rows if wan(r)]
        if not rows:
            self.store.set_status(pid, "running", stage="render")
            return None
        p_bible = Bible(pid)
        reg = VideoLoraRegistry(self.cfg)
        reg.scan()
        gen = KeyframeGen(self.cfg)
        ref = resolve_project_ref(self.store, pid)
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
        import movie_prompts
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
            active = [r for r in rows if r["status"] not in TERMINAL]
            if not active:
                break
            stuck = self._held_by_prompts(rows)
            if stuck:
                # the shots with a prompt are rendered; the rest render once
                # theirs are in (Shots page) and production continues. The
                # queue stops meanwhile, so a prompt saved now renders on
                # Continue, not under a project that says it waits.
                self.orch.stop()
                waiting, held = stuck
                first = next(r for r in rows if r["shot_id"] == waiting[0])
                reason = str(first.get("error") or "").removeprefix("needs its prompt: ")
                why = self._prompts_why(movie_prompts.UNREACHED in reason,
                                        f"{waiting[0]}: {reason}" if reason else "")
                done = sum(1 for r in rows if r["status"] in movie_prompts.DONE)
                self.store.set_status(
                    pid, "gate_pending", stage="prompts",
                    error=(f"{done} of {len(rows)} shots are rendered. {len(waiting)} shot(s) need their "
                           f"video prompt ({self._ids(waiting)})"
                           + (f", and {len(held)} more open on them" if held else "")
                           + f": {why}")[:700])
                log.info("director: %s waits for video prompts: %s (%d held behind them)",
                         pid, self._ids(waiting), len(held))
                return "gate"
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
        missing = self._missing_scenes(pid, proj)
        if missing:
            # the written scenes are rendered; the film waits for the rest
            self.store.set_status(
                pid, "gate_pending", stage="shots",
                error=f"{len(missing)} scene(s) still need their shots ({self._ids(missing)}): write "
                      "them on the Story page (Shots scene by scene), then press Start production")
            return "gate"
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
