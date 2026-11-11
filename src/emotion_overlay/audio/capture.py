"""Microphone capture with sounddevice, delivering 16 kHz mono float32 blocks."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

TARGET_RATE = 16_000
BLOCK_SECONDS = 0.032  # matches one 512-sample Silero frame at 16 kHz


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    default_samplerate: float
    is_default: bool


def list_input_devices() -> list[InputDevice]:
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

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self, device: int | None) -> None:
        import sounddevice as sd

        self.stop()
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
        stream.start()
        self._stream = stream
        self.device_name = str(info["name"])
        self.samplerate = rate
        log.info("Microphone open: %s @ %d Hz", self.device_name, rate)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
