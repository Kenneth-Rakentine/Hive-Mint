"""Open the pixel office in its own app window (no tabs or address bar) using Edge or Chrome's --app mode.

Both browsers ship this mode; Edge is on every Windows 11 PC. If neither is found, the default
browser opens a normal tab instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

WINDOW_SIZE = "1600,940"


def app_browser() -> str | None:
    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                           str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def open_window(url: str, *, app: bool = True) -> str:
    """Returns how it opened: "app" or "tab"."""
    exe = app_browser() if app else None
    if exe:
        subprocess.Popen([exe, f"--app={url}", f"--window-size={WINDOW_SIZE}"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return "app"
    webbrowser.open(url)
    return "tab"
