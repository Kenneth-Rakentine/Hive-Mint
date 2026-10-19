"""The niche scout: a wide, cited search for new niches, scored by code. Claude is always faked."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from agents.research import run_niche_research, scout_notes
from agents.scout import (CANDIDATE_MIN_SCORE, ScoutFailed, candidate_score, known_niches, next_candidate,
                          run_scout, scout_due)
from app.db import utcnow
from app.models import Job, NicheCandidate, ResearchRecord
from app.states import JobStatus
from tests.test_research import research_answer


def factor(score, sources=(0,)):
    return {"score": score, "reason": f"evidence for {score}", "sources": list(sources)}


def candidate(niche, demand=4, competition=2, price=4, buildability=5, safety=5):
    return {"niche": niche, "audience": "people who need it", "example_products": ["Tracker", "Planner"],
            "price_low": 8, "price_high": 18, "why_now": "Many listings show hundreds of reviews.",
            "risks": [], "factors": {"demand": factor(demand), "competition": factor(competition, (1,)),
                                     "price": factor(price, (2,)), "buildability": factor(buildability, (3,)),
                                     "safety": factor(safety, (4,))}}


NICHES = ["wedding budget planning for couples", "small landlord rent and repair tracking",
          "classroom grade and attendance tracking for teachers", "craft fair vendor inventory and sales",
          "freelance invoice and time tracking", "homeschool lesson planning", "garden planting and harvest logs",
          "pet sitter booking and income tracking", "weak niche with no buyers"]


def scout_answer(**overrides):
    cands = [candidate(n) for n in NICHES[:-1]] + [candidate(NICHES[-1], demand=1, price=1, buildability=2)]
    answer = {"summary": "Trackers with totals sell across many audiences.",
              "sources": [{"url": f"https://example.com/s{i}", "title": f"s{i}", "supports": "listing evidence"}
                          for i in range(10)],
              "candidates": cands}
    return answer | overrides


def test_score_formula_rewards_demand_and_buildability_and_inverts_competition():
    best = candidate_score({"demand": 5, "competition": 1, "price": 5, "buildability": 5, "safety": 5})
    worst = candidate_score({"demand": 1, "competition": 5, "price": 1, "buildability": 1, "safety": 1})
    assert (best, worst) == (100.0, 0.0)
    crowded = candidate_score({"demand": 5, "competition": 5, "price": 5, "buildability": 5, "safety": 5})
    assert crowded == 85.0


def test_a_valid_scout_stores_scored_candidates(settings, session, fake_claude):
    fake_claude.replies = [{"structured_output": scout_answer()}]
    rows = run_scout(session, settings)
    assert len(rows) == 9 and all(r.status == NicheCandidate.NEW for r in rows)
    assert next_candidate(session).score >= CANDIDATE_MIN_SCORE
    weak = next(r for r in rows if r.niche == NICHES[-1])
    assert weak.score < CANDIDATE_MIN_SCORE
    assert json.loads(rows[0].sources_json) and json.loads(rows[0].data_json)["example_products"]
    cmd, prompt = fake_claude.calls[0]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch" and "public pages only" in prompt
    assert "mobile service businesses" in prompt  # seed niches are excluded from the search
    assert session.scalar(select(Job).where(Job.job_type == "niche_scout")).status == JobStatus.SUCCEEDED


def test_thin_branded_or_repeated_answers_are_retried_with_the_problems(settings, session, fake_claude):
    thin = scout_answer(sources=[{"url": "https://example.com/a", "supports": "x"}])
    branded = scout_answer(candidates=[candidate(n) for n in NICHES[:7]] + [candidate("Etsy shop owners")])
    repeated = scout_answer(candidates=[candidate(n) for n in NICHES[:7]]
                            + [candidate("Mobile service businesses (mobile detailing, cleaning, pet grooming, "
                                         "handyman and similar)")])
    fake_claude.replies = [{"structured_output": a} for a in (thin, branded, repeated)]
    settings = replace(settings, max_job_attempts=3)
    with pytest.raises(ScoutFailed):
        run_scout(session, settings)
    prompts = [p for _, p in fake_claude.calls]
    assert "at least 10 are required" in prompts[1] and "brand names" in prompts[2]
    assert session.scalar(select(NicheCandidate)) is None


def test_deep_research_uses_the_scout_notes_and_marks_the_candidate(settings, session, fake_claude):
    fake_claude.replies = [{"structured_output": scout_answer()}]
    run_scout(session, settings)
    best = next_candidate(session)
    assert "Scout notes" in scout_notes(session, best.niche)
    fake_claude.replies = [{"structured_output": research_answer(niche=best.niche)}]
    record = run_niche_research(session, settings, best.niche)
    assert "Scout notes" in fake_claude.calls[-1][1] and "at least 6 comparable listings" in fake_claude.calls[-1][1]
    assert (best.status, best.research_id) == (NicheCandidate.RESEARCHED, record.id)
    assert next_candidate(session).id != best.id
    assert best.niche in known_niches(session)


def test_scout_is_due_weekly(settings, session):
    now = utcnow()
    assert scout_due(session, now)
    session.add(Job(product_id=None, department="RESEARCH", job_type="niche_scout", for_status="RESEARCH",
                    status=JobStatus.SUCCEEDED, created_at=now - timedelta(days=2)))
    session.flush()
    assert not scout_due(session, now) and scout_due(session, now + timedelta(days=6))


def test_the_office_sees_the_scout_at_work_while_it_runs(settings, session, engine, fake_claude):
    from app.db import make_session_factory
    from app.office import office_state

    def reply(cmd, prompt):
        with make_session_factory(engine)() as other:  # another connection, like the office server
            hall = next(b for b in office_state(other, settings, disk={})["buildings"] if b["id"] == "research")
            seen.append(len(hall["running"]))
        return {"structured_output": scout_answer()}

    seen = []
    fake_claude.replies = [reply]
    run_scout(session, settings)
    assert seen == [1]
