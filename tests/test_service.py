import time

from portaria import service
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
    top, bottom = CameraSpec(1, "Lens 1", url, "door", part="top"), CameraSpec(2, "Lens 2", url, "view", part="bottom")
    try:
        vision._apply([top, bottom])
        assert [source.url for source in opened] == [url]  # one connection for both lenses
        vision._apply([top])
        assert not opened[0].stopped  # the top lens still reads it
        vision._apply([])
        assert opened[0].stopped
    finally:
        vision._apply([])
