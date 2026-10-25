import numpy as np

from portaria.camera import Frame
from portaria.pipeline import RateMeter, RepeatedFrames

IMAGE = np.zeros((8, 8, 3), np.uint8)


def _frame(at: float, signature: bytes) -> Frame:
    return Frame(IMAGE, seq=0, captured_at=at, signature=signature)


def test_a_picture_handed_out_twice_is_skipped_not_called_frozen():
    repeats = RepeatedFrames(frozen_after_s=1.0)
    first = _frame(0.0, b"a")
    assert not repeats.skip(first) and not repeats.frozen(first)
    again = _frame(0.03, b"a")
    assert repeats.skip(again)


def test_a_picture_that_stays_the_same_means_the_stream_froze():
    repeats = RepeatedFrames(frozen_after_s=1.0)
    repeats.skip(_frame(0.0, b"a"))
    repeats.skip(_frame(0.1, b"a"))
    stuck = _frame(1.2, b"a")
    assert not repeats.skip(stuck) and repeats.frozen(stuck)
    moving = _frame(1.3, b"b")
    assert not repeats.skip(moving) and not repeats.frozen(moving)


def test_the_frame_rate_counts_frames_and_ignores_a_burst_after_a_slow_look():
    now = [0.0]
    meter = RateMeter(clock=lambda: now[0])
    for i in range(20):  # 10 frames per second, with one frame handled right after another
        now[0] = i * 0.1 + (0.00003 if i == 10 else 0.0)
        rate = meter.tick()
    assert 9 < rate < 11
