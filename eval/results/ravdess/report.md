# Evaluation: ravdess

1440 clips from 24 speaker(s). Combined = words and voice fused at the app's default voice weight **0.25**.

- Models: Whisper base.en (faster-whisper on cpu); text j-hartmann/emotion-english-distilroberta-base; voice superb/hubert-base-superb-er
- Hardware: AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD, 16 logical CPUs, Windows 11

## Summary

| Mode | Accuracy | Macro-F1 |
|---|---|---|
| Words only | 21.9% | 7.9% |
| Voice only | 21.4% | 11.9% |
| Combined (app default) | 22.2% | 8.5% |

Chance level is about 14.3% accuracy (7 classes). 0 clip(s) had no words recognised.

### Only the 4 emotions the voice model knows (anger, joy, neutral, sadness)

| Mode | Accuracy | Macro-F1 |
|---|---|---|
| Words only | 33.2% | 13.3% |
| Voice only | 35.6% | 26.3% |
| Combined (app default) | 33.4% | 13.9% |

## Voice weight

| Voice weight | Accuracy | Macro-F1 |
|---|---|---|
| 0.0 | 21.9% | 7.9% |
| 0.1 | 22.0% | 8.0% |
| 0.2 | 22.0% | 8.1% |
| 0.3 | 23.1% | 10.3% |
| 0.4 | 24.0% | 11.7% |
| 0.5 | 29.6% | 16.7% |
| 0.6 | 30.1% | 17.4% |
| 0.7 | 29.0% | 17.3% |
| 0.8 | 27.0% | 16.9% |
| 0.9 | 24.8% | 16.1% |
| 1.0 | 23.4% | 15.5% |

Reading the best row off this table would be optimistic, because it picks the weight using the same clips it's scored on. Picking the weight on half the speakers and testing it on the other half (both ways; chosen weights 0.6, 0.6) gives **30.1% accuracy / 17.4% macro-F1**.

## Calibration

Simulates the in-app calibration for each of 24 speakers: fit on 2 clips per emotion from that speaker, test on their other 1104 clips (combined mode, weight 0.25).

|  | Accuracy | Macro-F1 |
|---|---|---|
| Before calibration | 23.9% | 9.0% |
| After calibration | 25.6% | 12.0% |

The voice correction on its own (voice model, 4 broad moods): 45.5% → **45.9%** accuracy on the same held-out clips.

## By intensity

| intensity | Clips | Words only | Voice only | Combined (app default) |
|---|---|---|---|---|
| normal | 768 | 25.9% | 22.9% | 26.0% |
| strong | 672 | 17.4% | 19.6% | 17.9% |

## Words only: per emotion

| Emotion | Precision | Recall | F1 | Clips |
|---|---|---|---|---|
| anger | 0.0% | 0.0% | 0.0% | 192 |
| disgust | 28.6% | 2.1% | 3.9% | 192 |
| fear | 0.0% | 0.0% | 0.0% | 192 |
| joy | 0.0% | 0.0% | 0.0% | 192 |
| neutral | 22.5% | 99.7% | 36.7% | 288 |
| sadness | 0.0% | 0.0% | 0.0% | 192 |
| surprise | 17.1% | 13.0% | 14.8% | 192 |

Confusion matrix:

| true ↓ / predicted → | anger | disgust | fear | neutral | surprise |
|---|---|---|---|---|---|
| **anger** | 0 | 3 | 0 | 148 | 41 |
| **disgust** | 0 | 4 | 1 | 175 | 12 |
| **fear** | 1 | 4 | 0 | 146 | 41 |
| **joy** | 0 | 1 | 1 | 169 | 21 |
| **neutral** | 0 | 1 | 0 | 287 | 0 |
| **sadness** | 0 | 1 | 1 | 184 | 6 |
| **surprise** | 0 | 0 | 0 | 167 | 25 |

## Voice only: per emotion

| Emotion | Precision | Recall | F1 | Clips |
|---|---|---|---|---|
| anger | 16.8% | 99.0% | 28.7% | 192 |
| disgust | 0.0% | 0.0% | 0.0% | 192 |
| fear | 0.0% | 0.0% | 0.0% | 192 |
| joy | 10.2% | 6.8% | 8.1% | 192 |
| neutral | 58.9% | 35.8% | 44.5% | 288 |
| sadness | 33.3% | 1.0% | 2.0% | 192 |
| surprise | 0.0% | 0.0% | 0.0% | 192 |

Confusion matrix:

| true ↓ / predicted → | anger | joy | neutral | sadness |
|---|---|---|---|---|
| **anger** | 190 | 2 | 0 | 0 |
| **disgust** | 175 | 9 | 8 | 0 |
| **fear** | 168 | 21 | 3 | 0 |
| **joy** | 173 | 13 | 6 | 0 |
| **neutral** | 142 | 39 | 103 | 4 |
| **sadness** | 104 | 41 | 45 | 2 |
| **surprise** | 179 | 3 | 10 | 0 |

## Combined (app default): per emotion

| Emotion | Precision | Recall | F1 | Clips |
|---|---|---|---|---|
| anger | 66.7% | 1.0% | 2.1% | 192 |
| disgust | 35.0% | 3.6% | 6.6% | 192 |
| fear | 0.0% | 0.0% | 0.0% | 192 |
| joy | 0.0% | 0.0% | 0.0% | 192 |
| neutral | 22.6% | 99.7% | 36.8% | 288 |
| sadness | 0.0% | 0.0% | 0.0% | 192 |
| surprise | 16.8% | 12.5% | 14.3% | 192 |

Confusion matrix:

| true ↓ / predicted → | anger | disgust | fear | neutral | surprise |
|---|---|---|---|---|---|
| **anger** | 2 | 4 | 0 | 147 | 39 |
| **disgust** | 0 | 7 | 1 | 172 | 12 |
| **fear** | 1 | 4 | 0 | 146 | 41 |
| **joy** | 0 | 2 | 1 | 168 | 21 |
| **neutral** | 0 | 1 | 0 | 287 | 0 |
| **sadness** | 0 | 1 | 1 | 184 | 6 |
| **surprise** | 0 | 1 | 0 | 167 | 24 |

## Latency (end of speech → new emotion)

| Median | p90 | p95 | Max |
|---|---|---|---|
| 712 ms | 840 ms | 868 ms | 1022 ms |

= 400 ms waiting for the pause that ends a sentence + processing (median 312 ms: speech-to-text 284 ms and voice 301 ms in parallel, then text emotion 20 ms). The overlay's fade (300 ms by default) and the localhost WebSocket hop (well under a millisecond) come on top.
