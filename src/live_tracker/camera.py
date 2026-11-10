"""Frame sources: RTSP cameras, video files and webcams, and the pictures a browser sends from its own camera."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Cheap Wi-Fi cameras lose UDP packets, which smears frames, so RTSP video goes over TCP when the
# camera can. Some cameras only stream over UDP and answer a TCP request with a reply FFmpeg rejects.
TRANSPORTS = ("tcp", "udp")
OPEN_TIMEOUT_MS = 5000
BROWSER = "browser://"  # the start of the address of a camera a browser films for (PushedCamera)
_options_lock = threading.Lock()


@dataclass(frozen=True)
class Frame:
    image: np.ndarray
    seq: int
    captured_at: float  # time.monotonic()
    signature: bytes    # equal for bit-identical frames, i.e. a stream repeating itself


def open_capture(source: str, transport: str = "tcp") -> cv2.VideoCapture:
    if source.isdigit():
        return cv2.VideoCapture(int(source))
    timeouts = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000]
    with _options_lock:  # the options are process-wide and read while the capture opens
        _set_ffmpeg_options(f"rtsp_transport;{transport}")
        return cv2.VideoCapture(source, cv2.CAP_FFMPEG, timeouts)


def _set_ffmpeg_options(options: str) -> None:
    """OpenCV reads FFmpeg's options from the environment each time a capture opens.

    On Windows its FFmpeg plugin is a MinGW build that reads msvcrt.dll's own copy of the
    environment, which os.environ doesn't update, so the options are set there too.
    """
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options
    if sys.platform == "win32":
        ctypes.cdll.msvcrt._putenv(f"OPENCV_FFMPEG_CAPTURE_OPTIONS={options}".encode())


def is_rtsp(source: str) -> bool:
    return source.lower().startswith("rtsp")


def is_browser(source: str) -> bool:
    return source.startswith(BROWSER)


def new_browser_source() -> str:
    """An address of its own for a camera a browser films for: cameras with one address share one picture."""
    return f"{BROWSER}{secrets.token_hex(4)}"


def redact(source: str) -> str:
    """The source without its password, for logs."""
    return re.sub(r"//[^/@]*@", "//***@", source)


def crop(image: np.ndarray, part: str) -> np.ndarray:
    """The whole picture, or its "top" or "bottom" half.

    Two-lens cameras send both lenses in one picture, one above the other; each half is one lens.
    """
    half = image.shape[0] // 2
    if part == "top":
        return image[:half]
    if part == "bottom":
        return image[half:]
    return image


def part_of(frame: Frame, part: str) -> Frame:
    if not part:
        return frame
    image = crop(frame.image, part)
    return Frame(image, frame.seq, frame.captured_at, _signature(image))


def parse_region(region: str) -> tuple[float, float, float, float] | None:
    """A rectangle (x, y, w, h) in fractions of the picture (0 to 1), or None for the whole picture.

    Out-of-range values are clamped; the whole picture, an empty box or anything unparseable is None.
    """
    try:
        x, y, w, h = (float(n) for n in region.split(","))
    except (ValueError, AttributeError):
        return None
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    w, h = min(max(w, 0.0), 1.0 - x), min(max(h, 0.0), 1.0 - y)
    if w <= 0.0 or h <= 0.0 or (x, y, w, h) == (0.0, 0.0, 1.0, 1.0):
        return None
    return x, y, w, h


def region_text(region: str) -> str:
    """The region cleaned up for storage, or "" for the whole picture."""
    box = parse_region(region)
    return "" if box is None else ",".join(f"{value:.4f}" for value in box)


def masked(frame: Frame, region: str) -> Frame:
    """The frame with everything outside `region` blacked out, so the AI only looks inside it.

    The picture shown and recorded stays the whole frame; only the copy the models see is masked, and it keeps
    the frame's size, so boxes the models find still line up with the whole picture.
    """
    box = parse_region(region)
    if box is None:
        return frame
    height, width = frame.image.shape[:2]
    x0, y0 = round(box[0] * width), round(box[1] * height)
    x1, y1 = round((box[0] + box[2]) * width), round((box[1] + box[3]) * height)
    image = np.zeros_like(frame.image)
    image[y0:y1, x0:x1] = frame.image[y0:y1, x0:x1]
    return Frame(image, frame.seq, frame.captured_at, _signature(image))


def _signature(image: np.ndarray) -> bytes:
    return hashlib.blake2b(image[::8, ::8].tobytes(), digest_size=16).digest()


class FrameSource:
    """Keeps only the newest frame: always taking it keeps the system reacting to what is in front of it now."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._latest: Frame | None = None
        self.ended = False

    def next_frame(self, after_seq: int, timeout: float) -> Frame | None:
        """The newest frame if it is newer than `after_seq`, waiting up to `timeout` seconds for one."""
        with self._cond:
            self._cond.wait_for(
                lambda: self.ended or (self._latest is not None and self._latest.seq > after_seq), timeout
            )
            frame = self._latest
        return frame if frame is not None and frame.seq > after_seq else None

    def _keep(self, image: np.ndarray) -> None:
        signature = _signature(image)
        with self._cond:
            seq = self._latest.seq + 1 if self._latest else 1
            self._latest = Frame(image, seq, time.monotonic(), signature)
            self._cond.notify_all()

    def _end(self) -> None:
        with self._cond:
            self.ended = True
            self._cond.notify_all()


class PushedCamera(FrameSource):
    """A camera with nothing to connect to: a browser sends its pictures, one JPEG at a time (web/kiosk.py)."""

    def start(self) -> PushedCamera:
        return self

    def stop(self) -> None:
        self._end()

    def push(self, jpeg: bytes) -> bool:
        """Takes a picture; False when it isn't one."""
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
        if image is None:
            return False
        self._keep(image)
        return True


class Camera(FrameSource):
    """Reads frames on a background thread.

    Cheap cameras buffer seconds of video, which FrameSource leaves behind. Live streams reconnect on their
    own. Video files play at their own frame rate and either loop or end the stream at EOF, which makes them
    handy for tests.
    """

    def __init__(self, source: str, reconnect_delay_s: float = 2.0, loop: bool = False) -> None:
        super().__init__()
        self._source = source
        self._is_file = os.path.isfile(source)
        self._loop = loop
        self._reconnect_delay_s = reconnect_delay_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self) -> Camera:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        transport = TRANSPORTS[0]
        while not self._stop.is_set():
            capture = open_capture(self._source, transport)
            if capture.isOpened():
                log.info("camera connected: %s%s", redact(self._source), " over UDP" if transport == "udp" else "")
                self._read_until_failure(capture)
            elif is_rtsp(self._source):  # the camera may only stream over the other transport
                transport = "udp" if transport == "tcp" else "tcp"
            capture.release()
            if self._stop.is_set() or (self._is_file and not self._loop):
                break
            if self._is_file:
                continue
            log.warning("camera unavailable: %s; retrying in %.0fs", redact(self._source), self._reconnect_delay_s)
            self._stop.wait(self._reconnect_delay_s)
        self._end()

    def _read_until_failure(self, capture: cv2.VideoCapture) -> None:
        period = 1.0 / (capture.get(cv2.CAP_PROP_FPS) or 25.0) if self._is_file else 0.0
        while not self._stop.is_set():
            ok, image = capture.read()
            if not ok:
                break
            self._keep(image)
            if period:
                self._stop.wait(period)


def probe(source: str, seconds: float = 3.0) -> tuple[dict[str, object], np.ndarray | None]:
    """Stream properties, as reported and as measured over `seconds`, plus the last frame read."""
    for transport in (TRANSPORTS if is_rtsp(source) else TRANSPORTS[:1]):
        started = time.monotonic()
        capture = open_capture(source, transport)
        if capture.isOpened():
            break
        capture.release()
        if time.monotonic() - started > OPEN_TIMEOUT_MS / 2000:
            break  # the camera didn't answer, and the other transport won't change that
    if not capture.isOpened():
        return {"opened": False}, None
    fourcc = int(capture.get(cv2.CAP_PROP_FOURCC)) & 0xFFFFFFFF
    info: dict[str, object] = {
        "opened": True,
        "size": f"{int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}",
        "codec": fourcc.to_bytes(4, "little").decode(errors="replace").strip("\x00 ") or "?",
        "transport": transport.upper() if is_rtsp(source) else "",
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
