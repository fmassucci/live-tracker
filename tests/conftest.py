import socket
import threading

import numpy as np
import pytest

from portaria.config import Config
from portaria.db import Database
from portaria.faces import ARCFACE_TEMPLATE, Face
from portaria.people import People


class FakeDetector:
    """Finds one large, frontal face in any image, so tests don't need the model files."""

    def detect(self, image: np.ndarray) -> list[Face]:
        landmarks = ARCFACE_TEMPLATE * 3 + 100
        return [Face(box=np.array([80.0, 80.0, 336.0, 336.0]), landmarks=landmarks, score=0.99)]


class FakeEmbedder:
    """Embeds an image by its average color: equal images match, different colors don't."""

    def __init__(self, name: str) -> None:
        self.name = name

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray:
        vector = np.concatenate([image.reshape(-1, 3).mean(axis=0), [1.0]]).astype(np.float32)
        return vector / np.linalg.norm(vector)


class FakeTools:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.detector = FakeDetector()

    def embedder(self, model: str) -> FakeEmbedder:
        return FakeEmbedder(model)


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config()
    config.paths.data_dir = tmp_path / "data"
    config.paths.models_dir = tmp_path / "models"
    config.preview.port = _free_port()  # never a recognition service running on this machine
    return config


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def db(cfg) -> Database:
    database = Database(cfg.db_path)
    database.migrate()
    return database


@pytest.fixture
def people(cfg) -> People:
    return People(cfg, FakeTools())


def textured(color: tuple[int, int, int], size: int = 480) -> np.ndarray:
    """A sharp image of one dominant color: passes the blur check, embeds by its color."""
    rng = np.random.default_rng(sum(color))
    noise = rng.integers(-40, 40, (size, size, 3))
    return np.clip(np.array(color) + noise, 0, 255).astype(np.uint8)
