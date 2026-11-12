"""Emotion from tone of voice, using a Hugging Face audio-classification model.

Scores come back in the model's own labels (e.g. anger/happiness/contempt/...);
fusion.py maps them onto the canonical emotions explicitly via `audio_emotion.label_map`.
Most models load through transformers; Vox-Profile ones through vox_profile.py.
"""

import logging
from typing import Any

import numpy as np

from emotion_overlay.classify import vox_profile
from emotion_overlay.classify.vox_profile import is_vox_profile
from emotion_overlay.compute import torch_device
from emotion_overlay.config import AudioEmotionConfig
from emotion_overlay.model_cache import load_cached

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class AudioEmotionClassifier:
    def __init__(self, config: AudioEmotionConfig) -> None:
        import torch

        self.device = torch_device(config.device, "voice emotion model")
        self._device = torch.device(self.device)
        log.info("Loading voice emotion model '%s' on %s...", config.model, self._device)
        if is_vox_profile(config.model):
            self._model = vox_profile.build(config.model).to(self._device)
            self.labels = list(vox_profile.LABELS)
            self._logits = self._vox_profile_logits
        else:
            from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

            self._extractor = load_cached(AutoFeatureExtractor.from_pretrained, config.model)
            model = load_cached(AutoModelForAudioClassification.from_pretrained, config.model)
            self._model = model.to(self._device).eval()
            id2label = self._model.config.id2label
            self.labels = [str(id2label[i]).lower() for i in range(len(id2label))]
            self._logits = self._transformers_logits

        unmapped = [label for label in self.labels if label not in config.label_map]
        if unmapped:
            log.info("Voice model labels not used (not in label_map): %s", unmapped)
        self.scores(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))  # warm-up

    def _transformers_logits(self, audio: np.ndarray) -> Any:
        inputs = self._extractor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        return self._model(**inputs.to(self._device)).logits[0]

    def _vox_profile_logits(self, audio: np.ndarray) -> Any:
        import torch

        signal = torch.from_numpy(vox_profile.normalise(audio))[None].to(self._device)
        return self._model(signal)[0]

    def scores(self, audio: np.ndarray) -> dict[str, float]:
        import torch

        with torch.inference_mode():
            probs = torch.softmax(self._logits(audio), dim=-1).tolist()
        return dict(zip(self.labels, probs, strict=True))
