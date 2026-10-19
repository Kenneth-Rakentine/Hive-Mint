"""Read-only HTTP API and the pixel office. Run: python -m app.cli office (or uvicorn app.api:app --port 8000).

GET /              the pixel office (app/static/office.html), drawn only from database state
GET /office/state  what the office draws (app/office.py)
GET /health, /status
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy import text

from app.db import init_db, make_engine, make_session_factory
from app.office import office_state
from app.status import snapshot
from config.settings import Settings, get_settings


_last_seen = [time.monotonic()]


def last_seen() -> float:
    """When an office window last asked for data (time.monotonic); the idle watchdog reads it."""
    return _last_seen[0]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.db_url)
        init_db(engine)
        app.state.sessions = make_session_factory(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Hive-Mint", version="0.6.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        with app.state.sessions() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}

    @app.get("/status")
    def status() -> dict:
        with app.state.sessions() as session:
            return snapshot(session, settings)

    office_page = Path(__file__).parent / "static" / "office.html"
    cache: dict = {}

    @app.get("/", include_in_schema=False)
    def office() -> FileResponse:
        return FileResponse(office_page, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(office_page.with_name("hivemint.ico"), media_type="image/x-icon")

    @app.get("/office/state")
    def office_data() -> dict:
        _last_seen[0] = time.monotonic()
        # The page polls every 5 s; measuring folder sizes that often is wasteful, so disk figures refresh
        # at most once a minute. Everything else is read fresh from the database on every request.
        with app.state.sessions() as session:
            state = office_state(session, settings, disk=_cached_disk(cache, settings))
        return state

    return app


def _cached_disk(cache: dict, settings: Settings) -> dict:
    from tools.disk import disk_report

    if "disk" not in cache or cache["at"] < time.monotonic() - 60:
        cache["disk"], cache["at"] = disk_report(settings).as_dict(settings), time.monotonic()
    return cache["disk"]


app = create_app()
