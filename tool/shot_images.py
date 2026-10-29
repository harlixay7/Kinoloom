"""Script-driven per-shot reference images (workspace image studio).
ShotSpec -> PromptSpec -> image (any studio model + LoRAs) with character
identity locks: trained char LoRAs (Wan family), IP-Adapter identity refs
(SDXL family) or native references (Qwen-Image 2.1: each person's still and
the place). Outputs land in shots/keyframes/<shot_id>/img_<n>.png and can be
promoted to the clip's start image (ref_image)."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import models
import renderer
from core import ROOT, is_safe_component, load_config, log_setup

log = log_setup("shot_images")

KF_ROOT = ROOT / "shots" / "keyframes"   # testable via monkeypatch


def _ns(pid) -> str:
    return str(pid) if pid and is_safe_component(str(pid)) else "_"
IDENTITY_DIRS = ("refs/chars", "masters", "stills", "")  # search order hints

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def resolve_identity_path(pid: str, cid: str, fname: str) -> Path | None:
    """Locate an identity ref file by name across the served dirs
    (mirrors the /api/herorefs serving order)."""
    from bible import BIBLE_DIR
    # a bare file name only: no separators/drive (Path.name), no "."/".."
    # (they pass the name check but address the folder itself), no NUL
    if not fname or Path(fname).name != fname or fname in (".", "..") \
            or "\x00" in fname:
        return None
    if not cid or not is_safe_component(cid):
        return None
    cands: list[Path] = []
    if pid and is_safe_component(pid):
        cands.append(BIBLE_DIR / "projects" / pid / "refs" / "chars"
                     / cid / fname)
    face = ROOT / "roster" / "datasets" / "face" / cid
    cands.append(face / fname)
    cands.append(face / "masters" / fname)
    cands.append(face / "stills" / fname)
    cands.append(ROOT / "roster" / "keyframes" / cid / fname)
    for p in cands:
        if p.is_file():
            return p
    return None


def auto_identity_files(pid: str, cid: str) -> list[str]:
    """All auto-discovered identity ref names for a character (uploaded
    project hero refs + roster face masters/stills + roster keyframes).
    Every name is servable via /api/herorefs."""
    from bible import BIBLE_DIR
    if not cid or not is_safe_component(cid):
        return []
    out: list[str] = []

    def add_dir(d: Path) -> None:
        if not d.is_dir():
            return
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in _IMG_EXTS:
                if p.name not in out:
                    out.append(p.name)

    if pid and is_safe_component(pid):
        add_dir(BIBLE_DIR / "projects" / pid / "refs" / "chars" / cid)
    face = ROOT / "roster" / "datasets" / "face" / cid
    add_dir(face)
    add_dir(face / "masters")
    add_dir(face / "stills")
    add_dir(ROOT / "roster" / "keyframes" / cid)
    return out


def char_identity_refs(shot: dict, bible) -> dict[str, Path | None]:
    """Identity lock refs per main/supporting character:
      1. bible/projects/<pid>/refs/chars/<cid>/selected.png  (manual batch pick)
      2. bible/projects/<pid>/refs/chars/<cid>/*.png         (uploaded hero refs)
      3. roster/datasets/face/<cid>/master_*.png
      4. roster/datasets/face/<cid>/stills/<first png>
      5. roster/keyframes/<cid>/front.png
    Returns {cid: Path} (first hit; missing -> None)."""
    from bible import BIBLE_DIR
    out: dict[str, Path | None] = {}
    pid = str(shot.get("project_id") or "")
    for c in (shot.get("characters") or []):
        if not isinstance(c, dict):
            continue
        if c.get("tier") not in ("main", "supporting"):
            continue
        cid = c.get("id")
        if not cid or not is_safe_component(cid):
            continue
        found: Path | None = None
        if pid and is_safe_component(pid):
            cdir = BIBLE_DIR / "projects" / pid / "refs" / "chars" / cid
            for pref in ("selected.png",):
                p = cdir / pref
                if p.exists():
                    found = p
                    break
            if found is None:
                found = next((p for p in sorted(cdir.glob("*"))
                              if p.is_file()
                              and p.suffix.lower() in _IMG_EXTS), None)
        if found is None:
            for name in auto_identity_files(pid, cid):
                p = resolve_identity_path(pid, cid, name)
                if p is not None and p.exists():
                    found = p
                    break
        out[cid] = found
    return out


def location_ref(shot: dict) -> Path | None:
    """Environment reference for the shot's location (manual pick or
    upload on the location card), else the auto environment dataset
    stills for that location id."""
    from bible import BIBLE_DIR
    pid = str(shot.get("project_id") or "")
    lid = str(shot.get("location") or "")
    if not pid or not lid or not is_safe_component(pid) \
            or not is_safe_component(lid):
        return None
    ldir = BIBLE_DIR / "projects" / pid / "refs" / "locs" / lid
    for pref in ("selected.png",):
        p = ldir / pref
        if p.exists():
            return p
    if ldir.is_dir():
        hit = next((p for p in sorted(ldir.glob("*"))
                    if p.is_file() and p.suffix.lower() in _IMG_EXTS), None)
        if hit is not None:
            return hit
    env = ROOT / "roster" / "datasets" / "environment" / lid
    for sub in ("", "stills"):
        d = env / sub
        if d.is_dir():
            for p in sorted(d.glob("*")):
                if p.is_file() and p.suffix.lower() in _IMG_EXTS:
                    return p
    return None


NATIVE_MAX_REFS = 4     # Qwen-Image 2.1 takes one to four references


def native_refs(shot: dict, bible) -> list[dict]:
    """The pictures a model with native references (Qwen-Image 2.1) is given,
    as for the opening frames: the lead's picture, the place, then the others.
    Each person's picture is the still the video model gets (their outfit or
    loadout still): given a face close-up alone, Qwen drew another outfit in
    all 10 key-image tests (docs/research/MOVIE_CONTINUITY.md §2).
    [{"kind": "character"|"location", "id", "path"}]"""
    from ltx_refs import resolve_reference_set
    try:
        refs, _ = resolve_reference_set(shot, bible)
    except Exception as e:  # noqa: BLE001 - the picture is still made from the prompt
        log.warning(f"references for {shot.get('shot_id')} not resolved: {e}")
        return []
    people: list[dict] = []
    place: list[dict] = []
    for r in refs:
        parts = str(r.get("source") or "").split(":")
        p = Path(str(r.get("path") or ""))
        if len(parts) < 2 or not p.is_file():
            continue
        if parts[0] == "character":
            people.append({"kind": "character", "id": parts[1], "path": p})
        elif parts[0] == "location":
            place.append({"kind": "location", "id": parts[1], "path": p})
    out: list[dict] = []
    for r in people[:1] + place[:1] + people[1:]:
        if all(r["path"] != o["path"] for o in out):
            out.append(r)
    return out[:NATIVE_MAX_REFS]


def _ref_url(pid: str, r: dict) -> str | None:
    """Where the page shows a native reference, when a route serves it."""
    from bible import BIBLE_DIR
    p, rid = Path(r["path"]), str(r["id"])
    if not pid or not is_safe_component(pid) or not is_safe_component(rid):
        return None
    if r["kind"] == "character":
        hit = resolve_identity_path(pid, rid, p.name)
        if hit is not None and hit.resolve() == p.resolve():
            return f"/api/herorefs/{pid}/{rid}/{p.name}"
    elif p.parent.resolve() == (BIBLE_DIR / "projects" / pid / "refs" / "locs" / rid).resolve():
        return f"/api/locrefs/{pid}/{rid}/{p.name}"
    return None


def _wait_up(client, timeout_s: float = 180) -> None:
    """The image server answers (a lease may have just restarted it)."""
    t0 = time.time()
    while True:
        try:
            client.system_stats()
            return
        except Exception:  # noqa: BLE001
            if time.time() - t0 > timeout_s:
                raise RuntimeError(f"the image server on port {client.port} is not up")
            time.sleep(5)


def build_auto_prompt(shot: dict, bible) -> tuple[str, str]:
    """Auto (prompt, negative) for a shot — SCENE-first: describes the
    full shot (location + every visible character + the action + power
    VFX + camera) so the image is a reference for the SHOT, not a
    character portrait. Identity is carried by IP-Adapter/char LoRAs,
    not by repeating the primary character's full profile."""
    from prompts import PromptBuilder
    pb = PromptBuilder(bible)
    parts: list[str] = []

    # 1. environment: location descriptor + lighting + time
    loc = (bible.locations() if bible is not None else {}).get(
        shot.get("location", ""), {})
    if loc.get("descriptor"):
        parts.append(str(loc["descriptor"]))
    if shot.get("time"):
        parts.append(f"time of day: {shot['time']}")
    if loc.get("lighting"):
        parts.append(f"lighting: {loc['lighting']}")

    # 2. every character in frame (verbatim body/outfit/action/emotion)
    for c in (shot.get("characters") or []):
        if not isinstance(c, dict):
            continue
        prof = (bible.characters() if bible is not None else {}).get(
            c.get("id", ""), {}) or {}
        seg = []
        for f in ("body", "outfit"):
            if prof.get(f):
                seg.append(str(prof[f]))
        if c.get("action"):
            seg.append(str(c["action"]))
        elif c.get("emotion"):
            seg.append(f"expression: {c['emotion']}")
        if c.get("position") and c.get("facing"):
            seg.append(f"{c['position']}, facing {c['facing']}")
        if seg:
            parts.append(", ".join(seg))

    # 3. the shot's action + VFX + camera + composition
    if shot.get("primary_action"):
        parts.append(str(shot["primary_action"]))
    power_id = shot.get("power")
    if power_id:
        desc = str(power_id)
        for c in (shot.get("characters") or []):
            if not isinstance(c, dict):
                continue
            prof = (bible.characters() if bible is not None else {}) \
                .get(c.get("id", ""), {})
            p = (prof.get("powers") or {}).get(power_id)
            if p:
                if isinstance(p, dict):
                    d = p.get("descriptor") or power_id
                    m = p.get("motion")
                    desc = f"{d}, {m}" if m else str(d)
                else:
                    desc = str(p)
                break
        parts.append(f"VFX: {desc}")
    if shot.get("secondary_motion"):
        parts.append(str(shot["secondary_motion"]))
    cam = []
    if shot.get("shot_size"):
        cam.append(f"{shot['shot_size']} shot")
    if shot.get("camera"):
        cam.append(str(shot["camera"]))
    if cam:
        parts.append("camera: " + ", ".join(cam))
    if shot.get("composition"):
        parts.append(str(shot["composition"]))
    if shot.get("lens"):
        parts.append(f"{shot['lens']} lens")
    return ", ".join(p for p in parts if p), pb.negative(shot)


class ShotImageGen:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop: set[str] = set()

    # ------------------------------------------------------------- planning
    def plan(self, shot: dict, bible) -> dict:
        prompt, negative = build_auto_prompt(shot, bible)
        pid = str(shot.get("project_id") or "")
        identity = []
        for cid, path in char_identity_refs(shot, bible).items():
            if path is None:
                continue
            identity.append({"char_id": cid,
                             "url": f"/api/herorefs/{pid}/{cid}/{path.name}"})
        env = location_ref(shot)
        env_info = None
        if env is not None:
            env_info = {"url": f"/api/locrefs/{pid}/{shot.get('location')}/"
                               f"{env.name}"} if pid \
                else {"path": str(env)}
        try:
            names = {"character": bible.characters() or {},
                     "location": bible.locations() or {}} if bible is not None else {}
        except Exception:  # noqa: BLE001 - the ids label the pictures instead
            names = {}
        native = [{"kind": r["kind"], "id": r["id"], "file": r["path"].name,
                   "label": str(((names.get(r["kind"]) or {}).get(r["id"]) or {}).get("name")
                                or r["id"]),
                   "url": _ref_url(pid, r)} for r in native_refs(shot, bible)]
        return {"models": models.all_model_info(),
                "default_model": models.default_still_model(),
                "loras": models.GEN_LORAS,
                "auto": {"prompt": prompt, "negative": negative},
                "identity": identity,
                "env_ref": env_info,
                "native_refs": native}

    # ---------------------------------------------------------------- jobs
    def start(self, shot: dict, bible, params: dict) -> dict:
        sid = shot.get("shot_id") or ""
        if not sid or not is_safe_component(sid):
            return {"ok": False, "error": "invalid shot id"}
        pid = str(shot.get("project_id") or "")
        if pid and not is_safe_component(pid):
            return {"ok": False, "error": "invalid project id"}
        mid = str(params.get("model") or models.default_still_model())
        if mid not in models.MODELS:
            return {"ok": False, "error": f"unknown model '{mid}'"}
        m = models.MODELS[mid]
        if params.get("hires") and not m["capabilities"].get("hires", False):
            return {"ok": False,
                    "error": f"{m['name']} does not support the hires fix "
                             f"(SDXL-family models only)"}
        with self._lock:
            job = self._jobs.get(sid)
            if job is not None and job.get("running"):
                return {"ok": False, "error": "generation already running"}
            self._stop.discard(sid)
            count = int(params.get("count") or 1)
            count = min(4, max(1, count))
            self._jobs[sid] = {"sid": sid, "running": True, "done": 0,
                               "total": count, "message": "", "files": []}
        threading.Thread(target=self._run, args=(shot, bible, params),
                         daemon=True).start()
        return {"ok": True, "sid": sid}

    def status(self, sid: str, pid: str | None = None) -> dict:
        files: list[str] = []
        if sid and is_safe_component(sid):
            d = KF_ROOT / _ns(pid) / sid
            if d.is_dir():
                files = sorted(p.name for p in d.glob("img_*.png"))
        with self._lock:
            job = self._jobs.get(sid)
        return {"active": dict(job) if job else None, "files": files}

    def stop(self, sid: str) -> dict:
        with self._lock:
            job = self._jobs.get(sid)
            if job is None or not job.get("running"):
                return {"ok": False, "error": "no active job"}
            self._stop.add(sid)
        return {"ok": True}

    # -------------------------------------------------------------- render
    def _wan_chain(self, shot: dict, bible) -> list[dict]:
        """Char LoRA chain for the Wan family: bible lora overrides
        (character.lora + powers[p].lora) then registry char loras."""
        chain: list[dict] = []
        chars_map = bible.characters() if bible is not None else {}
        power_id = shot.get("power")
        seen: set[str] = set()
        for c in (shot.get("characters") or []):
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            prof = chars_map.get(cid, {}) or {}
            lora_ov = prof.get("lora")
            if isinstance(lora_ov, dict) and lora_ov.get("file"):
                chain.append({"name": str(lora_ov["file"]),
                              "strength": float(lora_ov.get("strength")
                                                or 0.8)})
            powers = prof.get("powers") or {}
            if power_id and isinstance(powers, dict):
                pentry = powers.get(power_id)
                if isinstance(pentry, dict) and isinstance(
                        pentry.get("lora"), dict) and \
                        pentry["lora"].get("file"):
                    chain.append({"name": str(pentry["lora"]["file"]),
                                  "strength": float(
                                      pentry["lora"].get("strength") or 0.7)})
        try:
            from lora_video import VideoLoraRegistry
            reg = VideoLoraRegistry(self.cfg)
            for cid in seen:
                hit = None
                for cand in (f"char_{cid}.safetensors",
                             f"{cid}_face.safetensors"):
                    e = reg.get(cand)
                    if e is not None:
                        hit = e
                        break
                if hit is None:
                    hit = next((e for e in reg.entries()
                                if e.get("category") == "Character"
                                and e.get("char_id") == cid
                                and e.get("enabled", True)), None)
                if hit is not None and hit.get("file"):
                    name = str(hit["file"])
                    if not any(x.get("name") == name for x in chain):
                        chain.append({"name": name,
                                      "strength": float(
                                          hit.get("strength") or 0.8)})
        except Exception as e:
            log.warning(f"registry char-lora lookup failed: {e}")
        return chain

    def _build_workflow(self, mid: str, m: dict, family: str, prompt: str,
                        negative: str, seed: int, w: int, h: int,
                        steps: int, cfgv: float, loras: list[dict],
                        uploaded: list[str], sid: str,
                        shot: dict, bible,
                        env_uploaded: str | None = None) -> dict:
        if family in ("sdxl", "illustrious"):
            if not uploaded:
                return models.build_workflow(mid, prompt, negative, seed, w, h,
                                             steps, cfgv, loras, shot_id=sid)
            if len(uploaded) == 1 and not env_uploaded:
                return models.build_ref_workflow(
                    mid, prompt, negative, seed, w, h, steps, cfgv, loras,
                    uploaded, 0.8, False, shot_id=sid)
            # multiple identity refs (and/or env ref): chain one
            # IPAdapterAdvanced per reference — each injects its own
            # identity; env ref last at low weight (style-ish guidance)
            base = models.build_workflow(mid, prompt, negative, seed, w, h,
                                         steps, cfgv, loras, shot_id=sid)
            weights = [0.8] * len(uploaded)
            if env_uploaded:
                uploaded = list(uploaded) + [env_uploaded]
                weights.append(0.35)
            return self._inject_ipadapter_chain(base, uploaded, weights)
        if family == "qwen21" and uploaded:
            # native references, as the opening frames take them
            return models.build_ref_workflow(
                mid, prompt, negative, seed, w, h, steps, cfgv, None,
                uploaded[:NATIVE_MAX_REFS], 1.0, False, shot_id=sid)
        if family == "wan":
            from workflows import WorkflowBuilder
            chain = self._wan_chain(shot, bible)
            try:
                return WorkflowBuilder().build(
                    "wan2.2_t2i_dynamic", prompt=prompt, negative=negative,
                    seed=seed, width=w, height=h, steps=steps, frames=1,
                    extra={"SHOT_ID": sid, "LORA_CHAIN": chain})
            except Exception as e:
                log.warning(f"wan t2i dynamic build failed for {sid}, "
                            f"falling back to model build: {e}")
        return models.build_workflow(mid, prompt, negative, seed, w, h,
                                     steps, cfgv, loras, shot_id=sid)

    @staticmethod
    def _inject_ipadapter_chain(wf: dict, ref_names: list[str],
                                weights: list[float]) -> dict:
        """Chain N IPAdapterAdvanced nodes onto the SDXL base graph — one
        identity per reference image (multi-subject support). Each node
        loads its own image and injects at its own weight."""
        import copy
        wf = copy.deepcopy(wf)
        model_src = None
        ksampler = None
        for nid, node in wf.items():
            if node.get("class_type") == "KSampler":
                ksampler = nid
                link = node.get("inputs", {}).get("model")
                if isinstance(link, list) and link:
                    model_src = link[0]
        if model_src is None or ksampler is None:
            raise RuntimeError("sdxl base graph missing KSampler/model link")
        cur = model_src
        for i, name in enumerate(ref_names):
            ln = f"ipa_img_{i}"
            ul = f"ipa_ul_{i}"
            ad = f"ipa_adv_{i}"
            wf[ln] = {"class_type": "LoadImage",
                      "inputs": {"image": name}}
            wf[ul] = {"class_type": "IPAdapterUnifiedLoader",
                      "inputs": {"model": [cur, 0],
                                 "preset": "PLUS (high strength)"}}
            wf[ad] = {"class_type": "IPAdapterAdvanced",
                      "inputs": {"model": [cur, 0],
                                 "ipadapter": [ul, 0],
                                 "image": [ln, 0],
                                 "weight": float(weights[i]),
                                 "weight_type": "linear",
                                 "combine_embeds": "concat",
                                 "start_at": 0.0, "end_at": 1.0,
                                 "embeds_scaling": "V only"}}
            cur = ad
        wf[ksampler]["inputs"]["model"] = [cur, 0]
        return wf

    def _run(self, shot: dict, bible, params: dict) -> None:
        sid = shot.get("shot_id") or "img"
        job = self._jobs.get(sid) or {}
        lease = None
        try:
            mid = str(params.get("model") or models.default_still_model())
            m = models.MODELS[mid]
            family = m.get("family", "sdxl")
            try:
                w, h = map(int, str(m.get("default_size",
                                          "1024x1024")).split("x"))
            except (ValueError, AttributeError):
                w, h = 1024, 1024
            steps = int(params.get("steps") or m.get("steps") or 20)
            cfgv = float(params.get("cfg")
                         if params.get("cfg") is not None
                         else m.get("cfg", 6.5))
            base_seed = int(params.get("seed")) \
                if params.get("seed") is not None \
                else 100000 + int(time.time()) % 900000
            prompt = str(params.get("prompt") or "").strip()
            negative = str(params.get("negative") or "").strip()
            if not prompt or not negative:
                auto_p, auto_n = build_auto_prompt(shot, bible)
                prompt = prompt or auto_p
                negative = negative or auto_n
            loras: list[dict] = []
            for l in (params.get("loras") or [])[:6]:
                if not isinstance(l, dict) or l.get("file") not in \
                        models.GEN_LORAS:
                    continue
                try:
                    strength = float(l.get("strength") or 0.7)
                except (TypeError, ValueError):
                    strength = 0.7
                loras.append({"file": l["file"], "strength": strength})

            # params.worker routes this image job to a
            # worker PC's ComfyUI through the agent (duck-typed client, so the
            # rest of this method is unchanged); '' / 'local' keeps the
            # production farm exactly as before.
            worker = str(params.get("worker") or "").strip()
            if worker and worker != "local":
                import workers as _workers
                client = _workers.client_for(worker)
            else:
                # a model on its own ComfyUI (Qwen-Image 2.1: comfy_port)
                # renders there; one GPU lease holds it for all the pictures
                import gpu_broker
                port = int(m.get("comfy_port")
                           or self.cfg["ensemble"]["comfyui_port"])
                lease = gpu_broker.acquire(model=mid, port=port,
                                           purpose="image", timeout=3600)
                client = renderer.ComfyClient(port)
                _wait_up(client)
            uploaded: list[str] = []
            env_uploaded: str | None = None
            if family == "qwen21":
                for r in native_refs(shot, bible):
                    try:
                        uploaded.append(client.upload_image(r["path"]))
                    except Exception as e:
                        log.warning(f"reference upload failed {r['id']}: {e}")
            else:
                for cid, path in char_identity_refs(shot, bible).items():
                    if path is None or not path.exists():
                        continue
                    try:
                        uploaded.append(client.upload_image(path))
                    except Exception as e:
                        log.warning(f"identity ref upload failed {cid}: {e}")
                try:
                    env_path = location_ref(shot)
                    if env_path is not None and env_path.exists() and \
                            family in ("sdxl", "illustrious"):
                        env_uploaded = client.upload_image(env_path)
                except Exception as e:
                    log.warning(f"env ref upload failed: {e}")

            kf_dir = KF_ROOT / _ns(shot.get("project_id")) / sid
            kf_dir.mkdir(parents=True, exist_ok=True)
            count = int(job.get("total") or 1)
            files: list[str] = []
            for i in range(count):
                if sid in self._stop:
                    job["message"] = "stopped"
                    break
                seed = (base_seed + i * 101) % (2 ** 31)
                dest = kf_dir / f"img_{i}.png"
                try:
                    wf = self._build_workflow(mid, m, family, prompt,
                                              negative, seed, w, h, steps,
                                              cfgv, loras, uploaded, sid,
                                              shot, bible,
                                              env_uploaded=env_uploaded)
                    prompt_id = client.queue_prompt(wf)
                    res = client.wait_for(prompt_id, timeout_s=600)
                    if res is None:
                        raise TimeoutError(f"img_{i} render timeout")
                    st = res.get("status", {})
                    if st.get("status_str") != "success":
                        raise RuntimeError(f"img_{i} render failed")
                    img = None
                    for node_out in res.get("outputs", {}).values():
                        imgs = node_out.get("images") or []
                        if imgs:
                            img = imgs[0]
                            break
                    if not img:
                        raise RuntimeError(f"img_{i}: no image output")
                    client.download(img.get("subfolder", ""),
                                    img["filename"], dest)
                    if not dest.exists() or dest.stat().st_size < 100:
                        raise RuntimeError(f"img_{i}: empty download")
                    if params.get("hires") and m["capabilities"].get(
                            "hires", False):
                        try:
                            from jobs import _hires_pass
                            _hires_pass(client, m, dest, prompt, negative,
                                        cfgv, seed, w, h)
                        except Exception as he:
                            log.warning(f"hires refine failed for {sid}: {he}")
                    files.append(dest.name)
                    job["done"] = i + 1
                except Exception as e:
                    msg = f"img_{i} failed: {e}"
                    job["message"] = msg
                    log.warning(f"shot image {sid} {msg}")
            job["files"] = files
            job["running"] = False
            if not job.get("message") and not files:
                job["message"] = "no images produced"
        except Exception as e:
            job["message"] = f"generation failed: {e}"
            job["running"] = False
            log.warning(f"shot image generation for {sid} failed: {e}")
        finally:
            if lease is not None:
                import gpu_broker
                gpu_broker.release(lease)
            with self._lock:
                self._jobs.pop(sid, None)
                self._stop.discard(sid)
