import time

import numpy as np
from sqlalchemy import select

from portaria import service
from portaria.camera import Frame
from portaria.db import Event
from portaria.decision import Decision
from portaria.pipeline import Result
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
