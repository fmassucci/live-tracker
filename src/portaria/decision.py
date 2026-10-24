"""Turns per-frame matches into one decision per visit to the door."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from portaria.config import DecisionConfig

Kind = Literal["recognized", "unknown", "spoof"]


@dataclass(frozen=True)
class Observation:
    """One usable frame: who it matched best, and how confidently."""

    at: float           # time.monotonic() of the frame
    person: str | None  # best match; None when nobody is enrolled
    score: float        # cosine similarity to that person
    margin: float       # lead over the runner-up
    live: bool


@dataclass(frozen=True)
class Decision:
    kind: Kind
    person: str | None
    score: float  # mean over the frames that agreed


class DecisionMaker:
    """Needs several agreeing frames, then stays quiet until the face has left.

    Waiting for the face to leave means a person lingering at the door, or a stream frozen on
    someone's face, can't trigger the door again and again.
    """

    def __init__(self, cfg: DecisionConfig, accept_threshold: float, min_margin: float) -> None:
        self._cfg = cfg
        self._accept_threshold = accept_threshold
        self._min_margin = min_margin
        self._window: deque[Observation] = deque()
        self._last_face_at: float | None = None
        self._visit_started_at: float | None = None  # first usable frame of this visit
        self._decided: tuple[Kind, str | None] | None = None  # what this visit was already decided as
        self._cooldown_until: dict[tuple[Kind, str | None], float] = {}

    def matches(self, obs: Observation) -> bool:
        """The frame names an enrolled person confidently enough (liveness aside)."""
        return obs.person is not None and obs.score >= self._accept_threshold and obs.margin >= self._min_margin

    def no_face(self, at: float) -> None:
        """A frame without any face. Once the door has been clear for a moment, the visit is over."""
        if self._last_face_at is not None and at - self._last_face_at >= self._cfg.rearm_after_s:
            self._window.clear()
            self._decided = None
            self._visit_started_at = None

    def face_seen(self, at: float) -> None:
        """A frame whose face can't be used (too small, blurry, repeated). Someone is still at the door."""
        self._last_face_at = at

    def observe(self, obs: Observation) -> Decision | None:
        self._last_face_at = obs.at
        if self._visit_started_at is None:
            self._visit_started_at = obs.at
        self._window.append(obs)
        while obs.at - self._window[0].at > self._cfg.window_s:
            self._window.popleft()

        if self.matches(obs):
            agreeing = [o for o in self._window if o.person == obs.person and self.matches(o)]
            live = [o for o in agreeing if o.live]
            if len(live) >= self._cfg.min_votes:
                return self._decide("recognized", obs.person, live, obs.at, self._cfg.person_cooldown_s)
            if len(agreeing) - len(live) >= self._cfg.min_votes:
                spoofed = [o for o in agreeing if not o.live]
                return self._decide("spoof", obs.person, spoofed, obs.at, self._cfg.unknown_cooldown_s)
            return None

        if self._decided is not None and self._decided[0] == "recognized":
            # Someone was recognized in this visit; the frames as they stand up, turn or walk off
            # match poorly but aren't a new attempt. A stranger right behind them, with no gap, isn't
            # flagged either, but can't open the door: only a recognition does.
            return None
        # A known face still turning toward the camera matches poorly for its first frames, so "not
        # recognized" waits until the face has had time to settle; a match comes much sooner.
        settled = obs.at - self._visit_started_at >= self._cfg.unknown_after_s
        if settled and len(self._window) >= self._cfg.min_votes and not any(self.matches(o) for o in self._window):
            return self._decide("unknown", None, list(self._window), obs.at, self._cfg.unknown_cooldown_s)
        return None

    def _decide(
        self, kind: Kind, person: str | None, votes: list[Observation], now: float, cooldown_s: float
    ) -> Decision | None:
        key = (kind, person if kind == "recognized" else None)
        if self._decided == key or now < self._cooldown_until.get(key, float("-inf")):
            return None
        self._decided = key
        self._cooldown_until[key] = now + cooldown_s
        return Decision(kind, person, sum(o.score for o in votes) / len(votes))
