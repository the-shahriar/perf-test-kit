"""Self-contained HTML rendering of a report Doc (inline CSS and SVG charts).

Open it in a browser; print it to PDF from there (sections start on new pages).
"""

from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path

from .model import Chart, Doc, Table, fmt_value

PALETTE = ["#2A78D6", "#1BAF7A", "#EDA100", "#6250D6", "#E87BA4", "#008300", "#D6001C"]
NUMERIC = {"int", "ms", "pct", "num1", "num2", "mb", "kb"}

CSS = """
:root { --accent: %(accent)s; --ink: #1F2933; --muted: #616E7C; --rule: #CBD2D9; --band: #F5F7FA; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif; color: var(--ink);
       margin: 0; font-size: 13px; line-height: 1.45; background: #fff; }
header.cover { background: var(--accent); color: #fff; padding: 36px 48px 28px; }
header.cover img { height: 40px; margin-bottom: 18px; display: block; }
header.cover .sub { font-size: 15px; opacity: .9; }
header.cover h1 { font-size: 30px; margin: 4px 0 18px; font-weight: 600; }
header.cover dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 18px; margin: 0; font-size: 12.5px; }
header.cover dt { opacity: .8; } header.cover dd { margin: 0; }
main { padding: 8px 48px 48px; max-width: 1400px; }
nav.toc { margin: 24px 0; columns: 2; font-size: 13px; }
nav.toc a { color: var(--accent); text-decoration: none; display: block; padding: 2px 0; }
section { margin-top: 36px; }
h2 { font-size: 20px; font-weight: 600; border-bottom: 2px solid var(--accent); padding-bottom: 6px; margin: 0 0 12px; }
h3 { font-size: 14px; margin: 22px 0 8px; color: var(--accent); font-weight: 600; }
p.intro { color: var(--muted); margin: 4px 0; max-width: 900px; }
dl.fields { display: grid; grid-template-columns: max-content 1fr; gap: 3px 20px; margin: 12px 0; }
dl.fields dt { color: var(--muted); font-weight: 600; } dl.fields dd { margin: 0; word-break: break-word; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; font-size: 12px; margin: 4px 0; min-width: 40%%; }
th { background: var(--ink); color: #fff; text-align: left; font-weight: 600; padding: 6px 8px; vertical-align: bottom; }
td { padding: 4px 8px; border-bottom: 1px solid var(--rule); vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr:nth-child(even) td { background: var(--band); }
td.bad { color: #B42318; font-weight: 600; }
p.note { color: var(--muted); font-size: 11.5px; margin: 4px 0 0; }
figure { margin: 8px 0 16px; } figcaption { font-size: 12px; color: var(--muted); }
svg text { font-family: inherit; }
footer { color: var(--muted); font-size: 11px; padding: 18px 48px; border-top: 1px solid var(--rule); }
@media print {
  header.cover { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  th, tr:nth-child(even) td { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  section { break-before: page; } nav.toc { break-after: page; } main { padding: 0 24px; }
}
"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _anchor(title: str) -> str:
    return "s-" + "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")


def _table(t: Table) -> str:
    head = "".join(f'<th class="{"num" if c.fmt in NUMERIC else ""}">{_e(c.name)}</th>' for c in t.cols)
    body = []
    for row in t.rows:
        cells = []
        for c, v in zip(t.cols, row):
            cls = "num" if c.fmt in NUMERIC else ""
            if isinstance(v, str) and v == "NOT met":
                cls += " bad"
            cells.append(f'<td class="{cls}">{_e(fmt_value(v, c.fmt))}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    note = f'<p class="note">{_e(t.note)}</p>' if t.note else ""
    return (f"<h3>{_e(t.title)}</h3><div class='tablewrap'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>{note}")


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    import math

    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def _chart(ch: Chart, accent: str) -> str:
    if len(ch.x) < 2:
        return ""
    W, H, L, R, T, B = 760, 280, 64, 16, 16, 46
    pw, ph = W - L - R, H - T - B
    numeric_x = all(isinstance(x, (int, float)) for x in ch.x)
    xs = [float(x) for x in ch.x] if numeric_x else list(range(len(ch.x)))
    x0, x1 = min(xs), max(xs)
    span = (x1 - x0) or 1
    vals = [v for s in ch.series.values() for v in s if v is not None]
    ymax = _nice_max(max(vals) * 1.05 if vals else 1)

    def px(x):
        return L + (x - x0) / span * pw

    def py(y):
        return T + ph - (y / ymax) * ph

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px" role="img" aria-label="{_e(ch.title)}">']
    tick_dp = 0 if ymax >= 20 else (1 if ymax >= 2 else 2)
    for i in range(5):
        y = ymax * i / 4
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{py(y):.1f}" y2="{py(y):.1f}" stroke="#E4E7EB"/>')
        parts.append(f'<text x="{L - 6}" y="{py(y) + 4:.1f}" font-size="10.5" text-anchor="end" fill="#616E7C">'
                     f'{y:,.{tick_dp}f}</text>')
    step = max(1, len(xs) // 10)
    for i, (x, raw) in enumerate(zip(xs, ch.x)):
        if i % step == 0 or i == len(xs) - 1:
            lbl = f"{raw:,}" if isinstance(raw, (int, float)) else str(raw)
            parts.append(f'<text x="{px(x):.1f}" y="{T + ph + 16}" font-size="10.5" text-anchor="middle" '
                         f'fill="#616E7C">{_e(lbl)}</text>')
    parts.append(f'<text x="{L + pw / 2}" y="{H - 6}" font-size="11" text-anchor="middle" fill="#1F2933">{_e(ch.x_label)}</text>')
    parts.append(f'<text x="12" y="{T + ph / 2}" font-size="11" text-anchor="middle" fill="#1F2933" '
                 f'transform="rotate(-90 12 {T + ph / 2})">{_e(ch.y_label)}</text>')
    colours = [accent] + [c for c in PALETTE if c.lower() != accent.lower()]
    legend = []
    for k, (name, series) in enumerate(ch.series.items()):
        col = colours[k % len(colours)]
        pts = [(px(x), py(v), v, raw) for x, v, raw in zip(xs, series, ch.x) if v is not None]
        if not pts:
            continue
        path = " ".join(f"{'M' if i == 0 else 'L'}{a:.1f},{b:.1f}" for i, (a, b, _, _) in enumerate(pts))
        parts.append(f'<path d="{path}" fill="none" stroke="{col}" stroke-width="2"/>')
        if len(pts) <= 40:
            for a, b, v, raw in pts:
                parts.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3" fill="{col}"><title>{_e(name)} at '
                             f'{_e(raw)}: {v:,.1f}</title></circle>')
        legend.append(f'<span style="display:inline-flex;align-items:center;margin-right:14px">'
                      f'<span style="width:14px;height:3px;background:{col};display:inline-block;margin-right:6px">'
                      f'</span>{_e(name)}</span>')
    parts.append("</svg>")
    return (f"<figure><h3>{_e(ch.title)}</h3>{''.join(parts)}"
            f"<figcaption>{''.join(legend)}</figcaption></figure>")


def _logo(project_dir: Path, logo: str | None) -> str:
    if not logo:
        return ""
    path = project_dir / logo
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f'<img alt="" src="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}">'


def write_html(doc: Doc, path: Path, project_dir: Path) -> Path:
    accent = doc.branding.get("accent") or "#2A78D6"
    meta = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in doc.meta if v)
    toc = "".join(f'<a href="#{_anchor(s.title)}">{_e(s.title)}</a>' for s in doc.sections)
    body = []
    for s in doc.sections:
        chunk = [f'<section id="{_anchor(s.title)}"><h2>{_e(s.title)}</h2>']
        chunk += [f'<p class="intro">{_e(line)}</p>' for line in s.intro]
        if s.fields:
            chunk.append("<dl class='fields'>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in s.fields)
                         + "</dl>")
        for b in s.blocks:
            chunk.append(_table(b) if isinstance(b, Table) else _chart(b, accent))
        chunk.append("</section>")
        body.append("".join(chunk))
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(doc.doc_id)} {_e(doc.title)}</title><style>{CSS % {"accent": accent}}</style></head>
<body>
<header class="cover">{_logo(project_dir, doc.branding.get("logo"))}
<div class="sub">{_e(doc.subtitle)}</div><h1>{_e(doc.title)}</h1><dl>{meta}</dl></header>
<main><nav class="toc">{toc}</nav>{''.join(body)}</main>
<footer>{_e(doc.doc_id)} · {_e(doc.branding.get("classification"))} · generated by perf-test-kit</footer>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path
