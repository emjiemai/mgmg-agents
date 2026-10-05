# Report pictures and PDFs

**Code:** `integrations/reports/render.py` (HTML → PDF → PNG),
`integrations/reports/templates/` (the designs), `integrations/reports/fonts/`
(Noto Sans, bundled)
**Since:** 2026-10-05 — the owner: the morning brief as a long text was
overwhelming for the Director; a page whose layout never changes, only the
numbers, reads at a glance.

## What is sent how

| Report | Form | To | Caption (shows in the notification) |
| ------ | ---- | -- | ----------------------------------- |
| Morning brief (A2) | **picture** (Telegram photo, 1080 px wide) | Director | the day + cash, sales, debt |
| 30-day cash calendar (B2, Mondays) | **PDF** | Director, accountants | overdue and outgoing totals |
| KPI table (E1: the 1st, the 5th, `/kpi`) | **PDF** | Director, HR | how many 80+ / 60–79 / below 60 |
| Billz → SAP check, when something is wrong | **PDF** | admin (trial) → Director | what's wrong in one line |
| Data quality (B4, Mondays, `/sifat`) | **PDF** | admin | problems per part |

Unchanged, still text: the Lead Agent's summary, the receivables alert, the
one-line Billz → SAP outcomes (✅ all entered / SAP data too old), tasks,
reports, cheer.

If a picture or PDF can't be drawn, the old text goes out instead — a report
is never lost to its layout. The brief's text is also what `daily_briefs`
stores and what the Director's questions read.

## The design

One system for every report: white page, Noto Sans with tabular figures,
a masthead (title, period, ЭМЖИЕМ), ruled sections. Colour means something:
red only for what needs the Director (overdue debt, people who didn't report
or come, cheques not in SAP), amber for partial or late, green for "all
fine"; missing data is said in grey words ("SAP маълумоти ўқилмади"), never
shown as 0. Icons are drawn (SVG), not emoji. Uzbek Cyrillic throughout.

The brief picture, top to bottom: the five numbers as a statement (label and
detail left, figure right, change since the last brief under it), then shops,
who didn't report (up to 5 names, "+N"), attendance ("эркин график" people
only counted). The website and QR cards are not part of this.

## How it works

`render.pdf(template, **values)` fills a Jinja2 template and lays it out with
WeasyPrint (A4). `render.image(...)` lays the page out 540 px wide on a tall
page, draws it at 1080 px with pypdfium2 and cuts it where the content ends.
No browser, no JavaScript. WeasyPrint needs Pango from the OS — the
Dockerfile installs it; on Windows without GTK, `selfcheck.py` skips only the
file-rendering checks.

Each agent builds its template's values: `ceo-daily-brief.view()`,
`cash-calendar.view()`, `kpi_score.table_view()`, `data-quality.view()`,
`sap_check.pdf_view()`.

## Trying it

```bash
python agents/ceo-daily-brief/agent.py --dry-run   # writes the picture to the temp folder (path in the log)
```

## Checks

`selfcheck.py` `test_reports`: the five numbers in order, quiet zeros, missing
data in words, red only for overdue debt, five names then "+N", Uzbek
Cyrillic, no emoji; a 1080 px PNG cut to its content; each PDF one page.
