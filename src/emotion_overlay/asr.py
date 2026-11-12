"""Speech-to-text with Whisper.

Two interchangeable backends:
- faster-whisper (CTranslate2): fastest on CPU, preferred.
- transformers (PyTorch): fallback. Windows Smart App Control blocks CTranslate2's
  unsigned native library, while PyTorch's loads fine, so on those machines this
  is the only option that works without turning off an OS security feature.
"""

import logging
import sys
from typing import Protocol

import numpy as np

from emotion_overlay.compute import torch_device
from emotion_overlay.config import AsrConfig
from emotion_overlay.model_cache import load_cached

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
WARMUP = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)

# Why faster-whisper can't use the GPU in this process, once known. CTranslate2 needs
# NVIDIA's CUDA 12 cuBLAS, which the CUDA 13 PyTorch build doesn't include. The first
# attempt without it fails fast, but a second attempt in the same process can hang
# for good (seen when switching CPU -> GPU), so never call into it twice.
_faster_whisper_gpu_problem: str | None = None
CUBLAS = "cublas64_12.dll" if sys.platform == "win32" else "libcublas.so.12"


def faster_whisper_gpu_problem() -> str | None:
    """Why faster-whisper can't run on the GPU, or None if it may. Checks for the
    CUDA libraries itself instead of letting CTranslate2 find out."""
    global _faster_whisper_gpu_problem
    if _faster_whisper_gpu_problem is None:
        import ctypes

        try:
            import ctranslate2  # noqa: F401 - on Windows this adds its DLL folders

            ctypes.CDLL(CUBLAS)
        except (ImportError, OSError) as exc:
            log.debug("CUDA library check failed: %s", exc)
            _faster_whisper_gpu_problem = f"NVIDIA's {CUBLAS} for CUDA 12 isn't installed"
    return _faster_whisper_gpu_problem


def _remember_gpu_problem(problem: str) -> None:
    global _faster_whisper_gpu_problem
    _faster_whisper_gpu_problem = problem


class Transcriber(Protocol):
    backend: str
    device: str

    def transcribe(self, audio: np.ndarray) -> str: ...


class FasterWhisperTranscriber:
    backend = "faster-whisper"

    def __init__(self, config: AsrConfig, device: str = "cpu") -> None:
        from faster_whisper import WhisperModel

        self._config = config
        self.model = config.model
        # int8 weights with float16 maths is CTranslate2's fast mode on NVIDIA GPUs.
        compute_type = config.compute_type
        if device == "cuda" and compute_type == "int8":
            compute_type = "int8_float16"
        log.info("Loading Whisper '%s' (faster-whisper, %s)...", config.model, device)
        self._model = load_cached(
            WhisperModel, config.model, device=device, compute_type=compute_type
        )
        # Warm-up: surfaces missing CUDA/cuDNN libraries now rather than mid-stream,
        # and makes the first real utterance as fast as the rest.
        self.transcribe(WARMUP)
        self.device = device

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

    def __init__(self, config: AsrConfig, device: str = "cpu") -> None:
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self._config = config
        self.model = config.model
        # faster-whisper short names ("base.en") map to the original OpenAI checkpoints.
        name = config.model if "/" in config.model else f"openai/whisper-{config.model}"
        self.device = device
        log.info("Loading Whisper '%s' (transformers, %s)...", name, self.device)
        self._processor = load_cached(WhisperProcessor.from_pretrained, name)
        model = load_cached(WhisperForConditionalGeneration.from_pretrained, name)
        self._model = model.to(self.device).eval()
        self._english_only = name.endswith(".en")
        # Whisper writes "you" or "Thank you." for noise. faster-whisper drops text whose
        # no-speech probability is high; do the same here. (generate()'s own
        # no_speech_threshold fails for short audio in current transformers.)
        tokenizer = self._processor.tokenizer
        ids = [tokenizer.convert_tokens_to_ids(t) for t in ("<|nospeech|>", "<|nocaptions|>")]
        known = [i for i in ids if i is not None and i != tokenizer.unk_token_id]
        self._no_speech_id = known[0] if known else None
        self.transcribe(WARMUP, skip_if_silent=False)  # warm up text generation too

    def transcribe(self, audio: np.ndarray, skip_if_silent: bool = True) -> str:
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
            encoded = self._model.get_encoder()(features)  # shared by both steps below
            silent = self.no_speech_prob(encoded) >= self._config.no_speech_threshold
            if silent and skip_if_silent:
                return ""
            ids = self._model.generate(encoder_outputs=encoded, **kwargs)
        return self._processor.batch_decode(ids, skip_special_tokens=True)[0].strip()

    def no_speech_prob(self, encoded: object) -> float:
        """Probability of the no-speech token right after start-of-transcript, as in
        OpenAI's Whisper and faster-whisper."""
        import torch

        if self._no_speech_id is None:
            return 0.0
        start = self._model.generation_config.decoder_start_token_id
        decoder_input = torch.tensor([[start]], device=self.device)
        logits = self._model(encoder_outputs=encoded, decoder_input_ids=decoder_input).logits
        return torch.softmax(logits[0, -1].float(), dim=-1)[self._no_speech_id].item()


def load_transcriber(config: AsrConfig) -> Transcriber:
    """Pick backend and device. Order: faster-whisper on the GPU, Whisper via PyTorch on
    the GPU, faster-whisper on the CPU, Whisper via PyTorch on the CPU; the GPU ones
    only if the GPU is wanted and usable."""
    device = torch_device(config.device, "speech-to-text")
    # A bigger, more accurate model when on the GPU, where it's still fast.
    config = config.model_copy(
        update={"model": config.gpu_model if device == "cuda" else config.model}
    )
    if config.backend in ("auto", "faster-whisper") and device == "cuda":
        problem = faster_whisper_gpu_problem()
        if problem is None:
            try:
                return FasterWhisperTranscriber(config, "cuda")
            except Exception as exc:  # e.g. missing cuDNN, or the DLL blocked
                problem = str(exc)
                _remember_gpu_problem(problem)
        log.warning("faster-whisper can't use the GPU (%s)", problem)
        if config.backend == "auto":
            log.info("Using Whisper through PyTorch on the GPU instead")
            return TransformersWhisperTranscriber(config, "cuda")
    if config.backend in ("auto", "faster-whisper"):
        try:
            return FasterWhisperTranscriber(config, "cpu")
        except (ImportError, OSError) as exc:
            if config.backend == "faster-whisper":
                raise
            log.warning(
                "faster-whisper unavailable (%s). This is usually Windows Smart App Control "
                "blocking CTranslate2. Using the transformers backend instead (somewhat slower).",
                exc,
            )
    return TransformersWhisperTranscriber(config, device)
