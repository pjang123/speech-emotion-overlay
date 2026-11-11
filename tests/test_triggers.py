import json
from pathlib import Path

import pytest

from emotion_overlay.triggers import (
    Trigger,
    TriggerError,
    TriggerStore,
    match,
    normalize,
    parse_phrases,
)

IMG = "a" * 32


def trig(*phrases: str, image: str = IMG) -> Trigger:
    return Trigger("t" * 32, phrases, image, 3.0)


@pytest.mark.parametrize(
    ("transcript", "fires"),
    [
        ("GG, nice one!", True),
        ("that was a gg", True),
        ("G.G. everyone", True),  # punctuation inside the word is ignored
        ("I like eggs", False),  # whole words only
        ("ggs", False),
        ("", False),
    ],
)
def test_whole_word_case_insensitive_matching(transcript: str, fires: bool) -> None:
    assert (match([trig("gg")], transcript) is not None) == fires


def test_phrases_and_alternatives() -> None:
    t = trig("let's go", "lets go")
    assert match([t], "Okay, LET'S GO!") == (t, "let's go")
    assert match([t], "lets go team") == (t, "lets go")
    assert match([t], "let us go") is None
    assert match([t], "let's goooo") is None


def test_first_trigger_in_list_wins() -> None:
    first, second = trig("nice"), Trigger("s" * 32, ("nice shot",), IMG, 2.0)
    assert match([first, second], "nice shot")[0] is first


def test_normalize() -> None:
    assert normalize("  Hello,   WORLD!  It's G.G.  ") == "hello world it's gg"


def test_parse_phrases_validation() -> None:
    assert parse_phrases("gg, good game ,GG,  ") == ("gg", "good game")  # dedupe by meaning
    for bad in ("", " , ", "!!!", "x" * 61, ",".join(f"w{i}" for i in range(11))):
        with pytest.raises(TriggerError):
            parse_phrases(bad)


def test_store_crud_and_persistence(tmp_path: Path) -> None:
    store = TriggerStore(tmp_path)
    t = store.add("gg, good game", IMG, 4)
    assert t.phrases == ("gg", "good game") and t.duration_s == 4.0
    updated = store.update(t.id, phrases="pog", duration_s=2.5)
    assert (updated.phrases, updated.duration_s, updated.image_id) == (("pog",), 2.5, IMG)
    assert TriggerStore(tmp_path).all() == [updated]  # survives a restart
    with pytest.raises(TriggerError):
        store.add("x", IMG, 0.1)  # too short a duration
    with pytest.raises(KeyError):
        store.update("nope", duration_s=2)
    assert store.remove(t.id) and not store.remove(t.id)


def test_deleting_an_image_removes_its_triggers(tmp_path: Path) -> None:
    store = TriggerStore(tmp_path)
    store.add("a", IMG, 3)
    keep = store.add("b", "b" * 32, 3)
    assert store.remove_for_image(IMG) == 1
    assert store.all() == [keep]


def test_validate_rejects_untrusted_garbage() -> None:
    good = [{"id": "c" * 32, "phrases": ["hi"], "image_id": IMG, "duration_s": 2}]
    assert TriggerStore.validate(good, {IMG})[0].phrases == ("hi",)
    for bad in (
        {"not": "a list"},
        [{"phrases": ["hi"], "image_id": "missing", "duration_s": 2}],
        [{"phrases": ["hi"], "image_id": IMG, "duration_s": 9999}],
        [{"phrases": "x" * 200, "image_id": IMG, "duration_s": 2}],
        ["string entry"],
    ):
        with pytest.raises(TriggerError):
            TriggerStore.validate(bad, {IMG})
    # A malformed id is replaced rather than trusted.
    weird = [{"id": "../x", "phrases": ["hi"], "image_id": IMG, "duration_s": 2}]
    assert len(TriggerStore.validate(weird, {IMG})[0].id) == 32


def test_corrupt_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "triggers.json").write_text("{nope")
    assert TriggerStore(tmp_path).all() == []
    (tmp_path / "triggers.json").write_text(json.dumps([{"phrases": [], "image_id": IMG}]))
    assert TriggerStore(tmp_path).all() == []
