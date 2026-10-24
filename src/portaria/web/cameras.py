"""Cameras: list with live status, add and edit, connection test, network search, and the video passed on
from the recognition service to signed-in users."""

from __future__ import annotations

import base64

import cv2
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select

from portaria import onvif
from portaria.camera import crop, probe, redact
from portaria.db import CAMERAS_VERSION, Camera, audit, bump
from portaria.web.common import (
    KINDS, RECORDING_STATES, flash, fragment, page, recording_state, require_user, safe_next, services, translator,
)

router = APIRouter(dependencies=[Depends(require_user)])

ANALYSES = {"faces": "Face recognition"}  # see Camera.analysis
DEVICES = {"": "None", "door": "Door"}  # see Camera.device
PARTS = {"top": "Top half", "bottom": "Bottom half", "": "Whole picture"}  # see Camera.part
MJPEG = "multipart/x-mixed-replace; boundary=frame"


@router.get("/cameras", response_class=HTMLResponse)
def camera_list(request: Request) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:
        cameras = session.scalars(select(Camera).order_by(Camera.device.desc(), Camera.name)).all()  # doors first
    status = s.vision.status()
    statuses = status.get("cameras", {}) if status else {}
    states = {c.id: recording_state(c, statuses.get(str(c.id)), status is not None) for c in cameras}
    return page(request, "cameras.html", section="cameras", cameras=cameras, statuses=statuses, states=states,
                service_up=status is not None, analyses=ANALYSES)


@router.get("/cameras/new", response_class=HTMLResponse)
def camera_new(request: Request) -> HTMLResponse:
    form = {"analysis": "faces", "device": ""}
    return page(request, "camera_form.html", section="cameras", camera=None, form=form, analyses=ANALYSES,
                devices=DEVICES, error="")


@router.post("/cameras/new", response_model=None)
def camera_create(
    request: Request, name: str = Form(""), url: str = Form(""), analysis: str = Form("faces"),
    device: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    error = _camera_error(name, url, analysis, device)
    if error:
        form = {"name": name, "url": url, "analysis": analysis, "device": device}
        return page(request, "camera_form.html", section="cameras", camera=None, form=form, analyses=ANALYSES,
                    devices=DEVICES, error=error, status_code=400)
    with services(request).db.session() as session:
        camera = Camera(name=name.strip(), url=url.strip(), analysis=analysis, device=device,
                        enabled=True)  # starts recording right away
        session.add(camera)
        bump(session, CAMERAS_VERSION)
        audit(session, request.session["user"], "camera added", name.strip())
    flash(request, "Camera {name} added.", name=name.strip())
    return RedirectResponse("/cameras", status_code=303)


@router.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_edit(request: Request, camera_id: int) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:
        camera = _get(session, camera_id)
    form = {"name": camera.name, "url": redact(camera.url), "analysis": camera.analysis, "device": camera.device,
            "part": camera.part}
    status = s.vision.status()
    camera_status = status.get("cameras", {}).get(str(camera_id)) if status else None
    state = recording_state(camera, camera_status, status is not None)
    return page(request, "camera_form.html", section="cameras", camera=camera, form=form, analyses=ANALYSES,
                devices=DEVICES, parts=PARTS, state=state,
                two_lenses=not camera.part and _taller_than_wide(camera_status), error="")


@router.post("/cameras/{camera_id}", response_model=None)
def camera_update(
    request: Request, camera_id: int, name: str = Form(""), url: str = Form(""), analysis: str = Form("faces"),
    device: str = Form(""), part: str | None = Form(None),
) -> HTMLResponse | RedirectResponse:
    with services(request).db.session() as session:
        camera = _get(session, camera_id)
        url = _keep_password(camera, url)
        part = camera.part if part not in PARTS else part  # the choice is only shown for a camera already split
        error = _camera_error(name, url, analysis, device)
        if error:
            form = {"name": name, "url": redact(url), "analysis": analysis, "device": device, "part": part}
            return page(request, "camera_form.html", section="cameras", camera=camera, form=form, analyses=ANALYSES,
                        devices=DEVICES, parts=PARTS, state=None, error=error, status_code=400)
        camera.name, camera.url, camera.part = name.strip(), url.strip(), part
        camera.analysis, camera.device = analysis, device
        bump(session, CAMERAS_VERSION)
        audit(session, request.session["user"], "camera changed", camera.name)
    flash(request, "Changes saved.")
    return RedirectResponse("/cameras", status_code=303)


@router.post("/cameras/{camera_id}/split")
def camera_split(request: Request, camera_id: int) -> RedirectResponse:
    """One camera for each lens of a two-lens camera, which sends both in one picture, one above the other.

    Both keep the same address: the recognition service still opens the camera once. Both record with
    the camera's analysis; only the first keeps its device, so a door opens once and a person seen by
    both lenses is one entry, not two.
    """
    t = translator(request)
    with services(request).db.session() as session:
        camera = _get(session, camera_id)
        if camera.part:
            return RedirectResponse(f"/cameras/{camera_id}", status_code=303)
        name = camera.name[:70]
        camera.name, camera.part = t("{name} (top)", name=name), "top"
        session.add(Camera(name=t("{name} (bottom)", name=name), url=camera.url, analysis=camera.analysis,
                           device="", part="bottom", enabled=camera.enabled))
        bump(session, CAMERAS_VERSION)
        audit(session, request.session["user"], "camera split", name)
    flash(request, "{name} is now two cameras, one for each lens.", name=name)
    return RedirectResponse("/cameras", status_code=303)


@router.post("/cameras/{camera_id}/start")
def camera_start(request: Request, camera_id: int, next: str = Form("/cameras")) -> RedirectResponse:
    return _switch(request, camera_id, True, next)


@router.post("/cameras/{camera_id}/stop")
def camera_stop(request: Request, camera_id: int, next: str = Form("/cameras")) -> RedirectResponse:
    return _switch(request, camera_id, False, next)


def _switch(request: Request, camera_id: int, on: bool, next: str) -> RedirectResponse:
    """Starts or stops a camera. The recognition service picks it up within a second."""
    with services(request).db.session() as session:
        camera = _get(session, camera_id)
        camera.enabled = on
        bump(session, CAMERAS_VERSION)
        audit(session, request.session["user"], "camera started" if on else "camera stopped", camera.name)
        name, door = camera.name, camera.device == "door"
    if on:
        flash(request, "Recording started: {name}.", name=name)
    elif door:
        flash(request, "Recording stopped: {name}. Nobody is recognized at this door until you start it again.",
              "warn", name=name)
    else:
        flash(request, "Recording stopped: {name}.", "warn", name=name)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/cameras/{camera_id}/delete")
def camera_delete(request: Request, camera_id: int) -> RedirectResponse:
    with services(request).db.session() as session:
        camera = _get(session, camera_id)
        name = camera.name
        session.delete(camera)
        bump(session, CAMERAS_VERSION)
        audit(session, request.session["user"], "camera deleted", name)
    flash(request, "Camera {name} removed. Its past records are kept.", name=name)
    return RedirectResponse("/cameras", status_code=303)


@router.post("/cameras-test", response_class=HTMLResponse)
def camera_test(
    request: Request, url: str = Form(""), camera_id: str = Form(""), part: str = Form("")
) -> HTMLResponse:
    """Opens the address for a moment and shows what came back, before the camera is saved."""
    if camera_id.isdigit():
        with services(request).db.session() as session:
            camera = session.get(Camera, int(camera_id))
            if camera:
                url = _keep_password(camera, url)
    if not url.strip():
        return fragment(request, "_camera_test.html", info=None, image=None, error="Write the camera address first.")
    info, image = probe(url.strip(), seconds=2.0)
    if not info["opened"]:
        return fragment(request, "_camera_test.html", info=None, image=None,
                        error="No image from this address. Check the address, user and password.")
    preview = None
    if image is not None:
        image = crop(image, part)
        info = {**info, "size": f"{image.shape[1]}x{image.shape[0]}"}
        scale = min(1.0, 480 / image.shape[1])
        ok, buffer = cv2.imencode(".jpg", cv2.resize(image, None, fx=scale, fy=scale), [cv2.IMWRITE_JPEG_QUALITY, 80])
        preview = base64.b64encode(buffer.tobytes()).decode() if ok else None
    return fragment(request, "_camera_test.html", info=info, image=preview, error="")


@router.post("/cameras-search", response_class=HTMLResponse)
def camera_search(request: Request, user: str = Form(""), password: str = Form("")) -> HTMLResponse:
    """Asks the network for ONVIF cameras and, with a user, for their video addresses."""
    found = []
    for ip, device_urls in onvif.discover().items():
        streams, error = [], ""
        if user:
            try:
                streams = onvif.stream_urls(device_urls[0], user, password)
            except OSError:
                error = "The camera refused the user or password."
        found.append({"ip": ip, "streams": [(n, _with_login(u, user, password)) for n, u in streams], "error": error})
    return fragment(request, "_camera_search.html", found=found, searched_with_user=bool(user))


@router.get("/cameras-states")
def camera_states(request: Request) -> JSONResponse:
    """Every camera's recording state, so open pages follow a camera that starts, stops or reconnects."""
    s = services(request)
    with s.db.session() as session:
        cameras = session.scalars(select(Camera)).all()
    status = s.vision.status()
    statuses = status.get("cameras", {}) if status else {}
    t = translator(request)
    states = {}
    for camera in cameras:
        state = recording_state(camera, statuses.get(str(camera.id)), status is not None)
        states[str(camera.id)] = {"state": state, "label": t(RECORDING_STATES[state])}
    return JSONResponse(states)


@router.get("/cameras/{camera_id}/state")
def camera_state(request: Request, camera_id: int) -> JSONResponse:
    """What the door decided just now, for the live frame: green with the name, or red."""
    status = services(request).vision.camera_status(camera_id)
    state = status.get("state") if status and status.get("online") else None
    t = translator(request)
    if state == "recognized":
        return JSONResponse({"tone": "ok", "label": t("{name} recognized", name=status.get("person_name") or "")})
    if state in KINDS and state != "recognized":
        return JSONResponse({"tone": "bad", "label": t(KINDS[state])})
    return JSONResponse({"tone": "idle", "label": ""})


@router.get("/cameras/{camera_id}/frame.jpg")
def camera_frame(request: Request, camera_id: int) -> Response:
    jpeg = services(request).vision.frame(f"cam{camera_id}")
    if jpeg is None:
        raise HTTPException(404)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/cameras/{camera_id}/stream")
def camera_stream(request: Request, camera_id: int) -> Response:
    chunks = services(request).vision.stream(f"cam{camera_id}")
    if chunks is None:
        raise HTTPException(404)
    return StreamingResponse(chunks, media_type=MJPEG, headers={"Cache-Control": "no-store"})


def _get(session, camera_id: int) -> Camera:
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(404)
    return camera


def _camera_error(name: str, url: str, analysis: str, device: str) -> str:
    if not name.strip():
        return "Give the camera a name, such as Staff entrance."
    if not url.strip():
        return "Write the camera address."
    if analysis not in ANALYSES or device not in DEVICES:
        return "Choose the AI model and the device attached to the camera."
    return ""


def _taller_than_wide(status: dict | None) -> bool:
    """Two-lens cameras send both lenses in one picture, one above the other, which is taller than wide."""
    width, _, height = (status or {}).get("size", "").partition("x")
    return width.isdigit() and height.isdigit() and int(height) > int(width)


def _keep_password(camera: Camera, url: str) -> str:
    """The edit form shows the address without its password; unchanged, it means the stored address."""
    return camera.url if url.strip() == redact(camera.url) else url


def _with_login(url: str, user: str, password: str) -> str:
    """ONVIF returns addresses without the login; the camera needs it to send video."""
    if not user or "@" in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{user}:{password}@{rest}"
