"""Command-line entry point: `emotion-overlay` or `python -m emotion_overlay`."""

import argparse
import ipaddress
import logging
import os
from pathlib import Path

import uvicorn

from emotion_overlay.config import load_config
from emotion_overlay.pipeline import Pipeline
from emotion_overlay.server.app import create_app

log = logging.getLogger("emotion_overlay")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="emotion-overlay", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="run the web UI only, without loading models or opening the microphone",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args(argv)

    # Windows without Developer Mode can't symlink; the HF cache still works, so skip the nag.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    if args.log_level != "DEBUG":
        # Keep per-request HTTP chatter out of the console (it still shows with DEBUG,
        # which is how you can verify no network calls happen after the first run).
        for noisy in ("httpx", "httpx2", "huggingface_hub", "transformers"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    config = load_config(args.config)
    host, port = config.server.host, config.server.port
    if not _is_loopback(host):
        log.warning("Server is bound to %s, which is reachable from other machines.", host)

    factory = None if args.no_audio else (lambda events: Pipeline(config, events))
    app = create_app(config, config_path=args.config, engine_factory=factory)

    print(f"\n  Setup page:  http://{host}:{port}/setup")
    print(f"  OBS overlay: http://{host}:{port}/overlay\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
