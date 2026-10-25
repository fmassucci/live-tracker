"""Shared pieces of the web UI: services, the sign-in check, page rendering, messages and images."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from live_tracker.config import Config
from live_tracker.db import Camera, Database, get_setting
from live_tracker.faces import BLURRY, NO_FACE, SEVERAL_FACES, TOO_SMALL, TURNED_AWAY
from live_tracker.i18n import Translator
from live_tracker.images import read_image
from live_tracker.network import local_ipv4
from live_tracker.people import People
from live_tracker.users import is_admin, user_exists
from live_tracker.web.vision_client import VisionClient

ASSET_VERSION = "38"  # bump to make browsers reload the stylesheet, scripts and icons
NOT_AN_IMAGE = "not an image"
IMAGE_CACHE_CONTROL = "private, max-age=86400"  # photos and snapshots never change once saved

# Why a photo was refused, as people read it.
PHOTO_PROBLEMS = {
    NO_FACE: "No face found. Use a photo where the face is clearly visible.",
    SEVERAL_FACES: "There is more than one person in the photo. Use a photo with only this person.",
    TOO_SMALL: "The face is too small. Use a closer photo.",
    TURNED_AWAY: "The face is turned too far to the side. Use a photo where the person looks at the camera.",
    BLURRY: "The photo is blurry. Use a sharper photo.",
    NOT_AN_IMAGE: "This file is not a photo the system can read. Use JPG or PNG.",
}

CATEGORIES = {"employee": "Employee", "contractor": "Contractor", "visitor": "Visitor"}
KINDS = {"recognized": "Recognized", "unknown": "Not recognized", "spoof": "Photo or screen suspected",
         "detected": "Detected"}  # "detected": a kind of thing the people-and-objects model saw
DOOR_RESULTS = {"opened": "door opened", "shadow": "test mode, door stayed closed", "kept": "door stayed closed"}

# What a camera is doing, as its chip says it. A camera with an AI model records a photo of what it finds,
# never video; one without a model only shows its picture.
RECORDING_STATES = {
    "recording": "Recording",
    "live": "Live, no AI model",
    "no-model": "AI model not downloaded",
    "stopped": "Stopped",
    "no-image": "No image, not recording",
    "service-down": "Recognition service stopped",
}


def recording_state(camera: Camera, status: dict | None, service_up: bool) -> str:
    """A key of RECORDING_STATES, from the camera's setting and its status in the recognition service."""
    if not camera.enabled:
        return "stopped"
    if not service_up:
        return "service-down"
    if not status or not status.get("online"):
        return "no-image"
    if not camera.analysis:
        return "live"
    return "no-model" if status.get("model_missing") else "recording"


@dataclass
class Services:
    cfg: Config
    db: Database
    people: People
    vision: VisionClient
    templates: Jinja2Templates
    stop_web: Callable[[], None] = lambda: None  # stops this web UI; `live-tracker web` sets it (cli.py)


class LoginRequired(Exception):
    pass


def services(request: Request) -> Services:
    return request.app.state.services


def require_user(request: Request) -> str:
    """The signed-in user. A session whose account was removed no longer counts."""
    user = request.session.get("user")
    if user:
        with services(request).db.session() as session:
            if user_exists(session, user):
                return user
        request.session.clear()
    raise LoginRequired()


def require_admin(request: Request) -> str:
    """The signed-in user, when they are an administrator."""
    user = require_user(request)
    with services(request).db.session() as session:
        if not is_admin(session, user):
            raise HTTPException(403)
    return user


def translator(request: Request) -> Translator:
    return Translator(request.session.get("lang") or services(request).cfg.web.language)


def flash(request: Request, text: str, tone: str = "ok", **values: object) -> None:
    """A message for the next page. `text` is English UI text, translated when it is shown."""
    messages = request.session.get("flash", [])
    messages.append({"text": text, "tone": tone, "values": {k: str(v) for k, v in values.items()}})
    request.session["flash"] = messages


def branding(s: Services) -> dict:
    with s.db.session() as session:
        name = get_setting(session, "display_name") or "Portaria"
        logo_version = get_setting(session, "logo_version")
    return {"name": name, "logo": f"/branding/logo.png?v={logo_version}" if logo_version else None}


def page(request: Request, template: str, *, section: str = "", status_code: int = 200, **context: object) -> HTMLResponse:
    """A full page: shows and clears the pending messages."""
    return _render(request, template, section, status_code, request.session.pop("flash", []), context)


def fragment(request: Request, template: str, **context: object) -> HTMLResponse:
    """Part of a page, refreshed in place; leaves pending messages for the next full page."""
    return _render(request, template, "", 200, [], context)


def _render(request: Request, template: str, section: str, status_code: int, flashes: list, context: dict) -> HTMLResponse:
    s = services(request)
    t = translator(request)
    return s.templates.TemplateResponse(
        request,
        template,
        {
            "t": t,
            "lang": t.language,
            "user": request.session.get("user"),
            "brand": branding(s),
            "section": section,
            "flashes": flashes,
            "door_mode": s.cfg.door.mode,
            "default_password": request.session.get("default_password", False),
            "hidden_notices": request.session.get("hidden_notices", []),
            "categories": CATEGORIES,
            "recording_states": RECORDING_STATES,
            **context,
        },
        status_code=status_code,
    )


def aspect(size: object) -> str:
    """CSS aspect ratio of a camera image from its "640x480" size; 16:9 until the camera reports one."""
    try:
        width, height = (int(v) for v in str(size or "").split("x"))
        return f"{width / height:.4f}"
    except (ValueError, ZeroDivisionError):
        return "1.7778"


def decode_upload(upload: UploadFile) -> np.ndarray | None:
    data = upload.file.read()
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def uploads(files: list[UploadFile]) -> list[UploadFile]:
    """The files actually chosen: browsers send one empty part when nothing was picked."""
    return [f for f in files if f.filename]


def image_response(path: Path, width: int | None) -> Response:
    """A JPEG from the data folder, optionally shrunk to `width` pixels for thumbnails."""
    if not path.is_file():
        raise HTTPException(404)
    headers = {"Cache-Control": IMAGE_CACHE_CONTROL}
    if not width:
        return FileResponse(path, media_type="image/jpeg", headers=headers)
    image = read_image(path)
    if image is None:
        raise HTTPException(404)
    width = max(48, min(width, 1600))
    if image.shape[1] > width:
        image = cv2.resize(image, (width, round(image.shape[0] * width / image.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return Response(buffer.tobytes(), media_type="image/jpeg", headers=headers)


def safe_next(target: str) -> str:
    """Only local paths, so a link can't send people to another site after signing in."""
    return target if target.startswith("/") and not target.startswith("//") else "/"


def web_addresses(cfg: Config) -> tuple[str, list[str]]:
    """The web UI's address on this computer, and the ones phones, tablets and other computers use.

    There are none for other devices when the UI only listens on this computer.
    """
    host, port = cfg.web.host, cfg.web.port
    if host == "0.0.0.0":
        return f"http://127.0.0.1:{port}", [f"http://{address}:{port}" for address in local_ipv4()]
    url = f"http://{host}:{port}"
    local_only = host.startswith("127.") or host == "localhost"
    return url, [] if local_only else [url]


def on_this_computer(request: Request) -> bool:
    """Whether the browser runs on this computer, opened by its loopback address or one of its network addresses."""
    host = request.client.host if request.client else ""
    return host == "::1" or host.startswith("127.") or host in local_ipv4()
