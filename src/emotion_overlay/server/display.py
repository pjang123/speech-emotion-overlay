"""Maps the displayed emotion to a concrete image and builds the overlay's state message.

An image is (re)picked when the emotion changes, when the current image is no
longer eligible (deleted, intensity tier changed, new upload for this emotion),
or on every utterance if `new_image_each_utterance` is on.

A word trigger temporarily overrides the emotion image; emotion changes keep
being tracked underneath, so the right image is back as soon as it ends.

`play` increases every time a (new) image starts showing. The overlay uses it to
restart animated GIFs from their first frame instead of resuming mid-loop.
"""

import random
import threading
from dataclasses import dataclass
from typing import Any

from emotion_overlay.config import DisplayConfig
from emotion_overlay.emotions import NEUTRAL
from emotion_overlay.images import ImageRecord, ImageStore
from emotion_overlay.state_machine import EmotionState


@dataclass(frozen=True)
class _Override:
    image: ImageRecord
    phrase: str
    token: int


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
        self._override: _Override | None = None
        self._play = 0
        self._tokens = 0
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
            if self._override and self._store.get(self._override.image.id) is None:
                self._override = None
                self._play += 1
            self._update(force_new=False)
            return self._message()

    def trigger(self, image: ImageRecord, phrase: str) -> tuple[dict[str, Any], int]:
        """Show `image` until end_trigger(token) is called. Returns (message, token)."""
        with self._lock:
            self._tokens += 1
            self._override = _Override(image, phrase, self._tokens)
            self._play += 1  # restart even if the same GIF is already showing
            return self._message(), self._tokens

    def end_trigger(self, token: int) -> dict[str, Any] | None:
        """End the override, unless a newer trigger has replaced it since."""
        with self._lock:
            if self._override is None or self._override.token != token:
                return None
            self._override = None
            self._play += 1
            return self._message()

    def message(self) -> dict[str, Any]:
        with self._lock:
            return self._message()

    def _update(self, force_new: bool) -> None:
        tier = self.tier(self._state.confidence)
        pool = self._store.pool(self._state.emotion, self._config.fallbacks, tier)
        if force_new or self._image not in pool:
            previous = self._image
            self._image = self._choose(pool, (self._state.emotion, tier))
            if self._image != previous or force_new:
                self._play += 1

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
        shown = self._override.image if self._override else self._image
        return {
            "type": "state",
            "emotion": self._state.emotion,
            "confidence": round(self._state.confidence, 3),
            "tier": self.tier(self._state.confidence),
            "reason": self._state.reason,
            "image": shown.url if shown else None,
            "play": self._play,
            "trigger": self._override.phrase if self._override else None,
            "transition": self._config.transition,
            "fade_ms": self._config.fade_ms,
        }
