from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateTable

from app.db import Base, init_db, make_engine
from app.migrate import migrate


def _old_schema(engine):
    """The v0.3 schema: no products.extra_rework_cycles, jobs.product_id NOT NULL."""
    from app import models

    Base.metadata.create_all(engine)
    ddl = str(CreateTable(models.Job.__table__).compile(engine)).replace("CREATE TABLE jobs", "CREATE TABLE jobs_old", 1)
    ddl = ddl.replace("product_id INTEGER,", "product_id INTEGER NOT NULL,", 1)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("ALTER TABLE products DROP COLUMN extra_rework_cycles")
        conn.exec_driver_sql("DROP TABLE jobs")
        conn.exec_driver_sql(ddl)
        conn.exec_driver_sql("ALTER TABLE jobs_old RENAME TO jobs")
        conn.execute(text("INSERT INTO products (id, slug, name, product_type, description, status, status_reason, "
                          "rework_count, created_at, updated_at) VALUES (1, 's', 'n', 'spreadsheet', '', 'REWORK', '', "
                          "3, '2026-01-01', '2026-01-01')"))
        conn.execute(text("INSERT INTO jobs (id, product_id, department, job_type, for_status, status, attempts, "
                          "max_attempts, input_json, output_json, created_at) VALUES (7, 1, 'QA', 'quality_review', "
                          "'QA', 'SUCCEEDED', 1, 3, '{}', '{}', '2026-01-01')"))
        conn.execute(text("INSERT INTO agent_runs (job_id, department, provider, model, tier, prompt_tokens, "
                          "completion_tokens, cost_usd_micros, status, result_summary, started_at) VALUES "
                          "(7, 'QA', 'qa', 'none', 0, 0, 0, 0, 'SUCCEEDED', '', '2026-01-01')"))


def test_migrate_upgrades_a_v03_database_and_keeps_rows(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    _old_schema(engine)
    applied = migrate(engine)
    assert applied == ["products.extra_rework_cycles added", "jobs.product_id made nullable"]
    columns = {c["name"]: c for c in inspect(engine).get_columns("jobs")}
    assert columns["product_id"]["nullable"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT extra_rework_cycles, rework_count FROM products")).one() == (0, 3)
        assert conn.execute(text("SELECT id, product_id FROM jobs")).one() == (7, 1)
        assert conn.execute(text("SELECT job_id FROM agent_runs")).scalar() == 7
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        fks = conn.exec_driver_sql("PRAGMA foreign_key_list(agent_runs)").fetchall()
        assert any(fk[2] == "jobs" for fk in fks)
    assert {i["name"] for i in inspect(engine).get_indexes("jobs")} >= {"ix_jobs_product_id", "ix_jobs_status"}


def test_migrate_twice_and_on_a_new_database_does_nothing(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    init_db(engine)
    assert migrate(engine) == []
    init_db(engine)
    assert migrate(engine) == []
