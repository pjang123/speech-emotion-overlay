"""Slang that Whisper spells inconsistently and the emotion model doesn't understand.

Two uses, one table:
- Word triggers: every spelling of a term counts as the term, so a "sup" trigger
  also fires when Whisper writes "Wassup", and "lol" matches "LOL" or "L-O-L".
- Emotion scoring: terms with a `meaning` are swapped for plain English before the
  text model sees them. The model was trained on ordinary English and reads "lol"
  as neutral or "that was fire" as anger.

Matching tolerates stretched letters ("bruhhhh", "sheeeesh"), missing apostrophes
("whats up") and letter-by-letter acronyms ("L.O.L.", "N-G-L"). Pure functions,
no I/O.

Deliberately left out: words that are just as often ordinary English ("bet",
"goat", "salty", "I'm dead"), where a wrong guess costs more than a missed one.
Context words (`after`) let a few ambiguous terms through only where they're slang:
"that was fire" but not "fire the gun".
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import groupby


@dataclass(frozen=True)
class Slang:
    term: str  # canonical spelling (what a trigger matches as)
    spellings: tuple[str, ...] = ()  # other ways Whisper writes it
    meaning: str | None = None  # plain English for the emotion model; None = leave as is
    after: tuple[str, ...] = ()  # only counts right after one of these words
    last: bool = False  # only counts with no word after it ("so mid" but not "mid air")


SLANG: tuple[Slang, ...] = (
    Slang("bruh", ("brah", "bru"), "ugh, come on"),
    Slang(
        "sup",
        ("wassup", "wazzup", "whassup", "whatsup", "wussup", "wuddup", "whaddup", "waddup"),
        "hey there",
    ),
    Slang("lol", ("lul", "lel", "lawl"), "haha"),
    Slang("lmao", ("lmfao", "rofl", "kekw", "kek"), "hahaha"),
    Slang("smh", (), "shaking my head, that's disappointing"),
    Slang("ngl", (), "not gonna lie"),
    Slang("tbh", (), "to be honest"),
    Slang("fr", ("frfr", "fr fr", "for real for real"), "for real"),
    Slang("idk", ("i dunno",)),  # "I don't know" reads as surprise to the model
    Slang("no cap", (), "honestly"),  # "no lie" reads as anger
    Slang("deadass", ("dead ass",), "seriously"),
    Slang("sheesh", (), "wow, that's impressive"),
    Slang("sus", (), "shady"),  # "suspicious" reads as fear
    Slang("pog", ("poggers", "pogchamp", "pogger"), "awesome"),
    Slang("oof", (), "ouch"),
    Slang("bussin", ("bussin'", "busting"), "really good", after=("is", "was", "it's", "its")),
    Slang("rizz", ("riz",)),
    Slang(
        "fire",
        (),
        "amazing",
        after=("that's", "thats", "that was", "this is", "it's", "its", "so", "straight", "kinda"),
    ),
    Slang(
        "mid",
        (),
        "mediocre",
        after=("so", "kinda", "pretty", "was", "is", "that's", "thats", "lowkey", "kind of"),
        last=True,
    ),
    Slang("l", (), "loss", after=("an", "big", "huge", "massive", "another", "take the")),
    Slang("w", ("dub",), "win", after=("a", "big", "huge", "massive", "another", "take the")),
    Slang("lowkey", ("low key",), "kind of"),
    Slang("highkey", ("high key",), "really"),
)


def stretchy(word: str, between: str = "") -> str:
    """Regex for a word whose letters may be stretched when written out.

    A single letter matches one or three-plus of it ("bruh" ~ "bruhhhh"), but not
    two, since doubles are ordinary spelling ("god" must not match "good"). A double
    matches two or more ("sheesh" ~ "sheeeesh"). `between` may separate letters.
    """
    parts = []
    for ch, run in groupby(word):
        n = len(list(run))
        if ch.isalnum():
            e = re.escape(ch)
            parts.append(f"{e}(?:{e}{e}+)?" if n == 1 else f"{e * n}{e}*")
        elif ch in "'’":
            parts.append("['’]?")  # "whats" = "what's"
        else:
            parts.append(re.escape(ch) * n)
    return between.join(parts)


def _word_pattern(word: str) -> str:
    """One written slang word; letters may also be spelled out ("l.o.l", "n-g-l")."""
    return stretchy(word, r"(?:[.\-])?")


def _phrase_pattern(phrase: str) -> str:
    return r"[\s,]+".join(_word_pattern(w) for w in phrase.lower().split())


# A dash counts as part of a word, so "mid-season" is not "mid".
_START, _END = r"(?<![\w'’-])", r"(?![\w'’-])"
_LAST = r"(?=\s*(?:[^\w\s'’-]|$))"  # only punctuation or the end of the text follows


@dataclass(frozen=True)
class _Compiled:
    entry: Slang
    regex: re.Pattern[str]  # group "pre" = context word (if any), group "slang" = the term


def _compile(entry: Slang) -> _Compiled:
    # Longest first so "fr fr" wins over "fr".
    spellings = sorted({entry.term, *entry.spellings}, key=len, reverse=True)
    body = "|".join(_phrase_pattern(s) for s in spellings)
    pre = ""
    if entry.after:
        context = "|".join(_phrase_pattern(a) for a in sorted(entry.after, key=len, reverse=True))
        pre = rf"(?P<pre>{context})[\s,]+"
    end = _END + (_LAST if entry.last else "")
    return _Compiled(entry, re.compile(rf"{_START}{pre}(?P<slang>{body}){end}", re.IGNORECASE))


# Longest canonical terms first, so multi-word slang is replaced before its parts.
_COMPILED = tuple(_compile(e) for e in sorted(SLANG, key=lambda e: len(e.term), reverse=True))


def _replace_all(text: str, replacement: Callable[[Slang], str | None]) -> str:
    for compiled in _COMPILED:
        new = replacement(compiled.entry)
        if new is None:
            continue

        def sub(m: re.Match[str], new: str = new) -> str:
            pre = m.groupdict().get("pre")  # context word, kept as spoken
            return f"{pre} {new}" if pre else new

        text = compiled.regex.sub(sub, text)
    return text


def canonical(text: str) -> str:
    """Every known slang spelling in `text` replaced by its canonical term (for matching)."""
    return _replace_all(text, lambda e: e.term)


def plain_english(text: str) -> str:
    """`text` with slang replaced by plain English, for the emotion model."""
    return _replace_all(text, lambda e: e.meaning)
