"""Render workbooks the way a buyer sees them: every sheet to PDF, every chart to PNG.

Engines, in "auto" order:
- **excel**: Microsoft Excel via COM (pywin32), Windows only. Each sheet is exported to PDF
  and each chart to PNG at its on-screen size (100% zoom). A private Excel instance is
  started (DispatchEx), so an Excel window the owner has open is never touched, and it is
  always quit in a finally block. If it lingers after Quit, that one process is terminated.
- **libreoffice**: soffice headless converts each workbook to one PDF. It cannot export
  single charts, so there are no PNGs and the vision pass is skipped (with a warning).
- **none**: nothing is rendered; the result carries a clear warning.

Output goes to <build>/renders/ with a manifest.json listing what was produced.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config.settings import RENDER_ENGINES as ENGINES
from tools.recalc import find_soffice

RENDERS_DIR = "renders"
SOFFICE_TIMEOUT_S = 180
EXCEL_EXIT_WAIT_MS = 10_000
XL_TYPE_PDF = 0
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3  # never run macros in a rendered file
XL_SHEET_VISIBLE = -1


class RenderError(RuntimeError):
    """The renderer failed on a workbook."""


@dataclass
class RenderedChart:
    workbook: str
    sheet: str
    index: int  # 1-based position on the sheet
    title: str
    png: str  # path


@dataclass
class RenderResult:
    engine: str | None
    out_dir: str
    sheet_pdfs: list[str] = field(default_factory=list)
    charts: list[RenderedChart] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return self.engine is None

    def as_dict(self) -> dict:
        return asdict(self)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def excel_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # noqa: F401
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Excel.Application\CLSID"))
        return True
    except (ImportError, OSError):
        return False


def choose_engine(preference: str, soffice_path: str | None = None) -> tuple[str | None, str | None, str]:
    """Resolve the preference to (engine or None, soffice path, reason when nothing is usable)."""
    if preference not in ENGINES:
        raise ValueError(f"render engine must be one of {', '.join(ENGINES)}")
    if preference == "none":
        return None, None, "rendering is turned off (HIVEMINT_RENDER_ENGINE=none)"
    if preference in ("auto", "excel") and excel_available():
        return "excel", None, ""
    if preference == "excel":
        return None, None, "HIVEMINT_RENDER_ENGINE=excel but Excel (with pywin32) is not available"
    soffice = find_soffice(soffice_path)
    if soffice:
        return "libreoffice", soffice, ""
    return None, None, "neither Microsoft Excel (with pywin32) nor LibreOffice was found"


def render_workbooks(workbooks: list[Path], out_dir: Path, *, preference: str = "auto",
                     soffice_path: str | None = None) -> RenderResult:
    """Render each workbook into out_dir (emptied first). Never raises for a missing engine."""
    engine, soffice, reason = choose_engine(preference, soffice_path)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    result = RenderResult(engine=engine, out_dir=str(out_dir))
    if engine is None:
        result.warnings.append(f"Visual render skipped: {reason}.")
    for workbook in workbooks:
        try:
            if engine == "excel":
                _excel(workbook, out_dir, result)
            elif engine == "libreoffice":
                _libreoffice(workbook, out_dir, result, soffice)
        except RenderError as error:
            result.warnings.append(str(error))
    if engine == "libreoffice":
        result.warnings.append("LibreOffice cannot export single charts: no chart PNGs, so the vision pass is "
                               "skipped. Install Microsoft Excel for chart images.")
    (out_dir / "manifest.json").write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    return result


# --------------------------------------------------------------------------- Excel (COM)

def _excel(workbook: Path, out_dir: Path, result: RenderResult) -> None:
    import pythoncom
    import pywintypes
    import win32com.client

    pythoncom.CoInitialize()
    excel = wb = None
    pid = None
    try:
        try:
            excel = win32com.client.DispatchEx("Excel.Application")  # a private instance, never the owner's
            pid = _excel_pid(excel)
            excel.Visible = False
            excel.DisplayAlerts = False
            excel.ScreenUpdating = False
            excel.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
            wb = excel.Workbooks.Open(str(workbook.resolve()), 0, True)  # UpdateLinks=0, ReadOnly=True
            excel.CalculateFull()
            stem = slug(workbook.stem)
            for ws in wb.Worksheets:
                if ws.Visible != XL_SHEET_VISIBLE:
                    continue
                name = ws.Name
                pdf = out_dir / f"{stem}--{slug(name)}.pdf"
                try:
                    ws.ExportAsFixedFormat(XL_TYPE_PDF, str(pdf))
                    result.sheet_pdfs.append(str(pdf))
                except pywintypes.com_error as error:
                    result.warnings.append(f"Excel could not export sheet '{name}' to PDF: {_com_message(error)}")
                for index in range(1, ws.ChartObjects().Count + 1):
                    chart = ws.ChartObjects(index).Chart
                    title = chart.ChartTitle.Text if chart.HasTitle else ""
                    png = out_dir / f"{stem}--{slug(name)}--chart{index}-{slug(title)}.png"
                    if chart.Export(str(png), "PNG") and png.is_file():
                        result.charts.append(RenderedChart(workbook.name, name, index, title, str(png)))
                    else:
                        result.warnings.append(f"Excel could not export chart {index} on '{name}' to PNG")
        except pywintypes.com_error as error:
            raise RenderError(f"Excel could not render {workbook.name}: {_com_message(error)}") from error
    finally:
        if wb is not None:
            try:
                wb.Close(False)
            except Exception:  # noqa: BLE001 - closing is best effort; Quit below still runs
                pass
        if excel is not None:
            try:
                excel.Quit()
            except Exception:  # noqa: BLE001
                pass
        wb = excel = None
        pythoncom.CoUninitialize()
        _ensure_exited(pid, result)


def _excel_pid(excel) -> int | None:
    try:
        import win32process

        return win32process.GetWindowThreadProcessId(excel.Hwnd)[1]
    except Exception:  # noqa: BLE001 - without a pid we can still Quit
        return None


def _ensure_exited(pid: int | None, result: RenderResult) -> None:
    """Wait for our Excel process to exit after Quit; terminate it (and only it) if it lingers."""
    if not pid:
        return
    import pywintypes
    import win32api
    import win32con
    import win32event

    try:
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_TERMINATE, False, pid)
    except pywintypes.error:
        return  # already gone
    try:
        if win32event.WaitForSingleObject(handle, EXCEL_EXIT_WAIT_MS) != win32event.WAIT_OBJECT_0:
            win32api.TerminateProcess(handle, 1)
            result.warnings.append(f"Excel (pid {pid}) did not exit after Quit and was terminated.")
    finally:
        win32api.CloseHandle(handle)


def _com_message(error) -> str:
    try:
        return error.excepinfo[2] or error.strerror
    except (AttributeError, IndexError, TypeError):
        return str(error)


# --------------------------------------------------------------------------- LibreOffice

def _libreoffice(workbook: Path, out_dir: Path, result: RenderResult, soffice: str | None) -> None:
    profile = (out_dir / ".lo-profile").resolve().as_uri()  # private profile: no clash with a running LibreOffice
    command = [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--calc",
               "--convert-to", "pdf", "--outdir", str(out_dir), str(workbook)]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=SOFFICE_TIMEOUT_S)
    except (subprocess.SubprocessError, OSError) as error:
        raise RenderError(f"LibreOffice could not render {workbook.name}: {error}") from error
    finally:
        shutil.rmtree(out_dir / ".lo-profile", ignore_errors=True)
    pdf = out_dir / f"{workbook.stem}.pdf"
    if not pdf.is_file():
        raise RenderError(f"LibreOffice produced no PDF for {workbook.name}")
    target = out_dir / f"{slug(workbook.stem)}--all-sheets.pdf"
    pdf.replace(target)
    result.sheet_pdfs.append(str(target))
