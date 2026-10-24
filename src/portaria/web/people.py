"""People: the registry every module builds on. List, add, edit, photos (uploaded or taken by a camera), delete."""

from __future__ import annotations

import time
from datetime import datetime

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select

from portaria.db import FACES_VERSION, Camera, Event, Person, Photo, audit, bump
from portaria.people import CATEGORIES
from portaria.web.common import (
    DOOR_RESULTS,
    KINDS,
    NOT_AN_IMAGE,
    PHOTO_PROBLEMS,
    decode_upload,
    flash,
    image_response,
    page,
    require_user,
    services,
    translator,
    uploads,
)

router = APIRouter(dependencies=[Depends(require_user)])

READY_PHOTOS = 3  # fewer photos than this still works, but less reliably
CAPTURE_PHOTOS = 5
CAPTURE_SECONDS = 15.0
PHOTO_SOURCES = {"upload": "Uploaded", "camera": "Taken by camera", "import": "Imported"}


@router.get("/people", response_class=HTMLResponse)
def people_list(request: Request, q: str = "", show: str = "active") -> HTMLResponse:
    with services(request).db.session() as session:
        query = select(Person).order_by(Person.name)
        if show != "all":
            query = query.where(Person.active)
        if q.strip():
            query = query.where(Person.name.ilike(f"%{q.strip()}%"))
        people = session.scalars(query).all()
        photos = dict(session.execute(select(Photo.person_id, func.count()).group_by(Photo.person_id)).all())
        avatars = dict(session.execute(select(Photo.person_id, func.min(Photo.id)).group_by(Photo.person_id)).all())
        last_seen = dict(session.execute(
            select(Event.person_id, func.max(Event.at)).where(Event.kind == "recognized").group_by(Event.person_id)
        ).all())
        total = session.scalar(select(func.count()).select_from(Person))
    return page(
        request, "people.html", section="people", people=people, photos=photos, avatars=avatars,
        last_seen=last_seen, q=q, show=show, total=total, ready=READY_PHOTOS,
    )


@router.get("/people/new", response_class=HTMLResponse)
def person_new(request: Request) -> HTMLResponse:
    return page(request, "person_new.html", section="people", form={}, error="")


@router.post("/people/new", response_model=None)
def person_create(
    request: Request,
    name: str = Form(""),
    category: str = Form("employee"),
    job_title: str = Form(""),
    notes: str = Form(""),
    door_access: bool = Form(False),
    informed: bool = Form(False),
    photos: list[UploadFile] = File(default=[]),
) -> HTMLResponse | RedirectResponse:
    files = uploads(photos)
    form = {"name": name, "category": category, "job_title": job_title, "notes": notes,
            "door_access": door_access, "informed": informed}
    error = _person_error(name, category) or (
        "Confirm that the person was told how their face is used before adding photos."
        if files and not informed else ""
    )
    if error:
        return page(request, "person_new.html", section="people", form=form, error=error, status_code=400)

    s = services(request)
    with s.db.session() as session:
        person = Person(
            name=name.strip(), category=category, job_title=job_title.strip(), notes=notes.strip(),
            door_access=door_access, informed_at=datetime.now() if informed else None,
        )
        session.add(person)
        session.flush()
        saved = _add_photos(request, session, person, files)
        audit(session, request.session["user"], "person added", f"{person.id} {person.name}, {saved} photos")
        person_id, person_name = person.id, person.name
    flash(request, "{name} was added. Photos saved: {count}.", name=person_name, count=saved)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@router.post("/people/check-photo")
def check_photo(request: Request, photo: UploadFile = File(...), person_id: int | None = Form(None)) -> JSONResponse:
    """Feedback while photos are being chosen: would this one be accepted? Nothing is saved."""
    t = translator(request)
    image = decode_upload(photo)
    if image is None:
        return JSONResponse({"ok": False, "message": t(PHOTO_PROBLEMS[NOT_AN_IMAGE])})
    s = services(request)
    with s.db.session() as session:
        check = s.people.inspect(session, image, person_id)
    if not check.ok:
        return JSONResponse({"ok": False, "message": t(PHOTO_PROBLEMS[check.problem])})
    if check.looks_like:
        message = t("Usable, but it also matches {name}, who is already registered.", name=check.looks_like)
        return JSONResponse({"ok": True, "warn": True, "message": message})
    return JSONResponse({"ok": True, "warn": False, "message": t("Good photo.")})


@router.get("/people/{person_id}", response_class=HTMLResponse)
def person_detail(request: Request, person_id: int) -> HTMLResponse:
    with services(request).db.session() as session:
        person = _get(session, person_id)
        photos = list(person.photos)
        events = session.scalars(
            select(Event).where(Event.person_id == person_id).order_by(Event.at.desc()).limit(12)
        ).all()
        cameras = session.scalars(select(Camera).where(Camera.enabled).order_by(Camera.role, Camera.name)).all()
        camera_names = {c.id: c.name for c in session.scalars(select(Camera))}
    form = {"name": person.name, "category": person.category, "job_title": person.job_title, "notes": person.notes,
            "door_access": person.door_access, "active": person.active}
    return page(
        request, "person.html", section="people", person=person, photos=photos, events=events, form=form,
        cameras=cameras, ready=READY_PHOTOS, sources=PHOTO_SOURCES, kinds=KINDS, door_results=DOOR_RESULTS,
        camera_names=camera_names,
    )


@router.post("/people/{person_id}")
def person_update(
    request: Request,
    person_id: int,
    name: str = Form(""),
    category: str = Form("employee"),
    job_title: str = Form(""),
    notes: str = Form(""),
    door_access: bool = Form(False),
    active: bool = Form(False),
    informed: bool = Form(False),
) -> RedirectResponse:
    error = _person_error(name, category)
    if error:
        flash(request, error, "bad")
        return RedirectResponse(f"/people/{person_id}#details", status_code=303)
    with services(request).db.session() as session:
        person = _get(session, person_id)
        person.name, person.category = name.strip(), category
        person.job_title, person.notes = job_title.strip(), notes.strip()
        person.door_access, person.active = door_access, active
        if informed and person.informed_at is None:
            person.informed_at = datetime.now()
        bump(session, FACES_VERSION)  # names and permissions are part of what recognition loads
        audit(session, request.session["user"], "person changed", f"{person.id} {person.name}")
    flash(request, "Changes saved.")
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@router.post("/people/{person_id}/informed")
def person_informed(request: Request, person_id: int) -> RedirectResponse:
    """Records that the person was told how their face is used (LGPD). Photos can be added after this."""
    with services(request).db.session() as session:
        person = _get(session, person_id)
        if person.informed_at is None:
            person.informed_at = datetime.now()
            audit(session, request.session["user"], "person informed", f"{person.id} {person.name}")
        name = person.name
    flash(request, "Recorded: {name} was informed.", name=name)
    return RedirectResponse(f"/people/{person_id}#photos", status_code=303)


@router.post("/people/{person_id}/photos")
def person_add_photos(
    request: Request, person_id: int, photos: list[UploadFile] = File(default=[]), informed: bool = Form(False)
) -> RedirectResponse:
    files = uploads(photos)
    with services(request).db.session() as session:
        person = _get(session, person_id)
        if not _may_add_photos(request, person, informed):
            return RedirectResponse(f"/people/{person_id}#photos", status_code=303)
        if not files:
            flash(request, "Choose at least one photo.", "warn")
            return RedirectResponse(f"/people/{person_id}#photos", status_code=303)
        saved = _add_photos(request, session, person, files)
        audit(session, request.session["user"], "photos added", f"{person.id} {person.name}, {saved} photos")
    if saved:
        flash(request, "Photos saved: {count}.", count=saved)
    return RedirectResponse(f"/people/{person_id}#photos", status_code=303)


@router.post("/people/{person_id}/capture")
def person_capture(
    request: Request, person_id: int, camera_id: int = Form(...), informed: bool = Form(False)
) -> RedirectResponse:
    """Photos taken by a camera while the person stands in front of it: the best match for what it sees later."""
    s = services(request)
    with s.db.session() as session:
        person = _get(session, person_id)
        if not _may_add_photos(request, person, informed):
            return RedirectResponse(f"/people/{person_id}#photos", status_code=303)
    saved, problem = 0, ""
    deadline = time.monotonic() + CAPTURE_SECONDS
    while saved < CAPTURE_PHOTOS and time.monotonic() < deadline:
        jpeg = s.vision.frame(f"cam{camera_id}-raw")
        if jpeg is None:
            flash(request, "The camera isn't sending images. Check that the recognition service is running.", "bad")
            return RedirectResponse(f"/people/{person_id}#photos", status_code=303)
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        with s.db.session() as session:
            check = s.people.add_photo(session, _get(session, person_id), image, "camera")
        if check.ok:
            saved += 1
            time.sleep(0.8)  # let the pose change a little between photos
        else:
            problem = check.problem
            time.sleep(0.2)
    with s.db.session() as session:
        audit(session, request.session["user"], "photos taken", f"{person_id}, camera {camera_id}, {saved} photos")
    if saved:
        flash(request, "Photos taken by the camera: {count}.", count=saved)
    else:
        flash(request, "No usable photo in {seconds} seconds. Last problem: {problem}", "warn",
              seconds=int(CAPTURE_SECONDS), problem=translator(request)(PHOTO_PROBLEMS.get(problem, problem)))
    return RedirectResponse(f"/people/{person_id}#photos", status_code=303)


@router.post("/people/{person_id}/delete")
def person_delete(request: Request, person_id: int, confirm: bool = Form(False)) -> RedirectResponse:
    if not confirm:
        flash(request, "Tick the box to confirm the deletion.", "warn")
        return RedirectResponse(f"/people/{person_id}#delete", status_code=303)
    s = services(request)
    with s.db.session() as session:
        person = _get(session, person_id)
        name = person.name
        s.people.delete_person(session, person)
        audit(session, request.session["user"], "person deleted", f"{person_id} {name}")
    flash(request, "{name} and all their photos were deleted.", name=name)
    return RedirectResponse("/people", status_code=303)


@router.post("/photos/{photo_id}/delete")
def photo_delete(request: Request, photo_id: int) -> RedirectResponse:
    s = services(request)
    with s.db.session() as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            raise HTTPException(404)
        person_id = photo.person_id
        s.people.delete_photo(session, photo)
        audit(session, request.session["user"], "photo deleted", f"{person_id}, photo {photo_id}")
    flash(request, "Photo removed.")
    return RedirectResponse(f"/people/{person_id}#photos", status_code=303)


@router.get("/photos/{photo_id}.jpg")
def photo_image(request: Request, photo_id: int, w: int | None = None) -> Response:
    s = services(request)
    with s.db.session() as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            raise HTTPException(404)
        path = s.cfg.paths.data_dir / photo.path
    return image_response(path, w)


@router.get("/photos/{photo_id}/face.jpg")
def photo_face(request: Request, photo_id: int, w: int | None = None) -> Response:
    """The face in the photo, cropped close, for round avatars."""
    s = services(request)
    with s.db.session() as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            raise HTTPException(404)
        relative = photo.path
    return image_response(s.people.face_image(relative), w)


def _get(session, person_id: int) -> Person:
    person = session.get(Person, person_id)
    if person is None:
        raise HTTPException(404)
    return person


def _person_error(name: str, category: str) -> str:
    if not name.strip():
        return "Write the person's name."
    if len(name.strip()) > 120:
        return "The name is too long."
    if category not in CATEGORIES:
        return "Choose a type."
    return ""


def _may_add_photos(request: Request, person: Person, informed: bool) -> bool:
    """Photos only after the person was told how their face is used (LGPD)."""
    if person.informed_at is None and not informed:
        flash(request, "Confirm that the person was told how their face is used before adding photos.", "warn")
        return False
    if person.informed_at is None:
        person.informed_at = datetime.now()
    return True


def _add_photos(request: Request, session, person: Person, files: list[UploadFile]) -> int:
    """Saves the usable photos and leaves a message for each one refused. Returns how many were saved."""
    s = services(request)
    t = translator(request)
    saved = 0
    for upload in files:
        image = decode_upload(upload)
        check = s.people.add_photo(session, person, image, "upload") if image is not None else None
        if check is None or not check.ok:
            problem = PHOTO_PROBLEMS[check.problem if check else NOT_AN_IMAGE]
            flash(request, "{file}: {problem}", "warn", file=upload.filename, problem=t(problem))
            continue
        saved += 1
        if check.looks_like:
            flash(request, "{file} also matches {name}. Check that it is the right person.", "warn",
                  file=upload.filename, name=check.looks_like)
    return saved
