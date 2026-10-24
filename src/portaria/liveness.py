"""Liveness (anti-spoofing): is this a real face at the door, not a photo or a phone screen?"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from portaria.faces import Face


class LivenessCheck(Protocol):
    name: str

    def is_live(self, image: np.ndarray, face: Face) -> bool: ...


class NoLiveness:
    """Accepts everything. Placeholder until a real check exists.

    Planned: a passive check on the regular camera's image (MiniFASNet), then a depth or infrared
    check when that camera is available. Until then the door must stay in shadow mode.
    """

    name = "none"

    def is_live(self, image: np.ndarray, face: Face) -> bool:
        return True
