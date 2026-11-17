"""Emotion from tone of voice, using a Hugging Face audio-classification model.

Scores come back in the model's own labels (e.g. anger/happiness/contempt/...);
fusion.py maps them onto the canonical emotions explicitly via `audio_emotion.label_map`.
Most models load through transformers; Vox-Profile ones through vox_profile.py.

On a GPU the model runs in half precision (half the memory, same speed and scores).
On the CPU a Vox-Profile model runs as a compressed int8 copy (onnx_voice.py) when
`audio_emotion.cpu_int8` is on, falling back to the full model if that fails.
"""

import logging
from typing import Any

import numpy as np

from emotion_overlay.classify import onnx_voice, vox_profile
from emotion_overlay.classify.vox_profile import is_vox_profile
from emotion_overlay.compute import is_gpu, torch_device
from emotion_overlay.config import AudioEmotionConfig
from emotion_overlay.model_cache import load_cached

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


def uses_int8(config: AudioEmotionConfig, device: str) -> bool:
    return device == "cpu" and config.cpu_int8 and is_vox_profile(config.model)


def needs_conversion(config: AudioEmotionConfig) -> bool:
    """True if loading will first make the compressed CPU copy (takes a while, once).
    Only checks local files; a model that isn't downloaded yet reports False."""
    if not uses_int8(config, torch_device(config.device, "voice emotion model")):
        return False
    return onnx_voice.needs_converting(config.model)


class AudioEmotionClassifier:
    def __init__(self, config: AudioEmotionConfig) -> None:
        import torch

        self.device = torch_device(config.device, "voice emotion model")
        self._device = torch.device(self.device)
        # Half precision on the GPU, as Whisper does there.
        self._dtype = torch.float16 if is_gpu(self.device) else torch.float32
        self.runtime = "pytorch"
        log.info("Loading voice emotion model '%s' on %s...", config.model, self._device)
        if is_vox_profile(config.model):
            self.labels = list(vox_profile.LABELS)
            if not (uses_int8(config, self.device) and self._load_int8(config.model)):
                model = vox_profile.build(config.model)
                self._model = model.to(self._device, dtype=self._dtype)
                self._logits = self._vox_profile_logits
        else:
            from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

            self._extractor = load_cached(AutoFeatureExtractor.from_pretrained, config.model)
            model = load_cached(
                AutoModelForAudioClassification.from_pretrained, config.model, dtype=self._dtype
            )
            self._model = model.to(self._device).eval()
            id2label = self._model.config.id2label
            self.labels = [str(id2label[i]).lower() for i in range(len(id2label))]
            self._logits = self._transformers_logits

        unmapped = [label for label in self.labels if label not in config.label_map]
        if unmapped:
            log.info("Voice model labels not used (not in label_map): %s", unmapped)
        self.scores(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))  # warm-up

    def _load_int8(self, model: str) -> bool:
        try:
            onnx = onnx_voice.OnnxVoiceModel(onnx_voice.prepare(model))
        except Exception:
            log.warning(
                "The compressed voice model isn't available; using the full one", exc_info=True
            )
            return False
        self._model = onnx
        self._logits = onnx.logits
        self.runtime = "onnx-int8"
        log.info("Voice model: compressed int8 copy on the CPU (ONNX Runtime)")
        return True

    def _transformers_logits(self, audio: np.ndarray) -> Any:
        inputs = self._extractor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        return self._model(**inputs.to(self._device, dtype=self._dtype)).logits[0]

    def _vox_profile_logits(self, audio: np.ndarray) -> Any:
        import torch

        signal = torch.from_numpy(vox_profile.normalise(audio))[None]
        return self._model(signal.to(self._device, dtype=self._dtype))[0]

    def scores(self, audio: np.ndarray) -> dict[str, float]:
        import torch

        with torch.inference_mode():
            logits = torch.as_tensor(self._logits(audio)).float()
            probs = torch.softmax(logits, dim=-1).tolist()
        return dict(zip(self.labels, probs, strict=True))
