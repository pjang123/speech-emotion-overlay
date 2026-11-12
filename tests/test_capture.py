"""Picking the saved microphone after the device list has changed."""

from emotion_overlay.audio.capture import InputDevice, pick_device

MIC = InputDevice(7, "USB Mic", "Windows WASAPI", 48000.0, False)
MIC_MME = InputDevice(2, "USB Mic", "MME", 44100.0, False)
OTHER = InputDevice(1, "Headset", "MME", 48000.0, True)


def test_saved_index_is_used_when_no_name_was_saved() -> None:
    assert pick_device([OTHER, MIC], 4, None, None) == 4
    assert pick_device([OTHER, MIC], None, None, None) is None


def test_name_wins_over_a_moved_index() -> None:
    assert pick_device([OTHER, MIC], 1, "USB Mic", "Windows WASAPI") == 7


def test_same_name_prefers_the_saved_audio_api() -> None:
    assert pick_device([MIC_MME, MIC], None, "USB Mic", "Windows WASAPI") == 7
    assert pick_device([MIC_MME, MIC], None, "USB Mic", "MME") == 2
    assert pick_device([MIC_MME], None, "USB Mic", "Windows WASAPI") == 2  # API gone: same mic


def test_unplugged_mic_falls_back_to_default_not_its_old_index() -> None:
    assert pick_device([OTHER], 7, "USB Mic", "Windows WASAPI") is None
