"""Minimal read-only HTTP API. Run: uvicorn app.api:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.db import init_db, make_engine, make_session_factory
from app.status import snapshot
from config.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.db_url)
        init_db(engine)
        app.state.sessions = make_session_factory(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Hive-Mint", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        with app.state.sessions() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}

    @app.get("/status")
    def status() -> dict:
        with app.state.sessions() as session:
            return snapshot(session, settings)

    return app


app = create_app()
