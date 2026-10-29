"""One-time import of the first setup's files into the database.

    data/enroll/<name>/*.jpg     -> people and their photos
    data/events/events.jsonl     -> events, with their snapshots
    [camera] source in config    -> the first camera, if there is none yet

Running it again skips people who already exist and events already imported.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select

from live_tracker.config import Config
from live_tracker.db import CAMERAS_VERSION, Camera, Database, Event, Person, bump
from live_tracker.gallery import IMAGE_SUFFIXES
from live_tracker.i18n import Translator
from live_tracker.images import read_image
from live_tracker.people import People


@dataclass
class ImportReport:
    people: int = 0
    photos: int = 0
    events: int = 0
    camera: str | None = None
    skipped: list[str] = field(default_factory=list)


def import_files(cfg: Config, db: Database, people: People) -> ImportReport:
    report = ImportReport()
    _import_camera(cfg, db, report)
    _import_people(cfg, db, people, report)
    _import_events(cfg, db, report)
    return report


def _import_camera(cfg: Config, db: Database, report: ImportReport) -> None:
    if not cfg.camera.source:
        return
    with db.session() as session:
        if session.scalar(select(func.count()).select_from(Camera)):
            return
        name = Translator(cfg.web.language)("Camera")
        session.add(Camera(name=name, url=cfg.camera.source))
        bump(session, CAMERAS_VERSION)
        report.camera = name


def _import_people(cfg: Config, db: Database, people: People, report: ImportReport) -> None:
    if not cfg.enroll_dir.exists():
        return
    for folder in sorted(p for p in cfg.enroll_dir.iterdir() if p.is_dir()):
        with db.session() as session:
            if session.scalar(select(Person).where(Person.name == folder.name)):
                report.skipped.append(f"{folder.name}: already registered")
                continue
            person = Person(name=folder.name)
            session.add(person)
            session.flush()
            report.people += 1
            for photo in sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES):
                image = read_image(photo)
                check = people.add_photo(session, person, image, "import") if image is not None else None
                if check and check.ok:
                    report.photos += 1
                else:
                    report.skipped.append(f"{photo}: {check.problem if check else 'not an image'}")


def _import_events(cfg: Config, db: Database, report: ImportReport) -> None:
    events_dir = cfg.paths.data_dir / "events"
    log_file = events_dir / "events.jsonl"
    if not log_file.exists():
        return
    with db.session() as session:
        names = {p.name: p.id for p in session.scalars(select(Person))}
        for line in log_file.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            at = datetime.fromisoformat(entry["time"])
            if session.scalar(select(Event.id).where(Event.at == at)):
                continue
            snapshot = None
            source = events_dir / entry["snapshot"]
            if source.exists():
                snapshot = f"snapshots/{at:%Y-%m-%d}/{at:%H%M%S_%f}_{entry['kind']}.jpg"
                (cfg.paths.data_dir / snapshot).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, cfg.paths.data_dir / snapshot)
            session.add(Event(
                at=at,
                kind=entry["kind"],
                person_id=names.get(entry.get("person") or ""),
                person_name=entry.get("person"),
                score=entry["score"],
                snapshot=snapshot,
                action={"opened": "switched", "kept": "denied"}.get(entry.get("door"), "shadow"),  # as in 0010
                model=entry.get("model", ""),
                liveness=entry.get("liveness", ""),
            ))
            report.events += 1
