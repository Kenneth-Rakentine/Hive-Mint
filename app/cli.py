"""Command line: python -m app.cli {init-db,demo,run,status,rework,resume,approve-rework,review-export,
review-import,research,spec-check,shift,report,review,approve,reject,relist,office}"""

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
from app.lifecycle import create_product, log_event, resume, transition
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


BUSY = ("Another shift (or `run`) is already working. Only one may run at a time; watch it in the office, "
        "or wait for it to finish.")


def cmd_run(_args) -> int:
    from tools.single_instance import lock_path, single_instance

    settings = get_settings()
    with single_instance(lock_path(settings.db_url)) as mine, _session() as session:
        if not mine:
            console.print(f"[yellow]{BUSY}[/yellow]")
            return 1
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
        if product.status == ProductStatus.READY_TO_PUBLISH:  # approved but not uploaded: withdraw it first
            transition(session, settings, product, ProductStatus.HUMAN_REVIEW, actor=args.actor,
                       reason=f"Approval withdrawn by {args.actor} for a rework: {brief_file.summary}")
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


def cmd_research(args) -> int:
    """Research one niche with headless Claude Code (web search, public pages only)."""
    from agents.research import ResearchFailed, run_niche_research
    from app.lifecycle import LimitReached
    from app.models import Opportunity
    from providers.claude_headless import HeadlessRefused

    settings = get_settings()
    with _session() as session:
        try:
            record = run_niche_research(session, settings, args.niche)
        except (ResearchFailed, LimitReached, HeadlessRefused) as error:
            session.commit()  # the job, its runs and events are the record of what happened
            console.print(f"[red]{escape(str(error))}[/red]")
            return 2
        session.commit()
        console.print(f"Research record #{record.id}: [bold]{record.confidence}[/bold] confidence, "
                      f"{len(json.loads(record.sources_json))} sources")
        console.print(escape(record.summary))
        for opp in session.scalars(select(Opportunity).where(Opportunity.research_id == record.id)):
            console.print(escape(f"  opportunity #{opp.id}: {opp.title} ({opp.product_type})"))
        console.print("Finance scores pending opportunities in the next shift.")
        return 0


def cmd_scout(_args) -> int:
    """Scout for new niches now (one headless Claude run, web search only)."""
    from agents.scout import ScoutFailed, run_scout
    from app.lifecycle import LimitReached
    from providers.claude_headless import HeadlessRefused

    settings = get_settings()
    with _session() as session:
        try:
            rows = run_scout(session, settings)
        except (ScoutFailed, LimitReached, HeadlessRefused) as error:
            session.commit()
            console.print(f"[red]{escape(str(error))}[/red]")
            return 2
        session.commit()
        console.print(f"Scouted [bold]{len(rows)}[/bold] niche candidates. The next shift researches the best one.")
    return cmd_niches(None)


def cmd_niches(_args) -> int:
    """List the scout's niche candidates, best first."""
    from agents.scout import CANDIDATE_MIN_SCORE
    from app.models import NicheCandidate

    with _session() as session:
        rows = session.scalars(select(NicheCandidate).order_by(NicheCandidate.score.desc(), NicheCandidate.id)).all()
        if not rows:
            console.print("No niche candidates yet. Run [bold]python -m app.cli scout[/bold], or wait for a shift.")
            return 0
        table = Table(title="Niche candidates (score 0-100; researched in depth from the top)")
        for col in ("#", "Score", "Status", "Niche", "Prices", "Demand", "Build"):
            table.add_column(col)
        for row in rows:
            data, factors = json.loads(row.data_json), json.loads(row.factors_json)
            status = row.status if row.status != NicheCandidate.NEW or row.score >= CANDIDATE_MIN_SCORE else "weak"
            table.add_row(str(row.id), f"{row.score:.0f}", status, escape(row.niche),
                          f"${data.get('price_low', 0):.0f}-{data.get('price_high', 0):.0f}",
                          str(factors["demand"]["score"]), str(factors["buildability"]["score"]))
        console.print(table)
    return 0


def cmd_rescore(args) -> int:
    """Send recent turned-down ideas back to Finance to be scored with the current settings."""
    from agents.finance import reopen_recent

    with _session() as session:
        rows = reopen_recent(session, days=args.days)
        session.commit()
        for opp in rows:
            console.print(escape(f"  #{opp.id} {opp.title}"))
        console.print(f"{len(rows)} idea(s) go back to Finance; the next shift scores them "
                      "(or run [bold]python -m app.cli shift[/bold] now).")
    return 0


def cmd_approve_rework(args) -> int:
    """The owner grants extra rework cycles; a product paused by the rework limit resumes into REWORK."""
    from app.lifecycle import approve_rework, rework_limit

    settings = get_settings()
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        try:
            approve_rework(session, settings, product, cycles=args.cycles, actor=args.actor)
        except (IllegalTransition, ValueError) as error:
            console.print(f"[red]{escape(str(error))}[/red]")
            return 2
        session.commit()
        console.print(f"Product {product.id}: {args.cycles} extra rework cycle(s) granted (limit now "
                      f"{rework_limit(settings, product)}, used {product.rework_count}). Status: "
                      f"[bold]{product.status}[/bold]. The next shift continues the rework.")
        return 0


def cmd_spec_check(args) -> int:
    """Validate a spec file, build it into a temporary folder and run every Testing check on it."""
    import tempfile

    from agents.spec_writer import spec_problems
    from agents.testing import run_checks
    from tools.builders import BUILDERS
    from tools.packaging import make_zip
    from tools.recalc import choose_engine
    from tools.spec import SpecError, prepare

    try:
        spec = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        console.print(f"SPEC FAILED: cannot read {escape(args.path)}: {escape(str(error))}")
        return 1
    if problems := spec_problems(spec):
        console.print("SPEC FAILED:\n" + "\n".join(f"- {escape(p)}" for p in problems))
        return 1
    settings = get_settings()
    spec = prepare(spec)
    with tempfile.TemporaryDirectory(prefix="hivemint-speccheck-") as tmp:
        out = Path(tmp) / "build"
        try:
            written = BUILDERS[spec["builder"]](spec, out)
            make_zip(out / spec["zip_name"], written)
        except (SpecError, KeyError, ValueError) as error:
            console.print(f"SPEC FAILED: the builder cannot build it: {escape(str(error))}")
            return 1
        engine, soffice = choose_engine(settings.recalc_engine, settings.soffice_path)
        checks = run_checks(spec, out, engine=engine, soffice=soffice)
    failed = [c for c in checks if not c.passed]
    if failed:
        console.print(f"SPEC FAILED: {len(failed)} of {len(checks)} Testing checks failed:\n"
                      + "\n".join(f"- {escape(c.reason())}" for c in failed))
        return 1
    console.print(f"SPEC OK: built and passed all {len(checks)} Testing checks")
    return 0


def cmd_shift(args) -> int:
    """One unattended shift: Research -> Finance -> Spec -> Production -> Testing -> QA -> Red Team -> Rework."""
    from app.report import write_report
    from app.shift import ShiftRunner

    from tools.single_instance import lock_path, single_instance

    settings = get_settings()
    with single_instance(lock_path(settings.db_url)) as mine, _session() as session:
        if not mine:
            console.print(f"[yellow]{BUSY}[/yellow]")
            return 1
        shift = ShiftRunner(session, settings, ignore_window=args.ignore_window).run()
        console.print(f"Shift #{shift.id}: {escape(shift.stop_reason)}")
        for step in json.loads(shift.steps_json):
            console.print(escape(f"  {step['step']:<11} {step['summary']}"))
        console.print(f"Claude headless runs this shift: {shift.claude_runs}")
        try:
            path = write_report(session, settings)
            console.print(f"Daily report: {escape(str(path))}")
        except OSError as error:
            console.print(f"[red]Could not write the daily report: {escape(str(error))}[/red]")
        return 0


def cmd_report(_args) -> int:
    from app.report import write_report

    with _session() as session:
        console.print(f"Daily report: {escape(str(write_report(session, get_settings())))}")
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


# ---------------------------------------------------------------------- the owner's review desk

def cmd_review(args) -> int:
    """Show the evidence for one product (or list everything waiting for a decision)."""
    from app.review import review_card

    with _session() as session:
        if args.product_id is None:
            from agents.owner_desk import APPROVER, desk_pending

            settings = get_settings()
            review = session.scalars(select(Product).where(Product.status == ProductStatus.HUMAN_REVIEW)
                                     .order_by(Product.id)).all()
            ready = session.scalars(select(Product).where(Product.status == ProductStatus.READY_TO_PUBLISH)
                                    .order_by(Product.id)).all()
            waiting = [p for p in review if not desk_pending(session, settings, p)]
            console.print("Needs you:" if waiting else "Nothing needs your decision.")
            for product in waiting:
                console.print(escape(f"  #{product.id} {product.name}: {product.status_reason}"))
            if queued := [p for p in review if desk_pending(session, settings, p)]:
                console.print("Waiting for the owner's desk (the next shift, or python -m app.cli desk):")
                for product in queued:
                    console.print(escape(f"  #{product.id} {product.name}"))
            if ready:
                console.print("Approved, ready for you to upload:")
            for product in ready:
                who = "the owner's desk" if product.approved_by == APPROVER else product.approved_by
                console.print(escape(f"  #{product.id} {product.name} (approved by {who})"))
            if waiting or ready:
                console.print("See one with [bold]python -m app.cli review <id>[/bold].")
            return 0
        product = _product(session, args.product_id)
        if product is None:
            return 2
        card = review_card(session, product)
    rule = "-" * 72
    lines = [rule, f"PRODUCT #{card['id']}  {card['name']}", f"Status: {card['status']}  ({card['status_reason']})", rule]
    if build := card.get("build"):
        tests = card["testing"]
        lines.append(f"Build v{build['version']}: {build['path']}")
        lines.append(f"Testing:     {tests['passed']}/{tests['total']} checks passed"
                     + ("" if not tests["failed"] else "  FAILED: " + "; ".join(tests["failed"])))
    if qa := card.get("qa"):
        lines.append(f"QA:          {qa['decision']} {qa['score'] if qa['score'] is not None else ''}/100")
    if red := card.get("red_team"):
        lines.append(f"Red Team:    {red['decision']} - {red['summary'][:160]}")
    if comp := card.get("compliance"):
        lines.append(f"Compliance:  {comp['message']}")
        lines += [f"             ! {item}" for item in comp["ambiguous"] + comp["claims"]]
    lines.append(f"Rework cycles used: {card['rework_cycles']}")
    if desk := card.get("owner_desk"):
        lines.append(f"Owner's desk: {desk['decision']} ({desk['pictures']} picture(s) checked) - {desk['summary'][:300]}")
        if desk["question"]:
            lines.append(f"             ? {desk['question'][:300]}")
        lines += [f"             ! {item}" for item in desk["issues"]]
    if card.get("approved_by"):
        lines.append(f"Approved by: {card['approved_by']}")
    if runs := card.get("ai_runs"):
        lines.append("AI runs:     " + ", ".join(f"{p} x{r['runs']}" for p, r in runs.items())
                     + "  (Claude runs are covered by your Max plan; nothing billed)")
    if listing := card.get("listing"):
        lines += [rule, f"LISTING  ${listing['price']:.2f}  ({listing['price_reason'][:120]})",
                  f"Title: {listing['title']}", f"Tags:  {', '.join(listing['tags'])}",
                  f"Images: {listing['images']}   Package: {listing['path']}",
                  "Open LISTING.txt and the images folder there to check it."]
    elif card["status"] == "HUMAN_REVIEW":
        lines += [rule, f"No listing yet. Make one: python -m app.cli relist {card['id']}"]
    lines += [rule, "Decide:", f"  python -m app.cli approve {card['id']} --by \"Your Name\"   (if it is still in HUMAN_REVIEW)",
              f"  python -m app.cli relist {card['id']} --reason \"what to change in the listing\"",
              f"  python -m app.cli reject {card['id']} --reason \"why\"",
              f"  python -m app.cli rework {card['id']} <brief.json>   (to change the product itself)"]
    console.print(escape("\n".join(lines)))
    return 0


def _decide(args, action) -> int:
    settings = get_settings()
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        try:
            action(session, settings, product)
        except (IllegalTransition, ValueError) as error:
            console.print(f"[red]{escape(str(error))}[/red]")
            return 2
        session.commit()
        console.print(escape(f"Product {product.id} is now {product.status}: {product.status_reason}"))
        return 0


def cmd_approve(args) -> int:
    from app.review import approve, latest_listing

    code = _decide(args, lambda s, st, p: approve(s, st, p, approver=args.by, note=args.note))
    if code == 0:
        with _session() as session:
            listing = latest_listing(session, session.get(Product, args.product_id))
            console.print(escape(f"Upload it by hand: follow UPLOAD_STEPS.txt in {listing.path}"))
    return code


def cmd_reject(args) -> int:
    from app.review import reject

    return _decide(args, lambda s, st, p: reject(s, st, p, actor=args.actor, reason=args.reason))


def cmd_relist(args) -> int:
    from app.review import relist

    code = _decide(args, lambda s, st, p: relist(s, st, p, actor=args.actor, reason=args.reason))
    if code == 0:
        console.print("The next shift (or python -m app.cli run) writes a new listing.")
    return code


def cmd_desk(args) -> int:
    """Run the owner's desk now: on one product, or on every product waiting for it."""
    from agents.operations import HARD_STOPS
    from agents.owner_desk import OwnerDesk, desk_pending, pending_products
    from tools.single_instance import lock_path, single_instance

    settings = get_settings()
    with single_instance(lock_path(settings.db_url)) as mine, _session() as session:
        if not mine:
            console.print(f"[yellow]{BUSY}[/yellow]")
            return 1
        if args.product_id is not None:
            product = _product(session, args.product_id)
            if product is None:
                return 2
            if not desk_pending(session, settings, product):
                console.print(escape(f"Product {product.id} is not waiting for the owner's desk "
                                     f"({product.status}; the desk looks once at each new listing from SEO)."))
                return 2
            products = [product]
        else:
            products = pending_products(session, settings)
            if not products:
                console.print("Nothing is waiting for the owner's desk.")
                return 0
        desk = OwnerDesk(settings)
        for product in products:
            try:
                result = desk.review(session, product)
            except HARD_STOPS as error:
                console.print(f"[red]Stopped:[/red] {escape(str(error))}")
                return 2
            session.commit()
            console.print(escape(f"#{product.id} {product.name}: {result.decision} -> {product.status}. "
                                 f"{result.summary}"))
    return 0


def cmd_drafted(args) -> int:
    """Record that an approved product's Etsy listing was saved as a draft (the owner still publishes it)."""
    with _session() as session:
        product = _product(session, args.product_id)
        if product is None:
            return 2
        if product.status != ProductStatus.READY_TO_PUBLISH:
            console.print(f"[red]Product {product.id} is {product.status}; only an approved product gets a draft.[/red]")
            return 2
        log_event(session, "listing.drafted", actor=args.actor, product=product,
                  message=f"Etsy draft saved{': ' + args.note if args.note else ''}; the owner checks it and publishes",
                  data={"note": args.note})
        session.commit()
        console.print(escape(f"Recorded: product {product.id}'s Etsy draft is saved. Open Shop Manager > Listings > "
                             "Drafts, check it and press Publish."))
    return 0


def cmd_office(args) -> int:
    """Open the pixel office in its own window, served on this computer only.

    If the office server is already running, this just opens another window. Otherwise it starts the
    server; with --close-when-idle (what the desktop shortcut uses) the server stops on its own a couple
    of minutes after the last office window closes. Without it, Ctrl+C stops the server.
    """
    import socket
    import threading
    import time

    import uvicorn

    from app import api
    from tools.app_window import open_window

    url = f"http://127.0.0.1:{args.port}/"
    with socket.socket() as probe:
        running = probe.connect_ex(("127.0.0.1", args.port)) == 0
    if running:
        how = open_window(url, app=not args.tab)
        console.print(f"The office is already running; opened it in a new {how} ({url}).")
        return 0

    config = uvicorn.Config("app.api:app", host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    if args.close_when_idle:
        def watchdog():
            time.sleep(args.idle_minutes * 60)  # give the first window time to open
            while not server.should_exit:
                if time.monotonic() - api.last_seen() > args.idle_minutes * 60:
                    server.should_exit = True
                time.sleep(15)
        threading.Thread(target=watchdog, daemon=True).start()
    if not args.no_window:
        threading.Timer(1.5, open_window, args=(url,), kwargs={"app": not args.tab}).start()
    console.print(f"The office is at [bold]{url}[/bold] (only this computer can see it)."
                  + ("" if args.close_when_idle else " Press Ctrl+C to stop it."))
    server.run()
    return 0

def ensure_output_streams(log_dir: Path) -> Path | None:
    """pythonw.exe (the Desktop shortcut) starts with no console, so sys.stdout and sys.stderr are None and
    anything that prints or logs (rich, uvicorn) fails before the office opens. Send both to a log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "office.log"
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream
    return path


def main(argv: list[str] | None = None) -> int:
    ensure_output_streams(Path(__file__).resolve().parents[1] / "logs")
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
    approve = commands.add_parser("approve-rework", help="grant a product extra rework cycles (resumes it if paused)")
    approve.add_argument("product_id", type=int)
    approve.add_argument("--cycles", type=int, default=1)
    approve.add_argument("--actor", default="owner")
    approve.set_defaults(func=cmd_approve_rework)
    check = commands.add_parser("spec-check", help="validate, build and test a spec file (no database changes)")
    check.add_argument("path")
    check.set_defaults(func=cmd_spec_check)
    shift = commands.add_parser("shift", help="run one unattended shift, then write the daily report")
    shift.add_argument("--ignore-window", action="store_true", help="run outside SHIFT_START-SHIFT_END (manual runs)")
    shift.set_defaults(func=cmd_shift)
    commands.add_parser("report", help="rewrite the daily report on the Desktop").set_defaults(func=cmd_report)
    commands.add_parser("scout", help="scout for new niches now (one headless Claude run)").set_defaults(func=cmd_scout)
    desk = commands.add_parser("desk", help="run the owner's desk now: approve, send back or leave for you")
    desk.add_argument("product_id", type=int, nargs="?")
    desk.set_defaults(func=cmd_desk)
    drafted = commands.add_parser("drafted", help="record that a product's Etsy listing is saved as a draft")
    drafted.add_argument("product_id", type=int)
    drafted.add_argument("--note", default="", help="e.g. 'photos still to add'")
    drafted.add_argument("--actor", default="claude-session")
    drafted.set_defaults(func=cmd_drafted)
    commands.add_parser("niches", help="list the scout's niche candidates").set_defaults(func=cmd_niches)
    rescore = commands.add_parser("rescore", help="send recent turned-down ideas back to Finance")
    rescore.add_argument("--days", type=int, default=14)
    rescore.set_defaults(func=cmd_rescore)
    research = commands.add_parser("research", help="research a niche with headless Claude Code (web search)")
    research.add_argument("--niche", required=True)
    research.set_defaults(func=cmd_research)
    review = commands.add_parser("review", help="show a product's evidence and listing (no id: list what waits)")
    review.add_argument("product_id", type=int, nargs="?")
    review.set_defaults(func=cmd_review)
    approve_cmd = commands.add_parser("approve", help="approve a reviewed product: READY_TO_PUBLISH (upload by hand)")
    approve_cmd.add_argument("product_id", type=int)
    approve_cmd.add_argument("--by", required=True, help="your name, recorded with the approval")
    approve_cmd.add_argument("--note", default="")
    approve_cmd.set_defaults(func=cmd_approve)
    reject_cmd = commands.add_parser("reject", help="reject a reviewed product")
    reject_cmd.add_argument("product_id", type=int)
    reject_cmd.add_argument("--reason", required=True)
    reject_cmd.add_argument("--actor", default="owner")
    reject_cmd.set_defaults(func=cmd_reject)
    relist_cmd = commands.add_parser("relist", help="send only the listing back to SEO for a new draft")
    relist_cmd.add_argument("product_id", type=int)
    relist_cmd.add_argument("--reason", default="")
    relist_cmd.add_argument("--actor", default="owner")
    relist_cmd.set_defaults(func=cmd_relist)
    office = commands.add_parser("office", help="open the pixel office in its own window (http://127.0.0.1:8000)")
    office.add_argument("--port", type=int, default=8000)
    office.add_argument("--no-window", action="store_true", help="start the server without opening a window")
    office.add_argument("--tab", action="store_true", help="open a normal browser tab instead of an app window")
    office.add_argument("--close-when-idle", action="store_true",
                        help="stop the server when no office window has checked in for --idle-minutes")
    office.add_argument("--idle-minutes", type=float, default=2.0)
    office.set_defaults(func=cmd_office)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
