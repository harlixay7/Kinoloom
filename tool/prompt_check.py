"""Prompt checks and the cast sheet for Video Studio renders.

How each model reads a prompt:

  MiniMax H3  For every attached image the text encoder writes the label
              "<Picture N>: " and then the image, in slot order (pic1..pic6,
              the background last), and then the prompt as plain text
              (comfy/text_encoders/minimax.py in ComfyUI_LTX). A prompt points
              at an image only through that exact label. The first frame is
              pinned to the start image, the previous clip's last frame, or
              else Picture 1 (video_studio._apply_references_h3).
  LTX-2.5     The images ride the MSR guide's slots (pic1..pic4 for people,
              background) and the prompt is encoded on its own: there are no
              labels, people are matched by how the prompt describes them. The
              guide needs pic1. A start image takes pic1, the strongest slot,
              and the people move down one slot.
  Wan 2.2     No reference images. A start image is the first frame.

plan() works out what a request renders with, from the functions the renders
use (video_studio.resolve_references for a clip; video_studio.chain_cast and
chain_studio for a chain). check() reads the prompts against that plan.
Errors are certain problems: the render would fail, an image the request names
does not reach the model, or a prompt points at a picture through a label the
model does not read. They stop Generate. Warnings are advice and never block.
cast_sheet() writes the brief for the agent that writes the prompts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import video_models
import video_studio

# planning runs the renders' own reference functions; their log lines belong
# to renders, not to a check that runs while the prompt is being typed
_QUIET = logging.getLogger("prompt_check.plan")
_QUIET.disabled = True

_SLOTS = video_studio._REF_SLOT_ORDER          # pic1..pic6, background
_PEOPLE_SLOTS = _SLOTS[:-1]


# ------------------------------------------------------------------ results
@dataclass
class Issue:
    level: str                      # "error" stops Generate, "warning" never does
    code: str
    message: str
    segment: int | None = None      # 1-based; None: the request as a whole
    # text edits that resolve it: {start, end, find, replace, label}, offsets
    # into that segment's prompt
    fixes: list[dict] = field(default_factory=list)


@dataclass
class Ref:
    """One image the model receives, in the model's order."""
    slot: str                       # the slot it rides; "" when it is not a reference
    name: str                       # the name the user gave it; "" when none (_given_name)
    kind: str                       # person, environment, start image, previous clip, ...
    look: str = ""
    picture: int = 0                # MiniMax H3: the N of its <Picture N> label
    file: str = ""                  # the reference image's file name, for the cast sheet
    path: str = field(default="", repr=False)   # stays on the server

    @property
    def label(self) -> str:
        """How the checks and the cast sheet refer to it."""
        if self.name:
            return self.name
        if self.kind == "start image":
            return "the start image"
        if self.kind == "previous clip":
            return "the previous clip's last frame"
        return Path(self.file).stem or self.slot or "reference"

    def described(self) -> str:
        """'Ruth (person, image ruth.png)' or 'a person (image ...)' for the sheet."""
        image = f"image {self.file}" if self.file else ""
        if self.name:
            return f"{self.name} ({', '.join(x for x in (self.kind, image) if x)})"
        article = "an" if self.kind[:1] in "aeiou" else "a"
        return f"{article} {self.kind}" + (f" ({image})" if image else "")


@dataclass
class Plan:
    model: str
    label: str
    family: str                     # "h3", "ltx", "wan" or ""
    segments: int
    chain: str | None = None        # None: one clip; "seamless" or "tail-frame"
    first_s: float = 0.0            # seconds segment 1 covers
    later_s: float = 0.0            # seconds each later segment's prompt covers
    carry_s: float = 0.0            # of those, the end of the segment before, carried over
    refs: list[Ref] = field(default_factory=list)
    opening: str = ""               # "start image", "previous clip", "picture" or ""
    opening_ref: Ref | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def pictures(self) -> list[Ref]:
        return [r for r in self.refs if r.picture]

    def summary(self) -> dict:
        return {"model": self.model, "label": self.label, "family": self.family,
                "segments": self.segments, "chain": self.chain,
                "first_s": self.first_s, "later_s": self.later_s, "carry_s": self.carry_s,
                "opening": self.opening,
                "refs": [{"slot": r.slot, "name": r.name, "label": r.label, "kind": r.kind,
                          "picture": r.picture, "look": r.look} for r in self.refs]}


def _tag(n: int) -> str:
    return f"<Picture {n}>"


def _fix(start: int, end: int, find: str, replace: str, label: str) -> dict:
    return {"start": start, "end": end, "find": find, "replace": replace, "label": label}


def _kind(ref_type) -> str:
    t = str(ref_type or "").lower()
    if t in ("", "face", "character"):
        return "person"
    if t == "power":
        return "power or effect"
    return t


def _ref_label(r: dict) -> str:
    """How a message names a requested reference: its name, else its file name."""
    return (str(r.get("name") or "").strip()
            or Path(str(r.get("file") or r.get("path") or "")).stem
            or str(r.get("slot") or "reference"))


def _given_name(r: dict, path: str) -> str:
    """The name the user gave a reference: "" when there is none or when it is
    only the image's file name, which the page fills in for a disk upload. A
    file name is no cast name: the sheet must not hand it to the prompt writer."""
    name = str(r.get("name") or "").strip()
    stems = {Path(str(r.get(k) or "")).stem for k in ("file", "path")} | {Path(path).stem}
    return "" if name in stems else name


def _user_ref(slot: str, r: dict, path: str) -> Ref:
    return Ref(slot, _given_name(r, path), _kind(r.get("type")), _look(r, path),
               file=Path(path).name, path=path)


def _look(r: dict, path: str) -> str:
    """A person's look: the reference's own description, else what its face
    dataset was generated from (chain_studio.cast_descriptions, the text LTX
    chains add to every segment)."""
    if _kind(r.get("type")) != "person" or r.get("slot") == "background":
        return ""
    import chain_studio
    found = chain_studio.cast_descriptions([{
        "slot": r.get("slot"), "path": path, "type": r.get("type") or "face",
        "name": r.get("name") or "", "description": str(r.get("description") or "")[:400]}])
    return found[0] if found else ""


def _names(items: list) -> str:
    """"a", "a and b", "a, b and c"."""
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------- plan
def plan(model: str, segments: int = 1, *, references: list[dict] | None = None,
         start_image: str | None = None, continuation: str | None = None,
         start_from_clip: bool = False, frames: int | None = None) -> Plan:
    """What a Video Studio request renders with: one clip (segments == 1) or a
    chain, the images each model receives in its own order, and the image the
    render opens on. `start_from_clip`: a chain's start image is the previous
    clip's last frame (the ↪ button), which only changes how it is described."""
    entry = video_models.VIDEO_MODELS.get(model) or {}
    fam = str(entry.get("family") or "")
    p = Plan(model=model, label=str(entry.get("display_name") or model),
             family=next((f for f in ("h3", "ltx", "wan") if fam.startswith(f)), ""),
             segments=max(1, int(segments or 1)))
    st = video_models.model_status(model)
    if not st.get("available"):
        why = st.get("reason") or "missing " + ", ".join(map(str, st.get("missing") or []))
        p.issues.append(Issue("error", "model", f"{model} is not available: {why}."))
        return p
    if p.segments > 1:
        p.chain = "seamless" if video_studio.seamless_engine(model) else "tail-frame"
    _durations(p, entry, frames)

    refs = _user_refs(p, entry, references)
    start_path = _start_path(p, start_image)
    cont_file = None
    if continuation and p.chain is None:
        from core import InvalidInput, resolve_user_path
        try:
            cont_file = resolve_user_path(continuation)
        except (InvalidInput, FileNotFoundError) as e:
            p.issues.append(Issue("error", "continuation", f"continuation frame unusable: {e}."))
    if p.chain is None:
        _plan_clip(p, entry, refs, start_image if start_path else None, start_path, cont_file)
    else:
        _plan_chain(p, entry, refs, start_path, start_from_clip)
    return p


def _durations(p: Plan, entry: dict, frames: int | None) -> None:
    import chain_graphs as cg
    import chain_studio as cs
    fps = float(entry.get("fps") or 24)
    f = int(frames or (entry.get("defaults") or {}).get("frames") or 121)
    if p.chain == "seamless" and p.family == "ltx":
        lf, carry = cs.ltx_link_frames(f, p.segments), cg.LTX_CARRY_FRAMES
    elif p.chain == "seamless" and p.family == "h3":
        lf, carry = cs.h3_link_frames(f, p.segments), cg.H3_CARRY_FRAMES
    else:
        lf, carry = [f] * p.segments, 0
    p.first_s = round(lf[0] / fps, 2)
    if p.segments > 1:
        p.later_s, p.carry_s = round(lf[1] / fps, 2), round(carry / fps, 2)


def _user_refs(p: Plan, entry: dict, references: list[dict] | None) -> list[tuple[dict, Path]]:
    """The request's references that can render: a known slot, one per slot,
    an image that resolves. Anything else is an error, since the cast the
    prompt describes would not be the one the model receives (and H3's Picture
    numbers would shift)."""
    from ltx_refs import resolve_reference_entry
    given = [r for r in references or [] if isinstance(r, dict)]
    if given and "refs" not in (entry.get("supported_controls") or []):
        p.issues.append(Issue("warning", "refs_ignored",
                              f"{p.label} takes no reference images, so the {len(given)} "
                              f"attached are not used."))
        return []
    out, seen = [], set()
    for r in given:
        slot, name = str(r.get("slot") or ""), _ref_label(r)
        if slot not in _SLOTS:
            p.issues.append(Issue("error", "ref_slot", f"{name}: unknown slot {slot!r}."))
        elif slot in seen:
            p.issues.append(Issue("error", "ref_slot",
                                  f"{name}: slot {slot} is used twice. Give each reference its own slot."))
        elif (path := resolve_reference_entry(r)) is None:
            p.issues.append(Issue("error", "ref_missing",
                                  f"{name} ({slot}): the image cannot be found. Pick it again or remove it."))
        else:
            seen.add(slot)
            out.append((r, path))
    return out


def _start_path(p: Plan, start_image: str | None) -> Path | None:
    if not start_image:
        return None
    name = str(start_image)
    if not video_studio._is_bare_filename(name):
        p.issues.append(Issue("error", "start_image", "start_image must be a filename in a "
                                                      "ComfyUI input folder (use the upload button)."))
        return None
    path = video_studio._resolve_start_image_path(name)
    if path is None:
        p.issues.append(Issue("error", "start_image", f"start image {name!r} is not in a ComfyUI "
                                                      f"input folder. Upload it again."))
    return path


def _plan_clip(p: Plan, entry: dict, refs: list[tuple[dict, Path]], start_image: str | None,
               start_path: Path | None, cont_file: Path | None) -> None:
    if p.family == "wan":
        # Wan opens on the start image natively, else on the previous clip's frame
        p.opening = "start image" if start_path else ("previous clip" if cont_file else "")
        if start_path and cont_file:
            p.issues.append(Issue("warning", "continuation_unused",
                                  "The previous clip's last frame is not used: the start image opens the clip."))
        return
    clean, cont_path, _ = video_studio.resolve_references(
        p.model, [r for r, _ in refs], start_image=start_image, continuation=cont_file, log=_QUIET)
    by_path = {str(path): r for r, path in refs}

    def ref(r: dict) -> Ref:
        path = str(r.get("path") or "")
        if path not in by_path:
            if start_path is not None and path == str(start_path):
                return Ref(r["slot"], "", "start image", path=path)
            if cont_file is not None and path == str(cont_file):
                return Ref(r["slot"], "", "previous clip", path=path)
        return _user_ref(r["slot"], by_path.get(path, r), path)

    if p.family == "h3":
        p.refs = [ref(r) for r in sorted(clean, key=video_studio._slot_rank)]
        for i, r in enumerate(p.refs):
            r.picture = i + 1
        if cont_path:
            # the pinned first frame, beside the pictures: the start image or the tail
            kind = "start image" if start_path is not None and cont_path == str(start_path) else "previous clip"
            anchor = Ref("", "", kind, path=cont_path)
            p.opening, p.opening_ref = anchor.kind, anchor
        elif p.refs:
            first = p.refs[0]
            p.opening = first.kind if first.kind in ("start image", "previous clip") else "picture"
            p.opening_ref = first
        used = {r.path for r in p.refs} | {cont_path or ""}
    elif p.family == "ltx":
        nodes = entry.get("ref_nodes") or {}
        if clean and "pic1" not in {r["slot"] for r in clean}:
            p.issues.append(Issue("error", "no_pic1",
                                  "LTX needs a person in pic1: without it this render fails. "
                                  "Move the lead person to pic1, or add a start image."))
        p.refs = [ref(r) for r in sorted(clean, key=video_studio._slot_rank) if r["slot"] in nodes]
        opener = next((r for r in p.refs if r.kind in ("start image", "previous clip")), None)
        if opener:
            p.opening, p.opening_ref = opener.kind, opener
        used = {r.path for r in p.refs}
    else:
        return
    _unused(p, entry, refs, used, start_path)
    if start_path is not None and str(start_path) not in used:
        why = ("the clip opens on the previous clip's last frame instead" if cont_file is not None
               else "this tier has no reference graph to carry it (choose a refs tier)"
               if not video_models.takes_reference_images(p.model) else "")
        p.issues.append(Issue("warning", "start_unused", "The start image is not used"
                              + (f": {why}." if why else " in this render.")))
    if cont_file is not None and str(cont_file) not in used:
        why = ("the start image takes pic1 and the people move down one slot, so there is no "
               "room for it" if start_path is not None and str(start_path) in used
               else "every LTX person slot it could take (pic2 to pic4) is taken")
        p.issues.append(Issue("warning", "continuation_unused",
                              f"The previous clip's last frame is not used: {why}. "
                              f"Remove one reference to keep it."))


def _plan_chain(p: Plan, entry: dict, refs: list[tuple[dict, Path]], start_path: Path | None,
                start_from_clip: bool) -> None:
    import chain_studio
    cast = video_studio.chain_cast(p.model, [r for r, _ in refs], log=_QUIET)
    by_path = {str(path): r for r, path in refs}
    kind = "previous clip" if start_from_clip else "start image"
    opener = Ref("", "", kind, path=str(start_path)) if start_path is not None else None

    def ref(slot: str, path: str) -> Ref:
        if opener is not None and path == opener.path and path not in by_path:
            return Ref(slot, "", opener.kind, path=path)
        return _user_ref(slot, by_path.get(path) or {}, path)

    if p.family == "h3":
        cap = 9 if p.chain == "seamless" else len(entry.get("ref_load_nodes") or []) or 5
        p.refs = [ref(c["slot"], c["path"]) for c in cast[:cap]]
        for i, r in enumerate(p.refs):
            r.picture = i + 1
        if opener is not None:
            p.opening, p.opening_ref = kind, opener
        elif p.refs:
            p.opening, p.opening_ref = "picture", p.refs[0]
    elif p.family == "ltx":
        nodes = entry.get("ref_nodes") or {}
        if p.chain == "seamless" or start_path is not None:
            # seamless: one reference set; tail-frame segment 1: the start image
            # takes pic1 and the people move down, the same mapping
            slots = chain_studio.ltx_reference_slots(start_path, cast)
            if slots and "pic1" not in slots:
                p.issues.append(Issue("error", "no_pic1",
                                      "An LTX chain needs a person reference or a start image: "
                                      "with only a background the chain fails."))
        else:
            slots = {c["slot"]: Path(c["path"]) for c in cast if c["slot"] in nodes}
            if any(s.startswith("pic") for s in slots) and "pic1" not in slots:
                p.issues.append(Issue("error", "no_pic1",
                                      "LTX needs a person in pic1: without it segment 1 fails. "
                                      "Move the lead person to pic1, or add a start image."))
            elif slots and "pic1" not in slots:
                slots = {}    # segment 1 renders text-to-video (video_studio._build)
                p.issues.append(Issue("warning", "background_late",
                                      "Segment 1 renders without references: a background is only "
                                      "used together with a person or a start image."))
            if "pic4" in slots:
                p.issues.append(Issue("warning", "pic4_first_only",
                                      f"{_ref_label(by_path.get(str(slots['pic4'])) or {})} (pic4) is used "
                                      f"in segment 1 only: from segment 2 the last frame of the segment "
                                      f"before takes pic1 and the people move down one slot."))
        p.refs = [ref(s, str(path)) for s, path in sorted(slots.items(), key=lambda kv: _SLOTS.index(kv[0]))]
        if opener is not None and any(r.path == opener.path for r in p.refs):
            p.opening, p.opening_ref = kind, next(r for r in p.refs if r.path == opener.path)
    else:
        p.opening = kind if opener is not None else ""
        return
    _unused(p, entry, refs, {r.path for r in p.refs}, start_path)


def _unused(p: Plan, entry: dict, refs: list[tuple[dict, Path]], used: set[str],
            start_path: Path | None) -> None:
    """One warning per reason for the attached references the model does not get."""
    nodes = entry.get("ref_nodes") or {}
    people = [r for r, _ in refs if r["slot"] in _PEOPLE_SLOTS]
    cap = len(entry.get("ref_load_nodes") or []) or 5
    why: dict[str, list[str]] = {}
    for r, path in refs:
        if str(path) in used:
            continue
        slot = r["slot"]
        if p.family == "ltx" and slot not in nodes:
            reason = f"LTX has no {slot} slot (it has pic1 to pic4 and background)"
        elif not people:
            reason = "a background is only used together with a person reference"
        elif p.family == "ltx" and start_path is not None:
            reason = "the start image takes pic1 and the people move down one slot, so the last one has no room"
        else:
            reason = f"{p.label} takes at most {cap} images"
        why.setdefault(reason, []).append(f"{_ref_label(r)} ({slot})")
    for reason, names in why.items():
        p.issues.append(Issue("warning", "ref_unused", f"Not used: {_names(names)}. {reason[0].upper()}{reason[1:]}."))


# -------------------------------------------------------------------- check
_EXACT = re.compile(r"<Picture ([1-9]\d*)>")
# a try at a picture label that is not the exact one: a picture word and a
# number in brackets (<picture1>, [Picture 1], (pic 2), {image_3}, < Picture 1 >),
# or the label without its opening bracket (Picture 1>)
_NEAR = re.compile(
    r"(?P<open>[<\[{(])[ \t]*(?P<word>pictures?|pics?|images?|imgs?|photos?|refs?|references?)"
    r"[ \t]*[#_.:-]?[ \t]*(?P<n>\d+)(?:[ \t]*(?P<close>[>\]})]))?"
    r"|(?<![<\w])(?P<word2>pictures?)[ \t]*(?P<n2>\d+)[ \t]*>",
    re.I)
_MEDIA = re.compile(r"<[ \t]*(?:video|audio)[ \t]*\d+[ \t]*>", re.I)
_BARE = re.compile(r"(?<![<\w])picture[ \t]+(?P<n>[1-9]\d*)(?![\d>])", re.I)
# names that are ordinary words are never read as a cast name
_GENERIC = {"man", "woman", "person", "people", "girl", "boy", "child", "kid", "lady", "guy",
            "subject", "character", "lead", "hero", "environment", "background", "location",
            "scene", "room", "power", "effect", "face", "portrait", "reference", "image",
            "picture", "photo", "start", "the", "a", "an", "he", "she", "they", "it", "her", "him"}

_CONTINUITY = re.compile(
    r"continu|seamless|carr(?:y|ies|ied|ying) on|without (?:a |any )?cut|"
    r"same (?:continuous )?(?:shot|take)|terminal frame|picks? up (?:from|where)", re.I)
_OPENING = re.compile(
    r"key (?:image|frame)|opening (?:key |anchor )?(?:image|frame|shot)|anchor frame|"
    r"start(?:ing)? (?:image|frame)|first frame|begins? (?:from|on)|starts? (?:from|on)|"
    r"opens? on|established in", re.I)
_SOUND = re.compile(
    r"\b(?:audio|sound\w*|silen\w+|diegetic|music\w*|score|soundtrack|ambien\w+|room tone|"
    r"nois\w+|voices?|says?|said|speak\w*|spoke|whisper\w*|shout\w*|yell\w*|dialog\w*|"
    r"hum|hums|humming|footsteps?|rustl\w*|crackl\w*|creak\w*|clink\w*|thud\w*|hiss\w*|"
    r"buzz\w*|chirp\w*|birdsong|breath\w*|laugh\w*|sigh\w*|murmur\w*|quiet\w*|hush\w*|"
    r"loud\w*|echo\w*|patter\w*|splash\w*|roar\w*|howl\w*|whistl\w*|bang\w*|knock\w*|"
    r"ring\w*|tapping|taps)\b", re.I)

_OPEN_LINE = {"ltx": "The shot begins from the scene established in the opening key image. ",
              "h3": "Opening Frame: Lock the setting, the light and the positions from the opening anchor frame.\n"}
_CONT_LINE = {"ltx": "The scene continues seamlessly in one continuous shot from the same camera angle. ",
              "h3": "Continue seamlessly from the terminal frame of the previous clip."}


class _Notes:
    """Issues grouped by a key, so a label misspelled five times is one item
    with five fixes."""

    def __init__(self) -> None:
        self._g: dict[tuple, dict] = {}

    def add(self, key: tuple, level: str, code: str, seg: int | None, message: str,
            fix: dict | None = None) -> None:
        g = self._g.setdefault(key, {"issue": Issue(level, code, message, seg), "n": 0})
        g["n"] += 1
        if fix:
            g["issue"].fixes.append(fix)

    def issues(self) -> list[Issue]:
        out = []
        for g in self._g.values():
            if g["n"] > 1:
                g["issue"].message += f" ({g['n']} times)"
            out.append(g["issue"])
        return out


def check(p: Plan, prompts: list[str]) -> list[Issue]:
    """The prompt issues of a planned request, errors first. prompts[i] is
    segment i + 1, as Generate sends it."""
    texts = [str(t or "") for t in prompts]
    if not any(t.strip() for t in texts):
        return [Issue("error", "no_prompt", "Write a prompt first.")]
    out = [Issue("error", "empty", f"Segment {i} is empty.", i)
           for i, t in enumerate(texts, 1) if not t.strip()]
    if p.family == "h3":
        out += _check_h3(p, texts)
    elif p.family in ("ltx", "wan"):
        out += _check_untagged(p, texts)
    if p.family in ("h3", "ltx"):
        out += _check_flow(p, texts)
    out += _check_looks(p, texts)
    return sorted(out, key=lambda i: i.level != "error")


def _where(p: Plan, seg: int) -> str:
    return "The prompt" if p.segments == 1 else f"Segment {seg}"


def _check_h3(p: Plan, texts: list[str]) -> list[Issue]:
    pics = p.pictures
    count = len(pics)
    listing = ", ".join(f"{r.picture} = {r.label}" for r in pics)
    have = (f"only {count} picture{'s are' if count != 1 else ' is'} attached ({listing})" if count
            else "no pictures are attached to this render")
    by_name: dict[str, set[int]] = {}
    for r in pics:
        key = " ".join(r.name.lower().split())
        if r.kind not in ("start image", "previous clip") and len(key) > 1 and key not in _GENERIC:
            by_name.setdefault(key, set()).add(r.picture)
    binds = _binding_patterns(by_name)
    notes, tagged, bound = _Notes(), set(), set()

    for seg, t in enumerate(texts, 1):
        taken: list[tuple[int, int]] = []
        for m in _MEDIA.finditer(t):
            taken.append(m.span())
            end = m.end() + (1 if t[m.end():m.end() + 1] == " " else 0)
            notes.add((seg, "media", m.group(0)), "error", "media_tag", seg,
                      f"{m.group(0)} points at nothing: the studio attaches only pictures to H3.",
                      _fix(m.start(), end, t[m.start():end], "", "Remove it"))
        for m in _NEAR.finditer(t):
            n = int(m.group("n") or m.group("n2"))
            if m.group(0) == _tag(n):
                continue
            taken.append(m.span())
            found = m.group(0)
            if not 1 <= n <= count:
                notes.add((seg, "range", found), "error", "tag_range", seg,
                          f"\"{found}\" points at Picture {n}, but {have}.")
                continue
            notes.add((seg, "spell", found), "error", "tag_spelling", seg,
                      f"\"{found}\" is not a label H3 reads: write {_tag(n)} exactly "
                      f"(capital P, one space, angle brackets).", _label_fix(m, n))
        for m in _EXACT.finditer(t):
            n = int(m.group(1))
            if n > count:
                notes.add((seg, "range", m.group(0)), "error", "tag_range", seg,
                          f"{m.group(0)} points at nothing: {have}.")
            else:
                tagged.add(n)
        for rx in binds:
            for m in rx.finditer(t):
                key = " ".join(m.group("name").lower().split())
                n, want = int(m.group("n")), by_name.get(key, set())
                bound.add(key)
                if n in want or n > count:
                    continue
                s, e = m.span("tag")
                fix = (_fix(s, e, t[s:e], _tag(next(iter(want))), f"Use {_tag(next(iter(want)))}")
                       if len(want) == 1 else None)
                right = _names([_tag(k) for k in sorted(want)])
                notes.add((seg, "bind", key, n), "error", "name_tag", seg,
                          f"{m.group('name')} is {right}, not {_tag(n)}.", fix)
        for m in _BARE.finditer(t):
            # "Picture 1: <Picture 1> ..." already carries the label
            n = int(m.group("n"))
            if (not 1 <= n <= count or _tag(n) in t
                    or any(s < m.end() and m.start() < e for s, e in taken)):
                continue
            notes.add((seg, "bare", n), "warning", "tag_bare", seg,
                      f"\"{m.group(0)}\" has no angle brackets: H3 reads only the label {_tag(n)}.",
                      _fix(m.start(), m.end(), m.group(0), _tag(n), f"Write {_tag(n)}"))

    unmentioned = []
    for r in pics:
        # a name bound to a label already has its issue (or none) above
        if (r.picture in tagged or r.kind in ("start image", "previous clip")
                or " ".join(r.name.lower().split()) in bound):
            continue
        fix = _name_fix(r, texts)
        if fix is not None:
            seg, f = fix
            notes.add(("named", r.picture), "warning", "name_untagged", seg,
                      f"{r.name} is named but never labelled: H3 links {r.name} to the image only "
                      f"through {_tag(r.picture)}. Write \"{r.name} ({_tag(r.picture)})\" once.", f)
        else:
            unmentioned.append(f"{_tag(r.picture)} ({r.label})")
    if unmentioned:
        notes.add(("unmentioned",), "warning", "picture_unused", None,
                  f"No segment mentions {_names(unmentioned)}. H3 still receives "
                  f"{'it' if len(unmentioned) == 1 else 'them'}, but only the label tells the "
                  f"model who or what {'it is' if len(unmentioned) == 1 else 'they are'}.")
    return notes.issues()


def _label_fix(m: re.Match, n: int) -> dict | None:
    """The exact label for a near miss, when the intent is unambiguous: a closed
    bracket, an opening <, or the label without its <."""
    open_, close = m.group("open"), m.group("close")
    if m.group("n2") is not None or open_ == "<" or close:
        rep = f"({_tag(n)})" if open_ == "(" and close == ")" else _tag(n)
        return _fix(m.start(), m.end(), m.group(0), rep, f"Write {_tag(n)}")
    return None


def _binding_patterns(by_name: dict[str, set[int]]) -> list[re.Pattern]:
    """Where a prompt ties a cast name to a label: "Ruth (<Picture 1>)",
    "Ruth: <Picture 1>", "<Picture 1> (Ruth", "<Picture 1>: Ruth". Only these
    explicit forms, so a name that merely stands next to a label is never read
    as a binding."""
    if not by_name:
        return []
    alt = "|".join(r"\s+".join(map(re.escape, k.split()))
                   for k in sorted(by_name, key=len, reverse=True))
    name = rf"(?<![\w'’])(?P<name>(?i:{alt}))"
    tag = r"(?P<tag><Picture (?P<n>[1-9]\d*)>)"
    return [re.compile(name + r"(?![\w'’])[ \t]*\([ \t]*" + tag + r"[ \t]*\)"),
            re.compile(name + r"(?![\w'’])[ \t]*[:=][ \t]*" + tag),
            re.compile(tag + r"[ \t]*\([ \t]*" + name + r"(?=[ \t]*[),])"),
            re.compile(tag + r"[ \t]*[:=][ \t]*" + name + r"(?![\w'’])")]


def _name_fix(r: Ref, texts: list[str]) -> tuple[int, dict] | None:
    """(segment, fix) binding the first plain mention of a Picture's name."""
    key = " ".join(r.name.lower().split())
    if len(key) < 2 or key in _GENERIC:
        return None
    rx = re.compile(rf"(?<![\w'’<])(?i:{r'\s+'.join(map(re.escape, key.split()))})(?![\w'’])")
    for seg, t in enumerate(texts, 1):
        m = rx.search(t)
        if m:
            return seg, _fix(m.end(), m.end(), "", f" ({_tag(r.picture)})", f"Add {_tag(r.picture)}")
    return None


def _check_untagged(p: Plan, texts: list[str]) -> list[Issue]:
    """LTX and Wan read no labels: a tag is plain text to them."""
    model = "LTX-2.5" if p.family == "ltx" else "Wan 2.2"
    how = ("it matches people by how the prompt describes them, so describe the person instead "
           "(age, hair, clothes)" if p.family == "ltx"
           else "it takes no reference images, so describe everyone in words")
    out = []
    for seg, t in enumerate(texts, 1):
        found, fixes = [], []
        spans = [m.span() for m in _EXACT.finditer(t)]
        spans += [m.span() for m in _NEAR.finditer(t) if m.group(0) != _tag(int(m.group("n") or m.group("n2")))]
        spans += [m.span() for m in _MEDIA.finditer(t)]
        for s, e in sorted(set(spans)):
            found.append(t[s:e])
            # remove only a tag in parentheses, "Theo (<Picture 1>)": anything
            # else is part of a sentence and needs rewording, not deleting
            if t[s] != "(" and s > 0 and t[s - 1] == "(" and t[e:e + 1] == ")":
                s, e = s - 1, e + 1
            if t[s] == "(" and t[e - 1] == ")":
                s -= 1 if s > 0 and t[s - 1] == " " else 0
                fixes.append(_fix(s, e, t[s:e], "", "Remove it"))
        if found:
            out.append(Issue("warning", "tag_ignored",
                             f"{model} does not read picture labels ({_names(sorted(set(found)))}): {how}.",
                             seg, fixes))
    return out


def _check_flow(p: Plan, texts: list[str]) -> list[Issue]:
    """The opening of segment 1, the continuity of a seamless take, and sound."""
    out, fam = [], p.family
    first = texts[0]
    if first.strip():
        if p.opening == "start image" and not _OPENING.search(first) and not (
                p.opening_ref is not None and p.opening_ref.picture
                and _tag(p.opening_ref.picture) in first):
            if fam == "h3":
                msg = (f"The {'take' if p.chain else 'clip'} opens exactly on the start image, but "
                       f"{_where(p, 1).lower()} does not start from it. Lock what that frame shows.")
            else:
                msg = (f"{_where(p, 1)} should open on the key image (the start image, pic1): "
                       f"give its shot size and setting.")
            out.append(Issue("warning", "opening", msg, 1,
                             [_fix(0, 0, "", _OPEN_LINE[fam], "Add the opening line")]))
        elif p.opening == "previous clip" and not _CONTINUITY.search(first):
            out.append(Issue("warning", "continuity",
                             f"This {'take' if p.chain else 'clip'} continues the previous clip, but "
                             f"{_where(p, 1).lower()} does not say so.", 1, [_continuity_fix(fam, first)]))
    if p.chain == "seamless":
        for seg, t in enumerate(texts[1:], 2):
            if t.strip() and not _CONTINUITY.search(t):
                line = ("\"... continues seamlessly in one continuous shot from the same camera angle\"."
                        if fam == "ltx" else f"\"{_CONT_LINE['h3']}\"")
                out.append(Issue("warning", "continuity",
                                 f"Segment {seg} does not say it continues the segment before. "
                                 f"In a seamless take, start it with {line}", seg,
                                 [_continuity_fix(fam, t)]))
    quiet = [seg for seg, t in enumerate(texts, 1) if t.strip() and not _SOUND.search(t)]
    if quiet:
        who = ("The prompt does" if p.segments == 1 else
               f"Segment {quiet[0]} does" if len(quiet) == 1 else
               f"Segments {_names(quiet)} do")
        example = ("\"Diegetic audio: rain on the window, a quiet room hum; no non-diegetic score.\""
                   if fam == "ltx" else "\"room_tone: a quiet kitchen; audio_constraints: zero "
                                        "non-diegetic music.\"")
        model = "LTX-2.5" if fam == "ltx" else "MiniMax H3"
        out.append(Issue("warning", "sound",
                         f"{who} not describe the sound. {model} renders the sound with the picture, "
                         f"so say what is heard, e.g. {example}",
                         quiet[0] if len(quiet) == 1 else None))
    return out


_WORD = re.compile(r"[\w'’-]+")


def _shared_runs(texts: list[str], n: int = 8) -> int:
    """How many separate runs of n or more words every segment has in common:
    a description the prompts repeat word for word is one."""
    words = [_WORD.findall(t.lower()) for t in texts if t.strip()]
    if len(words) < 2:
        return 0
    others = [{tuple(w[i:i + n]) for i in range(len(w) - n + 1)} for w in words[1:]]
    hits = [i for i in range(len(words[0]) - n + 1)
            if all(tuple(words[0][i:i + n]) in o for o in others)]
    return sum(1 for k, i in enumerate(hits) if k == 0 or i != hits[k - 1] + 1)


def _check_looks(p: Plan, texts: list[str]) -> list[Issue]:
    """A seamless LTX take keeps a person's age and look by repeating it in every
    segment: the studio adds the look from the reference card
    (chain_studio.with_cast_line). Without one, the prompts have to repeat a
    description themselves; a suggestion only when they do not."""
    if p.family != "ltx" or p.chain != "seamless":
        return []
    bare = [r for r in p.refs if r.kind == "person" and not r.look]
    if not bare or _shared_runs(texts) >= len(bare):
        return []
    one = len(bare) == 1
    return [Issue("warning", "no_look",
                  f"{_names([f'{r.slot} ({r.label})' for r in bare])} {'has' if one else 'have'} no look "
                  f"on {'its' if one else 'their'} reference card, and the segments do not describe "
                  f"{'this person' if one else 'each of them'} in the same words. Type age, hair and "
                  f"clothes in the card's look field (LTX chains add it to every segment, so the person "
                  f"keeps the same age and look), or repeat one description in every segment.")]


def _continuity_fix(fam: str, t: str) -> dict:
    if fam == "h3":
        m = re.search(r"Opening Frame:[ \t]*", t, re.I)
        if m:
            return _fix(m.end(), m.end(), "", _CONT_LINE["h3"] + " ", "Add the continuity line")
        return _fix(0, 0, "", f"Opening Frame: {_CONT_LINE['h3']}\n", "Add the continuity line")
    return _fix(0, 0, "", _CONT_LINE["ltx"], "Add the continuity line")


# --------------------------------------------------------------- cast sheet
def cast_sheet(p: Plan) -> str:
    """The brief for the agent that writes the prompts: how this model reads a
    prompt, the images it receives, how each segment opens and continues, and
    the reply format the Paste reply button reads."""
    lines: list[str] = []
    add = lines.append
    add(f"CAST SHEET: {p.label} ({p.model})")
    add(_render_line(p))
    if p.refs:
        add("Attach the images listed below to the chat with this sheet: the prompts describe the "
            "people and places from them.")
    if p.family == "h3":
        _sheet_h3(p, add)
    elif p.family == "ltx":
        _sheet_ltx(p, add)
    else:
        add("")
        add(f"HOW {p.label.upper()} READS THE PROMPT")
        add("- As plain text. This model takes no reference images, so write no <Picture N> labels "
            "and describe everyone in words.")
        add("")
        add("OPENING")
        add({"start image": "- The clip opens on the start image: it is the first frame.",
             "previous clip": "- The clip opens on the previous clip's last frame."}.get(
            p.opening, "- There is no opening image: segment 1 sets up the scene from the prompt alone."))
    _sheet_segments(p, add)
    return "\n".join(lines) + "\n"


def _render_line(p: Plan) -> str:
    if p.chain is None:
        return f"Render: one clip of {p.first_s:.2f} s."
    if p.chain == "tail-frame":
        return (f"Render: {p.segments} segments of {p.first_s:.2f} s. Each segment starts from the "
                f"last frame of the segment before, and the clips are joined.")
    return (f"Render: {p.segments} segments as one seamless take, with no cuts. Segment 1 covers "
            f"{p.first_s:.2f} s. Each later segment covers {p.later_s:.2f} s, of which the first "
            f"{p.carry_s:.2f} s is the end of the segment before, carried over, so the take grows by "
            f"{p.later_s - p.carry_s:.2f} s per segment.")


def _sheet_h3(p: Plan, add) -> None:
    pics = p.pictures
    what = "take" if p.chain else "clip"
    add("")
    add("HOW MINIMAX H3 READS THE PROMPT")
    if pics:
        add("- For each attached image the model sees a label and the image: <Picture 1>, "
            "<Picture 2>, ... Then it reads the prompt as plain text.")
        add("- A person or thing is linked to its image only through that exact label: a capital P, "
            "one space, the number, in angle brackets, like <Picture 1>. <picture1>, [Picture 1], "
            "(Pic 1) or Picture 1 without brackets are not the label.")
        add(f"- Use only the numbers listed below (1 to {len(pics)})." if len(pics) > 1
            else "- There is one picture, <Picture 1>: write no other number.")
        named = next((r for r in pics if r.name), None)
        if named is not None:
            add(f"- Names are optional. If you use one, bind it once, like {named.name} "
                f"({_tag(named.picture)}), and use the label after that.")
        else:
            add("- No names are given: refer to each person or place by its label, like <Picture 1>.")
        add("- Describe people and places only as their images and looks show them. Where you cannot "
            "see an image, do not guess age, hair or clothes: the label already tells H3 who it is.")
        add("")
        add("PICTURES")
        for r in pics:
            what_is = {"start image": "the start image",
                       "previous clip": "the previous clip's last frame"}.get(r.kind) or r.described()
            add(f"{_tag(r.picture)} = {what_is}" + (f". Look: {r.look}" if r.look else ""))
    else:
        add("- No images are attached as pictures, so write no <Picture N> labels: describe "
            "everyone in words.")
    add("")
    add("OPENING (segment 1)")
    if p.opening == "picture" and p.opening_ref is not None:
        add(f"- The {what} opens exactly on {_tag(p.opening_ref.picture)} ({p.opening_ref.label}): "
            f"that image is the first frame. Start segment 1 from its framing.")
    elif p.opening == "start image":
        pic = p.opening_ref.picture if p.opening_ref is not None else 0
        add(f"- The {what} opens exactly on the start image: it is the first frame"
            + (f" and it is {_tag(pic)}." if pic else ", and it is not a Picture."))
        add("- Start segment 1 from what that frame shows (the setting, the light, who is where), e.g. "
            "\"Opening Frame: Lock the setting, the light and the positions from the opening anchor frame.\"")
    elif p.opening == "previous clip":
        pic = p.opening_ref.picture if p.opening_ref is not None else 0
        add(f"- The {what} opens exactly on the last frame of the previous clip"
            + (f" ({_tag(pic)})." if pic else "."))
        add(f"- Start segment 1 with: \"Opening Frame: {_CONT_LINE['h3']}\"")
    else:
        add("- There is no opening image: segment 1 sets up the scene from the prompt alone.")
    if p.chain:
        add("")
        add("LATER SEGMENTS")
        add("- Each later segment continues the same shot with no cut"
            + (", from an exact carry of the segment before." if p.chain == "seamless"
               else " and opens exactly on the last frame of the segment before."))
        add(f"- Start every later segment with: \"Opening Frame: {_CONT_LINE['h3']}\"")
        if pics:
            add("- The pictures apply to every segment: label the people in each segment again.")


def _sheet_ltx(p: Plan, add) -> None:
    what = "take" if p.chain else "clip"
    add("")
    add("HOW LTX-2.5 READS THE PROMPT")
    add("- As plain text. There are no picture labels: do not write <Picture N>, pic1 or [image] tags.")
    people = [r for r in p.refs if r.kind == "person"]
    if people:
        add("- The reference images are matched to the people by description: describe each person "
            "(age, hair, clothes) in the same words in every segment"
            + (", as their look below says." if all(r.look for r in people) else "."))
        bare = [r.slot for r in people if not r.look]
        if bare:
            add(f"- No look is written for {_names(bare)}: take it from the reference image. Where you "
                f"cannot see the image, write only what is certain (\"the man\", \"the woman\") instead "
                f"of guessing: a wrong hair colour or outfit pulls the render away from the reference.")
        if any(r.name for r in people):
            add("- A name means nothing to LTX: describe the person wherever you name them.")
    if p.chain == "seamless" and any(r.look for r in people):
        add("- The studio adds the cast's look to every segment, after its first sentence: \"Cast, the "
            "same age and look in every shot: ...\". Do not contradict it.")
    add("")
    add("REFERENCES")
    if p.refs:
        for r in p.refs:
            what_is = {"start image": "the start image (the key image), the strongest reference",
                       "previous clip": "the previous clip's last frame"}.get(r.kind) or r.described()
            add(f"- {r.slot}: {what_is}" + (f". Look: {r.look}" if r.look else ""))
        if p.chain == "seamless" and any(r.kind == "person" for r in p.refs):
            add("- Person slots left free also get a close-up of each person's face, cut from their "
                "reference image (when a face is found).")
    else:
        add("- None: the prompt alone describes everyone.")
    add("")
    add("OPENING (segment 1)")
    if p.opening == "start image":
        add(f"- The {what} opens on the key image (pic1). It guides the render but is not pinned as "
            f"the exact first frame, so open segment 1 with its shot size and setting, e.g. \"A medium "
            f"shot begins from the ... established in the opening key image.\"")
    elif p.opening == "previous clip":
        slot = p.opening_ref.slot if p.opening_ref is not None else "a slot"
        add(f"- The {what} continues the previous clip: its last frame rides {slot} as a reference. "
            f"Open with the shot size and \"... continues seamlessly in one continuous shot from the "
            f"same camera angle\".")
    else:
        add("- There is no opening image: segment 1 sets up the scene from the prompt alone.")
    if p.chain == "seamless":
        add("")
        add("LATER SEGMENTS")
        add(f"- The take continues with no cut: the last {p.carry_s:.2f} s of each segment is carried "
            f"into the next.")
        add("- Start every later segment with its shot size and \"... continues seamlessly in one "
            "continuous shot from the same camera angle\", e.g. \"A medium close-up continues seamlessly "
            "in one continuous shot from the same camera angle as ...\"")
    elif p.chain == "tail-frame":
        add("")
        add("LATER SEGMENTS")
        add("- Each later segment starts from the last frame of the segment before (it takes pic1 and "
            "the people move down one slot). Continue the action from where the segment before ended.")


def _sheet_segments(p: Plan, add) -> None:
    add("")
    add("WRITE EACH SEGMENT WITH" if p.segments > 1 else "WRITE THE PROMPT WITH")
    add("- One clear action: how it starts and how it ends."
        + (" The end of a segment is where the next one starts." if p.chain else ""))
    add("- The camera: shot size, angle and one movement.")
    if p.family == "ltx":
        add("- The sound: LTX renders it with the picture. End with \"Diegetic audio: ...; no "
            "non-diegetic score.\"")
    elif p.family == "h3":
        add("- The sound: H3 renders it with the picture. Say what is heard, put any spoken line in "
            "quotes with who says it, and add \"Zero non-diegetic music.\" when there should be no score.")
    if any(r.kind == "person" for r in p.refs) or p.chain:
        add("- The same look for each person throughout"
            + ("; end each segment on a well-lit view of the faces, since the next one starts there."
               if p.chain else "."))
    add("- No names of famous film, game or comic characters.")
    if p.family == "h3":
        add("")
        add("LAYOUT (the one the H3 showcase prompts use)")
        later = f"; later segments {p.later_s:.2f} s" if p.chain else ""
        add("[REFERENCES & RETENTION] the opening line, then one line per picture in the segment, "
            "e.g. <Picture 1>: subject_identity -> fully_preserved.")
        add("[SPATIAL & GAZE ANCHORS] the framing and where each person looks")
        add(f"[TIMELINE (0.00s - {p.first_s:.2f}s)] the action in timed steps (segment 1: "
            f"{p.first_s:.2f} s{later})")
        add("[CAMERA KINEMATICS] one camera movement")
        add("[OVERALL SOUNDSCAPE] diegetic sound, any dialogue, audio constraints")
        add("[CONSTRAINTS] what must not happen")
    add("")
    add("REPLY FORMAT")
    if p.segments > 1:
        add(f"Reply with the {p.segments} prompts only, one block per segment:")
        for i in range(1, p.segments + 1):
            add(f"Segment {i}: ...")
    else:
        add("Reply with the prompt only, as: Segment 1: ...")
        add("(For a longer take, add segments in Video Studio and copy this sheet again.)")


# ---------------------------------------------------------------- endpoints
def report(model: str, prompts: list[str], **request) -> dict:
    """The prompt check of one Video Studio request: every issue, errors first,
    and the plan it was read against. One prompt is a clip, more are a chain,
    as Generate sends them."""
    p = plan(model, len(prompts) or 1, **request)
    issues = p.issues + ([] if any(i.code == "model" for i in p.issues) else check(p, prompts))
    issues.sort(key=lambda i: i.level != "error")
    return {"ok": True,
            "errors": sum(i.level == "error" for i in issues),
            "warnings": sum(i.level == "warning" for i in issues),
            "issues": [vars(i) for i in issues],
            "plan": p.summary()}


def refusal(model: str, prompts: list[str], **request) -> dict | None:
    """None when the request may render, else the refusal the generate and
    chain endpoints return: the first error and every issue."""
    r = report(model, prompts, **request)
    errors = [i for i in r["issues"] if i["level"] == "error"]
    if not errors:
        return None
    first = errors[0]
    where = f"Segment {first['segment']}: " if first["segment"] and len(prompts) > 1 else ""
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return {"ok": False, "error": f"{where}{first['message']}{more}", "issues": r["issues"]}


def sheet(model: str, segments: int = 1, **request) -> dict:
    p = plan(model, segments, **request)
    return {"ok": True, "text": cast_sheet(p), "plan": p.summary(),
            "issues": [vars(i) for i in p.issues]}
