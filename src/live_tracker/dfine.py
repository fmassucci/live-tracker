"""D-FINE-M: a more accurate model for people and objects, COCO's same 80 kinds, Apache-2.0, as exported to ONNX by
onnx-community. A camera's People and objects settings choose it instead of YOLOX-S.

Its scores run higher than YOLOX-S's, so each sensitivity level has its own threshold here, set on 250 COCO photos
for as many false boxes as YOLOX-S makes at that level. At Balanced it then found 68% of the people where YOLOX-S
found 58%, and 57% of all the things where YOLOX-S found 41%; at about 0.3 s per picture on the processor instead
of 0.12.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from live_tracker.objects import KEYS, Detection

# Each sensitivity level (objects.SENSITIVITY, in YOLOX-S's terms) and the score D-FINE-M needs for it.
LEVELS = ((0.35, 0.55), (0.5, 0.65), (0.65, 0.75))


class DFineDetector:
    """D-FINE-M on ONNX Runtime: a picture in, every thing found with its kind and score out, as YOLOX-S does."""

    SIZE = 640  # the picture is stretched to 640x640, as the model was trained
    THREADS = 4  # as objects.ObjectDetector

    def __init__(self, path: Path) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.THREADS
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._outputs = [output.name for output in self._session.get_outputs()]

    def detect(self, image: np.ndarray, min_score: float) -> list[Detection]:
        picture = cv2.resize(image, (self.SIZE, self.SIZE), interpolation=cv2.INTER_LINEAR)
        blob = cv2.cvtColor(picture, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[np.newaxis].astype(np.float32) / 255
        out = dict(zip(self._outputs, self._session.run(None, {"pixel_values": blob})))
        return decode(out["logits"][0], out["pred_boxes"][0], image.shape[1], image.shape[0], threshold(min_score))


def threshold(min_score: float) -> float:
    """The score D-FINE-M needs at a camera's sensitivity, which is stored in YOLOX-S's terms."""
    return float(np.interp(min_score, [level for level, _ in LEVELS], [score for _, score in LEVELS]))


def decode(logits: np.ndarray, boxes: np.ndarray, width: int, height: int, min_score: float) -> list[Detection]:
    """The model's 300 answers, each a box (center and size, as fractions of the picture) and a score per kind:
    every kind it is sure enough of, in the picture's pixels. Each answer is one thing, so nothing is duplicated."""
    scores = 1 / (1 + np.exp(-logits))
    found = []
    for query, kind in zip(*np.nonzero(scores >= min_score)):
        cx, cy, w, h = boxes[query] * (width, height, width, height)
        found.append(Detection(KEYS[kind], float(scores[query, kind]), np.array([cx - w / 2, cy - h / 2, w, h])))
    return found
