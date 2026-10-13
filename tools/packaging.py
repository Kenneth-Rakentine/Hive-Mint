"""Hashing and ZIP packaging for build output."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_zip(zip_path: Path, files: list[Path]) -> Path:
    """Zip files flat (no folders), in the given order."""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    return zip_path


def manifest(files: list[Path]) -> list[dict]:
    return [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in files]
