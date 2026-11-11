"""Combining text and voice scores, plus per-user calibration. Pure functions only.

Per utterance:
    voice (model labels) --audio bias--> corrected voice --label_map--> voice (emotions)
    text (emotions) + voice (emotions) --weighted average--> fused --bias--> final
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from emotion_overlay.emotions import Scores

EPS = 1e-4


def apply_bias(scores: Mapping[str, float], bias: Mapping[str, float]) -> Scores:
    """Add per-class offsets in log space and renormalise (softmax(log p + b))."""
    if not scores or not bias:
        return dict(scores)
    logits = {k: math.log(max(p, EPS)) + bias.get(k, 0.0) for k, p in scores.items()}
    top = max(logits.values())
    exp = {k: math.exp(v - top) for k, v in logits.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


def map_voice_scores(
    voice: Mapping[str, float], label_map: Mapping[str, Sequence[str]], text: Mapping[str, float]
) -> Scores:
    """Spread each voice label's probability over the emotions it stands for.

    Within a group the split follows the text model's relative scores, so the voice
    says "agitated" and the words decide between anger, disgust and fear. With no
    usable text, the first emotion listed for the label gets all of it.
    """
    out: Scores = {}
    for label, p in voice.items():
        targets = label_map.get(label)
        if not targets:
            continue
        weights = [max(text.get(t, 0.0), 0.0) for t in targets]
        total = sum(weights)
        if total < 1e-9:
            weights, total = [1.0] + [0.0] * (len(targets) - 1), 1.0
        for target, w in zip(targets, weights, strict=True):
            out[target] = out.get(target, 0.0) + p * w / total
    mass = sum(out.values())
    return {k: v / mass for k, v in out.items()} if mass > 0 else {}


def fuse(text: Mapping[str, float], voice: Mapping[str, float], weight: float) -> Scores:
    """Weighted average; if either side is missing, use the other one alone."""
    if not voice:
        return dict(text)
    if not text:
        return dict(voice)
    keys = text.keys() | voice.keys()
    return {k: (1 - weight) * text.get(k, 0.0) + weight * voice.get(k, 0.0) for k in keys}


@dataclass(frozen=True)
class ScoreBreakdown:
    text: Scores  # text model, canonical emotions
    voice_raw: Scores  # voice model, its own labels (after calibration correction)
    voice: Scores  # voice mapped onto canonical emotions
    fused: Scores  # weighted combination, before final calibration bias
    final: Scores  # what the state machine sees


def score_utterance(
    text: Mapping[str, float],
    voice_raw: Mapping[str, float],
    label_map: Mapping[str, Sequence[str]],
    weight: float,
    voice_bias: Mapping[str, float] | None = None,
    final_bias: Mapping[str, float] | None = None,
) -> ScoreBreakdown:
    corrected = apply_bias(voice_raw, voice_bias or {})
    voice = map_voice_scores(corrected, label_map, text)
    fused = fuse(text, voice, weight)
    return ScoreBreakdown(
        text=dict(text),
        voice_raw=corrected,
        voice=voice,
        fused=fused,
        final=apply_bias(fused, final_bias or {}),
    )
