# Hive-Mint

Hive-Mint is a local-first, AI-operated company that researches, builds, tests and lists useful digital utility products: spreadsheets, calculators, business form bundles, trackers, PDFs and ZIPs. Products move through a strict gated pipeline run by departments (roles, not separate models). A plain-Python Operations Manager does the routing, and every step is recorded in SQLite.

The permanent operating rules are in [CLAUDE.md](CLAUDE.md).

**Status: milestone v0.2.** Production and Testing are real for the sample product, "Mobile Auto Detailer Income & Expense Tracker". The other departments are still stubs.

## Run on Windows 11 (PowerShell)

Requires [Python 3.13](https://www.python.org/downloads/windows/) and [Git](https://git-scm.com/downloads/win).

```powershell
git clone https://github.com/Kenneth-Rakentine/Hive-Mint.git
cd Hive-Mint
git switch v0.2

py -3.13 -m venv .venv            # skip if .venv already exists
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt
Copy-Item .env.example .env       # optional; edit limits there
```

Then:

```powershell
python -m pytest                       # run the tests
python -m app.cli demo                 # build + test the sample product, IDEA -> ... -> HUMAN_REVIEW
python -m app.cli status               # products by state, queued jobs, limits
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

With the server running, open http://127.0.0.1:8000/health and http://127.0.0.1:8000/status. Press Ctrl+C to stop it.

The database lives at `data\hivemint.db` and is never committed. Delete it to start over.

The demo writes the product to `products\<product id>\v1\`, including `Mobile_Detailer_Business_Tracker.zip`. It takes about a minute, mostly spent recalculating every spreadsheet scenario.

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
  - all limits reset at local midnight, America/New_York (`HIVEMINT_TIMEZONE`)
- A read-only API (`/health`, `/status`) and a CLI.

## What v0.2 adds

- **Product specs as data**, in `config/specs/<slug>.json`. A spec lists the files, sheets, required columns, data validation, allowed spreadsheet functions and test scenarios (inputs and expected answers). Production builds to it and Testing checks against it.
- **Production** (`agents/production.py`, `tools/builders/`) writes four Excel workbooks (openpyxl), an instructions PDF (reportlab) and a README, then zips them. The workbooks use live formulas that work in Excel and Google Sheets. Builds are versioned (`v1`, `v2`, ...), staged before they are renamed into place, and recorded in the `builds` table with a SHA-256 for each file.
- **Testing** (`agents/testing.py`) is deterministic and has no AI. It checks:
  - every expected file exists, and the folder holds nothing else
  - the ZIP opens and contains exactly the spec'd files, which match the build folder
  - each workbook opens, with the required sheets, columns, frozen headers and validation
  - formulas only use functions available in both Excel and Google Sheets
  - recalculated values contain no `#REF!`, `#DIV/0!`, `#NAME?`, `#VALUE!` or `#N/A`, on the empty template and in every scenario
  - scenario answers are correct (e.g. $1,000 revenue − $250 expenses = $750 profit; 100 miles × the IRS rate for the trip's date)
  - the IRS rate table matches the spec
  - the PDF opens, has 3–10 pages and contains the required headings
  - the README contains its required phrases

  Formulas are recalculated with LibreOffice headless if it's installed, otherwise with the `formulas` Python package (`HIVEMINT_RECALC_ENGINE`). Every check is saved in the `test_results` table, linked to the product, job, agent run and build. A failure sends the product to REWORK with the reasons; a pass moves it to QA.

## What it does not do yet

No Ollama or other AI calls, no builders beyond the sample product, no web research, no marketplace or storefront connections, and no GUI. Nothing can be published: the publication limit is 0 and release needs human approval.

## Layout

| Folder | Contents |
| --- | --- |
| `app/` | Database, models, state machine, lifecycle, budget, API, CLI |
| `agents/` | Operations Manager and departments (Production and Testing are real; the rest are stubs) |
| `providers/` | Future AI provider layer, with budget caps |
| `tools/` | Deterministic helpers: spec loader, product builders, xlsx styling, packaging, formula recalculation |
| `config/` | Settings and product specs (`config/specs/`) |
| `tests/` | pytest suite |
| `research/` | Research notes |
| `data/`, `products/`, `logs/`, `archive/` | Local runtime output (gitignored) |

`docs/legacy/` keeps the earlier Etsy market scan. The previous Node graphics workbench is preserved on the [`legacy-workbench`](https://github.com/Kenneth-Rakentine/Hive-Mint/tree/legacy-workbench) branch.
