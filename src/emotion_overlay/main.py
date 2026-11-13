"""Command-line entry point: `emotion-overlay` or `python -m emotion_overlay`."""

import argparse
import ipaddress
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from emotion_overlay.config import load_config
from emotion_overlay.paths import is_frozen, user_dir
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


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _open_when_ready(url: str, host: str, port: int, timeout_s: float = 60.0) -> None:
    """Open the setup page once the server accepts connections (runs in a thread)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_open(host, port):
            webbrowser.open(url)
            return
        time.sleep(0.25)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emotion-overlay", description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=None, help="config file (default: config.yaml)"
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="run the web UI only, without loading models or opening the microphone",
    )
    parser.add_argument(
        "--open-browser",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="open the setup page on start (default: on for the installed app, off otherwise)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="load every model, run each once without a microphone, then exit",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args(argv)

    # A --config given on the command line is relative to where the user ran it from.
    config_path = args.config.resolve() if args.config else Path("config.yaml")
    if is_frozen():
        # Installed app: config.yaml and data/ live in a per-user folder, not next to the exe.
        home = user_dir()
        home.mkdir(parents=True, exist_ok=True)
        os.chdir(home)
    open_browser = is_frozen() if args.open_browser is None else args.open_browser

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
    config = load_config(config_path)

    if args.self_test:
        from emotion_overlay.selftest import run_self_test

        return 0 if run_self_test(config) else 1

    host, port = config.server.host, config.server.port
    if not _is_loopback(host):
        log.warning("Server is bound to %s, which is reachable from other machines.", host)
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # noqa: S104 - comparison
    setup_url = f"http://{browse_host}:{port}/setup"

    if _port_open(browse_host, port):
        print(f"\n  Emotion Overlay already seems to be running: {setup_url}\n")
        if open_browser:
            webbrowser.open(setup_url)
        return 0

    factory = None if args.no_audio else (lambda events: Pipeline(config, events))
    from emotion_overlay.hotkeys import HotkeyListener
    from emotion_overlay.obs import ObsController

    app = create_app(
        config,
        config_path=config_path,
        engine_factory=factory,
        hotkey_factory=HotkeyListener,
        obs_factory=ObsController,
    )

    print(f"\n  Setup page:  {setup_url}")
    print(f"  OBS overlay: http://{browse_host}:{port}/overlay")
    if is_frozen():
        print(f"  Your settings and images: {Path.cwd()}")
        print("  Close this window to quit.")
    print()
    if open_browser:
        threading.Thread(
            target=_open_when_ready, args=(setup_url, browse_host, port), daemon=True
        ).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
