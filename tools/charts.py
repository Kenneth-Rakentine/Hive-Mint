"""Bar/column charts from a chart spec: the chart types Excel and Google Sheets both import cleanly.

The title sits above the plot (overlay off), category labels are horizontal, the legend (if
any) goes where the spec puts it, and the value axis has a fixed minimum, major unit and number
format, so an empty workbook shows clean labels ($0, $1,000) instead of $0 $0 $1 $1.
"""

from __future__ import annotations

from openpyxl.chart import BarChart, Reference
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import CharacterProperties, ParagraphProperties, RichTextProperties
from openpyxl.drawing.text import Paragraph as TextParagraph
from openpyxl.utils.cell import range_boundaries

from tools.chart_xml import px_to_cm

CHART_COLORS = ("1F4E5A", "E07A2E", "8FB8C4")


def text_properties(size: int, *, bold: bool = False, rotation: int | None = None) -> RichText:
    """Font size (hundredths of a point), weight and an explicit label angle (0 = horizontal, never auto)."""
    body = RichTextProperties(rot=rotation, vert="horz") if rotation is not None else RichTextProperties()
    font = CharacterProperties(sz=size, b=bold)
    return RichText(bodyPr=body, p=[TextParagraph(pPr=ParagraphProperties(defRPr=font), endParaRPr=font)])


def reference(ws, cells: str) -> Reference:
    min_col, min_row, max_col, max_row = range_boundaries(cells)
    return Reference(ws, min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)


def bar_chart(chart_spec: dict, data_ws) -> BarChart:
    """chart_spec["data"] = {"sheet", "categories": "A7:A18", "series": ["B6:B18", ...]}; each series range
    starts with its header cell, which becomes the series name."""
    horizontal = chart_spec.get("horizontal", False)
    value_axis = chart_spec["value_axis"]
    data = chart_spec["data"]
    chart = BarChart()
    chart.type = "bar" if horizontal else "col"
    chart.grouping = "clustered"
    chart.style = 10
    chart.title = chart_spec["title"]
    chart.title.overlay = False
    chart.width, chart.height = px_to_cm(chart_spec["width_px"]), px_to_cm(chart_spec["height_px"])
    for series_range in data["series"]:
        chart.add_data(reference(data_ws, series_range), titles_from_data=True)
    chart.set_categories(reference(data_ws, data["categories"]))
    for series, color in zip(chart.series, CHART_COLORS):
        series.graphicalProperties.solidFill = color
        series.graphicalProperties.line.solidFill = color
        series.invertIfNegative = False  # a loss month keeps its colour instead of turning white

    category, value = chart.x_axis, chart.y_axis
    category.delete = value.delete = False
    category.axPos, value.axPos = ("l", "b") if horizontal else ("b", "l")
    category.txPr = text_properties(900, rotation=0)
    value.txPr = text_properties(900, rotation=0)
    category.tickLblPos = "low"  # labels stay at the edge even when a value is negative
    if horizontal:
        category.scaling.orientation = "maxMin"  # first category at the top, as on the source sheet
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
