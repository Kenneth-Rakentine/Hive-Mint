"""Builder for "Mobile Auto Detailer Income & Expense Tracker".

Writes four workbooks, an instructions PDF and a README into an output folder. Every
layout detail the Testing department checks (sheets, headers, scenarios) is in the spec;
this module must match it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import quote_sheetname
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tools.spec import parse_date
from tools.xlsx import (
    CURRENCY, DATE, INTEGER, NUMBER, PERCENT, RATE, SUBTITLE_FONT, TITLE_FONT, Column, header_row, label,
    value_cell, write_how_to, write_list_sheet, write_table, year_criteria,
)

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
NOT_ADVICE = ("This product is a record-keeping tool and is not tax advice. Tax rules change and depend on your "
              "situation; confirm deductions with a qualified tax professional or irs.gov.")
LIST_LENGTH = 30


def build(spec: dict, out_dir: Path) -> list[Path]:
    """Write every deliverable except the ZIP. Returns the paths written."""
    params = spec["parameters"]
    out_dir.mkdir(parents=True, exist_ok=True)
    writers = {
        "Income_Expense_Tracker.xlsx": income_expense_tracker,
        "Mileage_Log.xlsx": mileage_log,
        "Supply_Cost_Tracker.xlsx": supply_cost_tracker,
        "Customer_Log.xlsx": customer_log,
        "Instructions.pdf": instructions_pdf,
        "README.txt": readme,
    }
    written = []
    for file_spec in spec["files"]:
        path = out_dir / file_spec["name"]
        writers[file_spec["name"]](params, path)
        written.append(path)
    return written


def _save(wb: Workbook, path: Path) -> None:
    wb.active = 0  # open on "How to use"
    wb.save(path)


def _summary_sheet(wb: Workbook, title: str, subtitle: str, tax_year: int):
    ws = wb.create_sheet("Summary")
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = subtitle
    label(ws, "A4", "Tax Year")
    value_cell(ws, "B4", tax_year, INTEGER, entry=True)
    ws.sheet_view.showGridLines = False
    return ws


def _category_rollup(ws, top_row: int, first_col: str, heading: str, list_range: str, amount: str, key: str,
                     dates: str) -> None:
    """Heading row, then one row per Lists entry with the tax-year total for that category."""
    header_row(ws, top_row, ord(first_col) - 64, [heading, "Amount"])
    list_col = list_range.split("!")[1].split(":")[0].replace("$", "").rstrip("0123456789")
    amount_col = chr(ord(first_col) + 1)
    for offset in range(LIST_LENGTH):
        row = top_row + 1 + offset
        source = f"Lists!${list_col}{offset + 2}"
        value_cell(ws, f"{first_col}{row}", f'=IF({source}="","",{source})')
        value_cell(ws, f"{amount_col}{row}",
                   f'=IF({first_col}{row}="","",SUMIFS({amount},{key},{first_col}{row},'
                   f'{year_criteria(dates, "$B$4")}))', CURRENCY)


# --------------------------------------------------------------------------- workbooks

def income_expense_tracker(params: dict, path: Path) -> None:
    rows = params["data_rows"]
    wb = Workbook()
    write_how_to(
        wb, "Income & Expense Tracker",
        "Log every paid job and every business expense. The Summary sheet totals income, expenses and profit "
        "by month for the tax year you choose.",
        [
            "Income sheet: add one row per job: date, customer, vehicle, service, payment method, the amount "
            "charged and any tip. Total adds them up.",
            "Expenses sheet: add one row per purchase or bill with its date, category and amount.",
            "Summary sheet: type the tax year in the yellow cell. Monthly income, expenses, profit and "
            "expenses by category update automatically.",
            "Lists sheet: change the dropdown choices (services, payment methods, expense categories) to "
            "match your business.",
        ],
        [
            f"Each sheet has room for {rows} entries. Only rows dated in the chosen tax year count on the Summary.",
            "Use the filter arrows in the header row to find entries by customer, category or month.",
            "If you use the IRS standard mileage rate, don't also claim fuel or vehicle repairs for that vehicle. "
            "Track miles in the Mileage Log instead.",
            NOT_ADVICE,
        ],
    )
    income_ws = wb.create_sheet("Income")
    expense_ws = wb.create_sheet("Expenses")
    lists = write_list_sheet(wb, {"Services": params["services"], "Payment Methods": params["payment_methods"],
                                  "Expense Categories": params["expense_categories"]}, LIST_LENGTH)

    income = write_table(income_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Customer", 22),
        Column("Vehicle", 20),
        Column("Service", 22, validation="list:" + lists["Services"]),
        Column("Payment Method", 16, validation="list:" + lists["Payment Methods"]),
        Column("Service Amount", 15, CURRENCY, validation="money"),
        Column("Tip", 11, CURRENCY, validation="money"),
        Column("Total", 13, CURRENCY,
               formula='IF(COUNT({col[Service Amount]}{r}:{col[Tip]}{r})=0,"",'
                       'SUM({col[Service Amount]}{r}:{col[Tip]}{r}))'),
        Column("Notes", 30),
    ], rows)
    expenses = write_table(expense_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Vendor", 22),
        Column("Category", 28, validation="list:" + lists["Expense Categories"]),
        Column("Description", 30),
        Column("Payment Method", 16, validation="list:" + lists["Payment Methods"]),
        Column("Amount", 13, CURRENCY, validation="money"),
        Column("Notes", 30),
    ], rows)

    ws = _summary_sheet(wb, "Business Summary", "Totals for the tax year below. Change the year to see another "
                                                "year.", params["tax_year"])
    header_row(ws, 6, 1, ["Month", "Income", "Expenses", "Profit"])
    inc_dates, exp_dates = income.rng("Date"), expenses.rng("Date")
    for index, month in enumerate(MONTHS, start=1):
        row = 6 + index
        label(ws, f"A{row}", month)

        def in_month(dates: str) -> str:
            return f'{dates},">="&DATE($B$4,{index},1),{dates},"<"&DATE($B$4,{index + 1},1)'

        value_cell(ws, f"B{row}", f"=SUMIFS({income.rng('Total')},{in_month(inc_dates)})", CURRENCY)
        value_cell(ws, f"C{row}", f"=SUMIFS({expenses.rng('Amount')},{in_month(exp_dates)})", CURRENCY)
        value_cell(ws, f"D{row}", f"=B{row}-C{row}", CURRENCY)
    label(ws, "A19", "Total")
    for col in "BCD":
        value_cell(ws, f"{col}19", f"=SUM({col}7:{col}18)", CURRENCY)
        ws[f"{col}19"].font = Font(bold=True)

    header_row(ws, 6, 6, ["Key Figures", ""])
    for row, (text, formula, fmt) in enumerate([
        ("Total Income", "=B19", CURRENCY),
        ("Total Expenses", "=C19", CURRENCY),
        ("Net Profit", "=D19", CURRENCY),
        ("Profit Margin", "=IF(B19=0,0,D19/B19)", PERCENT),
    ], start=7):
        label(ws, f"F{row}", text)
        value_cell(ws, f"G{row}", formula, fmt)
    _category_rollup(ws, 12, "F", "Expenses by Category", lists["Expense Categories"], expenses.rng("Amount"),
                     expenses.rng("Category"), exp_dates)
    for col, width in zip("ABCDEFG", (14, 14, 14, 14, 3, 32, 16)):
        ws.column_dimensions[col].width = width
    wb.move_sheet("Summary", offset=-1)  # before Lists
    _save(wb, path)


def mileage_log(params: dict, path: Path) -> None:
    rows = params["data_rows"]
    rates = params["irs_mileage_rates"]
    rate_rows = 20
    wb = Workbook()
    rate_summary = "; ".join(f"{_rate(r['rate'])}/mile from {_us_date(r['effective_from'])}" for r in rates)
    write_how_to(
        wb, "Mileage Log",
        "Record every business trip: driving to customers, supply runs, the bank. The log looks up the IRS "
        "standard mileage rate for each trip's date and calculates your deduction.",
        [
            "Mileage Log sheet: add one row per trip with the date, the business purpose, where you went and the "
            "business miles. IRS Rate and Deduction fill in automatically.",
            "Summary sheet: type the tax year, plus your odometer readings on January 1 and December 31. You'll "
            "get business miles, the mileage deduction and your business-use percentage.",
            "IRS Rates sheet: the IRS sometimes changes the rate during the year. Add new rates there in date "
            "order, oldest first, and every trip on or after that date uses the new rate.",
        ],
        [
            f"Rates included: {rate_summary}. Check irs.gov for the current rate.",
            "Commuting from home to a regular workplace is not business mileage. Trips between customers are.",
            "Keep the log as you go. The IRS expects records made at or near the time of the trip.",
            "If you use the standard mileage rate, don't also claim fuel or repairs for that vehicle.",
            NOT_ADVICE,
        ],
    )
    log_ws = wb.create_sheet("Mileage Log")
    rate_ws = wb.create_sheet("IRS Rates")
    rate_table = write_table(rate_ws, [
        Column("Effective From", 16, DATE, validation="date"),
        Column("Rate per Mile", 15, RATE, validation="number"),
        Column("Source", 40),
    ], rate_rows)
    for offset, rate in enumerate(rates):
        rate_ws.cell(row=2 + offset, column=1, value=parse_date(rate["effective_from"]))
        rate_ws.cell(row=2 + offset, column=2, value=rate["rate"])
        rate_ws.cell(row=2 + offset, column=3, value=rate["source"])
    rate_dates = rate_table.rng("Effective From")
    lookup = f"{quote_sheetname('IRS Rates')}!$A$2:$B${rate_rows + 1}"

    log = write_table(log_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Business Purpose", 28),
        Column("Customer / Job", 22),
        Column("From", 22),
        Column("To", 22),
        Column("Business Miles", 12, NUMBER, validation="number"),
        Column("IRS Rate", 11, RATE,
               formula=lambda r, c: f'IF(ISNUMBER({c["Date"]}{r}),IF({c["Date"]}{r}<MIN({rate_dates}),"No rate",'
                                    f'VLOOKUP({c["Date"]}{r},{lookup},2,TRUE)),"")'),
        Column("Deduction", 13, CURRENCY,
               formula='IF(AND(ISNUMBER({col[Business Miles]}{r}),ISNUMBER({col[IRS Rate]}{r})),'
                       'ROUND({col[Business Miles]}{r}*{col[IRS Rate]}{r},2),"")'),
        Column("Notes", 30),
    ], rows)

    ws = _summary_sheet(wb, "Mileage Summary", "Business miles and the standard mileage deduction for the tax "
                                               "year below.", params["tax_year"])
    wb.move_sheet("Summary", offset=-1)  # before IRS Rates
    in_year = year_criteria(log.rng("Date"), "$B$4")
    for row, (text, formula, fmt) in enumerate([
        ("Business Miles", f"=SUMIFS({log.rng('Business Miles')},{in_year})", NUMBER),
        ("Mileage Deduction", f"=SUMIFS({log.rng('Deduction')},{in_year})", CURRENCY),
        ("Trips Logged", f"=COUNTIFS({in_year})", INTEGER),
    ], start=6):
        label(ws, f"A{row}", text)
        value_cell(ws, f"B{row}", formula, fmt)
    ws["A10"] = "Vehicle use (asked on Schedule C Part IV or Form 4562)"
    ws["A10"].font = SUBTITLE_FONT
    label(ws, "A11", "Odometer on January 1")
    value_cell(ws, "B11", None, "#,##0", entry=True)
    label(ws, "A12", "Odometer on December 31")
    value_cell(ws, "B12", None, "#,##0", entry=True)
    label(ws, "A13", "Total Miles Driven")
    value_cell(ws, "B13", '=IF(AND(ISNUMBER(B11),ISNUMBER(B12)),B12-B11,"")', "#,##0")
    label(ws, "A14", "Business Use %")
    value_cell(ws, "B14", '=IF(ISNUMBER(B13),IF(B13>0,B6/B13,""),"")', PERCENT)
    ws["A16"] = 'A trip showing "No rate" is dated before the first rate on the IRS Rates sheet. Add that rate there.'
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 16
    _save(wb, path)


def supply_cost_tracker(params: dict, path: Path) -> None:
    rows = params["data_rows"]
    wb = Workbook()
    write_how_to(
        wb, "Supply Cost Tracker",
        "Track what you spend on chemicals, towels, pads and equipment, and see what each supply costs you per "
        "job so you can price your services with confidence.",
        [
            "Supplies sheet: add one row per purchase with quantity, unit (gallon, bottle, pack) and cost per unit. "
            "Total Cost is calculated.",
            "Jobs per Unit: estimate how many jobs one unit lasts (e.g. a gallon of shampoo covers 40 washes). "
            "Cost per Job shows what that supply costs you on each job.",
            "Summary sheet: type the tax year to see total supply spend and spend by category.",
            "Lists sheet: edit the supply categories to match how you buy.",
        ],
        [
            "Add up Cost per Job for the supplies a service uses to see its true material cost.",
            "Copy your yearly supply total into the Expenses sheet of the Income & Expense Tracker, or log each "
            "purchase there too, but don't count it twice.",
            NOT_ADVICE,
        ],
    )
    supplies_ws = wb.create_sheet("Supplies")
    lists = write_list_sheet(wb, {"Supply Categories": params["supply_categories"]}, LIST_LENGTH)
    supplies = write_table(supplies_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Supplier", 20),
        Column("Product", 28),
        Column("Category", 20, validation="list:" + lists["Supply Categories"]),
        Column("Quantity", 10, validation="number"),
        Column("Unit", 10),
        Column("Cost per Unit", 13, CURRENCY, validation="money"),
        Column("Total Cost", 13, CURRENCY,
               formula='IF(COUNT({col[Quantity]}{r},{col[Cost per Unit]}{r})<2,"",'
                       'ROUND({col[Quantity]}{r}*{col[Cost per Unit]}{r},2))'),
        Column("Jobs per Unit", 12, validation="number"),
        Column("Cost per Job", 13, CURRENCY,
               formula='IF(AND(ISNUMBER({col[Cost per Unit]}{r}),ISNUMBER({col[Jobs per Unit]}{r})),'
                       'IF({col[Jobs per Unit]}{r}>0,ROUND({col[Cost per Unit]}{r}/{col[Jobs per Unit]}{r},2),""),"")'),
        Column("Notes", 30),
    ], rows)
    ws = _summary_sheet(wb, "Supply Summary", "Supply spending for the tax year below.", params["tax_year"])
    wb.move_sheet("Summary", offset=-1)  # before Lists
    in_year = year_criteria(supplies.rng("Date"), "$B$4")
    label(ws, "A6", "Total Supply Spend")
    value_cell(ws, "B6", f"=SUMIFS({supplies.rng('Total Cost')},{in_year})", CURRENCY)
    label(ws, "A7", "Purchases Logged")
    value_cell(ws, "B7", f"=COUNTIFS({in_year})", INTEGER)
    _category_rollup(ws, 9, "A", "Spend by Category", lists["Supply Categories"], supplies.rng("Total Cost"),
                     supplies.rng("Category"), supplies.rng("Date"))
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 16
    _save(wb, path)


def customer_log(params: dict, path: Path) -> None:
    rows = params["data_rows"]
    wb = Workbook()
    write_how_to(
        wb, "Customer Log",
        "Keep every customer's contact details, vehicles and job history in one place, and see at a glance who "
        "owes you money and who is due for a return visit.",
        [
            "Customers sheet: add each customer once, with contact details and vehicles. Jobs, Total Spent, "
            "Unpaid Balance and Last Visit fill in from the job history.",
            "Job History sheet: add one row per job. Pick the customer from the dropdown (it lists the names on "
            "the Customers sheet) and mark whether it's paid.",
            "Sort or filter the Customers sheet by Last Visit to find customers due for a maintenance wash.",
            "Lists sheet: edit services and payment methods to match your business.",
        ],
        [
            "Customer names must match exactly between the two sheets. Use the dropdown to avoid typos.",
            "Customer details are personal information. Store this file securely and don't share it.",
        ],
    )
    customers_ws = wb.create_sheet("Customers")
    history_ws = wb.create_sheet("Job History")
    lists = write_list_sheet(wb, {"Services": params["services"], "Payment Methods": params["payment_methods"]},
                             LIST_LENGTH)
    history = write_table(history_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Customer Name", 24, validation=f"list:Customers!$A$2:$A${rows + 1}"),
        Column("Vehicle", 20),
        Column("Service", 22, validation="list:" + lists["Services"]),
        Column("Price", 12, CURRENCY, validation="money"),
        Column("Paid?", 8, validation="yesno"),
        Column("Payment Method", 16, validation="list:" + lists["Payment Methods"]),
        Column("Notes", 30),
    ], rows)
    names, prices, paid, dates = (history.rng(h) for h in ("Customer Name", "Price", "Paid?", "Date"))

    def rollup(body: str):
        return lambda r, c: f'IF({c["Customer Name"]}{r}="","",{body.format(name=c["Customer Name"] + str(r))})'

    write_table(customers_ws, [
        Column("Customer Name", 24),
        Column("Phone", 15),
        Column("Email", 26),
        Column("Service Address", 30),
        Column("Vehicle(s)", 24),
        Column("Preferred Service", 20, validation="list:" + lists["Services"]),
        Column("Jobs", 8, INTEGER, formula=rollup(f"COUNTIF({names},{{name}})")),
        Column("Total Spent", 13, CURRENCY, formula=rollup(f"SUMIF({names},{{name}},{prices})")),
        Column("Unpaid Balance", 14, CURRENCY, formula=rollup(f'SUMIFS({prices},{names},{{name}},{paid},"No")')),
        Column("Last Visit", 12, DATE, formula=rollup(
            f'IF(COUNTIF({names},{{name}})=0,"",SUMPRODUCT(MAX(({names}={{name}})*{dates})))')),
        Column("Notes", 30),
    ], rows)
    _save(wb, path)


# --------------------------------------------------------------------------- PDF and README

def _rate(value: float) -> str:
    """$0.725 stays $0.725; $0.700 becomes $0.70."""
    text = f"${value:,.3f}"
    return text[:-1] if text.endswith("0") else text


def _us_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def instructions_pdf(params: dict, path: Path) -> None:
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10.5, leading=14.5, spaceAfter=6)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=17, textColor=colors.HexColor("#1F4E5A"),
                        spaceBefore=14, spaceAfter=8)
    title = ParagraphStyle("title", parent=styles["Title"], textColor=colors.HexColor("#1F4E5A"))
    story: list = []

    def heading(text: str) -> None:
        story.append(Paragraph(escape(text), h1))

    def para(text: str) -> None:
        story.append(Paragraph(text, body))

    def bullets(items: list[str], numbered: bool = False) -> None:
        story.append(ListFlowable([ListItem(Paragraph(i, body), leftIndent=14) for i in items],
                                  bulletType="1" if numbered else "bullet", start="1" if numbered else None,
                                  leftIndent=16, bulletFontSize=9))
        story.append(Spacer(1, 4))

    story.append(Paragraph("Mobile Auto Detailer Business Tracker", title))
    para("Instructions for the Income &amp; Expense Tracker, Mileage Log, Supply Cost Tracker and Customer Log. "
         "Built for one-person and small mobile detailing businesses that want clean records without "
         "accounting software.")

    heading("What's Included")
    table = Table([
        ["File", "What it does"],
        ["Income_Expense_Tracker.xlsx", "Jobs, tips and expenses, with monthly profit and expenses by category"],
        ["Mileage_Log.xlsx", "Business trips with the IRS standard mileage deduction"],
        ["Supply_Cost_Tracker.xlsx", "Supply purchases and your material cost per job"],
        ["Customer_Log.xlsx", "Customer contacts, job history, unpaid balances and last visit"],
        ["Instructions.pdf", "This guide"],
    ], colWidths=[2.3 * inch, 4.4 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E5A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B7C4C8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [table, Spacer(1, 8)]

    heading("Getting Started")
    bullets([
        "Unzip the download and save the files to a folder you back up, such as OneDrive, Google Drive or "
        "Dropbox.",
        "Open a workbook in Microsoft Excel (2010 or newer, Windows or Mac). Each one opens on a short "
        "<b>How to use</b> sheet.",
        "Type only in white table cells and yellow cells. Grey cells hold formulas and calculate on their own.",
        "Dropdown columns (service, category, payment method) read from the <b>Lists</b> sheet. Edit that "
        "sheet to change the choices.",
        "Enter dates as month/day/year, for example 3/15/2026, and amounts without a $ sign.",
    ], numbered=True)
    heading("Using Google Sheets")
    bullets([
        "In Google Drive choose <b>New &gt; File upload</b> and upload the .xlsx file.",
        "Open it, then choose <b>File &gt; Save as Google Sheets</b> so edits save automatically.",
        "All formulas, dropdowns and frozen headers work the same way in Google Sheets.",
        "Prefer to keep Excel format? You can also edit the .xlsx directly in Drive.",
    ])

    heading("Income & Expense Tracker")
    para("<b>Income sheet.</b> Add a row for every job: date, customer, vehicle, service, payment method, the "
         "amount charged and any tip. The Total column adds the service amount and tip.")
    para("<b>Expenses sheet.</b> Add a row for every business purchase or bill and pick a category. Categories "
         "follow the kinds of costs most detailers report: chemicals and supplies, equipment, insurance, "
         "marketing, phone and software, water, licenses and more.")
    para("<b>Summary sheet.</b> Type the tax year in the yellow cell. You'll see income, expenses and profit "
         "for each month, year totals, your profit margin and expenses by category. Only entries dated in that "
         "year are counted, so you can keep several years in one file. A fresh copy for each year is tidier.")

    heading("Mileage Log")
    rates = params["irs_mileage_rates"]
    rate_lines = [f"{_rate(r['rate'])} per mile from {_us_date(r['effective_from'])} ({escape(r['source'])})"
                  for r in rates]
    para("Log every business trip: driving to a customer, between customers, to buy supplies or to the bank. "
         "Enter the date, purpose, where you went and the business miles. The log looks up the IRS standard "
         "mileage rate in effect on that date and calculates the deduction.")
    para("Rates included in the <b>IRS Rates</b> sheet:")
    bullets(rate_lines)
    para("When the IRS announces a new rate, add a row to the IRS Rates sheet with the date it takes effect. "
         "Keep the rows in date order, oldest first. On the Summary sheet, enter your odometer readings for "
         "January 1 and December 31 to get total miles and your business-use percentage.")

    heading("Supply Cost Tracker")
    para("Record each supply purchase with quantity, unit and cost per unit. Then estimate <b>Jobs per Unit</b>: "
         "how many jobs one bottle, gallon or pack lasts. Cost per Job shows what that supply costs you each "
         "time. Add up the supplies a service uses to see its material cost, and price with a margin on top.")

    heading("Customer Log")
    para("List each customer once on the <b>Customers</b> sheet. Record every job on the <b>Job History</b> "
         "sheet, picking the customer from the dropdown so names match exactly. Each customer's job count, total "
         "spent, unpaid balance and last visit update automatically. Sort by Last Visit to find customers due "
         "for a maintenance detail. Filter Unpaid Balance to chase payments.")

    heading("Tax Time Tips")
    bullets([
        "Set the tax year on each Summary sheet and copy the totals into your records or hand them to your tax "
        "preparer.",
        "Keep receipts for expenses. A tracker supports your records but doesn't replace them.",
        "Choose either the standard mileage rate or actual vehicle expenses for a vehicle, not both. If you use "
        "the mileage rate, don't also claim fuel and repairs for that vehicle.",
        "Back up your files regularly, and keep a copy of each finished year.",
    ])

    heading("Important Notes")
    para(escape(NOT_ADVICE))
    para("IRS mileage rates are included as published at the time this version was built. Check irs.gov for "
         "the current rate before filing.")
    para("Customer contact details are personal information. Keep the Customer Log private and don't share it.")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.HexColor("#5A6B70"))
        canvas.drawString(doc.leftMargin, 0.5 * inch, "Mobile Auto Detailer Business Tracker")
        canvas.drawRightString(LETTER[0] - doc.rightMargin, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.9 * inch,
                            title="Mobile Auto Detailer Business Tracker - Instructions", author="Hive-Mint",
                            invariant=1)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def readme(params: dict, path: Path) -> None:
    text = f"""MOBILE AUTO DETAILER BUSINESS TRACKER
=====================================

Thank you for your purchase. This download contains:

  Income_Expense_Tracker.xlsx  Jobs, tips and expenses; monthly profit; expenses by category
  Mileage_Log.xlsx             Business trips and the IRS standard mileage deduction
  Supply_Cost_Tracker.xlsx     Supply purchases and material cost per job
  Customer_Log.xlsx            Customers, job history, unpaid balances, last visit
  Instructions.pdf             Step-by-step guide (start here)

QUICK START
1. Save these files somewhere you back up.
2. Open any workbook in Excel 2010 or newer, or upload it to Google Drive and open it
   with Google Sheets.
3. Read the "How to use" sheet. Type in white table cells and yellow cells; grey cells
   calculate automatically.

Each workbook holds {params['data_rows']} entries per sheet. Mileage rates are on the "IRS Rates"
sheet of Mileage_Log.xlsx; add new rates there when the IRS announces them.

This product is a record-keeping tool and is not tax advice. Confirm deductions with a
qualified tax professional or irs.gov.
"""
    path.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
