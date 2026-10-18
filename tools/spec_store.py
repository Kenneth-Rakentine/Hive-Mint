"""Versioned product specs in the database (the spec_versions table).

The database is the source of truth: Production, Testing, QA, Red Team and Product Design read
a product's spec with product_spec(). A hand-written config/specs/<slug>.json is imported as a
version the first time it is needed, and again whenever its spec_version is newer than the
latest stored one (a human edited it). Every new version stores a unified diff against the
previous one, and a copy is written to products/<id>/spec-v<n>.json for reviewers to read.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.lifecycle import log_event
from app.models import Job, Product, SpecVersion
from tools.spec import SPEC_DIR, SpecError, prepare


def latest_version(session: Session, product: Product) -> SpecVersion | None:
    return session.scalar(select(SpecVersion).where(SpecVersion.product_id == product.id)
                          .order_by(SpecVersion.version.desc()).limit(1))


def dump(spec: dict) -> str:
    return json.dumps(spec, indent=1, ensure_ascii=False)


def spec_diff(old: dict | None, new: dict, old_label: str, new_label: str) -> str:
    before = dump(old).splitlines(keepends=True) if old else []
    return "".join(difflib.unified_diff(before, dump(new).splitlines(keepends=True), old_label, new_label))


def save_version(session: Session, product: Product, spec: dict, *, author: str, reason: str,
                 products_dir: Path | None = None, brief_id: int | None = None, job: Job | None = None) -> SpecVersion:
    """Validate and store a new spec version for a product. Its spec_version must be higher than the last."""
    prepare(spec)  # raises SpecError
    if spec.get("slug") != product.slug:
        raise SpecError(f"spec slug '{spec.get('slug')}' does not match product slug '{product.slug}'")
    previous = latest_version(session, product)
    version = int(spec.get("spec_version", 1))
    if previous is not None and version <= previous.version:
        raise SpecError(f"spec_version {version} must be higher than the stored version {previous.version}")
    row = SpecVersion(product_id=product.id, version=version, spec_json=dump(spec), author=author, reason=reason,
                      brief_id=brief_id, job_id=job.id if job else None,
                      diff_text=spec_diff(previous.spec if previous else None, spec,
                                          f"spec v{previous.version}" if previous else "(none)", f"spec v{version}"))
    session.add(row)
    session.flush()
    changed = row.diff_text.count("\n+") + row.diff_text.count("\n-")
    log_event(session, "spec.version", actor=author, product=product, job=job,
              message=f"Spec v{version} stored ({author}): {reason}",
              data={"spec_version_id": row.id, "version": version, "brief_id": brief_id, "changed_lines": changed})
    if products_dir is not None:
        materialize(row, products_dir)
    return row


def materialize(row: SpecVersion, products_dir: Path) -> Path:
    path = products_dir / str(row.product_id) / f"spec-v{row.version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(row.spec_json + "\n", encoding="utf-8")
    return path


def product_spec(session: Session, product: Product, products_dir: Path | None = None,
                 spec_dir: Path = SPEC_DIR) -> dict | None:
    """The product's current spec, normalized; None if it has none yet."""
    latest = latest_version(session, product)
    path = spec_dir / f"{product.slug}.json"
    if path.is_file():
        on_file = json.loads(path.read_text(encoding="utf-8"))
        if latest is None or int(on_file.get("spec_version", 1)) > latest.version:
            author = "file-import" if latest is None else "human"
            latest = save_version(session, product, on_file, author=author, products_dir=products_dir,
                                  reason=f"Imported from config/specs/{path.name}")
    return prepare(latest.spec) if latest is not None else None
