# Project Brief: Real-Time Speech Emotion Image Overlay

Read this whole brief before writing any code. Then reply with (1) any questions or concerns, (2) a proposed repo structure, and (3) a plan for Phase 1. Wait for my approval before building. Save this brief in the repo as `SPEC.md` and keep a short `CLAUDE.md` with conventions and commands as the project evolves.

## 1. What we're building

A local application that listens to a person speaking through their microphone, detects the emotion in their speech in near real time, and displays an image the user has chosen for that emotion. The user uploads their own images and assigns them to emotions. Nothing is generated on the fly.

Primary use case: streamers and PNGTubers who want their on-screen image to react to their emotion automatically, without a webcam or manual hotkeys. The display must work as an OBS Browser Source. Secondary use case: a general standalone web page anyone can open.

This is also a portfolio project for an entry-level IT/security job search, so code quality, a clear README, measured results, and a privacy-conscious design all matter as much as features.

## 2. Existing tools (context, do not copy)

- (AI)terEgo: desktop PNGTuber app that changes avatar expression from voice tone.
- TuBERT: Princeton thesis model for multimodal speech emotion recognition aimed at PNGTubers, with a very basic GUI.
- Emotion-Avatar-OBS-script: webcam facial emotion to OBS images. Users complained about flickering between emotions and poor calibration.

Our differentiators: user-supplied images, anti-flicker smoothing, per-user calibration, intensity tiers, measured accuracy and latency, and fully local processing.

## 3. Architecture (proposed, push back if you see a better option)

**Local Python backend + browser frontends.**

Reasons:
- OBS Browser Sources run in their own embedded browser, so they cannot share browser storage or mic access with a normal browser tab. A local server is the simplest way to share state and images between a setup page and the OBS overlay.
- Chrome's Web Speech API sends audio to Google's servers, which conflicts with the "audio never leaves the machine" goal.
- Python gives access to better speech and emotion models.

Components:
- **Backend:** Python 3.11+, FastAPI, WebSockets, Uvicorn.
  - Mic capture with `sounddevice` (user selects the input device so game audio is excluded).
  - Voice activity detection (e.g. Silero VAD or webrtcvad) to segment speech and detect silence.
  - Streaming transcription with `faster-whisper` (default model `small` or `base`, configurable; CPU must work, GPU optional).
  - Text emotion classification with a Hugging Face model such as `j-hartmann/emotion-english-distilroberta-base` (labels: anger, disgust, fear, joy, neutral, sadness, surprise). Verify the model exists and its labels before relying on it.
  - Phase 3: an audio-based speech emotion recognition model (e.g. a wav2vec2 model fine-tuned on emotion data) fused with the text scores.
  - Smoothing and state machine (see section 5).
  - Image storage on local disk, served by the backend.
- **Setup page** (`/setup`): opened in a normal browser. Upload and manage images, pick mic device, adjust settings, run calibration, view live debug info (transcript, scores, current state).
- **Overlay page** (`/overlay`): the URL added to OBS as a Browser Source. Transparent background, shows only the current image with transitions, subscribes to the backend via WebSocket. Must also look fine as a standalone page.
- **Frontend tech:** your choice, but keep it light. Vanilla JS or a small framework like Svelte/Preact with Vite. No heavy UI libraries.

Bind the server to `127.0.0.1` by default. Never store audio or transcripts to disk unless a debug flag is explicitly enabled.

## 4. Features by phase

### Phase 1: Working pipeline (MVP)
- Mic device selection and capture.
- VAD + streaming transcription.
- Text emotion classification on a rolling window (last ~5 to 10 seconds of transcript, or per utterance).
- Emotion state pushed over WebSocket.
- Setup page: upload images per emotion (multiple images per emotion allowed), set a neutral/default image.
- Overlay page: displays the image for the current emotion with a cross-fade.
- Basic config file (YAML or JSON) for model choice, window size, thresholds.

**Done when:** I can talk into my mic and the overlay changes image to match obviously happy, angry, or sad sentences within about 2 seconds, and shows the neutral image during silence.

### Phase 2: Stability and polish
- Smoothing: exponential moving average over emotion scores.
- Hysteresis: a new emotion must win for N consecutive windows or exceed a confidence margin before switching.
- Minimum display time per emotion (cooldown), configurable.
- Confidence threshold: below it, hold the current image or fall back to neutral (configurable).
- Silence handling: after X seconds of silence, return to neutral (configurable).
- Image rotation: when several images exist for one emotion, choose randomly or cycle.
- Intensity tiers: optional "mild" and "strong" image per emotion, chosen by score.
- Transition options: cross-fade, cut, and a small bounce or scale effect.
- Export and import of a full setup (config + images) as a zip.

**Done when:** a normal 2 minute monologue does not cause visible flicker, and settings changes on the setup page apply live without restarting.

### Phase 3: Accuracy
- Audio emotion model running in parallel with the text model.
- Late fusion of text and audio scores with a configurable weight.
- Calibration flow: the user reads short prompts in each emotion; store per-user adjustments (e.g. score offsets or a small classifier on top of the model outputs).
- Handle the label mismatch between audio and text models with an explicit mapping.

### Phase 4: Evaluation and portfolio
- Evaluation script that runs labeled audio clips through the pipeline and reports per-class precision, recall, F1, a confusion matrix, and latency (speech end to image change), comparing text-only, audio-only, and fused.
- Use a public dataset (e.g. RAVDESS or CREMA-D) plus a small set of my own recordings. Note in the results that acted datasets overestimate real-world accuracy.
- README with: demo GIF, architecture diagram, setup instructions, OBS setup guide, results table, privacy section, and known limitations.

## 5. Emotion state machine (important)

Raw model output is noisy. The displayed emotion must come from a clear, testable state machine, separate from the models:

```
raw scores -> smoothing (EMA) -> candidate emotion
candidate must: exceed confidence threshold
              AND beat current emotion by margin OR persist for N windows
              AND current emotion has been shown for >= min display time
-> switch displayed emotion
silence for > T seconds -> neutral
```

Put this in its own module with unit tests covering flicker, ties, silence, and low confidence.

## 6. Non-functional requirements

- **Latency:** target under 2 seconds from end of phrase to image change on a mid-range CPU. Log timings for each stage.
- **Privacy:** no audio or transcript persisted by default, no network calls except downloading models on first run, server on localhost only. Document this.
- **Security:** validate uploaded files (type, size limit, safe filenames, no path traversal), do not execute or trust uploaded content, no secrets in the repo.
- **Cross-platform:** Windows first (most streamers), but avoid OS-specific code where possible.
- **Setup:** one command to install (`pip install -r requirements.txt` or `uv`) and one to run. Models download automatically with a clear progress message.
- **Code quality:** type hints, small focused modules, `pytest` tests for the state machine and API, linting with `ruff`, consistent formatting.
- **Git:** commit at logical checkpoints with clear messages. Include `.gitignore` for models, uploads, and virtual environments.

## 7. Non-goals (for now)

- No AI image generation.
- No webcam or facial expression detection.
- No cloud hosting or user accounts.
- No Live2D or rigged avatars (static images and GIFs only).
- No languages other than English for v1, but don't hardcode English in a way that blocks adding more later.

## 8. How I want you to work

- Before Phase 1, verify that the chosen models and libraries exist, are maintained, and run on CPU. Tell me if anything in this brief is outdated or a bad idea.
- Work one phase at a time. At the end of each phase, summarize what was built, how to run it, how to test it, and any known issues, then wait for me before starting the next phase.
- Prefer simple, working solutions over clever ones. Explain any non-obvious design decision in a short comment or in the README.
- When something needs my hardware to verify (mic, OBS), tell me exactly what to test and what to look for.
