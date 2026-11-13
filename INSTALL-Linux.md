# Installing on Linux

This guide covers 64-bit Linux on x86-64 (Ubuntu, Debian, Fedora, Arch and similar). For Windows, see [INSTALL-Windows.md](INSTALL-Windows.md). Once the app is running, the [README](README.md) explains how to set it up and use it.

Tested so far on Ubuntu 26.04: the CPU version, and an NVIDIA GPU (GeForce RTX 5070, under WSL 2). AMD GPUs should work as described below, but haven't been tested.

Differences from Windows:

- **No global hotkeys.** They use a Windows API. Use the setup page's buttons, or the HTTP API from a Stream Deck or a desktop shortcut (see [Hotkeys and Stream Deck](README.md#hotkeys-and-stream-deck)).
- **No launchers.** You start the app from a terminal with one command.

## What you need

- **A microphone**, and PipeWire or PulseAudio (standard on desktop Linux).
- **An internet connection** for the first start. After that the app works offline.
- **Free disk space:** about 3.5 GB for the CPU version, about 7 GB with NVIDIA GPU support, and 10 GB or more with AMD GPU support.
- **Optional: a graphics card** to make it faster.
  - **NVIDIA:** GTX 10 series or newer with the proprietary driver, version 580 or newer (check with `nvidia-smi`).
  - **AMD:** a GPU that ROCm 7.2 supports, such as Radeon RX 7000 or 9000 series (see [AMD's compatibility list](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/)).
  - About 4 GB of video memory is enough.

## Step 1: Install the system packages

The app needs PortAudio for the microphone, plus git and curl.

```sh
# Ubuntu, Debian, Linux Mint, Pop!_OS
sudo apt install libportaudio2 git curl

# Fedora
sudo dnf install portaudio git curl

# Arch, Manjaro
sudo pacman -S portaudio git curl
```

## Step 2: Install uv

[uv](https://docs.astral.sh/uv/) installs the right Python version and all the app's packages:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then open a new terminal so the `uv` command is found.

## Step 3: Download the app

```sh
git clone https://github.com/pjang123/speech-emotion-overlay.git
cd speech-emotion-overlay
```

## Step 4: Install and start

Choose one of the following. Each `uv sync` installs a different build of PyTorch into `.venv`, so use the matching `uv run` line every time you start the app.

**CPU:**

```sh
uv sync --extra cpu
uv run --extra cpu python -m emotion_overlay
```

On Linux, keep `--extra cpu`. Without it, uv installs PyTorch from PyPI, whose Linux build includes NVIDIA's CUDA libraries (about 4 GB more). That build still works on a CPU, but takes up more space.

**NVIDIA GPU:**

```sh
uv sync --extra gpu
uv run --extra gpu python -m emotion_overlay
```

**AMD GPU (ROCm 7.2):**

```sh
sudo usermod -aG render,video "$USER"   # once; then log out and back in
uv sync --extra amd
uv run --extra amd python -m emotion_overlay
```

Keep the same `--extra` option on every `uv run`. Without it, uv swaps in a different PyTorch build before starting.

The first start downloads the models: about 2 GB, plus 1.6 GB for the larger speech-to-text model used on a GPU. When the terminal shows `Setup page: http://127.0.0.1:8765/setup`, open that address in your browser. Then continue with the [Beginner's guide](README.md#beginners-guide) in the README, from Step 3. That guide is written for Windows, but everything from Step 3 onwards works the same way.

To stop the app, press **Ctrl+C** in the terminal.

## Step 5: Check it works

```sh
uv run --extra cpu python -m emotion_overlay --self-test    # or --extra gpu / --extra amd
```

It loads every model, runs each once without using the microphone, and names the step that fails if something is wrong. With a GPU, the setup page's **Processor** card shows its name when it's found.

## OBS on Linux

OBS Studio for Linux includes the Browser source. Add it as described in [Add the overlay to OBS](README.md#add-the-overlay-to-obs). If the Browser source is missing, your OBS package was built without it: install OBS from [Flathub](https://flathub.org/apps/com.obsproject.Studio) or [the OBS PPA](https://obsproject.com/download) instead.

## Start it from a desktop shortcut (optional)

Create `~/.local/share/applications/emotion-overlay.desktop`, replacing the folder path with yours and `--extra cpu` with the option you use:

```ini
[Desktop Entry]
Type=Application
Name=Emotion Overlay
Exec=sh -c 'cd "$HOME/speech-emotion-overlay" && "$HOME/.local/bin/uv" run --extra cpu python -m emotion_overlay --open-browser'
Terminal=true
```

## Updating

```sh
cd speech-emotion-overlay
git pull
uv sync --extra cpu    # or --extra gpu / --extra amd, whichever you use
```

Your settings and images (`config.yaml` and `data/` in the app's folder) are kept.

## Uninstalling

1. Delete the app's folder. This also deletes your settings and images, so export a backup first if you want to keep them (**Backup → Export setup** on the setup page).
2. Delete the downloaded models: in `~/.cache/huggingface/hub`, the folders whose names contain `whisper`, `emotion-english-distilroberta`, `wavlm-large-categorical-emotion` and `silero`. Leave the rest if other programs use Hugging Face models.
3. Optional: remove uv with `uv self uninstall`.

## Linux problems

**`OSError: PortAudio library not found`.** Install PortAudio (Step 1), then start the app again.

**The microphone list is empty or nothing is heard.** Check that the mic works in your system's sound settings. In the setup page's **Microphone** card, try the `pipewire`, `pulse` or `default` entries: they go through your sound server, which picks the mic set in your system settings. Inside a Flatpak or Snap terminal, the app may not be allowed to use the microphone; run it from a normal terminal.

**"The GPU is not available."** The setup page's **Processor** card says why:
- "PyTorch is CPU-only": you installed with `--extra cpu`. Run `uv sync --extra gpu` (or `--extra amd`), and start the app with the same option.
- "No NVIDIA GPU (or its driver) was found": install NVIDIA's proprietary driver, version 580 or newer, and check that `nvidia-smi` works.
- "No AMD GPU supported by ROCm was found": add yourself to the `render` and `video` groups (Step 4), then log out and back in. Check that your card is on AMD's ROCm compatibility list. A recent kernel's built-in `amdgpu` driver is usually enough. If it isn't, install AMD's driver as described in [AMD's ROCm guide](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/).

**Speech-to-text on an NVIDIA GPU logs "faster-whisper can't use the GPU".** That's expected unless NVIDIA's CUDA 12 cuBLAS and cuDNN libraries are installed. The app then runs Whisper through PyTorch on the GPU instead, which is fast too. AMD GPUs always use PyTorch, because faster-whisper only supports NVIDIA.

**Port 8765 is already in use.** Set `server.port` in `config.yaml` to another number, and use the new port in OBS too.

More problems and fixes are in the README's [Troubleshooting](README.md#troubleshooting) section.
