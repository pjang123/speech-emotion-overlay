"""Vox-Profile categorical speech-emotion models (e.g. tiantiaf/wavlm-large-categorical-emotion).

These were trained on MSP-Podcast (natural podcast speech, 9 emotions) and published
as plain safetensors for the authors' own wrapper class, not as a transformers model.
The inference path is small, so it is rebuilt here instead of depending on their
package: a stock WavLM encoder, a learned softmax-weighted sum of its hidden layers,
three point-wise 1-D convolutions, mean pooling over time, and a two-layer classifier.
Paper: https://arxiv.org/abs/2505.14648. Original code:
https://github.com/tiantiaf0627/vox-profile-release (src/model/emotion/wavlm_emotion.py).
"""

import functools
from pathlib import Path
from typing import Any

import numpy as np

# Supported repos -> the transformers backbone whose config they were trained from.
MODELS = {"tiantiaf/wavlm-large-categorical-emotion": "microsoft/wavlm-large"}

# Output order of the classifier (from the model card).
LABELS = (
    "anger",
    "contempt",
    "disgust",
    "fear",
    "happiness",
    "neutral",
    "sadness",
    "surprise",
    "other",
)
HIDDEN_DIM = 256


def is_vox_profile(model: str) -> bool:
    return model in MODELS


def normalise(audio: np.ndarray) -> np.ndarray:
    """Zero mean, unit variance: what Wav2Vec2FeatureExtractor(do_normalize=True) does."""
    audio = audio.astype(np.float32)
    return (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)


def layer_states(backbone: Any, signal: Any) -> tuple[Any, ...]:
    """The encoder's input embeddings, each layer's output, and the final layer-normed
    output: the 25 states (for WavLM-large) the learned layer weights were trained on.

    transformers 4.x returned exactly this as `hidden_states`. 5.x records the raw
    output of the last layer instead of the layer-normed one, and that state carries
    most of the learned weight, so take the normalised one from `last_hidden_state`.
    """
    out = backbone(signal, output_hidden_states=True)
    return (*out.hidden_states[:-1], out.last_hidden_state)


def weights_path(model: str) -> Path:
    """The checkpoint file in the local Hugging Face cache (downloaded if missing).
    Its folder is named after the exact revision, so it identifies the weights."""
    from huggingface_hub import hf_hub_download

    from emotion_overlay.model_cache import load_cached

    return Path(load_cached(hf_hub_download, model, filename="model.safetensors"))


def cached_weights_path(model: str) -> Path | None:
    """The checkpoint file if it's already in the local Hugging Face cache, else None.
    Never goes online."""
    from huggingface_hub import try_to_load_from_cache

    path = try_to_load_from_cache(model, "model.safetensors")
    return Path(path) if isinstance(path, str) else None


@functools.cache
def _module_type() -> Any:
    """The torch module class (defined lazily so importing this file doesn't load torch)."""
    import torch
    from torch import nn
    from transformers import WavLMConfig, WavLMModel

    class VoxProfileEmotion(nn.Module):
        def __init__(self, config: WavLMConfig) -> None:
            super().__init__()
            self.backbone_model = WavLMModel(config)
            size, layers = config.hidden_size, config.num_hidden_layers + 1  # + embeddings
            self.weights = nn.Parameter(torch.full((layers,), 1.0 / layers))
            self.model_seq = nn.Sequential(
                nn.Conv1d(size, HIDDEN_DIM, 1),
                nn.ReLU(),
                nn.Dropout(0.1),
                nn.Conv1d(HIDDEN_DIM, HIDDEN_DIM, 1),
                nn.ReLU(),
                nn.Dropout(0.1),
                nn.Conv1d(HIDDEN_DIM, HIDDEN_DIM, 1),
            )
            self.emotion_layer = nn.Sequential(
                nn.Linear(HIDDEN_DIM, HIDDEN_DIM), nn.ReLU(), nn.Linear(HIDDEN_DIM, len(LABELS))
            )

        def forward(self, signal: torch.Tensor) -> torch.Tensor:
            stacked = torch.stack(layer_states(self.backbone_model, signal))  # L x B x T x D
            weights = torch.softmax(self.weights, dim=-1).view(-1, 1, 1, 1)
            features = (weights * stacked).sum(dim=0)
            features = self.model_seq(features.transpose(1, 2)).transpose(1, 2)
            return self.emotion_layer(features.mean(dim=1))  # logits, B x len(LABELS)

    return VoxProfileEmotion


def assemble(config: Any, state: dict[str, Any], name: str = "model") -> Any:
    """The module for a WavLM `config`, holding the checkpoint tensors in `state`.

    It is created on the "meta" device (shapes only, no memory) and the checkpoint's
    tensors are then used as its weights directly. Creating it normally would first
    fill all 315M weights with random numbers (2.3 s for WavLM-large) only to
    overwrite every one of them, and hold a second copy in memory meanwhile.
    """
    import itertools

    import torch

    with torch.device("meta"):
        module = _module_type()(config)
    # The checkpoint also holds heads this app doesn't use (detailed classes, arousal,
    # valence, dominance); everything this module has must be present.
    missing, _unused = module.load_state_dict(state, strict=False, assign=True)
    tensors = itertools.chain(module.named_parameters(), module.named_buffers())
    missing = [*missing, *(n for n, t in tensors if t.is_meta and n not in missing)]
    if missing:
        raise ValueError(f"{name}: checkpoint is missing weights: {missing[:5]}")
    return module.eval()


def build(model: str) -> Any:
    """Load the model (cache first) and return it as a torch module in eval mode."""
    from safetensors.torch import load_file
    from transformers import WavLMConfig

    from emotion_overlay.model_cache import load_cached

    config = load_cached(WavLMConfig.from_pretrained, MODELS[model])
    return assemble(config, load_file(weights_path(model)), model)
