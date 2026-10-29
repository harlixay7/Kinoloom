---
name: pipeline
description: Operating manual for the Kinoloom pipeline. Use to understand how projects, story stages, shots, datasets, references, renders (LTX-2.5 / MiniMax H3 / Wan), the worker fleet and the queue fit together — and exactly what to produce when the operator sends a task, file or request.
---

# Pipeline Skill — Kinoloom

This is the operating manual for a local AI movie studio. Read it before acting on any request: it tells you what exists, what each artifact must look like, and what to do when the operator sends you something.

---

## 1. What the studio is

**Goal:** a full movie rendered as 5-second clips, assembled in order.

| Part | What it is |
|---|---|
| **Control room** | FastAPI app on `:8080` — projects, bible, shots DB, queue, datasets, workers, Video Studio |
| **Production farm** | ComfyUI `:8188` — SDXL photoreal stills and post-processing |
| **LTX farm** | ComfyUI `:8189` — LTX-2.5 22B (W4A8), reference-driven, two-stage 1080p |
| **H3 farm** | ComfyUI `:8199` — MiniMax H3 (W4A8), two-phase 1080p Ref2VA with native audio |
| **Story architect** | LLM (cloud or local) that writes treatment/bible/beats/scenes per the **storytelling skill** |
| **Worker PCs** | extra machines running the same ComfyUI + an agent; they take image/upscale jobs |
| **Main GPU** | RTX 3070 8 GB — one clip at a time; jobs are queued |

**Render facts to remember:**
- LTX-2.5: 1920×1088, 121 frames (~5.0 s), references **stage-1 only** (hard rule), ~6 min/clip warm (`ltx25_s6_cq`), 5 reference slots.
- MiniMax H3: two-phase 960×544 → 1920×1088, 124 frames (~5.2 s), ~18–23 min/clip, **native audio**, 6 reference pictures, first reference is pinned as **frame 0**.
- Stills (datasets) run on SDXL-family models (realvis), Z-Image Turbo (fast), or Qwen-Image-2.1; **qwen21/z-image are preferred for power references**.

---

## 2. Data model (what you produce, where it lives)

### Project
`{title, idea, genre, tone, duration_target, mode (autopilot|checkpoints|manual), video_family ("" | ltx25… | h3…), post_processing (bool), post_worker}`

### Bible (per project)
- **characters** — `id, name, tier (main|supporting|recurring|crowd), body, outfit, emotion_palette[], powers{}, weapons{}, outfits{}, loadouts{}, scene_tags{}, voice{}, lora?, enabled`
- **locations** — `id, descriptor, lighting, reference_images[], scene_tags{}`
- **crowd_types** — `id, descriptor`

### Presentation entities (inside a character)
```
powers:   { <power_id>: { descriptor, color, motion, sfx?, lora?, reference_images[] } }
weapons:  { <weapon_id>: { type, descriptor, handling[], sfx, material?, color?, energy?, reference_images[] } }
loadouts: { <LoadoutName>: { outfit: "<text or outfit_id>", weapon: "<weapon_id or empty>" } }
scene_tags: { "s03": "<LoadoutName>" }        # per-scene default look
```

### Shots
Compact authoring format (what you write):
`[shot_size, camera, character_id, emotion, primary_action, power_id_or_null, marker, extra?]`
with `extra = {weapon, loadout, place, level, handling, sounds}` and dialogue lines `["D", char, emotion, line]`.

Full shot record (what the pipeline stores):
`shot_id, scene, project_id, location, time, characters[{id,tier,emotion,action,position,facing,loadout?,weapon?,handling?}], power, power_level, location_tag, camera, shot_size, duration_s, tier, dialogue[], prev_shot, state_mutations[], visible_background[], weather, primary_action, secondary_motion, continuity (CUT|CONTINUE|MATCH_ACTION), expected_visible[], composition, lens, applied_references[], reference_warnings[]`

### Datasets (reference art)
Types: `face` (character), `power`, `environment`, `weapon`, `outfit`. Stored as `roster/datasets/<type>/<name>/stills/*.png` + `meta.json` (each still records its variations: `outfit`, `weapon`, `handling`, `level`, `place`).

### References (what actually reaches the video model)
A manifest entry on the entity:
`{type, name, file, copied, role|tag|level, added}` — where `tag` = **loadout name** (composite still) or **place name** (environment variant), `level` = power ladder level.

---

## 3. How a shot becomes a clip

1. **Resolver** (`ltx_refs`) builds the reference set for the shot:
   - **one slot per character** — the composite still of the shot's loadout (identity + outfit + weapon in one image);
   - **power** — the still with the highest `level ≤ shot.power_level` (floor rule);
   - **environment** — the place variant (`location_tag` → scene map → primary);
   - budget: **LTX 5 / H3 6** pictures; tier order main → supporting → recurring; crowd never; overflow is dropped with a warning.
2. **Prompt builder**:
   - H3 with refs → official six-section Ref2VA (`subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, `non_diegetic_music`), grouped subjects, frame-0 anchor declared;
   - LTX → the MSR reference legend (Image N roles) + motion recipe + proven negatives;
   - Wan → the legacy structured prompt.
3. **Farm job** → mp4 (H3 also audio) → copied to `output/videostudio`; the shot records `applied_references` + `reference_warnings`.

**Degradation is graceful but visible:** missing refs, missing loadout tags, missing places and budget overflow all appear as warnings — read them before blaming the model.

---

## 4. When the operator sends you something — do this

| Request | Your output |
|---|---|
| "Write/generate the story" (from title+idea) | Run the **storytelling skill**: idea → spine (treatment/bible/beats) → scenes, JSON only, using ONLY the provided cast/location/power ids. |
| "Generate a good idea for this title" | Logline, synopsis, themes, escalation ladder, clipable moments, ending (storytelling skill §1). |
| "Add a character / weapon / loadout" | The exact Bible YAML fields (see §2). Then state what art is needed: a dataset batch (character composites with the weapon), which the operator runs and attaches as a loadout-tagged reference. |
| "Add a location / places" | Location `id/descriptor/lighting`; places are named variants — list them (e.g. `the throne hall`, `the fighting arena`) and note each needs an environment still tagged with its place name. |
| "Change a shot" (look/level/place) | Edit the shot fields: `characters[].loadout` / `.weapon` / `.handling`, `power_level` (1–10), `location_tag`. Never invent ids — check the cast list first. |
| "Render this" | Confirm the shot has: cast ids present in the Bible, a power owned by an in-shot character, `level` within 1–10, `location_tag` that exists as a tagged place, and the reference budget. Then the render runs on the project's `video_family`. |
| "The render looks wrong" | Checklist: (1) `applied_references` — were refs actually passed? (2) `reference_warnings`; (3) is the shot's loadout tagged with a composite still? (4) does the power level have a matching still? (5) is a weapon showing that shouldn't (use the plain loadout); (6) model choice — H3 pins frame 0 to the first reference. |
| "Make the stills/batches" | Dataset batch specs: **characters** = outfit × weapon composites (+ optional weapon-only stills); **powers** = level ladder 1–10 (one still per level, wan/z-image); **environments** = one still per selected place (each has its own camera/framing). |
| "It's stuck / failed" | Where to look: control-room logs, farm logs (`logs/`), the Render queue (System menu). Recovery: cancel the job (`/api/training/jobs/cancel`, `/api/videostudio/jobs`), retry, or restart the affected farm. Never touch a farm while it is rendering. |
| "Plan a movie of N minutes" | `duration ÷ 5` = shot count; scenes of 25–40 shots; hero ≤ 15 %, volume ≥ 30 %; power level curve ending at 10. |

---

## 5. Rules of engagement

1. **Never invent ids. Especially main characters** Characters, weapons, loadouts, powers, places must exist in the project vocabulary you were given. The linter rejects invented ids and sends the scene back.
2. **JSON only** when producing pipeline artifacts — no prose outside the object.
3. **Respect the reference budget** (LTX 5 / H3 6) — one slot per character via its loadout composite.
4. **Levels escalate** 1 → 10 across the film; the finale peaks.
5. **Weapons persist** once drawn; weapon-less scenes need a weapon-less loadout (a reference showing the weapon will keep it).
6. **Places are specific**: choose the room/area that matches the action, not the generic location.
7. **Continuity is data**: wounds, weather and damage persist via `state_mutations` and must be referenced later.
8. **Do not disturb running work**: never restart farms/API or requeue jobs while a render or batch is active unless asked.
9. **Ask before destructive actions** (deleting projects, datasets, references) and before changing a running project's cast.
10. **Report provenance**: when you deliver a scene or shot list, state which loadouts/places/levels it uses so the operator can verify the references exist.

---

## 6. Reference card

- Control room: `http://127.0.0.1:8080`. Movie studio: Casting (datasets), Cast & World (bible), Story, Shots, Render, Review, Film. Studio: Video Studio, Image Studio, Library. System menu: Render queue, Worker PCs, AI writer, Settings, Logs
- Story: architect jobs (spine → scenes) — output is validated by the linter before anything renders
- Datasets: `roster/datasets/<type>/<name>/stills/` + `meta.json`
- Project refs: `bible/projects/<pid>/refs/{chars,locs,powers}/…`
- Renders: `output/videostudio/*.mp4`; shot provenance in the shots DB
- Models: LTX-2.5 (5 refs, stage-1 only), MiniMax H3 (6 refs, frame-0 anchor, audio)
- Story vocabulary given to you per request: CAST (with weapons + loadouts + per-scene defaults), LOCATIONS (with places), POWERS + LEVELS, and the reference-budget rule.
