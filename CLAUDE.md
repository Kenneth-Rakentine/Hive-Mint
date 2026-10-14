# Hive-Mint — permanent rules

Hive-Mint is a local-first, AI-operated company that researches, builds, tests and lists useful digital utility products: spreadsheets, calculators, business form bundles, trackers, PDFs and ZIPs. Stack: Python 3.13, SQLite, plain Python orchestration. **No agent frameworks.**

These rules are permanent. Change them only when the owner explicitly asks.

## Departments are roles, not models

Operations, Research, Finance, Product Design, Production, Testing, QA, Red Team, Compliance, SEO, Analytics, Customer Service.

A department is a role with its own inputs, checks and outputs. It is not a separate model or process. Any provider can perform a role.

Three departments stay separate and must never be merged:

- **Testing** asks "does it function?" It is deterministic code: formulas evaluate, files open, totals reconcile.
- **QA** asks "is it good?" It covers usefulness, clarity, presentation and fitness for the buyer.
- **Red Team** argues *against* publishing. It looks for reasons the product will fail, mislead, disappoint or create risk.

## Product state machine

```
IDEA → RESEARCH → FINANCE_REVIEW → SPECIFICATION → PRODUCTION → TESTING → QA
     → RED_TEAM → COMPLIANCE → SEO → HUMAN_REVIEW → READY_TO_PUBLISH → PUBLISHED
```

Side states: `REWORK`, `REJECTED`, `PAUSED`, `RETIRED`, `ERROR`.

- **No skipped gates.** Every product passes every gate in order. The transition table in `app/states.py`, plus the guards in `app/lifecycle.py`, reject illegal moves.
- **Failures route backward on purpose**, to REWORK or an earlier gate, with a recorded reason. A failure is never silently retried forward.
- `app.lifecycle.transition()` is the only code allowed to change a product's status.

## Deterministic first

- Deterministic checks beat AI judgment whenever possible. Use AI only where code cannot decide.
- **Operations routing is plain Python**: a table from state to department. It is never an LLM call.

## Logging and source of truth

- Log every job and every AI call: provider, model, tier, tokens, cost, result and errors (`Job`, `AgentRun` and `Event` tables).
- The **database is the source of truth**, never chat memory. If it is not recorded in `data/hivemint.db`, it did not happen.
- The `Event` table is an append-only audit log.

## AI tiers

| Tier | Provider | Notes |
| --- | --- | --- |
| 1 | Ollama, local | Free; the default for routine drafting. |
| 2/3 | Human-triggered Claude Code sessions | For now. No automatic cloud calls. |
| later | Metered API | Only behind a provider layer (`providers/`) with hard budget caps. |

## Default limits (`config/settings.py`, overridable in `.env`)

- Cloud spend: **$1/day**, **$20/month**
- **20** ideas/day
- **3** builds/day
- **0** publications/day
- Daily and monthly limits reset at local midnight, America/New_York (`HIVEMINT_TIMEZONE`). Timestamps are stored in UTC.

## Safety

- **A human must approve before anything is published.**
- Compliance ambiguity goes to HUMAN_REVIEW. Never guess.
- Retries are bounded: per job (`MAX_JOB_ATTEMPTS`) and per product rework cycle (`MAX_REWORK_CYCLES`).
- Secrets live only in `.env`, which is gitignored. Never commit keys, customer data or `data/`.

## Storage & cleanup tracking

Whenever you install, download, create, cache or change anything outside the repo (or anything large inside it), update the cleanup report in the same session. Never leave it stale.

- The report is the plain text file `Hive Mint Storage for Cleanup.txt` on the owner's Desktop. Find the real path with `[Environment]::GetFolderPath('Desktop')`, since it may be redirected to OneDrive.
- Check the machine before you edit the report; don't rely on memory. Record each item's exact path, current size, how it got there and the exact removal steps, then update "Last updated" and the size totals.
- The report is documentation only. Never run its removal commands.

## Architecture

- Storefronts (Etsy, Gumroad, etc.) are **output adapters**, not the core. The core is products, gates and the database.
- The pixel GUI comes **last** and only visualizes database state. It never drives logic or shows simulated activity.

## Working in this repo

- Work in **small, reviewable commits**. Each commit leaves the tests passing.
- Layout:
  - `app/`: database, models, state machine, API, CLI
  - `agents/`: Operations Manager and departments
  - `providers/`: AI provider layer
  - `tools/`: deterministic helpers
  - `config/`: settings
  - `tests/`
  - `research/`: research notes
  - Runtime output, not committed: `data/`, `products/`, `logs/`, `archive/`
- Use the existing `.venv` (Windows PowerShell):
  - `.venv\Scripts\python -m pytest`: run the tests
  - `.venv\Scripts\python -m app.cli demo`: run the end-to-end stub flow
- The old Node graphics workbench lives on the `legacy-workbench` branch. Do not revive it on v0.1.
