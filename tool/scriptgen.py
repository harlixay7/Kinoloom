"""Built-in script generator — produces a complete, linter-valid movie
package (Bible additions + 26 shot JSONs = 130 seconds at 5s/shot) for a
cinematic sci-fi short. No external AI needed. The LLM Story Architect
(storygen_llm.py, prompted by tool/skills/storytelling/SKILL.md) writes
richer stories.

Structural constraints (validated against the canon linter):
  - tier mix: hero 3 (11.5% <= 15%), standard 13 (50%), volume 10 (38.5% >= 30%)
  - valid prev_shot DAG per scene (one root, no cycles, no orphans)
  - emotions within each character's palette, gradual transitions
  - enums for camera/shot_size/position/facing
  - state mutations persist across the scene
  - s01 opens with a hero shot FIRST (keyframe harvesting enables I2V)
"""
from __future__ import annotations

from typing import Any

from bible import Bible


def _add_voss(bible: Bible) -> None:
    if "guard_captain_voss" not in bible.characters():
        bible.save_character({
            "id": "guard_captain_voss",
            "name": "Captain Voss",
            "tier": "supporting",
            "body": "stocky 40s man, square jaw, cropped gray hair, scar "
                    "across left cheek",
            "outfit": "steel plate armor, red command sash over right "
                      "shoulder, black gloves",
            "voice": {"engine": "moss-tts",
                      "style_tag": "gruff authoritative male",
                      "params": {"pitch": -3, "speed": 0.95}},
            "emotion_palette": ["stern", "fear", "respect", "anger"],
            "location_binding": ["storm_rooftop"],
        })


def _add_crowd(bible: Bible) -> None:
    ct = bible.crowd_types()
    if "palace_guards" not in ct:
        ct["palace_guards"] = {
            "tier": "crowd",
            "descriptor": "palace guards in silver plate armor and blue "
                          "cloaks, matching helmets",
        }
        bible.save_crowd_types(ct)


def _shot(scene: str, n: int, **kw: Any) -> dict:
    base = {
        "shot_id": f"{scene}_{n:03d}",
        "scene": scene,
        "location": "storm_rooftop",
        "time": "night",
        "characters": [],
        "camera": "static",
        "shot_size": "medium",
        "duration_s": 5,
        "tier": "volume",
        "prev_shot": None if n == 1 else f"{scene}_{n-1:03d}",
        "state_mutations": [],
        "visible_background": ["palace_guards"],
        "weather": "inherit",
    }
    base.update(kw)
    return base


def generate(bible: Bible) -> dict:
    _add_voss(bible)
    _add_crowd(bible)

    kael = {"id": "kael", "tier": "main"}
    voss = {"id": "guard_captain_voss", "tier": "supporting"}

    shots: list[dict] = []

    # ---------------- scene s01 — the confrontation (12 shots)
    def s01(n: int, **kw: Any) -> dict:
        return _shot("s01", n, **kw)

    shots += [
        # hero first: power reveal close-up (establishes Kael -> keyframe)
        s01(1, tier="hero", shot_size="close_up", camera="slow_push_in",
            power="lightning",
            characters=[{**kael, "emotion": "focused",
                         "action": "stands at rooftop edge, cyan arcs "
                                   "gathering in both palms",
                         "position": "foreground_center",
                         "facing": "camera"}],
            mood="tense, electric"),
        s01(2, tier="volume", shot_size="extreme_wide", camera="static",
            characters=[{**kael, "emotion": "focused",
                         "action": "silhouetted against neon skyline",
                         "position": "midground_center",
                         "facing": "away_from_camera"}],
            mood="vast, lonely"),
        s01(3, tier="volume", shot_size="medium_wide", camera="pan",
            characters=[{**kael, "emotion": "calm",
                         "action": "turns slowly, coat shifting in wind",
                         "position": "midground_left",
                         "facing": "camera_right"}],
            mood="gathering storm"),
        s01(4, tier="standard", shot_size="medium", camera="dolly",
            characters=[{**voss, "emotion": "stern",
                         "action": "marches forward with guards, sword "
                                   "drawn",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            state_mutations=[{"target": "storm_rooftop", "key": "weather",
                              "value": "heavy rain", "persist": True}],
            mood="confrontational"),
        s01(5, tier="volume", shot_size="extreme_wide", camera="static",
            characters=[{**kael, "emotion": "calm",
                         "action": "stands alone as guards fan out",
                         "position": "midground_center",
                         "facing": "away_from_camera"}],
            mood="odds against him"),
        s01(6, tier="standard", shot_size="medium_close_up",
            camera="slow_push_in",
            characters=[{**voss, "emotion": "stern",
                         "action": "raises hand to halt guards",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            dialogue=[{"char": "guard_captain_voss",
                       "line": "Stand down, Kael. This ends now.",
                       "emotion": "stern"}],
            mood="authority"),
        s01(7, tier="standard", shot_size="close_up", camera="static",
            characters=[{**kael, "emotion": "cold",
                         "action": "meets his gaze, unblinking",
                         "position": "foreground_center",
                         "facing": "camera_right"}],
            dialogue=[{"char": "kael", "line": "You should have run.",
                       "emotion": "cold"}],
            mood="quiet threat"),
        s01(8, tier="standard", shot_size="medium_wide", camera="handheld",
            characters=[{**voss, "emotion": "anger",
                         "action": "lunges forward, armor glinting",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="breaking point"),
        s01(9, tier="standard", shot_size="close_up", camera="orbit",
            power="lightning",
            characters=[{**kael, "emotion": "rage",
                         "action": "throws both arms wide, massive cyan "
                                   "arcs erupting",
                         "position": "foreground_center",
                         "facing": "camera"}],
            state_mutations=[{"target": "guard_captain_voss", "key": "shield",
                              "value": "cracked and sparking",
                              "persist": True}],
            mood="power unleashed"),
        s01(10, tier="volume", shot_size="extreme_wide", camera="static",
            power="lightning",
            characters=[{**kael, "emotion": "focused",
                         "action": "arms raised as lightning splits the "
                                   "roof",
                         "position": "foreground_left",
                         "facing": "camera_right"},
                        {**voss, "emotion": "fear",
                         "action": "shield raised against the blast",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="thunder"),
        s01(11, tier="standard", shot_size="medium_wide", camera="dolly",
            characters=[{**voss, "emotion": "fear",
                         "action": "stumbles backward, shield sparking",
                         "position": "midground_right",
                         "facing": "camera_left"},
                        {**kael, "emotion": "cold",
                         "action": "advances slowly, arcs crackling",
                         "position": "foreground_left",
                         "facing": "camera_right"}],
            state_mutations=[{"target": "storm_rooftop", "key": "lighting",
                              "value": "strobe-lit by lightning",
                              "persist": True}],
            mood="pursuit"),
        s01(12, tier="standard", shot_size="medium", camera="tracking",
            characters=[{**kael, "emotion": "focused",
                         "action": "vaults the parapet and drops from "
                                   "sight",
                         "position": "midground_center",
                         "facing": "away_from_camera"}],
            mood="escape begins"),
    ]

    # ---------------- scene s02 — the pursuit (7 shots)
    def s02(n: int, **kw: Any) -> dict:
        return _shot("s02", n, **kw)

    shots += [
        s02(1, tier="standard", shot_size="medium_wide", camera="handheld",
            characters=[{**voss, "emotion": "anger",
                         "action": "races across the roof, guards behind",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="chase"),
        s02(2, tier="volume", shot_size="extreme_wide", camera="crane",
            characters=[{**kael, "emotion": "focused",
                         "action": "leaps between rooftops, rain streaking",
                         "position": "midground_center",
                         "facing": "away_from_camera"}],
            mood="acrobatic escape"),
        s02(3, tier="standard", shot_size="close_up", camera="handheld",
            characters=[{**kael, "emotion": "focused",
                         "action": "lands in a roll, coat whipping",
                         "position": "foreground_center",
                         "facing": "camera_right"}],
            state_mutations=[{"target": "kael", "key": "wound_left_arm",
                              "value": "bleeding gash", "persist": True}],
            mood="grinding onward"),
        s02(4, tier="volume", shot_size="medium_wide", camera="tracking",
            characters=[{**voss, "emotion": "respect",
                         "action": "pauses at the edge, watching him go",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="reluctant respect"),
        s02(5, tier="standard", shot_size="medium", camera="static",
            characters=[{**voss, "emotion": "stern",
                         "action": "sheathes his sword, rain on armor",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="order restored"),
        s02(6, tier="hero", shot_size="extreme_wide", camera="crane",
            power="lightning",
            characters=[{**kael, "emotion": "focused",
                         "action": "far below, a flash of cyan arcs as he "
                                   "vanishes into the alleys",
                         "position": "background_center",
                         "facing": "away_from_camera"}],
            mood="gone"),
        s02(7, tier="volume", shot_size="extreme_wide", camera="static",
            characters=[{**voss, "emotion": "fear",
                         "action": "alone on the rooftop, storm rolling in",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="the storm remains"),
    ]

    # ---------------- scene s03 — the aftermath (7 shots)
    def s03(n: int, **kw: Any) -> dict:
        return _shot("s03", n, **kw)

    shots += [
        s03(1, tier="volume", shot_size="extreme_wide", camera="static",
            characters=[{**kael, "emotion": "calm",
                         "action": "walks through neon-lit rain, wound "
                                   "bandaged",
                         "position": "background_center",
                         "facing": "away_from_camera"}],
            state_mutations=[{"target": "kael", "key": "wound_left_arm",
                              "value": "bandaged wound", "persist": True}],
            mood="quiet aftermath"),
        s03(2, tier="volume", shot_size="extreme_wide", camera="static",
            characters=[{**voss, "emotion": "respect",
                         "action": "watches from the rooftop, alone",
                         "position": "midground_right",
                         "facing": "camera_left"}],
            mood="the city holds its breath"),
        s03(3, tier="volume", shot_size="extreme_wide", camera="crane",
            characters=[{**kael, "emotion": "calm",
                         "action": "tiny figure crossing a rain-swept "
                                   "plaza",
                         "position": "background_center",
                         "facing": "away_from_camera"}],
            mood="vast, peaceful"),
        s03(4, tier="standard", shot_size="medium_close_up", camera="dolly",
            characters=[{**kael, "emotion": "cold",
                         "action": "glances back at the distant rooftop",
                         "position": "midground_left",
                         "facing": "camera_right"}],
            mood="unfinished"),
        s03(5, tier="standard", shot_size="close_up", camera="static",
            characters=[{**kael, "emotion": "focused",
                         "action": "opens palm, a single cyan spark",
                         "position": "foreground_center",
                         "facing": "camera"}],
            power="lightning",
            mood="the power remains"),
        s03(6, tier="standard", shot_size="medium", camera="pan",
            characters=[{**kael, "emotion": "calm",
                         "action": "turns and walks on, sparks fading",
                         "position": "midground_left",
                         "facing": "away_from_camera"}],
            mood="moving forward"),
        s03(7, tier="hero", shot_size="extreme_wide", camera="pull_back",
            power="lightning",
            characters=[{**kael, "emotion": "calm",
                         "action": "small figure vanishing into the neon "
                                   "city, a last distant spark of cyan",
                         "position": "background_center",
                         "facing": "away_from_camera"}],
            mood="free"),
    ]

    return {"shots": shots,
            "summary": {"scenes": 3, "shots": len(shots),
                        "hero": sum(1 for s in shots if s["tier"] == "hero"),
                        "standard": sum(1 for s in shots
                                        if s["tier"] == "standard"),
                        "volume": sum(1 for s in shots
                                      if s["tier"] == "volume"),
                        "runtime_s": len(shots) * 5}}
