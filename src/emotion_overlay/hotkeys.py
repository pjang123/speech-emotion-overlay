"""Global hotkeys for forcing an emotion by hand (Windows only).

Modifiers + 1..7 hold an emotion (in EMOTIONS order), modifiers + 0 goes back to
detected emotions. Uses the Win32 RegisterHotKey API, so Windows only tells the app
about these exact key combinations: no keyboard hook, nothing else typed is seen.
"""

import logging
import sys
import threading
from collections.abc import Callable

from emotion_overlay.emotions import EMOTIONS

log = logging.getLogger(__name__)

MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000  # holding the keys down fires once
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
AUTO_KEY = 0  # modifiers + 0: back to detected emotions


def parse_modifiers(text: str) -> int:
    """'ctrl+alt+shift' -> Win32 modifier flags. Raises ValueError on unknown names."""
    names = [part.strip().lower() for part in text.split("+") if part.strip()]
    if not names:
        raise ValueError("hotkeys need at least one modifier, e.g. ctrl+alt")
    flags = 0
    for name in names:
        if name not in MODIFIERS:
            raise ValueError(f"unknown modifier '{name}' (use {', '.join(MODIFIERS)})")
        flags |= MODIFIERS[name]
    return flags


def describe(modifiers: str) -> str:
    """'ctrl+alt+shift' -> 'Ctrl+Alt+Shift' for the setup page."""
    return "+".join(part.strip().capitalize() for part in modifiers.split("+") if part.strip())


def key_emotion(digit: int) -> str | None:
    """The emotion a digit key holds; None for the 'back to detected' key."""
    return None if digit == AUTO_KEY else EMOTIONS[digit - 1]


KEYS = (AUTO_KEY, *range(1, len(EMOTIONS) + 1))


class HotkeyListener:
    """Registers the hotkeys on its own thread (Win32 delivers them to the registering
    thread's message queue) and calls `on_press(digit)` from that thread."""

    def __init__(self, modifiers: str, on_press: Callable[[int], None]) -> None:
        self._flags = parse_modifiers(modifiers) | MOD_NOREPEAT
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.failed: list[int] = []  # digits another app had already taken
        self.error: str | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def start(self) -> None:
        if not self.supported:
            self.error = "global hotkeys are only available on Windows"
            return
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._thread is None or not self._thread_id:
            return
        import ctypes

        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        for digit in KEYS:
            # The hotkey id is the digit; the virtual-key code of '0'..'9' is its ASCII code.
            if user32.RegisterHotKey(None, digit, self._flags, ord(str(digit))):
                registered.append(digit)
            else:
                self.failed.append(digit)
        if self.failed:
            log.warning("Hotkeys already used by another app: %s", self.failed)
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self._on_press(int(msg.wParam))
                    except Exception:
                        log.exception("Hotkey handling failed")
        finally:
            for digit in registered:
                user32.UnregisterHotKey(None, digit)
