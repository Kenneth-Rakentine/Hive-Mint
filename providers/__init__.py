"""AI provider layer.

Tier 1 = local Ollama (providers/ollama.py, $0). Tier 2/3 = human-triggered Claude Code
sessions, recorded through `python -m app.cli review-import` (also $0). A metered API comes
later and must call app.budget.assert_within_budget() before every paid request. Every
provider call is logged as an AgentRun.
"""
