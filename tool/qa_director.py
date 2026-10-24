"""QA director: semantic and identity QA evaluation with a stepped retry ladder.
InsightFace embeddings vs character refs, CLIP prompt
alignment, optical-flow motion — metrics degrade gracefully when dependencies
are missing, and failure flags drive the retry ladder."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import ROOT, load_config, log_setup

log = log_setup("qa_director")

RETRY_LADDER = ["reseed", "simplify_prompt", "adjust_lora", "regen_keyframe",
                "review"]

_FROZEN_MOTION = 0.02


class QADirector:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self._face: Any = None       # lazy singletons
        self._clip: Any = None

    # ------------------------------------------------------------ models
    def _ensure_face(self) -> bool:
        if self._face is not None:
            return self._face is not False
        try:
            from insightface.app import FaceAnalysis
            self._face = FaceAnalysis(
                name="buffalo_l", providers=["CPUExecutionProvider"],
                allowed_modules=["detection", "recognition"])
            self._face.prepare(ctx_id=-1)
            return True
        except Exception as e:
            log.warning(f"face analysis unavailable: {e}")
            self._face = False
            return False

    def _ensure_clip(self) -> bool:
        if self._clip is not None:
            return self._clip is not False
        try:
            from transformers import CLIPModel, CLIPProcessor
            model = CLIPModel.from_pretrained(
                "openai/clip-vit-base-patch32", local_files_only=True)
            proc = CLIPProcessor.from_pretrained(
                "openai/clip-vit-base-patch32", local_files_only=True)
            self._clip = (model, proc)
            return True
        except Exception as e:
            log.warning(f"CLIP unavailable: {e}")
            self._clip = False
            return False

    # ------------------------------------------------------------ frames
    @staticmethod
    def _read_frames(video_path: str, keep_last: int | None = None) \
            -> list[Any]:
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        frames: list[Any] = []
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if keep_last is not None:
                    frames.append(frame)
                    if len(frames) > keep_last:
                        frames.pop(0)
                else:
                    frames.append(frame)
        finally:
            cap.release()
        return frames

    # ------------------------------------------------------------ metrics
    def face_similarity(self, video_path: str,
                        ref_paths: list | tuple | None) -> float | None:
        """Best-face cosine sim over the video's last 5 frames vs the best
        ref embedding(s) (roster refs or keyframe). None = unavailable;
        0.0 = no face detected."""
        if not self._ensure_face():
            return None
        try:
            import cv2
            import numpy as np
            ref_embs: list[Any] = []
            for r in ref_paths or []:
                img = cv2.imread(str(r))
                if img is None:
                    continue
                faces = self._face.get(img)
                if not faces:
                    continue
                ref_embs.append(faces[0].normed_embedding)
            if not ref_embs:
                return None
            best: float | None = None
            for frame in self._read_frames(video_path, keep_last=5):
                for f in self._face.get(frame):
                    emb = f.normed_embedding
                    sim = float(max(float(np.dot(emb, re))
                                    for re in ref_embs))
                    if best is None or sim > best:
                        best = sim
            return 0.0 if best is None else best
        except Exception as e:
            log.warning(f"face similarity failed: {e}")
            return None

    def clip_alignment(self, video_path: str, prompt: str) -> float | None:
        """CLIP cosine of the mid-frame image vs prompt; None if the model
        is unavailable or anything fails."""
        if not prompt or not self._ensure_clip():
            return None
        try:
            import cv2
            import torch
            model, proc = self._clip
            frames = self._read_frames(video_path)
            if not frames:
                return None
            mid = cv2.cvtColor(frames[len(frames) // 2], cv2.COLOR_BGR2RGB)
            inputs = proc(text=[prompt], images=[mid], return_tensors="pt",
                          padding=True)
            with torch.no_grad():
                out = model(**inputs)
            img_f = out.image_embeds
            txt_f = out.text_embeds
            img_f = img_f / img_f.norm(dim=-1, keepdim=True)
            txt_f = txt_f / txt_f.norm(dim=-1, keepdim=True)
            return float((img_f * txt_f).sum())
        except Exception as e:
            log.warning(f"clip alignment failed: {e}")
            return None

    def motion_score(self, video_path: str) -> float:
        """Delegate to the existing optical-flow implementation."""
        from face_qa import FaceQA
        return FaceQA(self.cfg).motion_score(str(video_path))

    # ----------------------------------------------------------- evaluate
    def _approved_keyframe(self, shot: dict) -> Path | None:
        kp = shot.get("keyframe_path")
        if kp and Path(kp).exists():
            return Path(kp)
        sid = shot.get("shot_id", "")
        p = ROOT / "shots" / "keyframes" / sid / "candidates.json"
        if p.exists():
            try:
                from core import jload
                data = jload(p)
                approved = data.get("approved")
                if approved:
                    cand = ROOT / str(approved)
                    if cand.exists():
                        return cand
            except Exception:
                pass
        return None

    def evaluate(self, shot: dict, video_path: str, prompt: str,
                 refs: list | None = None) -> dict:
        """Semantic + identity + motion QA. Every metric is optional; the
        shot passes only when no computed metric fails."""
        qa = self.cfg.get("qa") or {}
        result: dict[str, Any] = {
            "passed": False, "clip": None, "face": None, "motion": 0.0,
            "face_fail": False, "clip_fail": False, "motion_fail": False,
            "frozen": False, "detail": "",
        }
        motion = self.motion_score(video_path)
        result["motion"] = motion
        mmin, mmax = qa.get("motion_score_range", [0.00005, 6.0])
        result["motion_fail"] = not (mmin <= motion <= mmax)
        result["frozen"] = motion < _FROZEN_MOTION

        chars = [c for c in shot.get("characters", []) or []
                 if c.get("tier") in ("main", "supporting", "recurring")]
        if chars:
            has_lora = any(
                (ROOT / "models" / "loras"
                 / f"{c['id']}_face.safetensors").exists() for c in chars)
            ref_list = list(refs or [])
            if not ref_list and has_lora:
                kp = self._approved_keyframe(shot)
                if kp is not None:
                    ref_list = [str(kp)]
            if ref_list:
                sim = self.face_similarity(video_path, ref_list)
                result["face"] = sim
                if sim is not None:
                    tier = chars[0].get("tier", "main")
                    thr = qa.get("face_sim", {}).get(tier, 0.55)
                    result["face_fail"] = sim < thr

        clip = self.clip_alignment(video_path, prompt)
        result["clip"] = clip
        if clip is not None:
            result["clip_fail"] = clip < qa.get("clip_score_min", 0.28)

        detail: list[str] = []
        if result["motion_fail"]:
            detail.append(f"motion {motion:.4f} out of range"
                          + (" (frozen)" if result["frozen"] else ""))
        elif result["frozen"]:
            detail.append(f"video frozen (motion {motion:.4f})")
        if result["face_fail"]:
            detail.append(f"face sim {result['face']:.3f} below threshold")
        if result["clip_fail"]:
            detail.append(f"clip {clip:.3f} below minimum")
        result["detail"] = "; ".join(detail)
        # "frozen" is advisory: the PASS/FAIL motion bound is the calibrated
        # qa.motion_score_range (its low end admits deliberately static
        # dialogue / establishing shots that sit under _FROZEN_MOTION)
        result["passed"] = not (result["face_fail"]
                                or result["motion_fail"]
                                or result["clip_fail"])
        return result

    # ------------------------------------------------------- retry ladder
    def next_retry_action(self, shot: dict, qa_result: dict) -> dict:
        """Retry ladder, driven by shot.qa_stage."""
        stage = int(shot.get("qa_stage") or 0)
        if stage <= 0:
            return {"action": "reseed", "description": "retry with next seed"}
        if stage == 1:
            primary = " ".join(str(shot.get("primary_action") or "")
                               .split()[:8])
            hint = f"primary action: {primary}".strip()
            return {"action": "simplify_prompt",
                    "description": "strip secondary motion, shorten action",
                    "prompt_hint": hint}
        if stage == 2:
            return {"action": "adjust_lora",
                    "description": "halve effect/camera LoRA strengths"}
        if stage == 3:
            return {"action": "regen_keyframe",
                    "description": "regenerate the start keyframe"}
        return {"action": "review",
                "description": "send to human review"}
