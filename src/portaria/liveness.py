"""Liveness (anti-spoofing): is this a real face at the camera, not a photo or a phone screen?

Part of face recognition: once its model is downloaded (Settings, AI models), every recognized face is checked, and
a face judged a photo or a screen is recorded as such and never opens a door. It looks at the regular camera's
picture only, so a good video or a mask can still fool it; a depth or infrared check can come when a camera has one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from portaria.faces import Face
from portaria.models import file_state, model_path

log = logging.getLogger(__name__)

# The two models and how much of the face's surroundings each looks at, as a multiple of the face's size.
MODELS = (("minifasnet_v2", 2.7), ("minifasnet_v1se", 4.0))
REAL = 1  # the models' classes: 0 and 2 are kinds of spoof, 1 is a real face


class LivenessCheck(Protocol):
    name: str

    def is_live(self, image: np.ndarray, face: Face) -> bool: ...


class NoLiveness:
    """Accepts everything: no liveness model is downloaded. The door must then stay in shadow mode."""

    name = "none"

    def is_live(self, image: np.ndarray, face: Face) -> bool:
        return True


class MiniFASNet:
    """MiniFASNetV2 and V1SE, from Minivision's Silent-Face-Anti-Spoofing (Apache-2.0), as exported to ONNX by
    yakhyo/face-anti-spoofing. Each looks at an 80x80 picture of the face with some of its surroundings, where a
    photo's edges, a screen's glare or its pixels show; together they say how likely the face is real."""

    name = "minifasnet"
    SIZE = 80

    def __init__(self, models_dir: Path, threshold: float) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # two tiny models: a thread each is plenty
        self._models = [
            (ort.InferenceSession(str(model_path(models_dir, key)), options, providers=["CPUExecutionProvider"]), scale)
            for key, scale in MODELS
        ]
        self._threshold = threshold

    def score(self, image: np.ndarray, face: Face) -> float:
        """How likely the face is real, from 0 to 1: the average of both models."""
        total = 0.0
        for session, scale in self._models:
            blob = crop(image, face.box, scale, self.SIZE).astype(np.float32).transpose(2, 0, 1)[np.newaxis]
            logits = session.run(None, {session.get_inputs()[0].name: blob})[0][0]
            odds = np.exp(logits - logits.max())
            total += float(odds[REAL] / odds.sum())
        return total / len(self._models)

    def is_live(self, image: np.ndarray, face: Face) -> bool:
        return self.score(image, face) >= self._threshold


def crop(image: np.ndarray, box: np.ndarray, scale: float, size: int) -> np.ndarray:
    """The face's box enlarged `scale` times around its center, moved to stay inside the picture rather than cut,
    and resized to size x size: how the models were trained (BGR, 0 to 255)."""
    height, width = image.shape[:2]
    x, y, w, h = (float(v) for v in box)
    scale = min((height - 1) / h, (width - 1) / w, scale)
    new_w, new_h = w * scale, h * scale
    x1, y1 = x + w / 2 - new_w / 2, y + h / 2 - new_h / 2
    x2, y2 = x1 + new_w, y1 + new_h
    if x1 < 0:
        x1, x2 = 0, x2 - x1
    if y1 < 0:
        y1, y2 = 0, y2 - y1
    if x2 > width - 1:
        x1, x2 = x1 - (x2 - width + 1), width - 1
    if y2 > height - 1:
        y1, y2 = y1 - (y2 - height + 1), height - 1
    return cv2.resize(image[int(y1):int(y2) + 1, int(x1):int(x2) + 1], (size, size))


def load_liveness(models_dir: Path, threshold: float) -> LivenessCheck:
    """The liveness check when its model is downloaded and intact, otherwise none."""
    if any(file_state(models_dir, key) != "ready" for key, _ in MODELS):
        return NoLiveness()
    try:
        return MiniFASNet(models_dir, threshold)
    except Exception:  # a file the runtime can't read: face recognition goes on without it
        log.exception("the liveness model can't be loaded")
        return NoLiveness()
