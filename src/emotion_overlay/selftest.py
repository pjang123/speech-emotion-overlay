"""`--self-test`: load every model and run each once, without a microphone.

Checks that an install (especially the bundled exe) has everything it needs, and
downloads the models on first use. Exits non-zero if any required step fails.
"""

import logging
import time
from collections.abc import Callable

import numpy as np

from emotion_overlay.config import AppConfig

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
HAPPY_TEXT = "I'm so happy, this is the best day ever!"


def _step(name: str, fn: Callable[[], str]) -> bool:
    print(f"  {name} ...", flush=True)
    start = time.perf_counter()
    try:
        detail = fn()
    except Exception:
        log.exception("%s failed", name)
        print(f"  FAIL  {name}", flush=True)
        return False
    print(f"  ok    {name} ({time.perf_counter() - start:.1f} s): {detail}", flush=True)
    return True


def run_self_test(config: AppConfig) -> bool:
    from emotion_overlay.asr import load_transcriber
    from emotion_overlay.audio.vad import load_silero_speech_prob
    from emotion_overlay.classify.audio import AudioEmotionClassifier
    from emotion_overlay.classify.text import TextEmotionClassifier

    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(SAMPLE_RATE) * 0.01).astype(np.float32)

    def vad() -> str:
        prob = load_silero_speech_prob()(noise[:512])
        return f"speech probability on noise {prob:.2f}"

    def asr() -> str:
        transcriber = load_transcriber(config.asr)
        text = transcriber.transcribe(noise)
        return f"{transcriber.backend} backend, heard {text!r}"

    def text_model() -> str:
        scores = TextEmotionClassifier(config.classifier).scores(HAPPY_TEXT)
        top = max(scores, key=scores.__getitem__)
        if top != "joy":
            raise AssertionError(f"expected joy for {HAPPY_TEXT!r}, got {top}")
        return f"{HAPPY_TEXT!r} -> {top} {scores[top]:.2f}"

    def voice_model() -> str:
        scores = AudioEmotionClassifier(config.audio_emotion).scores(noise)
        return ", ".join(f"{label} {p:.2f}" for label, p in scores.items())

    print("Self-test (the first run downloads the models, which takes a few minutes):")
    steps = [("voice activity model", vad), ("speech-to-text", asr), ("text emotion", text_model)]
    if config.audio_emotion.enabled:
        steps.append(("tone-of-voice emotion", voice_model))
    results = [_step(name, fn) for name, fn in steps]
    passed = all(results)
    print("Self-test passed." if passed else "Self-test FAILED (details above).", flush=True)
    return passed
