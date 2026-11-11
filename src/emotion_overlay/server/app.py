"""FastAPI app: setup page, overlay page, REST API and the /ws event stream."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from emotion_overlay.audio.capture import InputDevice, list_input_devices
from emotion_overlay.backup import BackupError, export_setup, read_setup
from emotion_overlay.calibration import Calibration, CalibrationError, CalibrationStore, SpeechStats
from emotion_overlay.config import AppConfig, save_config
from emotion_overlay.emotions import EMOTIONS, NEUTRAL, Scores
from emotion_overlay.images import FORMATS, TIERS, ImageStore, ImageValidationError
from emotion_overlay.pipeline import PipelineEvents
from emotion_overlay.server.calibration import CalibrationController
from emotion_overlay.server.display import DisplayState
from emotion_overlay.server.hub import Hub
from emotion_overlay.server.security import install_security
from emotion_overlay.settings import (
    SettingsError,
    apply_settings,
    commit_settings,
    editable_settings,
    reset_settings,
    validate_settings,
)
from emotion_overlay.state_machine import EmotionState

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


class AudioEngine(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def set_device(self, device: int | None) -> None: ...
    def set_listening(self, listening: bool) -> None: ...
    def set_calibration(self, calibration: Calibration | None) -> None: ...
    def set_calibration_listener(
        self, listener: Callable[[Scores, Scores, SpeechStats], None] | None
    ) -> None: ...
    def status(self) -> dict[str, Any]: ...


class DeviceRequest(BaseModel):
    device: int | None


class ListeningRequest(BaseModel):
    listening: bool


class SaveCalibrationRequest(BaseModel):
    apply: list[str] = []  # recommendation paths to apply, e.g. "vad.min_silence_ms"


class DefaultImageRequest(BaseModel):
    image_id: str | None


class ImageUpdateRequest(BaseModel):
    emotion: str | None = None
    tier: str | None = None


def create_app(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    engine_factory: Callable[[PipelineEvents], AudioEngine] | None = None,
    device_lister: Callable[[], list[InputDevice]] = list_input_devices,
) -> FastAPI:
    store = ImageStore(config.data_dir, config.uploads.max_bytes, config.uploads.max_pixels)
    hub = Hub()
    display = DisplayState(store, config.display)
    hub.publish(display.message())

    def on_state(state: EmotionState, new_utterance: bool) -> None:
        hub.publish(display.apply(state, new_utterance))

    def persist() -> None:
        if config_path:
            save_config(config, config_path)

    engine = (
        engine_factory(PipelineEvents(on_state=on_state, on_message=hub.publish))
        if engine_factory
        else None
    )
    if engine is None:
        hub.publish({"type": "status", "state": "disabled", "message": "Audio disabled"})

    def apply_patch(patch: dict[str, dict[str, Any]]) -> None:
        """Apply a validated settings patch from calibration, as the settings page would."""
        try:
            apply_settings(config, patch)
        except SettingsError as exc:
            raise CalibrationError(f"could not apply settings: {exc}") from exc
        persist()
        hub.publish(display.refresh())

    calibration = CalibrationController(
        config, CalibrationStore(config.data_dir), engine, hub.publish, apply_patch
    )
    hub.publish(calibration.message())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        hub.bind_loop(asyncio.get_running_loop())
        if engine:
            engine.start()
        try:
            yield
        finally:
            if engine:
                engine.stop()

    app = FastAPI(
        title="Emotion Overlay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    install_security(app, config.server)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # --- pages ------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/setup")

    @app.get("/setup", include_in_schema=False)
    def setup_page() -> FileResponse:
        return FileResponse(WEB_DIR / "setup" / "index.html")

    @app.get("/overlay", include_in_schema=False)
    def overlay_page() -> FileResponse:
        return FileResponse(WEB_DIR / "overlay" / "index.html")

    # --- api --------------------------------------------------------------

    @app.get("/api/emotions")
    def emotions() -> dict[str, Any]:
        return {"emotions": list(EMOTIONS), "neutral": NEUTRAL}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "engine": engine.status() if engine else hub.latest("status"),
            "state": display.message(),
        }

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return config.model_dump(mode="json")

    @app.get("/api/devices")
    def devices() -> dict[str, Any]:
        try:
            found = device_lister()
        except Exception as exc:
            log.exception("Could not list audio devices")
            raise HTTPException(503, f"Could not list audio devices: {exc}") from exc
        return {"devices": [asdict(d) for d in found], "selected": config.audio.device}

    @app.put("/api/device")
    def set_device(body: DeviceRequest) -> dict[str, Any]:
        if body.device is not None and body.device not in {d.index for d in device_lister()}:
            raise HTTPException(400, "Unknown input device")
        config.audio.device = body.device
        if engine:
            engine.set_device(body.device)
        persist()
        return {"selected": body.device, "engine": engine.status() if engine else None}

    @app.put("/api/listening")
    def set_listening(body: ListeningRequest) -> dict[str, Any]:
        if engine is None:
            raise HTTPException(409, "Audio is disabled (started with --no-audio)")
        engine.set_listening(body.listening)
        return engine.status()

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return editable_settings(config)

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            apply_settings(config, patch)
        except SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc
        persist()
        hub.publish(display.refresh())  # transition, fallbacks, tiers may have changed
        return editable_settings(config)

    @app.post("/api/settings/reset")
    def reset_all_settings() -> dict[str, Any]:
        reset_settings(config)
        persist()
        hub.publish(display.refresh())
        return editable_settings(config)

    @app.get("/api/images")
    def list_images() -> dict[str, Any]:
        return {
            "images": [r.to_api() for r in store.all()],
            "default_image": store.default_id,
            "max_bytes": config.uploads.max_bytes,
            "formats": [mime for _, mime in FORMATS.values()],
            "tiers": list(TIERS),
        }

    @app.post("/api/images", status_code=201)
    async def upload_image(
        emotion: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        tier: Annotated[str, Form()] = "any",
    ) -> dict[str, Any]:
        # Read one byte past the limit so oversize files are detected without
        # holding an arbitrarily large upload in memory.
        data = await file.read(config.uploads.max_bytes + 1)
        try:
            record = await asyncio.to_thread(store.add, data, emotion, file.filename, tier)
        except ImageValidationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        hub.publish(display.refresh())
        return record.to_api()

    @app.delete("/api/images/{image_id}", status_code=204)
    def delete_image(image_id: str) -> Response:
        if not store.remove(image_id):
            raise HTTPException(404, "Image not found")
        hub.publish(display.refresh())
        return Response(status_code=204)

    @app.put("/api/images/default")
    def set_default_image(body: DefaultImageRequest) -> dict[str, Any]:
        try:
            store.set_default(body.image_id)
        except KeyError as exc:
            raise HTTPException(404, "Image not found") from exc
        hub.publish(display.refresh())
        return {"default_image": store.default_id}

    # Must come after /api/images/default so "default" isn't taken as an image ID.
    @app.put("/api/images/{image_id}")
    def update_image(image_id: str, body: ImageUpdateRequest) -> dict[str, Any]:
        try:
            record = store.update(image_id, emotion=body.emotion, tier=body.tier)
        except KeyError as exc:
            raise HTTPException(404, "Image not found") from exc
        except ImageValidationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        hub.publish(display.refresh())
        return record.to_api()

    @app.get("/images/{image_id}", include_in_schema=False)
    def serve_image(image_id: str) -> FileResponse:
        record = store.get(image_id)
        if record is None:
            raise HTTPException(404, "Image not found")
        # IDs are never reused, so the browser (and OBS) can cache aggressively.
        return FileResponse(
            store.path(record),
            media_type=record.content_type,
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    # --- calibration ------------------------------------------------------

    @app.get("/api/calibration")
    def get_calibration() -> dict[str, Any]:
        return calibration.message()

    # Literal path before the {action} route below.
    @app.post("/api/calibration/save")
    def save_calibration(body: SaveCalibrationRequest | None = None) -> dict[str, Any]:
        try:
            return calibration.save(apply=body.apply if body else [])
        except CalibrationError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/calibration/{action}")
    def calibration_action(action: str) -> dict[str, Any]:
        actions = {
            "start": calibration.start,
            "skip": calibration.skip,
            "back": calibration.back,
            "finish": calibration.finish_early,
            "cancel": calibration.cancel,
        }
        if action not in actions:
            raise HTTPException(404, "Unknown calibration action")
        try:
            return actions[action]()
        except CalibrationError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/calibration")
    def delete_calibration() -> dict[str, Any]:
        return calibration.clear()

    # --- backup -----------------------------------------------------------

    @app.get("/api/export")
    def export() -> Response:
        return Response(
            export_setup(store, config),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="emotion-overlay-setup.zip"'},
        )

    @app.post("/api/import")
    async def import_setup(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        limit = config.uploads.max_import_bytes
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"setup zip is larger than {limit // (1024 * 1024)} MB")

        def restore() -> int:
            bundle = read_setup(data, config.uploads.max_bytes, config.uploads.max_pixels, limit)
            validated = validate_settings(config, bundle.settings, strict=False)
            store.replace_all(bundle.images, bundle.default_index)  # validates before replacing
            commit_settings(config, validated)
            return len(bundle.images)

        try:
            count = await asyncio.to_thread(restore)
        except (BackupError, SettingsError, ImageValidationError) as exc:
            raise HTTPException(400, f"Import failed, nothing was changed: {exc}") from exc
        persist()
        hub.publish(display.refresh())
        return {"images": count, "settings": editable_settings(config)}

    # --- websocket --------------------------------------------------------

    @app.websocket("/ws")
    async def events(websocket: WebSocket) -> None:
        include_debug = websocket.query_params.get("debug") == "1"
        await websocket.accept()
        queue, snapshot = hub.subscribe(include_debug)

        async def pump() -> None:
            for message in snapshot:
                await websocket.send_json(message)
            while True:
                await websocket.send_json(await queue.get())

        sender = asyncio.create_task(pump())
        try:
            while True:  # we never expect input; this just detects disconnects
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            hub.unsubscribe(queue)

    return app
