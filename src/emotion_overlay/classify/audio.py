"""Emotion from tone of voice, using a Hugging Face audio-classification model.

Scores come back in the model's own labels (e.g. neu/hap/ang/sad); fusion.py maps
them onto the canonical emotions explicitly via `audio_emotion.label_map`.
"""

import logging

import numpy as np

from emotion_overlay.config import AudioEmotionConfig
from emotion_overlay.model_cache import load_cached

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class AudioEmotionClassifier:
    def __init__(self, config: AudioEmotionConfig) -> None:
        import torch
        from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

        use_cuda = config.device == "cuda" or (
            config.device == "auto" and torch.cuda.is_available()
        )
        self._device = torch.device("cuda" if use_cuda else "cpu")
        log.info("Loading voice emotion model '%s' on %s...", config.model, self._device)
        self._extractor = load_cached(AutoFeatureExtractor.from_pretrained, config.model)
        model = load_cached(AutoModelForAudioClassification.from_pretrained, config.model)
        self._model = model.to(self._device).eval()

        id2label = self._model.config.id2label
        self.labels = [str(id2label[i]).lower() for i in range(len(id2label))]
        unmapped = [label for label in self.labels if label not in config.label_map]
        if unmapped:
            log.warning("Voice model labels with no entry in label_map are ignored: %s", unmapped)
        self.scores(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))  # warm-up

    def scores(self, audio: np.ndarray) -> dict[str, float]:
        import torch

        inputs = self._extractor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        with torch.inference_mode():
            logits = self._model(**inputs.to(self._device)).logits[0]
            probs = torch.softmax(logits, dim=-1).tolist()
        return dict(zip(self.labels, probs, strict=True))
