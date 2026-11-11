"""Storage for user-uploaded images, with strict validation.

Security notes:
- The file type is decided by parsing the content with Pillow (restricted to four
  formats), never by the client's filename or Content-Type.
- Files are stored under random IDs; the client filename is kept only as a display
  label, so path traversal through names is impossible.
- Lookups go through the manifest by a validated hex ID, never through a user path.
- The manifest is validated on load, so a hand-edited file cannot point outside
  the images directory.
"""

import io
import json
import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PIL import Image

from emotion_overlay.emotions import IMAGE_GROUPS, NEUTRAL

log = logging.getLogger(__name__)

# Pillow format -> (extension, MIME type). SVG is excluded on purpose: it can carry script.
FORMATS: dict[str, tuple[str, str]] = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}
# Intensity tiers: "any" images are used at every intensity; "mild"/"strong" are
# preferred below/above display.strong_threshold when they exist.
TIERS: tuple[str, ...] = ("any", "mild", "strong")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ImageValidationError(ValueError):
    status_code = 400


class ImageTooLargeError(ImageValidationError):
    status_code = 413


@dataclass(frozen=True)
class ImageRecord:
    id: str
    emotion: str
    format: str
    original_name: str
    width: int
    height: int
    tier: str = "any"

    @property
    def filename(self) -> str:
        return f"{self.id}.{FORMATS[self.format][0]}"

    @property
    def content_type(self) -> str:
        return FORMATS[self.format][1]

    @property
    def url(self) -> str:
        return f"/images/{self.id}"

    def to_api(self) -> dict[str, object]:
        return {**asdict(self), "url": self.url}


@dataclass(frozen=True)
class NewImage:
    """An image to add, e.g. from an imported setup."""

    data: bytes
    emotion: str
    original_name: str | None = None
    tier: str = "any"


def inspect_image(data: bytes, max_pixels: int) -> tuple[str, int, int]:
    """Return (format, width, height) or raise ImageValidationError."""
    try:
        with Image.open(io.BytesIO(data), formats=list(FORMATS)) as img:
            fmt, (width, height) = img.format, img.size
            if fmt not in FORMATS:
                raise ImageValidationError("unsupported image format")
            if width * height > max_pixels:
                raise ImageValidationError(f"image is too large ({width}x{height})")
            img.verify()
    except ImageValidationError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageValidationError("not a valid PNG, JPEG, WebP or GIF image") from exc
    return fmt, width, height


def clean_display_name(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    return _CONTROL_RE.sub("", base).strip()[:120] or "image"


def _check_labels(emotion: str, tier: str) -> None:
    if emotion not in IMAGE_GROUPS:
        raise ImageValidationError(f"unknown emotion '{emotion}'")
    if tier not in TIERS:
        raise ImageValidationError(f"unknown tier '{tier}'")


def prefer_tier(images: list[ImageRecord], tier: str) -> list[ImageRecord]:
    """Images of the wanted tier, else untiered ones, else whatever exists."""
    exact = [i for i in images if i.tier == tier]
    if exact:
        return exact
    return [i for i in images if i.tier == "any"] or images


class ImageStore:
    def __init__(self, root: Path, max_bytes: int, max_pixels: int) -> None:
        self._dir = root / "images"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._manifest = root / "manifest.json"
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._lock = threading.RLock()
        self._records: dict[str, ImageRecord] = {}
        self._default_id: str | None = None
        self._load()

    @property
    def default_id(self) -> str | None:
        return self._default_id

    def all(self) -> list[ImageRecord]:
        with self._lock:
            return list(self._records.values())

    def for_emotion(self, emotion: str) -> list[ImageRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.emotion == emotion]

    def get(self, image_id: str) -> ImageRecord | None:
        if not _ID_RE.match(image_id):
            return None
        with self._lock:
            return self._records.get(image_id)

    def path(self, record: ImageRecord) -> Path:
        return self._dir / record.filename

    def add(
        self, data: bytes, emotion: str, original_name: str | None, tier: str = "any"
    ) -> ImageRecord:
        record = self._create(NewImage(data, emotion, original_name, tier))
        with self._lock:
            self._records[record.id] = record
            self._save()
        return record

    def update(
        self,
        image_id: str,
        *,
        emotion: str | None = None,
        tier: str | None = None,
        name: str | None = None,
    ) -> ImageRecord:
        with self._lock:
            record = self.get(image_id)
            if record is None:
                raise KeyError(image_id)
            if name is not None and not _CONTROL_RE.sub("", name).strip():
                raise ImageValidationError("the name can't be empty")
            updated = replace(
                record,
                emotion=emotion or record.emotion,
                tier=tier or record.tier,
                original_name=clean_display_name(name)
                if name is not None
                else record.original_name,
            )
            _check_labels(updated.emotion, updated.tier)
            self._records[record.id] = updated
            self._save()
            return updated

    def remove(self, image_id: str) -> bool:
        with self._lock:
            record = self.get(image_id)
            if record is None:
                return False
            del self._records[record.id]
            if self._default_id == record.id:
                self._default_id = None
            self._save()
            self.path(record).unlink(missing_ok=True)
            return True

    def set_default(self, image_id: str | None) -> None:
        with self._lock:
            if image_id is not None and self.get(image_id) is None:
                raise KeyError(image_id)
            self._default_id = image_id
            self._save()

    def replace_all(self, images: list[NewImage], default_index: int | None) -> list[ImageRecord]:
        """Swap in a whole new image set. Every image is validated and written before
        anything existing is touched, so a bad import leaves the current setup intact.
        Returns the new records in the same order as `images`."""
        created: list[ImageRecord] = []
        try:
            for image in images:
                created.append(self._create(image))
        except Exception:
            for record in created:
                self.path(record).unlink(missing_ok=True)
            raise
        with self._lock:
            old = list(self._records.values())
            self._records = {r.id: r for r in created}
            self._default_id = created[default_index].id if default_index is not None else None
            self._save()
        for record in old:
            self.path(record).unlink(missing_ok=True)
        return created

    def pool(self, emotion: str, fallbacks: dict[str, str], tier: str) -> list[ImageRecord]:
        """Images eligible for an emotion: its own, its fallback's, the default, then neutral."""
        with self._lock:
            for candidate in (emotion, fallbacks.get(emotion)):
                images = self.for_emotion(candidate) if candidate else []
                if images:
                    return prefer_tier(images, tier)
            if self._default_id is not None:
                return [self._records[self._default_id]]
            return prefer_tier(self.for_emotion(NEUTRAL), tier)

    # --- internals ----------------------------------------------------------

    def _create(self, image: NewImage) -> ImageRecord:
        """Validate and write one image file; does not register it."""
        _check_labels(image.emotion, image.tier)
        if len(image.data) > self._max_bytes:
            raise ImageTooLargeError(f"file is larger than {self._max_bytes // (1024 * 1024)} MB")
        fmt, width, height = inspect_image(image.data, self._max_pixels)
        record = ImageRecord(
            id=uuid.uuid4().hex,
            emotion=image.emotion,
            format=fmt,
            original_name=clean_display_name(image.original_name),
            width=width,
            height=height,
            tier=image.tier,
        )
        # Original bytes are kept so animated GIFs survive; they are only ever
        # served back with a fixed image MIME type and `nosniff`.
        self.path(record).write_bytes(image.data)
        return record

    def _load(self) -> None:
        if not self._manifest.exists():
            return
        try:
            raw = json.loads(self._manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("Could not read %s; starting with no images", self._manifest)
            return
        for item in raw.get("images", []):
            try:
                record = ImageRecord(**item)
            except TypeError:
                log.warning("Skipping malformed manifest entry: %r", item)
                continue
            valid = (
                _ID_RE.match(record.id)
                and record.emotion in IMAGE_GROUPS
                and record.tier in TIERS
                and record.format in FORMATS
                and self.path(record).is_file()
            )
            if valid:
                self._records[record.id] = record
            else:
                log.warning("Skipping invalid or missing image %r", record.id)
        default = raw.get("default_image")
        if isinstance(default, str) and default in self._records:
            self._default_id = default

    def _save(self) -> None:
        data = {
            "images": [asdict(r) for r in self._records.values()],
            "default_image": self._default_id,
        }
        tmp = self._manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._manifest)
