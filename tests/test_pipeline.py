"""Pipeline control logic with the mic and models replaced by fakes."""

from typing import Any

import numpy as np
import pytest

from emotion_overlay.audio.vad import Utterance
from emotion_overlay.config import AppConfig
from emotion_overlay.emotions import NEUTRAL
from emotion_overlay.pipeline import Pipeline, PipelineEvents
from emotion_overlay.state_machine import EmotionState


class FakeCapture:
    def __init__(self) -> None:
        self.open_device: int | None | str = "closed"
        self.device_name = "Fake Mic"

    def start(self, device: int | None) -> None:
        self.open_device = device

    def stop(self) -> None:
        self.open_device = "closed"


class Recorder:
    def __init__(self) -> None:
        self.states: list[EmotionState] = []
        self.messages: list[dict[str, Any]] = []

    def events(self) -> PipelineEvents:
        return PipelineEvents(
            on_state=lambda state, _new: self.states.append(state),
            on_message=self.messages.append,
        )


@pytest.fixture
def setup(config: AppConfig) -> tuple[Pipeline, FakeCapture, Recorder]:
    recorder = Recorder()
    pipeline = Pipeline(config, recorder.events())
    capture = FakeCapture()
    pipeline._capture = capture  # type: ignore[assignment]
    pipeline._segmenter = object()  # type: ignore[assignment]  # "models loaded"
    pipeline._voice_state = "failed"  # never load the real voice model in tests
    pipeline._transcriber = type(
        "T",
        (),
        {"backend": "fake", "device": "cpu", "transcribe": lambda self, a: "I am so happy"},
    )()
    pipeline._classifier = type("C", (), {"scores": lambda self, t: {"joy": 0.95}})()
    return pipeline, capture, recorder


def utterance() -> Utterance:
    return Utterance(audio=np.zeros(16000, dtype=np.float32), ended_at=0.0, forced=False)


def test_pause_closes_mic_and_shows_neutral(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, capture, recorder = setup
    pipeline._open_mic()
    pipeline._handle_utterance(utterance())
    assert recorder.states[-1].emotion == "joy"

    pipeline.set_listening(False)
    assert capture.open_device == "closed"
    assert (recorder.states[-1].emotion, recorder.states[-1].reason) == (NEUTRAL, "paused")
    assert pipeline.status()["listening"] is False
    assert recorder.messages[-1]["state"] == "paused"


def test_resume_reopens_configured_mic(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, capture, _ = setup
    pipeline.set_listening(False)
    pipeline.set_device(4)  # changing device while paused must not open the mic
    assert capture.open_device == "closed"
    pipeline.set_listening(True)
    assert capture.open_device == 4
    assert pipeline.status()["state"] == "listening"


def test_speech_finishing_after_pause_is_discarded(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    pipeline, _, recorder = setup
    pipeline.set_listening(False)
    count = len(recorder.states)
    pipeline._handle_utterance(utterance())  # was already queued/transcribing
    assert len(recorder.states) == count


def test_pause_drops_queued_utterances(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, _, _ = setup
    pipeline._utt_q.put(utterance())
    pipeline.set_listening(False)
    assert pipeline._utt_q.empty()


class FakeVoice:
    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    def scores(self, audio: np.ndarray) -> dict[str, float]:
        return self._scores


def last_debug(recorder: Recorder) -> dict[str, Any]:
    return [m for m in recorder.messages if m.get("type") == "debug"][-1]


def test_voice_scores_are_fused(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, _, recorder = setup
    pipeline._voice = FakeVoice({"neu": 0.1, "hap": 0.9})
    pipeline._handle_utterance(utterance())
    debug = last_debug(recorder)
    assert debug["voice"] == {"neu": 0.1, "hap": 0.9}
    assert debug["timings"]["voice_ms"] is not None
    assert recorder.states[-1].emotion == "joy"


def test_voice_disabled_live_skips_voice(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, _, recorder = setup
    pipeline._voice = FakeVoice({"ang": 1.0})
    pipeline._config.audio_emotion.enabled = False
    pipeline._handle_utterance(utterance())
    assert last_debug(recorder)["voice"] == {}


def test_voice_only_when_no_words(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, _, recorder = setup
    pipeline._transcriber.transcribe = lambda audio: ""  # e.g. a laugh
    pipeline._voice = FakeVoice({"hap": 0.9, "neu": 0.1})
    pipeline._handle_utterance(utterance())
    assert recorder.states[-1].emotion == "joy"


def test_calibration_listener_takes_over(setup: tuple[Pipeline, FakeCapture, Recorder]) -> None:
    pipeline, _, recorder = setup
    pipeline._voice = FakeVoice({"hap": 1.0})
    received = []
    pipeline.set_calibration_listener(lambda text, voice, speech: received.append((text, voice)))
    pipeline._handle_utterance(utterance())
    assert received == [({"joy": 0.95}, {"hap": 1.0})]
    assert recorder.states == []  # the overlay isn't touched while calibrating
    pipeline.set_calibration_listener(None)
    pipeline._handle_utterance(utterance())
    assert recorder.states[-1].emotion == "joy"


def test_calibration_gets_speech_stats_and_longer_pauses(config: AppConfig) -> None:
    from emotion_overlay.audio.vad import UtteranceSegmenter
    from emotion_overlay.calibration import CALIBRATION_SILENCE_MS

    recorder = Recorder()
    pipeline = Pipeline(config, recorder.events())
    segmenter = UtteranceSegmenter(lambda frame: 0.0, config.vad)
    pipeline._segmenter = segmenter
    pipeline._voice_state = "failed"
    pipeline._transcriber = type(
        "T", (), {"backend": "f", "device": "cpu", "transcribe": lambda s, a: "hi"}
    )()
    pipeline._classifier = type("C", (), {"scores": lambda s, t: {"joy": 0.9}})()
    got = []
    pipeline.set_calibration_listener(lambda text, voice, speech: got.append(speech))
    assert segmenter.min_silence_override_ms == CALIBRATION_SILENCE_MS
    utt = Utterance(np.zeros(16000, np.float32), 0.0, False, (120, 300), (0.9, 0.95), (0.01,))
    pipeline._handle_utterance(utt)
    assert got[0].pauses_ms == (120, 300) and got[0].noise_probs == (0.01,)
    pipeline.set_calibration_listener(None)
    assert segmenter.min_silence_override_ms is None


def test_pause_before_models_load_is_remembered(config: AppConfig) -> None:
    recorder = Recorder()
    pipeline = Pipeline(config, recorder.events())
    pipeline.set_listening(False)  # still "loading": nothing to close yet
    assert pipeline.status()["listening"] is False
    assert recorder.states == []
