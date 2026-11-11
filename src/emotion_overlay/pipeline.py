"""Wires mic -> VAD -> Whisper -> text classifier -> state machine on worker threads.

Threads:
- PortAudio callback: resamples and enqueues raw audio (must stay cheap).
- vad: segments audio into utterances, tracks speaking/silence, ticks the state machine.
- asr: transcribes each utterance, classifies the rolling transcript, updates the state.

Results leave through `PipelineEvents` callbacks, which must be thread-safe.
Transcripts are only logged at DEBUG level so they don't end up in console logs by default.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from emotion_overlay.audio.capture import MicCapture
from emotion_overlay.audio.vad import SAMPLE_RATE, Utterance, UtteranceSegmenter
from emotion_overlay.classify.window import TranscriptWindow
from emotion_overlay.config import AppConfig
from emotion_overlay.debug_store import DebugRecorder
from emotion_overlay.state_machine import EmotionState, EmotionStateMachine

log = logging.getLogger(__name__)

Message = dict[str, Any]


@dataclass(frozen=True)
class PipelineEvents:
    # (state, new_utterance): sent after every classified utterance (so intensity
    # tiers and per-utterance image rotation can react) and on time-based changes.
    on_state: Callable[[EmotionState, bool], None]
    on_message: Callable[[Message], None]  # status, vad and debug messages


class Pipeline:
    def __init__(self, config: AppConfig, events: PipelineEvents) -> None:
        self._config = config
        self._events = events
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)  # ~10 s of audio
        self._utt_q: queue.Queue[Utterance] = queue.Queue()
        self._stop = threading.Event()
        self._reset_vad = threading.Event()
        self._state_lock = threading.Lock()
        self._machine = EmotionStateMachine(config.state, time.monotonic())
        self._last_emitted = self._machine.state
        self._window = TranscriptWindow(config.classifier.window_seconds)
        self._recorder = DebugRecorder(config.debug, config.data_dir)
        self._capture = MicCapture(self._on_audio)
        self._segmenter: UtteranceSegmenter | None = None
        self._transcriber: Any = None
        self._classifier: Any = None
        self._status: Message = {"type": "status", "state": "idle", "message": "Not started"}
        self._dropped_blocks = 0

    # --- control (called from the server) ---------------------------------

    def start(self) -> None:
        threading.Thread(target=self._boot, name="pipeline-boot", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._capture.stop()

    def set_device(self, device: int | None) -> None:
        self._config.audio.device = device
        if self._segmenter is None:
            return  # still loading; _boot opens the configured device when ready
        self._capture.stop()
        self._reset_vad.set()
        self._open_mic()

    def status(self) -> Message:
        return {
            **self._status,
            "device": self._capture.device_name or None,
            "asr": f"{self._transcriber.backend} on {self._transcriber.device}"
            if self._transcriber
            else None,
            "pending_utterances": self._utt_q.qsize(),
            "dropped_audio_blocks": self._dropped_blocks,
        }

    # --- startup ----------------------------------------------------------

    def _boot(self) -> None:
        from emotion_overlay.asr import load_transcriber
        from emotion_overlay.audio.vad import load_silero_speech_prob
        from emotion_overlay.classify.text import TextEmotionClassifier

        cfg = self._config
        try:
            self._set_status("loading", "Loading voice activity model...")
            self._segmenter = UtteranceSegmenter(load_silero_speech_prob(), cfg.vad)
            self._set_status(
                "loading", f"Loading speech model '{cfg.asr.model}' (first run downloads it)..."
            )
            self._transcriber = load_transcriber(cfg.asr)
            self._set_status("loading", "Loading emotion model (first run downloads it)...")
            self._classifier = TextEmotionClassifier(cfg.classifier)
        except Exception as exc:
            log.exception("Model loading failed")
            self._set_status("error", f"Model loading failed: {exc}")
            return

        for target, name in ((self._vad_loop, "vad"), (self._asr_loop, "asr")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self._open_mic()

    def _open_mic(self) -> None:
        try:
            self._capture.start(self._config.audio.device)
        except Exception as exc:
            log.exception("Could not open microphone")
            self._set_status("mic_error", f"Could not open microphone: {exc}")
            return
        self._set_status("listening", f"Listening on {self._capture.device_name}")

    # --- workers ----------------------------------------------------------

    def _on_audio(self, block: np.ndarray) -> None:
        try:
            self._audio_q.put_nowait(block)
        except queue.Full:
            self._dropped_blocks += 1
            if self._dropped_blocks % 100 == 1:
                log.warning("Audio queue full; dropped %d blocks so far", self._dropped_blocks)

    def _vad_loop(self) -> None:
        assert self._segmenter is not None  # noqa: S101 - set before this thread starts
        segmenter = self._segmenter
        was_speaking = False
        while not self._stop.is_set():
            if self._reset_vad.is_set():
                self._reset_vad.clear()
                segmenter.reset()
                self._drain(self._audio_q)
            try:
                block = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                block = None
            utterances = segmenter.feed(block) if block is not None else []
            for utterance in utterances:
                self._utt_q.put(utterance)

            speaking = segmenter.speaking
            if speaking != was_speaking:
                was_speaking = speaking
                self._events.on_message({"type": "vad", "speaking": speaking})

            now = time.monotonic()
            with self._state_lock:
                if speaking:
                    self._machine.on_activity(now)
                self._emit_if_changed(self._machine.tick(now))

    def _asr_loop(self) -> None:
        while not self._stop.is_set():
            try:
                utterance = self._utt_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle_utterance(utterance)
            except Exception:
                log.exception("Failed to process utterance")

    def _handle_utterance(self, utterance: Utterance) -> None:
        started = time.monotonic()  # same clock as utterance.ended_at
        t_start = time.perf_counter()
        text = self._transcriber.transcribe(utterance.audio)
        t_asr = time.perf_counter()
        self._recorder.record(utterance.audio, SAMPLE_RATE, text)
        if not text:
            return

        self._window.seconds = self._config.classifier.window_seconds  # live-editable
        self._window.add(text, utterance.ended_at)
        window_text = self._window.text(time.monotonic())
        scores = self._classifier.scores(window_text)
        t_classify = time.perf_counter()

        now = time.monotonic()
        with self._state_lock:
            state = self._machine.on_scores(scores, now)
            smoothed = self._machine.smoothed
            self._last_emitted = state
            self._events.on_state(state, True)

        # The VAD only notices the end of speech after min_silence_ms of quiet.
        detect_delay = 0.0 if utterance.forced else self._config.vad.min_silence_ms / 1000
        timings = {
            "queue_ms": round((started - utterance.ended_at) * 1000),
            "asr_ms": round((t_asr - t_start) * 1000),
            "classify_ms": round((t_classify - t_asr) * 1000),
            "speech_end_to_state_ms": round((now - utterance.ended_at + detect_delay) * 1000),
        }
        log.info(
            "utterance %.1fs%s | asr %d ms | classify %d ms | speech end -> state %d ms | %s %.2f",
            utterance.duration_s,
            " (cut)" if utterance.forced else "",
            timings["asr_ms"],
            timings["classify_ms"],
            timings["speech_end_to_state_ms"],
            state.emotion,
            state.confidence,
        )
        log.debug("transcript: %s", text)
        self._events.on_message(
            {
                "type": "debug",
                "transcript": text,
                "window": window_text,
                "scores": scores,
                "smoothed": smoothed,
                "emotion": state.emotion,
                "confidence": state.confidence,
                "forced": utterance.forced,
                "duration_s": round(utterance.duration_s, 2),
                "timings": timings,
            }
        )

    # --- helpers ----------------------------------------------------------

    def _emit_if_changed(self, state: EmotionState) -> None:
        """Publish a time-based switch (silence, delayed switch). Caller must hold _state_lock."""
        last = self._last_emitted
        if (state.emotion, state.since) != (last.emotion, last.since):
            self._last_emitted = state
            self._events.on_state(state, False)

    def _set_status(self, state: str, message: str) -> None:
        log.info(message)
        self._status = {"type": "status", "state": state, "message": message}
        self._events.on_message(self._status)

    @staticmethod
    def _drain(q: queue.Queue[Any]) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return
