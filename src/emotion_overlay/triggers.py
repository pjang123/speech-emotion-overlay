"""Word triggers: show a chosen image for a few seconds when a phrase is heard.

Matching is case-insensitive and whole-word on the latest utterance's transcript
("gg" fires on "GG, nice" but not on "eggs"). It forgives how Whisper happens to
spell things: punctuation and apostrophes are ignored ("G.G." = "gg", "whats" =
"what's"), stretched letters still match ("bruh" fires on "bruhhhh"), and known
slang spellings count as one word ("sup" fires on "Wassup", see `slang.py`).
Each trigger also accepts several comma-separated alternatives for anything else.

Stored in data/triggers.json and validated on load, like the image manifest.
"""

import json
import logging
import re
import threading
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from emotion_overlay.slang import canonical, stretchy

log = logging.getLogger(__name__)

MAX_TRIGGERS = 50
MAX_PHRASES = 10
MAX_PHRASE_CHARS = 60
MIN_DURATION_S, MAX_DURATION_S = 0.5, 60.0
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_NON_WORD = re.compile(r"\W+")


class TriggerError(ValueError):
    pass


@dataclass(frozen=True)
class Trigger:
    id: str
    phrases: tuple[str, ...]
    image_id: str
    duration_s: float

    def to_api(self) -> dict[str, Any]:
        return {**asdict(self), "phrases": list(self.phrases)}


def normalize(text: str) -> str:
    """Lowercase words separated by single spaces; punctuation and apostrophes dropped."""
    text = text.lower().replace(".", "").replace("'", "").replace("’", "")
    return " ".join(_NON_WORD.sub(" ", text).split())


def _key(text: str) -> str:
    """What matching compares: known slang spellings unified, then normalized."""
    return normalize(canonical(text))


@lru_cache(maxsize=1024)
def _phrase_regex(phrase: str) -> re.Pattern[str]:
    words = _key(phrase).split()
    return re.compile(r"(?<!\S)" + " ".join(stretchy(w) for w in words) + r"(?!\S)")


def parse_phrases(raw: str | Sequence[str]) -> tuple[str, ...]:
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    phrases: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TriggerError("phrases must be text")
        phrase = " ".join(item.split())
        if not phrase:
            continue
        if len(phrase) > MAX_PHRASE_CHARS:
            raise TriggerError(f"'{phrase[:20]}…' is longer than {MAX_PHRASE_CHARS} characters")
        if not normalize(phrase):
            raise TriggerError(f"'{phrase}' has no letters or numbers to listen for")
        if normalize(phrase) not in {normalize(p) for p in phrases}:
            phrases.append(phrase)
    if not phrases:
        raise TriggerError("enter at least one word or phrase")
    if len(phrases) > MAX_PHRASES:
        raise TriggerError(f"at most {MAX_PHRASES} phrases per trigger")
    return tuple(phrases)


def check_duration(value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TriggerError("duration must be a number of seconds")
    if not MIN_DURATION_S <= value <= MAX_DURATION_S:
        raise TriggerError(f"duration must be {MIN_DURATION_S}-{MAX_DURATION_S:.0f} seconds")
    return float(value)


def match(triggers: Sequence[Trigger], transcript: str) -> tuple[Trigger, str] | None:
    """The first trigger (in list order) with a phrase in the transcript, and that phrase."""
    heard = _key(transcript)
    if not heard:
        return None
    for trigger in triggers:
        for phrase in trigger.phrases:
            if _phrase_regex(phrase).search(heard):
                return trigger, phrase
    return None


class TriggerStore:
    def __init__(self, root: Path) -> None:
        self._path = root / "triggers.json"
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._triggers: list[Trigger] = []
        self._load()

    def all(self) -> list[Trigger]:
        with self._lock:
            return list(self._triggers)

    def get(self, trigger_id: str) -> Trigger | None:
        with self._lock:
            return next((t for t in self._triggers if t.id == trigger_id), None)

    def add(self, phrases: str | Sequence[str], image_id: str, duration_s: Any) -> Trigger:
        trigger = Trigger(
            uuid.uuid4().hex, parse_phrases(phrases), image_id, check_duration(duration_s)
        )
        with self._lock:
            if len(self._triggers) >= MAX_TRIGGERS:
                raise TriggerError(f"at most {MAX_TRIGGERS} triggers")
            self._triggers.append(trigger)
            self._save()
        return trigger

    def update(
        self,
        trigger_id: str,
        phrases: str | Sequence[str] | None = None,
        image_id: str | None = None,
        duration_s: Any = None,
    ) -> Trigger:
        with self._lock:
            index = next((i for i, t in enumerate(self._triggers) if t.id == trigger_id), None)
            if index is None:
                raise KeyError(trigger_id)
            old = self._triggers[index]
            new = Trigger(
                old.id,
                parse_phrases(phrases) if phrases is not None else old.phrases,
                image_id or old.image_id,
                check_duration(duration_s) if duration_s is not None else old.duration_s,
            )
            self._triggers[index] = new
            self._save()
            return new

    def remove(self, trigger_id: str) -> bool:
        with self._lock:
            before = len(self._triggers)
            self._triggers = [t for t in self._triggers if t.id != trigger_id]
            if len(self._triggers) == before:
                return False
            self._save()
            return True

    def remove_for_image(self, image_id: str) -> int:
        """Drop triggers whose image was deleted."""
        with self._lock:
            before = len(self._triggers)
            self._triggers = [t for t in self._triggers if t.image_id != image_id]
            removed = before - len(self._triggers)
            if removed:
                self._save()
            return removed

    def replace_all(self, triggers: Sequence[Trigger]) -> None:
        with self._lock:
            self._triggers = list(triggers)[:MAX_TRIGGERS]
            self._save()

    @staticmethod
    def validate(raw: Any, image_ids: set[str]) -> list[Trigger]:
        """Validate untrusted trigger data (a file or an imported setup)."""
        if not isinstance(raw, list):
            raise TriggerError("triggers must be a list")
        triggers = []
        for item in raw[:MAX_TRIGGERS]:
            if not isinstance(item, dict):
                raise TriggerError("invalid trigger entry")
            image_id = item.get("image_id")
            if image_id not in image_ids:
                raise TriggerError("a trigger points to a missing image")
            trigger_id = item.get("id")
            triggers.append(
                Trigger(
                    trigger_id
                    if isinstance(trigger_id, str) and _ID_RE.match(trigger_id)
                    else uuid.uuid4().hex,
                    parse_phrases(item.get("phrases") or []),
                    image_id,
                    check_duration(item.get("duration_s")),
                )
            )
        return triggers

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            ids = {item.get("image_id") for item in raw if isinstance(item, dict)}
            self._triggers = self.validate(raw, {i for i in ids if isinstance(i, str)})
        except (OSError, ValueError, AttributeError, TypeError):
            log.exception("Ignoring unreadable %s", self._path)

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([t.to_api() for t in self._triggers], indent=2), encoding="utf-8")
        tmp.replace(self._path)
