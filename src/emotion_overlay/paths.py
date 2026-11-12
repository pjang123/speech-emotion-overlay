"""Where bundled resources and per-user files live, run from source or as the installed exe."""

import os
import sys
from pathlib import Path

APP_DIR_NAME = "EmotionOverlay"


def is_frozen() -> bool:
    """True when running as the PyInstaller-built exe."""
    return bool(getattr(sys, "frozen", False))


def web_dir() -> Path:
    if is_frozen():
        return Path(sys._MEIPASS) / "web"  # type: ignore[attr-defined]  # set by PyInstaller
    return Path(__file__).resolve().parents[2] / "web"


def user_dir() -> Path:
    """Per-user folder for config.yaml and data/ in the installed app.

    The install folder may not be writable and is replaced on upgrade, so user files
    live in %LOCALAPPDATA% instead (kept when the app is uninstalled).
    """
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home()) / APP_DIR_NAME
