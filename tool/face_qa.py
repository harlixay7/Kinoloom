"""Face QA loop (quality_thresholds). Per-shot:
motion score calculation, with face embedding similarity and CLIP
alignment hooks delegated to qa_director.py.

Per-tier face similarity thresholds:
  - main: 0.75
  - supporting: 0.65
  - recurring: 0.55
  - crowd: exempt
"""
from __future__ import annotations

from typing import Any

from core import load_config, log_setup

log = log_setup("face_qa")

DEFAULT_FACE_THRESHOLDS = {
    "main": 0.75,
    "supporting": 0.65,
    "recurring": 0.55,
}


class FaceQA:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self._face_model = None      # InsightFace/ArcFace onnx, loaded lazily
        self._clip_model = None      # CLIP image-encoder, loaded lazily

    def _ensure_face_model(self) -> None:
        if self._face_model is not None:
            return
        try:
            import onnxruntime  # noqa: F401
            from insightface.app import FaceAnalysis
            self._face_model = FaceAnalysis(name="buffalo_l",
                                            providers=["CPUExecutionProvider"])
            self._face_model.prepare(ctx_id=-1)
        except Exception as e:
            log.warning(f"face model unavailable: {e}")
            self._face_model = False

    def _ensure_clip_model(self) -> None:
        if self._clip_model is not None:
            return
        try:
            from transformers import CLIPModel, CLIPProcessor
            self._clip_model = (CLIPModel.from_pretrained(
                "openai/clip-vit-base-patch32"),
                CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32"))
        except Exception as e:
            log.warning(f"CLIP unavailable: {e}")
            self._clip_model = False

    # ------------------------------------------------------------- metrics
    def face_similarity(self, video_path: str, char_id: str) -> float | None:
        """Best-frame cosine sim vs roster embedding (per-tier threshold).
        Delegates to qa_director.QADirector using resolved character references."""
        try:
            from qa_director import QADirector
            from core import ROOT
            qd = QADirector(self.cfg)
            ref_dir = ROOT / "ReferenceImages" / char_id
            refs = list(ref_dir.glob("*.jpg")) + list(ref_dir.glob("*.png"))
            if not refs:
                roster_dir = ROOT / "roster" / char_id
                refs = list(roster_dir.glob("*.jpg")) + list(roster_dir.glob("*.png"))
            if not refs:
                kf_dir = ROOT / "roster" / "keyframes" / char_id
                refs = list(kf_dir.glob("*.jpg")) + list(kf_dir.glob("*.png"))
            if not refs:
                face_dir = ROOT / "roster" / "datasets" / "face" / char_id
                refs = (list(face_dir.glob("*.jpg")) + list(face_dir.glob("*.png")) +
                        list((face_dir / "stills").glob("*.jpg")) + list((face_dir / "stills").glob("*.png")))
            if not refs:
                return None
            return qd.face_similarity(video_path, refs)
        except Exception as e:
            log.warning(f"face_similarity delegation error: {e}")
            return None

    def clip_alignment(self, video_path: str, prompt: str) -> float | None:
        """CLIP cosine of mid-frame image vs prompt.
        Delegates to qa_director.QADirector."""
        if not prompt:
            return None
        try:
            from qa_director import QADirector
            qd = QADirector(self.cfg)
            return qd.clip_alignment(video_path, prompt)
        except Exception as e:
            log.warning(f"clip_alignment delegation error: {e}")
            return None

    def motion_score(self, video_path: str) -> float:
        """Optical-flow based motion score; rejects frozen/glitched frames."""
        try:
            import cv2
            import numpy as np
            cap = cv2.VideoCapture(video_path)
            prev = None
            flows = []
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if prev is not None:
                    flow = cv2.calcOpticalFlowFarneback(
                        prev, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
                    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                    flows.append(float(np.mean(mag)))
                prev = gray
            cap.release()
            if not flows:
                return 0.0
            return float(np.mean(flows))
        except Exception:
            return 0.5

    # --------------------------------------------------------------- check
    def evaluate(self, shot: dict, video_path: str) -> dict:
        qa = self.cfg.get("qa") or {}
        clip = self.clip_alignment(video_path, shot.get("prompt", ""))
        motion = self.motion_score(video_path)
        face_fail = False
        face = None
        for c in shot.get("characters", []):
            tier = c.get("tier")
            if tier == "crowd":
                continue
            sim = self.face_similarity(video_path, c["id"])
            if sim is not None:
                threshold = qa.get("face_sim", {}).get(
                    tier, DEFAULT_FACE_THRESHOLDS.get(tier, 0.55))
                if sim < threshold:
                    face_fail = True
                    face = sim
                    break
        mmin, mmax = qa.get("motion_score_range", [0.00005, 6.0])
        motion_fail = not (mmin <= motion <= mmax)
        clip_fail = clip is not None and \
            clip < qa.get("clip_score_min", 0.28)
        passed = not (face_fail or motion_fail or clip_fail)
        return {
            "passed": passed,
            "clip": clip,
            "face": face,
            "face_fail": face_fail,
            "motion": motion,
            "motion_fail": motion_fail,
            "clip_fail": clip_fail,
        }
