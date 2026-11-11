"""Runs each clip through the real models, the same way the live pipeline does, and
caches the raw outputs as JSON lines so analysis can be redone without the models.

Per clip: VAD trims the clip to speech; Whisper and the voice model run in parallel
(as in the app); the text model scores the clip's own transcript (clips are
independent, so there is no rolling window).
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from emotion_overlay.config import AppConfig
from emotion_overlay.eval.datasets import Clip, load_wav


class Predictor:
    def __init__(self, config: AppConfig) -> None:
        from emotion_overlay.asr import load_transcriber
        from emotion_overlay.audio.vad import UtteranceSegmenter, load_silero_speech_prob
        from emotion_overlay.classify.audio import AudioEmotionClassifier
        from emotion_overlay.classify.text import TextEmotionClassifier

        self.config = config
        self.segmenter = UtteranceSegmenter(load_silero_speech_prob(), config.vad)
        self.transcriber = load_transcriber(config.asr)
        self.text = TextEmotionClassifier(config.classifier)
        self.voice = AudioEmotionClassifier(config.audio_emotion)
        self.pool = ThreadPoolExecutor(max_workers=1)

    @property
    def description(self) -> str:
        asr = self.transcriber
        return f"Whisper {self.config.asr.model} ({asr.backend} on {asr.device})"

    def speech(self, audio: np.ndarray) -> np.ndarray:
        """The VAD-trimmed speech in the clip (whole clip if the VAD finds nothing)."""
        self.segmenter.reset()
        tail = np.zeros(16_000, dtype=np.float32)  # let the VAD see the end of speech
        utterances = self.segmenter.feed(np.concatenate([audio, tail]))
        return np.concatenate([u.audio for u in utterances]) if utterances else audio

    def predict(self, clip: Clip) -> dict[str, Any]:
        audio = self.speech(load_wav(clip.path))

        def timed_voice() -> tuple[dict[str, float], float]:
            t = time.perf_counter()
            return self.voice.scores(audio), (time.perf_counter() - t) * 1000

        start = time.perf_counter()
        voice_job = self.pool.submit(timed_voice)
        transcript = self.transcriber.transcribe(audio)
        t_asr = time.perf_counter()
        text_scores = self.text.scores(transcript) if transcript else {}
        t_text = time.perf_counter()
        voice, voice_ms = voice_job.result()
        end = time.perf_counter()
        return {
            "id": clip.id,
            "label": clip.label,
            "speaker": clip.speaker,
            "meta": clip.meta,
            "transcript": transcript,
            "text_scores": text_scores,
            "voice": voice,
            "timings": {
                "asr_ms": (t_asr - start) * 1000,
                "text_ms": (t_text - t_asr) * 1000,
                "voice_ms": voice_ms,
                "processing_ms": (end - start) * 1000,  # ASR ∥ voice, then text
                "speech_s": len(audio) / 16_000,
            },
        }


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run(clips: list[Clip], out: Path, config: AppConfig) -> str:
    """Predict every clip not already in `out` (so long runs can be resumed)."""
    done = {r["id"] for r in load_records(out)}
    todo = [c for c in clips if c.id not in done]
    print(f"{len(clips)} clips, {len(done)} already done, {len(todo)} to go", file=sys.stderr)
    predictor = Predictor(config)
    out.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    with out.open("a", encoding="utf-8") as f:
        for i, clip in enumerate(todo, 1):
            f.write(json.dumps(predictor.predict(clip)) + "\n")
            f.flush()
            rate = (time.perf_counter() - start) / i
            print(
                f"\r  {i}/{len(todo)}  ~{rate * (len(todo) - i) / 60:.0f} min left ",
                end="",
                file=sys.stderr,
            )
    print(file=sys.stderr)
    return predictor.description
