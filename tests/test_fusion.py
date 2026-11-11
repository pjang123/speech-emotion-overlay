import pytest

from emotion_overlay.classify.fusion import apply_bias, fuse, map_voice_scores, score_utterance
from emotion_overlay.config import DEFAULT_AUDIO_LABEL_MAP, AudioEmotionConfig

MAP = DEFAULT_AUDIO_LABEL_MAP


def test_voice_group_is_split_by_text_scores() -> None:
    # Voice says "agitated"; the words say it's mostly fear.
    voice = map_voice_scores({"ang": 1.0}, MAP, {"fear": 0.6, "anger": 0.2, "disgust": 0.2})
    assert voice == pytest.approx({"anger": 0.2, "disgust": 0.2, "fear": 0.6})


def test_voice_without_text_goes_to_first_listed_emotion() -> None:
    voice = map_voice_scores({"hap": 0.8, "neu": 0.2}, MAP, {})
    assert voice == pytest.approx({"joy": 0.8, "surprise": 0.0, "neutral": 0.2})


def test_unmapped_voice_labels_are_dropped_and_renormalised() -> None:
    voice = map_voice_scores({"sad": 0.5, "xyz": 0.5}, MAP, {})
    assert voice == pytest.approx({"sadness": 1.0})


def test_fuse_weights_and_fallbacks() -> None:
    text = {"joy": 0.8, "neutral": 0.2}
    voice = {"neutral": 1.0}
    assert fuse(text, voice, 0.25) == pytest.approx({"joy": 0.6, "neutral": 0.4})
    assert fuse(text, {}, 0.5) == text  # no voice: words only
    assert fuse({}, voice, 0.1) == voice  # no words (laugh, gasp): voice only
    assert fuse(text, voice, 0.0) == pytest.approx(text)


def test_apply_bias_shifts_and_renormalises() -> None:
    out = apply_bias({"a": 0.5, "b": 0.5}, {"a": 1.0})
    assert out["a"] > out["b"]
    assert sum(out.values()) == pytest.approx(1.0)
    assert apply_bias({"a": 0.7, "b": 0.3}, {}) == {"a": 0.7, "b": 0.3}


def test_score_utterance_pipeline() -> None:
    result = score_utterance(
        text={"joy": 0.9, "neutral": 0.1},
        voice_raw={"neu": 0.5, "hap": 0.5},
        label_map=MAP,
        weight=0.5,
        voice_bias={"hap": 2.0},  # calibration: this mic undersells happiness
        final_bias={},
    )
    assert result.voice_raw["hap"] > 0.85
    assert max(result.final, key=result.final.get) == "joy"
    assert sum(result.final.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "label_map",
    [
        {"neu": []},
        {"neu": ["calm"]},
        {"neu": ["neutral"], "sad": ["neutral"]},
    ],
)
def test_label_map_validation(label_map: dict[str, list[str]]) -> None:
    with pytest.raises(ValueError):
        AudioEmotionConfig(label_map=label_map)
