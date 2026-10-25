"""Fire and smoke: a YOLO26n trained on photos of fire and smoke, on the processor.

Only in addition to the certified smoke detectors, never instead of them: a camera sees only what is in its picture,
and ovens, steam and flour dust can look like fire or smoke. A camera records them like people and objects
(objects.ObjectAnalyzer): once they have stayed in the picture for its minimum time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np

from live_tracker.objects import Detection, ObjectSettings
from live_tracker.yolo import YoloModel

# The kinds a camera can choose: the key stored in settings and records, and the English name, also the UI text.
THINGS = (("fire", "Fire"), ("smoke", "Smoke"))
NAMES = dict(THINGS)


@dataclass(frozen=True)
class FireSettings(ObjectSettings):
    KNOWN: ClassVar[dict[str, str]] = NAMES

    kinds: tuple[str, ...] = ("fire", "smoke")
    min_seconds: float = 3.0  # a flame passing by, or a puff of steam, isn't recorded
    fps: float = 2.0


class FireDetector:
    """The model's "fire" and "smoke", as detections objects.ObjectAnalyzer can use."""

    def __init__(self, path: Path) -> None:
        self._model = YoloModel(path)

    def detect(self, image: np.ndarray, min_score: float) -> list[Detection]:
        return [Detection(found.kind, found.score, found.box) for found in self._model.find(image, min_score)
                if found.kind in NAMES]
