from portaria.config import DecisionConfig
from portaria.decision import DecisionMaker, Observation

CONFIG = DecisionConfig(
    window_s=1.5, min_votes=3, rearm_after_s=1.0, person_cooldown_s=5.0, unknown_cooldown_s=10.0, unknown_after_s=2.0
)


def _maker() -> DecisionMaker:
    return DecisionMaker(CONFIG, accept_threshold=0.5, min_margin=0.1)


def _obs(at: float, person: str = "ana", score: float = 0.7, margin: float = 0.3, live: bool = True) -> Observation:
    return Observation(at, person, score, margin, live)


def _feed(maker: DecisionMaker, start: int, stop: int, **kwargs) -> list:
    """Observations every 0.1 s from start/10 to stop/10 seconds; returns the decisions made."""
    return [d for t in range(start, stop) if (d := maker.observe(_obs(t / 10, **kwargs)))]


def test_recognizes_after_enough_agreeing_frames():
    maker = _maker()
    assert maker.observe(_obs(0.0)) is None
    assert maker.observe(_obs(0.1)) is None
    decision = maker.observe(_obs(0.2))
    assert decision is not None and decision.kind == "recognized" and decision.person == "ana"


def test_frames_outside_the_window_do_not_count():
    maker = _maker()
    maker.observe(_obs(0.0))
    maker.observe(_obs(0.1))
    assert maker.observe(_obs(2.0)) is None


def test_low_score_or_small_margin_never_recognizes():
    assert not [d for d in _feed(_maker(), 0, 10, score=0.45) if d.kind == "recognized"]
    assert not [d for d in _feed(_maker(), 0, 10, margin=0.05) if d.kind == "recognized"]


def test_lingering_face_is_decided_once_until_it_leaves():
    maker = _maker()
    assert len(_feed(maker, 0, 100)) == 1  # ten seconds in front of the camera
    for t in range(100, 115):  # then 1.5 s with nobody at the door
        maker.no_face(t / 10)
    assert len(_feed(maker, 115, 130)) == 1


def test_frozen_stream_does_not_reopen_after_the_cooldown():
    maker = _maker()
    assert len(_feed(maker, 0, 3)) == 1
    for t in range(3, 60):  # the stream repeats the same frame: face present, frames unusable
        maker.face_seen(t / 10)
    assert _feed(maker, 60, 70) == []


def test_cooldown_blocks_a_quick_return():
    maker = _maker()
    assert len(_feed(maker, 0, 3)) == 1  # decided at 0.2 s
    for t in range(3, 15):
        maker.no_face(t / 10)
    assert _feed(maker, 15, 30) == []  # back at 1.5 s, still within the 5 s cooldown
    for t in range(30, 45):
        maker.no_face(t / 10)
    assert len(_feed(maker, 55, 60)) == 1  # back after the cooldown


def test_next_person_in_line_is_recognized_without_a_gap():
    maker = _maker()
    assert [d.person for d in _feed(maker, 0, 5)] == ["ana"]
    assert [d.person for d in _feed(maker, 5, 10, person="bia")] == ["bia"]


def test_unmatched_faces_become_one_unknown_event_once_the_face_has_settled():
    maker = _maker()
    assert _feed(maker, 0, 20, score=0.2) == []  # less than 2 s at the door
    decisions = _feed(maker, 20, 30, score=0.2)
    assert [d.kind for d in decisions] == ["unknown"]
    assert decisions[0].person is None


def test_after_a_recognition_the_rest_of_the_visit_is_not_recorded_as_unknown():
    maker = _maker()
    assert [d.kind for d in _feed(maker, 0, 5)] == ["recognized"]
    assert _feed(maker, 5, 40, score=0.2) == []  # standing up, turning away, walking off
    for t in range(40, 52):  # nobody at the door: the visit is over
        maker.no_face(t / 10)
    assert [d.kind for d in _feed(maker, 52, 80, score=0.2)] == ["unknown"]  # a new visit, a stranger


def test_a_known_face_turning_toward_the_camera_is_not_first_recorded_as_unknown():
    maker = _maker()
    assert _feed(maker, 0, 5, score=0.15) == []  # the first frames match poorly
    assert [d.kind for d in _feed(maker, 5, 10)] == ["recognized"]


def test_failed_liveness_is_a_spoof_not_an_entry():
    decisions = _feed(_maker(), 0, 10, live=False)
    assert [d.kind for d in decisions] == ["spoof"]


def test_nobody_enrolled_is_unknown():
    decisions = _feed(_maker(), 0, 25, person=None, score=-1.0, margin=0.0)
    assert [d.kind for d in decisions] == ["unknown"]
