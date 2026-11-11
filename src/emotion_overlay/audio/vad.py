"""Splits a 16 kHz audio stream into utterances using a per-frame speech probability."""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from emotion_overlay.config import VadConfig

SAMPLE_RATE = 16_000
FRAME = 512  # Silero VAD requires exactly 512 samples per call at 16 kHz
# Once speech has started, keep counting frames as speech until the probability
# drops this far below the threshold (same hysteresis as Silero's VADIterator).
RELEASE_MARGIN = 0.15

SpeechProb = Callable[[np.ndarray], float]


@dataclass(frozen=True)
class Utterance:
    audio: np.ndarray
    ended_at: float  # monotonic time the end (or forced cut) was detected
    forced: bool  # cut at max length while the speaker was still talking

    @property
    def duration_s(self) -> float:
        return len(self.audio) / SAMPLE_RATE


def _samples(ms: float) -> int:
    return int(ms * SAMPLE_RATE / 1000)


class UtteranceSegmenter:
    def __init__(
        self,
        speech_prob: SpeechProb,
        config: VadConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._speech_prob = speech_prob
        self._config = config
        self._clock = clock
        self.reset()

    @property
    def speaking(self) -> bool:
        return self._in_speech

    def reset(self) -> None:
        pad_frames = -(-_samples(self._config.speech_pad_ms) // FRAME)  # ceil
        self._pending = np.zeros(0, dtype=np.float32)
        self._preroll: deque[np.ndarray] = deque(maxlen=pad_frames)
        self._frames: list[np.ndarray] = []
        self._in_speech = False
        self._silence = 0  # trailing non-speech samples in the current utterance
        self._speech = 0  # speech samples in the current utterance

    def feed(self, samples: np.ndarray) -> list[Utterance]:
        """Accept any number of samples; return utterances completed by them."""
        self._pending = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        done = []
        n_frames = len(self._pending) // FRAME
        for i in range(n_frames):
            utterance = self._process_frame(self._pending[i * FRAME : (i + 1) * FRAME])
            if utterance is not None:
                done.append(utterance)
        self._pending = self._pending[n_frames * FRAME :]
        return done

    def _process_frame(self, frame: np.ndarray) -> Utterance | None:
        cfg = self._config
        prob = self._speech_prob(frame)

        if not self._in_speech:
            if prob >= cfg.threshold:
                self._in_speech = True
                self._frames = [*self._preroll, frame]
                self._preroll.clear()
                self._speech = FRAME
                self._silence = 0
            else:
                self._preroll.append(frame)
            return None

        self._frames.append(frame)
        if prob >= cfg.threshold - RELEASE_MARGIN:
            self._speech += FRAME
            self._silence = 0
        else:
            self._silence += FRAME

        if self._silence >= _samples(cfg.min_silence_ms):
            return self._finish(forced=False)
        if len(self._frames) * FRAME >= cfg.max_utterance_s * SAMPLE_RATE:
            return self._finish(forced=True)
        return None

    def _finish(self, forced: bool) -> Utterance | None:
        audio = np.concatenate(self._frames)
        speech = self._speech
        if not forced:
            # Keep a little trailing silence (speech_pad_ms), drop the rest.
            extra = self._silence - _samples(self._config.speech_pad_ms)
            if extra > 0:
                audio = audio[:-extra]
            self._in_speech = False
        self._frames = []
        self._speech = 0
        self._silence = 0
        if speech < _samples(self._config.min_utterance_ms):
            return None
        return Utterance(audio=audio, ended_at=self._clock(), forced=forced)


def load_silero_speech_prob() -> SpeechProb:
    """Load Silero VAD (bundled with the pip package, no download needed)."""
    import torch
    from silero_vad import load_silero_vad

    model = load_silero_vad()

    def speech_prob(frame: np.ndarray) -> float:
        with torch.inference_mode():
            return float(model(torch.from_numpy(frame), SAMPLE_RATE).item())

    return speech_prob
