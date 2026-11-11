"""Renders the evaluation summary as Markdown."""

from typing import Any

from emotion_overlay.emotions import EMOTIONS

MODE_NAMES = {"text": "Words only", "voice": "Voice only", "fused": "Combined (app default)"}


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _confusion(result: dict[str, Any]) -> list[str]:
    classes = result["classes"]
    predicted = [
        e
        for j, e in enumerate(EMOTIONS)
        if any(result["confusion"][i][j] for i in range(len(EMOTIONS)))
    ] or list(EMOTIONS)
    rows = []
    for i, true in enumerate(EMOTIONS):
        if true not in classes:
            continue
        cells = [str(result["confusion"][i][EMOTIONS.index(p)]) for p in predicted]
        rows.append([f"**{true}**", *cells])
    return _table(["true ↓ / predicted →", *predicted], rows)


def render(name: str, summary: dict[str, Any], meta: dict[str, str]) -> str:
    modes = summary["modes"]
    w = summary["weight"]
    out = [
        f"# Evaluation: {name}",
        "",
        f"{summary['clips']} clips from {summary['speakers']} speaker(s). "
        f"Combined = words and voice fused at the app's default voice weight **{w}**.",
        "",
        *(f"- {k}: {v}" for k, v in meta.items()),
        "",
        "## Summary",
        "",
        *_table(
            ["Mode", "Accuracy", "Macro-F1"],
            [[MODE_NAMES[m], pct(r["accuracy"]), pct(r["macro_f1"])] for m, r in modes.items()],
        ),
        "",
        f"Chance level is about {pct(1 / len(modes['text']['classes']))} accuracy "
        f"({len(modes['text']['classes'])} classes). "
        f"{summary['no_words']} clip(s) had no words recognised.",
    ]

    if summary.get("four_class"):
        four = summary["four_class"]
        out += [
            "",
            "### Only the 4 emotions the voice model knows (anger, joy, neutral, sadness)",
            "",
            *_table(
                ["Mode", "Accuracy", "Macro-F1"],
                [[MODE_NAMES[m], pct(r["accuracy"]), pct(r["macro_f1"])] for m, r in four.items()],
            ),
        ]

    cv = summary.get("speaker_cv")
    out += ["", "## Voice weight", ""]
    out += _table(
        ["Voice weight", "Accuracy", "Macro-F1"],
        [[f"{r['weight']:.1f}", pct(r["accuracy"]), pct(r["macro_f1"])] for r in summary["sweep"]],
    )
    if cv:
        weights = ", ".join(f"{f['weight']:.1f}" for f in cv["folds"])
        out += [
            "",
            "Reading the best row off this table would be optimistic, because it picks the "
            "weight using the same clips it's scored on. Picking the weight on half the speakers "
            f"and testing it on the other half (both ways; chosen weights {weights}) gives "
            f"**{pct(cv['accuracy'])} accuracy / {pct(cv['macro_f1'])} macro-F1**.",
        ]

    cal = summary.get("calibration")
    if cal:
        out += [
            "",
            "## Calibration",
            "",
            f"Simulates the in-app calibration for each of {cal['speakers']} speakers: "
            f"fit on 2 clips per emotion from that speaker, test on their other "
            f"{cal['test_clips']} clips "
            f"(combined mode, weight {w}).",
            "",
            *_table(
                ["", "Accuracy", "Macro-F1"],
                [
                    [
                        "Before calibration",
                        pct(cal["before"]["accuracy"]),
                        pct(cal["before"]["macro_f1"]),
                    ],
                    [
                        "After calibration",
                        pct(cal["after"]["accuracy"]),
                        pct(cal["after"]["macro_f1"]),
                    ],
                ],
            ),
        ]
        if cal.get("voice_before") is not None:
            out += [
                "",
                "The voice correction on its own (voice model, 4 broad moods): "
                f"{pct(cal['voice_before'])} → **{pct(cal['voice_after'])}** accuracy "
                "on the same held-out clips.",
            ]

    for key, groups in summary["breakdowns"].items():
        if key == "original_label":
            continue
        out += ["", f"## By {key}", ""]
        out += _table(
            [key, "Clips", *(MODE_NAMES[m] for m in modes)],
            [[g, str(r["n"]), *(pct(r[m]) for m in modes)] for g, r in groups.items()],
        )

    for mode, result in modes.items():
        out += ["", f"## {MODE_NAMES[mode]}: per emotion", ""]
        out += _table(
            ["Emotion", "Precision", "Recall", "F1", "Clips"],
            [
                [c, pct(s["precision"]), pct(s["recall"]), pct(s["f1"]), str(s["support"])]
                for c, s in result["per_class"].items()
            ],
        )
        out += ["", "Confusion matrix:", "", *_confusion(result)]

    lat = summary["latency"]
    total = lat["speech_end_to_state_ms"]
    med = lat["medians"]
    out += [
        "",
        "## Latency (end of speech → new emotion)",
        "",
        *_table(
            ["Median", "p90", "p95", "Max"],
            [[f"{total[k]:.0f} ms" for k in ("median", "p90", "p95", "max")]],
        ),
        "",
        f"= {lat['vad_wait_ms']} ms waiting for the pause that ends a sentence + processing "
        f"(median {med['processing_ms']:.0f} ms: speech-to-text {med['asr_ms']:.0f} ms and voice "
        f"{med['voice_ms']:.0f} ms in parallel, then text emotion {med['text_ms']:.0f} ms). "
        "The overlay's fade (300 ms by default) and the localhost WebSocket hop (well under "
        "a millisecond) come on top.",
    ]
    return "\n".join(out) + "\n"
