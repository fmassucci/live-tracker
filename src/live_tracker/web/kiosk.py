"""The recognition screen: a full-screen page for a tablet beside a face camera, or that films with its own.

It needs no sign-in, only the secret address from Settings, and shows first names only: anyone at the
camera can read it. On a camera a browser films for, the page shows the device's own camera and sends its pictures
here, which go on to the recognition service like any camera's: the same address that shows the screen feeds it.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from live_tracker.camera import is_browser
from live_tracker.db import SCREEN_PIN, Camera, Person, audit, get_setting
from live_tracker.faces import BLURRY, TOO_SMALL, TURNED_AWAY
from live_tracker.i18n import Translator
from live_tracker.pipeline import FROZEN
from live_tracker.users import verify_password
from live_tracker.web.common import branding, services

router = APIRouter()

ADJUST = {
    TOO_SMALL: "Come a little closer",
    TURNED_AWAY: "Look straight at the camera",
    BLURRY: "Hold still for a moment",
    FROZEN: "One moment",
}
MAX_FRAME_BYTES = 2_000_000  # far more than one picture from a tablet takes
THIS_COMPUTER = ("127.0.0.1", "localhost")  # browsers allow the camera here over plain http too


@router.get("/screen", response_class=HTMLResponse)
def recognition_screen(request: Request, camera: int, key: str, lang: str = "") -> Response:
    s = services(request)
    row = _check(request, camera, key)
    https_port = s.cfg.web.https_port
    if is_browser(row.url) and https_port and request.url.scheme == "http" and request.url.hostname not in THIS_COMPUTER:
        return RedirectResponse(str(request.url.replace(scheme="https", port=https_port)), status_code=307)
    t = Translator(lang or s.cfg.web.language)
    return s.templates.TemplateResponse(request, "kiosk.html", {
        "t": t, "lang": t.language, "brand": branding(s), "camera": camera, "camera_name": row.name, "key": key,
        "own_camera": is_browser(row.url), "pin_set": bool(_stored_pin(request)),
    })


@router.get("/screen/state")
def screen_state(request: Request, camera: int, key: str, lang: str = "") -> JSONResponse:
    s = services(request)
    _check(request, camera, key)
    t = Translator(lang or s.cfg.web.language)
    status = s.vision.camera_status(camera)
    if not status or not status.get("online"):
        tone, title, detail = "bad", t("The recognition system is not working"), t("Use your key and tell the manager.")
    elif status["state"] == "adjust":
        tone, title, detail = "adjust", t(ADJUST.get(status.get("problem", ""), "One moment")), ""
    elif status["state"] == "checking":
        tone, title, detail = "busy", t("Checking..."), ""
    elif status["state"] == "recognized":
        tone, title, detail = "ok", t("Hello, {name}!", name=_first_name(status.get("person_name"))), _access_text(request, status, t)
    elif status["state"] in ("unknown", "spoof"):
        tone, title, detail = "bad", t("Not recognized"), t("Try again, or call the manager.")
    else:
        tone, title, detail = "idle", t("Look at the camera"), ""
    return JSONResponse({"tone": tone, "title": title, "detail": detail})


@router.get("/screen/stream")
def screen_stream(request: Request, camera: int, key: str) -> Response:
    _check(request, camera, key)
    chunks = services(request).vision.stream(f"cam{camera}-kiosk")
    if chunks is None:
        raise HTTPException(404)
    return StreamingResponse(chunks, media_type="multipart/x-mixed-replace; boundary=frame")


@router.post("/screen/frame")
async def screen_frame(request: Request, camera: int, key: str) -> Response:
    """A picture from the page's own camera. The recognition service refuses it unless a browser films for this
    camera, so the address can't stand in for an IP camera's picture."""
    await run_in_threadpool(_check, request, camera, key)
    jpeg = b""
    async for chunk in request.stream():
        jpeg += chunk
        if len(jpeg) > MAX_FRAME_BYTES:
            raise HTTPException(413)
    if not await run_in_threadpool(services(request).vision.send_frame, f"cam{camera}", jpeg):
        raise HTTPException(503)
    return Response(status_code=204)


@router.post("/screen/unlock")
def screen_unlock(request: Request, camera: int, key: str, pin: str = Form("")) -> Response:
    """Whether the PIN set in Settings lets the screen stay out of full screen. It's checked here, so the page never
    holds it. What this guards is the page's own way out: no page can stop the device's back button."""
    s = services(request)
    row = _check(request, camera, key)
    stored = _stored_pin(request)
    if not stored:
        raise HTTPException(404)
    if s.pin_tries.waiting():
        raise HTTPException(429)
    if not verify_password(pin, stored):
        s.pin_tries.failed()
        raise HTTPException(403)
    s.pin_tries.passed()
    with s.db.session() as session:
        audit(session, "recognition screen", "left full screen", row.name)
    return Response(status_code=204)


def _stored_pin(request: Request) -> str:
    with services(request).db.session() as session:
        return get_setting(session, SCREEN_PIN) or ""


def _check(request: Request, camera_id: int, key: str) -> Camera:
    """The camera, if the address is valid."""
    with services(request).db.session() as session:
        expected = get_setting(session, "screen_key") or ""
        camera = session.get(Camera, camera_id)
        if not expected or not secrets.compare_digest(key, expected) or camera is None:
            raise HTTPException(404)
        return camera


def _access_text(request: Request, status: dict, t: Translator) -> str:
    person = status.get("person")
    with services(request).db.session() as session:
        row = session.get(Person, int(person)) if person and str(person).isdigit() else None
        allowed = bool(row and row.access)
    if not allowed:
        return t("You don't have access here. Please call the manager.")
    if services(request).cfg.devices.mode == "shadow":
        return t("Recognized. Test mode: nothing is switched yet.")
    return t("Access granted.")


def _first_name(name: str | None) -> str:
    return (name or "").split(" ")[0]
