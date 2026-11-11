from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_image
from emotion_overlay.audio.capture import InputDevice
from emotion_overlay.config import AppConfig
from emotion_overlay.pipeline import PipelineEvents
from emotion_overlay.server.app import create_app
from emotion_overlay.state_machine import EmotionState

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765"
SAME_ORIGIN = {"Origin": BASE}
EVIL_ORIGIN = {"Origin": "https://evil.example"}


class FakeEngine:
    def __init__(self, events: PipelineEvents) -> None:
        self.events = events
        self.device: int | None = None
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_device(self, device: int | None) -> None:
        self.device = device

    def status(self) -> dict[str, Any]:
        return {"state": "listening", "device": self.device}


@pytest.fixture
def engine_holder() -> dict[str, FakeEngine]:
    return {}


@pytest.fixture
def client(config: AppConfig, engine_holder: dict[str, FakeEngine]) -> Iterator[TestClient]:
    def factory(events: PipelineEvents) -> FakeEngine:
        engine_holder["engine"] = FakeEngine(events)
        return engine_holder["engine"]

    devices = [InputDevice(3, "USB Mic", "MME", 48000.0, True)]
    app = create_app(config, engine_factory=factory, device_lister=lambda: devices)
    with TestClient(app, base_url=BASE) as c:
        yield c


def upload(client: TestClient, emotion: str = "joy", data: bytes | None = None, **headers: str):
    files = {"file": ("face.png", data if data is not None else make_image(), "image/png")}
    return client.post("/api/images", data={"emotion": emotion}, files=files, headers=headers)


def test_pages_are_served_with_security_headers(client: TestClient) -> None:
    for path in ("/setup", "/overlay"):
        res = client.get(path)
        assert res.status_code == 200
        assert "default-src 'self'" in res.headers["content-security-policy"]
        assert res.headers["x-content-type-options"] == "nosniff"


def test_upload_list_serve_delete_flow(client: TestClient) -> None:
    res = upload(client, "joy")
    assert res.status_code == 201
    image = res.json()

    listed = client.get("/api/images").json()["images"]
    assert [i["id"] for i in listed] == [image["id"]]

    served = client.get(image["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"

    assert client.delete(f"/api/images/{image['id']}").status_code == 204
    assert client.get(image["url"]).status_code == 404


def test_upload_rejects_invalid_content(client: TestClient) -> None:
    res = upload(client, data=b"<script>alert(1)</script>")
    assert res.status_code == 400


def test_upload_rejects_oversize(client: TestClient, config: AppConfig) -> None:
    res = upload(client, data=b"x" * (config.uploads.max_bytes + 10))
    assert res.status_code == 413


def test_upload_rejects_unknown_emotion(client: TestClient) -> None:
    assert upload(client, emotion="smug").status_code == 400


def test_image_route_rejects_traversal(client: TestClient) -> None:
    assert client.get("/images/..%2Fmanifest.json").status_code == 404
    assert client.get("/images/" + "a" * 32).status_code == 404


def test_default_image(client: TestClient) -> None:
    image = upload(client).json()
    res = client.put("/api/images/default", json={"image_id": image["id"]})
    assert res.json() == {"default_image": image["id"]}
    assert client.put("/api/images/default", json={"image_id": "f" * 32}).status_code == 404


def test_cross_origin_writes_are_blocked(client: TestClient) -> None:
    assert upload(client, **EVIL_ORIGIN).status_code == 403
    assert client.put("/api/device", json={"device": 3}, headers=EVIL_ORIGIN).status_code == 403
    assert upload(client, **SAME_ORIGIN).status_code == 201


def test_foreign_host_header_is_rejected(client: TestClient) -> None:
    # DNS rebinding: a page on attacker.com resolving to 127.0.0.1 sends Host: attacker.com
    assert client.get("/api/status", headers={"Host": "attacker.com:8765"}).status_code == 400


def test_device_selection(client: TestClient, engine_holder: dict[str, FakeEngine]) -> None:
    assert client.get("/api/devices").json()["devices"][0]["name"] == "USB Mic"
    assert client.put("/api/device", json={"device": 3}).status_code == 200
    assert engine_holder["engine"].device == 3
    assert client.put("/api/device", json={"device": 99}).status_code == 400


def test_engine_lifecycle(client: TestClient, engine_holder: dict[str, FakeEngine]) -> None:
    assert engine_holder["engine"].started


def test_websocket_streams_state_with_image(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    joy = upload(client, "joy").json()
    with client.websocket_connect(f"{WS}/ws", headers=SAME_ORIGIN) as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["emotion"] == "neutral"
        engine_holder["engine"].events.on_state(EmotionState("joy", 0.9, "scores", 1.0))
        msg = ws.receive_json()
        assert (msg["emotion"], msg["image"]) == ("joy", joy["url"])


def test_overlay_websocket_never_gets_debug_messages(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    events = engine_holder["engine"].events
    with (
        client.websocket_connect(f"{WS}/ws") as overlay,
        client.websocket_connect(f"{WS}/ws?debug=1") as dbg,
    ):
        assert overlay.receive_json()["type"] == dbg.receive_json()["type"] == "state"  # snapshot
        events.on_message({"type": "debug", "transcript": "secret words"})
        events.on_state(EmotionState("anger", 0.8, "scores", 2.0))
        assert dbg.receive_json()["type"] == "debug"
        assert overlay.receive_json()["type"] == "state"  # debug message was skipped


def test_cross_origin_websocket_is_rejected(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"{WS}/ws", headers=EVIL_ORIGIN),
    ):
        pass
