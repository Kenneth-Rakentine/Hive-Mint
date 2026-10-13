"""Command line: python -m app.cli {init-db,demo,status}"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from agents.operations import OperationsManager
from app.db import get_engine, init_db, make_session_factory
from app.lifecycle import create_product
from app.models import Build, Event, Product, TestResult
from app.states import ProductStatus
from app.status import snapshot
from config.settings import get_settings

console = Console()

DEMO = {
    "slug": "mobile-auto-detailer-income-expense-tracker",
    "name": "Mobile Auto Detailer Income & Expense Tracker",
    "product_type": "spreadsheet",
    "description": "Track jobs, income, supplies, mileage and expenses for a mobile auto detailing business.",
}
MAX_DEMO_TICKS = 50  # safety bound; a clean run needs 10 ticks, each rework cycle adds a few


def _session():
    engine = get_engine()
    init_db(engine)
    return make_session_factory(engine)()


def cmd_init_db(_args) -> int:
    with _session():
        pass
    console.print(f"Database ready at [bold]{get_settings().db_url}[/bold]")
    return 0


def cmd_demo(_args) -> int:
    settings = get_settings()
    with _session() as session:
        product = session.scalar(select(Product).where(Product.slug == DEMO["slug"]))
        if product:
            console.print(f"Demo product already exists (status [bold]{product.status}[/bold]). "
                          "Delete data/hivemint.db to run the demo from scratch.")
            return 1
        product = create_product(session, settings, actor="cli-demo", **DEMO)
        session.commit()
        console.print(f"Created [bold]{product.name}[/bold] (id {product.id}). Building and testing real files; "
                      "this takes about a minute.")

        ops = OperationsManager(session, settings)
        for _ in range(MAX_DEMO_TICKS):
            if not ops.tick():
                break
        session.refresh(product)

        events = session.scalars(select(Event).where(Event.product_id == product.id).order_by(Event.id)).all()
        table = Table(title=f"Gate transitions: {product.name}")
        for column in ("time (UTC)", "actor", "from", "to", "reason"):
            table.add_column(column, overflow="fold")
        for event in events:
            if event.event_type == "product.transition":
                table.add_row(event.created_at.strftime("%H:%M:%S"), event.actor, event.from_status or "",
                              event.to_status or "", escape(event.message))
        console.print(table)
        console.print(f"{len(events)} audit events recorded for this product "
                      "(see the events, jobs and agent_runs tables).")
        _print_build(session, product)
        style = "green" if product.status == ProductStatus.HUMAN_REVIEW else "red"
        console.print(f"Final status: [bold {style}]{product.status}[/bold {style}]. "
                      "Nothing is published without human approval.")
        return 0 if product.status == ProductStatus.HUMAN_REVIEW else 1


def _print_build(session, product: Product) -> None:
    build = session.scalar(select(Build).where(Build.product_id == product.id).order_by(Build.version.desc()).limit(1))
    if build is None:
        console.print("No build was recorded.")
        return
    console.print(f"Build v{build.version}: [bold]{escape(build.path)}[/bold]")
    for entry in json.loads(build.manifest_json):
        console.print(escape(f"  {entry['name']:<30} {entry['bytes']:>8,} bytes"))
    console.print(escape(f"  ZIP: {build.zip_path}"))
    counts = dict(session.execute(select(TestResult.passed, func.count()).where(TestResult.build_id == build.id)
                                  .group_by(TestResult.passed)).all())
    if counts:
        style = "red" if counts.get(False) else "green"
        console.print(f"Testing: [{style}]{counts.get(True, 0)} passed, {counts.get(False, 0)} failed[/{style}] "
                      "(see the test_results table).")


def cmd_status(_args) -> int:
    with _session() as session:
        data = snapshot(session, get_settings())
    table = Table(title="Products by state")
    table.add_column("state")
    table.add_column("count", justify="right")
    for state, count in data["products_by_state"].items():
        table.add_row(state, str(count), style=None if count else "dim")
    console.print(table)
    console.print("Jobs:", data["jobs_by_status"])
    console.print(f"Queued jobs: {len(data['queued_jobs'])}")
    for job in data["queued_jobs"]:
        console.print(escape(f"  #{job['id']} {job['department']} {job['job_type']} - {job['product']}"))
    for job in data["paused_jobs"]:
        console.print(escape(f"  paused #{job['id']} {job['department']} {job['job_type']} - {job['product']}: "
                             f"{job['error']}"), style="yellow")
    disk = data["disk"]
    ollama = "not found" if disk["ollama_models_gb"] is None else f"{disk['ollama_models_gb']:.2f} GB"
    console.print(f"Disk: {disk['free_gb']:.1f} GB free (min {disk['min_free_disk_gb']:g}); Hive-Mint folder "
                  f"{disk['hivemint_folder_gb']:.2f} GB (max {disk['max_hivemint_storage_gb']:g}); "
                  f"Ollama models {ollama}")
    console.print("Cloud spend (USD):", data["cloud_spend_usd"])
    console.print("Limits:", data["limits"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Hive-Mint command line")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="create the SQLite database").set_defaults(func=cmd_init_db)
    commands.add_parser("demo", help="build and test the sample product, then run it to HUMAN_REVIEW") \
        .set_defaults(func=cmd_demo)
    commands.add_parser("status", help="show product and job counts").set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
