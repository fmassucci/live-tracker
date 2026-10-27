"""ESP32 boards with our firmware, over MQTT: one connection to the broker for all of them (firmware/README.md).

A board announces what it has on `info` when it connects, says `online` or `offline` on `status`, reports each
output's state and each input's events, and answers every command on `ack`. Commands go out with QoS 1 and a unique
ID; the board runs each ID once and times every output itself.
"""

from __future__ import annotations

import itertools
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import paho.mqtt.client as mqtt

from live_tracker.config import MqttConfig
from live_tracker.devices import BROKER, NO_ANSWER, OFFLINE, REFUSED, DeviceError, DeviceSpec

log = logging.getLogger(__name__)

PREFIX = "live-tracker"
ACK_TIMEOUT_S = 3.0
KEEPALIVE_S = 30
# What the app listens to, from every board.
TOPICS = [(f"{PREFIX}/+/{rest}", 1) for rest in ("status", "info", "ack", "health", "out/+/state", "in/+")]


@dataclass
class Board:
    """What a board has said about itself since this service started."""

    online: bool = False
    info: dict = field(default_factory=dict)
    ip: str = ""
    rssi: int | None = None
    outputs: dict[str, bool] = field(default_factory=dict)
    inputs: dict[str, dict] = field(default_factory=dict)  # key -> {"value": "pressed", "at": "2026-10-27T14:02:10"}
    seen: float | None = None


class Esp32Boards:
    """The broker connection, every board's state, and commands that wait for the board's answer."""

    def __init__(self, cfg: MqttConfig, on_info: Callable[[str, dict], None],
                 client_factory: Callable[..., mqtt.Client] | None = None) -> None:
        self._cfg = cfg
        self._on_info = on_info
        self._client_factory = client_factory or _client
        self._client: mqtt.Client | None = None
        self._connected = False
        self._boards: dict[str, Board] = {}
        self._lock = threading.Lock()
        self._waiting: dict[str, tuple[threading.Event, list[dict]]] = {}  # command ID -> (answered, [answer])
        self._ids = itertools.count(1)
        self._run_id = secrets.token_hex(3)  # IDs stay unique across restarts: boards remember their last eight

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        client = self._client_factory(f"live-tracker-{self._run_id}")
        if self._cfg.username:
            client.username_pw_set(self._cfg.username, self._cfg.password or None)
        client.on_connect = self._connected_to_broker
        client.on_disconnect = self._disconnected
        client.on_message = self._message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(self._cfg.host, self._cfg.port, keepalive=KEEPALIVE_S)
        client.loop_start()
        self._client = client
        log.info("MQTT: connecting to %s:%d for the ESP32 boards", self._cfg.host, self._cfg.port)

    def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None
        self._connected = False

    def status(self, uid: str) -> dict:
        with self._lock:
            board = self._boards.get(uid) or Board()
            return {"online": self._connected and board.online, "problem": "" if self._connected else BROKER,
                    "ip": board.ip, "rssi": board.rssi, "outputs": dict(board.outputs),
                    "inputs": {k: dict(v) for k, v in board.inputs.items()}, "seen": board.seen}

    def pulse(self, spec: DeviceSpec, key: str, ms: int) -> None:
        """Sends the command and waits for the board to confirm it; the board ends the pulse by itself."""
        output = spec.output(key)
        client = self._client
        if client is None or not self._connected:
            raise DeviceError(BROKER)
        with self._lock:
            if not (self._boards.get(spec.uid) or Board()).online:
                raise DeviceError(OFFLINE)
        command_id = f"lt-{self._run_id}-{next(self._ids)}"
        answered, answer = threading.Event(), []
        self._waiting[command_id] = (answered, answer)
        try:
            payload = {"id": command_id, "action": "pulse", "ms": min(ms, output.max_ms)}
            client.publish(f"{PREFIX}/{spec.uid}/out/{key}/set", json.dumps(payload), qos=1)
            if not answered.wait(ACK_TIMEOUT_S):
                raise DeviceError(NO_ANSWER)
        finally:
            self._waiting.pop(command_id, None)
        if not answer[0].get("ok"):
            raise DeviceError(REFUSED, str(answer[0].get("error", "")))
        log.info("%s: %s on for %d ms", spec.name, key, payload["ms"])

    def heard(self, topic: str, payload: bytes) -> None:
        """One message from a board (on the MQTT thread)."""
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != PREFIX:
            return
        uid, rest = parts[1], parts[2:]
        text = payload.decode("utf-8", "replace").strip()
        if rest == ["ack"]:
            answer = _json(text)
            waiting = self._waiting.get(str(answer.get("id", "")))
            if waiting:
                waiting[1].append(answer)
                waiting[0].set()
            return
        info = None
        with self._lock:
            board = self._boards.setdefault(uid, Board())
            board.seen = time.time()
            if rest == ["status"]:
                board.online = text == "online"
            elif rest == ["info"]:
                info = board.info = _json(text)
                board.ip = str(info.get("ip", board.ip))
                board.online = True
            elif rest == ["health"]:
                health = _json(text)
                board.ip = str(health.get("ip", board.ip))
                board.rssi = health.get("rssi", board.rssi)
            elif len(rest) == 3 and rest[0] == "out" and rest[2] == "state":
                board.outputs[rest[1]] = text == "on"
            elif len(rest) == 2 and rest[0] == "in":
                board.inputs[rest[1]] = {"value": str(_json(text).get("value", "")),
                                         "at": datetime.now().isoformat(timespec="seconds")}
        if info:
            self._on_info(uid, info)

    def _connected_to_broker(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            log.warning("MQTT: the broker refused the connection: %s", reason_code)
            return
        self._connected = True
        client.subscribe(TOPICS)
        log.info("MQTT: connected")

    def _disconnected(self, client, userdata, flags, reason_code, properties=None) -> None:
        if self._connected:
            log.warning("MQTT: disconnected (%s); trying again", reason_code)
        self._connected = False

    def _message(self, client, userdata, message) -> None:
        try:
            self.heard(message.topic, message.payload)
        except Exception:  # one odd message must not stop the MQTT thread
            log.exception("MQTT: couldn't read a message on %s", message.topic)


def _client(client_id: str) -> mqtt.Client:
    # A clean session: commands sent while a board was away are dropped, not delivered late (firmware rule 4).
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
