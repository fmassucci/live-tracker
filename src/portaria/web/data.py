"""Settings, Database: where the images are stored, and downloading or deleting records with their pictures.

Only administrators: the pictures show people's faces.
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select

from portaria.db import Camera, Event, audit
from portaria.events import delete_events, in_period
from portaria.i18n import Translator
from portaria.web.common import (
    DOOR_RESULTS, KINDS, flash, fragment, on_this_computer, require_admin, services, translator,
)

router = APIRouter()

MOUSEEVENTF_MOVE = 0x0001
ASFW_ANY = -1  # any program may bring a window to the front


@dataclass
class Summary:
    count: int
    first: datetime | None
    last: datetime | None


def summary(request: Request, first: date | None = None, last: date | None = None) -> Summary:
    """How many records there are from the day `first` to the day `last`, and when the oldest and newest are."""
    with services(request).db.session() as session:
        count, oldest, newest = session.execute(
            select(func.count(), func.min(Event.at), func.max(Event.at)).where(in_period(first, last))
        ).one()
    return Summary(count, oldest, newest)


@router.get("/settings/data/summary", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def records_summary(request: Request, start: str = "", end: str = "") -> HTMLResponse:
    first, last = _period(start, end)
    return fragment(request, "_records_summary.html", summary=summary(request, first, last), dated=bool(first or last))


@router.post("/settings/data/export")
def export_records(
    request: Request, start: str = Form(""), end: str = Form(""), user: str = Depends(require_admin)
) -> Response:
    """A zip with a spreadsheet of the records and their pictures, sent while it is being made."""
    s = services(request)
    first, last = _period(start, end)
    with s.db.session() as session:
        events = session.scalars(select(Event).where(in_period(first, last)).order_by(Event.at)).all()
        camera_names = {c.id: c.name for c in session.scalars(select(Camera))}
        if events:
            audit(session, user, "records downloaded", f"{_describe(first, last)}: {len(events)}")
    if not events:
        flash(request, "No records in this period.", "bad")
        return RedirectResponse("/settings#data", status_code=303)
    t = translator(request)
    name = f"{t('Records')}_{events[0].at:%Y-%m-%d}_{events[-1].at:%Y-%m-%d}"
    pictures = {e.id: e.snapshot for e in events if e.snapshot and (s.cfg.paths.data_dir / e.snapshot).is_file()}
    return StreamingResponse(
        _zip(f"{name}.csv", _table(events, pictures, camera_names, t),
             [(s.cfg.paths.data_dir / picture, picture) for picture in pictures.values()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip"},
    )


@router.post("/settings/data/delete")
def delete_records(
    request: Request, start: str = Form(""), end: str = Form(""), confirm: bool = Form(False),
    user: str = Depends(require_admin),
) -> RedirectResponse:
    if not confirm:
        flash(request, "Confirm that you understand this can't be undone.", "bad")
        return RedirectResponse("/settings#data", status_code=303)
    s = services(request)
    first, last = _period(start, end)
    count = delete_events(s.db, s.cfg.paths.data_dir, first, last)
    if count:
        with s.db.session() as session:
            audit(session, user, "records deleted", f"{_describe(first, last)}: {count}")
        flash(request, "Records deleted: {count}.", count=count)
    else:
        flash(request, "No records in this period.", "bad")
    return RedirectResponse("/settings#data", status_code=303)


@router.post("/settings/data-folder", dependencies=[Depends(require_admin)])
def open_data_folder(request: Request) -> RedirectResponse:
    """The folder opens on this computer's screen, so only a browser on this computer can ask for it."""
    if not on_this_computer(request):
        raise HTTPException(403)
    try:
        open_folder(services(request).cfg.paths.data_dir.resolve())
    except OSError:
        flash(request, "The folder couldn't be opened.", "bad")
    return RedirectResponse("/settings#images", status_code=303)


def open_folder(folder: Path) -> None:
    """Shows `folder` in the file manager of this computer, in front of the browser."""
    if sys.platform == "win32":
        _let_the_next_window_come_to_the_front()
        os.startfile(folder)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])


def _let_the_next_window_come_to_the_front() -> None:
    """Windows keeps the windows that background programs open, like this server, behind the one in use.

    An empty mouse move counts as this program's own input, which lets it pass that right on to Explorer.
    """
    user32 = ctypes.windll.user32
    user32.mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0)
    user32.AllowSetForegroundWindow(ASFW_ANY)


def _period(start: str, end: str) -> tuple[date | None, date | None]:
    """The days chosen, None where there is none. Text that isn't a date is refused, never read as "every day"."""
    try:
        return (date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None)
    except ValueError:
        raise HTTPException(400) from None


def _describe(first: date | None, last: date | None) -> str:
    """The period, as the audit log keeps it."""
    return f"{first or '...'} to {last or '...'}" if first or last else "all"


def _table(events: list[Event], pictures: dict[int, str], camera_names: dict[int, str], t: Translator) -> bytes:
    """The records as a spreadsheet. Excel in Portuguese expects semicolons, and the mark at the start for accents."""
    text = io.StringIO()
    rows = csv.writer(text, delimiter=";" if t.language == "pt" else ",")
    rows.writerow([t("Date"), t("Time"), t("Person"), t("Result"), t("Camera"), t("Door"), t("Match"), t("Picture")])
    for e in events:
        rows.writerow([
            e.at.date().isoformat(), e.at.strftime("%H:%M:%S"), e.person_name or "", t(KINDS.get(e.kind, e.kind)),
            camera_names.get(e.camera_id, ""), t(DOOR_RESULTS[e.door]) if e.door in DOOR_RESULTS else "",
            t.number(e.score), pictures.get(e.id, ""),
        ])
    return text.getvalue().encode("utf-8-sig")


class _Pipe(io.RawIOBase):
    """Where the zip is written: each piece is sent on and dropped, so a big zip never sits in memory."""

    def __init__(self) -> None:
        self._data = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        self._data += data
        return len(data)

    def take(self) -> bytes:
        data = bytes(self._data)
        self._data.clear()
        return data


def _zip(table_name: str, table: bytes, pictures: list[tuple[Path, str]]) -> Iterator[bytes]:
    pipe = _Pipe()
    with zipfile.ZipFile(pipe, "w") as archive:
        archive.writestr(table_name, table, compress_type=zipfile.ZIP_DEFLATED)
        for path, name in pictures:
            try:
                archive.write(path, name)  # stored as they are: JPEGs don't get smaller
            except OSError:
                continue  # deleted meanwhile, by the retention cleanup
            yield pipe.take()
    yield pipe.take()
