"""Story Architect — structured narrative and script generator.

Idea -> Treatment -> Story Bible -> Beat Sheet -> Scenes -> Shot List.

Every stage is versioned, independently visible/editable/regeneratable, and
produces linter-valid structured schemas (never prose prompts). The built-in
deterministic generator is the default provider; external AI agents can
inject content per stage via import_stage().

Determinism: every creative choice is drawn from a random.Random seeded by
md5(idea + genre + tone + duration_target) — regenerating a stage with the
same inputs yields the same output.
"""
from __future__ import annotations

import hashlib
import math
import random
from typing import Any

from core import log_setup

log = log_setup("storygen")

STAGES = ("treatment", "bible", "beats", "scenes", "shots")

# ---------------------------------------------------------------- keywords
POWER_KEYWORDS = (
    ("divine", "divine"), ("holy", "divine"), ("light", "divine"),
    ("fire", "fire"), ("flame", "fire"), ("burn", "fire"),
    ("ice", "ice"), ("frost", "ice"), ("frozen", "ice"),
    ("lightning", "lightning"), ("storm", "lightning"),
    ("shadow", "shadow"), ("darkness", "shadow"),
    ("water", "water"), ("ocean", "water"),
    ("wind", "wind"),
    ("earth", "earth"), ("stone", "earth"),
    ("arcane", "arcane"), ("magic", "arcane"),
    ("nature", "nature"), ("forest", "nature"),
    ("plasma", "plasma"), ("energy", "plasma"),
    ("force", "force"), ("telekinesis", "force"),
    ("void", "void"), ("abyss", "void"),
)

GENRE_KEYWORDS = (
    ("dark fantasy", "dark fantasy"), ("dark-fantasy", "dark fantasy"),
    ("high fantasy", "high fantasy"), ("epic fantasy", "high fantasy"),
    ("sci-fi", "sci-fi"), ("scifi", "sci-fi"), ("cyberpunk", "cyberpunk"),
    ("urban fantasy", "urban fantasy"), ("urban", "urban fantasy"),
    ("post-apocalyptic", "post-apocalyptic"), ("apocalypse", "post-apocalyptic"),
    ("steampunk", "steampunk"),
    ("mythic", "mythic"), ("myth", "mythic"),
)

ARCHETYPE_KEYWORDS = (
    ("princess", "princess"), ("royal", "princess"), ("queen", "queen"),
    ("knight", "knight"), ("warrior", "warrior"), ("soldier", "warrior"),
    ("mage", "mage"), ("wizard", "mage"), ("sorceress", "mage"),
    ("thief", "assassin"), ("assassin", "assassin"),
    ("wanderer", "wanderer"), ("stranger", "wanderer"), ("outcast", "wanderer"),
)

POWER_DESCRIPTORS = {
    "divine": {"descriptor": "radiant golden light erupting from open palms, "
                            "halo flare, floating embers drifting upward",
               "color": "#ffd76a", "motion": "expanding waves of holy light"},
    "fire": {"descriptor": "swirling orange flames roaring from the hands, "
                           "heat shimmer, sparks spiraling outward",
             "color": "#ff6a00", "motion": "lashing tongues of fire"},
    "ice": {"descriptor": "crystalline ice blades forming mid-air, frost "
                          "vapor, glinting blue facets",
            "color": "#9fe8ff", "motion": "rapid freezing crystalline growth"},
    "lightning": {"descriptor": "crackling cyan lightning arcs from both "
                                "hands, forked bolts",
                  "color": "#00e5ff", "motion": "fast zigzag bolts"},
    "shadow": {"descriptor": "living shadows pooling and rising, tendrils of "
                             "pure darkness, swallowing the light",
               "color": "#1a1033", "motion": "flowing, writhing tendrils"},
    "water": {"descriptor": "surges of churning water swirling upward, "
                            "glinting droplets suspended in air",
              "color": "#4fc3f7", "motion": "rolling waves and spirals"},
    "wind": {"descriptor": "visible wind currents streaking through the air, "
                           "debris lifted into spiraling gusts",
             "color": "#cfe8ea", "motion": "fast streaking gusts"},
    "earth": {"descriptor": "stone slabs tearing up from the ground, dust "
                            "plumes, grinding rock",
              "color": "#8d6e63", "motion": "slow heaving, crushing shifts"},
    "arcane": {"descriptor": "glowing violet sigils orbiting the hands, "
                             "concentrated arcane light weaving patterns",
               "color": "#b388ff", "motion": "orbiting sigils, strobing glow"},
    "nature": {"descriptor": "vines and roots bursting to life, blooming "
                             "light, drifting pollen motes",
               "color": "#7cb342", "motion": "organic growing tendrils"},
    "plasma": {"descriptor": "superheated plasma flares in vivid magenta, "
                             "condensed energy arcing and pulsing",
               "color": "#ff4081", "motion": "pulsing condensed flares"},
    "force": {"descriptor": "invisible telekinetic waves distorting the air, "
                            "objects lifted and hurled",
              "color": "#b0bec5", "motion": "slow displacement waves"},
    "void": {"descriptor": "a cold void tearing open mid-air, light bending "
                           "into nothingness, silence",
             "color": "#0b0b14", "motion": "slow consuming collapse"},
}

CHARACTER_POOLS = {
    "princess": (["Elara", "Seraphina", "Liora", "Isolde", "Elowen",
                  "Aurelia", "Cassia", "Thalassa"],
                 ["Varek", "Malric", "Draven", "Korzan", "Valerius",
                  "Aldric", "Dain", "Rurik"],
                 ["Maren", "Torin", "Sable", "Lian", "Rhea"]),
    "queen": (["Regina", "Morwenna", "Isabeau", "Celestine", "Vespera"],
              ["Haldor", "Baran", "Ormund", "Cassius", "Vex"],
              ["Anwen", "Kestrel", "Idris", "Naia"]),
    "knight": (["Gareth", "Brennan", "Sigurd", "Alden", "Cade"],
               ["Rothgar", "Malach", "Ulric", "Draco", "Voss"],
               ["Fenwick", "Odo", "Lysander", "Bria"]),
    "warrior": (["Kaia", "Ronan", "Sela", "Bran", "Ash"],
                ["Grimm", "Talos", "Zarek", "Mordecai", "Vex"],
                ["Wren", "Hale", "Mira", "Toren"]),
    "mage": (["Sylas", "Nadia", "Elias", "Freya", "Caspian"],
             ["Moreau", "Sabin", "Tarrant", "Veyra", "Kormac"],
             ["Aster", "Lumen", "Pip", "Sage"]),
    "assassin": (["Vex", "Shade", "Rook", "Nyx", "Cinder"],
                 ["Lord Oris", "Magistrate Vane", "The Warden", "Kal", "Sirus"],
                 ["Ember", "Wisp", "Dax", "Ione"]),
    "wanderer": (["Kael", "Rowan", "Finn", "Eira", "Aric"],
                 ["Voss", "Malrik", "Baron Thule", "Darragh", "Ond"],
                 ["Sera", "Tobias", "Nia", "Garrick"]),
}

LOCATION_POOLS = {
    "dark fantasy": [
        ("palace_throne_room", "vast throne room of black marble, banners of "
         "a fallen house, cold braziers", "cold amber torchlight, long shadows"),
        ("kingdom_gate", "great iron gates of the besieged capital, siege "
         "engines looming beyond the walls", "dusk, torchlight and smoke"),
        ("enchanted_forest", "ancient forest with glowing moss and silver "
         "mist between the trees", "moonlight filtered green through the canopy"),
        ("ruined_temple", "crumbling temple of an old god, broken statues, "
         "dust in light shafts", "god-rays through collapsed roof"),
        ("mountain_pass", "narrow pass between black peaks, storm clouds "
         "rolling overhead", "cold storm light, wind"),
        ("castle_corridor", "torchlit corridor of dressed stone, tapestries "
         "of the royal line", "warm torchlight, deep shadows"),
        ("underground_crypt", "royal crypt, sarcophagi in rows, thick dust "
         "and cobwebs", "faint candlelight, oppressive dark"),
        ("burning_village", "village in flames, embers drifting, smoke "
         "columns against the night", "orange firelight, black smoke"),
    ],
    "high fantasy": [
        ("elven_glade", "ethereal glade of silver trees, luminous flowers",
         "soft golden light, gentle mist"),
        ("sky_spire", "floating crystal spires linked by light bridges",
         "clear sky light, prismatic refractions"),
        ("grand_library", "endless library of ancient tomes and floating "
         "lanterns", "warm amber lamplight"),
        ("royal_courtyard", "sunlit courtyard with fountains and white stone",
         "bright daylight, soft shadows"),
        ("dragon_peak", "windswept peak where dragons once nested",
         "storm light breaking through clouds"),
        ("sacred_spring", "crystal-clear spring radiating soft light",
         "glowing aqua light from below"),
    ],
    "sci-fi": [
        ("starport_dock", "vast docking bay with starships behind glass, "
         "steam vents", "blue-white hangar light"),
        ("reactor_core", "pulsing fusion core chamber, catwalks, warning "
         "lights", "cyan core glow, flickering red alerts"),
        ("orbital_habitat", "ring habitat interior, city inverted overhead",
         "artificial daylight, panoramic"),
        ("wasteland_outpost", "scrap-built outpost in a red desert",
         "harsh sun, dust haze"),
        ("command_bridge", "ship bridge, holomaps, crew stations",
         "cool blue console light"),
        ("alien_ruins", "monolithic alien structure, glowing glyphs",
         "eerie green luminescence"),
    ],
    "urban fantasy": [
        ("rooftop_city", "rain-slicked rooftop above neon city",
         "moonlight, neon spill"),
        ("hidden_alley", "narrow brick alley with steam and puddles",
         "single streetlamp, deep shadow"),
        ("old_church", "gothic church interior, candles, stained glass",
         "colored light through windows"),
        ("metro_station", "abandoned subway platform, flickering lights",
         "flickering fluorescent"),
        ("antique_shop", "cramped occult shop, hanging charms",
         "dim warm lamplight"),
        ("city_bridge", "iron bridge over a black river",
         "city glow on water"),
    ],
    "post-apocalyptic": [
        ("collapsed_overpass", "ruined highway overpass, rusted cars",
         "harsh daylight through haze"),
        ("ruined_skyscraper", "shell of a skyscraper, open floors",
         "wind, dusty sunlight"),
        ("bunker_entrance", "blast door in a hillside, warning signs",
         "overcast, moody"),
        ("market_camp", "tent camp around a water pump, traders",
         "campfire and string lights"),
        ("dry_riverbed", "cracked riverbed with dead trees",
         "white-hot noon"),
        ("radio_tower", "lone radio tower on a hill",
         "storm clouds gathering"),
    ],
    "steampunk": [
        ("clockwork_plaza", "plaza of brass gears and steam pipes, clock "
         "tower", "golden hour through steam"),
        ("airship_deck", "airship deck among clouds",
         "bright sky light, wind"),
        ("engine_room", "boiler engine room, pistons, coal glow",
         "orange forge light"),
        ("victorian_street", "gaslit cobblestone street",
         "green gaslight, fog"),
    ],
    "cyberpunk": [
        ("neon_street", "rain-soaked street walled with neon signs",
         "neon haze, wet reflections"),
        ("hacker_den", "cramped room of monitors and cables",
         "screen glow, darkness"),
        ("megacorp_lobby", "black-glass corporate lobby",
         "cold white light"),
        ("skybridge", "glass bridge between towers",
         "city lights far below"),
    ],
    "mythic": [
        ("valley_of_kings", "sun-bleached valley with colossal statues",
         "golden dawn light"),
        ("olympus_gate", "cloud gate guarded by marble titans",
         "radiant sky light"),
        ("underworld_river", "black river through a cavern of souls",
         "cold silver glow"),
        ("sacred_grove", "grove of white pillars and laurel",
         "soft divine light"),
    ],
}

THEMES = {
    "dark fantasy": ["power demands sacrifice", "hope in the shadow",
                     "the throne is a burden"],
    "high fantasy": ["destiny and choice", "unity against ruin",
                     "courage of the small"],
    "sci-fi": ["humanity among the stars", "the machine that remembers",
               "frontier conscience"],
    "urban fantasy": ["magic beneath the modern", "two worlds, one heart",
                      "the hidden cost of wonder"],
    "post-apocalyptic": ["what survives the end", "community is armor",
                         "the green after ash"],
    "steampunk": ["progress and its price", "gears of fortune",
                   "the inventor's burden"],
    "cyberpunk": ["soul vs. system", "neon and dust",
                  "memory is currency"],
    "mythic": ["fate is a river", "gods need heroes", "mortal defiance"],
}

CROWD_POOLS = {
    "dark fantasy": {"royal_guards": "royal guards in blackened steel plate "
                      "with crimson cloaks, matching helms",
                     "city_folk": "frightened city folk in worn earth-tone "
                      "clothing, some carrying children"},
    "high fantasy": {"elven_attendants": "elven attendants in flowing silver "
                     "robes, unarmed",
                     "market_goers": "colorful market-goers with baskets "
                      "and banners"},
    "sci-fi": {"station_crew": "station crew in grey-and-orange utility "
               "suits with insignia patches",
               "docking_workers": "docking workers in exo-rigs and "
                "yellow hazard vests"},
    "urban fantasy": {"night_crowd": "night crowd in coats and scarves "
                      "hurrying through the rain",
                      "street_vendors": "street vendors at stall carts "
                       "under awnings"},
    "post-apocalyptic": {"scavengers": "weathered scavengers in patched "
                         "gear with goggles and packs",
                         "camp_children": "children in patched clothes "
                          "playing near the pump"},
    "steampunk": {"clockwork_clerks": "clerks in bowler hats and goggles, "
                  "leather aprons",
                  "airship_crew": "airship crew in brass-buttoned coats"},
    "cyberpunk": {"corpo_agents": "corporate agents in black suits with "
                  "chrome augments",
                  "street_gang": "street gang in neon-painted jackets"},
    "mythic": {"temple_priests": "temple priests in white-gold robes with "
               "laurel crowns",
               "pilgrims": "pilgrims in simple linen, dust-covered sandals"},
}

EMOTION_PALETTES = {
    "protagonist": ["calm", "focused", "tense", "fearful", "determined",
                    "grief", "serene", "cold", "rage"],
    "antagonist": ["stern", "cold", "fear", "respect", "anger", "amused"],
    "ally": ["amused", "wary", "sympathetic", "fearful", "determined"],
}

CAMERAS = ["static", "slow_push_in", "dolly", "pan", "handheld", "orbit",
           "pull_back", "tracking", "crane"]
SHOT_SIZES = ["extreme_close_up", "close_up", "medium_close_up", "medium",
              "medium_wide", "wide", "extreme_wide"]
POSITIONS = ["foreground_left", "foreground_center", "foreground_right",
             "midground_left", "midground_center", "midground_right",
             "background_left", "background_center", "background_right"]
FACINGS = ["camera_left", "camera_right", "camera", "away_from_camera"]

PRIMARY_ACTIONS = {
    "stand": "stands motionless, coat shifting in the wind",
    "step": "steps forward deliberately, eyes locked ahead",
    "turn": "turns slowly to face the camera",
    "look": "looks up, expression shifting as the light changes",
    "raise": "raises both hands as the power gathers",
    "release": "releases the power in a controlled burst",
    "reach": "reaches out one hand, the air rippling around it",
    "walk": "walks through the scene, gaze steady",
    "flee": "sprints across the frame, glancing back once",
    "kneel": "drops to one knee, head bowed",
    "draw": "draws the blade in one fluid motion",
    "clash": "meets the attack, sparks bursting on impact",
    "fall": "staggers backward, catching the wall for balance",
    "gaze": "gazes into the distance, unmoving",
}


class StoryArchitect:
    """Idea -> Treatment -> Bible -> Beats -> Scenes -> Shots."""

    def __init__(self, project: dict, store, bible) -> None:
        self.project = project
        self.pid = project["id"]
        self.store = store
        self.bible = bible
        self.idea = project.get("idea") or ""
        self.genre = project.get("genre") or self._detect_genre()
        self.tone = project.get("tone") or "epic, grounded"
        self.duration = int(project.get("duration_target") or 60)
        seed = int(hashlib.md5(
            (self.idea + self.genre + self.tone +
             str(self.duration)).encode("utf-8")).hexdigest()[:8], 16)
        self.rng = random.Random(seed)
        self.n_shots = max(4, math.ceil(self.duration / 5))
        self.n_scenes = min(8, max(2, round(self.n_shots / 7)))
        self._ctx: dict[str, Any] | None = None
        self.cast_config: dict[str, Any] | None = None
        self._cast_ids: dict[str, str] = {}
        self._cast_palette: dict[str, list[str]] = {}

    def _ensure_ctx(self) -> dict[str, Any]:
        """Derive (once per instance) archetype/power/names/locations/crowd —
        cached so every stage references the SAME cast and world. With a
        cast_config, the user-provided cast replaces the generated pools."""
        if self._ctx is None:
            if self.cast_config is not None:
                self._ctx = self._ctx_from_cast()
            else:
                self.archetype = self._detect_archetype()
                self.power = self._detect_power()
                pool = CHARACTER_POOLS.get(self.archetype,
                                           CHARACTER_POOLS["wanderer"])
                self.prot, self.antag, self.ally = (self.rng.choice(pool[0]),
                                                    self.rng.choice(pool[1]),
                                                    self.rng.choice(pool[2]))
                loc_pool = LOCATION_POOLS.get(self.genre,
                                              LOCATION_POOLS["dark fantasy"])
                loc_picks = self.rng.sample(loc_pool, k=min(3, len(loc_pool)))
                self._locations_cache = [
                    {"id": pid, "descriptor": desc, "lighting": light}
                    for pid, desc, light in loc_picks]
                crowd_pool = CROWD_POOLS.get(self.genre,
                                             CROWD_POOLS["dark fantasy"])
                crowd_keys = self.rng.sample(sorted(crowd_pool), k=2)
                self._crowd_cache = {k: {"tier": "crowd",
                                         "descriptor": crowd_pool[k]}
                                     for k in crowd_keys}
                self._ctx = {
                    "archetype": self.archetype, "power": self.power,
                    "prot": self.prot, "antag": self.antag, "ally": self.ally,
                    "locations": self._locations_cache,
                    "crowd": self._crowd_cache,
                }
        return self._ctx

    def _ctx_from_cast(self) -> dict[str, Any]:
        """Derive the context from the user's cast_config: protagonist is
        the first tier=='main' character, antagonist the first supporting,
        ally the second supporting. No pool selection happens here."""
        cc = self.cast_config or {}
        chars = [c for c in cc.get("characters", [])
                 if isinstance(c, dict) and c.get("id")]
        enabled = [c for c in chars if c.get("enabled", True)]
        mains = [c for c in enabled if c.get("tier") == "main"]
        supps = [c for c in enabled if c.get("tier") == "supporting"]
        prot_c = mains[0] if mains else (enabled[0] if enabled else None)
        antag_c = supps[0] if supps else None
        ally_c = supps[1] if len(supps) > 1 else None
        self.prot = prot_c["name"] if prot_c else ""
        self.antag = antag_c["name"] if antag_c else ""
        self.ally = ally_c["name"] if ally_c else ""
        self._cast_ids = {c["name"]: c["id"] for c in enabled}
        self._cast_palette = {c["id"]: list(c.get("emotion_palette") or [])
                              for c in enabled}
        self.archetype = self._detect_archetype()
        if prot_c and isinstance(prot_c.get("powers"), dict) \
                and prot_c["powers"]:
            self.power = next(iter(prot_c["powers"]))
        else:
            self.power = None
        locs = [l for l in cc.get("locations", [])
                if isinstance(l, dict) and l.get("id")]
        if locs:
            # preserve the FULL location dicts (lora/enabled included)
            self._locations_cache = [dict(l) for l in locs]
        else:
            self._locations_cache = [
                {"id": "default_room", "descriptor": "bare stone room",
                 "lighting": "neutral daylight"}]
        self._crowd_cache = {}
        return {
            "archetype": self.archetype, "power": self.power,
            "prot": self.prot, "antag": self.antag, "ally": self.ally,
            "locations": self._locations_cache,
            "crowd": self._crowd_cache,
        }

    def _char_id(self, name: str) -> str:
        """Character id for a role name: cast_config ids when present,
        else the slug."""
        if self.cast_config is not None and name in self._cast_ids:
            return self._cast_ids[name]
        return self._cid(name)

    def _cast_emo(self, cid: str | None, i: int, n: int) -> str:
        """Gradual walk along the character's own emotion palette."""
        pal = self._cast_palette.get(cid or "", []) or ["calm"]
        if n <= 1 or len(pal) == 1:
            return pal[0]
        idx = round((i - 1) / (n - 1) * (len(pal) - 1))
        return pal[max(0, min(len(pal) - 1, idx))]

    # -------------------------------------------------------------- parsing
    def _detect_genre(self) -> str:
        low = self.idea.lower()
        for key, genre in GENRE_KEYWORDS:
            if key in low:
                return genre
        return "dark fantasy"

    def _detect_power(self) -> str:
        low = self.idea.lower()
        for key, power in POWER_KEYWORDS:
            if key in low:
                return power
        return "arcane"

    def _detect_archetype(self) -> str:
        low = self.idea.lower()
        for key, arch in ARCHETYPE_KEYWORDS:
            if key in low:
                return arch
        return "wanderer"

    def _names(self) -> tuple[str, str, str]:
        pool = CHARACTER_POOLS.get(self.archetype, CHARACTER_POOLS["wanderer"])
        prot = self.rng.choice(pool[0])
        antag = self.rng.choice(pool[1])
        ally = self.rng.choice(pool[2])
        return prot, antag, ally

    def _locations(self) -> list[dict]:
        return list(self._ensure_ctx()["locations"])

    def _crowd_types(self) -> dict:
        return dict(self._ensure_ctx()["crowd"])

    # --------------------------------------------------------------- stages
    def generate_stage(self, stage: str) -> dict:
        if stage not in STAGES:
            return {"ok": False, "error": f"unknown stage {stage!r}"}
        self._ensure_ctx()
        fn = getattr(self, f"_stage_{stage}")
        return fn()

    def _stage_treatment(self) -> dict:
        a, p = self.archetype, (self.power or self._detect_power())
        themes = THEMES.get(self.genre, THEMES["dark fantasy"])
        return {
            "logline": (f"A {a} with an awakening gift of {p} must defend "
                        f"everything she loves when her world comes under "
                        f"siege."),
            "synopsis": (
                f"{self.idea.rstrip().rstrip('.')}. "
                f"What begins as a quiet life shatters in a single night: "
                f"the enemy is at the gates, and the {p} sleeping inside "
                f"{self.prot} awakens with terrifying force. "
                f"Hunted, doubted, and slowly mastering a power that "
                f"answers to her will, she discovers that {self.antag} did "
                f"not come for the kingdom alone — but for the power she "
                f"now carries."),
            "themes": themes[:3],
            "protagonist": self.prot,
            "antagonist": self.antag,
            "escalation": [
                f"first spark: {p} stirs unbidden during the attack",
                f"midpoint: {self.antag} reveals knowledge of the gift and "
                f"demands it",
                f"climax: full awakening, {p} unleashed at a terrible cost",
            ],
            "ending": (f"{self.prot} survives, changed — the kingdom safe "
                       f"but the true war only beginning."),
            "pacing": "slow-burn tension, burst of action, quiet aftermath",
            "clipable_moments": [
                f"the first {p} reveal, hands trembling with light",
                f"the confrontation: {self.prot} vs {self.antag}",
                f"the final shot: a new dawn over the scarred kingdom",
            ],
        }

    def _stage_bible(self) -> dict:
        if self.cast_config is not None:
            return self._stage_bible_from_cast()
        a, p = self.archetype, self.power
        pd = POWER_DESCRIPTORS.get(p, POWER_DESCRIPTORS["arcane"])
        prot_body = {
            "princess": "young woman with high cheekbones, soft brown eyes, "
                        "chestnut hair in a loose braid, regal posture",
            "queen": "middle-aged woman with silver-streaked dark hair, "
                     "commanding eyes, elegant bearing",
            "knight": "broad-shouldered man with short-cropped hair and a "
                      "weathered face",
            "warrior": "athletic build, scarred hands, alert eyes",
            "mage": "slender figure with ink-dark eyes and pale skin, "
                    "long fingers",
            "assassin": "lithe build, sharp features, eyes that never rest",
            "wanderer": "lean build, wind-worn face, steady gaze",
        }.get(a, "lean build, wind-worn face, steady gaze")
        prot_outfit = {
            "princess": "royal gown of deep emerald brocade, silver "
                        "embroidery, thin circlet",
            "queen": "black velvet gown with gold trim, heavy signet ring",
            "knight": "battle-worn plate armor with a torn tabard",
            "warrior": "leather armor with iron pauldrons, wrapped forearms",
            "mage": "dark blue robes with arcane stitching, worn satchel",
            "assassin": "black leathers, hooded cloak, wrist blades",
            "wanderer": "travel-worn coat, patched cloak, leather satchel",
        }.get(a, "travel-worn coat, patched cloak")
        antag_body = ("broad figure with a hard jaw, cropped grey hair, "
                      "cold grey eyes")
        antag_outfit = ("blackened steel armor with a crimson cloak, "
                        "gauntlets of dark iron")
        ally_body = ("lithe, quick-eyed companion with a ready half-smile, "
                     "practical clothes of grey and brown")
        ally_outfit = "grey hooded tunic, leather bracers, satchel of tools"
        characters = [
            {"id": self._cid(self.prot), "name": self.prot, "tier": "main",
             "body": prot_body, "outfit": prot_outfit,
             "powers": {p: dict(pd)},
             "voice": {"engine": "moss-tts",
                       "style_tag": "clear, resolute young woman"
                       if a in ("princess", "queen") else "calm, resolute",
                       "params": {"pitch": 0, "speed": 1.0}},
             "emotion_palette": EMOTION_PALETTES["protagonist"],
             "arc_summary": (f"from sheltered {a} to the kingdom's "
                             f"reluctant defender")},
            {"id": self._cid(self.antag), "name": self.antag,
             "tier": "supporting", "body": antag_body, "outfit": antag_outfit,
             "voice": {"engine": "moss-tts",
                       "style_tag": "commanding, ruthless",
                       "params": {"pitch": -2, "speed": 0.95}},
             "emotion_palette": EMOTION_PALETTES["antagonist"],
             "arc_summary": "ruthless invader who knows the true stakes"},
            {"id": self._cid(self.ally), "name": self.ally,
             "tier": "supporting", "body": ally_body, "outfit": ally_outfit,
             "voice": {"engine": "moss-tts",
                       "style_tag": "warm, quick-witted",
                       "params": {"pitch": 2, "speed": 1.05}},
             "emotion_palette": EMOTION_PALETTES["ally"],
             "arc_summary": "steadfast companion through the storm"},
        ]
        locations = self._locations()
        universe = {
            "genre": self.genre, "tone": self.tone,
            "grade": "cinematic_teal_amber.lut",
            "cinematography": {
                "default_lens": "35mm anamorphic",
                "default_film_stock": "Kodak Vision3 500T",
                "depth_of_field": "deep focus, everything sharp from foreground to background",
                "color_temp_key": 5600, "color_temp_fill": 3200,
                "grain": "fine cinematic grain",
            },
        }
        return {"characters": characters, "crowd_types": self._crowd_types(),
                "locations": locations, "universe": universe}

    def _stage_bible_from_cast(self) -> dict:
        """Bible stage from cast_config: characters/locations verbatim
        (lora/enabled keys survive into the YAML), crowd empty."""
        characters = []
        for c in self.cast_config.get("characters", []):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            ch = dict(c)
            ch.setdefault("name", ch["id"])
            if "voice" not in ch:
                ch["voice"] = {"engine": "moss-tts", "style_tag": "",
                               "params": {}}
            ch.setdefault("arc_summary", "")
            ch.setdefault("enabled", True)
            characters.append(ch)
        universe = {
            "genre": self.genre, "tone": self.tone,
            "grade": "cinematic_teal_amber.lut",
            "cinematography": {
                "default_lens": "35mm anamorphic",
                "default_film_stock": "Kodak Vision3 500T",
                "depth_of_field": "deep focus, everything sharp from foreground to background",
                "color_temp_key": 5600, "color_temp_fill": 3200,
                "grain": "fine cinematic grain",
            },
        }
        return {"characters": characters,
                "crowd_types": self._crowd_types(),
                "locations": self._locations(), "universe": universe}

    def _stage_beats(self) -> dict:
        locs = self._locations()
        mid = self.n_scenes // 2
        beats = []
        for i in range(self.n_scenes):
            if self.cast_config is not None:
                loc = locs[i % len(locs)]
            else:
                loc = locs[min(i, len(locs) - 1)]
            if i == 0:
                goal, turn, hook = ("the attack begins", "the power awakens",
                                    "what is she?")
                mood = "panic, awe"
            elif i == mid:
                goal, turn, hook = ("confront the invader", "the demand is "
                                    "made", "the power has a price")
                mood = "confrontation"
            elif i == self.n_scenes - 1:
                goal, turn, hook = ("the final stand", "full awakening",
                                    "a new dawn")
                mood = "cataclysm, hope"
            else:
                goal, turn, hook = ("regroup and plan", "a costly lesson",
                                    "the enemy moves")
                mood = "tension"
            beats.append({
                "scene": f"s{i + 1:02d}", "location": loc["id"],
                "time": "night" if i % 2 == 0 else "dusk",
                "mood": mood, "goal": goal, "turn": turn, "hook": hook,
                "est_shots": 0,
            })
        # distribute shots (heavier middle; ensure one scene >= 7 shots so
        # the linter's per-scene hero budget (<=15%) can host a hero)
        weights = [1] * self.n_scenes
        weights[mid] = weights[mid] + 1
        if self.n_scenes == 2 and self.n_shots >= 9:
            dist = [self.n_shots - 7, 7]
        else:
            total_w = sum(weights)
            dist = [int(self.n_shots * w / total_w) for w in weights]
            dist[mid] += self.n_shots - sum(dist)
        for b, n in zip(beats, dist):
            b["est_shots"] = max(2, n)
        return {"beats": beats}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _cid(name: str) -> str:
        return "".join(c.lower() if c.isalnum() else "_" for c in name)

    @staticmethod
    def _norm(s: str) -> str:
        """Lower-case, punctuation to spaces — for keyword matching."""
        return "".join(c.lower() if c.isalnum() else " "
                       for c in str(s or "")).strip()

    def _char(self, cid: str, tier: str, emotion: str, action: str,
              position: str, facing: str) -> dict:
        return {"id": cid, "tier": tier, "emotion": emotion,
                "action": action, "position": position, "facing": facing}

    _WEAPON_WORDS = ("fight", "battle", "attack", "strike", "weapon", "blade",
                     "spear", "sword", "draw", "charge", "duel", "confront",
                     "threaten", "hunt", "kill", "defend", "guard", "arena",
                     "combat", "clash", "slash", "stab", "wield")

    def _assign_weapon(self, cobj: dict, beat: dict, i: int,
                       has_power: bool = False, force: bool = False) -> None:
        """Give the character their weapon on combat/threat beats.

        Deterministic path only: the weapon config comes from the cast; the
        handling cycles (draw -> use -> rest) so a scene does not repeat the
        same pose. The resolver then passes the loadout composite still and
        (H3) the weapon design extra, and the prompt describes both.
        """
        cid = str(cobj.get("id") or "")
        char = None
        if self.cast_config:
            char = next((c for c in (self.cast_config.get("characters") or [])
                         if str(c.get("id")) == cid), None)
        if char is None:
            try:
                char = (self.bible.characters() or {}).get(cid)
            except Exception:  # noqa: BLE001
                char = None
        weapons = (char or {}).get("weapons") or {}
        if not weapons:
            return
        hay = self._norm(" ".join([str(beat.get("goal") or ""),
                                   str(beat.get("turn") or ""),
                                   str(beat.get("hook") or ""),
                                   str(cobj.get("action") or "")]))
        if not (force or has_power
                or any(w in hay for w in self._WEAPON_WORDS)):
            return
        cobj["weapon"] = next(iter(weapons))
        handling = ["held at side", "two-handed grip", "raised overhead",
                    "mid-swing", "planted in ground", "channeling"]
        cobj["handling"] = handling[(i - 1) % len(handling)]

    def _scene_chars(self, beat: dict, scene_idx: int) -> list[dict]:
        """Which characters appear in a scene's shots."""
        if self.cast_config is not None:
            chars = []
            if self.prot:
                chars.append(self._char_id(self.prot))
            if scene_idx >= 1 and self.antag:
                chars.append(self._char_id(self.antag))
            if scene_idx in (1, self.n_scenes - 2) and self.ally:
                chars.append(self._char_id(self.ally))
            return chars
        chars = [self._cid(self.prot)]
        if scene_idx >= 1:
            chars.append(self._cid(self.antag))
        if scene_idx in (1, self.n_scenes - 2):
            chars.append(self._cid(self.ally))
        return chars

    def _location_variant_tag(self, loc_id: str, scene_id: str,
                              beat: dict) -> str:
        """Environment variant tag for a scene (power of the reference system).

        Priority: the location's explicit `scene_tags` map (set from the Cast
        tab), then a text match of the tagged images against the beat's
        location / goal / turn wording, else "" (primary image — the historical
        behaviour, so untagged projects are unaffected).
        """
        try:
            loc = (self.bible.locations() or {}).get(loc_id) or {}
        except Exception:
            return ""
        smap = loc.get("scene_tags") or {}
        tag = str(smap.get(scene_id) or "").strip()
        if tag:
            return tag
        tags: list[str] = []
        for r in (loc.get("reference_images") or []):
            if isinstance(r, dict) and r.get("tag"):
                tags.append(str(r["tag"]).strip())
        if not tags:
            return ""

        norm = self._norm
        hay = norm(" ".join([loc_id, str(beat.get("goal") or ""),
                             str(beat.get("turn") or ""),
                             str(beat.get("hook") or ""),
                             str(beat.get("mood") or "")]))
        for t in tags:
            nt = norm(t)
            if nt and nt in hay:
                return t
        return ""

    def _stage_scenes(self) -> dict:
        beats = self._stage_beats()["beats"]
        scenes = []
        for i, b in enumerate(beats):
            scenes.append({
                "id": b["scene"], "location": b["location"],
                "time": b["time"], "beats": [b["goal"], b["turn"]],
                "emotion_arc": "calm -> tense -> resolve",
                "music_mood": b["mood"], "shot_count": b["est_shots"],
            })
        return {"scenes": scenes}

    # -------------------------------------------------------------- shots
    def _stage_shots(self) -> dict:
        beats = self._stage_beats()["beats"]
        prot = self._char_id(self.prot)
        antag = self._char_id(self.antag) if self.antag else None
        ally = self._char_id(self.ally) if self.ally else None
        crowd_ids = sorted(self._crowd_types().keys())
        locs = self._locations()
        loc_by_id = {l["id"]: l for l in locs}
        pd = POWER_DESCRIPTORS.get(self.power, POWER_DESCRIPTORS["arcane"])

        n_hero = max(1, min(int(self.n_shots * 0.15), len(beats)))
        n_vol = max(int(self.n_shots * 0.35), 1)
        # per-scene hero allowance (linter: hero <= 15% per scene)
        hero_allow = {b["scene"]: int(b["est_shots"] * 0.15) for b in beats}
        # hero placement priority: scene1 opener, midpoint climax, finale
        mid = self.n_scenes // 2
        priority = [(0, 1), (mid, max(2, beats[mid]["est_shots"] // 2)),
                    (self.n_scenes - 1, max(1, beats[-1]["est_shots"] - 1))]
        hero_spots: list[tuple[str, int]] = []
        for bi, si in priority:
            if len(hero_spots) >= n_hero:
                break
            scene = beats[bi]["scene"]
            if hero_allow.get(scene, 0) < 1:
                continue
            shot_i = max(1, min(si, beats[bi]["est_shots"]))
            hero_spots.append((scene, shot_i))
            hero_allow[scene] -= 1
        hero_set = set(hero_spots)

        # emotion arc per scene (gradual ladders)
        ladder = ["calm", "focused", "tense", "fearful", "determined",
                  "grief", "serene"]
        antag_ladder = ["stern", "cold", "anger", "fear", "respect", "amused"]

        shots: list[dict] = []
        used_power = False
        mutation_done = {"wound": False, "weather": False}
        for bi, b in enumerate(beats):
            sid = b["scene"]
            n = b["est_shots"]
            chars_here = self._scene_chars(b, bi)
            for i in range(1, n + 1):
                prev = None if i == 1 else f"{sid}_{i - 1:03d}"
                is_hero = (sid, i) in hero_set
                # emotion: gradual walk along the ladder
                if self.cast_config is not None:
                    emo = self._cast_emo(prot, i, n)
                    a_emo = self._cast_emo(antag, i, n) if antag else "calm"
                else:
                    e_idx = min(len(ladder) - 1,
                                round((i / max(1, n - 1)) *
                                      (len(ladder) - 1))
                                if n > 1 else 1)
                    emo = ladder[max(0, e_idx - 1)] \
                        if bi < self.n_scenes - 1 \
                        else ladder[min(len(ladder) - 1, e_idx)]
                    a_emo = antag_ladder[min(len(antag_ladder) - 1,
                                             round((i / max(1, n - 1)) *
                                                   (len(antag_ladder) - 1))
                                             if n > 1 else 0)]

                if self.cast_config is not None:
                    char_list = []
                    if prot in chars_here:
                        pos = "foreground_center" if i <= 2 or is_hero \
                            else self.rng.choice(
                                ["midground_center", "midground_left"])
                        fac = "camera" if is_hero else self.rng.choice(
                            ["camera_left", "camera"])
                        pc = self._char(
                            prot, "main", emo,
                            self._action_for(is_hero, self.power, i, n),
                            pos, fac)
                        self._assign_weapon(pc, b, i, has_power=is_hero)
                        char_list.append(pc)
                    if antag and antag in chars_here \
                            and i >= max(2, n // 3):
                        ac = self._char(
                            antag, "supporting", a_emo,
                            "advances slowly, hand on weapon",
                            "midground_right", "camera_left")
                        self._assign_weapon(ac, b, i, force=True)
                        char_list.append(ac)
                    if ally and ally in chars_here \
                            and i >= max(2, n // 3):
                        char_list.append(self._char(
                            ally, "supporting", self._cast_emo(ally, i, n),
                            "keeps to cover, watching the approach",
                            "background_left", "camera_right"))
                else:
                    char_list = []
                    if prot in chars_here:
                        pos = "foreground_center" if i <= 2 or is_hero \
                            else self.rng.choice(
                                ["midground_center", "midground_left"])
                        fac = "camera" if is_hero else self.rng.choice(
                            ["camera_left", "camera"])
                        pc = self._char(
                            prot, "main", emo,
                            self._action_for(is_hero, self.power, i, n),
                            pos, fac)
                        self._assign_weapon(pc, b, i, has_power=is_hero)
                        char_list.append(pc)
                    if antag in chars_here and i >= max(2, n // 3):
                        ac = self._char(
                            antag, "supporting", a_emo,
                            "advances slowly, hand on weapon",
                            "midground_right", "camera_left")
                        self._assign_weapon(ac, b, i, force=True)
                        char_list.append(ac)
                    if ally in chars_here and i >= max(2, n // 3):
                        char_list.append(self._char(
                            ally, "supporting", "wary",
                            "keeps to cover, watching the approach",
                            "background_left", "camera_right"))

                has_power = (is_hero and bool(self.power)) \
                    if self.cast_config is not None else is_hero
                size = ("close_up" if i == 1 and bi == 0 else
                        self.rng.choice(["close_up", "medium_close_up"])
                        if is_hero else
                        self.rng.choice(["medium", "medium_wide", "wide"]))
                cam = ("slow_push_in" if is_hero else
                       self.rng.choice(CAMERAS))
                tier = "hero" if is_hero else "volume" \
                    if size in ("wide", "extreme_wide") else "standard"
                continuity = "CUT"
                if i > 1:
                    if size in ("wide", "extreme_wide") and \
                            (i - 1) % 4 == 0 and b["est_shots"] > 4:
                        continuity = "CONTINUE"
                    elif bi >= self.n_scenes // 2 and i in (2, 3) \
                            and tier == "standard":
                        continuity = "MATCH_ACTION"

                muts = []
                if bi == self.n_scenes // 2 and i == 2 \
                        and not mutation_done["wound"]:
                    muts.append({"target": prot, "key": "wound_right_arm",
                                 "value": "shallow cut", "persist": True})
                    mutation_done["wound"] = True
                if bi == 0 and i == 1 and not mutation_done["weather"]:
                    muts.append({"target": b["location"],
                                 "key": "weather", "value": "gathering storm",
                                 "persist": True})
                    mutation_done["weather"] = True

                start_state, end_state = {}, {}
                if mutation_done["wound"] and bi > self.n_scenes // 2:
                    start_state = {"wound_right_arm": "bandaged wound"}
                if has_power:
                    end_state = {"power": "flaring"}

                if self.cast_config is not None:
                    dialogue = []
                    if tier == "standard" and bi == self.n_scenes // 2 \
                            and i == 2 and antag:
                        pal = self._cast_palette.get(antag) or []
                        dialogue = [{"char": antag,
                                     "line": "Give me the power. Or watch "
                                             "everything burn.",
                                     "emotion": pal[0] if pal else "calm"}]
                    elif tier == "standard" and bi == 0 and i == 2:
                        pal = self._cast_palette.get(prot) or []
                        dialogue = [{"char": prot,
                                     "line": "What is happening to me?",
                                     "emotion": pal[0] if pal else "calm"}]
                else:
                    dialogue = []
                    if tier == "standard" and bi == self.n_scenes // 2 \
                            and i == 2:
                        dialogue = [{"char": antag,
                                     "line": "Give me the power. Or watch "
                                             "everything burn.",
                                     "emotion": "cold"}]
                    elif tier == "standard" and bi == 0 and i == 2:
                        dialogue = [{"char": prot,
                                     "line": "What is happening to me?",
                                     "emotion": "fearful"}]

                action_text = self._primary_action(
                    prot, emo, has_power, self.power)
                # power intensity curve (1..10 across the film, finale = 10):
                # the renderer maps it to the power's reference image whose
                # level is the highest <= this value (resolver floor rule).
                power_level = None
                if has_power:
                    denom = max(1, self.n_scenes - 1)
                    power_level = max(1, min(10, round(1 + 9 * (bi / denom))))
                location_tag = self._location_variant_tag(b["location"], sid, b)
                shot = {
                    "shot_id": f"{sid}_{i:03d}", "scene": sid,
                    "project_id": self.pid,
                    "location": b["location"], "time": b["time"],
                    "characters": char_list,
                    "power": self.power if has_power else None,
                    "power_level": power_level,
                    "location_tag": location_tag,
                    "camera": cam, "shot_size": size,
                    "duration_s": 5, "tier": tier,
                    "dialogue": dialogue,
                    "prev_shot": prev, "state_mutations": muts,
                    "visible_background": [crowd_ids[0]]
                    if crowd_ids and size in ("wide", "extreme_wide") else [],
                    "weather": "inherit",
                    # --- ShotSpec ---
                    "narrative": b["goal"],
                    "beat": b["turn"],
                    "wardrobe": "",
                    "props": [],
                    "start_state": start_state,
                    "end_state": end_state,
                    "primary_action": action_text,
                    "secondary_motion": "" if not has_power else
                    f"{pd['motion']}",
                    "continuity": continuity,
                    "keyframe_source": "auto",
                    "selected_loras": [],
                    "expected_visible": [c["id"] for c in char_list],
                    "composition": "" if not is_hero else
                    "subject centered, deep focus",
                    "lens": "" if not is_hero else "85mm close-up lens",
                    "spec_version": 1,
                }
                if has_power:
                    used_power = True
                shots.append(shot)
        # tier budget correction pass (hero<=15%, volume>=30%)
        shots = self._rebalance_tiers(shots)
        return {"shots": shots}

    def _action_for(self, is_hero: bool, power: str, i: int,
                    n: int) -> str:
        if is_hero:
            return (f"raises both hands as {power or 'power'} gathers in "
                    f"glowing wisps around her palms")
        return PRIMARY_ACTIONS["raise"] if i == 1 else \
            PRIMARY_ACTIONS[self.rng.choice(
                ["stand", "step", "turn", "look", "walk", "gaze", "reach"])]

    def _primary_action(self, prot: str, emo: str, has_power: bool,
                        power: str) -> str:
        if has_power:
            return (f"raises both hands and unleashes a surge of "
                    f"{power or 'power'} that floods the frame")
        return self.rng.choice([
            "steps forward deliberately, eyes locked on the threat",
            "turns slowly, scanning the scene with growing dread",
            "reaches out a steadying hand, breathing hard",
            "walks through the scene, the weight of the moment visible",
            "glances back over one shoulder, then keeps moving",
        ])

    def _rebalance_tiers(self, shots: list[dict]) -> list[dict]:
        total = len(shots)
        heroes = [s for s in shots if s["tier"] == "hero"]
        hero_cap = max(1, int(total * 0.15))
        if len(heroes) > hero_cap:
            for s in heroes[hero_cap:]:
                s["tier"] = "standard"
                s["power"] = None
                s["secondary_motion"] = ""
                s["end_state"] = {}
        vols = [s for s in shots if s["tier"] == "volume"]
        need = max(0, int(total * 0.30) - len(vols))
        if need:
            for s in shots:
                if need <= 0:
                    break
                if s["tier"] == "standard" and \
                        s["shot_size"] in ("wide", "extreme_wide", "medium_wide"):
                    s["tier"] = "volume"
                    need -= 1
        return shots

    # ------------------------------------------------------------ pipeline
    def generate(self, stages=None, idea=None, cast_config=None) -> dict:
        """Generate stages end-to-end. Writes bible files, scene YAMLs,
        stages into story_versions, and ingests shots into the queue.
        cast_config (optional) replaces the generated character/location
        pools — every stage derives from the provided cast."""
        if idea:
            self.idea = idea
        if cast_config and isinstance(cast_config, dict) and \
                cast_config.get("characters"):
            self.cast_config = cast_config
        stages = stages or list(STAGES)
        done: dict[str, Any] = {}
        try:
            self._ensure_ctx()
            for stage in stages:
                content = self.generate_stage(stage)
                if stage == "bible":
                    self._write_bible(content)
                elif stage == "scenes":
                    self._write_scenes(content["scenes"])
                self.store.set_story_stage(self.pid, stage, content)
                done[stage] = content
            ingested = 0
            if "shots" in done:
                shots = done["shots"].get("shots", [])
                self.db_upsert(shots)
                ingested = len(shots)
            self.store.set_status(self.pid, "story_ready", stage="scenes")
            return {"ok": True, "stages": done, "shots_ingested": ingested}
        except Exception as e:  # noqa: BLE001
            log.exception("storygen failed for %s", self.pid)
            return {"ok": False, "error": str(e),
                    "stages_completed": list(done.keys())}

    def _write_bible(self, content: dict) -> None:
        b = self.bible
        # NON-DESTRUCTIVE MERGE: the AI's bible section carries only text
        # fields (name/tier/body/outfit/powers/emotions/voice/arc). It must
        # NEVER wipe what the Cast tab owns — weapons, loadouts, reference
        # images, scene maps, LoRAs, enabled flags. The old wholesale
        # save_character() destroyed all of that on every story import.
        for c in content.get("characters") or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            prev = (b.characters() or {}).get(str(c["id"])) or {}
            merged = dict(prev)
            for k in ("name", "tier", "body", "outfit", "powers",
                      "emotion_palette", "voice", "arc_summary"):
                if c.get(k) is not None:
                    merged[k] = c[k]
            merged.setdefault("id", str(c["id"]))
            merged.setdefault("enabled", True)
            merged.setdefault("voice", {"engine": "moss-tts",
                                        "style_tag": "", "params": {}})
            b.save_character(merged)
        ct = b.crowd_types()
        ct.update(content.get("crowd_types") or {})
        b.save_crowd_types(ct)
        for loc in content.get("locations") or []:
            if not isinstance(loc, dict) or not loc.get("id"):
                continue
            prev = (b.locations() or {}).get(str(loc["id"])) or {}
            merged = dict(prev)
            for k in ("descriptor", "lighting"):
                if loc.get(k) is not None:
                    merged[k] = loc[k]
            merged.setdefault("id", str(loc["id"]))
            merged.setdefault("enabled", True)
            b.save_location(merged)
        uni = content.get("universe")
        if isinstance(uni, dict) and uni:
            cur = b.universe()
            cur.update(uni)
            b.save_universe(cur)

    def _write_scenes(self, scenes: list[dict]) -> None:
        for s in scenes:
            self.bible.save_scene(s)

    def db_upsert(self, shots: list[dict]) -> dict:
        from shots_db import ShotsDB
        # the store was constructed with a ShotsDB instance; reuse its con
        db = self.store.db
        if not hasattr(db, "upsert_shots"):
            raise RuntimeError("store.db is not a ShotsDB")
        return db.upsert_shots(shots)

    # ---------------------------------------------------------- import path
    def import_stage(self, stage: str, content: dict) -> dict:
        """External provider (AI agent/LLM) import per stage."""
        if stage not in STAGES:
            return {"ok": False, "error": f"unknown stage {stage!r}"}
        if stage == "shots":
            shots = content.get("shots", [])
            if not shots:
                return {"ok": False, "error": "no shots in content"}
            from linter import Linter
            rep = Linter(self.bible).lint_package(shots)
            if not rep.ok:
                return {"ok": False, "error": "lint failed",
                        "report": rep.to_dict()}
            for s in shots:
                s["project_id"] = self.pid
            self.db_upsert(shots)
        if stage == "bible" and "characters" in content:
            self._write_bible(content)
        if stage == "scenes" and "scenes" in content:
            self._write_scenes(content["scenes"])
        self.store.set_story_stage(self.pid, stage, content)
        return {"ok": True, "stage": stage}

    def current_story(self) -> dict:
        proj = self.store.get(self.pid)
        story = proj.get("story") if proj else {}
        return story if isinstance(story, dict) else {}
