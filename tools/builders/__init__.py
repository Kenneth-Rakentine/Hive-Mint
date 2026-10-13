"""Product builders, keyed by the `builder` name in a product spec.

A builder takes (spec, out_dir), writes every non-ZIP file the spec lists, and returns the paths.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tools.builders import mobile_detailer

Builder = Callable[[dict, Path], list[Path]]

BUILDERS: dict[str, Builder] = {
    "mobile_detailer_tracker": mobile_detailer.build,
}
