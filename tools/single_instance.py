"""Only one worker at a time: a shift (or `run`) holds an OS file lock for as long as it runs.

Two shifts at once would fight over the SQLite database ("database is locked") and the second would
requeue the first one's running jobs as if they had crashed. The operating system releases the lock
when the process ends, even after a crash, so a stale lock can never block the next shift.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import make_url


def lock_path(db_url: str) -> Path:
    database = make_url(db_url).database or ""
    base = Path(database).parent if database and database != ":memory:" else Path(".")
    return base / "worker.lock"


@contextmanager
def single_instance(path: Path) -> Iterator[bool]:
    """Yields True while this process holds the lock, or False if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - held for the whole run
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()
