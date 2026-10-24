import numpy as np
import pytest

from portaria.gallery import Gallery, closest_pair


def _unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def _gallery() -> Gallery:
    vectors = np.stack([_unit(1, 0, 0), _unit(0.6, 0.8, 0), _unit(0, 0, 1)])
    return Gallery("m", ["1", "1", "2"], vectors, {"1": "Ana", "2": "Bia"})


def test_best_template_wins_and_runner_up_is_someone_else():
    probe = _unit(0.6, 0.8, 0.1)
    match = _gallery().match(probe)
    assert match.person == "1"
    assert match.score == pytest.approx(float(probe @ _unit(0.6, 0.8, 0)))
    assert match.runner_up == pytest.approx(float(probe @ _unit(0, 0, 1)))
    assert match.margin == pytest.approx(match.score - match.runner_up)


def test_labels_have_display_names():
    gallery = _gallery()
    assert gallery.name("1") == "Ana"
    assert gallery.name("9") == "9"
    assert Gallery("m", ["Ana"], _unit(1, 0, 0)[None]).name("Ana") == "Ana"


def test_single_person_has_no_runner_up():
    match = Gallery("m", ["1"], _unit(1, 0, 0)[None]).match(_unit(1, 0, 0))
    assert match.score == pytest.approx(1.0)
    assert match.runner_up == -1.0


def test_empty_gallery_matches_nobody():
    assert Gallery("m", [], []).match(_unit(1, 0, 0)) is None


def test_closest_pair_ignores_photos_of_the_same_person():
    a, b, score = closest_pair(_gallery())
    assert {a, b} == {"1", "2"}
    assert score == pytest.approx(0.0, abs=1e-6)
