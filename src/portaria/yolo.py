"""Ultralytics YOLO models exported to ONNX: the things they find in a picture, each with its kind, score and box,
and for pose models the 17 body points of each person.

The export keeps the model's own description in the file (its kinds, input size and output layout), read here, so a
model of the same family can replace another by changing only the file.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# The 17 body points of pose models, in their order (COCO's).
NOSE, LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP = 0, 5, 6, 11, 12
LEFT_KNEE, RIGHT_KNEE, LEFT_ANKLE, RIGHT_ANKLE = 13, 14, 15, 16


@dataclass(frozen=True)
class Found:
    kind: str  # the model's name for it, such as "fire" or "person"
    score: float
    box: np.ndarray  # x, y, width, height in the picture's pixels
    points: np.ndarray | None = None  # pose models: 17 rows of x, y (the picture's pixels) and how sure, 0 to 1


class YoloModel:
    """One model on ONNX Runtime, on the processor."""

    THREADS = 4  # as objects.ObjectDetector

    def __init__(self, path: Path, overlap: float = 0.6) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.THREADS
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        meta = self._session.get_modelmeta().custom_metadata_map
        self.names = {int(key): name for key, name in ast.literal_eval(meta["names"]).items()}
        self._end2end = meta.get("end2end") == "True"  # YOLO26: at most 300 things, duplicates already removed
        self._points = ast.literal_eval(meta["kpt_shape"])[0] if meta.get("task") == "pose" else 0
        shape = self._session.get_inputs()[0].shape
        self.size = shape[2] if isinstance(shape[2], int) else ast.literal_eval(meta["imgsz"])[0]
        self._input = self._session.get_inputs()[0].name
        self._overlap = overlap  # boxes of one kind overlapping more than this are the same thing

    def find(self, image: np.ndarray, min_score: float) -> list[Found]:
        blob, ratio, pad = letterbox(image, self.size)
        raw = self._session.run(None, {self._input: blob})[0][0]
        if self._end2end:
            return decode_end2end(raw, self.names, self._points, ratio, pad, min_score)
        return decode(raw, self.names, self._points, ratio, pad, min_score, self._overlap)


def letterbox(image: np.ndarray, size: int) -> tuple[np.ndarray, float, np.ndarray]:
    """What Ultralytics models are trained on: RGB from 0 to 1, scaled to fit and centered, the rest grey (114).
    Also returns the scale and the padding, to bring boxes back to the picture."""
    ratio = min(size / image.shape[0], size / image.shape[1])
    width, height = round(image.shape[1] * ratio), round(image.shape[0] * ratio)
    left, top = (size - width) // 2, (size - height) // 2
    canvas = np.full((size, size, 3), 114, np.uint8)
    canvas[top:top + height, left:left + width] = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
    blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[np.newaxis].astype(np.float32) / 255
    return blob, ratio, np.array([left, top], np.float32)


def decode(raw: np.ndarray, names: dict[int, str], points: int, ratio: float, pad: np.ndarray, min_score: float,
           overlap: float) -> list[Found]:
    """YOLOv8 and YOLO11: one column per prediction, x, y, width and height of its center box, then a score for each
    kind (pose models: one kind, then each body point's x, y and how sure). Duplicates are removed here."""
    rows = raw.T
    kinds = len(names)
    scores = rows[:, 4:4 + kinds]
    best, kind = scores.max(axis=1), scores.argmax(axis=1)
    keep = best >= min_score
    if not keep.any():
        return []
    rows, best, kind = rows[keep], best[keep], kind[keep]
    boxes = np.concatenate([rows[:, :2] - rows[:, 2:4] / 2, rows[:, 2:4]], axis=1)
    chosen = np.array(cv2.dnn.NMSBoxesBatched(boxes.tolist(), best.tolist(), kind.tolist(), min_score, overlap),
                      dtype=int).reshape(-1)
    found = []
    for i in chosen:
        body = rows[i, 4 + kinds:].reshape(points, 3) if points else None
        found.append(_found(names[int(kind[i])], best[i], boxes[i], body, ratio, pad))
    return found


def decode_end2end(raw: np.ndarray, names: dict[int, str], points: int, ratio: float, pad: np.ndarray,
                   min_score: float) -> list[Found]:
    """YOLO26: one row per thing, corners x1, y1, x2, y2, its score and kind (pose models: then the body points)."""
    found = []
    for row in raw[raw[:, 4] >= min_score]:
        box = np.array([row[0], row[1], row[2] - row[0], row[3] - row[1]])
        body = row[6:].reshape(points, 3) if points else None
        found.append(_found(names[int(row[5])], row[4], box, body, ratio, pad))
    return found


def _found(kind: str, score: float, box: np.ndarray, body: np.ndarray | None, ratio: float, pad: np.ndarray) -> Found:
    """From the model's square picture back to the camera's."""
    box = np.concatenate([(box[:2] - pad) / ratio, box[2:4] / ratio]).astype(np.float64)
    if body is not None:
        body = body.astype(np.float64)
        body[:, :2] = (body[:, :2] - pad) / ratio
    return Found(kind, float(score), box, body)
