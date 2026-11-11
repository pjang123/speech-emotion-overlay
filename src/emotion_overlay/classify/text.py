"""Emotion scores from text using a Hugging Face sequence-classification model."""

import logging

from emotion_overlay.config import ClassifierConfig
from emotion_overlay.emotions import EMOTIONS, Scores
from emotion_overlay.model_cache import load_cached

log = logging.getLogger(__name__)


class TextEmotionClassifier:
    def __init__(self, config: ClassifierConfig) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        use_cuda = config.device == "cuda" or (
            config.device == "auto" and torch.cuda.is_available()
        )
        self._device = torch.device("cuda" if use_cuda else "cpu")
        log.info(
            "Loading emotion model '%s' on %s (downloads on first run)...",
            config.model,
            self._device,
        )
        self._tokenizer = load_cached(AutoTokenizer.from_pretrained, config.model)
        model = load_cached(AutoModelForSequenceClassification.from_pretrained, config.model)
        self._model = model.to(self._device).eval()

        id2label = self._model.config.id2label
        self._labels = [str(id2label[i]).lower() for i in range(len(id2label))]
        unknown = [label for label in self._labels if label not in EMOTIONS]
        if unknown:
            log.warning("Ignoring model labels with no matching emotion: %s", unknown)

    def scores(self, text: str) -> Scores:
        import torch

        if not text.strip():
            return {}
        with torch.inference_mode():
            encoded = self._tokenizer(
                text, return_tensors="pt", truncation=True, max_length=256
            ).to(self._device)
            probs = torch.softmax(self._model(**encoded).logits[0], dim=-1).tolist()
        return {label: p for label, p in zip(self._labels, probs, strict=True) if label in EMOTIONS}
