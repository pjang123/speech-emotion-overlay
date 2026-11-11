import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from conftest import make_image
from emotion_overlay.backup import BackupError, export_setup, read_setup
from emotion_overlay.config import AppConfig
from emotion_overlay.images import ImageStore

LIMITS = {"max_image_bytes": 200_000, "max_pixels": 10**6, "max_total": 1_000_000}
IMG = "images/" + "a" * 32 + ".png"


def make_zip(files: dict[str, bytes | str], compression: int = zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def manifest(*entries: dict[str, object], default: str | None = None) -> str:
    return json.dumps({"version": 1, "default_image": default, "images": list(entries)})


def entry(file: str, emotion: str = "joy") -> dict[str, object]:
    return {"id": "a" * 32, "emotion": emotion, "file": file}


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path / "src", max_bytes=200_000, max_pixels=10**6)


def test_export_then_read_roundtrip(store: ImageStore) -> None:
    a = store.add(make_image(), "joy", "happy.png", tier="strong")
    store.add(make_image("GIF"), "anger", "mad.gif")
    store.set_default(a.id)
    config = AppConfig()
    config.display.transition = "bounce"

    bundle = read_setup(export_setup(store, config), **LIMITS)
    assert [(i.emotion, i.tier, i.original_name) for i in bundle.images] == [
        ("joy", "strong", "happy.png"),
        ("anger", "any", "mad.gif"),
    ]
    assert bundle.images[0].data == store.path(a).read_bytes()
    assert bundle.default_index == 0
    assert bundle.settings["display"]["transition"] == "bounce"


def test_export_contains_no_locked_settings(store: ImageStore) -> None:
    with zipfile.ZipFile(io.BytesIO(export_setup(store, AppConfig()))) as zf:
        settings = yaml.safe_load(zf.read("settings.yaml"))
    assert not {"server", "debug", "asr", "uploads"} & set(settings)


def test_zip_slip_entries_are_never_read() -> None:
    data = make_zip(
        {"manifest.json": manifest(entry("../../evil.png")), "../../evil.png": make_image()}
    )
    with pytest.raises(BackupError, match="invalid file"):
        read_setup(data, **LIMITS)


def test_unlisted_entries_are_ignored() -> None:
    data = make_zip(
        {"manifest.json": manifest(entry(IMG)), IMG: make_image(), "run_me.bat": "echo pwned"}
    )
    assert len(read_setup(data, **LIMITS).images) == 1


def test_disguised_non_image_is_rejected() -> None:
    data = make_zip({"manifest.json": manifest(entry(IMG)), IMG: b"<script>alert(1)</script>"})
    with pytest.raises(BackupError, match="not a valid"):
        read_setup(data, **LIMITS)


def test_highly_compressed_entry_is_rejected() -> None:
    bomb = b"\0" * 300_000  # deflates to a few hundred bytes
    data = make_zip({"manifest.json": manifest(entry(IMG)), IMG: bomb}, zipfile.ZIP_DEFLATED)
    assert len(data) < 5_000
    with pytest.raises(BackupError, match="too large"):
        read_setup(data, **LIMITS)


def test_total_size_is_capped() -> None:
    files: dict[str, bytes | str] = {}
    entries = []
    for digit in "012":
        name = f"images/{digit * 32}.png"
        files[name] = make_image(size=(200, 200))
        entries.append(entry(name))
    files["manifest.json"] = manifest(*entries)
    one_image = len(files["images/" + "0" * 32 + ".png"])
    with pytest.raises(BackupError, match="in total"):
        read_setup(make_zip(files), 200_000, 10**6, max_total=one_image * 2)


@pytest.mark.parametrize(
    "data",
    [
        b"not a zip",
        make_zip({"settings.yaml": "state: {}"}),  # no manifest
        make_zip({"manifest.json": "{not json"}),
        make_zip({"manifest.json": manifest(entry(IMG, emotion="smug")), IMG: make_image()}),
        make_zip({"manifest.json": manifest(entry(IMG))}),  # listed file missing
        make_zip({"manifest.json": manifest(), "settings.yaml": "- just a list"}),
    ],
)
def test_malformed_setups_are_rejected(data: bytes) -> None:
    with pytest.raises(BackupError):
        read_setup(data, **LIMITS)
