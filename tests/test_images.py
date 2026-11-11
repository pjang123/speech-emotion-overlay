import json
from pathlib import Path

import pytest

from conftest import make_image
from emotion_overlay.images import (
    ImageStore,
    ImageTooLargeError,
    ImageValidationError,
    NewImage,
)


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path, max_bytes=200_000, max_pixels=1000 * 1000)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_accepts_supported_formats(store: ImageStore, fmt: str) -> None:
    record = store.add(make_image(fmt), "joy", f"face.{fmt.lower()}")
    assert record.format == fmt
    assert store.path(record).read_bytes() == make_image(fmt)


@pytest.mark.parametrize(
    "data",
    [
        b"not an image",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,  # PNG signature, garbage body
        b"",
    ],
)
def test_rejects_non_images(store: ImageStore, data: bytes) -> None:
    with pytest.raises(ImageValidationError):
        store.add(data, "joy", "evil.png")
    assert store.all() == []


def test_rejects_unsupported_real_image_format(store: ImageStore) -> None:
    with pytest.raises(ImageValidationError):
        store.add(make_image("BMP"), "joy", "face.bmp")


def test_rejects_oversized_file(store: ImageStore) -> None:
    with pytest.raises(ImageTooLargeError):
        store.add(b"x" * 200_001, "joy", "big.png")


def test_rejects_too_many_pixels(store: ImageStore) -> None:
    with pytest.raises(ImageValidationError, match="too large"):
        store.add(make_image("PNG", size=(1001, 1000)), "joy", "huge.png")


def test_rejects_unknown_emotion(store: ImageStore) -> None:
    with pytest.raises(ImageValidationError):
        store.add(make_image(), "smug", "face.png")


@pytest.mark.parametrize("name", ["../../etc/passwd", "..\\..\\win.ini", "C:\\x\\a.png"])
def test_client_filename_never_becomes_a_path(store: ImageStore, tmp_path: Path, name: str) -> None:
    record = store.add(make_image(), "joy", name)
    assert store.path(record).parent == tmp_path / "images"
    assert store.path(record).name == f"{record.id}.png"
    assert "/" not in record.original_name and "\\" not in record.original_name


@pytest.mark.parametrize("bad_id", ["../manifest", "abc", "G" * 32, ""])
def test_get_rejects_malformed_ids(store: ImageStore, bad_id: str) -> None:
    assert store.get(bad_id) is None


def test_remove_deletes_file_and_clears_default(store: ImageStore) -> None:
    record = store.add(make_image(), "joy", "a.png")
    store.set_default(record.id)
    assert store.remove(record.id)
    assert not store.path(record).exists()
    assert store.default_id is None
    assert not store.remove(record.id)


def test_set_default_requires_existing_image(store: ImageStore) -> None:
    with pytest.raises(KeyError):
        store.set_default("0" * 32)


def test_manifest_survives_reload(store: ImageStore, tmp_path: Path) -> None:
    record = store.add(make_image(), "sadness", "sad.png")
    store.set_default(record.id)
    reloaded = ImageStore(tmp_path, max_bytes=200_000, max_pixels=10**6)
    assert reloaded.all() == [record]
    assert reloaded.default_id == record.id


def test_tampered_manifest_entries_are_skipped(store: ImageStore, tmp_path: Path) -> None:
    good = store.add(make_image(), "joy", "a.png")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["images"].append({**manifest["images"][0], "id": "../../secret"})
    manifest["images"].append({"unexpected": True})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    reloaded = ImageStore(tmp_path, max_bytes=200_000, max_pixels=10**6)
    assert reloaded.all() == [good]


def test_pool_fallback_order(store: ImageStore) -> None:
    assert store.pool("anger", {}, "mild") == []

    neutral = store.add(make_image(), "neutral", "n.png")
    assert store.pool("anger", {}, "mild") == [neutral]  # last resort: neutral images

    default = store.add(make_image(), "joy", "d.png")
    store.set_default(default.id)
    assert store.pool("anger", {}, "mild") == [default]  # default beats neutral

    sad = store.add(make_image(), "sadness", "s.png")
    assert store.pool("anger", {"anger": "sadness"}, "mild") == [sad]  # configured fallback

    angry = store.add(make_image(), "anger", "a.png")
    assert store.pool("anger", {"anger": "sadness"}, "mild") == [angry]  # own images first


def test_pool_prefers_matching_tier(store: ImageStore) -> None:
    plain = store.add(make_image(), "joy", "plain.png")
    assert store.pool("joy", {}, "strong") == [plain]  # no tiered images: use untiered

    strong = store.add(make_image(), "joy", "strong.png", tier="strong")
    assert store.pool("joy", {}, "strong") == [strong]
    assert store.pool("joy", {}, "mild") == [plain]  # untiered stands in for missing mild

    store.remove(plain.id)
    assert store.pool("joy", {}, "mild") == [strong]  # anything beats nothing


def test_update_changes_tier_and_emotion(store: ImageStore, tmp_path: Path) -> None:
    record = store.add(make_image(), "joy", "a.png")
    updated = store.update(record.id, tier="strong", emotion="surprise")
    assert (updated.tier, updated.emotion) == ("strong", "surprise")
    assert ImageStore(tmp_path, 200_000, 10**6).get(record.id) == updated
    with pytest.raises(ImageValidationError):
        store.update(record.id, tier="extreme")
    with pytest.raises(KeyError):
        store.update("0" * 32, tier="mild")


def test_replace_all_swaps_images_and_default(store: ImageStore) -> None:
    old = store.add(make_image(), "joy", "old.png")
    store.replace_all(
        [NewImage(make_image(), "anger", "a.png"), NewImage(make_image("GIF"), "joy", "b.gif")],
        default_index=1,
    )
    images = store.all()
    assert [i.emotion for i in images] == ["anger", "joy"]
    assert store.default_id == images[1].id
    assert not store.path(old).exists()


def test_replace_all_with_a_bad_image_changes_nothing(store: ImageStore, tmp_path: Path) -> None:
    keep = store.add(make_image(), "joy", "keep.png")
    with pytest.raises(ImageValidationError):
        store.replace_all([NewImage(make_image(), "anger"), NewImage(b"junk", "joy")], None)
    assert store.all() == [keep]
    assert sorted(p.name for p in (tmp_path / "images").iterdir()) == [keep.filename]


def test_manifest_without_tier_loads_as_untiered(store: ImageStore, tmp_path: Path) -> None:
    record = store.add(make_image(), "joy", "a.png")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    del manifest["images"][0]["tier"]  # written by Phase 1
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert ImageStore(tmp_path, 200_000, 10**6).get(record.id).tier == "any"
