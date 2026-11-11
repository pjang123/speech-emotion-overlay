"""Record your own labelled clips for evaluation, using your configured microphone.

Each emotion gets lines of two kinds:
- "words": the line itself sounds emotional, so the text model can get it right;
- "tone":  neutral words said with the emotion, so only the voice can tell.

Clips are saved as data/recordings/<emotion>__<kind>__<n>.wav, which is gitignored
and stays on your machine. Delete the folder whenever you like.
"""

import queue
import sys
import time
import wave
from pathlib import Path

import numpy as np

from emotion_overlay.audio.capture import MicCapture
from emotion_overlay.audio.vad import SAMPLE_RATE, UtteranceSegmenter, load_silero_speech_prob
from emotion_overlay.calibration import LINES
from emotion_overlay.config import AppConfig

TONE_LINES = ("I put the box on the table.", "The meeting is at three o'clock.")
SAY_IT = {
    "joy": "HAPPY",
    "anger": "ANGRY",
    "sadness": "SAD",
    "surprise": "SURPRISED",
    "fear": "SCARED",
    "disgust": "DISGUSTED",
    "neutral": "PLAIN / CALM",
}
MAX_SECONDS = 10


def script() -> list[tuple[str, str, int, str]]:
    """(emotion, kind, n, line), interleaved so emotions don't repeat back to back."""
    lines = []
    for n in range(2):
        for emotion, words in LINES.items():
            lines.append((emotion, "words", n + 1, words[n]))
            lines.append((emotion, "tone", n + 1, TONE_LINES[n]))
    return lines


def _save(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def record(config: AppConfig, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    blocks: queue.Queue[np.ndarray] = queue.Queue()
    segmenter = UtteranceSegmenter(load_silero_speech_prob(), config.vad)
    mic = MicCapture(blocks.put)
    lines = script()
    print(f"Recording to {out.resolve()}  ({len(lines)} lines; Ctrl+C to stop)\n")
    try:
        for i, (emotion, kind, n, line) in enumerate(lines, 1):
            path = out / f"{emotion}__{kind}__{n}.wav"
            if path.exists():
                continue
            print(f"[{i}/{len(lines)}] Say it {SAY_IT[emotion]}:  “{line}”")
            if input("    Enter = record, s = skip > ").strip().lower() == "s":
                continue
            segmenter.reset()
            mic.start(config.audio.device)
            deadline = time.monotonic() + MAX_SECONDS
            utterance = None
            print("    ● recording… speak, then pause")
            while utterance is None and time.monotonic() < deadline:
                try:
                    done = segmenter.feed(blocks.get(timeout=0.2))
                except queue.Empty:
                    continue
                utterance = done[0] if done else None
            mic.stop()
            while not blocks.empty():
                blocks.get_nowait()
            if utterance is None:
                print("    (no speech heard; skipped)\n")
                continue
            _save(path, utterance.audio)
            print(f"    saved {utterance.duration_s:.1f}s\n")
    except KeyboardInterrupt:
        print("\nStopped. Run again to continue where you left off.", file=sys.stderr)
    finally:
        mic.stop()
