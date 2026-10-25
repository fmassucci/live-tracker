"""The door screen: a full-screen page for the tablet at the entrance.

It needs no sign-in, only the secret address from Settings, and shows first names only: anyone at the
door can read it.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from portaria.db import Camera, Person, get_setting
from portaria.faces import BLURRY, TOO_SMALL, TURNED_AWAY
from portaria.i18n import Translator
from portaria.pipeline import FROZEN
from portaria.web.common import branding, services

router = APIRouter()

ADJUST = {
    TOO_SMALL: "Come a little closer",
    TURNED_AWAY: "Look straight at the camera",
    BLURRY: "Hold still for a moment",
    FROZEN: "One moment",
}


@router.get("/door", response_class=HTMLResponse)
def door_screen(request: Request, camera: int, key: str, lang: str = "") -> HTMLResponse:
    s = services(request)
    name = _check(request, camera, key)
    t = Translator(lang or s.cfg.web.language)
    return s.templates.TemplateResponse(request, "kiosk.html", {
        "t": t, "lang": t.language, "brand": branding(s), "camera": camera, "camera_name": name, "key": key,
    })


@router.get("/door/state")
def door_state(request: Request, camera: int, key: str, lang: str = "") -> JSONResponse:
    s = services(request)
    _check(request, camera, key)
    t = Translator(lang or s.cfg.web.language)
    status = s.vision.camera_status(camera)
    if not status or not status.get("online"):
        tone, title, detail = "bad", t("The door system is not working"), t("Use your key and tell the manager.")
    elif status["state"] == "adjust":
        tone, title, detail = "adjust", t(ADJUST.get(status.get("problem", ""), "One moment")), ""
    elif status["state"] == "checking":
        tone, title, detail = "busy", t("Checking..."), ""
    elif status["state"] == "recognized":
        tone, title, detail = "ok", t("Hello, {name}!", name=_first_name(status.get("person_name"))), _door_text(request, status, t)
    elif status["state"] in ("unknown", "spoof"):
        tone, title, detail = "bad", t("Not recognized"), t("Try again, or call the manager.")
    else:
        tone, title, detail = "idle", t("Look at the camera to come in"), ""
    return JSONResponse({"tone": tone, "title": title, "detail": detail})


@router.get("/door/stream")
def door_stream(request: Request, camera: int, key: str) -> Response:
    _check(request, camera, key)
    chunks = services(request).vision.stream(f"cam{camera}-kiosk")
    if chunks is None:
        raise HTTPException(404)
    return StreamingResponse(chunks, media_type="multipart/x-mixed-replace; boundary=frame")


def _check(request: Request, camera_id: int, key: str) -> str:
    """The camera's name, if the address is valid."""
    with services(request).db.session() as session:
        expected = get_setting(session, "door_screen_key") or ""
        camera = session.get(Camera, camera_id)
        if not expected or not secrets.compare_digest(key, expected) or camera is None:
            raise HTTPException(404)
        return camera.name


def _door_text(request: Request, status: dict, t: Translator) -> str:
    person = status.get("person")
    with services(request).db.session() as session:
        row = session.get(Person, int(person)) if person and str(person).isdigit() else None
        allowed = bool(row and row.door_access)
    if not allowed:
        return t("The door doesn't open for you here. Please call the manager.")
    if services(request).cfg.door.mode == "shadow":
        return t("Recognized. Test mode: the door does not open yet.")
    return t("Come in.")


def _first_name(name: str | None) -> str:
    return (name or "").split(" ")[0]
