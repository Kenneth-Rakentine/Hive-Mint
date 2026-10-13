"""Product specifications: structured data shared by Production (what to build) and Testing
(what to check). Specs live as JSON in config/specs/<slug>.json.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from config.settings import ROOT

SPEC_DIR = ROOT / "config" / "specs"
FILE_KINDS = frozenset({"xlsx", "pdf", "text"})
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXCEL_EPOCH = date(1899, 12, 30)


class SpecError(ValueError):
    """The specification file is missing required structure."""


def load_spec(slug: str, spec_dir: Path = SPEC_DIR) -> dict | None:
    """Return the validated spec for a product slug, or None if there is no spec."""
    path = spec_dir / f"{slug}.json"
    if not path.is_file():
        return None
    spec = json.loads(path.read_text(encoding="utf-8"))
    validate_spec(spec)
    return spec


def validate_spec(spec: dict) -> None:
    for key in ("slug", "builder", "zip_name", "files"):
        if key not in spec:
            raise SpecError(f"spec is missing '{key}'")
    names = [f["name"] for f in spec["files"]]
    if len(names) != len(set(names)):
        raise SpecError("spec lists a file twice")
    for file in spec["files"]:
        if file.get("kind") not in FILE_KINDS:
            raise SpecError(f"{file['name']}: kind must be one of {sorted(FILE_KINDS)}")
        if file["kind"] == "xlsx":
            sheet_names = {s["name"] for s in file.get("sheets", [])}
            for scenario in file.get("scenarios", []):
                for ref in scenario.get("inputs", []) + scenario.get("expect", []):
                    if ref["sheet"] not in sheet_names:
                        raise SpecError(f"{file['name']}: scenario '{scenario['name']}' uses unknown sheet "
                                        f"'{ref['sheet']}'")


def file_names(spec: dict) -> list[str]:
    return [f["name"] for f in spec["files"]]


def sheet_spec(file_spec: dict, sheet: str) -> dict:
    return next(s for s in file_spec["sheets"] if s["name"] == sheet)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def excel_value(value):
    """Spec values are JSON; ISO date strings become dates (inputs) / serial numbers (expectations)."""
    if isinstance(value, str) and ISO_DATE.match(value):
        return parse_date(value)
    return value


def excel_serial(value: date) -> int:
    return (value - EXCEL_EPOCH).days
