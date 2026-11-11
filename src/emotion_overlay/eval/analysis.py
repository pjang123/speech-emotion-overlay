"""Turns cached per-clip model outputs into the evaluation summary.

Each record (one clip) holds the raw model outputs, so every comparison here is
cheap to recompute without re-running the models:
    {"id", "label", "speaker", "meta": {...}, "transcript",
     "text_scores": {emotion: p}, "voice": {voice_label: p}, "timings": {...}}

Modes compared:
- text:  text model only (what Phase 1 shipped)
- voice: voice model only; can only ever say neutral/joy/anger/sadness
- fused: the live app's default combination at the configured weight
"""

import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from emotion_overlay.calibration import CalibrationSample, calibrate
from emotion_overlay.classify.fusion import apply_bias, map_voice_scores, score_utterance
from emotion_overlay.emotions import EMOTIONS, NEUTRAL
from emotion_overlay.eval.metrics import (
    accuracy,
    confusion_matrix,
    macro_f1,
    per_class,
    percentile,
)

Record = dict[str, Any]
LabelMap = Mapping[str, Sequence[str]]
MODES = ("text", "voice", "fused")
SWEEP = tuple(round(w * 0.1, 1) for w in range(11))
FOUR_CLASS = ("anger", "joy", "neutral", "sadness")  # what the voice model can express


def _top(scores: Mapping[str, float]) -> str:
    return max(scores, key=lambda k: scores[k]) if scores else NEUTRAL


def predict(
    record: Record,
    mode: str,
    label_map: LabelMap,
    weight: float,
    voice_bias: Mapping[str, float] | None = None,
    final_bias: Mapping[str, float] | None = None,
) -> str:
    text, voice = record["text_scores"], record["voice"]
    if mode == "text":
        return _top(text)  # no words heard -> neutral, as the live app would show
    if mode == "voice":
        return _top(map_voice_scores(apply_bias(voice, voice_bias or {}), label_map, {}))
    breakdown = score_utterance(text, voice, label_map, weight, voice_bias, final_bias)
    return _top(breakdown.final)


def evaluate(
    records: Sequence[Record], mode: str, label_map: LabelMap, weight: float
) -> dict[str, Any]:
    classes = [c for c in EMOTIONS if any(r["label"] == c for r in records)]
    y_true = [r["label"] for r in records]
    y_pred = [predict(r, mode, label_map, weight) for r in records]
    return {
        "n": len(records),
        "accuracy": accuracy(y_true, y_pred),
        "macro_f1": macro_f1(y_true, y_pred, classes),
        "per_class": per_class(y_true, y_pred, classes),
        "classes": classes,
        # Columns include every emotion the model could predict, not just true classes.
        "confusion": confusion_matrix(y_true, y_pred, list(EMOTIONS)),
    }


def weight_sweep(
    records: Sequence[Record], label_map: LabelMap, weights: Sequence[float] = SWEEP
) -> list[dict[str, float]]:
    rows = []
    for w in weights:
        result = evaluate(records, "fused", label_map, w)
        rows.append({"weight": w, "accuracy": result["accuracy"], "macro_f1": result["macro_f1"]})
    return rows


def speaker_cv_weight(
    records: Sequence[Record], label_map: LabelMap, weights: Sequence[float] = SWEEP
) -> dict[str, Any] | None:
    """Pick the best weight on half the speakers, test it on the other half (and swap).

    Choosing the weight on the test data would flatter it; this doesn't.
    """
    speakers = sorted({r["speaker"] for r in records})
    if len(speakers) < 2:
        return None
    halves = [set(speakers[0::2]), set(speakers[1::2])]
    folds = []
    for tune, test in (halves, halves[::-1]):
        tune_records = [r for r in records if r["speaker"] in tune]
        test_records = [r for r in records if r["speaker"] in test]
        best = max(weight_sweep(tune_records, label_map, weights), key=lambda row: row["macro_f1"])
        result = evaluate(test_records, "fused", label_map, best["weight"])
        folds.append(
            {
                "weight": best["weight"],
                "accuracy": result["accuracy"],
                "macro_f1": result["macro_f1"],
            }
        )
    return {
        "folds": folds,
        "accuracy": sum(f["accuracy"] for f in folds) / len(folds),
        "macro_f1": sum(f["macro_f1"] for f in folds) / len(folds),
    }


def calibration_simulation(
    records: Sequence[Record],
    label_map: LabelMap,
    weight: float,
    per_emotion: int = 2,
    seed: int = 0,
) -> dict[str, Any] | None:
    """Simulate the in-app calibration per speaker: fit on `per_emotion` clips of each
    emotion from that speaker (like reading the prompts), test on their other clips."""
    rng = random.Random(seed)
    by_speaker: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_speaker[r["speaker"]].append(r)

    voice_label = {e: label for label, targets in label_map.items() for e in targets}
    y_true: list[str] = []
    before: list[str] = []
    after: list[str] = []
    # The voice correction on its own, judged in the voice model's 4 coarse moods.
    voice_true: list[str] = []
    voice_before: list[str] = []
    voice_after: list[str] = []
    speakers = 0
    for clips in by_speaker.values():
        by_label: dict[str, list[Record]] = defaultdict(list)
        for r in clips:
            by_label[r["label"]].append(r)
        calib: list[Record] = []
        for group in by_label.values():
            calib += rng.sample(group, min(per_emotion, len(group) - 1)) if len(group) > 1 else []
        held_out = [r for r in clips if r not in calib]
        if len(calib) < 4 or not held_out:
            continue
        samples = [CalibrationSample(r["label"], r["text_scores"], r["voice"]) for r in calib]
        fitted = calibrate(samples, label_map, weight)
        speakers += 1
        for r in held_out:
            y_true.append(r["label"])
            before.append(predict(r, "fused", label_map, weight))
            after.append(
                predict(r, "fused", label_map, weight, fitted.voice_bias, fitted.final_bias)
            )
            if r["voice"] and r["label"] in voice_label:
                voice_true.append(voice_label[r["label"]])
                voice_before.append(_top(r["voice"]))
                voice_after.append(_top(apply_bias(r["voice"], fitted.voice_bias)))
    if not speakers:
        return None
    classes = [c for c in EMOTIONS if c in y_true]
    return {
        "speakers": speakers,
        "test_clips": len(y_true),
        "voice_before": accuracy(voice_true, voice_before) if voice_true else None,
        "voice_after": accuracy(voice_true, voice_after) if voice_true else None,
        "before": {
            "accuracy": accuracy(y_true, before),
            "macro_f1": macro_f1(y_true, before, classes),
        },
        "after": {
            "accuracy": accuracy(y_true, after),
            "macro_f1": macro_f1(y_true, after, classes),
        },
    }


def breakdown(
    records: Sequence[Record], key: str, label_map: LabelMap, weight: float
) -> dict[str, dict[str, float]]:
    """Accuracy per mode for each value of record["meta"][key]."""
    groups: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if key in r.get("meta", {}):
            groups[str(r["meta"][key])].append(r)
    return {
        group: {"n": len(rs), **{m: evaluate(rs, m, label_map, weight)["accuracy"] for m in MODES}}
        for group, rs in sorted(groups.items())
    }


def latency(records: Sequence[Record], min_silence_ms: int) -> dict[str, Any]:
    """Speech end -> new state = VAD end-of-speech wait + processing (ASR ∥ voice, then text)."""
    totals = [min_silence_ms + r["timings"]["processing_ms"] for r in records]

    def stats(values: Sequence[float]) -> dict[str, float]:
        return {
            "median": percentile(values, 50),
            "p90": percentile(values, 90),
            "p95": percentile(values, 95),
            "max": max(values) if values else 0.0,
        }

    components = {
        name: percentile(
            [r["timings"][name] for r in records if r["timings"].get(name) is not None], 50
        )
        for name in ("asr_ms", "voice_ms", "text_ms", "processing_ms")
    }
    return {
        "vad_wait_ms": min_silence_ms,
        "speech_end_to_state_ms": stats(totals),
        "medians": components,
    }


def analyse(
    records: Sequence[Record], label_map: LabelMap, weight: float, min_silence_ms: int
) -> dict[str, Any]:
    four = [r for r in records if r["label"] in FOUR_CLASS]
    meta_keys = sorted({k for r in records for k in r.get("meta", {})})
    return {
        "clips": len(records),
        "speakers": len({r["speaker"] for r in records}),
        "weight": weight,
        "modes": {m: evaluate(records, m, label_map, weight) for m in MODES},
        "four_class": {m: evaluate(four, m, label_map, weight) for m in MODES} if four else None,
        "sweep": weight_sweep(records, label_map),
        "speaker_cv": speaker_cv_weight(records, label_map),
        "calibration": calibration_simulation(records, label_map, weight),
        "breakdowns": {k: breakdown(records, k, label_map, weight) for k in meta_keys},
        "no_words": sum(1 for r in records if not r["text_scores"]),
        "latency": latency(records, min_silence_ms),
    }
