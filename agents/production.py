"""Production department: build the files a product spec lists, then zip them.

Output goes to products/<product_id>/v<n>/, where n counts this product's recorded builds.
The build is assembled in a staging folder and renamed into place only once it is complete,
so a crash never leaves a half-built version behind. Each build is recorded in the Build
table with a hash of every file.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from agents.departments import StageResult
from app.lifecycle import log_event
from app.models import Build, Job, Product
from app.states import Outcome, ProductStatus
from config.settings import Settings
from tools.builders import BUILDERS
from tools.packaging import make_zip, manifest, sha256
from tools.spec import file_names, load_spec

S = ProductStatus


class Production:
    provider = "deterministic"

    def __init__(self, settings: Settings):
        self.settings = settings

    def __call__(self, product: Product, job: Job) -> StageResult:
        session = object_session(product)
        spec = load_spec(product.slug)
        if spec is None:
            return StageResult(Outcome.FAIL, S.REWORK,
                               f"Production: no specification for '{product.slug}' in config/specs")
        builder = BUILDERS.get(spec["builder"])
        if builder is None:
            raise LookupError(f"Spec names unknown builder '{spec['builder']}'")

        version = 1 + session.scalar(select(func.count(Build.id)).where(Build.product_id == product.id))
        target = self.settings.products_dir / str(product.id) / f"v{version}"
        if target.exists():
            # Left over from an earlier database; never overwrite it, move it aside.
            moved = self._archive_stale(target, product)
            log_event(session, "build.stale_archived", actor="production", product=product, job=job,
                      message=f"Moved unrecorded folder {target} to {moved}")

        staging = target.with_name(f".{target.name}.staging")
        if staging.exists():
            shutil.rmtree(staging)  # our own incomplete attempt
        try:
            written = builder(spec, staging)
            names = [p.name for p in written]
            if names != file_names(spec):
                raise RuntimeError(f"Builder wrote {names}, spec lists {file_names(spec)}")
            make_zip(staging / spec["zip_name"], written)
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        files = [target / name for name in file_names(spec)]
        zip_path = target / spec["zip_name"]
        build = Build(product_id=product.id, job_id=job.id, version=version, spec_slug=spec["slug"],
                      path=str(target), zip_path=str(zip_path), zip_sha256=sha256(zip_path),
                      manifest_json=json.dumps(manifest(files)))
        session.add(build)
        log_event(session, "build.created", actor="production", product=product, job=job,
                  message=f"Built v{version} at {target}",
                  data={"version": version, "files": file_names(spec), "zip": spec["zip_name"]})
        return StageResult(Outcome.PASS, S.TESTING,
                           f"Built v{version}: {len(files)} files + {spec['zip_name']}",
                           {"build_id": build.id, "version": version, "path": str(target), "zip_path": str(zip_path)})

    def _archive_stale(self, folder: Path, product: Product) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = self.settings.archive_dir / "stale-builds" / f"product-{product.id}-{folder.name}-{stamp}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(destination))
        return destination
