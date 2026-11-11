"""Opt-in persistence of utterance audio and transcripts, for debugging only.

Disabled by default: with both debug flags off nothing is ever written to disk.
"""

import logging
import time
import wave
from pathlib import Path

import numpy as np

from emotion_overlay.config import DebugConfig

log = logging.getLogger(__name__)


class DebugRecorder:
    def __init__(self, config: DebugConfig, root: Path) -> None:
        self._config = config
        self._dir = root / "debug"
        if config.save_audio or config.save_transcripts:
            self._dir.mkdir(parents=True, exist_ok=True)
            log.warning("Debug recording is ON: writing to %s", self._dir.resolve())

    def record(self, audio: np.ndarray, sample_rate: int, transcript: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        if self._config.save_audio:
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            with wave.open(str(self._dir / f"{stamp}.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm.tobytes())
        if self._config.save_transcripts:
            with (self._dir / "transcripts.log").open("a", encoding="utf-8") as f:
                f.write(f"{stamp}\t{transcript}\n")
