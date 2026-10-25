import json
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeTools, place_model_file
from fastapi.testclient import TestClient
from sqlalchemy import select

from portaria import service
from portaria.camera import Frame
from portaria.db import Camera, Event
from portaria.falls import FallDetector, FallSettings, lying
from portaria.fire import FireDetector, FireSettings
from portaria.objects import Detection, ObjectAnalyzer, ObjectSettings, Sighting
from portaria.pipeline import Result
from portaria.service import CameraSpec, VisionService
from portaria.web.app import create_app
from portaria.yolo import Found, decode, decode_end2end

PAD = np.array([0, 140], np.float32)  # a 1280x720 picture in a 640 square: half size, 140 px of grey above


def test_yolov8_columns_become_boxes_in_the_picture():
    names = {0: "smoke", 1: "fire"}
    raw = np.zeros((6, 8400), np.float32)
    raw[:, 10] = [320, 240, 100, 50, 0.1, 0.9]  # a fire centered at (320, 240) of the model's square
    raw[:, 11] = [322, 241, 100, 50, 0.1, 0.8]  # the same fire, a little less sure
    raw[:, 12] = [100, 100, 20, 20, 0.3, 0.0]  # smoke too unsure to count
    found = decode(raw, names, 0, ratio=0.5, pad=PAD, min_score=0.5, overlap=0.6)
    assert [(f.kind, round(f.score, 2)) for f in found] == [("fire", 0.9)]
    assert found[0].box == pytest.approx([270 / 0.5, (215 - 140) / 0.5, 200, 100])


def test_yolo26_rows_become_boxes_and_body_points_in_the_picture():
    raw = np.zeros((300, 6 + 51), np.float32)
    raw[0, :6] = [100, 200, 300, 260, 0.8, 0]  # corners, score, kind
    raw[0, 6:9] = [150, 210, 0.9]  # the nose
    raw[1, :6] = [0, 0, 10, 10, 0.2, 0]
    found = decode_end2end(raw, {0: "person"}, 17, ratio=0.5, pad=PAD, min_score=0.5)
    assert len(found) == 1 and found[0].kind == "person"
    assert found[0].box == pytest.approx([200, 120, 400, 120])
    assert found[0].points.shape == (17, 3) and found[0].points[0] == pytest.approx([300, 140, 0.9])


def _body(shoulders: tuple, hips: tuple, sure: float = 0.9) -> np.ndarray:
    """Body points with only the shoulders and hips seen."""
    points = np.zeros((17, 3))
    points[5], points[6] = [*shoulders, sure], [shoulders[0] + 1, shoulders[1], sure]
    points[11], points[12] = [*hips, sure], [hips[0] + 1, hips[1], sure]
    return points


def test_a_person_lies_when_the_torso_leans_or_the_body_is_wider_than_tall():
    def person(points=None, box=(0, 0, 60, 160)):
        return Found("person", 0.9, np.array(box, float), points)

    assert not lying(person(_body(shoulders=(100, 50), hips=(100, 120))))  # standing
    assert lying(person(_body(shoulders=(40, 100), hips=(120, 106))))  # on the floor
    assert lying(person(_body(shoulders=(60, 110), hips=(130, 100))))  # on the floor, head lower than the hips
    assert not lying(person(_body(shoulders=(140, 80), hips=(100, 120))))  # bending down, 45 degrees
    assert not lying(person(_body(shoulders=(40, 100), hips=(120, 106), sure=0.2), box=(0, 0, 60, 160)))  # unseen
    assert lying(person(box=(0, 0, 160, 60)))  # no points: a box much wider than tall
    assert not lying(person(box=(0, 0, 100, 90)))


def test_each_model_keeps_to_its_own_kinds():
    assert FireSettings.parse("") == FireSettings() and FireSettings().kinds == ("fire", "smoke")
    assert FireSettings.parse('{"kinds": ["person", "smoke"], "fps": 3}').kinds == ("smoke",)
    assert FallSettings.parse('{"kinds": []}').kinds == ("fall",) and FallSettings().min_seconds == 10
    assert ObjectSettings.parse('{"kinds": ["fire"]}').kinds == ("person",)  # fire isn't among the 80


class FakeDetector:
    def __init__(self, found: list[Detection]) -> None:
        self.found = found

    def detect(self, image, min_score):
        return [d for d in self.found if d.score >= min_score]


def test_falls_mark_everyone_and_record_only_who_is_on_the_floor():
    now = [0.0]
    detector = FakeDetector([Detection("fall", 0.8, np.array([10, 300, 200, 80.0])),
                             Detection("person", 0.9, np.array([400, 50, 60, 200.0]))])
    analyzer = ObjectAnalyzer(detector, FallSettings(min_seconds=5), name={"Fall": "Queda"}.get,
                              clock=lambda: now[0], context=("person",))
    frame = Frame(np.zeros((480, 640, 3), np.uint8), 1, 0.0, b"")
    first = analyzer.process(frame, frozen=False)
    assert [(o.label, o.tone) for o in first.overlays] == [("Queda 80%", "object"), ("", "dim")]
    assert first.sightings == []
    now[0] = 5.0
    assert [s.kind for s in analyzer.process(frame, frozen=False).sightings] == ["fall"]  # down for 5 s


class FakeSource:
    url, ended = "0", False

    def stop(self) -> None:
        pass


def test_a_camera_runs_fire_and_smoke_or_falls_and_records_with_that_model(cfg, monkeypatch):
    monkeypatch.setattr(service, "PreviewServer", lambda host, port: None)
    vision = VisionService(cfg)
    monkeypatch.setattr(vision, "make_fire_detector", lambda: FakeDetector([]))
    monkeypatch.setattr(vision, "make_fall_detector", lambda: FakeDetector([]))
    fire = CameraSpec(1, "Cozinha", "0", analysis="fire", settings='{"kinds": ["fire"], "min_seconds": 4}')
    worker = service.CameraWorker(fire, vision, FakeSource())
    assert worker.analyzer._settings == FireSettings(kinds=("fire",), min_seconds=4) and not worker.model_missing
    falls = CameraSpec(2, "Salão", "0", analysis="falls")
    assert service.CameraWorker(falls, vision, FakeSource()).analyzer._settings == FallSettings()
    with vision.db.session() as session:
        session.add(Camera(id=1, name="Cozinha", url="0", analysis="fire"))
    frame = Frame(np.zeros((480, 640, 3), np.uint8), seq=1, captured_at=0.0, signature=b"")
    vision.on_sighting(fire, frame, Result(), Sighting("smoke", 1, 0.7, np.array([10, 10, 50, 50.0])))
    with vision.db.session() as session:
        event = session.scalars(select(Event)).one()
        assert (event.kind, event.label, event.model) == ("detected", "smoke", "yolo26n-fire")


class NoVision:
    def status(self):
        return None

    def camera_status(self, camera_id):
        return None


@pytest.fixture
def client(cfg):
    application = create_app(cfg)
    application.state.services.people._tools = FakeTools()
    application.state.services.vision = NoVision()
    with TestClient(application) as test_client:
        test_client.post("/login", data={"username": "admin", "password": "admin"})
        yield test_client


def test_settings_lists_liveness_fire_and_falls_and_cameras_choose_only_what_runs_on_them(client, cfg, monkeypatch):
    page = client.get("/settings/models").text
    assert "Prova de vida" in page and "Fogo e fumaça" in page and "Quedas" in page
    for key in ("minifasnet_v2", "minifasnet_v1se", "fire_yolo26n", "pose_yolov8s"):
        place_model_file(cfg.paths.models_dir, monkeypatch, key)
    page = client.get("/settings/models").text
    assert "As câmeras poderão usar em breve." not in page  # liveness is part of face recognition
    form = client.get("/cameras/new").text
    assert 'value="fire"' in form and 'value="falls"' in form and 'value="liveness"' not in form


def test_the_camera_form_keeps_fire_and_falls_settings(client, cfg, monkeypatch):
    for key in ("fire_yolo26n", "pose_yolov8s"):
        place_model_file(cfg.paths.models_dir, monkeypatch, key)
    fields = {"name": "Cozinha", "url": "0", "analysis": "fire", "device": "", "kinds": ["person"],
              "fire_confidence": "0.65", "fire_seconds": "5", "fire_fps": "3"}
    refused = client.post("/cameras/new", data=fields)  # people and objects' kinds don't count for fire
    assert refused.status_code == 400 and "Escolha fogo, fumaça ou os dois." in refused.text
    client.post("/cameras/new", data={**fields, "kinds": ["person", "smoke"]})
    with client.app.state.services.db.session() as session:
        stored = json.loads(session.get(Camera, 1).model_settings)
    assert stored == {"kinds": ["smoke"], "confidence": 0.65, "min_seconds": 5.0, "fps": 3.0}
    edit = client.get("/cameras/1").text
    assert 'name="fire_confidence" value="0.65" checked' in edit and 'value="smoke" checked' in edit
    assert 'value="fire" checked' not in edit.split("model-settings-fire")[1]  # only smoke is ticked
    client.post("/cameras/1", data={"name": "Salão", "url": "0", "analysis": "falls", "device": "",
                                    "falls_seconds": "20"})
    with client.app.state.services.db.session() as session:
        stored = json.loads(session.get(Camera, 1).model_settings)
    assert stored == {"kinds": ["fall"], "confidence": 0.5, "min_seconds": 20.0, "fps": 2.0}
    assert 'name="falls_seconds" min="0" max="600" step="1" value="20"' in client.get("/cameras/1").text


MODELS = Path("models")


@pytest.mark.skipif(not (MODELS / "fire_smoke_yolo26n.onnx").exists(), reason="fire and smoke isn't downloaded here")
def test_the_real_fire_model_finds_nothing_in_an_empty_picture():
    detector = FireDetector(MODELS / "fire_smoke_yolo26n.onnx")
    assert detector.detect(np.full((720, 1280, 3), 128, np.uint8), min_score=0.5) == []


@pytest.mark.skipif(not (MODELS / "pose_yolov8s.onnx").exists(), reason="falls isn't downloaded here")
def test_the_real_pose_model_finds_nobody_in_an_empty_picture():
    detector = FallDetector(MODELS / "pose_yolov8s.onnx")
    start = time.perf_counter()
    assert detector.detect(np.full((720, 1280, 3), 128, np.uint8), min_score=0.5) == []
    assert time.perf_counter() - start < 5
