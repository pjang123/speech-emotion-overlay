from pathlib import Path

import pytest

from conftest import make_image
from emotion_overlay.fetch import name_from_page
from emotion_overlay.images import ImageStore, ImageValidationError


@pytest.mark.parametrize(
    ("page", "file_name", "expected"),
    [
        ("https://tenor.com/view/happy-dance-gif-9047499432530111138", "x.gif", "happy dance.gif"),
        (
            "https://giphy.com/gifs/thank-you-thanks-thumbs-up-BYoRqTmcgzHcL9TCy1",
            "giphy.gif",
            "thank you thanks thumbs up.gif",
        ),
        ("https://giphy.com/gifs/BYoRqTmcgzHcL9TCy1", "giphy.gif", "giphy.gif"),  # id only
        ("https://giphy.com/gifs/big-celebration", "giphy.webp", "big celebration.webp"),
        ("https://example.com/", "pic.png", "pic.png"),
        ("https://example.com/post/12345", "pic.gif", "pic.gif"),  # no words in the slug
        (
            "https://tenor.com/view/honor-the-veterans-day-veterans-day-november-11-veterans-"
            "appreciation-day-hero-appreciation-day-gif-9047499432530111138",
            "x.gif",
            "honor the veterans day veterans day november 11 veterans.gif",  # whole words
        ),
    ],
)
def test_name_from_page(page: str, file_name: str, expected: str) -> None:
    assert name_from_page(page, file_name) == expected


def test_rename_image(tmp_path: Path) -> None:
    store = ImageStore(tmp_path, max_bytes=200_000, max_pixels=10**6)
    record = store.add(make_image("GIF"), "trigger", "giphy.gif")
    renamed = store.update(record.id, name="  victory dance  ")
    assert renamed.original_name == "victory dance"
    assert renamed.tier == record.tier and renamed.emotion == record.emotion
    assert ImageStore(tmp_path, 200_000, 10**6).get(record.id).original_name == "victory dance"
    assert store.update(record.id, name="../../evil").original_name == "evil"  # display only
    for empty in ("", "   ", "\x00\x01"):
        with pytest.raises(ImageValidationError):
            store.update(record.id, name=empty)
