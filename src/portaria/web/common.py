"""Shared pieces of the web UI: services, the sign-in check, page rendering, messages and images."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from portaria.config import Config
from portaria.db import Camera, Database, get_setting
from portaria.faces import BLURRY, NO_FACE, SEVERAL_FACES, TOO_SMALL, TURNED_AWAY
from portaria.i18n import Translator
from portaria.images import read_image
from portaria.people import People
from portaria.users import user_exists
from portaria.web.vision_client import VisionClient

ASSET_VERSION = "10"  # bump to make browsers reload the stylesheet and scripts
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
KINDS = {"recognized": "Recognized", "unknown": "Not recognized", "spoof": "Photo or screen suspected"}
DOOR_RESULTS = {"opened": "door opened", "shadow": "test mode, door stayed closed", "kept": "door stayed closed"}

# What a camera is doing, as its chip says it. Door cameras record a photo of each entry, never video.
RECORDING_STATES = {
    "recording": "Recording",
    "live": "Live, not recording",
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
    return "recording" if camera.role == "door" else "live"


@dataclass
class Services:
    cfg: Config
    db: Database
    people: People
    vision: VisionClient
    templates: Jinja2Templates


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
            "asset_version": ASSET_VERSION,
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
