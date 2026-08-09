#!/usr/bin/env python3
"""Fetch the face-matching models, sha256-pinned (F2).

The models are NEVER committed — ~300MB of weights don't belong in git —
but what runs must be exactly what was reviewed, so this script refuses
to install anything whose hash doesn't match the pins below. Matching is
tier 2 of docs/reference/biometric-attendance.md (our own code, wherever
hosted); the pins are what keeps a compromised mirror from becoming "a
vendor processing faces" by another name.

Source pack: insightface `buffalo_l` (SCRFD-10G detector + ArcFace-class
w600k_r50 embedder), Apache/MIT-licensed research release. Only the two
models the engine loads are extracted; the rest of the pack is dropped.

Usage:
    uv run python scripts/fetch_face_models.py [dest_dir]

Default destination is the `USALI_FACE_MODEL_DIR` setting (models/face).
Idempotent: verified-present files are left alone.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PACK_URL = (
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
)

# Computed 2026-07-21 from a fresh download; the zip hash also matches the
# value the insightface community has published for this release.
ZIP_SHA256 = "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"
MODEL_SHA256: dict[str, str] = {
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str, *, label: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise SystemExit(
            f"sha256 mismatch for {label}: expected {expected}, got {actual}. "
            "Refusing to install — the download is not the reviewed artifact."
        )


def install_from_zip(zip_path: Path, dest: Path, pins: dict[str, str]) -> None:
    """Extract the pinned members, verify each, and only then move them
    into `dest`. Order matters: a bad member must never appear in the
    destination, not even next to a good one — a partially-populated model
    dir would pass the engine's existence check with one unverified model."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        extracted: list[tuple[Path, str]] = []
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                name = Path(member).name
                if name in pins:
                    with z.open(member) as src, (staging / name).open("wb") as out:
                        shutil.copyfileobj(src, out)
                    extracted.append((staging / name, name))
        missing = set(pins) - {name for _, name in extracted}
        if missing:
            raise SystemExit(f"pack is missing expected models: {sorted(missing)}")
        for path, name in extracted:
            verify_sha256(path, pins[name], label=name)
        dest.mkdir(parents=True, exist_ok=True)
        for path, name in extracted:
            shutil.move(str(path), dest / name)


def main() -> None:
    if len(sys.argv) > 2:
        raise SystemExit(__doc__)
    if len(sys.argv) == 2:
        dest = Path(sys.argv[1])
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from usali.config import get_settings

        dest = get_settings().face_model_dir

    present = [
        name for name in MODEL_SHA256 if (dest / name).is_file()
    ]
    if len(present) == len(MODEL_SHA256):
        for name in MODEL_SHA256:
            verify_sha256(dest / name, MODEL_SHA256[name], label=name)
        print(f"models already present and verified under {dest}")
        return

    print(f"downloading {PACK_URL} ...")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "buffalo_l.zip"
        urllib.request.urlretrieve(PACK_URL, zip_path)  # noqa: S310 - pinned https URL
        verify_sha256(zip_path, ZIP_SHA256, label="buffalo_l.zip")
        install_from_zip(zip_path, dest, MODEL_SHA256)
    print(f"installed {sorted(MODEL_SHA256)} into {dest}")


if __name__ == "__main__":
    main()
