"""Maps the displayed emotion to a concrete image and builds the overlay's state message.

An image is (re)picked when the emotion changes, when the current image is no
longer eligible (deleted, intensity tier changed, new upload for this emotion),
or on every utterance if `new_image_each_utterance` is on.

A word trigger temporarily overrides the emotion image, and a manual override
(hotkey or setup page) holds a chosen emotion until it's released. Detected
emotion changes keep being tracked underneath either, so the right image is back
as soon as they end. Order on screen: trigger, then manual, then detected.

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
        self._manual: str | None = None
        self._manual_image: ImageRecord | None = None
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
            hidden = self._override is not None or self._manual is not None
            play = self._play
            self._update(force_new=emotion_changed or reshuffle)
            if hidden:
                self._play = play  # don't restart a GIF that is covering this change
            return self._message()

    def refresh(self) -> dict[str, Any]:
        """Images or settings changed: re-pick only if the current image is no longer eligible."""
        with self._lock:
            if self._override and self._store.get(self._override.image.id) is None:
                self._override = None
                self._play += 1
            if self._manual and self._manual_image not in self._manual_pool():
                self._manual_image = self._choose(self._manual_pool(), (self._manual, "manual"))
                self._play += 1
            self._update(force_new=False)
            return self._message()

    @property
    def manual(self) -> str | None:
        return self._manual

    def set_manual(self, emotion: str | None) -> dict[str, Any]:
        """Hold `emotion` on screen until called with None (back to detected emotions)."""
        with self._lock:
            if emotion == self._manual:
                return self._message()
            self._manual = emotion
            self._manual_image = (
                self._choose(self._manual_pool(), (emotion, "manual")) if emotion else None
            )
            self._play += 1
            return self._message()

    def _manual_pool(self) -> list[ImageRecord]:
        assert self._manual is not None  # noqa: S101 - callers check
        # A deliberate choice, so prefer the strong images.
        return self._store.pool(self._manual, self._config.fallbacks, "strong")

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
        if self._override:
            shown = self._override.image
        elif self._manual:
            shown = self._manual_image
        else:
            shown = self._image
        return {
            "type": "state",
            "emotion": self._manual or self._state.emotion,
            "confidence": 1.0 if self._manual else round(self._state.confidence, 3),
            "tier": "strong" if self._manual else self.tier(self._state.confidence),
            "reason": "manual" if self._manual else self._state.reason,
            "manual": self._manual,
            "detected": self._state.emotion,
            "image": shown.url if shown else None,
            "play": self._play,
            "trigger": self._override.phrase if self._override else None,
            # Played by the overlay when this image starts showing (not for word triggers).
            "effect": None
            if self._override
            else self._config.effects.get(self._manual or self._state.emotion),
            "transition": self._config.transition,
            "fade_ms": self._config.fade_ms,
        }
