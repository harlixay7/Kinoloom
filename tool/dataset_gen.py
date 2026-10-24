"""Dataset generator — parameter-driven character/power/environment photo
creation. Best-practice prompt templates + identity-
locked variant generation (master portrait -> I2V variants at low denoise).
Runs threaded; progress exposed via /api/training/gen_dataset/status."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from core import ROOT, log_setup, is_safe_component
from renderer import ComfyClient
from workflows import WorkflowBuilder

log = log_setup("dataset_gen")

NEG = ("blurry, deformed, distorted, warped, folded, overlapping features, "
       "low quality, soft, foggy, smear, extra person, multiple people, "
       "second face, watermark, text")

EMOTIONS = ["stoic", "cold", "angry", "calm", "grief", "focused", "stern",
            "determined", "sorrowful", "intense"]
ANGLES = ["front-facing", "three-quarter", "profile right", "profile left",
          "looking up", "looking down", "low angle", "over-shoulder"]
LIGHTING = ["moonlight rim light", "neon city glow", "golden hour",
            "studio softbox", "dramatic chiaroscuro", "backlit silhouette",
            "rain with streetlights", "firelight warm glow"]
HAIR_STYLES = ["short", "long", "curly", "buzz cut", "ponytail", "bald",
               "medium wavy", "braided"]
HAIR_COLORS = ["black", "dark brown", "brown", "blonde", "red", "gray",
               "white", "silver", "cyan-tipped"]
BUILDS = ["lean", "athletic", "stocky", "slender", "tall and broad"]
AGE_RANGES = ["young adult", "mid-30s", "40s", "older"]
SKIN_DETAILS = ["visible skin texture, pores and fine lines",
                "weathered face with character",
                "smooth skin with natural texture"]
FACE_FEATURES = ["sharp jaw", "soft jaw", "high cheekbones", "broad nose",
                 "thin lips", "full lips", "square jaw", "defined brow"]
OUTFITS = ["black tactical coat with silver pauldron", "worn leather jacket",
           "armored plates over dark cloth", "long dark trench coat",
           "hooded cloak", "military-style fatigues"]

EYE_COLORS = ["amber brown", "dark brown", "hazel", "steel blue", "ice blue",
              "emerald green", "grey-green", "pale grey"]
EYE_SHAPES = ["deep-set", "almond-shaped", "round", "hooded", "narrow",
              "wide-set"]
BROWS = ["strong straight", "thin arched", "thick dark", "bushy",
         "faint light", "neatly groomed"]
NOSES = ["straight", "aquiline", "broad", "slightly crooked", "delicate",
         "sharp prominent"]
LIPS = ["full", "thin", "medium-full", "wide", "soft full", "narrow firm"]
FACE_SHAPES = ["oval", "square", "heart-shaped", "round", "long", "angular"]
SKINS = ["fair with light freckles", "olive", "warm tan", "deep brown",
         "pale porcelain", "bronze"]
FACIAL_HAIR = ["clean-shaven", "light stubble", "full beard", "short boxed beard",
               "mustache only", "goatee"]
DISTINCTIVE = ["a thin scar across the left brow", "a small mole on the right cheek",
               "a faded burn scar on the neck", "piercing intense gaze",
               "a silver ring in the left ear", "no visible markings"]

POWER_AURAS = ["a crackling electric aura enveloping the whole body, sparks cascading off the shoulders",
               "an intense radiant corona of energy around the figure, light bleeding into the air",
               "energy tendrils and sparks radiating off the figure, static charge in the air",
               "a contained aura hugging the body, faint glow outlining the silhouette",
               "no visible aura"]
AURA_TEXT = {
    "crackling electric aura enveloping the body":
        "a crackling electric aura enveloping the whole body, sparks cascading off the shoulders",
    "radiant corona of light":
        "an intense radiant corona of energy around the figure, light bleeding into the air",
    "energy tendrils radiating off":
        "energy tendrils and sparks radiating off the figure, static charge in the air",
    "contained aura hugging the body":
        "a contained aura hugging the body, faint glow outlining the silhouette",
    "no aura": "no visible aura",
}

# short factual tags for TRAINING CAPTIONS (the generation prompt is rich;
# the caption is the compact vocabulary the LoRA actually learns)
POWER_FORM_TAGS = {
    "hands": "energy from raised hands",
    "sky": "giant sky bolt",
    "orb": "energy orb between hands",
    "whip": "lashing energy whip",
    "beam": "energy beam from hand",
    "strike": "energy strike on the ground",
}
AURA_TAGS = {
    "crackling electric aura enveloping the body": "full-body electric aura",
    "radiant corona of light": "radiant energy corona",
    "energy tendrils radiating off": "energy tendrils radiating",
    "contained aura hugging the body": "contained aura",
    "no aura": "",
}
CAMERA_TAGS = {"low-angle dramatic shot": "low angle",
               "wide shot": "wide shot",
               "extreme close-up": "extreme close-up",
               "overhead shot": "overhead shot",
               "three-quarter angle": "three-quarter angle"}
SUBJECT_TAGS = {"hands": "", "figure": "figure from behind",
                "no_person": "no person"}
POWER_INTENSITY = ["faint crackling sparks", "steady arcs of energy",
                   "violent crackling storm", "cataclysmic discharge",
                   "contained pulsing charge"]
POWER_CAMERA = ["low-angle dramatic shot", "wide shot", "extreme close-up",
                "overhead shot", "three-quarter angle"]

ENV_CAMERA = ["wide establishing shot", "aerial shot", "low-angle shot",
              "dutch angle", "ground-level shot", "ultra-wide shot"]
ENV_WEATHER = ["clear sky", "rolling fog", "heavy rain", "snowfall",
               "dust storm", "hazy atmosphere"]
ENV_FOREGROUND = ["drifting embers", "low creeping fog", "rain streaks",
                  "dust motes in the light", "snow flurries",
                  "silhouetted debris", "windblown grass"]

VFX_LANG = ("glowing volumetric energy, thin bright lightning core with "
            "bloom halo, god rays, particle sparks and flying embers, "
            "electric discharge lighting the scene, cinematic visual "
            "effects, professional CGI VFX shot, film still")

POWER_FORMS = {
    "hands": "two raised hands, palms facing forward, crackling arcs of energy branching from the fingertips, corona discharge at the palms, the energy wrapping around the forearms, extreme close-up of the hands filling the frame",
    "sky": "a giant branching bolt striking down from storm clouds onto a distant figure, visible structure, volumetric glow, the bolt dominating the sky, wide atmospheric shot",
    "orb": "a glowing orb of energy crackling between two raised hands, palms cupped around the orb, electric tendrils arcing off it, storm above, close-up on the hands and orb",
    "whip": "a whip of crackling energy lashing from an outstretched hand, sharp branching bolts, sparks trailing through the air, dynamic action shot",
    "beam": "a focused beam of energy erupting from a hand raised toward the sky, structured stream, intense glow, energy arcing from the palm",
    "strike": "energy striking the ground in an electric burst, branching discharge, sparks flying, a lone figure seen from behind with arms raised toward the blast",
}

def power_prompt(p: dict) -> str:
    """Generation prompt for power images. Fixed slot order so the model
    never gets confused: subject/form -> energy identity -> LIGHT
    INTERACTION (the glow casts light on the scene — this is what makes
    it look like real VFX) -> environment (setting/time/weather/ambient/
    atmosphere) -> camera -> style. Only slot VALUES change per image."""
    form = POWER_FORMS.get(p.get("form", "hands"))
    subject = p.get("subject", "")
    if subject == "figure":
        form = POWER_FORMS.get(p.get("form", "sky"))
        if p.get("form") not in ("sky", "strike"):
            form = (f"a lone figure seen from behind, arms raised toward the "
                    f"energy, {form}")
    elif subject == "no_person":
        plain = {
            "hands": "crackling arcs of pure energy floating in mid-air, corona discharge, sharp bolt structure",
            "sky": POWER_FORMS["sky"],
            "orb": "a glowing orb of energy crackling in mid-air, sharp electric structure, storm above",
            "whip": "a lashing whip of crackling energy, sharp branching bolts, sparks",
            "beam": "a focused beam of energy erupting upward, structured stream, intense glow",
            "strike": "energy striking the ground in an electric burst, branching discharge, sparks flying",
        }
        form = plain.get(p.get("form", "hands"))
    color = p.get("color", "cyan")
    aura = p.get("aura", POWER_AURAS[0])
    aura_txt = "" if aura == "no visible aura" else f", {aura}"
    env = p.get("env", "dark city street")
    scene = p.get("scene", POWER_SCENES[0])
    return (
        f"cinematic photorealistic still of {p.get('power', 'lightning')} "
        f"power: {form}{aura_txt}, {p.get('intensity', POWER_INTENSITY[2])}, "
        f"{color} {p.get('color_desc', 'electric')} glow, the glow casting "
        f"{color} light over the {env}, in a {env}, {scene}, "
        f"{p.get('camera', POWER_CAMERA[0])}, {VFX_LANG}, sharp details, "
        f"dramatic, film still"
    )


def power_caption(p: dict, m: dict) -> str:
    """COMPACT factual caption for TRAINING (15-25 tokens, same vocabulary
    as the generation prompt). This is what the LoRA actually learns —
    keep it short, comma-separated, no lens/style fluff."""
    parts = [f"{p.get('power', 'lightning')} power",
             POWER_FORM_TAGS.get(m.get("form", "hands"), m.get("form", "")),
             SUBJECT_TAGS.get(m.get("subject", ""), ""),
             AURA_TAGS.get(m.get("aura", ""), ""),
             f"{m.get('color', p.get('color', 'cyan'))} glow",
             m.get("intensity", p.get("intensity", "")),
             m.get("env", ""), m.get("scene", ""),
             CAMERA_TAGS.get(m.get("camera", ""), "")]
    return ", ".join(x for x in parts if x)

POWER_ENVS = ["dark city street", "rooftop at night", "stormy sky over ruins",
              "rain-soaked plaza", "dark forest clearing", "industrial yard",
              "ocean cliff at night", "empty highway"]
POWER_SCENES = [
    "rainy night with street lamp glow and rain haze",
    "storm twilight lit by distant lightning flashes",
    "foggy night with low creeping fog",
    "clear night under bright moonlight",
    "dusk with static-charged air and howling wind",
    "heavy storm clouds with crackling charged air",
    "deep darkness with barely any light",
    "smoke-filled night with drifting smoke",
    "overcast dawn with grey diffused light",
    "electric storm night with flashing sky",
]

# Each place: {"v": full prompt descriptor, "l": short UI label,
#              "locs": 6 specific locations INSIDE that place}
ENV_PLACES = {
    "city": [
        {"v": "a futuristic megacity with towering skyscrapers and flying traffic", "l": "Futuristic megacity",
         "locs": ["a transit tube station with maglev trains", "a sky-level pedestrian bridge between towers",
                  "a hydroponic farm level glowing with grow lights", "a rooftop helipad district",
                  "a data center canyon with cooling towers", "a residential arcology atrium"]},
        {"v": "a cyberpunk city glowing with neon signs and holograms", "l": "Cyberpunk city",
         "locs": ["a neon noodle market under a rain canopy", "the rain-slicked underpass with flickering signs",
                  "the hack district with stacked server farms", "a rooftop fight club ring",
                  "the chrome plaza with holographic performers", "a back-alley fixer clinic glowing with tubes"]},
        {"v": "a medieval old town with stone towers and cobbled streets", "l": "Medieval old town",
         "locs": ["the market square with wooden stalls", "the guild hall street",
                  "the castle courtyard with a great oak", "the smithy quarter with forge glow",
                  "the cathedral steps", "the city gate with a portcullis"]},
        {"v": "a magical fantasy city with soaring spires and glowing runes", "l": "Magical fantasy city",
         "locs": ["a crystal market square with floating stalls", "the rune-carved palace district",
                  "floating gardens above the rooftops", "the alchemist quarter with glowing bottles",
                  "the spire district with hovering towers", "the enchanted canal bridges",
                  "the sky temple on a floating platform", "the grand observatory with a star chart dome"]},
        {"v": "a post-apocalyptic ruined city with crumbling concrete and rusted metal", "l": "Post-apocalyptic city",
         "locs": ["a collapsed highway overpass", "a rusted parking structure",
                  "a dead shopping mall with a fallen roof", "a makeshift survivor market",
                  "the flooded subway entrance", "a scrap-metal fortress wall"]},
        {"v": "a modern metropolis with glass towers and bridges", "l": "Modern metropolis",
         "locs": ["the business district canyon", "the subway interchange with crowds",
                  "the riverside promenade", "a glass skybridge between towers",
                  "the parking garage rooftop", "the stadium concourse"]},
        {"v": "an ancient city in ruins with broken marble columns and statues", "l": "Ancient city ruins",
         "locs": ["the fallen forum with broken columns", "a weathered amphitheater",
                  "the overgrown aqueduct", "a half-buried temple portico",
                  "the mosaic courtyard", "the grand staircase to a ruined palace"]},
        {"v": "a floating city above the clouds with waterfalls cascading off its edge", "l": "Floating city",
         "locs": ["the cloud pier where airships dock", "a waterfall spillway over the edge",
                  "the central reservoir gardens", "a sky-bazaar suspended on cables",
                  "the weather spire", "the undercity hanging platforms"]},
        {"v": "a steampunk city of brass towers, gears and mooring airships", "l": "Steampunk city",
         "locs": ["the clockwork tower district", "a coal-burning sky-dock",
                  "the brass foundry quarter", "a steam tram viaduct",
                  "the gearwork market hall", "the dirigible hangar"]},
        {"v": "a canal city threaded with waterways and arched bridges", "l": "Canal city",
         "locs": ["a gondola crossing at dawn market", "the water-level plaza with stair landings",
                  "a bridge-house row", "the flooded basilica steps",
                  "a canal lock with cargo barges", "the lantern-lit docks"]},
    ],
    "nature": [
        {"v": "a vast ancient forest with towering trees and shafts of light", "l": "Ancient forest",
         "locs": ["a hidden waterfall clearing", "a moss-covered giant tree",
                  "the misty glade", "a stream crossing with fallen logs",
                  "the hollow at the forest heart", "an overgrown stone shrine"]},
        {"v": "a towering mountain range with jagged peaks and deep valleys", "l": "Mountain range",
         "locs": ["a high alpine pass", "the snowline ridge",
                  "a glacial lake basin", "a sheer cliff face",
                  "the summit wind shelter", "a scree slope valley"]},
        {"v": "an endless desert with rolling dunes stretching to the horizon", "l": "Desert dunes",
         "locs": ["a dune crest at sunset", "a dry riverbed canyon",
                  "a lone oasis", "the salt flat edge",
                  "a ruined caravan stop", "a mesa overlook"]},
        {"v": "a raging ocean coastline with cliffs and crashing waves", "l": "Ocean coastline",
         "locs": ["a tide pool cove", "the sea stack arch",
                  "a black sand beach", "the lighthouse cliff",
                  "a kelp forest shore", "a fishing village pier"]},
        {"v": "a frozen tundra with ice fields and aurora skies", "l": "Frozen tundra",
         "locs": ["a frozen lake", "an ice cave entrance",
                  "the glacier face", "the aurora fields",
                  "an abandoned research station", "a windswept ice ridge"]},
        {"v": "a dense jungle with hanging vines and thick canopy", "l": "Dense jungle",
         "locs": ["a vine-draped canopy clearing", "a river bend with mudbanks",
                  "ruins swallowed by roots", "a bamboo thicket path",
                  "a waterfall plunge pool", "the canopy walkway"]},
        {"v": "a misty swamp with gnarled trees and still water", "l": "Misty swamp",
         "locs": ["a cypress tunnel", "a still-water lily pond",
                  "a fallen log bridge", "the drowned chapel",
                  "a firefly clearing", "the peat bog flats"]},
        {"v": "a savanna grassland with scattered acacia trees", "l": "Savanna grassland",
         "locs": ["an acacia grove", "a watering hole at dusk",
                  "the termite mound plains", "a rocky kopje outcrop",
                  "the tall grass sea", "a baobab cluster"]},
        {"v": "a volcanic highland with cooling lava fields and craters", "l": "Volcanic highlands",
         "locs": ["a cooling lava field", "the crater rim",
                  "a sulfur fumarole basin", "a volcanic lake",
                  "the ash slope", "an obsidian cliff"]},
        {"v": "an endless flower meadow rolling with wild blooms", "l": "Flower meadows",
         "locs": ["a poppy field", "a wildflower hill",
                  "a lavender terrace", "a sunflower field edge",
                  "a clover meadow", "a petal-strewn lane"]},
    ],
    "interior": [
        {"v": "a vast cathedral hall with tall arches and stained glass", "l": "Cathedral hall",
         "locs": ["the main nave", "the crypt below",
                  "the bell tower", "the stained-glass gallery",
                  "the choir loft", "a side reliquary chapel"]},
        {"v": "an opulent palace throne room with gold and marble", "l": "Palace throne room",
         "locs": ["the grand throne dais", "the royal gallery",
                  "the audience hall doors", "the crown treasury",
                  "the banquet hall", "the sunken garden court"]},
        {"v": "an abandoned industrial warehouse with broken machinery", "l": "Abandoned warehouse",
         "locs": ["the crane bay", "a shattered skylight aisle",
                  "the rusted conveyor floor", "a makeshift shelter camp",
                  "the collapsed mezzanine", "the graffiti wall section"]},
        {"v": "an ancient library filled with towering bookshelves", "l": "Ancient library",
         "locs": ["the central rotunda", "a reading gallery",
                  "the forbidden stacks", "the map room",
                  "the scriptorium", "the spiral book tower"]},
        {"v": "an underground military bunker with steel walls", "l": "Military bunker",
         "locs": ["the command center", "a barracks corridor",
                  "the armory", "the generator hall",
                  "the medical bay", "the blast-door entrance"]},
        {"v": "a spaceship corridor with glowing panels", "l": "Spaceship interior",
         "locs": ["the bridge", "a crew quarters deck",
                  "the hydroponics bay", "the cargo hold",
                  "the engine access walkway", "the observation lounge"]},
        {"v": "an ancient temple with carved pillars and braziers", "l": "Ancient temple",
         "locs": ["the inner sanctum", "the prayer hall",
                  "a processional avenue", "the altar chamber",
                  "the bell tower", "the monk's courtyard"]},
        {"v": "a castle dungeon with iron chains and torchlight", "l": "Castle dungeon",
         "locs": ["the iron cell row", "the guardroom",
                  "the oubliette shaft", "the wine cellar vaults",
                  "the escape tunnel breach", "the tower stairs landing"]},
        {"v": "a grand museum with soaring exhibition halls", "l": "Grand museum",
         "locs": ["the fossil hall", "a classical sculpture gallery",
                  "the natural history diorama", "the art deco lobby",
                  "the conservation lab", "the grand staircase hall"]},
        {"v": "a grand opera house with velvet and gold detail", "l": "Opera house",
         "locs": ["the main auditorium", "the box seats gallery",
                  "the backstage fly tower", "the rehearsal room",
                  "the chandelier foyer", "the orchestra pit"]},
    ],
    "weather": [
        {"v": "a massive hurricane churning over the ocean", "l": "Hurricane",
         "locs": ["over a coastal town", "over the open ocean",
                  "over a harbor with boats", "over a flooded boardwalk",
                  "over a lighthouse", "over palm groves"]},
        {"v": "a violent blizzard with horizontal whiteout", "l": "Blizzard",
         "locs": ["over a mountain pass", "over a frozen highway",
                  "over a small town", "over a ski village",
                  "over a forest", "over a research outpost"]},
        {"v": "an enormous sandstorm engulfing the landscape", "l": "Sandstorm",
         "locs": ["over desert ruins", "over a caravan road",
                  "over an oasis town", "over a fossil field",
                  "over dune seas", "over a mining camp"]},
        {"v": "a supercell thunderstorm with rotating clouds", "l": "Supercell storm",
         "locs": ["over a prairie", "over a highway",
                  "over a small town", "over a wind farm",
                  "over a wheat field", "over an airport"]},
        {"v": "a towering tornado tearing across open land", "l": "Tornado",
         "locs": ["over a farmstead", "over a highway with trucks",
                  "over a small town", "over open plains",
                  "over a trailer park", "over a windbreak"]},
        {"v": "a volcanic ash storm with glowing embers in the air", "l": "Volcanic ash storm",
         "locs": ["over a city", "over a village",
                  "over a lava field", "over a lake",
                  "over a highway", "over a forest"]},
        {"v": "a monsoon downpour flooding the streets", "l": "Monsoon",
         "locs": ["over a flooded street", "over rice terraces",
                  "over a river", "over a temple",
                  "over a rail line", "over a fishing harbor"]},
        {"v": "an aurora-lit storm night with crackling energy in the sky", "l": "Aurora storm",
         "locs": ["over a frozen lake", "over a mountain ridge",
                  "over a research station", "over open tundra",
                  "over a lighthouse", "over a fjord"]},
        {"v": "a rolling thunderstorm with lightning over the land", "l": "Thunderstorm",
         "locs": ["over a lake", "over a city skyline",
                  "over a cornfield", "over a bridge",
                  "over a forest", "over a railway"]},
        {"v": "a fierce hail storm battering the countryside", "l": "Hail storm",
         "locs": ["over farmland", "over a village street",
                  "over a parked car lot", "over a greenhouse row",
                  "over a schoolyard", "over a highway"]},
    ],
}
ENV_SUBTYPES = {t: [p["v"] for p in pl] for t, pl in ENV_PLACES.items()}
ENV_LOCATIONS = {p["v"]: p["locs"] for pl in ENV_PLACES.values() for p in pl}
ENV_MOODS = ["ominous", "peaceful", "epic", "desolate", "mysterious",
             "post-apocalyptic", "utopian", "noir"]
ENV_TIMES = ["night", "sunset", "golden hour", "dawn", "storm twilight"]

def env_prompt(p: dict) -> str:
    """Generation prompt for environment shots. Fixed slot order:
    camera -> PLACE (the specific world) + LOCATION (where inside it) ->
    mood -> time -> weather (skipped for weather-type scenes) ->
    foreground -> style. Location phrases are self-contained so the model
    never gets a contradictory combination (locations belong to one place
    only, by construction)."""
    t = p.get("type", "city")
    subtype = p.get("subtype", "") or ENV_SUBTYPES.get(t, [""])[0]
    location = p.get("location", "")
    loc_txt = ""
    if location:
        if t == "weather":
            loc_txt = f" {location}"
        else:
            loc_txt = f", {location}"
    weather_txt = ""
    if t != "weather":
        weather_txt = f", {p.get('weather', ENV_WEATHER[0])}"
    return (
        f"cinematic photorealistic {p.get('camera', ENV_CAMERA[0])} of "
        f"{subtype}{loc_txt}, {p.get('mood', 'epic')} atmosphere, "
        f"{p.get('time', 'night')}{weather_txt}, "
        f"{p.get('foreground', ENV_FOREGROUND[1])}, volumetric "
        f"atmosphere, sharp detail, no people, film still, anamorphic"
    )

def env_caption(p: dict, m: dict) -> str:
    """COMPACT factual caption for TRAINING — same vocabulary as the
    generation prompt, short tag list."""
    parts = [m.get("location", ""), m.get("subtype", ""),
             f"{m.get('mood', '')} atmosphere", m.get("time", ""),
             m.get("weather", ""), m.get("foreground", "")]
    return ", ".join(x for x in parts if x)


EMOTION_LOOKUP = {
    "stoic": "stoic, neutral composed face, relaxed mouth",
    "cold": "cold emotionless stare, narrowed eyes, tight lips",
    "angry": "angry, furrowed brows, glaring eyes, clenched jaw",
    "calm": "calm and serene, soft gentle eyes, faint warm smile",
    "grief": "grieving, sorrowful eyes, trembling lips, tears welling",
    "focused": "focused, intense concentration, slight squint",
    "stern": "stern and disapproving, hard stare, pressed lips",
    "determined": "determined, set jaw, unwavering intense gaze",
    "sorrowful": "sorrowful, downcast eyes, forlorn expression",
    "intense": "intense piercing glare, rigid tension in the face",
    "happy smiling": "happy, broad warm smile, bright crinkled eyes",
    "laughing": "laughing hard, mouth wide open, eyes crinkled shut",
    "grinning": "grinning confidently, wide smirk, raised brows",
    "screaming in rage": "screaming in rage, mouth wide open, face contorted, veins on the forehead",
    "roaring": "roaring with fury, mouth agape, bared teeth, wild eyes",
    "terrified": "terrified, wide eyes, open mouth in shock, panicked",
    "surprised": "surprised, raised eyebrows, open mouth, wide eyes",
    "weeping": "weeping, tears streaming down the cheeks, scrunched face",
    "sneering": "sneering contemptuously, curled upper lip, narrowed eyes",
    "serious": "serious and solemn, flat expression, straight mouth",
}

# fixed seed base per character — same seed = same face structure across
# every image; only the prompt words (emotion/angle) change the picture
CHARACTER_SEED_BASE = 424242
POWER_SEED_BASE = 525252
ENV_SEED_BASE = 626262

MAX_BATCH = 120  # hard cap: every ticked option multiplies, so guard it

# Generation models now live in models.py (the canonical registry) — the
# old T2I_STACKS (Wan standard/fastwan) is retired. The wizard's Model Mix
# selects from the registry; 'stack' params are mapped for back-compat.


def identity_block(p: dict) -> str:
    """The fixed identity phrase (shared by prompts AND training captions,
    so what the model sees at generation is exactly what it learns)."""
    return (
        f"{p.get('hair_style', 'short')} {p.get('hair_color', 'black')} hair, "
        f"{p.get('eye_color', 'dark brown')} {p.get('eye_shape', 'deep-set')} eyes, "
        f"{p.get('brows', 'strong straight')} eyebrows, "
        f"{p.get('nose', 'straight')} nose, "
        f"{p.get('lips', 'medium-full')} lips, "
        f"{p.get('face_shape', 'angular')} face, "
        f"{p.get('skin', SKINS[0])} skin, "
        f"{p.get('facial_hair', 'clean-shaven')}, "
        f"{p.get('build', 'lean')} build"
    )


def character_prompt(p: dict) -> str:
    """Canonical identity-locked prompt (v2).
    - IDENTITY BLOCK identical in every image of the character (seed is
      also fixed, so the face is actually the SAME person)
    - gender emphasized right after the name (man -> 'a masculine
      mid-30s man') — prevents the model drifting to another gender
    - emotion expanded into a VIVID facial description (a static portrait
      does not show plain words like 'calm')
    - markings explicitly 'clearly visible'"""
    name = p.get('name', 'a person')
    gender = p.get('gender', 'man')
    age = p.get('age', 'mid-30s')
    gphrase = (f"a masculine {age} {gender}"
               if gender == "man" else f"a feminine {age} {gender}")
    emotion = EMOTION_LOOKUP.get(p.get('emotion', 'stoic'))
    identity = identity_block(p)
    marking = p.get('distinctive', 'no visible markings')
    marking_txt = "" if marking == "no visible markings" \
        else f", with {marking} clearly visible"
    return (
        f"cinematic photorealistic {p.get('angle', 'front-facing')} portrait "
        f"of {name}, {gphrase}, {identity}, {emotion}{marking_txt}, wearing "
        f"{p.get('outfit', OUTFITS[0])}, {p.get('lighting', LIGHTING[0])}, "
        f"85mm portrait lens, shallow depth of field, ultra detailed skin "
        f"texture, single subject, one face only, professional photography"
    )



class DatasetGen:
    """Thin job-driven facade. Generates dataset images
    through the shared job engine — supports Model Mix, continuation into
    existing datasets, resume/retry, provenance. Keeps the old
    /api/training/gen_dataset surface working."""

    def __init__(self) -> None:
        self.active: dict = {"running": False, "done": 0, "total": 0,
                             "message": "", "stage": "", "run_id": None}
        self._run_id: int | None = None

    # ------------------------------------------------------------ compat
    def start(self, mode: str, params: dict) -> dict:
        import jobs
        from models import model_ids
        # self-heal: if our tracked run already reached a terminal state
        # (polled via the jobs API), release the tracking slot. Runs may be
        # CONCURRENT now — the per-machine lane locks in jobs.py serialize
        # what touches each GPU, so this only tracks the newest run for the
        # legacy status surface.
        if self._run_id:
            import datasets_db as db
            s = db.run_stats(self._run_id)
            if not s or s.get("status") not in ("queued", "active"):
                self._run_id = None
                self.active["running"] = False
        if mode not in ("character", "power", "environment", "photo",
                        "weapon"):
            return {"ok": False, "error": f"bad mode {mode}"}
        # resolve target dataset (mode -> type, unless explicit)
        dtype = params.get("dataset_type") or {
            "character": "face", "power": "power",
            "environment": "environment", "photo": "face",
            "weapon": "weapon"}[mode]
        name = params.get("name", "").strip()
        if not name:
            return {"ok": False, "error": "dataset name required"}
        if not is_safe_component(dtype) or dtype not in (
                "face", "power", "environment", "weapon", "outfit",
                "anatomy", "quality"):
            return {"ok": False, "error": f"bad dataset type {dtype}"}
        if not is_safe_component(name):
            return {"ok": False, "error": f"bad dataset name {name}"}
        # Model Mix: explicit rows, else legacy single-model mapping
        rows = params.get("models")
        if not rows:
            # no Model Mix: RealVisXL, unless an old caller names one of the
            # retired Wan stacks ("standard"/"fastwan" -> the legacy `wan`)
            legacy = params.get("stack", "")
            mid = params.get("model") or (
                "wan" if legacy in ("standard", "fastwan") else "realvis")
            if mid not in model_ids():
                return {"ok": False, "error": f"unknown model '{mid}'"}
            total = params.get("count") or self._legacy_total(mode, params)
            try:
                total = int(total)
            except (TypeError, ValueError):
                total = self._legacy_total(mode, params)
            rows = [{"model": mid, "count": total}]
        for r in rows:
            r.setdefault("count", 1)
        # continuation: append into existing dataset
        d = ROOT / "roster" / "datasets" / dtype / name
        d.mkdir(parents=True, exist_ok=True)
        # per-image worker assignment: travels with the
        # request but is persisted separately (queue.db) by jobs.submit
        worker_map = params.pop("worker_map", None)
        job_params = dict(params)
        # the UI sends `mode` = prompt mode (structured|add|override); keep it
        # as `prompt_mode` before `mode` is overwritten with the generation
        # mode, or override/add silently degrade to structured (jobs._run_one)
        job_params.update({"mode": mode,
                           "prompt_mode": str(params.get("mode")
                                              or "structured"),
                           "models": rows,
                           "dataset_type": dtype, "dataset_name": name})
        res = jobs.submit(job_params, dtype, name,
                          worker_map=worker_map if worker_map else None)
        if not res.get("ok"):
            return res
        self._run_id = res["run_id"]
        self.active.update({"running": True, "done": 0,
                            "total": res["total"],
                            "message": "queued", "stage": "", "run_id": res["run_id"]})
        return {"ok": True, "run_id": res["run_id"], "total": res["total"],
                "combo_count": res["combo_count"]}

    def _legacy_total(self, mode: str, params: dict) -> int:
        import jobs
        combos = jobs.build_combos(mode, params)
        return len(combos)

    def stop(self) -> None:
        import jobs
        if self._run_id:
            jobs.cancel(self._run_id)
            self.active.update({"running": False, "message": "canceled"})

    def status(self) -> dict:
        import datasets_db as db
        if self._run_id:
            s = db.run_stats(self._run_id)
            if s:
                total = s.get("total") or 0
                done = s.get("done") or 0
                self.active.update({
                    "running": s.get("status") in ("queued", "active"),
                    "done": done, "total": total,
                    "stage": s.get("status"),
                    "message": (f"{done}/{total} · {s.get('status')}"
                                + (f" · {s.get('error')}"
                                   if s.get("error") else "")),
                })
                if not self.active["running"]:
                    self._run_id = None
                    # model eviction is owned by the GPU broker (per-model TTL)
        return {"gen": self.active}
