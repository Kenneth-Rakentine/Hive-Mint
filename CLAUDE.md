# Hive-Mint — permanent rules

Hive-Mint is a local-first, AI-operated company that researches, builds, tests and lists useful digital utility products: spreadsheets, calculators, business form bundles, trackers, PDFs and ZIPs. Stack: Python 3.13, SQLite, plain Python orchestration. **No agent frameworks.**

These rules are permanent. Change them only when the owner explicitly asks.

## Departments are roles, not models

Operations, Research, Finance, Product Design, Production, Testing, QA, Red Team, Compliance, SEO, Owner's Desk, Analytics, Customer Service.

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
- **The owner's desk** (`agents/owner_desk.py`) handles HUMAN_REVIEW for products that come from SEO: deterministic package and picture checks, then one headless Claude look with a per-picture checklist. It approves to READY_TO_PUBLISH, sends back (REWORK or SEO), or leaves the product for the owner. Compliance referrals always wait for the owner.

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
| 2/3 | Claude Code on the owner's Max subscription | Human-triggered sessions, or headless `claude -p` runs during a shift (`providers/claude_headless.py`). |
| later | Metered API | Only behind a provider layer (`providers/`) with hard budget caps. |

Headless Claude Code runs (owner-approved in v0.4):

- **Billing guard.** Every run is refused unless `ANTHROPIC_API_KEY` and the other pay-per-use variables are unset and `claude auth status` shows a claude.ai login with the Max plan. Nothing may bill per token.
- **Run cap.** Capped by `MAX_CLAUDE_RUNS_PER_DAY`. Their reported cost is logged but not counted against the cloud caps.
- **Tool policy.** Runs stay inside the repo under `providers/headless_policy.py`: `--restricted`, no MCP servers, `dontAsk`, explicit per-role tools. No git, installs, deletes or paths outside the repo.
- **Desktop files.** Headless runs never touch the Desktop report files; plain Python writes them.

## Default limits (`config/settings.py`, overridable in `.env`)

- Cloud spend: **$1/day**, **$20/month**
- **20** ideas/day
- **3** builds/day, of which at most **2** are first builds of new products
- **15** headless Claude Code runs/day
- **0** publications/day
- The shift runs only from **13:00 to 22:00** local time and never on battery.
- Daily and monthly limits reset at local midnight, America/New_York (`HIVEMINT_TIMEZONE`). Timestamps are stored in UTC.

## Safety

- **Nothing is listed without a recorded approval, and a human always does the upload.** Owner decision (2026-10-25): the owner's desk may approve on his behalf, but only when the strict picture checks pass: every picture opened and checked against the fixed checklist, a second look after SEO's picture review, the exact tested ZIP in the package, no blank or off-size pictures. Any ticked box sends the product back. `DESK_AUTO_APPROVE=false` turns desk approvals back into recommendations. The owner can withdraw a desk approval with `reject` or `rework` until he uploads.
- Uploading stays manual (**0** publications/day). A Claude session may fill an Etsy listing as a **draft** in the browser (`.claude/commands/etsy-draft.md`); only the owner presses Publish.
- Compliance ambiguity goes to HUMAN_REVIEW. Never guess.
- Retries are bounded: per job (`MAX_JOB_ATTEMPTS`) and per product rework cycle (`MAX_REWORK_CYCLES`, plus `AUTO_REWORK_GRANTS` = **1** extra cycle granted automatically). Owner decision (2026-10-28): a product still failing then goes back to the drawing board (`REDESIGNS_PER_PRODUCT` = **1** fresh spec from scratch, written from every failure so far, with a fresh rework budget); if the redesign fails too, it is dropped to REJECTED (`AUTO_DROP_FAILED`) so its slot goes to a new idea. Owner-specified products pause for the owner instead of being dropped.
- Secrets live only in `.env`, which is gitignored. Never commit keys, customer data or `data/`.

## Storage & cleanup tracking

Whenever you install, download, create, cache or change anything outside the repo (or anything large inside it), including scheduled tasks, update the cleanup report in the same session. Never leave it stale.

- The report is the plain text file `Hive Mint Storage for Cleanup.txt` on the owner's Desktop. Find the real path with `[Environment]::GetFolderPath('Desktop')`, since it may be redirected to OneDrive.
- Check the machine before you edit the report; don't rely on memory. Record each item's exact path, current size, how it got there and the exact removal steps, then update "Last updated" and the size totals.
- The report is documentation only. Never run its removal commands.

## Architecture

- Storefronts (Etsy, Gumroad, etc.) are **output adapters**, not the core. The core is products, gates and the database.
- The pixel GUI comes **last** and only visualizes database state. It never drives logic or shows simulated activity. Owner-approved ambience: an idle resident keeper at every building, the sea, the stars, trees and rocks, and three bats that fly past now and then; a keeper is shown *working*, and the building's windows glow orange, only while a job there is RUNNING; a keeper carries a crate along the road only when a product's status really changed. The lighthouse is the owner's desk; its lamp burns only when something needs the owner.

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
