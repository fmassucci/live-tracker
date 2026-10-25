"""Cameras: list with live status, add and edit, connection test, network search, and the video passed on
from the recognition service to signed-in users."""

from __future__ import annotations

import base64
from dataclasses import replace

import cv2
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select

from portaria import onvif
from portaria.camera import crop, probe, redact
from portaria.db import CAMERAS_VERSION, Camera, audit, bump
from portaria.falls import FallSettings
from portaria.fire import NAMES as FIRE_NAMES
from portaria.fire import FireSettings
from portaria.i18n import Translator
from portaria.models import CATALOG, file_state, files_of, model_state
from portaria.objects import COMMON, DETECTORS, GROUPS, NAMES, SENSITIVITY, ObjectSettings
from portaria.recording import APPEAR, DAILY, DEFAULT_EVERY_S, INTERVAL, MAX_EVERY_S, MIN_EVERY_S, MODES
from portaria.web.common import (
    KINDS, RECORDING_STATES, flash, fragment, page, recording_state, require_user, safe_next, services, translator,
)

router = APIRouter(dependencies=[Depends(require_user)])

ANALYSIS_NAMES = {"": "No AI model", **{model.key: model.name for model in CATALOG}}  # see Camera.analysis
# One line under each choice in the camera form: what the camera does with that model.
ANALYSIS_HINTS = {"": "Only shows the live picture.", "faces": "Recognizes the people registered in People.",
                  "objects": "Records what you choose, among 80 kinds of things.",
                  "fire": "Records fire and smoke that stay in the picture.",
                  "falls": "Records someone who stays lying on the floor."}
# The groups of kinds below the common ones, without them.
OTHER_GROUPS = tuple((group, tuple(k for k in keys if k not in COMMON)) for group, keys in GROUPS)
DEVICES = {"": "None", "door": "Door"}  # see Camera.device
# When a photo is kept (see Camera.record_mode), with a line under each choice; the interval's line depends on
# the model, so it is in the template.
RECORD_MODES = {APPEAR: "Each time it appears", INTERVAL: "At an interval", DAILY: "First time of the day"}
RECORD_HINTS = {APPEAR: "One photo when it appears. The next one only after it leaves the picture and comes back.",
                DAILY: "Only the first photo of each person or kind of thing, each day."}
UNITS = {"s": 1, "min": 60, "h": 3600}
UNIT_NAMES = {"s": "seconds", "min": "minutes", "h": "hours"}
PARTS = {"top": "Top half", "bottom": "Bottom half", "": "Whole picture"}  # see Camera.part
MJPEG = "multipart/x-mixed-replace; boundary=frame"


def _sent_settings(
    kinds: list[str] = Form([]),
    fire_confidence: float = Form(FireSettings.confidence), fire_seconds: float = Form(FireSettings.min_seconds),
    fire_fps: float = Form(FireSettings.fps), falls_confidence: float = Form(FallSettings.confidence),
    falls_seconds: float = Form(FallSettings.min_seconds), falls_fps: float = Form(FallSettings.fps),
) -> dict[str, ObjectSettings]:
    """The settings of fire and smoke, and of falls, as the camera form sent them, by model. Fire and smoke's
    kinds come in the form's "kinds" with those of people and objects: they never share a name."""
    fire = FireSettings.of({"kinds": kinds, "confidence": fire_confidence, "min_seconds": fire_seconds,
                            "fps": fire_fps})
    falls = FallSettings.of({"confidence": falls_confidence, "min_seconds": falls_seconds, "fps": falls_fps})
    return {"fire": replace(fire, kinds=tuple(k for k in dict.fromkeys(kinds) if k in FIRE_NAMES)), "falls": falls}


@router.get("/cameras", response_class=HTMLResponse)
def camera_list(request: Request) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:
        cameras = session.scalars(select(Camera).order_by(Camera.device.desc(), Camera.name)).all()  # doors first
    status = s.vision.status()
    statuses = status.get("cameras", {}) if status else {}
    states = {c.id: recording_state(c, statuses.get(str(c.id)), status is not None) for c in cameras}
    t = translator(request)
    return page(request, "cameras.html", section="cameras", cameras=cameras, statuses=statuses, states=states,
                service_up=status is not None, analyses=ANALYSIS_NAMES, rules={c.id: _rule(t, c) for c in cameras})


@router.get("/cameras/new", response_class=HTMLResponse)
def camera_new(request: Request) -> HTMLResponse:
    analyses = _analyses(request)
    form = {"analysis": "faces" if "faces" in analyses else "", "device": "", "record_mode": APPEAR,
            **_every_fields(DEFAULT_EVERY_S)}
    return page(request, "camera_form.html", section="cameras", camera=None, form=form, objects=ObjectSettings(),
                **_other_settings(), error="", **_choices(request, analyses))


@router.post("/cameras/new", response_model=None)
def camera_create(
    request: Request, name: str = Form(""), url: str = Form(""), analysis: str = Form(""), device: str = Form(""),
    kinds: list[str] = Form([]), confidence: float = Form(0.5), min_seconds: float = Form(2.0), fps: float = Form(4.0),
    objects_model: str = Form(""), record_mode: str = Form(APPEAR), every_value: str = Form(""),
    every_unit: str = Form("min"), others: dict[str, ObjectSettings] = Depends(_sent_settings),
) -> HTMLResponse | RedirectResponse:
    analyses = _analyses(request)
    objects = ObjectSettings.of({"kinds": kinds, "confidence": confidence, "min_seconds": min_seconds, "fps": fps,
                                 "model": objects_model})
    every_s = _every_s(every_value, every_unit)
    error = (_camera_error(name, url, analysis in analyses, device) or _objects_error(analysis, kinds)
             or _fire_error(analysis, others) or _record_error(record_mode, every_s))
    if error:
        form = {"name": name, "url": url, "analysis": analysis, "device": device, "record_mode": record_mode,
                "every_value": every_value, "every_unit": every_unit}
        return page(request, "camera_form.html", section="cameras", camera=None, form=form,
                    objects=_as_ticked(objects, kinds), **others, error=error, status_code=400,
                    **_choices(request, analyses))
    settings = objects if analysis == "objects" else others.get(analysis)
    with services(request).db.session() as session:
        camera = Camera(name=name.strip(), url=url.strip(), analysis=analysis, device=device,
                        model_settings=settings.dumps() if settings else "{}",
                        record_mode=record_mode, record_every_s=every_s or DEFAULT_EVERY_S,
                        enabled=True)  # starts recording right away
        session.add(camera)
        bump(session, CAMERAS_VERSION)
        audit(session, request.session["user"], "camera added", name.strip())
    flash(request, "Camera {name} added.", name=name.strip())
    return RedirectResponse("/cameras", status_code=303)


@router.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_edit(request: Request, camera_id: int) -> HTMLResponse:
    with services(request).db.session() as session:
        camera = _get(session, camera_id)
    form = {"name": camera.name, "url": redact(camera.url), "analysis": camera.analysis, "device": camera.device,
            "part": camera.part, "record_mode": camera.record_mode, **_every_fields(camera.record_every_s)}
    return page(request, "camera_form.html", section="cameras", camera=camera, form=form, parts=PARTS,
                objects=ObjectSettings.parse(camera.model_settings), **_other_settings(camera), error="",
                **_live(request, camera), **_choices(request, _analyses(request, camera.analysis)))


@router.post("/cameras/{camera_id}", response_model=None)
def camera_update(
    request: Request, camera_id: int, name: str = Form(""), url: str = Form(""), analysis: str = Form(""),
    device: str = Form(""), part: str | None = Form(None), kinds: list[str] = Form([]),
    confidence: float = Form(0.5), min_seconds: float = Form(2.0), fps: float = Form(4.0),
    objects_model: str = Form(""), record_mode: str = Form(APPEAR), every_value: str = Form(""),
    every_unit: str = Form("min"), others: dict[str, ObjectSettings] = Depends(_sent_settings),
) -> HTMLResponse | RedirectResponse:
    objects = ObjectSettings.of({"kinds": kinds, "confidence": confidence, "min_seconds": min_seconds, "fps": fps,
                                 "model": objects_model})
    every_s = _every_s(every_value, every_unit)
    with services(request).db.session() as session:
        camera = _get(session, camera_id)
        url = _keep_password(camera, url)
        part = camera.part if part not in PARTS else part  # the choice is only shown for a camera already split
        analyses = _analyses(request, camera.analysis)
        error = (_camera_error(name, url, analysis in analyses, device) or _objects_error(analysis, kinds)
                 or _fire_error(analysis, others) or _record_error(record_mode, every_s))
        if error:
            form = {"name": name, "url": redact(url), "analysis": analysis, "device": device, "part": part,
                    "record_mode": record_mode, "every_value": every_value, "every_unit": every_unit}
            return page(request, "camera_form.html", section="cameras", camera=camera, form=form, parts=PARTS,
                        objects=_as_ticked(objects, kinds), **others, error=error, status_code=400,
                        **_live(request, camera), **_choices(request, analyses))
        camera.name, camera.url, camera.part = name.strip(), url.strip(), part
        camera.analysis, camera.device = analysis, device
        camera.record_mode, camera.record_every_s = record_mode, every_s or camera.record_every_s
        settings = objects if analysis == "objects" else others.get(analysis)
        if settings:  # face recognition keeps the last ones, in case the camera comes back to them
            camera.model_settings = settings.dumps()
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
        name, door, records = camera.name, camera.device == "door", bool(camera.analysis)
    if not records:  # without an AI model, a camera only shows its picture
        flash(request, "Camera turned on: {name}." if on else "Camera turned off: {name}.", name=name)
    elif on:
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


def _camera_error(name: str, url: str, analysis_ok: bool, device: str) -> str:
    if not name.strip():
        return "Give the camera a name, such as Staff entrance."
    if not url.strip():
        return "Write the camera address."
    if not analysis_ok or device not in DEVICES:
        return "Choose the AI model and the device attached to the camera."
    return ""


def _objects_error(analysis: str, kinds: list[str]) -> str:
    if analysis == "objects" and not any(kind in NAMES for kind in kinds):
        return "Choose at least one thing to look for."
    return ""


def _fire_error(analysis: str, others: dict[str, ObjectSettings]) -> str:
    if analysis == "fire" and not others["fire"].kinds:
        return "Choose fire, smoke or both."
    return ""


def _other_settings(camera: Camera | None = None) -> dict[str, ObjectSettings]:
    """Fire and smoke's settings, and falls', for the camera form: the camera's own when it runs that model."""
    analysis, stored = (camera.analysis, camera.model_settings) if camera else ("", "")
    return {"fire": FireSettings.parse(stored) if analysis == "fire" else FireSettings(),
            "falls": FallSettings.parse(stored) if analysis == "falls" else FallSettings()}


def _every_s(value: str, unit: str) -> int | None:
    """The interval from the form, in seconds; None unless it is a whole number within the limits."""
    try:
        seconds = int(value) * UNITS[unit]
    except (ValueError, KeyError):
        return None
    return seconds if MIN_EVERY_S <= seconds <= MAX_EVERY_S else None


def _every_fields(seconds: int) -> dict[str, object]:
    """An interval as the form shows it, in the largest unit that keeps it whole: 900 is 15 minutes."""
    unit = next(u for u in ("h", "min", "s") if seconds % UNITS[u] == 0)
    return {"every_value": seconds // UNITS[unit], "every_unit": unit}


def _rule(t: Translator, camera: Camera) -> str:
    """When the camera keeps a photo, as its card on the Cameras page says it."""
    if camera.record_mode == INTERVAL:
        fields = _every_fields(camera.record_every_s)
        return t("Every {every}", every=f"{fields['every_value']}\u00a0{fields['every_unit']}")
    return t(RECORD_MODES.get(camera.record_mode, RECORD_MODES[APPEAR]))


def _record_error(mode: str, every_s: int | None) -> str:
    if mode not in MODES:
        return "Choose when to keep a photo."
    if mode == INTERVAL and every_s is None:
        return "Choose an interval between 5 seconds and 24 hours."
    return ""


def _as_ticked(objects: ObjectSettings, kinds: list[str]) -> ObjectSettings:
    """The settings as the form had them, even with nothing ticked, to show them again with an error."""
    return replace(objects, kinds=tuple(kind for kind in dict.fromkeys(kinds) if kind in NAMES))


def _live(request: Request, camera: Camera) -> dict[str, object]:
    """The camera's picture beside its settings, and how it is doing, as the recognition service sees it."""
    status = services(request).vision.status()
    camera_status = status.get("cameras", {}).get(str(camera.id)) if status else None
    return {"state": recording_state(camera, camera_status, status is not None), "camera_status": camera_status or {},
            "two_lenses": not camera.part and _taller_than_wide(camera_status)}


def _choices(request: Request, analyses: dict[str, tuple[str, bool]]) -> dict[str, object]:
    """What the camera form offers: the AI models, the devices, and the kinds of things to look for."""
    models_dir = services(request).cfg.paths.models_dir
    detectors = {key: (name, file_state(models_dir, file) == "ready") for key, (name, file) in DETECTORS.items()}
    return {"analyses": analyses, "hints": ANALYSIS_HINTS, "devices": DEVICES, "things": NAMES, "common": COMMON,
            "groups": OTHER_GROUPS, "sensitivity": SENSITIVITY, "record_modes": RECORD_MODES,
            "record_hints": RECORD_HINTS, "units": UNIT_NAMES, "fire_things": FIRE_NAMES, "detectors": detectors}


def _analyses(request: Request, current: str = "") -> dict[str, tuple[str, bool]]:
    """The AI models a camera can choose, as key -> (name, downloaded): none, or a downloaded model.

    The camera's current model stays in the list even when its files are gone, so saving keeps it. People and
    objects counts as downloaded with either of its models.
    """
    cfg = services(request).cfg
    choices = {"": ("None", True)}
    for model in CATALOG:
        ready = model.on_cameras and model_state(cfg.paths.models_dir, files_of(model, cfg.recognition.model)) == "ready"
        if model.key == "objects":
            ready = ready or any(file_state(cfg.paths.models_dir, file) == "ready" for _, file in DETECTORS.values())
        if ready or (model.key == current and current):
            choices[model.key] = (model.name, ready)
    return choices


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
