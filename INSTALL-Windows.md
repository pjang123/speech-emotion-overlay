# Installing on Windows

This guide covers Windows 10 and 11 (64-bit). For Linux, see [INSTALL-Linux.md](INSTALL-Linux.md). Once the app is running, the [README](README.md) explains how to set it up and use it.

## What you need

- **Windows 10 or 11, 64-bit.**
- **A microphone.**
- **An internet connection** for the first start. After that the app works offline.
- **Free disk space**, depending on where the models run:

  | Runs on | Launcher | Disk space |
  |---|---|---|
  | The CPU (any PC) | `run-cpu.bat` | about 3.5 GB |
  | An NVIDIA graphics card | `run-gpu.bat` | about 7 GB |
  | An AMD Radeon graphics card | `run-amd.bat` | 10 GB or more (AMD's PyTorch build is large) |

A graphics card makes the app react faster: about 0.55 s after you stop talking, against about 0.8 s on a CPU. It also uses a larger speech-to-text model that makes fewer mistakes with accents and noisy speech. The CPU version works well too.

**Which graphics cards work:**
- **NVIDIA:** GeForce GTX 10 series or newer with a recent driver (version 580 or newer; update it from the NVIDIA App or nvidia.com). About 4 GB of video memory is enough.
- **AMD:** Radeon RX 7000 or 9000 series, with **AMD Software: Adrenalin Edition 26.2.2 or newer**. This uses AMD's own PyTorch build for Windows ([AMD's guide](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/windows/install-pytorch.html)). AMD support is new and hasn't been tested on an AMD card yet. If it doesn't work, use `run-cpu.bat`.
- **Intel graphics** aren't supported. Use `run-cpu.bat`.

Not sure what you have? Press **Ctrl+Shift+Esc** to open Task Manager, then go to **Performance**. Your graphics card is listed as **GPU**.

## Step 1: Download the app

Either:

- **Without git:** on the GitHub page, click the green **Code** button, then **Download ZIP**. Right-click the downloaded file, choose **Extract All…**, then **Extract**. Move the folder somewhere you'll find again, like Documents.
- **With git:**

  ```powershell
  git clone https://github.com/pjang123/speech-emotion-overlay.git
  ```

## Step 2: Start it

Open the folder and double-click the launcher for your PC: `run-cpu.bat`, `run-gpu.bat` (NVIDIA) or `run-amd.bat` (AMD).

- If Windows shows **"Windows protected your PC"**, click **More info**, then **Run anyway**. This appears because the launchers aren't signed.
- A console window opens. **The first start takes several minutes.** The launcher installs [uv](https://docs.astral.sh/uv/), which installs Python and everything else the app needs, and then the app downloads its models. Later starts take a few seconds.
- When the window shows `Setup page: http://127.0.0.1:8765/setup`, open that address in your browser.

Keep the console window open while you use the app. Closing it quits the app. Then continue with the [Beginner's guide](README.md#beginners-guide) in the README, from Step 3.

### What the launchers install, and where

| Launcher | Python environment | Notes |
|---|---|---|
| `run-cpu.bat` | `.venv` in the app's folder | |
| `run-gpu.bat` | `%LOCALAPPDATA%\emotion-overlay\gpu-venv` | NVIDIA's CUDA build of PyTorch, about 3 GB |
| `run-amd.bat` | `%LOCALAPPDATA%\emotion-overlay\amd-venv` | AMD's ROCm build of PyTorch (Python 3.12), several GB |

The models (about 2 GB, plus 1.6 GB for the GPU speech-to-text model) are stored in `%USERPROFILE%\.cache\huggingface`. All three launchers share them, and they share your settings and images (`config.yaml` and `data\` in the app's folder). You can switch launchers at any time, but only run one at a time. If one is already running, starting another just opens its setup page.

Every launcher passes options on to the app, for example `run-gpu.bat --self-test`.

## Step 3: Check it works

Run the self-test from a terminal in the app's folder (or add `--self-test` to a shortcut):

```powershell
.\run-cpu.bat --self-test
```

It loads every model, runs each once without using the microphone, and names the step that fails if something is wrong. On a graphics card, the setup page's **Processor** card shows the card's name when it's found.

## Installing by hand (optional)

The launchers are optional. With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, in the app's folder:

```powershell
uv sync                                      # CPU
uv run python -m emotion_overlay

uv sync --extra gpu                          # NVIDIA GPU
uv run --extra gpu python -m emotion_overlay # keep --extra gpu, or uv puts the CPU build back
```

For AMD on Windows, use `run-amd.bat`. AMD's Windows build isn't on a package index that uv can lock, so the launcher installs it separately.

Without uv: create a virtual environment with Python 3.11 or newer, then run `pip install .` in the app's folder. For NVIDIA, first run `pip install torch --index-url https://download.pytorch.org/whl/cu130`.

## Updating

- **ZIP:** download the new ZIP and extract it. Then copy `config.yaml` and the `data` folder from your old folder into the new one, to keep your settings, images, triggers and calibration.
- **git:** run `git pull` in the app's folder.

The launchers update the installed packages on their next start.

## Uninstalling

1. Delete the app's folder. This also deletes your settings and images, so export a backup first if you want to keep them (**Backup → Export setup** on the setup page).
2. Delete `%LOCALAPPDATA%\emotion-overlay` (the GPU environments).
3. Delete the downloaded models: in `%USERPROFILE%\.cache\huggingface\hub`, the folders whose names contain `whisper`, `emotion-english-distilroberta`, `wavlm-large-categorical-emotion` and `silero`. Leave the rest if other programs use Hugging Face models.
4. Optional: uninstall uv with `uv self uninstall`, if nothing else uses it.

## Windows problems

**"An Application Control policy has blocked this file."** This is Smart App Control, which blocks unsigned native libraries that Windows doesn't know yet.
- If it blocks faster-whisper, the app switches to Whisper through PyTorch by itself. That's a little slower on the CPU, but works.
- If it blocks PyTorch in one environment, try another launcher. The NVIDIA environment's PyTorch also runs on the CPU: choose **Processor → CPU** on the setup page.

**The microphone list is empty, or nothing is heard.** Open **Settings → Privacy & security → Microphone** and turn on **Let desktop apps access your microphone**.

**"The GPU is not available."** The setup page's **Processor** card says why:
- "PyTorch is CPU-only": start the app with `run-gpu.bat` or `run-amd.bat` instead of `run-cpu.bat`.
- "No NVIDIA GPU (or its driver) was found" or "No AMD GPU supported by ROCm was found": update the graphics driver (for AMD, Adrenalin 26.2.2 or newer) and restart the app. AMD's Windows build supports Radeon RX 7000 and 9000 series cards only.

**The console window closes straight away, or `uv` isn't found.** Run the launcher from a terminal to read the error. If the uv install failed, install it by hand from [the uv website](https://docs.astral.sh/uv/getting-started/installation/), then try again.

**Port 8765 is already in use.** Set `server.port` in `config.yaml` to another number, and use the new port in OBS too.

More problems and fixes are in the README's [Troubleshooting](README.md#troubleshooting) section.
