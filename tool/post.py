"""Post pipeline. Per-shot:
extract -> interpolate (skip) -> upscale (SeedVR2 via ComfyUI | lanczos) ->
V2V refine -> face-fix (FaceDetailer) -> DHCC -> temporal pass -> LUT grade ->
encode. All heavy passes run through the same ComfyUI instance (the 1080)
while the next shot renders — hidden by pipelining."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path

from core import ROOT, ffmpeg_exe, load_config, log_setup, tool_path

log = log_setup("post")

_NVENC_OK: bool | None = None


def _ff(cfg: dict) -> str:
    return ffmpeg_exe(cfg)


def _run(cmd: list[str], cwd=None) -> tuple[int, str]:
    log.debug(" ".join(str(c) for c in cmd))
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       cwd=str(cwd) if cwd else None)
    if p.returncode != 0:
        log.error(p.stderr[-2000:])
    return p.returncode, p.stderr


class PostPipeline:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()

    # ------------------------------------------------------------- helpers
    def video_frames(self, video: Path, out_dir: Path) -> int:
        out_dir.mkdir(parents=True, exist_ok=True)
        _run([_ff(self.cfg), "-y", "-v", "error", "-i", str(video),
              "-fps_mode", "passthrough", str(out_dir / "f_%05d.png")])
        return len(glob.glob(str(out_dir / "f_*.png")))

    def probe_fps(self, video: Path) -> float | None:
        """The clip's frame rate from ffmpeg's stream info, or None."""
        try:
            p = subprocess.run([_ff(self.cfg), "-i", str(video)],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(r"Video:.*?,\s*([\d.]+)\s*fps", p.stderr or "")
        try:
            fps = float(m.group(1)) if m else 0.0
        except ValueError:
            fps = 0.0
        return fps if 1.0 <= fps <= 240.0 else None

    def frames_to_video(self, frames_dir: Path, out: Path, fps: float,
                        audio_src: Path | None = None) -> None:
        """Encode f_%05d.png at `fps`. audio_src: mux that clip's audio
        track back in (H3 renders carry sound; the frame round-trip dropped
        it). Optional mapping — a silent source just yields a silent clip."""
        cmd = [_ff(self.cfg), "-y", "-v", "error", "-framerate", f"{fps:g}",
               "-i", str(frames_dir / "f_%05d.png")]
        if audio_src is not None:
            cmd += ["-i", str(audio_src), "-map", "0:v:0", "-map", "1:a:0?"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-pix_fmt", "yuv420p"]
        if audio_src is not None:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        _run(cmd + [str(out)])

    def _run_comfy(self, template: str, values: dict) -> dict:
        """Run a post workflow through ComfyUI; returns {images: [paths]}.
        Retries transient connection resets — ComfyUI occasionally drops
        long downloads (WinError 10054), which previously stranded shots
        in 'post' forever."""
        from renderer import ComfyClient
        from workflows import WorkflowBuilder
        import uuid as _uuid
        b = WorkflowBuilder()
        if "OUT_PREFIX" in values:
            # unique prefix per call — SaveImage appends to existing files,
            # and stale outputs from earlier attempts were being re-downloaded
            values["OUT_PREFIX"] = (f"{values['OUT_PREFIX']}_"
                                    f"{_uuid.uuid4().hex[:6]}")
        wf = b.build(template, prompt="", negative="", seed=42,
                     width=0, height=0, steps=0, frames=0,
                     cfg=self.cfg, extra=values)
        c = ComfyClient(self.cfg["ensemble"]["comfyui_port"])
        last_err = None
        for attempt in range(4):
            try:
                pid = c.queue_prompt(wf)
                break
            except (ConnectionResetError, TimeoutError,
                    urllib.error.URLError) as e:
                last_err = e
                log.warning(f"post '{template}' submit transient error "
                            f"(attempt {attempt + 1}/4): {e}")
                time.sleep(8)
        else:
            raise RuntimeError(
                f"post workflow '{template}' submit gave up: {last_err}")
        # NEVER resubmit an accepted prompt (duplicates balloon the output)
        res = None
        for _ in range(4):
            try:
                res = c.wait_for(pid, timeout_s=3600)
                break
            except (ConnectionResetError, TimeoutError,
                    urllib.error.URLError) as e:
                last_err = e
                log.warning(f"post '{template}' wait transient error — "
                            f"rechecking history: {e}")
                time.sleep(8)
                try:
                    h = c.history(pid)
                    if pid in h:
                        res = h[pid]
                        break
                except Exception:
                    pass
        if res is None:
            raise RuntimeError(
                f"post workflow '{template}' gave up: {last_err}")
        st = res.get("status", {})
        if st.get("status_str") != "success":
            for m in st.get("messages", []):
                if m[0] == "execution_error":
                    raise RuntimeError(
                        f"post workflow '{template}' failed: "
                        f"{m[1].get('exception_message', '')[:300]}")
            raise RuntimeError(
                f"post workflow '{template}' failed")
        paths = []
        for o in res.get("outputs", {}).values():
            for v in o.get("images", []):
                paths.append((v.get("subfolder", ""), v["filename"]))
        return {"images": paths}

    def _stage_frames(self, frames: Path) -> str:
        """Copy frames into ComfyUI/input (VHS_LoadImages only accepts
        ComfyUI-relative dirs) and return the relative directory."""
        import uuid
        rel = (f"kinoloom_post/{frames.parent.parent.name}_"
               f"{uuid.uuid4().hex[:6]}")
        dst = ROOT / "ComfyUI" / "input" / rel
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        for f in glob.glob(str(frames / "f_*.png")):
            shutil.copy(f, dst / os.path.basename(f))
        return rel.replace("\\", "/")

    def _collect(self, entries: list[tuple[str, str]], dest: Path) -> int:
        """Download the EXACT output files returned by the workflow (no
        filename guessing — pattern-matching previously re-downloaded stale
        files from earlier attempts, ballooning frame counts). Saved as
        f_%05d.png in sequence order for the ffmpeg stage."""
        from renderer import ComfyClient
        c = ComfyClient(self.cfg["ensemble"]["comfyui_port"])
        count = 0
        for i, (subfolder, name) in enumerate(entries):
            try:
                c.download(subfolder, name, dest / f"f_{i + 1:05d}.png")
                count += 1
            except Exception as e:
                log.warning(f"post download failed for {subfolder}/{name}: {e}")
        return count

    def upscale_remote(self, video: Path, worker: str, fps: int | None = None,
                       rife: bool | None = None) -> Path | None:
        """Run RIFE (optional) + RealESRGAN x4 on `worker` instead of this
        machine: ONE mp4 in -> ONE mp4 out through the worker agent.

        Returns the local path of the upscaled clip, or None when the worker
        is unavailable/fails (callers decide the fallback — the post pipeline
        runs its normal local chain, the one-off upscale endpoint reports an
        error). The worker is serialized by design (agent executes one job at
        a time), so a busy worker is polled — exactly the "queue behind the
        upscaler" behaviour."""
        try:
            import workers as workers_mod
            import uuid as _uuid
            from workflows import WorkflowBuilder, SUBST
            client = workers_mod.client_for(worker)
            up_name = client.upload_file(video, name=f"ws_up_{_uuid.uuid4().hex[:10]}.mp4")
            cfgp = self.cfg["post"]
            target_fps = int(fps or cfgp.get("fps_target", 48) or 48)
            want_rife = (bool(rife) if rife is not None
                         else bool(cfgp.get("rife")) and
                         target_fps > int(cfgp.get("fps_generate", 24) or 24))
            template = "post_worker_upscale" if want_rife else "post_worker_upscale_norife"
            values = {
                "VIDEO_NAME": up_name,
                "ESRGAN_MODEL": SUBST.get("ESRGAN_MODEL", "RealESRGAN_x4plus.pth"),
                "OUT_PREFIX": f"kinoloom/worker_up_{_uuid.uuid4().hex[:6]}",
                "FPS": target_fps if want_rife else int(cfgp.get("fps_generate", 24) or 24),
            }
            wf = WorkflowBuilder().build(template, prompt="", negative="",
                                         seed=42, width=0, height=0, steps=0,
                                         frames=0, cfg=self.cfg, extra=values)
            pid = None
            last_err = None
            deadline = time.time() + 3600
            while time.time() < deadline:
                try:
                    pid = client.queue_prompt(wf)
                    break
                except Exception as e:  # 409 busy / transient
                    last_err = e
                    log.info(f"post worker {worker} busy/unreachable — retrying "
                             f"in 20 s ({e})")
                    time.sleep(20)
            if pid is None:
                raise RuntimeError(f"worker {worker} never accepted the job: {last_err}")
            res = client.wait_for(pid, timeout_s=1800)
            st = (res or {}).get("status", {})
            if res is None or st.get("status_str") != "success":
                raise RuntimeError("worker upscale job failed")
            entries = []
            for o in res.get("outputs", {}).values():
                for key in ("gifs", "videos", "images"):
                    for v in o.get(key) or []:
                        entries.append((v.get("subfolder", ""), v["filename"]))
            if not entries:
                raise RuntimeError("worker upscale produced no output")
            dst = video.with_name(f"{video.stem}_wsup.mp4")
            sub, name = entries[0]
            client.download(sub, name, dst)
            if not dst.exists() or dst.stat().st_size < 1024:
                raise RuntimeError("worker upscale output missing after download")
            log.info(f"remote upscale done on {worker}: {dst.name}")
            return dst
        except Exception as e:  # noqa: BLE001
            log.warning(f"remote upscale on {worker} unavailable ({e}) — "
                        f"running the local post chain instead")
            return None

    # ----------------------------------------------------------- pipeline
    def process(self, shot: dict, video: Path, qa_metrics: dict,
                worker: str = "") -> Path:
        cfg = self.cfg
        tier = shot.get("tier", "standard")
        work = ROOT / "shots" / "work" / shot["shot_id"]
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        frames = work / "frames"
        frames.mkdir()
        out_final = work / f"{shot['shot_id']}.mp4"
        target_fps = cfg["post"]["fps_target"]
        # purge stale post outputs — SaveImage never cleans old files and
        # pattern-based collection re-downloaded them (frame balloon bug)
        purge = ROOT / "ComfyUI" / "output" / "kinoloom"
        if purge.exists():
            for f in purge.glob("post_*"):
                try:
                    f.unlink()
                except OSError:
                    pass

        # 0) OPTIONAL REMOTE UPSCALE — the whole RIFE+ESRGAN pair runs on the
        #    second PC; its mp4 comes back and the local chain continues from
        #    the upscaled clip (no local RIFE/upscale anymore for this shot).
        source = video                  # the render: carries the audio track
        remote_done = False
        if worker and worker != "local":
            up = self.upscale_remote(video, worker, target_fps)
            if up is not None:
                video = up
                remote_done = True

        # 1) extract
        n = self.video_frames(video, frames)
        if n == 0:
            raise RuntimeError("no frames extracted from render")
        # the encode rate must follow the FRAMES, not the target: encoding a
        # native 24 fps sequence at 48 (RIFE failed / skipped) played the
        # shot at double speed and half its length
        out_fps = self.probe_fps(video) or float(
            cfg["post"].get("fps_generate", 24) or 24)

        # 1b) RIFE interpolation 2x, e.g. 24 -> 48 (post_rife workflow)
        if not remote_done and cfg["post"].get("rife") and target_fps > out_fps:
            try:
                self._interpolate_rife(frames)
                out_fps *= 2
            except Exception as e:
                log.warning(f"RIFE failed ({e}) — keeping native fps")

        # 2) upscale (tier-routed; SeedVR2 when available)
        if not remote_done:
            method = cfg["tiers"]["upscale"].get(tier, "none")
            if method == "seedvr2":
                try:
                    self._upscale_seedvr2(frames)
                except Exception as e:
                    log.warning(f"SeedVR2 failed ({e}) — lanczos fallback")
                    self._upscale_esrgan(frames)
            elif method in ("esrgan", "lanczos"):
                self._upscale_esrgan(frames)

        # 3) V2V refine (faces/detail regeneration) — hero/standard only
        if cfg["post"].get("v2v_refine") and tier in ("hero", "standard"):
            try:
                self._v2v_refine(frames, shot)
            except Exception as e:
                log.warning(f"V2V refine failed ({e}) — skipping")

        # 4) face-fix (gated per 7.10)
        if self._need_facefix(shot, qa_metrics) and cfg["post"].get(
                "face_fix"):
            try:
                self._facefix(frames, shot)
            except Exception as e:
                log.warning(f"face-fix failed ({e}) — skipping")

        # 5) DHCC (histogram match vs pre-upscale reference saved earlier)
        if cfg["post"].get("dhcc"):
            self._dhcc(frames)

        # 6) temporal shimmer pass
        if cfg["post"].get("temporal_pass"):
            self._temporal_pass(frames)

        # 7) LUT grade
        self._grade(frames)

        # 8) encode
        self.frames_to_video(frames, out_final, out_fps, audio_src=source)
        if not out_final.exists() or out_final.stat().st_size < 1024:
            raise RuntimeError("frames_to_video produced no output")

        # 9) NVENC draft (libx264 fallback when NVENC is unusable)
        draft = ROOT / "output" / "drafts" / \
            f"{shot.get('scene', 'x')}_{shot['shot_id']}.mp4"
        draft.parent.mkdir(parents=True, exist_ok=True)
        self._nvenc(out_final, draft)

        # an LTX-2.5 / H3 shot keeps the raw render's last frame (taken by the
        # orchestrator): the next CONTINUE opens on what the model rendered
        if not shot.get("applied_model"):
            from continuity import extract_tail
            try:
                extract_tail(draft if draft.exists() else out_final,
                             shot["shot_id"], pid=shot.get("project_id"))
            except Exception as e:
                log.warning(f"tail extraction failed: {e}")

        if cfg["post"].get("temp_cleanup", True):
            shutil.rmtree(work, ignore_errors=True)
        return draft

    # ------------------------------------------------------- stage impls
    def _interpolate_rife(self, frames: Path) -> None:
        """RIFE 2x via ComfyUI (post_rife workflow). Doubles the frame count."""
        rel = self._stage_frames(frames)
        prefix = f"post_{frames.parent.name}"
        res = self._run_comfy("post_rife", {
            "FRAMES_DIR": rel, "OUT_PREFIX": f"kinoloom/{prefix}"})
        tmp = frames.with_name("frames_rife")
        tmp.mkdir(exist_ok=True)
        n = self._collect(res["images"], tmp)
        if n == 0:
            raise RuntimeError("RIFE returned no frames")
        shutil.rmtree(frames)
        tmp.rename(frames)
        log.info(f"RIFE 2x done ({n} frames)")

    def _upscale_seedvr2(self, frames: Path) -> None:
        rel = self._stage_frames(frames)
        prefix = f"post_{frames.parent.name}"
        res = self._run_comfy("post_upscale", {
            "FRAMES_DIR": rel, "OUT_PREFIX": f"kinoloom/{prefix}"})
        tmp = frames.with_name("frames_sv2")
        tmp.mkdir(exist_ok=True)
        n = self._collect(res["images"], tmp)
        if n == 0:
            raise RuntimeError("SeedVR2 returned no frames")
        shutil.rmtree(frames)
        tmp.rename(frames)
        log.info(f"SeedVR2 upscale done ({n} frames)")

    def _upscale_esrgan(self, frames: Path) -> None:
        """AI upscale via `UpscaleModelLoader` + `ImageUpscaleWithModel`.

        ⚠ This method used to run `ffmpeg -vf scale=iw*2:ih*2:flags=lanczos`
        — a plain 2x resize with **no neural network at all**, despite the
        name. Enabling `tiers.upscale: esrgan` therefore added zero detail
        while looking like it had worked. The `post_esrgan` template now does
        the real thing.

        Measured on this machine (121 frames, 832x480 -> 3328x1920,
        `RealESRGAN_x4plus`): **~405 s**, ~6977 MB VRAM, and **no temporal
        flicker** (per-megapixel frame-to-frame instability 30.71 -> 1.90).

        `4xNomos8kDAT` was also tested and rejected: 1397 s for detail
        indistinguishable from RealESRGAN — 3.4x the cost for nothing.
        """
        rel = self._stage_frames(frames)
        prefix = f"post_{frames.parent.name}"
        res = self._run_comfy("post_esrgan", {
            "FRAMES_DIR": rel, "OUT_PREFIX": f"kinoloom/{prefix}"})
        tmp = frames.with_name("frames_up")
        tmp.mkdir(exist_ok=True)
        n = self._collect(res["images"], tmp)
        if n == 0:
            raise RuntimeError("ESRGAN upscale returned no frames")
        shutil.rmtree(frames)
        tmp.rename(frames)
        log.info(f"ESRGAN upscale done ({n} frames)")

    def _v2v_refine(self, frames: Path, shot: dict) -> None:
        rel = self._stage_frames(frames)
        prefix = f"post_{frames.parent.name}"
        res = self._run_comfy("wan_v2v", {
            "FRAMES_DIR": rel, "OUT_PREFIX": f"kinoloom/{prefix}",
            "PROMPT": ("high detail, sharp facial features, crisp "
                       "texture, photorealistic skin, fabric detail"),
            "NEGATIVE": ("blurry, soft, low detail, smeared, deformed")})
        tmp = frames.with_name("frames_v2v")
        tmp.mkdir(exist_ok=True)
        n = self._collect(res["images"], tmp)
        if n == 0:
            raise RuntimeError("V2V returned no frames")
        shutil.rmtree(frames)
        tmp.rename(frames)
        log.info(f"V2V refine done ({n} frames)")

    def _facefix(self, frames: Path, shot: dict) -> None:
        from bible import Bible
        char = next((c for c in shot.get("characters", [])
                     if c.get("tier") in ("main", "supporting")), None)
        char_lora = "none"
        if char:
            cand = ROOT / "models" / "loras" / f"{char['id']}_face.safetensors"
            if cand.exists():
                char_lora = cand.name
        # every-Nth-frame pass: process only the selected frames, then carry
        # their correction onto the frames in between (_apply_facefix)
        every_n = max(1, int(self.cfg["post"].get("face_fix_every_n", 3) or 1))
        all_frames = sorted(glob.glob(str(frames / "f_*.png")))
        if len(all_frames) <= every_n:
            work_frames = all_frames
        else:
            work_frames = all_frames[::every_n]
        tmp = frames.with_name("frames_fx")
        tmp.mkdir(exist_ok=True)
        for f in work_frames:
            shutil.copy(f, tmp / os.path.basename(f))
        prefix = f"post_{frames.parent.name}"
        res = self._run_comfy("face_fix", {
            "FRAMES_DIR": self._stage_frames(tmp),
            "OUT_PREFIX": f"kinoloom/{prefix}",
            "CHAR_LORA": char_lora,
            "PROMPT": "detailed face, photorealistic skin texture",
            "NEGATIVE": "deformed face, blurry face, morphing"})
        out_tmp = frames.with_name("frames_fx_out")
        out_tmp.mkdir(exist_ok=True)
        n = self._collect(res["images"], out_tmp)
        if n == 0:
            raise RuntimeError("face-fix returned no frames")
        # map processed frames back onto the full sequence (_collect writes
        # them as f_00001.. in input order)
        done = sorted(glob.glob(str(out_tmp / "f_*.png")))
        if len(done) != len(work_frames):
            log.warning(f"face-fix returned {len(done)} frames for "
                        f"{len(work_frames)} inputs — mapping by order")
        self._apply_facefix(all_frames, work_frames[:len(done)], done)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out_tmp, ignore_errors=True)
        log.info(f"face-fix done ({n} processed of {len(all_frames)} frames)")

    @staticmethod
    def _apply_facefix(all_frames: list[str], work_frames: list[str],
                       done: list[str]) -> None:
        """Write face-fixed frames back onto the sequence, in place.

        A processed frame takes the fix only where it changed the image (a
        feathered difference mask), so the background stays the original
        pixels. The frames BETWEEN processed ones (face_fix_every_n > 1) get
        the neighbouring corrections as a linearly interpolated delta: they
        keep their own motion (copying the fixed frame over them froze it)
        and the face does not flicker between fixed and unfixed every n-th
        frame (blending only the processed frames did). Streaming: at most
        two deltas are held in memory."""
        import cv2
        import numpy as np

        def masked_delta(orig, fixed):
            delta = fixed.astype(np.float32) - orig.astype(np.float32)
            mask = (np.abs(delta).max(axis=2) > 10).astype(np.uint8) * 255
            mask = cv2.GaussianBlur(mask, (15, 15), 0)
            return delta * (mask.astype(np.float32) / 255.0)[:, :, None]

        def add(path, delta) -> None:
            img = cv2.imread(path)
            if img is None or img.shape != delta.shape:
                return
            cv2.imwrite(path, np.clip(img.astype(np.float32) + delta, 0, 255)
                        .astype(np.uint8))

        index = {f: i for i, f in enumerate(all_frames)}
        prev = None                      # (sequence index, masked delta)
        for wf, d in zip(work_frames, done):
            k = index[wf]
            orig, fixed = cv2.imread(wf), cv2.imread(d)
            if orig is None or fixed is None or orig.shape != fixed.shape:
                shutil.copy(d, wf)       # different size: take the fix as-is
                prev = None
                continue
            cur = masked_delta(orig, fixed)
            add(wf, cur)
            lo = prev[0] + 1 if prev else k
            for j in range(lo, k):
                w = (j - prev[0]) / (k - prev[0])
                add(all_frames[j], cur * w + prev[1] * (1.0 - w))
            prev = (k, cur)
        if prev:
            for j in range(prev[0] + 1, len(all_frames)):
                add(all_frames[j], prev[1])

    def _need_facefix(self, shot: dict, qa: dict) -> bool:
        if shot.get("tier") == "volume":
            return False
        close_up = shot.get("shot_size") in ("extreme_close_up", "close_up",
                                             "medium_close_up")
        has_locked = any(c.get("tier") in ("main", "supporting")
                         for c in shot.get("characters", []))
        if close_up and has_locked:
            return True
        # QA-driven gating (7.10): failed face QA on a locked-character shot
        return bool(qa.get("face_fail")) and has_locked

    def _dhcc(self, frames: Path) -> None:
        """Histogram color correction placeholder (opencv impl, cheap)."""
        try:
            import cv2
            import numpy as np
            files = sorted(glob.glob(str(frames / "f_*.png")))
            if len(files) < 2:
                return
            # reference = average histogram of the first few frames (a single
            # frame-0 reference crushed dark scenes where frame 0 was an outlier)
            nref = min(5, len(files))
            refs = [cv2.imread(f) for f in files[:nref]]
            refs = [r for r in refs if r is not None]
            if not refs:
                return
            ref_hs = []
            for ch in range(3):
                hs = [cv2.calcHist([r], [ch], None, [256], [0, 256])
                      for r in refs]
                ref_hs.append(sum(hs) / len(hs))
            for f in files[1:]:
                img = cv2.imread(f)
                if img is None:
                    continue
                for ch in range(3):
                    src_h = cv2.calcHist([img], [ch], None, [256], [0, 256])
                    lut = np.interp(np.cumsum(src_h).ravel(),
                                    np.cumsum(ref_hs[ch]).ravel(),
                                    np.arange(256)).astype(np.uint8)
                    img[:, :, ch] = lut[img[:, :, ch]]
                cv2.imwrite(f, img)
            log.info("DHCC done")
        except Exception as e:
            log.warning(f"DHCC skipped: {e}")

    def _temporal_pass(self, frames: Path) -> None:
        """Light temporal smoothing on low-motion regions (flicker fix)."""
        try:
            import cv2
            import numpy as np
            files = sorted(glob.glob(str(frames / "f_*.png")))
            if len(files) < 3:
                return
            imgs = [cv2.imread(f) for f in files]
            imgs = [i for i in imgs if i is not None]
            if len(imgs) < 3:
                return
            for i in range(1, len(imgs) - 1):
                prev_g = cv2.cvtColor(imgs[i - 1], cv2.COLOR_BGR2GRAY)
                cur_g = cv2.cvtColor(imgs[i], cv2.COLOR_BGR2GRAY)
                flow = cv2.calcOpticalFlowFarneback(
                    prev_g, cur_g, None, 0.5, 3, 15, 3, 5, 1.2, 0)
                mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                mask = (mag < 0.3).astype(np.float32)[..., None]
                blended = (imgs[i].astype(np.float32) * 0.6 +
                           imgs[i - 1].astype(np.float32) * 0.4)
                imgs[i] = (imgs[i] * (1 - mask) +
                           blended * mask).astype(np.uint8)
            for f, img in zip(files, imgs):
                cv2.imwrite(f, img)
            log.info("temporal pass done")
        except Exception as e:
            log.warning(f"temporal pass skipped: {e}")

    def _grade(self, frames: Path) -> None:
        # relative LUT paths are studio-root relative (the server's cwd is
        # tool/, so a bare Path(lut) missed and the film shipped ungraded)
        lut = tool_path(self.cfg["post"].get("lut"))
        if lut is None or not lut.exists():
            return
        # Two Windows/ffmpeg-9 traps handled here:
        #  1) a drive-colon inside a filtergraph is unescapable in practice —
        #     run ffmpeg with the LUT's own directory as cwd and use the bare
        #     filename (no colon at all);
        #  2) ffmpeg refuses "Output == Input" — write each graded frame to a
        #     temp file and swap it in (in-place grading was silently rejected
        #     and the film shipped ungraded).
        lut_path = Path(lut)
        for f in sorted(glob.glob(str(frames / "f_*.png"))):
            tmp = f[:-4] + ".lut.png"
            rc, _ = _run([_ff(self.cfg), "-y", "-v", "error", "-i", f,
                          "-vf", f"lut3d=file={lut_path.name}", "-c:v", "png",
                          tmp], cwd=lut_path.parent)
            if rc != 0 or not os.path.exists(tmp):
                log.warning(f"LUT grade failed on {Path(f).name} "
                            f"(rc={rc}) — grade NOT applied")
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return
            os.replace(tmp, f)

    def _nvenc(self, src: Path, dst: Path) -> None:
        global _NVENC_OK
        if _NVENC_OK is False:
            # NVENC already failed in this process — skip h264_nvenc entirely
            # for the rest of the batch
            _run([_ff(self.cfg), "-y", "-i", str(src), "-c:v", "libx264",
                  "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                  str(dst)])
            return
        rc, _ = _run([_ff(self.cfg), "-y", "-i", str(src), "-c:v",
                      "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc",
                      "vbr", "-cq", "19", "-b:v", "0", "-rc-lookahead", "20",
                      "-spatial-aq", "1", "-profile:v", "high",
                      "-pix_fmt", "yuv420p", str(dst)])
        if rc != 0 or not dst.exists() or dst.stat().st_size < 1024:
            _NVENC_OK = False
            log.warning("h264_nvenc unavailable (driver/ffmpeg NVENC API "
                        "mismatch?) — libx264 fallback (cached for rest of "
                        "batch)")
            if dst.exists():
                dst.unlink()
            _run([_ff(self.cfg), "-y", "-i", str(src), "-c:v", "libx264",
                  "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                  str(dst)])
        else:
            _NVENC_OK = True
