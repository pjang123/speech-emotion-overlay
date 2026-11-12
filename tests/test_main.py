import socket
import sys
from pathlib import Path

import pytest

from emotion_overlay import main as main_module
from emotion_overlay import paths


def test_web_dir_from_source_has_pages() -> None:
    assert (paths.web_dir() / "setup" / "index.html").is_file()
    assert (paths.web_dir() / "overlay" / "index.html").is_file()


def test_web_dir_when_frozen_uses_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.is_frozen()
    assert paths.web_dir() == tmp_path / "web"


def test_user_dir_is_under_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.user_dir() == tmp_path / "EmotionOverlay"


def test_second_start_reports_running_instance_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(main_module.webbrowser, "open", opened.append)
    monkeypatch.setattr(main_module, "create_app", lambda *a, **k: pytest.fail("started twice"))
    with socket.socket() as running:
        running.bind(("127.0.0.1", 0))
        running.listen()
        port = running.getsockname()[1]
        config = tmp_path / "config.yaml"
        config.write_text(f"server:\n  port: {port}\n", encoding="utf-8")

        assert main_module.main(["--config", str(config), "--open-browser"]) == 0

    assert "already seems to be running" in capsys.readouterr().out
    assert opened == [f"http://127.0.0.1:{port}/setup"]
