"""Typed configuration loaded from YAML. See config.example.yaml for documentation."""

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from emotion_overlay.emotions import EMOTIONS

log = logging.getLogger(__name__)

Device = Literal["cpu", "cuda", "auto"]


class AudioConfig(BaseModel):
    device: int | None = None


class VadConfig(BaseModel):
    threshold: float = Field(0.5, ge=0.05, le=0.95)
    min_silence_ms: int = Field(400, ge=50, le=5000)
    speech_pad_ms: int = Field(150, ge=0, le=1000)
    min_utterance_ms: int = Field(250, ge=0)
    max_utterance_s: float = Field(6.0, ge=1.0, le=30.0)


class AsrConfig(BaseModel):
    backend: Literal["auto", "faster-whisper", "transformers"] = "auto"
    model: str = "base.en"
    device: Device = "cpu"
    compute_type: str = "int8"
    language: str | None = "en"
    beam_size: int = Field(1, ge=1, le=10)
    no_speech_threshold: float = Field(0.6, ge=0.0, le=1.0)


class ClassifierConfig(BaseModel):
    model: str = "j-hartmann/emotion-english-distilroberta-base"
    device: Device = "cpu"
    window_seconds: float = Field(6.0, ge=0.0, le=60.0)
    slang: bool = True  # translate slang ("lol" -> "haha") before the text model; see slang.py


# Audio model label -> the emotions it stands for. The voice model only knows four
# coarse states; its probability is shared among the listed emotions in proportion
# to the text model's scores (the voice says "agitated", the words say which kind).
DEFAULT_AUDIO_LABEL_MAP: dict[str, list[str]] = {
    "neu": ["neutral"],
    "hap": ["joy", "surprise"],
    "ang": ["anger", "disgust", "fear"],
    "sad": ["sadness"],
}


class AudioEmotionConfig(BaseModel):
    """Tone-of-voice emotion model, fused with the text model's scores."""

    enabled: bool = True
    model: str = "superb/hubert-base-superb-er"
    device: Device = "cpu"
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


class DebugConfig(BaseModel):
    save_audio: bool = False
    save_transcripts: bool = False


class AppConfig(BaseModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    audio_emotion: AudioEmotionConfig = Field(default_factory=AudioEmotionConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    uploads: UploadConfig = Field(default_factory=UploadConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    data_dir: Path = Path("data")


def load_config(path: Path) -> AppConfig:
    """Load config from YAML, falling back to defaults when the file does not exist."""
    if not path.exists():
        log.info("No %s found, using defaults", path)
        return AppConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def save_config(config: AppConfig, path: Path) -> None:
    """Write the full config back to YAML (comments from a hand-edited file are not kept)."""
    data = config.model_dump(mode="json")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
