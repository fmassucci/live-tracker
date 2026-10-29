"""Tuya-made Wi-Fi modules (NovaDigital and many other brands), on the local network with tinytuya.

Each module answers on TCP port 6668 to whoever has its local key, and accepts one connection at a time: the
recognition service keeps it, so Smart Life on a phone goes through Tuya's cloud meanwhile. The key comes from the
Tuya developer account the Smart Life account is linked to (`cloud_devices`), and changes whenever the module is
paired again.

A module reports numbered values (data points): a switch per channel, and its countdown, which switches the channel
back off on the module itself. A pulse switches the channel on, then sets its countdown, each confirmed; without the
countdown's confirmation it switches the channel off again, and a watchdog switches off any pulse that outlasts its
time.

A module also speaks unasked, as soon as a relay changes: when its countdown ends, or by its wall switch or Smart
Life. tinytuya takes the first message after a request for its answer, so those are read as they come, and never
taken for an answer.
"""

from __future__ import annotations

import json
import logging
import math
import re
import select
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import tinytuya

from live_tracker.devices import BAD_KEY, NO_ANSWER, NO_TIMER, NOT_FOUND, OFFLINE, DeviceError, DeviceSpec

log = logging.getLogger(__name__)

PROTOCOLS = ("3.4", "3.3", "3.5", "3.1")  # tried in this order until the module answers
POLL_S = 3.0  # how often its state is read; what it says unasked shows at once
LISTEN_S = 0.5  # between polls, how long a wait for it to speak lasts before checking whether to stop
UNASKED_MAX = 10  # messages read in a row before asking it something
RETRY_S = 5.0  # after it stops answering
SOCKET_TIMEOUT_S = 2.0
WATCH_GRACE_S = 2.0  # how long past its time a pulse may still be on before it is switched off from here
DISCOVERY_PORTS = (6666, 6667)  # where modules say who and where they are, every few seconds
MAX_MS = 3_600_000  # the longest a pulse lasts; the module's countdown goes up to a day
REGIONS = {"us": "Western America", "ue": "Eastern America", "eu": "Central Europe", "we": "Western Europe",
           "in": "India", "sg": "Singapore", "cn": "China"}  # Tuya's data centers, by tinytuya's name

_SWITCH = re.compile(r"^switch(?:_(\d+))?$")  # switch, switch_1 ... (not switch_led, switch_type, ...)
_COUNTDOWN = re.compile(r"^countdown(?:_(\d+))?$")
_POWER_ON = {"0": "off", "off": "off", "1": "on", "on": "on", "2": "memory", "memory": "memory"}


def pulse_seconds(ms: int, max_ms: int) -> int:
    """The module counts whole seconds: at least one, at most the output's longest time."""
    return max(1, min(math.ceil(ms / 1000), max(1, max_ms // 1000)))


@dataclass(frozen=True)
class CloudOutput:
    key: str  # the switch's data point
    number: int  # its channel, 1 for a module with one
    timer_dp: int | None


@dataclass(frozen=True)
class CloudDevice:
    """A device in the Tuya account, as the import shows it. The local key never leaves the server."""

    uid: str
    name: str
    model: str
    local_key: str
    outputs: tuple[CloudOutput, ...]
    online: bool
    power_on: str = ""  # what its relays do after a power cut: "off", "on", "memory" (as before), or "" unknown


class TuyaCloudError(Exception):
    """`code`: "login" (wrong Access ID or Secret), "list" (the account's devices couldn't be read), "network"."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def outputs_from_mapping(mapping: dict) -> tuple[CloudOutput, ...]:
    """Each channel's switch and countdown, from the module's data points as Tuya's cloud describes them."""
    switches: dict[int, int] = {}
    countdowns: dict[int, int] = {}
    for dp, info in (mapping or {}).items():
        code = str((info or {}).get("code", ""))
        if (match := _SWITCH.match(code)) and info.get("type") == "Boolean":
            switches[int(match.group(1) or 1)] = int(dp)
        elif match := _COUNTDOWN.match(code):
            countdowns[int(match.group(1) or 1)] = int(dp)
    return tuple(CloudOutput(str(dp), number, countdowns.get(number)) for number, dp in sorted(switches.items()))


def cloud_devices(access_id: str, secret: str, region: str, cloud_factory=tinytuya.Cloud) -> list[CloudDevice]:
    """The devices of the Smart Life account linked to the developer project, with their local keys."""
    try:
        cloud = cloud_factory(apiRegion=region, apiKey=access_id, apiSecret=secret)
        if cloud.error:
            raise TuyaCloudError("login", str(cloud.error))
        found = cloud.getdevices(verbose=False, include_map=True)
    except TuyaCloudError:
        raise
    except OSError as e:  # requests' connection errors are OSErrors too
        raise TuyaCloudError("network", str(e)) from e
    if isinstance(found, dict):  # an error, such as a project not linked to the account, or another data center
        raise TuyaCloudError("list", json.dumps(found)[:300])
    devices = []
    for d in found or []:
        devices.append(CloudDevice(
            uid=str(d.get("id", "")), name=str(d.get("name", "")).strip(), model=str(d.get("model", "")).strip(),
            local_key=str(d.get("key", "")), outputs=outputs_from_mapping(d.get("mapping") or {}),
            online=bool(d.get("online", True)), power_on=_power_on(cloud, str(d.get("id", ""))),
        ))
    return [d for d in devices if d.uid]


def _power_on(cloud, uid: str) -> str:
    """What the module's relays do after a power cut, from its state in the cloud; "" when it doesn't say."""
    try:
        status = cloud.getstatus(uid)
    except Exception:  # only a hint on the import page
        return ""
    for item in (status or {}).get("result") or []:
        if item.get("code") == "relay_status":
            return _POWER_ON.get(str(item.get("value")), "")
    return ""


class TuyaDiscovery:
    """Listens for the modules' broadcasts, so a module is found after its address changes. Broadcasts don't cross
    routers: a module on another network needs its address typed in its settings."""

    def __init__(self, ports: tuple[int, ...] = DISCOVERY_PORTS) -> None:
        self._ports = ports
        self._found: dict[str, tuple[str, str]] = {}  # device ID -> (address, protocol)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for port in self._ports:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # other Tuya tools may listen too
                sock.bind(("", port))
                sock.settimeout(1.0)
            except OSError as e:
                log.warning("Tuya modules can't be found on UDP port %d (%s): type their addresses instead", port, e)
                continue
            thread = threading.Thread(target=self._listen, args=(sock,), name=f"tuya-discovery-{port}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)

    def address(self, uid: str) -> tuple[str, str] | None:
        with self._lock:
            return self._found.get(uid)

    def heard(self, message: bytes) -> None:
        """One broadcast: {"ip": "192.168.100.102", "gwId": "eb2002...", "version": "3.4", ...}."""
        try:
            info = json.loads(tinytuya.decrypt_udp(message))
        except Exception:  # not a Tuya broadcast, or one we can't read
            return
        uid, ip = str(info.get("gwId", "")), str(info.get("ip", ""))
        if uid and ip:
            with self._lock:
                self._found[uid] = (ip, str(info.get("version", "")))

    def _listen(self, sock: socket.socket) -> None:
        with sock:
            while not self._stop.is_set():
                try:
                    message, _ = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    time.sleep(1.0)
                    continue
                self.heard(message)


@dataclass
class _State:
    online: bool = False
    problem: str = ""
    address: str = ""
    protocol: str = ""
    outputs: dict[str, bool] = field(default_factory=dict)
    seen: float | None = None


class TuyaModule:
    """One module: its connection on a thread of its own, its state every few seconds, and pulses."""

    def __init__(self, spec: DeviceSpec, find: Callable[[str], tuple[str, str] | None],
                 connect: Callable[..., tinytuya.Device] = tinytuya.Device) -> None:
        self.spec = spec
        self._find = find
        self._connect = connect
        self._device: tinytuya.Device | None = None
        self._lock = threading.RLock()  # one request at a time on the module's one connection
        self._state = _State()
        self._deadlines: dict[str, float] = {}  # output -> when its pulse must be over (time.monotonic)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"tuya-{spec.id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=SOCKET_TIMEOUT_S * 3)
        self._close()

    def status(self) -> dict:
        state = self._state
        return {"online": state.online, "problem": state.problem, "ip": state.address, "protocol": state.protocol,
                "outputs": dict(state.outputs), "seen": state.seen}

    def pulse(self, key: str, ms: int) -> None:
        """Switches the output on, then sets the module's own countdown to switch it off.

        Two commands, in this order: the module clears its countdown whenever its switch changes, even when both
        come in one command (the NovaDigital MS-107 then stayed on, on 2026-10-27). Each must be confirmed; a
        countdown that isn't is followed by "off" right away, and the watchdog in _run switches off a pulse that
        outlasts its time anyway.
        """
        output = self.spec.output(key)
        if output.timer_dp is None:
            raise DeviceError(NO_TIMER, key)
        seconds = pulse_seconds(ms, output.max_ms)
        timer = str(output.timer_dp)
        with self._lock:
            if self._device is None:
                raise DeviceError(OFFLINE)
            switched = self._ask(lambda device: device.set_value(output.key, True), output.key)
            if _value(switched, output.key) is not True:
                self._watch(output.key, 0)  # it may have switched on all the same
                self._drop("didn't confirm switching on: %s", switched)
                raise DeviceError(NO_ANSWER, str(switched))
            self._watch(output.key, seconds)
            timed = self._ask(lambda device: device.set_value(timer, seconds), timer)
            left = _value(timed, timer)
            if not (isinstance(left, int) and 0 < left <= seconds):
                off = self._ask(lambda device: device.set_value(output.key, False), output.key)
                if _value(off, output.key) is not False:
                    self._drop("didn't confirm its countdown, nor switching off: %s", off)
                log.warning("%s: %s didn't confirm its countdown (%s), switched off", self.spec.name, key, timed)
                raise DeviceError(NO_ANSWER, str(timed))
        self._update(timed)
        self._state.outputs[output.key] = True
        log.info("%s: %s on for %d s", self.spec.name, key, seconds)

    def _run(self) -> None:
        switches = [output.key for output in self.spec.outputs]
        while not self._stop.is_set():
            if self._device is None and not self._open():
                self._stop.wait(RETRY_S)
                continue
            with self._lock:
                reply = self._ask(lambda device: device.status(), *switches) if self._device else None
            if not isinstance(reply, dict) or "dps" not in reply:
                self._drop("stopped answering: %s", reply)
                self._stop.wait(RETRY_S)
                continue
            self._update(reply)
            self._end_overdue_pulses()
            self._listen(POLL_S)

    def _ask(self, request: Callable[[tinytuya.Device], object], *dps: str) -> object:
        """The module's answer to a request: the first message with these data points. What it said unasked
        before is read first, and what it says unasked meanwhile only updates the state: taken for answers, those
        put every answer one late on 2026-10-27, and the module was dropped as gone. Call with the lock."""
        self._read_unasked()
        device = self._device
        reply = request(device)
        until = time.monotonic() + SOCKET_TIMEOUT_S
        while (isinstance(reply, dict) and "dps" in reply and not all(dp in reply["dps"] for dp in dps)
               and time.monotonic() < until):
            self._update(reply)
            reply = device.receive()  # None once nothing comes for SOCKET_TIMEOUT_S
        return reply

    def _read_unasked(self) -> None:
        """Takes up what the module said without being asked and wasn't read yet. Call with the lock."""
        for _ in range(UNASKED_MAX):
            if self._device is None or not _unread(self._device, 0):
                return
            reply = self._device.receive()
            if isinstance(reply, dict) and "dps" in reply:
                self._update(reply)

    def _listen(self, seconds: float) -> None:
        """Waits for the next poll, taking up what the module says as it says it: a relay switched off by its
        countdown, or by its wall switch or Smart Life, shows on the Devices page at once."""
        until = time.monotonic() + seconds
        while not self._stop.is_set() and (left := until - time.monotonic()) > 0:
            unread = _unread(self._device, min(left, LISTEN_S))
            if unread is None:  # no connection to listen on: the next poll sees to it
                self._stop.wait(left)
            elif unread:
                with self._lock:
                    self._read_unasked()

    def _watch(self, key: str, seconds: int) -> None:
        self._deadlines[key] = time.monotonic() + seconds + WATCH_GRACE_S

    def _end_overdue_pulses(self) -> None:
        """The watchdog: a pulse still on past its time is switched off from here. It only watches pulses this
        service started, not a relay switched on by its wall switch or by Smart Life, and only once their time is
        up: a state read just before a pulse can't make it forget that pulse."""
        now = time.monotonic()
        for key, deadline in list(self._deadlines.items()):
            if now <= deadline:
                continue
            if not self._state.outputs.get(key):
                del self._deadlines[key]  # over, as it should be
                continue
            with self._lock:
                reply = self._ask(lambda device: device.set_value(key, False), key) if self._device else None
            if _value(reply, key) is False:
                log.warning("%s: %s was still on after its pulse: switched off from here", self.spec.name, key)
                self._state.outputs[key] = False
                del self._deadlines[key]
            else:
                self._drop("didn't confirm switching off an overdue pulse: %s", reply)  # tried again once connected

    def _open(self) -> bool:
        """Connects at its fixed address, or where its broadcasts said it is, trying each protocol version."""
        found = self._find(self.spec.uid)
        address = self.spec.address or (found[0] if found else "")
        if not address:
            self._state = _State(problem=NOT_FOUND)
            return False
        heard_protocol = found[1] if found and found[0] == address else ""
        protocols = [p for p in (self._state.protocol, heard_protocol) if p] + list(PROTOCOLS)
        refused = False
        for protocol in dict.fromkeys(protocols):
            device = self._connect(self.spec.uid, address, self.spec.local_key, version=float(protocol))
            device.set_socketPersistent(True)
            device.set_socketTimeout(SOCKET_TIMEOUT_S)
            device.set_socketRetryLimit(1)
            reply = device.status()
            if isinstance(reply, dict) and "dps" in reply:
                with self._lock:
                    self._device = device
                self._state = _State(online=True, address=address, protocol=protocol)
                self._update(reply)
                log.info("%s: connected at %s, protocol %s", self.spec.name, address, protocol)
                return True
            device.close()
            refused = refused or str((reply or {}).get("Err")) == str(tinytuya.ERR_KEY_OR_VER)
            if str((reply or {}).get("Err")) == str(tinytuya.ERR_CONNECT):
                break  # nothing listens there: no other version will do better
        self._state = _State(problem=BAD_KEY if refused else NO_ANSWER, address=address)
        return False

    def _update(self, reply: dict) -> None:
        dps = reply.get("dps") or {}
        outputs = dict(self._state.outputs)
        for output in self.spec.outputs:
            if output.key in dps:
                outputs[output.key] = bool(dps[output.key])
        self._state.outputs = outputs
        self._state.online, self._state.problem, self._state.seen = True, "", time.time()

    def _drop(self, why: str, reply: object) -> None:
        """Closes the connection, so the next round opens a new one."""
        log.warning("%s: " + why, self.spec.name, reply)
        self._close()
        self._state.online, self._state.problem = False, NO_ANSWER

    def _close(self) -> None:
        with self._lock:
            device, self._device = self._device, None
        if device is not None:
            try:
                device.close()
            except OSError:
                pass


def _unread(device: tinytuya.Device | None, timeout: float) -> bool | None:
    """Whether the module said something not read yet, waiting up to `timeout` for it; None without a connection."""
    sock = getattr(device, "socket", None)
    if sock is None:
        return None
    try:
        return bool(select.select([sock], [], [], timeout)[0])
    except (OSError, ValueError):  # closed meanwhile
        return None


def _value(reply: object, dp: str) -> object:
    """A data point's value in the module's answer to a command; None when the answer doesn't have it."""
    return (reply.get("dps") or {}).get(dp) if isinstance(reply, dict) else None
