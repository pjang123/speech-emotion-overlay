from emotion_overlay.calibration import PROMPTS, CalibrationSample, SpeechStats
from emotion_overlay.config import OLD_AUDIO_LABEL_MAP, AppConfig, AudioEmotionConfig
from emotion_overlay.emotions import EMOTIONS
from emotion_overlay.personalize import personalize

VOICE_OF = {"neutral": "neu", "joy": "hap", "surprise": "hap", "sadness": "sad"}


def text_for(emotion: str, strength: float = 0.7) -> dict[str, float]:
    rest = (1 - strength) / (len(EMOTIONS) - 1)
    return {e: (strength if e == emotion else rest) for e in EMOTIONS}


def flat_text() -> dict[str, float]:
    return {e: (0.4 if e == "neutral" else 0.1) for e in EMOTIONS}


WRONG_VOICE = {"neu": "ang", "hap": "sad", "ang": "neu", "sad": "hap"}


def voice_for(emotion: str, right: bool = True) -> dict[str, float]:
    """Confident voice scores, either right or confidently wrong (in varying ways:
    a voice that is always "angry" carries no information and is easy to correct)."""
    label = VOICE_OF.get(emotion, "ang")
    if not right:
        label = WRONG_VOICE[label]
    return {k: (0.85 if k == label else 0.05) for k in ("neu", "hap", "ang", "sad")}


def four_mood_config() -> AppConfig:
    """A 4-mood voice model (one label stands for several emotions), the harder case."""
    return AppConfig(audio_emotion=AudioEmotionConfig(label_map=OLD_AUDIO_LABEL_MAP))


def samples(text_fn, voice_fn, speech: SpeechStats | None = None) -> list[CalibrationSample]:
    return [CalibrationSample(e, text_fn(e), voice_fn(e), speech) for e, _ in PROMPTS]


def by_path(result) -> dict:
    return {r.path: r for r in result.recommendations}


def test_voice_turned_off_when_every_weight_hurts() -> None:
    # Words are right but faint; the voice is confidently wrong.
    result = personalize(
        samples(lambda e: text_for(e, 0.2), lambda e: voice_for(e, right=False)), four_mood_config()
    )
    rec = by_path(result)["audio_emotion.enabled"]
    assert rec.value is False and "faster" in rec.reason
    assert result.calibration.weight == 0.0
    assert result.calibration.profile["voice_weight_accuracy"]["0.0"] == 1.0


def test_voice_weight_lowered_rather_than_off_when_a_little_is_harmless() -> None:
    # Smallest change that fixes it: keep tone of voice (it can still catch laughs).
    result = personalize(
        samples(lambda e: text_for(e, 0.3), lambda e: voice_for(e, right=False)), four_mood_config()
    )
    recs = by_path(result)
    assert recs["audio_emotion.weight"].value == 0.1
    assert "audio_emotion.enabled" not in recs


def test_voice_weight_raised_when_words_carry_nothing() -> None:
    # Like RAVDESS: neutral words, a voice that's right. Only emotions the voice can express.
    four = [
        CalibrationSample(e, flat_text(), voice_for(e))
        for e, _ in PROMPTS
        if e in ("neutral", "joy", "anger", "sadness")
    ]
    result = personalize(four, four_mood_config())
    rec = by_path(result)["audio_emotion.weight"]
    assert rec.value > 0.25
    assert result.calibration.accuracy["after"] > result.calibration.accuracy["before"]


def test_no_weight_change_without_a_clear_win() -> None:
    result = personalize(samples(text_for, voice_for), four_mood_config())
    assert "audio_emotion.weight" not in by_path(result)
    assert "audio_emotion.enabled" not in by_path(result)


def test_confidence_and_strong_thresholds_follow_the_users_scores() -> None:
    config = four_mood_config()
    config.audio_emotion.enabled = False
    config.state.confidence_threshold = 0.6
    result = personalize(samples(lambda e: text_for(e, 0.45), lambda e: {}), config)
    recs = by_path(result)
    assert recs["state.confidence_threshold"].value < 0.6  # 0.6 would block real reactions
    strong = recs["display.strong_threshold"].value
    assert 0.5 <= strong <= 0.95


def test_pause_length_follows_mid_sentence_pauses() -> None:
    config = four_mood_config()
    config.audio_emotion.enabled = False
    slow = SpeechStats(pauses_ms=(500, 600, 550), speech_probs=(0.9,) * 10, noise_probs=(0.02,) * 5)
    rec = by_path(personalize(samples(text_for, lambda e: {}, slow), config))["vad.min_silence_ms"]
    assert rec.current == 400 and rec.value == 700  # p75 of pauses + 100, rounded, capped

    fast = SpeechStats(pauses_ms=(), speech_probs=(0.9,) * 10, noise_probs=(0.02,) * 5)
    rec = by_path(personalize(samples(text_for, lambda e: {}, fast), config))["vad.min_silence_ms"]
    assert rec.value == 300 and "didn't pause" in rec.reason


def test_vad_threshold_lowered_only_for_quiet_speakers() -> None:
    config = four_mood_config()
    config.audio_emotion.enabled = False
    quiet = SpeechStats((200,), speech_probs=(0.52,) * 10, noise_probs=(0.05,) * 5)
    rec = by_path(personalize(samples(text_for, lambda e: {}, quiet), config))["vad.threshold"]
    assert rec.value < 0.5 and "clipped" in rec.reason

    clear = SpeechStats((200,), speech_probs=(0.95,) * 10, noise_probs=(0.02,) * 5)
    result = personalize(samples(text_for, lambda e: {}, clear), config)
    assert "vad.threshold" not in by_path(result)  # nothing near the threshold: leave it
    assert result.calibration.profile["speech_level"] == 0.95


def test_vad_threshold_raised_for_noisy_rooms() -> None:
    config = four_mood_config()
    config.audio_emotion.enabled = False
    noisy = SpeechStats((200,), speech_probs=(0.97,) * 10, noise_probs=(0.4,) * 5)
    rec = by_path(personalize(samples(text_for, lambda e: {}, noisy), config))["vad.threshold"]
    assert rec.value > 0.5 and "noise" in rec.reason


def test_too_little_speech_data_changes_no_vad_settings() -> None:
    config = four_mood_config()
    config.audio_emotion.enabled = False
    few = [CalibrationSample(e, text_for(e), {}) for e, _ in PROMPTS]
    paths = by_path(personalize(few, config))
    assert "vad.min_silence_ms" not in paths and "vad.threshold" not in paths


def test_patch_is_valid_settings() -> None:
    config = four_mood_config()
    result = personalize(
        samples(lambda e: text_for(e, 0.2), lambda e: voice_for(e, right=False)), config
    )
    assert result.recommendations
    from emotion_overlay.settings import validate_settings

    validate_settings(config, result.patch())  # raises if any recommendation is invalid
