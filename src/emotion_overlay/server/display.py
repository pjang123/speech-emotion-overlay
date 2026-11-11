"""Maps the displayed emotion to a concrete image and builds the overlay's state message.

An image is (re)picked when the emotion changes, when the current image is no
longer eligible (deleted, intensity tier changed, new upload for this emotion),
or on every utterance if `new_image_each_utterance` is on.
"""

import random
import threading
from typing import Any

from emotion_overlay.config import DisplayConfig
from emotion_overlay.emotions import NEUTRAL
from emotion_overlay.images import ImageRecord, ImageStore
from emotion_overlay.state_machine import EmotionState


class DisplayState:
    def __init__(
        self, store: ImageStore, config: DisplayConfig, rng: random.Random | None = None
    ) -> None:
        self._store = store
        self._config = config
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._cycle: dict[tuple[str, str], int] = {}
        self._state = EmotionState(NEUTRAL, 0.0, "startup", 0.0)
        self._image: ImageRecord | None = None
        self._update(force_new=True)

    def tier(self, confidence: float) -> str:
        return "strong" if confidence >= self._config.strong_threshold else "mild"

    def apply(self, state: EmotionState, new_utterance: bool = False) -> dict[str, Any]:
        """Show a (possibly unchanged) state; `new_utterance` marks fresh classifier output."""
        with self._lock:
            emotion_changed = state.emotion != self._state.emotion
            self._state = state
            reshuffle = new_utterance and self._config.new_image_each_utterance
            self._update(force_new=emotion_changed or reshuffle)
            return self._message()

    def refresh(self) -> dict[str, Any]:
        """Images or settings changed: re-pick only if the current image is no longer eligible."""
        with self._lock:
            self._update(force_new=False)
            return self._message()

    def message(self) -> dict[str, Any]:
        with self._lock:
            return self._message()

    def _update(self, force_new: bool) -> None:
        tier = self.tier(self._state.confidence)
        pool = self._store.pool(self._state.emotion, self._config.fallbacks, tier)
        if force_new or self._image not in pool:
            self._image = self._choose(pool, (self._state.emotion, tier))

    def _choose(self, pool: list[ImageRecord], key: tuple[str, str]) -> ImageRecord | None:
        if not pool:
            return None
        if self._config.rotation == "cycle":
            index = (self._cycle.get(key, -1) + 1) % len(pool)
            self._cycle[key] = index
            return pool[index]
        # Random, but never the image already on screen when there is a choice.
        others = [image for image in pool if image != self._image]
        return self._rng.choice(others or pool)

    def _message(self) -> dict[str, Any]:
        return {
            "type": "state",
            "emotion": self._state.emotion,
            "confidence": round(self._state.confidence, 3),
            "tier": self.tier(self._state.confidence),
            "reason": self._state.reason,
            "image": self._image.url if self._image else None,
            "transition": self._config.transition,
            "fade_ms": self._config.fade_ms,
        }
