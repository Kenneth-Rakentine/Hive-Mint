"""The general spreadsheet builder (spec builder "spreadsheet"): builds any workbook + PDF + text
product entirely from its spec. No product knowledge lives here.

Workbook sheets, in spec order, each with a "kind":
- how_to: title, intro, steps[], notes[] (a short first sheet for the buyer).
- table: a data-entry table. columns[] of {name, width, format, formula, validation}; rows; optional
  prefill (rows typed in, or {"param", "fields"} copying a parameter list). Header in row 1, formulas
  pre-filled for every data row, frozen header and a filter.
- layout: free-form cells. items[] of
    {"type": "text", "cell", "text", "style": title|subtitle|label|wrap|plain}
    {"type": "value", "cell", "label"?, "label_cell"?, "value" | "formula", "format", "input", "validation", "style"}
    {"type": "header", "cell", "values": [...]}
    {"type": "merge", "range"}
    {"type": "grid", "first_row", "count" | "labels", "columns": [{col, labels|formula|value, format, style}],
     "total": {"row", "label", "label_col", "columns": [...], "formula", "bold"}}
  plus column_widths, freeze_panes, show_gridlines, print {area, landscape, fit_one_page} and charts.
- lists: editable dropdown values, {"length", "lists": {name: [values] | {"param": name}}}.

Number formats: currency, rate, date, number, integer, percent, count, or any Excel format string.
Validation: date, money, number, percent, yesno, or "list:<range>".

Formulas are Excel text and may use these tokens, resolved from the spec's own layout:
  {r} current row, {i}/{i_next} grid index (1-based) and index + 1, {col:Header} a column letter in
  this table, {range:Sheet!Header} a table column's absolute data range, {area:Sheet!First:Last} the
  absolute block from one table column to another, {cell:Sheet!Label} the absolute address of a
  labelled layout value, {list:Name} a dropdown list's range, {listcol:Name} its column letter.

Charts (layout sheets): the chart spec Testing/QA already check plus "data": {"sheet", "categories",
"series": [ranges starting at the header cell]}.

An xlsx file with "example_of": <other xlsx> is that workbook with "example_data" typed in (cell
references as in scenarios), so buyers see it filled in.

PDF: title, footer, blocks[] of {"heading"} | {"p"} | {"bullets", "numbered"} | {"table", "widths"};
text is escaped and **bold** becomes bold. Text: lines[] (joined with CRLF unless "newline": "lf").
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import column_index_from_string, get_column_letter, quote_sheetname
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tools.cells import locate
from tools.charts import bar_chart
from tools.spec import SpecError, excel_value, normalize
from tools.xlsx import (
    BOX, BRAND, CURRENCY, DATE, FORMULA_FILL, HEADER_FILL, HEADER_FONT, INPUT_FILL, INTEGER, NUMBER, PERCENT, RATE,
    SUBTITLE_FONT, TITLE_FONT, WRAP, add_validation, header_row, label, value_cell,
)

FORMATS = {"currency": CURRENCY, "rate": RATE, "date": DATE, "number": NUMBER, "integer": INTEGER,
           "percent": PERCENT, "count": "#,##0"}
TOKEN = re.compile(r"\{(r|i|i_next)\}|\{(col|range|area|cell|list|listcol):([^{}]+)\}")
EMPHASIS_FONT = Font(bold=True, color=BRAND)


def sheet_ref(name: str) -> str:
    """Sheet name as written in a formula: quoted only when it has to be (Jobs, but 'Mileage Log')."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else quote_sheetname(name)


def fmt(name: str | None) -> str | None:
    return FORMATS.get(name, name) if name else None


def build(spec: dict, out_dir: Path) -> list[Path]:
    """Write every deliverable except the ZIP. Returns the paths written, in spec order."""
    spec = normalize(spec)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {f["name"]: f for f in spec["files"]}
    written = []
    for file_spec in spec["files"]:
        path = out_dir / file_spec["name"]
        if file_spec["kind"] == "xlsx":
            base = files[file_spec["example_of"]] if file_spec.get("example_of") else file_spec
            wb = build_workbook(spec, base)
            if file_spec.get("example_of"):
                fill_example(wb, file_spec)
            wb.save(path)
        elif file_spec["kind"] == "pdf":
            write_pdf(file_spec, path)
        else:
            write_text(file_spec, path)
        written.append(path)
    return written


# --------------------------------------------------------------------------- layout map and tokens

class Layout:
    """Where everything in one workbook spec lives, computed before any cell is written."""

    def __init__(self, spec: dict, file_spec: dict):
        self.spec = spec
        self.tables: dict[str, dict] = {}  # sheet -> {"letters": {header: letter}, "rows": n}
        self.cells: dict[tuple[str, str], str] = {}  # (sheet, label) -> coordinate
        self.lists: dict[str, tuple[str, str, int]] = {}  # list name -> (sheet, letter, length)
        for sheet in file_spec["sheets"]:
            kind = sheet.get("kind")
            if kind == "table":
                letters = {c["name"]: get_column_letter(i) for i, c in enumerate(sheet["column_defs"], start=1)}
                self.tables[sheet["name"]] = {"letters": letters, "rows": sheet["rows"]}
            elif kind == "layout":
                for item in sheet.get("items", []):
                    if item.get("type") == "value" and item.get("label"):
                        self.cells[(sheet["name"], item["label"])] = item["cell"]
            elif kind == "lists":
                for index, name in enumerate(sheet["lists"], start=1):
                    self.lists[name] = (sheet["name"], get_column_letter(index), sheet.get("length", 30))

    def resolve(self, text, *, sheet: str, row: int | None = None, index: int | None = None):
        if not isinstance(text, str):
            return text

        def replace(match: re.Match) -> str:
            simple, kind, arg = match.group(1), match.group(2), match.group(3)
            if simple == "r":
                return _need(row, "{r}")
            if simple in ("i", "i_next"):
                return str(int(_need(index, "{i}")) + (simple == "i_next"))
            if kind == "col":
                return self._table(sheet)["letters"][arg] if arg in self._table(sheet)["letters"] else _bad(arg, sheet)
            if kind == "range":
                target, header = _split(arg)
                table = self._table(target)
                letter = table["letters"].get(header) or _bad(header, target)
                return f"{sheet_ref(target)}!${letter}$2:${letter}${table['rows'] + 1}"
            if kind == "area":
                target, span = _split(arg)
                first, last = span.split(":", 1)
                table = self._table(target)
                a, b = (table["letters"].get(h) or _bad(h, target) for h in (first, last))
                return f"{sheet_ref(target)}!${a}$2:${b}${table['rows'] + 1}"
            if kind == "cell":
                target, name = _split(arg)
                coordinate = self.cells.get((target, name)) or _bad(name, target)
                col, num = re.match(r"([A-Z]+)(\d+)", coordinate).groups()
                return f"{sheet_ref(target)}!${col}${num}"
            if kind in ("list", "listcol"):
                if arg not in self.lists:
                    raise SpecError(f"unknown list '{arg}'")
                list_sheet, letter, length = self.lists[arg]
                return letter if kind == "listcol" else f"{sheet_ref(list_sheet)}!${letter}$2:${letter}${length + 1}"
            raise SpecError(f"unknown token {match.group(0)}")

        return TOKEN.sub(replace, text)

    def _table(self, sheet: str) -> dict:
        if sheet not in self.tables:
            raise SpecError(f"'{sheet}' is not a table sheet")
        return self.tables[sheet]


def _need(value, token: str) -> str:
    if value is None:
        raise SpecError(f"{token} used outside a table row or grid")
    return str(value)


def _bad(name: str, sheet: str):
    raise SpecError(f"unknown name '{name}' on sheet '{sheet}'")


def _split(arg: str) -> tuple[str, str]:
    if "!" not in arg:
        raise SpecError(f"expected Sheet!Name, got '{arg}'")
    return tuple(arg.split("!", 1))


def _formula(text: str) -> str:
    return text if text.startswith("=") else "=" + text


# --------------------------------------------------------------------------- workbook

def build_workbook(spec: dict, file_spec: dict) -> Workbook:
    layout = Layout(spec, file_spec)
    wb = Workbook()
    sheets = file_spec["sheets"]
    wb.active.title = sheets[0]["name"]
    for sheet in sheets[1:]:
        wb.create_sheet(sheet["name"])
    writers = {"how_to": _how_to, "table": _table, "layout": _layout, "lists": _lists}
    for sheet in sheets:
        kind = sheet.get("kind")
        if kind not in writers:
            raise SpecError(f"sheet '{sheet['name']}': kind must be one of {sorted(writers)}")
        writers[kind](wb[sheet["name"]], sheet, layout)
    for sheet in sheets:  # charts last: they read cells other sheets fill
        for chart_spec in sheet.get("charts", []):
            data_ws = wb[chart_spec["data"]["sheet"]]
            wb[sheet["name"]].add_chart(bar_chart(chart_spec, data_ws), chart_spec["anchor"])
    wb.active = 0
    return wb


def fill_example(wb: Workbook, file_spec: dict) -> None:
    for ref in file_spec.get("example_data", []):
        try:
            wb[ref["sheet"]][locate(wb, file_spec, ref)] = excel_value(ref["value"])
        except LookupError as error:
            raise SpecError(f"{file_spec['name']}: cannot place example data: {error}") from error


def _how_to(ws, sheet: dict, layout: Layout) -> None:
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 100
    ws["B1"] = sheet["title"]
    ws["B1"].font = TITLE_FONT
    ws["B2"] = sheet.get("intro", "")
    ws["B2"].alignment = WRAP
    row = 4
    ws.cell(row=row, column=2, value=sheet.get("steps_heading", "How to Use")).font = SUBTITLE_FONT
    for number, step in enumerate(sheet.get("steps", []), start=1):
        row += 1
        ws.cell(row=row, column=1, value=number).alignment = Alignment(horizontal="right", vertical="top")
        ws.cell(row=row, column=2, value=step).alignment = WRAP
    row += 2
    ws.cell(row=row, column=2, value=sheet.get("notes_heading", "Good to know")).font = SUBTITLE_FONT
    for note in sheet.get("notes", []):
        row += 1
        ws.cell(row=row, column=2, value=note).alignment = WRAP
    row += 2
    key = ws.cell(row=row, column=2, value=sheet.get(
        "key", "White table cells and yellow cells are for your entries. Grey cells calculate automatically; "
               "don't type over them."))
    key.alignment = WRAP
    key.fill = INPUT_FILL
    ws.sheet_view.showGridLines = False


def _table(ws, sheet: dict, layout: Layout) -> None:
    columns, rows = sheet["column_defs"], sheet["rows"]
    letters = layout.tables[sheet["name"]]["letters"]
    last_row = rows + 1
    for index, column in enumerate(columns, start=1):
        letter = letters[column["name"]]
        header = ws.cell(row=1, column=index, value=column["name"])
        header.fill, header.font, header.border = HEADER_FILL, HEADER_FONT, BOX
        header.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[letter].width = column.get("width", 14)
        number_format = fmt(column.get("format"))
        for r in range(2, last_row + 1):
            cell = ws.cell(row=r, column=index)
            if column.get("formula"):
                cell.value = _formula(layout.resolve(column["formula"], sheet=sheet["name"], row=r))
                cell.fill = FORMULA_FILL
            if number_format:
                cell.number_format = number_format
        if column.get("validation"):
            add_validation(ws, layout.resolve(column["validation"], sheet=sheet["name"]), f"{letter}2:{letter}{last_row}")
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last_row}"
    for offset, values in enumerate(_prefill_rows(sheet, layout.spec)):
        for name, value in values.items():
            col = column_index_from_string(letters[name])
            ws.cell(row=2 + offset, column=col, value=excel_value(value))


def _prefill_rows(sheet: dict, spec: dict) -> list[dict]:
    prefill = sheet.get("prefill")
    if not prefill:
        return []
    if isinstance(prefill, list):
        return prefill
    items = spec.get("parameters", {}).get(prefill["param"])
    if items is None:
        raise SpecError(f"sheet '{sheet['name']}': prefill parameter '{prefill['param']}' not found")
    return [{column: item[key] for column, key in prefill["fields"].items()} for item in items]


def _layout(ws, sheet: dict, layout: Layout) -> None:
    name = sheet["name"]
    for item in sheet.get("items", []):
        kind = item.get("type")
        if kind == "text":
            ws[item["cell"]] = item["text"]
            style = item.get("style", "plain")
            if style == "title":
                ws[item["cell"]].font = TITLE_FONT
            elif style == "subtitle":
                ws[item["cell"]].font = SUBTITLE_FONT
            elif style == "label":
                label(ws, item["cell"], item["text"])
            elif style == "wrap":
                ws[item["cell"]].alignment = WRAP
        elif kind == "value":
            _value(ws, item, layout, name)
        elif kind == "header":
            col, row = re.match(r"([A-Z]+)(\d+)", item["cell"]).groups()
            header_row(ws, int(row), column_index_from_string(col), item["values"])
        elif kind == "merge":
            ws.merge_cells(item["range"])
        elif kind == "grid":
            _grid(ws, item, layout, name)
        else:
            raise SpecError(f"sheet '{name}': unknown item type '{kind}'")
    for col, width in sheet.get("column_widths", {}).items():
        ws.column_dimensions[col].width = width
    if sheet.get("freeze_panes"):
        ws.freeze_panes = sheet["freeze_panes"]
    if sheet.get("show_gridlines") is False:
        ws.sheet_view.showGridLines = False
    if area := (sheet.get("chart_layout") or {}).get("print_area"):
        ws.print_area = area
    if printing := sheet.get("print"):
        if printing.get("landscape"):
            ws.page_setup.orientation = "landscape"
        if printing.get("fit_one_page"):
            ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
            ws.sheet_properties.pageSetUpPr.fitToPage = True


def _value(ws, item: dict, layout: Layout, sheet: str) -> None:
    cell = item["cell"]
    if item.get("label"):
        col, row = re.match(r"([A-Z]+)(\d+)", cell).groups()
        label_cell = item.get("label_cell") or f"{get_column_letter(column_index_from_string(col) - 1)}{row}"
        label(ws, label_cell, item["label"])
    if "formula" in item:
        value = _formula(layout.resolve(item["formula"], sheet=sheet))
    else:
        value = item.get("value")
    value_cell(ws, cell, value, fmt(item.get("format")), entry=bool(item.get("input")))
    if item.get("style") == "emphasis":
        ws[cell].font = EMPHASIS_FONT
    if item.get("validation"):
        add_validation(ws, layout.resolve(item["validation"], sheet=sheet), cell)


def _grid(ws, item: dict, layout: Layout, sheet: str) -> None:
    labels = item.get("labels")
    count = len(labels) if labels else item["count"]
    first = item["first_row"]
    for index in range(1, count + 1):
        row = first + index - 1
        for column in item["columns"]:
            cell = f"{column['col']}{row}"
            if column.get("labels"):
                label(ws, cell, labels[index - 1])
            elif "formula" in column:
                value_cell(ws, cell, _formula(layout.resolve(column["formula"], sheet=sheet, row=row, index=index)),
                           fmt(column.get("format")))
            else:
                value_cell(ws, cell, column.get("value"), fmt(column.get("format")))
    if total := item.get("total"):
        row, last = total["row"], first + count - 1
        label(ws, f"{total.get('label_col', 'A')}{row}", total.get("label", "Total"))
        for col in total["columns"]:
            text = total.get("formula", "=SUM({c}{first}:{c}{last})")
            text = text.replace("{c}", col).replace("{first}", str(first)).replace("{last}", str(last))
            value_cell(ws, f"{col}{row}", _formula(layout.resolve(text, sheet=sheet, row=row)),
                       fmt(total.get("format", "currency")))
            if total.get("bold", True):
                ws[f"{col}{row}"].font = Font(bold=True)


def _lists(ws, sheet: dict, layout: Layout) -> None:
    length = sheet.get("length", 30)
    params = layout.spec.get("parameters", {})
    for index, (name, values) in enumerate(sheet["lists"].items(), start=1):
        if isinstance(values, dict):
            if values.get("param") not in params:
                raise SpecError(f"list '{name}': parameter '{values.get('param')}' not found")
            values = params[values["param"]]
        if len(values) > length:
            raise SpecError(f"list '{name}' has {len(values)} values; the sheet holds {length}")
        letter = get_column_letter(index)
        cell = ws.cell(row=1, column=index, value=name)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        ws.column_dimensions[letter].width = max(24, len(name) + 4)
        for offset in range(length):
            value = ws.cell(row=2 + offset, column=index, value=values[offset] if offset < len(values) else None)
            value.fill = INPUT_FILL
    ws.freeze_panes = "A2"


# --------------------------------------------------------------------------- PDF and text

def markup(text: str) -> str:
    """Escape for reportlab, then **bold** -> <b>bold</b>."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text))


def write_pdf(file_spec: dict, path: Path) -> None:
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10.5, leading=14.5, spaceAfter=6)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=17, textColor=colors.HexColor("#1F4E5A"),
                        spaceBefore=14, spaceAfter=8)
    title_style = ParagraphStyle("title", parent=styles["Title"], textColor=colors.HexColor("#1F4E5A"))
    title = file_spec.get("title", "")
    story: list = [Paragraph(escape(title), title_style)] if title else []
    for block in file_spec.get("blocks", []):
        if "heading" in block:
            story.append(Paragraph(escape(block["heading"]), h1))
        elif "p" in block:
            story.append(Paragraph(markup(block["p"]), body))
        elif "bullets" in block:
            numbered = block.get("numbered", False)
            story.append(ListFlowable([ListItem(Paragraph(markup(i), body), leftIndent=14) for i in block["bullets"]],
                                      bulletType="1" if numbered else "bullet", start="1" if numbered else None,
                                      leftIndent=16, bulletFontSize=9))
            story.append(Spacer(1, 4))
        elif "table" in block:
            rows = block["table"]
            widths = block.get("widths") or [6.7 / len(rows[0])] * len(rows[0])
            table = Table([[Paragraph(escape(c), body) for c in row] for row in rows],
                          colWidths=[w * inch for w in widths])
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3ECEE")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B7C4C8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.extend([table, Spacer(1, 8)])
        else:
            raise SpecError(f"{file_spec['name']}: unknown PDF block {sorted(block)}")
    footer_text = file_spec.get("footer", title)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.HexColor("#5A6B70"))
        canvas.drawString(doc.leftMargin, 0.5 * inch, footer_text)
        canvas.drawRightString(LETTER[0] - doc.rightMargin, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.9 * inch,
                            title=file_spec.get("doc_title", f"{title} - Instructions"), author="Hive-Mint",
                            invariant=1)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def write_text(file_spec: dict, path: Path) -> None:
    text = "\n".join(file_spec.get("lines", [])) + "\n"
    if file_spec.get("newline", "crlf") == "crlf":
        text = text.replace("\n", "\r\n")
    path.write_text(text, encoding="utf-8", newline="")
