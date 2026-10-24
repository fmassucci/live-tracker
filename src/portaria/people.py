"""The people registry: who the system knows, their face photos and embeddings.

It is the base every module builds on (the door today, others later), and is shared by the web UI,
the recognition service and the command line.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from portaria.config import RECOGNITION_MODELS, Config
from portaria.db import FACES_VERSION, Embedding, Person, Photo, bump
from portaria.embedders import Embedder, load_embedder
from portaria.events import face_crop, face_path
from portaria.faces import FaceDetector, photo_problem, upright
from portaria.gallery import Gallery
from portaria.images import read_image, write_jpeg
from portaria.models import model_path

CATEGORIES = ("employee", "contractor", "visitor")
MAX_PHOTO_SIDE = 1600  # plenty for a 112x112 face crop, and keeps photo files small


class FaceTools:
    """The face detector and embedders, loaded on first use (a few seconds, ~300 MB) and used one at a time."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self.lock = threading.Lock()
        self._detector: FaceDetector | None = None
        self._embedders: dict[str, Embedder] = {}

    @property
    def detector(self) -> FaceDetector:
        if self._detector is None:
            self._detector = FaceDetector(
                model_path(self._cfg.paths.models_dir, "yunet"),
                self._cfg.detection.score_threshold,
                self._cfg.detection.detect_width,
            )
        return self._detector

    def embedder(self, model: str) -> Embedder:
        if model not in self._embedders:
            self._embedders[model] = load_embedder(model, self._cfg.paths.models_dir)
        return self._embedders[model]


@dataclass(frozen=True)
class _Prepared:
    image: np.ndarray                # shrunk and upright, as it will be stored
    vectors: dict[str, np.ndarray]   # embedding per recognition model
    face_width: int


@dataclass(frozen=True)
class PhotoCheck:
    problem: str = ""              # a faces.py problem constant; empty when the photo is usable
    looks_like: str | None = None  # someone already registered whom this photo also matches
    photo_id: int | None = None    # set when the photo was saved

    @property
    def ok(self) -> bool:
        return not self.problem


class People:
    def __init__(self, cfg: Config, tools: FaceTools) -> None:
        self._cfg = cfg
        self._tools = tools

    def inspect(self, session: Session, image: np.ndarray, person_id: int | None = None) -> PhotoCheck:
        """Would this photo be accepted? Nothing is saved. Used to give feedback as photos are chosen."""
        prepared = self._prepare(image)
        if isinstance(prepared, str):
            return PhotoCheck(prepared)
        return PhotoCheck(looks_like=self._looks_like(session, prepared.vectors, person_id))

    def add_photo(self, session: Session, person: Person, image: np.ndarray, source: str) -> PhotoCheck:
        prepared = self._prepare(image)
        if isinstance(prepared, str):
            return PhotoCheck(prepared)
        photo = Photo(person=person, path="", source=source, face_width=prepared.face_width)
        session.add(photo)
        session.flush()  # assigns photo.id, used in the file name
        photo.path = f"photos/{person.id}/{photo.id}.jpg"
        write_jpeg(self._cfg.paths.data_dir / photo.path, prepared.image)
        for model, vector in prepared.vectors.items():
            session.add(Embedding(photo=photo, model=model, vector=vector.tobytes()))
        bump(session, FACES_VERSION)
        return PhotoCheck(looks_like=self._looks_like(session, prepared.vectors, person.id), photo_id=photo.id)

    def delete_photo(self, session: Session, photo: Photo) -> None:
        (self._cfg.paths.data_dir / photo.path).unlink(missing_ok=True)
        (self._cfg.paths.data_dir / face_path(photo.path)).unlink(missing_ok=True)
        session.delete(photo)
        bump(session, FACES_VERSION)

    def face_image(self, photo_path: str) -> Path:
        """A close crop of the face in a stored photo, for round avatars: made on first use and kept
        next to the photo. The whole photo when there is no face to crop."""
        path = self._cfg.paths.data_dir / photo_path
        crop_path = self._cfg.paths.data_dir / face_path(photo_path)
        if crop_path.is_file():
            return crop_path
        image = read_image(path)
        if image is None:
            return path
        with self._tools.lock:  # also keeps two requests from writing the same crop
            if not crop_path.is_file():
                try:
                    faces = self._tools.detector.detect(image)
                except FileNotFoundError:  # models not downloaded yet
                    return path
                if not faces:
                    return path
                face = max(faces, key=lambda f: f.width)
                temporary = crop_path.with_suffix(".tmp")
                write_jpeg(temporary, face_crop(image, face.box, margin=0.45, size=320))
                temporary.replace(crop_path)  # readers never see a half-written file
        return crop_path

    def delete_person(self, session: Session, person: Person) -> None:
        """Removes the person, their photos and embeddings. Past events keep the name until they expire."""
        shutil.rmtree(self._cfg.paths.data_dir / "photos" / str(person.id), ignore_errors=True)
        session.delete(person)
        bump(session, FACES_VERSION)

    def _prepare(self, image: np.ndarray) -> _Prepared | str:
        """The photo ready to store, with its embeddings for every model, or why it can't be used."""
        with self._tools.lock:
            image, faces = upright(_shrink(image), self._tools.detector)
            problem = photo_problem(image, faces, self._cfg.detection)
            if problem:
                return problem
            vectors = {model: self._tools.embedder(model).embed(image, faces[0]) for model in RECOGNITION_MODELS}
        return _Prepared(image, vectors, int(faces[0].width))

    def _looks_like(self, session: Session, vectors: dict[str, np.ndarray], person_id: int | None) -> str | None:
        model = self._cfg.recognition.model
        match = load_gallery(session, model, exclude=person_id).match(vectors[model])
        if match and match.score >= self._cfg.recognition.accept_threshold:
            return load_gallery(session, model).name(match.person)
        return None


def load_gallery(session: Session, model: str, exclude: int | None = None) -> Gallery:
    """Every active person's embeddings for `model`, labeled with the person id."""
    query = (
        select(Person.id, Person.name, Embedding.vector)
        .join(Photo, Photo.person_id == Person.id)
        .join(Embedding, Embedding.photo_id == Photo.id)
        .where(Person.active, Embedding.model == model)
    )
    if exclude is not None:
        query = query.where(Person.id != exclude)
    rows = session.execute(query).all()
    vectors = np.array([np.frombuffer(row.vector, np.float32) for row in rows])
    return Gallery(model, [str(row.id) for row in rows], vectors, {str(row.id): row.name for row in rows})


def door_allowed(session: Session) -> set[str]:
    """Labels of the people the door may open for."""
    return {str(i) for i in session.scalars(select(Person.id).where(Person.active, Person.door_access))}


def _shrink(image: np.ndarray) -> np.ndarray:
    scale = MAX_PHOTO_SIDE / max(image.shape[:2])
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
