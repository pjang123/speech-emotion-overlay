# CLAUDE.md

Real-time speech emotion -> user image overlay (OBS Browser Source). Full brief: `SPEC.md`.

## Status
- Phase 1 (pipeline MVP) built and verified by the user on real mic + overlay (2026-11-12).
- Phase 2 (stability rules, rotation, tiers, transitions, live settings, zip backup) built and used by the user. Added on request: Stop/Start listening, settings reset.
- Phase 3 (voice emotion model, originally superb/hubert-base-superb-er, late fusion, label_map, calibration) built. User's own calibration: words 79%, voice 36%, combined 79% before/after.
- Added on request: personalised calibration (`personalize.py`) that profiles the speaker from the 14 lines and recommends settings with reasons; user ticks which to apply (`POST /api/calibration/save {"apply": [paths]}`).
- Added on request: word triggers (`triggers.py`, display override + `hub.call_later` to end it) and GIFs from links (`fetch.py`, SSRF-guarded; follows Tenor/Giphy og:image once with Accept: image/* only). Image group "trigger" holds trigger-only images.
- Added on request: slang (`slang.py`, one table). Triggers match via `slang.canonical` + stretched letters (single letter = 1 or 3+, so "god" != "good"), apostrophes ignored. `classifier.slang` (live, default on) runs `plain_english` before the text model; each meaning was checked against the real model. Whisper vocabulary prompts were tried and rejected: they hallucinate the prompt words on noise.
- Added on request: Windows installer (2026-11-12). `packaging/` (PyInstaller spec, launcher, Inno Setup script) + `.github/workflows/windows-installer.yml` builds and tests it on GitHub's runners. Unsigned, models downloaded on first run (user's choices). The installed app keeps config/data in `%LOCALAPPDATA%\EmotionOverlay` (`paths.py`). `--self-test` loads and runs every model without a mic.
- Phase 4 (eval tooling, RAVDESS results, README) done. Pending from the user: own recordings (`eval record` + `eval run mine`) and a demo GIF for the README.
- Key results (RAVDESS, see eval/results/ravdess/report.md): voice model answers "ang" for 79% of clips (domain shift; level normalisation and resampling ruled out); CV-best voice weight 0.6 there, but default stays 0.25 because real speech carries emotion in words. Latency median 712 ms / p95 868 ms (faster-whisper, CPU).
- Voice model switched (2026-11-12, on request) to `tiantiaf/wavlm-large-categorical-emotion` (Vox-Profile, MSP-Podcast, 9 labels; `classify/vox_profile.py` rebuilds its inference path, no extra deps). transformers 5 returns the last hidden state *before* the final layer norm; the model's layer weights expect it after (`layer_states`), and getting this wrong silently skews predictions toward anger/sadness. Old configs holding the old default are upgraded via `config_version` (`config._migrate`); calibrations fitted to other voice labels are ignored (`CalibrationStore.stale`). It's slower (~800 ms vs ~400 ms processing on CPU alongside Whisper), so the pipeline shows the words-only result first when the voice is still running. int8 dynamic quantisation of its feed-forward layers was 33% faster at equal accuracy but uses a deprecated torch API, so not used.
- Added on request: mic remembered by name (`audio.device_name/device_hostapi`, `capture.pick_device`), manual emotion hold (`DisplayState.set_manual`, `PUT /api/manual`, Windows hotkeys via `RegisterHotKey` in `hotkeys.py`, Ctrl+Alt+Shift+1..7/0), interim updates while talking (`classifier.interim_s`, `pipeline.Interim`).
- Added on request: CPU/GPU choice. Setup page "Processor" -> `PUT /api/compute` sets `asr/classifier/audio_emotion.device` and `Pipeline.reload_models()` swaps models in the background (old ones serve until the new are ready). `compute.py` decides the device (asked-for GPU that's unusable -> CPU with a warning). GPU build is the optional `gpu` extra (`uv sync --extra gpu`, torch from the cu130 index; `cpu`/`gpu` extras declared conflicting so the default lock keeps CPU torch from PyPI). Defaults are now `auto`. Speech-to-text order: faster-whisper GPU, Whisper via torch GPU, faster-whisper CPU, torch CPU.
- User decision: the image should change freely with every sentence. Anti-flicker rules exist but default OFF; don't turn them on by default.

## Commands
- Install: `uv sync` (GPU: `uv sync --extra gpu`) (or `pip install -r requirements.txt`; regenerate with `uv export --no-hashes --no-dev -o requirements.txt`)
- Run: `run-cpu.bat` / `run-gpu.bat` (GPU env at `%LOCALAPPDATA%\emotion-overlay\gpu-venv`; always `uv run --extra gpu` there, a plain `uv run` reinstalls CPU torch), or `uv run python -m emotion_overlay` (setup at http://127.0.0.1:8765/setup, overlay at /overlay). `--no-audio` for UI only, `--log-level DEBUG` to audit network calls.
- Evaluate: `uv run python -m emotion_overlay.eval {download ravdess | record | run ravdess|mine | report ravdess|mine}`; pass `--config config.example.yaml` for reproducible (default) settings
- Installer: built and tested in CI (push to `packaging/**` or workflow_dispatch) but never uploaded or released: it's unsigned and Smart App Control blocks it. User decision (2026-11-12): don't publish releases or build files. Local: `uv sync --group build`, `uv run python -m PyInstaller packaging/emotion_overlay.spec` (use `--workpath`/`--distpath` outside OneDrive). Smart App Control blocks running the unsigned exe on the dev machine, so it can only be tested in CI.
- Test: `uv run python -m pytest` (NOT `uv run pytest`: Smart App Control blocks the pytest.exe shim on the dev machine)
- Lint/format: `uv run ruff check .` / `uv run ruff format .`

## Layout
- `src/emotion_overlay/`: `audio/` (capture, VAD segmenter), `asr.py` (two Whisper backends), `classify/` (text model, voice model, rolling window, `fusion.py` = pure scoring math), `calibration.py` (prompts, bias fitting with leave-one-out accuracy, session, store; `server/calibration.py` drives it live), `personalize.py` (speaking profile -> recommended settings; pure, conservative rules), `state_machine.py` (pure display logic), `pipeline.py` (worker threads), `images.py` (validated store, tiers), `settings.py` (live-editable config whitelist), `backup.py` (zip export/import), `server/` (FastAPI app, hub, display = image picker, security middleware), `model_cache.py` (cache-first loading)
- `web/`: vanilla JS/CSS, no build step. `web/common/socket.js` is shared.
- `tests/`: pytest; `conftest.make_image()` builds test images.

## Conventions
- Python 3.11+ (dev machine: 3.12). Type hints everywhere, small focused modules. No PEP 695 generics (3.11 support).
- State machine stays pure (no I/O, `now` passed in) and unit-tested. It holds a config reference so live edits apply.
- Live settings are copied into the existing config section objects (`settings.commit_settings`), never by replacing them, because components hold references. Only sections in `settings.EDITABLE` are changeable at runtime or via import.
- Imports are untrusted: never extract by zip path, cap bytes actually read, re-validate images, validate everything before mutating.
- Voice model labels never leak past `fusion.map_voice_scores`; everything downstream uses canonical EMOTIONS. Each emotion appears once in `label_map` (calibration needs the inverse).
- Don't evaluate on RAVDESS with a model trained on it (why ehcalabres/wav2vec2-lg-xlsr was rejected).
- Pipeline tests must set `_voice_state = "failed"` (or a fake `_voice`) or they start loading the real voice model.
- Outbound HTTP only via `fetch.fetch_image` (public-address check per hop). Tests that need a local HTTP server pass `check_host=allow_all`.
- Overlay restarts GIFs via the `play` counter in state messages (`?play=N` on the image URL); keep bumping it whenever a new image starts showing.
- FastAPI route order matters: literal paths (`/api/images/default`) before `{image_id}` routes.
- Server binds `127.0.0.1`. Security middleware enforces Host + Origin checks. Keep it when adding routes/WebSockets.
- Never write audio or transcripts to disk unless a `debug.*` flag is set. Transcripts are logged at DEBUG level only.
- Load HF models via `model_cache.load_cached` so cached starts make zero network calls.
- Uploads: validate by content (Pillow, 4 formats), size/pixel limits, random IDs, client filename is display-only.
- Frontend: build DOM with `textContent`/properties, never `innerHTML`. CSP allows only `'self'` scripts/styles.
- Resolve bundled files via `paths.web_dir()` (not `__file__`), and new runtime data files need adding to `packaging/emotion_overlay.spec`. Transformers model families beyond whisper/roberta/hubert/wav2vec2/wavlm need their `collect_submodules` line there too.
- Heavy imports (torch, transformers, faster_whisper, sounddevice) stay inside functions so the server/tests import fast.

## Dev machine gotchas
- Windows Smart App Control blocks CTranslate2 (faster-whisper) and uv's `.exe` shims. `asr.backend: auto` falls back to transformers Whisper.
- Repo lives in OneDrive; `.venv` and `data/` get synced. The user may move it.
