import io
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest

from emotion_overlay.config import DEFAULT_AUDIO_LABEL_MAP
from emotion_overlay.eval.analysis import analyse, predict, speaker_cv_weight
from emotion_overlay.eval.datasets import load_wav, mine, ravdess
from emotion_overlay.eval.download import extract_ravdess
from emotion_overlay.eval.metrics import (
    accuracy,
    confusion_matrix,
    macro_f1,
    per_class,
    percentile,
)
from emotion_overlay.eval.record import script
from emotion_overlay.eval.report import render

MAP = DEFAULT_AUDIO_LABEL_MAP


# --- metrics ------------------------------------------------------------------------


def test_metrics_on_a_hand_checked_example() -> None:
    y_true = ["joy", "joy", "anger", "anger", "neutral"]
    y_pred = ["joy", "anger", "anger", "anger", "joy"]
    stats = per_class(y_true, y_pred, ["joy", "anger", "neutral"])
    assert stats["joy"] == {"precision": 0.5, "recall": 0.5, "f1": 0.5, "support": 2}
    assert stats["anger"]["precision"] == pytest.approx(2 / 3)
    assert stats["anger"]["recall"] == 1.0
    assert stats["neutral"]["f1"] == 0.0
    assert accuracy(y_true, y_pred) == 0.6
    assert macro_f1(y_true, y_pred, ["joy", "anger", "neutral"]) == pytest.approx((0.5 + 0.8) / 3)
    assert confusion_matrix(y_true, y_pred, ["joy", "anger", "neutral"]) == [
        [1, 1, 0],
        [0, 2, 0],
        [1, 0, 0],
    ]


def test_macro_f1_ignores_classes_absent_from_ground_truth() -> None:
    assert macro_f1(["joy"], ["joy"], ["joy", "fear"]) == 1.0


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 95) == 10
    assert percentile([], 50) == 0.0


# --- analysis ------------------------------------------------------------------------


def record(label: str, text: dict, voice: dict, speaker: str = "a", **meta: str) -> dict:
    return {
        "id": f"{speaker}-{label}-{len(meta)}-{id(text)}",
        "label": label,
        "speaker": speaker,
        "meta": meta,
        "transcript": "x" if text else "",
        "text_scores": text,
        "voice": voice,
        "timings": {"asr_ms": 300, "text_ms": 20, "voice_ms": 400, "processing_ms": 420},
    }


def test_predict_modes() -> None:
    # Neutral words, angry voice: words say neutral, voice says anger.
    r = record("anger", {"neutral": 0.9, "anger": 0.1}, {"ang": 0.9, "neu": 0.1})
    assert predict(r, "text", MAP, 0.25) == "neutral"
    assert predict(r, "voice", MAP, 0.25) == "anger"
    assert predict(r, "fused", MAP, 0.25) == "neutral"
    assert predict(r, "fused", MAP, 0.8) == "anger"


def test_no_words_counts_as_neutral_for_text_mode() -> None:
    assert predict(record("joy", {}, {"hap": 1.0}), "text", MAP, 0.25) == "neutral"


def _toy_dataset() -> list[dict]:
    records = []
    for speaker in ("s1", "s2", "s3", "s4"):
        for label, voice in (
            ("anger", "ang"),
            ("joy", "hap"),
            ("sadness", "sad"),
            ("neutral", "neu"),
        ):
            for intensity in ("normal", "strong", "normal"):
                # Words are always neutral (like RAVDESS); the voice is right.
                records.append(
                    record(
                        label,
                        {"neutral": 0.8, label: 0.2},
                        {voice: 0.7, "neu": 0.3},
                        speaker,
                        intensity=intensity,
                    )
                )
    return records


def test_speaker_cv_prefers_voice_when_words_are_uninformative() -> None:
    cv = speaker_cv_weight(_toy_dataset(), MAP)
    assert cv is not None
    assert all(f["weight"] >= 0.5 for f in cv["folds"])
    assert cv["accuracy"] == 1.0


def test_analyse_and_render_end_to_end() -> None:
    summary = analyse(_toy_dataset(), MAP, 0.25, 400)
    assert summary["modes"]["voice"]["accuracy"] == 1.0
    assert summary["modes"]["text"]["accuracy"] == 0.25  # always says neutral
    assert summary["latency"]["speech_end_to_state_ms"]["median"] == 820
    assert set(summary["breakdowns"]["intensity"]) == {"normal", "strong"}
    assert summary["calibration"]["speakers"] == 4
    text = render("toy", summary, {"Hardware": "test"})
    assert "| Words only |" in text and "Confusion matrix" in text and "p95" in text


# --- datasets ------------------------------------------------------------------------


def _wav(path: Path, rate: int = 48_000, channels: int = 1, seconds: float = 0.1) -> None:
    samples = np.zeros(int(rate * seconds) * channels, dtype="<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def test_ravdess_filenames_are_parsed_and_mapped(tmp_path: Path) -> None:
    for name in (
        "03-01-02-02-01-01-07.wav",
        "03-01-08-01-02-02-12.wav",
        "02-01-01-01-01-01-01.wav",
    ):
        _wav(tmp_path / name)
    clips = ravdess(tmp_path)  # the 02- (video) file is ignored
    assert [(c.label, c.speaker, c.meta["intensity"]) for c in clips] == [
        ("neutral", "actor07", "strong"),  # calm -> neutral, explicitly
        ("surprise", "actor12", "normal"),
    ]
    assert clips[0].meta["original_label"] == "calm"


def test_own_recordings_are_parsed(tmp_path: Path) -> None:
    for name in ("anger__tone__1.wav", "joy__words__2.wav", "smug__tone__1.wav", "notes.wav"):
        _wav(tmp_path / name)
    assert [(c.label, c.meta["line"]) for c in mine(tmp_path)] == [
        ("anger", "tone"),
        ("joy", "words"),
    ]


def test_load_wav_resamples_and_downmixes(tmp_path: Path) -> None:
    _wav(tmp_path / "x.wav", rate=48_000, channels=2, seconds=0.5)
    audio = load_wav(tmp_path / "x.wav")
    assert audio.dtype == np.float32 and len(audio) == 8000


def test_extract_ignores_unexpected_and_traversal_entries(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Actor_01/03-01-05-01-01-01-01.wav", b"RIFF")
        zf.writestr("../../03-01-05-01-01-01-02.wav", b"RIFF")  # flattened, can't escape
        zf.writestr("Actor_01/evil.exe", b"MZ")
    archive = tmp_path / "r.zip"
    archive.write_bytes(buffer.getvalue())
    dest = tmp_path / "out"
    assert extract_ravdess(archive, dest) == 2
    assert sorted(p.name for p in dest.iterdir()) == [
        "03-01-05-01-01-01-01.wav",
        "03-01-05-01-01-01-02.wav",
    ]
    assert not (tmp_path.parent / "03-01-05-01-01-01-02.wav").exists()


def test_recording_script_covers_both_kinds_for_every_emotion() -> None:
    lines = script()
    assert len(lines) == 28
    assert {(e, k) for e, k, _, _ in lines} == {
        (e, k) for e in {e for e, *_ in lines} for k in ("words", "tone")
    }
    assert len({(e, k, n) for e, k, n, _ in lines}) == 28  # unique filenames
