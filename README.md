# Hive-Mint

Hive-Mint is a local-first, AI-operated company that researches, builds, tests and lists useful digital utility products: spreadsheets, calculators, business form bundles, trackers, PDFs and ZIPs. Products move through a strict gated pipeline run by departments (roles, not separate models). A plain-Python Operations Manager does the routing, and every step is recorded in SQLite.

The permanent operating rules are in [CLAUDE.md](CLAUDE.md).

**Status: milestone v0.1.** This release contains the database, jobs, the state machine and the Operations Manager. Departments are stubs.

## Run on Windows 11 (PowerShell)

Requires [Python 3.13](https://www.python.org/downloads/windows/) and [Git](https://git-scm.com/downloads/win).

```powershell
git clone https://github.com/Kenneth-Rakentine/Hive-Mint.git
cd Hive-Mint
git switch v0.1

py -3.13 -m venv .venv            # skip if .venv already exists
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt
Copy-Item .env.example .env       # optional; edit limits there
```

Then:

```powershell
python -m pytest                       # run the tests
python -m app.cli demo                 # sample product: IDEA -> ... -> HUMAN_REVIEW
python -m app.cli status               # products by state, queued jobs, limits
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

With the server running, open http://127.0.0.1:8000/health and http://127.0.0.1:8000/status. Press Ctrl+C to stop it.

The database lives at `data\hivemint.db` and is never committed. Delete it to start over.

## What v0.1 does

- SQLite models for `Product`, `Job`, `AgentRun` (provider, model, tier, tokens, cost, result, errors) and an append-only `Event` audit log.
- The product state machine: `IDEA → RESEARCH → FINANCE_REVIEW → SPECIFICATION → PRODUCTION → TESTING → QA → RED_TEAM → COMPLIANCE → SEO → HUMAN_REVIEW → READY_TO_PUBLISH → PUBLISHED`, plus `REWORK`, `REJECTED`, `PAUSED`, `RETIRED` and `ERROR`.
  - Illegal moves are rejected.
  - Release requires the SEO gate and a recorded human approval.
- A deterministic Operations Manager. It maps each state to a department, queues one job per gate, retries a bounded number of times, and enforces the daily build limit.
- Configurable limits in `.env`:
  - cloud spend $1/day and $20/month
  - 20 ideas/day
  - 3 builds/day
  - 0 publications/day
- A read-only API (`/health`, `/status`) and a CLI.

## What it does not do yet

No Ollama or other AI calls, no product generators, no web research, no marketplace or storefront connections, and no GUI. Nothing can be published: the publication limit is 0 and release needs human approval.

## Layout

| Folder | Contents |
| --- | --- |
| `app/` | Database, models, state machine, lifecycle, budget, API, CLI |
| `agents/` | Operations Manager and departments (stubs) |
| `providers/` | Future AI provider layer, with budget caps |
| `tools/` | Future deterministic helpers |
| `config/` | Settings |
| `tests/` | pytest suite |
| `research/` | Research notes |
| `data/`, `products/`, `logs/`, `archive/` | Local runtime output (gitignored) |

`docs/legacy/` keeps the earlier Etsy market scan. The previous Node graphics workbench is preserved on the [`legacy-workbench`](https://github.com/Kenneth-Rakentine/Hive-Mint/tree/legacy-workbench) branch.
