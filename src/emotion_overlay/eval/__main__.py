"""CLI: python -m emotion_overlay.eval {download,record,run,report} ..."""

import argparse
import json
import logging
import os
import platform
from pathlib import Path

from emotion_overlay.config import load_config
from emotion_overlay.eval.datasets import DATASETS

DATA = {"ravdess": Path("data/datasets/ravdess"), "mine": Path("data/recordings")}
RESULTS = Path("eval/results")


def _report(name: str, meta: dict[str, str], config_path: Path) -> Path:
    from emotion_overlay.eval.analysis import analyse
    from emotion_overlay.eval.report import render
    from emotion_overlay.eval.runner import load_records

    config = load_config(config_path)
    folder = RESULTS / name
    records = load_records(folder / "predictions.jsonl")
    if not records:
        raise SystemExit(
            f"No predictions for '{name}' yet. Run: python -m emotion_overlay.eval run {name}"
        )
    summary = analyse(
        records,
        config.audio_emotion.label_map,
        config.audio_emotion.weight,
        config.vad.min_silence_ms,
    )
    meta_path = folder / "meta.json"
    if meta:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    elif meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = folder / "report.md"
    report.write_text(render(name, summary, meta), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m emotion_overlay.eval")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("download", help="download a public dataset").add_argument(
        "dataset", choices=["ravdess"]
    )
    sub.add_parser("record", help="record your own labelled clips with your mic")
    run = sub.add_parser("run", help="run the models over a dataset, then write the report")
    run.add_argument("dataset", choices=sorted(DATASETS))
    run.add_argument("--limit", type=int, help="only the first N clips (quick check)")
    sub.add_parser("report", help="re-analyse cached predictions").add_argument(
        "dataset", choices=sorted(DATASETS)
    )
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    logging.basicConfig(level=logging.WARNING)

    if args.command == "download":
        from emotion_overlay.eval.download import download

        download(args.dataset, DATA[args.dataset].parent)
    elif args.command == "record":
        from emotion_overlay.eval.record import record

        record(load_config(args.config), DATA["mine"])
    elif args.command == "run":
        from emotion_overlay.eval.runner import run as run_models

        clips = DATASETS[args.dataset](DATA[args.dataset])
        if not clips:
            raise SystemExit(f"No clips found in {DATA[args.dataset]}")
        clips = clips[: args.limit] if args.limit else clips
        config = load_config(args.config)
        models = run_models(clips, RESULTS / args.dataset / "predictions.jsonl", config)
        cpu = platform.processor() or platform.machine()
        meta = {
            "Models": f"{models}; text {config.classifier.model}; "
            f"voice {config.audio_emotion.model}",
            "Hardware": f"{cpu}, {os.cpu_count()} logical CPUs, "
            f"{platform.system()} {platform.release()}",
        }
        print(f"Report: {_report(args.dataset, meta, args.config)}")
    else:
        print(f"Report: {_report(args.dataset, {}, args.config)}")


if __name__ == "__main__":
    main()
