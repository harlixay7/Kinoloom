"""Images — standalone text-to-image studio (web UI "Images" tab).

Totally decoupled from the movie pipeline: prompt in, pretty pictures out.
Model definitions live in models.py (the canonical registry) — this module
only manages generation jobs, albums and the gallery.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core import ROOT, log_setup
from renderer import ComfyClient, queue_position
from models import (GEN_LORAS, LORA_TRIGGER_EXTRA, adapt_prompt,
                    build_hires_workflow, build_workflow, get_model,
                    record_performance)
from workflows import SUBST as _WF_SUBST

log = log_setup("images")

GALLERY = ROOT / "output" / "images"
ALBUMS_FILE = GALLERY / "albums.json"
UNSORTED = "__unsorted__"
CLIENT_ID = "studio-imagegen"          # ComfyUI attribution for this producer


def _exec_timings(res: dict, submitted_at: float) -> tuple[float | None,
                                                           float | None]:
    """(queue_wait_s, exec_s) from ComfyUI history timestamps when present.

    ComfyUI records `execution_start` / `execution_success` (epoch ms) in the
    history entry's status messages. When they are missing the fields stay
    None rather than being guessed from wall time (which would silently fold
    queue wait into render time — the exact confusion this fixes).
    """
    start = end = None
    status = (res or {}).get("status") or {}
    for msg in status.get("messages") or []:
        try:
            kind, info = msg[0], msg[1]
        except (TypeError, IndexError):
            continue
        ts = info.get("timestamp") if isinstance(info, dict) else None
        if not isinstance(ts, (int, float)):
            continue
        if kind == "execution_start":
            start = float(ts) / 1000.0
        elif kind in ("execution_success", "execution_error",
                      "execution_interrupted"):
            end = float(ts) / 1000.0
    if start is None:
        return None, None
    queue_wait = round(max(0.0, start - submitted_at), 1)
    exec_s = round(max(0.0, end - start), 1) if end is not None else None
    return queue_wait, exec_s

# starter prompts — clickable cards in the UI
PRESETS: list[dict] = [
    {"id": "portrait_woman", "icon": "👩", "title": "Golden-hour portrait",
     "prompt": "cinematic close-up portrait of a young woman with freckles and wavy auburn hair, soft golden hour sunlight, warm rim light, 85mm f/1.4 lens, shallow depth of field, ultra detailed skin texture, natural makeup, Kodak film grain, professional photography"},
    {"id": "portrait_man", "icon": "🧔", "title": "Old sailor",
     "prompt": "photorealistic portrait of an elderly weathered sailor with a white beard and deep blue eyes, salt-crusted skin, creased face, stormy sea behind him, overcast light, 50mm lens, tack sharp, documentary photography style"},
    {"id": "cyberpunk", "icon": "🌃", "title": "Neon alley",
     "prompt": "rain-slicked cyberpunk alley at night, glowing neon signs in cyan and magenta, puddle reflections, drifting steam from vents, cinematic wide shot, volumetric fog, blade-runner atmosphere, ultra detailed"},
    {"id": "castle", "icon": "🏰", "title": "Fantasy citadel",
     "prompt": "epic fantasy citadel on a mountain cliff at dusk, warm torchlight in windows, dramatic clouds, waterfalls cascading into mist, painterly cinematic lighting, ultra detailed matte painting, grand scale"},
    {"id": "astronaut", "icon": "🚀", "title": "Lone astronaut",
     "prompt": "astronaut standing on a desolate alien landscape, huge ringed planet on the horizon, two suns casting long shadows, highly detailed spacesuit with worn fabric texture, cinematic sci-fi photography"},
    {"id": "forest", "icon": "🌲", "title": "Misty forest",
     "prompt": "ancient misty forest with towering pines, god rays piercing the fog, moss-covered rocks, a narrow winding path, ethereal morning light, hyper realistic nature photography, 8k detail"},
    {"id": "dog", "icon": "🐕", "title": "Working dog",
     "prompt": "photorealistic portrait of a border collie with one blue eye and one brown eye, wet fur after a rain, intense loyal gaze, soft overcast light, 85mm lens, ultra detailed fur, national geographic style"},
    {"id": "perfume", "icon": "🧴", "title": "Product shot",
     "prompt": "luxury perfume bottle on black wet stone, dramatic studio lighting, water droplets on glass, gold accents catching light, smoke wisps, macro product photography, ultra sharp, commercial quality"},
    {"id": "car", "icon": "🏎️", "title": "Vintage racer",
     "prompt": "classic 1960s racing car on a coastal highway at sunset, chrome details gleaming, motion blur on the road, cinematic anamorphic photography, warm amber tones, hyper realistic"},
    {"id": "interior", "icon": "🛋️", "title": "Minimal interior",
     "prompt": "award-winning architectural photography of a minimalist living room, floor-to-ceiling windows overlooking a forest, warm oak wood, designer furniture, soft diffused daylight, airy and serene"},
    {"id": "desert", "icon": "🏜️", "title": "Dune walker",
     "prompt": "lone figure in a flowing dark robe walking across giant golden desert dunes, tiny against the landscape, dramatic shadows at sunset, fine sand texture, epic scale, cinematic color grade"},
    {"id": "snow", "icon": "❄️", "title": "Winter cabin",
     "prompt": "cozy wooden cabin buried in deep snow at blue hour, warm light glowing from windows, snow falling gently, pine trees heavy with frost, chimney smoke rising, hyper realistic winter photography"},
]


class ImageGen:
    """One threaded generator + a growing gallery on disk (png + json)
    + user albums (named collections images stack under)."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._lease = None
        self._client = None           # the running job's client (stop() target)
        self.active: dict = {"running": False, "done": 0, "total": 0,
                             "failed": 0, "message": "", "model": "",
                             "prompt_id": "", "queue_pos": None,
                             "hires": False}
        GALLERY.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------- generate
    def start(self, prompt: str, model: str = "realvis", width: int = 1024,
              height: int = 1024, steps: int | None = None,
              cfg: float | None = None, seed: int | None = None,
              count: int = 1, negative: str | None = None,
              album: str = "", loras: list | None = None,
              hires: bool = False, refs: list | None = None,
              ref_strength: float = 0.8, anchor: bool = False,
              worker: str = "local") -> dict:
        loras = loras or []
        refs = refs or []
        with self._lock:
            if self.active["running"]:
                return {"ok": False, "error": "a generation is already "
                                              "running"}
            m = get_model(model)
            if m is None:
                return {"ok": False, "error": f"unknown model '{model}'"}
            if not prompt or not prompt.strip():
                return {"ok": False, "error": "prompt is empty"}
            if hires and not m["capabilities"].get("hires", False):
                return {"ok": False,
                        "error": f"{m['name']} does not support the hires "
                                 f"fix (SDXL-family models only) — turn it "
                                 f"off to generate with this model"}
            if count < 1 or count > 8:
                return {"ok": False, "error": "count must be 1-8"}
            if len(loras) > 6:
                return {"ok": False, "error": "max 6 LoRAs"}
            if refs and not m["capabilities"].get("refs", False):
                return {"ok": False,
                        "error": f"{m['name']} does not support reference "
                                 f"images (SDXL-family models only)"}
            if len(refs) > 5:
                return {"ok": False, "error": "max 5 reference images"}
            try:
                rstrength = float(ref_strength)
            except (TypeError, ValueError):
                rstrength = 0.8
            if not (0.1 <= rstrength <= 1.5):
                return {"ok": False, "error": "ref strength 0.1-1.5"}
            norm_refs: list[dict] = []
            for r in refs:
                fname = r if isinstance(r, str) else (r.get("file") if isinstance(r, dict) else "")
                fname = str(fname or "").strip()
                if not fname or ".." in fname or "/" in fname or "\\" in fname:
                    return {"ok": False, "error": f"invalid reference file '{fname}'"}
                if not (GALLERY / "refs" / fname).exists():
                    return {"ok": False, "error": f"reference not found: {fname}"}
                rtype = r.get("type", "Identity") if isinstance(r, dict) else "Identity"
                norm_refs.append({"file": fname, "type": rtype})
            refs = norm_refs
            if not m["capabilities"].get("loras", False) and loras:
                return {"ok": False,
                        "error": f"{m['name']} does not support LoRAs"}
            for l in loras:
                name = l.get("file", "")
                try:
                    strength = float(l.get("strength", 0.7))
                except (TypeError, ValueError):
                    log.warning(f"bad LoRA strength {l.get('strength')!r} "
                                f"for {name} — using 0.7")
                    strength = 0.7
                if name not in GEN_LORAS:
                    return {"ok": False,
                            "error": f"unknown LoRA '{name}'"}
                if not (0.05 <= strength <= 2.0):
                    return {"ok": False, "error": "LoRA strength 0.05-2.0"}
            if album and album != UNSORTED and \
                    album not in self._load_albums()["albums"]:
                return {"ok": False, "error": f"album '{album}' not found"}
            # let the previous worker (if any) wind down before reusing state
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=5.0)
            cancel = threading.Event()
            self._cancel = cancel
            self.active = {"running": True, "done": 0, "total": count,
                           "failed": 0, "message": "starting",
                           "model": model, "prompt_id": "", "queue_pos": None,
                           "hires": bool(hires)}
        self._thread = threading.Thread(
            target=self._run,
            args=(prompt.strip(), model, width, height,
                  steps or m["steps"], cfg if cfg is not None
                  else m["cfg"], seed, count,
                  negative if negative is not None else m["negative"],
                  album if album and album != UNSORTED else "", loras,
                  hires, cancel, refs, rstrength, anchor, worker),
            daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self) -> None:
        self._cancel.set()
        self.active["running"] = False
        self.active["message"] = "stopped by user"
        # Stop THIS run only, on the instance it runs on (the model's
        # comfy_port, or its worker PC). Interrupting 8188 + clearing its
        # whole queue here killed an unrelated movie render whenever the
        # image ran on the Qwen instance or a worker, and wiped every other
        # producer's pending prompts.
        c = self._client
        pid = str(self.active.get("prompt_id") or "")
        if c is None:
            return
        if isinstance(c, ComfyClient):
            try:
                if pid:
                    c.post("/queue", {"delete": [pid]})     # if still pending
                if pid and c.queue_position(pid) == 0:      # ours is running
                    c.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning(f"image stop: {e}")
        else:
            try:
                c.interrupt()          # the worker client cancels its own job
            except Exception as e:  # noqa: BLE001
                log.warning(f"image stop (worker): {e}")

    # -------------------------------------------------------------- run
    def _run(self, prompt: str, model: str, width: int, height: int,
             steps: int, cfg: float, seed: int | None, count: int,
             negative: str, album: str = "", loras: list | None = None,
             hires: bool = False, cancel: threading.Event | None = None,
             refs: list | None = None, ref_strength: float = 0.8,
             anchor: bool = False, worker: str = "local") -> None:
        """Hold one GPU-broker lease for the WHOLE run (batch + hires) so no
        other model can evict or interleave mid-batch."""
        lease = None
        if not worker or worker == "local":
            try:
                import gpu_broker
                mdef = get_model(model) or {}
                port = int(mdef.get("comfy_port") or 8188)
                lease = gpu_broker.acquire(model=model, port=port,
                                           purpose="image", timeout=3600)
                self._lease = lease
            except Exception as e:  # noqa: BLE001
                log.exception(f"image broker acquire failed: {e}")
                self.active.update({
                    "running": False,
                    "failed": int(self.active.get("failed") or 0) + 1,
                    "message": f"GPU broker unavailable: {e}"})
                return
        try:
            self._run_body(prompt, model, width, height, steps, cfg, seed,
                           count, negative, album, loras, hires, cancel,
                           refs, ref_strength, anchor, worker)
        finally:
            if lease is not None:
                try:
                    import gpu_broker
                    gpu_broker.release(lease)
                except Exception:
                    pass
                self._lease = None

    def _run_body(self, prompt: str, model: str, width: int, height: int,
                  steps: int, cfg: float, seed: int | None, count: int,
                  negative: str, album: str = "", loras: list | None = None,
                  hires: bool = False, cancel: threading.Event | None = None,
                  refs: list | None = None, ref_strength: float = 0.8,
                  anchor: bool = False, worker: str = "local") -> None:
        loras = loras or []
        refs = refs or []
        cancel = cancel or threading.Event()
        # worker routing: local keeps the historical
        # hardcoded client; anything else routes through the worker agent.
        # Registry models may declare comfy_port — a dedicated ComfyUI
        # instance for that family (Qwen-Image-2.1 -> 8190, the v0.30
        # production farm cannot load it).
        mdef = get_model(model) or {}
        port = int(mdef.get("comfy_port") or 8188)
        if worker and worker != "local":
            import workers as _workers_mod
            c = _workers_mod.client_for(worker)
        else:
            c = ComfyClient(port)
        self._client = c                 # stop() targets exactly this client
        if not self._wait_comfy(c):
            self.active.update({
                "running": False,
                "failed": int(self.active.get("failed") or 0) + 1,
                "message": "ComfyUI not reachable"})
            return
        start_idx = self._next_index()
        # upload reference images once per run (ComfyUI input dir)
        ref_comfy: list[str] = []
        try:
            for r in refs:
                p = GALLERY / "refs" / str(r["file"])
                if p.exists():
                    ref_comfy.append(c.upload_image(p))
        except Exception as e:
            log.warning(f"reference upload failed: {e}")
        for i in range(count):
            if cancel.is_set() or not self.active["running"]:
                return
            s = (seed if seed is not None
                 else 100000 + int(time.time()) % 900000) + i
            fprompt, fneg = adapt_prompt(model, prompt, "images",
                                         user_prompt=None,
                                         user_negative=negative)
            if loras and fneg == get_model(model)["negative"]:
                fneg = fneg + ", " + LORA_TRIGGER_EXTRA
            if ref_comfy:
                from models import build_ref_workflow
                wf = build_ref_workflow(
                    model, fprompt, fneg, s, width, height, steps, cfg,
                    loras, ref_comfy, ref_strength, anchor,
                    shot_id=f"img_{int(time.time())}")
            else:
                wf = build_workflow(model, fprompt, fneg, s, width, height,
                                    steps, cfg, loras,
                                    hires=False, shot_id=f"img_{int(time.time())}")
            try:
                t_submit = time.time()
                pid = c.queue_prompt(wf, client_id=CLIENT_ID)
                self.active["prompt_id"] = pid
                self.active["queue_pos"] = None
                self.active["message"] = f"{model} · {width}x{height} · " \
                                         f"{steps} steps · queued"

                def _on_queue(q: dict, _pid: str = pid) -> None:
                    pos = queue_position(q, _pid)
                    if pos is None:
                        return
                    self.active["queue_pos"] = pos
                    where = "rendering" if pos == 0 else f"queued (#{pos})"
                    self.active["message"] = (f"{model} · {width}x{height} · "
                                              f"{steps} steps · {where}")

                t0 = t_submit
                res = c.wait_for(pid, timeout_s=1800, cb=_on_queue,
                                 cancel=cancel)
                if cancel.is_set() or not self.active["running"]:
                    return
                if res is None:
                    self.active["message"] = f"image {i + 1} timed out"
                    self.active["failed"] += 1
                    break
                dur = time.time() - t0
                qwait, exec_s = _exec_timings(res, t_submit)
                st = res.get("status", {})
                if st.get("status_str") == "error":
                    for msg in st.get("messages", []):
                        if msg[0] == "execution_error":
                            err = (msg[1].get("exception_message") or "")[:400]
                            raise RuntimeError(err or "workflow failed")
                    raise RuntimeError("workflow failed")
                saved = 0
                for o in res.get("outputs", {}).values():
                    for v in o.get("images", []):
                        idx = start_idx + self.active["done"] + saved
                        png = self._save(v.get("subfolder", ""), v["filename"],
                                         idx, fprompt, model, width, height,
                                         steps, cfg, s,
                                         refs=refs if refs else None,
                                         ref_strength=ref_strength if refs else None,
                                         anchor=anchor if refs else False,
                                         prompt_id=pid,
                                         queue_wait_s=qwait,
                                         exec_s=exec_s,
                                         hires_requested=hires,
                                         client=c)
                        saved += 1
                        record_performance(model, f"{width}x{height}",
                                           steps, False, dur)
                        if hires and self.active["running"] and \
                                not cancel.is_set():
                            try:
                                hr_pid = self._hires(c, model, png, fprompt,
                                                     fneg, cfg, s, width,
                                                     height, cancel)
                                if cancel.is_set():
                                    return
                                meta = GALLERY / png.name.replace(
                                    ".png", ".json")
                                try:
                                    mm = json.loads(
                                        meta.read_text(encoding="utf-8"))
                                    mm["hires"] = True
                                    if hr_pid:
                                        mm["hires_prompt_id"] = hr_pid
                                    meta.write_text(
                                        json.dumps(mm, indent=2,
                                                   ensure_ascii=False),
                                        encoding="utf-8")
                                except Exception:
                                    pass
                            except Exception as e:
                                log.warning(f"hires refine failed: {e}")
                if saved == 0:
                    raise RuntimeError("workflow returned no images")
                if album:
                    self.assign(png.name, album)
            except Exception as e:
                log.exception(f"image {i + 1} failed: {e}")
                self.active["message"] = f"image {i + 1} failed: {e}"
                self.active["failed"] += 1
                self.active["done"] = i + 1
                break
            self.active["done"] = i + 1
        if self.active["failed"] == 0 and \
                self.active["done"] == self.active["total"] and \
                self.active["running"]:
            self.active["message"] = "done"
        self.active["running"] = False

    def _hires(self, c, model: str, png: Path, prompt: str, negative: str,
               cfg: float, seed: int, w: int, h: int,
               cancel: threading.Event | None = None) -> str | None:
        fname = c.upload_image(png)
        wf = build_hires_workflow(model, prompt, negative, seed, cfg, w, h,
                                  fname, shot_id=f"hr_{int(time.time())}")
        pid = c.queue_prompt(wf, client_id=CLIENT_ID + "-hires")
        self.active["prompt_id"] = pid       # stop() targets the live prompt
        self.active["message"] = "hires refine"
        t0 = time.time()
        res = c.wait_for(pid, timeout_s=1800, cancel=cancel)
        if cancel is not None and cancel.is_set():
            return None
        if res is None:
            raise RuntimeError("hires refine timed out")
        st = res.get("status", {})
        if st.get("status_str") == "error":
            raise RuntimeError("hires refine failed")
        for o in res.get("outputs", {}).values():
            for v in o.get("images", []):
                c.download(v.get("subfolder", ""), v["filename"], png)
                hr_steps = int(_WF_SUBST.get("HIRES_STEPS", 8))
                record_performance(model, f"{w}x{h}", hr_steps, True,
                                   time.time() - t0)
                log.info(f"hires refined -> {png.name}")
                return pid
        raise RuntimeError("hires refine produced no output")

    def _save(self, subfolder: str, filename: str, idx: int, prompt: str,
              model: str, width: int, height: int, steps: int, cfg: float,
              seed: int, refs: list | None = None,
              ref_strength: float | None = None,
              anchor: bool = False, prompt_id: str = "",
              queue_wait_s: float | None = None,
              exec_s: float | None = None,
              hires_requested: bool = False,
              client: ComfyClient | None = None) -> Path:
        c = client or ComfyClient(8188)
        png = GALLERY / f"img_{idx:05d}.png"
        c.download(subfolder, filename, png)
        doc = {
            "file": png.name, "prompt": prompt, "model": model,
            "width": width, "height": height, "steps": steps,
            "cfg": cfg, "seed": seed,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            # provenance: which ComfyUI job produced this, what was requested,
            # and how long it waited vs executed (attribution for "whose job
            # was that?" questions — cross-writer queues are real here).
            "prompt_id": prompt_id,
            "client_id": CLIENT_ID,
            "hires_requested": bool(hires_requested),
            "queue_wait_s": queue_wait_s,
            "exec_s": exec_s,
        }
        if refs:
            doc["refs"] = refs
            doc["ref_strength"] = ref_strength
            doc["anchor"] = anchor
        (GALLERY / f"img_{idx:05d}.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        return png

    def _next_index(self) -> int:
        best = 0
        for p in GALLERY.glob("img_*.png"):
            try:
                best = max(best, int(p.stem.split("_")[1]) + 1)
            except (ValueError, IndexError):
                continue
        return best

    def _wait_comfy(self, c: ComfyClient, timeout_s: float = 180) -> bool:
        """Readiness probe that works for BOTH clients: the local ComfyClient
        and the worker RemoteWorkerClient (which has no .get() — the old probe
        looped here for the full timeout on every remote image job and then
        reported the run as finished with zero images)."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                c.system_stats()
                return True
            except Exception:
                time.sleep(5)
        return False

    # ----------------------------------------------------------- albums
    def _load_albums(self) -> dict:
        if ALBUMS_FILE.exists():
            try:
                return json.loads(ALBUMS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"albums": [], "assign": {}}

    def _save_albums(self, data: dict) -> None:
        ALBUMS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                               encoding="utf-8")

    def albums(self) -> dict:
        return self._load_albums()

    def create_album(self, title: str) -> dict:
        title = title.strip()
        if not title:
            return {"ok": False, "error": "title is empty"}
        if len(title) > 60:
            return {"ok": False, "error": "title too long (max 60)"}
        data = self._load_albums()
        if title in data["albums"]:
            return {"ok": False, "error": f"'{title}' already exists"}
        data["albums"].append(title)
        self._save_albums(data)
        return {"ok": True, "albums": data["albums"]}

    def rename_album(self, old: str, new: str) -> dict:
        new = new.strip()
        data = self._load_albums()
        if old not in data["albums"]:
            return {"ok": False, "error": f"album '{old}' not found"}
        if not new or len(new) > 60:
            return {"ok": False, "error": "invalid new title"}
        data["albums"] = [new if a == old else a for a in data["albums"]]
        data["assign"] = {k: (new if v == old else v)
                          for k, v in data["assign"].items()}
        self._save_albums(data)
        return {"ok": True}

    def delete_album(self, title: str) -> dict:
        data = self._load_albums()
        if title not in data["albums"]:
            return {"ok": False, "error": f"album '{title}' not found"}
        data["albums"].remove(title)
        data["assign"] = {k: v for k, v in data["assign"].items()
                          if v != title}
        self._save_albums(data)
        return {"ok": True}

    def assign(self, name: str, album: str) -> dict:
        data = self._load_albums()
        p = GALLERY / name
        if ".." in name or "/" in name or "\\" in name or \
                not p.exists() or p.suffix.lower() != ".png":
            return {"ok": False, "error": "invalid image"}
        if album and album not in data["albums"]:
            return {"ok": False, "error": f"album '{album}' not found"}
        if album:
            data["assign"][name] = album
        else:
            data["assign"].pop(name, None)
        self._save_albums(data)
        return {"ok": True}

    # ----------------------------------------------------------- gallery
    def list(self) -> dict:
        data = self._load_albums()
        out: list[dict] = []
        for p in sorted(GALLERY.glob("img_*.png"), reverse=True):
            meta_path = p.with_suffix(".json")
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            out.append({"file": p.name, "prompt": meta.get("prompt", ""),
                        "model": meta.get("model", ""),
                        "width": meta.get("width"), "height": meta.get("height"),
                        "steps": meta.get("steps"), "cfg": meta.get("cfg"),
                        "seed": meta.get("seed"), "ts": meta.get("ts", ""),
                        "hires": bool(meta.get("hires")),
                        "prompt_id": meta.get("prompt_id", ""),
                        "client_id": meta.get("client_id", ""),
                        "queue_wait_s": meta.get("queue_wait_s"),
                        "exec_s": meta.get("exec_s"),
                        "album": data["assign"].get(p.name, "")})
        return {"images": out, "albums": data["albums"]}

    def delete(self, name: str) -> bool:
        if ".." in name or "/" in name or "\\" in name:
            return False
        p = GALLERY / name
        if not p.exists() or p.suffix.lower() != ".png":
            return False
        p.unlink()
        (GALLERY / name.replace(".png", ".json")).unlink(missing_ok=True)
        data = self._load_albums()
        data["assign"].pop(name, None)
        self._save_albums(data)
        return True
