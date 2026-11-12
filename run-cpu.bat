@echo off
rem Emotion Overlay with the CPU build of PyTorch, in .venv next to this file.
rem Extra arguments are passed on, e.g.  run-cpu.bat --self-test
cd /d "%~dp0"
uv run python -m emotion_overlay %*
