"""Turns a calibration session into settings tuned to one person's way of speaking.

From the same 14 lines used for score calibration, this measures:

- pauses inside a line     -> vad.min_silence_ms (the pause that ends a sentence)
- speech vs. background    -> vad.threshold (voice detection sensitivity)
- whether the voice helps  -> audio_emotion.weight, or turning tone of voice off
- confidence on hits       -> state.confidence_threshold
- expressiveness           -> display.strong_threshold (mild vs. strong images)

Every rule is conservative: it needs enough evidence, stays within safe bounds,
and only suggests a change when it's meaningfully different from the current
value. Accuracy-based choices use leave-one-out scores (each line judged by a fit
that never saw it), and the voice weight only changes if that wins at least one
more line than the current weight does. Nothing is applied automatically; the
setup page shows the reasons and the user applies them.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from emotion_overlay.calibration import (
    Calibration,
    CalibrationSample,
    accuracy_of,
    calibrate,
    loo_scores,
)
from emotion_overlay.config import AppConfig
from emotion_overlay.emotions import NEUTRAL, Scores

WEIGHTS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
THRESHOLDS = (0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25)
KEEP_SHARE = 0.85  # confidence threshold keeps this share of correct reactions
MIN_LINES_FOR_SPEECH = 6


@dataclass(frozen=True)
class Recommendation:
    path: str  # "section.key" in the live settings
    label: str
    current: Any
    value: Any
    reason: str

    def to_api(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Personalization:
    calibration: Calibration
    recommendations: list[Recommendation]

    def patch(self) -> dict[str, dict[str, Any]]:
        """The recommendations as a /api/settings patch."""
        patch: dict[str, dict[str, Any]] = {}
        for r in self.recommendations:
            section, key = r.path.split(".")
            patch.setdefault(section, {})[key] = r.value
        return patch


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _round_to(value: float, step: float) -> float:
    return round(round(value / step) * step, 4)


def personalize(samples: Sequence[CalibrationSample], config: AppConfig) -> Personalization:
    audio = config.audio_emotion
    current_weight = audio.weight if audio.enabled else 0.0
    labels = [s.label for s in samples]
    recommendations: list[Recommendation] = []
    profile: dict[str, Any] = {}

    weight = current_weight
    loo: list[Scores] | None = None
    if audio.enabled and any(s.voice for s in samples):
        weight, loo, recs, tested = _voice_weight(samples, labels, config, current_weight)
        recommendations += recs
        profile["voice_weight_accuracy"] = tested
    if loo is None:
        loo = loo_scores(samples, audio.label_map, weight)

    calibration = calibrate(samples, audio.label_map, weight, current_weight, loo)
    recommendations += _confidence(loo, labels, config)
    recommendations += _speech(samples, config, profile)
    calibration = Calibration(**{**calibration.to_api(), "profile": profile})
    return Personalization(calibration, recommendations)


def _voice_weight(
    samples: Sequence[CalibrationSample],
    labels: Sequence[str],
    config: AppConfig,
    current: float,
) -> tuple[float, list[Scores], list[Recommendation], dict[str, float]]:
    label_map = config.audio_emotion.label_map
    scored = {w: loo_scores(samples, label_map, w) for w in sorted({*WEIGHTS, current})}
    accuracy = {w: accuracy_of(s, labels) or 0.0 for w, s in scored.items()}
    tested = {f"{w:.1f}": round(a, 3) for w, a in accuracy.items()}
    best = max(accuracy.values())
    # Need at least one more line right than now; otherwise it's noise.
    if best - accuracy[current] < 1 / len(samples) - 1e-9:
        return current, scored[current], [], tested
    winners = [w for w, a in accuracy.items() if a >= best - 1e-9]
    chosen = min(winners, key=lambda w: (abs(w - current), w))
    evidence = (
        f"{_pct(accuracy[chosen])} of your lines were right at voice weight {chosen:.1f} vs "
        f"{_pct(accuracy[current])} at {current:.2f} (each line tested with a calibration "
        "that never saw it)."
    )
    if chosen == 0.0:
        rec = Recommendation(
            "audio_emotion.enabled",
            "Use tone of voice",
            True,
            False,
            f"For your voice the tone model hurt more than it helped: {evidence} "
            "Turning it off also makes reactions about 0.2 s faster.",
        )
        return 0.0, scored[0.0], [rec], tested  # calibrate for the recommended setup
    rec = Recommendation("audio_emotion.weight", "Voice weight", current, chosen, evidence)
    return chosen, scored[chosen], [rec], tested


def _confidence(
    loo: Sequence[Scores], labels: Sequence[str], config: AppConfig
) -> list[Recommendation]:
    hits = [
        max(s.values())
        for s, y in zip(loo, labels, strict=True)
        if s and y != NEUTRAL and max(s, key=lambda k: s[k]) == y
    ]
    if len(hits) < 3:
        return []
    recs = []
    current = config.state.confidence_threshold
    threshold = next(
        (t for t in THRESHOLDS if np.mean([h >= t for h in hits]) >= KEEP_SHARE), THRESHOLDS[-1]
    )
    if abs(threshold - current) >= 0.05 - 1e-9:
        kept = sum(h >= threshold for h in hits)
        recs.append(
            Recommendation(
                "state.confidence_threshold",
                "Minimum confidence",
                current,
                threshold,
                f"{kept} of your {len(hits)} correctly detected emotional lines scored at least "
                f"{threshold:.2f}, so this lets your real reactions through"
                + (" while ignoring weaker guesses." if threshold > current else "."),
            )
        )
    else:
        threshold = current

    strong_now = config.display.strong_threshold
    strong = float(
        np.clip(_round_to(float(np.median(hits)), 0.05), max(0.5, threshold + 0.1), 0.95)
    )
    if abs(strong - strong_now) >= 0.05 - 1e-9:
        recs.append(
            Recommendation(
                "display.strong_threshold",
                "Strong-tier threshold",
                strong_now,
                strong,
                f"Half of your correct reactions scored above {strong:.2f}, so your Mild and "
                "Strong images will show about equally often.",
            )
        )
    return recs


def _speech(
    samples: Sequence[CalibrationSample], config: AppConfig, profile: dict[str, Any]
) -> list[Recommendation]:
    stats = [s.speech for s in samples if s.speech is not None]
    if len(stats) < MIN_LINES_FOR_SPEECH:
        return []
    recs = []

    # End-of-sentence pause: just past your typical pause inside a line. Deliberately
    # the 75th percentile, capped at 700 ms: some measured pauses are breaks between
    # sentences, and an occasional split costs little (the text model reads the rolling
    # window, so both halves are judged together) while every extra ms delays reactions.
    pauses = [p for st in stats for p in st.pauses_ms]
    typical = float(np.percentile(pauses, 75)) if pauses else 0.0
    profile["pause_p75_ms"] = round(typical)
    profile["pauses_measured"] = len(pauses)
    current_ms = config.vad.min_silence_ms
    silence = int(np.clip(_round_to(typical + 100, 50), 300, 700))
    if abs(silence - current_ms) >= 50:
        if not pauses:
            why = "You didn't pause inside your lines at all, so a shorter pause can end one."
        elif typical + 100 > 700:
            why = (
                f"You take long pauses inside your lines (75% were {typical:.0f} ms or shorter). "
                "This is capped at 700 ms to keep reactions quick; a long pause may now and then "
                "split a sentence, which costs little because recent sentences are judged together."
            )
        else:
            why = (
                f"Most of the pauses inside your lines (75%) were {typical:.0f} ms or shorter. "
                f"Ending a sentence after {silence} ms of quiet avoids cutting you off mid-thought"
            )
            why += " and makes reactions come sooner." if silence < current_ms else "."
        recs.append(
            Recommendation(
                "vad.min_silence_ms", "Pause that ends a sentence (ms)", current_ms, silence, why
            )
        )

    # Voice detection threshold: only moved when something is actually close to it.
    speech = [p for st in stats for p in st.speech_probs]
    noise = [p for st in stats for p in st.noise_probs]
    if len(speech) >= 50 and len(noise) >= 30:
        speech_level = float(np.percentile(speech, 10))  # your quieter speech
        background = float(np.percentile(noise, 95))  # your louder background moments
        profile["speech_level"] = round(speech_level, 3)
        profile["background_level"] = round(background, 3)
        current = config.vad.threshold
        middle = _round_to((speech_level + background) / 2, 0.05)
        threshold, why = current, ""
        if speech_level > background and speech_level < current + 0.1:
            # Quiet speaker: parts of your speech barely clear the threshold.
            threshold = float(np.clip(middle, 0.3, current - 0.05))
            why = (
                f"Your quieter speech scored about {speech_level:.2f}, close to the current "
                f"threshold, while your background stayed under {background:.2f}. A lower "
                "threshold stops the start and end of your sentences being clipped."
            )
        elif speech_level > background and background > current - 0.2:
            # Noisy room: background sounds get close to counting as speech.
            threshold = float(np.clip(middle, current + 0.05, 0.7))
            why = (
                f"Background sound reached {background:.2f}, close to the current threshold, "
                f"while your speech scored at least {speech_level:.2f}. A higher threshold "
                "ignores more of that noise."
            )
        if abs(threshold - current) >= 0.05 - 1e-9:
            recs.append(
                Recommendation(
                    "vad.threshold", "Voice detection threshold", current, threshold, why
                )
            )
    return recs
