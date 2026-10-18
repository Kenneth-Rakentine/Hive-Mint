"""Builder for "Mobile Auto Detailer Income & Expense Tracker" (spec v3).

Writes one workbook with tabs, an instructions PDF and a README into an output folder.
Spec v2 resolved the first rework brief: no double entry (each job is entered once and
feeds income and customer history; supply purchases count as costs automatically), a
Dashboard with charts, profit after the mileage deduction and a user-set tax set-aside %.
Spec v3 fixes how the charts look: titles above the plot, short unrotated month labels, a
legend clear of the axis, fixed value-axis scales that stay clean on an empty workbook, and
a layout that fits a 1920 px screen at 100% zoom.
Every layout detail the Testing and QA departments check (sheets, headers, charts and their
placement, scenarios) is in the spec; this module reads the chart settings from it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import CharacterProperties, ParagraphProperties, RichTextProperties
from openpyxl.drawing.text import Paragraph as TextParagraph
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter, quote_sheetname
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tools.chart_xml import px_to_cm
from tools.spec import parse_date
from tools.xlsx import (
    BRAND, CURRENCY, DATE, INTEGER, NUMBER, PERCENT, RATE, SUBTITLE_FONT, TITLE_FONT, WRAP, Column, add_validation,
    header_row, label, value_cell, write_how_to, write_list_sheet, write_table, year_criteria,
)

WORKBOOK = "Mobile_Detailer_Business_Tracker.xlsx"
TITLE = "Mobile Auto Detailer Business Tracker"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]  # chart labels
NOT_ADVICE = ("This product is a record-keeping tool and is not tax advice. Tax rules change and depend on your "
              "situation; confirm deductions with a qualified tax professional or irs.gov.")
SET_ASIDE_NOTE = ("The tax set-aside is a savings target, not a tax calculation. Many self-employed people put "
                  "aside 25-30% of profit; ask a tax professional what fits you.")
LIST_LENGTH = 30
RATE_ROWS = 20
CHART_COLORS = ("1F4E5A", "E07A2E", "8FB8C4")
CHART_GRID_COLUMNS = ("D", "S")  # Dashboard columns under the charts, all CHART_GRID_WIDTH wide
CHART_GRID_WIDTH = 10  # 70 px per column at 96 DPI, 72 logical px at 120 DPI

# Dashboard cells other sheets refer to.
TAX_YEAR = "Dashboard!$B$5"
SET_ASIDE_PCT = "Dashboard!$B$6"
# Summary layout: monthly table header on row 6, months on rows 7-18, totals on row 19.
FIRST_MONTH_ROW, TOTAL_ROW = 7, 19


def build(spec: dict, out_dir: Path) -> list[Path]:
    """Write every deliverable except the ZIP. Returns the paths written."""
    params = spec["parameters"]
    out_dir.mkdir(parents=True, exist_ok=True)
    writers = {WORKBOOK: tracker_workbook, "Instructions.pdf": instructions_pdf, "README.txt": readme}
    written = []
    for file_spec in spec["files"]:
        path = out_dir / file_spec["name"]
        if file_spec["name"] == WORKBOOK:
            dashboard = next(s for s in file_spec["sheets"] if s["name"] == "Dashboard")
            tracker_workbook(params, path, dashboard)
        else:
            writers[file_spec["name"]](params, path)
        written.append(path)
    return written


# --------------------------------------------------------------------------- the workbook

def tracker_workbook(params: dict, path: Path, dashboard_spec: dict) -> None:
    rows, customer_rows = params["data_rows"], params["customer_rows"]
    wb = Workbook()
    _how_to(wb, params)
    dashboard = wb.create_sheet("Dashboard")
    jobs_ws, expenses_ws, supplies_ws, mileage_ws, customers_ws, summary_ws, rates_ws = (
        wb.create_sheet(name) for name in
        ("Jobs", "Expenses", "Supplies", "Mileage Log", "Customers", "Summary", "IRS Rates"))
    lists = write_list_sheet(wb, {"Services": params["services"], "Payment Methods": params["payment_methods"],
                                  "Expense Categories": params["expense_categories"],
                                  "Supply Categories": params["supply_categories"]}, LIST_LENGTH)

    jobs = write_table(jobs_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Customer Name", 24, validation=f"list:Customers!$A$2:$A${customer_rows + 1}"),
        Column("Vehicle", 20),
        Column("Service", 22, validation="list:" + lists["Services"]),
        Column("Payment Method", 16, validation="list:" + lists["Payment Methods"]),
        Column("Service Amount", 15, CURRENCY, validation="money"),
        Column("Tip", 11, CURRENCY, validation="money"),
        Column("Total", 13, CURRENCY,
               formula='IF(COUNT({col[Service Amount]}{r}:{col[Tip]}{r})=0,"",'
                       'SUM({col[Service Amount]}{r}:{col[Tip]}{r}))'),
        Column("Paid?", 8, validation="yesno"),
        Column("Notes", 30),
    ], rows)
    expenses = write_table(expenses_ws, [
        Column("Date", 12, DATE, validation="date"),
        Column("Vendor", 22),
        Column("Category", 28, validation="list:" + lists["Expense Categories"]),
        Column("Description", 30),
        Column("Payment Method", 16, validation="list:" + lists["Payment Methods"]),
        Column("Amount", 13, CURRENCY, validation="money"),
        Column("Notes", 30),
    ], rows)
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

    rate_table = write_table(rates_ws, [
        Column("Effective From", 16, DATE, validation="date"),
        Column("Rate per Mile", 15, RATE, validation="number"),
        Column("Source", 40),
    ], RATE_ROWS)
    for offset, rate in enumerate(params["irs_mileage_rates"]):
        rates_ws.cell(row=2 + offset, column=1, value=parse_date(rate["effective_from"]))
        rates_ws.cell(row=2 + offset, column=2, value=rate["rate"])
        rates_ws.cell(row=2 + offset, column=3, value=rate["source"])
    rate_dates = rate_table.rng("Effective From")
    lookup = f"{quote_sheetname('IRS Rates')}!$A$2:$B${RATE_ROWS + 1}"
    mileage = write_table(mileage_ws, [
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

    names, totals, paid, dates = (jobs.rng(h) for h in ("Customer Name", "Total", "Paid?", "Date"))

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
        Column("Total Spent", 13, CURRENCY, formula=rollup(f"SUMIF({names},{{name}},{totals})")),
        Column("Unpaid Balance", 14, CURRENCY, formula=rollup(f'SUMIFS({totals},{names},{{name}},{paid},"No")')),
        Column("Last Visit", 12, DATE, formula=rollup(
            f'IF(COUNTIF({names},{{name}})=0,"",SUMPRODUCT(MAX(({names}={{name}})*{dates})))')),
        Column("Notes", 30),
    ], customer_rows)

    _summary(summary_ws, params, lists, jobs, expenses, supplies, mileage)
    _dashboard(dashboard, params, dashboard_spec, jobs, mileage)
    wb.active = 0  # open on "How to Use"
    wb.save(path)


def _how_to(wb: Workbook, params: dict) -> None:
    rates = "; ".join(f"{_rate(r['rate'])}/mile from {_us_date(r['effective_from'])}"
                      for r in params["irs_mileage_rates"])
    write_how_to(
        wb, TITLE,
        "One workbook for a mobile detailing business. Enter each job, expense, supply purchase and trip once; the "
        "Dashboard, Summary and Customers tabs fill in from those entries.",
        [
            "Dashboard: type the tax year and your tax set-aside % in the yellow cells (and your odometer readings "
            "on January 1 and December 31 if you want your business-use %). Charts and key figures update as you "
            "add entries.",
            "Jobs: add one row per job with the date, customer, vehicle, service, payment method, amount and any "
            "tip, and mark Paid? Yes or No. This one row counts as income and updates that customer's history.",
            "Customers: add each customer once, before their first job, so their name appears in the Jobs dropdown. "
            "Jobs, Total Spent, Unpaid Balance and Last Visit fill in automatically.",
            "Expenses: add business bills and purchases other than detailing supplies, with a category.",
            "Supplies: add chemicals, towels, pads and equipment here (not on Expenses). They count as costs "
            "automatically, and Cost per Job shows what each supply costs you per job.",
            "Mileage Log: add one row per business trip. IRS Rate and Deduction fill in from the IRS Rates tab.",
            "Summary: month-by-month income, costs, profit and mileage for the chosen tax year.",
            "Lists: edit the dropdown choices (services, payment methods, expense and supply categories).",
        ],
        [
            f"Each data tab has room for {params['data_rows']} entries and Customers for {params['customer_rows']}. "
            "Only entries dated in the Dashboard's tax year count toward the totals.",
            "Profit After Mileage subtracts the IRS standard mileage deduction from Net Profit. If you use the "
            "standard mileage rate, don't also enter fuel or vehicle repairs as expenses for that vehicle.",
            SET_ASIDE_NOTE,
            f"IRS rates included: {rates}. Check irs.gov for the current rate and add new ones on the IRS Rates tab.",
            "Customer details are personal information. Store this file securely and don't share it.",
            NOT_ADVICE,
        ],
    )


def _summary(ws, params: dict, lists: dict, jobs, expenses, supplies, mileage) -> None:
    ws["A1"] = "Monthly Summary"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Totals for the tax year set on the Dashboard. Supplies count as costs."
    label(ws, "A4", "Tax Year")
    value_cell(ws, "B4", f"={TAX_YEAR}", INTEGER)
    header_row(ws, 6, 1, ["Month", "Income", "Expenses", "Supplies", "Net Profit", "Mileage Deduction",
                          "Profit After Mileage"])
    sources = [(jobs, "Total"), (expenses, "Amount"), (supplies, "Total Cost")]
    for index, month in enumerate(MONTHS, start=1):
        row = FIRST_MONTH_ROW + index - 1

        def in_month(table) -> str:
            dates = table.rng("Date")
            return f'{dates},">="&DATE({TAX_YEAR},{index},1),{dates},"<"&DATE({TAX_YEAR},{index + 1},1)'

        label(ws, f"A{row}", month)
        for col, (table, amount) in zip("BCD", sources):
            value_cell(ws, f"{col}{row}", f"=SUMIFS({table.rng(amount)},{in_month(table)})", CURRENCY)
        value_cell(ws, f"E{row}", f"=B{row}-C{row}-D{row}", CURRENCY)
        value_cell(ws, f"F{row}", f"=SUMIFS({mileage.rng('Deduction')},{in_month(mileage)})", CURRENCY)
        value_cell(ws, f"G{row}", f"=E{row}-F{row}", CURRENCY)
    label(ws, f"A{TOTAL_ROW}", "Total")
    for col in "BCDEFG":
        value_cell(ws, f"{col}{TOTAL_ROW}", f"=SUM({col}{FIRST_MONTH_ROW}:{col}{TOTAL_ROW - 1})", CURRENCY)
        ws[f"{col}{TOTAL_ROW}"].font = Font(bold=True)

    # Costs by category: supplies first (one line), then each expense category from Lists.
    header_row(ws, 6, 9, ["Costs by Category", "Amount"])
    label(ws, "I7", "Supplies")
    value_cell(ws, "J7", f"=D{TOTAL_ROW}", CURRENCY)
    _category_rollup(ws, 8, "I", lists["Expense Categories"], expenses.rng("Amount"), expenses.rng("Category"),
                     expenses.rng("Date"))
    header_row(ws, 6, 12, ["Supplies by Category", "Amount"])
    _category_rollup(ws, 7, "L", lists["Supply Categories"], supplies.rng("Total Cost"), supplies.rng("Category"),
                     supplies.rng("Date"))
    for col, width in zip("ABCDEFGHIJKLM", (14, 13, 13, 13, 13, 13, 15, 3, 30, 14, 3, 24, 14)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "B7"
    ws.sheet_view.showGridLines = False


def _category_rollup(ws, first_row: int, first_col: str, list_range: str, amount: str, key: str, dates: str) -> None:
    """One row per Lists entry with that category's total for the tax year."""
    list_col = list_range.split("!")[1].split(":")[0].replace("$", "").rstrip("0123456789")
    amount_col = chr(ord(first_col) + 1)
    for offset in range(LIST_LENGTH):
        row = first_row + offset
        source = f"Lists!${list_col}{offset + 2}"
        value_cell(ws, f"{first_col}{row}", f'=IF({source}="","",{source})')
        value_cell(ws, f"{amount_col}{row}",
                   f'=IF({first_col}{row}="","",SUMIFS({amount},{key},{first_col}{row},'
                   f'{year_criteria(dates, TAX_YEAR)}))', CURRENCY)


def _dashboard(ws, params: dict, sheet_spec: dict, jobs, mileage) -> None:
    ws["A1"] = "Detailing Business Dashboard"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Type in the yellow cells. Everything else updates from your entries."
    ws["A4"] = "Settings"
    ws["A4"].font = SUBTITLE_FONT
    for row, (text, value, fmt) in enumerate([
        ("Tax Year", params["tax_year"], INTEGER),
        ("Tax Set-Aside %", params["tax_set_aside_pct"], PERCENT),
        ("Odometer on January 1", None, "#,##0"),
        ("Odometer on December 31", None, "#,##0"),
    ], start=5):
        label(ws, f"A{row}", text)
        value_cell(ws, f"B{row}", value, fmt, entry=True)
    add_validation(ws, "number", "B5")
    add_validation(ws, "percent", "B6")
    add_validation(ws, "number", "B7:B8")

    total = lambda col: f"Summary!${col}${TOTAL_ROW}"  # noqa: E731
    in_year = year_criteria(jobs.rng("Date"), TAX_YEAR)
    trips_in_year = year_criteria(mileage.rng("Date"), TAX_YEAR)
    header_row(ws, 10, 1, ["Key Figures", ""])
    figures = [
        ("Total Income", f"={total('B')}", CURRENCY),
        ("Total Costs", f"={total('C')}+{total('D')}", CURRENCY),
        ("Net Profit", f"={total('E')}", CURRENCY),
        ("Profit Margin", "=IF(B11=0,0,B13/B11)", PERCENT),
        ("Mileage Deduction", f"={total('F')}", CURRENCY),
        ("Profit After Mileage", f"={total('G')}", CURRENCY),
        ("Tax Set-Aside", "=ROUND(MAX(0,B16)*B6,2)", CURRENCY),
        ("Take-Home After Set-Aside", "=B16-B17", CURRENCY),
        ("Jobs Completed", f"=COUNTIFS({in_year})", INTEGER),
        ("Unpaid Balance", f'=SUMIF({jobs.rng("Paid?")},"No",{jobs.rng("Total")})', CURRENCY),
        ("Business Miles", f"=SUMIFS({mileage.rng('Business Miles')},{trips_in_year})", NUMBER),
        ("Total Miles Driven", '=IF(AND(ISNUMBER(B7),ISNUMBER(B8)),B8-B7,"")', "#,##0"),
        ("Business Use %", '=IF(ISNUMBER(B22),IF(B22>0,B21/B22,""),"")', PERCENT),
    ]
    for row, (text, formula, fmt) in enumerate(figures, start=11):
        label(ws, f"A{row}", text)
        value_cell(ws, f"B{row}", formula, fmt)
    for row in (13, 16, 18):
        ws[f"B{row}"].font = Font(bold=True, color=BRAND)
    ws["A25"] = "Unpaid Balance counts every unpaid job, whatever the year. " + SET_ASIDE_NOTE
    ws["A25"].alignment = WRAP
    ws.merge_cells("A25:B28")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 3
    first, last = (ord(c) - 64 for c in CHART_GRID_COLUMNS)
    for index in range(first, last + 1):
        ws.column_dimensions[get_column_letter(index)].width = CHART_GRID_WIDTH
    ws.sheet_view.showGridLines = False

    # Print and screen layout: everything inside the print area, one landscape page.
    ws.print_area = sheet_spec["chart_layout"]["print_area"]
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    summary = ws.parent["Summary"]
    months = Reference(summary, min_col=1, min_row=FIRST_MONTH_ROW, max_row=TOTAL_ROW - 1)
    category_rows = 1 + len(params["expense_categories"])  # supplies line + each expense category
    categories = Reference(summary, min_col=9, min_row=7, max_row=6 + category_rows)
    sources = {
        "Income vs Costs by Month": ([2, 3, 4], months, FIRST_MONTH_ROW, TOTAL_ROW - 1),
        "Profit After Mileage by Month": ([7], months, FIRST_MONTH_ROW, TOTAL_ROW - 1),
        "Costs by Category": ([10], categories, 7, 6 + category_rows),
    }
    for chart_spec in sheet_spec["charts"]:
        columns, cats, first_row, last_row = sources[chart_spec["title"]]
        ws.add_chart(_bar_chart(chart_spec, summary, columns, cats, first_row, last_row), chart_spec["anchor"])


def _text_properties(size: int, *, bold: bool = False, rotation: int | None = None) -> RichText:
    """Font size (hundredths of a point), weight and an explicit label angle (0 = horizontal, never auto)."""
    body = RichTextProperties(rot=rotation, vert="horz") if rotation is not None else RichTextProperties()
    font = CharacterProperties(sz=size, b=bold)
    return RichText(bodyPr=body, p=[TextParagraph(pPr=ParagraphProperties(defRPr=font), endParaRPr=font)])


def _bar_chart(chart_spec: dict, sheet, columns: list[int], categories: Reference, first_row: int,
               last_row: int) -> BarChart:
    """A plain clustered bar/column chart: the chart types Excel and Google Sheets both import cleanly.

    The title sits above the plot (overlay off), category labels are horizontal, the legend
    (if any) goes where the spec puts it, and the value axis has a fixed minimum, major unit
    and number format, so an empty workbook shows clean labels ($0, $1,000) instead of
    $0 $0 $1 $1. The fixed step suits a one-person business; the PDF tells buyers with much
    bigger months how to raise it.
    """
    horizontal = chart_spec.get("horizontal", False)
    value_axis = chart_spec["value_axis"]
    chart = BarChart()
    chart.type = "bar" if horizontal else "col"
    chart.grouping = "clustered"
    chart.style = 10
    chart.title = chart_spec["title"]
    chart.title.overlay = False
    chart.width, chart.height = px_to_cm(chart_spec["width_px"]), px_to_cm(chart_spec["height_px"])
    for col in columns:
        chart.add_data(Reference(sheet, min_col=col, min_row=first_row - 1, max_row=last_row), titles_from_data=True)
    chart.set_categories(categories)
    for series, color in zip(chart.series, CHART_COLORS):
        series.graphicalProperties.solidFill = color
        series.graphicalProperties.line.solidFill = color
        series.invertIfNegative = False  # a loss month keeps its colour instead of turning white

    category, value = chart.x_axis, chart.y_axis
    category.delete = value.delete = False
    category.axPos, value.axPos = ("l", "b") if horizontal else ("b", "l")
    category.txPr = _text_properties(900, rotation=0)
    value.txPr = _text_properties(900, rotation=0)
    category.tickLblPos = "low"  # labels stay at the edge even when a value is negative
    if horizontal:
        category.scaling.orientation = "maxMin"  # first category at the top, as on the Summary tab
        value.crosses = "max"  # ...which keeps the value axis at the bottom
    if "min" in value_axis:
        value.scaling.min = value_axis["min"]
    value.majorUnit = value_axis["major_unit"]
    value.numFmt = value_axis["num_fmt"]
    if chart_spec.get("legend"):
        chart.legend.position = chart_spec["legend"]
        chart.legend.overlay = False  # Excel overlays a legend whose overlay element is missing
    else:
        chart.legend = None
    return chart


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

    def table(rows: list[list[str]], widths: tuple[float, float]) -> None:
        t = Table([[Paragraph(escape(c), body) for c in row] for row in rows],
                  colWidths=[w * inch for w in widths])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3ECEE")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B7C4C8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.extend([t, Spacer(1, 8)])

    story.append(Paragraph(TITLE, title))
    para("Instructions for the Mobile Detailer Business Tracker: one workbook for jobs, customers, expenses, "
         "supplies and mileage, with a Dashboard that shows how the business is doing. Built for one-person and "
         "small mobile detailing businesses that want clean records without accounting software.")

    heading("What's Included")
    table([
        ["File", "What it is"],
        [WORKBOOK, "The tracker workbook: Dashboard, Jobs, Expenses, Supplies, Mileage Log, Customers, Summary, "
                   "IRS Rates and Lists tabs"],
        ["Instructions.pdf", "This guide"],
        ["README.txt", "A short quick-start note"],
    ], (3.2, 3.5))
    para("The workbook's tabs:")
    table([
        ["Tab", "What you do there"],
        ["How to Use", "A one-page reminder of these steps"],
        ["Dashboard", "Set the tax year and tax set-aside %; see key figures and three charts"],
        ["Jobs", "Enter every job once: it counts as income and updates the customer's history"],
        ["Expenses", "Business bills and purchases other than detailing supplies"],
        ["Supplies", "Chemicals, towels, pads and equipment, with cost per job"],
        ["Mileage Log", "Business trips and the IRS standard mileage deduction"],
        ["Customers", "Contact details; visits, spend, unpaid balance and last visit fill in"],
        ["Summary", "Month-by-month income, costs, profit and mileage"],
        ["IRS Rates", "Standard mileage rates by date; add new ones here"],
        ["Lists", "The choices in every dropdown"],
    ], (1.5, 5.2))

    heading("Getting Started")
    bullets([
        "Unzip the download and save the files to a folder you back up, such as OneDrive, Google Drive or "
        "Dropbox.",
        f"Open {WORKBOOK} in Microsoft Excel (2010 or newer, Windows or Mac). It opens on the <b>How to Use</b> "
        "tab.",
        "Go to the <b>Dashboard</b> and type your tax year and tax set-aside % in the yellow cells.",
        "Type only in white table cells and yellow cells. Grey cells hold formulas and calculate on their own.",
        "Enter dates as month/day/year, for example 3/15/2026, and amounts without a $ sign.",
    ], numbered=True)
    heading("Using Google Sheets")
    bullets([
        "In Google Drive choose <b>New &gt; File upload</b> and upload the .xlsx file.",
        "Open it, then choose <b>File &gt; Save as Google Sheets</b> so edits save automatically.",
        "Formulas, dropdowns, frozen headers and the Dashboard charts carry over. Google Sheets may draw the "
        "charts with its own fonts and colors.",
    ])

    heading("Dashboard")
    para("The Dashboard is where you set up the year and check results. <b>Tax Year</b> decides which entries "
         "count. <b>Tax Set-Aside %</b> is the share of profit you want to put aside for taxes. Add your odometer "
         "readings on January 1 and December 31 to see total miles and your business-use percentage.")
    para("Key figures: total income, total costs (expenses plus supplies), net profit and margin, the mileage "
         "deduction, <b>Profit After Mileage</b> (net profit minus the mileage deduction), the <b>Tax "
         "Set-Aside</b> amount, take-home after the set-aside, jobs completed and unpaid balances. The three "
         "charts show income against costs by month, profit after mileage by month, and costs by category.")
    para("Nothing is set aside when profit after mileage is zero or negative. " + escape(SET_ASIDE_NOTE))
    para("The chart axes count up in steps of $1,000. If your months run well above $15,000 and the axis labels "
         "look crowded, right-click the numbers on the axis, choose <b>Format Axis</b> and raise the "
         "<b>Major</b> unit (for example to 5000).")

    heading("Jobs and Customers")
    para("Add each customer once on the <b>Customers</b> tab. Then record every job on the <b>Jobs</b> tab, "
         "picking the customer from the dropdown so names match exactly. Enter the service amount and any tip; "
         "Total adds them. Mark <b>Paid?</b> Yes or No. That single row is your income record and also updates "
         "the customer's job count, total spent, unpaid balance and last visit, so there is no second log to "
         "keep. Sort Customers by Last Visit to find people due for a maintenance detail.")

    heading("Expenses and Supplies")
    para("Use <b>Supplies</b> for detailing chemicals, towels, pads and equipment: quantity, unit and cost per "
         "unit, plus <b>Jobs per Unit</b> (how many jobs one bottle, gallon or pack lasts). Cost per Job shows "
         "what that supply costs you each time. Supplies count as costs automatically, so don't enter them on "
         "Expenses as well.")
    para("Use <b>Expenses</b> for everything else: insurance, marketing, phone and software, licenses, water "
         "and so on. Pick a category so the Summary and Dashboard can total costs by category.")

    heading("Mileage Log")
    rate_lines = [f"{_rate(r['rate'])} per mile from {_us_date(r['effective_from'])} ({escape(r['source'])})"
                  for r in params["irs_mileage_rates"]]
    para("Log every business trip: to a customer, between customers, to buy supplies or to the bank. Enter the "
         "date, purpose, where you went and the business miles. The log looks up the IRS standard mileage rate "
         "in effect on that date and calculates the deduction. Rates included on the <b>IRS Rates</b> tab:")
    bullets(rate_lines)
    para("When the IRS announces a new rate, add a row to the IRS Rates tab with the date it takes effect. Keep "
         "the rows in date order, oldest first. A trip showing \"No rate\" is dated before the first rate listed.")

    heading("Monthly Summary")
    para("The <b>Summary</b> tab shows income, expenses, supplies, net profit, mileage deduction and profit after "
         "mileage for each month of the Dashboard's tax year, with year totals. Beside it are costs by category "
         "and supplies by category. The Dashboard charts read from this tab.")

    heading("Tax Time Tips")
    bullets([
        "Set the tax year on the Dashboard and give the totals to your tax preparer.",
        "Keep receipts for expenses and supplies. A tracker supports your records but doesn't replace them.",
        "Choose either the standard mileage rate or actual vehicle expenses for a vehicle, not both. If you use "
        "the mileage rate, don't also enter fuel and repairs for that vehicle as expenses.",
        "Back up the workbook regularly, and keep a copy of each finished year.",
    ])

    heading("Important Notes")
    para(escape(NOT_ADVICE))
    para("IRS mileage rates are included as published when this version was built. Check irs.gov for the current "
         "rate before filing.")
    para("Customer contact details are personal information. Keep the workbook private and don't share it.")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.HexColor("#5A6B70"))
        canvas.drawString(doc.leftMargin, 0.5 * inch, TITLE)
        canvas.drawRightString(LETTER[0] - doc.rightMargin, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.9 * inch, title=f"{TITLE} - Instructions",
                            author="Hive-Mint", invariant=1)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def readme(params: dict, path: Path) -> None:
    text = f"""MOBILE AUTO DETAILER BUSINESS TRACKER
=====================================

Thank you for your purchase. This download contains:

  {WORKBOOK}  The tracker: Dashboard with charts, Jobs, Expenses,
                                         Supplies, Mileage Log, Customers, Summary
  Instructions.pdf                       Step-by-step guide (start here)
  README.txt                             This note

QUICK START
1. Save these files somewhere you back up.
2. Open the workbook in Excel 2010 or newer, or upload it to Google Drive and open it
   with Google Sheets.
3. On the Dashboard, type your tax year and tax set-aside % in the yellow cells.
4. Enter each job, expense, supply purchase and trip once. Type in white table cells
   and yellow cells; grey cells calculate automatically.

Each tab holds {params['data_rows']} entries (Customers holds {params['customer_rows']}). Mileage rates are on the
"IRS Rates" tab; add new rates there when the IRS announces them.

This product is a record-keeping tool and is not tax advice. Confirm deductions with a
qualified tax professional or irs.gov.
"""
    path.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
