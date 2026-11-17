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


def cache_dir() -> Path:
    """Per-user folder for files the app makes itself and can make again (the compressed
    voice model). Not the app's folder: that may be synced (OneDrive) or replaced."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "emotion-overlay" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "emotion-overlay"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "emotion-overlay"


def user_dir() -> Path:
    """Per-user folder for config.yaml and data/ in the installed app.

    The install folder may not be writable and is replaced on upgrade, so user files
    live in %LOCALAPPDATA% instead (kept when the app is uninstalled).
    """
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home()) / APP_DIR_NAME
