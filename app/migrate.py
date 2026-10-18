"""Small, idempotent schema migrations for existing SQLite databases.

create_all() adds missing tables but never changes existing ones. Each step here checks the
live schema first, so running it on a new or already-migrated database does nothing.
"""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

ADDED_COLUMNS = [
    # (table, column, DDL type and default)
    ("products", "extra_rework_cycles", "INTEGER NOT NULL DEFAULT 0"),
]


def migrate(engine: Engine) -> list[str]:
    """Bring an existing database up to the current models. Returns the steps applied."""
    if engine.dialect.name != "sqlite":
        return []
    applied = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column, ddl in ADDED_COLUMNS:
        if table in tables and column not in {c["name"] for c in inspector.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
            applied.append(f"{table}.{column} added")
    if "jobs" in tables:
        product_id = next(c for c in inspect(engine).get_columns("jobs") if c["name"] == "product_id")
        if not product_id["nullable"]:
            _rebuild_jobs(engine)
            applied.append("jobs.product_id made nullable")
    return applied


def _rebuild_jobs(engine: Engine) -> None:
    """SQLite cannot drop NOT NULL in place: copy the table, as the SQLite docs describe.

    Foreign keys are off during the copy; other tables keep referring to "jobs" by name,
    so they point at the rebuilt table once it is renamed into place.
    """
    from app.models import Job

    table = Job.__table__
    create = str(CreateTable(table).compile(engine)).replace('CREATE TABLE jobs', 'CREATE TABLE jobs_new', 1)
    columns = ", ".join(f'"{c.name}"' for c in table.columns)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()  # the pragma only takes effect outside a transaction
        try:
            with conn.begin():
                conn.exec_driver_sql(create)
                conn.exec_driver_sql(f"INSERT INTO jobs_new ({columns}) SELECT {columns} FROM jobs")
                conn.exec_driver_sql("DROP TABLE jobs")
                conn.exec_driver_sql("ALTER TABLE jobs_new RENAME TO jobs")
                for index in table.indexes:
                    conn.exec_driver_sql(str(CreateIndex(index).compile(engine)))
                problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError(f"jobs rebuild broke foreign keys: {problems[:5]}")
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()
