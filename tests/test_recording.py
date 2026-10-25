from datetime import datetime, timedelta

from portaria.db import Camera, Event, Person
from portaria.recording import (
    APPEAR, DAILY, INTERVAL, RecordGate, RecordRule, decision_subject, last_recorded, person, thing,
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def test_each_time_it_appears_keeps_every_photo():
    gate = RecordGate(RecordRule(APPEAR), clock=Clock(datetime(2026, 10, 24, 9, 0)))
    assert gate.allow(thing("person")) and gate.allow(thing("person"))


def test_an_interval_keeps_one_photo_of_each_person_or_thing_per_interval():
    clock = Clock(datetime(2026, 10, 24, 9, 0))
    gate = RecordGate(RecordRule(INTERVAL, every_s=900), clock=clock)
    assert gate.allow(thing("person"))
    clock.advance(minutes=10)
    assert not gate.allow(thing("person"))  # back too soon
    assert gate.allow(thing("dog"))  # each kind counts apart
    clock.advance(minutes=5)  # the next one is due
    assert gate.allow(thing("person"))


def test_an_interval_photographs_again_what_stays_only_if_the_camera_asks():
    clock = Clock(datetime(2026, 10, 24, 9, 0))
    repeats = RecordGate(RecordRule(INTERVAL, every_s=900), clock=clock)
    back_only = RecordGate(RecordRule(INTERVAL, every_s=900, while_present=False), clock=clock)
    assert repeats.rule.repeats and not back_only.rule.repeats
    assert repeats.allow(thing("person")) and back_only.allow(thing("person"))
    clock.advance(minutes=10)
    assert not repeats.allow(thing("person"), staying=True)  # still there, but the interval isn't over
    clock.advance(minutes=5)
    assert repeats.allow(thing("person"), staying=True)  # still there an interval after its last photo
    assert not back_only.allow(thing("person"), staying=True)  # waits until it leaves and comes back
    assert back_only.allow(thing("person"))  # back, an interval after its last photo
    clock.advance(minutes=5)
    assert not repeats.allow(thing("person"))  # back too soon after the photo taken while it stayed


def test_only_an_interval_photographs_what_stays():
    for rule in (RecordRule(APPEAR), RecordRule(DAILY)):
        gate = RecordGate(rule, clock=Clock(datetime(2026, 10, 24, 9, 0)))
        assert not rule.repeats and not gate.allow(thing("person"), staying=True)
        assert gate.allow(thing("person"))  # the arrival still counts
    assert not RecordGate(RecordRule(INTERVAL)).allow(None, staying=True)  # a face not recognized: only visits


def test_the_first_time_of_the_day_keeps_one_photo_a_day():
    clock = Clock(datetime(2026, 10, 24, 23, 50))
    gate = RecordGate(RecordRule(DAILY), clock=clock)
    assert gate.allow(person("7"))
    clock.advance(minutes=5)
    assert not gate.allow(person("7"))
    assert gate.allow(person("8"))
    clock.advance(minutes=10)  # 00:05, the next day
    assert gate.allow(person("7"))


def test_faces_not_recognized_are_always_kept():
    assert decision_subject("recognized", "7") == person("7")
    assert decision_subject("unknown", None) is None and decision_subject("spoof", "7") is None
    gate = RecordGate(RecordRule(DAILY))
    assert gate.allow(None) and gate.allow(None)


def test_a_restart_remembers_the_photos_the_rule_counts(db):
    with db.session() as session:
        session.add(Camera(id=1, name="Caixa", url="0", analysis="objects"))
        session.add(Person(id=5, name="Ana"))
        session.flush()  # the person exists before the events about her
        for at, kind, label, person_id in [
            (datetime(2026, 10, 24, 8, 0), "detected", "person", None),
            (datetime(2026, 10, 24, 9, 30), "detected", "person", None),
            (datetime(2026, 10, 23, 22, 0), "detected", "dog", None),  # yesterday
            (datetime(2026, 10, 24, 9, 10), "recognized", "", 5),
            (datetime(2026, 10, 24, 9, 20), "unknown", "", None),
        ]:
            session.add(Event(at=at, kind=kind, label=label, person_id=person_id, camera_id=1, score=0.8,
                              snapshot="x.jpg", door="", model="yolox", liveness="none"))
    now = datetime(2026, 10, 24, 10, 0)
    rule = RecordRule(DAILY)
    last = last_recorded(db, 1, rule.remembered_since(now))
    assert last == {thing("person"): datetime(2026, 10, 24, 9, 30), person("5"): datetime(2026, 10, 24, 9, 10)}
    gate = RecordGate(rule, last, clock=lambda: now)
    assert not gate.allow(thing("person")) and gate.allow(thing("dog")) and not gate.allow(person("5"))
    assert RecordRule(APPEAR).remembered_since(now) is None
    assert RecordRule(INTERVAL, every_s=600).remembered_since(now) == datetime(2026, 10, 24, 9, 50)
