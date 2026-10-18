"""Is the laptop running on battery? Windows GetSystemPowerStatus via ctypes; no installs."""

from __future__ import annotations

import ctypes
import os


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_battery() -> bool | None:
    """True on battery, False on AC power, None if unknown (not Windows, or the call failed)."""
    if os.name != "nt":
        return None
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    return {0: True, 1: False}.get(status.ACLineStatus)  # 255 = unknown
