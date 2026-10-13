"""Disk guard: free space on the drive, Hive-Mint's own footprint and the Ollama model folder.

Operations checks this before every Production job; a breached limit pauses the job with a
DISK_LIMIT error instead of writing more files.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings

GB = 1024 ** 3


def folder_size(path: Path) -> int | None:
    """Total bytes of regular files under path (symlinks not followed); None if it doesn't exist."""
    if not path.is_dir():
        return None
    total = 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue  # unreadable folder: skip rather than fail the guard
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _existing_anchor(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class DiskReport:
    free_bytes: int
    hivemint_bytes: int
    ollama_models_bytes: int | None

    def as_dict(self, settings: Settings) -> dict:
        return {
            "free_gb": round(self.free_bytes / GB, 2),
            "hivemint_folder_gb": round(self.hivemint_bytes / GB, 2),
            "ollama_models_gb": None if self.ollama_models_bytes is None else round(self.ollama_models_bytes / GB, 2),
            "min_free_disk_gb": settings.min_free_disk_gb,
            "max_hivemint_storage_gb": settings.max_hivemint_storage_gb,
            "ollama_models_dir": str(settings.ollama_models_dir),
        }


def disk_report(settings: Settings) -> DiskReport:
    free = shutil.disk_usage(_existing_anchor(settings.products_dir)).free
    own = folder_size(settings.storage_root) or 0
    for extra in (settings.products_dir, settings.archive_dir):
        if not _inside(extra, settings.storage_root):
            own += folder_size(extra) or 0
    return DiskReport(free, own, folder_size(settings.ollama_models_dir))


def check_disk(settings: Settings, report: DiskReport | None = None) -> str | None:
    """A DISK_LIMIT message if either limit is breached, else None."""
    report = report or disk_report(settings)
    problems = []
    if report.free_bytes < settings.min_free_disk_gb * GB:
        problems.append(f"free space {report.free_bytes / GB:.1f} GB is below the {settings.min_free_disk_gb:g} GB "
                        "minimum (MIN_FREE_DISK_GB)")
    if report.hivemint_bytes > settings.max_hivemint_storage_gb * GB:
        problems.append(f"Hive-Mint folder {report.hivemint_bytes / GB:.1f} GB exceeds the "
                        f"{settings.max_hivemint_storage_gb:g} GB cap (MAX_HIVEMINT_STORAGE_GB)")
    return "DISK_LIMIT: " + "; ".join(problems) if problems else None
