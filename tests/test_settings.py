import pytest

from emotion_overlay.config import AppConfig
from emotion_overlay.settings import (
    SettingsError,
    apply_settings,
    editable_settings,
    reset_settings,
)


def test_reset_restores_editable_defaults_only() -> None:
    config = AppConfig()
    state_section = config.state
    apply_settings(
        config, {"state": {"min_display_s": 5}, "display": {"fallbacks": {"fear": "anger"}}}
    )
    config.audio.device = 3  # not a live setting: must survive the reset
    config.classifier.model = "custom/model"
    reset_settings(config)
    assert editable_settings(config) == editable_settings(AppConfig())
    assert config.state is state_section
    assert (config.audio.device, config.classifier.model) == (3, "custom/model")


def test_updates_apply_in_place() -> None:
    config = AppConfig()
    state_section = config.state
    apply_settings(config, {"state": {"silence_timeout_s": 7}, "display": {"transition": "cut"}})
    assert config.state is state_section  # components holding the reference see the change
    assert state_section.silence_timeout_s == 7
    assert config.display.transition == "cut"


def test_invalid_value_changes_nothing() -> None:
    config = AppConfig()
    with pytest.raises(SettingsError, match="confidence_threshold"):
        apply_settings(config, {"display": {"fade_ms": 900}, "state": {"confidence_threshold": 3}})
    assert config.display.fade_ms == 300


@pytest.mark.parametrize(
    "patch",
    [
        {"server": {"host": "0.0.0.0"}},  # noqa: S104 - the point of the test
        {"debug": {"save_audio": True}},
        {"classifier": {"model": "evil/model"}},
        {"state": {"no_such_setting": 1}},
        {"state": 5},
        ["not", "a", "dict"],
    ],
)
def test_locked_or_malformed_settings_are_rejected(patch: object) -> None:
    config = AppConfig()
    with pytest.raises(SettingsError):
        apply_settings(config, patch)
    assert config == AppConfig()


def test_lenient_mode_ignores_locked_sections() -> None:
    config = AppConfig()
    apply_settings(
        config,
        {"debug": {"save_audio": True}, "classifier": {"model": "x", "window_seconds": 2}},
        strict=False,
    )
    assert config.debug.save_audio is False
    assert config.classifier.model == AppConfig().classifier.model
    assert config.classifier.window_seconds == 2


def test_editable_settings_exposes_only_editable_fields() -> None:
    settings = editable_settings(AppConfig())
    assert set(settings) == {"state", "display", "vad", "classifier", "audio_emotion"}
    assert settings["classifier"] == {"window_seconds": 6.0}
    assert set(settings["audio_emotion"]) == {"enabled", "weight"}  # model needs a restart
