"""Today (who came in), the records history and the live view."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from portaria.db import Camera, Event, Person, Photo
from portaria.events import face_path
from portaria.falls import NAMES as FALL_NAMES
from portaria.fire import NAMES as FIRE_NAMES
from portaria.objects import NAMES as OBJECT_NAMES
from portaria.web.common import (
    DOOR_RESULTS, KINDS, fragment, image_response, page, recording_state, require_user, services,
)

router = APIRouter(dependencies=[Depends(require_user)])

NAMES = {**OBJECT_NAMES, **FIRE_NAMES, **FALL_NAMES}  # every kind of thing a camera records, by its key
REFRESH_S = 5  # Today refreshes this often; an entry this recent gets the stamping animation
UNRECOGNIZED_SHOWN = 30  # faces not recognized shown on Today, newest first; Records has all of them


@router.get("/", response_class=HTMLResponse)
def today(request: Request) -> HTMLResponse:
    return page(request, "today.html", section="today", **_today(request))


@router.get("/today/live", response_class=HTMLResponse)
def today_live(request: Request) -> HTMLResponse:
    return fragment(request, "_today_live.html", **_today(request))


FACE_KINDS = {key: label for key, label in KINDS.items() if key != "detected"}


@router.get("/records", response_class=HTMLResponse)
def records(
    request: Request, day: str = "", kind: str = "", person: str = "", view: str = "", thing: str = "",
    camera: str = "",
) -> HTMLResponse:
    """Two views of a day: what happened with faces, and what the people-and-objects cameras found."""
    chosen = _parse_day(day)
    if view == "objects":
        return _findings(request, chosen, thing, camera)
    with services(request).db.session() as session:
        start = datetime.combine(chosen, datetime.min.time())
        query = select(Event).where(Event.at >= start, Event.at < start + timedelta(days=1), Event.kind != "detected")
        if kind in FACE_KINDS:
            query = query.where(Event.kind == kind)
        if person.isdigit():
            query = query.where(Event.person_id == int(person))
        events = session.scalars(query.order_by(Event.at.desc()).limit(500)).all()
        people = session.scalars(select(Person).order_by(Person.name)).all()
        camera_names = {c.id: c.name for c in session.scalars(select(Camera))}
        tab_counts = _tab_counts(session, start)
    return page(
        request, "records.html", section="records", events=events, people=people, camera_names=camera_names,
        tab_counts=tab_counts,
        day=chosen, kind=kind, person=person, kinds=FACE_KINDS, door_results=DOOR_RESULTS, things=NAMES,
        retention=services(request).cfg.storage.retention_days, **_days_around(chosen),
    )


def _findings(request: Request, chosen: date, thing: str, camera: str) -> HTMLResponse:
    """The day's people-and-objects records, newest first, grouped by hour, with a count per kind of thing."""
    with services(request).db.session() as session:
        start = datetime.combine(chosen, datetime.min.time())
        query = select(Event).where(Event.at >= start, Event.at < start + timedelta(days=1), Event.kind == "detected")
        if camera.isdigit():
            query = query.where(Event.camera_id == int(camera))
        found = session.scalars(query.order_by(Event.at.desc()).limit(1000)).all()
        cameras = session.scalars(select(Camera).order_by(Camera.name)).all()
        tab_counts = _tab_counts(session, start)
    counts = Counter(e.label for e in found).most_common()
    shown = [e for e in found if not thing or e.label == thing]
    hours = [(hour, list(items)) for hour, items in groupby(shown, key=lambda e: e.at.hour)]
    return page(
        request, "records_objects.html", section="records", hours=hours, counts=counts, total=len(found),
        tab_counts=tab_counts,
        thing=thing if thing in NAMES else "", camera=camera, cameras=cameras,
        camera_names={c.id: c.name for c in cameras}, things=NAMES, day=chosen,
        retention=services(request).cfg.storage.retention_days, **_days_around(chosen),
    )


def _tab_counts(session, start: datetime) -> dict[str, int]:
    """How many records each view has on the day, shown on its tab."""
    day = (Event.at >= start, Event.at < start + timedelta(days=1))
    count = select(func.count()).select_from(Event).where(*day)
    return {"faces": session.scalar(count.where(Event.kind != "detected")),
            "objects": session.scalar(count.where(Event.kind == "detected"))}


def _days_around(chosen: date) -> dict:
    return {"previous_day": chosen - timedelta(days=1), "next_day": chosen + timedelta(days=1),
            "is_today": chosen == date.today()}


@router.get("/records/{event_id}.jpg")
def record_image(request: Request, event_id: int, w: int | None = None) -> Response:
    return image_response(_snapshot_path(request, event_id), w)


@router.get("/records/{event_id}/face.jpg")
def record_face(request: Request, event_id: int, w: int | None = None) -> Response:
    """The face crop for thumbnails; the whole picture when there is no crop."""
    path = _snapshot_path(request, event_id)
    face = path.with_name(face_path(path.name))
    return image_response(face if face.is_file() else path, w)


def _snapshot_path(request: Request, event_id: int) -> Path:
    s = services(request)
    with s.db.session() as session:
        event = session.get(Event, event_id)
        if event is None or not event.snapshot:
            raise HTTPException(404)
        return s.cfg.paths.data_dir / event.snapshot


@router.get("/live", response_class=HTMLResponse)
@router.get("/live/{camera_id}", response_class=HTMLResponse)
def live(request: Request, camera_id: int | None = None) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:  # stopped cameras too, so they can be started from here
        cameras = session.scalars(
            select(Camera).order_by(Camera.enabled.desc(), Camera.device.desc(), Camera.name)  # doors first
        ).all()
    chosen = next((c for c in cameras if c.id == camera_id), cameras[0] if cameras else None)
    status = s.vision.status()
    camera_status = status.get("cameras", {}).get(str(chosen.id)) if status and chosen else None
    state = recording_state(chosen, camera_status, status is not None) if chosen else None
    return page(request, "live.html", section="cameras", cameras=cameras, chosen=chosen,
                service_up=status is not None, camera_status=camera_status, state=state)


def _today(request: Request) -> dict:
    s = services(request)
    now = datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    with s.db.session() as session:
        entries = session.execute(
            select(
                Event.person_id,
                func.coalesce(Person.name, Event.person_name).label("name"),
                func.coalesce(Person.job_title, "").label("job"),
                func.coalesce(Person.category, "").label("category"),
                func.min(Event.at).label("first"),
                func.max(Event.at).label("last"),
                func.count().label("visits"),
            )
            .outerjoin(Person, Person.id == Event.person_id)
            .where(Event.at >= start, Event.kind == "recognized", Event.door != "")  # entries: at a door camera
            .group_by(Event.person_id, "name", "job", "category")
            .order_by(func.min(Event.at))
        ).all()
        avatars = dict(session.execute(select(Photo.person_id, func.min(Photo.id)).group_by(Photo.person_id)).all())
        unrecognized_today = select(Event).where(Event.at >= start, Event.kind != "recognized", Event.door != "")
        not_recognized = session.scalars(unrecognized_today.order_by(Event.at.desc()).limit(UNRECOGNIZED_SHOWN)).all()
        not_recognized_count = session.scalar(select(func.count()).select_from(unrecognized_today.subquery()))
        door_cameras = session.scalars(  # a stopped one too: Today shows that it isn't recording
            select(Camera).where(Camera.device == "door").order_by(Camera.enabled.desc(), Camera.id)
        ).all()
    status = s.vision.status()
    door_camera = door_cameras[0] if door_cameras else None
    camera_status = status.get("cameras", {}).get(str(door_camera.id)) if status and door_camera else None
    return {
        "now": now,
        "entries": entries,
        "avatars": avatars,
        "not_recognized": not_recognized,
        "not_recognized_count": not_recognized_count,
        "door_cameras": door_cameras,
        "door_camera": door_camera,
        "camera_status": camera_status,
        "door_state": recording_state(door_camera, camera_status, status is not None) if door_camera else None,
        "service_up": status is not None,
        "just_now": now - timedelta(seconds=REFRESH_S + 1),
    }


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return date.today()
