import time
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select

from live_tracker import service, switching
from live_tracker.camera import Frame
from live_tracker.db import MODELS_VERSION, STOP_REQUESTS, Event, bump
from live_tracker.db import Camera as CameraRow
from live_tracker.decision import Decision
from live_tracker.devices import OFFLINE, DeviceError
from live_tracker.pipeline import Result
from live_tracker.objects import ObjectAnalyzer, Sighting
from live_tracker.recording import DAILY, INTERVAL, RecordGate, RecordRule, thing
from live_tracker.service import CameraSpec, VisionService


class FakeSource:
    """Stands in for the connection to a camera."""

    def __init__(self, url: str) -> None:
        self.url, self.ended, self.stopped = url, False, False

    def start(self) -> "FakeSource":
        return self

    def stop(self) -> None:
        self.stopped = True

    def next_frame(self, after_seq: int, timeout: float) -> None:
        time.sleep(0.01)
        return None


class FakeSwitch:
    def __init__(self) -> None:
        self.switched: list[tuple[str, int]] = []

    def switch(self, person: str, output_id: int) -> str:
        self.switched.append((person, output_id))
        return "shadow"


class FakeDevices:
    """The device manager, with one module that may be offline."""

    def __init__(self) -> None:
        self.pulsed: list[int] = []
        self.offline = False

    def pulse_output(self, output_id: int) -> str:
        if self.offline:
            raise DeviceError(OFFLINE)
        self.pulsed.append(output_id)
        return "Módulo"


def _vision(cfg, monkeypatch) -> VisionService:
    """The service without its preview port or the face models."""
    monkeypatch.setattr(service, "PreviewServer", lambda host, port: None)
    vision = VisionService(cfg)
    monkeypatch.setattr(vision, "make_recognizer", lambda: None)
    return vision


def test_the_two_lenses_of_one_picture_share_one_connection(cfg, monkeypatch):
    opened: list[FakeSource] = []

    def open_source(url: str, reconnect_delay_s: float, loop: bool) -> FakeSource:
        opened.append(FakeSource(url))
        return opened[-1]

    monkeypatch.setattr(service, "Camera", open_source)
    vision = _vision(cfg, monkeypatch)
    url = "rtsp://camera/onvif1"
    top, bottom = CameraSpec(1, "Lens 1", url, part="top"), CameraSpec(2, "Lens 2", url, part="bottom")
    try:
        vision._apply([top, bottom])
        assert [source.url for source in opened] == [url]  # one connection for both lenses
        vision._apply([top])
        assert not opened[0].stopped  # the top lens still reads it
        vision._apply([])
        assert opened[0].stopped
    finally:
        vision._apply([])


def test_every_camera_records_but_only_a_camera_with_an_output_switches_it(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    vision._switch, vision._with_access = FakeSwitch(), {"7"}
    frame = Frame(np.zeros((120, 160, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    recognized = Result(decision=Decision("recognized", "7", 0.8))
    vision.on_decision(CameraSpec(None, "Kitchen", "rtsp://kitchen"), frame, recognized)
    assert vision._switch.switched == []
    vision.on_decision(CameraSpec(None, "Entrance", "rtsp://entrance", output_id=3), frame, recognized)
    assert vision._switch.switched == [("7", 3)]
    with vision.db.session() as session:
        assert [event.action for event in session.scalars(select(Event).order_by(Event.id))] == ["", "shadow"]


def test_a_live_camera_switches_its_output_and_records_whether_it_did(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    devices = FakeDevices()
    vision._switch, vision._with_access = switching.make_switch("live", devices), {"7"}
    frame = Frame(np.zeros((120, 160, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    entrance = CameraSpec(None, "Entrance", "rtsp://entrance", output_id=3)
    vision.on_decision(entrance, frame, Result(decision=Decision("recognized", "7", 0.8)))
    vision.on_decision(entrance, frame, Result(decision=Decision("unknown", None, 0.2)))
    vision.on_decision(entrance, frame, Result(decision=Decision("recognized", "8", 0.8)))  # without access
    assert devices.pulsed == [3]
    devices.offline = True
    vision.on_decision(entrance, frame, Result(decision=Decision("recognized", "7", 0.8)))
    no_output = CameraSpec(None, "Kitchen", "rtsp://kitchen")
    vision.on_decision(no_output, frame, Result(decision=Decision("recognized", "7", 0.8)))
    assert devices.pulsed == [3]
    with vision.db.session() as session:
        actions = [event.action for event in session.scalars(select(Event).order_by(Event.id))]
    assert actions == ["switched", "denied", "denied", "failed", ""]
    with pytest.raises(ValueError):
        switching.make_switch("open", devices)  # a typo in the config


def test_the_output_switches_even_when_the_rule_keeps_no_photo(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    vision._switch, vision._with_access = FakeSwitch(), {"7"}
    frame = Frame(np.zeros((120, 160, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    recognized = Result(decision=Decision("recognized", "7", 0.8))
    vision.on_decision(CameraSpec(None, "Entrance", "rtsp://entrance", output_id=3), frame, recognized, keep=False)
    assert len(vision._switch.switched) == 1
    with vision.db.session() as session:
        assert session.scalars(select(Event)).all() == []


def test_a_camera_keeps_photos_by_its_rule_also_after_a_restart(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    monkeypatch.setattr(vision, "make_detector", lambda: object())
    frame = Frame(np.zeros((480, 640, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    with vision.db.session() as session:
        session.add(CameraRow(id=1, name="Depósito", url="rtsp://storage", analysis="objects", record_mode=DAILY))
    spec = CameraSpec(1, "Depósito", "rtsp://storage", analysis="objects", record_mode=DAILY)
    vision.on_sighting(spec, frame, Result(), Sighting("person", 1, 0.9, np.array([100, 50, 80, 300.0])))
    worker = service.CameraWorker(spec, vision, FakeSource(spec.url))  # started again, later the same day
    assert worker.gate.rule == RecordRule(DAILY)
    assert not worker.gate.allow(thing("person")) and worker.gate.allow(thing("dog"))
    every = CameraSpec(2, "Sala", "0", analysis="objects", record_mode=INTERVAL, record_every_s=600,
                       record_while_present=False)
    assert service.CameraWorker(every, vision, FakeSource("0")).gate.rule == RecordRule(INTERVAL, 600, False)


def test_someone_still_there_is_photographed_again_without_switching_the_output(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    vision._switch, vision._with_access = FakeSwitch(), {"7"}
    frame = Frame(np.zeros((120, 160, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    still_there = Result(staying=Decision("recognized", "7", 0.8, staying=True))
    vision.on_staying(CameraSpec(None, "Entrance", "rtsp://entrance", output_id=3), frame, still_there)
    assert vision._switch.switched == []
    with vision.db.session() as session:
        event = session.scalars(select(Event)).one()
        assert (event.kind, event.action, event.score) == ("recognized", "", 0.8)  # a photo, not an entry


class Frames:
    """A camera that sends a few different pictures, then ends."""

    def __init__(self, count: int) -> None:
        self.count, self.ended = count, False

    def next_frame(self, after_seq: int, timeout: float) -> Frame | None:
        if after_seq >= self.count:
            self.ended = True
            return None
        seq = after_seq + 1
        return Frame(np.zeros((120, 160, 3), np.uint8), seq=seq, captured_at=float(seq), signature=bytes([seq]))


class Script:
    """An analysis that answers each picture with the next of its results, five minutes after the one before."""

    def __init__(self, results: list[Result], now: list[datetime]) -> None:
        self.results, self.now = iter(results), now

    def process(self, frame: Frame, frozen: bool) -> Result:
        self.now[0] += timedelta(minutes=5)
        return next(self.results)


@pytest.mark.parametrize("while_present, actions", [(True, ["shadow", ""]), (False, ["shadow"])])
def test_a_person_who_stays_is_photographed_again_if_the_rule_repeats(cfg, monkeypatch, while_present, actions):
    vision = _vision(cfg, monkeypatch)
    vision._switch, vision._with_access = FakeSwitch(), {"7"}
    spec = CameraSpec(None, "Entrance", "rtsp://entrance", output_id=3, record_mode=INTERVAL, record_every_s=600,
                      record_while_present=while_present)
    worker = service.CameraWorker(spec, vision, Frames(4))
    now = [datetime(2026, 10, 24, 9, 0)]
    worker.gate = RecordGate(worker.gate.rule, clock=lambda: now[0])
    arrived = Result(decision=Decision("recognized", "7", 0.8))
    still_there = Result(staying=Decision("recognized", "7", 0.8, staying=True))
    worker.analyzer = Script([arrived, still_there, still_there, still_there], now)  # 9:05, 9:10, 9:15, 9:20
    monkeypatch.setattr(worker, "_publish", lambda frame, result: None)
    worker._run()
    assert len(vision._switch.switched) == 1  # only arriving switches the output
    with vision.db.session() as session:  # 9:05 and, while they stay, 9:15
        assert [event.action for event in session.scalars(select(Event).order_by(Event.id))] == actions


def test_a_camera_waiting_for_its_model_gets_it_once_downloaded(cfg, monkeypatch):
    monkeypatch.setattr(service, "Camera", lambda url, reconnect_delay_s, loop: FakeSource(url))
    vision = _vision(cfg, monkeypatch)
    attempts = []

    def load_model() -> None:
        attempts.append(True)
        if len(attempts) == 1:
            raise FileNotFoundError("models/auraface_v1_glintr100.onnx is missing")

    monkeypatch.setattr(vision, "make_recognizer", load_model)
    with vision.db.session() as session:
        session.add(CameraRow(name="Porta", url="rtsp://camera/onvif1", analysis="faces"))
    try:
        vision._sync()
        assert vision._workers[1].model_missing and vision._workers[1].status["model_missing"]
        with vision.db.session() as session:
            bump(session, MODELS_VERSION)
        vision._sync()
        assert not vision._workers[1].model_missing and len(attempts) == 2
    finally:
        vision._apply([])


def test_the_service_stops_when_settings_stops_the_system(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    with vision.db.session() as session:
        bump(session, STOP_REQUESTS)  # asked of the service that ran before this one
    vision._sync()
    assert not vision._stop.is_set()
    with vision.db.session() as session:
        bump(session, STOP_REQUESTS)
    vision._sync()
    assert vision._stop.is_set()


def test_a_thing_that_stayed_is_recorded_with_its_picture(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    frame = Frame(np.zeros((480, 640, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    sighting = Sighting("person", 2, 0.87, np.array([100, 50, 80, 300.0]))
    vision.on_sighting(CameraSpec(None, "Depósito", "rtsp://storage", analysis="objects"), frame, Result(), sighting)
    with vision.db.session() as session:
        event = session.scalars(select(Event)).one()
        assert (event.kind, event.label, event.count, event.action) == ("detected", "person", 2, "")
        assert (cfg.paths.data_dir / event.snapshot).exists()
        assert (cfg.paths.data_dir / event.snapshot.replace(".jpg", ".face.jpg")).exists()  # the record's thumbnail


def test_a_camera_runs_the_model_it_was_given(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    monkeypatch.setattr(vision, "make_detector", lambda: object())
    spec = CameraSpec(1, "Depósito", "rtsp://storage", analysis="objects", settings='{"kinds": ["dog"]}')
    worker = service.CameraWorker(spec, vision, FakeSource(spec.url))
    assert isinstance(worker.analyzer, ObjectAnalyzer) and not worker.model_missing
    assert service.CameraWorker(CameraSpec(2, "Sala", "0", analysis=""), vision, FakeSource("0")).analyzer is None
