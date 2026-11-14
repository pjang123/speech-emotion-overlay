#!/bin/bash
# Emotion Overlay on a Mac with Apple Silicon (M1 or newer, macOS 14 or newer).
# Double-click this file in Finder, or run it from Terminal: bash run-mac.command
# Installs uv (the Python package manager) first if it isn't there yet. The Mac's
# GPU is used automatically; pick Processor: CPU on the setup page to turn it off.
# Extra arguments are passed on, e.g.  bash run-mac.command --self-test
cd "$(dirname "$0")" || exit 1

pause_on_error() {
    echo
    read -r -p "Press Enter to close this window." _
}

if [ "$(sysctl -in hw.optional.arm64)" != "1" ]; then
    echo "Emotion Overlay needs a Mac with Apple Silicon (M1 or newer)."
    echo "Intel Macs can't run it: PyTorch no longer makes builds for them."
    pause_on_error
    exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
    # Started under Rosetta (Intel emulation): run natively instead.
    exec arch -arm64 /bin/bash "$0" "$@"
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv isn't installed yet. Installing it from https://astral.sh/uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "Couldn't install uv. See https://docs.astral.sh/uv/getting-started/installation/"
    pause_on_error
    exit 1
fi

uv run python -m emotion_overlay "$@"
status=$?
if [ $status -ne 0 ]; then
    pause_on_error
fi
exit $status
