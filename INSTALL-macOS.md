# Installing on a Mac

This guide covers Macs with **Apple Silicon** (M1 or newer) on **macOS 14 Sonoma or newer**. For Windows, see [INSTALL-Windows.md](INSTALL-Windows.md); for Linux, [INSTALL-Linux.md](INSTALL-Linux.md). Once the app is running, the [README](README.md) explains how to set it up and use it.

> [!NOTE]
> The Mac version has been tested on GitHub's Apple Silicon test machines: installing, every model, the test suite and a full run of the app. Those are virtual machines without a usable GPU, so **using the Mac's GPU hasn't been tested on a real Mac yet**. If it gives you trouble, choose **Processor → CPU** on the setup page.

## What you need

- **A Mac with Apple Silicon** (M1, M2, M3, M4 or newer) and **macOS 14 or newer**. Intel Macs can't run the app: PyTorch, which runs the models, no longer makes builds for them. To check, open the Apple menu → **About This Mac**: it says **Chip: Apple M…** on Apple Silicon.
- **A microphone.**
- **An internet connection** for the first start. After that the app works offline.
- **About 3 GB of free disk space**, plus about 1.6 GB more for the larger speech-to-text model the first time the GPU is used.

**The Mac's GPU is used automatically.** On Apple Silicon the CPU and GPU are on the same chip and share memory, and the app runs its models on the GPU part through Apple's Metal. That's faster and lets it use the larger, more accurate speech-to-text model. Nothing extra needs installing.

**Global hotkeys are Windows-only.** On a Mac, use the setup page's buttons to hold an emotion, or a Stream Deck through the HTTP API (see [Hotkeys and Stream Deck](README.md#hotkeys-and-stream-deck)).

## Step 1: Download the app

Either:

- **Without git:** on the GitHub page, click the green **Code** button, then **Download ZIP**. Safari unzips it by itself; otherwise double-click the ZIP in **Downloads**. Move the folder somewhere you'll find again, like Documents.
- **With git:** in Terminal,

  ```sh
  git clone https://github.com/pjang123/speech-emotion-overlay.git
  ```

## Step 2: Start it

The simplest way, which works on every macOS version:

1. Open **Terminal** (press **⌘ Space**, type *Terminal*, press Return).
2. Type `bash ` (with a space after it), then drag **`run-mac.command`** from the app's folder into the Terminal window, and press Return.

Or double-click **`run-mac.command`** in Finder. The first time, macOS blocks it because it isn't signed by a registered developer:

- **macOS 15 Sequoia or newer:** click **Done**, then open **System Settings → Privacy & Security**, scroll down to the message about `run-mac.command`, click **Open Anyway** and confirm.
- **macOS 14 Sonoma:** Control-click (or right-click) the file, choose **Open**, then **Open** again.

After that, double-clicking works.

What happens next:

- **The first start takes several minutes.** The launcher installs [uv](https://docs.astral.sh/uv/), which installs Python and everything else the app needs, and then the app downloads its models. Later starts take a few seconds.
- **macOS asks whether Terminal may use the microphone.** Click **Allow**. The app hears nothing without this.
- When the window shows `Setup page: http://127.0.0.1:8765/setup`, open that address in your browser.

Keep the Terminal window open while you use the app. Closing it (or pressing Control-C) quits the app. Then continue with the [Beginner's guide](README.md#beginners-guide) in the README, from Step 3: everything from there works the same on a Mac.

### What the launcher installs, and where

| What | Where |
|---|---|
| uv | `~/.local/bin` |
| The Python environment (PyTorch with Metal included) | `.venv` in the app's folder |
| The models (about 2 GB, plus 1.6 GB for the GPU speech-to-text model) | `~/.cache/huggingface` |
| Your settings and images | `config.yaml` and `data/` in the app's folder |

The launcher passes options on to the app, for example `bash run-mac.command --self-test`.

## Step 3: Check it works

In Terminal, in the app's folder:

```sh
bash run-mac.command --self-test
```

It loads every model, runs each once without using the microphone, and names the step that fails if something is wrong. The setup page's **Processor** card shows your chip's GPU (for example "Apple M2 GPU (Metal)") when it's found.

## Installing by hand (optional)

The launcher is optional. With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, in the app's folder:

```sh
uv sync
uv run python -m emotion_overlay
```

On a Mac there's no `--extra` to choose: the normal PyTorch build already includes Metal.

Without uv: create a virtual environment with Python 3.11–3.14, then run `pip install .` in the app's folder.

## Updating

- **ZIP:** download the new ZIP. Then copy `config.yaml` and the `data` folder from your old folder into the new one, to keep your settings, images, triggers and calibration.
- **git:** run `git pull` in the app's folder.

The launcher updates the installed packages on its next start.

## Uninstalling

1. Delete the app's folder. This also deletes your settings and images, so export a backup first if you want to keep them (**Backup → Export setup** on the setup page).
2. Delete the downloaded models: in `~/.cache/huggingface/hub` (in Finder, **Go → Go to Folder…**), the folders whose names contain `whisper`, `emotion-english-distilroberta` and `wavlm-large-categorical-emotion`. Leave the rest if other programs use Hugging Face models.
3. Optional: uninstall uv with `uv self uninstall`, if nothing else uses it.

## Mac problems

**The app never says "Speaking", or the transcript stays empty.** macOS gives the app silence until Terminal may use the microphone. Open **System Settings → Privacy & Security → Microphone**, turn on **Terminal**, then quit Terminal completely (⌘Q) and start the app again.

**"run-mac.command can't be opened" or "Apple could not verify…".** See Step 2: allow it once in **Privacy & Security**, or start it with `bash run-mac.command` in Terminal, which is never blocked.

**"Permission denied" when double-clicking.** The ZIP lost the file's "executable" flag. Start it with `bash run-mac.command` instead, or run `chmod +x run-mac.command` once in the app's folder.

**"Emotion Overlay needs a Mac with Apple Silicon".** This is an Intel Mac, which can't run PyTorch's current builds.

**"PyTorch can't use this Mac's GPU".** The app runs on the CPU instead, which works but is slower. This happens on macOS older than 14, and in virtual machines.

**The GPU gives errors or wrong results.** Choose **Processor → CPU** on the setup page, and please report what happened: GPU support hasn't been tested on a real Mac yet.

**Port 8765 is already in use.** Set `server.port` in `config.yaml` to another number, and use the new port in OBS too.

More problems and fixes are in the README's [Troubleshooting](README.md#troubleshooting) section.
