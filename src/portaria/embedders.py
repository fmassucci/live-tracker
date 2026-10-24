"""Face embedding models. Each returns L2-normalized vectors, so a dot product is a cosine similarity."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
import onnxruntime as ort

from portaria.config import RECOGNITION_MODELS
from portaria.faces import Face, align
from portaria.models import model_path


class Embedder(Protocol):
    name: str

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray: ...


def _normalize(vector: np.ndarray) -> np.ndarray:
    vector = vector.astype(np.float32).ravel()
    return vector / np.linalg.norm(vector)


class AuraFace:
    """AuraFace v1: ResNet-100 trained with the ArcFace loss, Apache-2.0. 512-d embeddings."""

    name = "auraface"

    def __init__(self, path: Path) -> None:
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray:
        # The preprocessing InsightFace applies to this model: RGB, scaled to [-1, 1].
        blob = cv2.dnn.blobFromImage(align(image, face), 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        return _normalize(self._session.run(None, {self._input: blob})[0])


class SFace:
    """OpenCV Zoo SFace, Apache-2.0. 128-d embeddings; lighter and less accurate than AuraFace."""

    name = "sface"

    def __init__(self, path: Path) -> None:
        self._net = cv2.FaceRecognizerSF.create(str(path), "")

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray:
        return _normalize(self._net.feature(self._net.alignCrop(image, face.yunet_row())))


def load_embedder(name: str, models_dir: Path) -> Embedder:
    if name == "auraface":
        return AuraFace(model_path(models_dir, "auraface"))
    if name == "sface":
        return SFace(model_path(models_dir, "sface"))
    raise ValueError(f"unknown recognition model '{name}' (choose from {', '.join(RECOGNITION_MODELS)})")
