"""README showcase stills: Qwen-Image 2.1 (fast, DMD 8-step) on a clean lab instance.

The stills (cast portraits, opening frames) are rendered with the studio's own graph
builders (models.build_workflow / build_ref_workflow, registry entry qwen21_fast) on
an isolated ComfyUI_Qwen21 instance (chain_lab profile "qwen", :8291) started for the
batch. Models stay loaded across the batch, as in the studio: the first render of a
batch is the cold one (weights read from disk), later ones are warm.

usage (tool\\.venv python):
  showcase.py images PLAN.json [--only id,id] [--force]
  showcase.py pick ID SEED [--why TEXT]

PLAN: {"images": [{"id", "prompt", "size": "1344x768", "seeds": [n, ...], "refs": [id or path, ...]}]}
Every seed renders output/showcase/images/<id>_s<seed>.png; `pick` copies the chosen
render to <id>.png, which later entries name in "refs" (Qwen native references,
"image_1".. in the prompt). Every render, its timing and the picks are recorded in
output/showcase/images/images.json; the lab run dir holds the per-job graph, events,
telemetry and server log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chain_lab as lab  # noqa: E402

import models  # noqa: E402  (tool/, on sys.path via chain_lab)

ROOT = lab.ROOT
SHOW = ROOT / "output" / "showcase"
IMG = SHOW / "images"
QWEN = ROOT / "ComfyUI_Qwen21"
MID = "qwen21_fast"


def stage(p: Path) -> str:
    """A reference copied into the Qwen input dir under a content name."""
    name = f"lab_{hashlib.sha256(p.read_bytes()).hexdigest()[:16]}{p.suffix.lower() or '.png'}"
    dst = QWEN / "input" / name
    if not dst.is_file():
        shutil.copyfile(p, dst)
    return name


def resolve(ref: str) -> Path:
    for p in (IMG / f"{ref}.png", Path(ref)):
        if p.is_file():
            return p
    raise SystemExit(f"reference {ref!r}: no picked image {IMG / (ref + '.png')} and no such file")


def load_db() -> dict:
    p = IMG / "images.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"renders": [], "picks": {}}


def save_db(db: dict) -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    tmp = IMG / "images.json.tmp"
    tmp.write_text(json.dumps(db, indent=1), encoding="utf-8")
    tmp.replace(IMG / "images.json")


def saved_image(outputs: dict) -> Path:
    for out in outputs.values():
        for im in out.get("images") or []:
            if im.get("type") == "output":
                return QWEN / "output" / str(im.get("subfolder") or "") / str(im["filename"])
    raise RuntimeError(f"no saved image in the job's outputs: {json.dumps(outputs)[:400]}")


def cmd_images(args) -> None:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    m = models.MODELS[MID]
    jobs = []
    for e in plan["images"]:
        if only and e["id"] not in only:
            continue
        ref_paths = [resolve(r) for r in e.get("refs") or []]
        refs = [stage(p) for p in ref_paths]
        w, h = (int(x) for x in str(e.get("size") or m["default_size"]).split("x"))
        for seed in e["seeds"]:
            out = IMG / f"{e['id']}_s{seed}.png"
            if out.is_file() and not args.force:
                lab.say(f"{out.name}: already rendered")
                continue
            shot = f"sc_{e['id']}_s{seed}"
            if refs:
                g = models.build_ref_workflow(MID, e["prompt"], "", int(seed), w, h, m["steps"], m["cfg"],
                                              None, refs, 1.0, False, shot_id=shot)
            else:
                g = models.build_workflow(MID, e["prompt"], "", int(seed), w, h, m["steps"], m["cfg"],
                                          shot_id=shot)
            jobs.append({"entry": e, "seed": int(seed), "out": out, "graph": g, "size": f"{w}x{h}",
                         "refs": [str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in ref_paths]})
    if not jobs:
        lab.say("nothing to render")
        return
    IMG.mkdir(parents=True, exist_ok=True)
    run_dir = lab.new_run("showcase_qwen")
    inst = lab.Instance("qwen", run_dir)
    runner = lab.LabRunner(inst, run_dir, stall_s=300, hard_stall_s=600, clean=False)
    db = load_db()
    try:
        t0 = time.time()
        inst.start()
        boot_s = round(time.time() - t0, 1)
        bad = {f"{j['entry']['id']}_s{j['seed']}": err for j in jobs
               if (err := lab.validate(j["graph"], inst.object_info))}
        if bad:
            raise SystemExit("graphs failed validation: " + json.dumps(bad, indent=1)[:3000])
        lab.say(f"qwen instance up in {boot_s} s; {len(jobs)} renders validated")
        db.setdefault("batches", []).append({"run": str(run_dir.relative_to(ROOT)), "boot_s": boot_s,
                                             "renders": len(jobs), "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        for k, j in enumerate(jobs):
            res = runner.run(f"{j['entry']['id']}_s{j['seed']}", j["graph"])
            src = saved_image(res["outputs"])
            shutil.copyfile(src, j["out"])
            src.unlink()                      # the render lives in output/showcase, not the studio gallery
            db["renders"].append({"id": j["entry"]["id"], "seed": j["seed"], "file": j["out"].name,
                                  "prompt": j["entry"]["prompt"], "size": j["size"], "refs": j["refs"],
                                  "model": MID, "steps": m["steps"], "cfg": m["cfg"],
                                  "seconds": res["seconds"], "cold": k == 0,
                                  "slowest": res["slowest"][:4], "peak": res["peak"],
                                  "run": str(run_dir.relative_to(ROOT)), "job": res["name"]})
            save_db(db)
    finally:
        inst.stop()
        lab._write_summary(run_dir, runner, {"images": [str(j["out"].name) for j in jobs]})


def cmd_pick(args) -> None:
    src = IMG / f"{args.id}_s{args.seed}.png"
    if not src.is_file():
        raise SystemExit(f"{src} was not rendered")
    shutil.copyfile(src, IMG / f"{args.id}.png")
    db = load_db()
    db["picks"][args.id] = {"seed": int(args.seed), "file": src.name, "why": args.why}
    save_db(db)
    lab.say(f"{args.id}: picked seed {args.seed}")


def cmd_compare_ltx(args) -> None:
    """An LTX chain's links decoded separately and hard-cut together (the carried
    frames dropped, the take's one-piece audio under it), seam-QA'd like the
    decode-once take, at draft and, when finished, at 1080p. No GPU: the per-link
    decodes are what the chain's draft and finish steps wrote."""
    import chain_assemble as ca
    import seam_qa
    spec, models = lab.ltx_request_spec(Path(args.request))
    chain = lab.ce.LtxChain(spec, None, lab.COMFY_OUT, models=models)  # type: ignore[arg-type]
    out = SHOW / args.story / "compare"
    out.mkdir(parents=True, exist_ok=True)
    ff = lab.ffmpeg_exe()
    audio = chain.path(chain.audio_take_rel(), ".audio.safetensors")
    doc: dict = {"chain": spec.chain_id, "made": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "what": "per-link decodes hard-cut at each seam (carried frames dropped), take audio"}
    for stage in ("draft", "final"):
        links = chain.link_media(stage, args.variant)
        if not all(Path(lk.video).is_file() for lk in links):
            lab.say(f"{stage}: per-link decodes missing, skipped")
            continue
        mp4 = out / f"{spec.chain_id}_{stage}_perlink_cut.mp4"
        res = ca.assemble_ltx(links, mp4, ff, fps=spec.fps, crossfade=0, crf=16,
                              take_audio=audio if audio.is_file() else None)
        rep = seam_qa.analyze_take(mp4, res["seams"], fps=spec.fps,
                                   audio=mp4.with_suffix(".wav") if res["audio"] else None,
                                   out_dir=out / f"qa_{stage}_perlink_cut")
        print(f"--- {stage}: per-link decodes, hard cut", flush=True)
        print(seam_qa.summarize(rep), flush=True)
        doc[stage] = {"mp4": str(mp4.relative_to(ROOT)), "seams": res["seams"], "qa": rep.to_json()}
    (out / "compare_ltx.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")


def cmd_cast(args) -> None:
    """Cast identity and apparent age per link of a take (face_drift: InsightFace on
    the CPU, the studio's own drift report), against the request's cast portraits."""
    import face_drift
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    cast = {str(c.get("name") or c["slot"]): Path(c["path"]) for c in req.get("cast") or []}
    seams = [int(s) for s in args.seams.split(",") if s.strip()]
    t0 = time.time()
    d = face_drift.cast_drift(Path(args.video), seams, cast)
    print(face_drift.summarize(d), flush=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"video": args.video, "seams": seams, "cast": {k: str(v) for k, v in cast.items()},
                               "seconds": round(time.time() - t0, 1), "drift": d}, indent=1, default=str),
                   encoding="utf-8")


def _ff(*a: str) -> None:
    import subprocess
    r = subprocess.run([lab.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *a],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-1500:]}")


def cmd_web(args) -> None:
    """README media from a take: an H.264 mp4 at --width (AAC sound, faststart) and a
    looping GIF preview (two-pass palette) at --gif-width / --gif-fps."""
    src, dst = Path(args.video), Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    trim = ["-t", str(args.seconds)] if args.seconds else []
    _ff("-i", str(src), *trim, "-vf", f"scale={args.width}:-2:flags=lanczos", "-c:v", "libx264",
        "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(dst.with_suffix(".mp4")))
    vf = f"fps={args.gif_fps},scale={args.gif_width}:-1:flags=lanczos"
    pal = dst.with_name(dst.stem + "_palette.png")
    _ff("-i", str(src), *trim, "-vf", f"{vf},palettegen=stats_mode=diff:max_colors=200", str(pal))
    _ff("-i", str(src), "-i", str(pal), *trim, "-lavfi",
        f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", "-loop", "0",
        str(dst.with_suffix(".gif")))
    pal.unlink()
    for p in (dst.with_suffix(".mp4"), dst.with_suffix(".gif")):
        lab.say(f"{p.name}: {p.stat().st_size / 1e6:.1f} MB")


def cmd_side(args) -> None:
    """Two takes side by side (scaled to --height, cut to the shorter one), mp4 + GIF,
    each labelled; the left take's sound."""
    a, b = Path(args.left), Path(args.right)
    dst = Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = int(args.height)

    def lab_(i: int, text: str) -> str:
        t = text.replace(":", r"\:").replace("'", "")
        return (f"[{i}:v]scale=-2:{h}:flags=lanczos,setsar=1,drawtext=text='{t}':x=12:y=10:fontsize={max(14, h // 22)}:"
                f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6:fontfile='C\\:/Windows/Fonts/segoeui.ttf'[v{i}]")
    graph = f"{lab_(0, args.left_label)};{lab_(1, args.right_label)};[v0][v1]hstack=inputs=2:shortest=1[v]"
    _ff("-i", str(a), "-i", str(b), "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-shortest",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst.with_suffix(".mp4")))
    vf = f"fps={args.gif_fps},scale={args.gif_width}:-1:flags=lanczos"
    pal = dst.with_name(dst.stem + "_palette.png")
    mp4 = dst.with_suffix(".mp4")
    _ff("-i", str(mp4), "-vf", f"{vf},palettegen=stats_mode=diff:max_colors=200", str(pal))
    _ff("-i", str(mp4), "-i", str(pal), "-lavfi",
        f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", "-loop", "0",
        str(dst.with_suffix(".gif")))
    pal.unlink()
    for p in (mp4, dst.with_suffix(".gif")):
        lab.say(f"{p.name}: {p.stat().st_size / 1e6:.1f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    im = sub.add_parser("images")
    im.add_argument("plan")
    im.add_argument("--only", default="")
    im.add_argument("--force", action="store_true")
    pk = sub.add_parser("pick")
    pk.add_argument("id")
    pk.add_argument("seed")
    pk.add_argument("--why", default="")
    cl = sub.add_parser("compare-ltx")
    cl.add_argument("request")
    cl.add_argument("story")
    cl.add_argument("--variant", default="F0")
    ca_ = sub.add_parser("cast")
    ca_.add_argument("request")
    ca_.add_argument("video")
    ca_.add_argument("--seams", required=True, help="first take frame of every later link, comma list")
    ca_.add_argument("--out", required=True)
    for name in ("web", "side"):
        w = sub.add_parser(name)
        if name == "web":
            w.add_argument("video")
            w.add_argument("--width", type=int, default=1280)
            w.add_argument("--seconds", type=float, default=0)
        else:
            w.add_argument("left")
            w.add_argument("right")
            w.add_argument("--left-label", default="")
            w.add_argument("--right-label", default="")
            w.add_argument("--height", type=int, default=360)
        w.add_argument("out", help="output path without extension (.mp4 and .gif are written)")
        w.add_argument("--crf", type=int, default=23)
        w.add_argument("--gif-width", type=int, default=480)
        w.add_argument("--gif-fps", type=int, default=10)
    args = ap.parse_args()
    {"images": cmd_images, "pick": cmd_pick, "compare-ltx": cmd_compare_ltx, "cast": cmd_cast,
     "web": cmd_web, "side": cmd_side}[args.cmd](args)


if __name__ == "__main__":
    main()
