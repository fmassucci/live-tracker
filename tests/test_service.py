import time

import numpy as np
from sqlalchemy import select

from portaria import service
from portaria.camera import Frame
from portaria.db import MODELS_VERSION, STOP_REQUESTS, Event, bump
from portaria.db import Camera as CameraRow
from portaria.decision import Decision
from portaria.pipeline import Result
from portaria.objects import ObjectAnalyzer, Sighting
from portaria.recording import DAILY, INTERVAL, RecordRule, thing
from portaria.service import CameraSpec, VisionService


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


class FakeDoor:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def open(self, name: str) -> None:
        self.opened.append(name)


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


def test_every_camera_records_but_only_a_door_camera_opens_it(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    vision._door, vision._door_allowed = FakeDoor(), {"7"}
    frame = Frame(np.zeros((120, 160, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    recognized = Result(decision=Decision("recognized", "7", 0.8))
    vision.on_decision(CameraSpec(None, "Kitchen", "rtsp://kitchen"), frame, recognized)
    assert vision._door.opened == []
    vision.on_decision(CameraSpec(None, "Staff door", "rtsp://door", device="door"), frame, recognized)
    assert len(vision._door.opened) == 1
    with vision.db.session() as session:
        assert [event.door for event in session.scalars(select(Event).order_by(Event.id))] == ["", "shadow"]


def test_the_door_opens_even_when_the_rule_keeps_no_photo(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    vision._door, vision._door_allowed = FakeDoor(), {"7"}
    frame = Frame(np.zeros((120, 160, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    recognized = Result(decision=Decision("recognized", "7", 0.8))
    vision.on_decision(CameraSpec(None, "Staff door", "rtsp://door", device="door"), frame, recognized, keep=False)
    assert len(vision._door.opened) == 1
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
    every = CameraSpec(2, "Sala", "0", analysis="objects", record_mode=INTERVAL, record_every_s=600)
    assert service.CameraWorker(every, vision, FakeSource("0")).gate.rule.repeat_s == 600


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
        assert (event.kind, event.label, event.count, event.door) == ("detected", "person", 2, "")
        assert (cfg.paths.data_dir / event.snapshot).exists()
        assert (cfg.paths.data_dir / event.snapshot.replace(".jpg", ".face.jpg")).exists()  # the record's thumbnail


def test_a_camera_runs_the_model_it_was_given(cfg, monkeypatch):
    vision = _vision(cfg, monkeypatch)
    monkeypatch.setattr(vision, "make_detector", lambda: object())
    spec = CameraSpec(1, "Depósito", "rtsp://storage", analysis="objects", settings='{"kinds": ["dog"]}')
    worker = service.CameraWorker(spec, vision, FakeSource(spec.url))
    assert isinstance(worker.analyzer, ObjectAnalyzer) and not worker.model_missing
    assert service.CameraWorker(CameraSpec(2, "Sala", "0", analysis=""), vision, FakeSource("0")).analyzer is None
