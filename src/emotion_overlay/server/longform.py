"""Drives a long-form calibration: record live, or analyse an uploaded/linked
recording, then the user's review, then hand the fitted result to calibration.

States: idle -> recording | uploading | downloading -> (file) ready -> analysing
-> review -> (calibrate: result appears in the Calibration card) ... -> idle.
Uploaded and downloaded files go to a private temporary folder and are deleted as
soon as their audio has been analysed (or on cancel / shutdown).
"""

import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from emotion_overlay.audio.vad import SAMPLE_RATE, SpeechProb
from emotion_overlay.calibration import CalibrationError
from emotion_overlay.config import AppConfig
from emotion_overlay.emotions import Scores
from emotion_overlay.fetch import FetchError, check_public_host, fetch_media
from emotion_overlay.longform import LongformSession, analyse_stream
from emotion_overlay.media import AudioTrack, MediaError, decode, probe
from emotion_overlay.server.calibration import CalibrationController

log = logging.getLogger(__name__)

MAX_MEDIA_BYTES = 8 * 1024**3  # uploads and downloads
PUBLISH_EVERY_S = 0.5


class LongformEngine(Protocol):
    def status(self) -> dict[str, Any]: ...
    def set_sentence_observer(
        self, observer: Callable[[np.ndarray, str, Scores, Scores], None] | None
    ) -> None: ...
    def score_sentence(self, audio: np.ndarray) -> tuple[str, Scores, Scores]: ...


def _default_speech_prob() -> SpeechProb:
    from emotion_overlay.audio.vad import load_silero_speech_prob

    return load_silero_speech_prob()


class LongformController:
    def __init__(
        self,
        config: AppConfig,
        engine: LongformEngine | None,
        calibration: CalibrationController,
        publish: Callable[[dict[str, Any]], None],
        speech_prob: Callable[[], SpeechProb] = _default_speech_prob,
        check_host: Callable[[str], None] = check_public_host,
    ) -> None:
        self._config = config
        self._engine = engine
        self._calibration = calibration
        self._publish = publish
        self._speech_prob = speech_prob
        self._check_host = check_host
        self._lock = threading.RLock()
        self.state = "idle"
        self._session: LongformSession | None = None
        self._error: str | None = None
        self._cancel = threading.Event()
        self._started = 0.0  # live recording start (monotonic)
        self._dir: Path | None = None  # temporary folder for the media file
        self._file: Path | None = None
        self._file_name = ""
        self._file_source = "file"
        self._tracks: list[AudioTrack] = []
        self._progress: dict[str, Any] | None = None
        self._last_publish = 0.0
        calibration.blocked_by = self.blocks_calibration

    # --- live recording ---------------------------------------------------

    def start_recording(self) -> dict[str, Any]:
        engine = self._ready_engine()
        if not engine.status().get("listening", True):
            raise CalibrationError("start listening first")
        with self._lock:
            self._begin("recording")
            self._session = LongformSession("live", "Live recording")
            self._started = time.monotonic()
        engine.set_sentence_observer(self._on_live_sentence)
        return self._announce()

    def stop_recording(self) -> dict[str, Any]:
        with self._lock:
            if self.state != "recording":
                raise CalibrationError("not recording")
            if self._engine:
                self._engine.set_sentence_observer(None)
            self._to_review()
        return self._announce(with_sentences=True)

    def _on_live_sentence(
        self, audio: np.ndarray, text: str, scores: Scores, voice: Scores
    ) -> None:
        """From the pipeline's worker thread."""
        with self._lock:
            session = self._session
            if self.state != "recording" or session is None:
                return
            end = time.monotonic() - self._started
            session.add(max(0.0, end - len(audio) / SAMPLE_RATE), end, text, scores, voice, audio)
        self._announce()

    # --- files and links -------------------------------------------------

    def begin_upload(self, name: str) -> Path:
        """Reserve a temporary file for an upload; the route streams into it."""
        with self._lock:
            self._begin("uploading")
            self._file = self._new_dir() / "upload"
            self._file_name, self._file_source = (name or "recording")[:120], "file"
        self._announce()
        return self._file

    def upload_failed(self, message: str) -> None:
        with self._lock:
            self._cleanup()
            self.state, self._error = "error", message
        self._announce()

    def upload_done(self) -> dict[str, Any]:
        self._probe()
        return self._announce()

    def start_link(self, url: str) -> dict[str, Any]:
        with self._lock:
            self._begin("downloading")
            self._file = self._new_dir() / "download"
            self._file_source = "link"
            self._progress = {"done": 0, "total": None, "unit": "bytes"}
        threading.Thread(
            target=self._download, args=(url,), name="longform-download", daemon=True
        ).start()
        return self._announce()

    def _download(self, url: str) -> None:
        def progress(done: int, total: int | None) -> None:
            self._progress = {"done": done, "total": total, "unit": "bytes"}
            self._announce(throttle=True)

        try:
            assert self._file is not None  # noqa: S101
            with self._file.open("wb") as out:
                name = fetch_media(
                    url, out, MAX_MEDIA_BYTES, self._check_host, progress, self._cancel.is_set
                )
        except (FetchError, OSError) as exc:
            if not self._cancel.is_set():
                self.upload_failed(f"Couldn't download that link: {exc}")
            return
        with self._lock:
            self._file_name = name
        self._probe()
        self._announce()

    def _probe(self) -> None:
        with self._lock:
            path = self._file
        try:
            if path is None:
                raise MediaError("no file")
            tracks = probe(path)
        except MediaError as exc:
            self.upload_failed(str(exc))
            return
        with self._lock:
            self._tracks, self.state, self._progress = tracks, "ready", None

    def analyse(self, track: int) -> dict[str, Any]:
        engine = self._ready_engine()
        with self._lock:
            if self.state != "ready" or self._file is None:
                raise CalibrationError("add a recording first")
            if not 0 <= track < len(self._tracks):
                raise CalibrationError("that audio track doesn't exist")
            self.state = "analysing"
            self._cancel.clear()
            self._session = LongformSession(self._file_source, self._file_name)
            total = self._tracks[track].duration_s
            self._progress = {"done": 0.0, "total": total, "unit": "seconds"}
            path = self._file
        threading.Thread(
            target=self._analyse, args=(engine, path, track), name="longform-analyse", daemon=True
        ).start()
        return self._announce()

    def _analyse(self, engine: LongformEngine, path: Path, track: int) -> None:
        session = self._session
        assert session is not None  # noqa: S101

        def progress(done_s: float) -> None:
            if self._progress is not None:
                self._progress = {**self._progress, "done": round(done_s, 1)}
            self._announce(throttle=True)

        try:
            analyse_stream(
                decode(path, track),
                engine,
                self._speech_prob(),
                self._config.vad,
                session,
                self._cancel.is_set,
                progress,
            )
        except Exception as exc:
            log.exception("Long-form analysis failed")
            if not self._cancel.is_set():
                self.upload_failed(f"Analysis failed: {exc}")
            return
        with self._lock:
            if self._cancel.is_set() or self._session is not session:
                return
            self._cleanup()  # the file isn't needed any more
            self._to_review()
        self._announce(with_sentences=True)

    # --- review and result ------------------------------------------------

    def set_label(self, sentence_id: int, label: str | None) -> dict[str, Any]:
        session = self._review_session()
        session.set_label(sentence_id, label)
        return {"id": sentence_id, "label": label, **session.counts()}

    def wav(self, sentence_id: int) -> bytes:
        session = self._session
        sentence = session.get(sentence_id) if session else None
        if sentence is None:
            raise CalibrationError("no such sentence")
        return sentence.wav()

    def calibrate(self) -> dict[str, Any]:
        session = self._review_session()
        result = session.fit(self._config)
        self._calibration.offer(result)
        self._announce(with_sentences=True)  # guesses follow the updated correction
        return self._calibration.message()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._stop_everything()
            self.state, self._error = "idle", None
        return self._announce()

    def close(self) -> None:
        with self._lock:
            self._stop_everything()

    def blocks_calibration(self) -> str | None:
        """Reason the 14-line calibration can't start now, if any."""
        if self.state in ("recording", "analysing"):
            return "finish the long recording calibration first"
        return None

    # --- messages ---------------------------------------------------------

    def message(self, with_sentences: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self._session
            return {
                "type": "longform",
                "state": self.state,
                "error": self._error,
                "progress": self._progress,
                "file": self._file_name
                if self.state in ("uploading", "downloading", "ready")
                else None,
                "tracks": [t.to_api() for t in self._tracks] if self.state == "ready" else [],
                "session": session.to_api(self._config, with_sentences) if session else None,
                "full": with_sentences,
                "elapsed_s": round(time.monotonic() - self._started)
                if self.state == "recording"
                else None,
            }

    def _announce(self, with_sentences: bool = False, throttle: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if throttle and now - self._last_publish < PUBLISH_EVERY_S:
            return {}
        self._last_publish = now
        message = self.message(with_sentences)
        self._publish(message)
        return message

    # --- internals ----------------------------------------------------------

    def _ready_engine(self) -> LongformEngine:
        engine = self._engine
        if engine is None:
            raise CalibrationError("audio is disabled (started with --no-audio)")
        if not engine.status().get("ready", True):
            raise CalibrationError("the models are still loading; try again in a moment")
        if self._calibration.active:
            raise CalibrationError("finish the 14-line calibration first")
        return engine

    def _begin(self, state: str) -> None:
        """Caller holds the lock. Replaces whatever was there before."""
        if self.state in ("uploading", "downloading", "analysing", "recording"):
            raise CalibrationError("a long recording calibration is already running")
        self._stop_everything()
        self.state, self._error, self._progress = state, None, None
        self._cancel.clear()

    def _to_review(self) -> None:
        """Caller holds the lock."""
        assert self._session is not None  # noqa: S101
        self._session.update_auto(self._config.audio_emotion.label_map)
        self.state, self._progress = "review", None
        if not self._session.sentences:
            self.state = "error"
            self._error = "No speech was found in that recording."
            self._session = None

    def _review_session(self) -> LongformSession:
        with self._lock:
            if self.state != "review" or self._session is None:
                raise CalibrationError("nothing to review")
            return self._session

    def _new_dir(self) -> Path:
        self._dir = Path(tempfile.mkdtemp(prefix="emotion-overlay-"))
        return self._dir

    def _stop_everything(self) -> None:
        """Caller holds the lock."""
        self._cancel.set()
        if self.state == "recording" and self._engine:
            self._engine.set_sentence_observer(None)
        self._cleanup()
        self._session = None
        self._tracks, self._progress = [], None

    def _cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = self._file = None
