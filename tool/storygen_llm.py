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
    """The architect's role + the storytelling skill: the system message of
    the AI writer, and the head of the prompt copied into a chat AI."""
    base = ("You are the Story Architect for the Kinoloom pipeline. Every shot "
            "renders as ONE ~5-second video clip (LTX-2.5 / MiniMax H3), so write "
            "for the RENDERER: each shot is one visible change, camera-ready. "
            "Reply with strict JSON only, never prose.")
    skill = _skill_text("storytelling")
    if skill:
        return base + "\n\nFollow this skill exactly:\n\n" + skill
    return base


def cast_lines(p_bible) -> str:
    """The LLM's full vocabulary: identities AND their presentation options.

    Weapons, outfits and loadouts must be listed or the script cannot use
    them — a hallucinated weapon/loadout id is rejected by the linter and
    forces a scene rewrite. Each character's look is the cast's (its
    pictures and the text written for them): the story writes what people
    do, never how they look.
    """
    from cast_look import body_text
    chars = p_bible.characters()
    out = []
    for c in sorted(chars.values(), key=lambda x: x.get("id", "")):
        powers = ", ".join(c.get("powers") or {}.keys()) or "none"
        loras = ""
        l = c.get("lora")
        if isinstance(l, dict) and l.get("file"):
            loras = f" | LoRA: {l['file']}"
        emos = ", ".join(c.get("emotion_palette") or [])
        look = "; ".join(x for x in (body_text(c),
                                     f"wearing {str(c['outfit']).strip()}"
                                     if str(c.get("outfit") or "").strip() else "") if x)
        line = (f"- {c.get('id')} ({c.get('name', '')}, tier "
                f"{c.get('tier', '?')}) — look (the cast's, fixed): "
                f"{look or 'not written in the cast yet: do not invent one'} — powers: {powers}{loras} — "
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
    """Locations with their NAMED PLACES (reference variants) and what each
    picture shows: the background the video model receives, so a scene set
    there is written in that set and light, never an invented one."""
    from cast_look import place_views
    locs = p_bible.locations()
    lines = []
    for l in sorted(locs.values(), key=lambda x: x.get("id", "")):
        line = f"- {l.get('id')}: {l.get('descriptor', '')}"
        views = place_views(l)
        main = next((txt for tag, txt in views if not tag and txt), "")
        if main:
            line += f" — its picture shows: {main}"
        tags = [(tag, txt) for tag, txt in views if tag]
        if tags:
            line += " — places (use as place): " + "; ".join(
                f"{tag} = {txt}" if txt else tag for tag, txt in tags)
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


def shot_rules(take_len: int) -> str:
    """The shot rules every way of writing the story gets. `take_len`: the
    most shots one take holds on the project's video model
    (movie_takes.story_length)."""
    from prompt_check import FACES, SHOWS_BADLY, SHOWS_WELL
    n = max(1, int(take_len))
    return f"""- WRITE EACH SCENE AS TAKES. A take is one continuous camera passage in ONE place (one view of a location): its shots are consecutive ~4-5 s moments of the same unbroken shot, and the take renders as ONE continuous video. Inside a take the camera never jumps: it moves (push in, pull back, track, pan, crane, orbit) and the people move, and that is how the framing changes from shot to shot.
- A take holds 1 to {n} shots. A story is continuous: most of a scene is long takes, so a scene of 30 shots is a handful of takes, never 30 cuts. The edit follows from the takes: write no cut / continue markers.
- A new take in the SAME place is a match cut: a new camera angle while the action carries on. Use one only where the story needs a new angle (a reaction across the room, a reveal behind someone), or where a take reaches {n} shots and the moment goes on.
- A new take in ANOTHER place (another location, or another of its listed places) is a hard cut. Change place only where the story moves on.
- The first shot of a take sets it up (who is where); every next shot carries on from the end of the shot before (the same people, clothes and light, positions moving on). `end` (the last frame) is required on a take's last shot when the next take is in the same place.
- FRAMING FOR DETAIL: in the video model {FACES}. Dialogue, emotion, a hero moment and a power reveal are medium, medium_close_up or close_up. wide and extreme_wide only establish a place or show big movement (a charge, a fall, a crowd) where no face matters. Open a take on its people at medium_wide or closer; open wide only when the place itself is the point.
- WHAT THE VIDEO MODEL SHOWS WELL: {SHOWS_WELL}. It shows badly: {SHOWS_BADLY}.
- THE PLACE PICTURE IS THE SET: each place in LOCATIONS says what its picture shows and its light. Stage the action in that set and that light; never invent rooms, weather, props or a time of day the picture does not show (the background picture keeps its own light, so another time of day renders poorly).
- One clear primary_action per shot (<= 20 words, camera-ready).
- tier: hero <= 15% of a scene, only at close range (close_up, medium_close_up, extreme_close_up) or with a power, and only when the moment earns it; volume for establishing and atmosphere shots; standard the rest.
- prev_shot chains: one root per scene, linear.
- Emotions must come from each character's palette; transitions gradual (no calm -> rage).
- with: every other character on screen goes in `with` (ids, or [id, emotion, action]) — a character who speaks in the shot is on screen unless the action says otherwise. Their reference pictures keep their faces; a character left out is drawn from text alone.
- end: one short phrase for the shot's last frame (pose, where people stand, what is in frame).
- state_mutations persist (wounds, weather, damage) and must be referenced later.
- Dialogue <= 2 lines per shot, <= 25 words, emotion from the speaker's palette.
- powers: only the powers listed on the cast. Primary action may unleash a power only for the character that owns it.
- REFERENCE BUDGET (hard): one reference per character + one for the power + one for the environment. The model accepts 5 (LTX) / 6 (H3) pictures, so a shot can carry at most 3 characters WITH a power, or 4 characters without one. For bigger groups, put the extra characters in visible_background (crowd) or split the action across shots.
- weapon/loadout: only ids/names from the CAST list. Use `weapon` when the shot's action uses the weapon (and set `handling`); use `loadout` when the scene needs a specific outfit/weapon set. A drawn weapon stays in hand across following shots (no vanishing props).
- place: a take's `place` is a name from that location's `places` list; leave it out (null) for the location's main picture.
- level: 1-10, escalating across the film; the finale reaches 10.
- sound: name the concrete diegetic sounds of THIS shot (blade ring, thunder crack, footsteps) — they become the audio track."""


SHOT_JSON_TPL = '{{"shot_id": "{scene}_001", "scene": "{scene}", "location": "<id>", "time": "...", "characters": [{{"id": "<id>", "tier": "main|supporting|recurring|crowd", "emotion": "...", "action": "...", "position": "foreground_center|midground_left|midground_right|background_center|...", "facing": "camera|camera_left|camera_right|away_from_camera"}}], "power": null, "camera": "static|slow_push_in|dolly|pan|crane|handheld|orbit|pull_back|tracking", "shot_size": "extreme_close_up|close_up|medium_close_up|medium|medium_wide|wide|extreme_wide", "duration_s": 5, "tier": "hero|standard|volume", "dialogue": [], "prev_shot": null, "state_mutations": [], "visible_background": [], "weather": "inherit", "primary_action": "...", "secondary_motion": "", "start_state": {{}}, "end_state": {{}}, "continuity": "CUT", "expected_visible": ["<ids>"], "composition": "", "lens": ""}}'

# Compact entry extension: the 8th element is an OPTIONAL object (read by
# package_norm.expand_compact; validated by _extra_errors):
# {"weapon": "<id>", "loadout": "<name>", "place": "<place>", "level": 1-10,
#  "handling": "...", "sounds": "...", "with": [ids or [id, emotion, action]],
#  "end": "<the last frame>", "tier": "...", "continuity": "..."}
COMPACT_EXTRA = ('{"with": ["<other character on screen>"], "end": "<the last frame, or null>", '
                 '"weapon": "<id or null>", "loadout": "<name or null>", '
                 '"place": "<place or null>", "level": <1-10 or null>, '
                 '"handling": "<how the weapon is used or null>", "sounds": "<or null>"}')
# inside a take the place is the take's and the edit follows from the takes
TAKE_EXTRA = ('{"with": ["<other character on screen>"], "end": "<the last frame, or null>", '
              '"weapon": "<id or null>", "loadout": "<name or null>", "level": <1-10 or null>, '
              '"handling": "<how the weapon is used or null>", "sounds": "<or null>"}')


def _scene_json(scene: str, loc: str, time: str, indent: str = "") -> str:
    """A scene block in takes, as the reply format shows it."""
    entry = ('["<shot_size>", "<camera>", "<character_id>", "<emotion>", "<primary_action>", '
             f'"<power_id_or_null>", "<tier>", {TAKE_EXTRA}]')
    return (f'{indent}{{"scene": "{scene}", "location": "{loc}", "time": "{time}", "takes": [\n'
            f'{indent}  {{"place": "<a listed place of the location, or null>", "shots": [\n'
            f'{indent}    {entry},\n'
            f'{indent}    ["D", "<character_id>", "<emotion>", "<dialogue line>"],\n'
            f'{indent}    {entry}\n'
            f'{indent}  ]}},\n'
            f'{indent}  {{"place": "...", "shots": [...]}}\n'
            f'{indent}]}}')


def _entry_help(n: int) -> str:
    return ("COMPACT ENTRY: [shot_size, camera, character_id, emotion, primary_action, power_id_or_null, "
            "tier, extra?] where character_id is the shot's lead and tier is \"hero\"/\"volume\"/\"standard\". "
            "The 8th element `extra` is OPTIONAL: include it whenever the shot has more than one character on "
            "screen (`with`), ends a take that the next take carries on from (`end`), or uses a weapon, a "
            "specific loadout or a power level; leave out the keys it does not need. Dialogue lines as "
            "[\"D\", char, emotion, line] AFTER the shot they belong to, inside its take. A TAKE is "
            f"{{\"place\": <view or null>, \"shots\": [1 to {n} shot entries]}}; the scene's takes in story "
            "order. shot_size from: extreme_close_up|close_up|medium_close_up|medium|medium_wide|wide|"
            "extreme_wide. camera from: static|slow_push_in|dolly|pan|crane|handheld|orbit|pull_back|tracking.")


def _take_length(proj: dict) -> int:
    import movie_takes
    try:
        return movie_takes.story_length(load_config(), proj)
    except Exception as e:  # noqa: BLE001 - the rules still need a number
        log.warning("take length for %s: %s", proj.get("id"), e)
        return movie_takes.LTX_TAKE_MAX_SHOTS


# up to this many shots one chat reply holds the whole film (the one-reply
# prompt); a longer film is written as an outline, then scene by scene
FULL_MAX_SHOTS = 60

_TREATMENT_JSON = ('{"logline": "...", "synopsis": "...", "themes": ["..."], "protagonist": "<id>", '
                   '"antagonist": "<id>", "escalation": ["..."], "ending": "...", "pacing": "...", '
                   '"clipable_moments": ["..."]}')
# the story bible of a project that has its cast: the story's own text only
_CAST_BIBLE_JSON = ('{"characters": [{"id": "<cast id>", "arc_summary": "...", "voice": {"style_tag": "..."}, '
                    '"emotion_palette": ["... only for a character listed without emotions"]}], '
                    '"crowd_types": {"<id>": {"tier": "crowd", "descriptor": "..."}}}')
# the story bible of a project without a cast: the AI writes the cast
_BIBLE_JSON = ('{"characters": [{"id": "<new id>", "name": "...", "tier": "main|supporting|recurring|crowd", '
               '"body": "...", "outfit": "...", "powers": {"<power_id>": {"descriptor": "...", "color": "#hex", '
               '"motion": "..."}}, "emotion_palette": ["..."], "voice": {"engine": "moss-tts", "style_tag": "..."}, '
               '"arc_summary": "..."}], "locations": [{"id": "<new id>", "descriptor": "...", "lighting": "..."}], '
               '"crowd_types": {"<id>": {"tier": "crowd", "descriptor": "..."}}}')


def story_size(proj: dict) -> tuple[int, int, int]:
    """The film's length in seconds, its shots (one ~5 s clip each) and its
    scenes (25-40 shots each)."""
    dur = int(proj.get("duration_target") or 60)
    n_shots = max(4, -(-dur // 5))
    return dur, n_shots, max(2, min(30, round(n_shots / 28)))


def _project_block(proj: dict, p_bible) -> str:
    """The film and its whole vocabulary: the cast with its weapons, loadouts
    and per-scene defaults, the places, the powers and their levels, and the
    shot rules. Every way of writing the story gets the same block."""
    dur, n_shots, n_scenes = story_size(proj)
    return (f"=== PROJECT ===\n{proj.get('genre', '')} · tone \"{proj.get('tone', '')}\" · {dur}s = "
            f"{n_shots} shots across {n_scenes} scenes (25-40 shots per scene)\n"
            f"IDEA: {proj.get('idea', '')}\n"
            "AUTHORITY: the IDEA is a premise that may predate the current cast. The CAST / "
            "LOCATIONS / POWERS lists below are AUTHORITATIVE: if the idea names a character, place "
            "or power that is not listed, adapt it to the closest listed id (or make it a crowd "
            "type). Never output an id that is not in the lists, and write every id exactly as "
            "listed (same letters, same capitals). The cast is FIXED: never change a character's name, "
            "look, outfit, powers, weapons or loadouts, never describe a face, age or clothes the cast "
            "does not give, and never invent a person: anyone else on screen is a crowd type.\n\n"
            "CAST (use ONLY these ids; weapons/loadouts/levels are the presentation vocabulary):\n"
            f"{cast_lines(p_bible)}\n\n"
            f"LOCATIONS (use ONLY these ids and their listed places):\n{loc_lines(p_bible)}\n\n"
            f"POWERS + LEVELS (use ONLY these):\n{power_lines(p_bible)}\n\n"
            f"SHOT RULES:\n{shot_rules(_take_length(proj))}\n")


def _bible_json(p_bible) -> str:
    """The story bible's format: the story's text about the cast, or a cast
    of its own for a project that has none."""
    return _CAST_BIBLE_JSON if p_bible.characters() else _BIBLE_JSON


def _bible_rule(p_bible) -> str:
    if p_bible.characters():
        return ("- The bible section lists each cast character the story uses, by its id, with the "
                "story's own text: arc_summary, a voice style, and an emotion_palette only for a "
                "character listed without emotions. Never a name, look, outfit, power or weapon: those "
                "are the cast's. Extras go in crowd_types. Never add a character or a location.")
    places = ("; the locations are the ones listed" if p_bible.locations()
              else ", and the locations it needs (each with a descriptor and its lighting)")
    return ("- There is no cast yet: the bible section creates the characters the story needs (each "
            f"with a look: body and outfit){places}.")


def build_spine_prompt(proj: dict, p_bible) -> str:
    """The outline (treatment, story bible, beats): the AI writer's first
    request, and the chat prompt of a long film."""
    _, n_shots, n_scenes = story_size(proj)
    return (_project_block(proj, p_bible) +
            "\nWrite the MASTER PLAN (spine) only — treatment, bible, beats. Do NOT write shots yet: "
            "they are written scene by scene afterwards.\n"
            "BEAT RULES:\n"
            f"- Exactly {n_scenes} beats. Every scene: goal -> obstacle -> turn (something changes), ends on a hook.\n"
            f"- First scene = inciting incident; scene {n_scenes // 2} = midpoint reversal; last scene = climax "
            "+ resolution hook.\n"
            "- Reward cadence: a payoff every 2-3 minutes. Escalate powers/threats scene by scene; the power "
            "level curve climbs to 10 at the finale.\n"
            f"- Give each scene a specific location (or a named place), time, mood, and est_shots that sum to {n_shots}.\n"
            f"{_bible_rule(p_bible)}\n\n"
            "OUTPUT — return ONLY this JSON object (no prose outside it):\n"
            f'{{"treatment": {_TREATMENT_JSON},\n "bible": {_bible_json(p_bible)},\n'
            ' "beats": {"beats": [{"scene": "s01", "location": "<id>", "time": "night|dusk|dawn|day", '
            '"mood": "...", "goal": "...", "turn": "...", "hook": "...", "est_shots": 30}]}}\n'
            "Do NOT include a shots key in this output.")


def build_full_prompt(proj: dict, p_bible) -> str:
    """The whole film in one reply (a short film: up to FULL_MAX_SHOTS)."""
    _, n_shots, _ = story_size(proj)
    return (_project_block(proj, p_bible) +
            f"\nWrite the FULL package: treatment + bible + beats + every shot (exactly {n_shots}). "
            f"Use the COMPACT shot format in takes so the reply fits.\n{_bible_rule(p_bible)}\n\n"
            f'{{"treatment": {_TREATMENT_JSON},\n "bible": {_bible_json(p_bible)},\n'
            ' "beats": {"beats": [{"scene": "s01", "location": "<id>", "time": "night|dusk|dawn|day", '
            '"mood": "...", "goal": "...", "turn": "...", "hook": "...", "est_shots": 6}]},\n'
            ' "shots": {"shots": [\n'
            f'{_scene_json("s01", "<id>", "night", "   ")}\n'
            " ]}\n}\n\n"
            f"{_entry_help(_take_length(proj))} One scene block per scene, in order.")


def chat_prompt(proj: dict, p_bible, kind: str = "auto") -> tuple[str, str]:
    """The prompt to paste into any chat AI and its kind: "full" (the whole
    film in one reply), "spine" (the outline of a long film), or "auto"
    (full up to FULL_MAX_SHOTS, else the outline)."""
    if kind not in ("full", "spine"):
        kind = "full" if story_size(proj)[1] <= FULL_MAX_SHOTS else "spine"
    body = build_spine_prompt(proj, p_bible) if kind == "spine" else build_full_prompt(proj, p_bible)
    return _system_prompt() + "\n\n" + body, kind


def _clip(v: Any, n: int) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _story_so_far(proj: dict, rows: list[dict], scene: str, beats: list[dict]) -> str:
    """Where a scene sits in the film, for an AI that writes it without the
    conversation before it (the AI writer asks scene by scene): the logline
    and synopsis, every scene on one line, how the scene before ended, and
    the scene that follows."""
    tr = (proj.get("story") or {}).get("treatment") or {}
    out: list[str] = []
    if isinstance(tr, dict) and tr.get("logline"):
        out.append(f"Logline: {_clip(tr['logline'], 300)}")
    if isinstance(tr, dict) and tr.get("synopsis"):
        out.append(f"Synopsis: {_clip(tr['synopsis'], 700)}")
    if beats:
        out.append("Scenes (>> = the one to write now):")
        for b in beats:
            sc = str(b.get("scene") or "")
            out.append(f"{'>>' if sc == scene else '  '} {sc} @ {b.get('location')} ({b.get('time')}): "
                       f"{_clip(b.get('goal'), 120)} -> {_clip(b.get('turn'), 100)}")
    before = [r.get("json") or {} for r in rows if str((r.get("json") or {}).get("scene") or "") < scene]
    if before:
        last = str(before[-1].get("scene") or "")
        out.append(f"How {last} ended (its last shots):")
        for j in [j for j in before if str(j.get("scene") or "") == last][-3:]:
            who = ", ".join(str(c.get("id")) for c in j.get("characters") or [] if isinstance(c, dict))
            end = (j.get("end_state") or {}).get("last_frame") if isinstance(j.get("end_state"), dict) else ""
            out.append(f"- {j.get('shot_id')}: {j.get('shot_size')}, {who}: {_clip(j.get('primary_action'), 140)}"
                       + (f" [last frame: {_clip(end, 100)}]" if end else ""))
    ids = [str(b.get("scene") or "") for b in beats]
    if scene in ids and ids.index(scene) + 1 < len(ids):
        nb = beats[ids.index(scene) + 1]
        out.append(f"Next scene: {nb.get('scene')} @ {nb.get('location')}: {_clip(nb.get('goal'), 160)} "
                   "(end this scene so it leads there)")
    return "\n".join(out)


def build_scene_prompt(proj: dict, p_bible, db, scene: str,
                       beat: dict) -> str:
    """One-scene prompt with the story around it and the running continuity
    state: the same text for the AI writer and for a chat AI."""
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
    so_far = _story_so_far(proj, rows, scene, beats)
    so_far = f"\nTHE STORY SO FAR — where this scene sits in the film:\n{so_far}\n" if so_far else ""
    take_len = _take_length(proj)
    return f"""You are writing ONE scene for a local AI movie pipeline (~4-5 second shots, joined into continuous takes). Scene {scene} of "{proj.get('title','')}".

SCENE BEAT: {scene} @ {beat.get('location')} ({beat.get('time')}, {beat.get('mood')})
- goal: {beat.get('goal')}
- turn: {beat.get('turn')}
- hook: {beat.get('hook')}
- write exactly {n} shots, in takes of 1 to {take_len} shots
{so_far}
RUNNING CONTINUITY STATE — these are facts established by earlier scenes. Reference them in actions and dialogue; NEVER contradict them:
{context}

CAST (use ONLY these ids):
{cast_lines(p_bible)}

LOCATIONS (use ONLY these ids and their listed places):
{loc_lines(p_bible)}

POWERS + LEVELS (use ONLY these):
{power_lines(p_bible)}

SHOT RULES:
{shot_rules(take_len)}

OUTPUT — use the COMPACT format in takes (fits more shots per reply). Return ONLY this JSON object (no prose outside it):
{{"shots": {{"shots": [
{_scene_json(scene, str(beat.get('location') or ''), str(beat.get('time') or ''), "  ")}
]}}}}
{_entry_help(take_len)} Emotions from each character's palette. Exactly {n} shot entries in the scene, across its takes."""


# ---------------------------------------------------------------- merging
_EXTRA_KEYS = ("weapon", "loadout", "place", "level", "handling", "sounds",
               "with", "end", "tier", "continuity")


def _extra_errors(ext: dict) -> list[str]:
    """What is wrong with a compact entry's extra object (package_norm reads it)."""
    errs = [f"unknown extra keys {[k for k in ext if k not in _EXTRA_KEYS]} "
            f"(allowed: {list(_EXTRA_KEYS)})"] if any(k not in _EXTRA_KEYS for k in ext) else []
    lvl = ext.get("level")
    if lvl is not None:
        try:
            li = int(lvl)
        except (TypeError, ValueError):
            li = -1
        if li < 1 or li > 10:
            errs.append("level must be 1-10")
    w = ext.get("with")
    if w is not None and not (isinstance(w, list) and all(
            isinstance(x, str) or (isinstance(x, list) and x and all(isinstance(y, str) for y in x))
            for x in w)):
        errs.append('"with" must be a list of character ids or [id, emotion, action] arrays')
    for k, ok in (("tier", ("hero", "volume", "standard")), ("continuity", ("cut", "continue", "match_action"))):
        if ext.get(k) is not None and str(ext[k]).lower() not in ok:
            errs.append(f'"{k}" must be one of {"/".join(ok)}')
    if ext.get("end") is not None and not isinstance(ext["end"], str):
        errs.append('"end" must be a short text')
    return errs


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
        takes = block.get("takes")
        if takes is not None:
            errs += _takes_errors(takes, bi)
            continue
        entries = block.get("shots")
        if not isinstance(entries, list) or not entries:
            errs.append(f"shots[{bi}] needs its takes (or a non-empty shots list)")
            continue
        errs += _entry_errors(entries, str(bi))
    if errs:
        return None, errs
    return shots, []


def _takes_errors(takes, bi: int) -> list[str]:
    """What is wrong with a scene block's takes."""
    if not isinstance(takes, list) or not takes:
        return [f"shots[{bi}].takes must be a non-empty list"]
    errs: list[str] = []
    n = 0
    for ti, t in enumerate(takes):
        where = f"{bi}.take{ti + 1}"
        if not isinstance(t, dict):
            errs.append(f"take {where} is not an object")
            continue
        unknown = [k for k in t if k not in ("place", "location", "shots")]
        if unknown:
            errs.append(f"take {where}: unknown keys {unknown} (allowed: place, location, shots)")
        for k in ("place", "location"):
            if t.get(k) is not None and not isinstance(t[k], str):
                errs.append(f"take {where}: {k} must be a name or null")
        entries = t.get("shots")
        if not isinstance(entries, list) or not entries:
            errs.append(f"take {where}: shots must be a non-empty list")
            continue
        errs += _entry_errors(entries, where)
        n += sum(1 for e in entries if isinstance(e, list) and e and e[0] != "D")
    if not errs and not n:
        errs.append(f"shots[{bi}]: the takes hold no shot, only dialogue")
    return errs


def _entry_errors(entries: list, where: str) -> list[str]:
    errs: list[str] = []
    for ei, e in enumerate(entries):
        if not isinstance(e, list) or not e:
            errs.append(f"entry {where}.{ei} is not an array")
            continue
        if e[0] == "D":
            if len(e) != 4 or not all(isinstance(x, str) for x in e[1:]):
                errs.append(f'dialogue {where}.{ei} must be '
                            f'["D", char, emotion, line]')
            continue
        if len(e) < 5 or len(e) > 8:
            errs.append(f"shot {where}.{ei} must have 5-8 elements "
                        f"(got {len(e)})")
            continue
        if not isinstance(e[0], str) or not isinstance(e[2], str):
            errs.append(f"shot {where}.{ei}: shot_size and character id "
                        f"must be strings")
            continue
        if len(e) >= 8 and e[7] is not None:
            if not isinstance(e[7], dict):
                errs.append(f"shot {where}.{ei}: the 8th element must be an "
                            f"object or null")
                continue
            errs += [f"shot {where}.{ei}: {m}" for m in _extra_errors(e[7])]
    return errs


def _project_family(store, pid: str) -> str:
    """The project's video family (ltx25 / h3 / ...) for budget-aware linting."""
    try:
        import video_models
        proj = store.get(pid) or {}
        mid = str(proj.get("video_family") or "") or video_models.DEFAULT_MODEL
        return str((video_models.VIDEO_MODELS.get(mid) or {}).get("family") or mid)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to get video family for %s: %s", pid, e)
        return ""


def project_linter(store, pid: str, p_bible):
    """The linter for a project's story: its video family's reference budget
    and its take length."""
    from linter import Linter
    return Linter(p_bible, family=_project_family(store, pid),
                  take_len=_take_length(store.get(pid) or {}))


def merge_scene_shots(store, db, p_bible, pid: str, scene: str,
                      shots: list) -> dict:
    """Replace a scene's shots; validate the WHOLE project together;
    version the merged shots stage."""
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
              and (isinstance(b.get("shots"), list) or isinstance(b.get("takes"), list))]
    if len(blocks) == 1 and blocks[0].get("scene") != scene:
        # one scene per call: a block the AI labelled "s5" for scene "s05"
        # replaced s05 with shots filed under s5 (ids derive from it)
        blocks[0]["scene"] = scene
    for b in blocks:
        if str(b.get("scene") or "").strip().lower() == str(scene).lower():
            b["scene"] = scene
    known = [b.get("scene") for b in (((store.get(pid) or {}).get("story") or {}).get("beats") or {})
             .get("beats", []) if isinstance(b, dict)]
    norm = normalize_package({"shots": {"shots": shots}}, p_bible, known)
    shots = norm["shots"]["shots"]
    for s in shots:
        s["project_id"] = pid
        if not s.get("scene"):
            s["scene"] = scene
    # one scene per import: a reply that also holds other scenes leaves them
    # out (pasted in their own rows, the same reply imports each one)
    left_out = sorted({str(s.get("scene")) for s in shots if s.get("scene") != scene})
    shots = [s for s in shots if s.get("scene") == scene]
    if not shots:
        return {"ok": False, "error": f"the reply has no shots for {scene}"
                + (f" (it has {', '.join(left_out)})" if left_out else ""),
                "errors": [f"[{scene}] the reply has no shots for this scene"], "elsewhere": [],
                "report": {"ok": False, "errors": [f"[{scene}] the reply has no shots for this scene"],
                           "warnings": []}}
    bad_ids: list[str] = []
    seen_ids: set[str] = set()
    for s in shots:
        sid = s.get("shot_id")
        if not isinstance(sid, str) or not sid.strip():
            bad_ids.append(f"[{scene}] a shot has no shot_id")
        elif sid in seen_ids:
            bad_ids.append(f"[{sid}] shot_id used twice: write one block per scene")
        else:
            seen_ids.add(sid)
    if bad_ids:
        return {"ok": False, "error": f"{len(bad_ids)} problem(s) in {scene}: {bad_ids[0]}",
                "errors": bad_ids, "elsewhere": [],
                "report": {"ok": False, "errors": bad_ids, "warnings": []}}
    other = [r["json"] for r in db.all(project=pid)
             if r.get("scene") != scene]
    rep = project_linter(store, pid, p_bible).lint_package(other + shots)
    # checked with the whole project (continuity across scenes), judged on
    # its own: the problems filed under this scene or its shots refuse it,
    # the ones of other scenes are reported and never block it
    own_ids = {str(s.get("shot_id")) for s in shots} | {str(scene)}
    own: list[str] = []
    elsewhere: list[str] = []
    for e in rep.errors:
        tag = e[1:e.find("]")] if e.startswith("[") and "]" in e else ""
        (own if tag in own_ids else elsewhere).append(e)
    if own:
        return {"ok": False, "error": f"{len(own)} problem(s) in {scene}: {own[0]}",
                "errors": own, "elsewhere": elsewhere,
                "report": {"ok": False, "errors": own, "warnings": rep.warnings}}
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
            "warnings": rep.warnings[:10], "elsewhere": elsewhere, "left_out": left_out}


# ---------------------------------------------------------------- job
class _Down(RuntimeError):
    """Nothing answers at the AI writer's endpoint."""


class _Halted(Exception):
    """The run was asked to stop."""


SCENE_MODES = ("all", "missing", "none")


class ArchitectJob:
    """Writes the story with the connected AI: the outline (treatment, story
    bible, beats), then the scenes' shots one at a time with the continuity
    carried forward and the validator's errors sent back for a fix. One run
    per project at a time, from the Story page (start, a thread) or the
    Director (run, in its own thread)."""

    def __init__(self, cfg=None, db=None, store=None) -> None:
        self.cfg = cfg or load_config()
        self.db = db
        self.store = store
        self._jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ control
    def _check(self, pid: str, outline: bool, scenes: Any) -> str:
        """Why this run cannot start, or ""."""
        from core import is_safe_component
        if isinstance(scenes, list):
            if not scenes or not all(isinstance(s, str) and is_safe_component(s) for s in scenes):
                return "scenes must be scene ids"
        elif scenes not in SCENE_MODES:
            return f"scenes must be one of {', '.join(SCENE_MODES)} or a list of scene ids"
        if not outline:
            proj = self.store.get(pid) if self.store else None
            if not ((proj or {}).get("story") or {}).get("beats", {}).get("beats"):
                return ("write the outline first: copy the story prompt into a chat AI and paste "
                        "its reply, or let the AI writer write the story")
        return ""

    def _claim(self, pid: str, outline: bool) -> bool:
        with self._lock:
            job = self._jobs.get(pid)
            if job is not None and job.get("running"):
                return False
            self._stop.discard(pid)
            self._jobs[pid] = {"running": True, "stage": "spine" if outline else "scenes",
                               "done": 0, "total": 0, "message": "",
                               "log": [], "started": time.time()}
            return True

    def start(self, pid: str, outline: bool = True, scenes: Any = "all") -> dict:
        from llm import LLMClient
        if not LLMClient(self.cfg).config_ok():
            return {"ok": False, "error": "the AI writer is not configured: connect one under AI "
                                          "writer, or write the story with any chat AI"}
        err = self._check(pid, outline, scenes)
        if err:
            return {"ok": False, "error": err}
        if not self._claim(pid, outline):
            return {"ok": False, "error": "this story is already being written"}
        t = threading.Thread(target=self._run, args=(pid, outline, scenes), daemon=True)
        self._threads[pid] = t
        t.start()
        return {"ok": True}

    def run(self, pid: str, outline: bool = True, scenes: Any = "all",
            stop: Any = None) -> dict:
        """The same work in the calling thread (the Director), once no other
        run holds the project. {"ok", "message", "failed_scenes",
        "unreachable", "stopped"}."""
        err = self._check(pid, outline, scenes)
        if err:
            return {"ok": False, "message": err, "failed_scenes": [], "unreachable": False,
                    "stopped": False}
        waited = False
        while not self._claim(pid, outline):
            if stop is not None and stop():
                return {"ok": False, "message": "stopped", "failed_scenes": [],
                        "unreachable": False, "stopped": True}
            waited = True
            time.sleep(2)
        story = ((self.store.get(pid) or {}).get("story") or {}) if waited and outline else {}
        if story.get("treatment") and (story.get("beats") or {}).get("beats"):
            # the run it waited for wrote the outline: never write it again
            outline = False
            if scenes == "none":
                with self._lock:
                    self._jobs[pid]["running"] = False
                return {"ok": True, "message": "the outline was written meanwhile",
                        "failed_scenes": [], "unreachable": False, "stopped": False}
        return self._run(pid, outline, scenes, stop)

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

    def _run(self, pid: str, outline: bool = True, scenes: Any = "all",
             stop: Any = None) -> dict:
        import movie_prompts
        from bible import Bible
        from llm import LLMClient, extract_json, is_local
        from package_norm import normalize_package
        from storygen import StoryArchitect

        job = self._jobs.get(pid) or {}
        result: dict = {"ok": False, "message": "", "failed_scenes": [],
                        "unreachable": False, "stopped": False}
        client = LLMClient(self.cfg)
        base = str((self.cfg.get("architect") or {}).get("base_url") or "")
        local = is_local(base)
        asked = False

        def halted() -> bool:
            return pid in self._stop or (stop is not None and bool(stop()))

        def ask(user: str) -> str:
            """One request; a failed one ends the run with its reason (an
            endpoint that does not answer is remembered for a minute, as for
            the video prompts)."""
            nonlocal asked
            if halted():
                raise _Halted()
            if time.time() - movie_prompts._DOWN.get(base, -movie_prompts.DOWN_S) < movie_prompts.DOWN_S:
                raise _Down(f"the AI writer at {base} did not answer a moment ago")
            try:
                if local and not movie_prompts.wait_gpu(
                        halted, lambda: self._log(pid, "waiting: the AI runs on this PC and a render "
                                                       "is using the GPU"), base=base):
                    raise _Halted()
            except movie_prompts.Unreachable:
                raise _Down(f"the AI writer at {base} did not answer") from None
            asked = True
            out = client.complete(_system_prompt(), user, json_mode=True)
            if out is None:
                if client.last_unreachable is True:
                    movie_prompts._DOWN[base] = time.time()
                    log.warning("architect[%s]: %s", pid, client.last_error)
                    raise _Down(f"the AI writer at {base} did not answer")
                raise RuntimeError(f"the AI writer's request failed: {client.last_error or 'no reply'}")
            movie_prompts._DOWN.pop(base, None)
            return out

        try:
            proj = self.store.get(pid)
            if proj is None:
                raise RuntimeError("project not found")
            # the project's own bible, never re-copied from the global one:
            # a snapshot clears the cast (projects get theirs when created)
            p_bible = Bible(pid)
            arch = self.cfg.get("architect") or {}
            retries = int(arch.get("retries") or 2)

            # ---- 1. the outline (spine)
            if outline:
                self._log(pid, "generating the spine (treatment + bible + beats)")
                package = None
                spine_prompt = build_spine_prompt(proj, p_bible)
                for attempt in range(retries + 1):
                    out = ask(spine_prompt)
                    package = extract_json(out or "")
                    if package is not None:
                        break
                    self._log(pid, f"spine parse failed (attempt {attempt + 1}) — "
                                   "asking the AI to fix its JSON")
                    # corrective follow-up: a completion has no memory, so it
                    # carries the request and the reply to fix
                    out = ask("Your reply to the request below was not valid JSON.\n\n"
                              f"Original request was:\n{spine_prompt}\n\n"
                              f"Your previous attempt was:\n{(out or '')[:2000]}\n\n"
                              "Return ONLY the requested JSON object, nothing else.")
                    package = extract_json(out or "")
                    if package is not None:
                        break
                if package is None:
                    raise RuntimeError("spine JSON unparseable after retries")
                package = normalize_package(package, p_bible, [
                    b.get("scene") for b in ((proj.get("story") or {}).get("beats") or {}).get("beats", [])
                    if isinstance(b, dict)])
                sarch = StoryArchitect(proj, self.store, p_bible)
                results = {}
                for stage in ("treatment", "bible", "beats", "scenes"):
                    if stage in package and isinstance(package[stage], dict):
                        try:
                            results[stage] = sarch.import_stage(stage, package[stage])
                        except Exception as e:  # noqa: BLE001
                            results[stage] = {"ok": False, "error": str(e)}
                bad = {k: v.get("error") for k, v in results.items() if not v.get("ok")}
                if bad:
                    raise RuntimeError(f"spine stages failed: {bad}")
            proj = self.store.get(pid)
            beats = (proj.get("story") or {}).get("beats", {}).get("beats", [])
            if not beats:
                raise RuntimeError("spine produced no beats")

            # ---- 2. scenes, one at a time, continuity carried forward
            if scenes == "none":
                todo: list[dict] = []
            elif scenes == "missing":
                written = {str(r.get("scene")).lower() for r in self.db.all(project=pid)}
                todo = [b for b in beats if b.get("scene") and str(b["scene"]).lower() not in written]
            elif isinstance(scenes, list):
                todo = [b for b in beats if b.get("scene") in set(scenes)]
            else:
                todo = [b for b in beats if b.get("scene")]
            total = len(todo)
            job["total"] = total
            failed: list[str] = []
            if total:
                self._log(pid, f"writing {total} scene(s)")
            for i, beat in enumerate(todo):
                scene = beat.get("scene")
                job["stage"] = f"scene {scene} ({i + 1}/{total})"
                job["done"] = i
                self._log(pid, f"writing scene {scene}")
                shots = None
                imported = False
                for attempt in range(retries + 1):
                    orig_prompt = build_scene_prompt(proj, p_bible, self.db, scene, beat)
                    out = ask(orig_prompt)
                    data = extract_json(out or "")
                    shots, verrs = (validate_scene_package(data)
                                    if data is not None else (None, ["JSON could not be parsed from reply"]))
                    if verrs:
                        # STRICT shape gate: malformed structures never reach
                        # the linter — the AI gets the exact errors back.
                        shots = None
                        self._log(pid, f"{scene}: JSON shape invalid — asking "
                                       f"the AI to fix ({attempt + 1})")
                        out = ask(f"Your reply for scene {scene} failed STRICT JSON validation:\n" +
                                  "\n".join(f"- {e}" for e in verrs[:8]) +
                                  f"\n\nOriginal scene prompt was:\n{orig_prompt}\n\n" +
                                  f"Your previous attempt was:\n{(out or '')[:2000]}\n\n" +
                                  "Return ONLY the corrected JSON object for this scene in the same format.")
                        data2 = extract_json(out or "")
                        shots, verrs2 = (validate_scene_package(data2)
                                         if data2 is not None else (None, ["JSON could not be parsed from retry reply"]))
                        if verrs2:
                            shots = None
                    if shots:
                        res = merge_scene_shots(self.store, self.db, p_bible, pid, scene, shots)
                        if res.get("ok"):
                            self._log(pid, f"{scene}: {res['added']} shots imported")
                            imported = True
                            break
                        # self-heal: tell the AI what the validator rejected
                        errs = ((res.get("report") or {}).get("errors") or [])[:8]
                        self._log(pid, f"{scene}: lint rejected — asking "
                                       f"the AI to fix ({attempt + 1})")
                        out = ask(f"Your shots for scene {scene} were rejected by the script validator with these errors:\n"
                                  + "\n".join(f"- {e}" for e in errs)
                                  + f"\n\nOriginal scene prompt was:\n{orig_prompt}\n\n"
                                  + f"Your previous attempt was:\n{(out or '')[:2000]}\n\n"
                                  + "Return ONLY the corrected JSON object for this scene (same format).")
                        data = extract_json(out or "")
                        shots, verrs3 = (validate_scene_package(data)
                                         if data is not None else (None, ["JSON could not be parsed from retry reply"]))
                        if verrs3:
                            shots = None
                        if shots:
                            res = merge_scene_shots(self.store, self.db, p_bible, pid, scene, shots)
                            if res.get("ok"):
                                self._log(pid, f"{scene}: fixed and imported")
                                imported = True
                                break
                if not imported:
                    # a scene whose last attempt failed is reported, never
                    # skipped silently; the others are independent, and the
                    # scene can be written again on its own
                    failed.append(str(scene))
                    self._log(pid, f"{scene}: still invalid after "
                                   f"{retries + 1} attempt(s) — skipped")
            job["done"] = total
            result["failed_scenes"] = failed
            if failed:
                job["stage"] = "partial"
                job["failed_scenes"] = failed
                msg = (f"script incomplete: {len(failed)} of {total} scene(s) failed validation "
                       f"({', '.join(failed)}) — fix or re-import them from the Story page")
            else:
                job["stage"] = "done"
                if total:
                    msg = (f"full script complete: {total} scenes" if outline and scenes == "all"
                           else f"{total} scene(s) written")
                elif outline:
                    msg = "the outline is written"
                else:
                    msg = ("nothing to write: every scene has its shots" if scenes == "missing"
                           else "no such scene in the outline")
            self._log(pid, msg)
            result.update(ok=not failed and bool(total or outline or scenes == "missing"), message=msg)
        except _Halted:
            self._log(pid, "stopped")
            result.update(stopped=True, message="stopped")
        except _Down as e:
            log.warning("architect job for %s: %s", pid, e)
            job["message"] = f"failed: {e}"
            result.update(unreachable=True, message=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("architect job failed for %s", pid)
            job["message"] = f"failed: {e}"
            result["message"] = str(e)
        finally:
            if local and asked and not result["unreachable"]:
                movie_prompts._release_local(client)
            with self._lock:
                job["running"] = False
                self._stop.discard(pid)
        return result

