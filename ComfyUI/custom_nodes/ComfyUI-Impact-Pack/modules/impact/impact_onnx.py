# From ComfyUI-Impact-Pack by Dr.Lt.Data
# (https://github.com/ltdrdata/ComfyUI-Impact-Pack, commit 429d015, version 8.28.3).
# GPL-3.0: see ../../LICENSE.txt.
# Modified for Kinoloom (2026-08, 2026-10): onnx_inference also reads a standard
# Ultralytics ONNX export (fixed NCHW input, /255 scaling, one [1, 4+classes, N]
# output, or the end-to-end [1, N, 6] output) with a score pick and a light NMS,
# and scales the boxes back to the image size. The classic three-output format
# works as before. Used by the face fix and by tool/impact_onnx_face.py.
import impact.additional_dependencies
import numpy as np
from impact import utils
import logging

impact.additional_dependencies.ensure_onnx_package()

try:
    import onnxruntime

    def onnx_inference(image, onnx_model):
        # prepare image
        import cv2
        pil = utils.tensor2pil(image)
        image = np.ascontiguousarray(pil)
        orig_h, orig_w = image.shape[:2]
        image = image[:, :, ::-1]  # to BGR image
        image = image.astype(np.float32)

        # do detection
        onnx_model = onnxruntime.InferenceSession(onnx_model, providers=["CPUExecutionProvider"])
        inp = onnx_model.get_inputs()[0]
        in_shape = inp.shape  # may be dynamic
        target = (orig_w, orig_h)   # a dynamic input runs at the image size
        # compat: our face_yolov8m.onnx expects NCHW fixed 640 input
        if len(in_shape) == 4 and isinstance(in_shape[2], int):
            target = (int(in_shape[2]), int(in_shape[3]))
            if (orig_w, orig_h) != target:
                image = cv2.resize(image, target)
            image = image / 255.0        # ultralytics normalization
            image = image.transpose(2, 0, 1)  # HWC -> CHW
        outputs = onnx_model.run(
            [s_i.name for s_i in onnx_model.get_outputs()],
            {inp.name: np.expand_dims(image, axis=0)},
        )

        # compat: ultralytics standard export outputs [1, C, N] with
        # C = 4 (xywh) + n_classes. Do score pick + lightweight NMS here and
        # emit the labels/scores/boxes arrays this module expects.
        if len(outputs) == 1 and outputs[0].ndim == 3 and \
                outputs[0].shape[2] > 20:
            pred = outputs[0][0]  # [C, N]
            n_anchors = pred.shape[1]
            boxes_xywh = pred[:4].T  # [N, 4]
            cls = pred[4:]           # [n_cls, N]
            scores = cls.max(axis=0)
            cls_ids = cls.argmax(axis=0)
            keep = scores > 0.05
            if not keep.any():
                boxes = np.zeros((1, 1, 4), dtype=np.float32)
                scores = np.zeros((1, 1), dtype=np.float32)
                labels = np.full((1, 1), -1, dtype=np.int32)
            else:
                x = boxes_xywh[keep, 0]
                y = boxes_xywh[keep, 1]
                w = boxes_xywh[keep, 2]
                h = boxes_xywh[keep, 3]
                x1 = x - w / 2; y1 = y - h / 2
                x2 = x + w / 2; y2 = y + h / 2
                sc = scores[keep]
                ci = cls_ids[keep].astype(np.int32)
                order = np.argsort(-sc)
                pick = []
                for i in order:
                    if len(pick) == 0:
                        pick.append(i); continue
                    overlap = False
                    for j in pick:
                        xx1 = max(x1[i], x1[j]); yy1 = max(y1[i], y1[j])
                        xx2 = min(x2[i], x2[j]); yy2 = min(y2[i], y2[j])
                        iw = max(0, xx2 - xx1); ih = max(0, yy2 - yy1)
                        inter = iw * ih
                        uni = ((x2[i]-x1[i])*(y2[i]-y1[i]) +
                               (x2[j]-x1[j])*(y2[j]-y1[j]) - inter)
                        if uni > 0 and inter / uni > 0.5:
                            overlap = True; break
                    if not overlap:
                        pick.append(i)
                pick = np.array(sorted(pick))
                n = len(pick)
                boxes = np.concatenate([
                    x1[pick][:, None], y1[pick][:, None],
                    x2[pick][:, None], y2[pick][:, None]], axis=1)[None]
                scores = sc[pick][None]
                labels = np.concatenate(
                    [ci[pick], np.array([-1], dtype=np.int32)])[None]
        elif len(outputs) == 1 and outputs[0].ndim == 3:
            # ultralytics end2end (nms=True): [1, N, 6] zero-padded
            det = outputs[0][0]
            n = det.shape[0]
            valid = det[:, 4] > 0.0
            n_valid = int(valid.sum())
            boxes = np.zeros((1, n, 4), dtype=np.float32)
            scores = np.zeros((1, n), dtype=np.float32)
            labels = np.full((1, n), -1, dtype=np.int32)
            if n_valid:
                boxes[0, :n_valid] = det[:n_valid, :4]
                scores[0, :n_valid] = det[:n_valid, 4]
                labels[0, :n_valid] = det[:n_valid, 5].astype(np.int32)
        else:
            # classic Impact-Pack format: caffe preprocessing + 3 outputs
            image = image - [103.939, 116.779, 123.68]
            outputs = onnx_model.run(
                [s_i.name for s_i in onnx_model.get_outputs()],
                {inp.name: np.expand_dims(image, axis=0)},
            )
            labels = [op for op in outputs if op.dtype == "int32"][0]
            scores = [op for op in outputs if isinstance(op[0][0], np.float32)][0]
            boxes = [op for op in outputs if isinstance(op[0][0], np.ndarray)][0]

        # filter-out useless item
        idx = np.where(labels[0] == -1)[0][0]

        labels = labels[0][:idx]
        scores = scores[0][:idx]
        boxes = boxes[0][:idx].astype(np.float32)
        # rescale boxes from model input space back to the native image
        if (orig_w, orig_h) != target:
            sx = orig_w / float(target[0])
            sy = orig_h / float(target[1])
            boxes = boxes * np.array([sx, sy, sx, sy])
        boxes = boxes.astype(np.uint32)

        return labels, scores, boxes
except Exception:
    logging.error("[Impact Pack] ComfyUI-Impact-Pack: 'onnxruntime' package doesn't support 'python 3.11', yet.\t{e}")
