"""Format-neutral report model. build.py fills it; excel.py and html.py render it,
so the workbook and the HTML report can never disagree."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Col:
    name: str
    fmt: str = "text"     # text | int | ms | pct | num1 | num2 | mb | kb | bool


@dataclass
class Table:
    title: str
    cols: list
    rows: list
    note: str = ""


@dataclass
class Chart:
    title: str
    x_label: str
    y_label: str
    x: list
    series: dict          # name -> list of y values (None = gap)


@dataclass
class Section:
    title: str
    intro: list = field(default_factory=list)
    fields: list = field(default_factory=list)   # (label, value)
    blocks: list = field(default_factory=list)   # Table | Chart


@dataclass
class Doc:
    doc_id: str
    title: str
    subtitle: str
    meta: list            # (label, value) for the cover
    sections: list
    branding: dict


def fmt_value(value, fmt: str) -> str:
    if value is None or value == "":
        return "—"
    try:
        if fmt == "int":
            return f"{int(value):,}"
        if fmt == "ms":
            return f"{float(value):,.1f}"
        if fmt == "pct":
            return f"{float(value) * 100:,.2f}%"
        if fmt == "num1":
            return f"{float(value):,.1f}"
        if fmt == "num2":
            return f"{float(value):,.2f}"
        if fmt == "mb":
            return f"{float(value):,.0f}"
        if fmt == "kb":
            return f"{float(value) / 1024:,.1f}"
        if fmt == "bool":
            return "yes" if value else "no"
    except (TypeError, ValueError):
        return str(value)
    return str(value)
