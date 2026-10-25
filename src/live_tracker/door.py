"""Door control. The ESP32 relay comes later; for now decisions are only logged."""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Door(Protocol):
    def open(self, person: str) -> None: ...


class ShadowDoor:
    """Logs what would happen, so the thresholds can be checked on real people before anything opens."""

    def open(self, person: str) -> None:
        log.info("SHADOW MODE: would open the door for %s", person)


def make_door(mode: str) -> Door:
    if mode == "shadow":
        return ShadowDoor()
    raise ValueError(f"unknown door mode '{mode}' (only 'shadow' exists for now)")
