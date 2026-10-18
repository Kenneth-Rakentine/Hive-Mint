"""Read charts straight from a workbook's XML (no Excel, no AI) and place them on the sheet.

openpyxl drops or normalises some chart details on load, so the visual checks read the raw
parts: xl/workbook.xml -> worksheets -> drawings -> charts.

Geometry is in logical pixels (96 per inch) at 100% zoom. A chart's size is fixed in EMU
(9525 per logical px), but Excel sizes columns in whole device pixels from the font's max
digit width, which depends on the display's DPI: a column of stored width w is
trunc(((256*w + trunc(128/m)) / 256) * m) device px, with m = 7 at 96 DPI (100% scaling)
and 9 at 120 DPI (125%) for Calibri 11. So the same chart covers slightly different cells
on different screens; checks measure at both DPIs. A row of h points is h*96/72 logical px.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl.utils import range_boundaries

EMU_PER_PX = 9525
EMU_PER_CM = 360_000
MAX_DIGIT_WIDTH = {96: 7, 120: 9}  # Calibri 11, in device px, by display DPI
CHECK_DPIS = tuple(MAX_DIGIT_WIDTH)
DEFAULT_COL_WIDTH = 9.140625  # Excel's stored width for the default 8.43-character column = 64 px at 96 DPI
DEFAULT_ROW_HEIGHT_PT = 15.0

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
R_ID = f"{{{NS['r']}}}id"
_REF = re.compile(r"^(?:'(?P<q>(?:[^']|'')+)'|(?P<s>[^'!]+))!(?P<rng>\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)$")


def col_px(width: float | None, dpi: int = 96) -> float:
    """A column's width in logical px at the given display DPI."""
    width = DEFAULT_COL_WIDTH if width is None else width
    digit = MAX_DIGIT_WIDTH[dpi]
    return int(((256 * width + int(128 / digit)) / 256) * digit) * 96 / dpi


def row_px(height_pt: float | None) -> int:
    return round((DEFAULT_ROW_HEIGHT_PT if height_pt is None else height_pt) * 96 / 72)


def px_to_cm(px: float) -> float:
    return px * EMU_PER_PX / EMU_PER_CM


@dataclass
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    def overlaps(self, other: "Rect") -> bool:
        return self.left < other.right and other.left < self.right and self.top < other.bottom and \
            other.top < self.bottom

    def inside(self, other: "Rect") -> bool:
        return self.left >= other.left and self.top >= other.top and self.right <= other.right and \
            self.bottom <= other.bottom


@dataclass
class Axis:
    kind: str  # "cat", "val", "date" or "ser"
    ax_id: str
    position: str | None
    deleted: bool
    has_title: bool
    num_fmt: str | None
    num_fmt_linked: bool | None
    min: float | None
    max: float | None
    major_unit: float | None
    label_rotation: int | None  # from txPr bodyPr rot, in 60000ths of a degree; None = Excel decides


@dataclass
class ChartInfo:
    sheet: str
    index: int  # 1-based order in the drawing
    part: str
    title: str
    has_title: bool
    title_overlay: bool | None  # None when the overlay element is missing (Excel then overlays the title)
    bar_dir: str | None
    legend_pos: str | None  # None = no legend
    legend_overlay: bool | None  # None when the overlay element is missing (Excel then overlays the legend)
    series_values: list[str] = field(default_factory=list)
    category_refs: list[str] = field(default_factory=list)
    category_cache: list[str] = field(default_factory=list)
    axes: list[Axis] = field(default_factory=list)
    anchor: Rect | None = None  # pixels from the sheet's top-left corner

    def axis(self, kind: str) -> list[Axis]:
        return [a for a in self.axes if a.kind == kind]


@dataclass
class SheetCharts:
    name: str
    charts: list[ChartInfo]
    print_area: Rect | None
    print_area_ref: str | None
    grid: "_Grid"

    def cell_rect(self, cells: str) -> Rect:
        """A1-style range -> its rectangle in logical px on this sheet."""
        return self.grid.rect(cells.split("!")[-1].replace("$", ""))


def parse_ref(ref: str) -> tuple[str, str] | None:
    """'Summary'!$A$7:$A$18 -> ("Summary", "A7:A18")."""
    match = _REF.match(ref.strip())
    if not match:
        return None
    sheet = match["q"].replace("''", "'") if match["q"] is not None else match["s"]
    return sheet, match["rng"].replace("$", "")


# --------------------------------------------------------------------------- package walking

def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """Relationship id -> absolute part name for a part."""
    folder, name = posixpath.split(part)
    rels_part = posixpath.join(folder, "_rels", f"{name}.rels")
    if rels_part not in z.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(z.read(rels_part)).findall("rel:Relationship", NS):
        target = rel.get("Target", "")
        out[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else \
            posixpath.normpath(posixpath.join(folder, target))
    return out


def _sheet_geometry(root: ET.Element) -> tuple[dict[int, float], dict[int, float], float, float]:
    """(column widths by 1-based index, row heights by 1-based row, default col width, default row height)."""
    widths: dict[int, float] = {}
    for col in root.findall("main:cols/main:col", NS):
        if col.get("width") is None:
            continue
        for index in range(int(col.get("min")), int(col.get("max")) + 1):
            widths[index] = float(col.get("width"))
    heights = {int(row.get("r")): float(row.get("ht")) for row in root.findall("main:sheetData/main:row", NS)
               if row.get("ht") is not None}
    fmt = root.find("main:sheetFormatPr", NS)
    default_width = DEFAULT_COL_WIDTH
    default_height = DEFAULT_ROW_HEIGHT_PT
    if fmt is not None:
        if fmt.get("defaultColWidth"):
            default_width = float(fmt.get("defaultColWidth"))
        elif fmt.get("baseColWidth"):
            default_width = float(fmt.get("baseColWidth")) + 5 / MAX_DIGIT_WIDTH[96]
        if fmt.get("defaultRowHeight"):
            default_height = float(fmt.get("defaultRowHeight"))
    return widths, heights, default_width, default_height


class _Grid:
    def __init__(self, widths, heights, default_width, default_height, dpi):
        self.widths, self.heights = widths, heights
        self.default_width, self.default_height = default_width, default_height
        self.dpi = dpi

    def x(self, col0: int, offset_emu: int = 0) -> float:
        """Left edge in logical px of a 0-based column, plus an EMU offset."""
        return sum(col_px(self.widths.get(c, self.default_width), self.dpi) for c in range(1, col0 + 1)) + \
            offset_emu / EMU_PER_PX

    def y(self, row0: int, offset_emu: int = 0) -> float:
        return sum(row_px(self.heights.get(r, self.default_height)) for r in range(1, row0 + 1)) + \
            offset_emu / EMU_PER_PX

    def rect(self, ref: str) -> Rect:
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        return Rect(self.x(min_col - 1), self.y(min_row - 1), self.x(max_col), self.y(max_row))


def _marker(element: ET.Element) -> tuple[int, int, int, int]:
    def num(tag: str) -> int:
        return int(element.findtext(f"xdr:{tag}", "0", NS))

    return num("col"), num("colOff"), num("row"), num("rowOff")


def _anchor_rect(anchor: ET.Element, grid: _Grid) -> Rect | None:
    tag = anchor.tag.split("}")[1]
    if tag == "absoluteAnchor":
        pos, ext = anchor.find("xdr:pos", NS), anchor.find("xdr:ext", NS)
        left, top = int(pos.get("x")) / EMU_PER_PX, int(pos.get("y")) / EMU_PER_PX
        return Rect(left, top, left + int(ext.get("cx")) / EMU_PER_PX, top + int(ext.get("cy")) / EMU_PER_PX)
    col, col_off, row, row_off = _marker(anchor.find("xdr:from", NS))
    left, top = grid.x(col, col_off), grid.y(row, row_off)
    if tag == "oneCellAnchor":
        ext = anchor.find("xdr:ext", NS)
        return Rect(left, top, left + int(ext.get("cx")) / EMU_PER_PX, top + int(ext.get("cy")) / EMU_PER_PX)
    if tag == "twoCellAnchor":
        col2, col2_off, row2, row2_off = _marker(anchor.find("xdr:to", NS))
        return Rect(left, top, grid.x(col2, col2_off), grid.y(row2, row2_off))
    return None


def _print_areas(z: zipfile.ZipFile) -> dict[int, str]:
    root = ET.fromstring(z.read("xl/workbook.xml"))
    out = {}
    for name in root.findall("main:definedNames/main:definedName", NS):
        if name.get("name") == "_xlnm.Print_Area" and name.get("localSheetId") is not None:
            out[int(name.get("localSheetId"))] = (name.text or "").split(",")[0]
    return out


# --------------------------------------------------------------------------- chart parts

def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(t.text or "" for t in element.iter(f"{{{NS['a']}}}t")) or \
        "".join(v.text or "" for v in element.iter(f"{{{NS['c']}}}v"))


def _val(element: ET.Element | None, tag: str) -> str | None:
    if element is None:
        return None
    found = element.find(f"c:{tag}", NS)
    return None if found is None else found.get("val")


def _float(value: str | None) -> float | None:
    return None if value is None else float(value)


def _axis(element: ET.Element) -> Axis:
    kind = element.tag.split("}")[1].removesuffix("Ax")
    num_fmt = element.find("c:numFmt", NS)
    scaling = element.find("c:scaling", NS)
    body = element.find("c:txPr/a:bodyPr", NS)
    rotation = body.get("rot") if body is not None else None
    return Axis(
        kind=kind,
        ax_id=_val(element, "axId") or "",
        position=_val(element, "axPos"),
        deleted=_val(element, "delete") in ("1", "true"),
        has_title=element.find("c:title", NS) is not None,
        num_fmt=num_fmt.get("formatCode") if num_fmt is not None else None,
        num_fmt_linked=(num_fmt.get("sourceLinked") in ("1", "true")) if num_fmt is not None else None,
        min=_float(_val(scaling, "min")),
        max=_float(_val(scaling, "max")),
        major_unit=_float(_val(element, "majorUnit")),
        label_rotation=int(rotation) if rotation is not None else None,
    )


def _chart(z: zipfile.ZipFile, part: str, sheet: str, index: int, anchor: Rect | None) -> ChartInfo:
    root = ET.fromstring(z.read(part))
    chart = root.find("c:chart", NS)
    title = chart.find("c:title", NS)
    overlay = _val(title, "overlay") if title is not None else None
    auto_deleted = _val(chart, "autoTitleDeleted") in ("1", "true")
    legend = chart.find("c:legend", NS)
    plot = chart.find("c:plotArea", NS)
    info = ChartInfo(
        sheet=sheet, index=index, part=part,
        title=_text(title.find("c:tx", NS)) if title is not None else "",
        has_title=title is not None and not auto_deleted,
        title_overlay=None if overlay is None else overlay in ("1", "true"),
        bar_dir=next((d.get("val") for d in plot.iter(f"{{{NS['c']}}}barDir")), None),
        legend_pos=(_val(legend, "legendPos") or "r") if legend is not None else None,
        legend_overlay=None if _val(legend, "overlay") is None else _val(legend, "overlay") in ("1", "true"),
    )
    for series in plot.iter(f"{{{NS['c']}}}ser"):
        info.series_values += [f.text or "" for f in series.findall("c:val//c:f", NS)]
        cat = series.find("c:cat", NS)
        if cat is not None:
            info.category_refs += [f.text or "" for f in cat.findall(".//c:f", NS)]
            if not info.category_cache:
                info.category_cache = [pt.findtext("c:v", "", NS) for pt in cat.iter(f"{{{NS['c']}}}pt")]
    for element in plot:
        if element.tag.split("}")[1] in ("catAx", "valAx", "dateAx", "serAx"):
            info.axes.append(_axis(element))
    info.anchor = anchor
    return info


def read_charts(path: Path, dpi: int = 96) -> dict[str, SheetCharts]:
    """Every chart on every worksheet, keyed by sheet name (sheets without charts are included, empty).

    Anchors and print areas are in logical px for a display at `dpi` (see CHECK_DPIS)."""
    out: dict[str, SheetCharts] = {}
    with zipfile.ZipFile(path) as z:
        workbook_rels = _rels(z, "xl/workbook.xml")
        print_areas = _print_areas(z)
        sheets = ET.fromstring(z.read("xl/workbook.xml")).findall("main:sheets/main:sheet", NS)
        for position, sheet in enumerate(sheets):
            name = sheet.get("name")
            part = workbook_rels.get(sheet.get(R_ID))
            if part is None or part not in z.namelist():
                continue
            root = ET.fromstring(z.read(part))
            grid = _Grid(*_sheet_geometry(root), dpi)
            area_ref = print_areas.get(position)
            area_cells = area_ref.split("!")[-1].replace("$", "") if area_ref else None
            charts: list[ChartInfo] = []
            sheet_rels = _rels(z, part)
            for drawing in root.findall("main:drawing", NS):
                drawing_part = sheet_rels.get(drawing.get(R_ID))
                if drawing_part is None or drawing_part not in z.namelist():
                    continue
                drawing_rels = _rels(z, drawing_part)
                for anchor in ET.fromstring(z.read(drawing_part)):
                    ref = anchor.find(".//c:chart", NS)
                    chart_part = drawing_rels.get(ref.get(R_ID)) if ref is not None else None
                    if chart_part is None or chart_part not in z.namelist():
                        continue
                    charts.append(_chart(z, chart_part, name, len(charts) + 1, _anchor_rect(anchor, grid)))
            out[name] = SheetCharts(name, charts, grid.rect(area_cells) if area_cells else None, area_ref, grid)
    return out
