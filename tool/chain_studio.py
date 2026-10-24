"""Seamless chains for the Video Studio: LTX-2.5 and MiniMax H3 on chain_engine.

The studio's chain request (prompts, seed, segment length, start image, cast
references) becomes a chain_engine spec and runs on the model's own ComfyUI
instance under the chain's broker lease (video_studio._run_chain holds it).

    LTX-2.5   every link is drafted (stage 1), the take's sound is decoded once,
              a draft preview is assembled and QA'd, then every link is finished
              at 1080p and the delivery take is assembled and QA'd.
    MiniMax H3 every link renders at base resolution with an exact latent carry,
              a base preview is assembled, then the whole take is refined at
              1080p and decoded once.

draft_only=True stops after the preview (status "draft_ready"); finish_chain()
continues from the saved state later. Chain records are persisted under
output/videostudio/chains/ so they survive a studio restart.

For the UI the record carries `plan` (every step in order with its state, start
time and seconds) and, for LTX, `link_previews`: a small MP4 of each segment,
encoded in the background as soon as the segment is drafted or finished.

Reference policy: one reference set for the whole take. LTX: the start image
owns pic1 when given (cast pic1..pic3 move to pic2..pic4, background stays);
continuity comes from the pinned carry, not from swapping pic1 per link. H3:
the cast fills <Picture 1..N> in slot order; link 1 opens on the start image
(or the lead cast still) as its frame-0 guide.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chain_assemble as ca
import chain_engine as ce
import chain_graphs as cg
from core import ROOT, ffmpeg_exe, log_setup
from video_models import seamless_kind

log = log_setup("chain_studio")

CHAIN_DIR = ROOT / "output" / "videostudio" / "chains"
H3_MAX_WINDOW = 192
_SLOTS = ("pic1", "pic2", "pic3", "pic4", "pic5", "pic6", "background")
# the chain thread and the preview encoder both update a record: one writer at a time
_SAVE_LOCK = threading.RLock()


# ------------------------------------------------------------------ records
def save_record(rec: dict) -> None:
    try:
        with _SAVE_LOCK:
            CHAIN_DIR.mkdir(parents=True, exist_ok=True)
            p = CHAIN_DIR / f"{rec['id']}.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
            tmp.replace(p)
    except OSError as e:
        log.warning(f"chain record {rec.get('id')} not saved: {e}")


def load_record(cid: str) -> dict | None:
    if not cid or not all(c.isalnum() for c in cid):
        return None
    p = CHAIN_DIR / f"{cid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ support
def ltx_link_frames(frames: int, n: int, carry: int = cg.LTX_CARRY_FRAMES) -> list[int]:
    """Link 1 renders `frames` (snapped up to 1+8k); later links render the carry
    plus frames-1 new ones, so every link adds the same span (old chains: 17-frame
    overlap, frames+16 rendered)."""
    f = max(9, int(frames))
    f = f if (f - 1) % 8 == 0 else f + (8 - (f - 1) % 8)
    return [f] + [f + carry - 1] * (n - 1)


def ltx_draft_size(width: int | None, height: int | None) -> tuple[int, int]:
    """Draft (width, height) of an LTX chain: the measured 960x544 recipe, turned
    to 544x960 when the request is portrait. The finish doubles it (1920x1088 /
    1088x1920), and both orientations carry the same token count."""
    w, h = ce.LtxChainSpec.width, ce.LtxChainSpec.height
    return (h, w) if width and height and int(height) > int(width) else (w, h)


def h3_window(frames: int) -> int:
    """Smallest H3 window >= frames that lands on whole 40 Hz audio ticks
    (17k+5 frames, divisible by 3: 39, 90, 141, 192), capped at the model limit."""
    for w in (39, 90, 141, 192):
        if w >= frames:
            return w
    return H3_MAX_WINDOW


def h3_link_frames(frames: int, n: int, carry: int = cg.H3_CARRY_FRAMES) -> list[int]:
    return [h3_window(frames)] + [h3_window(carry + int(frames))] * (n - 1)


def ltx_reference_slots(start_image: Path | None, cast: list[dict]) -> dict[str, Path]:
    by = {r["slot"]: Path(r["path"]) for r in cast if r.get("slot") in _SLOTS and r.get("path")}
    if start_image is not None:
        out = {"pic1": start_image}
        for src, dst in (("pic1", "pic2"), ("pic2", "pic3"), ("pic3", "pic4")):
            if src in by:
                out[dst] = by[src]
        if "background" in by:
            out["background"] = by["background"]
        return out
    # no start image: slots fill from pic1 in order (the MSR guide needs pic1)
    subjects = [by[s] for s in ("pic1", "pic2", "pic3", "pic4") if s in by]
    out = {f"pic{i + 1}": p for i, p in enumerate(subjects)}
    if "background" in by:
        out["background"] = by["background"]
    return out


LTX_SUBJECT_SLOTS = ("pic1", "pic2", "pic3", "pic4")
FACE_CACHE = ROOT / "output" / "videostudio" / "face_refs"
LTX_FACE_REFS = True          # face close-ups of the cast in free MSR subject slots (face_ref)
LTX_CAST_LINE = True          # the cast's age/look sentence in every link prompt (with_cast_line)


def ltx_face_slots(slots: dict[str, Path], cast: list[dict], cache_dir: Path = FACE_CACHE) -> dict[str, Path]:
    """`slots` plus face close-ups (face_ref.face_closeup) of the cast's portraits in
    the MSR subject slots still free, lead first (cast slot order). A portrait
    that is a close-up already, or has no detectable face, gets none."""
    import face_ref
    free = [s for s in LTX_SUBJECT_SLOTS if s not in slots]
    if not free or "pic1" not in slots:
        return dict(slots)
    out = dict(slots)
    for r in _people(cast):
        if not free:
            break
        face = face_ref.face_closeup(Path(r["path"]), cache_dir)
        if face is not None:
            out[free.pop(0)] = face
    return out


def h3_reference_order(cast: list[dict]) -> list[Path]:
    rank = {s: i for i, s in enumerate(_SLOTS)}
    return [Path(r["path"]) for r in sorted(cast, key=lambda r: rank.get(str(r.get("slot")), 99)) if r.get("path")]


def h3_tier_params(graph: dict) -> tuple[cg.H3Models, dict, dict | None]:
    """The tier's own recipe, read from the graph the studio builds for it:
    (models, H3Link overrides, H3Refine overrides or None for a base-only tier).
    H3 tiers differ in UNet, step count (32 / turbo 4 / acc 8), LoRAs, shift,
    attention backend and whether they refine at 1080p."""
    nodes = {k: v for k, v in graph.items() if isinstance(v, dict) and "class_type" in v}

    def by(ct: str) -> list[dict]:
        return [n["inputs"] for n in nodes.values() if n["class_type"] == ct]

    def src(inp, key):
        v = inp.get(key)
        return nodes.get(v[0]) if isinstance(v, list) and v else None

    m = cg.H3Models()
    unet = by("UNETLoader")
    if unet:
        m.unet = Path(str(unet[0]["unet_name"])).name
    clip = by("CLIPLoader")
    if clip:
        m.clip = Path(str(clip[0]["clip_name"])).name
    vaes = [Path(str(v["vae_name"])).name for v in by("VAELoader")]
    m.vae = next((v for v in vaes if "audio" not in v), m.vae)
    m.audio_vae = next((v for v in vaes if "audio" in v), m.audio_vae)
    ups = by("MinimaxH3LatentUpscaler3D")
    if ups:
        m.upscaler = Path(str(ups[0]["model_name"])).name

    link: dict = {"loras": [(str(n["lora_name"]), float(n["strength_model"])) for n in by("LoraLoaderModelOnly")]}
    att = by("ModelAttentionBackend")
    if att:
        link["attention"] = str(att[0]["attention"])
    sh = by("MiniMaxH3SigmaShift")
    if sh:
        link["shift_video"], link["shift_audio"] = float(sh[0]["shift_video"]), float(sh[0]["shift_audio"])
    sched = by("BasicScheduler")
    if sched:
        link["steps"] = int(sched[0]["steps"])
    r2v = by("MiniMaxH3ReferenceToVideo")
    if r2v:
        link["width"], link["height"] = int(r2v[0]["width"]), int(r2v[0]["height"])

    split = by("MMH3SplitUpscale")
    if not split or not ups:
        return m, link, None
    refine: dict = {k: link[k] for k in ("loras", "attention", "shift_video", "shift_audio") if k in link}
    refine["base_width"], refine["base_height"] = link.get("width", 960), link.get("height", 544)
    refine["width"], refine["height"] = int(ups[0]["mode.width"]), int(ups[0]["mode.height"])
    sig = src(split[0], "sigmas")
    if sig and sig["class_type"] == "ManualSigmas":
        refine["sigmas"] = str(sig["inputs"]["sigmas"])
    sp = src(split[0], "spatial_split_param")
    if sp:
        refine["tile_width"], refine["tile_height"] = int(sp["inputs"]["tile_width"]), int(sp["inputs"]["tile_height"])
        refine["seam_denoise"] = float(sp["inputs"].get("seam_denoise", 0.8))
    return m, link, refine


# ------------------------------------------------------------------- runner
class StudioRunner:
    """chain_engine runner over the studio's ComfyClient (no /free between steps:
    the chain holds one broker lease and keeps the models warm).

    timeout_s is the model's per-clip budget: a link step (name ending _l<N>) gets
    it once, a whole-take step (H3 refine / decode, LTX audio / take decode) gets it
    once per link — the H3 gate's 4-link 1080p refine alone took 3653 s against a
    2700 s clip budget."""

    def __init__(self, client, timeout_s: int, on_step=None, links: int = 1) -> None:
        self.client, self.timeout_s, self.on_step = client, int(timeout_s), on_step
        self.links = max(1, int(links))

    def step_timeout(self, name: str) -> int:
        return self.timeout_s if re.search(r"_l\d+$", name) else self.timeout_s * self.links

    def run(self, name: str, graph: dict) -> dict:
        if self.on_step:
            self.on_step(name)
        t0 = time.time()
        timeout = self.step_timeout(name)
        # no client id: ComfyUI then sends the progress to every socket, the UI's
        # progress relay included (a client id gets it sent to that client only)
        pid = self.client.queue_prompt(graph)
        hist = self.client.wait_for(pid, timeout_s=timeout)
        if not hist:
            try:
                self.client.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning(f"{name}: timeout interrupt failed: {e}")
            raise RuntimeError(f"{name}: no result after {timeout}s (render interrupted)")
        st = hist.get("status") or {}
        if st.get("status_str") == "error":
            for msg in st.get("messages", []):
                if msg[0] == "execution_error":
                    raise RuntimeError(f"{name}: {msg[1].get('exception_message', 'render failed')}")
            raise RuntimeError(f"{name}: render failed")
        return {"prompt_id": pid, "status": "success", "seconds": round(time.time() - t0, 1)}

    def fresh_process(self, name: str) -> None:
        """chain_engine.H3Chain: restart the server under the chain's broker lease
        before a graph that would make it drop a model in-process."""
        import gpu_broker
        lease = gpu_broker.current_lease()
        if lease is None or not lease.managed:
            log.warning(f"{name}: no managed GPU lease; the server is not restarted first")
            return
        res = gpu_broker.restart_leased(lease)
        if not res.get("ok"):
            raise RuntimeError(f"{name}: could not restart the server first: {res.get('error')}")
        log.info(f"{name}: restarted the server on :{res['port']} first ({res['ms']} ms)")


# -------------------------------------------------------------------- chains
def _set(rec: dict, **kw) -> None:
    with _SAVE_LOCK:
        rec.update(kw)
        save_record(rec)


def _on_step(rec: dict):
    """Runner callback: the plan, the status text and the current/total link counter."""
    def cb(step: str) -> None:
        _plan_mark(rec, step)
        m = re.search(r"_l(\d+)$", step)
        _set(rec, status=f"rendering {step}", **({"current": int(m.group(1))} if m else {}))
    return cb


# ----------------------------------------------------------------- progress
def chain_plan(kind: str, n: int, *, take_decode: bool = True, refine: bool = True) -> list[str]:
    """Every step a chain runs, in order: the runner's step names plus the assemble
    phases (encode + seam QA) between them. The UI's progress follows it."""
    if kind == "ltx":
        steps = [f"draft_l{i + 1}" for i in range(n)] + (["audio_take"] if n > 1 else [])
        steps += (["take_draft"] if take_decode else []) + ["assemble_draft"]
        steps += [f"finish_F0_l{i + 1}" for i in range(n)]
        return steps + (["take_final_F0"] if take_decode else []) + ["assemble_final"]
    steps = [f"link_l{i + 1}" for i in range(n)] + ["decode_B0", "assemble_B0"]
    return steps + (["refcond_R0", "refine_R0", "decode_R0", "assemble_R0"] if refine else [])


def _plan_init(rec: dict, steps: list[str]) -> None:
    # link_previews is created here, before the preview encoder can touch the record
    _set(rec, plan=[{"id": s, "state": "pending"} for s in steps],
         link_previews=dict(rec.get("link_previews") or {}))


def _plan_mark(rec: dict, step: str, close: bool = False) -> None:
    """Start `step` in rec["plan"]. Steps run strictly in order, so this closes every
    step before it; one an earlier run already rendered never starts and is closed
    as `reused`, without seconds. close=True closes `step` as well (the end of a run).
    A step started again while running (a seam re-roll drafts its link twice) keeps
    its start and is flagged `redrawn`. The plan is replaced, never edited in place:
    the API may be serialising the old one."""
    with _SAVE_LOCK:
        plan = [dict(s) for s in rec.get("plan") or []]
        hit = next((k for k, s in enumerate(plan) if s["id"] == step), None)
        if hit is None:
            return
        now = round(time.time(), 1)
        for s in plan[:hit + (1 if close else 0)]:
            if s["state"] == "running":
                s.update(state="done", seconds=round(now - float(s.get("t0") or now), 1))
            elif s["state"] == "pending":
                s.update(state="done", reused=True)
        if not close:
            s = plan[hit]
            if s["state"] == "running":
                s["redrawn"] = True
            else:
                s.pop("seconds", None)
                s.pop("reused", None)
                s.update(state="running", t0=now)
        _set(rec, plan=plan)


# ----------------------------------------------------------------- previews
_PREVIEWS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chain-preview")


def preview_dir(cid: str) -> Path:
    return CHAIN_DIR / cid / "preview"


def _queue_preview(rec: dict, chain: ce.LtxChain, i: int, stage: str, variant: str = "F0") -> None:
    """Encode link i ("draft" or "final") as a small MP4 for the UI in the background,
    so the next link renders meanwhile. Never fails or holds up the chain."""
    rel = chain.draft_rel(i) + "_draft" if stage == "draft" else chain.finish_rel(i, variant) + "_final"
    _PREVIEWS.submit(_make_preview, rec, i, stage, chain.path(rel, ".mkv"),
                     chain.spec.carry_frames if i else 0, chain.spec.fps,
                     preview_dir(rec["id"]) / f"link{i + 1}_{stage}.mp4")


def _make_preview(rec: dict, i: int, stage: str, mkv: Path, carry: int, fps: float, out: Path) -> None:
    try:
        if not (out.is_file() and out.stat().st_mtime >= mkv.stat().st_mtime):
            ca.link_preview(mkv, carry, fps, out, ffmpeg_exe())
    except Exception as e:  # noqa: BLE001 - a preview is a bonus, never a failure
        log.warning(f"[chain {rec['id'][:8]}] link {i + 1} {stage} preview failed: {e}")
        return
    with _SAVE_LOCK:
        lp = dict(rec.get("link_previews") or {})
        lp[str(i + 1)] = {**(lp.get(str(i + 1)) or {}), stage: out.name}
        _set(rec, link_previews=lp)


_PERSON_TYPES = ("face", "character")       # the face batches / the UI's "character"


def _people(cast: list[dict]) -> list[dict]:
    rank = {s: i for i, s in enumerate(_SLOTS)}
    return [r for r in sorted(cast, key=lambda r: rank.get(str(r.get("slot")), 99))
            if r.get("path") and r.get("slot") != "background"
            and str(r.get("type") or "face").lower() in _PERSON_TYPES]


def cast_descriptions(cast: list[dict]) -> list[str]:
    """One identity phrase per cast person, lead first: the reference's own
    `description`, else what its face dataset was generated from (cast_import:
    age, presentation, hair, eyes, skin, face, build, then the outfit)."""
    out = []
    for r in _people(cast):
        d = str(r.get("description") or "").strip()
        if not d and r.get("name") and str(r.get("type") or "").lower() in _PERSON_TYPES:
            try:
                import cast_import as ci
                rows, _ = ci._rows_for_import("face", str(r["name"]))
                p = ci._run_params(rows) if rows else {}
                d = ", ".join(x for x in (ci._identity_text(p), ci._outfit_text(p, {})) if x)
            except Exception as e:  # noqa: BLE001 - a description is a bonus, never a failure
                log.warning(f"cast description for {r.get('name')!r} unavailable: {e}")
                d = ""
        if d:
            out.append(d.rstrip(". "))
    return out


def with_cast_line(prompt: str, descriptions: list[str]) -> str:
    """The cast's identity sentence after the prompt's first (establishing)
    sentence. Stage 2 refines without references, so the prompt is its only
    identity cue: on gate link 1 (2026-10-23) the age-and-look sentence moved the
    lead's apparent age in the 1080p finish from 40 to 36 (portrait ~29) and her
    ArcFace similarity from 0.178 to 0.207."""
    if not descriptions:
        return prompt
    line = "Cast, the same age and look in every shot: " + "; ".join(descriptions) + "."
    i = prompt.find(". ")
    return f"{prompt} {line}" if i < 0 else f"{prompt[:i + 1]} {line}{prompt[i + 1:]}"


def cast_faces(cast: list[dict]) -> dict[str, str]:
    """name -> portrait path of the cast's people (face references), for the drift report."""
    out: dict[str, str] = {}
    for r in _people(cast):
        name = str(r.get("name") or r.get("slot") or f"cast{len(out) + 1}")
        out[name if name not in out else f"{name}_{r.get('slot')}"] = str(r["path"])
    return out


def _cast_report(rec: dict, key: str, res: dict) -> None:
    """Per-link cast identity / apparent age of an assembled take (face_drift),
    stored as rec[key]. Reported only: never fails the chain."""
    faces = rec.get("cast_faces") or {}
    if not faces or not res.get("mp4"):
        return
    try:
        import face_drift
        d = face_drift.cast_drift(Path(res["mp4"]), list(res.get("seams") or []),
                                  {k: Path(v) for k, v in faces.items()})
    except Exception as e:  # noqa: BLE001
        log.warning(f"[chain {rec['id'][:8]}] cast drift report failed: {e}")
        return
    if d:
        _set(rec, **{key: d})
        log.info(f"[chain {rec['id'][:8]}] cast through the take ({key}):\n{face_drift.summarize(d)}")


def _qa_brief(res: dict) -> dict:
    qa = res.get("qa") or {}
    return {"passed": qa.get("passed"), "summary": res.get("qa_summary"),
            "seams": [{k: s.get(k) for k in ("frame", "pop_ratio", "d_sat", "d_L", "d_contrast",
                                             "detail_ratio", "pass", "fails")} for s in qa.get("seams", [])],
            "drift": (qa.get("drift") or {}).get("worst_pct")}


def run_chain(rec: dict, entry: dict, client, prompts: list[str], *, negative: str, base_seed: int,
              frames: int, start_image: Path | None, cast: list[dict], draft_only: bool = False,
              tier_graph: dict | None = None, width: int | None = None,
              height: int | None = None) -> None:
    """Render a seamless chain into rec (mutated + persisted). Raises on failure.
    tier_graph: the studio's built single-clip graph for this model (H3: the tier's
    recipe is read from it). width/height: the requested size; LTX takes only its
    orientation (ltx_draft_size), H3 reads its size from tier_graph."""
    kind = seamless_kind(entry)
    if kind is None:
        raise ValueError("model has no seamless chain support")
    cid = rec["id"]
    comfy_out = ROOT / str(entry.get("comfy_root") or "ComfyUI_LTX") / "output"
    timeout = int(entry.get("timeout_s") or 2700)
    runner = StudioRunner(client, timeout, on_step=_on_step(rec), links=len(prompts))
    out_dir = ROOT / "output" / "videostudio"
    out_dir.mkdir(parents=True, exist_ok=True)
    uploads: dict[str, str] = {}

    def up(p: Path) -> str:
        key = str(p)
        if key not in uploads:
            uploads[key] = client.upload_image(p)
        return uploads[key]

    n = len(prompts)
    _set(rec, cast_faces=cast_faces(cast))
    if kind == "ltx":
        slots = ltx_reference_slots(start_image, cast)
        if LTX_FACE_REFS:
            slots = ltx_face_slots(slots, cast)
        descs = cast_descriptions(cast) if LTX_CAST_LINE else []
        if descs:
            _set(rec, cast_line=descs)
        lf = ltx_link_frames(frames, n)
        dw, dh = ltx_draft_size(width, height)
        spec = ce.LtxChainSpec(chain_id=f"studio_{cid}",
                               links=[ce.Link(prompt=with_cast_line(str(p), descs), seed=base_seed + i,
                                              frames=lf[i], negative=negative)
                                      for i, p in enumerate(prompts)],
                               refs={s: up(p) for s, p in slots.items()}, width=dw, height=dh)
        chain = ce.LtxChain(spec, runner, comfy_out, models=cg.LtxModels.from_registry(entry))
        _set(rec, engine="seamless-ltx", link_frames=lf, seam_frames=spec.seam_frames(),
             draft_size=[dw, dh], final_size=[2 * dw, 2 * dh],
             references={s: str(p) for s, p in slots.items()}, chain_dir=str(comfy_out / chain.base))
        _plan_init(rec, chain_plan("ltx", n, take_decode=spec.take_decode))
        for i in range(n):
            chain.draft(i)
            _queue_preview(rec, chain, i, "draft")
        if n > 1:
            _set(rec, status="decoding the take's audio")
            chain.audio_take()
        if spec.take_decode:
            _set(rec, status="decoding the draft take")
            chain.take("draft")
        _set(rec, status="assembling draft preview")
        _plan_mark(rec, "assemble_draft")
        prev = chain.assemble(out_dir / f"chain_{cid[:8]}_draft.mp4", ffmpeg_exe(), stage="draft",
                              qa_dir=out_dir / "chains" / cid / "qa_draft")
        _set(rec, draft_video=prev["mp4"], draft_qa=_qa_brief(prev))
        _cast_report(rec, "draft_cast", prev)
        if draft_only:
            _plan_mark(rec, "assemble_draft", close=True)
            _set(rec, status="draft_ready")
            return
        finish_ltx(rec, chain)
    else:
        if tier_graph is None:
            raise ValueError("an H3 chain needs the tier's built graph (its sampling recipe)")
        models, link_over, refine_over = h3_tier_params(tier_graph)
        lf = h3_link_frames(frames, n)
        order = h3_reference_order(cast)
        anchor = start_image or (order[0] if order else None)
        spec = ce.H3ChainSpec(chain_id=f"studio_{cid}",
                              links=[ce.Link(prompt=str(p), seed=base_seed + i, frames=lf[i])
                                     for i, p in enumerate(prompts)],
                              refs=[up(p) for p in order], start_image=up(anchor) if anchor else None,
                              link={**link_over, "decode_draft": False},
                              refine=dict(refine_over or {}))
        chain = ce.H3Chain(spec, runner, comfy_out, models=models)
        _set(rec, engine="seamless-h3", link_frames=lf, seam_frames=spec.seam_frames(),
             refine=refine_over is not None, chain_dir=str(comfy_out / chain.base))
        _plan_init(rec, chain_plan("h3", n, refine=refine_over is not None))
        for i in range(n):
            chain.link(i)
        _set(rec, status="decoding the base take")
        chain.decode_take("B0")
        _plan_mark(rec, "assemble_B0")
        prev = chain.finish_take("B0", out_dir / f"chain_{cid[:8]}_draft.mp4", ffmpeg_exe(),
                                 qa_dir=out_dir / "chains" / cid / "qa_draft")
        _set(rec, draft_video=prev["mp4"], draft_qa=_qa_brief(prev))
        if refine_over is None:
            # base-only tier: the decode-once take is the delivery
            final = out_dir / f"chain_{cid[:8]}_final.mp4"
            import shutil
            shutil.copyfile(prev["mp4"], final)
            _plan_mark(rec, "assemble_B0", close=True)
            _set(rec, final_video=str(final), outputs=[str(final)], qa=rec.get("draft_qa"), status="completed")
            _cast_report(rec, "cast", prev)
            return
        _cast_report(rec, "draft_cast", prev)
        if draft_only:
            _plan_mark(rec, "assemble_B0", close=True)
            _set(rec, status="draft_ready")
            return
        finish_h3(rec, chain)


def finish_ltx(rec: dict, chain: ce.LtxChain, variant: str = "F0") -> None:
    cid = rec["id"]
    for i in range(len(chain.spec.links)):
        _set(rec, status=f"finishing link {i + 1}/{len(chain.spec.links)}")
        chain.finish(i, variant)
        _queue_preview(rec, chain, i, "final", variant)
    if chain.spec.take_decode:
        _set(rec, status="decoding the take")
        chain.take("final", variant)
    _set(rec, status="assembling")
    _plan_mark(rec, "assemble_final")
    out_dir = ROOT / "output" / "videostudio"
    final = out_dir / f"chain_{cid[:8]}_final.mp4"
    res = chain.assemble(final, ffmpeg_exe(), stage="final", variant=variant,
                         qa_dir=out_dir / "chains" / cid / "qa_final")
    _plan_mark(rec, "assemble_final", close=True)
    _set(rec, final_video=str(final), outputs=[str(final)], qa=_qa_brief(res), status="completed")
    log.info(f"[chain {cid[:8]}] seamless LTX take: {res['frames']} frames, QA "
             f"{'PASS' if (res.get('qa') or {}).get('passed') else 'FAIL'}\n{res.get('qa_summary')}")
    _cast_report(rec, "cast", res)


def finish_h3(rec: dict, chain: ce.H3Chain, variant: str = "R0") -> None:
    cid = rec["id"]
    _set(rec, status="refining the take at 1080p")
    chain.refine(variant)
    out_dir = ROOT / "output" / "videostudio"
    final = out_dir / f"chain_{cid[:8]}_final.mp4"
    _plan_mark(rec, "assemble_R0")
    res = chain.finish_take(variant, final, ffmpeg_exe(), qa_dir=out_dir / "chains" / cid / "qa_final")
    _plan_mark(rec, "assemble_R0", close=True)
    _set(rec, final_video=str(final), outputs=[str(final)], qa=_qa_brief(res), status="completed")
    log.info(f"[chain {cid[:8]}] seamless H3 take: QA "
             f"{'PASS' if (res.get('qa') or {}).get('passed') else 'FAIL'}\n{res.get('qa_summary')}")
    _cast_report(rec, "cast", res)


LTX_LEGACY_CARRY = {"pin_strengths": "", "latent_carry": False, "carry_restore": 0.0, "seam_adain": 0.0,
                    "seam_ramp": 6, "seam_mode": "window", "take_decode": False, "take_join": 2, "take_blend": 0,
                    "cq_strength": 0.6}
H3_LEGACY_CARRY = {"seam_adain": 0.0}


def resume_chain(rec: dict, entry: dict, client) -> None:
    """Finish a draft_ready chain from its manifest (drafts are reused)."""
    kind = seamless_kind(entry)
    comfy_out = ROOT / str(entry.get("comfy_root") or "ComfyUI_LTX") / "output"
    man = json.loads((Path(rec["chain_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    spec_d = dict(man["spec"])
    spec_d.pop("kind", None)
    spec_d["links"] = [ce.Link(**lk) for lk in spec_d["links"]]
    runner = StudioRunner(client, int(entry.get("timeout_s") or 2700), on_step=_on_step(rec),
                          links=len(spec_d["links"]))
    if kind == "ltx":
        # a chain drafted before a carry setting existed keeps the behaviour its drafts
        # were made with (a finish must match its drafts' carry)
        for k, v in LTX_LEGACY_CARRY.items():
            spec_d.setdefault(k, v)
        chain = ce.LtxChain(ce.LtxChainSpec(**spec_d), runner, comfy_out, models=cg.LtxModels.from_registry(entry))
        finish_ltx(rec, chain)
    elif kind == "h3":
        for k, v in H3_LEGACY_CARRY.items():
            spec_d.setdefault(k, v)
        chain = ce.H3Chain(ce.H3ChainSpec(**spec_d), runner, comfy_out, models=cg.H3Models.from_registry(entry))
        finish_h3(rec, chain)
    else:
        raise ValueError("model has no seamless chain support")
