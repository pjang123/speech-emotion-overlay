from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_image
from emotion_overlay.audio.capture import InputDevice
from emotion_overlay.config import AppConfig, load_config
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
        self.listening = True
        self.calibration: Any = None
        self.listener: Any = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_device(self, device: int | None) -> None:
        self.device = device

    def set_listening(self, listening: bool) -> None:
        self.listening = listening

    def set_calibration(self, calibration: Any) -> None:
        self.calibration = calibration

    def set_calibration_listener(self, listener: Any) -> None:
        self.listener = listener

    def status(self) -> dict[str, Any]:
        return {"state": "listening", "device": self.device, "listening": self.listening}


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


def test_listening_toggle(client: TestClient, engine_holder: dict[str, FakeEngine]) -> None:
    res = client.put("/api/listening", json={"listening": False})
    assert res.status_code == 200 and res.json()["listening"] is False
    assert engine_holder["engine"].listening is False
    assert client.put("/api/listening", json={"listening": True}).json()["listening"] is True
    blocked = client.put("/api/listening", json={"listening": False}, headers=EVIL_ORIGIN)
    assert blocked.status_code == 403 and engine_holder["engine"].listening is True


def test_listening_without_audio_engine(config: AppConfig) -> None:
    with TestClient(create_app(config, device_lister=lambda: []), base_url=BASE) as client:
        assert client.put("/api/listening", json={"listening": False}).status_code == 409


def test_websocket_streams_state_with_image(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    joy = upload(client, "joy").json()
    with client.websocket_connect(f"{WS}/ws", headers=SAME_ORIGIN) as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["emotion"] == "neutral"
        engine_holder["engine"].events.on_state(EmotionState("joy", 0.9, "scores", 1.0), True)
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
        assert overlay.receive_json()["type"] == "state"  # overlay snapshot: state only
        assert [dbg.receive_json()["type"] for _ in range(2)] == ["state", "calibration"]
        events.on_message({"type": "debug", "transcript": "secret words"})
        events.on_state(EmotionState("anger", 0.8, "scores", 2.0), True)
        assert dbg.receive_json()["type"] == "debug"
        assert overlay.receive_json()["type"] == "state"  # debug message was skipped


def test_cross_origin_websocket_is_rejected(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"{WS}/ws", headers=EVIL_ORIGIN),
    ):
        pass


# --- calibration ------------------------------------------------------------


def test_calibration_session_end_to_end(
    client: TestClient, engine_holder: dict[str, FakeEngine], config: AppConfig
) -> None:
    engine = engine_holder["engine"]
    started = client.post("/api/calibration/start").json()
    assert started["active"] and started["session"]["index"] == 0
    assert engine.listener is not None  # pipeline now feeds calibration, not the display

    total = started["session"]["total"]
    for _ in range(total):
        wanted = client.get("/api/calibration").json()["session"]["prompt"]["emotion"]
        engine.listener({wanted: 0.8, "neutral": 0.2}, {"ang": 0.7, "neu": 0.3})
    done = client.get("/api/calibration").json()
    assert not done["active"] and done["result"]["samples"] == total
    assert engine.listener is None  # live display resumes

    saved = client.post("/api/calibration/save").json()
    assert saved["saved"]["samples"] == total
    assert engine.calibration is not None
    assert (config.data_dir / "calibration.json").exists()

    assert client.delete("/api/calibration").json()["saved"] is None
    assert engine.calibration is None


def test_calibration_recommendations_can_be_applied_selectively(
    client: TestClient, engine_holder: dict[str, FakeEngine], config: AppConfig
) -> None:
    from emotion_overlay.calibration import SpeechStats

    engine = engine_holder["engine"]
    client.post("/api/calibration/start")
    # A slow speaker: long mid-sentence pauses, clear voice, quiet room.
    slow = SpeechStats((520, 610, 580), (0.95,) * 20, (0.02,) * 10)
    for _ in range(14):
        wanted = client.get("/api/calibration").json()["session"]["prompt"]["emotion"]
        engine.listener({wanted: 0.7, "neutral": 0.3}, {}, slow)
    done = client.get("/api/calibration").json()
    recs = {r["path"]: r for r in done["recommendations"]}
    assert recs["vad.min_silence_ms"]["value"] == 700  # p75 + 100 ms, capped
    assert recs["vad.min_silence_ms"]["reason"]
    assert done["result"]["profile"]["pause_p75_ms"] > 500

    # Apply only the pause recommendation.
    saved = client.post("/api/calibration/save", json={"apply": ["vad.min_silence_ms"]}).json()
    assert [r["path"] for r in saved["applied"]] == ["vad.min_silence_ms"]
    assert config.vad.min_silence_ms == 700  # live: the segmenter reads this object
    others = set(recs) - {"vad.min_silence_ms"}
    if "state.confidence_threshold" in others:
        assert config.state.confidence_threshold == 0.5  # untouched
    assert client.get("/api/settings").json()["vad"]["min_silence_ms"] == 700


def test_calibration_save_without_body_applies_nothing(
    client: TestClient, engine_holder: dict[str, FakeEngine], config: AppConfig
) -> None:
    from emotion_overlay.calibration import SpeechStats

    client.post("/api/calibration/start")
    slow = SpeechStats((900,), (0.95,) * 20, (0.02,) * 10)
    for _ in range(14):
        wanted = client.get("/api/calibration").json()["session"]["prompt"]["emotion"]
        engine_holder["engine"].listener({wanted: 0.7}, {}, slow)
    saved = client.post("/api/calibration/save").json()
    assert saved["applied"] == [] and saved["saved"] is not None
    assert config.vad.min_silence_ms == 400


def test_calibration_cancel_and_errors(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    assert client.post("/api/calibration/save").status_code == 409  # nothing finished
    assert client.post("/api/calibration/nope").status_code == 404
    client.post("/api/calibration/start")
    client.post("/api/calibration/cancel")
    assert engine_holder["engine"].listener is None
    engine_holder["engine"].listening = False
    res = client.post("/api/calibration/start")
    assert res.status_code == 409 and "listening" in res.json()["detail"]
    assert client.post("/api/calibration/start", headers=EVIL_ORIGIN).status_code == 403


def test_calibration_finish_early_with_too_few_lines(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    client.post("/api/calibration/start")
    engine_holder["engine"].listener({"joy": 0.9}, {})
    res = client.post("/api/calibration/finish").json()
    assert res["result"] is None and "at least" in res["error"]


def test_saved_calibration_is_loaded_on_startup(config: AppConfig) -> None:
    holder: dict[str, FakeEngine] = {}

    def factory(events: PipelineEvents) -> FakeEngine:
        holder["engine"] = FakeEngine(events)
        return holder["engine"]

    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "calibration.json").write_text(
        '{"voice_bias": {"neu": 0.4}, "final_bias": {"joy": 0.2}, "weight": 0.25, "samples": 9}'
    )
    create_app(config, engine_factory=factory, device_lister=lambda: [])
    assert holder["engine"].calibration.final_bias == {"joy": 0.2}


# --- settings ---------------------------------------------------------------


def test_settings_apply_live_and_persist(config: AppConfig, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    app = create_app(config, config_path=config_path, device_lister=lambda: [])
    with TestClient(app, base_url=BASE) as client:
        state_section = config.state
        res = client.put("/api/settings", json={"state": {"silence_timeout_s": 9}})
        assert res.status_code == 200
        assert res.json()["state"]["silence_timeout_s"] == 9
        assert state_section.silence_timeout_s == 9  # same object the state machine holds
        assert load_config(config_path).state.silence_timeout_s == 9


def test_settings_change_is_broadcast_to_overlay(client: TestClient) -> None:
    with client.websocket_connect(f"{WS}/ws") as ws:
        ws.receive_json()
        client.put("/api/settings", json={"display": {"transition": "bounce"}})
        assert ws.receive_json()["transition"] == "bounce"


def test_settings_reset(client: TestClient) -> None:
    client.put("/api/settings", json={"display": {"transition": "cut", "fade_ms": 900}})
    res = client.post("/api/settings/reset")
    assert res.status_code == 200
    assert (res.json()["display"]["transition"], res.json()["display"]["fade_ms"]) == ("fade", 300)
    assert client.post("/api/settings/reset", headers=EVIL_ORIGIN).status_code == 403


def test_settings_reject_locked_and_invalid(client: TestClient) -> None:
    assert client.put("/api/settings", json={"debug": {"save_audio": True}}).status_code == 400
    bad = client.put("/api/settings", json={"display": {"fade_ms": -5}})
    assert bad.status_code == 400 and "fade_ms" in bad.json()["detail"]
    blocked = client.put("/api/settings", json={"state": {}}, headers=EVIL_ORIGIN)
    assert blocked.status_code == 403


# --- image edits --------------------------------------------------------------


def test_upload_with_tier_and_update(client: TestClient) -> None:
    files = {"file": ("x.png", make_image(), "image/png")}
    image = client.post("/api/images", data={"emotion": "joy", "tier": "strong"}, files=files)
    assert image.json()["tier"] == "strong"
    image_id = image.json()["id"]
    res = client.put(f"/api/images/{image_id}", json={"tier": "mild", "emotion": "fear"})
    assert (res.json()["tier"], res.json()["emotion"]) == ("mild", "fear")
    assert client.put(f"/api/images/{image_id}", json={"tier": "huge"}).status_code == 400
    assert client.put("/api/images/" + "0" * 32, json={"tier": "mild"}).status_code == 404


def test_default_route_is_not_shadowed_by_image_update(client: TestClient) -> None:
    image = upload(client).json()
    res = client.put("/api/images/default", json={"image_id": image["id"]})
    assert res.json() == {"default_image": image["id"]}


# --- backup -------------------------------------------------------------------


def test_export_import_roundtrip(config: AppConfig, tmp_path: Path) -> None:
    source = create_app(config, device_lister=lambda: [])
    with TestClient(source, base_url=BASE) as client:
        image = upload(client, "anger").json()
        client.put("/api/images/default", json={"image_id": image["id"]})
        client.put("/api/settings", json={"display": {"transition": "cut"}})
        exported = client.get("/api/export")
    assert exported.headers["content-type"] == "application/zip"

    target_config = AppConfig(data_dir=tmp_path / "other")
    target = create_app(target_config, device_lister=lambda: [])
    with TestClient(target, base_url=BASE) as client:
        upload(client, "joy")  # replaced by the import
        files = {"file": ("setup.zip", exported.content, "application/zip")}
        res = client.post("/api/import", files=files)
        assert res.status_code == 200 and res.json()["images"] == 1
        listed = client.get("/api/images").json()
        assert [i["emotion"] for i in listed["images"]] == ["anger"]
        assert listed["default_image"] == listed["images"][0]["id"]
    assert target_config.display.transition == "cut"


def test_failed_import_changes_nothing(client: TestClient) -> None:
    existing = upload(client).json()
    files = {"file": ("setup.zip", b"definitely not a zip", "application/zip")}
    res = client.post("/api/import", files=files)
    assert res.status_code == 400 and "nothing was changed" in res.json()["detail"]
    assert [i["id"] for i in client.get("/api/images").json()["images"]] == [existing["id"]]


def test_cross_origin_import_is_blocked(client: TestClient) -> None:
    files = {"file": ("setup.zip", b"x", "application/zip")}
    assert client.post("/api/import", files=files, headers=EVIL_ORIGIN).status_code == 403


# --- word triggers & links ----------------------------------------------------------


def upload_trigger_image(client: TestClient) -> dict:
    files = {"file": ("gg.gif", make_image("GIF"), "image/gif")}
    res = client.post("/api/images", data={"emotion": "trigger"}, files=files)
    assert res.status_code == 201
    return res.json()


def test_trigger_crud(client: TestClient) -> None:
    image = upload_trigger_image(client)
    body = {"phrases": "gg, good game", "image_id": image["id"], "duration_s": 2}
    res = client.post("/api/triggers", json=body)
    assert res.status_code == 201
    trigger = res.json()
    assert trigger["phrases"] == ["gg", "good game"]
    assert client.get("/api/triggers").json()["triggers"] == [trigger]
    updated = client.put(f"/api/triggers/{trigger['id']}", json={"duration_s": 5}).json()
    assert updated["duration_s"] == 5
    bad_image = {"phrases": "x", "image_id": "f" * 32}
    assert client.post("/api/triggers", json=bad_image).status_code == 400
    empty = {"phrases": "", "image_id": image["id"]}
    assert client.post("/api/triggers", json=empty).status_code == 400
    assert client.post("/api/triggers", json=body, headers=EVIL_ORIGIN).status_code == 403
    assert client.delete(f"/api/triggers/{trigger['id']}").status_code == 204
    assert client.get("/api/triggers").json()["triggers"] == []


def test_spoken_trigger_shows_image_then_reverts(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    image = upload_trigger_image(client)
    body = {"phrases": "gg", "image_id": image["id"], "duration_s": 0.5}
    client.post("/api/triggers", json=body)
    with client.websocket_connect(f"{WS}/ws") as ws:
        ws.receive_json()  # snapshot
        engine_holder["engine"].events.on_words("GG everyone!")
        shown = ws.receive_json()
        assert (shown["image"], shown["trigger"]) == (image["url"], "gg")
        ended = ws.receive_json()  # arrives after 0.5 s
        assert ended["trigger"] is None and ended["image"] != image["url"]


def test_unmatched_words_do_nothing(
    client: TestClient, engine_holder: dict[str, FakeEngine]
) -> None:
    image = upload_trigger_image(client)
    client.post("/api/triggers", json={"phrases": "gg", "image_id": image["id"]})
    engine_holder["engine"].events.on_words("I like eggs")
    assert client.get("/api/status").json()["state"]["trigger"] is None


def test_trigger_test_button(client: TestClient) -> None:
    image = upload_trigger_image(client)
    t = client.post("/api/triggers", json={"phrases": "pog", "image_id": image["id"]}).json()
    assert client.post(f"/api/triggers/{t['id']}/test").status_code == 200
    assert client.get("/api/status").json()["state"]["trigger"] == "pog"
    assert client.post("/api/triggers/nope/test").status_code == 404


def test_deleting_image_removes_its_triggers(client: TestClient) -> None:
    image = upload_trigger_image(client)
    client.post("/api/triggers", json={"phrases": "gg", "image_id": image["id"]})
    client.delete(f"/api/images/{image['id']}")
    assert client.get("/api/triggers").json()["triggers"] == []


def test_triggers_survive_export_import(config: AppConfig, tmp_path: Path) -> None:
    with TestClient(create_app(config, device_lister=lambda: []), base_url=BASE) as client:
        image = upload_trigger_image(client)
        client.post("/api/triggers", json={"phrases": "gg", "image_id": image["id"]})
        exported = client.get("/api/export").content
    target = AppConfig(data_dir=tmp_path / "other")
    with TestClient(create_app(target, device_lister=lambda: []), base_url=BASE) as client:
        files = {"file": ("s.zip", exported, "application/zip")}
        assert client.post("/api/import", files=files).status_code == 200
        (trigger,) = client.get("/api/triggers").json()["triggers"]
        (imported_image,) = client.get("/api/images").json()["images"]
        assert trigger["image_id"] == imported_image["id"] != image["id"]  # remapped


def test_image_from_link(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import emotion_overlay.server.app as app_module
    from emotion_overlay.fetch import FetchError

    calls: list[str] = []

    def fake_fetch(url: str, max_bytes: int) -> tuple[bytes, str]:
        calls.append(url)
        if "bad" in url:
            raise FetchError("links to this computer or a local network are not allowed")
        if "html" in url:
            return b"<html>", "page"
        return make_image("GIF"), "dance.gif"

    monkeypatch.setattr(app_module, "fetch_image", fake_fetch)
    ok = {"url": "https://media.example/dance.gif", "emotion": "joy"}
    res = client.post("/api/images/from-url", json=ok)
    assert res.status_code == 201 and res.json()["format"] == "GIF"
    assert res.json()["original_name"] == "dance.gif"
    bad = client.post("/api/images/from-url", json={"url": "http://bad/", "emotion": "joy"})
    assert bad.status_code == 400 and "local network" in bad.json()["detail"]
    page = client.post("/api/images/from-url", json={"url": "https://x/html", "emotion": "joy"})
    assert page.status_code == 400  # downloaded, but not a valid image
    blocked = client.post("/api/images/from-url", json=ok, headers=EVIL_ORIGIN)
    assert blocked.status_code == 403 and len(calls) == 3  # never fetched
