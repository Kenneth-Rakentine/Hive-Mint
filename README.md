# Hive Mint

A local-first Windows 11 workbench for researching, designing, checking, and preparing original graphics for products sold through Etsy. Its low-resolution hive map shows actual jobs and events, not simulated activity.

**Status:** first runnable local workbench. It tracks real jobs, artwork, costs, QA, and owner approval, with optional Ollama drafting. It has no Etsy connection, automatic artwork generation, or live storefront yet.

## Run on Windows 11

1. Install [Node.js 22 or later](https://nodejs.org/en/download) and [Git](https://git-scm.com/downloads/win) if they are not installed.
2. Open PowerShell and run:

   ```powershell
   git clone https://github.com/Kenneth-Rakentine/Hive-Mint.git
   cd Hive-Mint
   npm start
   ```

3. Open **http://127.0.0.1:3741** in your browser. Leave the PowerShell window open while using the app; press Ctrl+C to stop it.

No `npm install` or account is required for the first version. The server listens only on this PC. Job records and uploaded files are saved under `%APPDATA%\HiveMint` on Windows. Back up that folder if you want to preserve your work. Never commit it to GitHub.

### Optional local drafting with Ollama

Install [Ollama for Windows](https://ollama.com/download/windows), then in another PowerShell window run `ollama pull qwen3.5:4b`. When Ollama is running, Hive Mint shows the installed models and can draft research **hypotheses**, a design brief, or listing copy. The research worker has no live Etsy browsing, and the design worker writes a brief rather than an image. Every generated draft remains editable. Without Ollama, all fields work manually.

To use a different data directory or Ollama endpoint, set `HIVE_MINT_DATA_DIR` or `OLLAMA_URL` before running. The default Ollama endpoint is `http://127.0.0.1:11434`. Do not point it at an untrusted remote host.

Run `npm test` for the workflow and persistence check.

### What this release does

- Create jobs, attach PNG/JPG/WebP artwork, and move through research, design, QA, costing, and owner approval.
- Show real stage counts on a low-resolution hive map. Bees appear only for real jobs in an active station.
- Estimate amount left per order with editable fees and cost assumptions.
- Keep a timestamped job activity record and local drafts of listings or customer replies.
- Return a changed design to design/QA; replacing artwork resets QA and approval.

It **does not** publish to Etsy, send messages, buy products, monitor sales, search the web, inspect artwork automatically, or generate finished image files. Publishing and customer sending will require separate explicit controls when integrations are built.

## First usable milestone

Take one product idea through this loop:

1. **Research:** record a specific customer/audience, product, search phrases, competitor links, and an evidence note.
2. **Design:** create original artwork candidates and store prompts, source assets, and export settings.
3. **Quality control:** inspect text, transparency, print dimensions, placement, resemblance to known marks, and presentation. Flag uncertain IP issues for a human.
4. **Economics:** calculate expected contribution per order from price, production, shipping subsidy, Etsy fees, ads allowance, refunds allowance, and AI costs. Keep assumptions editable.
5. **Approval:** the owner chooses a candidate and approves the exact artwork and listing copy.
6. **Listing:** prepare an Etsy-ready draft. A separate approval is required before any future integration can publish or spend money.
7. **Support:** draft replies for owner review; never auto-send buyer messages in the first release.

The first version should work with local sample jobs and no shop credentials. Later connectors can add Etsy draft creation and a single print partner. No secret keys or customer data belong in this public repository.

## The workbench

- **Research desk:** ideas, evidence, customer segment, and keyword hypotheses.
- **Design floor:** candidate artwork and production files.
- **Inspection station:** QA findings and fixes.
- **Manager's office:** queue, approvals, budgets, and blockers.
- **Mail desk:** proposed customer responses.
- **Ledger:** unit economics and actual results when connected.

A job card links to its sources, files, timestamps, model/tool costs, QA findings, approval history, and current stage. The pixel hive is a visual projection of this data. Activity and worker movement must come from recorded state changes.

## Architecture direction

A small local Node service owns an atomic JSON job file and stores artwork in an application data directory. It invokes Ollama only when the owner clicks a drafting button. This simple single-user store is suitable for a first local prototype; migrate to SQLite when concurrent agents and external integrations require transactional updates. Package the app for Windows after the workflow and screen have been tested on the owner's PC. Keep provider keys outside the repo and require explicit per-tool permissions.

**Core records:** ideas, jobs, candidate assets, research citations, QA findings, cost estimates, approvals, listing drafts, customer drafts, events. Every external action is traceable to a job and an approval.

**State path:** idea → researched → designing → inspection → costed → awaiting approval → listing draft → ready to publish → published. Failed checks return the job to the relevant stage with a recorded reason.

## Business starting point

Test a coherent series of **original fictional emblems** for a particular audience, beginning with a small number of shirt or poster designs. Retro audio gear and imaginary instrument brands are one promising creative direction, subject to research. The market review in [docs/market-research.md](docs/market-research.md) distinguishes observed shop data from product-level demand.

## Release gates

- No listing publication, ad spend, product order, refund, or buyer message without human review.
- Record seller-directed AI use and production partner information for listing disclosure.
- Verify print output and order a physical sample before scaling a product.
- Track realized costs and conversion; revise or retire designs based on evidence.

## References

[StarNet](https://github.com/androoAGI/starnet) inspired the live map principle; Hive Mint will use its own hive-themed interface and business workflow. See [market research](docs/market-research.md) for Etsy examples, sources, and uncertainties.
