"""Face detection (YuNet), alignment to the ArcFace crop, and quality checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from live_tracker.config import DetectionConfig

# Why a face or photo can't be used. The web UI translates these for people.
NO_FACE = "no face found"
SEVERAL_FACES = "more than one person"
TOO_SMALL = "face too small"
TURNED_AWAY = "face turned away"
BLURRY = "blurry"

# Where ArcFace-family models expect the 5 landmarks in their 112x112 input. The order is YuNet's:
# right eye, left eye, nose tip, right and left mouth corners. These are the subject's right and
# left, so the right eye is on the image's left.
ARCFACE_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]]
)


@dataclass(frozen=True)
class Face:
    box: np.ndarray        # x, y, width, height in full-frame pixels
    landmarks: np.ndarray  # 5x2, same order as ARCFACE_TEMPLATE
    score: float

    @property
    def width(self) -> float:
        return float(self.box[2])

    def yunet_row(self) -> np.ndarray:
        """The detector's raw output format, which OpenCV's SFace alignCrop expects."""
        return np.concatenate([self.box, self.landmarks.ravel(), [self.score]]).astype(np.float32)


class FaceDetector:
    def __init__(self, model_path: Path, score_threshold: float = 0.8, detect_width: int = 640) -> None:
        self._net = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold, 0.3, 500)
        self._detect_width = detect_width

    def detect(self, image: np.ndarray) -> list[Face]:
        """Faces, largest first. Detection runs on a downscaled copy; coordinates are full-size."""
        height, width = image.shape[:2]
        if width > self._detect_width:
            size = (self._detect_width, round(height * self._detect_width / width))
            small = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        else:
            small = image
        scale = np.array([width / small.shape[1], height / small.shape[0]])
        self._net.setInputSize((small.shape[1], small.shape[0]))
        _, rows = self._net.detect(small)
        if rows is None:
            return []
        faces = [
            Face(box=row[0:4] * np.tile(scale, 2), landmarks=row[4:14].reshape(5, 2) * scale, score=float(row[14]))
            for row in rows.astype(np.float64)
        ]
        return sorted(faces, key=lambda face: face.width, reverse=True)


def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """2x3 matrix (rotation, uniform scale, translation) that best maps src points onto dst (Umeyama)."""
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - src_mean, dst - dst_mean
    covariance = dst_c.T @ src_c / len(src)
    u, singular, vt = np.linalg.svd(covariance)
    signs = np.array([1.0, -1.0 if np.linalg.det(covariance) < 0 else 1.0])
    rotation = u @ np.diag(signs) @ vt
    scale = (singular * signs).sum() / src_c.var(axis=0).sum()
    translation = dst_mean - scale * rotation @ src_mean
    return np.hstack([scale * rotation, translation[:, None]])


def align(image: np.ndarray, face: Face, size: int = 112) -> np.ndarray:
    """The face warped into the standard crop that ArcFace-family models are trained on."""
    matrix = similarity_transform(face.landmarks, ARCFACE_TEMPLATE * size / 112)
    return cv2.warpAffine(image, matrix, (size, size), borderValue=0)


@dataclass(frozen=True)
class Quality:
    problem: str  # empty when the face is usable
    yaw: float
    sharpness: float

    @property
    def ok(self) -> bool:
        return not self.problem


def yaw(face: Face) -> float:
    """How far the nose sits from the eyes' midpoint along the eye line, relative to eye distance.

    About 0 when facing the camera, roughly +/-0.25 at 30 degrees of head turn. Head tilt doesn't affect it.
    """
    right_eye, left_eye, nose = face.landmarks[:3]
    eye_axis = left_eye - right_eye
    distance = float(np.linalg.norm(eye_axis))
    if distance < 1.0:
        return float("inf")
    return float(np.dot(nose - (right_eye + left_eye) / 2, eye_axis) / distance**2)


def sharpness(crop: np.ndarray) -> float:
    """Variance of the Laplacian: low for blurry crops."""
    return float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def assess(face: Face, crop: np.ndarray, cfg: DetectionConfig, max_yaw: float | None = None) -> Quality:
    turn, sharp = yaw(face), sharpness(crop)
    if face.width < cfg.min_face_px:
        problem = TOO_SMALL
    elif abs(turn) > (cfg.max_yaw if max_yaw is None else max_yaw):
        problem = TURNED_AWAY
    elif sharp < cfg.min_sharpness:
        problem = BLURRY
    else:
        problem = ""
    return Quality(problem, turn, sharp)


def photo_problem(image: np.ndarray, faces: list[Face], cfg: DetectionConfig) -> str:
    """Why a photo can't be used for enrollment, or "" when it can. `faces` must come from this image."""
    if not faces:
        return NO_FACE
    if len(faces) > 1 and faces[1].width > 0.5 * faces[0].width:
        return SEVERAL_FACES
    return assess(faces[0], align(image, faces[0]), cfg, max_yaw=cfg.enroll_max_yaw).problem


def upright(image: np.ndarray, detector: FaceDetector) -> tuple[np.ndarray, list[Face]]:
    """The image turned so the face stands upright: phone photos often keep their rotation only in EXIF data.

    The detector also finds faces upside down, and then places the landmarks as if they were upright,
    so finding a face isn't enough; it is most confident about the upright one.
    """
    best: tuple[np.ndarray, list[Face]] = (image, [])
    for rotation in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        turned = image if rotation is None else cv2.rotate(image, rotation)
        faces = detector.detect(turned)
        if faces and (not best[1] or faces[0].score > best[1][0].score):
            best = (turned, faces)
    return best
