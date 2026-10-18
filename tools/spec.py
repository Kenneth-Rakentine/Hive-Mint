"""Product specifications: structured data shared by Production (what to build) and Testing
(what to check). Hand-written specs live as JSON in config/specs/<slug>.json; each product's
versions are stored in the spec_versions table (tools/spec_store.py).

Spec format v4 (builder "spreadsheet") describes every sheet, column, formula, chart and the
PDF/README text; see tools/builders/spreadsheet.py. normalize() derives the flat views Testing
and QA read (column names, validated columns) and expands example workbooks.
"""

from __future__ import annotations

import copy
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
    return prepare(json.loads(path.read_text(encoding="utf-8")))


def prepare(spec: dict) -> dict:
    """Validate a spec and return its normalized copy (the form Production, Testing and QA read)."""
    validate_spec(spec)
    return normalize(spec)


def normalize(spec: dict) -> dict:
    """A copy with derived fields: table column names and validated columns, example workbooks expanded.

    Idempotent. Raw v4 table columns are dicts ({name, width, format, formula, validation}); the
    normalized sheet keeps them as column_defs and lists plain names under columns.
    """
    spec = copy.deepcopy(spec)
    by_name = {f["name"]: f for f in spec["files"]}
    for file in spec["files"]:
        if file["kind"] == "xlsx" and file.get("example_of"):
            base = by_name[file["example_of"]]
            for key in ("sheets", "rate_table"):
                if key in base and key not in file:
                    file[key] = copy.deepcopy(base[key])
    for file in spec["files"]:
        for sheet in file.get("sheets", []):
            columns = sheet.get("columns")
            if columns and isinstance(columns[0], dict):
                sheet["column_defs"] = columns
                sheet["columns"] = [c["name"] for c in columns]
                sheet.setdefault("validated_columns", [c["name"] for c in columns if c.get("validation")])
                sheet.setdefault("header_row", 1)
                sheet.setdefault("freeze_header", True)
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
            if base := file.get("example_of"):
                target = next((f for f in spec["files"] if f["name"] == base), None)
                if target is None or target["kind"] != "xlsx" or target.get("example_of"):
                    raise SpecError(f"{file['name']}: example_of must name another (non-example) xlsx file")
                file = {**file, "sheets": file.get("sheets", target.get("sheets", []))}
            sheet_names = {s["name"] for s in file.get("sheets", [])}
            refs = [("example_data", r) for r in file.get("example_data", [])]
            for scenario in file.get("scenarios", []):
                refs += [(scenario["name"], r) for r in scenario.get("inputs", []) + scenario.get("expect", [])]
            for owner, ref in refs:
                if ref["sheet"] not in sheet_names:
                    raise SpecError(f"{file['name']}: scenario '{owner}' uses unknown sheet '{ref['sheet']}'")


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
