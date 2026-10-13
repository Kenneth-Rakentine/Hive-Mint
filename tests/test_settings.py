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
