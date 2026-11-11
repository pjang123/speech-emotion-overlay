"""Speech-to-text with Whisper.

Two interchangeable backends:
- faster-whisper (CTranslate2): fastest on CPU, preferred.
- transformers (PyTorch): fallback. Windows Smart App Control blocks CTranslate2's
  unsigned native library, while PyTorch's loads fine, so on those machines this
  is the only option that works without turning off an OS security feature.
"""

import logging
from typing import Protocol

import numpy as np

from emotion_overlay.config import AsrConfig
from emotion_overlay.model_cache import load_cached

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
WARMUP = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)


class Transcriber(Protocol):
    backend: str
    device: str

    def transcribe(self, audio: np.ndarray) -> str: ...


def _devices(config: AsrConfig) -> list[str]:
    return ["cuda", "cpu"] if config.device == "auto" else [config.device]


class FasterWhisperTranscriber:
    backend = "faster-whisper"

    def __init__(self, config: AsrConfig) -> None:
        from faster_whisper import WhisperModel

        self._config = config
        candidates = _devices(config)
        for device in candidates:
            try:
                log.info("Loading Whisper '%s' (faster-whisper, %s)...", config.model, device)
                self._model = load_cached(
                    WhisperModel, config.model, device=device, compute_type=config.compute_type
                )
                # Warm-up: surfaces missing CUDA/cuDNN libraries now rather than mid-stream,
                # and makes the first real utterance as fast as the rest.
                self.transcribe(WARMUP)
                self.device = device
                return
            except Exception as exc:
                if device == candidates[-1]:
                    raise
                log.warning("Whisper on %s failed (%s); falling back to CPU", device, exc)

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self._model.transcribe(
            audio,
            language=self._config.language,
            beam_size=self._config.beam_size,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,  # we already segment with Silero
        )
        # Whisper invents text ("Thank you.") on noise; drop segments it doubts are speech.
        parts = [
            s.text.strip() for s in segments if s.no_speech_prob < self._config.no_speech_threshold
        ]
        return " ".join(p for p in parts if p)


class TransformersWhisperTranscriber:
    backend = "transformers"

    def __init__(self, config: AsrConfig) -> None:
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self._config = config
        # faster-whisper short names ("base.en") map to the original OpenAI checkpoints.
        name = config.model if "/" in config.model else f"openai/whisper-{config.model}"
        use_cuda = "cuda" in _devices(config) and torch.cuda.is_available()
        self.device = "cuda" if use_cuda else "cpu"
        log.info("Loading Whisper '%s' (transformers, %s)...", name, self.device)
        self._processor = load_cached(WhisperProcessor.from_pretrained, name)
        model = load_cached(WhisperForConditionalGeneration.from_pretrained, name)
        self._model = model.to(self.device).eval()
        self._english_only = name.endswith(".en")
        self.transcribe(WARMUP)

    def transcribe(self, audio: np.ndarray) -> str:
        import torch

        features = self._processor(
            audio, sampling_rate=SAMPLE_RATE, return_tensors="pt"
        ).input_features.to(self.device)
        kwargs: dict[str, object] = {"num_beams": self._config.beam_size}
        if not self._english_only:
            kwargs["task"] = "transcribe"
            if self._config.language:
                kwargs["language"] = self._config.language
        with torch.inference_mode():
            ids = self._model.generate(features, **kwargs)
        return self._processor.batch_decode(ids, skip_special_tokens=True)[0].strip()


def load_transcriber(config: AsrConfig) -> Transcriber:
    if config.backend in ("auto", "faster-whisper"):
        try:
            return FasterWhisperTranscriber(config)
        except (ImportError, OSError) as exc:
            if config.backend == "faster-whisper":
                raise
            log.warning(
                "faster-whisper unavailable (%s). This is usually Windows Smart App Control "
                "blocking CTranslate2. Using the transformers backend instead (somewhat slower).",
                exc,
            )
    return TransformersWhisperTranscriber(config)
