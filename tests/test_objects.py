from pathlib import Path

import numpy as np
import pytest

from portaria.camera import Frame
from portaria.objects import (
    KEYS, Detection, ObjectAnalyzer, ObjectDetector, ObjectSettings, Presence, anchors, decode,
)

GRIDS, STRIDES = anchors(640)


def _prediction(raw: np.ndarray, row: int, kind: str, score: float, cell_offset=(0.5, 0.5), size_log=(1.0, 2.0)):
    """Writes what the model would output for one thing: position in its grid cell, log size, and scores."""
    raw[row, 0:2] = cell_offset
    raw[row, 2:4] = size_log
    raw[row, 4] = 1.0  # sure there is something
    raw[row, 5 + KEYS.index(kind)] = score


def test_raw_predictions_become_boxes_in_the_original_picture():
    raw = np.zeros((8400, 85), np.float32)
    _prediction(raw, 100, "person", 0.9)  # stride 8: row 100 is cell x=20, y=1
    _prediction(raw, 101, "person", 0.8)  # the cell next to it, overlapping: the same person
    _prediction(raw, 5000, "cell_phone", 0.3)  # too unsure
    found = decode(raw, GRIDS, STRIDES, ratio=0.5, min_score=0.5, overlap=0.45)
    assert [(d.kind, round(d.score, 2)) for d in found] == [("person", 0.9)]
    x, y, w, h = found[0].box
    width, height = np.exp(1.0) * 8, np.exp(2.0) * 8  # in the model's 640x640 picture
    assert (w, h) == pytest.approx((width / 0.5, height / 0.5))  # scaled back to the original picture
    assert (x, y) == pytest.approx(((20.5 * 8 - width / 2) / 0.5, (1.5 * 8 - height / 2) / 0.5))


def test_a_kind_is_recorded_once_per_stay():
    presence = Presence(min_seconds=2)
    person = [Detection("person", 0.9, np.array([0, 0, 10, 10.0]))]
    assert presence.update(person, now=0.0) == []
    assert presence.update(person, now=1.0) == []
    sightings = presence.update(person + person, now=2.0)
    assert [(s.kind, s.count) for s in sightings] == [("person", 2)]
    assert presence.update(person, now=3.0) == []  # still the same stay
    assert presence.update([], now=4.0) == []  # missed for a moment
    assert presence.update(person, now=5.0) == []  # back within 3 s: the same stay
    assert presence.update([], now=9.0) == []  # gone for more than 3 s: the stay ends
    assert presence.update(person, now=10.0) == []
    assert [s.kind for s in presence.update(person, now=12.0)] == ["person"]  # a new stay, recorded again


def test_a_kind_that_stays_is_reported_again_every_interval():
    person = [Detection("person", 0.9, np.array([0, 0, 10, 10.0]))]
    every_minute = Presence(min_seconds=2, repeat_s=60)
    assert [t for t in range(200) if every_minute.update(person, now=float(t))] == [2, 62, 122, 182]
    once = Presence(min_seconds=2)
    assert [t for t in range(200) if once.update(person, now=float(t))] == [2]


def test_settings_are_kept_valid_whatever_is_stored():
    assert ObjectSettings.parse("") == ObjectSettings()
    assert ObjectSettings.parse("not json") == ObjectSettings()
    settings = ObjectSettings.parse('{"kinds": ["dog", "unicorn", "cell_phone"], "confidence": 3, "fps": "x"}')
    assert settings.kinds == ("dog", "cell_phone")
    assert settings.confidence == 0.8 and settings.fps == ObjectSettings().fps
    assert ObjectSettings.parse(settings.dumps()) == settings
    assert ObjectSettings.of({"kinds": ["unicorn"]}).kinds == ("person",)
    assert ObjectSettings.of({"kinds": ["dog", "cat", "dog"]}).kinds == ("dog", "cat")


def test_sensitivity_shows_as_the_nearest_named_level():
    assert ObjectSettings(confidence=0.45).level == 0.5
    assert ObjectSettings(confidence=0.2).level == 0.35
    assert ObjectSettings(confidence=0.8).level == 0.65


class FakeDetector:
    def __init__(self, found: list[Detection]) -> None:
        self.found, self.looks = found, 0

    def detect(self, image, min_score):
        self.looks += 1
        return [d for d in self.found if d.score >= min_score]


def test_the_analyzer_looks_a_few_times_per_second_and_marks_the_chosen_kinds():
    now = [0.0]
    detector = FakeDetector([Detection("person", 0.9, np.array([10, 10, 50, 120.0])),
                             Detection("chair", 0.8, np.array([200, 10, 50, 50.0])),
                             Detection("cell_phone", 0.3, np.array([30, 40, 5, 9.0]))])
    settings = ObjectSettings(kinds=("person", "cell_phone"), confidence=0.5, min_seconds=1, fps=2)
    analyzer = ObjectAnalyzer(detector, settings, name=lambda text: {"Person": "Pessoa"}.get(text, text),
                              clock=lambda: now[0])
    frame = Frame(np.zeros((480, 640, 3), np.uint8), 1, 0.0, b"")
    first = analyzer.process(frame, frozen=False)
    assert [o.label for o in first.overlays] == ["Pessoa 90%"]  # the chair isn't chosen; the phone is too unsure
    now[0] = 0.25
    analyzer.process(frame, frozen=False)
    assert detector.looks == 1  # 2 looks per second: too soon to look again
    now[0] = 1.0
    assert [s.kind for s in analyzer.process(frame, frozen=False).sightings] == ["person"]
    now[0] = 2.0
    assert analyzer.process(frame, frozen=True).overlays and detector.looks == 2  # a frozen picture isn't looked at


MODEL = Path("models/object_detection_yolox_2022nov.onnx")


@pytest.mark.skipif(not MODEL.exists(), reason="the people-and-objects model isn't downloaded here")
def test_the_real_model_runs_and_finds_nothing_in_an_empty_picture():
    detector = ObjectDetector(MODEL)
    assert detector.detect(np.full((720, 1280, 3), 128, np.uint8), min_score=0.5) == []
