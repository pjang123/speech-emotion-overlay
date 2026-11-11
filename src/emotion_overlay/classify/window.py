"""Rolling window of recent transcript text that the classifier sees."""

from collections import deque


class TranscriptWindow:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds  # 0 = only the latest utterance
        self._items: deque[tuple[float, str]] = deque()

    def add(self, text: str, at: float) -> None:
        self._items.append((at, text))
        self._prune(at)

    def text(self, now: float) -> str:
        self._prune(now)
        return " ".join(text for _, text in self._items)

    def clear(self) -> None:
        self._items.clear()

    def _prune(self, now: float) -> None:
        if self.seconds <= 0:
            while len(self._items) > 1:
                self._items.popleft()
            return
        while self._items and now - self._items[0][0] > self.seconds:
            self._items.popleft()
