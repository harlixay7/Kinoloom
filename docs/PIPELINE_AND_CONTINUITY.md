[Kinoloom](../README.md) › [Docs](README.md) › Pipeline and continuity

# Filmmaking pipeline, canon checks and continuity

> **In short:** This doc follows a film through the studio, from the written story to the finished film file. It covers the story bible, the LLM story writer, and the canon linter that checks every shot against the bible. It explains how each shot hands over to the next, including Video Studio chains and movie takes, and then the quality checks (QA), post-processing and assembly. It is for anyone who wants to understand or change this pipeline as the code in `tool/` runs it today.

On this page:

- [1. The story bible](#1-the-story-bible-bible-and-studiodb)
  - [1.1 What the Bible holds](#11-what-the-bible-holds)
  - [1.2 Shot records](#12-shot-records-toolstudiodb)
- [2. The story architect and its LLM guardrails](#2-the-story-architect-and-its-llm-guardrails-toolstorygen_llmpy-toolllmpy)
  - [2.1 Talking to any OpenAI-compatible server](#21-talking-to-any-openai-compatible-server)
  - [2.2 Writing the story in stages](#22-writing-the-story-in-stages)
  - [2.3 Keeping the model to the Bible's names](#23-keeping-the-model-to-the-bibles-names)
  - [2.4 Recovering JSON from messy replies](#24-recovering-json-from-messy-replies-toolllmpyextract_json)
  - [2.5 The self-heal loop](#25-the-self-heal-loop)
- [3. The canon linter](#3-the-canon-linter-toollinterpy)
  - [3.1 Errors and warnings](#31-errors-and-warnings)
  - [3.2 Allowed values for camera, framing and staging](#32-allowed-values-for-camera-framing-and-staging)
  - [3.3 The hero gate](#33-the-hero-gate)
  - [3.4 Reference budgets](#34-reference-budgets-toollinterpy65)
  - [3.5 Continuity order rules](#35-continuity-order-rules)
  - [3.6 Emotion scale and emotion jumps](#36-emotion-scale-and-emotion-jumps)
  - [3.7 Deaths and exits, the shot graph, tier budget and lighting](#37-deaths-and-exits-the-shot-graph-tier-budget-and-lighting)
- [4. The continuity engine](#4-the-continuity-engine-toolcontinuitypy)
  - [4.1 Extracting and scoring the tail frame](#41-extracting-and-scoring-the-tail-frame)
  - [4.2 Choosing the start image for Wan shots](#42-choosing-the-start-image-start_image_for-wan-project-shots)
  - [4.3 Video Studio chains](#43-video-studio-chains-toolvideo_studiopy-toolchain_studiopy-toolchain_enginepy)
  - [4.4 Continuity for LTX-2.5 and MiniMax H3 movie shots](#44-continuity-for-ltx-25-and-minimax-h3-movie-shots-toolorchestratorpy-toolmovie_takespy)
- [5. The QA director and face checks](#5-the-qa-director-and-face-checks-toolqa_directorpy-toolface_qapy)
  - [5.1 Face identity check on the CPU](#51-face-identity-check-on-the-cpu)
  - [5.2 Prompt match (CLIP)](#52-prompt-match-clip)
  - [5.3 Motion check (Farneback optical flow)](#53-motion-check-farneback-optical-flow)
  - [5.4 The 5-stage retry ladder and tier budgets](#54-the-5-stage-retry-ladder-and-tier-budgets)
- [6. Post-processing alongside rendering](#6-post-processing-alongside-rendering-toolorchestratorpy-toolpostpy)
  - [6.1 The post queue](#61-the-post-queue-_post_worker)
  - [6.2 Post chain order](#62-post-chain-order-postpipelineprocess)
  - [6.3 Thermal gate](#63-thermal-gate)
- [7. Film assembly](#7-film-assembly-toolassemblerpy)
  - [7.1 Plain cuts with the concat demuxer](#71-plain-cuts-with-the-concat-demuxer-no-crossfades)
  - [7.2 Loudness normalization (EBU R128)](#72-loudness-normalization-ebu-r128)
  - [7.3 Two export files](#73-two-export-files)

---

## 1. The story bible (`bible/` and `studio.db`)

A film with many scenes only looks consistent if every shot draws on the same fixed facts. In Kinoloom those story facts do not live in the model weights. They live in structured Bible files (`bible/universe.yaml`, `bible/characters/`, `bible/locations/`, `bible/scenes/`, `bible/crowd_types.yaml`, `bible/luts/`) and in database records in `tool/studio.db`.

```
                    ┌────────────────────────────┐
                    │      Narrative Bible       │
                    │   (bible/*.yaml + Git)     │
                    └─────────────┬──────────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ Character Roster │    │ World Locations  │    │ Universe & Color │
│  - Identity tier │    │  - Lighting spec │    │  - Camera lenses │
│  - Loadouts      │    │  - Ref variants  │    │  - 3D LUT cubes  │
│  - Weapons/Gear  │    │  - Staging tags  │    │  - Depth of field│
│  - Powers/Color  │    └──────────────────┘    └──────────────────┘
│  - Emotion list  │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                  Relational Records (studio.db)                  │
│                                                                  │
│  shots: [shot_id, scene, json, status, tier, model, project_id,  │
│          spec_version, render_profile, qa_stage, keyframe_path,  │
│          tail_frame, prompt, negative, ref_image, final_path]    │
└──────────────────────────────────────────────────────────────────┘
```

### 1.1 What the Bible holds
1. **Universe rules (`bible/universe.yaml`):** the project-wide camera defaults, under `cinematography`. They set the default lens (`35mm prime lens`), the film stock (`cinematic film stock`), the depth of field (`deep focus, ...`), the key and fill light color temperatures (`color_temp_key: 3800`, `color_temp_fill: 6500`) and the grain.
2. **Characters and tiers (`bible/characters/<char_id>.yaml`):** every character has one of four identity tiers (`main`, `supporting`, `recurring`, `crowd`; `linter.CHAR_TIERS`). If a shot gives a character a different tier than the Bible does, the linter reports an error.
   - QA compares faces against a threshold per tier, from settings `qa.face_sim`. `settings.example.yaml` sets main 0.75, supporting 0.65, recurring 0.55. Crowd characters are exempt.
   - Main characters usually carry face references and a face LoRA (a small add-on model trained on that character's face). For example, `kael.yaml` has `face_refs` with front, side and three-quarter views, plus `face_lock`. Crowd types are defined in `bible/crowd_types.yaml`.
3. **Outfits, loadouts, weapons and powers:**
   - **Outfit and loadouts:** `outfit` is the base look (for example `black tactical coat, silver pauldron`). Optional named `loadouts` bundle an outfit with a weapon. A shot's `loadout` must be one that the character defines, or the linter reports an error.
   - **Weapons:** registered weapon ids, each with a `descriptor`. A shot's `weapon` must exist on the character (an error otherwise). `handling` without a weapon or loadout is a warning.
   - **Powers:** abilities bound to a character. Each has a descriptor, a hex color, a motion description and a sound effect. For example, `lightning` has `#00e5ff`, `fast zigzag bolts`, `crackling cyan lightning arcs from both hands` and `electric crackle`. A shot's `power` must be registered on a character in that shot (an error otherwise).
4. **Emotion palettes:** the emotions each character is allowed to show (for example kael: `["cold", "rage", "grief", "focused", "calm"]`). When a character has a palette, a shot or dialogue emotion outside it is a linter error, and the LLM prompt lists only these words.
5. **Color grading (3D LUTs):** the post chain grades with the LUT (a color look-up table) in settings `post.lut`. The default is `bible/luts/cinematic_teal_amber.cube`.
6. **Project snapshots and git history:** each project has its own Bible folder under git (`bible/projects/<project_id>/`). `Bible.snapshot_from_global()` runs when a project is created. It runs again when the Director or the LLM architect finds no `universe.yaml`. It clears the project's `characters/`, `locations/` and `scenes/` folders, copies in `universe.yaml`, `crowd_types.yaml` and the global character, location and scene YAMLs, and commits them as `project snapshot`. It does not copy `luts/`. Later Bible edits are committed per entity.

### 1.2 Shot records (`tool/studio.db`)
The Bible holds the story facts. The life of each shot is tracked in SQLite (`tool/studio.db`) through `tool/shots_db.py`.
- **Statuses:** `shots_db.py` defines `pending`, `drafting`, `queued`, `rendering`, `post`, `qa`, `passed`, `failed`, `review` and `assembled`. The orchestrator also uses `rendered`.
  - A movie shot moves through `pending` → `queued` (the render loop picks it) → `rendering` → `post` (raw render recorded) → `rendered` (early tail frame extracted) → `post` (post worker) → `passed` or `review`.
  - A QA retry or a render retry sends the shot back to `queued`.
  - `drafting`, `qa` and `assembled` appear only in recovery and terminal-status checks. The orchestrator never sets them. Only `record_qa(passed=False)` writes `failed`, and the orchestrator does not call it. The loop still re-picks `failed` rows for retry.
- **Stored fields:** `spec_version`, `render_profile`, `qa_stage` (the shot's place on the retry ladder), `keyframe_path`, `tail_frame`, `ref_image`, and `final_path` (the post-processed clip, when post-processing ran).

---

## 2. The story architect and its LLM guardrails (`tool/storygen_llm.py`, `tool/llm.py`)

The LLM story architect (`storygen_llm.ArchitectJob`, started from the Projects tab) writes a treatment, a Bible, story beats and a shot list for each scene. A built-in deterministic generator (`tool/storygen.py`) covers the same stages without an LLM. The Project Director uses that one (`docs/ARCHITECTURE.md`, section 4).

### 2.1 Talking to any OpenAI-compatible server
`tool/llm.py:LLMClient` speaks the OpenAI-compatible `/chat/completions` protocol through `httpx`, so you can use any server that offers that endpoint. The module names OpenAI, Anthropic gateways, DeepSeek, Ollama and LM Studio. The shipped default is Ollama at `http://127.0.0.1:11434/v1`.
- Settings `architect.*`: `base_url`, `api_key`, `model`, `max_tokens` (default 32000), `timeout_s` (default 600) and `retries` (default 2, capped at 5).
- Requests use `temperature: 0.8`. JSON stages also send `response_format: {"type": "json_object"}`.
- Transport errors, HTTP 429 and 5xx responses are retried with exponential backoff (2^attempt seconds, at most 30 s). Other 4xx responses fail at once. The client does not follow redirects, so the Bearer key is never sent on to another host.

### 2.2 Writing the story in stages
A whole film script in one call runs out of context and breaks the structure, so the architect splits the work:
1. **Spine (one call):** `build_spine_prompt` asks for three things in a single JSON object: the treatment (logline, synopsis, themes, protagonist, antagonist, escalation, ending...), the Bible (characters, locations, crowd types) and the beats. The scene count comes from `duration_target` (5-second shots). If the reply cannot be parsed, a follow-up message asks for JSON only. There are up to `architect.retries + 1` attempts.
2. **Normalization and import:** `package_norm.normalize_package` makes the package safe to import:
   - it expands the compact shot format;
   - it renames ids that cannot be file names, along with every reference to them (`normalize_ids`);
   - it forces any CONTINUE / MATCH_ACTION across a scene or location change to CUT;
   - it caps hero shots per scene.

   Then `StoryArchitect.import_stage` imports the treatment, bible, beats and scenes stages.
3. **Scene breakdown (one call per beat):** `build_scene_prompt` carries a running continuity state forward: the `state_mutations` of earlier scenes that still hold, each character's last `end_state`, and the previous scene's hook. It asks for exactly `est_shots` entries in the compact array format.

### 2.3 Keeping the model to the Bible's names
The scene prompt includes the project Bible's vocabulary and tells the model to use only these ids:
- `cast_lines`: each character's id, name, tier, powers, LoRA file, emotion palette, weapons (id + descriptor), loadouts (outfit / weapon) and per-scene defaults.
- `loc_lines`: each location's id and descriptor, its named places (the tags of its reference images) and per-scene defaults.
- `power_lines`: each power's descriptor, color and motion (with a note when it has no reference stills yet), plus the 1–10 power-level ladder.
- `SHOT_RULES`: tier mix, continuity rules, the reference budget (5 LTX / 6 H3 pictures), dialogue limits, and so on.
- **Rule:** characters, locations, powers, weapons, loadouts, location tags and background crowd types must all match Bible entries. Anything the model invents is a linter error, and that error feeds the self-heal loop.

### 2.4 Recovering JSON from messy replies (`tool/llm.py:extract_json`)
Long LLM replies often come wrapped in markdown, with prose around them, with trailing commas, or cut off in the middle. `extract_json()` recovers the reply object in these steps:
1. **Fences:** fenced blocks (```` ```json ```` or ```` ``` ````) that contain `{` are tried last one first, because earlier fences usually repeat the prompt's examples. The full text is tried after them.
2. **Trailing commas:** commas before a closing bracket or brace are removed (`,\s*([}\]])` → `\1`).
3. **Stage-marker tail scan:** the parser searches backwards (`rfind`) for the last stage marker (`{"treatment"`, `{"shots"`, `{"beats"`, `{"bible"`, `{"characters"`). It then moves out to the outermost object that encloses that marker (`_top_level_start`), because a package nests `{"shots": {"shots": [...]}}`. Without a marker it starts at the first `{`.
4. **Bracket scan that tracks strings (`_scan_object`):** one pass over the text tracks whether it is inside a string or an escape, and keeps a stack of open containers. When the root object closes (the stack is empty), that exact slice is parsed.
5. **Salvaging a cut-off reply:** if the reply ends before the object closes, the scan has already recorded every point where a value had just ended: the end of a string, a closed container, or a number or literal. For each point it also recorded the closing brackets and braces still needed there. It works backwards from the latest of those points (at most `_SALVAGE_TRIES` = 200 attempts). At each point it trims trailing whitespace and commas, adds the closers and tries `json.loads`, until one parses. If the start found from the marker fails, it tries again from the first `{`.

### 2.5 The self-heal loop
Each scene gets up to `architect.retries + 1` attempts (default 3). Each attempt runs these steps:
1. **Shape check:** `validate_scene_package()` checks the reply's structure strictly: only a `shots` key, non-empty lists, compact entries with 5–8 elements, dialogue entries shaped `["D", char, emotion, line]`, and an 8th element that is an object with known keys and a level of 1–10. If the check fails, one follow-up message sends the first 8 errors, the original prompt and the first 2000 characters of the previous reply:
   ```
   Your reply for scene <id> failed STRICT JSON validation:
   - <error 1>
   - <error 2>
   ...
   Return ONLY the corrected JSON object for this scene in the same format.
   ```
2. **Lint check:** a well-formed reply goes to `merge_scene_shots()`. It normalizes the shots and lints the whole project (the other scenes plus this one), using the reference budget of the project's video family. If the lint fails, one follow-up sends up to 8 linter errors with the same context:
   ```
   Your shots for scene <id> were rejected by the script validator with these errors:
   - [s01_03] hero tier requires shot_size in close-up range or a power
   - [s01_04] reference budget: 7 references needed (5 characters + power + environment) but the model accepts 6 — move extra characters to visible_background or drop the power
   ...
   Return ONLY the corrected JSON object for this scene (same format).
   ```
3. **Import:** a scene that passes replaces that scene's existing shots, and the merged shots stage is versioned.
4. **Partial result:** a scene that is still invalid after all attempts is skipped and recorded in `failed_scenes`, and the job goes on with the next scene. If any scene was skipped, the job ends with stage `partial` and a message to fix or re-import those scenes from the Story tab. Otherwise it ends with stage `done`. If the LLM endpoint sends no reply at all to a scene request, the whole job stops (`architect LLM request failed`).

---

## 3. The canon linter (`tool/linter.py`)

`linter.py` checks shot packages against the Bible when they come in. Three paths call it: `Orchestrator.ingest`, the story-stage import and the LLM architect's per-scene merge. A package with any error is not stored. So broken shots never reach the GPU, and the report lists what to fix.

```
                         [Proposed ShotSpec JSON]
                                    │
                                    ▼
       ┌────────────────────────────────────────────────────────┐
       │                Two-Tier Severity System                │
       │                                                        │
       │  • errors:   block the package (not stored)            │
       │  • warnings: advisory; reported, never block           │
       └────────────────────────────┬───────────────────────────┘
                                    │
       ┌────────────────────────────┼───────────────────────────┐
       ▼                            ▼                           ▼
┌───────────────┐           ┌───────────────┐           ┌───────────────┐
│ Enum Checking │           │ Budget Gates  │           │ Continuity &  │
│  - Camera (9) │           │  - Hero gate  │           │ Graph Invariants│
│  - Sizes (7)  │           │  - Ref budget │           │  - Emotion jump│
│  - Zones (9)  │           │  (6 H3/5 other)│          │  - No cross-loc│
│  - Facings (4)│           │  - Tier mix   │           │  - Run <= 2   │
│  - Tiers (3)  │           │    (<=15% hero│           │  - DAG acyclic│
└───────────────┘           └───────────────┘           └───────────────┘
                                    │
                                    ▼
                         [LintReport: ok / e / w]
```

### 3.1 Errors and warnings
- **`errors` (`rep.e(shot, msg)`):** any error makes `rep.ok` false and blocks the package. Errors are:
  - enum violations (a value outside the allowed set);
  - unregistered characters, locations, powers, weapons, loadouts, location tags, dialogue speakers and background crowd types;
  - shot vs. Bible tier mismatches, and emotions outside the palette;
  - a location-bound character placed outside its locations;
  - the hero gate, the hero budget and the reference budget;
  - `power_level` outside 1–99, and `selected_loras` that is not a list;
  - continuity bridges across a scene or location change;
  - DAG defects (root count, missing or self `prev_shot`, cycles, orphans).
- **`warnings` (`rep.w(shot, msg)`):** shown in the lint result and the UI, but they never block. Warnings are:
  - `duration_s` other than 5; an empty or overlong (>25 words) `primary_action`; `expected_visible` ids that are not in the shot;
  - a shot with no characters; `handling` without a weapon or loadout; dialogue lines over 25 words;
  - volume tier below 30% of a scene;
  - continuity without a `prev_shot`, more than two bridges in a row, and time-of-day changes across a bridge;
  - emotion jumps, death / exit violations, lighting and time inconsistencies;
  - one informational line per accumulated state mutation.

### 3.2 Allowed values for camera, framing and staging
Any value outside these fixed sets is an error. The linter checks `camera`, `shot_size`, `tier` and `continuity` on every shot, and `position` and `facing` on every registered non-crowd character:
- **Camera moves (`CAMERA_MOVES`):** `{"static", "slow_push_in", "dolly", "pan", "crane", "handheld", "orbit", "pull_back", "tracking"}`.
- **Shot sizes (`SHOT_SIZES`):** `{"extreme_close_up", "close_up", "medium_close_up", "medium", "medium_wide", "wide", "extreme_wide"}`.
- **Staging grid (`POSITIONS`, 9 zones):**
  - Foreground: `foreground_left`, `foreground_center`, `foreground_right`
  - Midground: `midground_left`, `midground_center`, `midground_right`
  - Background: `background_left`, `background_center`, `background_right`
- **Facings, relative to the camera (`FACINGS`):** `{"camera_left", "camera_right", "camera", "away_from_camera"}`.
- **Shot tiers (`TIERS`):** `{"hero", "standard", "volume"}`.
- **Continuity types (`CONTINUITY_TYPES`):** `{"CUT", "CONTINUE", "MATCH_ACTION"}`.
- **Character tiers (`CHAR_TIERS`):** `{"main", "supporting", "recurring", "crowd"}`. A character's tier in the shot must equal its Bible tier.

### 3.3 The hero gate
The `hero` tier marks the shots that get the most expensive treatment (for example a MiniMax H3 tier, through the project's per-tier model map). The gate keeps that cost off wide staging, where facial detail is lost:
- **The rule:** a shot with `tier: "hero"` must have its `shot_size` in `HERO_SHOT_SIZES` (`extreme_close_up`, `close_up`, `medium_close_up`), **or** it must show a character power (`s.get("power")`).
- A wide shot without a power fails the lint with this error:
  `"hero tier requires shot_size in close-up range or a power"`.

### 3.4 Reference budgets (`tool/linter.py:65`)
Each video family accepts a fixed number of reference pictures. The linter takes the project's family and enforces this budget:
$$\text{Budget} = \begin{cases} 6 \text{ reference slots} & \text{if the family starts with } \text{"h3"} \\ 5 \text{ reference slots} & \text{otherwise (LTX-2.5 MSR; also the default when no family is passed)} \end{cases}$$
- The number of references a shot needs is:
  $$\text{Need} = N_{\text{characters whose shot tier is not crowd}} + (\text{power} ? 1 : 0) + (\text{location} ? 1 : 0)$$
- If $\text{Need} > \text{Budget}$, the linter rejects the shot with an error that tells the LLM to move extra characters to `visible_background` or drop the power.

### 3.5 Continuity order rules
The linter walks the shots in `(scene, shot_id)` order:
- **Bridge without a source:** `continuity: CONTINUE` or `MATCH_ACTION` without a `prev_shot` in the package is a **warning** (`nothing to bridge`).
- **No bridges across scenes or locations:** if `prev_shot` belongs to a different scene, or to a different location (when both are set), the linter reports an **error**. Scene cuts and location changes must be `CUT`. LLM packages are normalized before the lint, and `package_norm` already turns such bridges into `CUT` with a note.
- **Limiting identity drift:** chaining tail-frame anchors one after another adds up visual drift (posture, costume, face). The run counter goes up on every non-`CUT` shot and resets on `CUT`:
  $$\text{Continuity Run} \le 2$$
  When `run > 2`, the linter gives a **warning** (advice only) to insert a `CUT` and reset the identity drift.

### 3.6 Emotion scale and emotion jumps
Emotions should build step by step. The linter maps emotion words to an intensity scale from 0 to 5 (it skips words that are not on the scale):
- Level 0: `calm`, `serene`, `relaxed`, `peaceful`
- Level 1: `neutral`, `content`, `composed`, `curious`
- Level 2: `sad`, `melancholy`, `somber`, `worried`, `concerned`
- Level 3: `nervous`, `anxious`, `tense`, `suspicious`
- Level 4: `angry`, `fearful`, `frustrated`, `defiant`
- Level 5: `furious`, `rage`, `terrified`, `horrified`

**Rule:** `EMOTION_JUMP_MIN = 3`. When a character's emotion changes by $|\Delta E| \ge 3$ between two appearances in a row within a scene (for example from `calm` (0) straight to `furious` (5)), the linter gives a warning:
`"<cid>: emotion jump <prev> -> <now> between consecutive appearances"`.

### 3.7 Deaths and exits, the shot graph, tier budget and lighting
- **Deaths and exits (warning):** a state mutation whose `key` starts with `dead` or `left` marks its `target` as gone. If that character appears again in a later shot of the same scene, the linter gives a warning.
- **Shot graph (errors):** the shots of a scene form a DAG (a directed acyclic graph: links that never loop back), connected through `prev_shot`:
  - Exactly one root shot (`prev_shot: None`) per scene.
  - A `prev_shot` that is missing from the scene, or that points to the shot itself, is an error.
  - A depth-first walk from the root detects cycles (`rep.e(scene, "cycle detected at ...")`).
  - Shots that cannot be reached from the root (orphans) are errors.
  - The walk (the cycle and orphan checks) runs only when the package has no errors so far.
- **Tier budget:** in a scene of `n` shots, more hero shots than `max(int(n × 0.15), 1 if n ≤ 6 else 0)` is an error, so a small scene can still have one hero moment. A volume share below 30% is a warning.
- **Lighting (warnings):** three cases are reported as **warnings**: a shot whose `lighting` differs from its Bible location's lighting, a scene whose shots disagree on `time`, and a CONTINUE / MATCH_ACTION bridge across a time-of-day change. `time` is compared ignoring case and whitespace. These never block a package, because errors drive the LLM self-heal loop and time labels are free text.

---

## 4. The continuity engine (`tool/continuity.py`)

To keep continuity across cuts, a video model needs a clean, stable start image. `tool/continuity.py` extracts the tail frame (a clean frame from the end of a clip, handed to the next shot), scores the candidate frames, and picks the start image for legacy Wan 2.2 shots. Section 4.3 covers Video Studio chains, and section 4.4 covers LTX-2.5 / H3 movie shots.

**Which model a project shot renders on** (`orchestrator._video_family`): the shot's own `video_model` first, then the project's model for the shot's tier (`video_models`), then the project's video model (`video_family`).
- An installed LTX-2.5 or MiniMax H3 tier renders as picked.
- The legacy Wan 2.2 model renders only when it is picked and installed.
- Anything else falls back to the studio default, `video_models.default_model_id()`: no model set, a tier that is not installed, or an entry of another family. The default is `ltx25_s6_cq`, or the first installed LTX-2.5 or H3 tier when that one is missing. It is never Wan.
- With no LTX-2.5 or H3 tier installed, the shot goes to `review` with an error.
- Shots without a project (sent through the API, `/api/shots/ingest`) still take the legacy Wan path.

### 4.1 Extracting and scoring the tail frame
When a shot finishes rendering, `extract_tail()` decodes every frame of the clip to PNGs in a temporary folder and scores the last 5 (`n=5`). The last frames of a diffusion render often show motion blur or smearing, so it picks the sharpest one:
- Each candidate gets a score from its Laplacian variance (a sharpness measure) and its Canny edge density (Canny thresholds 50/150; density = share of edge pixels):
  $$\text{Score} = 0.7 \cdot \min\left(1.0, \frac{\ln(1 + \max(\text{Var}_{\text{Laplacian}}, 0.0))}{\ln(1 + 5000.0)}\right) + 0.3 \cdot \text{Density}_{\text{Canny}}$$
- The frame with the highest score is copied to:
  `shots/tails/<project_id>/<shot_id>_tail.png` (`_` replaces a missing or unsafe project id)
- This image is the continuity hand-off to the next shot.

### 4.2 Choosing the start image (`start_image_for`, Wan project shots)
For project shots on the legacy Wan 2.2 model, the orchestrator first uses the shot's `ref_image` when that file exists. Otherwise `start_image_for()` picks the image-to-video start image in this order:
1. **`CONTINUE` / `MATCH_ACTION`:** the previous shot's tail frame (`tail_frame` of `prev_shot`). Both modes use the same image. They differ only in the continuity note the UI shows ("CONTINUE from ... tail frame" vs. "MATCH_ACTION — match ... tail composition").
2. **Keyframe:** the shot's `keyframe_path`. Next, when `keyframe_source` is `generated` or `auto`, the approved candidate in `shots/keyframes/<project_id>/<shot_id>/candidates.json`, or the highest-scoring one if none is approved. A `CUT` starts at this step.
3. **Roster fallback:** a portrait from `roster/keyframes/<first non-crowd character>/`, or else a location still from `roster/locations/<location>/` or `bible/locations/<location>*.png`. The project's `ref_fallback` setting (default on) controls this step. When it is off (`allow_roster=False`), or when nothing is found, there is no start image and the shot renders as text-to-video.

### 4.3 Video Studio chains (`tool/video_studio.py`, `tool/chain_studio.py`, `tool/chain_engine.py`)
A Video Studio chain renders one long take as a series of links, each continuing the one before. The join between two links is a seam. The chain holds one GPU-broker lease (its turn on the graphics card) for all its links, and there is no `/free` (the ComfyUI call that unloads models) between links. There are two engines:

- **Seamless chains** (the default; `video_studio.seamless_chains: true`) are for the LTX-2.5 tier with a `chain` workflow on `ComfyUI_LTX`, and for H3 tiers with a reference workflow. Every link continues from saved, lossless state, never from an encoded mp4: latents (the model's compressed form of the frames) and uint8 frames under ComfyUI's `output/studio_chain/<chain>/`. The take is encoded once. See section 4.3.1.
- **Segment chains** (the earlier engine) are for every other tier, or for any tier when the switch is off. See section 4.3.2.

**In the Video Studio page.** The prompt is segment 1, and **+ Segment** adds the next ones. Two or more segments render as a chain. A tag above the prompt names the engine that the selected model uses.

Some ⛓ models have a finish pass: LTX-2.5 and the H3 1080p tiers (`video_models.chain_has_finish`). An H3 draft tier delivers its decode-once take directly. For a ⛓ model with a finish pass, **Preview first** renders the draft take, runs QA on it and waits. **Finish 1080p** on the chain's History card then renders the finish from the same drafts.

**History** (`GET /api/videostudio/history`, `video_studio.history`) lists every clip and chain the studio rendered, newest first. That includes Video Studio chains, the movie's continuous takes (tagged *movie*, with their project and shots), and chains from before chain records existed, rebuilt from their `chain_<id>_*.mp4` files. A chain record that an earlier studio process left mid-run shows as *interrupted*.

Opening a card plays it. The card shows the draft and final takes and the seam QA of each: every seam with its pop (the picture's jump at the seam) and its saturation and lightness steps. It also shows the drift and the per-person cast report. ↪ on any finished clip or take stages its last frame as the opening anchor of the next render (`continue_from` takes a job id, a path or a file `name`). A single clip sends that frame as its `continuation`. A **chain** opens on it: with no start image chosen, the page sends the name of the copy `continue_from` staged in the ComfyUI input folders, and `start_chain` refuses a start image that is in neither of them.

Each reference card has a **look** field (age, hair, outfit) that becomes the cast line of an LTX chain. When it is empty, the face dataset's parameters are used. H3 chains do not use it.

Video Studio has rendered LTX-2.5 chains end to end on the GPU through these paths, in landscape and in portrait. H3 chains in Video Studio have not run on the GPU yet (`SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md` §6).

**Drafts, finishes and resume.** A chain can stop after its drafts (`draft_only`, status `draft_ready`) and be finished later (`POST /api/videostudio/chain/{cid}/finish`). The chain's `manifest.json` records every step with its graph digest (a fingerprint of the graph), plus the size and mtime (last-modified time) of every chain file the graph reads. So a resumed or re-run chain skips a step only when its graph, its outputs and its inputs are all unchanged. That also means re-rendering one step (an edited link prompt, a changed carry setting) re-renders every later step that reads its files: the next links' drafts, the finishes and the takes.

#### 4.3.1 Seamless chains
**LTX-2.5** (`chain_engine.LtxChain`, graphs in `tool/chain_graphs.py`, nodes in `ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/`):
- **Links:** link 1 renders `frames` frames. Each later link renders 24 more and opens with a **25-frame carry** (4 latents: a single-frame anchor plus 3 latents of 8 frames) that repeats the previous link's last 25 frames.
- **Draft (stage 1, 960x544):** S6 sigmas, euler ancestral, the MSR multi-reference guide with the chain's reference set on every link, and no CQ enhancer LoRA (`cq_strength` 0; it ages faces, see *Identity* below).
- **Finish (stage 2, 1920x1088):** the draft's latent goes through the latent upsampler. The draft's own model then refines it (the MSR IC-LoRA and the CQ LoRA at the draft's strengths, `msr_lora`), with sigmas 0.55/0.3/0 and euler ancestral. It runs at the link's spec seed + 1, also when the draft was re-rolled to seed + 7919.
  - The MSR references are *not* re-applied in stage 2 by default (`msr_stage2`). At 1080p they take the token count from about 43k to 73k, which no longer fits an 8 GB card. In the one run that tried it, the first stage-2 step had not finished after 7 min 37 s when the run ended with a dropped server connection.
  - The draft's detailing keyframes are not re-applied either (`stage2_keyframes`, +10.2k tokens).
  - **Why not the official distilled stage 2** (the bare model from sigma 0.909375, euler): it keeps only 9% of the upsampled draft. Without references in stage 2, the model re-draws whatever the prompt does not name. On gate link 1 it turned the woman's cream cable-knit turtleneck floral and the man's overshirt plaid, and it changed both faces. The table compares each stage 2 with the draft, same seed (SSIM / mid-band texture correlation / Lab chroma error; detail = Laplacian variance):

    | Stage 2 | SSIM | Texture | Chroma | Detail | Wardrobe | Time |
    |---|---|---|---|---|---|---|
    | bare, 0.909, euler, keyframes (previous default) | 0.55 | 0.35 | 5.0 | 202 | floral, plaid | 733 s |
    | bare, 0.909, euler | 0.52 | 0.32 | 5.0 | 219 | floral | 287 s |
    | bare, 0.85, euler | 0.61 | 0.52 | 3.9 | 228 | leaf embroidery | 248 s |
    | draft's model, 0.909, euler | 0.50 | 0.29 | 5.3 | 224 | pointelle knit | 193 s |
    | draft's model, 0.85, euler ancestral (single-shot workflows) | 0.58 | 0.46 | 4.4 | 253 | cable knit; faces aged | 212 s |
    | draft's model, 0.725, euler ancestral | 0.72 | 0.71 | 3.0 | 225 | cable knit; faces older | 182 s |
    | **draft's model, 0.55, euler ancestral (default)** | **0.80** | **0.84** | **2.3** | 215 | **cable knit; faces kept** | **138 s** |

    Times are the finish step's wall time in the run records (`output/seamlab/runs/`). The session's comparison script printed the fidelity columns, and they were not saved. The saved comparison of 0.725 against 0.55 on the CQ-off draft with the face close-up gives SSIM 0.742 / 0.818 and texture 0.695 / 0.827 (`SEAMLESS_CHAINS_01_LTX.md` §6).

    From 0.725 the wardrobe held, but the faces came out older, with forehead furrows and lines under the eyes. On this CQ-0.6 draft the lead's apparent age (InsightFace) was 40.1 from 0.725 against 35.8 from 0.55. On the CQ-off draft with the face close-up (see identity below) the gap is similar, 3.6 years against 4.3: the lead is 29.5 in the draft, 33.1 from 0.725 and 29.5 from 0.55 (ArcFace similarity to her portrait 0.394 / 0.241 / 0.351). Stage 2 with the CQ LoRA at 0.6 over a CQ-off draft adds about 20% detail and 1.5 years (`--set cq_strength=0.6` in the lab; not the default).
- **Audio:** stage 1 carries the previous link's audio latents into the head of the new audio stream under noise mask 0 (`LTX25CarryAudioLatent`). Stage 2 freezes the audio. The take's sound is joined in latent space and decoded once (`StudioLtxAudioTake`), so the sound has no decode boundary at the joins; the seam QA still checks its level at every join.

**The carry recipe.** These are the defaults of `LtxChainSpec`, the same in both stages. Each part deals with a defect measured on a same-seed gate chain (a fixed test chain):

| Part | Node | Defect it removes (measured) |
|---|---|---|
| Soft pin ramp 1.0 / 0.8 / 0.6 / 0.4 per carried latent | `StudioPinRamp` | A hard pin freezes the carry, and the first free latent starts the new content in one step: lightness −3.5, motion 5x, ragged edges. |
| Latent carry | `StudioLtxLatentCarry` | Re-encoding the decoded tail (`LTXVImgToVideoInplace`) is a VAE round trip (decode to pixels, encode back) per link. Measured once (decode-only; printed in the session, not saved): +1.7 saturation and extra sharpening. Repeated every link it is expected to compound, which was not isolated. The carry is now the anchor (encoded from the first carried frame alone) plus the previous link's last 3 sampled latents, copied exactly. |
| Carry restore | `StudioCarryRestore` | The model re-renders soft-pinned latents with a per-channel gain of x1.06–1.09 (+10 saturation over the carry; printed in the session, not saved). For each channel and latent frame, the output is regressed on the encoded carry, and that gain and offset are removed. The residual (the model bending the carry toward the new content) is kept. |
| Seam look path, factor 1.0 over 4 latents | `StudioSeamAdain` (mode `path`) | The first free latent still overshoots (+5 saturation, about +30% detail), and it can flash on its own. On gate link 3 it left the carry's look and came straight back on the next latent: a +12 to +15 saturation flash on the static background over frames 241–248. Matching the 4 ramp latents as one block (mode `window`) cannot remove that. Now each ramp latent is matched on its own, in per-channel mean and log-std, to the straight line from the last carried latent to the first latent after the ramp. So the look leaves the carry continuously, and the ramp ends on the content's own look. At that carry end the saturation step went from +9.6 to −2.6. Chains drafted before the mode existed resume with `window`. |
| Decode-once take, latent crossfade over the carry | `StudioLtxDecodeTake` (join 4, blend 3) | Dissolving two decodes of the same moment ghosts, and each link's decode sees different context. So the links' latents are joined into one take latent and decoded in streamed, overlapping windows with the links' own tiling. The LTX video decoder is not causal (it also looks at later latents), so every window keeps 2 latents of context on both sides. A 1080p take is never held in memory. The 3 carried latents after the anchor are crossfaded in latent space, from the previous link's latents to the new link's re-render of them (weights 1/4, 2/4, 3/4), and the take switches at the carry end. A hard switch on carried latent 2 (the earlier default) left a second, separate step next to the carry end (+3.1 saturation at one junction of the 1080p gate take). With the crossfade, each link boundary is one event, with no ghosting on the review strips. |

Then the take is encoded once (x264 crf 16, BT.709) with the one-piece audio. A pixel cross-fade assembly (`chain_assemble.assemble_ltx`) stays as a fallback (`take_decode=False`). Chains drafted before these settings existed resume with their original carry (`chain_studio.LTX_LEGACY_CARRY`).

**Pixel look path.** A carry end whose look still steps in the decoded take gets a pixel-space look path at the delivery encode (`chain_assemble.look_path_plan`, `look_path`). The latent match adjusts each 8-frame latent as a whole, so a change inside the first free latent survives it. On the v4 gate, link 2 ended on a growing sun glare (lightness 24.6 → 31.4 over its last 10 frames), and link 3 dropped it within 2 frames at both seeds (−4.4; QA on the lossless draft take: saturation +7.8, lightness −4.9, pop 1.51x).

The frames after such a carry end (16) are matched one by one, in Lab color space, to the straight line from the last carried frame's look to the look 16 frames on:
- lightness in mean and spread (spread on a log line);
- chroma in mean (matching the chroma spread overshot saturation on dark frames);
- then the chroma is scaled about neutral, by at most 0.8–1.25x, so the frame's HSV saturation lands on its own line too, since brightening a frame lowers its HSV saturation.

The seam then measured pop 0.83x, saturation −1.62, lightness −2.06 in the draft take and 0.89x / −1.95 / −1.78 in the 1080p take. The lightness step passes because the scene's own lightness changes that fast there. Seams that pass the look checks are not touched, and the lossless take stays as decoded.

**Seam re-roll.** Some carry-end checks are decided by the link's own sampling: pop, saturation, lightness or contrast (`seam_qa.carry_end_check`). A drafted link that fails one of them is drafted once more with seed + 7919, and the attempt with the lower excess over the gates is kept (`reroll_seams`). The kept seed goes into the manifest, and the other attempt is kept under `rejected/`.

**Identity (LTX).** With the seams fixed, the cast still aged along a chain. This was measured with InsightFace (apparent age and ArcFace similarity to the cast portrait, `tool/face_drift.py`) on gate link 1 with the same seed. The ages are the lead's. The estimator read her two reference images as 29 and 35 (the first three rows used the one read as 35). It reads crying as older and is off by years in absolute terms, so only the same person across variants means something:

| Change | Lead's age / similarity | The cause it deals with |
|---|---|---|
| Previous recipe (CQ 0.6, portraits only), draft | 32.6 / 0.205 | |
| Previous recipe, 1080p finish from 0.725 | 40.1 / 0.178 | |
| Draft without the CQ LoRA | 25.0 / 0.350 | The CQ "enhancer" LoRA sharpens by carving skin texture and lines. The face renders older with it in every variant, both people, both seeds tried (seed 2: 26.5 / 30.6 at CQ 0 / 0.6; the man 38 / 43). |
| + the lead's face close-up in a free MSR slot | 29.5 / 0.394 | The MSR guide fits each reference to the draft grid. In a full-length 16:9 portrait the face is ~90 px tall, too small to carry identity. `chain_studio.ltx_face_slots` crops square face close-ups of the cast portraits, lead first (`tool/face_ref.py`, OpenCV Haar, cached), into the `pic1`–`pic4` slots still free, so the face gets ~270 px. With a start image and three cast references no slot is free, and no close-up is added. On the CQ-0.6 draft the close-up alone raised her similarity from 0.205 to 0.303 (same measurement set), and the face matches the portrait (moles, freckles). |
| + stage 2 from 0.55 instead of 0.725 | 29.5 / 0.351 (final) | From 0.725 stage 2 re-draws the face with forehead furrows (33.1 / 0.241 from the same draft). |

The first three rows and the last two come from two separate runs with different reference crops and face sampling (41 and 21 faces). Ages compare across the two runs, similarities only within each.

Over the whole 4-link gate chain (same seeds; per-link means from `face_drift`), the lead's apparent age changed like this:
- previous recipe: 32 → 36 → 39 → 41 in the draft (link 4 on one detected face) and 40 / 39 / 39 in the 1080p take;
- now, at seed 1: 30 → 32 → 28 → 26 in the draft and 30 / 33 / 30 / 28 in the 1080p take;
- at a second seed the 1080p take reads 24 / 30 / 32 / 28, so the rise is smaller but not gone.

Her similarity now rises along the chain (0.32 → 0.35), where it used to fall (0.20 → 0.17). The second character, detected mainly in links 1-2, still reads older in link 2 (34 → 38), and his similarity falls (0.41 → 0.28). That is not solved.

**Cast sentence.** A cast sentence (`chain_studio.with_cast_line`: "Cast, the same age and look in every shot: …") goes after the first sentence of every link prompt. It comes from each reference's `description` or the face dataset's generation parameters. Stage 2 has no references, so the prompt is its only identity cue. In the lab a hand-written cast sentence (`chain_lab.py --cast-line`) took the 0.725 finish from 40 to 36 (printed in the session, not saved). The studio's generated wording has not been measured. Every take gets a per-link cast report (`draft_cast` / `cast` on the chain record; reported, not gated).

**The prompt is read literally.** With the gate's link 1 prompt ("…raises his right hand…, both holding a stationary, brightly lit close two-shot…"), seed 1 drew a glowing hand-held light in the man's hand. It did so whenever the face close-up was used at CQ 0 or 0.3. CQ 0.6 at seed 1, and every CQ strength at seed 2, drew none. Write camera holds as "the camera holds …", not "holding a … shot". The seam re-roll does not catch a content defect like this. Re-draft the link with another seed.

**H3** (`chain_engine.H3Chain`):
- **Windows:** up to 192 frames on H3's 17k+5 grid, and divisible by 3, so the video lands on whole 40 Hz audio ticks.
- **Carry:** each later link takes the previous link's last 39 frames as a masked audio-video latent prefix (12 video tokens + 65 audio ticks, `StudioH3CarryPrefix`), instead of a frame-0 still.
- **Seam look match:** the first 5 new tokens after each carry are look-matched to the line from the carry's look to the new content's (`StudioSeamAdain` path mode, `H3ChainSpec.seam_adain` 1.0, `seam_ramp` 5). H3's video tokens come in groups of 5 (the first covers one frame, the rest four), and each phase has its own level. So every token is matched against the carry and content tokens of its own phase (`period=5`). On the 544p smoke take the seam failed without the match (pop 1.74x, saturation step +2.98, detail 1.21x) and passed with it (1.21x, +1.76, 1.13x). A 10-token ramp eased the saturation further (+1.25) but still popped (1.57x). Chains linked before the match existed resume without it (`chain_studio.H3_LEGACY_CARRY`).
- **Decode once:** the links are joined into one take latent (`StudioH3ConcatTake`) and decoded once at the base resolution.
- **Refine once:** tiers with a refine stage then refine the whole take **once** at 1080p, instead of refining each link. The refine uses the learned 3D latent upscaler + `MMH3SplitUpscale`, whose temporal chunk junctions are kept at least 5 tokens off the seams, so each seam is refined inside one chunk. Base-only tiers deliver the decode-once take.
- **Refine steps:** the refine runs in three steps (`H3Chain.refine`):
  - the link conditionings are encoded (each link's prompt with the references, at the base size) and saved;
  - the refine samples from them with the DiT (the main video model) and the upscaler only, and saves the refined take latent;
  - a last step decodes it once.

**H3 gate results.** Measured on the H3 gate (the old engine's 4-link chain `chain_5c9b1e8b` re-rendered, 4 x 192-frame windows, 651 frames). One render per engine.
- The 1080p take (refine from 0.45) passes every seam (pop 1.08 / 1.25 / 0.94, saturation step +0.78 / +1.29 / +0.43). The old engine's take failed every one (pop 3.06 / 2.25 / 2.16, +18.8 / +11.9 / +10.2) and visibly jumped in pose and light at its cuts.
- The take's overall verdict still fails on the drift gate (54 %, the story's push-in to a darker close-up).
- The 544p base take still fails one seam (+2.74 saturation, 1.21x detail). The refine smooths it.
- Identity to the reference portraits falls less along the chain (woman 0.34 -> 0.28, man 0.37 -> 0.38 by `face_drift`) than in the old take (0.32 -> 0.18, 0.40 -> 0.25). Apparent age varies by up to about 6-7 years between links in both (the woman 23.8-30.2 in the new 1080p take, 24.9-31.6 in the old).
- Timings on the RTX 3070: links ~20 min each at 544p (32 steps), 544p take decode 4 min, then 3 min of conditioning, 61 min of refine (7 chunks x 6 tiles x 5 steps) and 12 min of 1080p decode. That is about 2 h 40 min for 27 s of 1080p.

**H3 model drops and the restarts.** An H3 chain does not drop a model in-process at a job hand-over. Inside the refine graph, the upscaler pack's own end-of-node unloads remain (`MinimaxH3LatentUpscaler3D` with `force_unload`, `MMH3SplitUpscale` unloading the DiT when it ends), as they do in the studio's single-clip H3 1080p workflows.

ComfyUI's default RAM-pressure cache releases, after the first node of a graph, every cached model that the running graph does not use. So a decode-only graph after the links makes the server let go of the DiT and the text encoder. On the gate the server's RSS (the RAM the process holds) fell from 34.6 GB to 2.9 GB within about 6 s.

During the H3 tests the studio PC blue-screened inside the Windows GPU memory manager three times. Each crash came within about a minute of H3 models being released in-process: after the lab's `/free` between jobs at 07:05 and 14:46, and after that cache release at 15:54. The lab's `/free` dropped a loaded DiT at four hand-overs in those tests, and two of them were followed by a crash.

Before a graph that would drop a model, the chain now restarts the server instead (`H3Chain._fresh_if_dropping`; in the studio `gpu_broker.restart_leased`, under the chain's own lease). The broker already does the same when it evicts H3. Links name the same models and run on without a restart. By design a 1080p chain restarts the server before `decode_B0`, before the refine and before the refine's decode. The conditioning step runs in the process left by the take decode.

Measured in the lab: four restarts of 19-21 s on the gate, before the refine and the decode of R0 and R25. The two H3 showcase chains also ran the restart before `decode_B0` (`docs/SHOWCASE.md` §6-7). Two parts have not run on the GPU: the conditioning step in the process left by the take decode (each showcase chain's 1080p pass started on a fresh server), and the studio's `restart_leased`.

The split also sizes each process to its step. In one graph (encode, upscale and refine, no decode), the gate's refine held 41 GB (DiT, text encoder, VAEs, upscaler) and left 5-7 GB of RAM. That is at and below the cache's headroom (10 % of RAM, about 6.4 GB here; `comfy_execution/caching.py`, `main.py`). Below that headroom the cache first evicts the running graph's own outputs of 512 MB or more. On Windows it frees pinned memory and further outputs, models included, only once less than 512 MB is available or the pagefile is at least 5 % used. A 1080p decode of the take (16 GB of float frames for four links) would have had to fit in that headroom had it run in the same process.

**QA:** `tool/seam_qa.py` measures every seam of the delivered take:
- the frame-difference pop against the local motion;
- steps in saturation, lightness, contrast and detail;
- the optical-flow speed on both sides;
- the audio level step and click (a sound starting out of silence or ending into it, below −60 dBFS, is reported as an onset, not failed);
- the drift of the look from link 1 to the last link.

The lab harness `tool/scripts/chain_lab.py` runs gate chains on an isolated ComfyUI instance. It has GPU/RAM telemetry, a stall watchdog (stack dump, interrupt, kill) and a GPU-starvation warning that is only logged. It also keeps per-job graphs, event logs and server-log slices.

#### 4.3.2 Segment chains
Segments render one after another (`_run_chain` → `_run_chain_body`), and each segment continues from the previous one.
- **LTX overlap-extend (17 frames):** this applies when the tier's family starts with `ltx` and the tier has a `chain` workflow.
  - After each segment, its last 17 frames (`OVERLAP = 17`) are extracted as an all-keyframe mp4 (crf 12) into the instance's input folder.
  - From segment 2 onward the chain graph is used. It pins those frames in place at the leading latent frames (stage 1), and again after the latent upsampler (stage 2).
  - These segments render `frames + 16` frames (`OVERLAP - 1`; `1+8k+16` stays on LTX's `1+8k` grid), and the assembly trims 17 leading frames from each of them.
  - The tail still also takes `pic1`, and cast references shift down (`pic1→pic2`, `pic2→pic3`, `pic3→pic4`; `background` stays; higher slots are dropped).
  - If the tail-video extraction fails, the next segment falls back to the `pic1` still path without a trim.
- **H3 identity-aware anchor frame:** for H3 the tail becomes the next segment's frame-0 identity anchor (`_apply_references_h3(..., continuation=...)`), and the cast references go in the Picture slots. `_extract_anchor_frame` picks that tail:
  1. It scans the segment's last 36 frames (scaled to 480 px wide).
  2. It scores each one as $0.60 \cdot \min(1, \text{face\_frac}/0.05) + 0.20 \cdot \text{sharp} + 0.20 \cdot \text{exposure}$. Faces come from OpenCV Haar cascades (frontal and profile, also mirrored) on a CLAHE-equalized image (local contrast evened out). `sharp` = min(1, Laplacian variance / 300), and `exposure` = 1 − min(1, dark / 0.5), where `dark` is the share of pixels below 38.
  3. When the best score is at least 0.45, it takes the **latest** frame that scores within 90% of the best. Otherwise it keeps the exact last frame.
  4. A chosen score below 0.35 adds a warning to the chain record ("the next clip may drift in appearance..."), and every choice is logged in `anchor_choices`.
  5. If OpenCV is unavailable or the scan fails, the exact last frame is used.
- **Wan (legacy):** the tail still goes into the first-frame slot of the i2v (image-to-video) template. Cast references are ignored, because Wan has no reference system.
- **Junction trimming (`_assemble_chain`):** when any segment has a trim, a filter pass drops the leading frames that replay the overlap, and re-stamps the timestamps by frame index (fps from the registry entry, typically 24):
  ```bash
  [i:v]trim=start_frame=17,setpts=PTS-STARTPTS,setpts=N/(24*TB),format=yuv420p[v{i}]
  ```
  After the trim, the previous segment's frames flow straight into the first newly generated frame. The output is re-encoded with libx264 at crf 15 (preset medium).
- **Audio junction fades:** when every segment carries audio, each segment's audio gets a short fade at both ends (`fade_s = 0.08`):
  ```bash
  [i:a]atrim=start={17/fps},asetpts=PTS-STARTPTS,areverse,afade=t=in:d=0.08,areverse,afade=t=in:d=0.08[a{i}]
  ```
  This removes clicks at the junctions without shortening the audio or drifting the A/V sync, as a crossfade would. The assembly first checks every segment for an audio stream (`ffmpeg -i`) and binds audio only when all of them have one. The LTX-2.5 workflows decode their sound into the video (`LTXVAudioVAEDecode` into `CreateVideo`, in `ltx25_msr_dfr_s6_cq`, `ltx25_t2v_dfr_cq` and the `_chain` template), so by the code an LTX-2.5 segment chain keeps its sound and gets these fades. A segment chain rendered this way has not been checked.
- **H3 and Wan chains (no trims):** when every segment carries audio, the video streams are joined with the concat demuxer as a stream copy (bit-exact). The audio is rebuilt once with the junction fades and muxed on. If some segment has no audio, the filter pass re-encodes the video (crf 15) without audio. If assembly fails, a plain stream-copy concat is tried. If that also fails, the segments are returned one by one.

### 4.4 Continuity for LTX-2.5 and MiniMax H3 movie shots (`tool/orchestrator.py`, `tool/movie_takes.py`)
LTX-2.5 and H3 project shots do not use `start_image_for`. Reference images drive them instead (`ltx_refs.resolve_reference_set`).

#### 4.4.1 Continuous takes
A `CONTINUE` shot carries the camera on from the shot before it, so a run of them is one camera take. Some shot models render seamless chains (`video_models.seamless_kind`: the LTX-2.5 quality tier `ltx25_s6_cq` and every H3 tier, marked ⛓ in the pickers). For those, the orchestrator renders the run as one seamless chain (section 4.3.1) instead of one clip per shot:
- **Which shots (`movie_takes.plan_take`):** the shot the queue picks, plus every following shot that meets all of these:
  - it continues the one before it (`continuity` `CONTINUE`, `prev_shot` pointing at it);
  - it stays in the same scene and location;
  - it is still `pending`/`queued`;
  - it is not a QA retake (`qa_stage` 0) or forced text-to-video;
  - it renders on the same model.

  A fork (two shots continuing one) ends the take. A take holds at most `movie.take_max_shots` shots (default 4: 481 frames, 20 s, at the LTX gate's link length; the 4-link H3 1080p gate took about 2 h 40 min). `CUT` and `MATCH_ACTION` shots (an edit, a new camera set-up) never join a take.
- **One cast for the take:** the references are resolved once for the whole run (`movie_takes.take_shot`: every shot's characters, the first power, the head shot's location). They are in slot order, which is also the order the chain numbers them in. Each character reference carries its bible name and look (body plus the shot's outfit), and that becomes the chain's cast line in every link prompt. When the take's first shot continues a shot that is already rendered, the take opens on the previous shot's tail frame (LTX: `pic1`; H3: the frame-0 anchor).
- **One prompt per shot:** each link gets its shot's own prompt and family recipe. For LTX that is the motion-first recipe and reference legend. For H3 it is the Ref2VA sections, where links after the first say the shot carries on instead of opening on `<Picture 1>`. The chain samples with its own recipes (H3: the tier's registry schedule), never the movie tier's step count.
- **Render:** the take renders through `video_studio.start_chain` with a stable chain id (`movie_takes.take_id`: project, model, shot ids), under one broker lease. The orchestrator waits for the delivery take. Whole-take steps (H3 refine and decode, LTX audio and take decode) get the model's clip time budget once per link (`StudioRunner.step_timeout`).
- **Cut back into shots (`movie_takes.split_take`):** the delivered take is cut at its seams (the first frame of each later link's new content) into `output/videostudio/take_<id>_<n>_<shot>.mp4`. The cut is frame-exact (one x264 crf 14 pass, BT.709), the audio is cut at the same instants, and every frame count is checked. From there each shot goes on alone: its own render record, tail frame, post-processing and QA, and provenance (`take`: id, index, shots, engine, delivery video, seam QA). For 121-frame shots, LTX shots of a take run 5.0 s (link 1: 5.04 s). H3 shots run 5.9 s (link 1) and 6.4 s (later links), because H3 windows land on its audio grid.
- **Failures:** a failed take goes back to the queue whole, with the same seeds. Every shot of the take uses one attempt, and the take goes to review when the tightest budget among its shots is spent.
  - The retry reuses every step the engine already rendered. Each start uploads the references again, but under names derived from their bytes (`renderer.upload_name`), so the graphs and their digests stay the same (`SEAMLESS_CHAINS_04_STUDIO_INTEGRATION.md` §7).
  - A transient failure (the server unreachable while it restarts) retries the same way without using an attempt.
  - The orchestrator waits while the chain renders. The chain's steps end by their own budgets, and an outer deadline of 8 x shots x the model's `timeout_s` only guards against a hung chain thread (`_wait_take`).
  - If a shot of a take later fails QA, it is re-rendered alone (section 4.4.2), opening on its predecessor's tail.
- **Switches:** the project's `seamless_takes` (Generate tab, default on), settings `movie.seamless_takes` (default on) and the engine itself (`video_studio.seamless_chains`; `video_studio.seamless_engine` decides for chains, takes and the pickers' ⛓ alike). If any of them is off, or the model has no such engine, every shot renders alone.

Joining the cuts again loses nothing at the seams. The 4-link LTX gate take was cut into 4 shot clips and concatenated the way the assembler does (section 7.1). That gave back all 481 frames at 39.7–42.5 dB PSNR (two encode generations, no dip at the seams). The audio came back sample-aligned (962,000 samples in both, offset 0 samples, no error peak at any seam; the AAC re-encode means it is not bit-identical). This was checked on the CPU, on a lab take.

#### 4.4.2 Shots rendered alone
- For `CONTINUE` / `MATCH_ACTION`, the previous shot's tail frame is attached with `ltx_refs.add_continuation` (`Orchestrator._continuation_tail`). That tail was extracted from its raw render right after that render finished.
  - **H3:** the tail goes into a `continuation` slot that Video Studio pins as the frame-0 anchor, so the clip opens on the previous shot's final frame.
  - **LTX:** the tail takes the last free subject slot, and the prompt's reference legend names it.
- The tail is skipped with a warning when there is no subject reference, no free LTX slot, or no tail file. A location change between the two shots forces `CUT`, and a missing tail renders the shot as a cut.

---

## 5. The QA director and face checks (`tool/qa_director.py`, `tool/face_qa.py`)

Rendered clips are checked before they count as `passed`. `QADirector` (`tool/qa_director.py`) checks project shots, and `FaceQA` (`tool/face_qa.py`) checks legacy shots that have no project. With `automation.auto_post: false`, shots are marked `passed` without QA.

```
                           [Rendered Shot MP4]
                                    │
                                    ▼
       ┌────────────────────────────────────────────────────────┐
       │                 CPU-only Face Model                    │
       │                                                        │
       │   InsightFace buffalo_l on CPU (CPUExecutionProvider)  │
       │   ctx_id = -1  │  no GPU allocation                    │
       └────────────────────────────┬───────────────────────────┘
                                    │
       ┌────────────────────────────┼───────────────────────────┐
       ▼                            ▼                           ▼
┌───────────────┐           ┌───────────────┐           ┌───────────────┐
│ Face Identity │           │ Semantic CLIP │           │ Farneback Flow│
│  Main:  0.75  │           │  Alignment    │           │  Motion Gate  │
│  Supp:  0.65  │           │  Threshold:   │           │  [0.00005,6.0]│
│  Recur: 0.55  │           │  >= 0.28      │           │  Freeze < 0.02│
│               │           │               │           │  (advisory)   │
└───────┬───────┘           └───────┬───────┘           └───────┬───────┘
        │                           │                           │
        └───────────────────────────┼───────────────────────────┘
                                    │
                         [Pass / Fail Evaluation]
                                    │
                                    ▼ (On Failure, post-processing on)
       ┌────────────────────────────────────────────────────────┐
       │                 5-Stage Retry Ladder                   │
       │                                                        │
       │  Stage 0: reseed (next deterministic retry seed)       │
       │  Stage 1: simplify_prompt (first 8 words of action)    │
       │  Stage 2: adjust_lora (halve VFX/Camera/Motion LoRAs)  │
       │  Stage 3: regen_keyframe (new start-keyframe candidate)│
       │  Stage 4: review (not acted on; re-queued)             │
       │                                                        │
       │  Review when attempts >= qa.max_retries[tier]          │
       │  (hero 3, standard 2, volume 1)                        │
       └────────────────────────────────────────────────────────┘
```

### 5.1 Face identity check on the CPU
- **Model:** InsightFace `buffalo_l` (`detection` and `recognition` modules).
- **Kept off the GPU:** it loads with `providers=["CPUExecutionProvider"]` and `prepare(ctx_id=-1)`, so face recognition runs on the CPU and does not allocate GPU memory next to the render.
- **Metric:** cosine similarity between the reference embeddings (the first detected face of each reference image) and every face detected in the video's last 5 frames:
  $$\text{Sim} = \max_{i, j} \left( \hat{e}_{\text{frame}, i} \cdot \hat{e}_{\text{ref}, j} \right)$$
  When no face is detected in those frames, the score is 0.0 (a failure). When no usable reference exists, the metric is skipped.
- **References:**
  - Project shots: face QA runs only for characters of tier main / supporting / recurring. It runs only when one of them has a face LoRA (`models/loras/<id>_face.safetensors`) and the shot has an approved keyframe (`keyframe_path`, or the `approved` entry in `shots/keyframes/<shot_id>/candidates.json`). That keyframe is the reference.
  - Legacy shots: `FaceQA` looks in `ReferenceImages/<id>/`, `roster/<id>/`, `roster/keyframes/<id>/` and `roster/datasets/face/<id>/`.
- **Pass thresholds:** settings `qa.face_sim` by tier, using the tier of the shot's first QA-eligible character (main / supporting / recurring). A tier missing from settings uses 0.55. Shipped values:
  - `main` tier: cosine similarity $\ge 0.75$.
  - `supporting` tier: cosine similarity $\ge 0.65$.
  - `recurring` tier: cosine similarity $\ge 0.55$.
  - `crowd` tier: exempt from the face check.

### 5.2 Prompt match (CLIP)
- The check computes the cosine similarity between the middle frame's image embedding and the prompt's text embedding, with `openai/clip-vit-base-patch32`. The model loads from the local Hugging Face cache only (`local_files_only=True`). When it is not cached, the metric is skipped.
- **Threshold:** $\text{Cosine} \ge 0.28$ (`qa.clip_score_min`). A clip below it fails QA (`clip_fail`).

### 5.3 Motion check (Farneback optical flow)
Diffusion models sometimes freeze into a still image, or blow up into chaotic high-frequency noise:
- The motion score is the mean dense Farneback optical-flow magnitude (`cv2.calcOpticalFlowFarneback`) over all consecutive frame pairs. If the computation fails, the score is a neutral 0.5.
- **Allowed motion range** (`qa.motion_score_range`, the pass/fail bound): $0.00005 \le \text{Motion} \le 6.0$.
- **Freeze flag:** clips with an average flow $< 0.02$ are flagged `frozen` in the QA result and its detail text. The flag is advice only. Dialogue and establishing shots that are static on purpose sit below 0.02, so only the configured range decides pass or fail. To reject near-static clips, raise the range's lower bound.

`QADirector.evaluate()` passes a shot when none of the computed metrics fails (face, motion range, CLIP). A skipped metric never fails a shot.

### 5.4 The 5-stage retry ladder and tier budgets
What happens after a failed QA depends on the project's post-processing switch (`projects.post_processing`, default off):
- **Post-processing off (the default):** QA runs on the raw render (`_finalize_raw`). A failure sends the shot to `review` with the QA result in `qa_notes`. There is no re-render.
- **Post-processing on:** each QA failure increments `attempts`. When `attempts` reaches `qa.max_retries[tier]` (shipped: hero 3, standard 2, volume 1), the shot goes to `review`. Otherwise `QADirector.next_retry_action()` picks the next step from the shot's current `qa_stage`, and the shot is re-queued with `qa_stage + 1` and the next retry seed (`base + attempts × 10000`). Render failures (exceptions) increment the same counter, except connection and timeout errors. Those retry without using it (up to 60 times).

The steps are:
1. **`reseed` (stage 0):** re-render with the next retry seed.
2. **`simplify_prompt` (stage 1):** the stored prompt becomes `primary action: <first 8 words of primary_action>`, and the following renders use it (the orchestrator prefers the stored prompt once `qa_stage > 0`).
3. **`adjust_lora` (stage 2):** from the next render on (`qa_stage >= 3`), effect and camera LoRAs (categories VFX / Camera / Motion) run at half strength (for example $0.7 \to 0.35$). This is meant to get rid of tensor blowout and edge artifacts (`lora_video.soften_effect_loras`). Character, environment, quality and acceleration LoRAs stay unchanged. Wan project renders use the softened LoRA chain. LTX-2.5 / H3 renders are started through Video Studio without it.
4. **`regen_keyframe` (stage 3):** generates one new keyframe candidate (`KeyframeGen.generate_candidates(..., count=1)`). Legacy Wan 2.2 shots can pick it up as their start image (section 4.2); LTX-2.5 and H3 shots do not use it. The candidate renders with the Images-tab model in `keyframes.model` (default RealVisXL; `wan` keeps the old Wan 2.2 graphs).
5. **`review` (stage 4+):** the orchestrator does not act on this step and just re-queues the shot. The `review` status only comes from the attempts budget.

Because the budget is checked first, the shipped budgets never reach the later steps:
- hero shots get reseed and simplify_prompt;
- standard shots get reseed only;
- volume shots go to review on the first failure.

The first render with softened LoRAs needs `max_retries >= 4`, and `regen_keyframe` needs `>= 5`. Other shots keep rendering while a shot waits in `review`. Legacy shots without a project only retry with the next seed until the budget is used up.

---

## 6. Post-processing alongside rendering (`tool/orchestrator.py`, `tool/post.py`)

Rendering and post-processing (frame work, encode, QA) have different bottlenecks. So the orchestrator runs post + QA on a separate thread while the render loop goes on.

```
Time ──►
GPU Pipeline:     [ Render Shot N ]────────►[ Render Shot N+1 ]──────►[ Render Shot N+2 ]
                         │                         │
                         ▼ (early tail extract)    ▼ (early tail extract)
Queue Handoff:    _post_queue.put(N)        _post_queue.put(N+1)
                         │                         │
Post Worker:             └────────►[ Post & QA Shot N ]──────►[ Post & QA Shot N+1 ]
                                   (post chain if enabled,    (post chain if enabled,
                                    QA eval)                   QA eval)
```

### 6.1 The post queue (`_post_worker`)
- When shot N finishes rendering, `tool/orchestrator.py` at once extracts its tail frame from the raw render through `continuity.extract_tail()`.
- The tail frame is written to disk and recorded in `studio.db` as `tail_frame` (status `rendered`), so shot N+1 can use it for `CONTINUE` or `MATCH_ACTION` right away.
- Shot N is put on the queue with `self._post_queue.put((shot, sjson, video, prompt))`.
- The render loop then picks the next shot.
- The background thread `_post_worker` takes shot N from `_post_queue`, marks it `post`, and runs the post chain (when the project turned it on) followed by QA (section 5).
- The CPU stages (ffmpeg frame work, DHCC, temporal pass, LUT grade, encode, QA) overlap the next render. The ComfyUI stages (RIFE, RealESRGAN, face-fix) are GPU jobs on the 8188 farm. Like every local prompt, they wait for the GPU broker (`docs/ARCHITECTURE.md`, section 2), so they never share the card with a running render.

### 6.2 Post chain order (`PostPipeline.process`)
The chain runs only for projects with post-processing switched on. Otherwise the raw render is the deliverable. The steps, in order:
0. **Remote upscale (optional):** when the project names a post worker (`post_worker`), RIFE (if wanted) and RealESRGAN x4 run on that worker PC, and the local RIFE and upscale steps are skipped. If the worker is unavailable, the local chain runs instead.
1. **Extract** all frames (`-fps_mode passthrough`). The output fps is probed from the clip itself (fallback `post.fps_generate`, 24), so a clip is never re-timed.
2. **RIFE 2x** (frame interpolation, `post_rife` workflow): only when `post.rife` is on and `post.fps_target` (48) is above the probed fps. The output fps doubles. If RIFE fails, the clip keeps its native fps.
3. **Upscale** per tier (`tiers.upscale`, shipped `esrgan`): `esrgan` / `lanczos` run the `post_esrgan` workflow, and `seedvr2` falls back to it on failure.
4. **V2V refine:** `post.v2v_refine` (off by default), hero and standard tiers only. Its graph (`wan_v2v`) runs the legacy Wan 2.2 TI2V-5B model, so it needs the Wan files.
5. **Face-fix:** `post.face_fix` (on by default), for non-volume close-ups (`extreme_close_up` / `close_up` / `medium_close_up`) with a main or supporting character.
   - It sends every `post.face_fix_every_n`-th frame (default 3) through the `face_fix` workflow, with the character's face LoRA when `models/loras/<id>_face.safetensors` exists. That graph also runs the legacy Wan 2.2 TI2V-5B model and needs its files.
   - The fix is applied as a feathered difference mask. In-between frames get a linearly interpolated delta from their neighbouring fixed frames, so they keep their own motion.
6. **DHCC:** `post.dhcc` (off by default) matches each frame's histogram to the average of the first 5 frames.
7. **Temporal pass:** `post.temporal_pass` (on by default) blends each frame 0.6 / 0.4 with the previous one where the optical-flow magnitude is below 0.3.
8. **LUT grade:** `post.lut` applied per frame with ffmpeg `lut3d`. If grading fails, the shot ships ungraded with a warning.
9. **Encode:** libx264 veryfast, crf 16, yuv420p at the output fps. The source render's audio track is muxed back (AAC 192k), so H3 sound survives the frame round-trip.
10. **Draft:** `output/drafts/<scene>_<shot_id>.mp4` with `h264_nvenc` (NVIDIA's hardware video encoder; `-preset p7 -tune hq -rc vbr -cq 19 -b:v 0 -rc-lookahead 20 -spatial-aq 1 -profile:v high`).
    - **NVENC fallback:** if NVENC fails (non-zero exit, or an output under 1 KB), the draft is re-encoded with libx264 veryfast crf 18, and NVENC is skipped for the rest of the process.
    - The draft path is stored as the shot's `final_path`, and the continuity tail is re-extracted from it.

### 6.3 Thermal gate
Long render sessions heat the GPU, so the render loop has a temperature gate:
- Before it picks each shot, the orchestrator reads the GPU temperatures with `nvidia-smi --query-gpu=index,temperature.gpu`. If `nvidia-smi` fails, the gate is skipped.
- **Safety ceiling:** if any GPU is above $85^\circ\text{C}$, the loop pauses before the next shot. A render that is already running is not interrupted.
- While paused, it checks again every 30 seconds and resumes once every GPU is at or below $75^\circ\text{C}$.

---

## 7. Film assembly (`tool/assembler.py`)

`tool/assembler.py` builds the film from the project's passed shots. It runs when the Director reaches the `assemble` stage, or when the Film tab's **Assemble** runs (`POST /api/assembly/run` with the project id; see `docs/ARCHITECTURE.md`, section 4.1).

### 7.1 Plain cuts with the concat demuxer (no crossfades)
Shots are joined with plain cuts. There are no dissolves between shots:
- For each scene, the assembler writes a concat list with absolute paths: `temp/<project_id>__<scene>.txt` for a project, or `temp/<scene>.txt` for the legacy glob of `output/drafts/<scene>_*.mp4` (only when no project is given).
- For a project, the list holds the project's passed clips in story order, following the `prev_shot` chain (`assembler.project_clips`; `final_path` when post-processing produced one, else the raw `video_path`). So the cuts of a continuous take (section 4.4.1) join back into the take:
  ```text
  file '<workspace_root>/output/videostudio/<raw LTX/H3 render>.mp4'
  file '<workspace_root>/output/drafts/<scene>_<shot_id>.mp4'
  ```
- FFmpeg reads the list with the concat demuxer and re-encodes each scene to `output/scenes/<project_id>__<scene>.mp4`:
  ```bash
  ffmpeg -y -f concat -safe 0 -i list.txt -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p out.mp4
  ```
- When only some clips have audio, the silent ones first get a silent AAC track (`anullsrc`), so the concat keeps one audio stream.

### 7.2 Loudness normalization (EBU R128)
When any clip has an audio stream, the scene encode applies single-pass `loudnorm`:
```bash
-af loudnorm=I=<post.audio.loudness_target_lufs>:TP=-1.5:LRA=11
```
- Integrated loudness target: settings `post.audio.loudness_target_lufs`. `settings.example.yaml` ships −14 LUFS. If the key is missing, the code default is −16.
- True peak ceiling: `TP = -1.5 dBTP`.
- Loudness range: `LRA = 11 LU`.

### 7.3 Two export files
The scene files are concatenated again into two film files, named after the sanitized project title (letters, digits, space, `_`, `-`, `.`; at most 80 characters):
1. **Draft export (`output/films/<title>_draft.mp4`):** NVENC. It falls back to `libx264 veryfast -crf 18` when the NVENC encode fails or writes an output under 1 KB:
   ```bash
   ffmpeg -y -f concat -safe 0 -i list.txt -c:v h264_nvenc -preset p7 -tune hq -rc vbr -cq 19 -b:v 0 -pix_fmt yuv420p -c:a aac -b:a 192k draft.mp4
   ```
2. **Master export (`output/films/<title>_master.mp4`):** software encoding:
   ```bash
   ffmpeg -y -f concat -safe 0 -i list.txt -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k master.mp4
   ```
