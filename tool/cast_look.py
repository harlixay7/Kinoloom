"""A cast member's look text against the picture it was picked from.

Every video prompt, key image and story prompt describes a person with the
character's Body and Outfit text. The picture is what the models see, so the
text has to describe the picture: a story AI cannot see it, and a look it
guessed ("a weathered exile in their thirties" for a young woman in a silver
dress) pulls every render away from the reference and leaves the gender
unsaid.

The truth is the Casting record of the character's primary picture: the
identity and outfit text cast_import writes from it (the Autofill text), its
presentation and its age. `look_source` on the character says where its text
came from:

  "casting:<type>/<batch>/<file>"  filled from that picture
  "story"                          written by a story AI
  "hand"                           typed by the user: never replaced, never
                                   flagged
  (missing)                        older records: flagged, never replaced

`check` says when the text contradicts the picture: the presentation unsaid or
contradicted, another age, other clothes. `refresh` gives the text that
follows a new primary picture.

Places work the same way: `place_text` says what a place picture shows, from
its record, so the story and prompt writers never invent a set the
background reference does not have.
"""
from __future__ import annotations

import json
import re
import time

HAND = "hand"
STORY = "story"
CASTING = "casting:"

_GENDER = {
    "woman": ("woman", "women", "female", "lady", "girl", "she", "her", "hers", "herself"),
    "man": ("man", "men", "male", "guy", "boy", "he", "him", "his", "himself"),
}
_GENDER_WORDS = set(_GENDER["woman"]) | set(_GENDER["man"])
# decades a Casting age covers, and the decades an age phrase in a text names
_AGE_DECADES = {"young adult": {10, 20}, "mid-20s": {20}, "mid-30s": {30}, "40s": {40},
                "mature": {50, 60, 70}, "older": {50, 60, 70}}
_AGE_WORDS = (
    (r"\bteen(?:s|age|ager)?\b", {10}), (r"\b(?:twenties|20s)\b", {20}),
    (r"\b(?:thirties|30s)\b", {30}), (r"\b(?:forties|40s)\b", {40}),
    (r"\b(?:fifties|50s)\b", {50}), (r"\b(?:sixties|60s|seventies|70s)\b", {60, 70}),
    (r"\bmiddle[- ]aged\b", {40, 50}), (r"\belderly\b", {60, 70}),
    (r"\bold (?:man|woman|person)\b", {60, 70}), (r"\b(?:young|youthful)\b", {10, 20}),
)
_AGE_YEARS = re.compile(r"\b([1-9]\d)[- ]?(?:years?|yrs?)(?:[- ]old)?\b")
_PRESENTATION = {"woman": "woman", "female": "woman", "f": "woman",
                 "man": "man", "male": "man", "m": "man"}
# clothes, to compare what a text and a picture say a person wears (a shared
# colour or material is not the same clothes: "leathers" is no dress)
_GARMENTS = (
    "dress", "gown", "robe", "tunic", "shirt", "blouse", "sweater", "jumper", "cardigan", "hoodie",
    "jacket", "coat", "trench", "cloak", "cape", "mantle", "poncho", "vest", "waistcoat", "suit",
    "uniform", "armor", "armour", "breastplate", "chainmail", "mail", "plate", "leathers",
    "trousers", "pants", "jeans", "skirt", "shorts", "leggings", "kimono", "sari", "toga",
    "nightwear", "nightgown", "pajamas", "lingerie", "swimsuit", "bodysuit", "jumpsuit",
    "overalls", "apron", "scrubs", "tuxedo", "corset", "bodice", "doublet", "kilt", "parka",
)


def source(char: dict) -> str:
    return str((char or {}).get("look_source") or "")


def is_identity(entry: dict) -> bool:
    """A picture of the character as they are: untagged, or an identity plate
    from a batch import (`plate:close|medium|full`); any other tag names an
    outfit for scenes (a loadout composite)."""
    tag = str(entry.get("tag") or "").strip()
    return not tag or tag.startswith("plate:")


def primary_face(char: dict) -> dict | None:
    """The reference that decides the look: the primary identity picture,
    else the first one."""
    refs = [r for r in (char or {}).get("reference_images") or []
            if isinstance(r, dict) and str(r.get("type") or "face") == "face"
            and is_identity(r) and r.get("file") and r.get("name")]
    return next((r for r in refs if r.get("role") == "primary"), refs[0] if refs else None)


def picture_look(entry: dict | None) -> dict | None:
    """What a Casting picture was made with: {body, outfit, weapon, handling,
    presentation, age, from}; None when the picture has no record (an upload,
    a deleted batch)."""
    if not entry or not entry.get("name") or not entry.get("file"):
        return None
    key = (str(entry.get("type") or "face"), str(entry["name"]), str(entry["file"]))
    hit = _LOOKS.get(key)
    if hit and time.monotonic() - hit[0] < _LOOK_TTL_S:
        return dict(hit[1])
    look = _record_look(*key)
    if look is None:
        _LOOKS.pop(key, None)
        return None
    if len(_LOOKS) >= 512:
        _LOOKS.clear()
    _LOOKS[key] = (time.monotonic(), look)
    return dict(look)


# every project load asks for each character's record; a batch made again
# under the same name can reuse a file name, so a record is kept only briefly
_LOOKS: dict[tuple[str, str, str], tuple[float, dict]] = {}
_LOOK_TTL_S = 60.0


def _as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        out = json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _record_look(dtype: str, name: str, file: str) -> dict | None:
    try:
        import datasets_db
        row = next((r for r in datasets_db.list_images(dtype, name, limit=200000)
                    if r["file"] == file), None)
        if row is None:
            return None
        run = datasets_db.get_run(int(row["run_id"])) if row.get("run_id") else None
    except Exception:  # noqa: BLE001 - no record is the answer, never a failure
        return None
    # list_images hands variations over parsed, a raw row as JSON text
    variations, params = _as_dict(row.get("variations")), _as_dict((run or {}).get("params"))
    from cast_import import _identity_text, _outfit_text
    return {"body": _identity_text(params), "outfit": _outfit_text(params, variations),
            "weapon": str(variations.get("weapon") or ""),
            "handling": str(variations.get("handling") or ""),
            "presentation": str(params.get("presentation") or params.get("gender") or "").strip().lower(),
            "age": str(params.get("age") or "").strip().lower(),
            "from": f"{dtype}/{name}/{file}"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", str(text or "").lower()))


def _garments(text: str) -> set[str]:
    words = _words(text)
    return {g for g in _GARMENTS if g in words or f"{g}s" in words}


def _ages(text: str) -> set[int]:
    out: set[int] = set()
    low = str(text or "").lower()
    for pat, decades in _AGE_WORDS:
        if re.search(pat, low):
            out |= decades
    out |= {int(m.group(1)) // 10 * 10 for m in _AGE_YEARS.finditer(low)}
    return out


def problems(body: str, outfit: str, look: dict) -> list[str]:
    """How the Body/Outfit text contradicts the picture's record (empty: it
    does not)."""
    out: list[str] = []
    words = _words(f"{body} {outfit}")
    pres = _PRESENTATION.get(str(look.get("presentation") or "").strip().lower(), "")
    if pres:
        other = next(g for g in _GENDER if g != pres)
        if words & set(_GENDER[other]) and not words & set(_GENDER[pres]):
            out.append(f"it describes a {other}, the picture is of a {pres}")
        elif not words & set(_GENDER[pres]):
            out.append(f"it does not say this is a {pres}")
    age = str(look.get("age") or "").strip().lower()
    want = _AGE_DECADES.get(age) or _ages(age)
    said = _ages(body)
    if want and said and not said & want:
        out.append(f"it gives another age than the picture ({look['age']})")
    pic_clothes, text_clothes = _garments(look.get("outfit") or ""), _garments(outfit)
    if pic_clothes and text_clothes and not pic_clothes & text_clothes:
        out.append("its outfit is other clothes than the picture shows "
                   f"({', '.join(sorted(pic_clothes))})")
    return out


def check(char: dict) -> dict | None:
    """The character's look text against its primary picture: None when there
    is nothing to compare (no picture with a record, or text typed by hand),
    else {ok, problems, body, outfit, from} with the picture's text."""
    if source(char) == HAND:
        return None
    look = picture_look(primary_face(char))
    if not look or not (look.get("body") or look.get("outfit")):
        return None
    body, outfit = str(char.get("body") or ""), str(char.get("outfit") or "")
    found = problems(body, outfit, look) if (body.strip() or outfit.strip()) else ["it is empty"]
    return {"ok": not found, "problems": found, "body": look["body"], "outfit": look["outfit"],
            "from": look["from"]}


def body_text(char: dict) -> str:
    """The Body text as a prompt states it: with the gender of the main
    picture's record put first when the text names none. A prompt that never
    says "woman" lets the model drift (clips of a young woman read as a man)."""
    body = str((char or {}).get("body") or "").strip()
    if _words(body) & _GENDER_WORDS:
        return body
    try:
        look = picture_look(primary_face(char)) or {}
    except Exception:  # noqa: BLE001 - the text as written, never a failure
        look = {}
    pres = _PRESENTATION.get(str(look.get("presentation") or "").strip().lower(), "")
    if not pres:
        return body
    return f"a {pres}, {body}" if body else f"a {pres}"


# ------------------------------------------------------------------ places
def _place_record(entry: dict) -> dict | None:
    name, file = str(entry.get("name") or ""), str(entry.get("file") or "")
    if not name or not file:
        return None
    key = ("place record", name, file)       # never a face key: picture_look keeps looks there
    hit = _LOOKS.get(key)
    if hit and time.monotonic() - hit[0] < _LOOK_TTL_S:
        return dict(hit[1])
    try:
        import datasets_db
        row = next((r for r in datasets_db.list_images(str(entry.get("type") or "environment"), name,
                                                       limit=200000) if r["file"] == file), None)
        run = datasets_db.get_run(int(row["run_id"])) if row and row.get("run_id") else None
    except Exception:  # noqa: BLE001 - no record is the answer, never a failure
        return None
    if row is None:
        return None
    rec = {"variations": _as_dict(row.get("variations")), "params": _as_dict((run or {}).get("params"))}
    _LOOKS[key] = (time.monotonic(), rec)
    return dict(rec)


def place_text(entry: dict | None) -> str:
    """What a place picture shows, in words, from what it was made with: "a
    vast paved courtyard ringed by colonnades and a central fountain (the
    grand courtyard, royal palace, exterior, overcast daylight)". The light
    is the batch's lighting, which the picture was prompted with; a still's
    own time is only a fallback (the measured courtyard says "night" and
    shows overcast daylight). "" without a record."""
    rec = _place_record(entry or {}) if entry else None
    if not rec:
        return ""
    v, p = rec["variations"], rec["params"]
    desc = str(v.get("place_desc") or v.get("location") or p.get("location") or "").strip()
    where = str(v.get("location") or "").strip()
    bits = [where if where and where.lower() not in desc.lower() else "",
            str(p.get("world") or "").strip(), str(v.get("place_kind") or "").strip(),
            str(p.get("lighting") or v.get("time") or "").strip()]
    bits = [b for b in bits if b]
    if not desc:
        return ", ".join(bits)
    return desc + (f" ({', '.join(bits)})" if bits else "")


def place_entry(loc: dict, tag: str = "", path: str = "") -> dict | None:
    """The reference entry of a place view: the one whose project copy is
    `path` (what the shot renders with), else the one tagged `tag`, else the
    main (untagged) picture."""
    from pathlib import PureWindowsPath as Path     # \ and / alike, on any OS
    refs = [r for r in (loc or {}).get("reference_images") or [] if isinstance(r, dict) and r.get("file")]
    name = Path(path).name if path else ""
    if name:
        hit = next((r for r in refs if name in (str(r.get("copied") or ""), Path(str(r["file"])).name)), None)
        if hit:
            return hit
    want = str(tag or "").strip().lower()
    if want:
        return next((r for r in refs if str(r.get("tag") or "").strip().lower() == want), None)
    return next((r for r in refs if not str(r.get("tag") or "").strip()), None)


def place_views(loc: dict) -> list[tuple[str, str]]:
    """(view name, what it shows) for each picture of a place: the tagged
    views by their tag, the main picture as ""."""
    out, seen = [], set()
    for r in (loc or {}).get("reference_images") or []:
        if not isinstance(r, dict):
            continue
        tag = str(r.get("tag") or "").strip()
        if tag in seen:
            continue
        seen.add(tag)
        out.append((tag, place_text(r)))
    return out


def checks(chars: dict) -> dict:
    """{character id: check} for the cast members in the movie whose look
    text contradicts their picture."""
    out = {}
    for cid, char in (chars or {}).items():
        if not isinstance(char, dict) or char.get("enabled") is False:
            continue
        try:
            c = check(char)
        except Exception:  # noqa: BLE001 - a warning never breaks a page load
            continue
        if c and not c["ok"]:
            out[str(cid)] = c
    return out


def apply(char: dict, look: dict) -> dict:
    """`char` with the picture's look text, marked as coming from it."""
    out = dict(char)
    for k in ("body", "outfit"):
        if look.get(k):
            out[k] = look[k]
    out["look_source"] = CASTING + str(look.get("from") or "")
    return out


def refresh(char: dict, entry: dict) -> tuple[dict, dict]:
    """After `entry` became the primary picture: the character with its look
    text following it, and the fields that changed. Text from Casting or a
    story follows the picture; an empty field is filled; text typed by hand
    and older text of unknown origin are kept (check flags the latter)."""
    look = picture_look(entry)
    if not look:
        return char, {}
    src = source(char)
    follows = src.startswith(CASTING) or src == STORY
    out, changed = dict(char), {}
    for k in ("body", "outfit"):
        if look.get(k) and (follows or not str(char.get(k) or "").strip()) and char.get(k) != look[k]:
            out[k] = changed[k] = look[k]
    if changed and (follows or not src):
        out["look_source"] = CASTING + look["from"]
    return out, changed
