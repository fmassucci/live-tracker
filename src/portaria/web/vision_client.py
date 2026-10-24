"""Reads the recognition service's local port (see portaria.preview): status, frames and live streams."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator

from portaria.config import PreviewConfig


class VisionClient:
    def __init__(self, preview: PreviewConfig) -> None:
        host = "127.0.0.1" if preview.host in ("0.0.0.0", "", "::") else preview.host
        self._base = f"http://{host}:{preview.port}"

    def status(self) -> dict | None:
        """None when the recognition service isn't running."""
        try:
            with urllib.request.urlopen(f"{self._base}/status.json", timeout=1.0) as response:
                return json.load(response)
        except (OSError, ValueError):
            return None

    def camera_status(self, camera_id: int) -> dict | None:
        status = self.status()
        return status.get("cameras", {}).get(str(camera_id)) if status else None

    def frame(self, channel: str) -> bytes | None:
        try:
            with urllib.request.urlopen(f"{self._base}/{channel}.jpg", timeout=3.0) as response:
                return response.read()
        except OSError:
            return None

    def stream(self, channel: str) -> Iterator[bytes] | None:
        """The channel's MJPEG stream as raw chunks, or None when it isn't available."""
        try:
            response = urllib.request.urlopen(f"{self._base}/{channel}.mjpg", timeout=30.0)
        except OSError:
            return None

        def chunks() -> Iterator[bytes]:
            with response:
                try:
                    while chunk := response.read1(65536):
                        yield chunk
                except OSError:
                    return

        return chunks()
