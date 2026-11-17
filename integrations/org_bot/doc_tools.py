"""The documenter's three tools, shared by the analyst and the employees' work AI (2026-11-17).

``make_document`` (PDF / DOCX / XLSX from a block spec), ``make_chart`` (Vega-Lite), ``make_diagram`` (Graphviz DOT) —
laid out by ``integrations/reports/documenter.py``. They read no system: a document holds only what the asker's
own tools returned (the analyst) or what the employee wrote and general knowledge (the work AI, which has no company
data). A made file is appended to the answer's file list and sent after the text — pictures as photos.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

MAX_FILES_PER_ANSWER = 6


def _fn(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }}


TOOLS: list[dict] = [
    _fn("make_document", "Make a designed DOCUMENT and the bot sends it after your answer: pdf (the designed page — "
        "default), docx (Word, editable) or xlsx (Excel: every table a sheet). Use it whenever a report, letter, "
        "list, plan, instruction, checklist, analysis or table is asked as a document / file / PDF / Word / Excel. "
        "Put only real figures in it (looked up, or given by the person). Blocks, in order: heading {text, level "
        "2|3}; paragraph {text}; bullets / numbered {items: [text]}; table {title, columns: [..], rows: [[..]], total: "
        "[..] optional, alert_rows: [row indexes] optional — numbers as numbers}; kpis {items: [{label, value, note, "
        "alert}]} (2–4 key figures at the top); note {text, tone info|ok|warn|alert}; chart {vega_lite, caption}; "
        "diagram {dot, caption}; divider; pagebreak. **bold** is the only markup. Uzbek Cyrillic unless asked otherwise.",
        {"format": {"type": "string", "enum": ["pdf", "docx", "xlsx"]},
         "title": {"type": "string"}, "subtitle": {"type": "string", "description": "period, source — optional"},
         "blocks": {"type": "array", "items": {"type": "object"}}}, ("title", "blocks")),
    _fn("make_chart", "Draw a CHART (bar, line, area, pie/donut, scatter, heatmap, stacked, small multiples…) as a "
        "picture sent with your answer. vega_lite = a Vega-Lite v5 spec with the data INLINE (data.values, already "
        "aggregated, at most a few hundred rows; never data.url). One y-axis only (two measures → two charts); at most "
        "7 series (fold the rest into «Бошқа»); axis/legend titles in Uzbek Cyrillic; don't set colors or fonts — the "
        "house theme does. Use it when asked for a chart / график / диаграмма, or when a trend reads better as one.",
        {"vega_lite": {"type": "object"}, "title": {"type": "string", "description": "the picture's file name"}},
        ("vega_lite",)),
    _fn("make_diagram", "Draw a DIAGRAM (flowchart, process, org chart, structure, decision tree, timeline, "
        "relations) as a picture sent with your answer. dot = Graphviz DOT text: digraph { rankdir=LR; a [label=\"…\"]; "
        "a -> b [label=\"…\"]; } — labels in Uzbek Cyrillic, ≤ 60 boxes, clusters (subgraph cluster_x {label=\"…\"}) "
        "for groups. Style comes from the house theme; only for emphasis use fillcolor=\"#fcecea\", color=\"#b3261e\" "
        "(alert). No image / file attributes.",
        {"dot": {"type": "string"}, "title": {"type": "string", "description": "the picture's file name"}},
        ("dot",)),
]
NAMES = frozenset(t["function"]["name"] for t in TOOLS)


def _attach(files: list[tuple[bytes, str]] | None, content: bytes, name: str) -> str | None:
    if files is None:
        return "files can't be sent here"
    if len(files) >= MAX_FILES_PER_ANSWER:
        return f"at most {MAX_FILES_PER_ANSWER} files per answer"
    files.append((content, name))
    return None


async def run(name: str, raw_args: Any, files: list[tuple[bytes, str]] | None, today: date | None = None) -> str:
    """One documenter tool call; the result text goes back to the model (never raises)."""
    from integrations.reports import documenter

    try:
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        if name == "make_document":
            spec = {"title": args.get("title"), "subtitle": args.get("subtitle"), "blocks": args.get("blocks")}
            content, file_name = await asyncio.to_thread(documenter.build, str(args.get("format") or "pdf"), spec, today)
            refused = _attach(files, content, file_name)
            if refused:
                return f"Not sent: {refused}."
            return (f"Document ready: «{file_name}» ({len(content) // 1024} KB). The bot sends it right after your "
                    "answer — say in one line what is in it; don't repeat the whole document in the text.")
        if name == "make_chart":
            png = await asyncio.to_thread(documenter.chart_png, args.get("vega_lite"))
            refused = _attach(files, png, documenter.filename(str(args.get("title") or "Диаграмма"), "png"))
            return f"Not sent: {refused}." if refused else "Chart ready — it is sent as a picture after your answer."
        if name == "make_diagram":
            png = await asyncio.to_thread(documenter.diagram_png, args.get("dot"))
            refused = _attach(files, png, documenter.filename(str(args.get("title") or "Схема"), "png"))
            return f"Not sent: {refused}." if refused else "Diagram ready — it is sent as a picture after your answer."
    except documenter.DocError as exc:
        what = {"make_document": "The document could not be made", "make_chart": "The chart could not be drawn",
                "make_diagram": "The diagram could not be drawn"}[name]
        return f"{what}: {exc}. Fix it and call {name} again."
    return f"No such tool {name!r}."
