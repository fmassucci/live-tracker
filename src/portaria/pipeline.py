"""Recognition for one frame: face -> quality -> embedding -> match -> decision."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from portaria.camera import Frame
from portaria.config import Config
from portaria.decision import Decision, DecisionMaker, Observation
from portaria.drawing import Overlay
from portaria.embedders import Embedder
from portaria.faces import Face, FaceDetector, align, assess
from portaria.gallery import Gallery
from portaria.liveness import LivenessCheck

FROZEN = "frozen frame"
FROZEN_AFTER_S = 1.0  # the same picture for this long means the stream froze


@dataclass
class Result:
    state: str = "idle"  # "idle" (nobody), "adjust" (face not usable yet), "checking" (recognizing)
    problem: str = ""    # why the face isn't usable, while adjusting
    overlays: list[Overlay] = field(default_factory=list)
    face: Face | None = None
    decision: Decision | None = None


class Recognizer:
    """One frame in, at most one decision out. Only the largest face, the person closest to the camera, counts."""

    def __init__(
        self,
        cfg: Config,
        detector: FaceDetector,
        embedder: Embedder,
        gallery: Gallery,
        liveness: LivenessCheck,
        translate: Callable[[str], str] = lambda text: text,
    ) -> None:
        self._cfg = cfg
        self._detector = detector
        self._embedder = embedder
        self._liveness = liveness
        self._translate = translate
        self.gallery = gallery  # replaced by the service when people change
        self.decider = DecisionMaker(cfg.decision, cfg.recognition.accept_threshold, cfg.recognition.min_margin)

    def process(self, frame: Frame, repeated: bool) -> Result:
        faces = self._detector.detect(frame.image)
        if not faces:
            self.decider.no_face(frame.captured_at)
            return Result()
        face = faces[0]
        result = Result("adjust", overlays=[Overlay(other, "", "dim") for other in faces[1:]], face=face)
        quality = assess(face, align(frame.image, face), self._cfg.detection)
        if not quality.ok or repeated:
            self.decider.face_seen(frame.captured_at)
            result.problem = quality.problem or FROZEN
            result.overlays.append(Overlay(face, self._translate(result.problem), "warn"))
            return result

        result.state = "checking"
        match = self.gallery.match(self._embedder.embed(frame.image, face))
        observation = Observation(
            at=frame.captured_at,
            person=match.person if match else None,
            score=match.score if match else -1.0,
            margin=match.margin if match else 0.0,
            live=self._liveness.is_live(frame.image, face),
        )
        accepted = self.decider.matches(observation) and observation.live
        if not match:
            label = ""
        elif accepted:
            label = f"{self.gallery.name(match.person)} {match.score:.2f}"
        else:  # closest person, for tuning; the "?" keeps it from reading as an identification
            label = f"? {self.gallery.name(match.person)} {match.score:.2f}"
        result.overlays.append(Overlay(face, label, "ok" if accepted else "bad"))
        result.decision = self.decider.observe(observation)
        return result


class RepeatedFrames:
    """Some webcam drivers hand out the same picture more than once: those repeats are skipped.
    Only a picture that stays the same for FROZEN_AFTER_S means the stream froze."""

    def __init__(self, frozen_after_s: float = FROZEN_AFTER_S) -> None:
        self._frozen_after_s = frozen_after_s
        self._signature = b""
        self._since: float | None = None  # when the current picture first repeated

    def skip(self, frame: Frame) -> bool:
        """True for a repeat with nothing new to look at."""
        if frame.signature != self._signature:
            self._signature, self._since = frame.signature, None
            return False
        if self._since is None:
            self._since = frame.captured_at
        return not self.frozen(frame)

    def frozen(self, frame: Frame) -> bool:
        return self._since is not None and frame.captured_at - self._since >= self._frozen_after_s


class RateMeter:
    """Frames per second, counted over the last couple of seconds.

    Counting, not averaging the gaps between frames: after a slow look, the frame that arrived meanwhile is
    handled right away, and a gap that short would read as thousands of frames per second.
    """

    WINDOW_S = 2.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._times: deque[float] = deque()

    def tick(self) -> float:
        now = self._clock()
        self._times.append(now)
        while now - self._times[0] > self.WINDOW_S:
            self._times.popleft()
        span = now - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0
