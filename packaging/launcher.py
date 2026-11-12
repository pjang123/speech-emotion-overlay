"""Entry point for the PyInstaller-built EmotionOverlay.exe.

Wraps `emotion_overlay.main` so a crash doesn't close the console window before the
user can read the error.
"""

import contextlib
import multiprocessing
import sys
import traceback


def _pause() -> None:
    if sys.stdin is not None and sys.stdin.isatty() and "--self-test" not in sys.argv:
        with contextlib.suppress(EOFError):
            input("\nPress Enter to close this window...")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        from emotion_overlay.main import main

        code = main()
    except KeyboardInterrupt:
        code = 0
    except Exception:
        traceback.print_exc()
        print("\nEmotion Overlay stopped because of the error above.")
        _pause()
        code = 1
    sys.exit(code)
