"""Microphone capture with sounddevice, delivering 16 kHz mono float32 blocks."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

TARGET_RATE = 16_000
BLOCK_SECONDS = 0.032  # matches one 512-sample Silero frame at 16 kHz

# PortAudio only scans for devices when it starts. A mic added, removed or restarted since
# then (Bluetooth headsets and virtual mixers like Elgato's do this) keeps a stale entry:
# indices shift and opening it fails, e.g. DirectSound -2005401480 (DSERR_NODRIVER).
# Restarting PortAudio re-scans, but closes every stream, so only while none is open.
_portaudio_lock = threading.RLock()
_open_streams = 0


def _rescan_devices() -> None:
    import sounddevice as sd

    with _portaudio_lock:
        if _open_streams:
            return
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            log.warning("Could not re-scan audio devices", exc_info=True)
            if not sd._initialized:
                sd._initialize()


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    default_samplerate: float
    is_default: bool


def list_input_devices() -> list[InputDevice]:
    """Input devices as Windows sees them now (re-scanned unless the mic is open)."""
    with _portaudio_lock:
        _rescan_devices()
        return _list_input_devices()


def _list_input_devices() -> list[InputDevice]:
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    default_input = sd.default.device[0]
    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] <= 0:
            continue
        api = hostapis[info["hostapi"]]["name"]
        # Windows kernel-streaming endpoints usually can't be opened alongside other apps.
        if "WDM-KS" in api:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=info["name"],
                hostapi=api,
                default_samplerate=float(info["default_samplerate"]),
                is_default=index == default_input,
            )
        )
    return devices


def pick_device(
    devices: list[InputDevice], index: int | None, name: str | None, hostapi: str | None
) -> int | None:
    """The PortAudio index to open: the saved mic by name, else the saved index.

    Returns None (system default) if a mic was saved by name but isn't connected.
    """
    if not name:
        return index
    same_name = [d for d in devices if d.name == name]
    for device in same_name:
        if device.hostapi == hostapi:
            return device.index
    return same_name[0].index if same_name else None


def resample(block: np.ndarray, rate: int) -> np.ndarray:
    """Convert a mono block to 16 kHz. Good enough for speech recognition, not for music."""
    if rate == TARGET_RATE:
        return block
    ratio = rate / TARGET_RATE
    if ratio.is_integer():
        # Averaging each group of samples doubles as a crude anti-aliasing filter.
        step = int(ratio)
        usable = len(block) - len(block) % step
        return block[:usable].reshape(-1, step).mean(axis=1).astype(np.float32, copy=False)
    n_out = round(len(block) / ratio)
    positions = np.arange(n_out) * ratio
    return np.interp(positions, np.arange(len(block)), block).astype(np.float32)


class MicCapture:
    def __init__(self, on_audio: Callable[[np.ndarray], None]) -> None:
        self._on_audio = on_audio
        self._stream: Any = None
        self.device_name = ""
        self.samplerate = 0
        self.missing_device: str | None = None  # saved mic that wasn't connected

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(
        self, device: int | None, name: str | None = None, hostapi: str | None = None
    ) -> None:
        """Open the mic saved by `name` (+ `hostapi`), else `device` (index), else the default."""
        with _portaudio_lock:
            self.stop()
            _rescan_devices()
            self._open(device, name, hostapi)

    def _open(self, device: int | None, name: str | None, hostapi: str | None) -> None:
        global _open_streams
        import sounddevice as sd

        if name:
            device = pick_device(_list_input_devices(), device, name, hostapi)
            self.missing_device = name if device is None else None
            if device is None:
                log.warning("Saved microphone '%s' isn't connected; using the default", name)
        else:
            self.missing_device = None
        info = sd.query_devices(device, "input")
        rate = TARGET_RATE
        try:
            sd.check_input_settings(device=device, channels=1, samplerate=rate, dtype="float32")
        except Exception:
            # e.g. WASAPI only opens at the device's native rate; resample ourselves.
            rate = int(info["default_samplerate"])

        def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if status:
                log.debug("audio status: %s", status)
            self._on_audio(resample(indata[:, 0].astype(np.float32, copy=True), rate))

        stream = sd.InputStream(
            device=device,
            channels=1,
            samplerate=rate,
            dtype="float32",
            blocksize=int(rate * BLOCK_SECONDS),
            callback=callback,
        )
        _open_streams += 1
        try:
            stream.start()
        except Exception:
            stream.close()
            _open_streams -= 1
            raise
        self._stream = stream
        self.device_name = str(info["name"])
        self.samplerate = rate
        log.info("Microphone open: %s @ %d Hz", self.device_name, rate)

    def stop(self) -> None:
        global _open_streams
        with _portaudio_lock:
            if self._stream is not None:
                stream, self._stream = self._stream, None
                _open_streams -= 1
                stream.stop()
                stream.close()
