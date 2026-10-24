"""Frame source for RTSP cameras, video files and webcams."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Cheap Wi-Fi cameras lose UDP packets, which smears frames; RTSP over TCP keeps them intact.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


@dataclass(frozen=True)
class Frame:
    image: np.ndarray
    seq: int
    captured_at: float  # time.monotonic()
    signature: bytes    # equal for bit-identical frames, i.e. a stream repeating itself


def open_capture(source: str) -> cv2.VideoCapture:
    if source.isdigit():
        return cv2.VideoCapture(int(source))
    timeouts = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000]
    return cv2.VideoCapture(source, cv2.CAP_FFMPEG, timeouts)


def redact(source: str) -> str:
    """The source without its password, for logs."""
    return re.sub(r"//[^/@]*@", "//***@", source)


def _signature(image: np.ndarray) -> bytes:
    return hashlib.blake2b(image[::8, ::8].tobytes(), digest_size=16).digest()


class Camera:
    """Reads frames on a background thread and keeps only the newest.

    Cheap cameras buffer seconds of video; always taking the latest frame keeps the door reacting
    to what is in front of it now. Live streams reconnect on their own. Video files play at their
    own frame rate and either loop or end the stream at EOF, which makes them handy for tests.
    """

    def __init__(self, source: str, reconnect_delay_s: float = 2.0, loop: bool = False) -> None:
        self._source = source
        self._is_file = os.path.isfile(source)
        self._loop = loop
        self._reconnect_delay_s = reconnect_delay_s
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._latest: Frame | None = None
        self.ended = False
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self) -> Camera:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def next_frame(self, after_seq: int, timeout: float) -> Frame | None:
        """The newest frame if it is newer than `after_seq`, waiting up to `timeout` seconds for one."""
        with self._cond:
            self._cond.wait_for(
                lambda: self.ended or (self._latest is not None and self._latest.seq > after_seq), timeout
            )
            frame = self._latest
        return frame if frame is not None and frame.seq > after_seq else None

    def _run(self) -> None:
        seq = 0
        while not self._stop.is_set():
            capture = open_capture(self._source)
            if capture.isOpened():
                log.info("camera connected: %s", redact(self._source))
                seq = self._read_until_failure(capture, seq)
            capture.release()
            if self._stop.is_set() or (self._is_file and not self._loop):
                break
            if self._is_file:
                continue
            log.warning("camera unavailable: %s; retrying in %.0fs", redact(self._source), self._reconnect_delay_s)
            self._stop.wait(self._reconnect_delay_s)
        with self._cond:
            self.ended = True
            self._cond.notify_all()

    def _read_until_failure(self, capture: cv2.VideoCapture, seq: int) -> int:
        period = 1.0 / (capture.get(cv2.CAP_PROP_FPS) or 25.0) if self._is_file else 0.0
        while not self._stop.is_set():
            ok, image = capture.read()
            if not ok:
                break
            seq += 1
            with self._cond:
                self._latest = Frame(image, seq, time.monotonic(), _signature(image))
                self._cond.notify_all()
            if period:
                self._stop.wait(period)
        return seq


def probe(source: str, seconds: float = 3.0) -> tuple[dict[str, object], np.ndarray | None]:
    """Stream properties, as reported and as measured over `seconds`, plus the last frame read."""
    capture = open_capture(source)
    if not capture.isOpened():
        return {"opened": False}, None
    fourcc = int(capture.get(cv2.CAP_PROP_FOURCC)) & 0xFFFFFFFF
    info: dict[str, object] = {
        "opened": True,
        "size": f"{int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}",
        "codec": fourcc.to_bytes(4, "little").decode(errors="replace").strip("\x00 ") or "?",
        "reported_fps": round(capture.get(cv2.CAP_PROP_FPS), 1),
    }
    frames, last, start = 0, None, time.monotonic()
    while time.monotonic() - start < seconds:
        ok, image = capture.read()
        if not ok:
            break
        frames, last = frames + 1, image
    capture.release()
    info["measured_fps"] = round(frames / max(time.monotonic() - start, 1e-6), 1)
    return info, last
