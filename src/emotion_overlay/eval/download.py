"""Downloads a public dataset, verifies its checksum and extracts only expected files.

Only runs when you explicitly ask for it; the app itself never downloads datasets.
"""

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from emotion_overlay.eval.datasets import RAVDESS_NAME


@dataclass(frozen=True)
class Source:
    url: str
    md5: str
    size_mb: int
    license: str


SOURCES = {
    "ravdess": Source(
        url="https://zenodo.org/api/records/1188976/files/Audio_Speech_Actors_01-24.zip/content",
        md5="bc696df654c87fed845eb13823edef8a",
        size_mb=208,
        license="CC BY-NC-SA 4.0 (Livingstone & Russo, 2018), non-commercial use",
    ),
}


def _fetch(source: Source, target: Path) -> None:
    tmp = target.with_suffix(".part")
    digest = hashlib.md5()  # noqa: S324 - integrity check against the publisher's checksum
    with urllib.request.urlopen(source.url, timeout=60) as response, tmp.open("wb") as out:  # noqa: S310 - fixed https URL
        total = int(response.headers.get("Content-Length") or source.size_mb * 1e6)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            print(f"\r  {done / 1e6:6.0f} / {total / 1e6:.0f} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    if digest.hexdigest() != source.md5:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("checksum mismatch: download corrupted or tampered with")
    tmp.replace(target)


def extract_ravdess(archive: Path, dest: Path) -> int:
    """Extract only RAVDESS speech WAVs, flattened into dest by their validated names.

    Paths inside the zip are never used as output paths (no zip slip).
    """
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.rsplit("/", 1)[-1]
            if info.is_dir() or not RAVDESS_NAME.match(name) or info.file_size > 50_000_000:
                continue
            with zf.open(info) as src, (dest / name).open("wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def download(name: str, root: Path) -> Path:
    source = SOURCES[name]
    dest = root / name
    archive = root / f"{name}.zip"
    root.mkdir(parents=True, exist_ok=True)
    print(f"{name}: {source.license}", file=sys.stderr)
    if not archive.exists():
        print(f"Downloading {source.size_mb} MB from {source.url}", file=sys.stderr)
        _fetch(source, archive)
    count = extract_ravdess(archive, dest)
    print(f"Extracted {count} clips to {dest}", file=sys.stderr)
    return dest
