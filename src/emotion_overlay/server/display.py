"""Maps the displayed emotion to a concrete image and builds the overlay's state message."""

import threading
from typing import Any

from emotion_overlay.config import DisplayConfig
from emotion_overlay.emotions import NEUTRAL
from emotion_overlay.images import ImageRecord, ImageStore
from emotion_overlay.state_machine import EmotionState


class DisplayState:
    def __init__(self, store: ImageStore, config: DisplayConfig) -> None:
        self._store = store
        self._config = config
        self._lock = threading.Lock()
        self._state = EmotionState(NEUTRAL, 0.0, "startup", 0.0)
        self._image: ImageRecord | None = store.choose(NEUTRAL, config.fallbacks)

    def apply(self, state: EmotionState) -> dict[str, Any]:
        """New displayed emotion: pick a fresh image for it."""
        with self._lock:
            if state.emotion != self._state.emotion or self._image is None:
                self._image = self._store.choose(state.emotion, self._config.fallbacks)
            self._state = state
            return self._message()

    def refresh(self) -> dict[str, Any]:
        """Images changed: keep the current image only if it still exists and belongs to
        the current emotion (otherwise a new upload for that emotion would never show)."""
        with self._lock:
            current = self._image
            still_valid = (
                current is not None
                and self._store.get(current.id) is not None
                and current.emotion == self._state.emotion
            )
            if not still_valid:
                self._image = self._store.choose(self._state.emotion, self._config.fallbacks)
            return self._message()

    def message(self) -> dict[str, Any]:
        with self._lock:
            return self._message()

    def _message(self) -> dict[str, Any]:
        return {
            "type": "state",
            "emotion": self._state.emotion,
            "confidence": round(self._state.confidence, 3),
            "reason": self._state.reason,
            "image": self._image.url if self._image else None,
            "fade_ms": self._config.fade_ms,
        }
