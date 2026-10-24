"""Kinoloom - Projects & Story Architect domain router.

Provides endpoints for project CRUD, story generation, AI architect prompts,
normalization, version history, rollback, and packaging.
"""
from __future__ import annotations

import os
import re
import time
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import common
from .common import store, db, director, cfg, architect_job, _proj_guard, Bible
from core import ROOT, is_safe_component, load_config, log_setup, now, save_config, to_jsonable
from projects import ProjectStore
from storygen_llm import ArchitectJob
from video_models import DEFAULT_MODEL

router = APIRouter()
log = log_setup("common.projects")

# the video model a new project starts on: the adopted LTX-2.5 quality config
# (also the movie pipeline's fallback, video_models.default_model_id)
DEFAULT_VIDEO_MODEL = DEFAULT_MODEL

def __getattr__(name: str):
    return getattr(common, name)

class ProjectBody(BaseModel):
    title: str
    idea: str = ""
    genre: str = ""
    tone: str = ""
    duration_target: int = 60
    mode: str = "checkpoints"
    gates: dict | None = None


class ProjectUpdateBody(BaseModel):
    title: str | None = None
    mode: str | None = None
    gates: dict | None = None
    duration_target: int | None = None
    idea: str | None = None
    genre: str | None = None
    tone: str | None = None
    # movie-pipeline video model: a video_models registry id such as
    # "ltx25_s6_cq" (LTX-2.5 quality config); "" = the studio default
    # (video_models.default_model_id, LTX-2.5). Wan 2.2 is legacy and renders
    # only when its id is picked here.
    video_family: str | None = None
    # per-tier video model overrides: {"hero": "<id>", "standard": "<id>",
    # "volume": "<id>"} — empty values fall back to video_family. Lets hero
    # shots run on H3 while standard/volume run on the faster LTX path.
    video_models: dict | None = None
    # per-project post-processing (upscale/face-fix/RIFE/grade) — default OFF:
    # shots are delivered exactly as rendered (the agreed LTX-2.5 workflow)
    post_processing: bool | None = None
    # which worker PC runs the post stage ("" = local). The remote lane sends
    # the clip to the worker: RIFE + RealESRGAN x4 -> mp4 back (queue-safe).
    post_worker: str | None = None
    # runs of CONTINUE shots on a seamless model render as one continuous
    # take (default on); off = every shot renders alone
    seamless_takes: bool | None = None


@router.get("/api/projects")
def projects_list() -> dict:
    return {"projects": store.list()}


@router.post("/api/projects")
def projects_create(body: ProjectBody) -> dict:
    if body.mode not in ("autopilot", "checkpoints", "manual"):
        return {"ok": False, "error": "mode must be autopilot|checkpoints|manual"}
    gates = body.gates or {}
    if body.mode == "autopilot":
        gates = {"story": "auto", "keyframes": "auto", "review": "auto"}
    proj = store.create(body.title, body.idea, body.genre, body.tone,
                        body.duration_target, body.mode, gates)
    from bible import Bible
    try:
        Bible(proj["id"]).snapshot_from_global(proj["id"])
    except Exception as e:
        print(f"project bible snapshot failed: {e}")
    # New projects default to the adopted LTX-2.5 quality config (1080p,
    # reference-driven, seamless takes for CONTINUE runs); the Generate tab can
    # still switch the project to a MiniMax H3 tier or the legacy Wan 2.2
    # model. ProjectBody has
    # no video_family field today - an explicit field added later would take
    # precedence over this default.
    try:
        store.update(proj["id"], video_family=getattr(body, "video_family", None)
                     or DEFAULT_VIDEO_MODEL)
    except Exception as e:
        print(f"project default video_family failed: {e}")
    return {"ok": True, "project": proj}


@router.get("/api/projects/{pid}")
def project_get(pid: str) -> dict:
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    from bible import Bible
    stats = db.project_stats(pid)
    p_bible = Bible(pid)
    refs_dir = p_bible.dir / "refs"
    story = proj.get("story") or {}
    active = story.get("reference") if isinstance(story, dict) else None
    refs = ([{"file": f.name, "active": f.name == active}
             for f in sorted(refs_dir.glob("*")) if f.is_file()]
            if refs_dir.is_dir() else [])
    return {"project": proj, "bible": p_bible.all(),
            "stats": stats, "director": director.status(pid),
            "refs": {"refs": refs}}


@router.post("/api/projects/{pid}")
def project_update(pid: str, body: ProjectUpdateBody) -> dict:
    kw = {k: v for k, v in body.dict().items() if v is not None}
    if "mode" in kw and kw["mode"] not in ("autopilot", "checkpoints", "manual"):
        return {"ok": False, "error": "mode must be autopilot|checkpoints|manual"}
    if "gates" in kw and not isinstance(kw["gates"], dict):
        return {"ok": False, "error": "gates must be a dict"}
    if "video_family" in kw:
        # Validated here so a typo cannot be stored: an unknown id would fall
        # back to the default model, which is confusing to diagnose later.
        vf = str(kw["video_family"] or "")
        if vf:
            try:
                import video_models
                if vf not in video_models.VIDEO_MODELS:
                    return {"ok": False, "error": f"unknown video model '{vf}'"}
            except Exception as e:
                log.exception("projects operation failed: %s", e)
                return JSONResponse(status_code=500, content={"ok": False, "error": f"video model registry error: {e}"})
        kw["video_family"] = vf
    if "video_models" in kw:
        # per-tier overrides: validate every non-empty id against the registry
        # (a typo would fall back to the default model — confusing to
        # diagnose later)
        vm = kw["video_models"] or {}
        if not isinstance(vm, dict):
            return {"ok": False, "error": "video_models must be an object"}
        clean: dict = {}
        try:
            import video_models as _vm
            known = set(_vm.VIDEO_MODELS.keys())
        except Exception as e:
            log.exception("projects operation failed: %s", e)
            return JSONResponse(status_code=500, content={"ok": False, "error": f"video model registry error: {e}"})
        for tier in ("hero", "standard", "volume"):
            mid = str(vm.get(tier) or "").strip()
            if mid and mid not in known:
                return {"ok": False,
                        "error": f"unknown video model '{mid}' for tier "
                                 f"'{tier}'"}
            clean[tier] = mid
        kw["video_models"] = clean
    if "post_processing" in kw:
        kw["post_processing"] = 1 if kw["post_processing"] else 0
    if "seamless_takes" in kw:
        kw["seamless_takes"] = 1 if kw["seamless_takes"] else 0
    if "post_worker" in kw:
        pw = str(kw["post_worker"] or "").strip()
        if pw and pw != "local":
            try:
                import workers as workers_mod
                if workers_mod.get(pw) is None:
                    return {"ok": False,
                            "error": f"unknown worker '{pw}' (register it on "
                                     f"the Workers page first)"}
            except Exception as e:
                log.exception("projects operation failed: %s", e)
                return JSONResponse(status_code=500, content={"ok": False, "error": f"worker registry error: {e}"})
            kw["post_worker"] = pw
        else:
            kw["post_worker"] = ""
    ok = store.update(pid, **kw)
    return {"ok": ok}


@router.post("/api/projects/{pid}/delete")
def project_delete(pid: str) -> dict:
    return {"ok": True, **store.delete(pid)}


class StoryGenBody(BaseModel):
    stages: list[str] | None = None
    idea: str | None = None
    cast_config: dict | None = None


@router.post("/api/projects/{pid}/story/generate")
def project_story_generate(pid: str, body: StoryGenBody) -> dict:
    from bible import Bible
    from storygen import StoryArchitect
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    p_bible = Bible(pid)
    if not (p_bible.dir / "universe.yaml").exists():
        p_bible.snapshot_from_global(pid)
    res = StoryArchitect(proj, store, p_bible).generate(
        stages=body.stages, idea=body.idea, cast_config=body.cast_config)
    if res.get("ok") and body.cast_config:
        story = proj.get("story") or {}
        story = dict(story) if isinstance(story, dict) else {}
        story["cast_config"] = body.cast_config
        store.update(pid, story=story)
    return res


class StageImportBody(BaseModel):
    stage: str
    content: dict | None = None
    text: str | None = None


@router.get("/api/projects/{pid}/story/architect-prompt")
def project_architect_prompt(pid: str, kind: str = "full") -> dict:
    """The full system prompt to paste into any chat AI.

    kind=full  : one-shot package for short films (<= ~60 shots)
    kind=spine : treatment + bible + beats ONLY — the master plan for long
                 films; shots are then generated scene-by-scene via
                 /story/scene-prompt.

    The vocabulary comes from the SAME builders the built-in architect uses
    (storygen_llm.cast_lines / loc_lines / power_lines + SHOT_RULES), and the
    storytelling skill is embedded — a cloud model must receive everything:
    weapons, loadouts, per-scene defaults, named places, power levels and the
    reference budget.
    """
    from bible import Bible
    from storygen_llm import (cast_lines as _cast_lines,
                              loc_lines as _loc_lines,
                              power_lines as _power_lines,
                              SHOT_RULES as _SHOT_RULES,
                              COMPACT_EXTRA as _COMPACT_EXTRA,
                              _skill_text)
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    p_bible = Bible(pid)
    cast_txt = _cast_lines(p_bible)
    loc_txt = _loc_lines(p_bible)
    power_txt = _power_lines(p_bible)
    skill = _skill_text("storytelling")
    dur = int(proj.get("duration_target") or 60)
    n_shots = max(4, -(-dur // 5))
    n_scenes = max(2, min(30, round(n_shots / 28)))
    head = (f"You are the Story Architect for the Kinoloom pipeline. "
            f"Every shot renders as ONE ~5-second video clip (LTX-2.5 / "
            f"MiniMax H3), so write for the RENDERER: each shot is one "
            f"visible change, camera-ready, JSON only.\n")
    if skill:
        head += ("\nFollow this skill exactly:\n\n" + skill + "\n")
    vocab = (f"\n=== PROJECT ===\n{proj.get('genre','')} · tone "
             f"\"{proj.get('tone','')}\" · {dur}s = {n_shots} shots across "
             f"{n_scenes} scenes (25-40 shots per scene)\n"
             f"IDEA: {proj.get('idea','')}\n"
             f"AUTHORITY: the IDEA is a premise that may predate the current "
             f"cast. The CAST / LOCATIONS / POWERS lists below are "
             f"AUTHORITATIVE: if the idea names a character, place or power "
             f"that is not listed, adapt it to the closest listed id (or make "
             f"it a crowd type). Never output an id that is not in the lists.\n\n"
             f"CAST (use ONLY these ids; weapons/loadouts/levels are the "
             f"presentation vocabulary):\n{cast_txt}\n\n"
             f"LOCATIONS (use ONLY these ids and their listed places):\n"
             f"{loc_txt}\n\n"
             f"POWERS + LEVELS (use ONLY these):\n{power_txt}\n\n"
             f"SHOT RULES:\n{_SHOT_RULES}\n")
    # ---- stale-premise guard: names the IDEA mentions that are not in the
    # cast. The premise is free text written at project creation, so it can
    # predate the current cast (measured: an idea kept naming "Morgana" while
    # the cast was only Sara — the AI adapts via the AUTHORITY clause, but the
    # human should be told exactly which names are stale).
    idea = str(proj.get("idea") or "")
    cast_words = set()
    for c in p_bible.characters().values():
        cast_words.add(str(c.get("id") or "").lower())
        cast_words.add(str(c.get("name") or "").lower())
    for l in p_bible.locations().values():
        cast_words.add(str(l.get("id") or "").lower())
        cast_words.add(str(l.get("name") or "").lower())
    _STOP = {"the", "and", "but", "when", "while", "with", "his", "her",
             "their", "they", "she", "he", "its", "our", "your", "from",
             "into", "through", "every", "all", "after", "before", "then",
             "that", "this", "those", "these", "one", "two", "only", "once",
             "because", "if", "not", "new", "old", "under", "over", "who",
             "whose", "which", "where", "what", "there", "here", "no", "of",
             "in", "on", "at", "as", "to", "a", "an", "it", "is", "are",
             # thematic / title words that are not character ids
             "ice", "queen", "king", "lord", "lady", "prince", "princess",
             "emperor", "empress", "god", "goddess", "city", "kingdom",
             "empire", "winter", "summer", "spring", "autumn", "frost",
             "snow", "blood", "fire", "sun", "moon", "star", "stars",
             "realm", "world", "war", "death", "life", "light", "dark",
             "darkness", "shadow", "order", "house", "court", "crown",
             "throne", "magic", "power", "storm", "sea", "sky", "land",
             "north", "south", "east", "west", "great", "high", "eternal"}
    idea_warnings: list[str] = []
    for m in re.finditer(r"\b[A-Z][a-z]{2,}\b", idea):
        tok = m.group(0)
        low = tok.lower()
        if low in cast_words or low in _STOP or tok in idea_warnings:
            continue
        # sentence-initial capitals are grammar, not names ("Exiled ice
        # queen Sara ..." must not flag "Exiled")
        before = idea[:m.start()].rstrip()
        if not before or before[-1] in ".!?\"'":
            continue
        idea_warnings.append(tok)
    if kind == "spine":
        prompt = (head + vocab +
                  f"\nWrite the MASTER PLAN (spine) only — treatment, bible, "
                  f"beats. Do NOT write shots yet.\n"
                  f"BEAT RULES:\n"
                  f"- Exactly {n_scenes} beats. Every scene: goal -> obstacle "
                  f"-> turn (something changes), ends on a hook.\n"
                  f"- First scene = inciting incident; scene "
                  f"{n_scenes // 2} = midpoint reversal; last scene = climax "
                  f"+ resolution hook.\n"
                  f"- Reward cadence: a payoff every 2-3 minutes. Escalate "
                  f"powers/threats scene by scene; the power level curve "
                  f"climbs to 10 at the finale.\n"
                  f"- Give each scene a specific location (or a named place), "
                  f"time, mood, and est_shots that sum to {n_shots}.\n"
                  f"- Characters/locations listed above ALREADY EXIST: the "
                  f"bible section may only refine their TEXT (name/body/"
                  f"outfit/descriptor) — never remove weapons, loadouts, "
                  f"references or places.\n\n"
                  f"OUTPUT — return ONLY this JSON object (no prose outside "
                  f"it):\n"
                  f'{{"treatment": {{"logline": "...", "synopsis": "...", '
                  f'"themes": ["..."], "protagonist": "<id>", '
                  f'"antagonist": "<id>", "escalation": ["..."], '
                  f'"ending": "...", "pacing": "...", '
                  f'"clipable_moments": ["..."]}},\n'
                  f' "bible": {{"characters": [{{"id": "<existing id>", '
                  f'"name": "...", "tier": "main|supporting|recurring|crowd", '
                  f'"body": "...", "outfit": "...", "powers": {{"<power_id>": '
                  f'{{"descriptor": "...", "color": "#hex", "motion": "..."}}}}, '
                  f'"emotion_palette": ["..."], "voice": {{"engine": '
                  f'"moss-tts", "style_tag": "..."}}, "arc_summary": "..."}}], '
                  f'"locations": [{{"id": "<existing id>", "descriptor": '
                  f'"...", "lighting": "..."}}], "crowd_types": '
                  f'{{"<id>": {{"tier": "crowd", "descriptor": "..."}}}}}},\n'
                  f' "beats": {{"beats": [{{"scene": "s01", "location": '
                  f'"<id>", "time": "night|dusk|dawn|day", "mood": "...", '
                  f'"goal": "...", "turn": "...", "hook": "...", '
                  f'"est_shots": 30}}]}}}}\n'
                  f"Do NOT include a shots key in this output.")
        return {"prompt": prompt, "kind": "spine", "shot_count": n_shots,
                "scene_count": n_scenes, "idea_warnings": idea_warnings}

    prompt = (head + vocab +
              f"\nWrite the FULL package: treatment + bible + beats + every "
              f"shot (exactly {n_shots}). Use the COMPACT shot format so the "
              f"reply fits:\n\n"
              f'{{"treatment": {{"logline": "...", "synopsis": "...", '
              f'"themes": ["..."], "protagonist": "<id>", '
              f'"antagonist": "<id>", "escalation": ["..."], '
              f'"ending": "...", "pacing": "...", '
              f'"clipable_moments": ["..."]}},\n'
              f' "bible": {{"characters": [{{"id": "<existing id>", '
              f'"name": "...", "tier": "...", "body": "...", "outfit": "...", '
              f'"powers": {{"<power_id>": {{"descriptor": "...", '
              f'"color": "#hex", "motion": "..."}}}}, "emotion_palette": '
              f'["..."], "voice": {{"engine": "moss-tts", "style_tag": "..."}}, '
              f'"arc_summary": "..."}}], "locations": [{{"id": "<existing id>", '
              f'"descriptor": "...", "lighting": "..."}}], "crowd_types": '
              f'{{"<id>": {{"tier": "crowd", "descriptor": "..."}}}}}},\n'
              f' "beats": {{"beats": [{{"scene": "s01", "location": "<id>", '
              f'"time": "night|dusk|dawn|day", "mood": "...", "goal": "...", '
              f'"turn": "...", "hook": "...", "est_shots": 6}}]}},\n'
              f' "shots": {{"shots": [\n'
              f'   {{"scene": "s01", "location": "<id>", "time": "night", '
              f'"shots": [\n'
              f'     ["<shot_size>", "<camera>", "<character_id>", "<emotion>", '
              f'"<primary_action>", "<power_id_or_null>", '
              f'"<tier|continuity>", {_COMPACT_EXTRA}],\n'
              f'     ["D", "<character_id>", "<emotion>", "<line>"]\n'
              f'   ]}}\n'
              f' ]}}\n'
              f"}}\n\n"
              f"shot_size from: extreme_close_up|close_up|medium_close_up|"
              f"medium|medium_wide|wide|extreme_wide · camera from: static|"
              f"slow_push_in|dolly|pan|crane|handheld|orbit|pull_back|"
              f"tracking. One scene block per scene, in order.")
    return {"prompt": prompt, "kind": "full", "shot_count": n_shots,
            "idea_warnings": idea_warnings}


@router.post("/api/projects/{pid}/story/rewrite-idea")
def project_rewrite_idea(pid: str) -> dict:
    """Rewrite the premise to match the CURRENT cast (architect LLM).

    The idea is free text saved at project creation; after cast changes it
    can reference characters that no longer exist (measured: an idea kept
    naming a character that had been replaced). This rewrites it from the
    live CAST/LOCATIONS/POWERS vocabulary. The text is returned for REVIEW —
    the operator saves it with the normal project update.
    """
    from bible import Bible
    from storygen_llm import cast_lines as _cl, loc_lines as _ll, \
        power_lines as _pl
    from llm import LLMClient
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    client = LLMClient(load_config())
    if not client.config_ok():
        return {"ok": False, "error": "architect LLM not configured — set it "
                                      "in AI config (Story page)"}
    b = Bible(pid)
    system = ("You write ONE movie premise paragraph (2-3 sentences) for a "
              "pipeline where every shot is a 5-second clip. Concrete nouns, "
              "visible stakes, a ticking clock. Use ONLY the cast, locations "
              "and powers given — never invent names. Return the paragraph "
              "only, no quotes, no preamble.")
    user = (f"Genre: {proj.get('genre','')} · tone: {proj.get('tone','')}\n"
            f"CAST:\n{_cl(b)}\n\nLOCATIONS:\n{_ll(b)}\n\n"
            f"POWERS:\n{_pl(b)}\n\n"
            f"Outdated premise for reference:\n{proj.get('idea','')}")
    out = client.complete(system, user, max_tokens=400)
    if not out:
        return {"ok": False, "error": "the LLM returned nothing — check AI "
                                      "config / connection"}
    return {"ok": True, "idea": out.strip().strip('"')}


def _normalize_package(package: dict, p_bible) -> dict:
    """Shared normalization lives in package_norm.py (used by both the
    paste endpoints and the LLM Story Architect)."""
    from package_norm import normalize_package
    return normalize_package(package, p_bible)


@router.get("/api/projects/{pid}/story/scene-prompt")
def project_scene_prompt(pid: str, scene: str) -> dict:
    """One-scene prompt for long films: includes the running continuity
    state (accumulated wounds/weather/props + previous scene hook) so the
    AI continues exactly where the story left off. Requests the compact
    storyboard format (2-4 scenes fit per chat reply)."""
    from bible import Bible
    from storygen_llm import build_scene_prompt
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    if not scene or not is_safe_component(scene):
        return {"ok": False, "error": "invalid scene id"}
    beats = (proj.get("story") or {}).get("beats", {}).get("beats", [])
    beat = next((b for b in beats if b.get("scene") == scene), {})
    if not beat:
        return {"ok": False, "error": f"scene {scene} has no beat — "
                                       "import the spine first"}
    p_bible = Bible(pid)
    prompt = build_scene_prompt(proj, p_bible, db, scene, beat)
    return {"ok": True, "prompt": prompt, "scene": scene,
            "shots": max(2, int(beat.get("est_shots") or 6))}


def _prune_stale_shots(pid: str, p_bible) -> dict:
    """Delete project shots whose characters or location no longer exist
    in the project Bible (leftovers from a previous cast/story)."""
    chars = set(p_bible.characters().keys())
    locs = set(p_bible.locations().keys())
    removed = []
    for r in db.all(project=pid):
        j = r.get("json") or {}
        bad = False
        if j.get("location") and j["location"] not in locs:
            bad = True
        for c in (j.get("characters") or []):
            if isinstance(c, dict) and c.get("id") and c["id"] not in chars:
                bad = True
                break
        if bad:
            db.delete_shot(r["shot_id"], pid)
            removed.append(r["shot_id"])
    return {"removed": removed, "count": len(removed)}


@router.post("/api/projects/{pid}/story/prune-stale")
def project_prune_stale(pid: str) -> dict:
    """Remove shots referencing a cast/world that no longer exists."""
    from bible import Bible
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    return {"ok": True, **_prune_stale_shots(pid, Bible(pid))}


@router.post("/api/projects/{pid}/story/import-file")
def project_story_import_file(pid: str, body: dict) -> dict:
    """Import an AI reply directly from a file on disk (bypasses clipboard
    truncation). path must be a .txt/.json/.md file inside the studio tree
    or a folder listed in security.media_roots."""
    from bible import Bible
    from storygen import StoryArchitect
    from llm import extract_json
    from core import InvalidInput, resolve_user_path
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    path = str(body.get("path") or "").strip().strip('"')
    if not path or len(path) > 260:
        return {"ok": False, "error": "invalid path"}
    try:
        p = resolve_user_path(path, cfg)
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except InvalidInput as e:
        return {"ok": False, "error": str(e)}
    if p.suffix.lower() not in (".txt", ".json", ".md", ".log"):
        return {"ok": False, "error": "must be a .txt/.json/.md file"}
    if p.stat().st_size > 32 * 1024 * 1024:
        return {"ok": False, "error": "file too large (32 MB max)"}
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.exception("projects operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"read failed: {e}"})
    package = extract_json(raw)
    if package is None:
        return {"ok": False, "error": "no JSON found in the file — the "
                                      "reply may be truncated; ask the AI "
                                      "to continue and paste/point at both "
                                      "halves"}
    p_bible = Bible(pid)
    try:
        package = _normalize_package(package, p_bible)
    except Exception as e:
        log.exception("projects operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"normalization failed: {e}"})
    arch = StoryArchitect(proj, store, p_bible)
    results = {}
    stages = [s for s in ("treatment", "bible", "beats", "scenes", "shots")
              if s in package and isinstance(package[s], dict)]
    for stage in stages:
        try:
            res = arch.import_stage(stage, package[stage])
            results[stage] = res
        except Exception as e:  # noqa: BLE001
            results[stage] = {"ok": False, "error": str(e)}
    ok = all(r.get("ok") for r in results.values())
    pruned = _prune_stale_shots(pid, p_bible) if "bible" in stages else \
        {"count": 0, "removed": []}
    return {"ok": ok, "stages": results, "file": path, "pruned": pruned}


@router.post("/api/projects/{pid}/story/import-scene-shots")
def project_import_scene_shots(pid: str, body: dict) -> dict:
    """Merge one scene's AI-generated shots into the project: replaces
    that scene's existing shots, validates the WHOLE project together,
    versions the shots stage. Scene-by-scene long-film workflow."""
    from bible import Bible
    from storygen_llm import merge_scene_shots
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    scene = body.get("scene")
    shots = body.get("shots")
    if not scene or not is_safe_component(scene):
        return {"ok": False, "error": "scene id required"}
    if (not isinstance(shots, list) or not shots) and body.get("text"):
        from llm import extract_json
        data = extract_json(str(body["text"]))
        if isinstance(data, dict):
            s2 = data.get("shots")
            if isinstance(s2, dict):
                s2 = s2.get("shots")
            shots = s2 if isinstance(s2, list) else None
    if not isinstance(shots, list) or not shots:
        return {"ok": False, "error": "scene + shots[] required"}
    try:
        return merge_scene_shots(store, db, Bible(pid), pid, scene, shots)
    except Exception as e:
        log.exception("projects operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"import failed: {e}"})

@router.post("/api/projects/{pid}/story/import-all")
def project_story_import_all(pid: str, body: dict) -> dict:
    """Paste the full AI package (or any fragment of it) in one shot:
    chat-AI schema deviations and the compact storyboard format are
    normalized/expanded, stages distributed, shots validated + ingested.
    Raw text pastes (prompt text + JSON reply, even truncated) are
    auto-extracted."""
    from bible import Bible
    from storygen import StoryArchitect
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    package = body.get("package")
    if package is None and isinstance(body, dict) and body.get("text"):
        from llm import extract_json
        package = extract_json(str(body["text"]))
        if package is None:
            return {"ok": False, "error": "no JSON found in the pasted text "
                                          "- the reply may be truncated; "
                                          "ask the AI to continue and "
                                          "paste both halves together"}
    if not isinstance(package, dict):
        return {"ok": False, "error": "package must be a JSON object"}
    p_bible = Bible(pid)
    try:
        package = _normalize_package(package, p_bible)
    except Exception as e:
        log.exception("projects operation failed: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": f"normalization failed: {e}"})
    arch = StoryArchitect(proj, store, p_bible)
    results = {}
    stages = [s for s in ("treatment", "bible", "beats", "scenes", "shots")
              if s in package and isinstance(package[s], dict)]
    for stage in stages:
        try:
            res = arch.import_stage(stage, package[stage])
            results[stage] = res
        except Exception as e:  # noqa: BLE001
            results[stage] = {"ok": False, "error": str(e)}
    ok = all(r.get("ok") for r in results.values())
    return {"ok": ok, "stages": results}


@router.get("/api/projects/{pid}/story/ai-config")
def project_ai_config(pid: str) -> dict:
    if not store.get(pid):
        raise FileNotFoundError(pid)
    arch = dict(cfg.get("architect") or {})
    key = arch.get("api_key") or ""
    arch["api_key"] = ("*" * 8 + key[-4:]) if len(key) > 4 else key
    return {"architect": arch}


class AiConfigBody(BaseModel):
    base_url: str
    api_key: str = ""
    model: str


@router.post("/api/projects/{pid}/story/ai-config")
def project_ai_config_save(pid: str, body: AiConfigBody) -> dict:
    if not store.get(pid):
        raise FileNotFoundError(pid)
    from urllib.parse import urlsplit
    from core import save_config
    arch = dict(cfg.get("architect") or {})
    new_url = body.base_url.strip()
    parts = urlsplit(new_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {"ok": False, "error": "base_url must be an http(s) URL"}

    def _origin(u: str) -> tuple[str, str]:
        s = urlsplit(str(u or ""))
        return (s.scheme.lower(), s.netloc.lower())

    new_key = body.api_key if body.api_key and not body.api_key.startswith("*") else ""
    if new_key:
        arch["api_key"] = new_key
    elif _origin(new_url) != _origin(arch.get("base_url", "")) and arch.get("api_key"):
        # the stored key was issued for the OLD endpoint: never send it to a
        # new host just because the masked field was left unchanged
        arch["api_key"] = ""
        log.info("architect endpoint changed host — stored API key cleared")
    arch["base_url"] = new_url
    arch["model"] = body.model.strip()
    cfg["architect"] = arch
    save_config(cfg)
    return {"ok": True, "api_key_cleared": not new_key and not arch.get("api_key")}


@router.post("/api/architect/test")
def architect_test(body: dict) -> dict:
    """Test the configured LLM endpoint (optionally with a new base_url)."""
    from llm import LLMClient
    return LLMClient(cfg).test()


@router.post("/api/projects/{pid}/story/generate-ai")
def project_generate_ai(pid: str) -> dict:
    """ONE click, ONE prompt -> the ENTIRE script: the system handles all
    LLM continuation calls (spine -> every scene with continuity carried
    forward -> self-healing lint fixes) until the full shot list exists."""
    return architect_job.start(pid)


@router.get("/api/projects/{pid}/story/generate-ai/status")
def project_generate_ai_status(pid: str) -> dict:
    return architect_job.status(pid)


@router.post("/api/projects/{pid}/story/generate-ai/stop")
def project_generate_ai_stop(pid: str) -> dict:
    return architect_job.stop(pid)


@router.post("/api/projects/{pid}/story/import")
def project_story_import(pid: str, body: StageImportBody) -> dict:
    from bible import Bible
    from storygen import StoryArchitect
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    content = body.content
    if content is None and body.text:
        from llm import extract_json
        content = extract_json(body.text)
        if content is None:
            return {"ok": False, "error": "no JSON found in the pasted text"}
    if not isinstance(content, dict):
        return {"ok": False, "error": "content must be a JSON object"}
    return StoryArchitect(proj, store, Bible(pid)).import_stage(
        body.stage, content)


@router.get("/api/projects/{pid}/story/versions")
def project_story_versions(pid: str, stage: str | None = None) -> dict:
    return {"versions": store.story_versions(pid, stage)}


class RollbackBody(BaseModel):
    stage: str
    version: int


@router.post("/api/projects/{pid}/story/rollback")
def project_story_rollback(pid: str, body: RollbackBody) -> dict:
    return {"ok": store.rollback_stage(pid, body.stage, body.version)}


class ClearStoryBody(BaseModel):
    stages: list[str] | None = None      # None = every story stage
    keep_history: bool = False           # keep the version history
    also_shots: bool = True              # delete the shot rows too


@router.post("/api/projects/{pid}/story/clear")
def project_story_clear(pid: str, body: ClearStoryBody) -> dict:
    """Delete the live story (and history/shots) — a clean slate for a new
    package. The cast is never touched."""
    proj, err = _proj_guard(pid)
    if err:
        return err
    stages = body.stages
    if body.also_shots and (stages is None or "shots" in stages):
        # the shot rows ARE the story's shots stage — remove them so the
        # Scenes & Shots tab is empty (and a fresh package can't collide)
        try:
            removed = 0
            for r in db.all(project=pid):
                db.delete_shot(r["shot_id"], pid)
                removed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("story clear: shot rows not removed: %s", e)
    res = store.clear_story(pid, stages, body.keep_history)
    return {"ok": True, **res}


class DeleteVersionBody(BaseModel):
    stage: str
    version: int


@router.post("/api/projects/{pid}/story/delete-version")
def project_story_delete_version(pid: str, body: DeleteVersionBody) -> dict:
    """Remove ONE version from the history (the live story is untouched)."""
    proj, err = _proj_guard(pid)
    if err:
        return err
    ok = store.delete_story_version(pid, body.stage, body.version)
    return {"ok": bool(ok),
            "error": None if ok else "version not found"}

