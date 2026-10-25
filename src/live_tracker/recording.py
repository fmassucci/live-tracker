"""When a camera keeps a photo of what its AI model found.

Each camera has a rule. Each time a person or thing appears; at an interval (at most one photo every so often,
and, if the camera asks for it, another every interval while they stay in the picture; otherwise the next only
once they leave and come back); or only the first time of the day. The rule counts each recognized person, and
each kind of thing, apart. A face that isn't recognized is always kept: each can be someone else.

The models report what arrives and, after that, what is still there (decision.py, objects.py); the rule alone
decides when a photo is due, counted from the last photo kept.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from live_tracker.db import Database, Event

APPEAR, INTERVAL, DAILY = "appear", "interval", "daily"
MODES = (APPEAR, INTERVAL, DAILY)
MIN_EVERY_S, MAX_EVERY_S = 5, 24 * 3600
DEFAULT_EVERY_S = 900


@dataclass(frozen=True)
class RecordRule:
    mode: str = APPEAR
    every_s: int = DEFAULT_EVERY_S  # for INTERVAL
    while_present: bool = True  # for INTERVAL: again every interval while it stays, not only once it's back

    def due(self, last: datetime | None, now: datetime) -> bool:
        """Whether a new photo of a person or kind of thing is kept, given when the last one was."""
        if last is None or self.mode == APPEAR:
            return True
        if self.mode == INTERVAL:
            return (now - last).total_seconds() >= self.every_s
        return last.date() != now.date()

    @property
    def repeats(self) -> bool:
        """Whether a person or thing that stays in the picture is photographed again, every interval."""
        return self.mode == INTERVAL and self.while_present

    def remembered_since(self, now: datetime) -> datetime | None:
        """How far back earlier photos count for this rule; None when they don't."""
        if self.mode == INTERVAL:
            return now - timedelta(seconds=self.every_s)
        if self.mode == DAILY:
            return datetime.combine(now.date(), time())
        return None


def thing(kind: str) -> str:
    return f"thing:{kind}"


def person(person_id: str) -> str:
    return f"person:{person_id}"


def decision_subject(kind: str, person_id: str | None) -> str | None:
    """Who a face decision is about, for the rule: a recognized person; None for anyone else, always kept."""
    return person(person_id) if kind == "recognized" and person_id else None


class RecordGate:
    """One camera's rule, and when it last kept a photo of each person and kind of thing."""

    def __init__(self, rule: RecordRule, last: dict[str, datetime] | None = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.rule = rule
        self._last = dict(last or {})
        self._clock = clock

    def allow(self, subject: str | None, staying: bool = False) -> bool:
        """Whether to keep a photo of this person or kind of thing now; remembers it when so.

        `staying`: it has been in the picture since it arrived. Only a rule that repeats while it stays keeps
        that one; any other waits until it leaves and comes back.
        """
        if staying and not (subject and self.rule.repeats):
            return False
        if subject is None:
            return True
        now = self._clock()
        if not self.rule.due(self._last.get(subject), now):
            return False
        self._last[subject] = now
        return True


def last_recorded(db: Database, camera_id: int, since: datetime) -> dict[str, datetime]:
    """A camera's last photo of each person and kind of thing since a moment, so a restart keeps to the rule."""
    with db.session() as session:
        rows = session.execute(
            select(Event.kind, Event.label, Event.person_id, func.max(Event.at))
            .where(Event.camera_id == camera_id, Event.at >= since, Event.kind.in_(("detected", "recognized")))
            .group_by(Event.kind, Event.label, Event.person_id)
        ).all()
    last: dict[str, datetime] = {}
    for kind, label, person_id, at in rows:
        subject = thing(label) if kind == "detected" else decision_subject(kind, str(person_id) if person_id else None)
        if subject:
            last[subject] = max(at, last.get(subject, at))
    return last
