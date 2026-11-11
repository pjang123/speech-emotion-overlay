from emotion_overlay.classify.window import TranscriptWindow


def test_keeps_recent_text_only() -> None:
    window = TranscriptWindow(seconds=6)
    window.add("I hate this.", at=0.0)
    window.add("Oh well.", at=4.0)
    assert window.text(now=5.0) == "I hate this. Oh well."
    assert window.text(now=7.0) == "Oh well."
    assert window.text(now=20.0) == ""


def test_zero_seconds_means_latest_utterance() -> None:
    window = TranscriptWindow(seconds=0)
    window.add("first", at=0.0)
    window.add("second", at=100.0)
    assert window.text(now=500.0) == "second"
