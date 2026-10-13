"""Command line: python -m app.cli {init-db,demo,status}"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from agents.operations import OperationsManager
from app.db import get_engine, init_db, make_session_factory
from app.lifecycle import create_product
from app.models import Event, Product
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
MAX_DEMO_TICKS = 50  # safety bound; the stub pipeline needs about 10


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
        console.print(f"Created [bold]{product.name}[/bold] (id {product.id})")

        ops = OperationsManager(session, settings)
        for _ in range(MAX_DEMO_TICKS):
            if not ops.tick():
                break
        session.refresh(product)

        table = Table(title=f"Event log: {product.name}")
        for column in ("time (UTC)", "event", "actor", "from", "to", "message"):
            table.add_column(column)
        for event in session.scalars(select(Event).where(Event.product_id == product.id).order_by(Event.id)):
            table.add_row(event.created_at.strftime("%H:%M:%S"), event.event_type, event.actor,
                          event.from_status or "", event.to_status or "", event.message)
        console.print(table)
        style = "green" if product.status == ProductStatus.HUMAN_REVIEW else "red"
        console.print(f"Final status: [bold {style}]{product.status}[/bold {style}]. "
                      "Nothing is published without human approval.")
        return 0 if product.status == ProductStatus.HUMAN_REVIEW else 1


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
        console.print(f"  #{job['id']} {job['department']} {job['job_type']} - {job['product']}")
    console.print("Cloud spend (USD):", data["cloud_spend_usd"])
    console.print("Limits:", data["limits"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Hive-Mint command line")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="create the SQLite database").set_defaults(func=cmd_init_db)
    commands.add_parser("demo", help="run the sample product through stub departments to HUMAN_REVIEW") \
        .set_defaults(func=cmd_demo)
    commands.add_parser("status", help="show product and job counts").set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
