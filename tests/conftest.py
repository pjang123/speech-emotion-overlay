import io
from pathlib import Path

import pytest
from PIL import Image

from emotion_overlay.config import AppConfig


def make_image(fmt: str = "PNG", size: tuple[int, int] = (8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA" if fmt in {"PNG", "WEBP", "GIF"} else "RGB", size, (255, 0, 0, 255)).save(
        buf, format=fmt
    )
    return buf.getvalue()


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(data_dir=tmp_path / "data")
