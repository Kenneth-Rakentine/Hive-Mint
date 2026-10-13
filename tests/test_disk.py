from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import select

from agents.departments import STUB_HANDLERS
from agents.operations import OperationsManager
from app.api import create_app
from app.models import Event
from app.states import JobStatus, ProductStatus as S
from config.settings import Settings
from tests.conftest import advance_to
from tools import disk
from tools.disk import GB, DiskReport, check_disk, folder_size


def test_defaults():
    s = Settings()
    assert (s.min_free_disk_gb, s.max_hivemint_storage_gb) == (50, 20)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MIN_FREE_DISK_GB", "10")
    monkeypatch.setenv("MAX_HIVEMINT_STORAGE_GB", "5.5")
    s = Settings.from_env()
    assert (s.min_free_disk_gb, s.max_hivemint_storage_gb) == (10, 5.5)


def test_folder_size_counts_nested_files(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "x.bin").write_bytes(b"1" * 100)
    (tmp_path / "a" / "b" / "y.bin").write_bytes(b"1" * 23)
    assert folder_size(tmp_path) == 123
    assert folder_size(tmp_path / "missing") is None


def test_check_disk_reports_each_limit(settings):
    strict = replace(settings, min_free_disk_gb=50, max_hivemint_storage_gb=20)
    assert check_disk(strict, DiskReport(100 * GB, 1 * GB, None)) is None
    low = check_disk(strict, DiskReport(10 * GB, 1 * GB, None))
    assert low.startswith("DISK_LIMIT") and "10.0 GB" in low and "MIN_FREE_DISK_GB" in low
    big = check_disk(strict, DiskReport(100 * GB, 25 * GB, None))
    assert big.startswith("DISK_LIMIT") and "MAX_HIVEMINT_STORAGE_GB" in big


def test_production_job_pauses_on_disk_limit_then_resumes(session, settings, make_product, monkeypatch):
    reports = {"now": DiskReport(1 * GB, 0, None)}
    monkeypatch.setattr(disk, "disk_report", lambda _s: reports["now"])
    strict = replace(settings, min_free_disk_gb=50)
    ops = OperationsManager(session, strict, handlers=STUB_HANDLERS)
    product = advance_to(session, strict, make_product(), S.PRODUCTION)
    session.commit()

    assert ops.tick() == 0  # paused, not run
    job = ops.open_job(product)
    assert job.status == JobStatus.PAUSED and job.error.startswith("DISK_LIMIT")
    assert job.attempts == 0 and product.status == S.PRODUCTION
    assert ops.tick() == 0 and ops.open_job(product).id == job.id  # no duplicate job, no loop

    reports["now"] = DiskReport(100 * GB, 0, None)
    assert ops.tick() >= 1
    assert job.status == JobStatus.SUCCEEDED and product.status != S.PRODUCTION
    kinds = [e.event_type for e in session.scalars(select(Event).where(Event.job_id == job.id))]
    assert "job.paused" in kinds and "job.resumed" in kinds


def test_status_shows_disk_usage(settings):
    with TestClient(create_app(settings)) as client:
        data = client.get("/status").json()
    assert set(data["disk"]) >= {"free_gb", "hivemint_folder_gb", "ollama_models_gb", "min_free_disk_gb",
                                 "max_hivemint_storage_gb"}
    assert data["disk"]["free_gb"] > 0
    assert data["disk"]["ollama_models_gb"] is None  # the test settings point at a missing folder
    assert data["paused_jobs"] == []
