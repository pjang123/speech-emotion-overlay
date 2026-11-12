"""Wires mic -> VAD -> (Whisper -> text model) + (voice model) -> fusion -> state machine.

Threads:
- PortAudio callback: resamples and enqueues raw audio (must stay cheap).
- vad: segments audio into utterances, tracks speaking/silence, ticks the state machine.
- asr: transcribes each utterance, classifies the rolling transcript, fuses it with
  the voice model's scores, updates the state. While someone keeps talking it also
  gets "interim" jobs (the sentence so far, every `classifier.interim_s` seconds of
  speech, only when it's idle) so the image can change before the sentence ends.
  Interims use the words only; the finished sentence is scored in full as usual.
- voice (pool of 1): scores tone of voice in parallel with transcription. If it's
  still running when the words are scored, the words-only result is shown first and
  the combined one replaces it when the voice is done, so a slow voice model never
  delays the first reaction.

Results leave through `PipelineEvents` callbacks, which must be thread-safe.
Transcripts are only logged at DEBUG level so they don't end up in console logs by default.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np

from emotion_overlay.audio.capture import MicCapture
from emotion_overlay.audio.vad import SAMPLE_RATE, Utterance, UtteranceSegmenter
from emotion_overlay.calibration import CALIBRATION_SILENCE_MS, Calibration, SpeechStats
from emotion_overlay.classify.fusion import score_utterance
from emotion_overlay.classify.window import TranscriptWindow
from emotion_overlay.config import AppConfig
from emotion_overlay.debug_store import DebugRecorder
from emotion_overlay.emotions import Scores
from emotion_overlay.slang import plain_english
from emotion_overlay.state_machine import EmotionState, EmotionStateMachine

log = logging.getLogger(__name__)

Message = dict[str, Any]
# Receives (text scores for this utterance alone, raw voice scores, how it was spoken)
# during calibration.
CalibrationListener = Callable[[Scores, Scores, SpeechStats], None]


@dataclass(frozen=True)
class Interim:
    """The part of an utterance spoken so far, while the speaker is still going."""

    audio: np.ndarray


@dataclass(frozen=True)
class PipelineEvents:
    # (state, new_utterance): sent after every classified utterance (so intensity
    # tiers and per-utterance image rotation can react) and on time-based changes.
    on_state: Callable[[EmotionState, bool], None]
    on_message: Callable[[Message], None]  # status, vad and debug messages
    # Each utterance's transcript, as soon as it's known (for word triggers).
    on_words: Callable[[str], None] = lambda text: None


class Pipeline:
    def __init__(self, config: AppConfig, events: PipelineEvents) -> None:
        self._config = config
        self._events = events
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)  # ~10 s of audio
        self._utt_q: queue.Queue[Utterance | Interim] = queue.Queue()
        self._asr_busy = False
        # Length of the utterance in progress at which to send the next interim.
        self._interim_due_s = config.classifier.interim_s
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
        self._voice: Any = None
        self._voice_state = "off"  # off | loading | ready | failed
        self._voice_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")
        self._calibration: Calibration | None = None
        self._calibration_listener: CalibrationListener | None = None
        self._status: Message = {"type": "status", "state": "idle", "message": "Not started"}
        self._dropped_blocks = 0
        self._listening = True  # runtime only: every start begins listening
        self._control_lock = threading.Lock()  # serialises start/stop/device changes
        self._reload_lock = threading.Lock()  # one CPU/GPU switch at a time

    # --- control (called from the server) ---------------------------------

    def start(self) -> None:
        threading.Thread(target=self._boot, name="pipeline-boot", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._capture.stop()
        self._voice_pool.shutdown(wait=False, cancel_futures=True)

    def set_device(self, device: int | None) -> None:
        with self._control_lock:
            self._config.audio.device = device
            if self._segmenter is None or not self._listening:
                return  # opened with the new device on boot / when listening resumes
            self._capture.stop()
            self._reset_vad.set()
            self._open_mic()

    def set_listening(self, listening: bool) -> None:
        """Pause or resume. Pausing closes the microphone entirely (the OS mic-in-use
        indicator goes off), discards unprocessed speech and shows neutral."""
        with self._control_lock:
            if listening == self._listening:
                return
            self._listening = listening
            if self._segmenter is None:
                return  # still loading; _boot respects the flag
            if listening:
                self._open_mic()
                return
            self._capture.stop()
            self._reset_vad.set()
            self._drain(self._utt_q)
            self._window.clear()
            with self._state_lock:
                state = self._machine.reset(time.monotonic(), "paused")
                self._last_emitted = state
                self._events.on_state(state, False)
            self._set_status("paused", "Paused: microphone is off")

    def reload_models(self) -> bool:
        """Reload the models with the current `device` settings (CPU/GPU switch), in
        the background. The old models keep working until the new ones are ready.
        Returns False if models are still loading or a switch is already running."""
        if self._transcriber is None or not self._reload_lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._reload, name="model-reload", daemon=True).start()
        return True

    def _reload(self) -> None:
        from emotion_overlay.asr import load_transcriber
        from emotion_overlay.classify.audio import AudioEmotionClassifier
        from emotion_overlay.classify.text import TextEmotionClassifier
        from emotion_overlay.compute import release_gpu_memory

        cfg = self._config
        previous = self._status
        try:
            self._set_status("loading", "Switching processor: loading speech-to-text...")
            transcriber = load_transcriber(cfg.asr)
            self._set_status("loading", "Switching processor: loading the emotion models...")
            classifier = TextEmotionClassifier(cfg.classifier)
            voice = AudioEmotionClassifier(cfg.audio_emotion) if self._voice is not None else None
        except Exception as exc:
            log.exception("Switching processor failed; keeping the current models")
            self._status = {**previous, "message": f"Couldn't switch processor: {exc}"}
        else:
            self._transcriber, self._classifier = transcriber, classifier
            if voice is not None:
                self._voice = voice
            release_gpu_memory()
            self._status = previous
            log.info("Models now on: %s", self.devices())
        finally:
            self._reload_lock.release()
        self._events.on_message(self.status())

    def devices(self) -> dict[str, str | None]:
        """Where each model is running ('cpu'/'cuda'), None if not loaded."""
        return {
            "speech": getattr(self._transcriber, "device", None),
            "text": getattr(self._classifier, "device", None),
            "voice": getattr(self._voice, "device", None),
        }

    def set_calibration(self, calibration: Calibration | None) -> None:
        self._calibration = calibration

    def set_calibration_listener(self, listener: CalibrationListener | None) -> None:
        """While set, utterances are handed to the listener instead of changing the display."""
        self._calibration_listener = listener
        self._window.clear()  # don't mix calibration lines with live speech
        if isinstance(self._segmenter, UtteranceSegmenter):
            # A longer end-of-line pause keeps each prompt in one piece.
            self._segmenter.min_silence_override_ms = CALIBRATION_SILENCE_MS if listener else None

    def status(self) -> Message:
        return {
            **self._status,
            "listening": self._listening,
            "voice_model": self._voice_state,
            "device": self._capture.device_name or None,
            "asr": f"{self._transcriber.backend} on {self._transcriber.device}"
            if self._transcriber
            else None,
            "devices": self.devices(),
            "switching": self._reload_lock.locked(),
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
        if cfg.audio_emotion.enabled:
            self._set_status("loading", "Loading tone-of-voice model (first run downloads it)...")
            self._load_voice_model()

        for target, name in ((self._vad_loop, "vad"), (self._asr_loop, "asr")):
            threading.Thread(target=target, name=name, daemon=True).start()
        with self._control_lock:
            if self._listening:
                self._open_mic()
            else:
                self._set_status("paused", "Paused: microphone is off")

    def _load_voice_model(self) -> None:
        """Optional model: failure is logged and the app carries on with text only."""
        from emotion_overlay.classify.audio import AudioEmotionClassifier

        self._voice_state = "loading"
        try:
            self._voice = AudioEmotionClassifier(self._config.audio_emotion)
            self._voice_state = "ready"
        except Exception:
            log.exception("Tone-of-voice model failed to load; continuing with text only")
            self._voice_state = "failed"
        self._events.on_message(self.status())

    def _voice_ready(self) -> bool:
        """True if voice scoring should run now. Enabling it live loads it in the background."""
        if not self._config.audio_emotion.enabled:
            return False
        if self._voice is not None:
            return True
        if self._voice_state == "off":
            self._voice_state = "loading"
            threading.Thread(target=self._load_voice_model, name="voice-load", daemon=True).start()
        return False

    def _open_mic(self) -> None:
        audio = self._config.audio
        try:
            self._capture.start(audio.device, audio.device_name, audio.device_hostapi)
        except Exception as exc:
            log.exception("Could not open microphone")
            self._set_status("mic_error", f"Could not open microphone: {exc}")
            return
        message = f"Listening on {self._capture.device_name}"
        if self._capture.missing_device:
            message += f" ('{self._capture.missing_device}' isn't connected)"
        self._set_status("listening", message)

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
            self._maybe_interim(segmenter, restart=bool(utterances))

            speaking = segmenter.speaking
            if speaking != was_speaking:
                was_speaking = speaking
                self._events.on_message({"type": "vad", "speaking": speaking})

            now = time.monotonic()
            with self._state_lock:
                if speaking:
                    self._machine.on_activity(now)
                self._emit_if_changed(self._machine.tick(now))

    def _maybe_interim(self, segmenter: UtteranceSegmenter, restart: bool) -> None:
        """Queue the sentence so far if it has grown by another `interim_s` of speech."""
        interval = self._config.classifier.interim_s
        length = segmenter.buffered_s
        if restart or not segmenter.speaking:
            self._interim_due_s = interval
        if interval <= 0 or not segmenter.speaking or length < self._interim_due_s:
            return
        if segmenter.trailing_silence_s > 0:
            return  # maybe the end of the sentence: don't delay the final result
        self._interim_due_s = length + interval
        idle = not self._asr_busy and self._utt_q.empty()
        if idle and self._listening and self._calibration_listener is None:
            self._utt_q.put(Interim(segmenter.partial()))

    def _asr_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._utt_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._asr_busy = True
            try:
                if isinstance(item, Interim):
                    if self._utt_q.empty():  # the finished sentence is already waiting
                        self._handle_interim(item)
                else:
                    self._handle_utterance(item)
            except Exception:
                log.exception("Failed to process utterance")
            finally:
                self._asr_busy = False

    def _handle_interim(self, interim: Interim) -> None:
        cfg = self._config
        t_start = time.perf_counter()
        text = self._transcriber.transcribe(interim.audio)
        if not text or not self._listening or self._calibration_listener is not None:
            return
        self._window.seconds = cfg.classifier.window_seconds
        window_text = self._window.preview(text, time.monotonic())
        if cfg.classifier.slang:
            window_text = plain_english(window_text)
        calibration = self._calibration
        breakdown = score_utterance(
            self._classifier.scores(window_text),
            {},  # the voice model is too slow to run twice; the full sentence gets it
            cfg.audio_emotion.label_map,
            cfg.audio_emotion.weight,
            calibration.voice_bias if calibration else None,
            calibration.final_bias if calibration else None,
        )
        with self._state_lock:
            state = self._machine.on_scores(breakdown.final, time.monotonic())
            smoothed = self._machine.smoothed
            self._last_emitted = state
            self._events.on_state(state, False)
        took_ms = round((time.perf_counter() - t_start) * 1000)
        log.debug("interim %.1fs (%d ms): %s", len(interim.audio) / SAMPLE_RATE, took_ms, text)
        self._events.on_message(
            {
                "type": "debug",
                "interim": True,
                "transcript": text,
                "window": window_text,
                "text_scores": breakdown.text,
                "scores": breakdown.final,
                "smoothed": smoothed,
                "calibrated": calibration is not None,
                "emotion": state.emotion,
                "confidence": state.confidence,
                "duration_s": round(len(interim.audio) / SAMPLE_RATE, 2),
                "took_ms": took_ms,
            }
        )

    def _score_voice(self, audio: np.ndarray) -> tuple[Scores, float]:
        t = time.perf_counter()
        return self._voice.scores(audio), (time.perf_counter() - t) * 1000

    def _handle_utterance(self, utterance: Utterance) -> None:
        cfg = self._config
        started = time.monotonic()  # same clock as utterance.ended_at
        voice_job: Future[tuple[Scores, float]] | None = None
        if self._voice_ready():
            voice_job = self._voice_pool.submit(self._score_voice, utterance.audio)

        t_start = time.perf_counter()
        text = self._transcriber.transcribe(utterance.audio)
        t_asr = time.perf_counter()
        if not self._listening:
            return  # paused while this was being transcribed: drop it
        self._recorder.record(utterance.audio, SAMPLE_RATE, text)

        listener = self._calibration_listener
        if text and not listener:
            # Before emotion scoring, so a trigger shows as soon as the words are known.
            try:
                self._events.on_words(text)
            except Exception:
                log.exception("Word trigger handling failed")
        text_scores: Scores = {}
        window_text = ""
        if text:
            if listener:
                window_text = text  # calibration scores each line on its own
            else:
                self._window.seconds = cfg.classifier.window_seconds  # live-editable
                self._window.add(text, utterance.ended_at)
                window_text = self._window.text(time.monotonic())
            if cfg.classifier.slang:
                window_text = plain_english(window_text)  # shown as what the model read
            text_scores = self._classifier.scores(window_text)
        t_classify = time.perf_counter()

        early_ms = None
        new_utterance = True
        voice_pending = voice_job is not None and not voice_job.done()
        if voice_pending and text_scores and not listener and self._show_words_first(text_scores):
            early_ms = round((time.monotonic() - utterance.ended_at) * 1000)
            new_utterance = False  # the image was already re-picked for this one

        voice_raw: Scores = {}
        voice_ms = None
        if voice_job is not None:
            try:
                voice_raw, voice_ms = voice_job.result()
            except Exception:
                log.exception("Voice scoring failed for this utterance")
        if not text_scores and not voice_raw:
            return  # nothing usable (e.g. a cough Whisper heard no words in)

        if listener:
            listener(
                text_scores,
                voice_raw,
                SpeechStats(utterance.pauses_ms, utterance.speech_probs, utterance.noise_probs),
            )
            self._events.on_message(
                {"type": "debug", "transcript": text, "calibrating": True, "scores": text_scores}
            )
            return

        calibration = self._calibration
        breakdown = score_utterance(
            text_scores,
            voice_raw,
            cfg.audio_emotion.label_map,
            cfg.audio_emotion.weight,
            calibration.voice_bias if calibration else None,
            calibration.final_bias if calibration else None,
        )
        now = time.monotonic()
        with self._state_lock:
            state = self._machine.on_scores(breakdown.final, now)
            smoothed = self._machine.smoothed
            self._last_emitted = state
            self._events.on_state(state, new_utterance)

        # The VAD only notices the end of speech after min_silence_ms of quiet.
        detect_delay = 0.0 if utterance.forced else cfg.vad.min_silence_ms / 1000
        timings = {
            "queue_ms": round((started - utterance.ended_at) * 1000),
            "asr_ms": round((t_asr - t_start) * 1000),
            "classify_ms": round((t_classify - t_asr) * 1000),
            "voice_ms": round(voice_ms) if voice_ms is not None else None,
            "speech_end_to_state_ms": round((now - utterance.ended_at + detect_delay) * 1000),
            # When the words-only result was shown before the voice was done.
            "words_shown_ms": round(early_ms + detect_delay * 1000)
            if early_ms is not None
            else None,
        }
        log.info(
            "utterance %.1fs%s | asr %d ms | text %d ms | voice %s ms | speech end -> state %d ms"
            " | %s %.2f",
            utterance.duration_s,
            " (cut)" if utterance.forced else "",
            timings["asr_ms"],
            timings["classify_ms"],
            timings["voice_ms"] if voice_ms is not None else "-",
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
                "text_scores": breakdown.text,
                "voice": breakdown.voice_raw,
                "scores": breakdown.final,
                "smoothed": smoothed,
                "calibrated": calibration is not None,
                "emotion": state.emotion,
                "confidence": state.confidence,
                "forced": utterance.forced,
                "duration_s": round(utterance.duration_s, 2),
                "timings": timings,
            }
        )

    def _show_words_first(self, text_scores: Scores) -> bool:
        """Show the words-only result while the voice model is still working.

        Skipped when anti-flicker rules are on: they count classifier windows, and one
        sentence must stay one window there.
        """
        cfg = self._config
        if cfg.state.persist_windows > 1 or cfg.state.smoothing < 1.0:
            return False
        calibration = self._calibration
        breakdown = score_utterance(
            text_scores,
            {},
            cfg.audio_emotion.label_map,
            cfg.audio_emotion.weight,
            calibration.voice_bias if calibration else None,
            calibration.final_bias if calibration else None,
        )
        with self._state_lock:
            state = self._machine.on_scores(breakdown.final, time.monotonic())
            self._last_emitted = state
            self._events.on_state(state, True)
        return True

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
        self._events.on_message(self.status())

    @staticmethod
    def _drain(q: queue.Queue[Any]) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return
