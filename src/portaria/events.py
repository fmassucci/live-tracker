"""Event log: a JPEG snapshot per decision under data/snapshots/, and a row in the events table.

Next to each snapshot (x.jpg) goes a crop of the face (x.face.jpg), used for thumbnails.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select

from portaria.db import Camera, Database, Event, Person
from portaria.decision import Decision
from portaria.images import write_jpeg


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
        now = datetime.now()
        relative = f"snapshots/{now:%Y-%m-%d}/{now:%H%M%S_%f}_{decision.kind}.jpg"
        write_jpeg(self._data_dir / relative, snapshot)
        if face is not None and face.size:
            write_jpeg(self._data_dir / face_path(relative), face)
        with self._db.session() as session:
            person_id = int(decision.person) if decision.person and decision.person.isdigit() else None
            if person_id is not None and session.get(Person, person_id) is None:
                person_id = None  # deleted while they stood at the door
            if camera_id is not None and session.get(Camera, camera_id) is None:
                camera_id = None
            event = Event(
                at=now,
                kind=decision.kind,
                camera_id=camera_id,
                person_id=person_id,
                person_name=person_name,
                score=decision.score,
                snapshot=relative,
                door=door,
                model=model,
                liveness=liveness,
            )
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


def purge_events(db: Database, data_dir: Path, older_than_days: int) -> int:
    """Deletes events older than the retention period, with their snapshots. Returns how many."""
    cutoff = datetime.now() - timedelta(days=older_than_days)
    with db.session() as session:
        old = session.scalars(select(Event).where(Event.at < cutoff)).all()
        for event in old:
            if event.snapshot:
                (data_dir / event.snapshot).unlink(missing_ok=True)
                (data_dir / face_path(event.snapshot)).unlink(missing_ok=True)
            session.delete(event)
    snapshots = data_dir / "snapshots"
    if snapshots.exists():
        for day in snapshots.iterdir():
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
    return len(old)
