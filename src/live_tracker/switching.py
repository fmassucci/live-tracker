"""What a camera does when it recognizes someone with access: it switches on the device output linked to it.

The system doesn't know what the output drives; the person who set it up does. `[devices] mode` in the config is
the master switch: "shadow" only logs, so the thresholds can be checked on real people before anything is switched;
"live" switches the output on for its pulse time, and the device switches it off by itself.
"""

from __future__ import annotations

import logging
from typing import Protocol

from live_tracker.devices import DeviceError
from live_tracker.devices.manager import DeviceManager

log = logging.getLogger(__name__)

MODES = ("shadow", "live")


class Switch(Protocol):
    def switch(self, person: str, output_id: int) -> str:
        """Switches the camera's output for `person`. Returns the record's action: "shadow", "switched" or
        "failed"."""
        ...


class ShadowSwitch:
    """Logs what would happen, so the thresholds can be checked on real people before anything is switched."""

    def switch(self, person: str, output_id: int) -> str:
        log.info("SHADOW MODE: would switch output %d for %s", output_id, person)
        return "shadow"


class LiveSwitch:
    """Switches the output on; the device switches it off by itself after the output's pulse time."""

    def __init__(self, devices: DeviceManager) -> None:
        self._devices = devices

    def switch(self, person: str, output_id: int) -> str:
        try:
            device = self._devices.pulse_output(output_id)
        except DeviceError as e:
            log.warning("output %d wasn't switched for %s: %s", output_id, person, e)
            return "failed"
        except Exception:  # a library error must not stop the camera
            log.exception("output %d wasn't switched for %s", output_id, person)
            return "failed"
        log.info("%s switched for %s", device, person)
        return "switched"


def make_switch(mode: str, devices: DeviceManager) -> Switch:
    if mode == "shadow":
        return ShadowSwitch()
    if mode == "live":
        return LiveSwitch(devices)
    raise ValueError(f"unknown devices mode '{mode}' (one of {', '.join(MODES)})")
