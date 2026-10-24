"""Face detection helper for dataset filtering — wraps the patched
impact_onnx detector (same compat shim as face-fix)."""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ComfyUI"))
sys.path.insert(0, str(ROOT / "ComfyUI" / "custom_nodes" / "ComfyUI-Impact-Pack" / "modules"))

import cv2
import torch

_log = logging.getLogger("impact_onnx_face")
_logged_failure = False


def detect_faces(image_path: str | Path, threshold: float = 0.15) -> int:
    global _logged_failure
    try:
        from impact import impact_onnx
        img = cv2.imread(str(image_path))
        if img is None:
            return 0
        rgb = img[:, :, ::-1].copy()
        t = torch.from_numpy(rgb).float().div_(255.0).unsqueeze(0)
        model_path = ROOT / "ComfyUI" / "models" / "onnx" / "bbox" / "face_yolov8m.onnx"
        labels, scores, _ = impact_onnx.onnx_inference(t, str(model_path))
        return int(sum(1 for s in scores if s > threshold))
    except Exception:
        if not _logged_failure:
            _logged_failure = True
            _log.exception("face detection failed — returning 0 "
                           "(detector unavailable)")
        return 0
