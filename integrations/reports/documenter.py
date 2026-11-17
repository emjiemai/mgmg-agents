"""AI documenter and charts / diagrams (2026-11-17, the owner: "an AI documenter that can create any type of
document with better design, and a graph AI that draws any diagram or chart").

The analyst (the Director's questions; accounting / finance and the Garmin manager on their data) writes WHAT goes in
a document as a small JSON spec — headings, paragraphs, lists, tables, key figures, notes, charts, diagrams — and this
module lays it out in the company's report design, as:

  * **PDF** — the designed page (Jinja2 template ``document.html`` on ``base.html`` → WeasyPrint, as the reports);
  * **DOCX** — Word, the same structure, editable (python-docx);
  * **XLSX** — Excel: every table a sheet (header row styled, numbers as numbers, frozen header), key figures too.

Pictures, all open source and drawn on our own server (nothing is sent anywhere):

  * **charts** — a Vega-Lite spec (the declarative chart language the model writes well), drawn by ``vl-convert``
    (Vega + resvg, no browser; in a short-lived process, ``chart_worker``, for memory) with the house theme: the validated categorical palette (dataviz skill, 7 hues — red
    is kept for alerts), hairline grid, horizontal labels, Noto Sans. External data links are refused;
  * **diagrams** — Graphviz DOT (flowcharts, org charts, processes, timelines), ``dot`` from the Docker image, house
    styling as defaults; attributes that read files (image, shapefile, fontpath…) are refused.

Every text is escaped; ``**bold**`` is the only markup. Sizes are bounded so one request can't exhaust the 512 MB
service. Errors come back as ``DocError`` with a reason the model can fix.
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from integrations.common.config import settings
from integrations.reports.render import FONTS, html_to_pdf, render_html

# --- the house look ---------------------------------------------------------------------------------------------

INK, INK_2, INK_3 = "#15171c", "#474d59", "#6b7280"
RULE, RULE_STRONG, BAND = "#e4e6ea", "#c9ced6", "#f4f5f7"
ALERT = "#b3261e"
# The dataviz reference categorical order (validated: adjacent CVD ΔE ≥ 9.1, normal-vision ≥ 19.6 on light), slot 8
# (red) left out — red means «needs the Director» in this company's reports.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
FONT = "Noto Sans"

THEME: dict[str, Any] = {
    "font": FONT,
    "background": "#ffffff",
    "padding": 12,
    # Size follows the data: a band per bar / category (no wide empty gaps), a fixed size for continuous axes.
    "view": {"stroke": None, "continuousWidth": 560, "continuousHeight": 260, "discreteWidth": {"step": 56},
             "discreteHeight": {"step": 34}},
    "title": {"font": FONT, "fontSize": 15, "fontWeight": 700, "color": INK, "anchor": "start", "offset": 12,
              "subtitleColor": INK_2, "subtitleFontSize": 12},
    "axis": {"labelFont": FONT, "titleFont": FONT, "labelColor": INK_2, "titleColor": INK_2, "labelFontSize": 11,
             "titleFontSize": 11, "titleFontWeight": 400, "domainColor": RULE_STRONG, "tickColor": RULE_STRONG,
             "gridColor": RULE, "gridWidth": 1, "labelAngle": 0, "labelLimit": 160, "titlePadding": 8},
    "axisX": {"grid": False},
    "axisY": {"domain": False, "ticks": False, "labelPadding": 6},
    "legend": {"labelFont": FONT, "titleFont": FONT, "labelColor": INK_2, "titleColor": INK_2, "labelFontSize": 11,
               "titleFontSize": 11, "orient": "top", "symbolType": "circle"},
    "header": {"labelFont": FONT, "titleFont": FONT, "labelColor": INK_2},
    "range": {"category": PALETTE, "ordinal": SEQUENTIAL[2:], "ramp": SEQUENTIAL,
              "diverging": ["#184f95", "#6da7ec", "#f0efec", "#ec835a", ALERT]},
    "bar": {"color": PALETTE[0], "cornerRadiusEnd": 4, "stroke": "#ffffff", "strokeWidth": 1},
    "line": {"color": PALETTE[0], "strokeWidth": 2, "strokeCap": "round", "strokeJoin": "round"},
    "point": {"color": PALETTE[0], "size": 64, "filled": True, "stroke": "#ffffff", "strokeWidth": 2},
    "area": {"color": PALETTE[0], "opacity": 0.12, "line": {"strokeWidth": 2}},
    "arc": {"stroke": "#ffffff", "strokeWidth": 2},
    "rect": {"stroke": "#ffffff", "strokeWidth": 2},
    "text": {"font": FONT, "color": INK, "fontSize": 11},
}

# Graphviz defaults (the graph's own attributes still win): rounded boxes on the band grey, hairline ink edges.
DOT_DEFAULTS = [
    f"-Gfontname={FONT}", "-Gfontsize=13", "-Gbgcolor=white", "-Gpad=0.25", "-Gnodesep=0.35", "-Granksep=0.45",
    f"-Gfontcolor={INK}",
    f"-Nfontname={FONT}", "-Nfontsize=12", "-Nshape=box", "-Nstyle=rounded,filled", f"-Nfillcolor={BAND}",
    f"-Ncolor={RULE_STRONG}", f"-Nfontcolor={INK}", "-Npenwidth=1", "-Nmargin=0.18,0.08",
    f"-Efontname={FONT}", "-Efontsize=10", f"-Ecolor={INK_3}", f"-Efontcolor={INK_2}", "-Earrowsize=0.7",
    "-Epenwidth=1.1",
]
# Attributes that make Graphviz read a file or a path — never from a model.
DOT_FORBIDDEN = re.compile(r"\b(image|imagepath|shapefile|fontpath|fontnames|stylesheet|xlp|imagepos|imagescale|"
                           r"labelfontname|layers?select)\s*=", re.I)

MAX_SPEC_CHARS = 200_000
MAX_DOT_CHARS = 20_000
MAX_BLOCKS = 150
MAX_TABLE_ROWS = 3000
MAX_COLUMNS = 25
MAX_TEXT = 6000
MAX_PICTURES = 8
PICTURE_SCALE = 2  # pictures are drawn at 2× and shown at their natural size
MAX_CHART_CSS = (900, 900)  # a chart wider / taller than this (CSS px) is redrawn fitted to the page


class DocError(ValueError):
    """The spec can't be laid out — the message says why, for the model to fix."""


# --- charts -----------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _vlc() -> Any:
    import vl_convert as vlc

    vlc.register_font_directory(str(FONTS))
    return vlc


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def chart_spec(spec: Any) -> dict[str, Any]:
    """The model's Vega-Lite spec with the house theme under it (its own choices win, except the font)."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            raise DocError(f"vega_lite is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict) or not spec:
        raise DocError("vega_lite must be a Vega-Lite JSON object")
    if len(json.dumps(spec, default=str)) > MAX_SPEC_CHARS:
        raise DocError("the chart's data is too large — aggregate it first (at most a few hundred rows)")
    out = dict(spec)
    out["config"] = _merge(_merge(THEME, out.get("config") or {}), {"font": FONT})
    out.setdefault("background", "#ffffff")
    return out


_CHART_LOCK = threading.Lock()  # one chart process at a time (memory)
ROOT = Path(__file__).resolve().parents[2]


def chart_png(spec: Any, scale: float = 2.0) -> bytes:
    """A Vega-Lite chart as a PNG (scale 2 = sharp on a phone and in print) — drawn in a short-lived process
    (``chart_worker``): Vega's V8 keeps ~60–220 MB that the 512 MB service can't spare."""
    full = chart_spec(spec)
    png = _draw_chart(full, scale)
    # A band per category is right for a few bars, not for 300 dates: too wide / tall → fitted to the page once.
    if not any(k in full for k in ("facet", "hconcat", "vconcat", "concat", "repeat")):
        width, height = png_size(png)
        if width / scale > MAX_CHART_CSS[0] or height / scale > MAX_CHART_CSS[1]:
            fitted = dict(full)
            if width / scale > MAX_CHART_CSS[0]:
                fitted["width"] = 620
            if height / scale > MAX_CHART_CSS[1]:
                fitted["height"] = 560
            png = _draw_chart(fitted, scale)
    return png


def _draw_chart(full: dict[str, Any], scale: float) -> bytes:
    job = json.dumps({"spec": full, "scale": scale}, default=str).encode("utf-8")
    with _CHART_LOCK:
        try:
            done = subprocess.run([sys.executable, "-m", "integrations.reports.chart_worker"], input=job,
                                  capture_output=True, timeout=45, cwd=str(ROOT), check=False)
        except subprocess.TimeoutExpired as exc:
            raise DocError("the chart took too long to draw — use less data") from exc
    if done.returncode != 0 or not done.stdout.startswith(b"\x89PNG"):
        reason = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
        raise DocError(f"the chart could not be drawn: {reason[0][:300]}")
    return done.stdout


# --- diagrams ---------------------------------------------------------------------------------------------------

def _dot_env() -> dict[str, str]:
    env = dict(os.environ)
    if settings.graphviz_lib_dir:  # a Graphviz unpacked outside the system paths (tests on a laptop)
        env["LD_LIBRARY_PATH"] = settings.graphviz_lib_dir
        env["GVBINDIR"] = os.path.join(settings.graphviz_lib_dir, "graphviz")
    return env


def diagram_svg(dot: Any) -> str:
    """Graphviz DOT → SVG, with the house defaults; file-reading attributes refused; 15 s at most."""
    if not isinstance(dot, str) or not dot.strip():
        raise DocError("dot must be Graphviz DOT text, e.g. digraph { a -> b }")
    if len(dot) > MAX_DOT_CHARS:
        raise DocError("the diagram is too large — at most about 150 boxes")
    if DOT_FORBIDDEN.search(dot):
        raise DocError("image / file attributes are not allowed in a diagram")
    if not re.match(r"\s*(strict\s+)?(di)?graph\b", dot, re.I):
        raise DocError("the DOT text must start with digraph { … } or graph { … }")
    try:
        done = subprocess.run([settings.graphviz_dot, "-Tsvg", *DOT_DEFAULTS], input=dot.encode("utf-8"),
                              capture_output=True, timeout=15, env=_dot_env(), check=False)
    except FileNotFoundError as exc:
        raise DocError("diagrams are not available on this server (Graphviz missing)") from exc
    except subprocess.TimeoutExpired as exc:
        raise DocError("the diagram took too long to lay out — make it smaller") from exc
    if done.returncode != 0 or not done.stdout:
        reason = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
        raise DocError(f"the DOT text has an error: {reason[0][:200]}")
    return done.stdout.decode("utf-8")


def diagram_png(dot: Any, scale: float = 2.0) -> bytes:
    svg = diagram_svg(dot)
    try:
        return _vlc().svg_to_png(svg, scale=scale)
    except Exception as exc:  # noqa: BLE001
        raise DocError(f"the diagram could not be drawn: {str(exc)[:200]}") from exc


# --- document spec ----------------------------------------------------------------------------------------------

BLOCK_TYPES = ("heading", "paragraph", "bullets", "numbered", "table", "kpis", "note", "chart", "diagram", "divider",
               "pagebreak")
TONES = ("info", "ok", "warn", "alert")


@dataclass
class Doc:
    title: str
    subtitle: str
    blocks: list[dict[str, Any]]


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.strip()[:limit]


def _cell(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return "" if value is None else ("ҳа" if value else "йўқ")
    if isinstance(value, (int, float)):
        return value
    return _text(value, 500)


def normalize(spec: Any) -> Doc:
    """The model's spec checked and bounded; unknown block types refused (with the list of the allowed ones)."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            raise DocError(f"the document spec is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise DocError("the document spec must be an object with title and blocks")
    title = _text(spec.get("title"), 160)
    if not title:
        raise DocError("the document needs a title")
    raw = spec.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise DocError("blocks must be a non-empty list")
    if len(raw) > MAX_BLOCKS:
        raise DocError(f"at most {MAX_BLOCKS} blocks")
    blocks: list[dict[str, Any]] = []
    pictures = 0
    for i, b in enumerate(raw):
        if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
            raise DocError(f"block {i}: type must be one of {', '.join(BLOCK_TYPES)}")
        kind = b["type"]
        if kind == "heading":
            blocks.append({"type": kind, "text": _text(b.get("text"), 200), "level": 3 if b.get("level") == 3 else 2})
        elif kind in ("paragraph", "note"):
            blocks.append({"type": kind, "text": _text(b.get("text")),
                           "tone": b.get("tone") if b.get("tone") in TONES else "info"})
        elif kind in ("bullets", "numbered"):
            items = [_text(x, 1000) for x in (b.get("items") or []) if _text(x, 1000)][:200]
            if not items:
                raise DocError(f"block {i}: {kind} needs items")
            blocks.append({"type": kind, "items": items})
        elif kind == "table":
            columns = [_text(c, 120) for c in (b.get("columns") or [])][:MAX_COLUMNS]
            rows = [[_cell(v) for v in (r if isinstance(r, list) else [r])][:MAX_COLUMNS]
                    for r in (b.get("rows") or [])][:MAX_TABLE_ROWS]
            if not columns or not rows:
                raise DocError(f"block {i}: a table needs columns and rows")
            width = len(columns)
            rows = [(r + [""] * width)[:width] for r in rows]
            total = b.get("total")
            total = ([_cell(v) for v in total] + [""] * width)[:width] if isinstance(total, list) else None
            numeric = [all(isinstance(r[c], (int, float)) or r[c] == "" for r in rows)
                       and any(isinstance(r[c], (int, float)) for r in rows) for c in range(width)]
            alert = {int(x) for x in (b.get("alert_rows") or []) if isinstance(x, int) and 0 <= x < len(rows)}
            blocks.append({"type": kind, "title": _text(b.get("title"), 160), "columns": columns, "rows": rows,
                           "total": total, "numeric": numeric, "alert_rows": alert})
        elif kind == "kpis":
            items = []
            for k in (b.get("items") or [])[:8]:
                if isinstance(k, dict) and _text(k.get("label"), 80):
                    items.append({"label": _text(k.get("label"), 80), "value": _text(k.get("value"), 40),
                                  "note": _text(k.get("note"), 120), "alert": bool(k.get("alert"))})
            if not items:
                raise DocError(f"block {i}: kpis needs items with label and value")
            blocks.append({"type": kind, "items": items})
        elif kind in ("chart", "diagram"):
            pictures += 1
            if pictures > MAX_PICTURES:
                raise DocError(f"at most {MAX_PICTURES} charts and diagrams in one document")
            source = b.get("vega_lite") if kind == "chart" else b.get("dot")
            if source in (None, ""):
                raise DocError(f"block {i}: a {kind} needs {'vega_lite' if kind == 'chart' else 'dot'}")
            blocks.append({"type": kind, "source": source, "caption": _text(b.get("caption"), 300)})
        else:
            blocks.append({"type": kind})
    return Doc(title=title, subtitle=_text(spec.get("subtitle"), 200), blocks=blocks)


def _pictures(doc: Doc) -> dict[int, bytes]:
    """Each chart / diagram block drawn once (PNG), by block index."""
    out: dict[int, bytes] = {}
    for i, b in enumerate(doc.blocks):
        if b["type"] == "chart":
            out[i] = chart_png(b["source"])
        elif b["type"] == "diagram":
            out[i] = diagram_png(b["source"])
    return out


def png_size(png: bytes) -> tuple[int, int]:
    """A PNG's pixel (width, height) from its header (IHDR)."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 24:
        return 1200, 800
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def png_width(png: bytes) -> int:
    return png_size(png)[0]


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def rich_html(text: str) -> str:
    """Escaped text; **bold** → <strong>, line breaks kept."""
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text)).replace("\n", "<br>")


def plain(text: str) -> str:
    return _BOLD.sub(r"\1", text)


def _fmt_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}".replace(",", " ")
    return f"{int(value):,}".replace(",", " ")


# --- PDF --------------------------------------------------------------------------------------------------------

def to_pdf(doc: Doc, today: date | None = None) -> bytes:
    from markupsafe import Markup

    pictures = _pictures(doc)
    blocks = []
    for i, b in enumerate(doc.blocks):
        item = dict(b)
        if b["type"] in ("paragraph", "note", "heading"):
            item["html"] = Markup(rich_html(b.get("text") or ""))
        elif b["type"] in ("bullets", "numbered"):
            item["html_items"] = [Markup(rich_html(x)) for x in b["items"]]
        elif b["type"] == "table":
            item["shown_rows"] = [[_fmt_number(v) for v in r] for r in b["rows"]]
            item["shown_total"] = [_fmt_number(v) for v in b["total"]] if b["total"] else None
        elif i in pictures:
            item["src"] = "data:image/png;base64," + base64.b64encode(pictures[i]).decode("ascii")
            item["width_pt"] = round(png_width(pictures[i]) / PICTURE_SCALE * 0.75)  # CSS px → pt
        blocks.append(item)
    day = today or date.today()
    page = render_html("document.html", image=False, title=doc.title, subtitle=doc.subtitle, blocks=blocks,
                       when=f"{day:%d.%m.%Y}", footer_left=f"ЭМЖИЕМ · {doc.title[:60]}")
    return html_to_pdf(page)


# --- DOCX -------------------------------------------------------------------------------------------------------

def _docx_runs(paragraph: Any, text: str, color: Any = None) -> None:
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            run = paragraph.add_run(text[pos:m.start()])
            if color is not None:
                run.font.color.rgb = color
        run = paragraph.add_run(m.group(1))
        run.bold = True
        if color is not None:
            run.font.color.rgb = color
        pos = m.end()
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        if color is not None:
            run.font.color.rgb = color


def _shade(cell: Any, hex_fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_fill.lstrip("#"))
    cell._tc.get_or_add_tcPr().append(shading)  # noqa: SLF001 — python-docx has no public shading API


def to_docx(doc: Doc, today: date | None = None) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    ink, ink2, alert = RGBColor(0x15, 0x17, 0x1C), RGBColor(0x47, 0x4D, 0x59), RGBColor(0xB3, 0x26, 0x1E)
    pictures = _pictures(doc)
    d = Document()
    for section in d.sections:
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin = section.bottom_margin = Cm(1.8)
    normal = d.styles["Normal"]
    normal.font.name, normal.font.size, normal.font.color.rgb = "Arial", Pt(10.5), ink
    for name, size in (("Title", 22), ("Heading 1", 15), ("Heading 2", 12.5)):
        style = d.styles[name]
        style.font.name, style.font.size, style.font.color.rgb, style.font.bold = "Arial", Pt(size), ink, True
    d.add_paragraph(doc.title, style="Title")
    meta = d.add_paragraph()
    meta_run = meta.add_run(" · ".join(x for x in (doc.subtitle, f"{(today or date.today()):%d.%m.%Y}", "ЭМЖИЕМ") if x))
    meta_run.font.color.rgb, meta_run.font.size = ink2, Pt(9.5)
    for i, b in enumerate(doc.blocks):
        kind = b["type"]
        if kind == "heading":
            d.add_paragraph(plain(b["text"]), style="Heading 1" if b["level"] == 2 else "Heading 2")
        elif kind == "paragraph":
            _docx_runs(d.add_paragraph(), b["text"])
        elif kind == "note":
            p = d.add_paragraph()
            _docx_runs(p, b["text"], alert if b["tone"] == "alert" else ink2)
            p.paragraph_format.left_indent = Cm(0.4)
        elif kind in ("bullets", "numbered"):
            for x in b["items"]:
                _docx_runs(d.add_paragraph(style="List Bullet" if kind == "bullets" else "List Number"), x)
        elif kind == "kpis":
            table = d.add_table(rows=2, cols=len(b["items"]))
            for c, k in enumerate(b["items"]):
                top, bottom = table.cell(0, c), table.cell(1, c)
                _shade(top, BAND)
                _shade(bottom, BAND)
                run = top.paragraphs[0].add_run(k["value"])
                run.bold, run.font.size = True, Pt(16)
                run.font.color.rgb = alert if k["alert"] else ink
                label = bottom.paragraphs[0].add_run(k["label"] + (f" — {k['note']}" if k["note"] else ""))
                label.font.size, label.font.color.rgb = Pt(8.5), ink2
        elif kind == "table":
            if b["title"]:
                d.add_paragraph(plain(b["title"]), style="Heading 2")
            rows = b["rows"] + ([b["total"]] if b["total"] else [])
            table = d.add_table(rows=1 + len(rows), cols=len(b["columns"]))
            table.style = "Table Grid"
            for c, name in enumerate(b["columns"]):
                cell = table.cell(0, c)
                _shade(cell, BAND)
                run = cell.paragraphs[0].add_run(name)
                run.bold, run.font.size = True, Pt(9)
            for r, row in enumerate(rows, start=1):
                last = b["total"] is not None and r == len(rows)
                for c, value in enumerate(row):
                    paragraph = table.cell(r, c).paragraphs[0]
                    run = paragraph.add_run(_fmt_number(value))
                    run.font.size = Pt(9)
                    run.bold = last
                    if (r - 1) in b["alert_rows"]:
                        run.font.color.rgb = alert
                    if b["numeric"][c]:
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif kind in ("chart", "diagram"):
            d.add_picture(io.BytesIO(pictures[i]), width=Cm(min(16.5, png_width(pictures[i]) / PICTURE_SCALE / 96 * 2.54)))
            if b["caption"]:
                cap = d.add_paragraph().add_run(b["caption"])
                cap.font.size, cap.font.color.rgb = Pt(9), ink2
        elif kind == "pagebreak":
            d.add_page_break()
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


# --- XLSX -------------------------------------------------------------------------------------------------------

def _sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]\*\?/\\:]", " ", name).strip()[:28] or "Жадвал"
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate, n = f"{base[:25]} {n}", n + 1
    used.add(candidate.lower())
    return candidate


def to_xlsx(doc: Doc, today: date | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    pictures = _pictures(doc)
    wb = Workbook()
    head = wb.active
    head.title = "Умумий"
    head["A1"] = doc.title
    head["A1"].font = Font(bold=True, size=15)
    head["A2"] = " · ".join(x for x in (doc.subtitle, f"{(today or date.today()):%d.%m.%Y}", "ЭМЖИЕМ") if x)
    head["A2"].font = Font(color="474D59")
    line, used = 4, {"умумий"}
    header_fill, thin = PatternFill("solid", fgColor="F4F5F7"), Side(style="thin", color="C9CED6")
    heading = ""
    for i, b in enumerate(doc.blocks):
        kind = b["type"]
        if kind == "heading":
            heading = plain(b["text"])
            head.cell(line, 1, heading).font = Font(bold=True, size=12)
            line += 1
        elif kind in ("paragraph", "note"):
            cell = head.cell(line, 1, plain(b["text"]))
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if b.get("tone") == "alert":
                cell.font = Font(color="B3261E")
            line += 1
        elif kind in ("bullets", "numbered"):
            for n, x in enumerate(b["items"], start=1):
                head.cell(line, 1, f"{n}. {plain(x)}" if kind == "numbered" else f"• {plain(x)}")
                line += 1
        elif kind == "kpis":
            for k in b["items"]:
                head.cell(line, 1, k["label"]).font = Font(color="474D59")
                value = head.cell(line, 2, k["value"])
                value.font = Font(bold=True, color="B3261E" if k["alert"] else "15171C")
                head.cell(line, 3, k["note"])
                line += 1
        elif kind == "table":
            ws = wb.create_sheet(_sheet_name(b["title"] or heading or f"Жадвал {i + 1}", used))
            for c, name in enumerate(b["columns"], start=1):
                cell = ws.cell(1, c, name)
                cell.font, cell.fill = Font(bold=True), header_fill
                cell.border = Border(bottom=thin)
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            for r, row in enumerate(b["rows"], start=2):
                for c, value in enumerate(row, start=1):
                    cell = ws.cell(r, c, value)
                    if isinstance(value, float) and not value.is_integer():
                        cell.number_format = "#,##0.00"
                    elif isinstance(value, (int, float)):
                        cell.number_format = "#,##0"
                    if (r - 2) in b["alert_rows"]:
                        cell.font = Font(color="B3261E")
            if b["total"]:
                r = len(b["rows"]) + 2
                for c, value in enumerate(b["total"], start=1):
                    cell = ws.cell(r, c, value)
                    cell.font, cell.border = Font(bold=True), Border(top=Side(style="thin", color="15171C"))
                    if isinstance(value, (int, float)):
                        cell.number_format = "#,##0.00" if isinstance(value, float) and not value.is_integer() else "#,##0"
            for c, name in enumerate(b["columns"], start=1):
                longest = max([len(str(name))] + [len(_fmt_number(row[c - 1])) for row in b["rows"][:500]])
                ws.column_dimensions[get_column_letter(c)].width = min(max(10, longest + 2), 60)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
        elif kind in ("chart", "diagram"):
            image = XLImage(io.BytesIO(pictures[i]))
            image.width, image.height = image.width / 2, image.height / 2  # drawn at 2×
            ws = wb.create_sheet(_sheet_name(b["caption"] or ("Диаграмма" if kind == "chart" else "Схема"), used))
            ws.add_image(image, "A1")
    head.column_dimensions["A"].width = 90
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --- the one entry point ----------------------------------------------------------------------------------------

FORMATS = {"pdf": to_pdf, "docx": to_docx, "xlsx": to_xlsx}


def filename(title: str, fmt: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title).strip()[:60] or "Ҳужжат"
    return f"{safe}.{fmt}"


def build(fmt: str, spec: Any, today: date | None = None) -> tuple[bytes, str]:
    """(file bytes, file name). ``DocError`` with the reason when the spec can't be laid out."""
    fmt = (fmt or "pdf").lower().lstrip(".")
    if fmt not in FORMATS:
        raise DocError(f"format must be one of {', '.join(FORMATS)}")
    doc = normalize(spec)
    return FORMATS[fmt](doc, today), filename(doc.title, fmt)
