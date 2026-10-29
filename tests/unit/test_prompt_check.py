"""Video Studio prompt checks (prompt_check): the picture labels MiniMax H3 reads,
the plan of what each model receives (the same mapping the renders use), the
opening and continuity lines of a take, the cast sheet, and the endpoints that
refuse a request with errors."""
import json
from pathlib import Path

import pytest

import prompt_check as pc
import video_models
import video_studio

H3, LTX, LTX_SEAMLESS = "minimax_h3_hero_v2", "ltx25_w4a8_refs", "ltx25_s6_cq"
SHOWCASE = Path(__file__).resolve().parents[2] / "docs" / "showcase" / "inputs"


@pytest.fixture
def studio(sandbox_root, monkeypatch):
    """Every model available; reference stills, a start image and a previous
    clip's tail frame inside the sandbox."""
    monkeypatch.setattr(video_models, "model_status", lambda mid: {"available": True})
    refs_dir = sandbox_root / "output" / "pc_refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("ruth", "lena", "theo", "nora", "kitchen", "tail"):
        files[name] = refs_dir / f"{name}.png"
        files[name].write_bytes(b"png " + name.encode())
    start = sandbox_root / "ComfyUI" / "input" / "pc_start.png"
    start.parent.mkdir(parents=True, exist_ok=True)
    start.write_bytes(b"start")
    yield files
    start.unlink(missing_ok=True)


def ref(files, slot, name, kind="face", **kw):
    return {"slot": slot, "path": str(files[name.lower()]), "type": kind, "name": name, **kw}


def cast3(files):
    return [ref(files, "pic1", "Ruth"), ref(files, "pic2", "Lena"),
            ref(files, "background", "Kitchen", "environment")]


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


def apply(text, fixes):
    for f in sorted(fixes, key=lambda f: (f["start"], f["end"]), reverse=True):
        assert text[f["start"]:f["end"]] == f["find"]
        text = text[:f["start"]] + f["replace"] + text[f["end"]:]
    return text


# --------------------------------------------------------------- H3 labels
@pytest.mark.parametrize("written, fixed", [
    ("<picture1> pours tea.", "<Picture 1> pours tea."),
    ("[Picture 2] smiles.", "<Picture 2> smiles."),
    ("Ruth (pic 1) pours tea.", "Ruth (<Picture 1>) pours tea."),
    ("< Picture 1 > pours tea.", "<Picture 1> pours tea."),
    ("<Picture 2 smiles.", "<Picture 2> smiles."),
    ("Picture 3> is the kitchen.", "<Picture 3> is the kitchen."),
    ("{image_2} smiles.", "<Picture 2> smiles."),
    ("<Pictures 01> pours tea.", "<Picture 1> pours tea."),
])
def test_a_misspelled_h3_label_is_an_error_with_its_exact_fix(studio, written, fixed):
    p = pc.plan(H3, references=cast3(studio))
    issues = pc.check(p, [written])
    assert codes(issues, "error") == ["tag_spelling"]
    assert apply(written, issues[0].fixes) == fixed
    assert codes(pc.check(p, [fixed]), "error") == []


def test_h3_labels_point_only_at_attached_pictures(studio):
    p = pc.plan(H3, references=cast3(studio))
    assert [(r.picture, r.name, r.kind) for r in p.pictures] == [
        (1, "Ruth", "person"), (2, "Lena", "person"), (3, "Kitchen", "environment")]
    issues = pc.check(p, ["<Picture 4> waves; <picture 7> nods. <Subject 1> sits."])
    assert codes(issues, "error") == ["tag_range", "tag_range"]
    assert all(not i.fixes for i in issues if i.code == "tag_range")
    assert "only 3 pictures are attached (1 = Ruth, 2 = Lena, 3 = Kitchen)" in issues[0].message
    # nothing attached: every label points at nothing
    t2v = pc.check(pc.plan(H3), ["<Picture 1> walks in."])
    assert codes(t2v, "error") == ["tag_range"] and "no pictures are attached" in t2v[0].message


def test_an_unclosed_bracket_or_a_list_is_an_error_without_a_guessed_fix(studio):
    p = pc.plan(H3, references=cast3(studio))
    issues = pc.check(p, ["Both (pictures 1 and 2) laugh."])
    assert codes(issues, "error") == ["tag_spelling"] and issues[0].fixes == []


def test_video_and_audio_labels_point_at_nothing(studio):
    p = pc.plan(H3, references=cast3(studio))
    issues = pc.check(p, ["<Picture 1> hums along to <Audio 1> softly."])
    assert codes(issues, "error") == ["media_tag"]
    assert apply("<Picture 1> hums along to <Audio 1> softly.", issues[0].fixes) == \
        "<Picture 1> hums along to softly."


def test_a_name_bound_to_the_wrong_picture_is_an_error(studio):
    p = pc.plan(H3, references=cast3(studio))
    issues = pc.check(p, ["Ruth (<Picture 2>) pours tea for Lena (<Picture 2>)."])
    assert codes(issues, "error") == ["name_tag"]
    assert apply("Ruth (<Picture 2>) pours tea for Lena (<Picture 2>).", issues[0].fixes) == \
        "Ruth (<Picture 1>) pours tea for Lena (<Picture 2>)."
    for bound in ("<Picture 2>: Lena smiles.", "<Picture 1> (Ruth, the grandmother) pours.",
                  "ruth: <Picture 1> pours."):
        assert codes(pc.check(p, [bound]), "error") == []


@pytest.mark.parametrize("prose", [
    "The woman beside Ruth is <Picture 2>.",          # a sentence, not a binding
    "<Picture 1> is Lena's grandmother.",
    "In the kitchen <Picture 1> pours tea.",
    "Ruth <Picture 2> smiles.",                        # only a space between them
])
def test_a_name_next_to_a_label_is_never_read_as_a_binding(studio, prose):
    p = pc.plan(H3, references=cast3(studio))
    assert codes(pc.check(p, [prose]), "error") == []


def test_unlabelled_pictures_are_suggestions(studio):
    p = pc.plan(H3, references=cast3(studio))
    issues = pc.check(p, ["Ruth pours tea for <Picture 2>. Picture 2 smiles."])
    assert codes(issues, "error") == []
    named = next(i for i in issues if i.code == "name_untagged")
    assert apply("Ruth pours tea for <Picture 2>. Picture 2 smiles.", named.fixes).startswith(
        "Ruth (<Picture 1>) pours")
    assert "<Picture 3> (Kitchen)" in next(i for i in issues if i.code == "picture_unused").message
    # "Picture 2" without brackets is a suggestion only where the label is missing
    assert "tag_bare" not in codes(issues)
    assert "tag_bare" in codes(pc.check(p, ["Picture 2 smiles at <Picture 1> in <Picture 3>."]))


# ------------------------------------------------------------ plan mapping
def test_h3_pictures_follow_the_render_order(studio):
    refs = [ref(studio, "background", "Kitchen", "environment"), ref(studio, "pic3", "Nora"),
            ref(studio, "pic1", "Ruth")]
    p = pc.plan(H3, references=refs)
    clean, _, _ = video_studio.resolve_references(H3, refs)
    assert [r.path for r in p.pictures] == [r["path"] for r in sorted(clean, key=video_studio._slot_rank)]
    assert [r.name for r in p.pictures] == ["Ruth", "Nora", "Kitchen"]       # pic3 is Picture 2
    assert p.opening == "picture" and p.opening_ref.name == "Ruth"


def test_h3_openings(studio):
    # a start image beside the cast is the first frame, not a Picture
    p = pc.plan(H3, references=cast3(studio), start_image="pc_start.png")
    assert p.opening == "start image" and p.opening_ref.picture == 0 and len(p.pictures) == 3
    # alone, the start image is Picture 1 and the first frame
    p = pc.plan(H3, start_image="pc_start.png")
    assert [(r.picture, r.kind) for r in p.pictures] == [(1, "start image")] and p.opening == "start image"
    # continuing a clip: its last frame opens the clip, the cast keeps the Pictures
    p = pc.plan(H3, references=cast3(studio), continuation=str(studio["tail"]))
    assert p.opening == "previous clip" and len(p.pictures) == 3
    # an environment alone is dropped (the render is text-to-video)
    p = pc.plan(H3, references=[ref(studio, "background", "Kitchen", "environment")])
    assert p.pictures == [] and codes(p.issues) == ["ref_unused"]


def test_h3_chain_pictures_and_opening(studio):
    p = pc.plan(H3, 3, references=cast3(studio), start_image="pc_start.png", frames=90)
    assert p.chain == "seamless" and len(p.pictures) == 3 and p.opening == "start image"
    assert (p.first_s, p.later_s) == (3.75, 5.88)
    # a chain with a start image and no cast has no Pictures at all
    assert pc.plan(H3, 2, start_image="pc_start.png").pictures == []


def test_ltx_clip_slots(studio):
    refs = [ref(studio, "pic1", "Ruth"), ref(studio, "pic2", "Lena"), ref(studio, "pic3", "Theo"),
            ref(studio, "pic4", "Nora"), ref(studio, "background", "Kitchen", "environment")]
    p = pc.plan(LTX, references=refs, start_image="pc_start.png")
    assert [(r.slot, r.label) for r in p.refs] == [("pic1", "the start image"), ("pic2", "Ruth"),
                                                  ("pic3", "Lena"), ("pic4", "Theo"),
                                                  ("background", "Kitchen")]
    assert p.opening == "start image" and p.pictures == []
    unused = next(i for i in p.issues if i.code == "ref_unused")
    assert "Nora (pic4)" in unused.message and "start image takes pic1" in unused.message


def test_ltx_needs_pic1(studio):
    # without pic1 the MSR guide fails the render (video_studio._apply_references)
    p = pc.plan(LTX, references=[ref(studio, "pic2", "Lena")])
    assert codes(p.issues, "error") == ["no_pic1"]
    assert codes(pc.plan(LTX, references=[ref(studio, "pic2", "Lena")], start_image="pc_start.png").issues,
                 "error") == []
    # a background alone renders text-to-video: a suggestion, not an error
    p = pc.plan(LTX, references=[ref(studio, "background", "Kitchen", "environment")])
    assert p.refs == [] and codes(p.issues) == ["ref_unused"]


def test_ltx_seamless_chain_slots(studio):
    bg = [ref(studio, "background", "Kitchen", "environment")]
    # chain_graphs refuses MSR refs without pic1
    assert codes(pc.plan(LTX_SEAMLESS, 2, references=bg).issues, "error") == ["no_pic1"]
    assert codes(pc.plan(LTX_SEAMLESS, 2, references=bg, start_image="pc_start.png").issues, "error") == []
    # people fill from pic1 when no start image is given
    p = pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic2", "Lena", description="a woman of 30")])
    assert [(r.slot, r.name, r.look) for r in p.refs] == [("pic1", "Lena", "a woman of 30")]


LOOK = "a woman in her late twenties with a chin-length black bob, wearing a cream trench coat"


def test_a_missing_look_is_a_suggestion_only_when_the_segments_do_not_repeat_one(studio):
    """A seamless LTX take repeats each look in every segment (chain_studio.with_cast_line);
    without a look on the card the prompts can do it themselves."""
    p = pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic1", "Theo")])
    assert "no_look" not in codes(p.issues)
    seg2 = "A medium shot continues seamlessly in one continuous shot from the same camera angle as "
    varied = ["A woman reads a letter. Diegetic audio: rain.", seg2 + "she smiles. Diegetic audio: rain."]
    issues = pc.check(p, varied)
    assert codes(issues) == ["no_look"] and issues[0].message.startswith("pic1 (Theo) has no look")
    same = [f"A full shot of {LOOK}, reading a letter. Diegetic audio: rain.",
            f"{seg2}{LOOK} looks up and smiles. Diegetic audio: rain."]
    assert pc.check(p, same) == []
    # two people without a look need two repeated descriptions
    p2 = pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic1", "Theo"), ref(studio, "pic2", "Lena")])
    assert codes(pc.check(p2, same)) == ["no_look"]
    # a look on the card covers it whatever the prompts say
    looked = pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic1", "Theo", description="a man")])
    assert pc.check(looked, varied) == []


def test_an_ltx_clip_describes_each_person_with_the_look_on_the_card(studio):
    """LTX matches a reference picture to the person the prompt describes: a look
    on the card that the prompt leaves out gets the cast sentence of the takes."""
    p = pc.plan(LTX, references=[ref(studio, "pic1", "Ruth", description=LOOK),
                                 ref(studio, "pic2", "Theo")])
    text = "A medium shot of the kitchen. Ruth pours tea. Diegetic audio: a kettle hums."
    issues = pc.check(p, [text])
    assert codes(issues) == ["look"] and "Ruth" in issues[0].message and "Theo" not in issues[0].message
    fixed = apply(text, issues[0].fixes)
    assert fixed == ("A medium shot of the kitchen. Cast, the same age and look in every shot: "
                     f"{LOOK}. Ruth pours tea. Diegetic audio: a kettle hums.")
    assert pc.check(p, [fixed]) == []
    assert pc.check(p, [f"A medium shot of {LOOK.upper()} pouring tea. Diegetic audio: a kettle hums."]) == []


def test_a_file_name_is_not_a_cast_name(studio):
    """A disk upload's name defaults to its file name: the sheet lists the image as a file,
    never as a name for the prompt writer to use."""
    up = {"slot": "pic1", "path": str(studio["nora"]), "type": "character", "name": "nora"}
    for model in (LTX_SEAMLESS, H3):
        p = pc.plan(model, 2, references=[up])
        assert [(r.name, r.label, r.file) for r in p.refs] == [("", "nora", "nora.png")]
    ltx = pc.cast_sheet(pc.plan(LTX_SEAMLESS, 2, references=[up]))
    assert "- pic1: a person (image nora.png)" in ltx and "No look is written for pic1" in ltx
    assert "A name means nothing to LTX" not in ltx
    h3 = pc.cast_sheet(pc.plan(H3, 2, references=[up]))
    assert "<Picture 1> = a person (image nora.png)" in h3 and "No names are given" in h3
    assert "There is one picture, <Picture 1>" in h3 and "bind it once" not in h3


def test_request_errors(studio):
    refs = [ref(studio, "pic1", "Ruth"), {"slot": "pic2", "path": str(studio["ruth"]) + ".gone", "name": "Lost"},
            ref(studio, "pic1", "Lena"), {"slot": "pic9", "path": str(studio["lena"])}]
    p = pc.plan(H3, references=refs)
    assert sorted(codes(p.issues, "error")) == ["ref_missing", "ref_slot", "ref_slot"]
    assert [r.name for r in p.pictures] == ["Ruth"]
    for bad, msg in (("C:/x.png", "must be a filename"), ("none.png", "not in a ComfyUI input folder")):
        p = pc.plan(LTX_SEAMLESS, 2, start_image=bad)
        assert codes(p.issues, "error") == ["start_image"] and msg in p.issues[0].message


# ------------------------------------------------------- LTX / flow checks
def test_ltx_reads_no_labels(studio):
    p = pc.plan(LTX, references=[ref(studio, "pic1", "Theo")])
    issues = pc.check(p, ["Theo (<Picture 1>) reads. <Picture 1> smiles. Diegetic audio: rain."])
    assert codes(issues) == ["tag_ignored"] and issues[0].level == "warning"
    # only the tag in parentheses is removed; the one in the sentence needs rewording
    assert apply("Theo (<Picture 1>) reads. <Picture 1> smiles. Diegetic audio: rain.", issues[0].fixes) == \
        "Theo reads. <Picture 1> smiles. Diegetic audio: rain."


def test_seamless_takes_ask_for_the_opening_and_continuity_lines(studio):
    p = pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic1", "Theo", description="a man")],
                start_image="pc_start.png")
    seg1, seg2 = "He reads a letter. Diegetic audio: rain.", "He smiles. Diegetic audio: rain."
    issues = pc.check(p, [seg1, seg2])
    assert [(i.code, i.segment) for i in issues] == [("opening", 1), ("continuity", 2)]
    fixed = [apply(seg1, issues[0].fixes), apply(seg2, issues[1].fixes)]
    assert "opening key image" in fixed[0]
    assert fixed[1].startswith("The scene continues seamlessly in one continuous shot from the same camera angle.")
    assert pc.check(p, fixed) == []
    # H3: the line goes after an existing "Opening Frame:"
    p = pc.plan(H3, 2, references=cast3(studio))
    seg2 = "[REFERENCES & RETENTION]\nOpening Frame: Lock the kitchen.\n<Picture 1> sips. room_tone: hum."
    issue = next(i for i in pc.check(p, ["<Picture 1> <Picture 2> <Picture 3> hum.", seg2])
                 if i.code == "continuity")
    assert "Opening Frame: Continue seamlessly from the terminal frame of the previous clip. Lock" in \
        apply(seg2, issue.fixes)


def test_sound_is_one_suggestion(studio):
    p = pc.plan(LTX_SEAMLESS, 3, references=[ref(studio, "pic1", "Theo", description="a man")])
    issues = [i for i in pc.check(p, ["A man reads.", "He continues reading.", "He continues. Rain taps."])
              if i.code == "sound"]
    assert len(issues) == 1 and issues[0].message.startswith("Segments 1 and 2 do not describe the sound")


def test_the_showcase_prompts_pass_clean():
    """The showcase chains (docs/showcase/inputs) were written for these models:
    the check finds nothing in them."""
    found = 0
    for f in sorted(SHOWCASE.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if "prompts" not in d:
            continue
        found += 1
        fam = "h3" if d["model"].startswith("minimax_h3") else "ltx"
        p = pc.Plan(model=d["model"], label=d["model"], family=fam, segments=len(d["prompts"]),
                    chain="seamless", opening="start image" if d.get("start_image") else "")
        for i, c in enumerate(d.get("cast") or []):
            p.refs.append(pc.Ref(c["slot"], c["name"], pc._kind(c.get("type")), c.get("description", ""),
                                 picture=i + 1 if fam == "h3" else 0))
        assert pc.check(p, d["prompts"]) == [], f.name
    assert found >= 5


# --------------------------------------------------------------- cast sheet
def test_h3_cast_sheet(studio):
    p = pc.plan(H3, 3, references=cast3(studio), start_image="pc_start.png", frames=90)
    text = pc.cast_sheet(p)
    for line in ("<Picture 1> = Ruth (person, image ruth.png)", "<Picture 3> = Kitchen (environment, image kitchen.png)",
                 "bind it once, like Ruth (<Picture 1>)", "Use only the numbers listed below (1 to 3).",
                 "opens exactly on the start image: it is the first frame, and it is not a Picture",
                 "\"Opening Frame: Continue seamlessly from the terminal frame of the previous clip.\"",
                 "Segment 1 covers 3.75 s. Each later segment covers 5.88 s",
                 "[TIMELINE (0.00s - 3.75s)]", "Segment 3: ..."):
        assert line in text, line


def test_ltx_cast_sheet(studio):
    p = pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic1", "Theo", description="a man around thirty")],
                start_image="pc_start.png")
    text = pc.cast_sheet(p)
    for line in ("do not write <Picture N>", "- pic1: the start image (the key image)",
                 "- pic2: Theo (person, image theo.png). Look: a man around thirty",
                 "in the same words in every segment, as their look below says.",
                 "A name means nothing to LTX",
                 "established in the opening key image",
                 "continues seamlessly in one continuous shot from the same camera angle",
                 "Diegetic audio: ...; no non-diegetic score.", "Segment 2: ..."):
        assert line in text, line
    single = pc.cast_sheet(pc.plan(LTX_SEAMLESS, references=[ref(studio, "pic1", "Theo")]))
    assert "Render: one clip" in single and "add segments in Video Studio" in single
    assert "LATER SEGMENTS" not in single


def test_the_cast_sheet_states_what_the_model_does_badly_and_how_long_a_prompt_is(studio):
    ltx = pc.cast_sheet(pc.plan(LTX_SEAMLESS, 2, references=[ref(studio, "pic1", "Theo")]))
    h3 = pc.cast_sheet(pc.plan(H3, 2, references=cast3(studio)))
    for text in (ltx, h3):
        assert "MODEL LIMITS (" in text and pc.FACES in text and pc.SHOWS_BADLY in text
        assert "At most 3 people who matter on screen" in text and "no whip pans" in text
        # before the reply format, after how the model reads the prompt
        assert text.index("READS THE PROMPT") < text.index("MODEL LIMITS") < text.index("REPLY FORMAT")
    assert "about 80 to 180 words" in ltx and "no keyword lists" in ltx and "inside a segment" in ltx
    assert "about 230 to 260 words in the layout below" in h3
    single = pc.cast_sheet(pc.plan(LTX_SEAMLESS, references=[ref(studio, "pic1", "Theo")]))
    assert "Each clip lasts a few seconds" in single and "Each clip's prompt:" in single


# ---------------------------------------------------------------- endpoints
def test_prompt_check_and_cast_sheet_endpoints(test_client, studio):
    body = {"model": H3, "prompts": ["<picture1> pours tea."], "references": cast3(studio)}
    r = test_client.post("/api/videostudio/prompt_check", json=body).json()
    assert r["ok"] and r["errors"] == 1 and r["issues"][0]["fixes"][0]["replace"] == "<Picture 1>"
    assert [x["picture"] for x in r["plan"]["refs"]] == [1, 2, 3]
    assert all("path" not in x for x in r["plan"]["refs"])
    r = test_client.post("/api/videostudio/cast_sheet", json={**body, "segments": 2}).json()
    assert r["ok"] and "Segment 2: ..." in r["text"]


def test_generate_and_chain_refuse_a_request_with_errors(test_client, studio, monkeypatch):
    started = []
    monkeypatch.setattr(video_studio, "start", lambda *a, **kw: started.append("clip") or {"id": "j"})
    monkeypatch.setattr(video_studio, "start_chain", lambda *a, **kw: started.append("chain") or {"id": "c"})
    refs = cast3(studio)
    r = test_client.post("/api/videostudio/generate",
                         json={"model": H3, "prompt": "<Picture 4> waves.", "references": refs}).json()
    assert r["ok"] is False and r["error"].startswith("<Picture 4> points at nothing") and r["issues"]
    r = test_client.post("/api/videostudio/chain",
                         json={"model": H3, "prompts": ["<Picture 1> sits.", "[pic2] waves."],
                               "references": refs}).json()
    assert r["ok"] is False and r["error"].startswith("Segment 2: \"[pic2]\"")
    assert started == []
    # suggestions never block
    r = test_client.post("/api/videostudio/generate",
                         json={"model": H3, "prompt": "<Picture 1> waves.", "references": refs}).json()
    assert r == {"id": "j"} and started == ["clip"]
