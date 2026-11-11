"""Classification metrics. Plain Python so the numbers are easy to audit."""

from collections.abc import Sequence
from typing import Any


def confusion_matrix(
    y_true: Sequence[str], y_pred: Sequence[str], classes: Sequence[str]
) -> list[list[int]]:
    """Rows are true labels, columns predicted labels, both in `classes` order."""
    index = {c: i for i, c in enumerate(classes)}
    matrix = [[0] * len(classes) for _ in classes]
    for t, p in zip(y_true, y_pred, strict=True):
        if t in index and p in index:
            matrix[index[t]][index[p]] += 1
    return matrix


def per_class(
    y_true: Sequence[str], y_pred: Sequence[str], classes: Sequence[str]
) -> dict[str, dict[str, Any]]:
    result = {}
    for c in classes:
        tp = sum(t == c and p == c for t, p in zip(y_true, y_pred, strict=True))
        predicted = sum(p == c for p in y_pred)
        support = sum(t == c for t in y_true)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        result[c] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
    return result


def accuracy(y_true: Sequence[str], y_pred: Sequence[str]) -> float:
    if not y_true:
        return 0.0
    return sum(t == p for t, p in zip(y_true, y_pred, strict=True)) / len(y_true)


def macro_f1(y_true: Sequence[str], y_pred: Sequence[str], classes: Sequence[str]) -> float:
    """Mean F1 over the classes that actually occur in y_true."""
    stats = per_class(y_true, y_pred, classes)
    present = [c for c in classes if stats[c]["support"]]
    return sum(stats[c]["f1"] for c in present) / len(present) if present else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
