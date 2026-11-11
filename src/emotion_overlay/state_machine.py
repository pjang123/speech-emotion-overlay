"""Turns noisy emotion scores into the emotion that is actually displayed.

Deliberately free of I/O and clock calls: callers pass `now` (monotonic seconds),
so every rule is deterministic and unit-testable.

Phase 1 rules:
- The top-scoring emotion is the candidate. Ties keep the current emotion, then
  prefer neutral, then fall back to alphabetical order so results are stable.
- A candidate below `confidence_threshold` does not switch the display; it either
  holds the current emotion or drops to neutral (`low_confidence`).
- With no speech and no new scores for `silence_timeout_s`, return to neutral.

Phase 2 adds EMA smoothing, hysteresis and minimum display time here, behind the
same `on_scores` / `on_activity` / `tick` interface.
"""

import math
from dataclasses import dataclass, replace

from emotion_overlay.config import StateConfig
from emotion_overlay.emotions import NEUTRAL, Scores


@dataclass(frozen=True)
class EmotionState:
    emotion: str
    confidence: float
    reason: str  # startup | scores | low_confidence | silence
    since: float  # when this emotion started being displayed


def pick_candidate(scores: Scores, current: str) -> tuple[str, float]:
    """Return the top emotion and its score, breaking ties deterministically."""
    best = max(scores.values())
    tied = [e for e, s in scores.items() if math.isclose(s, best, abs_tol=1e-6)]
    if current in tied:
        return current, best
    if NEUTRAL in tied:
        return NEUTRAL, best
    return min(tied), best


class EmotionStateMachine:
    def __init__(self, config: StateConfig, now: float) -> None:
        # Holding a reference (not a copy) lets live config edits take effect.
        self._config = config
        self._state = EmotionState(NEUTRAL, 0.0, "startup", now)
        self._last_activity = now

    @property
    def state(self) -> EmotionState:
        return self._state

    def on_activity(self, now: float) -> None:
        """Record that the user is speaking, which postpones the silence timeout."""
        self._last_activity = max(self._last_activity, now)

    def on_scores(self, scores: Scores, now: float) -> EmotionState:
        """Feed one window of classifier scores."""
        # New scores count as activity: transcription can finish after the speaker
        # has gone quiet, and the result must not be wiped out by the silence rule.
        self.on_activity(now)
        if not scores:
            return self._state
        candidate, confidence = pick_candidate(scores, self._state.emotion)
        if confidence < self._config.confidence_threshold:
            if self._config.low_confidence == "neutral":
                return self._set(NEUTRAL, confidence, "low_confidence", now)
            return self._state
        return self._set(candidate, confidence, "scores", now)

    def tick(self, now: float) -> EmotionState:
        """Apply time-based rules. Call regularly, even when nothing is said."""
        idle = now - self._last_activity
        if self._state.emotion != NEUTRAL and idle >= self._config.silence_timeout_s:
            return self._set(NEUTRAL, 0.0, "silence", now)
        return self._state

    def _set(self, emotion: str, confidence: float, reason: str, now: float) -> EmotionState:
        if emotion == self._state.emotion:
            self._state = replace(self._state, confidence=confidence)
        else:
            self._state = EmotionState(emotion, confidence, reason, now)
        return self._state
