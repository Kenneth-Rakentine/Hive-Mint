"""The general spreadsheet builder (layout, tokens, charts and example workbooks come from the spec)."""

import json

import pytest
from openpyxl import load_workbook

from tools.builders import spreadsheet
from tools.spec import SpecError, prepare


def test_tokens_resolve_to_the_spec_layout(good_build):
    wb = load_workbook(good_build / "Mobile_Detailer_Business_Tracker.xlsx")
    assert wb["Jobs"]["H2"].value == '=IF(COUNT(F2:G2)=0,"",SUM(F2:G2))'  # {col:...}{r}
    assert wb["Summary"]["B7"].value.startswith("=SUMIFS(Jobs!$H$2:$H$501,Jobs!$A$2:$A$501,")  # {range:...}
    assert "DATE(Dashboard!$B$5,1,1)" in wb["Summary"]["B7"].value  # {cell:Dashboard!Tax Year}, {i}
    assert "VLOOKUP(A2,'IRS Rates'!$A$2:$B$21,2,TRUE)" in wb["Mileage Log"]["G2"].value  # {area:...}
    assert wb["Summary"]["I8"].value == '=IF(Lists!$C2="","",Lists!$C2)'  # {listcol:...}{i_next}
    assert wb["IRS Rates"]["B3"].value == 0.725  # prefill from parameters


def test_unknown_tokens_and_names_are_spec_errors(tmp_path, sample_spec):
    spec = json.loads(json.dumps(sample_spec))
    jobs = next(s for s in spec["files"][0]["sheets"] if s["name"] == "Jobs")
    jobs["column_defs"][7]["formula"] = "=SUM({col:No Such Column}{r})"
    with pytest.raises(SpecError, match="No Such Column"):
        spreadsheet.build(spec, tmp_path / "a")
    jobs["column_defs"][7]["formula"] = "=SUM({range:Nowhere!Total})"
    with pytest.raises(SpecError, match="Nowhere"):
        spreadsheet.build(spec, tmp_path / "b")


def test_example_workbook_is_the_template_with_data_typed_in(tmp_path, sample_spec):
    spec = json.loads(json.dumps(sample_spec))
    base = spec["files"][0]["name"]
    spec["files"].insert(1, {"name": "Example.xlsx", "kind": "xlsx", "example_of": base, "role": "example",
                             "example_data": [{"sheet": "Jobs", "row": 1, "column": "Date", "value": "2026-03-05"},
                                              {"sheet": "Jobs", "row": 1, "column": "Service Amount", "value": 120},
                                              {"sheet": "Dashboard", "label": "Tax Year", "value": 2026}]})
    spec = prepare(spec)
    written = spreadsheet.build(spec, tmp_path / "out")
    assert [p.name for p in written] == [base, "Example.xlsx", "Instructions.pdf", "README.txt"]
    example = load_workbook(written[1])
    assert example["Jobs"]["F2"].value == 120 and example["Jobs"]["A2"].value.year == 2026
    assert example["Jobs"]["H2"].value.startswith("=IF(COUNT(")  # formulas kept
    assert len(example["Dashboard"]._charts) == 3
    template = load_workbook(written[0])
    assert template["Jobs"]["F2"].value is None
    assert next(f for f in spec["files"] if f["name"] == "Example.xlsx")["sheets"]  # normalize expanded it


def test_tables_print_one_page_wide_with_the_header_repeated(good_build):
    ws = load_workbook(good_build / "Mobile_Detailer_Business_Tracker.xlsx")["Jobs"]
    assert ws.page_setup.orientation == "landscape" and ws.sheet_properties.pageSetUpPr.fitToPage
    assert (ws.page_setup.fitToWidth, ws.page_setup.fitToHeight) == (1, 0)
    assert ws.print_title_rows == "$1:$1"
