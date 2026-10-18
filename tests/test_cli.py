import json
import zipfile

import pytest
from sqlalchemy import select

from agents.operations import OperationsManager, default_handlers
from agents.qa import QA
from app import cli
from app.db import make_engine, make_session_factory
from app.lifecycle import create_product
from app.models import Build, Product, Review, ReworkBrief, TestResult
from app.states import Department, ProductStatus
from tests.conftest import advance_to, ollama_client, ollama_reply

CLEAR = {"clarity_score": 92, "findings": []}


@pytest.fixture
def cli_env(settings, monkeypatch, tmp_path):
    """The CLI wired to the test database, a temp reviews folder and a mocked Ollama."""
    engine = make_engine(settings.db_url)
    client = ollama_client(lambda request: ollama_reply(CLEAR))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_engine", lambda: engine)
    monkeypatch.setattr(cli, "red_team_dir", lambda: tmp_path / "reviews")
    monkeypatch.setattr(cli, "OperationsManager", lambda session, s: OperationsManager(
        session, s, handlers={**default_handlers(s), Department.QA: QA(s, client)}))
    yield engine, tmp_path / "reviews"
    engine.dispose()


def _product(engine, slug=cli.DEMO["slug"]):
    with make_session_factory(engine)() as session:
        return session.scalar(select(Product).where(Product.slug == slug))


def test_demo_stops_for_red_team_then_a_pass_verdict_reaches_human_review(cli_env, settings, fake_claude,
                                                                         sample_spec):
    from tests.test_seo import good_draft

    engine, reviews = cli_env
    assert cli.main(["demo"]) == 0
    assert cli.main(["demo"]) == 1  # refuses to duplicate the sample product
    product = _product(engine)
    assert product.status == ProductStatus.RED_TEAM
    with make_session_factory(engine)() as session:
        build = session.scalar(select(Build).where(Build.product_id == product.id))
        assert build.path == str(settings.products_dir / str(product.id) / "v1")
        assert zipfile.ZipFile(build.zip_path).testzip() is None
        results = session.scalars(select(TestResult).where(TestResult.build_id == build.id)).all()
        assert results and all(r.passed for r in results)
        qa = session.scalar(select(Review).where(Review.department == Department.QA))
        assert qa.decision == "PASS" and qa.reviewer == "qa+ollama"

    assert cli.main(["review-export", str(product.id)]) == 0
    packet = reviews / str(product.id) / "packet.md"
    assert "## Product digest" in packet.read_text(encoding="utf-8")

    verdict = reviews / str(product.id) / "verdict.json"
    verdict.write_text(json.dumps({"product_id": product.id, "build_version": 2, "decision": "PASS",
                                   "summary": "Holds up against free alternatives."}))
    assert cli.main(["review-import", str(product.id), str(verdict)]) == 2  # stale build version
    assert _product(engine).status == ProductStatus.RED_TEAM
    verdict.write_text(json.dumps({"product_id": product.id, "build_version": 1, "decision": "PASS",
                                   "summary": "Holds up against free alternatives."}))
    assert cli.main(["review-import", str(product.id), str(verdict)]) == 0
    assert _product(engine).status == ProductStatus.COMPLIANCE

    fake_claude.replies = [{"structured_output": good_draft(sample_spec)}]  # the SEO listing draft
    assert cli.main(["run"]) == 0
    assert _product(engine).status == ProductStatus.HUMAN_REVIEW
    assert cli.main(["status"]) == 0
    assert cli.main(["review"]) == 0 and cli.main(["review", str(product.id)]) == 0
    assert cli.main(["approve", str(product.id), "--by", "Kenny"]) == 0
    assert _product(engine).status == ProductStatus.READY_TO_PUBLISH


def test_rework_command_seeds_a_brief_and_resume_returns_a_held_product(cli_env, session, settings):
    engine, _ = cli_env
    product = create_product(session, settings, slug="tracker", name="Tracker", product_type="spreadsheet")
    advance_to(session, settings, product, ProductStatus.HUMAN_REVIEW)
    session.commit()

    assert cli.main(["rework", str(product.id), str(cli.Path("does-not-exist.json"))]) == 2
    seed = cli.Path(__file__).parent.parent / "config" / "rework" / \
        "mobile-auto-detailer-income-expense-tracker-001.json"
    assert cli.main(["rework", str(product.id), str(seed)]) == 0
    with make_session_factory(engine)() as check:
        reloaded = check.get(Product, product.id)
        assert reloaded.status == ProductStatus.REWORK and reloaded.rework_count == 1
        brief = check.scalar(select(ReworkBrief))
        assert brief.source == "HUMAN" and brief.change_ids[0] == "merge_workbooks"

    assert cli.main(["resume", str(product.id)]) == 2  # not held
    assert cli.main(["resume", "999"]) == 2


def test_research_command_records_a_niche(cli_env, fake_claude, capsys):
    from app.models import Opportunity, ResearchRecord
    from tests.test_research import research_answer

    engine, _ = cli_env
    fake_claude.replies = [{"structured_output": research_answer()}]
    assert cli.main(["research", "--niche", "small motels"]) == 0
    with make_session_factory(engine)() as session:
        assert session.scalar(select(ResearchRecord)).niche == "small motels"
        assert session.scalar(select(Opportunity)).title == "Motel Night Audit Workbook"
    assert "high" in capsys.readouterr().out


def test_research_command_reports_a_refusal(cli_env, fake_claude, monkeypatch, capsys):
    engine, _ = cli_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert cli.main(["research", "--niche", "x"]) == 2
    assert "HEADLESS_REFUSED" in capsys.readouterr().out
    assert not fake_claude.calls


def test_approve_rework_command_grants_cycles(cli_env, settings):
    from app.lifecycle import transition

    from app.db import init_db

    engine, _ = cli_env
    init_db(engine)
    with make_session_factory(engine)() as session:
        product = create_product(session, settings, slug="p1", name="P1", product_type="spreadsheet")
        advance_to(session, settings, product, ProductStatus.QA)
        transition(session, settings, product, ProductStatus.REWORK, actor="qa", reason="fail")
        session.commit()
        pid = product.id
    assert cli.main(["approve-rework", str(pid), "--cycles", "2"]) == 0
    assert _product(engine, "p1").extra_rework_cycles == 2
    assert cli.main(["approve-rework", "999"]) == 2


def test_spec_check_reports_ok_and_problems(cli_env, tmp_path, capsys, monkeypatch):
    from agents import testing
    from tools.spec import SPEC_DIR

    monkeypatch.setattr(testing, "run_checks", lambda spec, out, **kw: [testing.Check("file_exists", "x", True)])
    good = SPEC_DIR / f"{cli.DEMO['slug']}.json"
    assert cli.main(["spec-check", str(good)]) == 0
    assert "SPEC OK" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(json.loads(good.read_text(encoding="utf-8")), claims_not_to_make=[])))
    assert cli.main(["spec-check", str(bad)]) == 1
    assert "claims_not_to_make is empty" in capsys.readouterr().out
    monkeypatch.setattr(testing, "run_checks",
                        lambda spec, out, **kw: [testing.Check("scenario", "Dashboard", False, "expected 700")])
    assert cli.main(["spec-check", str(good)]) == 1
    assert "expected 700" in capsys.readouterr().out


def test_shift_command_runs_and_writes_the_report(cli_env, monkeypatch, tmp_path, capsys):
    from app import report, shift

    monkeypatch.setattr(report, "report_path", lambda: tmp_path / "Hive Mint Daily Report.txt")
    monkeypatch.setattr(shift, "on_battery", lambda: True)
    assert cli.main(["shift"]) == 0
    out = capsys.readouterr().out
    assert "skipped: on battery" in out
    assert "skipped: on battery" in (tmp_path / "Hive Mint Daily Report.txt").read_text(encoding="utf-8")
