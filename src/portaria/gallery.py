"""The enrolled faces for one recognition model, and who each one belongs to."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from portaria.config import DetectionConfig
from portaria.embedders import Embedder
from portaria.faces import FaceDetector, photo_problem
from portaria.images import read_image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass(frozen=True)
class Match:
    person: str       # label of the best-matching person
    score: float      # cosine similarity to that person's best-matching template
    runner_up: float  # best score of anyone else; -1 when only one person is enrolled

    @property
    def margin(self) -> float:
        return self.score - self.runner_up


class Gallery:
    """Templates (embeddings) for one model, several per person; a person's score is their best template.

    `labels` names the person behind each template: a person id from the database, or a folder name
    for `evaluate`. `names` maps labels to display names. Embeddings only make sense for the model
    that produced them, so each model has its own gallery.
    """

    def __init__(self, model: str, labels: list[str], vectors, names: dict[str, str] | None = None) -> None:
        self.model = model
        self.labels = labels
        self.vectors = np.asarray(vectors, np.float32).reshape(len(labels), -1) if labels else np.zeros((0, 0), np.float32)
        self.people = sorted(set(labels))
        self.names = names or {label: label for label in self.people}
        self._person_index = np.array([self.people.index(label) for label in labels], dtype=np.int64)

    def name(self, label: str) -> str:
        return self.names.get(label, label)

    def match(self, vector: np.ndarray) -> Match | None:
        if not self.labels:
            return None
        best = np.full(len(self.people), -1.0, dtype=np.float32)
        np.maximum.at(best, self._person_index, self.vectors @ vector)
        order = np.argsort(best)[::-1]
        runner_up = float(best[order[1]]) if len(order) > 1 else -1.0
        return Match(self.people[order[0]], float(best[order[0]]), runner_up)


class PhotoRejected(Exception):
    pass


def embed_photo(path: Path, detector: FaceDetector, embedder: Embedder, quality: DetectionConfig) -> np.ndarray:
    image = read_image(path)
    if image is None:
        raise PhotoRejected("not an image")
    faces = detector.detect(image)
    problem = photo_problem(image, faces, quality)
    if problem:
        raise PhotoRejected(problem)
    return embedder.embed(image, faces[0])


@dataclass(frozen=True)
class Enrollment:
    gallery: Gallery
    sources: list[Path]                # the photo behind each template
    rejected: list[tuple[Path, str]]   # photo, reason


def build_gallery(enroll_dir: Path, detector: FaceDetector, embedder: Embedder, quality: DetectionConfig) -> Enrollment:
    """A gallery from photo folders, one per person (data/enroll/<name>/). Used by `evaluate`."""
    labels, vectors, sources, rejected = [], [], [], []
    folders = sorted(p for p in enroll_dir.iterdir() if p.is_dir()) if enroll_dir.exists() else []
    for folder in folders:
        for photo in sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES):
            try:
                vectors.append(embed_photo(photo, detector, embedder, quality))
            except PhotoRejected as e:
                rejected.append((photo, str(e)))
                continue
            labels.append(folder.name)
            sources.append(photo)
    return Enrollment(Gallery(embedder.name, labels, np.array(vectors)), sources, rejected)


def closest_pair(gallery: Gallery) -> tuple[str, str, float] | None:
    """The two different people whose photos look most alike, and their score."""
    if len(gallery.people) < 2:
        return None
    labels = np.array(gallery.labels)
    scores = gallery.vectors @ gallery.vectors.T
    scores[labels[:, None] == labels[None, :]] = -1.0
    i, j = np.unravel_index(np.argmax(scores), scores.shape)
    return str(labels[i]), str(labels[j]), float(scores[i, j])
