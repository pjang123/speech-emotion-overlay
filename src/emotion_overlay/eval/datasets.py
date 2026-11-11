"""Labelled clip sources and their explicit label mappings onto our 7 emotions."""

import re
import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from emotion_overlay.audio.capture import TARGET_RATE, resample
from emotion_overlay.emotions import EMOTIONS


@dataclass(frozen=True)
class Clip:
    id: str
    path: Path
    label: str  # canonical emotion
    speaker: str
    meta: dict[str, str] = field(default_factory=dict)


# --- RAVDESS ----------------------------------------------------------------------
# Filenames: modality-channel-emotion-intensity-statement-repetition-actor.wav,
# e.g. 03-01-06-01-02-01-12.wav. Audio-only speech = modality 03, channel 01.
# https://zenodo.org/records/1188976 (CC BY-NC-SA 4.0)

RAVDESS_EMOTIONS = {
    "01": "neutral",
    "02": "neutral",  # "calm": no counterpart in our labels; closest is neutral
    "03": "joy",
    "04": "sadness",
    "05": "anger",
    "06": "fear",
    "07": "disgust",
    "08": "surprise",
}
RAVDESS_NAME = re.compile(r"^03-01-(0[1-8])-(0[12])-(0[12])-(0[12])-(\d{2})\.wav$")


def ravdess(root: Path) -> list[Clip]:
    clips = []
    for path in sorted(root.rglob("*.wav")):
        match = RAVDESS_NAME.match(path.name)
        if not match:
            continue
        emotion, intensity, statement, _rep, actor = match.groups()
        clips.append(
            Clip(
                id=path.stem,
                path=path,
                label=RAVDESS_EMOTIONS[emotion],
                speaker=f"actor{actor}",
                meta={
                    "intensity": "strong" if intensity == "02" else "normal",
                    "original_label": "calm" if emotion == "02" else RAVDESS_EMOTIONS[emotion],
                },
            )
        )
    return clips


# --- your own recordings ------------------------------------------------------------
# <emotion>__<kind>__<n>.wav, written by `python -m emotion_overlay.eval record`;
# kind is "words" (the line itself is emotional) or "tone" (neutral words, emotional voice).

MINE_NAME = re.compile(r"^([a-z]+)__(words|tone)__(\d+)\.wav$")


def mine(root: Path) -> list[Clip]:
    clips = []
    for path in sorted(root.glob("*.wav")):
        match = MINE_NAME.match(path.name)
        if not match or match.group(1) not in EMOTIONS:
            continue
        emotion, kind, _n = match.groups()
        clips.append(Clip(path.stem, path, emotion, "me", {"line": kind}))
    return clips


DATASETS = {"ravdess": ravdess, "mine": mine}


def load_wav(path: Path) -> np.ndarray:
    """Read a PCM WAV as 16 kHz mono float32 (same resampling as the live app)."""
    with wave.open(str(path), "rb") as w:
        width, channels, rate = w.getsampwidth(), w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: only 16-bit PCM WAV is supported")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return resample(audio, rate) if rate != TARGET_RATE else audio


def iter_audio(clips: list[Clip]) -> Iterator[tuple[Clip, np.ndarray]]:
    for clip in clips:
        yield clip, load_wav(clip.path)
