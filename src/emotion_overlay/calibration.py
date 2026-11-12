"""Per-user calibration: read short lines in each emotion, learn score offsets.

Two small corrections are fitted, both as per-class biases in log space
(softmax(log p + b)), which is the simplest calibration that can fix a model that
systematically over- or under-predicts a class for one person's voice and mic:

1. voice bias, in the voice model's own labels: undoes a skew such as a mic that
   makes everything sound "angry" to the voice model.
2. final bias, on the fused scores: nudges each emotion up or down for this user.

Biases are L2-regularised toward 0 and clipped, and classes with no recordings are
left at 0, so a short session can't push the model anywhere extreme. Only score
vectors are kept: no audio or transcripts are stored.

The reported "after" accuracy is leave-one-out (each recording is scored by a fit
that never saw it), so it isn't inflated by testing on the training data.
"""

import json
import logging
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from emotion_overlay.classify.fusion import EPS, score_utterance
from emotion_overlay.emotions import EMOTIONS, Scores

log = logging.getLogger(__name__)

MAX_BIAS = 3.0

# Two lines per emotion, interleaved so the same emotion never comes twice in a row.
LINES: dict[str, tuple[str, str]] = {
    "joy": ("Yes! We finally won, this is amazing!", "I love this so much, best day ever!"),
    "anger": ("Are you kidding me? That is so unfair!", "I'm done, this game is ridiculous!"),
    "sadness": (
        "I really miss how things used to be.",
        "I tried my best and it still wasn't enough.",
    ),
    "surprise": ("Whoa, wait, what just happened?", "No way! I did not see that coming!"),
    "fear": (
        "Did you hear that? Something is in here.",
        "I don't want to go in there, I'm scared.",
    ),
    "disgust": ("Ew, that is absolutely disgusting.", "Ugh, gross, I can't even look at that."),
    "neutral": ("The next match starts in about five minutes.", "I'm going to check the settings."),
}
PROMPTS: tuple[tuple[str, str], ...] = tuple(
    (emotion, lines[i]) for i in range(2) for emotion, lines in LINES.items()
)


# While calibrating, a line only ends after this much quiet, so a mid-line pause
# doesn't split it (and the length of those pauses can be measured).
CALIBRATION_SILENCE_MS = 900


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class SpeechStats:
    """How a line was spoken, from the voice activity detector."""

    pauses_ms: tuple[int, ...] = ()
    speech_probs: tuple[float, ...] = ()
    noise_probs: tuple[float, ...] = ()


@dataclass(frozen=True)
class CalibrationSample:
    label: str
    text: Scores  # text model scores for this line alone
    voice: Scores  # raw voice model scores (its own labels)
    speech: SpeechStats | None = None


@dataclass(frozen=True)
class Calibration:
    voice_bias: dict[str, float]
    final_bias: dict[str, float]
    weight: float  # fusion weight the final bias was fitted with
    samples: int
    created: str
    # Share of recordings classified correctly. None when not measurable.
    accuracy: dict[str, float | None] = field(default_factory=dict)
    # Measured speaking profile (see personalize.py); numbers only.
    profile: dict[str, Any] = field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        return asdict(self)


# --- fitting --------------------------------------------------------------------


def fit_bias(
    rows: Sequence[Mapping[str, float]],
    labels: Sequence[str],
    classes: Sequence[str],
    l2: float = 0.1,
    steps: int = 400,
    lr: float = 0.5,
) -> dict[str, float]:
    """Per-class log-space offsets minimising cross-entropy + l2 * |b|^2."""
    index = {c: i for i, c in enumerate(classes)}
    pairs = [(r, index[y]) for r, y in zip(rows, labels, strict=True) if y in index and r]
    if not pairs:
        return {}
    x = np.log(np.array([[max(r.get(c, 0.0), EPS) for c in classes] for r, _ in pairs]))
    y = np.array([i for _, i in pairs])
    onehot = np.eye(len(classes))[y]
    active = onehot.any(axis=0)  # classes nobody recorded keep bias 0
    b = np.zeros(len(classes))
    for _ in range(steps):
        z = x + b
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        grad = (p - onehot).mean(axis=0) + 2 * l2 * b
        grad[~active] = 0.0
        b -= lr * grad
    b = np.clip(b, -MAX_BIAS, MAX_BIAS)
    return {c: round(float(v), 4) for c, v in zip(classes, b, strict=True)}


def _top(scores: Mapping[str, float]) -> str | None:
    return max(scores, key=lambda k: scores[k]) if scores else None


def _accuracy(predictions: Sequence[str | None], labels: Sequence[str]) -> float | None:
    pairs = [(p, y) for p, y in zip(predictions, labels, strict=True) if p is not None]
    if not pairs:
        return None
    return round(sum(p == y for p, y in pairs) / len(pairs), 3)


def _fit(
    samples: Sequence[CalibrationSample], label_map: Mapping[str, Sequence[str]], weight: float
) -> tuple[dict[str, float], dict[str, float]]:
    voice_label = {e: label for label, targets in label_map.items() for e in targets}
    voiced = [s for s in samples if s.voice and s.label in voice_label]
    voice_bias = fit_bias(
        [s.voice for s in voiced], [voice_label[s.label] for s in voiced], list(label_map)
    )
    fused = [score_utterance(s.text, s.voice, label_map, weight, voice_bias).fused for s in samples]
    final_bias = fit_bias(fused, [s.label for s in samples], EMOTIONS)
    return voice_bias, final_bias


def loo_scores(
    samples: Sequence[CalibrationSample], label_map: Mapping[str, Sequence[str]], weight: float
) -> list[Scores]:
    """Calibrated final scores for each line, from a fit that never saw that line."""
    scores = []
    for i, sample in enumerate(samples):
        voice_bias, final_bias = _fit(
            [s for j, s in enumerate(samples) if j != i], label_map, weight
        )
        scores.append(
            score_utterance(
                sample.text, sample.voice, label_map, weight, voice_bias, final_bias
            ).final
        )
    return scores


def accuracy_of(scores: Sequence[Scores], labels: Sequence[str]) -> float | None:
    return _accuracy([_top(s) for s in scores], labels)


def calibrate(
    samples: Sequence[CalibrationSample],
    label_map: Mapping[str, Sequence[str]],
    weight: float,
    baseline_weight: float | None = None,
    loo: Sequence[Scores] | None = None,
) -> Calibration:
    """Fit at `weight`. "before" is measured at `baseline_weight` (the user's current
    setting) without calibration; "after" is leave-one-out at `weight`."""
    if len(samples) < 4:
        raise CalibrationError("record at least 4 lines to calibrate")
    labels = [s.label for s in samples]
    voice_label = {e: label for label, targets in label_map.items() for e in targets}
    baseline = weight if baseline_weight is None else baseline_weight
    if loo is None:
        loo = loo_scores(samples, label_map, weight)
    voice_bias, final_bias = _fit(samples, label_map, weight)

    accuracy = {
        "text": _accuracy([_top(s.text) for s in samples], labels),
        # Voice is judged on its own coarse labels (e.g. fear counts as "ang").
        "voice": _accuracy(
            [_top(s.voice) for s in samples], [voice_label.get(y, "") for y in labels]
        ),
        "before": accuracy_of(
            [score_utterance(s.text, s.voice, label_map, baseline).final for s in samples], labels
        ),
        "after": accuracy_of(loo, labels),
    }
    return Calibration(
        voice_bias=voice_bias,
        final_bias=final_bias,
        weight=weight,
        samples=len(samples),
        created=datetime.now(UTC).isoformat(timespec="seconds"),
        accuracy=accuracy,
    )


# --- session --------------------------------------------------------------------


class CalibrationSession:
    """Walks through PROMPTS; each recorded utterance answers the current prompt."""

    def __init__(self, prompts: Sequence[tuple[str, str]] = PROMPTS) -> None:
        self.prompts = list(prompts)
        self.index = 0
        self._samples: dict[int, CalibrationSample] = {}
        self.last: dict[str, Any] | None = None

    @property
    def done(self) -> bool:
        return self.index >= len(self.prompts)

    @property
    def samples(self) -> list[CalibrationSample]:
        return [self._samples[i] for i in sorted(self._samples)]

    def record(self, text: Scores, voice: Scores, speech: SpeechStats | None = None) -> None:
        if self.done:
            return
        emotion, _ = self.prompts[self.index]
        self._samples[self.index] = CalibrationSample(emotion, dict(text), dict(voice), speech)
        heard = _top(text) if text else None
        self.last = {"wanted": emotion, "heard": heard, "voice": _top(voice)}
        self.index += 1

    def skip(self) -> None:
        if not self.done:
            self._samples.pop(self.index, None)
            self.index += 1

    def back(self) -> None:
        self.index = max(0, self.index - 1)
        self._samples.pop(self.index, None)
        self.last = None

    def to_api(self) -> dict[str, Any]:
        prompt = None
        if not self.done:
            emotion, line = self.prompts[self.index]
            prompt = {"emotion": emotion, "text": line}
        return {
            "index": self.index,
            "total": len(self.prompts),
            "recorded": len(self._samples),
            "prompt": prompt,
            "last": self.last,
        }


# --- storage --------------------------------------------------------------------


def _clean_bias(raw: Any, allowed: Sequence[str]) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        if key in allowed and isinstance(value, int | float) and math.isfinite(value):
            clean[key] = max(-MAX_BIAS, min(MAX_BIAS, float(value)))
    return clean


PROFILE_NUMBERS = ("pause_p75_ms", "pauses_measured", "speech_level", "background_level")


def _clean_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    def number(v: Any) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)

    clean: dict[str, Any] = {k: raw[k] for k in PROFILE_NUMBERS if number(raw.get(k))}
    weights = raw.get("voice_weight_accuracy")
    if isinstance(weights, dict):
        clean["voice_weight_accuracy"] = {
            str(k): v for k, v in weights.items() if number(v) and len(str(k)) <= 8
        }
    return clean


class CalibrationStore:
    """data/calibration.json. Validated on load; per user, so not part of setup exports."""

    def __init__(self, root: Path) -> None:
        self._path = root / "calibration.json"
        self._lock = threading.Lock()
        self.stale = False  # last load found a calibration made for another voice model

    def load(self, voice_labels: Sequence[str]) -> Calibration | None:
        self.stale = False
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            old_voice = raw.get("voice_bias")
            if isinstance(old_voice, dict) and old_voice and not set(old_voice) & set(voice_labels):
                # Fitted to a different voice model's labels: its corrections don't apply.
                log.warning("Calibration was made for another voice model; ignoring it")
                self.stale = True
                return None
            return Calibration(
                voice_bias=_clean_bias(raw.get("voice_bias"), voice_labels),
                final_bias=_clean_bias(raw.get("final_bias"), EMOTIONS),
                weight=float(raw.get("weight", 0.0)),
                samples=int(raw.get("samples", 0)),
                created=str(raw.get("created", "")),
                accuracy={
                    k: v if isinstance(v, int | float) else None
                    for k, v in (raw.get("accuracy") or {}).items()
                    if k in ("text", "voice", "before", "after")
                },
                profile=_clean_profile(raw.get("profile")),
            )
        except (OSError, ValueError, TypeError, AttributeError):
            log.exception("Ignoring unreadable %s", self._path)
            return None

    def save(self, calibration: Calibration) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(calibration.to_api(), indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def delete(self) -> None:
        with self._lock:
            self._path.unlink(missing_ok=True)
