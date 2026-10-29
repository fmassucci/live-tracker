"""Devices: the boards and modules that switch things (relays) and report sensors, behind one interface.

- esp32.py: boards with our firmware, over MQTT (firmware/README.md). They announce themselves.
- tuya.py: ready-made Wi-Fi modules made by Tuya (NovaDigital and others), on the local network with their local
  key, imported from the Tuya developer account.
- manager.py: runs in the recognition service, keeps the connections, carries out the commands the web UI adds,
  and reports every device's state.

Safety, for every kind (firmware/README.md): an output only switches on with an end. The device itself switches it
off after the pulse, even when the network or this computer is down, and a command that can't go out right away is
dropped, never sent late.
"""

from __future__ import annotations

from dataclasses import dataclass

# What goes wrong with a command or a device, as codes the web UI turns into words (web/devices.py).
OFFLINE = "offline"  # the device isn't connected
NO_ANSWER = "no-answer"  # it didn't confirm in time
NO_TIMER = "no-timer"  # the output has no timer of its own on the device, so it is never switched on
NO_OUTPUT = "no-output"  # the device has no such output
REFUSED = "refused"  # the board said no
BROKER = "broker"  # the MQTT broker isn't connected
NOT_FOUND = "not-found"  # a Tuya module that isn't on the network, or not on this one
BAD_KEY = "key"  # a Tuya module that refuses its local key: it was paired again


class DeviceError(Exception):
    """A command that didn't happen; `code` is one of the codes above."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class OutputSpec:
    key: str
    kind: str = "relay"
    pulse_ms: int = 3000
    max_ms: int = 10000
    timer_dp: int | None = None  # Tuya: the countdown that switches it off on the module
    id: int | None = None  # in the database (DeviceOutput.id), which a camera linked to it names it by


@dataclass(frozen=True)
class DeviceSpec:
    """A device as the database has it, which its driver works from."""

    id: int
    name: str
    kind: str  # "esp32" or "tuya"
    uid: str
    address: str = ""
    local_key: str = ""
    outputs: tuple[OutputSpec, ...] = ()

    def output(self, key: str) -> OutputSpec:
        for output in self.outputs:
            if output.key == key:
                return output
        raise DeviceError(NO_OUTPUT, key)
