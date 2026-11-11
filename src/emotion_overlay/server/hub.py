"""Fan-out of JSON messages from worker threads to WebSocket clients."""

import asyncio
import contextlib
import threading
from typing import Any

Message = dict[str, Any]

# Message types that carry transcripts, scores or setup-only state. Only clients
# that ask for debug data (the setup page) receive them; the OBS overlay never does.
DEBUG_TYPES = frozenset({"debug", "vad", "calibration"})


class Hub:
    def __init__(self, max_queue: int = 64) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: dict[asyncio.Queue[Message], bool] = {}
        self._latest: dict[str, Message] = {}
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, message: Message) -> None:
        """Thread-safe. Remembers the latest message per type for new subscribers."""
        with self._lock:
            self._latest[message["type"]] = message
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # loop shutting down
            loop.call_soon_threadsafe(self._fanout, message)

    def latest(self, message_type: str) -> Message | None:
        with self._lock:
            return self._latest.get(message_type)

    def subscribe(self, include_debug: bool) -> tuple[asyncio.Queue[Message], list[Message]]:
        """Must be called on the event loop. Returns the queue and a snapshot to send first."""
        q: asyncio.Queue[Message] = asyncio.Queue(maxsize=self._max_queue)
        self._clients[q] = include_debug
        with self._lock:
            snapshot = [m for t, m in self._latest.items() if include_debug or t not in DEBUG_TYPES]
        return q, snapshot

    def unsubscribe(self, q: asyncio.Queue[Message]) -> None:
        self._clients.pop(q, None)

    def _fanout(self, message: Message) -> None:
        is_debug = message["type"] in DEBUG_TYPES
        for q, wants_debug in list(self._clients.items()):
            if is_debug and not wants_debug:
                continue
            with contextlib.suppress(asyncio.QueueFull):  # slow client: drop, don't block
                q.put_nowait(message)
