"""Export and import a whole setup (settings + images) as a zip.

Zip layout:
    settings.yaml    editable settings only (see settings.py)
    manifest.json    {"version": 1, "default_image": id, "images": [{id, emotion, tier,
                      original_name, file}]}
    images/<id>.<ext>

Imports are treated as untrusted (setups get shared between streamers):
- Nothing is extracted by its path inside the zip. Only `settings.yaml`,
  `manifest.json` and `images/<32 hex>.<ext>` entries named by the manifest are
  read into memory, so "../" entries (zip slip) can't write anywhere.
- Entry count, per-file size and total size are capped, and reads are capped by
  bytes actually read, not the sizes the zip header claims (zip bombs).
- Every image goes through the same validation as a normal upload.
- Only live-editable settings are applied; server address, debug recording,
  models and limits in an imported file are ignored.
- Everything is validated before anything is changed.
"""

import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any

import yaml

from emotion_overlay.config import AppConfig
from emotion_overlay.emotions import IMAGE_GROUPS
from emotion_overlay.images import (
    TIERS,
    ImageStore,
    ImageValidationError,
    NewImage,
    clean_display_name,
    inspect_image,
)
from emotion_overlay.settings import editable_settings
from emotion_overlay.triggers import Trigger

SETTINGS_NAME = "settings.yaml"
MANIFEST_NAME = "manifest.json"
TRIGGERS_NAME = "triggers.json"
MAX_ENTRIES = 1000
MAX_TEXT_BYTES = 1024 * 1024
_IMAGE_PATH_RE = re.compile(r"^images/[0-9a-f]{32}\.(png|jpg|webp|gif)$")


class BackupError(ValueError):
    pass


@dataclass(frozen=True)
class SetupBundle:
    settings: dict[str, Any]
    images: list[NewImage]
    default_index: int | None
    image_ids: list[str | None]  # each image's id in the zip, for remapping triggers
    triggers: list[Any]  # raw, validated by TriggerStore.validate once images have new ids


def export_setup(store: ImageStore, config: AppConfig, triggers: list[Trigger]) -> bytes:
    records = store.all()
    manifest = {
        "version": 1,
        "default_image": store.default_id,
        "images": [
            {
                "id": r.id,
                "emotion": r.emotion,
                "tier": r.tier,
                "original_name": r.original_name,
                "file": f"images/{r.filename}",
            }
            for r in records
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(SETTINGS_NAME, yaml.safe_dump(editable_settings(config), sort_keys=False))
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        zf.writestr(TRIGGERS_NAME, json.dumps([t.to_api() for t in triggers], indent=2))
        for r in records:
            zf.write(store.path(r), f"images/{r.filename}")
    return buffer.getvalue()


def read_setup(data: bytes, max_image_bytes: int, max_pixels: int, max_total: int) -> SetupBundle:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return _read_bundle(zf, max_image_bytes, max_pixels, max_total)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, EOFError, NotImplementedError) as exc:
        # RuntimeError: encrypted entries; NotImplementedError: unsupported compression
        raise BackupError(f"not a readable setup zip ({exc})") from exc


def _read_bundle(
    zf: zipfile.ZipFile, max_image_bytes: int, max_pixels: int, max_total: int
) -> SetupBundle:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise BackupError("zip has too many entries")
    by_name = {info.filename: info for info in infos}

    manifest_raw = _read(zf, by_name.get(MANIFEST_NAME), MANIFEST_NAME, MAX_TEXT_BYTES)
    if manifest_raw is None:
        raise BackupError("zip has no manifest.json; is this an exported setup?")
    try:
        manifest = json.loads(manifest_raw)
    except ValueError as exc:
        raise BackupError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("images"), list):
        raise BackupError("manifest.json has an unexpected structure")

    settings_raw = _read(zf, by_name.get(SETTINGS_NAME), SETTINGS_NAME, MAX_TEXT_BYTES)
    try:
        settings = yaml.safe_load(settings_raw) if settings_raw else {}
    except yaml.YAMLError as exc:
        raise BackupError("settings.yaml is not valid YAML") from exc
    if not isinstance(settings, dict):
        raise BackupError("settings.yaml has an unexpected structure")

    triggers_raw = _read(zf, by_name.get(TRIGGERS_NAME), TRIGGERS_NAME, MAX_TEXT_BYTES)
    try:
        triggers = json.loads(triggers_raw) if triggers_raw else []
    except ValueError as exc:
        raise BackupError("triggers.json is not valid JSON") from exc
    if not isinstance(triggers, list):
        raise BackupError("triggers.json has an unexpected structure")

    images: list[NewImage] = []
    image_ids: list[str | None] = []
    default_index = None
    total = 0
    for entry in manifest["images"][:MAX_ENTRIES]:
        if not isinstance(entry, dict):
            raise BackupError("manifest.json has an invalid image entry")
        path = entry.get("file")
        if not isinstance(path, str) or not _IMAGE_PATH_RE.match(path) or path not in by_name:
            raise BackupError(f"manifest lists a missing or invalid file: {path!r}")
        emotion, tier = entry.get("emotion"), entry.get("tier", "any")
        if emotion not in IMAGE_GROUPS or tier not in TIERS:
            raise BackupError(f"{path}: unknown emotion or tier")
        blob = _read(zf, by_name[path], path, max_image_bytes)
        assert blob is not None  # noqa: S101 - entry exists, checked above
        total += len(blob)
        if total > max_total:
            raise BackupError("images in the zip are too large in total")
        try:
            inspect_image(blob, max_pixels)
        except ImageValidationError as exc:
            raise BackupError(f"{path}: {exc}") from exc
        name = entry.get("original_name")
        if entry.get("id") is not None and entry.get("id") == manifest.get("default_image"):
            default_index = len(images)
        images.append(
            NewImage(
                blob, emotion, clean_display_name(name if isinstance(name, str) else None), tier
            )
        )
        image_ids.append(entry["id"] if isinstance(entry.get("id"), str) else None)
    return SetupBundle(settings, images, default_index, image_ids, triggers)


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo | None, name: str, limit: int) -> bytes | None:
    if info is None:
        return None
    if info.file_size > limit:
        raise BackupError(f"{name} is too large")
    with zf.open(info) as f:
        blob = f.read(limit + 1)  # the header's file_size can lie; cap what we actually read
    if len(blob) > limit:
        raise BackupError(f"{name} is too large")
    return blob
