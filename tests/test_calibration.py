import json
from pathlib import Path

import pytest

from emotion_overlay.calibration import (
    PROMPTS,
    Calibration,
    CalibrationError,
    CalibrationSample,
    CalibrationSession,
    CalibrationStore,
    calibrate,
    fit_bias,
)
from emotion_overlay.config import OLD_AUDIO_LABEL_MAP
from emotion_overlay.emotions import EMOTIONS

# A 4-mood voice model's map (one label stands for several emotions), the harder case.
MAP = OLD_AUDIO_LABEL_MAP


def test_prompts_cover_every_emotion_without_repeats_in_a_row() -> None:
    assert {e for e, _ in PROMPTS} == set(EMOTIONS)
    assert all(a[0] != b[0] for a, b in zip(PROMPTS, PROMPTS[1:], strict=False))


def test_fit_bias_corrects_systematic_skew() -> None:
    # The model always leans "ang", even for happy lines.
    rows = [{"ang": 0.6, "hap": 0.4}] * 5 + [{"ang": 0.9, "hap": 0.1}] * 5
    labels = ["hap"] * 5 + ["ang"] * 5
    bias = fit_bias(rows, labels, ["ang", "hap"])
    assert bias["hap"] > bias["ang"]


def test_fit_bias_leaves_unrecorded_classes_alone() -> None:
    bias = fit_bias([{"a": 0.5, "b": 0.3, "c": 0.2}], ["a"], ["a", "b", "c"])
    assert bias["b"] == bias["c"] == 0.0


def test_fit_bias_is_bounded() -> None:
    bias = fit_bias([{"a": 0.0001, "b": 0.9999}] * 20, ["a"] * 20, ["a", "b"], l2=0.0, steps=5000)
    assert abs(bias["a"]) <= 3.0


def _skewed_samples() -> list[CalibrationSample]:
    """Text model is right; the voice model thinks everything is angry."""
    samples = []
    for emotion, _ in PROMPTS:
        text = {e: (0.7 if e == emotion else 0.05) for e in EMOTIONS}
        voice = {"ang": 0.7, "neu": 0.1, "hap": 0.1, "sad": 0.1}
        samples.append(CalibrationSample(emotion, text, voice))
    return samples


def test_calibrate_reports_accuracy_and_learns_voice_skew() -> None:
    result = calibrate(_skewed_samples(), MAP, weight=0.5)
    assert result.samples == len(PROMPTS)
    assert result.accuracy["text"] == 1.0
    assert result.voice_bias["ang"] < 0  # learned that "ang" is over-predicted for this user
    assert result.accuracy["after"] >= result.accuracy["before"]


def test_calibrate_needs_a_few_samples() -> None:
    with pytest.raises(CalibrationError):
        calibrate(_skewed_samples()[:3], MAP, weight=0.3)


def test_session_flow() -> None:
    session = CalibrationSession(PROMPTS[:3])
    assert session.to_api()["prompt"]["emotion"] == PROMPTS[0][0]
    session.record({"joy": 0.9}, {"hap": 0.8})
    assert session.last == {"wanted": PROMPTS[0][0], "heard": "joy", "voice": "hap"}
    session.skip()
    session.back()  # back to the skipped prompt
    assert session.index == 1
    session.back()  # back to the first one: its recording is discarded
    assert session.index == 0 and session.samples == []
    for _ in range(3):
        session.record({"joy": 0.9}, {})
    assert session.done and len(session.samples) == 3
    session.record({"anger": 1.0}, {})  # ignored after the end
    assert len(session.samples) == 3


def test_store_roundtrip_and_validation(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path)
    assert store.load(["neu"]) is None
    cal = Calibration(
        {"neu": 0.5},
        {"joy": -0.2},
        0.25,
        14,
        "2026-11-12T00:00:00+00:00",
        {"text": 0.8, "voice": None, "before": 0.7, "after": 0.9},
    )
    store.save(cal)
    assert store.load(["neu"]) == cal

    raw = json.loads((tmp_path / "calibration.json").read_text())
    raw["final_bias"] = {"joy": 99, "made_up": 1, "fear": "NaN", "anger": float("inf")}
    (tmp_path / "calibration.json").write_text(json.dumps(raw))
    assert store.load(["neu"]).final_bias == {"joy": 3.0}  # clipped; junk dropped

    (tmp_path / "calibration.json").write_text("not json")
    assert store.load(["neu"]) is None
    store.delete()
    assert not (tmp_path / "calibration.json").exists()


def test_calibration_for_another_voice_model_is_ignored(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path)
    store.save(Calibration({"neu": 0.5, "ang": -1.0}, {"joy": -0.2}, 0.25, 14, "x"))
    assert store.load(["neu", "ang"]) is not None and not store.stale
    assert store.load(["neutral", "anger"]) is None  # the voice model's labels changed
    assert store.stale
    store.save(Calibration({}, {"joy": -0.2}, 0.0, 14, "x"))  # made with voice off: still fine
    assert store.load(["neutral", "anger"]) is not None and not store.stale
