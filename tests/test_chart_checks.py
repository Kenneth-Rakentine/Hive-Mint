"""Deterministic chart checks, including the regression that started Visual QA.

tests/fixtures/v2_overlapping_charts.xlsx is the v2 workbook as shipped (build 2 of product 1):
titles drawn over the plot, full month names rotated into a bottom legend, auto-scaled value
axes that showed $0 $0 $1 $1 when empty, and chart 1 running into chart 3. It must fail.
"""

import json
import shutil
from pathlib import Path

import pytest

from agents.qa import deterministic_checks
from tools.chart_checks import chart_checks
from tools.chart_xml import read_charts

FIXTURE = Path(__file__).parent / "fixtures" / "v2_overlapping_charts.xlsx"
WORKBOOK = "Mobile_Detailer_Business_Tracker.xlsx"


@pytest.fixture(scope="module")
def workbook_spec(sample_spec):
    return next(f for f in sample_spec["files"] if f["kind"] == "xlsx")


def _by_check(problems) -> dict[str, list]:
    out: dict[str, list] = {}
    for problem in problems:
        out.setdefault(problem.check, []).append(problem)
    return out


def test_clean_v3_build_passes_every_chart_check(workbook_spec, good_build):
    assert chart_checks(workbook_spec, good_build / WORKBOOK) == []


def test_v2_overlapping_title_charts_fail_the_chart_checks(workbook_spec):
    found = _by_check(chart_checks(workbook_spec, FIXTURE))
    titles = [p for p in found["chart_title_overlay"] if "title is drawn over the plot" in p.issue]
    assert len(titles) == 3  # every chart's title overlays its plot
    assert any("legend is drawn over the plot" in p.issue for p in found["chart_title_overlay"])
    assert any("January, February" in p.issue for p in found["chart_month_labels"])
    assert any("no fixed 0 rotation" in p.issue for p in found["chart_month_labels"])
    assert "at the bottom" in found["chart_legend"][0].issue
    assert sum("no fixed major unit" in p.issue for p in found["chart_axis_scale"]) == 3
    assert "Chart 1 'Income vs Costs by Month' overlaps chart 3 'Costs by Category'" in found["chart_overlap"][0].issue
    assert "print area is not set" in found["chart_print_area"][0].issue
    assert any("chart 3" in p.where and "past the intended print area A1:S35" in p.issue
               for p in found["chart_print_area"])
    assert "past the 1440 px" in found["chart_width"][0].issue and len(found["chart_width"]) == 1  # once, not per DPI


def test_qa_treats_chart_problems_as_blocking(sample_spec, build_copy):
    shutil.copyfile(FIXTURE, build_copy / WORKBOOK)
    findings = [f for f in deterministic_checks(sample_spec, build_copy) if f.check.startswith("chart_")]
    assert findings and all(f.source == "deterministic" and f.severity == "major" for f in findings)


def test_required_axis_title_and_spec_scale_are_enforced(workbook_spec, good_build):
    spec = json.loads(json.dumps(workbook_spec))
    dashboard = next(s for s in spec["sheets"] if s["name"] == "Dashboard")
    dashboard["charts"][0]["axis_titles"] = ["value"]
    dashboard["charts"][2]["value_axis"]["major_unit"] = 250
    found = _by_check(chart_checks(spec, good_build / WORKBOOK))
    assert "value axis has no title" in found["chart_axis_title"][0].issue
    assert "major unit is 1000, the spec says 250" in found["chart_axis_scale"][0].issue


def test_layout_limits_come_from_the_spec_and_are_measured_at_each_dpi(workbook_spec, good_build):
    spec = json.loads(json.dumps(workbook_spec))
    dashboard = next(s for s in spec["sheets"] if s["name"] == "Dashboard")
    dashboard["chart_layout"] = {"print_area": "A1:H20", "max_right_px": 1410}
    found = _by_check(chart_checks(spec, good_build / WORKBOOK))
    area = found["chart_print_area"]
    assert "print area is A1:S35, but the intended print area is A1:H20" in area[0].issue
    assert {p.where.split("chart ")[1][0] for p in area[1:]} == {"1", "2", "3"}  # all outside A1:H20
    # Chart 3 ends at 1403 px at 96 DPI but 1429 px at 120 DPI: only the 125% screen is too narrow.
    (width,) = found["chart_width"]
    assert "120 DPI" in width.issue


def test_reader_places_charts_where_excel_does(good_build):
    sheet = read_charts(good_build / WORKBOOK)["Dashboard"]
    assert [round(c.anchor.left) for c in sheet.charts] == [343, 343, 903]  # columns A-C, then D and L at 96 DPI
    assert [round(c.anchor.right - c.anchor.left) for c in sheet.charts] == [500, 500, 500]
    assert sheet.print_area_ref == "'Dashboard'!$A$1:$S$35"
    assert all(c.title_overlay is False for c in sheet.charts)
