import pytest

from emotion_overlay.config import HotkeyConfig
from emotion_overlay.emotions import EMOTIONS
from emotion_overlay.hotkeys import KEYS, describe, key_emotion, parse_modifiers


def test_modifiers_parse_to_win32_flags() -> None:
    assert parse_modifiers("ctrl+alt+shift") == 0x2 | 0x1 | 0x4
    assert parse_modifiers(" Ctrl + Win ") == 0x2 | 0x8
    for bad in ("", "ctrl+hyper", "+"):
        with pytest.raises(ValueError):
            parse_modifiers(bad)


def test_config_rejects_bad_modifiers() -> None:
    assert HotkeyConfig(modifiers="Ctrl + Alt").modifiers == "ctrl+alt"
    with pytest.raises(ValueError):
        HotkeyConfig(modifiers="ctrl+meta")


def test_digits_map_to_emotions_in_order() -> None:
    assert [key_emotion(d) for d in KEYS] == [None, *EMOTIONS]
    assert describe("ctrl+alt+shift") == "Ctrl+Alt+Shift"
