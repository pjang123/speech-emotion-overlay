import random
from pathlib import Path

import pytest

from conftest import make_image
from emotion_overlay.config import DisplayConfig
from emotion_overlay.images import ImageStore
from emotion_overlay.server.display import DisplayState
from emotion_overlay.state_machine import EmotionState


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path, max_bytes=200_000, max_pixels=10**6)


def joy(confidence: float = 0.7) -> EmotionState:
    return EmotionState("joy", confidence, "scores", 1.0)


def test_cycle_rotation_goes_through_images_in_order(store: ImageStore) -> None:
    images = [store.add(make_image(), "joy", f"{i}.png") for i in range(3)]
    display = DisplayState(store, DisplayConfig(rotation="cycle", new_image_each_utterance=True))
    shown = [display.apply(joy(), new_utterance=True)["image"] for _ in range(4)]
    assert shown == [images[i % 3].url for i in (0, 1, 2, 3)]


def test_random_rotation_avoids_repeating_current_image(store: ImageStore) -> None:
    store.add(make_image(), "joy", "a.png")
    store.add(make_image(), "joy", "b.png")
    display = DisplayState(store, DisplayConfig(new_image_each_utterance=True), random.Random(1))
    shown = [display.apply(joy(), new_utterance=True)["image"] for _ in range(6)]
    assert all(a != b for a, b in zip(shown, shown[1:], strict=False))


def test_same_emotion_keeps_image_unless_reshuffle_is_on(store: ImageStore) -> None:
    store.add(make_image(), "joy", "a.png")
    store.add(make_image(), "joy", "b.png")
    display = DisplayState(store, DisplayConfig())
    first = display.apply(joy(), new_utterance=True)["image"]
    assert all(display.apply(joy(), new_utterance=True)["image"] == first for _ in range(5))


def test_intensity_tier_switches_image(store: ImageStore) -> None:
    mild = store.add(make_image(), "joy", "mild.png", tier="mild")
    strong = store.add(make_image(), "joy", "strong.png", tier="strong")
    display = DisplayState(store, DisplayConfig(strong_threshold=0.85))
    assert display.apply(joy(0.6))["image"] == mild.url
    message = display.apply(joy(0.9))
    assert (message["image"], message["tier"]) == (strong.url, "strong")
    assert display.apply(joy(0.5))["image"] == mild.url


def test_refresh_picks_up_new_upload_for_current_emotion(store: ImageStore) -> None:
    default = store.add(make_image(), "neutral", "n.png")
    store.set_default(default.id)
    display = DisplayState(store, DisplayConfig())
    assert display.apply(joy())["image"] == default.url  # no joy images yet
    own = store.add(make_image(), "joy", "j.png")
    assert display.refresh()["image"] == own.url


def test_message_carries_transition_settings(store: ImageStore) -> None:
    config = DisplayConfig(transition="bounce", fade_ms=500)
    message = DisplayState(store, config).message()
    assert (message["transition"], message["fade_ms"], message["image"]) == ("bounce", 500, None)
