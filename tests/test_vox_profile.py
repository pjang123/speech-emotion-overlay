"""The Vox-Profile voice model must see the same layer states it was trained on."""

import numpy as np
import torch
from transformers import WavLMConfig, WavLMModel

from emotion_overlay.classify.vox_profile import layer_states, normalise


def tiny_wavlm() -> WavLMModel:
    config = WavLMConfig(
        hidden_size=32,
        num_hidden_layers=3,
        num_attention_heads=2,
        intermediate_size=64,
        conv_dim=(16, 16),
        conv_kernel=(10, 3),
        conv_stride=(5, 2),
        num_conv_pos_embeddings=16,
        num_conv_pos_embedding_groups=2,
        do_stable_layer_norm=True,
        feat_extract_norm="layer",
    )
    torch.manual_seed(0)
    return WavLMModel(config).eval()


def test_last_state_is_the_layer_normed_output() -> None:
    # transformers 5 returns the last layer's raw output (no final layer norm) as the
    # last hidden state; the model's learned layer weights expect the normed one.
    model = tiny_wavlm()
    signal = torch.randn(1, 4000)
    captured = []
    model.encoder.layers[-1].register_forward_hook(lambda m, i, o: captured.append(o[0]))
    with torch.no_grad():
        states = layer_states(model, signal)
        out = model(signal, output_hidden_states=True)
        expected_last = model.encoder.layer_norm(captured[0])  # last layer's raw output, normed
    assert len(states) == model.config.num_hidden_layers + 1  # embeddings + each layer
    assert torch.allclose(states[-1], expected_last, atol=1e-5)
    assert all(torch.equal(a, b) for a, b in zip(states[:-1], out.hidden_states[:-1], strict=True))


def test_normalise_matches_the_feature_extractor() -> None:
    audio = np.random.default_rng(0).normal(0.3, 0.1, 16000).astype(np.float32)
    out = normalise(audio)
    assert abs(out.mean()) < 1e-5 and abs(out.std() - 1) < 1e-3
