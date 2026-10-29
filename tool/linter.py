"""Canon linter. Validates shot specifications
against Bible constraints before rendering, returning a structured validation
report of errors and warnings."""
from __future__ import annotations

from typing import Any

from bible import Bible

CAMERA_MOVES = {"static", "slow_push_in", "dolly", "pan", "crane", "handheld",
                "orbit", "pull_back", "tracking"}
SHOT_SIZES = {"extreme_close_up", "close_up", "medium_close_up", "medium",
              "medium_wide", "wide", "extreme_wide"}
POSITIONS = {"foreground_left", "foreground_center", "foreground_right",
             "midground_left", "midground_center", "midground_right",
             "background_left", "background_center", "background_right"}
FACINGS = {"camera_left", "camera_right", "camera", "away_from_camera"}
TIERS = {"hero", "standard", "volume"}
CHAR_TIERS = {"main", "supporting", "recurring", "crowd"}
HERO_SHOT_SIZES = {"extreme_close_up", "close_up", "medium_close_up"}
WIDE_SIZES = {"wide", "extreme_wide"}
CONTINUITY_TYPES = {"CUT", "CONTINUE", "MATCH_ACTION"}

# ordered emotion intensity for jump warnings (0 calm .. 5 rage)
EMOTION_INTENSITY = {
    "calm": 0, "serene": 0, "relaxed": 0, "peaceful": 0,
    "neutral": 1, "content": 1, "composed": 1, "curious": 1,
    "sad": 2, "melancholy": 2, "somber": 2, "worried": 2,
    "concerned": 2,
    "nervous": 3, "anxious": 3, "tense": 3, "suspicious": 3,
    "angry": 4, "fearful": 4, "frustrated": 4, "defiant": 4,
    "furious": 5, "rage": 5, "terrified": 5, "horrified": 5,
}
EMOTION_JUMP_MIN = 3


def _norm_time(t: Any) -> str:
    return " ".join(str(t or "").lower().split())


class LintReport:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def e(self, shot: str, msg: str) -> None:
        self.errors.append(f"[{shot}] {msg}")

    def w(self, shot: str, msg: str) -> None:
        self.warnings.append(f"[{shot}] {msg}")

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


class Linter:
    def __init__(self, bible: Bible, family: str = "", take_len: int = 0) -> None:
        self.b = bible
        self.family = str(family or "")
        # the most shots one take holds (movie_takes.story_length); 0: the
        # family's measured default
        self.take_len = int(take_len or 0)
        # Reference capacity per video family: LTX MSR is a hard 5-slot guide;
        # H3 fills 6 pictures. The story pipeline passes the project family so
        # the linter can flag over-budget shots for the self-heal loop.
        self.ref_budget = 6 if self.family.startswith("h3") else 5

    # ------------------------------------------------------------- pipelines
    def lint_package(self, shots: list[dict]) -> LintReport:
        rep = LintReport()
        self._lint_continuity_order(shots, rep)
        by_scene: dict[str, list[dict]] = {}
        for s in shots:
            self.lint_shot(s, rep)
            by_scene.setdefault(s["scene"], []).append(s)
        for scene, ss in by_scene.items():
            self._lint_dag(scene, ss, rep)
            self._lint_tier_budget(scene, ss, rep)
            self._lint_state_mutations(scene, ss, rep)
            self._lint_lighting_consistency(scene, ss, rep)
            self._lint_emotion_jumps(scene, ss, rep)
            self._lint_death_permanence(scene, ss, rep)
        return rep

    def _lint_continuity_order(self, shots: list[dict],
                               rep: LintReport) -> None:
        """Continuity is an EDIT decision, not a mood: never bridge across a
        scene or location change (the tail belongs to another world), never
        bridge from nothing, a MATCH_ACTION cuts to a new angle, and a take
        (a shot and the CONTINUE shots after it) holds at most the take
        length (movie_takes.max_shots): a longer run goes on in a new take.
        A CUT or a MATCH_ACTION opens fresh from the references."""
        from movie_takes import H3_TAKE_MAX_SHOTS, LTX_TAKE_MAX_SHOTS
        h3 = self.family.startswith("h3")
        cap = self.take_len or (H3_TAKE_MAX_SHOTS if h3 else LTX_TAKE_MAX_SHOTS)
        by_id = {str(s.get("shot_id")): s for s in shots}
        run = 0
        ordered = sorted(shots, key=lambda x: (str(x.get("scene") or ""),
                                               str(x.get("shot_id") or "")))
        for s in ordered:
            sid = str(s.get("shot_id") or "?")
            cont = str(s.get("continuity") or "CUT").upper()
            run = run + 1 if cont == "CONTINUE" else 0
            if cont == "CUT":
                continue
            prev = by_id.get(str(s.get("prev_shot") or ""))
            if prev is None:
                rep.w(sid, f"continuity {cont} but no prev_shot — "
                           f"nothing to bridge")
                continue
            if str(prev.get("scene")) != str(s.get("scene")):
                rep.e(sid, f"continuity {cont} across a scene change "
                           f"({prev.get('scene')} -> {s.get('scene')}) — "
                           f"a scene change must be CUT")
            elif str(prev.get("location") or "") and \
                    str(s.get("location") or "") and \
                    str(prev.get("location")) != str(s.get("location")):
                rep.e(sid, f"continuity {cont} across a location change "
                           f"({prev.get('location')} -> {s.get('location')}) — "
                           f"a location change must be CUT")
            elif _norm_time(prev.get("time")) and _norm_time(s.get("time")) \
                    and _norm_time(prev.get("time")) != _norm_time(s.get("time")):
                # a warning, not an error: errors block the package and drive
                # the LLM self-heal loop, and time labels are free text
                rep.w(sid, f"continuity {cont} across time-of-day change "
                           f"({prev.get('time')} -> {s.get('time')}) — "
                           f"lighting should match across continuous action")
            if cont == "MATCH_ACTION" and (prev.get("shot_size"), prev.get("camera")) == \
                    (s.get("shot_size"), s.get("camera")):
                rep.w(sid, "MATCH_ACTION keeps the shot size and camera of the shot "
                           "before — a match cut needs a new angle (or use CONTINUE)")
            if cont == "CONTINUE" and run >= cap and run % cap == 0:
                rep.w(sid, f"a take of more than {cap} shots (the take length): it goes on in a "
                           f"new take from this shot, on a new angle made from the last frame of "
                           f"the one before (or from that frame itself without opening frames); "
                           f"a match cut written here fits the story better")

    def lint_shot(self, s: dict, rep: LintReport | None = None) -> LintReport:
        rep = rep or LintReport()
        sid = str(s.get("shot_id", "?"))

        # --- enums
        if s.get("camera") not in CAMERA_MOVES:
            rep.e(sid, f"camera '{s.get('camera')}' not in enum")
        if s.get("shot_size") not in SHOT_SIZES:
            rep.e(sid, f"shot_size '{s.get('shot_size')}' not in enum")
        if s.get("tier") not in TIERS:
            rep.e(sid, f"tier '{s.get('tier')}' not in {{hero,standard,volume}}")
        if s.get("duration_s") != 5:
            rep.w(sid, f"duration_s {s.get('duration_s')} != 5")

        # --- hero gating (5.7): hero requires close-up or power reveal
        if s.get("tier") == "hero":
            if s.get("shot_size") not in HERO_SHOT_SIZES and not s.get("power"):
                rep.e(sid, "hero tier requires shot_size in close-up range or a power")

        # --- framing for detail: the video models keep a face's identity and
        # detail at medium or closer
        size = s.get("shot_size")
        if size in WIDE_SIZES and s.get("characters"):
            why = "dialogue" if s.get("dialogue") else "a hero moment" if s.get("tier") == "hero" else ""
            if why:
                rep.w(sid, f"{why} at {size}: faces lose their identity and detail at this size; "
                           f"use medium or closer")

        # --- location & lighting
        loc = s.get("location")
        if loc and loc not in self.b.locations():
            rep.e(sid, f"location '{loc}' not in Bible")
        locobj = self.b.locations().get(loc) if loc else {}
        loc_light = str(locobj.get("lighting") or "").strip().lower() if locobj else ""
        shot_light = str(s.get("lighting") or "").strip().lower()
        if loc_light and shot_light and loc_light != shot_light:
            rep.w(sid, f"shot lighting '{s.get('lighting')}' differs from Bible location lighting '{locobj.get('lighting')}'")

        # --- ShotSpec filmmaking contract — additive only
        cont = s.get("continuity")
        if cont is not None and cont not in CONTINUITY_TYPES:
            rep.e(sid, f"continuity '{cont}' not in "
                       "{CUT,CONTINUE,MATCH_ACTION}")
        pa = s.get("primary_action")
        if pa is not None:
            words = str(pa).split()
            if not words:
                rep.w(sid, "primary_action empty")
            elif len(words) > 25:
                rep.w(sid, f"primary_action {len(words)} words (>25)")
        if s.get("expected_visible"):
            cids = {c.get("id") for c in s.get("characters", [])}
            for ev in s["expected_visible"]:
                if ev not in cids:
                    rep.w(sid, f"expected_visible '{ev}' not among "
                               "shot characters")
        if s.get("selected_loras") is not None and \
                not isinstance(s.get("selected_loras"), list):
            rep.e(sid, "selected_loras must be a list")

        # --- characters
        chars = s.get("characters", [])
        if not chars:
            rep.w(sid, "no characters declared")
        seen: set[str] = set()
        for c in chars:
            cid = c.get("id")
            if cid in seen:
                rep.e(sid, f"duplicate character '{cid}'")
            seen.add(cid)
            char = self.b.characters().get(cid) or self.b.crowd_types().get(cid)
            if char is None:
                rep.e(sid, f"character '{cid}' not registered in Bible")
                continue
            btier = char.get("tier")
            if btier == "crowd":
                continue
            if c.get("tier") and c["tier"] != btier:
                rep.e(sid, f"{cid}: shot tier {c['tier']} != Bible tier {btier}")
            emo = c.get("emotion")
            palette = char.get("emotion_palette", [])
            if emo and palette and emo not in palette:
                rep.e(sid, f"{cid}: emotion '{emo}' outside palette {palette}")
            pos, fac = c.get("position"), c.get("facing")
            if pos and pos not in POSITIONS:
                rep.e(sid, f"{cid}: position '{pos}' not in enum")
            if fac and fac not in FACINGS:
                rep.e(sid, f"{cid}: facing '{fac}' not in enum")
            locs = char.get("location_binding", [])
            if locs and loc and loc not in locs:
                rep.e(sid, f"{cid}: location-bound character outside {locs}")

        # --- power
        pwr = s.get("power")
        if pwr:
            owners = [c for c in chars if pwr in (self.b.characters()
                       .get(c["id"], {}).get("powers", {}) or {})]
            if not owners:
                rep.e(sid, f"power '{pwr}' not registered on any character in shot")
        lvl = s.get("power_level")
        if lvl not in (None, ""):
            try:
                lvl_i = int(lvl)
            except (ValueError, TypeError, OverflowError):   # "x", None, inf
                lvl_i = -1
            if lvl_i < 1 or lvl_i > 99:
                rep.e(sid, f"power_level {lvl!r} not in 1..99")

        # --- character presentation: loadouts + weapons must exist on the
        # character (the LLM must never invent vocabulary)
        for c in chars:
            cid = c.get("id")
            char = self.b.characters().get(cid) or self.b.crowd_types().get(cid) or {}
            ld = str(c.get("loadout") or "").strip()
            if ld and ld not in (char.get("loadouts") or {}):
                rep.e(sid, f"{cid}: loadout '{ld}' not defined on the character")
            wid = str(c.get("weapon") or "").strip()
            if wid and wid not in (char.get("weapons") or {}):
                rep.e(sid, f"{cid}: weapon '{wid}' not defined on the character")
            hand = str(c.get("handling") or "").strip()
            if hand and not wid and not ld:
                rep.w(sid, f"{cid}: handling '{hand}' set but no weapon/loadout")

        # --- environment variant must exist as a tagged reference image
        tag = str(s.get("location_tag") or "").strip()
        if tag and loc:
            locobj = self.b.locations().get(loc) or {}
            tags = {str(r.get("tag") or "") for r in
                    (locobj.get("reference_images") or []) if isinstance(r, dict)}
            if tags and tag not in tags:
                rep.e(sid, f"location_tag '{tag}' has no reference image on "
                           f"'{loc}' (named variants: {sorted(tags)})")

        # --- reference budget: one slot per character + power + environment.
        # The deterministic resolver degrades gracefully, but the LLM should
        # compose within budget — this error drives the self-heal rewrite.
        n_subj = sum(1 for c in chars if str(c.get("tier") or "") != "crowd")
        need = n_subj + (1 if pwr else 0) + (1 if loc else 0)
        if need > self.ref_budget:
            rep.e(sid, f"reference budget: {need} references needed "
                       f"({n_subj} characters + power + environment) but the "
                       f"model accepts {self.ref_budget} — move extra "
                       f"characters to visible_background or drop the power")

        # --- dialogue
        for d in s.get("dialogue", []):
            ch = self.b.characters().get(d.get("char", ""))
            if ch is None:
                rep.e(sid, f"dialogue char '{d.get('char')}' not in Bible")
            elif d.get("emotion") and ch.get("emotion_palette") and \
                    d["emotion"] not in ch["emotion_palette"]:
                rep.e(sid, f"dialogue emotion '{d['emotion']}' outside palette")
            words = len(str(d.get("line", "")).split())
            if words > 25:
                rep.w(sid, f"dialogue line {words} words (>~2 sentences)")

        # --- background
        for bg in s.get("visible_background", []):
            if bg not in self.b.crowd_types():
                rep.e(sid, f"background type '{bg}' not in crowd_types.yaml")

        return rep

    # ------------------------------------------------------------------- dag
    def _lint_dag(self, scene: str, shots: list[dict], rep: LintReport) -> None:
        before = len(rep.errors)
        ids = {s["shot_id"] for s in shots}
        children: dict[str, list[str]] = {i: [] for i in ids}
        roots = 0
        for s in shots:
            prev = s.get("prev_shot")
            if prev is None:
                roots += 1
            elif prev not in ids:
                rep.e(s["shot_id"], f"prev_shot '{prev}' missing from scene {scene}")
            elif prev == s["shot_id"]:
                rep.e(s["shot_id"], "prev_shot points to itself (cycle)")
            else:
                children[prev].append(s["shot_id"])
        if roots != 1:
            rep.e(scene, f"scene {scene} has {roots} roots (need exactly 1)")
        # only this scene's chain problems stop its walk (an error in another
        # scene used to hide every scene's cycles and orphans)
        if len(rep.errors) > before:
            return
        # reachability
        root = next(s["shot_id"] for s in shots if s.get("prev_shot") is None)
        seen: set[str] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                rep.e(scene, f"cycle detected at {cur}")
                return
            seen.add(cur)
            stack.extend(children[cur])
        orphans = ids - seen
        if orphans:
            rep.e(scene, f"orphan shots: {sorted(orphans)}")

    # ------------------------------------------------------------- tier budget
    def _lint_tier_budget(self, scene: str, shots: list[dict], rep: LintReport) -> None:
        n = len(shots)
        if n < 2:
            return
        # every scene may host ONE hero moment even when small (a 2-shot
        # scene with a single hero close-up is valid filmmaking; the 15%
        # cap still applies to scenes with 7+ shots)
        # (no volume quota: it pushed shots wide, where faces lose their
        # identity and detail; see the framing warnings in lint_shot)
        hero_cap = max(int(n * 0.15), 1 if n <= 6 else 0)
        hero = sum(1 for s in shots if s.get("tier") == "hero")
        if hero > hero_cap:
            rep.e(scene, f"hero tier {hero}/{n} > {hero_cap} budget")

    # -------------------------------------------------------- state mutations
    def _lint_state_mutations(self, scene: str, shots: list[dict], rep: LintReport) -> None:
        state: dict[str, dict[str, str]] = {}
        order = {s["shot_id"]: s for s in shots}
        for s in shots:
            for mut in s.get("state_mutations", []):
                t, k, v = mut.get("target"), mut.get("key"), mut.get("value")
                if t and k and v:
                    state.setdefault(t, {})[k] = v
            for t, kv in state.items():
                if t.startswith("dead_") or t.startswith("left_"):
                    continue
                # warn only: prompt assembly injects state; nothing here checks it visually
                for k, v in kv.items():
                    rep.w(s["shot_id"], f"state: {t}.{k}={v} (injected into prompt)")

    def _lint_lighting_consistency(self, scene: str, shots: list[dict], rep: LintReport) -> None:
        """WARN: conflicting time-of-day or lighting setups across shots in the same scene."""
        times: dict[str, list[str]] = {}
        for s in shots:
            t = _norm_time(s.get("time"))
            if t:
                times.setdefault(t, []).append(str(s.get("shot_id") or "?"))
        if len(times) > 1:
            details = ", ".join(f"'{t}': {sids}" for t, sids in sorted(times.items()))
            rep.w(scene, f"scene {scene} has inconsistent time-of-day/lighting ({details})")

    # ------------------------------------------------------------ warnings
    def _scene_order(self, shots: list[dict]) -> list[dict]:
        """Scene order: follow the prev_shot chain from the root; fall back
        to shot_id sort for non-linear DAGs."""
        roots = [s for s in shots if s.get("prev_shot") is None]
        if len(roots) == 1:
            ordered: list[dict] = []
            cur = roots[0]
            seen: set[str] = set()
            while cur is not None:
                sid = str(cur["shot_id"])
                if sid in seen:
                    break
                seen.add(sid)
                ordered.append(cur)
                nxt = [s for s in shots if s.get("prev_shot") == sid]
                cur = nxt[0] if len(nxt) == 1 else None
            if len(ordered) == len(shots):
                return ordered
        return sorted(shots, key=lambda s: str(s.get("shot_id", "")))

    def _lint_emotion_jumps(self, scene: str, shots: list[dict], rep: LintReport) -> None:
        """WARN: a character's emotion jumping directly between far intensity
        levels (e.g. calm -> rage) across consecutive appearances."""
        ordered = self._scene_order(shots)
        last: dict[str, tuple[int, str]] = {}
        for s in ordered:
            for c in s.get("characters", []):
                cid, emo = c.get("id"), c.get("emotion")
                if not cid or not emo:
                    continue
                now = EMOTION_INTENSITY.get(emo)
                if now is None:            # unknown word — skip
                    continue
                prev = last.get(cid)
                if prev is not None and abs(now - prev[0]) >= EMOTION_JUMP_MIN:
                    rep.w(s["shot_id"],
                          f"{cid}: emotion jump {prev[1]} -> {emo} between "
                          f"consecutive appearances")
                last[cid] = (now, emo)

    def _lint_death_permanence(self, scene: str, shots: list[dict], rep: LintReport) -> None:
        """WARN: a 'dead*'/'left*' state mutation targeting a character that
        still appears in a later shot of the same scene."""
        ordered = self._scene_order(shots)
        for i, s in enumerate(ordered):
            for mut in s.get("state_mutations", []):
                k = str(mut.get("key", ""))
                if not (k.startswith("dead") or k.startswith("left")):
                    continue
                t = mut.get("target")
                if not t:
                    continue
                for later in ordered[i + 1:]:
                    if any(c.get("id") == t
                           for c in later.get("characters", [])):
                        rep.w(later["shot_id"],
                              f"{t}: {k}={mut.get('value')} but appears "
                              f"again in a later shot of {scene}")
                        break

    # ------------------------------------------------------------ validation
    def validate_shot_list(self, shots: list[dict]) -> dict:
        rep = self.lint_package(shots)
        return rep.to_dict()
