"""Faces marked on frames, for the live view and the event snapshots: corner brackets and a name tag."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import cv2
import numpy as np

from portaria.faces import Face

# BGR. Green: recognized. Red: not recognized. Yellow: face not usable yet. Grey: other faces.
COLORS = {"ok": (94, 197, 34), "bad": (68, 68, 239), "warn": (21, 204, 250), "dim": (170, 170, 170)}
LABEL_TEXT = {"ok": (14, 12, 11), "bad": (255, 255, 255), "warn": (14, 12, 11), "dim": (14, 12, 11)}
FONT = cv2.FONT_HERSHEY_DUPLEX


@dataclass(frozen=True)
class Overlay:
    face: Face
    label: str
    tone: str  # a key of COLORS


def render(image: np.ndarray, overlays: list[Overlay], status: list[str], width: int = 960) -> np.ndarray:
    scale = min(1.0, width / image.shape[1])
    if scale < 1.0:
        canvas = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        canvas = image.copy()
    for overlay in overlays:
        x, y, w, h = (int(v) for v in (overlay.face.box * scale).round())
        _brackets(canvas, x, y, w, h, COLORS[overlay.tone])
        if overlay.label:
            _tag(canvas, overlay.label, x, y, overlay.tone)
    for i, line in enumerate(status):
        _tag(canvas, line, 12, 40 + 34 * i, "dim")
    return canvas


def _brackets(canvas: np.ndarray, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
    """Four corners instead of a full box: marks the face without hiding it."""
    length = max(8, int(min(w, h) * 0.24))
    for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        cv2.line(canvas, (cx, cy), (cx + dx * length, cy), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (cx, cy), (cx, cy + dy * length), color, 3, cv2.LINE_AA)


def _tag(canvas: np.ndarray, text: str, x: int, y: int, tone: str) -> None:
    """A rounded tag above (x, y), or just inside the frame when there is no room above."""
    # OpenCV's fonts are ASCII-only, so accented letters are drawn without their accents.
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    (text_w, text_h), _ = cv2.getTextSize(text, FONT, 0.55, 1)
    height = text_h + 14
    radius = height // 2
    top = y - height - 8 if y - height - 8 >= 0 else y + 8
    left = max(0, min(x, canvas.shape[1] - text_w - 2 * radius))
    color = COLORS[tone]
    cv2.rectangle(canvas, (left + radius, top), (left + radius + text_w, top + height), color, -1, cv2.LINE_AA)
    cv2.circle(canvas, (left + radius, top + radius), radius, color, -1, cv2.LINE_AA)
    cv2.circle(canvas, (left + radius + text_w, top + radius), radius, color, -1, cv2.LINE_AA)
    cv2.putText(canvas, text, (left + radius, top + height - 7), FONT, 0.55, LABEL_TEXT[tone], 1, cv2.LINE_AA)
