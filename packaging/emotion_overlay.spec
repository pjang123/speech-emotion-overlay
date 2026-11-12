# PyInstaller spec for EmotionOverlay.exe (one-folder build, console window).
# Build from the repo root:  uv run python -m PyInstaller packaging/emotion_overlay.spec
# Models are not bundled; the app downloads them on first run into the Hugging Face cache.

from importlib.metadata import PackageNotFoundError
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 - defined by PyInstaller

datas = [(str(ROOT / "web"), "web")]
datas += collect_data_files("silero_vad")  # bundled VAD weights
datas += collect_data_files("faster_whisper")  # its own VAD assets
# transformers checks installed versions of its dependencies at import time.
for dist in (
    "transformers",
    "tokenizers",
    "huggingface-hub",
    "safetensors",
    "tqdm",
    "regex",
    "httpx",
    "packaging",
    "filelock",
    "numpy",
    "pyyaml",
    "torch",
    "faster-whisper",
    "ctranslate2",
    "silero-vad",
):
    try:
        datas += copy_metadata(dist)
    except PackageNotFoundError:
        pass  # not a dependency in this version

binaries = collect_dynamic_libs("ctranslate2")

hiddenimports = [
    *collect_submodules("uvicorn"),
    *collect_submodules("emotion_overlay"),
    # transformers loads model classes by name at runtime.
    *collect_submodules("transformers.models.auto"),
    *collect_submodules("transformers.models.whisper"),
    *collect_submodules("transformers.models.roberta"),
    *collect_submodules("transformers.models.hubert"),
    *collect_submodules("transformers.models.wav2vec2"),
    *collect_submodules("transformers.models.wavlm"),  # Vox-Profile voice model backbone
]

excludes = ["tkinter", "matplotlib", "IPython", "pytest", "notebook", "PyQt5", "PySide6"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EmotionOverlay",
    console=True,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="EmotionOverlay",
    upx=False,
)
