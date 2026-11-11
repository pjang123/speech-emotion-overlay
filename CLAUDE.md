# CLAUDE.md

Real-time speech emotion -> user image overlay (OBS Browser Source). Full brief: `SPEC.md`.

## Status
- Phase 1 (pipeline MVP) built and verified by the user on real mic + overlay (2026-11-12). Phase 2 (smoothing/hysteresis/cooldown, rotation, tiers, transitions, zip export) next. Wait for user approval between phases.

## Commands
- Install: `uv sync` (or `pip install -r requirements.txt`; regenerate with `uv export --no-hashes --no-dev -o requirements.txt`)
- Run: `uv run python -m emotion_overlay` (setup at http://127.0.0.1:8765/setup, overlay at /overlay). `--no-audio` for UI only, `--log-level DEBUG` to audit network calls.
- Test: `uv run python -m pytest` (NOT `uv run pytest`: Smart App Control blocks the pytest.exe shim on the dev machine)
- Lint/format: `uv run ruff check .` / `uv run ruff format .`

## Layout
- `src/emotion_overlay/`: `audio/` (capture, VAD segmenter), `asr.py` (two Whisper backends), `classify/` (text model, rolling window), `state_machine.py` (pure display logic), `pipeline.py` (worker threads), `images.py` (validated store), `server/` (FastAPI app, hub, display, security middleware), `model_cache.py` (cache-first loading)
- `web/`: vanilla JS/CSS, no build step. `web/common/socket.js` is shared.
- `tests/`: pytest; `conftest.make_image()` builds test images.

## Conventions
- Python 3.11+ (dev machine: 3.12). Type hints everywhere, small focused modules. No PEP 695 generics (3.11 support).
- State machine stays pure (no I/O, `now` passed in) and unit-tested. It holds a config reference so live edits apply.
- Server binds `127.0.0.1`. Security middleware enforces Host + Origin checks. Keep it when adding routes/WebSockets.
- Never write audio or transcripts to disk unless a `debug.*` flag is set. Transcripts are logged at DEBUG level only.
- Load HF models via `model_cache.load_cached` so cached starts make zero network calls.
- Uploads: validate by content (Pillow, 4 formats), size/pixel limits, random IDs, client filename is display-only.
- Frontend: build DOM with `textContent`/properties, never `innerHTML`. CSP allows only `'self'` scripts/styles.
- Heavy imports (torch, transformers, faster_whisper, sounddevice) stay inside functions so the server/tests import fast.

## Dev machine gotchas
- Windows Smart App Control blocks CTranslate2 (faster-whisper) and uv's `.exe` shims. `asr.backend: auto` falls back to transformers Whisper.
- Repo lives in OneDrive; `.venv` and `data/` get synced. The user may move it.
