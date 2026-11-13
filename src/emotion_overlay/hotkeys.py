"""Global hotkeys (Windows only): key combinations the user binds to actions.

A combination is written like "ctrl+alt+shift+1" or "f13": modifiers first, then
one key (see KEY_CODES for the names). What each one does is set in the config
(`hotkeys.bindings`, see config.HotkeyBinding); this module only turns the
combinations into Win32 hotkeys and reports which one was pressed.

Uses the Win32 RegisterHotKey API, so Windows only tells the app about these exact
key combinations: no keyboard hook, nothing else typed is seen.
"""

import logging
import sys
import threading
from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

MODIFIERS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}  # display order
MOD_NOREPEAT = 0x4000  # holding the keys down fires once
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MAX_BINDINGS = 50


def _key_codes() -> dict[str, int]:
    codes: dict[str, int] = {}
    for c in range(ord("A"), ord("Z") + 1):
        codes[chr(c).lower()] = c  # the virtual-key code of a letter is its capital
    for d in range(10):
        codes[str(d)] = ord(str(d))
        codes[f"num{d}"] = 0x60 + d
    for n in range(1, 25):
        codes[f"f{n}"] = 0x70 + n - 1
    codes.update(
        {
            "nummultiply": 0x6A,
            "numadd": 0x6B,
            "numsubtract": 0x6D,
            "numdecimal": 0x6E,
            "numdivide": 0x6F,
            "space": 0x20,
            "pageup": 0x21,
            "pagedown": 0x22,
            "end": 0x23,
            "home": 0x24,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
            "insert": 0x2D,
            "delete": 0x2E,
            "pause": 0x13,
            "scrolllock": 0x91,
            # Punctuation, by its position on a US keyboard (VK_OEM_*).
            ";": 0xBA,
            "=": 0xBB,
            ",": 0xBC,
            "-": 0xBD,
            ".": 0xBE,
            "/": 0xBF,
            "`": 0xC0,
            "[": 0xDB,
            "\\": 0xDC,
            "]": 0xDD,
            "'": 0xDE,
        }
    )
    return codes


KEY_CODES = _key_codes()
# Keys that don't type anything or move the cursor, so they may be used on their
# own (handy for Stream Decks and macro pads, which often send F13-F24).
LONE_KEYS = frozenset({*(f"f{n}" for n in range(1, 25)), "pause", "scrolllock"})
# Any other key needs one of these (Shift + a letter just types a capital).
_NON_TYPING = MODIFIERS["ctrl"] | MODIFIERS["alt"] | MODIFIERS["win"]

_KEY_LABELS = {
    "nummultiply": "Num *",
    "numadd": "Num +",
    "numsubtract": "Num -",
    "numdecimal": "Num .",
    "numdivide": "Num /",
    "pageup": "Page Up",
    "pagedown": "Page Down",
    "scrolllock": "Scroll Lock",
}


def parse_keys(text: str) -> tuple[int, int]:
    """'ctrl+alt+1' -> (Win32 modifier flags, virtual-key code). Raises ValueError."""
    parts = [part.strip().lower() for part in text.split("+")]
    if not text.strip() or any(not part for part in parts):
        raise ValueError(f"'{text}' isn't a key combination, e.g. ctrl+alt+1")
    *mods, key = parts
    flags = 0
    for name in mods:
        if name not in MODIFIERS:
            raise ValueError(f"unknown modifier '{name}' (use {', '.join(MODIFIERS)})")
        if flags & MODIFIERS[name]:
            raise ValueError(f"'{name}' is listed twice in '{text}'")
        flags |= MODIFIERS[name]
    if key in MODIFIERS:
        raise ValueError("a hotkey needs a key besides Ctrl, Alt, Shift and Win")
    if key not in KEY_CODES:
        raise ValueError(f"unsupported key '{key}'")
    if key not in LONE_KEYS and not flags & _NON_TYPING:
        raise ValueError(
            f"{describe_keys(text)} would block normal typing: add Ctrl, Alt or Win "
            "(only F1-F24, Pause and Scroll Lock work on their own)"
        )
    return flags, KEY_CODES[key]


def normalize_keys(text: str) -> str:
    """Canonical spelling, modifiers in a fixed order: 'Alt + Ctrl+A' -> 'ctrl+alt+a'."""
    flags, _ = parse_keys(text)
    key = text.split("+")[-1].strip().lower()
    return "+".join([*(m for m, f in MODIFIERS.items() if flags & f), key])


def describe_keys(text: str) -> str:
    """'ctrl+alt+num5' -> 'Ctrl+Alt+Num 5' for the setup page."""
    parts = [part.strip().lower() for part in text.split("+")]
    labels = []
    for part in parts:
        if part in _KEY_LABELS:
            labels.append(_KEY_LABELS[part])
        elif part.startswith("num") and part[3:].isdigit():
            labels.append(f"Num {part[3:]}")
        elif part.startswith("f") and part[1:].isdigit():
            labels.append(part.upper())
        else:
            labels.append(part.capitalize())
    return "+".join(labels)


class HotkeyListener:
    """Registers hotkeys on its own thread (Win32 delivers them to the registering
    thread's message queue) and calls `on_press(index)` from that thread, where
    index is the combination's position in the list passed to start().

    Can be stopped and started again with a new list, e.g. after the user edits it.
    """

    def __init__(self, on_press: Callable[[int], None]) -> None:
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._combos: list[tuple[int, int]] = []
        self.failed: list[int] = []  # indexes whose keys another app had already taken
        self.error: str | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, keys: Sequence[str]) -> None:
        self.stop()
        self.failed = []
        self.error = None
        if not self.supported:
            self.error = "global hotkeys are only available on Windows"
            return
        self._combos = [parse_keys(k) for k in keys]
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._ready.wait(timeout=5)  # the thread id is known once it's registered
        if self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        for index, (flags, vk) in enumerate(self._combos):
            # Hotkey ids start at 1; the id is the index + 1.
            if user32.RegisterHotKey(None, index + 1, flags | MOD_NOREPEAT, vk):
                registered.append(index + 1)
            else:
                self.failed.append(index)
        if self.failed:
            log.warning("Hotkeys already used by another app: %s", self.failed)
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self._on_press(int(msg.wParam) - 1)
                    except Exception:
                        log.exception("Hotkey handling failed")
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
