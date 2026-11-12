@echo off
rem Emotion Overlay on an NVIDIA GPU. Uses its own environment outside this folder
rem (the CUDA build of PyTorch is ~3 GB, and keeping it apart means the CPU version in
rem .venv keeps working). The first run installs it, and uv itself if needed. Then
rem pick Processor: Automatic or GPU on the setup page.
rem Extra arguments are passed on, e.g.  run-gpu.bat --self-test
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv isn't installed yet. Installing it from https://astral.sh/uv ...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
    echo Couldn't install uv. See https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\emotion-overlay\gpu-venv"
uv run --extra gpu python -m emotion_overlay %*
if errorlevel 1 pause
