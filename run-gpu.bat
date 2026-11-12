@echo off
rem Emotion Overlay on an NVIDIA GPU. Uses its own environment outside this folder
rem (the CUDA build of PyTorch is ~3 GB, and keeping it apart means the CPU version in
rem .venv keeps working). The first run installs it. Then pick Processor: Automatic or
rem GPU on the setup page. Extra arguments are passed on, e.g.  run-gpu.bat --self-test
cd /d "%~dp0"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\emotion-overlay\gpu-venv"
uv run --extra gpu python -m emotion_overlay %*
