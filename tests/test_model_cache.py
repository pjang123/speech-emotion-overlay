from typing import Any

from emotion_overlay.model_cache import load_cached


def test_uses_local_cache_without_network() -> None:
    calls: list[dict[str, Any]] = []

    def loader(name: str, **kwargs: Any) -> str:
        calls.append(kwargs)
        return f"model:{name}"

    assert load_cached(loader, "m", device="cpu") == "model:m"
    assert calls == [{"local_files_only": True, "device": "cpu"}]


def test_downloads_only_when_not_cached() -> None:
    calls: list[dict[str, Any]] = []

    def loader(name: str, **kwargs: Any) -> str:
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise OSError("not in cache")
        return "downloaded"

    assert load_cached(loader, "m") == "downloaded"
    assert calls == [{"local_files_only": True}, {}]
