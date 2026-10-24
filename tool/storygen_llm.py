"""LLM Story Architect: prompt -> structured script generation.

Generates narrative spine (treatment, bible, beats) and scene sequences with
continuity state carried forward and linter validation across iterative chunked
calls.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from core import load_config, log_setup

log = log_setup("storygen_llm")


# ---------------------------------------------------------------- prompts
def _skill_text(name: str) -> str:
    """Pipeline skill documents (editable markdown under tool/skills)."""
    try:
        from core import ROOT
        p = ROOT / "tool" / "skills" / name / "SKILL.md"
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to load skill text %s: %s", name, e)
    return ""


def _system_prompt() -> str:
    """The architect's system prompt = role + the storytelling skill."""
    base = ("You are a professional movie Story Architect for the AI Movie "
            "Studio pipeline. Reply with strict JSON only, never prose.")
    skill = _skill_text("storytelling")
    if skill:
        return base + "\n\nFollow this skill exactly:\n\n" + skill
    return base


def cast_lines(p_bible) -> str:
    """The LLM's full vocabulary: identities AND their presentation options.

    Weapons, outfits and loadouts must be listed or the script cannot use
    them — a hallucinated weapon/loadout id is rejected by the linter and
    forces a scene rewrite.
    """
    chars = p_bible.characters()
    out = []
    for c in sorted(chars.values(), key=lambda x: x.get("id", "")):
        powers = ", ".join(c.get("powers") or {}.keys()) or "none"
        loras = ""
        l = c.get("lora")
        if isinstance(l, dict) and l.get("file"):
            loras = f" | LoRA: {l['file']}"
        emos = ", ".join(c.get("emotion_palette") or [])
        line = (f"- {c.get('id')} ({c.get('name', '')}, tier "
                f"{c.get('tier', '?')}) — powers: {powers}{loras} — "
                f"emotions: {emos or '(none set — define 4-8 in the bible stage)'}")
        weapons = c.get("weapons") or {}
        if weapons:
            wl = []
            for wid, w in weapons.items():
                d = str((w or {}).get("descriptor") or wid)
                wl.append(f"{wid} ({d})")
            line += " — weapons: " + "; ".join(wl)
        loadouts = c.get("loadouts") or {}
        if loadouts:
            ll = []
            for name, ld in loadouts.items():
                ld = ld or {}
                bits = []
                if ld.get("outfit"):
                    bits.append(f"outfit: {ld['outfit']}")
                if ld.get("weapon"):
                    bits.append(f"weapon: {ld['weapon']}")
                ll.append(f"{name} [{', '.join(bits) or 'base look'}]")
            line += " — loadouts: " + "; ".join(ll)
        smap = c.get("scene_tags") or {}
        if smap:
            line += " — default per scene: " + ", ".join(
                f"{k}={v}" for k, v in sorted(smap.items()))
        out.append(line)
    return "\n".join(out) or "- none yet — create your own in the bible stage"


def loc_lines(p_bible) -> str:
    """Locations with their NAMED PLACES (reference variants)."""
    locs = p_bible.locations()
    lines = []
    for l in sorted(locs.values(), key=lambda x: x.get("id", "")):
        line = f"- {l.get('id')}: {l.get('descriptor', '')}"
        tags = []
        for r in (l.get("reference_images") or []):
            if isinstance(r, dict) and str(r.get("tag") or "").strip():
                t = str(r["tag"]).strip()
                if t not in tags:
                    tags.append(t)
        if tags:
            line += " — places (use as place): " + "; ".join(tags)
        smap = l.get("scene_tags") or {}
        if smap:
            line += " — default per scene: " + ", ".join(
                f"{k}={v}" for k, v in sorted(smap.items()))
        lines.append(line)
    return "\n".join(lines) or "- none yet — create your own in the bible stage"


def power_lines(p_bible) -> str:
    """Powers + the level ladder meaning (what intensity N looks like)."""
    out = []
    for c in sorted(p_bible.characters().values(),
                    key=lambda x: x.get("id", "")):
        for pid_, p in (c.get("powers") or {}).items():
            p = p or {}
            desc = str(p.get("descriptor") or "").strip()
            if not desc or desc.lower() == str(pid_).lower():
                # never hand the LLM a bare id as the power's description —
                # synthesize one from the recorded vfx vocabulary
                core = " ".join(x for x in (str(p.get("color") or "").strip(),
                                            str(p.get("material") or "").strip())
                                if x) or str(pid_)
                motion = str(p.get("motion") or "").strip()
                glow = str(p.get("glow") or "").strip()
                desc = (f"{core} energy"
                        + (f", {motion}" if motion else "")
                        + (f", {glow}" if glow and glow not in core else ""))
            nrefs = len([r for r in (p.get("reference_images") or [])
                         if isinstance(r, dict)])
            note = "" if nrefs else \
                " [no reference stills yet — import a power batch]"
            out.append(
                f"- {pid_} (on {c.get('id')}): {desc} "
                f"[color {p.get('color', '?')}; motion {p.get('motion', '?')}]"
                + note)
    if not out:
        return "- none yet"
    return ("\n".join(out) +
            "\n  LEVELS: set `level` 1-10 per shot — 1-3 subtle aura/sparks, "
            "4-6 controlled displays, 7-9 violent with environment damage, "
            "10 cataclysmic (arena-scale destruction). The renderer passes the "
            "still whose level is the highest at or below the shot's level.")


SHOT_RULES = """- One clear primary_action per shot (<= 20 words, camera-ready).
- tier: hero <= 15% (only close-up-range shots OR shots with a power), volume >= 30%, standard the rest.
- hero shots only when the moment earns it (power reveal, close-up emotion).
- prev_shot chains: one root per scene, linear.
- Emotions must come from each character's palette; transitions gradual (no calm -> rage).
- continuity: CUT is the default — a new angle, a new moment, a new place is a CUT. CONTINUE when ONE action spans more than one 5s clip: the next clip OPENS on the previous shot's final frame, so the motion must literally continue (a long walk, a climb, a fall, a spell building). MATCH_ACTION when the cut lands mid-action and the movement carries across it (shared choreography). Aim for roughly one bridge per 4-8 shots in action/movement scenes; never more than two in a row. NEVER bridge across a scene or location change — that is always a hard CUT.
- state_mutations persist (wounds, weather, damage) and must be referenced later.
- Dialogue <= 2 lines per shot, <= 25 words, emotion from the speaker's palette.
- powers: only the powers listed on the cast. Primary action may unleash a power only for the character that owns it.
- REFERENCE BUDGET (hard): one reference per character + one for the power + one for the environment. The model accepts 5 (LTX) / 6 (H3) pictures, so a shot can carry at most 3 characters WITH a power, or 4 characters without one. For bigger groups, put the extra characters in visible_background (crowd) or split the action across shots.
- weapon/loadout: only ids/names from the CAST list. Use `weapon` when the shot's action uses the weapon (and set `handling`); use `loadout` when the scene needs a specific outfit/weapon set. A drawn weapon stays in hand across following shots (no vanishing props).
- place: only names from that location's `places` list. Use it when the beat happens in a specific room/area (arena pit, corridor, bedchamber...).
- level: 1-10, escalating across the film; the finale reaches 10.
- sound: name the concrete diegetic sounds of THIS shot (blade ring, thunder crack, footsteps) — they become the audio track."""

SHOT_JSON_TPL = '{{"shot_id": "{scene}_001", "scene": "{scene}", "location": "<id>", "time": "...", "characters": [{{"id": "<id>", "tier": "main|supporting|recurring|crowd", "emotion": "...", "action": "...", "position": "foreground_center|midground_left|midground_right|background_center|...", "facing": "camera|camera_left|camera_right|away_from_camera"}}], "power": null, "camera": "static|slow_push_in|dolly|pan|crane|handheld|orbit|pull_back|tracking", "shot_size": "extreme_close_up|close_up|medium_close_up|medium|medium_wide|wide|extreme_wide", "duration_s": 5, "tier": "hero|standard|volume", "dialogue": [], "prev_shot": null, "state_mutations": [], "visible_background": [], "weather": "inherit", "primary_action": "...", "secondary_motion": "", "start_state": {{}}, "end_state": {{}}, "continuity": "CUT", "expected_visible": ["<ids>"], "composition": "", "lens": ""}}'

# Compact entry extension: the 8th element is an OPTIONAL object with the
# reference-facing choices (ignored by old parsers, read by package_norm):
# {"weapon": "<id>", "loadout": "<name>", "place": "<place>", "level": 1-10,
#  "handling": "...", "sounds": "..."}
COMPACT_EXTRA = ('{"weapon": "<id or null>", "loadout": "<name or null>", '
                 '"place": "<place or null>", "level": <1-10 or null>, '
                 '"handling": "<how the weapon is used or null>"}')


def build_spine_prompt(proj: dict, p_bible) -> str:
    dur = int(proj.get("duration_target") or 60)
    n_shots = max(4, -(-dur // 5))
    n_scenes = max(2, min(30, round(n_shots / 28)))
    return f"""You are the Story Architect for a local AI movie pipeline that renders every shot as a 5-second video clip. Write the MASTER PLAN for a {proj.get('genre','')} movie, tone "{proj.get('tone','')}": {dur} seconds = {n_shots} shots across {n_scenes} scenes (each scene 25-40 shots). Shots will be written scene-by-scene later, so this stage defines the SPINE only.

IDEA: {proj.get('idea','')}

CAST (use ONLY these ids):
{cast_lines(p_bible)}

LOCATIONS (use ONLY these ids):
{loc_lines(p_bible)}

BEAT RULES:
- Exactly {n_scenes} beats. Every scene: goal -> obstacle -> turn (something changes), ends on a hook.
- First scene = inciting incident; scene {n_scenes // 2} = midpoint reversal; last scene = climax + resolution hook.
- Reward cadence: a payoff every 2-3 minutes. Escalate powers/threats scene by scene.
- Give each scene a specific location, time, mood, and est_shots that sum to {n_shots}.

OUTPUT — return ONLY this JSON object (no prose outside it):
{{"treatment": {{"logline": "...", "synopsis": "...", "themes": ["..."], "protagonist": "<id>", "antagonist": "<id>", "escalation": ["..."], "ending": "...", "pacing": "...", "clipable_moments": ["..."]}},
 "bible": {{"characters": [{{"id": "<id>", "name": "...", "tier": "main|supporting|recurring|crowd", "body": "...", "outfit": "...", "powers": {{"<power_id>": {{"descriptor": "...", "color": "#hex", "motion": "..."}}}}, "emotion_palette": ["..."], "voice": {{"engine": "moss-tts", "style_tag": "..."}}, "arc_summary": "..."}}], "locations": [{{"id": "<id>", "descriptor": "...", "lighting": "..."}}], "crowd_types": {{"<id>": {{"tier": "crowd", "descriptor": "..."}}}}}},
 "beats": {{"beats": [{{"scene": "s01", "location": "<id>", "time": "night|dusk|dawn|day", "mood": "...", "goal": "...", "turn": "...", "hook": "...", "est_shots": 30}}]}}}}
Do NOT include a shots key in this output."""


def build_scene_prompt(proj: dict, p_bible, db, scene: str,
                       beat: dict) -> str:
    """One-scene prompt with the running continuity state."""
    rows = sorted(db.all(project=proj["id"]),
                  key=lambda r: (r["scene"], r["shot_id"]))
    state: dict[str, dict] = {}
    char_end: dict[str, dict] = {}
    prev_scene_hook = ""
    last_scene = None
    beats = (proj.get("story") or {}).get("beats", {}).get("beats", [])
    for r in rows:
        j = r.get("json") or {}
        if (j.get("scene") or "") >= scene:
            break
        for mut in j.get("state_mutations", []):
            if not isinstance(mut, dict) or not mut.get("persist"):
                continue
            state.setdefault(str(mut.get("target") or ""), {})[
                str(mut.get("key") or "state")] = mut.get("value")
        for c in (j.get("characters") or []):
            if isinstance(c, dict) and c.get("id"):
                char_end[c["id"]] = j.get("end_state") or {}
        last_scene = j.get("scene")
    if last_scene:
        lb = next((b for b in beats if b.get("scene") == last_scene), {})
        prev_scene_hook = lb.get("hook", "")
    n = max(2, int(beat.get("est_shots") or 6))
    context = json.dumps({"state": state, "character_end_state": char_end,
                          "previous_scene_hook": prev_scene_hook},
                         ensure_ascii=False, indent=1) \
        if (state or char_end or prev_scene_hook) else \
        "(clean slate — this scene starts fresh)"
    return f"""You are writing ONE scene for a local AI movie pipeline (5-second shots). Scene {scene} of "{proj.get('title','')}".

SCENE BEAT: {scene} @ {beat.get('location')} ({beat.get('time')}, {beat.get('mood')})
- goal: {beat.get('goal')}
- turn: {beat.get('turn')}
- hook: {beat.get('hook')}
- write exactly {n} shots

RUNNING CONTINUITY STATE — these are facts established by earlier scenes. Reference them in actions and dialogue; NEVER contradict them:
{context}

CAST (use ONLY these ids):
{cast_lines(p_bible)}

LOCATIONS (use ONLY these ids and their listed places):
{loc_lines(p_bible)}

POWERS + LEVELS (use ONLY these):
{power_lines(p_bible)}

SHOT RULES:
{SHOT_RULES}

OUTPUT — use the COMPACT format (fits more shots per reply). Return ONLY this JSON object (no prose outside it):
{{"shots": {{"shots": [
  {{"scene": "{scene}", "location": "{beat.get('location')}", "time": "{beat.get('time')}", "shots": [
    ["<shot_size>", "<camera>", "<character_id>", "<emotion>", "<primary_action>", "<power_id_or_null>", "<tier|continuity>", {COMPACT_EXTRA}],
    ["D", "<character_id>", "<emotion>", "<dialogue line>"]
  ]}}
]}}}}
COMPACT ENTRY: [shot_size, camera, character_id, emotion, primary_action, power_id_or_null, marker, extra?] where marker is "hero"/"volume"/"standard" (tier) OR "cut"/"continue"/"match_action" (continuity, default CUT when omitted). The 8th element `extra` is OPTIONAL — include it whenever the shot uses a weapon, a specific loadout, a named place or a power level. shot_size from: extreme_close_up|close_up|medium_close_up|medium|medium_wide|wide|extreme_wide. camera from: static|slow_push_in|dolly|pan|crane|handheld|orbit|pull_back|tracking. emotions from each character's palette. Exactly {n} shot entries per scene block; dialogue lines as ["D", char, emotion, line] AFTER the shot they belong to."""


# ---------------------------------------------------------------- merging
_EXTRA_KEYS = ("weapon", "loadout", "place", "level", "handling", "sounds")


def validate_scene_package(data) -> tuple[list | None, list[str]]:
    """STRICT shape validation for one scene reply.

    The LLM must never be able to inject a malformed structure: unknown
    top-level keys, non-list shots, wrong-arity compact entries, dialogue
    entries that don't start with "D", and non-dict/unknown-key extras are
    all rejected with precise messages that feed the self-heal rewrite.
    """
    if not isinstance(data, dict):
        return None, ["reply is not a JSON object"]
    # the scene prompt asks for the {"shots": {"shots": [...]}} envelope;
    # accept it as well as the bare inner {"shots": [...]} object
    if set(data.keys()) == {"shots"} and isinstance(data["shots"], dict):
        data = data["shots"]
    errs: list[str] = []
    extra_keys = [k for k in data.keys() if k != "shots"]
    if extra_keys:
        errs.append(f"unexpected top-level keys {extra_keys} (only 'shots')")
    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        return None, errs + ["'shots' must be a non-empty list"]
    for bi, block in enumerate(shots):
        if not isinstance(block, dict):
            errs.append(f"shots[{bi}] is not an object")
            continue
        if not isinstance(block.get("scene"), str) or not block.get("scene"):
            errs.append(f"shots[{bi}] missing scene id")
        entries = block.get("shots")
        if not isinstance(entries, list) or not entries:
            errs.append(f"shots[{bi}].shots must be a non-empty list")
            continue
        for ei, e in enumerate(entries):
            if not isinstance(e, list) or not e:
                errs.append(f"entry {bi}.{ei} is not an array")
                continue
            if e[0] == "D":
                if len(e) != 4 or not all(isinstance(x, str) for x in e[1:]):
                    errs.append(f'dialogue {bi}.{ei} must be '
                                f'["D", char, emotion, line]')
                continue
            if len(e) < 5 or len(e) > 8:
                errs.append(f"shot {bi}.{ei} must have 5-8 elements "
                            f"(got {len(e)})")
                continue
            if not isinstance(e[0], str) or not isinstance(e[2], str):
                errs.append(f"shot {bi}.{ei}: shot_size and character id "
                            f"must be strings")
                continue
            if len(e) >= 8 and e[7] is not None:
                if not isinstance(e[7], dict):
                    errs.append(f"shot {bi}.{ei}: the 8th element must be an "
                                f"object or null")
                    continue
                bad = [k for k in e[7].keys() if k not in _EXTRA_KEYS]
                if bad:
                    errs.append(f"shot {bi}.{ei}: unknown extra keys {bad} "
                                f"(allowed: {list(_EXTRA_KEYS)})")
                    continue
                lvl = e[7].get("level")
                if lvl is not None:
                    try:
                        li = int(lvl)
                    except (TypeError, ValueError):
                        li = -1
                    if li < 1 or li > 10:
                        errs.append(f"shot {bi}.{ei}: level must be 1-10")
                        continue
    if errs:
        return None, errs
    return shots, []


def _project_family(store, pid: str) -> str:
    """The project's video family (ltx25 / h3 / ...) for budget-aware linting."""
    try:
        proj = store.get(pid) or {}
        return str(proj.get("video_family") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to get video family for %s: %s", pid, e)
        return ""


def merge_scene_shots(store, db, p_bible, pid: str, scene: str,
                      shots: list) -> dict:
    """Replace a scene's shots; validate the WHOLE project together;
    version the merged shots stage."""
    from linter import Linter
    from package_norm import normalize_package
    # compact list entries need scene context — wrap them in a scene block
    # (location/time come from the beat)
    if shots and not all(isinstance(s, dict) for s in shots):
        proj = store.get(pid) or {}
        beats = (proj.get("story") or {}).get("beats", {}).get("beats", [])
        beat = next((b for b in beats if b.get("scene") == scene), {})
        shots = [{"scene": scene,
                  "location": str(beat.get("location") or ""),
                  "time": str(beat.get("time") or "night"),
                  "shots": shots}]
    blocks = [b for b in shots if isinstance(b, dict)
              and isinstance(b.get("shots"), list)]
    if len(blocks) == 1 and blocks[0].get("scene") != scene:
        # one scene per call: a block the AI labelled "s5" for scene "s05"
        # replaced s05 with shots filed under s5 (ids derive from it)
        blocks[0]["scene"] = scene
    norm = normalize_package({"shots": {"shots": shots}}, p_bible)
    shots = norm["shots"]["shots"]
    for s in shots:
        s["project_id"] = pid
        if not s.get("scene"):
            s["scene"] = scene
    other = [r["json"] for r in db.all(project=pid)
             if r.get("scene") != scene]
    rep = Linter(p_bible, family=_project_family(store, pid)).lint_package(other + shots)
    if not rep.ok:
        return {"ok": False, "error": "lint failed", "report": rep.to_dict()}
    removed = 0
    for r in db.all(project=pid):
        if r.get("scene") == scene:
            db.delete_shot(r["shot_id"], pid)
            removed += 1
    db.upsert_shots(shots)
    merged = [dict(x) for x in other + shots]
    for m in merged:
        m.pop("project_id", None)
    store.set_story_stage(pid, "shots", {"shots": merged})
    return {"ok": True, "added": len(shots), "replaced": removed,
            "warnings": rep.warnings[:10]}


# ---------------------------------------------------------------- job
class ArchitectJob:
    """Runs full-script generation in a background thread.
    Resume-capable: story state is versioned through the project store."""

    def __init__(self, cfg=None, db=None, store=None) -> None:
        self.cfg = cfg or load_config()
        self.db = db
        self.store = store
        self._jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ control
    def start(self, pid: str) -> dict:
        from llm import LLMClient
        if not LLMClient(self.cfg).config_ok():
            return {"ok": False, "error": "architect LLM not configured — "
                                          "set base_url/model in the AI "
                                          "config"}
        with self._lock:
            job = self._jobs.get(pid)
            if job is not None and job.get("running"):
                return {"ok": False, "error": "already generating"}
            self._stop.discard(pid)
            self._jobs[pid] = {"running": True, "stage": "spine",
                               "done": 0, "total": 0, "message": "",
                               "log": [], "started": time.time()}
            t = threading.Thread(target=self._run, args=(pid,),
                                 daemon=True)
            self._threads[pid] = t
            t.start()
        return {"ok": True}

    def status(self, pid: str) -> dict:
        with self._lock:
            job = self._jobs.get(pid)
        return {"active": dict(job) if job else None}

    def stop(self, pid: str) -> dict:
        with self._lock:
            self._stop.add(pid)
        return {"ok": True}

    # -------------------------------------------------------------- run
    def _log(self, pid: str, msg: str) -> None:
        with self._lock:
            job = self._jobs.get(pid)
            if job is None:
                return
            job["log"].append(msg)
            job["log"] = job["log"][-30:]
            job["message"] = msg
        log.info("architect[%s]: %s", pid, msg)

    def _run(self, pid: str) -> None:
        from bible import Bible
        from llm import LLMClient, extract_json
        from package_norm import normalize_package
        from storygen import StoryArchitect

        job = self._jobs.get(pid) or {}
        try:
            proj = self.store.get(pid)
            if proj is None:
                raise RuntimeError("project not found")
            p_bible = Bible(pid)
            if not (p_bible.dir / "universe.yaml").exists():
                p_bible.snapshot_from_global(pid)
            client = LLMClient(self.cfg)
            arch = self.cfg.get("architect") or {}
            retries = int(arch.get("retries") or 2)

            # ---- 1. spine
            self._log(pid, "generating the spine (treatment + bible + beats)")
            package = None
            for attempt in range(retries + 1):
                if pid in self._stop:
                    self._log(pid, "stopped")
                    job["running"] = False
                    return
                out = client.complete(
                    _system_prompt(),
                    build_spine_prompt(proj, p_bible), json_mode=True)
                package = extract_json(out or "")
                if package is not None:
                    break
                self._log(pid, f"spine parse failed (attempt {attempt + 1}) — "
                               "asking the AI to fix its JSON")
                if attempt == retries:
                    raise RuntimeError("spine JSON unparseable after retries")
                # corrective follow-up
                out = client.complete(
                    _system_prompt(),
                    "Your last reply was not valid JSON. Return ONLY the "
                    "requested JSON object, nothing else.", json_mode=True)
                package = extract_json(out or "")
                if package is not None:
                    break
                if attempt == retries:
                    raise RuntimeError("spine JSON unparseable after retries")

            package = normalize_package(package, p_bible)
            sarch = StoryArchitect(proj, self.store, p_bible)
            results = {}
            for stage in ("treatment", "bible", "beats", "scenes"):
                if stage in package and isinstance(package[stage], dict):
                    try:
                        results[stage] = sarch.import_stage(
                            stage, package[stage])
                    except Exception as e:  # noqa: BLE001
                        results[stage] = {"ok": False, "error": str(e)}
            bad = {k: v.get("error") for k, v in results.items()
                   if not v.get("ok")}
            if bad:
                raise RuntimeError(f"spine stages failed: {bad}")
            proj = self.store.get(pid)
            beats = (proj.get("story") or {}).get("beats", {}).get("beats", [])
            if not beats:
                raise RuntimeError("spine produced no beats")

            # ---- 2. scenes, one at a time, continuity carried forward
            total = len(beats)
            job["total"] = total
            failed: list[str] = []
            self._log(pid, f"spine done — generating {total} scenes")
            for i, beat in enumerate(beats):
                if pid in self._stop:
                    self._log(pid, "stopped")
                    job["running"] = False
                    return
                scene = beat.get("scene")
                if not scene:
                    continue
                job["stage"] = f"scene {scene} ({i + 1}/{total})"
                job["done"] = i
                self._log(pid, f"writing scene {scene}")
                shots = None
                imported = False
                for attempt in range(retries + 1):
                    orig_prompt = build_scene_prompt(
                        proj, p_bible, self.db, scene, beat)
                    out = client.complete(
                        _system_prompt(), orig_prompt, json_mode=True)
                    if out is None:
                        # the client already retried transport errors: the
                        # endpoint is down — every later scene would fail too
                        raise RuntimeError("architect LLM request failed — "
                                           "check the AI config / server")
                    data = extract_json(out or "")
                    shots, verrs = (validate_scene_package(data)
                                    if data is not None else (None, ["JSON could not be parsed from reply"]))
                    if verrs:
                        # STRICT shape gate: malformed structures never reach
                        # the linter — the AI gets the exact errors back.
                        shots = None
                        self._log(pid, f"{scene}: JSON shape invalid — asking "
                                       f"the AI to fix ({attempt + 1})")
                        retry_msg = (
                            f"Your reply for scene {scene} failed STRICT JSON validation:\n" +
                            "\n".join(f"- {e}" for e in verrs[:8]) +
                            f"\n\nOriginal scene prompt was:\n{orig_prompt}\n\n" +
                            f"Your previous attempt was:\n{(out or '')[:2000]}\n\n" +
                            "Return ONLY the corrected JSON object for this scene in the same format."
                        )
                        out = client.complete(
                            _system_prompt(), retry_msg, json_mode=True)
                        data2 = extract_json(out or "")
                        shots, verrs2 = (validate_scene_package(data2)
                                         if data2 is not None else (None, ["JSON could not be parsed from retry reply"]))
                        if verrs2:
                            shots = None
                    if shots:
                        res = merge_scene_shots(self.store, self.db, p_bible,
                                                pid, scene, shots)
                        if res.get("ok"):
                            self._log(pid, f"{scene}: {res['added']} shots "
                                           f"imported")
                            imported = True
                            break
                        # self-heal: tell the AI what the linter rejected
                        report = res.get("report") or {}
                        errs = (report.get("errors") or [])[:8]
                        self._log(pid, f"{scene}: lint rejected — asking "
                                       f"the AI to fix ({attempt + 1})")
                        retry_msg = (
                            f"Your shots for scene {scene} were rejected by the script validator with these errors:\n"
                            + "\n".join(f"- {e}" for e in errs)
                            + f"\n\nOriginal scene prompt was:\n{orig_prompt}\n\n"
                            + f"Your previous attempt was:\n{(out or '')[:2000]}\n\n"
                            + "Return ONLY the corrected JSON object for this scene (same format)."
                        )
                        out = client.complete(
                            _system_prompt(), retry_msg, json_mode=True)
                        data = extract_json(out or "")
                        shots, verrs3 = (validate_scene_package(data)
                                         if data is not None else (None, ["JSON could not be parsed from retry reply"]))
                        if verrs3:
                            shots = None
                        if shots:
                            res = merge_scene_shots(
                                self.store, self.db, p_bible, pid, scene,
                                shots)
                            if res.get("ok"):
                                self._log(pid, f"{scene}: fixed and "
                                               f"imported")
                                imported = True
                                break
                if not imported:
                    # was: a scene whose last attempt parsed but failed lint
                    # was skipped silently and the job claimed "complete".
                    # Keep going (the other scenes are independent) and
                    # report it; the scene can be re-imported on its own.
                    failed.append(str(scene))
                    self._log(pid, f"{scene}: still invalid after "
                                   f"{retries + 1} attempt(s) — skipped")
            job["done"] = total
            job["running"] = False
            if failed:
                job["stage"] = "partial"
                job["failed_scenes"] = failed
                self._log(pid, f"script incomplete: {len(failed)} of {total} "
                               f"scene(s) failed validation "
                               f"({', '.join(failed)}) — fix or re-import "
                               f"them from the Story tab")
            else:
                job["stage"] = "done"
                self._log(pid, f"full script complete: {total} scenes")
        except Exception as e:  # noqa: BLE001
            log.exception("architect job failed for %s", pid)
            job["message"] = f"failed: {e}"
            job["running"] = False
            with self._lock:
                self._stop.discard(pid)
