"""Today (who came in), the records history and the live view."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from live_tracker.db import Camera, Event, Person, Photo
from live_tracker.events import face_path
from live_tracker.falls import NAMES as FALL_NAMES
from live_tracker.fire import NAMES as FIRE_NAMES
from live_tracker.models import CATALOG
from live_tracker.objects import NAMES as OBJECT_NAMES
from live_tracker.web.cameras import ANALYSIS_NAMES
from live_tracker.web.common import (
    DOOR_RESULTS, KINDS, fragment, image_response, page, recording_state, require_user, services,
)

router = APIRouter(dependencies=[Depends(require_user)])

NAMES = {**OBJECT_NAMES, **FIRE_NAMES, **FALL_NAMES}  # every kind of thing a camera records, by its key
THING_MODELS = tuple(m.key for m in CATALOG if m.on_cameras and m.key != "faces")  # models that record things
REFRESH_S = 5  # Today refreshes this often; an entry this recent gets the stamping animation
UNRECOGNIZED_SHOWN = 20  # a face camera's newest faces not recognized, in its panel on Today; Records has all
FINDINGS_SHOWN = 6  # a people-and-objects camera's newest photos, in its panel on Today; Records has all
LIVE_VIDEOS = 4  # Today plays up to this many cameras as video; with more, each shows a picture a second, since a
#                  browser keeps only 6 connections open to one address and every video holds one


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
    today = Event.at >= start
    with s.db.session() as session:
        cameras = session.scalars(select(Camera).order_by(Camera.device.desc(), Camera.name)).all()  # as on Cameras
        shown = [c for c in cameras if c.analysis or c.enabled]  # a stopped camera with a model shows it's stopped
        recognized = session.execute(
            select(
                Event.person_id,
                func.coalesce(Person.name, Event.person_name).label("name"),
                func.coalesce(Person.job_title, "").label("job"),
                func.coalesce(Person.category, "").label("category"),
                Event.at,
                Event.camera_id,
            )
            .outerjoin(Person, Person.id == Event.person_id)
            .where(today, Event.kind == "recognized")
            .order_by(Event.at)
        ).all()
        avatars = dict(session.execute(select(Photo.person_id, func.min(Photo.id)).group_by(Photo.person_id)).all())
        not_recognized = Event.kind.in_(("unknown", "spoof"))
        unknown_counts = dict(session.execute(
            select(Event.camera_id, func.count()).where(today, not_recognized).group_by(Event.camera_id)
        ).all())
        kinds = _found_by_camera(session.execute(
            select(Event.camera_id, Event.label, func.count().label("count"))
            .where(today, Event.kind == "detected")
            .group_by(Event.camera_id, Event.label)
        ).all())
        unknown, photos = {}, {}  # each camera's newest, for its panel
        for c in shown:
            newest = select(Event).where(today, Event.camera_id == c.id).order_by(Event.at.desc())
            if c.id in unknown_counts:
                unknown[c.id] = session.scalars(newest.where(not_recognized).limit(UNRECOGNIZED_SHOWN)).all()
            if c.id in kinds:
                photos[c.id] = session.scalars(newest.where(Event.kind == "detected").limit(FINDINGS_SHOWN)).all()
    status = s.vision.status()
    statuses = status.get("cameras", {}) if status else {}
    return {
        "now": now,
        "cameras": shown,
        "states": {c.id: recording_state(c, statuses.get(str(c.id)), status is not None) for c in shown},
        "statuses": statuses,
        "live_video": len(shown) <= LIVE_VIDEOS,
        "camera_names": {c.id: c.name for c in cameras},
        "people": _people_by_camera(recognized),
        "avatars": avatars,
        "unknown": unknown,
        "unknown_counts": unknown_counts,
        "found": kinds,
        "found_totals": {camera_id: sum(count for _, count in counted) for camera_id, counted in kinds.items()},
        "photos": photos,
        "thing_models": THING_MODELS,
        "analyses": ANALYSIS_NAMES,  # each camera's AI model, by name beside the camera's
        "things": NAMES,
        "service_up": status is not None,
        "just_now": now - timedelta(seconds=REFRESH_S + 1),
    }


@dataclass
class Seen:
    """One person in a face camera's panel on Today: when it first and last recognized them."""

    person_id: int | None
    name: str
    job: str
    category: str
    first: datetime
    last: datetime
    times: int = 1


def _people_by_camera(recognized: list) -> dict[int | None, list[Seen]]:
    """The people each camera recognized today, in the order they arrived there."""
    cameras: dict[int | None, dict[object, Seen]] = {}
    for row in recognized:
        people = cameras.setdefault(row.camera_id, {})
        seen = people.get(row.person_id or row.name)
        if seen is None:
            people[row.person_id or row.name] = Seen(row.person_id, row.name, row.job, row.category, row.at, row.at)
        else:
            seen.last, seen.times = row.at, seen.times + 1
    return {camera_id: list(people.values()) for camera_id, people in cameras.items()}


def _found_by_camera(found: list) -> dict[int | None, list[tuple[str, int]]]:
    """The kinds of things each camera found today, with how many records each, the most first."""
    kinds: dict[int | None, list[tuple[str, int]]] = {}
    for row in sorted(found, key=lambda r: -r.count):
        kinds.setdefault(row.camera_id, []).append((row.label, row.count))
    return kinds


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return date.today()
