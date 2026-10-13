"""AI provider layer (future).

Tier 1 = local Ollama, Tier 2/3 = human-triggered Claude Code sessions, later a metered
API. Every provider must log an AgentRun and pass app.budget.assert_within_budget before
any paid call. Nothing is implemented in v0.1.
"""
