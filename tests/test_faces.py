import cv2
import numpy as np

from live_tracker.config import DetectionConfig
from live_tracker.faces import ARCFACE_TEMPLATE, TURNED_AWAY, Face, assess, similarity_transform, upright, yaw


def _rotation(degrees: float) -> np.ndarray:
    a = np.deg2rad(degrees)
    return np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])


def _face(landmarks: np.ndarray) -> Face:
    return Face(box=np.array([0.0, 0.0, 100.0, 100.0]), landmarks=landmarks, score=0.9)


def test_similarity_transform_recovers_rotation_scale_and_shift():
    rotation, scale, shift = _rotation(20), 2.5, np.array([30.0, -12.0])
    dst = scale * ARCFACE_TEMPLATE @ rotation.T + shift
    matrix = similarity_transform(ARCFACE_TEMPLATE, dst)
    np.testing.assert_allclose(matrix[:, :2], scale * rotation, atol=1e-9)
    np.testing.assert_allclose(matrix[:, 2], shift, atol=1e-9)


def test_frontal_face_has_no_yaw_even_when_the_head_is_tilted():
    assert abs(yaw(_face(ARCFACE_TEMPLATE))) < 0.01
    tilted = (ARCFACE_TEMPLATE - 56) @ _rotation(25).T * 3 + 400
    assert abs(yaw(_face(tilted))) < 0.01


class _OrientationDetector:
    """Behaves like YuNet: surest about an upright face, less sure upside down, blind to it sideways.

    The test image is marked white at its top-left corner, so the marker's position tells how it was turned.
    """

    def detect(self, image):
        h, w = image.shape[:2]
        box, landmarks = np.array([50.0, 50.0, 336.0, 336.0]), ARCFACE_TEMPLATE * 3 + 50
        if image[0, 0, 0] == 255:
            return [Face(box=box, landmarks=landmarks, score=0.95)]
        if image[h - 1, w - 1, 0] == 255:
            return [Face(box=box, landmarks=landmarks, score=0.88)]
        return []


def test_sideways_and_upside_down_photos_are_turned_upright():
    image = np.zeros((480, 480, 3), np.uint8)
    image[0, 0] = 255
    for rotation in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        turned, faces = upright(cv2.rotate(image, rotation), _OrientationDetector())
        assert turned[0, 0, 0] == 255 and faces


def test_enrollment_allows_more_head_turn_than_the_door():
    landmarks = ARCFACE_TEMPLATE.copy()
    landmarks[2, 0] += 11  # yaw about 0.31: around 35 degrees
    face = Face(box=np.array([0.0, 0.0, 200.0, 200.0]), landmarks=landmarks, score=0.9)
    crop = np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8)
    cfg = DetectionConfig()
    assert assess(face, crop, cfg).problem == TURNED_AWAY
    assert assess(face, crop, cfg, max_yaw=cfg.enroll_max_yaw).ok


def test_yaw_grows_when_the_nose_moves_off_center():
    landmarks = ARCFACE_TEMPLATE.copy()
    landmarks[2, 0] += 10  # nose 10 px toward the image's right, eyes 35 px apart
    assert yaw(_face(landmarks)) > 0.25
    landmarks[2, 0] -= 20
    assert yaw(_face(landmarks)) < -0.25
