# Hive Mint

A local-first Windows 11 workbench for researching, designing, checking, and preparing original graphics for products sold through Etsy. Its low-resolution hive map shows actual jobs and events, not simulated activity.

**Status:** project brief and market research. No agent runtime, Etsy connection, or live storefront is implemented yet.

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

A small local service owns a SQLite job/event database and stores artwork in an application data directory. Bounded workers are invoked at stage transitions; they do not continuously converse. Start with a local web UI; package it for Windows only after the real workflow and screen have been tested. An automation runner or model adapter can be selected when the interfaces and costs are clearer. Keep provider keys outside the repo and require explicit per-tool permissions.

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
