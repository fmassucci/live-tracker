"""The recognition service's local HTTP port: the latest frame of each channel, and a status document.

The web UI reads it and passes it on to signed-in users; it listens on localhost by default.
Frames are kept as they arrive and only JPEG-encoded when someone asks for them.

    /status.json        cameras, states and counters
    /<channel>.jpg      latest frame
    /<channel>.mjpg     live MJPEG stream
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

log = logging.getLogger(__name__)

_FRAME_PATH = re.compile(r"^/([\w-]+)\.(jpg|mjpg)$")


class PreviewServer:
    def __init__(self, host: str, port: int) -> None:
        self._cond = threading.Condition()
        self._frames: dict[str, tuple[int, np.ndarray]] = {}
        self._jpegs: dict[str, tuple[int, bytes]] = {}
        self._status: dict = {}
        self._closing = False
        self._httpd = ThreadingHTTPServer((host, port), _handler(self))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="preview", daemon=True)

    def start(self) -> None:
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        log.info("frames and status at http://%s:%d", host, port)

    def stop(self) -> None:
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        self._httpd.shutdown()
        self._httpd.server_close()

    def publish(self, channel: str, image: np.ndarray) -> None:
        with self._cond:
            seq = self._frames[channel][0] + 1 if channel in self._frames else 1
            self._frames[channel] = (seq, image)
            self._cond.notify_all()

    def set_status(self, status: dict) -> None:
        with self._cond:
            self._status = status

    def status(self) -> dict:
        with self._cond:
            return self._status

    def jpeg(self, channel: str, after_seq: int = 0, timeout: float = 5.0) -> tuple[int, bytes] | None:
        """The channel's newest frame as JPEG, waiting up to `timeout` for one newer than `after_seq`.

        Returns the current frame again when nothing newer arrives, and None when the channel doesn't
        exist or the server is closing.
        """

        def current_seq() -> int:
            return self._frames[channel][0] if channel in self._frames else 0

        with self._cond:
            self._cond.wait_for(lambda: self._closing or current_seq() > after_seq, timeout)
            if self._closing or channel not in self._frames:
                return None
            seq, image = self._frames[channel]
            cached = self._jpegs.get(channel)
            if cached and cached[0] == seq:
                return cached
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return None
        encoded = (seq, buffer.tobytes())
        with self._cond:
            self._jpegs[channel] = encoded
        return encoded


def _handler(server: PreviewServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/status.json":
                self._reply(json.dumps(server.status()).encode(), "application/json")
                return
            match = _FRAME_PATH.match(self.path)
            if not match:
                self.send_error(404)
            elif match.group(2) == "jpg":
                frame = server.jpeg(match.group(1), timeout=2.0)
                if frame:
                    self._reply(frame[1], "image/jpeg")
                else:
                    self.send_error(404, "no such camera, or no frame yet")
            else:
                self._stream(match.group(1))

        def _reply(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, channel: str) -> None:
            first = server.jpeg(channel, timeout=2.0)
            if not first:
                self.send_error(404, "no such camera, or no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            frame: tuple[int, bytes] | None = first
            try:
                while frame:
                    seq, jpeg = frame
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg))
                    self.wfile.write(jpeg + b"\r\n")
                    frame = server.jpeg(channel, after_seq=seq)
                    while frame and frame[0] == seq:  # no new frame for a while: keep waiting
                        frame = server.jpeg(channel, after_seq=seq)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep the console for recognition logs

    return Handler
