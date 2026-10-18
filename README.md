# Hive-Mint

Hive-Mint is a local-first, AI-operated company that researches, builds, tests and lists useful digital utility products: spreadsheets, calculators, business form bundles, trackers, PDFs and ZIPs. Products move through a strict gated pipeline run by departments (roles, not separate models). A plain-Python Operations Manager does the routing, and every step is recorded in SQLite.

The permanent operating rules are in [CLAUDE.md](CLAUDE.md).

**Status: milestone v0.2.** Production and Testing are real for the sample product, "Mobile Auto Detailer Income & Expense Tracker". The other departments are still stubs.

## Run on Windows 11 (PowerShell)

Requires [Python 3.13](https://www.python.org/downloads/windows/) and [Git](https://git-scm.com/downloads/win).

```powershell
git clone https://github.com/Kenneth-Rakentine/Hive-Mint.git
cd Hive-Mint
git switch main

py -3.13 -m venv .venv            # skip if .venv already exists
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt
Copy-Item .env.example .env       # optional; edit limits there
```

Then:

```powershell
python -m pytest                       # run the tests (Ollama is always mocked)
python -m app.cli demo                 # build, test and QA the sample product; stops at RED_TEAM
python -m app.cli run                  # run Operations until no job is left to run
python -m app.cli status               # products by state, queued/paused jobs, disk usage, limits
python -m app.cli review-export 1      # Red Team packet for product 1 (or /hive-review 1 in Claude Code)
python -m app.cli review-import 1 research\reviews\1\verdict.json
python -m app.cli rework 1 brief.json  # send a product back with a structured rework brief
python -m app.cli resume 1             # return a PAUSED or ERROR product to its gate
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

With the server running, open http://127.0.0.1:8000/health and http://127.0.0.1:8000/status. Press Ctrl+C to stop it.

The database lives at `data\hivemint.db` and is never committed. Delete it to start over.

The demo writes the product to `products\<product id>\v1\`, including `Mobile_Detailer_Business_Tracker.zip`. It takes a few minutes, mostly spent recalculating every spreadsheet scenario. QA calls the local model, so start Ollama first (`ollama pull qwen3.5:4b` once). If Ollama is off, QA retries a bounded number of times, then the product goes to ERROR with `OLLAMA_UNAVAILABLE`; start Ollama and run `resume`.

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

## What v0.3 adds

- **Ollama provider** (`providers/ollama.py`): Tier 1, local, $0. Default model `qwen3.5:4b` (`OLLAMA_MODEL`). Every call is an `AgentRun` with model, token counts, duration and $0 cost. If Ollama is down, the job retries `MAX_JOB_ATTEMPTS` times and then stops in ERROR with a clear reason. It never crashes or loops.
- **QA** (`agents/qa.py`): "is it good?"
  - Deterministic checks first: spelling (dictionary plus `config/qa_words.txt`), consistent sheet and column naming, every feature the spec promises present in the build, and the README and PDF naming every file.
  - Then the local model reviews a text digest of the product (`tools/digest.py`, never raw files) for clarity and confusing instructions.
  - The report is a score from 0 to 100 with findings by severity and a PASS/REWORK decision, stored in the `reviews` table.
  - The model's findings are capped (`QA_MODEL_MAX_DEDUCTION`), so a small model can't fail a product on its own.
- **Visual QA** (part of QA): nothing used to look at the rendered file, so v2 shipped with chart titles over the plot, $0 $0 $1 $1 axes and overlapping charts.
  - `tools/render.py` renders every workbook the way a buyer sees it: each sheet to PDF and each chart to PNG under `products/<id>/v<n>/renders/`. It uses Excel via COM (pywin32) and quits it in a `finally` block, falls back to LibreOffice headless, or skips with a clear warning (`HIVEMINT_RENDER_ENGINE`).
  - `tools/chart_checks.py` reads the chart XML with no AI and fails title or legend overlay, missing axis titles the spec requires, month labels over 3 characters or without a fixed 0 rotation, a bottom legend, value axes without their own number format, major unit or minimum, and charts that overlap or leave the spec's print area or screen width (measured at 96 and 120 DPI). `tests/fixtures/v2_overlapping_charts.xlsx` is the regression case.
  - The local qwen3.5:4b then looks at every chart PNG. It reports what it sees (title position, every axis label, rotation, cut-off text) and code decides what is a defect, such as a repeated axis label. Vision findings are capped (`QA_VISION_MAX_DEDUCTION`); with the text cap they may not exceed 100 - `QA_PASS_SCORE`.
  - The Red Team packet lists the renders, and `/hive-review` opens every chart image.
- **Red Team via Claude Code** (Tier 2/3, $0, human-triggered): run `/hive-review <id>` in Claude Code (`.claude/commands/hive-review.md`).
  - It exports a packet to `research/reviews/<id>/packet.md` and argues the strongest fair case against publishing: free alternatives, weak differentiation, double entry, missing features, refund risks.
  - It then imports a validated verdict. PASS goes on to COMPLIANCE. REWORK needs specific required changes. REJECT ends the product.
  - Operations queues the Red Team job but never runs it itself.
- **Rework loop**: QA, Red Team and human REWORK decisions become a structured rework brief (`rework_briefs` table), and each required change has a stable id.
  - Product Design sends the product back to Production with the brief attached, but only once the spec lists every id under `"resolves"`.
  - If the spec doesn't cover them yet, the product pauses with the missing ids instead of rebuilding the same thing.
  - `MAX_REWORK_CYCLES` still applies.
- **Detailer tracker v2** (spec v2, from the first rework brief in `config/rework/`):
  - one workbook with tabs, where each job is entered once and feeds income and customer history, and supply purchases count as costs automatically
  - a Dashboard with three charts that work in Excel and Google Sheets
  - profit after the mileage deduction, and a user-set tax set-aside %
  - Testing now also checks the charts.
- **Detailer tracker v3** (spec v3, rework brief 002): chart titles and legend clear of the plot, Jan-Dec labels with no rotation, fixed $1,000 axis steps (so an empty workbook shows $0 and $1,000), loss months shown below zero, and 500x300 px charts that fit Excel at 100% zoom on a 1920 px screen at 125% scaling.
- **Disk guard**: Production jobs pause with `DISK_LIMIT` when free space drops below `MIN_FREE_DISK_GB` (50) or the Hive-Mint folder grows past `MAX_HIVEMINT_STORAGE_GB` (20). They resume automatically once space is back. `status` and `GET /status` show free space, the Hive-Mint folder size and the Ollama model folder size.

## What it does not do yet

No metered cloud AI, no builders beyond the sample product, no web research, no listing preview images, no marketplace or storefront connections, and no GUI. Research, Finance, Compliance and SEO are still stubs. Nothing can be published: the publication limit is 0 and release needs human approval.

## Layout

| Folder | Contents |
| --- | --- |
| `app/` | Database, models, state machine, lifecycle, budget, API, CLI |
| `agents/` | Operations Manager and departments (Production, Testing, QA, Red Team and rework routing are real; the rest are stubs) |
| `providers/` | AI provider layer: local Ollama (Tier 1); metered APIs later, behind budget caps |
| `tools/` | Deterministic helpers: spec loader, product builders, xlsx styling, packaging, formula recalculation, rendering, chart checks |
| `config/` | Settings, product specs (`config/specs/`), rework briefs (`config/rework/`), QA word list |
| `tests/` | pytest suite |
| `research/` | Research notes; `research/reviews/` holds Red Team packets and verdicts (gitignored) |
| `data/`, `products/`, `logs/`, `archive/` | Local runtime output (gitignored) |

`docs/legacy/` keeps the earlier Etsy market scan. The previous Node graphics workbench is preserved on the [`legacy-workbench`](https://github.com/Kenneth-Rakentine/Hive-Mint/tree/legacy-workbench) branch.
