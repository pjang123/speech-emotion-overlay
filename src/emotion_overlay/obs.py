"""Drive OBS from the detected emotion, over OBS's built-in WebSocket server (v5 protocol).

Per emotion, the user can pick a scene to switch to and/or a filter to turn on (on one
chosen source; the other emotions' filters on it are turned off). The client runs on
its own thread and event loop, reconnects on its own, and never blocks the pipeline:
`show()` only records the wanted emotion.

Protocol: https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
"""

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from emotion_overlay.config import ObsConfig

log = logging.getLogger(__name__)

RETRY_S = 5.0
REQUEST_TIMEOUT_S = 5.0
RPC_VERSION = 1


class ObsError(Exception):
    pass


def auth_response(password: str, salt: str, challenge: str) -> str:
    """The `authentication` string obs-websocket expects in Identify."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsController:
    def __init__(self, config: ObsConfig, publish: Callable[[dict[str, Any]], None]) -> None:
        self._config = config  # shared reference: edits apply on the next change
        self._publish = publish
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._wake: asyncio.Event | None = None
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count()
        self._wanted: str | None = None  # emotion the overlay shows now
        self._applied: str | None = None  # emotion OBS was last set up for
        self._generation = 0  # bumped by reconnect(): drops the current connection
        self.state = "off"  # off | connecting | connected | error
        self.message = "Not connected"

    # --- control (any thread) ----------------------------------------------

    def start(self) -> None:
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            self._wake = asyncio.Event()
            ready.set()
            with contextlib.suppress(asyncio.CancelledError):  # stop()
                self._loop.run_until_complete(self._main())

        self._thread = threading.Thread(target=run, name="obs", daemon=True)
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._shutdown)
        if self._thread:
            self._thread.join(timeout=3)

    def show(self, emotion: str) -> None:
        """The overlay now shows `emotion`; set OBS up for it (if anything is mapped)."""
        self._wanted = emotion
        self._poke()

    def reconnect(self) -> None:
        """Connection settings changed: drop the connection and start over."""
        self._generation += 1
        self._applied = None
        self._poke()

    def mappings_changed(self) -> None:
        """Scene/filter choices changed: apply them to the current emotion again."""
        self._applied = None
        self._poke()

    def call(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and wait for its response data (from any thread but the OBS one)."""
        loop = self._loop
        if loop is None or self.state != "connected":
            raise ObsError("not connected to OBS")
        future = asyncio.run_coroutine_threadsafe(self._request(request_type, data), loop)
        try:
            return future.result(REQUEST_TIMEOUT_S + 1)
        except TimeoutError as exc:
            raise ObsError("OBS didn't answer") from exc

    def status(self) -> dict[str, Any]:
        return {"type": "obs", "state": self.state, "message": self.message}

    # --- OBS thread ----------------------------------------------------------

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            loop.call_soon_threadsafe(wake.set)

    def _shutdown(self) -> None:
        for task in asyncio.all_tasks(self._loop):
            task.cancel()

    def _set_state(self, state: str, message: str) -> None:
        if (state, message) != (self.state, self.message):
            self.state, self.message = state, message
            log.info("OBS: %s", message)
            self._publish(self.status())

    async def _main(self) -> None:
        assert self._wake is not None  # noqa: S101 - set before the loop starts
        while True:
            cfg = self._config
            if not cfg.enabled:
                self._set_state("off", "Not connected")
                await self._wait_for_wake(None)
                continue
            generation = self._generation
            try:
                await self._session(generation)
            except asyncio.CancelledError:
                raise
            except ObsError as exc:
                self._set_state("error", f"Couldn't connect to OBS: {exc}")
            except Exception as exc:  # refused, reset, not OBS, ...
                self._set_state(
                    "error", f"Couldn't connect to OBS at {cfg.host}:{cfg.port} ({exc})"
                )
            if self._generation == generation:  # not a deliberate reconnect: wait first
                await self._wait_for_wake(RETRY_S)

    async def _wait_for_wake(self, timeout: float | None) -> None:
        assert self._wake is not None  # noqa: S101
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout)

    async def _session(self, generation: int) -> None:
        from websockets.asyncio.client import connect

        cfg = self._config
        self._set_state("connecting", f"Connecting to OBS at {cfg.host}:{cfg.port}...")
        uri = f"ws://{cfg.host}:{cfg.port}"
        async with connect(uri, open_timeout=REQUEST_TIMEOUT_S, max_size=2**22) as ws:
            await self._identify(ws, cfg.password)
            self._ws = ws
            reader = asyncio.ensure_future(self._read(ws))
            self._set_state("connected", f"Connected to OBS at {cfg.host}:{cfg.port}")
            self._applied = None  # set OBS up for the current emotion right away
            try:
                while self._generation == generation and self._config.enabled:
                    if reader.done():
                        reader.result()  # raises why the connection ended
                        raise ObsError("OBS closed the connection")
                    if self._wanted is not None and self._wanted != self._applied:
                        wanted = self._wanted
                        await self._apply(wanted)
                        self._applied = wanted
                    await self._wait_for_wake(1.0)
            finally:
                self._ws = None
                reader.cancel()
                for future in self._pending.values():
                    future.cancel()
                self._pending.clear()

    async def _identify(self, ws: Any, password: str) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), REQUEST_TIMEOUT_S))
        if hello.get("op") != 0:
            raise ObsError("that isn't an OBS WebSocket server")
        identify: dict[str, Any] = {"rpcVersion": RPC_VERSION, "eventSubscriptions": 0}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not password:
                raise ObsError("OBS asks for a password; enter it here")
            identify["authentication"] = auth_response(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        try:
            reply = json.loads(await asyncio.wait_for(ws.recv(), REQUEST_TIMEOUT_S))
        except Exception as exc:  # OBS closes the socket on a wrong password
            raise ObsError("OBS refused the connection; check the password") from exc
        if reply.get("op") != 2:
            raise ObsError("OBS refused the connection; check the password")

    async def _read(self, ws: Any) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if message.get("op") != 7:  # only request responses matter here
                continue
            data = message.get("d", {})
            future = self._pending.pop(str(data.get("requestId")), None)
            if future is not None and not future.done():
                future.set_result(data)

    async def _request(self, request_type: str, data: dict[str, Any] | None) -> dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise ObsError("not connected to OBS")
        request_id = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"requestType": request_type, "requestId": request_id}
        if data:
            payload["requestData"] = data
        await ws.send(json.dumps({"op": 6, "d": payload}))
        try:
            reply = await asyncio.wait_for(future, REQUEST_TIMEOUT_S)
        except TimeoutError as exc:
            raise ObsError(f"OBS didn't answer {request_type}") from exc
        finally:
            self._pending.pop(request_id, None)
        status = reply.get("requestStatus", {})
        if not status.get("result"):
            raise ObsError(status.get("comment") or f"{request_type} failed ({status.get('code')})")
        return reply.get("responseData") or {}

    async def _apply(self, emotion: str) -> None:
        """Switch scene and filters for `emotion`. A failure (e.g. a renamed scene) is
        reported but doesn't drop the connection."""
        cfg = self._config
        try:
            scene = cfg.scenes.get(emotion)
            if scene:
                await self._request("SetCurrentProgramScene", {"sceneName": scene})
            if cfg.filter_source and cfg.filters:
                wanted = cfg.filters.get(emotion)
                # Turn the others off first, so two emotion filters never overlap.
                for name in sorted(set(cfg.filters.values()) - {wanted}):
                    await self._set_filter(cfg.filter_source, name, False)
                if wanted:
                    await self._set_filter(cfg.filter_source, wanted, True)
            host = f"{cfg.host}:{cfg.port}"
            self._set_state("connected", f"Connected to OBS at {host}")
        except ObsError as exc:
            self._set_state("connected", f"Connected, but OBS said: {exc}")

    async def _set_filter(self, source: str, name: str, enabled: bool) -> None:
        data = {"sourceName": source, "filterName": name, "filterEnabled": enabled}
        await self._request("SetSourceFilterEnabled", data)
