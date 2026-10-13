"""Command line: python -m app.cli {init-db,demo,run,status,rework,resume,review-export,review-import}"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from agents import red_team
from agents.operations import OperationsManager
from agents.rework import BriefFile, create_brief
from app.db import get_engine, init_db, make_session_factory
from app.lifecycle import create_product, resume, transition
from app.models import Build, Event, Product, TestResult
from app.states import IllegalTransition, ProductStatus, can_transition
from app.status import snapshot
from config.settings import get_settings

console = Console()

DEMO = {
    "slug": "mobile-auto-detailer-income-expense-tracker",
    "name": "Mobile Auto Detailer Income & Expense Tracker",
    "product_type": "spreadsheet",
    "description": "Track jobs, income, supplies, mileage and expenses for a mobile auto detailing business.",
}
MAX_TICKS = 50  # safety bound; a clean run needs about 10 ticks, each rework cycle adds a few


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
        console.print(f"Created [bold]{product.name}[/bold] (id {product.id}). Building, testing and QA-ing real "
                      "files with the local model; this takes a few minutes.")

        _run_until_idle(session, settings)
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
        return _print_outcome(product)


def _run_until_idle(session, settings) -> int:
    ops = OperationsManager(session, settings)
    total = 0
    for _ in range(MAX_TICKS):
        ran = ops.tick()
        if not ran:
            break
        total += ran
    return total


def _print_outcome(product: Product) -> int:
    waiting = product.status in (ProductStatus.RED_TEAM, ProductStatus.HUMAN_REVIEW)
    style = "green" if waiting else "red"
    console.print(f"Status: [bold {style}]{product.status}[/bold {style}]. {escape(product.status_reason)}")
    if product.status == ProductStatus.RED_TEAM:
        console.print(f"Next: run the Red Team review in Claude Code: [bold]/hive-review {product.id}[/bold] "
                      f"(or review-export / review-import {product.id} by hand).")
    elif product.status in (ProductStatus.PAUSED, ProductStatus.ERROR):
        console.print(f"Fix the cause, then: [bold]python -m app.cli resume {product.id}[/bold]")
    console.print("Nothing is published without human approval.")
    return 0 if waiting else 1


def _product(session, product_id: int) -> Product | None:
    product = session.get(Product, product_id)
    if product is None:
        console.print(f"[red]No product with id {product_id}[/red]")
    return product


def cmd_run(_args) -> int:
    settings = get_settings()
    with _session() as session:
        ran = _run_until_idle(session, settings)
        console.print(f"Ran {ran} job(s).")
        for product in session.scalars(select(Product).order_by(Product.id)):
            console.print(escape(f"  #{product.id} {product.name}: {product.status} - {product.status_reason}"))
            build = session.scalar(select(Build).where(Build.product_id == product.id)
                                   .order_by(Build.version.desc()).limit(1))
            if build is not None:
                console.print(escape(f"      latest build v{build.version}: {build.path}"))
        return 0


def cmd_rework(args) -> int:
    """A person sends a product back with a structured rework brief."""
    settings = get_settings()
    try:
        brief_file = BriefFile.model_validate(json.loads(Path(args.brief).read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        console.print(f"[red]Invalid rework brief:[/red] {escape(str(error))}")
        return 2
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        if not can_transition(product.status, ProductStatus.REWORK):
            console.print(f"[red]A product in {product.status} cannot be sent to REWORK.[/red]")
            return 2
        brief = create_brief(session, product, source="HUMAN", actor=args.actor, summary=brief_file.summary,
                             required_changes=[c.model_dump() for c in brief_file.required_changes],
                             findings=brief_file.findings)
        transition(session, settings, product, ProductStatus.REWORK, actor=args.actor,
                   reason=f"Rework brief #{brief.id}: {brief_file.summary}")
        session.commit()
        console.print(f"Product {product.id} is now [bold]{product.status}[/bold] with rework brief #{brief.id} "
                      f"({', '.join(brief.change_ids)}). Run [bold]python -m app.cli run[/bold] to rebuild.")
        return 0


def cmd_resume(args) -> int:
    settings = get_settings()
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        try:
            resume(session, settings, product, actor=args.actor)
        except IllegalTransition as error:
            console.print(f"[red]{escape(str(error))}[/red]")
            return 2
        session.commit()
        console.print(f"Product {product.id} resumed to [bold]{product.status}[/bold].")
        return 0


def cmd_review_export(args) -> int:
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        try:
            packet = red_team.export_packet(session, product, red_team_dir())
        except red_team.VerdictError as error:
            console.print(f"[red]{escape(str(error))}[/red]")
            return 2
        session.commit()
        console.print(f"Review packet: [bold]{escape(str(packet))}[/bold]")
        if product.status != ProductStatus.RED_TEAM:
            console.print(f"[yellow]Note: product is {product.status}; a verdict can only be imported at "
                          "RED_TEAM.[/yellow]")
        return 0


def cmd_review_import(args) -> int:
    settings = get_settings()
    path = Path(args.verdict)
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        try:
            verdict = red_team.load_verdict(path)
            review = red_team.import_verdict(session, settings, product, verdict, source=path, base=red_team_dir())
        except red_team.VerdictError as error:
            session.rollback()
            console.print(f"[red]Verdict not recorded:[/red] {escape(str(error))}")
            return 2
        session.commit()
        console.print(f"Recorded Red Team verdict #{review.id}: [bold]{review.decision}[/bold].")
        _print_outcome(product)
        return 0


def red_team_dir() -> Path:
    return red_team.REVIEWS_DIR


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
    commands.add_parser("demo", help="build, test and QA the sample product, then stop for the Red Team") \
        .set_defaults(func=cmd_demo)
    commands.add_parser("status", help="show product and job counts, disk usage and limits") \
        .set_defaults(func=cmd_status)
    commands.add_parser("run", help="run Operations until no job is left to run").set_defaults(func=cmd_run)
    rework = commands.add_parser("rework", help="send a product back with a rework brief (JSON file)")
    rework.add_argument("product_id", type=int)
    rework.add_argument("brief")
    rework.add_argument("--actor", default="owner")
    rework.set_defaults(func=cmd_rework)
    resume_cmd = commands.add_parser("resume", help="return a PAUSED or ERROR product to its gate")
    resume_cmd.add_argument("product_id", type=int)
    resume_cmd.add_argument("--actor", default="owner")
    resume_cmd.set_defaults(func=cmd_resume)
    export = commands.add_parser("review-export", help="write the Red Team review packet")
    export.add_argument("product_id", type=int)
    export.set_defaults(func=cmd_review_export)
    import_cmd = commands.add_parser("review-import", help="validate and record a Red Team verdict (JSON file)")
    import_cmd.add_argument("product_id", type=int)
    import_cmd.add_argument("verdict")
    import_cmd.set_defaults(func=cmd_review_import)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
