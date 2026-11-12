# Evaluation: ravdess

1440 clips from 24 speaker(s). Combined = words and voice fused at the app's default voice weight **0.25**.

- Models: Whisper base.en (faster-whisper on cpu); text j-hartmann/emotion-english-distilroberta-base; voice tiantiaf/wavlm-large-categorical-emotion
- Hardware: AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD, 16 logical CPUs, Windows 11

## Summary

| Mode | Accuracy | Macro-F1 |
|---|---|---|
| Words only | 21.9% | 7.9% |
| Voice only | 40.1% | 35.5% |
| Combined (app default) | 22.5% | 9.0% |

Chance level is about 14.3% accuracy (7 classes). 0 clip(s) had no words recognised.

### Only anger, joy, neutral and sadness (comparable with 4-mood voice models)

| Mode | Accuracy | Macro-F1 |
|---|---|---|
| Words only | 33.2% | 13.3% |
| Voice only | 42.4% | 44.9% |
| Combined (app default) | 34.1% | 15.1% |

## Voice weight

| Voice weight | Accuracy | Macro-F1 |
|---|---|---|
| 0.0 | 21.9% | 7.9% |
| 0.1 | 21.9% | 7.9% |
| 0.2 | 22.0% | 8.1% |
| 0.3 | 22.8% | 9.5% |
| 0.4 | 23.5% | 11.0% |
| 0.5 | 26.7% | 16.6% |
| 0.6 | 32.2% | 23.8% |
| 0.7 | 36.8% | 30.2% |
| 0.8 | 39.4% | 33.2% |
| 0.9 | 40.1% | 34.9% |
| 1.0 | 40.1% | 35.5% |

Reading the best row off this table would be optimistic, because it picks the weight using the same clips it's scored on. Picking the weight on half the speakers and testing it on the other half (both ways; chosen weights 1.0, 1.0) gives **40.1% accuracy / 35.5% macro-F1**.

## Calibration

Simulates the in-app calibration for each of 24 speakers: fit on 2 clips per emotion from that speaker, test on their other 1104 clips (combined mode, weight 0.25).

|  | Accuracy | Macro-F1 |
|---|---|---|
| Before calibration | 24.3% | 9.5% |
| After calibration | 25.8% | 13.1% |

The voice correction on its own (voice model, its own labels): 40.2% → **44.1%** accuracy on the same held-out clips.

## By intensity

| intensity | Clips | Words only | Voice only | Combined (app default) |
|---|---|---|---|---|
| normal | 768 | 25.9% | 38.5% | 25.8% |
| strong | 672 | 17.4% | 42.0% | 18.8% |

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
| anger | 60.3% | 79.2% | 68.5% | 192 |
| disgust | 100.0% | 2.1% | 4.1% | 192 |
| fear | 26.8% | 70.3% | 38.8% | 192 |
| joy | 50.0% | 7.3% | 12.7% | 192 |
| neutral | 39.9% | 48.3% | 43.7% | 288 |
| sadness | 29.9% | 31.8% | 30.8% | 192 |
| surprise | 72.3% | 38.0% | 49.8% | 192 |

Confusion matrix:

| true ↓ / predicted → | anger | disgust | fear | joy | neutral | sadness | surprise |
|---|---|---|---|---|---|---|---|
| **anger** | 152 | 0 | 20 | 0 | 15 | 3 | 2 |
| **disgust** | 51 | 4 | 68 | 0 | 46 | 14 | 9 |
| **fear** | 12 | 0 | 135 | 2 | 22 | 18 | 3 |
| **joy** | 20 | 0 | 91 | 14 | 49 | 5 | 13 |
| **neutral** | 0 | 0 | 47 | 0 | 139 | 102 | 0 |
| **sadness** | 4 | 0 | 57 | 10 | 59 | 61 | 1 |
| **surprise** | 13 | 0 | 85 | 2 | 18 | 1 | 73 |

## Combined (app default): per emotion

| Emotion | Precision | Recall | F1 | Clips |
|---|---|---|---|---|
| anger | 77.8% | 3.6% | 7.0% | 192 |
| disgust | 44.4% | 2.1% | 4.0% | 192 |
| fear | 0.0% | 0.0% | 0.0% | 192 |
| joy | 0.0% | 0.0% | 0.0% | 192 |
| neutral | 22.5% | 100.0% | 36.8% | 288 |
| sadness | 0.0% | 0.0% | 0.0% | 192 |
| surprise | 17.7% | 13.0% | 15.0% | 192 |

Confusion matrix:

| true ↓ / predicted → | anger | disgust | fear | neutral | surprise |
|---|---|---|---|---|---|
| **anger** | 7 | 0 | 0 | 149 | 36 |
| **disgust** | 0 | 4 | 1 | 175 | 12 |
| **fear** | 2 | 3 | 0 | 146 | 41 |
| **joy** | 0 | 1 | 1 | 169 | 21 |
| **neutral** | 0 | 0 | 0 | 288 | 0 |
| **sadness** | 0 | 1 | 1 | 184 | 6 |
| **surprise** | 0 | 0 | 0 | 167 | 25 |

## Latency (end of speech → new emotion)

|  | Median | p90 | p95 | Max |
|---|---|---|---|---|
| First reaction (words) | 817 ms | 830 ms | 835 ms | 1140 ms |
| Words + voice | 1255 ms | 1413 ms | 1473 ms | 1770 ms |

When the voice model is still working after the words are scored, the app shows the words-only result first and replaces it with the combined one when the voice is done.

= 400 ms waiting for the pause that ends a sentence + processing (median 855 ms: speech-to-text 394 ms and voice 855 ms in parallel, then text emotion 22 ms). The overlay's fade (300 ms by default) and the localhost WebSocket hop (well under a millisecond) come on top.
