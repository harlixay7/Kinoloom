"""Structured cinematic prompt engine (the PromptSpec system).

Replaces fixed prompt templates with structured internal blocks:

    Identity -> Design -> Performance -> Wardrobe -> Shot -> Scene
    -> Lighting -> VFX -> Realism

Adapters convert the spec into final model prompts (the existing model
adapters in models.py stay on top: quality prefixes, negatives, LoRA
guards — unchanged). Three modes per generation:
    structured  (auto from spec blocks)
    add         (structured + user instructions appended)
    override    (raw user prompt)

Two intents:
    training   (clean minimal styling for LoRA/reference datasets)
    cinematic  (full stylized film look)

Legacy params (old wizard shapes) are translated into specs — full
backward compatibility: existing batches, meta and jobs keep working.
"""
from __future__ import annotations

from typing import Any

# ============================================================== catalogs

# ---- shot system (replaces "angle") -------------------------------
SHOTS: dict[str, dict] = {
    "close-up": {"framing": "extreme close-up on the face", "angle": "at eye level",
                 "composition": "the face fills the frame", "lens": "85mm"},
    "portrait": {"framing": "head-and-shoulders portrait", "angle": "at eye level",
                 "composition": "subject centered, clean background", "lens": "85mm"},
    "medium": {"framing": "medium shot from the waist up", "angle": "at eye level",
               "composition": "subject in frame with headroom", "lens": "50mm"},
    "3/4 body": {"framing": "three-quarter body shot", "angle": "slight low angle",
                 "composition": "subject fully framed with margin", "lens": "35mm"},
    "full body": {"framing": "full body shot", "angle": "at chest level",
                  "composition": "full figure visible head to feet", "lens": "35mm"},
    "wide": {"framing": "wide establishing shot", "angle": "at eye level",
             "composition": "subject small within the environment", "lens": "24mm"},
    "low angle hero": {"framing": "hero shot", "angle": "low angle looking up",
                       "composition": "subject towering, dramatic scale", "lens": "35mm"},
    "high angle": {"framing": "high angle shot", "angle": "looking down",
                   "composition": "subject seen from above", "lens": "50mm"},
    "profile": {"framing": "profile shot", "angle": "true side profile",
                "composition": "profile silhouette against the background", "lens": "85mm"},
    "close-up detail": {"framing": "tight detail shot", "angle": "at subject level",
                        "composition": "the detail fills the frame, background dark and simple", "lens": "100mm macro"},
    "over-the-shoulder": {"framing": "over-the-shoulder shot", "angle": "behind the subject",
                          "composition": "shoulder in the foreground, face in focus", "lens": "50mm"},
}

# ---- identity core (compact, fixed) --------------------------------
AGES = ["young adult", "mid-20s", "mid-30s", "40s", "mature"]
PRESENTATIONS = ["woman", "man"]
HAIR_STYLES = ["short", "medium-length", "long", "curly", "wavy", "straight",
               "braided", "bun", "ponytail", "buzz cut", "bald", "flowing"]
HAIR_COLORS = ["black", "dark brown", "chestnut brown", "auburn", "blonde",
               "silver-white", "grey", "red", "platinum", "raven"]
EYE_COLORS = ["dark brown", "hazel", "amber", "steel blue", "ice blue",
              "green", "grey", "emerald", "violet"]
EYE_SHAPES = ["almond", "round", "hooded", "narrow", "deep-set", "wide"]
SKIN_TONES = ["fair porcelain", "fair with light freckles", "light olive",
              "olive", "warm tan", "deep bronze", "rich brown", "deep brown"]
FACE_STRUCTURES = ["oval", "heart-shaped", "angular", "soft round",
                   "square jaw", "delicate features", "strong features"]
BODY_TYPES = ["slender", "lean", "athletic", "curvy", "statuesque",
              "broad-shouldered", "wiry", "imposing"]
MARKS = ["none", "a thin scar across the left brow", "a faded burn scar on the neck",
         "a small mole on the right cheek", "a scar across the cheek",
         "faint freckles", "a silver ring in the left ear"]

# ---- character design ----------------------------------------------
LINEAGES = ["human", "elf", "half-elf", "fae", "vampire", "celestial",
            "dark elf", "dragonkin", "half-fae"]
FACTIONS = ["royal house", "order of knights", "arcane circle", "dark court",
            "free city", "holy order", "nomad clan", "shadow guild"]
PALETTES = ["ivory, rose gold and pale blue", "crimson, black and gold",
            "midnight blue and silver", "emerald and bronze",
            "pearl, lavender and gold", "charcoal and amber",
            "snow white and ice blue", "royal purple and gold"]
WARDROBE_MATERIALS = ["silk", "velvet", "brocade", "satin", "linen", "wool",
                      "leather", "studded leather", "chainmail", "plate armor",
                      "gilded plate armor", "rune-etched armor", "enchanted silk"]
WARDROBE_SILHOUETTES = ["flowing gown", "ball gown", "fitted dress", "riding coat",
                        "cloak over simple clothes", "robes", "armored cuirass over tunic",
                        "full plate armor", "leather armor", "royal coronation robe",
                        "hunting attire", "battle dress"]
WARDROBE_COLORS = ["ivory", "deep crimson", "midnight blue", "forest green",
                   "royal purple", "black", "silver-grey", "gold", "rose",
                   "snow white", "emerald"]
WARDROBE_DETAILS = ["gold embroidery", "silver trim", "pearl beading",
                    "gemstone inlay", "fur lining", "intricate lace",
                    "battle-worn stitching", "flowing train", "high collar",
                    "ornate clasps", "none"]
LENSES = ["24mm", "35mm", "50mm", "85mm", "135mm"]
SCENE_LOCATIONS = ["", "the grand entrance", "a window alcove", "the central dais",
                   "beside the throne", "a balcony edge", "the courtyard steps",
                   "before the main doors", "the garden path", "an arched corridor"]
HEADWEAR = ["none", "tiara", "crown", "circlet", "hood", "veil", "helmet",
            "ornate headpiece", "battle helm"]
JEWELRY = ["none", "gold jewelry", "silver jewelry", "pearl jewelry",
           "ruby and gold", "emerald and silver", "sapphire and gold",
           "rune-carved jewelry"]
WARDROBE_CONDITIONS = ["pristine", "battle-worn", "travel-worn", "ceremonial",
                       "weathered", "gilded"]
# Precomposed wardrobe sets for the cycle axis (§6: wardrobe should cycle
# like scene/lighting — never redefine identity)
WARDROBE_PRESETS: dict[str, dict] = {
    "gown — silk & pearls": {"silhouette": "flowing gown", "materials": ["silk", "pearl beading"],
                             "color": "ivory", "jewelry": "pearl jewelry", "headwear": "tiara",
                             "detail": "pearl beading", "condition": "pristine"},
    "gown — velvet & gold": {"silhouette": "ball gown", "materials": ["velvet", "brocade", "gold"],
                             "color": "royal purple", "jewelry": "gold jewelry", "headwear": "circlet",
                             "detail": "gold embroidery", "condition": "ceremonial"},
    "royal armor": {"silhouette": "full plate armor", "materials": ["gilded plate armor", "chainmail"],
                    "color": "gold", "jewelry": "none", "headwear": "battle helm",
                    "detail": "gold embroidery", "condition": "battle-worn"},
    "leather adventurer": {"silhouette": "leather armor", "materials": ["leather", "linen"],
                           "color": "deep crimson", "jewelry": "silver jewelry", "headwear": "hood",
                           "detail": "battle-worn stitching", "condition": "travel-worn"},
    "mage robes": {"silhouette": "robes", "materials": ["wool", "linen", "enchanted silk"],
                   "color": "midnight blue", "jewelry": "rune-carved jewelry", "headwear": "hood",
                   "detail": "ornate clasps", "condition": "travel-worn"},
    "hunting attire": {"silhouette": "hunting attire", "materials": ["leather", "wool"],
                       "color": "forest green", "jewelry": "none", "headwear": "none",
                       "detail": "fur lining", "condition": "pristine"},
    "coronation robe": {"silhouette": "royal coronation robe", "materials": ["brocade", "gold"],
                        "color": "royal purple", "jewelry": "ruby and gold", "headwear": "crown",
                        "detail": "gold embroidery", "condition": "ceremonial"},
    "battle dress": {"silhouette": "battle dress", "materials": ["leather", "chainmail", "silk"],
                     "color": "deep crimson", "jewelry": "silver jewelry", "headwear": "circlet",
                     "detail": "ornate clasps", "condition": "battle-worn"},
    # ---- expanded set (creative / glamour / fantasy): the wardrobe matrix
    # generates one composite per outfit so every look has its own reference
    "evening gown — high slit": {"silhouette": "silk evening gown with a high slit",
                                 "materials": ["silk", "satin"], "color": "emerald green",
                                 "jewelry": "diamond jewelry", "headwear": "none",
                                 "detail": "side slit", "condition": "pristine"},
    "cocktail dress": {"silhouette": "fitted cocktail dress",
                       "materials": ["satin", "lace"], "color": "deep crimson",
                       "jewelry": "gold jewelry", "headwear": "none",
                       "detail": "sequin trim", "condition": "pristine"},
    "red carpet gown": {"silhouette": "floor-length red carpet gown",
                        "materials": ["silk", "satin"], "color": "scarlet",
                        "jewelry": "diamond jewelry", "headwear": "none",
                        "detail": "open back", "condition": "pristine"},
    "cocktail — black satin": {"silhouette": "black satin slip dress",
                               "materials": ["satin"], "color": "black",
                               "jewelry": "silver jewelry", "headwear": "none",
                               "detail": "cowl neckline", "condition": "pristine"},
    "lingerie — black lace": {"silhouette": "black lace lingerie set",
                              "materials": ["lace", "silk"], "color": "black",
                              "jewelry": "none", "headwear": "none",
                              "detail": "lace trim", "condition": "pristine"},
    "lingerie — ivory silk": {"silhouette": "ivory silk lingerie set",
                              "materials": ["silk", "lace"], "color": "ivory",
                              "jewelry": "none", "headwear": "none",
                              "detail": "delicate lace", "condition": "pristine"},
    "nightgown — sheer": {"silhouette": "sheer flowing nightgown",
                          "materials": ["sheer chiffon", "silk"], "color": "pale blue",
                          "jewelry": "none", "headwear": "none",
                          "detail": "soft draping", "condition": "pristine"},
    "sundress — light": {"silhouette": "light summer sundress",
                         "materials": ["linen", "cotton"], "color": "soft yellow",
                         "jewelry": "none", "headwear": "sun hat",
                         "detail": "floral print", "condition": "pristine"},
    "bikini — summer": {"silhouette": "summer bikini",
                        "materials": ["satin"], "color": "turquoise",
                        "jewelry": "none", "headwear": "none",
                        "detail": "gold clasps", "condition": "pristine"},
    "swimwear — one-piece": {"silhouette": "sleek one-piece swimsuit",
                             "materials": ["satin"], "color": "midnight blue",
                             "jewelry": "none", "headwear": "none",
                             "detail": "open back", "condition": "pristine"},
    "corset & skirt": {"silhouette": "structured corset with a flowing skirt",
                       "materials": ["brocade", "satin"], "color": "deep burgundy",
                       "jewelry": "gold jewelry", "headwear": "none",
                       "detail": "corset lacing", "condition": "pristine"},
    "bodysuit — sleek": {"silhouette": "sleek fitted bodysuit",
                         "materials": ["spandex", "leather"], "color": "black",
                         "jewelry": "silver jewelry", "headwear": "none",
                         "detail": "high collar", "condition": "pristine"},
    "shirt — unbuttoned": {"silhouette": "loose white shirt unbuttoned at the collar",
                           "materials": ["linen"], "color": "white",
                           "jewelry": "none", "headwear": "none",
                           "detail": "rolled sleeves", "condition": "pristine"},
    "valkyrie armor": {"silhouette": "valkyrie battle armor",
                       "materials": ["silvered steel", "leather", "feathers"],
                       "color": "silver", "jewelry": "none", "headwear": "winged helm",
                       "detail": "feather mantle", "condition": "battle-worn"},
    "sorceress gown": {"silhouette": "dark sorceress gown",
                       "materials": ["velvet", "enchanted silk"], "color": "deep purple",
                       "jewelry": "rune-carved jewelry", "headwear": "hood",
                       "detail": "silver sigils", "condition": "pristine"},
    "gothic victorian": {"silhouette": "victorian gothic dress",
                         "materials": ["velvet", "lace"], "color": "black",
                         "jewelry": "silver jewelry", "headwear": "veil",
                         "detail": "corset boning", "condition": "pristine"},
    "pirate coat": {"silhouette": "long pirate captain coat",
                    "materials": ["wool", "leather"], "color": "deep crimson",
                    "jewelry": "gold jewelry", "headwear": "tricorn hat",
                    "detail": "gold buttons", "condition": "weathered"},
    "steampunk corset": {"silhouette": "steampunk corset and skirt",
                         "materials": ["leather", "brass"], "color": "chestnut brown",
                         "jewelry": "brass jewelry", "headwear": "goggles",
                         "detail": "gear clasps", "condition": "pristine"},
    "assassin leathers": {"silhouette": "fitted assassin leathers",
                          "materials": ["leather", "silk"], "color": "charcoal",
                          "jewelry": "none", "headwear": "hood",
                          "detail": "dagger harness", "condition": "travel-worn"},
    "barbarian furs": {"silhouette": "barbarian furs and leather wraps",
                       "materials": ["fur", "leather"], "color": "chestnut brown",
                       "jewelry": "bone jewelry", "headwear": "none",
                       "detail": "fur mantle", "condition": "weathered"},
    "military dress uniform": {"silhouette": "military dress uniform",
                               "materials": ["wool", "gold"], "color": "midnight blue",
                               "jewelry": "none", "headwear": "peaked cap",
                               "detail": "gold epaulettes", "condition": "pristine"},
    "wedding gown": {"silhouette": "white wedding gown",
                     "materials": ["silk", "lace"], "color": "white",
                     "jewelry": "pearl jewelry", "headwear": "veil",
                     "detail": "lace overlay", "condition": "pristine"},
    "ritual robes — dark": {"silhouette": "dark ritual robes",
                            "materials": ["wool", "silk"], "color": "black",
                            "jewelry": "none", "headwear": "deep hood",
                            "detail": "embroidered sigils", "condition": "ancient"},
}
# ---- weapons (per-character config; feeds text + the weapon batch) ----
# The weapon catalog is prompt language first: each entry expands into the
# descriptor the image/video models read. Loadouts bind one weapon + one
# outfit into a composite still, so a character always costs ONE ref slot.
WEAPON_TYPES: dict[str, dict] = {
    "longsword": {"form": "a straight double-edged longsword",
                  "blade": "a narrow fullered blade", "length": "waist-length"},
    "greatsword": {"form": "a massive two-handed greatsword",
                   "blade": "a broad heavy blade", "length": "shoulder-height"},
    "katana": {"form": "a curved single-edged katana",
               "blade": "a folded steel blade", "length": "waist-length"},
    "rapier": {"form": "a slender thrusting rapier",
               "blade": "a needle-thin blade", "length": "waist-length"},
    "scimitar": {"form": "a curved scimitar",
                 "blade": "a wide crescent blade", "length": "waist-length"},
    "dagger": {"form": "a compact dagger",
               "blade": "a short double-edged blade", "length": "forearm-length"},
    "twin daggers": {"form": "a matched pair of daggers",
                     "blade": "short curved blades", "length": "forearm-length"},
    "spear": {"form": "a two-handed spear",
              "blade": "a leaf-shaped spearhead", "length": "taller than its bearer"},
    "glaive": {"form": "a glaive with a broad crescent head",
               "blade": "a curved cutting head", "length": "taller than its bearer"},
    "halberd": {"form": "a halberd with axe head and spike",
                "blade": "an axe-and-spike head", "length": "taller than its bearer"},
    "battle axe": {"form": "a single-handed battle axe",
                   "blade": "a bearded axe head", "length": "waist-length"},
    "greataxe": {"form": "a two-handed greataxe",
                 "blade": "a vast double-bit head", "length": "shoulder-height"},
    "war hammer": {"form": "a war hammer",
                   "blade": "a heavy squared head", "length": "waist-length"},
    "mace": {"form": "a flanged mace",
             "blade": "a flanged striking head", "length": "waist-length"},
    "bow": {"form": "a recurve bow",
            "blade": "smooth limbs", "length": "chest-height"},
    "crossbow": {"form": "a heavy crossbow",
                 "blade": "a steel prod", "length": "chest-height"},
    "staff": {"form": "a tall wizard's staff",
              "blade": "a crystal-set head", "length": "taller than its bearer"},
    "scepter": {"form": "a royal scepter",
                "blade": "an orb-and-crown head", "length": "waist-length"},
    "orb focus": {"form": "a floating orb focus",
                  "blade": "a hovering sphere", "length": "fist-sized"},
    "whip": {"form": "a coiled whip",
             "blade": "a tapered lash", "length": "arm-span"},
    "chain blade": {"form": "a segmented chain blade",
                    "blade": "linked razor segments", "length": "arm-span"},
    "gauntlets": {"form": "a pair of armored gauntlets",
                  "blade": "knuckle plates", "length": "fist-sized"},
    "scythe": {"form": "a long-hafted scythe",
               "blade": "a sweeping crescent blade", "length": "taller than its bearer"},
    "trident": {"form": "a three-pronged trident",
                "blade": "three barbed prongs", "length": "taller than its bearer"},
    "custom": {"form": "a custom weapon", "blade": "", "length": ""},
}
WEAPON_MATERIALS = ["forged steel", "blackened iron", "damascus steel",
                    "silvered steel", "gilded steel", "obsidian", "bronze",
                    "bone and iron", "ice crystal", "volcanic glass",
                    "living wood", "rune-etched alloy", "celestial gold"]
WEAPON_STYLES = ["elegant", "brutal", "ornate", "ancient", "royal", "tribal",
                 "arcane", "holy", "cursed", "military", "hunter", "assassin"]
WEAPON_SIZES = ["one-handed", "two-handed", "compact", "oversized", "polearm"]
WEAPON_CONDITIONS = ["pristine", "battle-worn", "notched", "bloodied",
                     "ancient", "reforged", "gilded"]
WEAPON_ORNAMENTS = ["none", "glowing runes along the fuller",
                    "a gemstone pommel", "engraved knotwork",
                    "a wrapped leather grip", "a chain tassel",
                    "winged cross-guard", "serrated edge",
                    "a relic set in the guard", "a tattered banner"]
WEAPON_HANDLING: dict[str, str] = {
    "held at side": "held low at the side, ready",
    "two-handed grip": "gripped with both hands, blade lowered",
    "raised overhead": "raised overhead for a strike",
    "resting on shoulder": "resting across the shoulder",
    "planted in ground": "planted point-down into the ground",
    "sheathed": "sheathed at the hip",
    "mid-swing": "caught mid-swing, motion blur on the blade",
    "aimed": "aimed forward with steady hands",
    "channeling": "held out while energy runs along the blade",
}
# Energy channels: a weapon can carry the character's power (ties into the
# power system's "weapon coating" manifestation and the H3 soundscape).
WEAPON_ENERGIES = ["none", "lightning", "fire", "ice", "arcane", "divine",
                   "shadow", "nature"]


def weapon_txt(block: dict) -> str:
    """The weapon's descriptor line for prompts (always travels as TEXT)."""
    if not block:
        return ""
    t = WEAPON_TYPES.get(str(block.get("type") or ""), WEAPON_TYPES["custom"])
    form = str(t.get("form", "a weapon"))
    parts = [form]
    size = str(block.get("size") or "")
    if size and size not in form:
        parts.insert(0, size)
    style = str(block.get("style") or "")
    if style:
        parts.insert(0, style)
    mat = str(block.get("material") or "")
    if mat:
        parts.append(f"of {mat}")
    if t.get("blade"):
        parts.append(t["blade"])
    if t.get("length"):
        parts.append(t["length"])
    orn = str(block.get("ornament") or "")
    if orn and orn != "none":
        parts.append(orn)
    cond = str(block.get("condition") or "")
    if cond and cond != "pristine":
        parts.append(f"{cond}")
    txt = ", ".join(p for p in parts if p)
    energy = str(block.get("energy") or "none")
    if energy != "none":
        glow = str(block.get("glow") or "").strip() or f"{energy} energy"
        txt += f", {glow} running along the blade"
    color = str(block.get("color") or "").strip()
    if color:
        txt += f", {color} accents"
    return txt


def weapon_still_txt(block: dict) -> str:
    """Clean product-style still for the weapon REFERENCE image.

    Reference models read these at 960x544 (LTX stage 1) and match H3's
    canvas; a centered, isolated, evenly lit weapon transfers far better than
    an in-scene shot.
    """
    txt = weapon_txt(block)
    if not txt:
        txt = "a weapon"
    handling = block.get("handling") or "no hands"
    if isinstance(handling, (list, tuple)):
        handling = handling[0] if handling else "no hands"
    handling = str(handling)
    held = ""
    if handling and handling not in ("no hands", "sheathed"):
        held = f", {WEAPON_HANDLING.get(handling, handling)}"
    return (f"studio reference still of {txt}{held}, the entire weapon "
            f"visible and centered, isolated on a neutral dark studio "
            f"background, dramatic rim lighting, ultra sharp detail, "
            f"photorealistic materials, no people, no text, no watermark")


# ---- outfits (loadout variants; the character batch cycles them) -----
OUTFIT_VARIANTS = ["base outfit", "battle dress", "royal gown", "traveling gear",
                   "formal court attire", "nightwear", "ceremonial robe",
                   "armored plate", "hunting leathers", "commoner clothes",
                   "evening gown — high slit", "cocktail dress",
                   "red carpet gown", "cocktail — black satin",
                   "lingerie — black lace", "lingerie — ivory silk",
                   "nightgown — sheer", "sundress — light", "bikini — summer",
                   "swimwear — one-piece", "corset & skirt", "bodysuit — sleek",
                   "shirt — unbuttoned", "valkyrie armor", "sorceress gown",
                   "gothic victorian", "pirate coat", "steampunk corset",
                   "assassin leathers", "barbarian furs",
                   "military dress uniform", "wedding gown",
                   "ritual robes — dark"]
ANATOMY_DEPTH = {"subtle": "natural soft skin texture, subtle detail",
                 "normal": "natural skin texture, realistic skin",
                 "detailed": "highly detailed skin texture, visible pores, fine skin detail"}
ARCHETYPES: dict[str, dict] = {
    "royal princess": {"lineage": "human", "faction": "royal house",
                       "palette": "ivory, rose gold and pale blue",
                       "materials": ["silk", "velvet", "brocade"],
                       "headwear": "tiara", "jewelry": "pearl jewelry",
                       "armor": "none", "condition": "pristine"},
    "warrior princess": {"lineage": "human", "faction": "royal house",
                         "palette": "crimson, black and gold",
                         "materials": ["leather", "gilded plate armor"],
                         "headwear": "circlet", "jewelry": "gold jewelry",
                         "armor": "gilded plate armor", "condition": "battle-worn"},
    "dark princess": {"lineage": "dark elf", "faction": "dark court",
                      "palette": "charcoal and amber",
                      "materials": ["velvet", "leather", "dark silk"],
                      "headwear": "circlet", "jewelry": "silver jewelry",
                      "armor": "none", "condition": "pristine"},
    "queen": {"lineage": "human", "faction": "royal house",
              "palette": "royal purple and gold",
              "materials": ["velvet", "brocade", "gold"],
              "headwear": "crown", "jewelry": "ruby and gold",
              "armor": "none", "condition": "ceremonial"},
    "elven royal": {"lineage": "elf", "faction": "royal house",
                    "palette": "emerald and bronze",
                    "materials": ["silk", "enchanted silk", "bronze"],
                    "headwear": "circlet", "jewelry": "emerald and silver",
                    "armor": "none", "condition": "pristine"},
    "knight": {"lineage": "human", "faction": "order of knights",
               "palette": "steel grey and silver",
               "materials": ["plate armor", "chainmail"],
               "headwear": "helm", "jewelry": "none",
               "armor": "plate armor", "condition": "battle-worn"},
    "mage": {"lineage": "human", "faction": "arcane circle",
             "palette": "midnight blue and silver",
             "materials": ["wool", "linen", "enchanted silk"],
             "headwear": "hood", "jewelry": "rune-carved jewelry",
             "armor": "none", "condition": "travel-worn"},
    "assassin": {"lineage": "human", "faction": "shadow guild",
                 "palette": "charcoal and deep grey",
                 "materials": ["leather", "dark linen"],
                 "headwear": "hood", "jewelry": "none",
                 "armor": "studded leather", "condition": "travel-worn"},
    "dark lord": {"lineage": "dark elf", "faction": "dark court",
                  "palette": "charcoal and amber",
                  "materials": ["velvet", "plate armor", "dark metal"],
                  "headwear": "crown", "jewelry": "ruby and gold",
                  "armor": "rune-etched armor", "condition": "weathered"},
    "celestial": {"lineage": "celestial", "faction": "holy order",
                  "palette": "pearl, lavender and gold",
                  "materials": ["silk", "enchanted silk", "gold"],
                  "headwear": "ornate headpiece", "jewelry": "gold jewelry",
                  "armor": "gilded plate armor", "condition": "pristine"},
    "vampire noble": {"lineage": "vampire", "faction": "royal house",
                      "palette": "midnight blue and silver",
                      "materials": ["velvet", "silk", "silver"],
                      "headwear": "none", "jewelry": "silver jewelry",
                      "armor": "none", "condition": "pristine"},
    "battle mage": {"lineage": "human", "faction": "arcane circle",
                    "palette": "emerald and bronze",
                    "materials": ["leather", "wool", "bronze"],
                    "headwear": "none", "jewelry": "rune-carved jewelry",
                    "armor": "studded leather", "condition": "battle-worn"},
    "dark knight": {"lineage": "human", "faction": "dark court",
                    "palette": "charcoal and crimson",
                    "materials": ["plate armor", "chainmail"],
                    "headwear": "helm", "jewelry": "none",
                    "armor": "plate armor", "condition": "battle-worn"},
    "elven queen": {"lineage": "elf", "faction": "royal house",
                    "palette": "snow white and ice blue",
                    "materials": ["enchanted silk", "silver", "crystal"],
                    "headwear": "crown", "jewelry": "sapphire and gold",
                    "armor": "none", "condition": "ceremonial"},
    "celestial warrior": {"lineage": "celestial", "faction": "holy order",
                          "palette": "pearl and gold",
                          "materials": ["gilded plate armor", "enchanted silk"],
                          "headwear": "ornate headpiece", "jewelry": "gold jewelry",
                          "armor": "gilded plate armor", "condition": "pristine"},
    "elven royalty": {"lineage": "elf", "faction": "royal house",
                      "palette": "emerald and bronze",
                      "materials": ["silk", "velvet", "bronze"],
                      "headwear": "circlet", "jewelry": "emerald and silver",
                      "armor": "none", "condition": "pristine"},
    "dark lady": {"lineage": "dark elf", "faction": "dark court",
                  "palette": "charcoal and crimson",
                  "materials": ["velvet", "leather", "dark silk"],
                  "headwear": "circlet", "jewelry": "ruby and gold",
                  "armor": "none", "condition": "pristine"},
    "wanderer": {"lineage": "human", "faction": "nomad clan",
                 "palette": "charcoal and amber",
                 "materials": ["leather", "linen", "wool"],
                 "headwear": "hood", "jewelry": "none",
                 "armor": "studded leather", "condition": "travel-worn"},
}

# ---- performance ----------------------------------------------------
from dataset_gen import EMOTION_LOOKUP as _EMOTION_LOOKUP  # reuse vivid text
EXPRESSIONS = list(_EMOTION_LOOKUP.keys())
EXPRESSION_INTENSITY = {"subtle": "subtle expression, ",
                        "normal": "",
                        "strong": "intense, amplified expression, "}
EXPRESSION_ADVERBS = {"subtle": "subtly", "normal": "",
                      "strong": "intensely"}
GAZES = ["camera", "off-camera", "upward", "downward", "distant", "eyes closed"]
POSES = ["standing", "walking", "seated", "throne pose", "turning", "running",
         "combat-ready", "casting", "reaching", "holding object", "lying",
         "kneeling", "leaning", "custom"]

# ---- character scenes ----------------------------------------------
CHARACTER_SCENES = ["neutral studio (no environment)", "palace", "throne room",
                    "ballroom", "castle corridor", "balcony", "magical city street",
                    "medieval street", "enchanted forest", "battlefield",
                    "ancient temple", "ruins", "mountain fortress",
                    "snowy kingdom", "dark chamber", "custom"]
SCENE_PLACE_DESC: dict[str, str] = {
    "neutral studio (no environment)": "plain neutral studio background",
    "palace": "a grand palace interior", "throne room": "a vast throne room with marble columns",
    "ballroom": "a grand ballroom with chandeliers", "castle corridor": "a stone castle corridor",
    "balcony": "a stone balcony overlooking the kingdom", "magical city street": "a magical city street with glowing lanterns",
    "medieval street": "a medieval cobblestone street", "enchanted forest": "an enchanted forest with glowing flora",
    "battlefield": "a war-torn battlefield", "ancient temple": "an ancient temple with carved pillars",
    "ruins": "ancient ruins", "mountain fortress": "a mountain fortress with snow-dusted walls",
    "snowy kingdom": "a snowy kingdom with ice-crowned spires", "dark chamber": "a dark chamber lit by braziers",
}

# ---- lighting -------------------------------------------------------
LIGHTING_SETUPS = ["golden hour", "soft window light", "candlelight",
                   "torchlight", "moonlight", "dramatic chiaroscuro",
                   "palace chandelier light", "overcast daylight",
                   "neon glow", "firelight", "ethereal glow", "rim light",
                   "studio softbox", "storm light"]
LIGHTING_DESC: dict[str, str] = {
    "golden hour": "warm golden hour light", "soft window light": "soft diffused window light",
    "candlelight": "warm flickering candlelight", "torchlight": "orange torchlight with deep shadows",
    "moonlight": "cool silver moonlight", "dramatic chiaroscuro": "dramatic chiaroscuro lighting",
    "palace chandelier light": "warm chandelier light with soft gold tones",
    "overcast daylight": "flat overcast daylight", "neon glow": "cyan and magenta neon glow",
    "firelight": "warm firelight glow", "ethereal glow": "soft ethereal glow",
    "rim light": "strong rim light outlining the figure", "studio softbox": "clean studio softbox lighting",
    "storm light": "cold storm light with hard shadows",
}

# ---- realism layer --------------------------------------------------
TRAINING_REALISM = ("photorealistic, natural skin texture, subtle skin pores, "
                    "soft realistic lighting, single subject")
CINEMATIC_REALISM = ("photorealistic, cinematic filmic look, natural skin texture, "
                     "filmic color grade, deep focus, professional photography")
# Environments have no skin: they need PHOTOGRAPHY cues or the image models
# fall back to their fantasy-illustration prior (the "animated palace" case).
# Measured 2026-09-22: a "magical city" env still came out as a glossy CG
# game-art render — and because LTX copies the REFERENCE'S STYLE, every
# video conditioned on it looked animated. z-image ignores negative prompts
# (cfg 1.0), so the positive text must carry the live-action read: weathered
# real materials, matte surfaces, muted natural color, location photography.
ENV_REALISM = ("photorealistic real-world photograph, architectural "
               "photography, physically accurate materials, weathered real "
               "surfaces with visible wear, matte materials, muted natural "
               "colour palette, natural light, sharp focus, high dynamic "
               "range, shot on a full-frame camera, ultra detailed, 8k")
# Tail anchor for environment prompts: the LAST tokens stick too, and the
# fantasy nouns earlier in the sentence otherwise drag the render back into
# illustration.
ENV_ANCHOR = ("live-action film still, practical real-world location, "
              "documentary location photograph, natural colour science, "
              "subtle film grain, no stylisation")
# Every dataset prompt opens with this: image models weight the first tokens
# most, and leading with fantasy nouns produced illustrated results.
PHOTO_LEAD = ("photorealistic, ultra-detailed, real photograph")
ANTI_ILLUSTRATION = ["illustration", "painting", "digital art", "anime",
                     "cartoon", "cgi", "3d render", "concept art", "stylized",
                     "videogame", "drawing", "sketch", "poster art"]

# ---- power system (generalized) -------------------------------------
POWER_TYPES: dict[str, dict] = {
    "lightning": {"color": "electric cyan", "material": "electric arc",
                  "glow": "crackling cyan glow",
                  "sound_hint": "sharp thunder cracks and electric buzzing"},
    "fire": {"color": "molten orange", "material": "living flame",
             "glow": "orange firelight glow",
             "sound_hint": "a roaring wall of flame and snapping embers"},
    "ice": {"color": "pale blue", "material": "crystalline frost",
            "glow": "cold blue glow",
            "sound_hint": "sharp cracking ice and a low crystalline chime"},
    "water": {"color": "deep azure", "material": "flowing water",
              "glow": "aqua shimmer",
              "sound_hint": "churning rushing water"},
    "wind": {"color": "white-grey", "material": "swirling air",
             "glow": "pale wind streaks",
             "sound_hint": "a howling gale tearing across the scene"},
    "earth": {"color": "umber", "material": "stone and soil",
              "glow": "deep amber glow",
              "sound_hint": "grinding rock and deep subterranean rumbles"},
    "force": {"color": "violet", "material": "raw kinetic energy",
              "glow": "violet distortion",
              "sound_hint": "a deep concussive thump and air displacement"},
    "arcane": {"color": "violet-blue", "material": "glowing arcane glyphs",
               "glow": "arcane blue-violet glow",
               "sound_hint": "a resonant arcane hum with chiming glyphs"},
    "divine": {"color": "radiant gold", "material": "holy light",
               "glow": "blinding golden light",
               "sound_hint": "a choir-like resonance and a pure ringing tone"},
    "shadow": {"color": "deep purple-black", "material": "living shadow",
               "glow": "smoky purple shadow",
               "sound_hint": "whispering dark tendrils and muffled pressure"},
    "void": {"color": "black with violet rim", "material": "collapsing void",
             "glow": "violet-edged darkness",
             "sound_hint": "a sucking silence with a low subsonic drone"},
    "plasma": {"color": "white-hot magenta", "material": "superheated plasma",
               "glow": "intense plasma glow",
               "sound_hint": "a searing crackle and superheated air roar"},
    "nature": {"color": "emerald green", "material": "living vine and bloom",
               "glow": "soft green life-glow",
               "sound_hint": "creaking vines and rustling leaves"},
    "storm": {"color": "storm grey-violet", "material": "gathered stormfront",
              "glow": "flickering storm light",
              "sound_hint": "rolling thunder and driving rain"},
    "light": {"color": "blinding white", "material": "hard light",
              "glow": "white searing glow",
              "sound_hint": "a high ringing tone and a sharp report"},
    "gravity": {"color": "deep violet", "material": "warped space",
                "glow": "lens-warping violet shimmer",
                "sound_hint": "a groaning subsonic pull and collapsing debris"},
    "magma": {"color": "orange-black", "material": "liquid rock",
              "glow": "molten veins of orange",
              "sound_hint": "bubbling molten rock and ground-splitting cracks"},
    "poison": {"color": "sickly green", "material": "toxic mist",
               "glow": "venom-green haze",
               "sound_hint": "hissing vapor and dripping acid"},
    "blood": {"color": "deep crimson", "material": "living blood",
              "glow": "crimson wet sheen",
              "sound_hint": "a wet pulsing rhythm and heavy drops"},
    "custom": {"color": "vivid", "material": "energy",
               "glow": "glowing", "sound_hint": ""},
}
POWER_MANIFESTATIONS = ["aura", "hands", "orb", "beam", "wave", "shield",
                        "weapon coating", "sky event", "explosion",
                        "ground eruption", "impact", "whip"]
POWER_MANIFESTATION_DESC: dict[str, str] = {
    "aura": "the energy enveloping the body as a visible aura",
    "hands": "energy gathering in the raised hands and arcing from the fingertips",
    "orb": "a sphere of energy crackling between the hands",
    "beam": "a focused beam of energy erupting from an outstretched hand",
    "wave": "a rolling wave of energy expanding outward",
    "shield": "a translucent shield of energy forming before the caster",
    "weapon coating": "the weapon wreathed in living energy",
    "sky event": "the sky itself answering — a massive display overhead",
    "explosion": "a violent eruption of energy detonating outward",
    "ground eruption": "energy bursting up from the ground in jagged columns",
    "impact": "energy striking the ground in a blinding discharge",
    "whip": "a lashing whip of energy crackling through the air",
}
POWER_BEHAVIORS = ["crackling", "flowing", "erupting", "swirling", "blazing",
                   "streaming", "radiating", "pulsing", "arcing"]
POWER_BEHAVIOR_DESC: dict[str, str] = {
    "crackling": "crackling with sharp discharges", "flowing": "flowing like liquid light",
    "erupting": "erupting outward in bursts", "swirling": "swirling in tightening spirals",
    "blazing": "blazing with intense heat", "streaming": "streaming in continuous ribbons",
    "radiating": "radiating in steady waves", "pulsing": "pulsing in a steady rhythm",
    "arcing": "arcing in branching bolts",
}
POWER_INTENSITIES = ["faint", "steady", "violent", "cataclysmic", "contained"]
POWER_INTENSITY_DESC: dict[str, str] = {
    "faint": "faint and barely visible", "steady": "steady and controlled",
    "violent": "violent and unrestrained", "cataclysmic": "cataclysmic in scale",
    "contained": "tightly contained", }
POWER_ENV_REACTIONS = ["light reflection", "smoke", "debris", "scorch marks",
                       "frost", "sparks", "space distortion", "water displacement",
                       "surface damage", "heat shimmer", "glass melt"]
POWER_ENV_REACTION_DESC: dict[str, str] = {
    "light reflection": "the glow casting colored light across the environment",
    "smoke": "smoke and vapor curling around the energy",
    "debris": "debris lifted and scattered by the energy",
    "scorch marks": "scorch marks spreading across the ground",
    "frost": "frost crystallizing over nearby surfaces",
    "sparks": "sparks and embers cascading through the air",
    "space distortion": "the air visibly distorting around the energy",
    "water displacement": "water displaced and churned by the energy",
    "surface damage": "cracks and fractures racing through surfaces",
    "heat shimmer": "heat shimmer rising from the discharge",
    "glass melt": "nearby glass melting and warping",
}
POWER_SUBJECTS = ["silhouette figure", "generic figure", "selected character",
                  "none"]

# ---- POWER LEVEL LADDER (the whole power batch is driven by this) ----
# One image per level. Every level changes the MANIFESTATION, the FRAMING and
# the subject's action together — a level-3 aura and a level-9 sky event must
# not look like the same picture with different adjectives (that was the
# "all images look identical" failure: only intensity words changed while the
# composition stayed "hands, medium shot"). A lone human silhouette anchors
# scale and reads as a reference sheet for the video models.
POWER_LADDER: dict[int, dict] = {
    1: {"manifestation": "hands", "behavior": "crackling", "intensity": "faint",
        "scale": "contained", "destruction": "none", "aftermath": "none",
        "env_reaction": "light reflection", "shot": "close-up detail",
        "figure": "a single clenched fist held low, tiny sparks escaping "
                  "between the fingers",
        "notes": "tight detail shot, the fist and sparks filling the frame, "
                 "background dark and simple"},
    2: {"manifestation": "hands", "behavior": "arcing", "intensity": "faint",
        "scale": "contained", "destruction": "none", "aftermath": "none",
        "env_reaction": "light reflection", "shot": "close-up detail",
        "figure": "an open palm held out at waist height, thin arcs of "
                  "energy snapping across the fingers",
        "notes": "tight detail shot on the palm, arcs clearly structured, "
                 "dark simple background"},
    3: {"manifestation": "aura", "behavior": "flowing", "intensity": "steady",
        "scale": "contained", "destruction": "none", "aftermath": "none",
        "env_reaction": "light reflection", "shot": "full body",
        "figure": "a lone human silhouette standing upright, a soft aura "
                  "outlining the whole body",
        "notes": "full figure centered, the aura readable against the dark "
                 "background"},
    4: {"manifestation": "orb", "behavior": "swirling", "intensity": "steady",
        "scale": "local", "destruction": "none", "aftermath": "none",
        "env_reaction": "light reflection", "shot": "medium",
        "figure": "the silhouette cupping a growing orb of energy between "
                  "the hands",
        "notes": "medium shot, the orb lighting the figure from below"},
    5: {"manifestation": "beam", "behavior": "streaming", "intensity": "steady",
        "scale": "local", "destruction": "none", "aftermath": "none",
        "env_reaction": "light reflection", "shot": "full body",
        "figure": "the figure standing firm, energy streaming upward in a "
                  "controlled column",
        "notes": "full figure, the beam connecting the figure to the sky"},
    6: {"manifestation": "wave", "behavior": "erupting", "intensity": "violent",
        "scale": "local", "destruction": "surface", "aftermath": "none",
        "env_reaction": "debris", "shot": "full body",
        "figure": "the silhouette braced, cloth and hair lifted by the "
                  "discharge, a wave of energy expanding outward",
        "notes": "full figure with room for the expanding wave",
        "scene_hint": "a storm-lit courtyard of cracked stone, dust already "
                      "hanging in the air"},
    7: {"manifestation": "ground eruption", "behavior": "erupting",
        "intensity": "violent", "scale": "devastating",
        "destruction": "structural", "aftermath": "rubble",
        "env_reaction": "surface damage", "shot": "wide",
        "figure": "the figure at the center of jagged columns bursting from "
                  "the ground, debris rising",
        "notes": "wide shot, the figure small against the eruption",
        "scene_hint": "a ruined plaza of broken colonnades under a dark "
                      "storm sky"},
    8: {"manifestation": "explosion", "behavior": "blazing",
        "intensity": "violent", "scale": "devastating",
        "destruction": "structural", "aftermath": "rubble",
        "env_reaction": "smoke", "shot": "wide",
        "figure": "the silhouette behind an expanding dome of light, "
                  "shockwave rings flattening the ground outward",
        "notes": "wide shot, walls bursting and debris hurled past the lens, "
                 "dust and embers around the dome",
        "scene_hint": "a fortress courtyard collapsing inward, banners torn "
                      "from the walls"},
    9: {"manifestation": "sky event", "behavior": "arcing",
        "intensity": "cataclysmic", "scale": "apocalyptic",
        "destruction": "cataclysmic", "aftermath": "afterglow",
        "env_reaction": "space distortion", "shot": "wide",
        "figure": "a lone figure on a broken rooftop as colossal lightning "
                  "columns strike the burning skyline around them",
        "notes": "hero shot from low angle, city-scale destruction, the sky "
                 "split by branching strikes, the figure tiny on the skyline",
        "scene_hint": "a burning city skyline under a torn-open storm sky, "
                      "smoke columns rising between the towers"},
    10: {"manifestation": "sky event", "behavior": "blazing",
         "intensity": "cataclysmic", "scale": "apocalyptic",
         "destruction": "cataclysmic", "aftermath": "crater",
         "env_reaction": "space distortion", "shot": "wide",
         "figure": "a speck of a figure before an annihilation — a "
                   "continent-scale pillar of light tearing the sky open, "
                   "shockwave rings racing to the horizon, the land split "
                   "into glowing fissures, a city reduced to silhouettes "
                   "against the glare",
         "notes": "maximum scale: the horizon itself is breaking, the "
                  "ground opening, light overwhelming everything",
         "scene_hint": "a devastated kingdom at the moment of annihilation — "
                       "shattered towers, glowing fissures splitting the "
                       "land, ash and embers filling the air"},
}

# ---- power destruction ladder (H3-measured craft: the model scales VFX to
# the adjectives — understated wording produced "small shockwave, little
# glow"; over-exaggeration is the proven fix) --------------------------
POWER_SCALES = ["contained", "local", "devastating", "apocalyptic"]
POWER_SCALE_DESC: dict[str, str] = {
    "contained": "the effect stays tight around its source",
    "local": "the blast engulfs everything within a few paces",
    "devastating": "the blast covers and shakes the entire location",
    "apocalyptic": "the blast dwarfs the scene, sky-reaching and world-shaking",
}
POWER_DESTRUCTION = ["none", "surface", "structural", "cataclysmic"]
POWER_DESTRUCTION_DESC: dict[str, str] = {
    "none": "",
    "surface": "cracks and scorch marks racing across the ground",
    "structural": "walls shattering, pillars toppling, debris hurled outward",
    "cataclysmic": "the structure itself collapsing, a wall of dust and "
                   "embers racing past the lens",
}
POWER_AFTERMATH = ["none", "scorch", "rubble", "crater", "afterglow"]
POWER_AFTERMATH_DESC: dict[str, str] = {
    "none": "",
    "scorch": "scorched ground smoking where the energy struck",
    "rubble": "rubble and shattered stone strewn across the ground",
    "crater": "a glowing crater torn into the ground",
    "afterglow": "embers drifting down through the settling light",
}

# ---- environment system (redesigned) --------------------------------
ENV_WORLDS: dict[str, dict] = {
    "royal palace": {"architecture": "white marble halls with gilded arches",
                     "materials": ["marble", "gold", "glass"],
                     "scale": "vast and echoing", "features": ["glowing lanterns"],
                     "locs": ["the throne hall", "a gilded corridor", "the royal gardens",
                              "the grand staircase", "the banquet hall"]},
    "dark castle": {"architecture": "black stone fortress with iron gates",
                    "materials": ["basalt", "iron", "dark wood"],
                    "scale": "brooding and massive", "features": ["candlelit windows"],
                    "locs": ["the great hall", "the dungeon stairs", "the parapet",
                             "the courtyard", "the war room"]},
    "magical city": {"architecture": "soaring spires with glowing runes",
                     "materials": ["crystal", "stone", "enchanted glass"],
                     "scale": "towering and luminous", "features": ["floating structures", "glowing runes"],
                     "locs": ["the crystal market square", "the rune-carved palace district",
                              "the floating gardens", "the lantern-lit canal bridges",
                              "the sky temple"]},
    "medieval city": {"architecture": "timber-framed buildings with cobbled streets",
                      "materials": ["timber", "stone", "thatch"],
                      "scale": "intimate and busy", "features": ["banners", "market stalls"],
                      "locs": ["the market square", "the main gate", "the cathedral steps",
                               "the harbor docks", "the guild hall street"]},
    "ancient temple": {"architecture": "colossal stone pillars with carved reliefs",
                       "materials": ["sandstone", "marble", "bronze"],
                       "scale": "ancient and monumental", "features": ["colossal statues", "glowing braziers"],
                       "locs": ["the inner sanctum", "the processional avenue", "the altar chamber",
                                "the sunken courtyard", "the meditation halls"]},
    "enchanted forest": {"architecture": "ancient trees with glowing flora",
                         "materials": ["wood", "moss", "crystal"],
                         "scale": "vast and alive", "features": ["enchanted nature", "glowing crystals"],
                         "locs": ["the hidden waterfall clearing", "the glowing grove",
                                  "the mushroom circle", "the silver stream", "the hollow throne"]},
    "frozen kingdom": {"architecture": "ice-carved spires over a snowy city",
                       "materials": ["ice", "crystal", "frosted stone"],
                       "scale": "glacial and majestic", "features": ["aurora skies", "ice bridges"],
                       "locs": ["the ice throne hall", "the frozen harbor", "the aurora balcony",
                                "the crystal mines", "the snow gardens"]},
    "mountain fortress": {"architecture": "granite stronghold carved into the cliff",
                          "materials": ["granite", "iron", "snow"],
                          "scale": "imposing and remote", "features": ["watchtowers", "rope bridges"],
                          "locs": ["the gatehouse", "the training yard", "the great keep",
                                   "the wind-worn ramparts", "the armory"]},
    "floating realm": {"architecture": "islands of stone suspended on chains",
                       "materials": ["stone", "bronze", "cloud"],
                       "scale": "impossible and serene", "features": ["floating structures", "waterfalls off the edge"],
                       "locs": ["the cloud pier", "the suspended gardens", "the chain bridges",
                                "the sky bazaar", "the weather spire"]},
    "celestial realm": {"architecture": "bridges of light over pearl clouds",
                        "materials": ["light", "pearl", "gold"],
                        "scale": "infinite and radiant", "features": ["floating structures", "glowing runes"],
                        "locs": ["the gates of light", "the radiant hall", "the star garden",
                                 "the bridge of dawn", "the eternal library"]},
    "void realm": {"architecture": "shattered stone adrift in darkness",
                   "materials": ["obsidian", "void", "faint crystal"],
                   "scale": "bottomless and wrong", "features": ["violet-edged darkness", "floating debris"],
                   "locs": ["the shattered plaza", "the edge of nothing", "the crystal field",
                            "the broken stair", "the silent hall"]},
    "ruins": {"architecture": "collapsed columns and broken arches",
              "materials": ["stone", "overgrowth", "dust"],
              "scale": "fallen and silent", "features": ["colossal statues", "overgrown paths"],
              "locs": ["the fallen forum", "the cracked amphitheater", "the sunken temple",
                       "the overgrown aqueduct", "the mosaic courtyard"]},
    "battlefield": {"architecture": "churned earth and broken standards",
                    "materials": ["soil", "iron", "ash"],
                    "scale": "scarred and vast", "features": ["smoke columns", "torn banners"],
                    "locs": ["the front line", "the burning village edge", "the broken bridge",
                             "the war camp", "the hill of banners"]},
    "village": {"architecture": "thatched cottages around a stone well",
                "materials": ["timber", "thatch", "clay"],
                "scale": "small and warm", "features": ["chimney smoke", "market stalls"],
                "locs": ["the village square", "the mill pond", "the tavern porch",
                         "the chapel lane", "the field edge"]},
    "arena": {"architecture": "a torchlit stone arena ringed by tiered stands",
              "materials": ["stone", "sand", "iron"],
              "scale": "vast and roaring", "features": ["flaming braziers", "torn banners"],
              "locs": ["the fighting arena", "the fighters' tunnel", "the armory",
                       "the arena gates", "the champions' terrace"]},
    "colosseum": {"architecture": "a colossal colosseum of sand and stone",
                  "materials": ["sandstone", "marble", "bronze"],
                  "scale": "monumental and packed", "features": ["roaring crowds", "iron gates"],
                  "locs": ["the fighting pit", "the arena stands", "the beast pens",
                           "the emperor's box", "the gladiator barracks"]},
    "elven city": {"architecture": "living wood and silver spirals woven through ancient trees",
                   "materials": ["living wood", "silver", "crystal"],
                   "scale": "graceful and luminous", "features": ["glowing blossoms", "hanging waterfalls"],
                   "locs": ["the high hall of boughs", "the canopy bridges", "the silver falls",
                            "the song library", "the dueling glade"]},
    "dwarven hold": {"architecture": "colossal stone halls carved into a mountain",
                     "materials": ["granite", "iron", "gold"],
                     "scale": "cavernous and echoing", "features": ["forge light", "hanging chains"],
                     "locs": ["the great forge", "the throne of stone", "the deep mine",
                              "the ale hall", "the trial pit"]},
    "wizard academy": {"architecture": "stacked towers of libraries over a courtyard of floating stairs",
                       "materials": ["stone", "brass", "glass"],
                       "scale": "tall and cluttered", "features": ["floating candles", "arcane sigils"],
                       "locs": ["the grand lecture hall", "the floating stair court", "the potion labs",
                                "the forbidden stacks", "the duel ring"]},
    "fae court": {"architecture": "a court of thorns, petals and pale light",
                  "materials": ["thorn", "silk", "moonlight"],
                  "scale": "beautiful and wrong", "features": ["petals falling upward", "will-o'-wisps"],
                  "locs": ["the throne of thorns", "the masquerade hall", "the moonlight ring",
                           "the dance of blades", "the wish well"]},
    "necropolis": {"architecture": "a city of tombs and silent avenues under a grey sun",
                   "materials": ["marble", "bone", "dust"],
                   "scale": "vast and silent", "features": ["cold mist", "crows on the statues"],
                   "locs": ["the crypt of kings", "the silent avenue", "the judgment pit",
                            "the ossuary", "the black chapel"]},
    "dragon roost": {"architecture": "a shattered peak around a colossal cavern mouth",
                     "materials": ["obsidian", "ash", "gold"],
                     "scale": "immense and scorched", "features": ["heat shimmer", "molten veins"],
                     "locs": ["the cavern hoard", "the trial of flame", "the lava falls",
                              "the ash fields", "the egg chamber"]},
}
ENV_LOCATION_DESC: dict[str, str] = {   # default location text per world-loc
    "the throne hall": "the throne hall", "a gilded corridor": "a gilded corridor",
    "the royal gardens": "the royal gardens", "the grand staircase": "the grand staircase",
    "the banquet hall": "the banquet hall", "the great hall": "the great hall",
    "the dungeon stairs": "the dungeon stairs", "the parapet": "the parapet",
    "the courtyard": "the courtyard", "the war room": "the war room",
    "the crystal market square": "the crystal market square",
    "the rune-carved palace district": "the rune-carved palace district",
    "the floating gardens": "the floating gardens",
    "the lantern-lit canal bridges": "the lantern-lit canal bridges",
    "the sky temple": "the sky temple", "the market square": "the market square",
    "the main gate": "the main gate", "the cathedral steps": "the cathedral steps",
    "the harbor docks": "the harbor docks", "the guild hall street": "the guild hall street",
    "the inner sanctum": "the inner sanctum", "the processional avenue": "the processional avenue",
    "the altar chamber": "the altar chamber", "the sunken courtyard": "the sunken courtyard",
    "the meditation halls": "the meditation halls",
    "the hidden waterfall clearing": "the hidden waterfall clearing",
    "the glowing grove": "the glowing grove", "the mushroom circle": "the mushroom circle",
    "the silver stream": "the silver stream", "the hollow throne": "the hollow throne",
    "the ice throne hall": "the ice throne hall", "the frozen harbor": "the frozen harbor",
    "the aurora balcony": "the aurora balcony", "the crystal mines": "the crystal mines",
    "the snow gardens": "the snow gardens", "the gatehouse": "the gatehouse",
    "the training yard": "the training yard", "the great keep": "the great keep",
    "the wind-worn ramparts": "the wind-worn ramparts", "the armory": "the armory",
    "the cloud pier": "the cloud pier", "the suspended gardens": "the suspended gardens",
    "the chain bridges": "the chain bridges", "the sky bazaar": "the sky bazaar",
    "the weather spire": "the weather spire", "the gates of light": "the gates of light",
    "the radiant hall": "the radiant hall", "the star garden": "the star garden",
    "the bridge of dawn": "the bridge of dawn", "the eternal library": "the eternal library",
    "the shattered plaza": "the shattered plaza", "the edge of nothing": "the edge of nothing",
    "the crystal field": "the crystal field", "the broken stair": "the broken stair",
    "the silent hall": "the silent hall", "the fallen forum": "the fallen forum",
    "the cracked amphitheater": "the cracked amphitheater", "the sunken temple": "the sunken temple",
    "the overgrown aqueduct": "the overgrown aqueduct", "the mosaic courtyard": "the mosaic courtyard",
    "the front line": "the front line", "the burning village edge": "the burning village edge",
    "the broken bridge": "the broken bridge", "the war camp": "the war camp",
    "the hill of banners": "the hill of banners", "the village square": "the village square",
    "the mill pond": "the mill pond", "the tavern porch": "the tavern porch",
    "the chapel lane": "the chapel lane", "the field edge": "the field edge",
}
ENV_CONDITIONS = ["pristine", "weathered", "ruined", "battle-scarred", "overgrown"]
ENV_TIMES = ["night", "sunset", "golden hour", "dawn", "storm twilight", "blue hour"]
ENV_WEATHERS = ["clear sky", "rolling fog", "heavy rain", "snowfall", "dust storm",
                "hazy atmosphere", "overcast"]
ENV_ATMOSPHERES = ["ominous", "peaceful", "epic", "desolate", "mysterious",
                   "post-apocalyptic", "utopian", "noir", "reverent", "ancient"]
ENV_FOREGROUNDS = ["low creeping fog", "drifting embers", "rain streaks",
                   "dust motes in the light", "snow flurries", "silhouetted debris",
                   "windblown grass", "scattered petals"]
ENV_POPULATIONS = ["none", "sparse", "guards", "crowd"]
POPULATION_DESC: dict[str, str] = {
    # "none" is the reference-plate default and must be unambiguous: a single
    # stray figure in an environment reference reads as a subject to the video
    # model (measured: a market crowd in the background ref flipped the lead
    # character's gender in the render).
    "none": "no people at all, no figures, empty and deserted",
    "sparse": "a few distant figures",
    "guards": "uniformed guards on patrol",
    "crowd": "a busy crowd",
}
ENV_FANTASY_FEATURES = ["floating structures", "glowing runes", "magical rivers",
                        "colossal statues", "enchanted nature", "glowing crystals",
                        "aurora skies", "ice bridges"]

# ---- interior / exterior PLACES per world (the environment generator) ----
# Every place carries its own prompt profile: `k` kind (i=interior,
# x=exterior), `d` description, `c` camera, `f` framing/composition. The
# batch generates ONE image per selected place (× time), so selecting twelve
# places gives twelve genuinely different pictures — the old generator only
# varied seed and time for two places, which is why every image looked alike.
ENV_PLACES: dict[str, dict[str, dict]] = {
    # ---------------------------------------------------------- royal palace
    "royal palace": {
        "the palace — third-eye view": {"k": "x", "d": "the royal palace complex seen from high above, domes, spires and formal gardens spread below", "c": "high three-quarter aerial view", "f": "the whole complex fills the frame"},
        "the main gate approach": {"k": "x", "d": "the monumental palace gate at the end of a processional avenue lined with statues", "c": "centered one-point perspective at eye level", "f": "the gate framed by the avenue"},
        "the grand courtyard": {"k": "x", "d": "a vast paved courtyard ringed by colonnades and a central fountain — the center is empty and clear", "c": "wide eye-level view", "f": "the courtyard opening away from the camera"},
        "the royal gardens": {"k": "x", "d": "formal gardens with clipped hedges, statues and flowering arbors — the center is empty and clear", "c": "low gliding view along a garden path", "f": "layered hedges and statues"},
        "the outer wall at dusk": {"k": "x", "d": "the palace outer wall and watchtowers under a burning dusk sky", "c": "wide side view from the fields", "f": "the wall stretching across the frame"},
        "the throne hall": {"k": "i", "d": "a vast symmetrical hall with a raised dais, columns and a crimson carpet", "c": "symmetrical centered view down the carpet", "f": "the throne at the vanishing point"},
        "a gilded corridor": {"k": "i", "d": "a long corridor of repeating golden arches and hanging lanterns", "c": "one-point perspective down the corridor", "f": "the arches repeating into the distance"},
        "the grand staircase": {"k": "i", "d": "the grand staircase sweeping up between marble balustrades", "c": "low angle looking up the stairs", "f": "the stairs rising out of frame"},
        "the banquet hall": {"k": "i", "d": "a banquet hall with long tables, chandeliers and tall windows", "c": "wide eye-level view", "f": "the tables receding toward the windows"},
        "the royal bedchamber": {"k": "i", "d": "the royal bedchamber with a canopied bed, tapestries and a lit fireplace", "c": "medium wide view from the doorway", "f": "the bed as the focal point"},
        "the hall of mirrors": {"k": "i", "d": "a mirrored gallery of enchanted glass where reflections move late", "c": "centered view down the gallery", "f": "reflections repeating beyond the frame"},
        "the hidden vault": {"k": "i", "d": "a sealed vault of relic cases, sigils and cold candlelight", "c": "medium view across the plinths", "f": "the relics glinting in the dark"},
        "the dueling gallery": {"k": "i", "d": "a long practice hall with sanded floor, weapon racks and scuffed walls — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "racks lining both walls"},
        "the dungeon": {"k": "i", "d": "stone cells, chains and a single guttering torch — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the cells receding into darkness"},
    },
    # ----------------------------------------------------------- dark castle
    "dark castle": {
        "the castle — third-eye view": {"k": "x", "d": "a black stone fortress on a crag seen from high above, banners snapping in the wind", "c": "high three-quarter aerial view", "f": "the fortress small against the land"},
        "the gatehouse": {"k": "x", "d": "an iron gatehouse with a raised portcullis and flanking towers — the center is empty and clear", "c": "centered low angle", "f": "the gate towering over the camera"},
        "the courtyard": {"k": "x", "d": "a muddy courtyard of training dummies, straw and cold braziers", "c": "wide eye-level view", "f": "the courtyard enclosed by walls"},
        "the parapet walk": {"k": "x", "d": "the parapet walk along the battlements over a dark valley", "c": "tracking view along the wall", "f": "the drop on one side, wall on the other"},
        "the raven tower": {"k": "x", "d": "the raven tower, its top ringed by circling birds — the center is empty and clear", "c": "low angle looking up the tower", "f": "the tower filling the frame vertically"},
        "the great hall": {"k": "i", "d": "the great hall with long fire pits, banners and a raised high table — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the fire pits leading to the high table"},
        "the war room": {"k": "i", "d": "the war room with a huge map table, candles and hanging charts", "c": "high angle over the map table", "f": "the map table centered"},
        "the lord's chamber": {"k": "i", "d": "a heavy bed, furs and a shuttered window behind a stone arch", "c": "medium view from the doorway", "f": "the bed under the arch"},
        "the armory": {"k": "i", "d": "racks of swords, shields and armor stands oiled and ready", "c": "eye-level view down the racks", "f": "weapons repeating into the dark"},
        "the crypt": {"k": "i", "d": "a low crypt of stone kings, cobwebs and candle stubs", "c": "low eye-level view down the tombs", "f": "effigies receding into shadow"},
        "the training yard": {"k": "i", "d": "an indoor drill hall of packed earth, pells and shield racks", "c": "wide eye-level view", "f": "the pells lining the wall"},
                "the gallows yard": {"k": "x", "d": "a bare yard of gallows frames and crow-picked stones", "c": "wide eye-level view", "f": "the frames stark against the sky"},
        "the kennels": {"k": "i", "d": "cold kennels of straw, chains and restless hounds", "c": "low view along the runs", "f": "eyes catching the torchlight"},
"the rookery": {"k": "i", "d": "a rookery of nesting shelves, feathers and letter tubes", "c": "medium view across the shelves", "f": "birds shifting in the dark"},
    },
    # --------------------------------------------------------- magical city
    "magical city": {
        "the city — third-eye view": {"k": "x", "d": "a city of glowing spires and floating structures seen from above at night", "c": "high aerial view", "f": "rune-lit rooftops stretching away"},
        "the crystal market square": {"k": "x", "d": "a market square of crystal stalls under floating lanterns", "c": "wide eye-level view", "f": "the stalls and lanterns layered"},
        "the floating gardens": {"k": "x", "d": "gardens on floating platforms linked by bridges of light", "c": "wide three-quarter view", "f": "platforms receding into mist"},
        "the canal bridges": {"k": "x", "d": "lantern-lit canal bridges over glowing water", "c": "eye-level view along the canal", "f": "bridges repeating down the waterway"},
        "the sky docks": {"k": "x", "d": "docks where skyships moor among hanging chains and signal fires", "c": "wide eye-level view", "f": "ships and chains crossing the frame"},
        "the rune-lit plaza": {"k": "x", "d": "a plaza paved with glowing runes beneath a crystal spire", "c": "low angle with the spire above", "f": "the spire dominating the sky"},
        "the rune-carved palace hall": {"k": "i", "d": "a hall of rune-carved pillars and hovering lights — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "pillars repeating toward the dais"},
        "the arcane library": {"k": "i", "d": "a library of floating books and glowing shelves", "c": "vertical wide view", "f": "books orbiting the shelves"},
        "the duel ring": {"k": "i", "d": "a floating stone ring where mages duel, scorch marks on the slabs — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the ring suspended over the drop"},
        "the enchanter's workshop": {"k": "i", "d": "alembics, runes and half-built devices crowding a workbench", "c": "medium view across the workbench", "f": "apparatus packed to the ceiling"},
        "the portal plaza": {"k": "i", "d": "a vaulted hall of standing portals, each humming with light", "c": "centered view down the portal row", "f": "portals glowing on both sides"},
                "the gondola canals": {"k": "x", "d": "narrow canals where gondolas drift between glowing walls", "c": "low view along the water", "f": "the canal bending out of sight"},
        "the rune foundry": {"k": "i", "d": "a foundry where runes are poured into molten metal", "c": "medium view across the crucibles", "f": "sparks climbing into the dark"},
        "the astral observatory": {"k": "i", "d": "a domed observatory where star charts turn on the walls", "c": "vertical view up the dome", "f": "stars wheeling overhead"},
"a lantern-lit tavern": {"k": "i", "d": "a cozy tavern of driftwood beams and paper lanterns", "c": "medium eye-level view", "f": "the bar glowing at the center"},
    },
    # -------------------------------------------------------- medieval city
    "medieval city": {
        "the city — third-eye view": {"k": "x", "d": "a timber-framed city seen from above, roofs and chimneys packed together", "c": "high aerial view", "f": "rooftops to the horizon"},
        "the market square": {"k": "x", "d": "a bustling market square with stalls, carts and a stone well", "c": "wide eye-level view", "f": "stalls receding into the crowd"},
        "the main gate": {"k": "x", "d": "the city's main gate in a stone wall with banners above", "c": "centered low angle", "f": "the gate arch framing the street beyond"},
        "the harbor docks": {"k": "x", "d": "wooden docks with moored boats, nets and stacked crates", "c": "eye-level view along the quay", "f": "boats layered along the water"},
        "the jousting grounds": {"k": "x", "d": "the jousting grounds with rails, pennants and a crowded stand — the center is empty and clear", "c": "wide side view across the lists", "f": "the lists running out of frame"},
        "the rooftops": {"k": "x", "d": "a run of steep rooftops with chimneys and pigeons", "c": "wide three-quarter view", "f": "roofs climbing toward the cathedral"},
        "the cathedral nave": {"k": "i", "d": "a cathedral nave with columns, candles and colored light", "c": "centered aisle view", "f": "the altar far down the nave"},
        "the guild hall": {"k": "i", "d": "a guild hall with long tables, banners and a raised seat", "c": "wide centered view", "f": "the banners lining the walls"},
        "a tavern interior": {"k": "i", "d": "a low-beamed tavern with barrels, benches and a smoking hearth", "c": "medium eye-level view", "f": "the hearth glowing behind the bar"},
        "the alchemist's cellar": {"k": "i", "d": "a cellar of jars, copper pipes and bubbling glass", "c": "medium view across the bench", "f": "shelves of jars in lamplight"},
        "the bathhouse": {"k": "i", "d": "a steam-filled bathhouse of tiled pools and hanging cloth", "c": "wide view across the pools", "f": "steam softening the far wall"},
                "the tannery district": {"k": "x", "d": "a reeking district of drying racks and dye vats", "c": "wide eye-level view down the lane", "f": "hides hanging overhead"},
        "the clock tower": {"k": "i", "d": "a clock tower of gears, ropes and a cracked bell", "c": "vertical view up the mechanism", "f": "gears climbing into the dark"},
"the town jail": {"k": "i", "d": "a small stone jail of barred cells and straw", "c": "low eye-level view", "f": "the bars catching torchlight"},
    },
    # --------------------------------------------------------- ancient temple
    "ancient temple": {
        "the temple — third-eye view": {"k": "x", "d": "a colossal temple complex seen from above among jungle and stone", "c": "high aerial view", "f": "the temple laid out below"},
        "the processional avenue": {"k": "x", "d": "an avenue of colossal statues leading to the temple steps", "c": "centered one-point view", "f": "statues flanking the path"},
        "the sunken courtyard": {"k": "x", "d": "a sunken courtyard with carved walls and shallow water", "c": "wide view from the rim", "f": "the courtyard below the camera"},
        "the colossal stair": {"k": "x", "d": "a vast stone stair climbing the temple face", "c": "low angle up the stairs", "f": "the steps filling the frame"},
        "the obelisk plaza": {"k": "x", "d": "a plaza of standing obelisks carved with glyphs", "c": "wide eye-level view", "f": "obelisks crossing the sky"},
        "the inner sanctum": {"k": "i", "d": "a glowing altar between carved pillars", "c": "centered view toward the altar", "f": "the altar at the vanishing point"},
        "the altar chamber": {"k": "i", "d": "a chamber of braziers around a monolithic altar", "c": "medium wide view", "f": "the altar centered in firelight"},
        "the trial arena": {"k": "i", "d": "a ritual combat pit ringed by carved seats, sand and old blood — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the seats rising all around"},
        "the star map room": {"k": "i", "d": "a domed room where constellations turn on the ceiling", "c": "medium view across the floor", "f": "stars wheeling overhead"},
        "the burial vault": {"k": "i", "d": "a vault of sarcophagi and cobwebs", "c": "low eye-level view", "f": "sarcophagi receding into dark"},
        "the sacrificial well": {"k": "i", "d": "a deep well ringed by glyphs, mist rising from below — the center is empty and clear", "c": "high angle down at the well", "f": "the well mouth centered"},
                "the flooded stair": {"k": "x", "d": "a ceremonial stair half-drowned in still water", "c": "low view along the water", "f": "steps vanishing into the flood"},
        "the oracle's cell": {"k": "i", "d": "a bare cell of one chair, one brazier and old smoke", "c": "medium view toward the chair", "f": "the brazier the only light"},
"the meditation halls": {"k": "i", "d": "quiet halls of mats, screens and incense smoke", "c": "eye-level view down the hall", "f": "screens layered into the distance"},
    },
    # ------------------------------------------------------ enchanted forest
    "enchanted forest": {
        "the forest — third-eye view": {"k": "x", "d": "an enchanted forest canopy seen from above, glowing glades between ancient trees", "c": "high aerial view", "f": "glades scattered through the canopy"},
        "the hidden waterfall clearing": {"k": "x", "d": "a clearing with a waterfall falling into a glowing pool — the center is empty and clear", "c": "wide eye-level view", "f": "the falls centered behind the pool"},
        "the glowing grove": {"k": "x", "d": "a grove of luminous leaves and drifting spores", "c": "low gliding view", "f": "glowing trunks layered"},
        "the mushroom circle": {"k": "x", "d": "a ring of giant mushrooms around a mossy stone — the center is empty and clear", "c": "wide eye-level view", "f": "the ring closing around the stone"},
        "the fae ring": {"k": "x", "d": "a dueling glade ringed by standing stones, petals drifting — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the stones closing the circle"},
        "the hollow throne": {"k": "x", "d": "a throne grown from roots beneath a colossal tree", "c": "centered view toward the throne", "f": "the roots arching overhead"},
        "a root cavern": {"k": "i", "d": "a cavern woven through living roots, lit by fungus", "c": "eye-level view along the tunnel", "f": "roots framing the passage"},
        "the hollow tree hall": {"k": "i", "d": "a hall carved inside a colossal trunk with spiral stairs", "c": "vertical wide view", "f": "the stairs spiraling up the trunk"},
        "the ancient library tree": {"k": "i", "d": "a library grown into a tree, shelves in living bark", "c": "medium view along the shelves", "f": "books nested in the wood"},
        "the witch's hut interior": {"k": "i", "d": "a hut of herbs, jars and hanging charms", "c": "medium view across the table", "f": "shelves crowded with jars"},
        "the spirit pool": {"k": "i", "d": "a still pool in a cave, faces moving beneath the surface", "c": "high angle down at the water", "f": "the pool filling the floor"},
    },
    # -------------------------------------------------------- frozen kingdom
    "frozen kingdom": {
        "the kingdom — third-eye view": {"k": "x", "d": "an ice-carved city seen from above under a rippling aurora", "c": "high aerial view", "f": "aurora bands over the spires"},
        "the frozen harbor": {"k": "x", "d": "a harbor locked in ice with trapped ships", "c": "wide eye-level view", "f": "masts frozen in the ice field"},
        "the aurora balcony": {"k": "x", "d": "a balcony under rippling aurora light", "c": "wide view toward the sky", "f": "aurora filling the upper frame"},
        "the snow gardens": {"k": "x", "d": "gardens of ice sculptures under fresh snow", "c": "low view along the path", "f": "sculptures layered in snow"},
        "the ice bridge": {"k": "x", "d": "a natural ice bridge over a dark chasm", "c": "wide three-quarter view", "f": "the bridge spanning the gap"},
        "the glacier field": {"k": "x", "d": "a cracked glacier field with blue crevasses", "c": "wide high view", "f": "crevasses cutting across the ice"},
        "the ice throne hall": {"k": "i", "d": "a hall of clear ice around a frozen throne", "c": "centered view toward the throne", "f": "ice columns refracting light"},
        "the frozen arena": {"k": "i", "d": "a combat ring cut into ice, old cracks and frozen stains — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the ring walled by ice"},
        "the frost keep": {"k": "i", "d": "a keep of frost-covered stone and blue fire", "c": "wide eye-level view", "f": "braziers glowing blue"},
        "the great hearth hall": {"k": "i", "d": "a hall with a huge hearth fighting the cold — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the hearth at the far wall"},
        "the ice prison": {"k": "i", "d": "cells of clear ice with shapes frozen inside", "c": "low eye-level view down the cells", "f": "the frozen shapes catching light"},
        "the crystal mines": {"k": "i", "d": "mineshafts glittering with ice crystals — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "crystal veins in the rock"},
    },
    # ----------------------------------------------------- mountain fortress
    "mountain fortress": {
        "the fortress — third-eye view": {"k": "x", "d": "a granite fortress carved into a cliff seen from above, clouds below", "c": "high aerial view", "f": "the fortress wedged in the rock"},
        "the gatehouse": {"k": "x", "d": "a gatehouse of timber and iron above a switchback road", "c": "centered low angle", "f": "the gate over the road"},
        "the wind-worn ramparts": {"k": "x", "d": "ramparts scoured by wind above the clouds", "c": "tracking view along the wall", "f": "the drop beyond the wall"},
        "the rope bridge": {"k": "x", "d": "a rope bridge swaying over a gorge", "c": "wide side view", "f": "the bridge spanning the void"},
        "the signal tower": {"k": "x", "d": "a signal tower with a beacon fire and brass mirrors", "c": "low angle up the tower", "f": "the beacon against the sky"},
        "the great keep": {"k": "i", "d": "the keep's main hall of stone, banners and a long hearth", "c": "wide centered view", "f": "the hearth at the far end"},
        "the dueling pit": {"k": "i", "d": "a sunken pit of packed earth and weapon racks where disputes are settled — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the pit below the camera"},
        "the armory": {"k": "i", "d": "an armory of racks, whetstones and oiled steel", "c": "eye-level view down the racks", "f": "blades repeating into shadow"},
        "the war table room": {"k": "i", "d": "a carved relief map with campaign markers", "c": "high angle over the table", "f": "the map centered"},
        "the barracks": {"k": "i", "d": "bunk rows, footlockers and hanging cloaks", "c": "eye-level view down the aisle", "f": "bunks repeating into the hall"},
        "the mine shaft": {"k": "i", "d": "a shaft with timber bracing and lantern light", "c": "low view down the tunnel", "f": "bracing frames receding"},
    },
    # -------------------------------------------------------- floating realm
    "floating realm": {
        "the realm — third-eye view": {"k": "x", "d": "islands of stone suspended on chains above an ocean of cloud", "c": "high aerial view", "f": "islands layered into the mist"},
        "the cloud pier": {"k": "x", "d": "a pier jutting into open cloud, bells on the posts", "c": "wide view along the pier", "f": "the drop beyond the rail"},
        "the suspended gardens": {"k": "x", "d": "gardens on hanging platforms with waterfalls off the edge — the center is empty and clear", "c": "wide three-quarter view", "f": "falls pouring into the void"},
        "the chain bridges": {"k": "x", "d": "bridges of chain and plank between islands", "c": "low view along the bridge", "f": "the bridge arcing into mist"},
        "the sky arena": {"k": "x", "d": "a fighting platform suspended on chains, open cloud on every side — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the void beyond the ropes"},
        "the edge overlook": {"k": "x", "d": "an overlook at the very edge of the realm", "c": "wide view into the clouds", "f": "the void filling the frame"},
        "the cloud hall": {"k": "i", "d": "a hall open to the sky with hanging banners", "c": "wide centered view", "f": "clouds moving beyond the columns"},
        "the anchor vault": {"k": "i", "d": "the vault where the realm's great chains are anchored", "c": "low angle up the chains", "f": "the chains vanishing into the roof"},
        "the wind tunnels": {"k": "i", "d": "carved tunnels where the wind roars through grates", "c": "eye-level view down the tunnel", "f": "grated light across the walls"},
        "the weather spire chamber": {"k": "i", "d": "a chamber of instruments and drifting mist", "c": "vertical view up the spire", "f": "instruments climbing the walls"},
    },
    # ------------------------------------------------------- celestial realm
    "celestial realm": {
        "the realm — third-eye view": {"k": "x", "d": "bridges of light over pearl clouds seen from above", "c": "high aerial view", "f": "light bridges crossing the clouds"},
        "the gates of light": {"k": "x", "d": "monumental gates of light opening onto radiance", "c": "centered low angle", "f": "the gates blazing above"},
        "the star garden": {"k": "x", "d": "a garden of floating lights and silver trees", "c": "low gliding view", "f": "lights drifting through the trees"},
        "the bridge of dawn": {"k": "x", "d": "a bridge of light arcing over the cloud sea", "c": "wide side view", "f": "the arc spanning the frame"},
        "the trial of light": {"k": "x", "d": "a circular arena of white stone where champions are judged — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the circle ringed by radiance"},
        "the radiant hall": {"k": "i", "d": "a hall of pure light and floating platforms", "c": "wide centered view", "f": "platforms receding into glow"},
        "the eternal library": {"k": "i", "d": "a library of floating tomes and endless shelves", "c": "vertical wide view", "f": "tomes orbiting the shelves"},
        "the mirror pool": {"k": "i", "d": "a still pool that shows other places, ringed by marble — the center is empty and clear", "c": "high angle down at the water", "f": "the pool mirroring the hall"},
        "the choir dome": {"k": "i", "d": "a dome where sound hangs in the air as rings of light — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "light rings filling the dome"},
        "the star chamber": {"k": "i", "d": "a dark chamber where constellations move on the walls", "c": "medium view across the floor", "f": "stars wheeling on the walls"},
    },
    # ------------------------------------------------------------ void realm
    "void realm": {
        "the realm — third-eye view": {"k": "x", "d": "shattered stone adrift in darkness seen from above", "c": "high aerial view", "f": "fragments floating in the void"},
        "the shattered plaza": {"k": "x", "d": "a plaza broken into floating fragments", "c": "wide eye-level view", "f": "fragments hanging in space"},
        "the edge of nothing": {"k": "x", "d": "the edge where stone simply ends", "c": "wide view toward the dark", "f": "the void beyond the edge"},
        "the crystal field": {"k": "x", "d": "a field of faint crystals glowing in the dark", "c": "low view across the crystals", "f": "crystal light scattered ahead"},
        "the broken stair": {"k": "x", "d": "a colossal stair broken mid-flight in the void", "c": "low angle up the stair", "f": "the stair vanishing into dark"},
        "the arena of echoes": {"k": "i", "d": "a black ring where every blow returns twice as sound — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "echo rings rippling outward"},
        "the silent hall": {"k": "i", "d": "a hall with no walls, floor adrift in darkness", "c": "wide centered view", "f": "the floor ending in black"},
        "the broken sanctum": {"k": "i", "d": "a sanctum split by a vertical fracture", "c": "medium view across the crack", "f": "the fracture splitting the room"},
        "the hungering gate": {"k": "i", "d": "a gate of teeth and chains that swallows light — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the gate drinking the glow"},
        "the crystal vault": {"k": "i", "d": "a vault of violet-lit crystals and floating shards — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "shards hovering in the dark"},
    },
    # ---------------------------------------------------------------- ruins
    "ruins": {
        "the ruins — third-eye view": {"k": "x", "d": "fallen columns and broken arches overgrown, seen from above", "c": "high aerial view", "f": "the fallen plan laid out below"},
        "the fallen forum": {"k": "x", "d": "a forum of toppled columns and cracked paving", "c": "wide eye-level view", "f": "columns lying across the frame"},
        "the cracked amphitheater": {"k": "x", "d": "an amphitheater with a broken rim and grassed seats", "c": "wide three-quarter view", "f": "the bowl opening away"},
        "the overgrown aqueduct": {"k": "x", "d": "an aqueduct arching through the overgrowth", "c": "low angle along the arches", "f": "arches marching into the trees"},
        "the mosaic courtyard": {"k": "x", "d": "a courtyard with a cracked mosaic floor", "c": "high angle down at the mosaic", "f": "the mosaic filling the ground"},
        "the sunken temple": {"k": "i", "d": "a temple half-swallowed by earth and roots", "c": "eye-level view toward the altar", "f": "roots breaking through stone"},
        "the gladiator pit": {"k": "i", "d": "a sand pit under a broken dome, chains and old iron — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the broken dome open above"},
        "a collapsed hall": {"k": "i", "d": "a hall with a fallen roof and shafts of light", "c": "wide view through the debris", "f": "light beams cutting the dust"},
        "the ruined keep": {"k": "i", "d": "a keep with a shattered stair and birds nesting in the rafters", "c": "vertical view up the stair", "f": "the stair spiraling into ruin"},
        "the tomb chamber": {"k": "i", "d": "a tomb of broken sarcophagi and cobwebs", "c": "low eye-level view", "f": "sarcophagi cracked open"},
        "the buried archive": {"k": "i", "d": "shelves of rotted scrolls under fallen stone", "c": "medium view across the shelves", "f": "scrolls spilling from shelves"},
    },
    # ----------------------------------------------------------- battlefield
    "battlefield": {
        "the battlefield — third-eye view": {"k": "x", "d": "a scarred battlefield seen from above, smoke columns and broken lines", "c": "high aerial view", "f": "smoke and formations below"},
        "the front line": {"k": "x", "d": "the front line of churned earth, shields and stakes", "c": "wide eye-level view", "f": "the line stretching across the frame"},
        "the burning village edge": {"k": "x", "d": "the burning edge of a village, roofs alight", "c": "wide view through smoke", "f": "fire and smoke filling the sky"},
        "the broken bridge": {"k": "x", "d": "a shattered bridge over a slow river", "c": "wide side view", "f": "the gap in the span"},
        "the siege engines": {"k": "x", "d": "burning siege engines and a toppled trebuchet", "c": "low angle among the wreckage", "f": "timbers leaning out of frame"},
        "the hill of banners": {"k": "x", "d": "a hill crowned with planted banners and spears", "c": "low angle up the hill", "f": "banners against the sky"},
        "the war camp": {"k": "x", "d": "a war camp of tents, fires and picket lines", "c": "wide eye-level view", "f": "tents receding into the dusk"},
        "a command tent": {"k": "i", "d": "a command tent with a map table and hanging lanterns", "c": "medium view across the table", "f": "the map lit from above"},
        "the dueling ground": {"k": "i", "d": "a champion's ring of trampled ground ringed by spears — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the ring closed by spears"},
        "the field hospital": {"k": "i", "d": "cots, lanterns and stained canvas — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "cots receding into the tent"},
        "the pyre field": {"k": "i", "d": "rows of pyres burning low under a dark sky", "c": "wide eye-level view", "f": "firelight across the rows"},
    },
    # --------------------------------------------------------------- village
    "village": {
        "the village — third-eye view": {"k": "x", "d": "a thatched village around a green seen from above", "c": "high aerial view", "f": "roofs and gardens laid out below"},
        "the village square": {"k": "x", "d": "the square with a stone well, benches and market stalls", "c": "wide eye-level view", "f": "the well at the center"},
        "the mill pond": {"k": "x", "d": "the mill pond with the water wheel turning", "c": "medium wide view", "f": "the wheel at the frame's edge"},
        "the harvest ring": {"k": "x", "d": "a trampled ring beside the fields where the village settles disputes — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the ring ringed by onlookers' boots"},
        "the chapel lane": {"k": "x", "d": "a lane of cottages leading to the chapel", "c": "eye-level view down the lane", "f": "the chapel at the end"},
        "the orchard": {"k": "x", "d": "an orchard in blossom with beehives", "c": "low view between the trees", "f": "blossom arching overhead"},
        "the tavern common room": {"k": "i", "d": "the tavern common room with benches and a low fire", "c": "medium eye-level view", "f": "the fire behind the bar"},
        "a cottage interior": {"k": "i", "d": "a cottage interior with a hearth, table and herbs", "c": "medium view across the table", "f": "the hearth glowing behind"},
        "the smithy": {"k": "i", "d": "the smithy with a glowing forge, anvil and quenching barrel", "c": "medium view across the anvil", "f": "sparks rising into the rafters"},
        "the chapel": {"k": "i", "d": "a tiny chapel with a carved altar and candles", "c": "centered view down the aisle", "f": "the altar at the end"},
        "the well cellar": {"k": "i", "d": "a stone cellar beneath the well, cold and dripping", "c": "low view along the water", "f": "the water vanishing into dark"},
    },
    # ------------------------------------------------------------ arena
    "arena": {
        "the arena — third-eye view": {"k": "x", "d": "a torchlit arena complex seen from above, the pit ringed by stands", "c": "high aerial view", "f": "the pit centered below"},
        "the arena gates": {"k": "x", "d": "the arena's outer gates with banners and a waiting crowd — the center is empty and clear", "c": "centered low angle", "f": "the gates towering over the approach"},
        "the training grounds": {"k": "x", "d": "training grounds of practice dummies and racked weapons — the center is empty and clear", "c": "wide eye-level view", "f": "dummies spread across the sand"},
        "the champions' terrace": {"k": "x", "d": "a terrace overlooking the pit, trophies and banners — the center is empty and clear", "c": "wide view down into the arena", "f": "the pit far below the rail"},
        "the fighting arena": {"k": "i", "d": "the fighting pit itself, sand floor, scorch marks and tiered stands around — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the stands rising all around"},
        "the fighters' tunnel": {"k": "i", "d": "the torchlit tunnel from which fighters enter", "c": "one-point view toward the gate", "f": "the bright gate at the tunnel's end"},
        "the champions' hall": {"k": "i", "d": "a hall of trophies, oiled gear and carved names", "c": "medium view along the wall", "f": "trophies catching the lamplight"},
        "the armory": {"k": "i", "d": "the arena armory with racks of gladiator gear — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "weapons and shields layered"},
        "the infirmary": {"k": "i", "d": "a sawdust infirmary of cots, splints and herb smoke", "c": "eye-level view down the cots", "f": "lanterns swaying overhead"},
        "the holding cells": {"k": "i", "d": "stone holding cells with chains and a brazier", "c": "low eye-level view down the cells", "f": "bars catching the firelight"},
        "the beast pens": {"k": "i", "d": "dark pens with heavy gates and clawed walls", "c": "low view along the pens", "f": "gates receding into darkness"},
        "the under-pit": {"k": "i", "d": "the machine level beneath the sand, chains and trapdoors — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "trapdoors open above"},
    },
    # ----------------------------------------------------------- colosseum
    "colosseum": {
        "the colosseum — third-eye view": {"k": "x", "d": "the colosseum's full ellipse seen from high above, awnings rippling over the tiers", "c": "high aerial view", "f": "the whole bowl in frame"},
        "the arena stands": {"k": "x", "d": "the packed stands with striped awnings and roaring tiers — the center is empty and clear", "c": "wide eye-level view", "f": "tiers stacked to the rim"},
        "the emperor's box": {"k": "x", "d": "the imperial box with purple awnings, laurels and guards", "c": "medium wide view", "f": "the box above the crowd"},
        "the sand field": {"k": "x", "d": "the raked sand field under harsh Mediterranean sun", "c": "wide low view across the sand", "f": "the field to the far wall"},
        "the fighting pit": {"k": "i", "d": "the pit floor with sand, bronze gates and shadowed walls — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "walls and gates closing around"},
        "the animal cages": {"k": "i", "d": "iron cages and hoists where the beasts wait below", "c": "low view along the cages", "f": "cages receding into dark"},
        "the ludus": {"k": "i", "d": "the gladiator school: practice posts, oil lamps and barred cells", "c": "eye-level view down the yard", "f": "practice posts along the wall"},
        "the hypogeum": {"k": "i", "d": "the under-arena maze of lifts, chains and trapdoors — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "lamps leading into the dark"},
        "the infirmary": {"k": "i", "d": "a sawdust infirmary of cots, splints and herb smoke", "c": "eye-level view down the cots", "f": "lanterns swaying overhead"},
    },
    # ------------------------------------------------------- elven city (new)
    "elven city": {
        "the city — third-eye view": {"k": "x", "d": "an elven city of living wood and silver spirals woven through ancient trees", "c": "high aerial view", "f": "canopy walkways threading the trees"},
        "the river gates": {"k": "x", "d": "silver gates over a river crossing into the city", "c": "centered low angle", "f": "the gates reflected in the water"},
        "the canopy bridges": {"k": "x", "d": "rope and silver bridges between the crowns of giant trees", "c": "wide view along the bridge", "f": "the drop between the trunks"},
        "the silver falls": {"k": "x", "d": "a waterfall falling through the city into a luminous basin", "c": "wide view toward the falls", "f": "the falls veiled in mist"},
        "the starlit grove": {"k": "x", "d": "a grove where the light never fully fades", "c": "low gliding view", "f": "glow drifting between the trunks"},
        "the high hall of boughs": {"k": "i", "d": "a hall grown from interwoven boughs, open to the sky", "c": "wide centered view", "f": "the boughs arching overhead"},
        "the song library": {"k": "i", "d": "a library of carved song-cylinders and hanging scrolls", "c": "medium view along the shelves", "f": "scrolls drifting on their threads"},
        "the dueling glade": {"k": "i", "d": "a moonlit practice ring of soft moss and standing blades — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "blades planted at the edge"},
        "the healer's bower": {"k": "i", "d": "a bower of herbs, steaming bowls and soft light", "c": "medium view across the table", "f": "herbs drying overhead"},
        "the mirror pool": {"k": "i", "d": "a still pool ringed by carved stones that shows distant places — the center is empty and clear", "c": "high angle down at the water", "f": "the pool mirroring the canopy"},
        "the seed vault": {"k": "i", "d": "a vault of glowing seeds and living relics", "c": "medium view across the plinths", "f": "seed-light scattered in the dark"},
    },
    # ------------------------------------------------------ dwarven hold (new)
    "dwarven hold": {
        "the hold — third-eye view": {"k": "x", "d": "a dwarven hold carved into a mountain, forge smoke rising from its gates", "c": "high aerial view", "f": "the mountain face split by the gate"},
        "the mountain gate": {"k": "x", "d": "a colossal gate of iron and stone flanked by carved kings — the center is empty and clear", "c": "centered low angle", "f": "the gate towering over the road"},
        "the cliff stair": {"k": "x", "d": "a stair cut into the cliff face above the clouds", "c": "wide side view", "f": "the stair climbing out of frame"},
        "the slag fields": {"k": "x", "d": "fields of slag and broken crucibles under a soot sky", "c": "wide eye-level view", "f": "smoke drifting across the waste"},
        "the great forge": {"k": "i", "d": "a cavernous forge of rivers of molten metal and hammering crews — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the forge fires filling the hall"},
        "the throne of stone": {"k": "i", "d": "a throne carved from the mountain itself, runes deep in the rock", "c": "centered view toward the throne", "f": "the throne rooted in the floor"},
        "the trial pit": {"k": "i", "d": "a deep pit of iron and stone where grudges are settled — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the pit glowing with forge light"},
        "the ale hall": {"k": "i", "d": "a roaring ale hall of long tables, barrels and song — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "tables running into the dark"},
        "the deep mine": {"k": "i", "d": "a mine of crystal seams, rails and hanging lamps", "c": "eye-level view down the rails", "f": "rails vanishing into the shaft"},
        "the treasure vault": {"k": "i", "d": "a vault of counted gold, relics and sealed chests", "c": "medium view across the plinths", "f": "gold catching the lamp"},
        "the rune engine": {"k": "i", "d": "a chamber where a colossal rune engine turns in the dark", "c": "low angle up at the engine", "f": "the engine filling the chamber"},
    },
    # --------------------------------------------------- wizard academy (new)
    "wizard academy": {
        "the academy — third-eye view": {"k": "x", "d": "a wizard academy of stacked towers and floating staircases above a courtyard", "c": "high aerial view", "f": "staircases drifting between towers"},
        "the gates of the academy": {"k": "x", "d": "iron gates under a stone arch inscribed with sigils", "c": "centered low angle", "f": "the arch glowing faintly"},
        "the floating stair court": {"k": "x", "d": "a courtyard where staircases float and rearrange", "c": "wide view across the court", "f": "stairs hanging in the air"},
        "the observatory roof": {"k": "x", "d": "a brass observatory roof open to the night sky", "c": "wide view toward the stars", "f": "the telescope arcing overhead"},
        "the grand lecture hall": {"k": "i", "d": "a tiered lecture hall of benches, chalk sigils and floating candles", "c": "high angle from the lectern", "f": "benches rising in tiers"},
        "the duel ring": {"k": "i", "d": "a warded practice ring scorched by student duels — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "ward sigils glowing on the floor"},
        "the potion labs": {"k": "i", "d": "long benches of bubbling glass, burners and stained notes", "c": "medium view across the benches", "f": "glass catching the burner light"},
        "the forbidden stacks": {"k": "i", "d": "chained books and cold lanterns behind an iron grate", "c": "eye-level view down the aisle", "f": "chains crossing the shelves"},
        "the summoning chamber": {"k": "i", "d": "a circle of chalk and brass where something waits to be called", "c": "high angle down at the circle", "f": "the circle centered in the floor"},
        "the dormitory tower": {"k": "i", "d": "a spiral dormitory of bunk alcoves and scattered notes", "c": "vertical view up the spiral", "f": "alcoves spiraling upward"},
        "the headmaster's study": {"k": "i", "d": "a study of tall shelves, a great desk and a watching portrait", "c": "medium view across the desk", "f": "shelves looming behind"},
    },
    # ---------------------------------------------------------- fae court (new)
    "fae court": {
        "the court — third-eye view": {"k": "x", "d": "a fae court of thorns, petals and pale light seen from above", "c": "high aerial view", "f": "petals drifting across the paths"},
        "the thorn gate": {"k": "x", "d": "a gate of woven thorns that opens only for the invited", "c": "centered low angle", "f": "thorns closing over the arch"},
        "the petal bridge": {"k": "x", "d": "a bridge of woven blossoms over black water", "c": "wide view along the bridge", "f": "petals falling upward"},
        "the moonlight ring": {"k": "x", "d": "a ring of pale stones where the court dances — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "wisps circling the stones"},
        "the throne of thorns": {"k": "i", "d": "a throne grown from thorn and bone under a canopy of petals", "c": "centered view toward the throne", "f": "thorns framing the seat"},
        "the masquerade hall": {"k": "i", "d": "a hall of masks, mirrored pillars and endless music", "c": "wide centered view", "f": "masks watching from the walls"},
        "the dance of blades": {"k": "i", "d": "a courtly dueling floor where etiquette demands blood — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "blades laid at the edge"},
        "the bargain chamber": {"k": "i", "d": "a small room with a table, two chairs and a ledger of debts", "c": "medium view across the table", "f": "the ledger open and waiting"},
        "the sleeping garden": {"k": "i", "d": "a garden where sleepers lie in glass coffins among flowers", "c": "low gliding view", "f": "coffins nested in the blooms"},
        "the wish well": {"k": "i", "d": "a well of pale light that answers in whispers", "c": "high angle down at the well", "f": "light rising from the shaft"},
    },
    # --------------------------------------------------------- necropolis (new)
    "necropolis": {
        "the necropolis — third-eye view": {"k": "x", "d": "a city of tombs and silent avenues under a grey sun", "c": "high aerial view", "f": "avenues between mausoleums"},
        "the bone gates": {"k": "x", "d": "gates built from the ribs of something colossal", "c": "centered low angle", "f": "the ribs arching overhead"},
        "the silent avenue": {"k": "x", "d": "an avenue of statues with their faces worn away", "c": "centered one-point view", "f": "statues receding into mist"},
        "the pyre terrace": {"k": "x", "d": "a terrace of cold pyres and ash pits — the center is empty and clear", "c": "wide eye-level view", "f": "ash drifting across the stone"},
        "the crypt of kings": {"k": "i", "d": "a crypt of royal sarcophagi and dying candles", "c": "low eye-level view down the tombs", "f": "effigies receding into dark"},
        "the judgment pit": {"k": "i", "d": "a pit where the dead are tried, ringed by silent seats — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "the seats watching from above"},
        "the embalmer's hall": {"k": "i", "d": "stone tables, copper tools and jars of dark liquid", "c": "medium view across the tables", "f": "tools laid out in rows"},
        "the ossuary": {"k": "i", "d": "walls of stacked bones and skulls in candlelight", "c": "eye-level view along the walls", "f": "bones climbing out of frame"},
        "the wailing vault": {"k": "i", "d": "a vault where the air hums with unfinished grief", "c": "low view down the vault", "f": "shadows pressing inward"},
        "the black chapel": {"k": "i", "d": "a chapel of black stone with an empty altar", "c": "centered view down the aisle", "f": "the altar bare and waiting"},
    },
    # ------------------------------------------------------- dragon roost (new)
    "dragon roost": {
        "the roost — third-eye view": {"k": "x", "d": "a shattered peak with a colossal cavern mouth, smoke rising", "c": "high aerial view", "f": "the cavern yawning below"},
        "the ash fields": {"k": "x", "d": "fields of grey ash and cracked basalt", "c": "wide eye-level view", "f": "ash dunes to the horizon"},
        "the bone ridge": {"k": "x", "d": "a ridge of colossal bones half-buried in ash", "c": "wide side view", "f": "ribs arcing over the path"},
        "the lava falls": {"k": "x", "d": "a waterfall of molten rock pouring into a glowing lake — the center is empty and clear", "c": "wide view toward the falls", "f": "heat shimmer warping the air"},
        "the cavern hoard": {"k": "i", "d": "a cavern floor of gold, relics and shattered armor", "c": "wide view across the hoard", "f": "gold climbing the walls"},
        "the trial of flame": {"k": "i", "d": "a scorched ring of black rock where challengers are tested — the center is empty and clear", "c": "wide eye-level view from inside, the empty floor leading to the far side", "f": "flame veins cracking the floor"},
        "the roost nests": {"k": "i", "d": "colossal nests of bone and cloth on ledges above the drop", "c": "low angle up at the ledges", "f": "nests stacked into the dark"},
        "the molten forge": {"k": "i", "d": "a forge fed by a lava channel, tools scaled for giants", "c": "medium view across the anvil", "f": "sparks climbing into the dark"},
        "the scorched hall": {"k": "i", "d": "a hall of melted stone and fused banners", "c": "wide eye-level view", "f": "the far wall sagging inward"},
        "the egg chamber": {"k": "i", "d": "a warm chamber of stone eggs veined with light", "c": "medium view across the eggs", "f": "the eggs glowing faintly"},
    },
}

# Legacy per-world scope lists (kept for the API/UI compatibility helpers).
ENV_PLACE_KINDS: dict[str, dict] = {
    world: {"interiors": [n for n, p in places.items() if p.get("k") == "i"],
            "exteriors": [n for n, p in places.items() if p.get("k") != "i"]}
    for world, places in ENV_PLACES.items()
}


# ============================================================ PromptSpec

class PromptSpec:
    """One structured generation intent. Blocks are dicts of values;
    renderers convert them to final text. Fully serializable."""

    def __init__(self, kind: str = "character", intent: str = "cinematic",
                 mode: str = "structured", custom: str = "") -> None:
        self.kind = kind                # character | power | environment | photo
        self.intent = intent            # training | cinematic
        self.mode = mode                # structured | add | override
        self.custom = (custom or "").strip()
        self.blocks: dict[str, dict] = {}
        self.refs_active = False

    # ---- convenience -------------------------------------------------
    def set(self, block: str, **values: Any) -> "PromptSpec":
        self.blocks.setdefault(block, {}).update(values)
        return self

    def get(self, block: str, key: str, default: Any = "") -> Any:
        return self.blocks.get(block, {}).get(key, default)

    # ---- rendering ---------------------------------------------------
    def identity_txt(self, compact: bool = False) -> str:
        i = self.blocks.get("identity", {})
        if compact:
            return i.get("short", "")
        parts = []
        if i.get("presentation") and i.get("age"):
            parts.append(f"a {i['age']} {i['presentation']}")
        if i.get("hair"):
            parts.append(f"{i['hair']} hair")
        if i.get("eyes"):
            parts.append(f"{i['eyes']} eyes")
        if i.get("skin"):
            parts.append(f"{i['skin']} skin")
        if i.get("face"):
            parts.append(f"{i['face']} face")
        if i.get("body"):
            parts.append(f"{i['body']} build")
        mark = i.get("marks", "none")
        if mark and mark != "none":
            parts.append(f"with {mark} clearly visible")
        return ", ".join(p for p in parts if p)

    def design_txt(self) -> str:
        d = self.blocks.get("design", {})
        parts = []
        arch = d.get("archetype")
        if arch:
            parts.append(f"{arch}")
        lin = d.get("lineage")
        if lin and lin != "human":
            parts.append(f"{lin}")
        fac = d.get("faction")
        if fac:
            parts.append(f"of the {fac}")
        if d.get("palette"):
            parts.append(f"palette of {d['palette']}")
        cond = d.get("condition")
        if cond and cond not in ("pristine", ""):
            parts.append(f"{cond}")
        return ", ".join(p for p in parts if p)

    def wardrobe_txt(self) -> str:
        w = self.blocks.get("wardrobe", {})
        parts = []
        sil = w.get("silhouette")
        if sil:
            parts.append(sil)
        mats = w.get("materials") or []
        if mats:
            parts.append(" and ".join(mats) if len(mats) <= 2
                         else ", ".join(mats))
        if w.get("color"):
            parts.append(f"in {w['color']}")
        det = w.get("detail")
        if det:
            parts.append(det)
        acc = w.get("accessories") or w.get("jewelry")
        if acc:
            parts.append(acc)
        hw = w.get("headwear")
        if hw and hw != "none":
            parts.append(f"wearing a {hw}")
        cond = w.get("condition")
        if cond and cond != "pristine":
            parts.append(f"{cond}")
        return ", ".join(p for p in parts if p)

    def performance_txt(self) -> str:
        p = self.blocks.get("performance", {})
        parts = []
        expr = p.get("expression")
        if expr:
            vivid = _EMOTION_LOOKUP.get(expr, expr)
            intensity = p.get("expression_intensity", "normal")
            prefix = EXPRESSION_INTENSITY.get(intensity, "")
            text = prefix + vivid
            adv = EXPRESSION_ADVERBS.get(p.get("intensity") or "", "")
            if adv:
                text = f"{text} {adv}"
            parts.append(text)
        gaze = p.get("gaze")
        if gaze:
            parts.append(f"gaze {gaze}" if gaze != "camera"
                         else "looking directly at the camera")
        pose = p.get("pose")
        if pose:
            parts.append(pose)
        return ", ".join(p for p in parts if p)

    def shot_txt(self) -> str:
        s = self.blocks.get("shot", {})
        preset = s.get("preset") or "portrait"
        sh = SHOTS.get(preset, SHOTS["portrait"])
        parts = [sh["framing"], sh["angle"], sh["composition"]]
        lens = s.get("lens") or sh["lens"]
        parts.append(f"{lens} lens")
        return ", ".join(parts)

    def scene_txt(self) -> str:
        sc = self.blocks.get("scene", {})
        if self.kind == "environment":
            # PLACE FIRST: the subject noun must lead so "photorealistic
            # photograph of <place>" binds the realism to the actual room.
            # (Leading with photography words alone produced a mundane real
            # room with no arena in it.)
            parts = []
            kind = str(sc.get("place_kind") or "").strip()
            subject = (str(sc.get("place_desc") or "").strip()
                       or str(sc.get("place") or "").strip()
                       or str(sc.get("location") or "").strip())
            if subject:
                parts.append(subject)
            if kind == "interior":
                parts.append("an interior space")
            elif kind == "exterior":
                parts.append("an exterior view")
            arch = sc.get("architecture")
            if arch:
                parts.append(arch)
            mats = sc.get("materials") or []
            if mats:
                parts.append(", ".join(mats) if len(mats) > 2
                             else " and ".join(mats))
            if sc.get("scale"):
                parts.append(f"{sc['scale']} in scale")
            if kind == "interior":
                # interior spaces need enclosure cues or the model renders the
                # outside: ceiling, walls, floor, and light entering the room
                parts.append("enclosed interior space with a visible ceiling "
                             "and walls, light entering through openings")
            cond = sc.get("condition")
            if cond and cond != "pristine":
                parts.append(cond)
            feats = sc.get("features") or []
            if feats:
                parts.append("with " + ", ".join(feats))
            loc = sc.get("location_desc") or sc.get("location")
            if sc.get("place"):
                parts.append(f"place: {sc['place']}")
            if sc.get("camera"):
                parts.append(f"camera: {sc['camera']}")
            if sc.get("framing"):
                parts.append(str(sc["framing"]))
            if loc:
                parts.append(f"at {loc}")
            fantasy = sc.get("fantasy_features") or []
            if fantasy:
                parts.append(f"fantasy features: {', '.join(fantasy)}")
            if sc.get("time"):
                parts.append(f"time of day: {sc['time']}")
            if sc.get("weather"):
                parts.append(sc["weather"])
            if sc.get("atmosphere"):
                parts.append(f"{sc['atmosphere']} atmosphere")
            if sc.get("foreground"):
                parts.append(f"{sc['foreground']} in the foreground")
            pop = sc.get("population")
            if pop:
                parts.append(f"population: "
                             f"{POPULATION_DESC.get(pop, str(pop))}")
            # reference stills are BACKDROPS: an unobstructed view of the
            # space transfers; props and figures in the way do not
            if not pop or pop == "none":
                parts.append("empty and unobstructed, a clear view of the "
                             "location for a reference photograph")
            return ", ".join(p for p in parts if p)
        name = sc.get("scene") or ""
        if name == "neutral studio (no environment)":
            return "in a neutral studio, plain background"
        desc = sc.get("custom_desc") or SCENE_PLACE_DESC.get(name, "")
        place = f"in {desc}" if desc else ""
        loc = sc.get("location")
        parts = [p for p in (place, f"at {loc}" if loc else "") if p]
        return ", ".join(parts) if parts else ""

    def lighting_txt(self) -> str:
        li = self.blocks.get("lighting", {})
        name = li.get("setup") or "soft window light"
        desc = li.get("custom_desc") or LIGHTING_DESC.get(name, name)
        return desc

    def figure_txt(self) -> str:
        """The subject's action line (power ladder): silhouette + scale cue."""
        f = self.blocks.get("figure", {})
        parts = [str(f.get("text") or "").strip(),
                 str(f.get("notes") or "").strip()]
        return ", ".join(p for p in parts if p)

    def vfx_txt(self) -> str:
        v = self.blocks.get("vfx", {})
        if not v:
            return ""
        ptype = POWER_TYPES.get(v.get("type", ""), {})
        parts = []
        manif = v.get("manifestation") or "hands"
        parts.append(POWER_MANIFESTATION_DESC.get(manif, manif))
        behavior = v.get("behavior") or "crackling"
        parts.append(POWER_BEHAVIOR_DESC.get(behavior, behavior))
        intensity = v.get("intensity") or "steady"
        parts.append(POWER_INTENSITY_DESC.get(intensity, intensity))
        color = v.get("color") or ptype.get("color", "vivid")
        material = v.get("material") or ptype.get("material", "energy")
        parts.append(f"{color} {material}")
        glow = v.get("glow") or ptype.get("glow", "glowing")
        reaction = v.get("env_reaction") or "light reflection"
        parts.append(POWER_ENV_REACTION_DESC.get(reaction, reaction))
        # destruction ladder (H3 craft): scale + damage + aftermath turn a
        # glow into a set piece. Empty entries are skipped, so untuned
        # power batches render exactly as before.
        scale = str(v.get("scale") or "")
        if scale:
            parts.append(POWER_SCALE_DESC.get(scale, scale))
        dmg = str(v.get("destruction") or "")
        if dmg and POWER_DESTRUCTION_DESC.get(dmg):
            parts.append(POWER_DESTRUCTION_DESC[dmg])
        after = str(v.get("aftermath") or "")
        if after and POWER_AFTERMATH_DESC.get(after):
            parts.append(POWER_AFTERMATH_DESC[after])
        parts.append(glow)
        return ", ".join(parts)

    def power_sfx_txt(self) -> str:
        """Diegetic sound line for the H3 soundscape (empty when unknown)."""
        v = self.blocks.get("vfx", {})
        if not v:
            return ""
        ptype = POWER_TYPES.get(v.get("type", ""), {})
        return str(v.get("sfx") or ptype.get("sound_hint") or "")

    def realism_txt(self) -> str:
        base = TRAINING_REALISM if self.intent == "training" \
            else CINEMATIC_REALISM
        if self.kind == "environment":
            base = ENV_REALISM
        anatomy = self.blocks.get("realism", {}).get("anatomy", "")
        if anatomy and anatomy in ANATOMY_DEPTH:
            base = base.replace("natural skin texture",
                                ANATOMY_DEPTH[anatomy])
        return base

    # ---- final prompt ------------------------------------------------
    def to_prompt(self) -> str:
        """Block order per kind. Mode handling:
        structured -> blocks; add -> blocks + custom; override -> custom."""
        if self.mode == "override":
            return self.custom or ""
        parts: list[str] = [PHOTO_LEAD]
        if self.kind == "character":
            parts += [self.identity_txt(compact=self.refs_active),
                      self.design_txt(), self.performance_txt(),
                      self.wardrobe_txt(),
                      weapon_txt(self.blocks.get("weapon", {})),
                      self.shot_txt(), self.scene_txt(),
                      self.lighting_txt(), self.realism_txt()]
        elif self.kind == "power":
            p = self.blocks.get("power", {})
            pname = p.get("name") or "power"
            subj = self.get("power", "subject", "")
            head = f"photorealistic {pname} power"
            if subj and subj not in ("none", "generic figure"):
                head += f" wielded by {subj}"
            # COMPOSITION FIRST: image models weight the opening tokens most,
            # and leading with "power" re-triggered the training-data cliché
            # (the raised-hands meme). Shot/figure/action lead; the effect
            # follows; scene and lighting close.
            parts += [self.shot_txt(), self.figure_txt(), head,
                      self.vfx_txt(), self.scene_txt(),
                      self.lighting_txt(), self.realism_txt()]
        elif self.kind == "environment":
            # "photorealistic photograph of <PLACE>, ..." — realism bound to
            # the subject noun, photography cues after it, and the live-action
            # anchor LAST (fantasy nouns sit in the middle; without the tail
            # anchor the render drifts back into game-art — measured)
            parts += ["photorealistic photograph of " + self.scene_txt(),
                      ENV_REALISM, self.lighting_txt(), ENV_ANCHOR]
        elif self.kind == "weapon":
            # clean design reference: the still IS the weapon (product-style),
            # which transfers best into both reference models
            parts += [weapon_still_txt(self.blocks.get("weapon", {})),
                      self.lighting_txt(), self.realism_txt()]
        elif self.kind == "photo":
            parts = [self.custom]
        parts = [p for p in parts if p]
        if self.mode == "add" and self.custom and self.kind != "photo":
            parts.append(self.custom)
        return ", ".join(parts)

    def to_caption(self) -> str:
        """Training caption built from the SPEC, not the final prompt:
        variable traits emphasized, identity compact."""
        if self.kind == "character":
            i = self.blocks.get("identity", {})
            ident = i.get("short") or self.identity_txt(compact=True)
            perf = self.blocks.get("performance", {})
            var = [perf.get("expression", ""), perf.get("pose", "")]
            ward = self.blocks.get("wardrobe", {})
            var += [ward.get("silhouette", ""),
                    ward.get("materials", [""])[0] if
                    ward.get("materials") else ""]
            scene = self.blocks.get("scene", {})
            var.append(scene.get("scene", ""))
            return ", ".join(x for x in
                             ([ident] + [v for v in var if v]) if x)
        if self.kind == "power":
            p = self.blocks.get("power", {})
            v = self.blocks.get("vfx", {})
            return ", ".join(x for x in
                             [p.get("name", "power"),
                              v.get("manifestation", ""),
                              v.get("intensity", ""),
                              v.get("color", ""), v.get("behavior", "")] if x)
        if self.kind == "environment":
            sc = self.blocks.get("scene", {})
            li = self.blocks.get("lighting", {})
            return ", ".join(x for x in
                             [sc.get("location", ""), sc.get("scene", ""),
                              li.get("setup", ""),
                              self.get("scene", "atmosphere", "")] if x)
        return self.custom


    def estimate_tokens(self) -> int:
        """Estimate tokens for the compiled prompt."""
        return estimate_tokens(self.to_prompt())

    def compile(self, max_tokens: int | None = None) -> str:
        """Compile the spec to a model prompt, optionally constrained to max_tokens."""
        prompt = self.to_prompt()
        if max_tokens is not None and max_tokens > 0:
            return fit_to_token_budget(prompt, max_tokens)
        return prompt


# ====================================================== token budgeting

def estimate_tokens(text: str) -> int:
    """Robust token estimator for prompts.
    Heuristic ~1.3 tokens per word + punctuation weighting.
    Handles empty and malformed inputs gracefully."""
    if not text or not isinstance(text, str):
        return 0
    words = text.strip().split()
    if not words:
        return 0
    punct = sum(1 for c in text if c in ",;:.!?()[]{}")
    return max(1, int(len(words) * 1.3) + int(punct * 0.5))


def fit_to_token_budget(prompt: str, max_tokens: int = 77) -> str:
    """Trim trailing comma-separated clauses until within the token budget.
    Preserves highest priority leading clauses (composition / identity)."""
    if not prompt or not isinstance(prompt, str):
        return ""
    if estimate_tokens(prompt) <= max_tokens:
        return prompt
    clauses = [c.strip() for c in prompt.split(",") if c.strip()]
    while len(clauses) > 1 and estimate_tokens(", ".join(clauses)) > max_tokens:
        clauses.pop()
    return ", ".join(clauses)


# ====================================================== legacy translation

def _pick(p: dict, key: str, default: Any = "") -> Any:
    return p.get(key, default)


def character_spec_from_params(p: dict) -> PromptSpec:
    """Translate legacy + new character params into a PromptSpec."""
    spec = PromptSpec("character", _pick(p, "intent", "cinematic"),
                      _pick(p, "mode", "structured"),
                      _pick(p, "custom_prompt", ""))
    spec.refs_active = bool(_pick(p, "refs", []))
    hair_s = _pick(p, "hair_style", "")
    hair_c = _pick(p, "hair_color", "")
    eyes = _pick(p, "eye_color", "")
    e_shape = _pick(p, "eye_shape", "")
    face = ", ".join(x for x in [_pick(p, "face_shape", ""),
                                 _pick(p, "brows", ""),
                                 _pick(p, "nose", ""),
                                 _pick(p, "lips", ""),
                                 _pick(p, "facial_hair", "")] if x)
    ident = {"age": _pick(p, "age", "mid-30s"),
             "presentation": _pick(p, "gender", "woman")}
    if hair_s or hair_c:
        ident["hair"] = f"{hair_s} {hair_c}".strip()
    if eyes or e_shape:
        ident["eyes"] = f"{eyes} {e_shape}".strip()
    if _pick(p, "skin"):
        ident["skin"] = _pick(p, "skin")
    if face:
        ident["face"] = face
    if _pick(p, "build"):
        ident["body"] = _pick(p, "build")
    ident["marks"] = _pick(p, "distinctive", "none")
    hair_short = f"{hair_s} {hair_c}".strip()
    ident["short"] = ", ".join(x for x in (
        f"{_pick(p, 'age', 'mid-30s')} {_pick(p, 'gender', 'woman')}",
        f"{hair_short} hair" if hair_short else "",
    ) if x)
    spec.set("identity", **ident)
    d: dict[str, Any] = {}
    ward_defaults: dict[str, Any] = {}
    arch = _pick(p, "archetype", "")
    if arch and arch in ARCHETYPES:
        arch_data = ARCHETYPES[arch]
        d.update({k: v for k, v in arch_data.items()
                  if k not in ("materials", "headwear", "jewelry", "armor")})
        ward_defaults.update(
            {k: arch_data[k] for k in ("materials", "headwear", "jewelry",
                                       "armor") if k in arch_data})
        d["archetype"] = arch
        for k in ("palette", "condition"):
            if _pick(p, k):
                d[k] = _pick(p, k)
    d.setdefault("archetype", arch)
    d.setdefault("lineage", _pick(p, "lineage", ""))
    d.setdefault("faction", _pick(p, "faction", ""))
    d.setdefault("palette", _pick(p, "palette", ""))
    d.setdefault("condition", _pick(p, "condition", ""))
    spec.set("design", **d)
    mats = _pick(p, "materials", None)
    spec.set("wardrobe",
             silhouette=_pick(p, "outfit", ""),
             materials=mats or ward_defaults.get("materials") or [],
             color=_pick(p, "wardrobe_color", ""),
             detail=_pick(p, "detail", ""),
             accessories=_pick(p, "jewelry", "")
             or ward_defaults.get("jewelry", ""),
             headwear=_pick(p, "headwear", "")
             or ward_defaults.get("headwear") or "none",
             condition=_pick(p, "wardrobe_condition", ""))
    spec.set("performance", expression=_pick(p, "emotion", "stoic"),
             expression_intensity=_pick(p, "expression_intensity", "normal"),
             intensity=_pick(p, "intensity", ""),
             gaze=_pick(p, "gaze", ""), pose=_pick(p, "pose", ""))
    spec.set("shot", preset=_pick(p, "shot", "") or
             _pick(p, "angle", "portrait"), lens=_pick(p, "lens", ""))
    spec.set("scene", scene=_pick(p, "scene", "") or
             "neutral studio (no environment)",
             location=_pick(p, "scene_location", ""),
             custom_desc=_pick(p, "custom_scene", "")
             or _pick(p, "custom_desc", "")
             or _pick(p, "scene_desc", ""))
    spec.set("lighting", setup=_pick(p, "lighting", "") or
             "soft window light",
             custom_desc=_pick(p, "custom_lighting", ""))
    spec.set("realism", anatomy=_pick(p, "anatomy_depth", "normal"))
    return spec


def power_spec_from_params(p: dict) -> PromptSpec:
    spec = PromptSpec("power", _pick(p, "intent", "cinematic"),
                      _pick(p, "mode", "structured"),
                      _pick(p, "custom_prompt", ""))
    spec.set("power", name=_pick(p, "power", "")
             or p.get("power_type", "") or "arcane",
             subject=_pick(p, "subject", "silhouette figure"))
    spec.set("vfx",
             type=_pick(p, "power_type", "") or _pick(p, "power", "arcane"),
             manifestation=_pick(p, "manifestation", "aura"),
             behavior=_pick(p, "behavior", "flowing"),
             intensity=_pick(p, "intensity", "steady"),
             color=_pick(p, "color", ""),
             material=_pick(p, "material", ""),
             glow=_pick(p, "glow", ""),
             env_reaction=_pick(p, "env_reaction", "light reflection"))
    spec.set("figure", text=_pick(p, "figure", ""),
             notes=_pick(p, "notes", ""))
    spec.set("shot", preset=_pick(p, "shot", "medium"))
    spec.set("scene", scene=_pick(p, "scene", "") or
             "neutral studio (no environment)",
             location=_pick(p, "scene_location", ""),
             custom_desc=_pick(p, "custom_scene", "")
             or _pick(p, "custom_desc", "")
             or _pick(p, "scene_desc", ""))
    spec.set("lighting", setup=_pick(p, "lighting", "") or
             "dramatic chiaroscuro",
             custom_desc=_pick(p, "custom_lighting", ""))
    return spec


def env_places(world: str) -> dict:
    """Interior/exterior place NAME lists for one world (empty-safe)."""
    kinds = ENV_PLACE_KINDS.get(world) or {}
    if kinds:
        return {"interiors": list(kinds.get("interiors") or []),
                "exteriors": list(kinds.get("exteriors") or [])}
    return {"interiors": [],
            "exteriors": list(ENV_WORLDS.get(world, {}).get("locs") or [])}


def env_place_profile(world: str, name: str) -> dict:
    """The prompt profile for one place (kind/desc/camera/framing).

    Dash/case tolerant: the UI hands back the exact key, but a hand-written
    params payload may use a plain hyphen where the catalog has an em dash.
    """
    places = ENV_PLACES.get(world) or {}
    prof = places.get(name)
    if prof is None and name:
        want = str(name).replace("—", "-").replace("–", "-").strip().lower()
        for k, v in places.items():
            if k.replace("—", "-").replace("–", "-").strip().lower() == want:
                prof = v
                break
    if isinstance(prof, dict):
        return prof
    return {"k": "", "d": "", "c": "", "f": ""}


def env_spec_from_params(p: dict) -> PromptSpec:
    spec = PromptSpec("environment", _pick(p, "intent", "cinematic"),
                      _pick(p, "mode", "structured"),
                      _pick(p, "custom_prompt", ""))
    world = _pick(p, "world", "") or _pick(p, "type", "royal palace")
    mats = _pick(p, "materials", None) or \
        ENV_WORLDS.get(world, {}).get("materials", [])
    feats = _pick(p, "features", None) or \
        ENV_WORLDS.get(world, {}).get("features", [])
    spec.set("scene", world=world, scene=world,
             place=_pick(p, "place", ""),
             place_kind=_pick(p, "place_kind", ""),
             location=_pick(p, "location", ""),
             architecture=ENV_WORLDS.get(world, {}).get("architecture", ""),
             materials=mats,
             scale=ENV_WORLDS.get(world, {}).get("scale", ""),
             condition=_pick(p, "condition", "pristine"),
             features=feats,
             fantasy_features=_pick(p, "fantasy_features", []) or [],
             time=_pick(p, "time", "night"),
             weather=_pick(p, "weather", ""),
             atmosphere=_pick(p, "atmosphere", "epic"),
             foreground=_pick(p, "foreground", ""),
             population=_pick(p, "population", "none"))
    spec.set("shot", preset=_pick(p, "shot", "wide"))
    spec.set("lighting", setup=_pick(p, "lighting", "") or
             "overcast daylight",
             custom_desc=_pick(p, "custom_lighting", ""))
    return spec


def weapon_spec_from_params(p: dict) -> PromptSpec:
    """Weapon design stills: the clean product-style reference images.

    These feed the weapon slots (extra H3 pictures) and, more importantly,
    the loadout composites are built from the same config, so the weapon the
    character holds matches the weapon reference exactly.
    """
    spec = PromptSpec("weapon", _pick(p, "intent", "cinematic"),
                      _pick(p, "mode", "structured"),
                      _pick(p, "custom_prompt", ""))
    spec.set("weapon",
             type=_pick(p, "weapon_type", "") or _pick(p, "type", "custom"),
             material=_pick(p, "material", ""),
             style=_pick(p, "style", ""),
             size=_pick(p, "size", ""),
             color=_pick(p, "color", ""),
             energy=_pick(p, "energy", "none"),
             glow=_pick(p, "glow", ""),
             ornament=_pick(p, "ornament", "none"),
             condition=_pick(p, "condition", "pristine"),
             handling=_pick(p, "handling", "no hands"))
    spec.set("lighting", setup=_pick(p, "lighting", "") or "studio softbox",
             custom_desc=_pick(p, "custom_lighting", ""))
    spec.set("realism", anatomy=_pick(p, "anatomy_depth", "detailed"))
    return spec


def spec_from_params(mode: str, params: dict) -> PromptSpec:
    if mode == "character":
        return character_spec_from_params(params)
    if mode == "power":
        return power_spec_from_params(params)
    if mode == "environment":
        return env_spec_from_params(params)
    if mode == "weapon":
        return weapon_spec_from_params(params)
    return PromptSpec("photo", _pick(params, "intent", "cinematic"),
                      _pick(params, "mode", "structured"),
                      _pick(params, "prompt", ""))


# ========================================================== system presets
SYSTEM_PRESETS: dict[str, dict] = {
    "characters": {
        "Royal Princess": {"archetype": "royal princess"},
        "Dark Princess": {"archetype": "dark princess"},
        "Warrior Princess": {"archetype": "warrior princess"},
        "Elven Queen": {"archetype": "elven queen"},
        "Battle Mage": {"archetype": "battle mage"},
        "Dark Knight": {"archetype": "dark knight"},
        "Vampire Noble": {"archetype": "vampire noble"},
        "Celestial Warrior": {"archetype": "celestial warrior"},
        "Queen": {"archetype": "queen"},
        "Elven Royal": {"archetype": "elven royal"},
        "Knight": {"archetype": "knight"},
        "Mage": {"archetype": "mage"},
        "Assassin": {"archetype": "assassin"},
        "Dark Lord": {"archetype": "dark lord"},
        "Celestial": {"archetype": "celestial"},
    },
    "powers": {
        "Lightning": {"power_type": "lightning", "color": "electric cyan",
                      "behavior": "crackling", "manifestation": "hands"},
        "Fire": {"power_type": "fire", "color": "molten orange",
                 "behavior": "blazing", "manifestation": "hands"},
        "Divine": {"power_type": "divine", "color": "radiant gold",
                   "behavior": "radiating", "manifestation": "aura"},
        "Shadow": {"power_type": "shadow", "color": "deep purple-black",
                   "behavior": "flowing", "manifestation": "aura"},
        "Arcane": {"power_type": "arcane", "color": "violet-blue",
                   "behavior": "swirling", "manifestation": "orb"},
        "Telekinesis": {"power_type": "force", "color": "violet",
                        "behavior": "pulsing", "manifestation": "hands"},
        "Ice": {"power_type": "ice", "color": "pale blue",
                "behavior": "streaming", "manifestation": "hands"},
        "Nature": {"power_type": "nature", "color": "emerald green",
                   "behavior": "flowing", "manifestation": "aura"},
        "Void": {"power_type": "void", "color": "black with violet rim",
                 "behavior": "swirling", "manifestation": "shield"},
    },
    "environments": {
        "Royal Palace": {"world": "royal palace"},
        "Magical City": {"world": "magical city"},
        "Dark Castle": {"world": "dark castle"},
        "Enchanted Forest": {"world": "enchanted forest"},
        "Ancient Temple": {"world": "ancient temple"},
        "Frozen Kingdom": {"world": "frozen kingdom"},
        "Floating Realm": {"world": "floating realm"},
        "Celestial Realm": {"world": "celestial realm"},
        "Void Realm": {"world": "void realm"},
        "Mountain Fortress": {"world": "mountain fortress"},
        "Ruins": {"world": "ruins"},
        "Battlefield": {"world": "battlefield"},
    },
}
