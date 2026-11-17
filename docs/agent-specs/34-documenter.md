# 34 — AI documenter, charts, diagrams (2026-11-17)

**Why (the owner, 17.11):** "an AI documenter that can create any type of document with better design, and a graph
AI that draws any diagram, chart…" — open-source tools, not built from scratch. The Boss's dead-stock request of the
same day (an Excel split by department) is the kind of thing it is for.

**Who:** everyone who uses the analyst — the Director, accounting / finance with data access, the Garmin manager.
The tools read no system themselves (`TOOL_SYSTEMS = set()`): a document holds only what the person's own tools
returned. **Employees (17.11, later the same day):** the work AI gets the same three tools (`org_bot/doc_tools.py`, shared;
`ai_chat.answer_with_tools`, ≤ 3 rounds, only these tools) — documents from what the employee wrote and general
knowledge, never invented figures; a daily report is never made a file. Files go after the answer (`_send_files`).

## Tools (integrations/org_bot/analyst.py → integrations/reports/documenter.py)
| Tool | What the model sends | Out |
| ---- | -------------------- | --- |
| `make_document` | `format` pdf / docx / xlsx, `title`, `subtitle`, `blocks` | the file after the answer |
| `make_chart` | `vega_lite` (Vega-Lite v5, data inline) | a PNG shown as a picture |
| `make_diagram` | `dot` (Graphviz DOT) | a PNG shown as a picture |

Blocks: heading {text, level 2|3} · paragraph {text} · bullets / numbered {items} · table {title, columns, rows,
total?, alert_rows?} · kpis {items: [{label, value, note, alert}]} · note {text, tone info|ok|warn|alert} · chart
{vega_lite, caption} · diagram {dot, caption} · divider · pagebreak. `**bold**` is the only markup; all text escaped.
At most 6 files per answer, 150 blocks, 3000 table rows, 25 columns, 8 pictures per document.

## Rendering (open source, all on our server — nothing sent anywhere)
- **PDF:** `templates/document.html` on `base.html` (the report design: masthead, Noto Sans, red only for alerts,
  KPI tiles, callouts, tables with repeated headers) → WeasyPrint.
- **DOCX:** python-docx — Title / Heading styles, shaded table headers, KPI row, pictures.
- **XLSX:** openpyxl — sheet «Умумий» (title, text, key figures), each table its own sheet (styled header, numbers as
  numbers with `#,##0`, frozen header, filter, total row), each picture a sheet.
- **Charts:** `vl-convert-python` (Vega + resvg, no browser) with `THEME`: the dataviz skill's validated categorical
  palette (7 hues, red left for alerts — validator: adjacent CVD ΔE ≥ 9.1, normal-vision ≥ 19.6; three light hues
  under 3:1 → labels/axes always shown), blue sequential ramp, hairline grid, horizontal labels, rounded bar ends,
  bands per category (redrawn at page width when > 900 px). External data (`data.url`) refused
  (`allowed_base_urls=[]`). Each chart in its own process (`chart_worker.py`, one at a time): Vega's V8 holds
  60–220 MB, the service has 512 MB.
- **Diagrams:** Graphviz `dot -Tsvg` (Docker image) with house defaults (rounded band-grey boxes, ink-3 edges, Noto
  Sans — copied into fontconfig), 15 s timeout, ≤ 20 000 characters, file-reading attributes (image, shapefile,
  fontpath, stylesheet…) refused → resvg → PNG.

Settings: `GRAPHVIZ_DOT` (default `dot`), `GRAPHVIZ_LIB_DIR` (only for a Graphviz unpacked outside system paths).
Test: `test_documenter_1117`.
