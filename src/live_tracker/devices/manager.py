"""The devices inside the recognition service: their connections, the commands the web UI adds, and their state.

The web UI never talks to a device: a Tuya module accepts one connection, and the devices must not depend on the web
UI. It adds a row to device_commands; this picks it up within a fraction of a second, carries it out and fills in
the result. A command older than LATE_S is dropped, never sent late. New ESP32 boards that announce themselves
are added to the database, to be named on the Devices page.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from live_tracker.config import Config
from live_tracker.db import DEVICES_VERSION, Database, Device, DeviceCommand, DeviceInput, DeviceOutput, bump
from live_tracker.devices import BROKER, NO_OUTPUT, OFFLINE, DeviceError, DeviceSpec, OutputSpec
from live_tracker.devices.esp32 import Esp32Boards
from live_tracker.devices.tuya import TuyaDiscovery, TuyaModule

log = logging.getLogger(__name__)

LATE_S = 5.0  # a command not picked up by then is dropped: an output must not switch seconds after the button
COMMANDS_EVERY_S = 0.2
OUTPUT_KINDS = ("lock", "light", "siren", "relay")
INPUT_KINDS = ("button", "contact")


def device_spec(device: Device) -> DeviceSpec:
    outputs = tuple(OutputSpec(o.key, o.kind, o.pulse_ms, o.max_ms, o.timer_dp, o.id) for o in device.outputs)
    return DeviceSpec(device.id, device.name, device.kind, device.uid, device.address, device.local_key, outputs)


class DeviceManager:
    def __init__(self, cfg: Config, db: Database,
                 tuya_module: Callable[..., TuyaModule] = TuyaModule,
                 boards: Callable[..., Esp32Boards] = Esp32Boards,
                 discovery: Callable[[], TuyaDiscovery] = TuyaDiscovery) -> None:
        self._cfg = cfg
        self._db = db
        self._tuya_module = tuya_module
        self._make_boards = boards
        self._make_discovery = discovery
        self._discovery: TuyaDiscovery | None = None
        self._boards: Esp32Boards | None = None
        self._specs: dict[int, DeviceSpec] = {}
        self._modules: dict[int, TuyaModule] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_commands, name="device-commands", daemon=True)

    def start(self) -> None:
        if self._cfg.mqtt.host:
            self._boards = self._make_boards(self._cfg.mqtt, self._board_info)
            self._boards.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        for module in self._modules.values():
            module.stop()
        self._modules.clear()
        if self._boards:
            self._boards.stop()
        if self._discovery:
            self._discovery.stop()

    def load(self, session: Session) -> None:
        """Takes up the devices as the database has them now: new modules connect, removed ones disconnect."""
        rows = session.scalars(select(Device).options(selectinload(Device.outputs))).all()
        specs = {d.id: device_spec(d) for d in rows}
        with self._lock:
            self._specs = specs
            for device_id, module in list(self._modules.items()):
                if specs.get(device_id) != module.spec:
                    module.stop()
                    del self._modules[device_id]
            for device_id, spec in specs.items():
                if spec.kind == "tuya" and device_id not in self._modules:
                    if self._discovery is None:
                        self._discovery = self._make_discovery()
                        self._discovery.start()
                    self._modules[device_id] = self._tuya_module(spec, self._discovery.address)
                    self._modules[device_id].start()
        log.info("devices loaded: %d", len(specs))

    def status(self) -> dict:
        """Every device's state, for the web UI, by device ID."""
        with self._lock:
            specs, modules = dict(self._specs), dict(self._modules)
        devices = {}
        for device_id, spec in specs.items():
            if spec.kind == "tuya" and device_id in modules:
                devices[str(device_id)] = modules[device_id].status()
            elif spec.kind == "esp32":
                devices[str(device_id)] = self._boards.status(spec.uid) if self._boards else {"online": False,
                                                                                              "problem": BROKER}
        broker = "off" if not self._cfg.mqtt.host else "connected" if self._boards and self._boards.connected else "down"
        return {"broker": broker, "devices": devices}

    def pulse(self, device_id: int, key: str, ms: int) -> None:
        """Switches an output on for `ms`; the device switches it off by itself. Raises DeviceError."""
        with self._lock:
            spec, module = self._specs.get(device_id), self._modules.get(device_id)
        if spec is None:
            raise DeviceError(OFFLINE, f"no device {device_id}")
        if spec.kind == "tuya" and module:
            module.pulse(key, ms)
        elif spec.kind == "esp32" and self._boards:
            self._boards.pulse(spec, key, ms)
        else:
            raise DeviceError(OFFLINE)

    def pulse_output(self, output_id: int) -> str:
        """Switches an output on for its own pulse time, as a camera linked to it does. Returns the device's name;
        raises DeviceError."""
        with self._lock:
            found = [(spec, output) for spec in self._specs.values() for output in spec.outputs
                     if output.id == output_id]
        if not found:
            raise DeviceError(NO_OUTPUT, f"output {output_id}")
        spec, output = found[0]
        self.pulse(spec.id, output.key, output.pulse_ms)
        return spec.name

    def _run_commands(self) -> None:
        while not self._stop.wait(COMMANDS_EVERY_S):
            try:
                for command in self._take_commands():
                    threading.Thread(target=self._carry_out, args=command, name="device-command", daemon=True).start()
            except Exception:  # the database may be busy for a moment; the next round tries again
                log.exception("device commands")

    def _take_commands(self) -> list[tuple[int, int, str, int]]:
        """The commands waiting, marked as being carried out; those waiting too long are dropped."""
        now = datetime.now()
        taken = []
        with self._db.session() as session:
            for command in session.scalars(select(DeviceCommand).where(DeviceCommand.result == "")):
                if now - command.requested_at > timedelta(seconds=LATE_S):
                    command.result, command.done_at = "late", now
                    log.warning("dropped a command for device %d, %s: %s late", command.device_id, command.output_key,
                                now - command.requested_at)
                    continue
                command.result = "sent"
                taken.append((command.id, command.device_id, command.output_key, command.ms))
        return taken

    def _carry_out(self, command_id: int, device_id: int, key: str, ms: int) -> None:
        result, error = "ok", ""
        try:
            self.pulse(device_id, key, ms)
        except DeviceError as e:
            result, error = "failed", e.code
            log.warning("device %d, %s: the command failed: %s", device_id, key, e)
        except Exception as e:  # a library error must still end the command
            result, error = "failed", "no-answer"
            log.exception("device %d, %s: the command failed", device_id, key)
        with self._db.session() as session:
            command = session.get(DeviceCommand, command_id)
            if command:
                command.result, command.error, command.done_at = result, error[:200], datetime.now()

    def _board_info(self, uid: str, info: dict) -> None:
        """A board said what it has: a new board is added, to be named; a known one gets its outputs and inputs as
        the board has them now, keeping their names."""
        try:
            with self._db.session() as session:
                if record_board(session, uid, info):
                    bump(session, DEVICES_VERSION)
        except Exception:  # never stop the MQTT thread
            log.exception("couldn't record the board %s", uid)


def record_board(session: Session, uid: str, info: dict) -> bool:
    """Adds or updates a board from its `info` message. True when something changed."""
    device = session.scalar(select(Device).where(Device.uid == uid))
    changed = device is None
    if device is None:
        device = Device(kind="esp32", uid=uid, name=str(info.get("name") or uid)[:80], new=True)
        session.add(device)
        log.info("new ESP32 board: %s", uid)
    firmware = str(info.get("firmware", ""))[:80]
    if device.model != firmware:
        device.model, changed = firmware, True
    outputs = {str(o.get("key")): o for o in info.get("outputs") or [] if isinstance(o, dict) and o.get("key")}
    inputs = {str(i.get("key")): i for i in info.get("inputs") or [] if isinstance(i, dict) and i.get("key")}
    for output in list(device.outputs):
        if output.key not in outputs:
            device.outputs.remove(output)
            changed = True
    for output in list(device.inputs):
        if output.key not in inputs:
            device.inputs.remove(output)
            changed = True
    known = {o.key: o for o in device.outputs}
    for key, o in outputs.items():
        max_ms = int(o.get("max_ms") or 10000)
        kind = o.get("kind") if o.get("kind") in OUTPUT_KINDS else "relay"
        if key not in known:
            device.outputs.append(DeviceOutput(key=key[:20], name=key[:80], kind=kind, max_ms=max_ms,
                                               pulse_ms=min(3000, max_ms)))
            changed = True
        elif known[key].max_ms != max_ms:
            known[key].max_ms, known[key].pulse_ms = max_ms, min(known[key].pulse_ms, max_ms)
            changed = True
    known_inputs = {i.key for i in device.inputs}
    for key, i in inputs.items():
        if key not in known_inputs:
            kind = i.get("kind") if i.get("kind") in INPUT_KINDS else "button"
            device.inputs.append(DeviceInput(key=key[:20], name=key[:80], kind=kind))
            changed = True
    return changed
