"""Purpose-built cinematic I2V keyframes: ShotSpec -> PromptSpec -> Keyframe
image -> candidates -> ranking -> approval.

Each shot with a keyframe_source of auto/generated gets 1..N candidate
still images, then ranked by sharpness + face presence and (optionally)
auto-approved. The stills come from an Images-tab model (settings
`keyframes.model`, default RealVisXL, else the first installed of
KEYFRAME_FALLBACKS) with that model's own recipe; `keyframes.model: wan`
keeps the legacy Wan 2.2 graphs (not as good, opt-in only). Approved candidates are the start frame of legacy Wan renders and a
QA reference; LTX-2.5 / H3 renders take the cast references instead."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from core import (ROOT, InvalidInput, is_safe_component, jload, jsave,
                  load_config, log_setup, safe_join)

log = log_setup("keyframe_gen")

KF_DIR = ROOT / "shots" / "keyframes"


def _kf_dir(shot_id: str, pid: str | None = None) -> Path:
    """Shot assets are namespaced per project — two projects may both
    have an s01_001 and must never overwrite each other's keyframes.

    `shot_id` arrives from URL params (and clear() rmtree's the result), so
    anything but a plain id is refused with InvalidInput."""
    if not is_safe_component(str(shot_id or "")):
        raise InvalidInput(f"invalid shot id: {shot_id!r}")
    ns = pid if pid and is_safe_component(str(pid)) else "_"
    return KF_DIR / ns / str(shot_id)


def _kf_file(rel) -> Path | None:
    """A stored candidate/approved path (ROOT-relative) -> file, confined to
    the keyframes tree; None for absolute/drive/UNC/traversal values."""
    try:
        p = safe_join(ROOT, str(rel or ""))
    except InvalidInput:
        return None
    return p if p.is_relative_to(KF_DIR.resolve()) else None
TAIL_FACE_MODEL: Any = None

SHOT_SIZE_PRESETS = {
    "extreme_close_up": "close-up",
    "close_up": "close-up",
    "medium_close_up": "portrait",
    "medium": "medium",
    "medium_wide": "3/4 body",
    "wide": "full body",
    "extreme_wide": "wide",
}

TIME_LIGHTING = {
    "night": "moonlight",
    "sunset": "golden hour",
    "golden hour": "golden hour",
    "dawn": "golden hour",
    "storm twilight": "storm light",
    "blue hour": "moonlight",
}

BODY_TYPES = ("slender", "lean", "athletic", "curvy", "statuesque",
              "broad-shouldered", "wiry", "imposing")
AGES = ("young adult", "mid-20s", "mid-30s", "40s", "mature")


def _face_model() -> Any:
    """Lazy buffalo_l detector (CPU). Returns the FaceAnalysis app or None.
    Cached module-wide; False never cached — retries stay possible."""
    global TAIL_FACE_MODEL
    if TAIL_FACE_MODEL:
        return TAIL_FACE_MODEL
    try:
        from insightface.app import FaceAnalysis
        m = FaceAnalysis(name="buffalo_l",
                         providers=["CPUExecutionProvider"],
                         allowed_modules=["detection"])
        m.prepare(ctx_id=-1)
        TAIL_FACE_MODEL = m
        return m
    except Exception as e:
        log.warning(f"keyframe face detection unavailable: {e}")
        return None


def _sharpness(path: Path) -> float:
    """Laplacian variance normalized to 0-1 (log scale)."""
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            return 0.0
        var = float(cv2.Laplacian(img, cv2.CV_64F).var())
        import math
        return min(1.0, math.log1p(max(var, 0.0)) / math.log1p(5000.0))
    except Exception:
        return 0.0


def _has_face(path: Path) -> bool:
    try:
        import cv2
        m = _face_model()
        if m is None:
            return False
        img = cv2.imread(str(path))
        if img is None:
            return False
        return bool(m.get(img))
    except Exception:
        return False


def _keyframe_size(m: dict, width: int, height: int) -> tuple[int, int]:
    """The image model's own size (its `sizes` list) whose aspect ratio is
    nearest the keyframes width/height: SDXL and Qwen render at the sizes
    they were trained on, not at an arbitrary 768x480."""
    target = width / max(1, height)
    best: tuple[float, int, int] | None = None
    for s in m.get("sizes") or [m.get("default_size") or "1024x1024"]:
        try:
            sw, sh = (int(x) for x in str(s).lower().split("x"))
        except ValueError:
            continue
        d = abs(sw / max(1, sh) - target)
        if best is None or d < best[0]:
            best = (d, sw, sh)
    return (best[1], best[2]) if best else (1024, 1024)


# Images-tab models a keyframe falls back to, best first, when the configured
# one is not installed (RealVisXL, the default, cannot be downloaded by the
# installer, so a fresh install may have only Qwen-Image or Z-Image).
KEYFRAME_FALLBACKS = ("realvis", "qwen21", "zimage", "cyberrealistic_xl")


def _keyframe_model(kf: dict) -> tuple[str, dict]:
    """keyframes.model -> (id, registry entry). `wan` is kept as is; any
    other model whose weights are missing gives way to the first installed
    one in KEYFRAME_FALLBACKS (or stays, to fail loudly, when none is)."""
    import models as img_models
    mid = str(kf.get("model") or "realvis")
    if mid not in img_models.MODELS:
        log.warning(f"keyframes.model {mid!r} is not an image model - using realvis")
        mid = "realvis"
    if mid != "wan" and not img_models.is_available(mid):
        alt = next((f for f in KEYFRAME_FALLBACKS if img_models.is_available(f)), None)
        if alt:
            log.warning(f"keyframes.model {mid!r} is not installed - using {alt}")
            mid = alt
    return mid, img_models.MODELS[mid]


def _parse_body(body: str) -> dict[str, str]:
    """Split a Bible character body string into promptspec identity
    fields. Free-text segments that don't slot cleanly are folded into
    the face field so no identity detail is lost."""
    segs = [s.strip() for s in (body or "").split(",") if s.strip()]
    out: dict[str, str] = {"hair": "", "eyes": "", "skin": "", "face": "",
                           "build": "", "marks": ""}
    hair: list[str] = []
    eyes: list[str] = []
    face: list[str] = []
    for s in segs:
        low = s.lower()
        if "hair" in low and not s.lower().startswith("hair"):
            hair.append(s.replace(" hair", "").strip())
        elif "eyebrow" in low:
            face.append(s)
        elif "eye" in low:
            eyes.append(s.replace(" eyes", "").strip())
        elif "skin" in low:
            out["skin"] = s.replace(" skin", "").strip()
        elif "build" in low:
            out["build"] = s.replace(" build", "").strip()
        elif "scar" in low or "mole" in low:
            mark = s.replace(" clearly visible", "").strip()
            if mark.lower().startswith("with "):
                mark = mark[5:].strip()
            out["marks"] = mark
        elif any(t in s.lower() for t in BODY_TYPES):
            out["build"] = s
        elif "face" in low:
            face.append(s.replace(" face", "").strip())
        elif s and "with" not in low and "clearly visible" not in low:
            face.append(s)
    out["hair"] = " ".join(hair)
    out["eyes"] = " ".join(eyes)
    out["face"] = ", ".join(face)
    return out


class KeyframeGen:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()

    # ---------------------------------------------------------- spec params
    def spec_from_shot(self, shot: dict, bible) -> dict:
        """Translate a ShotSpec into character params consumable by
        promptspec.spec_from_params('character', params)."""
        chars = shot.get("characters") or []
        main_char = next((c for c in chars if c.get("tier") != "crowd"),
                         chars[0] if chars else None) or {}
        profiles = bible.characters() if bible is not None else {}
        prof = profiles.get(main_char.get("id", ""), {}) if \
            main_char.get("id") else {}
        body = str(prof.get("body", "") or "")
        parsed = _parse_body(body)

        low = body.lower()
        gender = prof.get("gender") or (
            "woman" if any(w in low for w in
                           ("woman", "female", "girl", "lady", "feminine"))
            else "man")
        age = prof.get("age") or next(
            (a for a in AGES if a in low), "mid-30s")

        wardrobe = shot.get("wardrobe") or prof.get("outfit", "")

        emotion = (main_char.get("emotion") or "").strip()
        power_id = shot.get("power") or ""
        power_desc = ""
        powers = prof.get("powers") or {}
        if power_id and isinstance(powers, dict) and power_id in powers:
            power_desc = str(powers[power_id].get("descriptor", "")) if \
                isinstance(powers[power_id], dict) else str(powers[power_id])

        loc_id = shot.get("location") or ""
        locs = bible.locations() if bible is not None else {}
        loc = locs.get(loc_id, {}) if loc_id else {}
        scene_desc = str(loc.get("descriptor", "") or "")
        if power_desc:
            scene_desc = f"{scene_desc}, {power_desc}".strip(", ")
        loc_lighting = str(loc.get("lighting", "") or "")

        params: dict[str, Any] = {
            "intent": "cinematic",
            "mode": "structured",
            "gender": gender,
            "age": age,
            "hair_style": parsed["hair"],
            "eye_color": parsed["eyes"],
            "skin": parsed["skin"],
            "face_shape": parsed["face"],
            "build": parsed["build"],
            "distinctive": parsed["marks"] or "none",
            "outfit": wardrobe,
            "emotion": emotion or "stoic",
            "pose": shot.get("primary_action", "")
            or main_char.get("action", ""),
            "shot": SHOT_SIZE_PRESETS.get(shot.get("shot_size", ""),
                                          "portrait"),
            "lens": shot.get("lens", ""),
            "scene": "custom",
            "custom_scene": scene_desc,
            "scene_location": str(loc.get("name", "") or loc_id),
            "lighting": TIME_LIGHTING.get(shot.get("time", ""),
                                          "soft window light"),
            "custom_lighting": loc_lighting,
            "anatomy_depth": "normal",
        }
        if shot.get("gaze"):
            params["gaze"] = shot["gaze"]
        return params

    # ---------------------------------------------------------- candidates
    def generate_candidates(self, shot: dict, bible, lora_chain: list,
                            count: int = 2, base_seed: int | None = None,
                            ref_image=None) -> dict:
        sid = shot.get("shot_id", "shot")
        pid = shot.get("project_id")
        kf = self.cfg.get("keyframes", {}) or {}
        count = int(kf.get("candidates", count) or count)
        width = int(kf.get("width", 768) or 768)
        height = int(kf.get("height", 480) or 480)
        steps = int(kf.get("steps", 8) or 8)
        try:
            kf_dir = _kf_dir(sid, pid)
        except InvalidInput as e:
            return {"ok": False, "shot_id": sid, "error": str(e)}
        kf_dir.mkdir(parents=True, exist_ok=True)

        try:
            from promptspec import spec_from_params
            from renderer import ComfyClient
            ps = spec_from_params("character", self.spec_from_shot(shot,
                                                                   bible))
            prompt = ps.to_prompt()
            if shot.get("composition"):
                prompt = f"{prompt}, {shot['composition']}"
            if shot.get("lens"):
                prompt = f"{prompt}, {shot['lens']} lens"
            negative = str(shot.get("negative", "") or
                           "blurry, deformed, distorted, warped, low "
                           "quality, soft, foggy, smear, watermark, text")
            scene = str(shot.get("scene") or sid)
            scene_seed = int(hashlib.md5(scene.encode("utf-8")).hexdigest()[:8],
                             16) % (2 ** 31)
            mid, m = _keyframe_model(kf)
            client = ComfyClient(m.get("comfy_port")
                                 or self.cfg["ensemble"]["comfyui_port"])
            produced: list[dict] = []
            first_err: str | None = None
            if mid == "wan":
                make_wf = self._wan_graphs(client, sid, prompt, negative,
                                           scene_seed, width, height, steps,
                                           lora_chain, ref_image)
            else:
                make_wf = self._image_graphs(mid, m, client, shot, sid, prompt,
                                             width, height, ref_image)
            for i in range(count):
                seed = int(base_seed) if base_seed is not None else \
                    (scene_seed + i * 137) % (2 ** 31)
                dest = kf_dir / f"kf_{i}.png"
                try:
                    wf = make_wf(seed)
                    prompt_id = client.queue_prompt(wf)
                    res = client.wait_for(prompt_id, timeout_s=1800)
                    if res is None:
                        raise TimeoutError(f"keyframe {i} render timeout")
                    st = res.get("status", {})
                    if st.get("status_str") != "success":
                        raise RuntimeError(f"keyframe {i} render failed")
                    img = None
                    for node_out in res.get("outputs", {}).values():
                        imgs = node_out.get("images") or []
                        if imgs:
                            img = imgs[0]
                            break
                    if not img:
                        raise RuntimeError(f"keyframe {i}: no image output")
                    client.download(img.get("subfolder", ""),
                                    img["filename"], dest)
                    if not dest.exists() or dest.stat().st_size < 100:
                        raise RuntimeError(f"keyframe {i}: empty download")
                    produced.append({"file": str(dest), "seed": seed})
                except Exception as e:
                    first_err = first_err or str(e)
                    log.warning(f"keyframe candidate {i} for {sid} failed: "
                                f"{e}")
            if not produced:
                return {"ok": False, "shot_id": sid,
                        "error": first_err or "no candidates produced"}

            ranked = self.rank([p["file"] for p in produced])
            seeds = {str(Path(p["file"]).resolve()): p["seed"]
                     for p in produced}
            rel = lambda p: str(Path(p).resolve().relative_to(ROOT)).replace(
                "\\", "/")  # noqa: E731
            files = [{"file": rel(r["file"]), "seed": seeds.get(
                str(Path(r["file"]).resolve()), 0),
                "score": r["score"], "face": r["face"]} for r in ranked]
            approved = files[0]["file"] if \
                bool(kf.get("auto_approve", False)) and files else None
            jsave(kf_dir / "candidates.json",
                  {"shot_id": sid, "files": files, "approved": approved})
            return {"ok": True, "shot_id": sid,
                    "candidates": files,
                    "approved": approved,
                    "prompt": prompt}
        except Exception as e:
            log.warning(f"keyframe generation for {sid} failed: {e}")
            return {"ok": False, "shot_id": sid, "error": str(e)}

    # ------------------------------------------------------------- graphs
    def _image_graphs(self, mid: str, m: dict, client, shot: dict, sid: str,
                      prompt: str, width: int, height: int, ref_image):
        """seed -> graph for an Images-tab model: its own steps, CFG, prompt
        adapter and negative, at its size nearest the keyframes aspect. A
        story reference image anchors identity when the model takes
        references (SDXL IP-Adapter, Qwen native refs)."""
        import models as img_models
        w, h = _keyframe_size(m, width, height)
        steps = int(m.get("steps") or 20)
        cfgv = float(m.get("cfg") if m.get("cfg") is not None else 1.0)
        fprompt, fneg = img_models.adapt_prompt(
            mid, prompt, "images",
            user_negative=str(shot.get("negative") or "") or None)
        uploaded: str | None = None
        if isinstance(ref_image, Path) and ref_image.exists() \
                and (m.get("capabilities") or {}).get("refs"):
            try:
                uploaded = client.upload_image(ref_image)
            except Exception as e:  # noqa: BLE001
                log.warning(f"keyframe reference upload failed for {sid}, "
                            f"rendering without it: {e}")

        def make(seed: int) -> dict:
            if uploaded is not None:
                try:
                    return img_models.build_ref_workflow(
                        mid, fprompt, fneg, seed, w, h, steps, cfgv, [],
                        [uploaded], 0.8, False, shot_id=sid)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"keyframe reference graph failed for {sid}, "
                                f"rendering without the reference: {e}")
            return img_models.build_workflow(mid, fprompt, fneg, seed, w, h,
                                             steps, cfgv, [], shot_id=sid)
        return make

    def _wan_graphs(self, client, sid: str, prompt: str, negative: str,
                    scene_seed: int, width: int, height: int, steps: int,
                    lora_chain: list, ref_image):
        """seed -> graph for the legacy Wan 2.2 keyframes (keyframes.model:
        wan): the character LoRA chain, and a story reference image as an
        image-to-image still. Any failure setting up the reference falls back
        to the text-to-image graph."""
        from workflows import WorkflowBuilder, list_templates
        builder = WorkflowBuilder()
        i2v_uploaded: str | None = None
        if isinstance(ref_image, Path) and ref_image.exists():
            try:
                i2v_uploaded = client.upload_image(ref_image)
                chain = list(lora_chain or [])
                extra_i2v = {"SHOT_ID": sid, "LORA_CHAIN": chain,
                             "START_IMAGE": i2v_uploaded,
                             "DENOISE": 0.5}
                if not any(e.get("enabled", True) and
                           "turbo" in str(e.get("name", "")).lower()
                           for e in chain):
                    extra_i2v["LORA_TURBO"] = ""
                probe = builder.build(
                    "wan2.2_i2v_still", prompt=prompt,
                    negative=negative, seed=scene_seed, width=width,
                    height=height, steps=12, frames=1, cfg=self.cfg,
                    extra=extra_i2v)
                for n in probe.values():
                    if n.get("class_type") == "LoadImage":
                        n["inputs"]["image"] = i2v_uploaded
            except Exception as e:
                log.warning(f"ref-image i2v setup failed for {sid}, "
                            f"falling back to t2i: {e}")
                i2v_uploaded = None
        template = ("wan2.2_t2i_dynamic"
                    if "wan2.2_t2i_dynamic" in list_templates()
                    else "wan2.2_t2i_5b")

        def make(seed: int) -> dict:
            if i2v_uploaded is not None:
                wf = builder.build(
                    "wan2.2_i2v_still", prompt=prompt,
                    negative=negative, seed=seed, width=width,
                    height=height, steps=12, frames=1, cfg=self.cfg,
                    extra={"SHOT_ID": sid,
                           "LORA_CHAIN": list(lora_chain or []),
                           "START_IMAGE": i2v_uploaded,
                           "DENOISE": 0.5})
                for n in wf.values():
                    if n.get("class_type") == "LoadImage":
                        n["inputs"]["image"] = i2v_uploaded
                return wf
            return builder.build(
                template, prompt=prompt, negative=negative,
                seed=seed, width=width, height=height, steps=steps,
                frames=1, cfg=self.cfg,
                extra={"SHOT_ID": sid,
                       "LORA_CHAIN": list(lora_chain or [])})
        return make

    # --------------------------------------------------------------- rank
    def rank(self, files) -> list[dict]:
        """Pure scoring, no Comfy: sharpness (Laplacian, log-normalized)
        + face presence (insightface buffalo_l, CPU)."""
        out = []
        for f in files:
            p = Path(f)
            score = _sharpness(p)
            face = _has_face(p)
            out.append({"file": str(p), "score": round(score, 4),
                        "face": bool(face)})
        out.sort(key=lambda d: (d["score"], d["face"]), reverse=True)
        return out

    # ------------------------------------------------------------- approve
    def approve(self, shot_id: str, file, pid: str | None = None) -> bool:
        try:
            p = _kf_dir(shot_id, pid) / "candidates.json"
            if not p.exists():
                return False
            # a bare candidate name (what the UI sends) or a ROOT-relative
            # path inside the keyframes tree — never an arbitrary path, since
            # best_keyframe() later opens it as the I2V start frame
            f = str(file)
            if (Path(f).name != f or f in ("", ".", "..")) \
                    and _kf_file(f) is None:
                log.warning(f"keyframe approve {shot_id}: refused {f!r}")
                return False
            data = jload(p)
            data["approved"] = str(file)
            jsave(p, data)
            return True
        except Exception as e:
            log.warning(f"keyframe approve {shot_id} failed: {e}")
            return False

    def candidates(self, shot_id: str, pid: str | None = None) -> dict:
        if not is_safe_component(str(shot_id or "")):
            return {}
        p = _kf_dir(shot_id, pid) / "candidates.json"
        if not p.exists():
            return {}
        try:
            return jload(p)
        except Exception as e:
            log.warning(f"keyframe candidates read failed: {e}")
            return {}

    def clear(self, shot_id: str, pid: str | None = None) -> None:
        shutil.rmtree(_kf_dir(shot_id, pid), ignore_errors=True)


def best_keyframe(shot_id: str, pid: str | None = None) -> Path | None:
    """Best-ranked candidate for a shot (approved wins, else top score)."""
    if not is_safe_component(str(shot_id or "")):
        return None
    p = _kf_dir(shot_id, pid) / "candidates.json"
    if not p.exists():
        return None
    try:
        data = jload(p)
        files = data.get("files") or []
        approved = data.get("approved")
        if approved:
            cand = _kf_file(approved)
            if cand is not None and cand.exists():
                return cand
        for f in sorted(files, key=lambda d: d.get("score", 0.0),
                        reverse=True):
            cand = _kf_file(f.get("file", ""))
            if cand is not None and cand.exists():
                return cand
    except Exception as e:
        log.warning(f"keyframe best read failed: {e}")
    return None


def resolve_project_ref(store, pid) -> Path | None:
    """Project story reference image -> file under the project bible's
    refs/ dir. Returns None when no reference is set or the file is gone."""
    try:
        if store is None:
            return None
        proj = store.get(pid)
        if not proj:
            return None
        story = proj.get("story") or {}
        if not isinstance(story, dict):
            return None
        fname = story.get("reference")
        if not fname or not isinstance(fname, str):
            return None
        if Path(fname).name != fname or not is_safe_component(str(pid)):
            return None
        from bible import BIBLE_DIR
        try:
            cand = safe_join(BIBLE_DIR / "projects" / pid / "refs", fname)
        except InvalidInput:
            return None
        return cand if cand.is_file() else None
    except Exception as e:
        log.warning(f"project ref resolve failed for {pid}: {e}")
        return None
