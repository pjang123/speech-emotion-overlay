import pytest

from emotion_overlay.slang import SLANG, canonical, plain_english


@pytest.mark.parametrize(
    ("heard", "read_as"),
    [
        ("lol", "haha"),
        ("LOL!", "haha!"),
        ("L.O.L.", "haha."),
        ("lmaooo that's hilarious", "hahaha that's hilarious"),
        ("Bruhhh, come on.", "ugh, come on, come on."),
        ("Wassup chat", "hey there chat"),
        ("ngl that was sus", "not gonna lie that was shady"),
        ("fr fr", "for real"),
        ("FRFR", "for real"),
        ("no cap that was fire", "honestly that was amazing"),
        ("Sheeeesh.", "wow, that's impressive."),
        ("That's an L.", "That's an loss."),
        ("huge W", "huge win"),
        ("That movie was so mid.", "That movie was so mediocre."),
        ("that food is bussin", "that food is really good"),
    ],
)
def test_slang_is_read_as_plain_english(heard: str, read_as: str) -> None:
    assert plain_english(heard) == read_as


@pytest.mark.parametrize(
    "ordinary",
    [
        "fire the gun",  # "fire" only after "that's", "so", ...
        "the fire was everywhere",
        "mid air jump",  # "mid" only at the end of a phrase
        "the mid-season update",
        "I bet he's mad",  # left out: too often ordinary English
        "I like soup",
        "the bus is late",
        "a wall",
        "I dunno",  # spelling-only entry: no meaning to swap in
        "",
    ],
)
def test_ordinary_english_is_left_alone(ordinary: str) -> None:
    assert plain_english(ordinary) == ordinary


def test_canonical_unifies_spellings() -> None:
    assert canonical("Wassup, Brah? LMFAO") == "sup, bruh? lmao"
    assert canonical("dead ass, I dunno") == "deadass, idk"


def test_table_is_well_formed() -> None:
    terms = [entry.term for entry in SLANG]
    assert len(terms) == len(set(terms))
    spellings = [s for entry in SLANG for s in (entry.term, *entry.spellings)]
    assert len(spellings) == len(set(spellings)), "a spelling belongs to two terms"
    for entry in SLANG:
        assert entry.term == entry.term.lower().strip()
        # Meanings must not themselves be slang, or the output would depend on table order.
        if entry.meaning:
            assert plain_english(entry.meaning) == entry.meaning
