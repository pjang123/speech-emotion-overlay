# Emotion Overlay

Listens to your microphone, works out the emotion in what you're saying, and shows an image you picked for that emotion. Built for streamers and PNGTubers: add one URL to OBS as a Browser Source, and your on-screen image reacts to you with no webcam and no hotkeys.

Everything runs locally. Audio never leaves your machine.

> **Status:** Phase 1 (working pipeline). Anti-flicker smoothing, intensity tiers, audio-based emotion, calibration and measured accuracy are next. See [SPEC.md](SPEC.md).

## How it works

```
mic ─▶ Silero VAD ─▶ Whisper ─▶ text emotion model ─▶ state machine ─▶ WebSocket ─▶ overlay page (OBS)
       (utterances)  (speech     (7 emotion scores    (threshold,                    setup page (debug view)
                      to text)    on recent text)      silence → neutral)
```

- **Voice activity detection** ([Silero VAD](https://github.com/snakers4/silero-vad)) splits audio into utterances. One ends after a 400 ms pause. Long speech is cut every 6 s so the image still updates mid-rant.
- **Speech-to-text:** Whisper `base.en` via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), or via 🤗 transformers when faster-whisper can't load (see [Windows notes](#windows-notes)).
- **Emotion:** [`j-hartmann/emotion-english-distilroberta-base`](https://huggingface.co/j-hartmann/emotion-english-distilroberta-base) scores the last ~6 s of transcript. The 7 labels are anger, disgust, fear, joy, neutral, sadness and surprise.
- **State machine** (`state_machine.py`): pure, unit-tested logic that decides what is actually displayed. Models never drive the image directly.
- **Server:** FastAPI on `127.0.0.1:8765`, which serves the pages, the REST API for images and settings, and a WebSocket event stream.

## Install

You need Python 3.11+ on Windows, macOS or Linux, and [uv](https://docs.astral.sh/uv/) (recommended) or pip.

```sh
uv sync                      # or: pip install -r requirements.txt
```

## Run

```sh
uv run python -m emotion_overlay      # or: python -m emotion_overlay
```

The first start downloads the models (~400 MB, one time). After that, startup is offline.

1. Open **http://127.0.0.1:8765/setup**.
2. Pick your microphone and click **Use this mic**.
3. Add images for each emotion. Click ★ on one to make it the default, which is shown for emotions that have no images of their own.
4. Talk. The **Live** panel shows what was heard, the emotion scores and timings.

### Add to OBS

1. **Sources → + → Browser**.
2. URL: `http://127.0.0.1:8765/overlay`. Width and height: your image size (e.g. 800 × 800).
3. Leave custom CSS empty. The page background is already transparent.

URL options: `?fit=cover` fills the source and crops instead of letterboxing. `?label=1` shows the emotion name, which helps when testing.

### Options

| Flag | Purpose |
|---|---|
| `--config PATH` | Config file (default `config.yaml`; every key is optional). See [`config.example.yaml`](config.example.yaml). |
| `--no-audio` | Serve the web UI without loading models or opening the mic. |
| `--log-level DEBUG` | Verbose logs, including any HTTP requests (useful to verify the privacy claims below). |

## Privacy

- **Audio stays in memory.** Utterances are processed and discarded. Nothing is written to disk unless you turn on `debug.save_audio` or `debug.save_transcripts`.
- **Transcripts aren't logged** at the default log level. Only timings and the detected emotion are.
- **No network after first run.** Models load from the local cache first, and the Hub is contacted only if a model is missing. Telemetry and transformers' background "safetensors conversion" lookups are disabled. To check, run with `--log-level DEBUG` and look for HTTP requests (there are none).
- **The overlay never receives transcripts.** Only the setup page subscribes to debug data.

## Security

The server binds to `127.0.0.1`, so other computers can't reach it. Web pages open in your own browser can still send requests to localhost, so the server also defends against that:

| Threat | Mitigation |
|---|---|
| A website reads your live transcript over the WebSocket (cross-site WebSocket hijacking) | WebSocket connections from any other `Origin` are refused |
| A website uploads, deletes or changes settings (CSRF) | `POST`/`PUT`/`DELETE` from another origin get 403 |
| DNS rebinding | Requests with a `Host` header other than localhost get 400 |
| Malicious upload (script, SVG, polyglot, decompression bomb) | Type is decided by parsing the content with Pillow, limited to PNG/JPEG/WebP/GIF. Size limit 10 MB, pixel limit 4096². Served with a fixed MIME type and `nosniff` |
| Path traversal via filename or ID | Files are stored under random IDs. Client filenames are display-only. IDs are regex-validated and looked up in the manifest, never used as paths |
| XSS via filenames or transcripts | The frontend never uses `innerHTML`. A strict CSP allows `'self'` scripts only |

## Windows notes

**Smart App Control** blocks CTranslate2's native library, which faster-whisper depends on. Instead of asking you to turn off an OS security feature, `asr.backend: auto` detects this and falls back to running Whisper through transformers/PyTorch. That backend is about 1.2 s per utterance on CPU instead of a few hundred ms. The setup page shows which backend is active.

## Development

```sh
uv run python -m pytest     # tests
uv run ruff check .         # lint
uv run ruff format .        # format
```

`uv run pytest` may be blocked by Smart App Control (it launches an unsigned `.exe`). `python -m pytest` works.

## Known limitations (Phase 1)

- **Flicker:** each utterance can switch the image. Smoothing, hysteresis and minimum display time come in Phase 2.
- **Text only:** emotion is read from the words, not the tone. "Great, just great" said sarcastically reads as joy. An audio emotion model comes in Phase 3.
- **English only** for now. The language is a config value, not hard-coded, but the default emotion model is English.
- **Microphone by index:** if you plug in or remove audio devices, re-select your mic on the setup page.
