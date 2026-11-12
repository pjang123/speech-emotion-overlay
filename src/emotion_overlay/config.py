"""Typed configuration loaded from YAML. See config.example.yaml for documentation."""

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from emotion_overlay.emotions import EMOTIONS

log = logging.getLogger(__name__)

Device = Literal["cpu", "cuda", "auto"]


class AudioConfig(BaseModel):
    device: int | None = None  # PortAudio index; only used when no name is saved
    # The mic is remembered by name (and audio API), because indexes shift when
    # devices are plugged in or removed.
    device_name: str | None = None
    device_hostapi: str | None = None


class VadConfig(BaseModel):
    threshold: float = Field(0.5, ge=0.05, le=0.95)
    min_silence_ms: int = Field(400, ge=50, le=5000)
    speech_pad_ms: int = Field(150, ge=0, le=1000)
    min_utterance_ms: int = Field(250, ge=0)
    max_utterance_s: float = Field(6.0, ge=1.0, le=30.0)


class AsrConfig(BaseModel):
    backend: Literal["auto", "faster-whisper", "transformers"] = "auto"
    model: str = "base.en"
    device: Device = "auto"  # GPU if PyTorch can use one (see compute.py), else CPU
    compute_type: str = "int8"
    language: str | None = "en"
    beam_size: int = Field(1, ge=1, le=10)
    no_speech_threshold: float = Field(0.6, ge=0.0, le=1.0)


class ClassifierConfig(BaseModel):
    model: str = "j-hartmann/emotion-english-distilroberta-base"
    device: Device = "auto"  # GPU if PyTorch can use one (see compute.py), else CPU
    window_seconds: float = Field(6.0, ge=0.0, le=60.0)
    slang: bool = True  # translate slang ("lol" -> "haha") before the text model; see slang.py
    # While someone is still talking, re-check the words every this many seconds of
    # speech, so the image can change before the sentence ends. 0 = only after it ends.
    interim_s: float = Field(1.0, ge=0.0, le=10.0)


# Audio model label -> the emotions it stands for. If a label lists several, its
# probability is shared among them in proportion to the text model's scores (the
# voice says "agitated", the words say which kind). Labels left out are ignored.
# The default model (Vox-Profile, trained on podcast speech) knows all seven emotions
# plus "contempt" and "other", which have no image group here.
DEFAULT_VOICE_MODEL = "tiantiaf/wavlm-large-categorical-emotion"
DEFAULT_AUDIO_LABEL_MAP: dict[str, list[str]] = {
    "anger": ["anger"],
    "disgust": ["disgust"],
    "fear": ["fear"],
    "happiness": ["joy"],
    "neutral": ["neutral"],
    "sadness": ["sadness"],
    "surprise": ["surprise"],
}

# The previous default (4 coarse moods). Config files that still have it saved
# unchanged are moved to the new default when loaded; see load_config.
OLD_VOICE_MODEL = "superb/hubert-base-superb-er"
OLD_AUDIO_LABEL_MAP: dict[str, list[str]] = {
    "neu": ["neutral"],
    "hap": ["joy", "surprise"],
    "ang": ["anger", "disgust", "fear"],
    "sad": ["sadness"],
}


class AudioEmotionConfig(BaseModel):
    """Tone-of-voice emotion model, fused with the text model's scores."""

    enabled: bool = True
    model: str = DEFAULT_VOICE_MODEL
    device: Device = "auto"  # GPU if PyTorch can use one (see compute.py), else CPU
    weight: float = Field(0.25, ge=0.0, le=1.0)  # voice share of the fused score; 0 = words only
    label_map: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_AUDIO_LABEL_MAP.items()}
    )

    @field_validator("label_map")
    @classmethod
    def _valid_map(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        seen: set[str] = set()
        for label, targets in value.items():
            if not targets:
                raise ValueError(f"audio label '{label}' maps to no emotions")
            for emotion in targets:
                if emotion not in EMOTIONS:
                    raise ValueError(f"unknown emotion '{emotion}' in label_map")
                if emotion in seen:
                    # Calibration needs to know which voice label each emotion belongs to.
                    raise ValueError(f"emotion '{emotion}' is mapped from two audio labels")
                seen.add(emotion)
        return value


class StateConfig(BaseModel):
    confidence_threshold: float = Field(0.5, ge=0.0, le=1.0)
    low_confidence: Literal["hold", "neutral"] = "hold"
    silence_timeout_s: float = Field(3.0, ge=0.5, le=600.0)
    # Stability (anti-flicker). The defaults switch on every window, i.e. all off.
    smoothing: float = Field(1.0, gt=0.0, le=1.0)  # EMA weight of the newest window; 1 = off
    persist_windows: int = Field(1, ge=1, le=10)  # windows a new emotion must stay on top
    instant_switch_margin: float = Field(0.0, ge=0.0, le=1.0)  # lead that skips the wait; 0 = off
    min_display_s: float = Field(0.0, ge=0.0, le=60.0)


class DisplayConfig(BaseModel):
    transition: Literal["fade", "cut", "bounce"] = "fade"
    fade_ms: int = Field(300, ge=0, le=5000)
    rotation: Literal["random", "cycle"] = "random"
    new_image_each_utterance: bool = False
    strong_threshold: float = Field(0.85, ge=0.0, le=1.0)  # "strong" tier at or above this
    fallbacks: dict[str, str] = Field(default_factory=dict)

    @field_validator("fallbacks")
    @classmethod
    def _known_emotions(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = {e for pair in value.items() for e in pair if e not in EMOTIONS}
        if unknown:
            raise ValueError(f"unknown emotions in fallbacks: {sorted(unknown)}")
        return value


class UploadConfig(BaseModel):
    max_bytes: int = Field(10 * 1024 * 1024, ge=1024)
    max_pixels: int = Field(4096 * 4096, ge=1)
    max_import_bytes: int = Field(200 * 1024 * 1024, ge=1024)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)


class HotkeyConfig(BaseModel):
    """Global hotkeys (Windows): modifiers + 1..7 hold an emotion, modifiers + 0 releases."""

    enabled: bool = True
    # Three modifiers by default so the keys can't clash with typing (Ctrl+Alt is
    # AltGr on many keyboard layouts, where AltGr + a digit types a character).
    modifiers: str = "ctrl+alt+shift"

    @field_validator("modifiers")
    @classmethod
    def _valid_modifiers(cls, value: str) -> str:
        from emotion_overlay.hotkeys import parse_modifiers

        parse_modifiers(value)
        return value.lower().replace(" ", "")


class DebugConfig(BaseModel):
    save_audio: bool = False
    save_transcripts: bool = False


CONFIG_VERSION = 2  # 2: new default voice model


class AppConfig(BaseModel):
    config_version: int = CONFIG_VERSION
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    audio_emotion: AudioEmotionConfig = Field(default_factory=AudioEmotionConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    uploads: UploadConfig = Field(default_factory=UploadConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    hotkeys: HotkeyConfig = Field(default_factory=HotkeyConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    data_dir: Path = Path("data")


def load_config(path: Path) -> AppConfig:
    """Load config from YAML, falling back to defaults when the file does not exist."""
    if not path.exists():
        log.info("No %s found, using defaults", path)
        return AppConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict):
        _migrate(raw)
    return AppConfig.model_validate(raw)


def _migrate(raw: dict[str, Any]) -> None:
    """Upgrade an older config file in place (the new version is saved on the next save).

    Every save writes all settings, so an old default can't be told apart from a
    deliberate choice except by the file's version: only files from before the
    switch that still hold the old default voice model are moved to the new one.
    """
    if raw.get("config_version", 1) >= CONFIG_VERSION:
        return
    voice = raw.get("audio_emotion")
    if (
        isinstance(voice, dict)
        and voice.get("model", OLD_VOICE_MODEL) == OLD_VOICE_MODEL
        and voice.get("label_map", OLD_AUDIO_LABEL_MAP) == OLD_AUDIO_LABEL_MAP
    ):
        log.info("Switching the tone-of-voice model to the new default, %s", DEFAULT_VOICE_MODEL)
        voice.pop("model", None)
        voice.pop("label_map", None)
    raw["config_version"] = CONFIG_VERSION


def save_config(config: AppConfig, path: Path) -> None:
    """Write the full config back to YAML (comments from a hand-edited file are not kept)."""
    data = config.model_dump(mode="json")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
