@echo off
rem Emotion Overlay with the CPU build of PyTorch, in .venv next to this file.
rem Installs uv (the Python package manager) first if it isn't there yet.
rem Extra arguments are passed on, e.g.  run-cpu.bat --self-test
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

uv run python -m emotion_overlay %*
if errorlevel 1 pause
