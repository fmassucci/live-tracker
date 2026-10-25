"""Falls: someone lying on the floor, from the points of their body (YOLOv8s-pose, on the processor).

A person counts as lying when their torso leans 50 degrees or more from upright, or, when their shoulders and hips
can't be seen, when they are clearly wider than tall. A fall is recorded once they have stayed like that for the
camera's minimum time, so someone bending down to pick something up isn't one. On 189 labelled photos of falls and of
people standing or sitting, this found 82% of the falls and took nobody upright for one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np

from portaria.objects import Detection, ObjectSettings
from portaria.yolo import LEFT_HIP, LEFT_SHOULDER, RIGHT_HIP, RIGHT_SHOULDER, Found, YoloModel

FALL = "fall"
NAMES = {FALL: "Fall"}
PERSON = "person"  # the people standing, sitting or walking: marked on the live picture, never recorded

LYING_ANGLE = 50.0  # degrees between the torso and upright
WIDER_THAN_TALL = 1.2  # without shoulders and hips: width over height of the person's box
SURE = 0.5  # how sure the model must be of a body point to use it


@dataclass(frozen=True)
class FallSettings(ObjectSettings):
    KNOWN: ClassVar[dict[str, str]] = NAMES

    kinds: tuple[str, ...] = (FALL,)
    min_seconds: float = 10.0  # on the floor this long: someone who fell and isn't getting up
    fps: float = 2.0


def torso_angle(points: np.ndarray) -> float | None:
    """Degrees between the torso (hips to shoulders) and upright: 0 standing, 90 lying. None without the points."""
    shoulders = [points[i, :2] for i in (LEFT_SHOULDER, RIGHT_SHOULDER) if points[i, 2] >= SURE]
    hips = [points[i, :2] for i in (LEFT_HIP, RIGHT_HIP) if points[i, 2] >= SURE]
    if not shoulders or not hips:
        return None
    dx, dy = np.mean(shoulders, axis=0) - np.mean(hips, axis=0)
    return math.degrees(math.atan2(abs(dx), -dy))  # the picture's y grows downward


def lying(person: Found) -> bool:
    angle = torso_angle(person.points) if person.points is not None else None
    if angle is not None:
        return angle >= LYING_ANGLE
    width, height = person.box[2:4]
    return width >= WIDER_THAN_TALL * height


class FallDetector:
    """Every person the pose model finds: "fall" when lying, otherwise "person"."""

    def __init__(self, path: Path) -> None:
        self._model = YoloModel(path)

    def detect(self, image: np.ndarray, min_score: float) -> list[Detection]:
        return [Detection(FALL if lying(found) else PERSON, found.score, found.box)
                for found in self._model.find(image, min_score) if found.kind == PERSON]
