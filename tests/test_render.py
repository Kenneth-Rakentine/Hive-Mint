"""Renderer: engine choice, clear skips and the LibreOffice fallback; real Excel only on request.

The Excel test starts Microsoft Excel, so it runs only with HIVEMINT_TEST_EXCEL=1:
    $env:HIVEMINT_TEST_EXCEL=1; .venv\\Scripts\\python -m pytest tests/test_render.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from config.settings import Settings
from tools import render
from tools.render import choose_engine, render_workbooks

WORKBOOK = "Mobile_Detailer_Business_Tracker.xlsx"


def test_none_skips_with_a_clear_warning_and_a_manifest(tmp_path):
    out = tmp_path / "renders"
    result = render_workbooks([tmp_path / "book.xlsx"], out, preference="none")
    assert result.skipped and result.charts == [] and result.sheet_pdfs == []
    assert "Visual render skipped: rendering is turned off" in result.warnings[0]
    assert json.loads((out / "manifest.json").read_text())["engine"] is None


def test_no_excel_and_no_libreoffice_skips_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "excel_available", lambda: False)
    monkeypatch.setattr(render, "find_soffice", lambda configured=None: None)
    assert choose_engine("auto")[0] is None
    result = render_workbooks([tmp_path / "book.xlsx"], tmp_path / "renders")
    assert result.skipped and "neither Microsoft Excel" in result.warnings[0]
    engine, _, reason = choose_engine("excel")
    assert engine is None and "Excel (with pywin32) is not available" in reason
    with pytest.raises(ValueError):
        choose_engine("paint")


def test_libreoffice_fallback_writes_one_pdf_and_says_there_are_no_chart_pngs(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "excel_available", lambda: False)
    monkeypatch.setattr(render, "find_soffice", lambda configured=None: "soffice")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        out_dir = Path(command[command.index("--outdir") + 1])
        (out_dir / "book.pdf").write_bytes(b"%PDF-1.7")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    result = render_workbooks([tmp_path / "book.xlsx"], tmp_path / "renders")
    assert result.engine == "libreoffice" and result.charts == []
    assert [Path(p).name for p in result.sheet_pdfs] == ["book--all-sheets.pdf"]
    assert "--headless" in calls[0] and "pdf" in calls[0]
    assert any("no chart PNGs" in w for w in result.warnings)


def test_a_failed_libreoffice_run_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "excel_available", lambda: False)
    monkeypatch.setattr(render, "find_soffice", lambda configured=None: "soffice")

    def boom(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(render.subprocess, "run", boom)
    result = render_workbooks([tmp_path / "book.xlsx"], tmp_path / "renders")
    assert result.sheet_pdfs == [] and any("LibreOffice could not render book.xlsx" in w for w in result.warnings)


def test_render_engine_setting_is_validated():
    assert Settings(render_engine="excel").render_engine == "excel"
    with pytest.raises(ValueError, match="HIVEMINT_RENDER_ENGINE"):
        Settings(render_engine="paint")


def _excel_pids() -> set[str]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return {line.split('","')[1] for line in out.splitlines() if line.startswith('"EXCEL')}


@pytest.mark.skipif(os.getenv("HIVEMINT_TEST_EXCEL") != "1", reason="starts Microsoft Excel; set HIVEMINT_TEST_EXCEL=1")
def test_excel_exports_every_sheet_and_chart_and_leaves_no_excel_running(good_build, sample_spec, tmp_path):
    if not render.excel_available():
        pytest.skip("Excel is not installed")
    before = _excel_pids()
    result = render_workbooks([good_build / WORKBOOK], tmp_path / "renders", preference="excel")
    assert _excel_pids() <= before, "the renderer's Excel process is still running"
    assert result.engine == "excel" and result.warnings == []
    sheets = [s["name"] for s in sample_spec["files"][0]["sheets"]]
    assert len(result.sheet_pdfs) == len(sheets)
    assert all(Path(p).read_bytes()[:4] == b"%PDF" for p in result.sheet_pdfs)
    titles = [c["title"] for s in sample_spec["files"][0]["sheets"] for c in s.get("charts", [])]
    assert [c.title for c in result.charts] == titles
    assert all(Path(c.png).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for c in result.charts)
