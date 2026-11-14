"""Calibration from a long recording of natural speech (a play session or a video).

The 14 read lines of the normal calibration are acted, and people sound different
when they're actually playing: quieter, flatter, faster. A voice model can read
that flat game-talk as one emotion (seen in practice: "sadness" for half of all
sentences), which no amount of read lines reveals. So this calibrates on the
speech the app will really hear, in two steps:

1. Automatic, no effort: over all sentences, the voice model's labels are shifted
   (a per-label bias in log space, like the normal calibration's) until how often
   it hears each emotion matches how often the *words* express it. The text model
   doesn't care how flat someone sounds, so it's the reference. This removes a
   skew; it can't tell which single sentence was right.
2. Review, as much as the user likes: each sentence is listed with the app's guess,
   the user confirms or corrects it, or marks it "not me" (friends on voice chat,
   game audio, music). Reviewed sentences become ordinary labelled calibration
   samples, fitted starting from the automatic correction, with accuracy measured
   on them and the usual settings recommendations (personalize.py).

Pure logic plus the in-memory session. Audio and transcripts live only in memory
(for the review list and its play buttons) and are gone when the session ends.
"""

import io
import math
import threading
import wave
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from emotion_overlay.audio.vad import SAMPLE_RATE, SpeechProb, UtteranceSegmenter
from emotion_overlay.calibration import Calibration, CalibrationError, CalibrationSample
from emotion_overlay.classify.fusion import EPS, score_utterance
from emotion_overlay.config import AppConfig, VadConfig
from emotion_overlay.emotions import EMOTIONS, Scores
from emotion_overlay.personalize import Personalization, personalize

NOT_ME = "not_me"  # review label: someone else's voice, game audio, music...
LABELS = (*EMOTIONS, NOT_ME)

MIN_REVIEWED = 8  # fewer reviewed sentences: automatic correction only
MIN_AUTO_SENTENCES = 20  # fewer sentences with voice scores: no automatic correction
# The automatic shift is capped tighter than calibration's MAX_BIAS, and more so
# upward: a label the voice model overuses is the skew that matters, while boosting
# one it rarely picks would mostly copy the text model's own quirks into it.
MAX_AUTO_CUT = 2.0
MAX_AUTO_BOOST = 1.0
MAX_SENTENCES = 2000  # about 2-3 hours of steady talking
MAX_CLIP_S = 15.0  # audio kept per sentence for the review's play button
NOTABLE_SHARE = 0.10  # report a skew when voice and words differ by 10+ points


# --- one sentence ---------------------------------------------------------------


@dataclass
class Sentence:
    id: int
    start_s: float  # position in the recording (seconds from its start)
    end_s: float
    transcript: str
    text: Scores  # text model on this sentence alone (canonical emotions)
    voice: Scores  # raw voice model scores (its own labels); {} without the voice model
    audio: bytes = field(repr=False)  # 16 kHz mono int16, for playback only
    label: str | None = None  # None = not reviewed

    def wav(self) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(self.audio)
        return out.getvalue()


def clip_audio(audio: np.ndarray) -> bytes:
    """A sentence's audio as int16 bytes, cut to MAX_CLIP_S."""
    clipped = audio[: int(MAX_CLIP_S * SAMPLE_RATE)]
    return (np.clip(clipped, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _top(scores: Mapping[str, float]) -> str | None:
    return max(scores, key=lambda k: scores[k]) if scores else None


# --- step 1: automatic correction -----------------------------------------------


@dataclass(frozen=True)
class AutoCorrection:
    bias: dict[str, float]  # voice bias, in the voice model's labels ({} = none)
    sentences: int  # sentences it was measured on
    # Share of sentences each emotion was top for: by the voice model before and after
    # the correction, and by the words.
    voice_before: dict[str, float]
    voice_after: dict[str, float]
    words: dict[str, float]

    def notes(self) -> list[str]:
        """Plain-language findings, biggest skew first."""
        found = []
        for emotion in EMOTIONS:
            before = self.voice_before.get(emotion, 0.0)
            words = self.words.get(emotion, 0.0)
            if abs(before - words) >= NOTABLE_SHARE:
                found.append((abs(before - words), emotion, before, words))
        notes = []
        for _, emotion, before, words in sorted(found, reverse=True):
            after = self.voice_after.get(emotion, 0.0)
            notes.append(
                f"Your tone of voice sounded like {emotion} in {before:.0%} of your sentences, "
                f"but your words expressed {emotion} in {words:.0%}. "
                f"Corrected: now {after:.0%}."
            )
        return notes

    def to_api(self) -> dict[str, Any]:
        return {
            "sentences": self.sentences,
            "voice_before": self.voice_before,
            "voice_after": self.voice_after,
            "words": self.words,
            "notes": self.notes(),
        }


NO_CORRECTION = AutoCorrection({}, 0, {}, {}, {})


def _mapped(
    voice: Mapping[str, float], bias: Mapping[str, float], labels: Sequence[str]
) -> np.ndarray:
    """Voice probabilities of the mapped labels after the bias, renormalised."""
    logits = np.array([math.log(max(voice.get(k, 0.0), EPS)) + bias.get(k, 0.0) for k in labels])
    p = np.exp(logits - logits.max())
    return p / p.sum()


def _shares(tops: Iterable[str | None]) -> dict[str, float]:
    tops = [t for t in tops if t is not None]
    if not tops:
        return {}
    return {e: round(sum(t == e for t in tops) / len(tops), 3) for e in EMOTIONS if e in tops}


def auto_voice_bias(
    pairs: Sequence[tuple[Scores, Scores]],
    label_map: Mapping[str, Sequence[str]],
    steps: int = 300,
    rate: float = 0.5,
) -> AutoCorrection:
    """Bias the voice model so its average distribution over the user's sentences
    matches the words' (both as the voice model's labels). `pairs` = (text, voice).

    A fixed-point iteration on the label priors (as in Saerens et al., 2002, for
    prior shift), damped and clipped. Labels missing from `label_map` are left alone:
    the app ignores them anyway.
    """
    pairs = [(t, v) for t, v in pairs if t and v]
    if len(pairs) < MIN_AUTO_SENTENCES:
        return NO_CORRECTION
    labels = [k for k in label_map if label_map[k]]
    emotion_of = {k: label_map[k][0] for k in labels}  # for reporting shares
    # What the words say, gathered into voice labels (each emotion is in one label).
    target = np.array(
        [[sum(t.get(e, 0.0) for e in label_map[k]) for k in labels] for t, _ in pairs]
    )
    target = (target / np.maximum(target.sum(axis=1, keepdims=True), EPS)).mean(axis=0)
    target = np.maximum(target, 1e-3)

    bias = dict.fromkeys(labels, 0.0)
    for _ in range(steps):
        current = np.mean([_mapped(v, bias, labels) for _, v in pairs], axis=0)
        for i, k in enumerate(labels):
            step = rate * (math.log(target[i]) - math.log(max(current[i], 1e-6)))
            bias[k] = float(np.clip(bias[k] + step, -MAX_AUTO_CUT, MAX_AUTO_BOOST))
    bias = {k: round(b, 4) for k, b in bias.items()}

    def voice_top(v: Scores, b: Mapping[str, float]) -> str | None:
        return emotion_of[labels[int(np.argmax(_mapped(v, b, labels)))]]

    return AutoCorrection(
        bias=bias,
        sentences=len(pairs),
        voice_before=_shares(voice_top(v, {}) for _, v in pairs),
        voice_after=_shares(voice_top(v, bias) for _, v in pairs),
        words=_shares(_top(t) for t, _ in pairs),
    )


# --- the session ----------------------------------------------------------------


class LongformSession:
    """Sentences collected from one recording, and the user's review of them."""

    def __init__(self, source: str, name: str) -> None:
        self.source = source  # "live" | "file" | "link"
        self.name = name
        self._sentences: list[Sentence] = []
        self._lock = threading.Lock()
        self.truncated = False  # hit MAX_SENTENCES
        self.auto = NO_CORRECTION

    @property
    def sentences(self) -> list[Sentence]:
        with self._lock:
            return list(self._sentences)

    def add(
        self,
        start_s: float,
        end_s: float,
        transcript: str,
        text: Scores,
        voice: Scores,
        audio: np.ndarray,
    ) -> Sentence | None:
        """Add a sentence; None if it carried nothing usable or the session is full."""
        if not text and not voice:
            return None
        with self._lock:
            if len(self._sentences) >= MAX_SENTENCES:
                self.truncated = True
                return None
            sentence = Sentence(
                len(self._sentences),
                round(start_s, 2),
                round(end_s, 2),
                transcript,
                dict(text),
                dict(voice),
                clip_audio(audio),
            )
            self._sentences.append(sentence)
            return sentence

    def get(self, sentence_id: int) -> Sentence | None:
        with self._lock:
            if 0 <= sentence_id < len(self._sentences):
                return self._sentences[sentence_id]
            return None

    def set_label(self, sentence_id: int, label: str | None) -> Sentence:
        if label is not None and label not in LABELS:
            raise CalibrationError(f"unknown label '{label}'")
        sentence = self.get(sentence_id)
        if sentence is None:
            raise CalibrationError("no such sentence")
        sentence.label = label
        return sentence

    def counts(self) -> dict[str, int]:
        sentences = self.sentences
        return {
            "sentences": len(sentences),
            "reviewed": sum(s.label in EMOTIONS for s in sentences),
            "not_me": sum(s.label == NOT_ME for s in sentences),
        }

    def update_auto(self, label_map: Mapping[str, Sequence[str]]) -> AutoCorrection:
        """Recompute the automatic correction from every sentence not marked "not me"."""
        self.auto = auto_voice_bias(
            [(s.text, s.voice) for s in self.sentences if s.label != NOT_ME], label_map
        )
        return self.auto

    def guess(self, sentence: Sentence, config: AppConfig) -> str | None:
        """What the app would show for this sentence alone, with the automatic correction."""
        audio = config.audio_emotion
        scores = score_utterance(
            sentence.text,
            sentence.voice if audio.enabled else {},
            audio.label_map,
            audio.weight,
            self.auto.bias,
        ).final
        return _top(scores)

    def samples(self) -> list[CalibrationSample]:
        return [
            CalibrationSample(s.label, s.text, s.voice)
            for s in self.sentences
            if s.label in EMOTIONS and (s.text or s.voice)
        ]

    def fit(self, config: AppConfig) -> Personalization:
        """Calibrate from this recording: the automatic correction, refined by the
        reviewed sentences when there are enough of them."""
        audio = config.audio_emotion
        auto = self.update_auto(audio.label_map)
        samples = self.samples()
        used = sum(1 for s in self.sentences if s.label != NOT_ME)
        if len(samples) >= MIN_REVIEWED:
            result = personalize(samples, config, voice_prior=auto.bias or None)
            calibration = Calibration(
                **{**result.calibration.to_api(), "source": "long", "sentences": used}
            )
            return Personalization(calibration, result.recommendations)
        if not auto.bias:
            raise CalibrationError(
                f"review at least {MIN_REVIEWED} sentences (or analyse a longer recording "
                "with the tone-of-voice model on) to calibrate"
            )
        calibration = Calibration(
            voice_bias=auto.bias,
            final_bias={},
            weight=audio.weight,
            samples=len(samples),
            created=_now(),
            accuracy={},
            source="long",
            sentences=used,
        )
        return Personalization(calibration, [])

    def to_api(self, config: AppConfig, with_sentences: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "name": self.name,
            "truncated": self.truncated,
            **self.counts(),
            "auto": self.auto.to_api() if self.auto.bias else None,
            "min_reviewed": MIN_REVIEWED,
        }
        sentences = self.sentences
        if with_sentences:
            out["items"] = [self._sentence_api(s, config) for s in sentences]
        else:
            out["recent"] = [self._sentence_api(s, config) for s in sentences[-3:]]
        return out

    def _sentence_api(self, s: Sentence, config: AppConfig) -> dict[str, Any]:
        return {
            "id": s.id,
            "start": s.start_s,
            "end": s.end_s,
            "transcript": s.transcript,
            "guess": self.guess(s, config),
            "words": _top(s.text),
            "label": s.label,
        }


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


# --- turning a recording into sentences ------------------------------------------


class SentenceScorer(Protocol):
    def score_sentence(self, audio: np.ndarray) -> tuple[str, Scores, Scores]:
        """(transcript, text scores of this sentence alone, raw voice scores)."""
        ...


def analyse_stream(
    chunks: Iterable[np.ndarray],
    scorer: SentenceScorer,
    speech_prob: SpeechProb,
    vad: VadConfig,
    session: LongformSession,
    cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[float], None] = lambda done_s: None,
) -> None:
    """Split 16 kHz mono audio into sentences with the app's own voice detection
    (same settings as live), score each one and add it to the session."""
    position = [0]  # samples fed so far: the segmenter's clock is the recording's
    segmenter = UtteranceSegmenter(speech_prob, vad, clock=lambda: position[0] / SAMPLE_RATE)
    tail = np.zeros(SAMPLE_RATE, dtype=np.float32)  # lets the last sentence end

    def feed(chunk: np.ndarray) -> None:
        position[0] += len(chunk)
        for utterance in segmenter.feed(chunk):
            if cancelled():
                return
            transcript, text, voice = scorer.score_sentence(utterance.audio)
            end = utterance.ended_at - (0.0 if utterance.forced else vad.min_silence_ms / 1000)
            start = max(0.0, utterance.ended_at - utterance.duration_s)
            session.add(start, max(start, end), transcript, text, voice, utterance.audio)

    for chunk in chunks:
        if cancelled() or session.truncated:
            return
        feed(chunk)
        on_progress(position[0] / SAMPLE_RATE)
    if not cancelled():
        feed(tail)
