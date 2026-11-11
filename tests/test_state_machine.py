import pytest

from emotion_overlay.config import StateConfig
from emotion_overlay.emotions import NEUTRAL
from emotion_overlay.state_machine import EmotionStateMachine, pick_candidate


def scores(**values: float) -> dict[str, float]:
    return values


@pytest.fixture
def cfg() -> StateConfig:
    return StateConfig(confidence_threshold=0.5, low_confidence="hold", silence_timeout_s=3.0)


def test_starts_neutral(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    assert sm.state.emotion == NEUTRAL
    assert sm.state.reason == "startup"


def test_switches_on_confident_scores(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    state = sm.on_scores(scores(joy=0.9, anger=0.05, neutral=0.05), now=1.0)
    assert (state.emotion, state.reason, state.since) == ("joy", "scores", 1.0)
    assert state.confidence == pytest.approx(0.9)


def test_low_confidence_holds_current(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(anger=0.8, neutral=0.2), now=1.0)
    state = sm.on_scores(scores(joy=0.4, anger=0.3, neutral=0.3), now=2.0)
    assert state.emotion == "anger"


def test_low_confidence_can_fall_back_to_neutral(cfg: StateConfig) -> None:
    cfg.low_confidence = "neutral"
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(anger=0.8, neutral=0.2), now=1.0)
    state = sm.on_scores(scores(joy=0.4, anger=0.3, neutral=0.3), now=2.0)
    assert (state.emotion, state.reason) == (NEUTRAL, "low_confidence")


def test_threshold_is_inclusive(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    assert sm.on_scores(scores(sadness=0.5, neutral=0.5 - 1e-3), now=1.0).emotion == "sadness"


def test_same_emotion_keeps_since_and_updates_confidence(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.7), now=1.0)
    state = sm.on_scores(scores(joy=0.95), now=2.0)
    assert state.since == 1.0
    assert state.confidence == pytest.approx(0.95)


def test_empty_scores_change_nothing(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=1.0)
    assert sm.on_scores({}, now=2.0).emotion == "joy"


# --- ties -------------------------------------------------------------------


def test_tie_keeps_current_emotion() -> None:
    assert pick_candidate(scores(joy=0.5, anger=0.5), current="anger") == ("anger", 0.5)


def test_tie_prefers_neutral_over_other_emotions() -> None:
    assert pick_candidate(scores(joy=0.4, neutral=0.4), current="anger")[0] == NEUTRAL


def test_tie_without_current_or_neutral_is_deterministic() -> None:
    a = pick_candidate(scores(surprise=0.45, joy=0.45, fear=0.1), current="anger")
    b = pick_candidate(scores(joy=0.45, surprise=0.45, fear=0.1), current="anger")
    assert a == b == ("joy", 0.45)


def test_tie_does_not_flip_displayed_emotion(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=1.0)
    assert sm.on_scores(scores(joy=0.5, anger=0.5), now=2.0).emotion == "joy"


# --- silence ------------------------------------------------------------------


def test_silence_returns_to_neutral_after_timeout(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(anger=0.9), now=10.0)
    assert sm.tick(now=12.9).emotion == "anger"
    state = sm.tick(now=13.0)
    assert (state.emotion, state.reason, state.since) == (NEUTRAL, "silence", 13.0)


def test_speech_activity_postpones_silence(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(anger=0.9), now=10.0)
    sm.on_activity(now=12.0)
    assert sm.tick(now=14.0).emotion == "anger"
    assert sm.tick(now=15.0).emotion == NEUTRAL


def test_late_scores_after_silence_are_not_immediately_wiped(cfg: StateConfig) -> None:
    # Transcription can finish well after the speaker stopped; that result must still show.
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_activity(now=1.0)
    sm.on_scores(scores(joy=0.9), now=5.0)
    assert sm.tick(now=5.1).emotion == "joy"


def test_silence_timeout_reads_live_config(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(fear=0.9), now=0.0)
    cfg.silence_timeout_s = 1.0
    assert sm.tick(now=1.0).emotion == NEUTRAL


def test_tick_while_neutral_is_a_no_op(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    assert sm.tick(now=100.0).reason == "startup"


# --- flicker (Phase 1 baseline) -------------------------------------------------


def test_defaults_switch_on_every_confident_window(cfg: StateConfig) -> None:
    """Stability rules are off by default: the image follows every utterance."""
    sm = EmotionStateMachine(cfg, now=0.0)
    shown = [
        sm.on_scores(scores(joy=0.8) if i % 2 == 0 else scores(anger=0.8), now=float(i)).emotion
        for i in range(4)
    ]
    assert shown == ["joy", "anger", "joy", "anger"]


def test_noisy_low_confidence_scores_never_flicker(cfg: StateConfig) -> None:
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=0.0)
    noisy = [scores(anger=0.4, joy=0.3), scores(sadness=0.45), scores(fear=0.35, joy=0.3)]
    for i, window in enumerate(noisy, start=1):
        assert sm.on_scores(window, now=float(i)).emotion == "joy"


# --- stability rules (opt-in) ---------------------------------------------------


def test_smoothing_blends_windows(cfg: StateConfig) -> None:
    cfg.smoothing = 0.5
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9, anger=0.1), now=1.0)
    state = sm.on_scores(scores(joy=0.1, anger=0.9), now=2.0)
    assert sm.smoothed == pytest.approx({"joy": 0.5, "anger": 0.5})
    assert state.emotion == "joy"  # tie keeps current


def test_smoothing_resets_after_silence(cfg: StateConfig) -> None:
    cfg.smoothing = 0.2
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(anger=0.95), now=0.0)
    sm.tick(now=10.0)
    assert sm.smoothed == {}
    assert sm.on_scores(scores(joy=0.9, anger=0.1), now=11.0).emotion == "joy"


def test_persist_windows_requires_consecutive_wins(cfg: StateConfig) -> None:
    cfg.persist_windows = 3
    sm = EmotionStateMachine(cfg, now=0.0)
    assert sm.on_scores(scores(joy=0.9), now=1.0).emotion == NEUTRAL
    assert sm.on_scores(scores(joy=0.9), now=2.0).emotion == NEUTRAL
    assert sm.on_scores(scores(joy=0.9), now=3.0).emotion == "joy"


def test_interrupted_streak_starts_over(cfg: StateConfig) -> None:
    cfg.persist_windows = 2
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=1.0)
    sm.on_scores(scores(anger=0.9), now=2.0)  # breaks joy's streak
    assert sm.on_scores(scores(joy=0.9), now=3.0).emotion == NEUTRAL
    assert sm.on_scores(scores(joy=0.9), now=4.0).emotion == "joy"


def test_instant_margin_skips_persistence(cfg: StateConfig) -> None:
    cfg.persist_windows = 5
    cfg.instant_switch_margin = 0.5
    sm = EmotionStateMachine(cfg, now=0.0)
    assert sm.on_scores(scores(joy=0.6, neutral=0.4), now=1.0).emotion == NEUTRAL  # lead 0.2
    assert sm.on_scores(scores(joy=0.9, neutral=0.1), now=2.0).emotion == "joy"  # lead 0.8


def test_min_display_time_delays_switch_until_tick(cfg: StateConfig) -> None:
    cfg.min_display_s = 2.0
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=5.0)
    assert sm.on_scores(scores(anger=0.9), now=6.0).emotion == "joy"
    assert sm.tick(now=6.5).emotion == "joy"
    state = sm.tick(now=7.0)
    assert (state.emotion, state.since) == ("anger", 7.0)


def test_waiting_switch_is_cancelled_if_current_wins_again(cfg: StateConfig) -> None:
    cfg.min_display_s = 2.0
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=5.0)
    sm.on_scores(scores(anger=0.9), now=6.0)
    sm.on_scores(scores(joy=0.9), now=6.5)
    assert sm.tick(now=8.0).emotion == "joy"


def test_silence_bypasses_min_display_time(cfg: StateConfig) -> None:
    cfg.min_display_s = 30.0
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=0.0)
    assert sm.tick(now=3.0).emotion == NEUTRAL


def test_confidence_tracks_current_emotion_while_held(cfg: StateConfig) -> None:
    cfg.persist_windows = 2
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=1.0)
    sm.on_scores(scores(joy=0.9), now=1.5)
    state = sm.on_scores(scores(anger=0.7, joy=0.3), now=2.0)
    assert state.emotion == "joy"
    assert state.confidence == pytest.approx(0.3)


def test_stability_settings_suppress_flicker(cfg: StateConfig) -> None:
    """A noisy monologue: mostly joy with stray single-window spikes of other emotions."""
    cfg.smoothing = 0.4
    cfg.persist_windows = 2
    cfg.min_display_s = 4.0
    sm = EmotionStateMachine(cfg, now=0.0)
    windows = [
        scores(joy=0.8, anger=0.1, neutral=0.1),
        scores(joy=0.7, surprise=0.2, neutral=0.1),
        scores(anger=0.9, joy=0.1),
        scores(joy=0.8, neutral=0.2),
        scores(sadness=0.85, joy=0.15),
        scores(joy=0.9, neutral=0.1),
        scores(fear=0.8, joy=0.2),
        scores(joy=0.85, neutral=0.15),
    ]
    shown = []
    for i, window in enumerate(windows):
        shown.append(sm.on_scores(window, now=float(i)).emotion)
        sm.tick(now=i + 0.5)
    assert shown[0] == NEUTRAL  # joy needs two windows to take over
    assert set(shown[1:]) == {"joy"}  # then no spike gets through


def test_unsure_window_does_not_reset_pending_switch(cfg: StateConfig) -> None:
    cfg.persist_windows = 2
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=1.0)
    sm.on_scores(scores(joy=0.4, anger=0.3), now=2.0)  # unsure
    assert sm.on_scores(scores(joy=0.9), now=3.0).emotion == "joy"


def test_first_reaction_after_silence_ignores_min_display(cfg: StateConfig) -> None:
    cfg.min_display_s = 10.0
    sm = EmotionStateMachine(cfg, now=0.0)
    sm.on_scores(scores(joy=0.9), now=0.0)
    sm.tick(now=3.0)  # silence -> neutral
    assert sm.on_scores(scores(anger=0.9), now=3.5).emotion == "anger"
