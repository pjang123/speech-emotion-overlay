import numpy as np
import pytest

from emotion_overlay.audio.capture import resample
from emotion_overlay.audio.vad import FRAME, SAMPLE_RATE, UtteranceSegmenter
from emotion_overlay.config import VadConfig


def loud_is_speech(frame: np.ndarray) -> float:
    """Fake VAD: frames with amplitude count as speech."""
    return 1.0 if np.abs(frame).mean() > 0.1 else 0.0


def speech(seconds: float) -> np.ndarray:
    return np.full(int(seconds * SAMPLE_RATE), 0.5, dtype=np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


@pytest.fixture
def cfg() -> VadConfig:
    return VadConfig(min_silence_ms=400, speech_pad_ms=100, min_utterance_ms=250, max_utterance_s=6)


def test_speech_then_silence_yields_one_utterance(cfg: VadConfig) -> None:
    seg = UtteranceSegmenter(loud_is_speech, cfg, clock=lambda: 42.0)
    out = seg.feed(np.concatenate([silence(1), speech(1.5), silence(1)]))
    assert len(out) == 1
    utt = out[0]
    assert not utt.forced and utt.ended_at == 42.0
    # 1.5 s of speech plus at most ~100 ms padding on each side (rounded up to frames)
    assert 1.5 <= utt.duration_s <= 1.5 + 0.3
    assert not seg.speaking


def test_preroll_keeps_audio_before_speech_onset(cfg: VadConfig) -> None:
    seg = UtteranceSegmenter(loud_is_speech, cfg)
    quiet_lead = np.full(FRAME * 4, 0.01, dtype=np.float32)  # below the fake VAD threshold
    out = seg.feed(np.concatenate([quiet_lead, speech(1), silence(1)]))
    assert np.isclose(out[0].audio[0], 0.01)


def test_short_blip_is_ignored(cfg: VadConfig) -> None:
    seg = UtteranceSegmenter(loud_is_speech, cfg)
    assert seg.feed(np.concatenate([speech(0.1), silence(1)])) == []


def test_long_speech_is_cut_at_max_length(cfg: VadConfig) -> None:
    seg = UtteranceSegmenter(loud_is_speech, cfg)
    out = seg.feed(speech(13))
    assert [u.forced for u in out] == [True, True]
    assert all(u.duration_s == pytest.approx(6.0, abs=FRAME / SAMPLE_RATE) for u in out)
    assert seg.speaking  # still talking after the cuts
    tail = seg.feed(silence(1))
    assert len(tail) == 1 and not tail[0].forced


def test_brief_pause_does_not_split_utterance(cfg: VadConfig) -> None:
    seg = UtteranceSegmenter(loud_is_speech, cfg)
    out = seg.feed(np.concatenate([speech(1), silence(0.2), speech(1), silence(1)]))
    assert len(out) == 1


def test_arbitrary_chunk_sizes_give_same_result(cfg: VadConfig) -> None:
    audio = np.concatenate([silence(0.5), speech(1), silence(1), speech(0.8), silence(1)])
    whole = UtteranceSegmenter(loud_is_speech, cfg).feed(audio)
    seg = UtteranceSegmenter(loud_is_speech, cfg)
    pieces = []
    for start in range(0, len(audio), 333):
        pieces += seg.feed(audio[start : start + 333])
    assert [len(u.audio) for u in pieces] == [len(u.audio) for u in whole]
    assert len(whole) == 2


def test_reset_discards_partial_utterance(cfg: VadConfig) -> None:
    seg = UtteranceSegmenter(loud_is_speech, cfg)
    seg.feed(speech(1))
    assert seg.speaking
    seg.reset()
    assert not seg.speaking
    assert seg.feed(silence(1)) == []


@pytest.mark.parametrize("rate", [16000, 48000, 44100, 32000])
def test_resample_produces_16k(rate: int) -> None:
    block = np.random.default_rng(0).standard_normal(int(rate * 0.032)).astype(np.float32)
    out = resample(block, rate)
    assert out.dtype == np.float32
    assert abs(len(out) - 512) <= 1
