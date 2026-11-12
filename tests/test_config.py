"""Upgrading config files written by older versions."""

from pathlib import Path

import yaml

from emotion_overlay.config import (
    DEFAULT_AUDIO_LABEL_MAP,
    DEFAULT_VOICE_MODEL,
    OLD_AUDIO_LABEL_MAP,
    OLD_VOICE_MODEL,
    load_config,
    save_config,
)


def write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_old_default_voice_model_is_upgraded(tmp_path: Path) -> None:
    old = {
        "audio_emotion": {"model": OLD_VOICE_MODEL, "label_map": OLD_AUDIO_LABEL_MAP, "weight": 0.4}
    }
    config = load_config(write(tmp_path / "c.yaml", old))
    assert config.audio_emotion.model == DEFAULT_VOICE_MODEL
    assert config.audio_emotion.label_map == DEFAULT_AUDIO_LABEL_MAP
    assert config.audio_emotion.weight == 0.4  # the user's own settings are kept
    save_config(config, tmp_path / "c.yaml")
    assert yaml.safe_load((tmp_path / "c.yaml").read_text())["config_version"] == 2


def test_chosen_voice_model_is_kept(tmp_path: Path) -> None:
    # Saved by this version: the old model is now a deliberate choice.
    current = {
        "config_version": 2,
        "audio_emotion": {"model": OLD_VOICE_MODEL, "label_map": OLD_AUDIO_LABEL_MAP},
    }
    assert load_config(write(tmp_path / "a.yaml", current)).audio_emotion.model == OLD_VOICE_MODEL
    # An old file with an edited label map was customised: leave it alone.
    custom = {"neu": ["neutral"], "hap": ["joy"], "ang": ["anger"], "sad": ["sadness"]}
    edited = {"audio_emotion": {"model": OLD_VOICE_MODEL, "label_map": custom}}
    config = load_config(write(tmp_path / "b.yaml", edited))
    assert (config.audio_emotion.model, config.audio_emotion.label_map) == (OLD_VOICE_MODEL, custom)
    # Another model entirely is never touched.
    other = {"audio_emotion": {"model": "someone/else", "label_map": {"x": ["joy"]}}}
    assert load_config(write(tmp_path / "c.yaml", other)).audio_emotion.model == "someone/else"
