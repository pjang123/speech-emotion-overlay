"""Turns noisy emotion scores into the emotion that is actually displayed.

Deliberately free of I/O and clock calls: callers pass `now` (monotonic seconds),
so every rule is deterministic and unit-testable. It holds a reference to its
config, so settings edited on the setup page apply to the next window.

    raw scores -> smoothing (EMA) -> candidate (top score; ties keep current,
                                     then prefer neutral, then alphabetical)
    candidate must: reach confidence_threshold
                    AND stay on top for persist_windows OR lead by instant_switch_margin
                    AND current emotion shown for >= min_display_s (not applied to the
                        resting neutral after startup/silence)
    -> switch displayed emotion
    an unsure window (below threshold) in "hold" mode neither advances nor cancels
    a pending switch; in "neutral" mode it requests neutral
    no speech and no scores for silence_timeout_s -> neutral (bypasses min_display_s)

With the default config (smoothing 1, persist 1, min display 0) every confident
window switches immediately; the stability rules are opt-in.
"""

import math
from dataclasses import dataclass, replace

from emotion_overlay.config import StateConfig
from emotion_overlay.emotions import NEUTRAL, Scores


@dataclass(frozen=True)
class EmotionState:
    emotion: str
    confidence: float  # smoothed score of the displayed emotion; drives intensity tiers
    reason: str  # startup | scores | low_confidence | silence | paused
    since: float  # when this emotion started being displayed


# Neutral states the app rests in rather than reacted with; min_display_s doesn't hold them.
RESTING_REASONS = frozenset({"startup", "silence", "paused"})


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
        self._config = config
        self._state = EmotionState(NEUTRAL, 0.0, "startup", now)
        self._last_activity = now
        self._smoothed: Scores = {}
        self._streak_emotion: str | None = None
        self._streak = 0
        # A switch that passed every rule except min_display_s; applied by tick().
        self._waiting: tuple[str, float, str] | None = None

    @property
    def state(self) -> EmotionState:
        return self._state

    @property
    def smoothed(self) -> Scores:
        return dict(self._smoothed)

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
        cfg = self._config
        smoothed = self._smooth(scores)
        current = self._state.emotion
        current_score = smoothed.get(current, 0.0)
        candidate, confidence = pick_candidate(smoothed, current)

        if confidence < cfg.confidence_threshold:
            if cfg.low_confidence == "neutral":
                self._clear_pending()
                return self._request(NEUTRAL, confidence, "low_confidence", now)
            # "hold": an unsure window neither counts toward nor cancels a pending switch.
            return self._set_confidence(current_score)

        if candidate == current:
            self._clear_pending()
            return self._set_confidence(confidence)

        if candidate == self._streak_emotion:
            self._streak += 1
        else:
            self._streak_emotion, self._streak = candidate, 1
        persisted = self._streak >= cfg.persist_windows
        decisive = (
            cfg.instant_switch_margin > 0
            and confidence - current_score >= cfg.instant_switch_margin
        )
        if not (persisted or decisive):
            self._waiting = None
            return self._set_confidence(current_score)
        return self._request(candidate, confidence, "scores", now)

    def tick(self, now: float) -> EmotionState:
        """Apply time-based rules. Call regularly, even when nothing is said."""
        idle = now - self._last_activity
        if self._state.emotion != NEUTRAL and idle >= self._config.silence_timeout_s:
            self._smoothed.clear()  # don't let the old mood leak into the next sentence
            self._clear_pending()
            self._state = EmotionState(NEUTRAL, 0.0, "silence", now)
        elif self._waiting is not None:
            self._request(*self._waiting, now)
        return self._state

    def reset(self, now: float, reason: str = "paused") -> EmotionState:
        """Go to neutral immediately and forget all history (e.g. listening paused)."""
        self._smoothed.clear()
        self._clear_pending()
        self._last_activity = now
        self._state = EmotionState(NEUTRAL, 0.0, reason, now)
        return self._state

    # --- helpers ----------------------------------------------------------

    def _smooth(self, scores: Scores) -> Scores:
        alpha = self._config.smoothing
        if alpha >= 1.0 or not self._smoothed:
            self._smoothed = dict(scores)
        else:
            keys = scores.keys() | self._smoothed.keys()
            self._smoothed = {
                e: alpha * scores.get(e, 0.0) + (1 - alpha) * self._smoothed.get(e, 0.0)
                for e in keys
            }
        return self._smoothed

    def _request(self, emotion: str, confidence: float, reason: str, now: float) -> EmotionState:
        """Switch to `emotion` unless the current one hasn't been shown long enough."""
        if emotion == self._state.emotion:
            self._waiting = None
            return self._set_confidence(confidence)
        # The resting neutral isn't a reaction worth holding on screen.
        resting = self._state.reason in RESTING_REASONS
        if not resting and now - self._state.since < self._config.min_display_s:
            self._waiting = (emotion, confidence, reason)
            return self._state
        self._clear_pending()
        self._state = EmotionState(emotion, confidence, reason, now)
        return self._state

    def _set_confidence(self, confidence: float) -> EmotionState:
        self._state = replace(self._state, confidence=confidence)
        return self._state

    def _clear_pending(self) -> None:
        self._streak_emotion, self._streak = None, 0
        self._waiting = None
