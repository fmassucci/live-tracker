import json
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeTools, place_model_file
from fastapi.testclient import TestClient

from live_tracker import service
from live_tracker.db import Camera
from live_tracker.dfine import DFineDetector, decode, threshold
from live_tracker.objects import ObjectAnalyzer, ObjectSettings
from live_tracker.service import CameraSpec, VisionService, thing_model
from live_tracker.web.app import create_app


def test_answers_become_things_in_the_picture():
    logits = np.full((300, 80), -9.0, np.float32)
    boxes = np.zeros((300, 4), np.float32)
    logits[3, 0], boxes[3] = 2.0, [0.5, 0.5, 0.2, 0.4]  # a person in the middle, sure (0.88)
    logits[7, 56], boxes[7] = 0.0, [0.1, 0.9, 0.1, 0.1]  # a chair, unsure (0.5)
    found = decode(logits, boxes, width=1920, height=1080, min_score=0.65)
    assert [(d.kind, round(d.score, 2)) for d in found] == [("person", 0.88)]
    assert found[0].box == pytest.approx([1920 * 0.4, 1080 * 0.3, 1920 * 0.2, 1080 * 0.4])


def test_each_sensitivity_level_has_its_own_threshold():
    assert (threshold(0.35), threshold(0.5), threshold(0.65)) == (0.55, 0.65, 0.75)
    assert threshold(0.2) == 0.55 and threshold(0.8) == 0.75  # kept within the levels


def test_the_model_choice_is_kept_and_old_settings_mean_yolox():
    assert ObjectSettings.parse('{"kinds": ["person"]}').model == ""
    chosen = ObjectSettings.of({"kinds": ["dog"], "model": "dfine_m"})
    assert ObjectSettings.parse(chosen.dumps()) == chosen
    assert "model" not in json.loads(ObjectSettings().dumps())  # YOLOX-S writes what it wrote before
    assert ObjectSettings.of({"model": "unicorn"}).model == "" and ObjectSettings.of({"model": ["x"]}).model == ""


class FakeSource:
    url, ended = "0", False

    def stop(self) -> None:
        pass


def test_a_camera_runs_the_model_its_settings_chose(cfg, monkeypatch):
    monkeypatch.setattr(service, "PreviewServer", lambda host, port: None)
    vision = VisionService(cfg)
    made = []
    monkeypatch.setattr(vision, "make_detector", lambda: made.append("yolox") or object())
    monkeypatch.setattr(vision, "make_dfine_detector", lambda: made.append("dfine") or object())
    dfine = CameraSpec(1, "Salão", "0", analysis="objects", settings='{"kinds": ["chair"], "model": "dfine_m"}')
    worker = service.CameraWorker(dfine, vision, FakeSource())
    assert isinstance(worker.analyzer, ObjectAnalyzer) and made == ["dfine"]
    service.CameraWorker(CameraSpec(2, "Caixa", "0", analysis="objects"), vision, FakeSource())
    assert made == ["dfine", "yolox"]
    assert thing_model(dfine) == "dfine-m" and thing_model(CameraSpec(2, "Caixa", "0", analysis="objects")) == "yolox"


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


FIELDS = {"name": "Salão", "url": "0", "analysis": "objects", "device": "", "kinds": ["person", "chair"],
          "confidence": "0.5", "min_seconds": "2", "fps": "2"}


def test_a_camera_chooses_d_fine_once_it_is_downloaded(client, cfg, monkeypatch):
    place_model_file(cfg.paths.models_dir, monkeypatch, "yolox")
    form = client.get("/cameras/new").text
    assert 'name="objects_model" value="dfine_m"  disabled' in form and "Baixar outros modelos" in form
    place_model_file(cfg.paths.models_dir, monkeypatch, "dfine_m")
    form = client.get("/cameras/new").text
    assert 'name="objects_model" value="dfine_m" >' in form  # can be chosen
    client.post("/cameras/new", data={**FIELDS, "objects_model": "dfine_m"})
    with client.app.state.services.db.session() as session:
        assert json.loads(session.get(Camera, 1).model_settings)["model"] == "dfine_m"
    assert 'name="objects_model" value="dfine_m" checked' in client.get("/cameras/1").text


def test_people_and_objects_can_run_on_d_fine_alone(client, cfg, monkeypatch):
    place_model_file(cfg.paths.models_dir, monkeypatch, "dfine_m")
    assert 'value="objects"' in client.get("/cameras/new").text  # YOLOX-S isn't downloaded


def test_a_model_a_camera_runs_on_stays_until_the_camera_changes(client, cfg, monkeypatch):
    for key in ("yolox", "dfine_m"):
        place_model_file(cfg.paths.models_dir, monkeypatch, key)
    client.post("/cameras/new", data={**FIELDS, "objects_model": "dfine_m"})
    refused = client.post("/settings/models/dfine_m/remove")
    assert "Estas câmeras usam este modelo: Salão." in refused.text
    removed = client.post("/settings/models/objects/remove")  # YOLOX-S: no camera runs on it
    assert "Modelo removido: Pessoas e objetos." in removed.text
    assert (cfg.paths.models_dir / "dfine_m.onnx").exists() and not (cfg.paths.models_dir / "yolox.onnx").exists()


MODEL = Path("models/object_detection_dfine_m.onnx")


@pytest.mark.skipif(not MODEL.exists(), reason="D-FINE-M isn't downloaded here")
def test_the_real_model_finds_nothing_in_an_empty_picture():
    detector = DFineDetector(MODEL)
    start = time.perf_counter()
    assert detector.detect(np.full((720, 1280, 3), 128, np.uint8), min_score=0.5) == []
    assert time.perf_counter() - start < 10
