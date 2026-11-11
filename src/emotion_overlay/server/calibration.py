"""Runs a calibration session against the live pipeline and publishes its progress."""

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

from emotion_overlay.calibration import (
    Calibration,
    CalibrationError,
    CalibrationSession,
    CalibrationStore,
    calibrate,
)
from emotion_overlay.config import AppConfig
from emotion_overlay.emotions import Scores

log = logging.getLogger(__name__)


class CalibratableEngine(Protocol):
    def status(self) -> dict[str, Any]: ...
    def set_calibration(self, calibration: Calibration | None) -> None: ...
    def set_calibration_listener(
        self, listener: Callable[[Scores, Scores], None] | None
    ) -> None: ...


class CalibrationController:
    def __init__(
        self,
        config: AppConfig,
        store: CalibrationStore,
        engine: CalibratableEngine | None,
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self._config = config
        self._store = store
        self._engine = engine
        self._publish = publish
        self._lock = threading.Lock()
        self._session: CalibrationSession | None = None
        self._result: Calibration | None = None
        self._error: str | None = None
        self.saved = store.load(list(config.audio_emotion.label_map))
        if engine is not None:
            engine.set_calibration(self.saved)

    # --- actions (from API routes) ---------------------------------------

    def start(self) -> dict[str, Any]:
        if self._engine is None:
            raise CalibrationError("audio is disabled (started with --no-audio)")
        if not self._engine.status().get("listening", True):
            raise CalibrationError("start listening first")
        with self._lock:
            self._session = CalibrationSession()
            self._result = self._error = None
            self._engine.set_calibration_listener(self._on_sample)
        return self._announce()

    def skip(self) -> dict[str, Any]:
        with self._lock:
            if self._session:
                self._session.skip()
                self._finish_if_done()
        return self._announce()

    def back(self) -> dict[str, Any]:
        with self._lock:
            if self._session:
                self._session.back()
        return self._announce()

    def finish_early(self) -> dict[str, Any]:
        with self._lock:
            if self._session:
                self._session.index = len(self._session.prompts)
                self._finish_if_done()
        return self._announce()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._stop_listening()
            self._session = self._result = None
            self._error = None
        return self._announce()

    def save(self) -> dict[str, Any]:
        with self._lock:
            if self._result is None:
                raise CalibrationError("no finished calibration to save")
            self._store.save(self._result)
            self.saved, self._result, self._session = self._result, None, None
        if self._engine:
            self._engine.set_calibration(self.saved)
        return self._announce()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._store.delete()
            self.saved = None
        if self._engine:
            self._engine.set_calibration(None)
        return self._announce()

    def message(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "calibration",
                "active": self._session is not None and not self._session.done,
                "session": self._session.to_api() if self._session else None,
                "result": self._result.to_api() if self._result else None,
                "saved": self.saved.to_api() if self.saved else None,
                "error": self._error,
            }

    # --- internals ----------------------------------------------------------

    def _on_sample(self, text: Scores, voice: Scores) -> None:
        """Called from the pipeline's worker thread for each utterance."""
        with self._lock:
            if self._session is None or self._session.done:
                return
            self._session.record(text, voice)
            self._finish_if_done()
        self._announce()

    def _finish_if_done(self) -> None:
        """Caller holds the lock."""
        assert self._session is not None  # noqa: S101
        if not self._session.done:
            return
        self._stop_listening()
        audio = self._config.audio_emotion
        try:
            self._result = calibrate(self._session.samples, audio.label_map, audio.weight)
        except CalibrationError as exc:
            self._error = str(exc)

    def _stop_listening(self) -> None:
        if self._engine:
            self._engine.set_calibration_listener(None)

    def _announce(self) -> dict[str, Any]:
        message = self.message()
        self._publish(message)
        return message
