@echo off
rem Emotion Overlay on an AMD Radeon GPU (Windows). Uses AMD's PyTorch build for ROCm
rem 7.2.1, which supports Radeon RX 7000 and 9000 series cards (and some Ryzen AI
rem processors) with AMD Software: Adrenalin Edition 26.2.2 or newer. Not yet tested on a
rem real AMD card. It lives in its own
rem environment outside this folder; the first run installs it (several GB). Then pick
rem Processor: Automatic or GPU on the setup page.
rem Extra arguments are passed on, e.g.  run-amd.bat --self-test
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

set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\emotion-overlay\amd-venv"
set "ROCM=https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1"
set "MARKER=%UV_PROJECT_ENVIRONMENT%\amd-rocm-7.2.1.installed"

rem Install (or update) everything except PyTorch from the lockfile; AMD's wheels are
rem for Python 3.12 only. --inexact keeps AMD's PyTorch, which the lockfile doesn't know.
uv sync --python 3.12 --inexact --no-install-package torch
if errorlevel 1 goto failed

if not exist "%MARKER%" (
    echo Note: AMD GPU support hasn't been tested on a real AMD card yet. If it doesn't
    echo work for you, use run-cpu.bat, and please report what happened.
    echo.
    echo Installing AMD's PyTorch for ROCm 7.2.1. This happens once and downloads several GB...
    uv pip install --python "%UV_PROJECT_ENVIRONMENT%" ^
        "%ROCM%/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl" ^
        "%ROCM%/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl" ^
        "%ROCM%/rocm-7.2.1.tar.gz" ^
        "%ROCM%/torch-2.9.1%%2Brocm7.2.1-cp312-cp312-win_amd64.whl"
    if errorlevel 1 goto failed
    type nul > "%MARKER%"
)

rem --no-sync: a plain "uv run" would swap AMD's PyTorch for the CPU build.
uv run --no-sync python -m emotion_overlay %*
if errorlevel 1 pause
exit /b

:failed
echo.
echo Installing AMD GPU support failed (see above). run-cpu.bat still works.
pause
exit /b 1
