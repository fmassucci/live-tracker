"""Event log: a JPEG snapshot per decision under data/snapshots/, and a row in the events table.

Next to each snapshot (x.jpg) goes a crop of the face, or of the thing seen (x.face.jpg), used for thumbnails.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
from sqlalchemy import ColumnElement, and_, select, true

from portaria.db import Camera, Database, Event, Person
from portaria.decision import Decision
from portaria.images import write_jpeg

if TYPE_CHECKING:
    from portaria.objects import Sighting

log = logging.getLogger(__name__)


class EventLog:
    def __init__(self, db: Database, data_dir: Path) -> None:
        self._db = db
        self._data_dir = data_dir

    def record(
        self,
        decision: Decision,
        snapshot: np.ndarray,
        *,
        face: np.ndarray | None = None,
        camera_id: int | None,
        person_name: str | None,
        door: str,
        model: str,
        liveness: str,
    ) -> int:
        person_id = int(decision.person) if decision.person and decision.person.isdigit() else None
        return self._save(decision.kind, snapshot, face, camera_id, person_id=person_id, person_name=person_name,
                          score=decision.score, door=door, model=model, liveness=liveness)

    def record_sighting(
        self, sighting: Sighting, snapshot: np.ndarray, *, thumbnail: np.ndarray | None, camera_id: int | None,
        model: str,
    ) -> int:
        """A kind of thing the people-and-objects model saw stay in the picture: no person, no door."""
        return self._save("detected", snapshot, thumbnail, camera_id, label=sighting.kind, count=sighting.count,
                          score=sighting.score, door="", model=model, liveness="none")

    def _save(self, kind: str, snapshot: np.ndarray, thumbnail: np.ndarray | None, camera_id: int | None,
              person_id: int | None = None, **columns: object) -> int:
        now = datetime.now()
        relative = f"snapshots/{now:%Y-%m-%d}/{now:%H%M%S_%f}_{kind}.jpg"
        write_jpeg(self._data_dir / relative, snapshot)
        if thumbnail is not None and thumbnail.size:
            write_jpeg(self._data_dir / face_path(relative), thumbnail)
        with self._db.session() as session:
            if person_id is not None and session.get(Person, person_id) is None:
                person_id = None  # deleted while they stood at the door
            if camera_id is not None and session.get(Camera, camera_id) is None:
                camera_id = None
            event = Event(at=now, kind=kind, camera_id=camera_id, person_id=person_id, snapshot=relative, **columns)
            session.add(event)
            session.flush()
            return event.id


def face_path(snapshot: str) -> str:
    """Where the face crop of a snapshot is kept."""
    return snapshot.removesuffix(".jpg") + ".face.jpg"


def face_crop(image: np.ndarray, box: np.ndarray, margin: float = 0.5, size: int = 240) -> np.ndarray:
    """The face with `margin` of its size around it, at most `size` pixels wide."""
    x, y, w, h = box
    x0, y0 = max(0, int(x - margin * w)), max(0, int(y - margin * h))
    x1, y1 = min(image.shape[1], int(x + (1 + margin) * w)), min(image.shape[0], int(y + (1 + margin) * h))
    crop = image[y0:y1, x0:x1]
    if crop.size and crop.shape[1] > size:
        crop = cv2.resize(crop, (size, round(crop.shape[0] * size / crop.shape[1])), interpolation=cv2.INTER_AREA)
    return crop


def in_period(first: date | None, last: date | None) -> ColumnElement[bool]:
    """Events from the day `first` to the day `last`, both included. A missing day leaves that side open."""
    conditions = [true()]
    if first:
        conditions.append(Event.at >= datetime.combine(first, time.min))
    if last:
        conditions.append(Event.at < datetime.combine(last + timedelta(days=1), time.min))
    return and_(*conditions)


def delete_events(db: Database, data_dir: Path, first: date | None, last: date | None) -> int:
    """Deletes the events from `first` to `last` (see in_period), with their snapshots. Returns how many."""
    return _delete_events(db, data_dir, in_period(first, last))


def purge_events(db: Database, data_dir: Path, older_than_days: int) -> int:
    """Deletes events older than the retention period, with their snapshots. Returns how many."""
    return _delete_events(db, data_dir, Event.at < datetime.now() - timedelta(days=older_than_days))


def _delete_events(db: Database, data_dir: Path, condition: ColumnElement[bool]) -> int:
    with db.session() as session:
        events = session.scalars(select(Event).where(condition)).all()
        for event in events:
            session.delete(event)
    # The records go first: a picture that can't be deleted, open in another program, only leaves a file behind.
    for event in events:
        if event.snapshot:
            for path in (data_dir / event.snapshot, data_dir / face_path(event.snapshot)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not delete %s", path)
    snapshots = data_dir / "snapshots"
    if snapshots.exists():
        for day in snapshots.iterdir():
            if day.is_dir() and not any(day.iterdir()):
                with suppress(OSError):  # a picture taken just now
                    day.rmdir()
    return len(events)
