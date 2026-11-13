"""Maps the displayed emotion to a concrete image and builds the overlay's state message.

An image is (re)picked when the emotion changes, when the current image is no
longer eligible (deleted, intensity tier changed, new upload for this emotion),
or on every utterance if `new_image_each_utterance` is on.

A word trigger (or a timed hotkey image) temporarily overrides the emotion image,
and a manual hold (hotkey, setup page or API) keeps a chosen emotion or one chosen
image on screen until it's released. Detected emotion changes keep being tracked
underneath, so the right image is back as soon as they end. Order on screen:
trigger, then held image or emotion (only one at a time), then detected.

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
    source: str  # "word" (a word trigger) or "hotkey" (a timed hotkey image)


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
        self._held: ImageRecord | None = None  # one specific image held by hand
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
            hidden = any(x is not None for x in (self._override, self._manual, self._held))
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
            if self._held:
                self._held = self._store.get(self._held.id)  # renamed, or gone
                if self._held is None:
                    self._play += 1
            if self._manual and self._manual_image not in self._manual_pool():
                self._manual_image = self._choose(self._manual_pool(), (self._manual, "manual"))
                self._play += 1
            self._update(force_new=False)
            return self._message()

    @property
    def manual(self) -> str | None:
        return self._manual

    @property
    def held_image(self) -> str | None:
        """ID of the image held by hand, if any."""
        return self._held.id if self._held else None

    def set_manual(self, emotion: str | None) -> dict[str, Any]:
        """Hold `emotion` on screen until called with None (back to detected emotions).
        Replaces a held image; None releases that too."""
        with self._lock:
            if emotion == self._manual and self._held is None:
                return self._message()
            self._held = None
            self._manual = emotion
            self._manual_image = (
                self._choose(self._manual_pool(), (emotion, "manual")) if emotion else None
            )
            self._play += 1
            return self._message()

    def hold_image(self, image: ImageRecord) -> dict[str, Any]:
        """Keep showing `image` until set_manual(None) or another hold replaces it."""
        with self._lock:
            if self._held != image:
                self._held = image
                self._manual = self._manual_image = None
                self._play += 1
            return self._message()

    def _manual_pool(self) -> list[ImageRecord]:
        assert self._manual is not None  # noqa: S101 - callers check
        # A deliberate choice, so prefer the strong images.
        return self._store.pool(self._manual, self._config.fallbacks, "strong")

    def trigger(
        self, image: ImageRecord, phrase: str, source: str = "word"
    ) -> tuple[dict[str, Any], int]:
        """Show `image` until end_trigger(token) is called. Returns (message, token)."""
        with self._lock:
            self._tokens += 1
            self._override = _Override(image, phrase, self._tokens, source)
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
        elif self._held:
            shown = self._held
        elif self._manual:
            shown = self._manual_image
        else:
            shown = self._image
        return {
            "type": "state",
            "emotion": self._manual or self._state.emotion,
            "confidence": 1.0 if self._manual else round(self._state.confidence, 3),
            "tier": "strong" if self._manual else self.tier(self._state.confidence),
            "reason": "manual" if self._manual else "held" if self._held else self._state.reason,
            "manual": self._manual,
            "held_image": self._held.original_name if self._held else None,
            "detected": self._state.emotion,
            "image": shown.url if shown else None,
            "play": self._play,
            "trigger": self._override.phrase if self._override else None,
            "trigger_source": self._override.source if self._override else None,
            # Played by the overlay when this image starts showing (not for word
            # triggers or images chosen by hand, which belong to no emotion).
            "effect": None
            if self._override or self._held
            else self._config.effects.get(self._manual or self._state.emotion),
            "transition": self._config.transition,
            "fade_ms": self._config.fade_ms,
        }
