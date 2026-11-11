"""Excel rendering of a report Doc: a cover sheet, then one sheet per section."""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .model import Chart, Doc, Table

NUMBER_FORMATS = {"int": "#,##0", "ms": "#,##0.0", "pct": "0.00%", "num1": "#,##0.0", "num2": "#,##0.00",
                  "mb": "#,##0", "kb": "#,##0.0"}
INK = "1F2933"
MUTED = "616E7C"
BAND = "F5F7FA"
RULE = "CBD2D9"


def _hex(c: str | None, default: str) -> str:
    return (c or default).lstrip("#").upper()


def _sheet_name(title: str, used: set) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "", title)[:31]
    base, i = name, 2
    while name in used:
        name = f"{base[:28]} {i}"
        i += 1
    used.add(name)
    return name


def _cell_value(value, fmt):
    if value is None:
        return None
    if fmt == "kb":
        try:
            return float(value) / 1024
        except (TypeError, ValueError):
            return value
    if fmt == "bool":
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        return str(value)
    return value


class _Writer:
    def __init__(self, ws, accent: str):
        self.ws = ws
        self.row = 1
        self.accent = accent
        self.widths: dict[int, int] = {}

    def width(self, col: int, text) -> None:
        n = min(70, len(str(text)) + 2) if text is not None else 4
        self.widths[col] = max(self.widths.get(col, 8), n)

    def text(self, value, bold=False, size=10, color=INK, wrap=False) -> None:
        c = self.ws.cell(self.row, 1, value)
        c.font = Font(bold=bold, size=size, color=color)
        if wrap:
            c.alignment = Alignment(wrap_text=True, vertical="top")
            self.ws.merge_cells(start_row=self.row, start_column=1, end_row=self.row, end_column=10)
        self.row += 1

    def fields(self, fields) -> None:
        for label, value in fields:
            a = self.ws.cell(self.row, 1, label)
            a.font = Font(bold=True, color=MUTED)
            b = self.ws.cell(self.row, 2, value if not isinstance(value, (list, dict)) else str(value))
            b.alignment = Alignment(wrap_text=False)
            self.width(1, label)
            self.row += 1
        if fields:
            self.row += 1

    def table(self, t: Table) -> tuple[int, int]:
        self.text(t.title, bold=True, size=11, color=self.accent)
        thin = Side(style="thin", color=RULE)
        head = self.row
        for j, col in enumerate(t.cols, 1):
            c = self.ws.cell(self.row, j, col.name)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=INK)
            c.alignment = Alignment(wrap_text=True, vertical="center")
            self.width(j, col.name)
        self.row += 1
        for i, values in enumerate(t.rows):
            for j, (col, v) in enumerate(zip(t.cols, values), 1):
                c = self.ws.cell(self.row, j, _cell_value(v, col.fmt))
                c.border = Border(bottom=thin)
                if col.fmt in NUMBER_FORMATS:
                    c.number_format = NUMBER_FORMATS[col.fmt]
                if i % 2:
                    c.fill = PatternFill("solid", fgColor=BAND)
                if col.fmt == "text":
                    self.width(j, v)
            self.row += 1
        if t.note:
            self.text(t.note, color=MUTED, size=9)
        self.row += 1
        return head, self.row

    def chart(self, ch: Chart) -> None:
        self.text(ch.title, bold=True, size=11, color=self.accent)
        top = self.row
        names = list(ch.series)
        self.ws.cell(self.row, 1, ch.x_label).font = Font(bold=True)
        for j, n in enumerate(names, 2):
            self.ws.cell(self.row, j, n).font = Font(bold=True)
        self.row += 1
        for i, x in enumerate(ch.x):
            self.ws.cell(self.row, 1, x)
            for j, n in enumerate(names, 2):
                v = ch.series[n][i] if i < len(ch.series[n]) else None
                self.ws.cell(self.row, j, v).number_format = "#,##0.0"
            self.row += 1
        if len(ch.x) >= 2:
            lc = LineChart()
            lc.title = ch.title
            lc.y_axis.title = ch.y_label
            lc.x_axis.title = ch.x_label
            lc.height, lc.width = 7.5, 16
            data = Reference(self.ws, min_col=2, max_col=1 + len(names), min_row=top, max_row=self.row - 1)
            lc.add_data(data, titles_from_data=True)
            lc.set_categories(Reference(self.ws, min_col=1, min_row=top + 1, max_row=self.row - 1))
            self.ws.add_chart(lc, f"{get_column_letter(len(names) + 3)}{top}")
            self.row = max(self.row, top + 16)
        self.row += 1

    def finish(self) -> None:
        for col, w in self.widths.items():
            self.ws.column_dimensions[get_column_letter(col)].width = w


def write_xlsx(doc: Doc, path: Path, project_dir: Path) -> Path:
    accent = _hex(doc.branding.get("accent"), "2A78D6")
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    w = _Writer(cover, accent)
    logo = doc.branding.get("logo")
    if logo and (project_dir / logo).suffix.lower() == ".png" and (project_dir / logo).exists():
        try:
            from openpyxl.drawing.image import Image

            img = Image(str(project_dir / logo))
            ratio = 40 / img.height
            img.height, img.width = 40, int(img.width * ratio)
            cover.add_image(img, "A1")
            w.row = 4
        except Exception:  # noqa: BLE001 - a logo is never worth failing a report
            pass
    w.text(doc.subtitle, bold=True, size=12, color=MUTED)
    w.text(doc.title, bold=True, size=20, color=accent)
    w.row += 1
    w.fields([(k, v) for k, v in doc.meta if v])
    w.text("Contents", bold=True, size=11, color=accent)
    used = {"Cover"}
    names = [_sheet_name(s.title, used) for s in doc.sections]
    for name in names:
        c = cover.cell(w.row, 1, name)
        c.hyperlink = f"#'{name}'!A1"
        c.font = Font(color=accent, underline="single")
        w.row += 1
    w.widths[1] = 34
    w.widths[2] = 60
    w.finish()
    for sec, name in zip(doc.sections, names):
        ws = wb.create_sheet(name)
        sw = _Writer(ws, accent)
        sw.text(sec.title, bold=True, size=14, color=accent)
        for line in sec.intro:
            sw.text(line, color=MUTED)
        sw.row += 1 if sec.intro else 0
        sw.fields(sec.fields)
        for block in sec.blocks:
            if isinstance(block, Table):
                sw.table(block)
            elif isinstance(block, Chart):
                sw.chart(block)
        sw.finish()
        ws.sheet_view.showGridLines = False
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
