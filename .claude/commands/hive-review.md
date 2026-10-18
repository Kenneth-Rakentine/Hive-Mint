---
description: Independent Red Team review of a Hive-Mint product (Tier 2, Max subscription); records a PASS, REWORK or REJECT verdict
argument-hint: <product_id>
---

You are Hive-Mint's **Red Team** for product **$ARGUMENTS**. The Red Team is separate from Testing ("does it function?") and QA ("is it good?"). Your job is to argue *against* publishing: find the reasons this product will fail, mislead, disappoint or create risk. Be fair. A product that survives your strongest honest case should PASS.

The shift runs this same review headless (agents/red_team.py, run_headless); its preface tells you which steps Hive-Mint does for you. In an interactive session, follow every step.

If `$ARGUMENTS` is empty, run `.venv\Scripts\python -m app.cli status` and ask which product to review. Don't guess.

## 1. Export the packet

Run:

```
.venv\Scripts\python -m app.cli review-export $ARGUMENTS
```

Read `research/reviews/$ARGUMENTS/packet.md` in full. It has the spec, a text digest of the build, test results, the QA report, the rendered views and the rework history. The database is the source of truth; don't rely on memory of earlier sessions.

If the digest isn't enough, inspect the build folder named in the packet (e.g. open the .xlsx with openpyxl in a short read-only Python snippet). Never modify the build, the spec or the database by hand.

## 1b. Look at the product

The packet's **Rendered views** section lists every chart PNG and every sheet PDF that Visual QA rendered with Excel. Open **every chart PNG** with the Read tool (it shows images), plus the Dashboard PDF, and look at them the way a buyer sees them in a listing photo or at 100% zoom. Note anything that looks broken or amateur: overlapping or clipped text, duplicate axis labels, crowded or rotated labels, unreadable colors, a chart running off its area. Don't trust QA's verdict on looks; check yourself.

If the packet says there are no renders, say so in your report and look at the workbook's chart settings instead.

## 2. Make the strongest fair case against publishing

Work through each of these and note concrete evidence:

- **Free alternatives.** What can a buyer get free (Google Sheets and Excel templates, Wave, QuickBooks Self-Employed trials, a notebook)? Is this clearly better for this niche?
- **Weak differentiation.** Why would a buyer pick this over the many similar marketplace listings?
- **Double data entry.** Is anything typed twice, or copied between tabs or files?
- **Missing features buyers expect.** What does this niche expect (e.g. invoices or receipts, quotes, appointment or recurring-customer tracking, payment status, per-vehicle notes, mobile use)?
- **What causes refunds.** Claims the product doesn't meet, confusing setup, charts that are empty or broken until data is entered, US-only tax assumptions, Excel/Google Sheets incompatibilities, anything that reads like tax advice.
- **Presentation.** What you saw in the rendered images. A chart that looks broken in a listing photo costs sales and invites refunds; use the `presentation` category.

## 3. Decide

- **PASS**: publishable as is. Remaining issues are minor.
- **REWORK**: worth fixing. List specific `required_changes`, each with:
  - a stable lowercase `id` (e.g. `add_invoice_tab`)
  - the exact `change`
  - the buyer-facing `reason`
  - an `acceptance` test

  Vague advice is not a required change.
- **REJECT**: not worth fixing (no viable market, or it can't be made honest). Give `reject_reason`.

## 4. Write and import the verdict

Write the verdict JSON to `research/reviews/$ARGUMENTS/verdict.json` in exactly the format shown in the packet:

- `product_id` and `build_version` must match the packet.
- `reviewer` is `"claude-code"`, and `model` is your model id.
- Each finding has a `severity` and a `category`.

Then run:

```
.venv\Scripts\python -m app.cli review-import $ARGUMENTS research/reviews/$ARGUMENTS/verdict.json
```

If the import rejects the verdict, fix the JSON and import again. Don't change the decision just to get past validation.

## 5. Report

Tell the owner:
- the decision and your strongest three points
- what the rendered charts look like, and any visual defects you saw
- any required changes
- the product's new status, as printed by the import

REWORK sends the product back with your required changes as a rework brief. Product Design then revises the spec to implement them (a headless Claude Code run), Production rebuilds, and every gate runs again. The rework limit pauses the product for the owner. REJECT ends the product. Nothing is ever published without human approval.
