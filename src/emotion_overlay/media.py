"""Reading the audio out of a video or audio file, for long-form calibration.

Uses PyAV (FFmpeg), which faster-whisper already depends on, so any common format
works: MP4/MKV/MOV/WebM recordings, MP3, WAV, M4A, OGG, FLAC... Files are opened
by FFmpeg's demuxers as data only; nothing in them is executed.

OBS can record several audio tracks (e.g. mic on one, game on another); the user
picks one, since the mic-only track calibrates best.
"""

from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
CHUNK_S = 0.25  # audio is handed on in pieces this long
MAX_DURATION_S = 4 * 3600


class MediaError(ValueError):
    pass


@dataclass(frozen=True)
class AudioTrack:
    index: int  # position among the file's audio streams (0 = first)
    title: str
    channels: int
    duration_s: float | None

    def to_api(self) -> dict[str, Any]:
        return asdict(self)


def _open(path: Path) -> Any:
    import av

    try:
        return av.open(str(path))
    except (av.FFmpegError, OSError) as exc:
        raise MediaError("that file isn't a video or audio file this app can read") from exc


def probe(path: Path) -> list[AudioTrack]:
    """The file's audio tracks. Raises MediaError if it has none."""
    with _open(path) as container:
        tracks = []
        container_s = container.duration / 1_000_000 if container.duration else None
        for i, stream in enumerate(container.streams.audio):
            duration = (
                float(stream.duration * stream.time_base)
                if stream.duration and stream.time_base
                else container_s
            )
            meta = stream.metadata or {}
            title = meta.get("title") or meta.get("handler_name") or f"Track {i + 1}"
            tracks.append(
                AudioTrack(i, str(title)[:80], stream.codec_context.channels or 0, duration)
            )
    if not tracks:
        raise MediaError("that file has no sound")
    return tracks


def decode(path: Path, track: int = 0) -> Iterator[np.ndarray]:
    """16 kHz mono float32 audio of one track, in CHUNK_S pieces (up to MAX_DURATION_S)."""
    import av

    with _open(path) as container:
        streams = container.streams.audio
        if not 0 <= track < len(streams):
            raise MediaError("that audio track doesn't exist")
        stream = streams[track]
        # Keep the channels and average them here: FFmpeg's own downmix adds them at
        # -3 dB, which can push a loud stereo recording past full scale.
        resampler = av.AudioResampler(format="fltp", rate=SAMPLE_RATE)
        chunk = int(CHUNK_S * SAMPLE_RATE)
        pending = np.zeros(0, dtype=np.float32)
        total = 0
        try:
            frames = container.decode(stream)
            for frame in frames:
                for out in resampler.resample(frame):
                    pending = np.concatenate([pending, _mono(out)])
                while len(pending) >= chunk:
                    yield pending[:chunk]
                    pending = pending[chunk:]
                    total += chunk
                    if total >= MAX_DURATION_S * SAMPLE_RATE:
                        return
            for out in resampler.resample(None):
                pending = np.concatenate([pending, _mono(out)])
        except av.FFmpegError as exc:
            raise MediaError(f"couldn't read the sound in that file ({exc})") from exc
        if len(pending):
            yield pending


def _mono(frame: Any) -> np.ndarray:
    planes = frame.to_ndarray()  # channels x samples ("fltp" is planar)
    return planes.mean(axis=0, dtype=np.float32) if planes.ndim == 2 else planes
