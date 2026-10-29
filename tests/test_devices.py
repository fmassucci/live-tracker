import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from live_tracker.config import MqttConfig
from live_tracker.db import Device, DeviceCommand, DeviceOutput
from live_tracker.devices import (
    BAD_KEY, BROKER, NO_ANSWER, NO_OUTPUT, NO_TIMER, NOT_FOUND, OFFLINE, REFUSED, DeviceError, DeviceSpec, OutputSpec,
    tuya,
)
from live_tracker.devices.esp32 import Esp32Boards
from live_tracker.devices.manager import DeviceManager, record_board

# The NovaDigital MS-107 (2 channels) as Tuya's cloud described it on 2026-10-27.
MS107_MAPPING = {
    "1": {"code": "switch_1", "type": "Boolean", "values": {}},
    "2": {"code": "switch_2", "type": "Boolean", "values": {}},
    "9": {"code": "countdown_1", "type": "Integer", "values": {"unit": "s", "min": 0, "max": 86400}},
    "10": {"code": "countdown_2", "type": "Integer", "values": {"unit": "s", "min": 0, "max": 86400}},
    "38": {"code": "relay_status", "type": "Enum", "values": {"range": ["0", "1", "2"]}},
    "44": {"code": "switch_inching", "type": "String", "values": "{}"},
    "47": {"code": "switch_type", "type": "Enum", "values": {"range": ["flip", "sync", "button"]}},
}


def test_each_channel_of_a_tuya_module_is_an_output_with_its_own_countdown():
    outputs = tuya.outputs_from_mapping(MS107_MAPPING)
    assert outputs == (tuya.CloudOutput("1", 1, 9), tuya.CloudOutput("2", 2, 10))
    single = tuya.outputs_from_mapping({"1": {"code": "switch", "type": "Boolean"}, "9": {"code": "countdown"}})
    assert single == (tuya.CloudOutput("1", 1, 9),)


def test_tuya_pulses_last_whole_seconds_within_the_outputs_longest_time():
    assert tuya.pulse_seconds(1000, 10_000) == 1
    assert tuya.pulse_seconds(2500, 10_000) == 3
    assert tuya.pulse_seconds(100, 10_000) == 1
    assert tuya.pulse_seconds(60_000, 10_000) == 10


COUNTDOWNS = {"1": "9", "2": "10"}  # each switch's countdown, as on the MS-107


class FakeTuyaDevice:
    """A module on the network: answers on some protocol versions, and keeps its data points as the NovaDigital
    MS-107 does: changing a switch clears its countdown, even within one command. What it says unasked waits on the
    connection, and comes before the answer to whatever is asked next, as with tinytuya."""

    def __init__(self, answers_on: set[float], dps: dict, refuses_key: bool = False,
                 ignores: tuple[str, ...] = ()) -> None:
        self.answers_on, self.dps, self.refuses_key, self.ignores = answers_on, dps, refuses_key, ignores
        self.sent: list[tuple[str, object]] = []
        self.unread: list[dict] = []
        self.version = None

    def __call__(self, uid, address, local_key, version):  # stands in for tinytuya.Device(...)
        self.version, self.address = version, address
        return self

    def set_socketPersistent(self, on):
        pass

    def set_socketTimeout(self, seconds):
        pass

    def set_socketRetryLimit(self, tries):
        pass

    def status(self):
        if self.refuses_key:
            return {"Error": "Check device key or version", "Err": "914"}
        if self.version not in self.answers_on:
            return {"Error": "Unexpected Payload from Device", "Err": "904"}
        return self._answer({"dps": dict(self.dps)})

    def set_value(self, dp, value):
        self.sent.append((dp, value))
        return self.set_multiple_values({dp: value}, record=False)

    def set_multiple_values(self, values, record=True):
        if record:
            self.sent.append(values)
        changed = {dp: v for dp, v in values.items() if dp not in self.ignores}
        self.dps.update(changed)
        for dp in changed:
            if dp in COUNTDOWNS:
                self.dps[COUNTDOWNS[dp]] = 0
        return self._answer({"dps": changed})

    def _answer(self, reply):
        if not self.unread:
            return reply
        self.unread.append(reply)
        return self.unread.pop(0)

    def receive(self):
        return self.unread.pop(0) if self.unread else None

    def run_countdowns(self):
        """The countdowns run out: their switches go off, and the module says so."""
        for switch, countdown in COUNTDOWNS.items():
            if self.dps.get(countdown):
                self.dps[switch], self.dps[countdown] = False, 0
                self.unread.append({"protocol": 4, "dps": {switch: False, countdown: 0}})

    def close(self):
        pass


SPEC = DeviceSpec(1, "Porta", "tuya", "eb20021fc469df2bdbgmsl", local_key="k" * 16,
                  outputs=(OutputSpec("1", "lock", 3000, 3_600_000, 9), OutputSpec("2", "relay", 3000, 3_600_000, None)))


def _opened(device: FakeTuyaDevice, spec: DeviceSpec = SPEC, found=("192.168.100.102", "")) -> tuya.TuyaModule:
    module = tuya.TuyaModule(spec, lambda uid: found, connect=device)
    module._open()
    return module


def test_a_tuya_module_is_found_on_the_protocol_it_speaks():
    device = FakeTuyaDevice({3.3}, {"1": False, "2": True})
    module = _opened(device)
    status = module.status()
    assert status["online"] and status["protocol"] == "3.3" and status["ip"] == "192.168.100.102"
    assert status["outputs"] == {"1": False, "2": True}


def test_a_pulse_switches_the_output_on_then_sets_the_modules_own_countdown():
    device = FakeTuyaDevice({3.4}, {"1": False, "9": 0})
    module = _opened(device)
    module.pulse("1", 3000)
    assert device.sent == [("1", True), ("9", 3)]  # the countdown second: switching clears it
    assert device.dps == {"1": True, "9": 3} and module.status()["outputs"]["1"] is True
    device.run_countdowns()
    assert device.dps["1"] is False  # switched off by the module itself
    with pytest.raises(DeviceError) as error:
        module.pulse("2", 3000)  # no countdown on the module: never switched on
    assert error.value.code == NO_TIMER and len(device.sent) == 2


def test_a_countdown_the_module_doesnt_confirm_switches_the_output_off_again():
    device = FakeTuyaDevice({3.4}, {"1": False, "9": 0}, ignores=("9",))
    module = _opened(device)
    with pytest.raises(DeviceError) as error:
        module.pulse("1", 3000)
    assert error.value.code == NO_ANSWER
    assert device.sent == [("1", True), ("9", 3), ("1", False)] and device.dps["1"] is False


TWO_TIMERS = replace(SPEC, outputs=(OutputSpec("1", "lock", 1000, 3_600_000, 9),
                                    OutputSpec("2", "relay", 1000, 3_600_000, 10)))


@pytest.mark.parametrize("read_before_asking", [True, False])
def test_what_the_module_says_unasked_is_never_taken_for_an_answer(monkeypatch, read_before_asking):
    """On 2026-10-27 a countdown's end was taken for the answer to the next pulse, which then failed, and the
    module was dropped as gone. Said before the next pulse, it's read first; said as it's asked, it's read past."""
    if read_before_asking:
        monkeypatch.setattr(tuya, "_unread", lambda device, timeout: bool(device.unread))
    device = FakeTuyaDevice({3.4}, {"1": False, "9": 0, "2": False, "10": 0})
    module = _opened(device, TWO_TIMERS)
    module.pulse("1", 1000)
    device.run_countdowns()
    module.pulse("2", 1000)
    assert device.sent == [("1", True), ("9", 1), ("2", True), ("10", 1)] and not device.unread
    status = module.status()
    assert status["online"] and status["outputs"] == {"1": False, "2": True}


def test_a_relay_switched_by_the_module_shows_at_once_not_at_the_next_poll(monkeypatch):
    monkeypatch.setattr(tuya, "_unread", lambda device, timeout: bool(device.unread))
    device = FakeTuyaDevice({3.4}, {"1": False, "9": 0, "2": False, "10": 0})
    module = _opened(device, TWO_TIMERS)
    module.pulse("1", 1000)
    device.run_countdowns()
    module._listen(0.05)
    assert module.status()["outputs"]["1"] is False and not device.unread


def test_the_watchdog_switches_off_a_pulse_that_outlasts_its_time_and_nothing_else():
    device = FakeTuyaDevice({3.4}, {"1": False, "9": 0, "2": False})
    module = _opened(device)
    module.pulse("1", 1000)
    device.dps["9"] = 0  # its countdown got lost: the relay would stay on
    device.dps["2"] = True  # someone used the wall switch of the other channel
    module._end_overdue_pulses()
    assert device.dps["1"] is True  # still within its time
    module._deadlines["1"] = 0.0  # its time is up
    module._update(device.status())
    module._end_overdue_pulses()
    assert device.dps["1"] is False and device.dps["2"] is True
    assert device.sent[-1] == ("1", False) and not module._deadlines


def test_a_tuya_module_that_isnt_there_or_refuses_its_key_says_why():
    nowhere = tuya.TuyaModule(SPEC, lambda uid: None, connect=FakeTuyaDevice({3.4}, {}))
    assert not nowhere._open() and nowhere.status()["problem"] == NOT_FOUND
    with pytest.raises(DeviceError) as error:
        nowhere.pulse("1", 1000)
    assert error.value.code == OFFLINE
    paired_again = _opened(FakeTuyaDevice({3.4}, {}, refuses_key=True))
    assert paired_again.status()["problem"] == BAD_KEY


def test_a_fixed_address_wins_over_the_one_heard_on_the_network():
    device = FakeTuyaDevice({3.4}, {"1": False})
    fixed = replace(SPEC, address="10.0.0.7")
    _opened(device, fixed)
    assert device.address == "10.0.0.7"


def test_tuya_broadcasts_say_where_each_module_is(monkeypatch):
    monkeypatch.setattr(tuya.tinytuya, "decrypt_udp", lambda message: message.decode())
    discovery = tuya.TuyaDiscovery(ports=())
    discovery.heard(json.dumps({"ip": "192.168.100.102", "gwId": "eb2002", "version": "3.4"}).encode())
    discovery.heard(b"not a broadcast")
    assert discovery.address("eb2002") == ("192.168.100.102", "3.4")
    assert discovery.address("other") is None


class FakeCloud:
    def __init__(self, apiRegion, apiKey, apiSecret):
        self.error = None if apiSecret == "right" else {"Error": "sign invalid"}

    def getdevices(self, verbose=False, include_map=False):
        return [{"id": "eb2002", "name": "Módulo Mini Ms-107 2 Canais Wi-Fi", "key": "k" * 16, "model": "MS-107 2C WF",
                 "mapping": MS107_MAPPING, "online": True}]

    def getstatus(self, uid):
        return {"result": [{"code": "switch_1", "value": False}, {"code": "relay_status", "value": "1"}]}


def test_the_tuya_account_lists_its_modules_with_their_keys_and_power_on_state():
    [module] = tuya.cloud_devices("id", "right", "us", cloud_factory=FakeCloud)
    assert module.uid == "eb2002" and module.local_key == "k" * 16 and module.model == "MS-107 2C WF"
    assert [o.key for o in module.outputs] == ["1", "2"] and module.power_on == "on"
    with pytest.raises(tuya.TuyaCloudError) as error:
        tuya.cloud_devices("id", "wrong", "us", cloud_factory=FakeCloud)
    assert error.value.code == "login"


class FakeMqttClient:
    """Records what is published; the test plays the board."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict, int]] = []
        self.on_publish = None

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload), qos))
        if self.on_publish:
            self.on_publish(topic, json.loads(payload))


BOARD_INFO = {"name": "Porta funcionarios", "firmware": "1.0.0", "ip": "192.168.0.50",
              "outputs": [{"key": "relay1", "kind": "lock", "max_ms": 10000}],
              "inputs": [{"key": "button1", "kind": "button"}]}


def _boards() -> tuple[Esp32Boards, FakeMqttClient, list]:
    announced = []
    boards = Esp32Boards(MqttConfig(host="127.0.0.1"), lambda uid, info: announced.append((uid, info)))
    client = FakeMqttClient()
    boards._client, boards._connected = client, True
    return boards, client, announced


def test_a_board_announces_what_it_has_and_reports_its_state():
    boards, _, announced = _boards()
    boards.heard("live-tracker/esp32-a1b2c3/info", json.dumps(BOARD_INFO).encode())
    boards.heard("live-tracker/esp32-a1b2c3/out/relay1/state", b"on")
    boards.heard("live-tracker/esp32-a1b2c3/in/button1", b'{"value": "pressed"}')
    boards.heard("live-tracker/esp32-a1b2c3/health", b'{"uptime_s": 60, "rssi": -61, "ip": "192.168.0.51"}')
    assert announced == [("esp32-a1b2c3", BOARD_INFO)]
    status = boards.status("esp32-a1b2c3")
    assert status["online"] and status["ip"] == "192.168.0.51" and status["rssi"] == -61
    assert status["outputs"] == {"relay1": True} and status["inputs"]["button1"]["value"] == "pressed"
    boards.heard("live-tracker/esp32-a1b2c3/status", b"offline")  # its last will, from the broker
    assert not boards.status("esp32-a1b2c3")["online"]


BOARD = DeviceSpec(2, "Porta", "esp32", "esp32-a1b2c3", outputs=(OutputSpec("relay1", "lock", 3000, 10000),))


def test_a_command_to_a_board_waits_for_its_confirmation(monkeypatch):
    boards, client, _ = _boards()
    boards.heard("live-tracker/esp32-a1b2c3/status", b"online")
    client.on_publish = lambda topic, command: boards.heard(
        "live-tracker/esp32-a1b2c3/ack", json.dumps({"id": command["id"], "ok": True}).encode())
    boards.pulse(BOARD, "relay1", 60_000)
    topic, command, qos = client.published[0]
    assert topic == "live-tracker/esp32-a1b2c3/out/relay1/set" and qos == 1
    assert command["action"] == "pulse" and command["ms"] == 10000  # never longer than the output's longest time

    client.on_publish = lambda topic, command: boards.heard(
        "live-tracker/esp32-a1b2c3/ack", json.dumps({"id": command["id"], "ok": False, "error": "busy"}).encode())
    with pytest.raises(DeviceError) as error:
        boards.pulse(BOARD, "relay1", 1000)
    assert error.value.code == REFUSED

    monkeypatch.setattr("live_tracker.devices.esp32.ACK_TIMEOUT_S", 0.05)
    client.on_publish = None
    with pytest.raises(DeviceError) as error:
        boards.pulse(BOARD, "relay1", 1000)
    assert error.value.code == NO_ANSWER


def test_commands_need_the_broker_and_the_board_online():
    boards, _, _ = _boards()
    with pytest.raises(DeviceError) as error:
        boards.pulse(BOARD, "relay1", 1000)
    assert error.value.code == OFFLINE
    boards._connected = False
    with pytest.raises(DeviceError) as error:
        boards.pulse(BOARD, "relay1", 1000)
    assert error.value.code == BROKER


def test_a_new_board_is_added_to_be_named_and_keeps_its_names_after(db):
    with db.session() as session:
        assert record_board(session, "esp32-a1b2c3", BOARD_INFO)
    with db.session() as session:
        board = session.scalar(select(Device))
        assert board.new and board.kind == "esp32" and board.name == "Porta funcionarios"
        assert [(o.key, o.kind, o.max_ms) for o in board.outputs] == [("relay1", "lock", 10000)]
        assert [(i.key, i.kind) for i in board.inputs] == [("button1", "button")]
        board.outputs[0].name, board.new = "Fechadura", False
    with db.session() as session:
        assert not record_board(session, "esp32-a1b2c3", BOARD_INFO)  # it connects again: nothing changed
        changed = {**BOARD_INFO, "outputs": [*BOARD_INFO["outputs"], {"key": "relay2", "kind": "light", "max_ms": 60000}]}
        assert record_board(session, "esp32-a1b2c3", changed)
    with db.session() as session:
        board = session.scalar(select(Device))
        assert [o.name for o in board.outputs] == ["Fechadura", "relay2"] and not board.new


class FakeModule:
    def __init__(self, spec, find) -> None:
        self.spec, self.started, self.stopped, self.pulses = spec, False, False, []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def status(self):
        return {"online": True, "outputs": {}}

    def pulse(self, key, ms):
        self.pulses.append((key, ms))


class FakeDiscovery:
    def start(self):
        pass

    def stop(self):
        pass

    def address(self, uid):
        return None


def _manager(cfg, db) -> DeviceManager:
    return DeviceManager(cfg, db, tuya_module=FakeModule, discovery=FakeDiscovery)


def _tuya_device(db) -> int:
    with db.session() as session:
        device = Device(kind="tuya", uid="eb2002", name="Porta", local_key="k" * 16,
                        outputs=[DeviceOutput(key="1", name="Fechadura", kind="lock", timer_dp=9)])
        session.add(device)
        session.flush()
        return device.id


def test_the_manager_connects_the_modules_in_the_database_and_lets_go_of_removed_ones(cfg, db):
    device_id = _tuya_device(db)
    manager = _manager(cfg, db)
    with db.session() as session:
        manager.load(session)
    module = manager._modules[device_id]
    assert module.started and manager.status()["devices"][str(device_id)]["online"]
    assert manager.status()["broker"] == "off"  # no [mqtt] host: no ESP32 boards
    with db.session() as session:
        session.delete(session.get(Device, device_id))
    with db.session() as session:
        manager.load(session)
    assert module.stopped and not manager._modules


def test_a_camera_pulses_its_output_for_the_outputs_own_time(cfg, db):
    device_id = _tuya_device(db)
    with db.session() as session:
        output = session.scalar(select(DeviceOutput))
        output.pulse_ms, output_id = 5000, output.id
    manager = _manager(cfg, db)
    with db.session() as session:
        manager.load(session)
    assert manager.pulse_output(output_id) == "Porta"
    assert manager._modules[device_id].pulses == [("1", 5000)]
    with pytest.raises(DeviceError) as error:
        manager.pulse_output(output_id + 1)  # removed meanwhile
    assert error.value.code == NO_OUTPUT


def test_commands_from_the_web_ui_are_carried_out_and_late_ones_dropped(cfg, db):
    device_id = _tuya_device(db)
    manager = _manager(cfg, db)
    with db.session() as session:
        manager.load(session)
        session.add(DeviceCommand(device_id=device_id, output_key="1", ms=1000, requested_by="admin"))
        session.add(DeviceCommand(device_id=device_id, output_key="1", ms=1000, requested_by="admin",
                                  requested_at=datetime.now() - timedelta(seconds=30)))
    taken = manager._take_commands()
    assert len(taken) == 1
    manager._carry_out(*taken[0])
    assert manager._modules[device_id].pulses == [("1", 1000)]  # only the fresh one
    with db.session() as session:
        results = [c.result for c in session.scalars(select(DeviceCommand).order_by(DeviceCommand.id))]
    assert results == ["ok", "late"]
    assert manager._take_commands() == []  # each command is carried out once
