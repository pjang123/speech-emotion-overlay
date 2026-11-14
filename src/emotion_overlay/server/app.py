"""FastAPI app: setup page, overlay page, REST API and the /ws event stream."""

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import unquote

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from emotion_overlay.audio.capture import InputDevice, list_input_devices, pick_device
from emotion_overlay.backup import BackupError, export_setup, read_setup
from emotion_overlay.calibration import Calibration, CalibrationError, CalibrationStore, SpeechStats
from emotion_overlay.compute import GpuInfo, gpu_info
from emotion_overlay.config import (
    AppConfig,
    HotkeyBinding,
    HotkeyConfig,
    ObsConfig,
    default_hotkeys,
    save_config,
)
from emotion_overlay.emotions import EMOTIONS, IMAGE_GROUPS, NEUTRAL, Scores
from emotion_overlay.fetch import FetchError, fetch_image
from emotion_overlay.hotkeys import HotkeyListener, describe_keys
from emotion_overlay.images import FORMATS, TIERS, ImageRecord, ImageStore, ImageValidationError
from emotion_overlay.obs import ObsController, ObsError
from emotion_overlay.paths import web_dir
from emotion_overlay.pipeline import PipelineEvents
from emotion_overlay.server.calibration import CalibrationController
from emotion_overlay.server.display import DisplayState
from emotion_overlay.server.hub import Hub
from emotion_overlay.server.longform import MAX_MEDIA_BYTES, LongformController
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
from emotion_overlay.triggers import Trigger, TriggerError, TriggerStore
from emotion_overlay.triggers import match as match_trigger

log = logging.getLogger(__name__)

WEB_DIR = web_dir()


class AudioEngine(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def set_device(self, device: int | None) -> None: ...
    def set_listening(self, listening: bool) -> None: ...
    def reload_models(self) -> bool: ...
    def set_calibration(self, calibration: Calibration | None) -> None: ...
    def set_calibration_listener(
        self, listener: Callable[[Scores, Scores, SpeechStats], None] | None
    ) -> None: ...
    def set_sentence_observer(
        self, observer: Callable[[np.ndarray, str, Scores, Scores], None] | None
    ) -> None: ...
    def score_sentence(self, audio: np.ndarray) -> tuple[str, Scores, Scores]: ...
    def status(self) -> dict[str, Any]: ...


class DeviceRequest(BaseModel):
    device: int | None


class ListeningRequest(BaseModel):
    listening: bool


class ComputeRequest(BaseModel):
    device: Literal["cpu", "cuda", "auto"]


class ObsConnectionRequest(BaseModel):
    enabled: bool
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    password: str | None = Field(None, max_length=256)  # None = keep the saved one


class ObsMappingRequest(BaseModel):
    scenes: dict[str, str] | None = None
    filter_source: str | None = None
    filters: dict[str, str] | None = None


class ManualRequest(BaseModel):
    # Both None = back to detected emotions.
    emotion: str | None = None
    image_id: str | None = None  # hold this one image instead of an emotion


class HotkeysRequest(BaseModel):
    enabled: bool
    bindings: list[dict[str, Any]]  # validated as HotkeyConfig


class CaptureRequest(BaseModel):
    capturing: bool


# While the setup page records a new key combination, the hotkeys are switched off
# (Windows would deliver an already-registered combination to us, not the browser).
# They come back on their own after this long if the page never says it's done.
CAPTURE_TIMEOUT_S = 30.0


class SaveCalibrationRequest(BaseModel):
    apply: list[str] = []  # recommendation paths to apply, e.g. "vad.min_silence_ms"


class DefaultImageRequest(BaseModel):
    image_id: str | None


class ImageUpdateRequest(BaseModel):
    emotion: str | None = None
    tier: str | None = None
    original_name: str | None = Field(None, max_length=200)  # rename (display only)


class LongformLinkRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class LongformAnalyseRequest(BaseModel):
    track: int = 0


class LongformLabelRequest(BaseModel):
    label: str | None  # an emotion, "not_me", or None (not reviewed)


class ImageFromUrlRequest(BaseModel):
    url: str
    emotion: str
    tier: str = "any"


class TriggerRequest(BaseModel):
    phrases: str | list[str]
    image_id: str
    duration_s: float = 3.0


class TriggerUpdateRequest(BaseModel):
    phrases: str | list[str] | None = None
    image_id: str | None = None
    duration_s: float | None = None


def create_app(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    engine_factory: Callable[[PipelineEvents], AudioEngine] | None = None,
    device_lister: Callable[[], list[InputDevice]] = list_input_devices,
    hotkey_factory: Callable[[Callable[[int], None]], HotkeyListener] | None = None,
    gpu_probe: Callable[[], GpuInfo] = gpu_info,
    obs_factory: Callable[[ObsConfig, Callable[[dict[str, Any]], None]], ObsController]
    | None = None,
    longform_options: dict[str, Any] | None = None,  # tests: speech_prob, check_host
) -> FastAPI:
    store = ImageStore(config.data_dir, config.uploads.max_bytes, config.uploads.max_pixels)
    hub = Hub()
    display = DisplayState(store, config.display)
    obs = obs_factory(config.obs, hub.publish) if obs_factory else None

    def show(message: dict[str, Any]) -> None:
        """Publish a state message to the overlay, and let OBS follow the emotion."""
        hub.publish(message)
        if obs is not None:
            obs.show(message["emotion"])

    show(display.message())

    triggers = TriggerStore(config.data_dir)
    for stale in [t for t in triggers.all() if store.get(t.image_id) is None]:
        triggers.remove(stale.id)  # its image was removed while the app wasn't running

    def on_state(state: EmotionState, new_utterance: bool) -> None:
        show(display.apply(state, new_utterance))

    def show_for(image: ImageRecord, label: str, seconds: float, source: str) -> None:
        message, token = display.trigger(image, label, source)
        show(message)

        def end() -> None:
            ended = display.end_trigger(token)
            if ended:
                show(ended)

        hub.call_later(seconds, end)

    def fire_trigger(trigger: Trigger, phrase: str) -> bool:
        image = store.get(trigger.image_id)
        if image is None:
            return False
        show_for(image, phrase, trigger.duration_s, "word")
        return True

    def on_words(text: str) -> None:
        found = match_trigger(triggers.all(), text)
        if found:
            fire_trigger(*found)

    def set_manual(emotion: str | None) -> None:
        show(display.set_manual(emotion))

    def run_hotkey(binding: HotkeyBinding) -> bool:
        """Do what a hotkey is bound to. False if its image or trigger no longer exists."""
        if binding.action == "auto":
            set_manual(None)
        elif binding.action == "emotion":
            # The same key again releases.
            set_manual(None if binding.emotion == display.manual else binding.emotion)
        elif binding.action == "image":
            image = store.get(binding.image_id or "")
            if image is None:
                return False
            if binding.duration_s:
                show_for(image, image.original_name, binding.duration_s, "hotkey")
            elif display.held_image == image.id:
                set_manual(None)  # the same key again releases
            else:
                show(display.hold_image(image))
        elif binding.action == "trigger":
            trigger = triggers.get(binding.trigger_id or "")
            return trigger is not None and fire_trigger(trigger, trigger.phrases[0])
        return True

    hotkey_lock = threading.Lock()
    active: list[HotkeyBinding] = []  # what the listener has registered, by index
    capture = {"token": 0, "on": False}

    def on_hotkey(index: int) -> None:
        bindings = active  # a restart swaps the list, never edits it
        if 0 <= index < len(bindings) and not run_hotkey(bindings[index]):
            log.info("Hotkey %s points to something that was deleted", bindings[index].keys)

    hotkeys = hotkey_factory(on_hotkey) if hotkey_factory else None

    def start_hotkeys() -> None:
        """(Re)register the configured hotkeys; none while turned off or capturing keys."""
        nonlocal active
        if hotkeys is None:
            return
        with hotkey_lock:
            hotkeys.stop()
            wanted = config.hotkeys.enabled and not capture["on"]
            active = list(config.hotkeys.bindings) if wanted else []
            if active:
                hotkeys.start([b.keys for b in active])

    def hotkeys_problem() -> str | None:
        if hotkeys is None:
            return "not available in this mode"
        if not config.hotkeys.enabled:
            return "turned off"
        return hotkeys.error

    def binding_problem(index: int, binding: HotkeyBinding) -> str | None:
        if binding.action == "image" and store.get(binding.image_id or "") is None:
            return "its image was deleted"
        if binding.action == "trigger" and triggers.get(binding.trigger_id or "") is None:
            return "its word trigger was deleted"
        registered = bool(active) and active == config.hotkeys.bindings
        if registered and hotkeys is not None and index in hotkeys.failed:
            return "another app already uses these keys"
        return None

    def hotkeys_info() -> dict[str, Any]:
        return {
            "enabled": config.hotkeys.enabled,
            "problem": hotkeys_problem(),
            "capturing": capture["on"],
            "bindings": [
                {
                    **b.model_dump(mode="json"),
                    "label": describe_keys(b.keys),
                    "problem": binding_problem(i, b),
                }
                for i, b in enumerate(config.hotkeys.bindings)
            ],
        }

    def manual_info() -> dict[str, Any]:
        return {
            "emotion": display.manual,
            "image_id": display.held_image,
            "hotkeys": {"problem": hotkeys_problem()},
        }

    def persist() -> None:
        if config_path:
            save_config(config, config_path)

    engine = (
        engine_factory(PipelineEvents(on_state=on_state, on_message=hub.publish, on_words=on_words))
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
        show(display.refresh())

    calibration = CalibrationController(
        config, CalibrationStore(config.data_dir), engine, hub.publish, apply_patch
    )
    hub.publish(calibration.message())
    longform = LongformController(
        config, engine, calibration, hub.publish, **(longform_options or {})
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        hub.bind_loop(asyncio.get_running_loop())
        if engine:
            engine.start()
        start_hotkeys()
        if obs:
            obs.start()
            hub.publish(obs.status())
        try:
            yield
        finally:
            if obs:
                obs.stop()
            if hotkeys:
                hotkeys.stop()
            longform.close()
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
        return {"emotions": list(EMOTIONS), "image_groups": list(IMAGE_GROUPS), "neutral": NEUTRAL}

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
        audio = config.audio
        if audio.device_name is None and audio.device is not None:
            # Saved before mics were remembered by name: remember this one's name now.
            current = next((d for d in found if d.index == audio.device), None)
            if current:
                audio.device_name, audio.device_hostapi = current.name, current.hostapi
                persist()
        selected = pick_device(found, audio.device, audio.device_name, audio.device_hostapi)
        return {
            "devices": [asdict(d) for d in found],
            "selected": selected,
            "missing": audio.device_name if audio.device_name and selected is None else None,
        }

    @app.put("/api/device")
    def set_device(body: DeviceRequest) -> dict[str, Any]:
        chosen = None
        if body.device is not None:
            chosen = next((d for d in device_lister() if d.index == body.device), None)
            if chosen is None:
                raise HTTPException(400, "Unknown input device")
        config.audio.device = body.device
        config.audio.device_name = chosen.name if chosen else None
        config.audio.device_hostapi = chosen.hostapi if chosen else None
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

    @app.get("/api/manual")
    def get_manual() -> dict[str, Any]:
        return manual_info()

    @app.put("/api/manual")
    def put_manual(body: ManualRequest) -> dict[str, Any]:
        """Hold an emotion or one image on screen (e.g. from a Stream Deck), or
        send neither to go back to detected emotions."""
        if body.emotion is not None and body.image_id is not None:
            raise HTTPException(400, "Hold an emotion or an image, not both")
        if body.image_id is not None:
            image = store.get(body.image_id)
            if image is None:
                raise HTTPException(404, "Image not found")
            show(display.hold_image(image))
            return manual_info()
        if body.emotion is not None and body.emotion not in EMOTIONS:
            raise HTTPException(400, f"Unknown emotion '{body.emotion}'")
        set_manual(body.emotion)
        return manual_info()

    # --- hotkeys ------------------------------------------------------------

    @app.get("/api/hotkeys")
    def get_hotkeys() -> dict[str, Any]:
        return hotkeys_info()

    @app.put("/api/hotkeys")
    def put_hotkeys(body: HotkeysRequest) -> dict[str, Any]:
        """Replace the whole list of hotkeys; it applies right away."""
        try:
            new = HotkeyConfig.model_validate(body.model_dump())
        except ValidationError as exc:
            problems = "; ".join(
                e["msg"].removeprefix("Value error, ") for e in exc.errors(include_url=False)
            )
            raise HTTPException(400, problems) from exc
        config.hotkeys.enabled, config.hotkeys.bindings = new.enabled, new.bindings
        persist()
        start_hotkeys()
        return hotkeys_info()

    # Literal paths before the {index} route below.
    @app.post("/api/hotkeys/reset")
    def reset_hotkeys() -> dict[str, Any]:
        config.hotkeys.bindings = default_hotkeys()
        persist()
        start_hotkeys()
        return hotkeys_info()

    @app.put("/api/hotkeys/capture")
    def put_capture(body: CaptureRequest) -> dict[str, Any]:
        """Switch the hotkeys off while the setup page records a new combination."""
        capture["token"] += 1
        capture["on"] = body.capturing
        start_hotkeys()
        if body.capturing:
            token = capture["token"]

            def give_up() -> None:
                if capture["token"] == token and capture["on"]:
                    capture["on"] = False
                    start_hotkeys()

            hub.call_later(CAPTURE_TIMEOUT_S, give_up)
        return hotkeys_info()

    @app.post("/api/hotkeys/{index}/press")
    def press_hotkey(index: int) -> dict[str, Any]:
        """Do what a hotkey does without pressing it (setup page "Test", Stream Deck)."""
        bindings = config.hotkeys.bindings
        if not 0 <= index < len(bindings):
            raise HTTPException(404, "Hotkey not found")
        if not run_hotkey(bindings[index]):
            raise HTTPException(404, "That hotkey's image or word trigger was deleted")
        return manual_info()

    def compute_info() -> dict[str, Any]:
        chosen = {config.asr.device, config.classifier.device, config.audio_emotion.device}
        return {
            # "mixed" when config.yaml sets the models differently by hand.
            "device": chosen.pop() if len(chosen) == 1 else "mixed",
            "gpu": gpu_probe().to_api(),
            "running": engine.status().get("devices") if engine else None,
        }

    @app.get("/api/compute")
    def get_compute() -> dict[str, Any]:
        return compute_info()

    @app.put("/api/compute")
    def put_compute(body: ComputeRequest) -> dict[str, Any]:
        """Run the models on the CPU, the GPU, or the GPU when there is one ("auto")."""
        if body.device == "cuda" and not (gpu := gpu_probe()).available:
            raise HTTPException(400, f"The GPU can't be used: {gpu.reason}")
        before = (config.asr.device, config.classifier.device, config.audio_emotion.device)
        changed = before != (body.device,) * 3
        if engine and changed and not engine.status().get("ready", True):
            # Checked before touching the config: startup reads it while loading.
            raise HTTPException(409, "The models are still loading; try again in a moment")
        config.asr.device = config.classifier.device = config.audio_emotion.device = body.device
        if engine and changed and not engine.reload_models():
            config.asr.device, config.classifier.device, config.audio_emotion.device = before
            raise HTTPException(409, "The models are still loading; try again in a moment")
        persist()
        return compute_info()

    # --- OBS ----------------------------------------------------------------

    def need_obs() -> ObsController:
        if obs is None:
            raise HTTPException(409, "OBS control isn't available")
        return obs

    def obs_names(
        request: str, key: str, field: str, data: dict[str, Any] | None = None
    ) -> list[str]:
        try:
            items = need_obs().call(request, data).get(key, [])
        except ObsError:
            return []
        return sorted({str(item.get(field)) for item in items if item.get(field)})

    def obs_info() -> dict[str, Any]:
        o = config.obs
        connected = obs is not None and obs.state == "connected"
        scenes = obs_names("GetSceneList", "scenes", "sceneName") if connected else []
        inputs = obs_names("GetInputList", "inputs", "inputName") if connected else []
        filters = (
            obs_names(
                "GetSourceFilterList", "filters", "filterName", {"sourceName": o.filter_source}
            )
            if connected and o.filter_source
            else []
        )
        return {
            "available": obs is not None,
            "enabled": o.enabled,
            "host": o.host,
            "port": o.port,
            "password_set": bool(o.password),  # the password itself never leaves the server
            "scenes": o.scenes,
            "filter_source": o.filter_source,
            "filters": o.filters,
            "status": obs.status() if obs else None,
            "obs_scenes": scenes,
            "obs_sources": sorted(set(scenes) | set(inputs)),  # filters can be on either
            "obs_filters": filters,
        }

    @app.get("/api/obs")
    def get_obs() -> dict[str, Any]:
        return obs_info()

    @app.put("/api/obs/connection")
    def put_obs_connection(body: ObsConnectionRequest) -> dict[str, Any]:
        controller = need_obs()
        update = {"enabled": body.enabled, "host": body.host.strip(), "port": body.port}
        if body.password is not None:
            update["password"] = body.password
        try:
            new = ObsConfig.model_validate({**config.obs.model_dump(), **update})
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        for name in ("enabled", "host", "port", "password"):
            setattr(config.obs, name, getattr(new, name))
        persist()
        controller.reconnect()
        return obs_info()

    @app.put("/api/obs/mappings")
    def put_obs_mappings(body: ObsMappingRequest) -> dict[str, Any]:
        controller = need_obs()
        update = body.model_dump(exclude_none=True)
        try:
            new = ObsConfig.model_validate({**config.obs.model_dump(), **update})
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        for name in ("scenes", "filter_source", "filters"):
            setattr(config.obs, name, getattr(new, name))
        persist()
        controller.mappings_changed()
        return obs_info()

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
        show(display.refresh())  # transition, fallbacks, tiers may have changed
        return editable_settings(config)

    @app.post("/api/settings/reset")
    def reset_all_settings() -> dict[str, Any]:
        reset_settings(config)
        persist()
        show(display.refresh())
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
        show(display.refresh())
        return record.to_api()

    @app.post("/api/images/from-url", status_code=201)
    def image_from_url(body: ImageFromUrlRequest) -> dict[str, Any]:
        # Sync route: runs in the threadpool, so the download doesn't block the event loop.
        try:
            data, name = fetch_image(body.url, config.uploads.max_bytes)
            record = store.add(data, body.emotion, name, body.tier)
        except FetchError as exc:
            raise HTTPException(400, f"Couldn't add that link: {exc}") from exc
        except ImageValidationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        show(display.refresh())
        return record.to_api()

    @app.delete("/api/images/{image_id}", status_code=204)
    def delete_image(image_id: str) -> Response:
        if not store.remove(image_id):
            raise HTTPException(404, "Image not found")
        triggers.remove_for_image(image_id)
        show(display.refresh())
        return Response(status_code=204)

    @app.put("/api/images/default")
    def set_default_image(body: DefaultImageRequest) -> dict[str, Any]:
        try:
            store.set_default(body.image_id)
        except KeyError as exc:
            raise HTTPException(404, "Image not found") from exc
        show(display.refresh())
        return {"default_image": store.default_id}

    # Must come after /api/images/default so "default" isn't taken as an image ID.
    @app.put("/api/images/{image_id}")
    def update_image(image_id: str, body: ImageUpdateRequest) -> dict[str, Any]:
        try:
            record = store.update(
                image_id, emotion=body.emotion, tier=body.tier, name=body.original_name
            )
        except KeyError as exc:
            raise HTTPException(404, "Image not found") from exc
        except ImageValidationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        show(display.refresh())
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

    # --- word triggers ----------------------------------------------------

    def _trigger_image(image_id: str) -> None:
        if store.get(image_id) is None:
            raise HTTPException(400, "Pick an image that exists")

    @app.get("/api/triggers")
    def list_triggers() -> dict[str, Any]:
        return {"triggers": [t.to_api() for t in triggers.all()]}

    @app.post("/api/triggers", status_code=201)
    def add_trigger(body: TriggerRequest) -> dict[str, Any]:
        _trigger_image(body.image_id)
        try:
            return triggers.add(body.phrases, body.image_id, body.duration_s).to_api()
        except TriggerError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/triggers/{trigger_id}")
    def update_trigger(trigger_id: str, body: TriggerUpdateRequest) -> dict[str, Any]:
        if body.image_id is not None:
            _trigger_image(body.image_id)
        try:
            return triggers.update(
                trigger_id, body.phrases, body.image_id, body.duration_s
            ).to_api()
        except KeyError as exc:
            raise HTTPException(404, "Trigger not found") from exc
        except TriggerError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/triggers/{trigger_id}", status_code=204)
    def delete_trigger(trigger_id: str) -> Response:
        if not triggers.remove(trigger_id):
            raise HTTPException(404, "Trigger not found")
        return Response(status_code=204)

    @app.post("/api/triggers/{trigger_id}/test")
    def test_trigger(trigger_id: str) -> dict[str, Any]:
        """Fire a trigger without saying it, e.g. to check it in OBS."""
        trigger = triggers.get(trigger_id)
        if trigger is None or not fire_trigger(trigger, trigger.phrases[0]):
            raise HTTPException(404, "Trigger or its image not found")
        return {"fired": trigger.id}

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

    # --- long-form calibration ---------------------------------------------

    def longform_call(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return action()
        except CalibrationError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/longform")
    def get_longform() -> dict[str, Any]:
        return longform.message(with_sentences=True)

    @app.post("/api/longform/record")
    def longform_record() -> dict[str, Any]:
        return longform_call(longform.start_recording)

    @app.post("/api/longform/stop")
    def longform_stop() -> dict[str, Any]:
        return longform_call(longform.stop_recording)

    @app.post("/api/longform/upload")
    async def longform_upload(request: Request) -> dict[str, Any]:
        """The raw file as the request body (streamed to a temporary file, so a
        multi-GB recording never sits in memory). Its name comes in X-File-Name."""
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_MEDIA_BYTES:
            raise HTTPException(413, f"the file is larger than {MAX_MEDIA_BYTES // 1024**3} GB")
        name = unquote(request.headers.get("x-file-name", ""))  # URI-encoded by the page
        try:
            path = longform.begin_upload(name)
        except CalibrationError as exc:
            raise HTTPException(409, str(exc)) from exc
        size = 0
        try:
            with path.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        raise HTTPException(
                            413, f"the file is larger than {MAX_MEDIA_BYTES // 1024**3} GB"
                        )
                    await asyncio.to_thread(out.write, chunk)  # keep the loop free
            if size == 0:
                raise HTTPException(400, "the file is empty")
        except HTTPException as exc:
            longform.upload_failed(str(exc.detail))
            raise
        except Exception as exc:  # client went away, disk full...
            longform.upload_failed(f"Upload failed: {exc}")
            raise HTTPException(400, f"Upload failed: {exc}") from exc
        return await asyncio.to_thread(longform.upload_done)

    @app.post("/api/longform/link")
    def longform_link(body: LongformLinkRequest) -> dict[str, Any]:
        return longform_call(lambda: longform.start_link(body.url))

    @app.post("/api/longform/analyse")
    def longform_analyse(body: LongformAnalyseRequest) -> dict[str, Any]:
        return longform_call(lambda: longform.analyse(body.track))

    @app.post("/api/longform/calibrate")
    def longform_calibrate() -> dict[str, Any]:
        return longform_call(longform.calibrate)

    @app.post("/api/longform/cancel")
    def longform_cancel() -> dict[str, Any]:
        return longform.cancel()

    @app.put("/api/longform/sentences/{sentence_id}")
    def longform_label(sentence_id: int, body: LongformLabelRequest) -> dict[str, Any]:
        return longform_call(lambda: longform.set_label(sentence_id, body.label))

    @app.get("/api/longform/sentences/{sentence_id}/audio")
    def longform_audio(sentence_id: int) -> Response:
        try:
            data = longform.wav(sentence_id)
        except CalibrationError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # --- backup -----------------------------------------------------------

    @app.get("/api/export")
    def export() -> Response:
        return Response(
            export_setup(store, config, triggers.all()),
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
            # Triggers refer to images by their ids inside the zip; check them before
            # anything changes, then point them at the ids the images get here.
            zip_ids = {i for i in bundle.image_ids if i}
            imported = TriggerStore.validate(bundle.triggers, zip_ids)
            created = store.replace_all(bundle.images, bundle.default_index)
            new_id = {
                old: rec.id for old, rec in zip(bundle.image_ids, created, strict=True) if old
            }
            triggers.replace_all(
                [Trigger(t.id, t.phrases, new_id[t.image_id], t.duration_s) for t in imported]
            )
            commit_settings(config, validated)
            return len(bundle.images)

        try:
            count = await asyncio.to_thread(restore)
        except (BackupError, SettingsError, ImageValidationError, TriggerError) as exc:
            raise HTTPException(400, f"Import failed, nothing was changed: {exc}") from exc
        persist()
        show(display.refresh())
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
