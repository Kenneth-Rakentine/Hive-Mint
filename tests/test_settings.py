import pytest

from config.settings import Settings, usd_to_micros


def test_defaults_match_operating_rules():
    s = Settings()
    assert s.cloud_daily_budget_micros == 1_000_000
    assert s.cloud_monthly_budget_micros == 20_000_000
    assert (s.max_ideas_per_day, s.max_builds_per_day, s.max_publications_per_day) == (20, 3, 0)
    assert s.db_url.endswith("data/hivemint.db")


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CLOUD_DAILY_BUDGET_USD", "0.50")
    monkeypatch.setenv("MAX_BUILDS_PER_DAY", "1")
    s = Settings.from_env()
    assert s.cloud_daily_budget_micros == usd_to_micros("0.50") == 500_000
    assert s.max_builds_per_day == 1


def test_autopilot_defaults():
    s = Settings()
    assert (s.max_claude_runs_per_day, s.max_products_built_per_day) == (15, 2)
    assert (s.shift_start, s.shift_end, s.skip_on_battery) == ("13:00", "22:00", True)
    assert abs(sum(w for _, w in s.opportunity_weights) - 1) < 1e-9
    assert s.opportunity_threshold == 65


def test_autopilot_env_overrides(monkeypatch):
    monkeypatch.setenv("MAX_CLAUDE_RUNS_PER_DAY", "4")
    monkeypatch.setenv("SHIFT_END", "21:30")
    monkeypatch.setenv("SKIP_ON_BATTERY", "false")
    monkeypatch.setenv("OPPORTUNITY_WEIGHTS", "demand=0.4,competition=0.1,margin=0.1,ease=0.1,differentiation=0.1,"
                                              "support_burden=0.1,automation_suitability=0.1")
    s = Settings.from_env()
    assert (s.max_claude_runs_per_day, s.shift_end, s.skip_on_battery) == (4, "21:30", False)
    assert dict(s.opportunity_weights)["demand"] == 0.4


@pytest.mark.parametrize("kwargs", [
    {"shift_start": "1pm"},
    {"shift_end": "24:00"},
    {"opportunity_weights": (("demand", 1.0),)},
    {"opportunity_weights": (("demand", 0.5), ("competition", 0.15), ("margin", 0.15), ("ease", 0.10),
                             ("differentiation", 0.15), ("support_burden", 0.10), ("automation_suitability", 0.10))},
    {"opportunity_threshold": 120},
])
def test_invalid_autopilot_settings_fail_fast(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
