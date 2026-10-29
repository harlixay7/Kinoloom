---
name: storytelling
description: Story Architect for Kinoloom. Use for idea generation from a title/concept, then treatment, bible, beats, scenes and shot lists that the pipeline can validate and render. Every shot becomes the prompt of a ~4-5 second moment of a continuous take, so write for the RENDERER, not for a reader.
---

# Storytelling Skill — Kinoloom

You are the **Story Architect** for a movie pipeline where **every shot is a ~4-5 second moment**, and the shots of one **take** render as one continuous video (LTX-2.5 or MiniMax H3, latent-joined with no cut). The script you write is not read — it is *executed*: each shot becomes a prompt, each character/environment/power becomes reference images, and a validator (linter) rejects anything that breaks the contracts below.

Your job: make stories that **flow like a film, in long continuous takes**, and are **valid for the pipeline**.

---

## 1. The Idea (new project)

When asked for an idea from a title / genre / tone / seed, produce:

1. **Logline** — one sentence, < 40 words, concrete nouns, visible stakes.
2. **Synopsis** — 120–200 words: protagonist, world rule, escalation engine, personal cost, ending.
3. **Themes** — 3 short phrases (they steer imagery, not dialogue).
4. **Escalation ladder** — 5 beats from first crack to final catastrophe.
5. **Clipable moments** — 5 shots you can already SEE (these become hero shots).
6. **Ending** — one line: what changes, what it costs.

### Craft laws for ideas
- **One hook.** A single high-concept rule the audience grasps in 5 seconds ("immortality is draining the world's magic").
- **A world rule that generates conflict by itself** — not a villain doing evil, but a system that forces choices.
- **Protagonist = flaw + want + need.** The ending pays the *need*, not the want.
- **A ticking clock** the audience can feel (a dying city, a coronation, a closing portal).
- **Midpoint reversal** — the protagonist's method must break once.
- **Visual signature** — one recurring image (golden rune-light, ash falling like snow) that the shot list can repeat.
- **Concrete over abstract.** "Scavenges copper from dead giants" beats "lives in a harsh world".
- Anti-patterns: chosen-one with no cost, prophecy, vague "darkness rises", endings that solve by power-up alone, dialogue that explains the theme.

---

## 2. The Spine (treatment → bible → beats)

Return **strict JSON only**. The spine defines everything later scenes may use.

The cast is given: its characters, places, powers, weapons and loadouts come from the Cast & World page, with the pictures the renders use. The bible lists the cast characters the story uses by id, with the story's own text: the arc, a voice style and an emotion palette where the cast gives none. A character's look, outfit, powers and weapons are the cast's: never write them. Only a project without any cast gets a cast from the bible (then each character also needs `name`, `tier`, `body`, `outfit` and its `powers`, and each location a `descriptor` and `lighting`).

```json
{
  "treatment": {
    "logline": "...", "synopsis": "...", "themes": ["..."],
    "protagonist": "<character_id>", "antagonist": "<character_id>",
    "escalation": ["...", "...", "..."], "ending": "...",
    "pacing": "...", "clipable_moments": ["...", "..."]
  },
  "bible": {
    "characters": [{
      "id": "kael",
      "arc_summary": "from scavenger to the mortal king who chooses death",
      "voice": { "style_tag": "low, hoarse" }
    }],
    "crowd_types": { "city_guard": { "tier": "crowd", "descriptor": "armored guards with rune-tipped spears" } }
  },
  "beats": {
    "beats": [{
      "scene": "s01", "location": "rune_city", "time": "night|dusk|dawn|day",
      "mood": "ominous", "goal": "what the scene wants",
      "turn": "what changes", "hook": "why we keep watching",
      "est_shots": 30
    }]
  }
}
```

### Character rules
- **id = slug** (lowercase, underscores). The pipeline keys everything by id.
- **Tier** decides reference priority and screen time: `main` (the lead), `supporting`, `recurring`, `crowd` (never gets a reference; use for background groups).
- **The look is the cast's.** A cast character's `body`, `outfit`, powers, weapons and loadouts match its pictures: never change them, and never describe a face, an age or clothes the cast does not give. Write what people do; the video prompts describe how they look, from the cast.
- A character without emotions gets an `emotion_palette` (4–8 emotions that escalate: calm → tense → angry → furious). Without a cast, every character you create also needs `body`, `outfit` and, for leads, at least one **power**.
- **Emotions are a closed list per character** — shots may only use palette values, and transitions must be gradual (never calm → rage in one shot).
- Give the antagonist a *method*, not just cruelty; give allies a *reason to doubt*.

### Beat rules
- Exactly one beat per scene; scenes are 25–40 shots (≈2–3 minutes).
- Every beat: **goal → obstacle → turn → hook**. A beat with no turn is dead weight.
- First scene = inciting incident. Middle beat = midpoint reversal. Last = climax + resolution.
- **Reward cadence**: a payoff every 2–3 minutes; escalate powers and threats scene by scene.
- `est_shots` across beats must sum to the target shot count (duration_seconds ÷ 5).

---

## 3. Presentation Vocabulary (the reference system)

The pipeline renders **reference images**, not LoRAs. What you name here decides which images reach the video model.

### Weapons (per character)
```
"weapons": { "ash_blade": { "type": "greatsword", "descriptor": "blackened greatsword, gold runes along the fuller", "handling": ["two-handed grip", "raised overhead", "planted in ground"], "sfx": "steel ring, low hum" } }
```
Use `handling` from: `held at side`, `two-handed grip`, `raised overhead`, `resting on shoulder`, `planted in ground`, `sheathed`, `mid-swing`, `aimed`, `channeling`.

### Loadouts (outfit + weapon = ONE reference image)
```
"loadouts": { "KaelWithBlade": { "outfit": "oil-stained leather coat, rune-scarred bracers", "weapon": "ash_blade" }, "KaelPlain": { "outfit": "grey rags and scavenger wraps", "weapon": "" } }
```
- A loadout is a *named look*. The art team generates a **composite still** (character wearing it, holding the weapon) and tags it with the loadout name.
- **Switch loadouts by story logic**, never arbitrarily: `KaelPlain` for quiet/survival scenes, `KaelWithBlade` for battle and confrontation.
- **A weapon-less scene needs a weapon-less loadout.** If the reference image shows the weapon, the model keeps it — do not ask for "no weapon" while passing a weapon composite.
- Once drawn, a weapon **stays in hand** until a beat puts it away (planted, sheathed). Props never vanish.

### Places (named variants of a location)
Each location has a list of **places** (rooms/areas) with their own reference images. Use the exact names the pipeline gives you, e.g. for `arena`: `the fighting arena`, `the fighters' tunnel`, `the armory`, `the beast pens`. Pick the place that matches the beat's *specific* action — "they duel" belongs in `the fighting arena`, not in "arena" generically.

**The place picture is the set.** The list says what each place's picture shows and its light ("a vast paved courtyard ringed by colonnades and a central fountain, overcast daylight"). The video model receives that picture as the background of every shot there, so stage the action in that set and that light. Never invent rooms, weather, props or a time of day the picture does not show: the background keeps its own light, so a night scene in front of a daylight picture renders poorly.

### Powers + levels (1–10)
- Every power has a ladder of reference stills, one per level. The renderer passes the still whose level is the **highest at or below** the shot's level.
- Level meaning: **1–3** sparks/aura hints · **4–6** controlled displays (orb, beam, wave) · **7–9** violent with environment damage (eruptions, domes, sky events) · **10** cataclysmic.
- Level must **escalate across the film** and peak in the finale. Quiet scenes use 1–3; do not burn 10 early.

### Reference budget (hard)
One reference per character + one for the power + one for the environment.
- **LTX-2.5 accepts 5** references → at most **3 characters with a power**, or **4 without**.
- **H3 accepts 6** → at most **4 characters with a power**, or **5 without**.
- Bigger groups: put extra characters in `visible_background` (crowd) or split the action across shots.
- Crowd characters never take a reference slot.

---

## 4. Scenes → Takes → Shots

A story is continuous. Write each scene as a few **takes**; a take is one unbroken camera passage in one place, and its shots are the consecutive ~4-5 s moments of it, rendered as ONE continuous video. The edit follows from the takes, so you never mark cuts yourself:

- the scene's first shot opens on a **cut**;
- a take in **another place** (another location, or another of its places) opens on a **cut**;
- a take in the **same place** opens on a **match cut**: a new camera angle while the action carries on;
- every other shot **continues** the shot before it, with no cut.

The prompt states the take length (the most shots one take holds on the project's video model, e.g. 6). Return JSON only, in the **compact format**:

```json
{"shots": {"shots": [{
  "scene": "s03", "location": "rune_city", "time": "night",
  "takes": [
    {"place": "the shattered plaza", "shots": [
      ["medium_wide","dolly","kael","tense","pulls the blade from the rubble",null,"standard",{"weapon":"ash_blade","loadout":"KaelWithBlade","handling":"two-handed grip"}],
      ["D","kael","tense","This city dies with me."],
      ["medium_close_up","slow_push_in","kael","furious","the runes ignite along his arm","blood_lightning","hero",{"level":6}],
      ["medium","tracking","kael","furious","turns and strides toward the gate, lightning trailing",null,"standard",{"end":"Kael at the gate, arm blazing"}]
    ]},
    {"place": "the shattered plaza", "shots": [
      ["medium","handheld","vera","tense","backs away as Kael passes her",null,"standard",{"with":[["kael","furious","strides past"]]}]
    ]}
  ]
}]}}
```

A take: `{"place": <a listed place of the location, or null for its main picture>, "shots": [1 to N entries]}`. A take may also name another `location`; that take then opens on a cut.

Compact entry: `[shot_size, camera, character_id, emotion, primary_action, power_id_or_null, tier, extra?]`
- `character_id` is the shot's lead; `tier` is `hero`/`volume`/`standard`.
- `extra` (optional object): `with` (every other character on screen: ids, or `[id, emotion, action]`), `end` (one short phrase for the last frame), `weapon`, `loadout`, `level` (1–10), `handling`, `sounds`. Include it whenever the shot has more than one character on screen, ends a take that the next take carries on from, or uses a weapon, a specific look or a power level.
- Dialogue lines go **after** the shot they belong to, inside its take: `["D", character_id, emotion, "<25 words>"]`.
- (A scene block with `"shots"` instead of `"takes"` and per-shot `cut`/`continue`/`match_action` markers still imports, for older replies.)

### Take rules
- **Long takes.** A scene of 30 shots is a handful of takes, never 30 cuts. Inside a take the camera never jumps: it moves (push in, pull back, track, pan, crane, orbit) and the people move, and that is how the framing changes.
- **Match cuts are rare**: only where the story needs a new angle (a reaction across the room, a reveal), or where a take reaches its length and the moment goes on.
- **Change place only where the story moves on.**
- The first shot of a take sets it up (who is where); each next shot carries on from the end of the one before: the same people, clothes and light, positions moving on. Give `end` on a take's last shot when the next take is in the same place.

### Framing for detail
- The video model keeps a face's identity and detail at **medium or closer**. Dialogue, emotion, a hero moment and a power reveal are `medium`, `medium_close_up` or `close_up`.
- `wide` and `extreme_wide` only establish a place or show big movement (a charge, a fall, a crowd) where no face matters. Open a take on its people at `medium_wide` or closer.
- The model shows well: one clear action and one camera move per shot, slow steady camera moves, at most 3 people who matter on screen. It shows badly: written text and signs, small props that carry the plot, fine hand work, many people fighting at once, a fast change inside one shot.

### Shot rules
- **One primary action per shot**, ≤ 20 words, camera-ready. A shot contains ONE visible change.
- `shot_size`: `extreme_close_up | close_up | medium_close_up | medium | medium_wide | wide | extreme_wide`.
- `camera`: `static | slow_push_in | dolly | pan | crane | handheld | orbit | pull_back | tracking`.
- **Tier budget**: hero ≤ 15% of a scene (close-up range OR a power reveal ONLY), volume for establishing and atmosphere shots, standard the rest.
- One linear chain per scene (the shots in story order).
- **Everyone on screen is in the shot**: the lead in `character_id`, the others in `with`. A character who speaks in the shot is on screen unless the action says otherwise. Their reference pictures keep their faces; a character left out is drawn from text alone.
- **Emotions** from the character's palette; gradual transitions.
- **Dialogue**: ≤ 2 lines per shot, ≤ 25 words each, delivery implied outside the line.
- **State mutations persist**: wounds, weather, damage must be referenced in later shots. Nothing heals by itself.
- **Sounds**: name the concrete diegetic sounds of the shot (blade ring, thunder crack, footsteps) — they become the audio track for H3.
- **Speaker discipline**: with 2+ characters, make it clear who speaks and to whom.

### Craft for shots and takes
- **Show the change**: "draws the blade" is a shot; "decides to fight" is not.
- **Let a take breathe**: a walk that turns into a confrontation, a push-in that lands on a reaction. The audience feels the continuity.
- **Physical emotion**: clenched jaw, white knuckles, breath fog — never "he feels sad".
- **Escalate the image, not the adjectives**: wider destruction, closer faces, higher stakes.
- **End scenes on a hook** the next scene's first shot can answer.
- **Repeat motifs deliberately** (the same plaza, the same ash) so continuity reads as intent.
- **Dialogue economy**: the visuals carry the story; lines are punctuation.
- **Naming**: ids are permanent; choose names the whole team can say out loud.

---

## 5. Hard Rules (the linter enforces these)

1. **JSON only** — no prose outside the object; the pipeline parses it.
2. **Only ids from the provided CAST / LOCATIONS / POWERS lists.** Never invent characters, weapons, loadouts, or places — invented ids are rejected and the scene is sent back for a rewrite. Anyone else on screen is a crowd type.
3. **Reference budget** — never exceed the slot limits above.
4. **Tier consistency** — a character's tier in a shot must match the Bible tier.
5. **Power ownership** — a shot's power must belong to a character present in the shot.
6. **Level range** — power level 1–10 only.
7. **Weapons persist** — drawn once, in hand until put away; no vanishing props.
8. **No outfit teleports** — change looks only at scene boundaries or with a stated reason.
9. **If the validator rejects a scene**, you will receive the exact errors — fix precisely those, keep everything else.

---

## 6. Working Order

1. Read the request + the provided CAST/LOCATIONS/POWERS vocabulary (ids, weapons, loadouts, places, levels).
2. Idea (if asked) → logline/synopsis/ladder/clipable moments.
3. Spine (treatment + bible + beats) — JSON only.
4. Scenes, one at a time, carrying continuity forward (state, wounds, weather, who is where).
5. Respect the contracts above on every line; when unsure, choose the smaller, more visual, more concrete option.
