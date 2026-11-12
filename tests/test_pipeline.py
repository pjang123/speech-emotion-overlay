"""Pipeline control logic with the mic and models replaced by fakes."""

import threading
import time
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
        self.missing_device: str | None = None

    def start(
        self, device: int | None, name: str | None = None, hostapi: str | None = None
    ) -> None:
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
    pipeline._voice = FakeVoice({"neutral": 0.1, "happiness": 0.9})
    pipeline._handle_utterance(utterance())
    debug = last_debug(recorder)
    assert debug["voice"] == {"neutral": 0.1, "happiness": 0.9}
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
    pipeline._voice = FakeVoice({"happiness": 0.9, "neutral": 0.1})
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


def test_words_are_sent_for_triggers_but_not_while_calibrating(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    pipeline, _, _ = setup
    heard: list[str] = []
    pipeline._events = PipelineEvents(
        pipeline._events.on_state, pipeline._events.on_message, on_words=heard.append
    )
    pipeline._handle_utterance(utterance())
    assert heard == ["I am so happy"]
    pipeline.set_calibration_listener(lambda *args: None)
    pipeline._handle_utterance(utterance())
    assert heard == ["I am so happy"]


def test_interim_updates_display_from_words_only(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    from emotion_overlay.pipeline import Interim

    pipeline, _, recorder = setup
    heard: list[str] = []
    pipeline._events = PipelineEvents(
        pipeline._events.on_state, pipeline._events.on_message, on_words=heard.append
    )
    pipeline._voice = FakeVoice({"ang": 1.0})  # must not be used for the partial sentence
    pipeline._handle_interim(Interim(np.zeros(16000, np.float32)))
    assert recorder.states[-1].emotion == "joy"
    debug = last_debug(recorder)
    assert debug["interim"] is True and "voice" not in debug
    assert heard == []  # word triggers wait for the finished sentence
    assert pipeline._window.text(0.0) == ""  # the finished sentence is added later, once


def speaking_segmenter(pipeline: Pipeline) -> Any:
    from emotion_overlay.audio.vad import UtteranceSegmenter

    segmenter = UtteranceSegmenter(lambda frame: 1.0, pipeline._config.vad)  # all speech
    pipeline._segmenter = segmenter
    return segmenter


def queued(pipeline: Pipeline) -> list[Any]:
    items = []
    while not pipeline._utt_q.empty():
        items.append(pipeline._utt_q.get_nowait())
    return items


def test_interims_are_sent_every_interval_of_speech(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    from emotion_overlay.pipeline import Interim

    pipeline, _, _ = setup
    pipeline._config.classifier.interim_s = 1.0
    segmenter = speaking_segmenter(pipeline)
    sent = []
    for _ in range(80):  # 80 x 32 ms = 2.56 s of speech
        segmenter.feed(np.zeros(512, np.float32))
        pipeline._maybe_interim(segmenter, restart=False)
        sent += queued(pipeline)
    assert [round(len(i.audio) / 16000, 1) for i in sent] == [1.0, 2.0]
    assert all(isinstance(i, Interim) for i in sent)


def test_no_interims_when_off_busy_or_calibrating(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    pipeline, _, _ = setup

    def run(frames: int = 40) -> list[Any]:
        segmenter = speaking_segmenter(pipeline)
        pipeline._interim_due_s = 0.0
        for _ in range(frames):
            segmenter.feed(np.zeros(512, np.float32))
            pipeline._maybe_interim(segmenter, restart=False)
        return queued(pipeline)

    pipeline._config.classifier.interim_s = 0.0
    assert run() == []
    pipeline._config.classifier.interim_s = 1.0
    pipeline._asr_busy = True  # still working on the last one: skip, don't pile up
    assert run() == []
    pipeline._asr_busy = False
    pipeline._calibration_listener = lambda *args: None
    assert run() == []


class SlowVoice(FakeVoice):
    def scores(self, audio: np.ndarray) -> dict[str, float]:
        import time

        time.sleep(0.3)
        return super().scores(audio)


def test_words_show_first_when_the_voice_is_slow(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    pipeline, _, recorder = setup
    flags: list[tuple[str, bool]] = []
    pipeline._events = PipelineEvents(
        lambda state, new: flags.append((state.emotion, new)), recorder.messages.append
    )
    pipeline._voice = SlowVoice({"anger": 1.0})
    pipeline._config.audio_emotion.weight = 0.9  # the voice will change the answer
    pipeline._handle_utterance(utterance())
    # Words first (and the image re-picked once), then the combined result.
    assert flags == [("joy", True), ("anger", False)]
    assert last_debug(recorder)["timings"]["words_shown_ms"] is not None


def test_one_update_per_sentence_when_voice_is_fast_or_anti_flicker_is_on(
    setup: tuple[Pipeline, FakeCapture, Recorder],
) -> None:
    pipeline, _, recorder = setup
    flags: list[bool] = []
    pipeline._events = PipelineEvents(lambda s, new: flags.append(new), recorder.messages.append)
    pipeline._voice = FakeVoice({"happiness": 1.0})
    pipeline._handle_utterance(utterance())
    pipeline._voice = SlowVoice({"happiness": 1.0})
    pipeline._config.state.persist_windows = 2  # each sentence must count once
    pipeline._handle_utterance(utterance())
    assert flags == [True, True]


def fake_models(monkeypatch: pytest.MonkeyPatch, fail: bool = False) -> threading.Event:
    """Patch the model loaders to record the device they were asked for."""
    import emotion_overlay.asr as asr
    import emotion_overlay.classify.audio as audio
    import emotion_overlay.classify.text as text

    release = threading.Event()

    def model(kind: str):
        def make(config: Any) -> Any:
            release.wait(5)
            if fail:
                raise RuntimeError("CUDA out of memory")
            attrs = {
                "device": config.device,
                "backend": "fake",
                "scores": lambda s, x: {"joy": 1.0},
            }
            return type(kind, (), attrs)()

        return make

    monkeypatch.setattr(asr, "load_transcriber", model("T"))
    monkeypatch.setattr(text, "TextEmotionClassifier", model("C"))
    monkeypatch.setattr(audio, "AudioEmotionClassifier", model("V"))
    return release


def wait_until(condition: Any) -> None:
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


def test_switching_processor_swaps_models_in_the_background(
    setup: tuple[Pipeline, FakeCapture, Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, _, recorder = setup
    pipeline._voice = FakeVoice({"happiness": 1.0})
    release = fake_models(monkeypatch)
    for section in (pipeline._config.asr, pipeline._config.classifier):
        section.device = "cuda"
    pipeline._config.audio_emotion.device = "cuda"
    pipeline._set_status("listening", "Listening on Fake Mic")
    old = pipeline._transcriber
    assert pipeline.reload_models()
    assert not pipeline.reload_models()  # one switch at a time
    assert pipeline._transcriber is old  # still serving while the new ones load
    release.set()
    wait_until(lambda: not pipeline.status()["switching"])
    assert pipeline.devices() == {"speech": "cuda", "text": "cuda", "voice": "cuda"}
    assert recorder.messages[-1]["state"] == "listening"  # status restored


def test_failed_switch_keeps_the_working_models(
    setup: tuple[Pipeline, FakeCapture, Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, _, recorder = setup
    release = fake_models(monkeypatch, fail=True)
    release.set()
    old = (pipeline._transcriber, pipeline._classifier)
    pipeline._set_status("listening", "Listening on Fake Mic")
    assert pipeline.reload_models()
    wait_until(lambda: not pipeline.status()["switching"])
    assert (pipeline._transcriber, pipeline._classifier) == old
    last = recorder.messages[-1]
    assert last["state"] == "listening" and "out of memory" in last["message"]
