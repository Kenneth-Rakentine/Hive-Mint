"""Product builders, keyed by the `builder` name in a product spec.

A builder takes (spec, out_dir), writes every non-ZIP file the spec lists, and returns the paths.
"spreadsheet" builds any spreadsheet product from its spec (tools/builders/spreadsheet.py).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tools.builders import spreadsheet

Builder = Callable[[dict, Path], list[Path]]

BUILDERS: dict[str, Builder] = {
    "spreadsheet": spreadsheet.build,
}
