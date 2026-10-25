from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import FakeDetector, FakeEmbedder, place_model_file, textured

from portaria import service
from portaria.camera import Frame
from portaria.gallery import Gallery
from portaria.liveness import MiniFASNet, NoLiveness, crop, load_liveness
from portaria.pipeline import Recognizer
from portaria.service import VisionService


def test_the_crop_keeps_its_size_and_moves_inside_the_picture():
    image = np.zeros((480, 640, 3), np.uint8)
    image[:, :320] = 255  # left half white: shows where the crop was taken
    centered = crop(image, np.array([280.0, 200, 80, 80]), 2.7, 80)
    assert centered.shape == (80, 80, 3) and 0 < centered.mean() < 255  # both halves
    at_the_edge = crop(image, np.array([0.0, 0, 80, 80]), 2.7, 80)  # 216x216 around a face in the corner
    assert at_the_edge.shape == (80, 80, 3) and at_the_edge.mean() == 255  # moved right and down, not cut
    huge = crop(image, np.array([100.0, 100, 400, 400]), 4.0, 80)  # can't grow 4 times: the whole height
    assert huge.shape == (80, 80, 3)


def test_without_its_model_faces_are_not_checked(cfg, monkeypatch):
    assert load_liveness(cfg.paths.models_dir, 0.5).name == "none"
    for key in ("minifasnet_v2", "minifasnet_v1se"):
        place_model_file(cfg.paths.models_dir, monkeypatch, key)  # matches its checksum, but isn't a real model
    assert load_liveness(cfg.paths.models_dir, 0.5).name == "none"  # face recognition goes on without it


class Photo:
    name = "photo"

    def is_live(self, image, face) -> bool:
        return False


def test_a_face_judged_a_photo_is_decided_as_one_and_named_on_the_picture(cfg):
    image = textured((200, 120, 80))
    embedder = FakeEmbedder("auraface")
    gallery = Gallery("auraface", ["7"], [embedder.embed(image, None)], {"7": "Ana"})
    pt = {"Photo or screen suspected": "Suspeita de foto ou tela"}
    recognizer = Recognizer(cfg, FakeDetector(), embedder, gallery, Photo(), lambda text: pt.get(text, text))
    results = [recognizer.process(Frame(image, i, i * 0.1, bytes([i])), repeated=False) for i in range(3)]
    mark = results[0].overlays[-1]
    assert (mark.label, mark.tone) == ("Suspeita de foto ou tela: Ana", "bad")
    assert results[-1].decision.kind == "spoof"  # never "recognized": the door stays closed


def test_face_cameras_start_checking_once_the_model_is_downloaded(cfg, monkeypatch):
    monkeypatch.setattr(service, "PreviewServer", lambda host, port: None)
    vision = VisionService(cfg)
    recognizer = Recognizer(cfg, FakeDetector(), FakeEmbedder("auraface"), vision.gallery, vision._liveness)
    vision._workers = {1: SimpleNamespace(analyzer=recognizer, status={}), 2: SimpleNamespace(analyzer=None, status={})}
    vision._reload_liveness()
    assert recognizer.liveness.name == "none"
    check = SimpleNamespace(name="minifasnet")
    monkeypatch.setattr(service, "load_liveness", lambda models_dir, threshold: check)
    vision._reload_liveness()
    assert recognizer.liveness is check and vision._status()["liveness"] == "minifasnet"
    vision._workers = {}


MODELS = Path("models")


@pytest.mark.skipif(not (MODELS / "liveness_minifasnet_v2.onnx").exists(), reason="liveness isn't downloaded here")
def test_the_real_models_say_how_likely_a_face_is_real():
    check = load_liveness(MODELS, 0.5)
    assert isinstance(check, MiniFASNet)
    face = SimpleNamespace(box=np.array([200.0, 120, 160, 200]))
    assert 0.0 <= check.score(textured((150, 110, 90), 640), face) <= 1.0
    assert isinstance(NoLiveness().is_live(np.zeros((8, 8, 3), np.uint8), face), bool)
