---
name: MGMG Command Center
description: The morning brief's paper, read like an exchange — ink on white, hairline rules, state colour only; the same system on graphite in dark mode. PULSE has its own analytics look since 2026-10-23 (see "PULSE screen").
colors:
  ink: "#15171c"
  ok: "#1d6b46"
  warn: "#8a5300"
  alert: "#b3261e"
  ok-wash: "#e5f3eb"
  warn-wash: "#fdf2dd"
  alert-wash: "#fcecea"
  ground: "#ffffff"
  band: "#f4f5f7"
  ink-2: "#474d59"
  ink-3: "#646b78"
  rule: "#e4e6ea"
  rule-strong: "#c9ced6"
  chart-bars: "#c9ced6"
  chart-area: "rgba(21, 23, 28, 0.07)"
  select: "#dfe3e9"
  ground-dark: "#111317"
  band-dark: "#181b20"
  ink-dark: "#eceef1"
  ink-2-dark: "#b8bdc6"
  ink-3-dark: "#8e95a1"
  rule-dark: "#252a31"
  rule-strong-dark: "#353b44"
  ok-dark: "#46b884"
  warn-dark: "#e2a64a"
  alert-dark: "#ef6f63"
  chart-bars-dark: "#3a414b"
  chart-area-dark: "rgba(236, 238, 241, 0.08)"
  select-dark: "#2b313a"
typography:
  display:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "40px"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: '"tnum" 1, "lnum" 1'
  headline:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: '"tnum" 1, "lnum" 1'
  figure:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "-0.01em"
    fontFeature: '"tnum" 1, "lnum" 1'
  title:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    lineHeight: 1.4
  heading:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    lineHeight: 1.4
  body:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.4
    fontFeature: '"tnum" 1, "lnum" 1'
  body-sm:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.4
    fontFeature: '"tnum" 1, "lnum" 1'
  symbol:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.06em"
  label:
    fontFamily: "Noto Sans, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
    fontFeature: '"tnum" 1, "lnum" 1'
rounded:
  hairline: "3px"
  control: "6px"
  button: "8px"
  pill: "999px"
  round: "50%"
spacing:
  xs: "6px"
  sm: "8px"
  md: "14px"
  pad: "20px"
  pad-phone: "16px"
  column: "28px"
components:
  top-bar:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    typography: "{typography.title}"
    padding: "12px 20px"
  range-switch:
    textColor: "{colors.ink-2}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.control}"
    padding: "6px 12px"
  range-switch-hover:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink}"
  range-switch-pressed:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
  sector-chip:
    textColor: "{colors.ink-2}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.pill}"
    padding: "4px 12px"
  sector-chip-pressed:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
  icon-button:
    textColor: "{colors.ink}"
    rounded: "{rounded.button}"
    size: "36px"
  icon-button-hover:
    backgroundColor: "{colors.band}"
  quote:
    textColor: "{colors.ink}"
    typography: "{typography.display}"
  ticker:
    textColor: "{colors.ink}"
    typography: "{typography.figure}"
    padding: "14px 14px 12px"
  delta-up:
    textColor: "{colors.ok}"
  delta-down:
    textColor: "{colors.alert}"
  delta-flat:
    textColor: "{colors.ink-3}"
  tape-row:
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    padding: "7px 0"
  board-row:
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    padding: "9px 8px"
  board-row-hover:
    backgroundColor: "{colors.band}"
  status-dot:
    size: "9px"
    rounded: "{rounded.round}"
  person-sheet:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    width: "440px"
    padding: "14px 20px 24px"
  notice:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
    typography: "{typography.body}"
    padding: "10px 12px 10px 16px"
  skeleton:
    backgroundColor: "{colors.band}"
    rounded: "{rounded.hairline}"
    height: "12px"
---

# Design System: MGMG Command Center

## Overview

**Creative North Star: "The Paper Exchange"**

The morning brief's paper, read like an exchange. The surfaces made for the Director and his managers — the 08:00 brief picture and the A4 report PDFs (`integrations/reports/templates/`) — are near-black ink on a white page, divided by hairline rules, set in one sans with tabular figures. PULSE carries that paper onto a screen and borrows a trading terminal's grammar: a quote with its arrow, a pulse line over volume bars, a time-and-sales tape, a ranked board. Dark mode is the same paper on a graphite ground, not a second theme. The public customer pages (`/f` complaints in red, `/r` reviews in green), their QR cards and the `/db` viewer are outside this system and keep their own settled looks.

Density is a terminal's: many figures, very few words. Every user-facing word is Uzbek Cyrillic (business and product names stay as written: PULSE, Garmin, Primus, Londry), and the owner's standing order is to add no more text — numbers, arrows and lines carry the meaning. Colour is never decoration: green, amber and red mean good, late and needs-the-Director, and nothing else. Structure comes from rules and alignment; there are no shadows, no gradients and no tiles.

Rejected outright: the card-grid dashboard of equal tiles, rounded card soup, emoji.

**Key Characteristics:**
- White paper (graphite in dark mode), near-black ink, three line weights.
- Noto Sans 400 and 700 only; tabular, lining figures everywhere.
- State colour only: green good, amber late, red for what needs the Director.
- Drawn stroke icons and drawn ▲/▼ marks, never glyphs or emoji.
- Flat: no shadows, no gradients; regions are ruled, never boxed.
- Uzbek Cyrillic, as few words as possible.

## PULSE screen (2026-10-23 — its own look)

The owner rejected the paper look for PULSE and gave a reference: a navy analytics dashboard. **PULSE now
follows this section; everything else in this file is for the brief picture and the report PDFs.** The words,
icons, delta and missing-data rules below still hold on PULSE.

- **Two themes, the viewer's choice:** dark navy (default when the device is dark) and cool white; the ☀/☾ button
  in the left rail (top bar on phones) switches and remembers it (`localStorage` `pulse-theme`). Tokens are CSS
  custom properties at the top of `pulse.css`; the charts read them and repaint on a switch.
- **Dark:** ground #0c1424, rail #121c30, cards #18233a / #212e48, ink #f1f5fb / #c3ccdb / #8794ab, rule #26334d.
  **White:** ground #f2f5fa, cards #ffffff with a soft two-layer shadow, ink #0f1a2e / #34425a / #64738b.
- **Data colours:** cyan (#5ccbe6 / #0b96bf) the index line, purple (#8f83e0 / #6f61cc) the sales bars, teal and
  amber for the other businesses; up green (#2ccf8a / #12a061), down red (#f2546d / #df2f4f) — still by the
  Good-Not-Sign Rule.
- **Shape:** a 76px icon rail on the left (hidden on phones); rounded 16px cards on a 12-column grid — six ticker
  cards (value, ▲▼ %, smooth sparkline), the index card (8 columns: curved cyan line over a gradient) beside the
  sales card (4: purple daily bars on their own so'm scale; one crosshair across both), the board (8) beside the
  tape (4). Avatars are initials on a colour from the person's id. («Бизнеслар» removed 2026-10-23.)
- **Words allowed here (the owner, 2026-10-23):** a card title may carry its meaning in brackets in 13px ink-3
  («Индекс (компаниянинг кунлик балли)») and one hint line under it; the tape's marks have a key behind ⓘ.
- **Motion:** cards rise in (640ms, staggered), lines rise from their floor left to right (1.3s, ease-out cubic,
  scale held at the final values), bars grow, figures count up (1s), sparklines draw (clip reveal), rows follow
  45ms apart; the last index point breathes. Only on entry and on a new period; none under reduced motion.
- **Speed:** fonts are Latin + Cyrillic WOFF2 subsets (87 KB, not 1 MB of TTF); the chart library and fonts are
  cached a year, the page's own files revalidate by ETag; everything is gzipped; the data is served from a kept
  copy and rebuilt behind it (spec 28 §Speed).

## Colors

Ink on paper, plus three state colours that each mean one thing and are used for nothing else.

### Primary
- **Press Ink** (#15171c): all primary text and figures; the 2px masthead rule under the top bar and a sheet's head; the pulse line and the sparkline end-dot; the pressed state of switches and chips (ink fill, paper text); the focus ring; the chart crosshair's label. There is no brand hue — ink is the voice.

### Secondary
State colours. PULSE names green and red `--up` / `--down`; the reports name the same values `--ok` / `--alert`.
- **Ledger Green** (#1d6b46): a change that is good (▲ on sales, ▼ on complaints); present or on time; a report in; a complaint resolved; an explanation given; the tape's live dot.
- **Late Amber** (#8a5300): late arrival; a lower-bound figure («камида») in the brief. Never a fall, never an error.
- **Signal Red** (#b3261e): what needs the Director — a bad change, an absence, a missed report, a complaint.
- **State Washes** (ok-wash #e5f3eb, warn-wash #fdf2dd, alert-wash #fcecea): the faint ground behind one state callout in a report. PULSE uses none.

### Neutral
- **Paper White** (#ffffff): the ground of every surface and of the chart canvas.
- **Ledger Band** (#f4f5f7): hover on rows and controls, skeleton bars, the flash behind a newly arrived tape row, a report's single callout panel.
- **Slate Ink** (#474d59): secondary text — the tape's action, the unit beside a figure, unpressed controls, the sparkline stroke, the quote symbol.
- **Pencil Grey** (#646b78): tertiary text — times, labels, column headers, roles, details, chart axes, flat or unknown deltas, the "—" of missing data, today's volume bar. Holds 4.9:1 on Ledger Band.
- **Hairline** (#e4e6ea): 1px rules between rows and tickers; the chart's horizontal gridlines.
- **Strong Rule** (#c9ced6): 1px rule under section heads; control outlines; the person sheet's edge; the hollow status dot.
- **Volume Grey** (chart-bars #c9ced6): the sales volume bars under the pulse line.
- **Chart Wash** (chart-area rgba(21, 23, 28, 0.07)): the flat fill under the pulse line and every sparkline.
- **Selection** (#dfe3e9): selected text.

### Graphite (dark mode)
Under `prefers-color-scheme: dark` the same tokens re-point to the `-dark` values: Graphite Ground (#111317), Graphite Band (#181b20), Paper Ink (#eceef1), Slate (#b8bdc6), Pencil (#8e95a1), rules (#252a31 / #353b44), Green (#46b884), Amber (#e2a64a), Red (#ef6f63), Volume (#3a414b), Wash (rgba(236, 238, 241, 0.08)), Selection (#2b313a). State colours are lifted in lightness so each holds at least 6:1 on graphite. Reports are light only.

### Named Rules
**The Red-Is-for-the-Director Rule.** Red marks only what needs the Director: a bad change, an absence, a missed report, a complaint. Never emphasis, branding or a heading.

**The Good-Not-Sign Rule.** The drawn ▲/▼ shows the sign; the colour shows whether it is good. Complaints going up are a red ▲. Zero or unknown is Pencil Grey, never green.

**The One-Paper Rule.** Dark mode re-points the same tokens to graphite; on screen, no hue, token or component exists in only one mode. Charts read their colours from the CSS custom properties at paint time and repaint when the scheme changes.

## Typography

**Display Font:** Noto Sans 700 (with system-ui, sans-serif)
**Body Font:** Noto Sans 400 (with system-ui, sans-serif)

**Character:** One humanist sans with full Cyrillic in two weights; a terminal's density built from size and weight alone. The app serves its own TTFs (`/pulse/static/`, the reports' bundled fonts) — never a font host.

### Hierarchy
- **Display** (700, 40px, line-height 1, −0.02em): the index price; 34px on phones.
- **Headline** (700, 28px, line-height 1): a person's score at the head of their sheet.
- **Figure** (700, 20px, −0.01em): a ticker's value; the board's score at 18px. An unknown value drops to 16px 400 in Pencil Grey.
- **Title** (700, 17px): a person's name in the sheet; the PULSE wordmark, tracked +0.08em beside its drawn mark.
- **Heading** (700, 15px): section heads (Бугун, Ходимлар) — the same size as a report's block head.
- **Body** (400, 14px, 1.4): the default; names on the board in 700.
- **Body small** (400, 13px): tape rows, switch and chip labels, the time stamp, the unit beside a figure.
- **Symbol** (700, 13px, +0.06em, uppercase, Slate Ink): a quote's name set on the price's baseline (ИНДЕКС), the way an exchange prints a ticker symbol.
- **Label** (400, 12px): ticker labels, column headers, roles, the chart legend and axes; deltas at 12.5px.

### Named Rules
**The Tabular Figures Rule.** Every figure is tabular and lining (`tnum`, `lnum`), so tickers and columns hold still while values change. Thousands group with a space and decimals take a comma (ru-RU): 1,3 млрд.

**The Quiet Unit Rule.** A unit sits after its number at 13px 400 in Slate Ink (млрд, млн, минг, сўм, %), never at the figure's size or weight.

**The Two Weights Rule.** 400 and 700 only; no italics, no second family. Hierarchy is size and weight.

## Layout

PULSE is a trading screen on a two-column grid: a fluid main column and a 340px tape on the right, a 28px column gap, a 20px page gutter, at most 1480px wide and centred. The tickers and the market (quote line, then a 380px chart) stack in the main column; the tape spans both beside them and scrolls inside itself (at most 560px tall); the board runs full width beneath, capped at 1040px so a row reads as one line. The six tickers share one line in equal columns, separated by vertical hairlines. The top bar is sticky and respects safe-area insets.

**Tablet (≤1100px):** the tape narrows to 290px; the tickers fold into 3 × 2 with a hairline between the rows.

**Phone (≤760px):** one column — tickers, market, tape, board — at a 16px gutter. The tickers become a horizontal strip bleeding to the screen edges, each 42% wide with scroll-snap. The index drops to 34px, the chart to 260px, the tape to 360px with no left rule; the board drops its sparkline column; the time stamp wraps under the wordmark; switches and chips grow to 40px tall. The person sheet becomes a full-screen sheet.

Spacing steps are 6, 8, 14, 20 and 28px; rows pad 7–9px vertically. Reports keep the same tokens at A4 (16 / 15 / 18mm margins) and in the brief picture (30 / 28px padding).

### Named Rules
**The Equal Phone Rule.** The phone is a first reader, not a fallback: every view works in one column at a 16px gutter with no horizontal page scroll, and every control there is at least 40px tall.

## Elevation & Depth

Flat paper. No shadows, no gradients, no blur. Depth is order and line: the sticky top bar sits on the page's own ground above its 2px ink rule; the person sheet slides in from the right as plain ground with a 1px Strong Rule edge (no scrim); the error notice, the only floating element, separates by inversion — an ink bar with paper text. The fill under the pulse line is one flat translucent wash, the same at top and bottom.

### Named Rules
**The Flat Paper Rule.** Nothing casts a shadow and nothing fades. An element that must sit above the page gets an edge (1px Strong Rule) or inverts to ink.

## Shapes

Square regions, softened controls. Sections, tickers, the tape, the board and the person sheet have no corners and no boxes — rules bound them. Controls carry small functional radii: 6px on the segmented switch and the notice's button, 8px on icon buttons and on a report's single callout panel, a full pill on sector chips, 3px on the focus ring and skeleton bars, full circles for status and live dots.

Icons are drawn on a 24-unit grid with round caps and joins in `currentColor`: stroke 1.7 on screen (1.8 in the reports' SVG renderer, which ignores page CSS), 2.2 for the PULSE mark. The ▲/▼ of a delta are drawn 10px triangles, filled.

### Named Rules
**The Three Weights Rule.** 2px Press Ink under the top bar and a sheet's head (the masthead rule); 1px Strong Rule under a section head and around controls; 1px Hairline between rows, tickers and gridlines. No fourth weight.

## Components

### Buttons
- **Segmented switch (7 / 30 / 90 кун):** one 1px Strong Rule outline with 6px corners, segments divided by the same rule, 13px Slate Ink labels, 6px × 12px padding. Hover lays Ledger Band and turns the label to ink; the pressed segment is an ink fill with paper text in 700. Colour transitions 160ms ease-out.
- **Icon button (close, previous / next day):** 36px square, 8px corners, 1px Strong Rule, an 18px drawn icon. Hover lays Ledger Band; disabled drops to 35% opacity.
- **Focus (every control):** a 2px Press Ink outline offset 2px with 3px corners.

### Chips
- **Sector filter (Ҳамма, Garmin, Primus, Londry…):** pill outline in 1px Strong Rule, 13px Slate Ink label, the sector's score after it in 700. Hover darkens the outline to Pencil Grey and the label to ink; pressed is an ink fill with paper text. The row scrolls sideways without a scrollbar; it shows only when there are two or more sectors.

### Cards / Containers
- None. There are no cards anywhere in the system. A region is a heading over a 1px Strong Rule with its rows beneath, or a column bounded by a hairline.

### Navigation
- **Top bar:** sticky, Paper White, closed by the 2px ink masthead rule. Left: the drawn pulse mark (22px) and PULSE at 17px 700, tracked +0.08em; then the time stamp in 13px Pencil Grey; the segmented switch pushed right. On phones the time stamp wraps to its own line.

### Quote and chart (signature)
- **Quote line:** the symbol (ИНДЕКС, 13px 700 tracked uppercase, Slate Ink) on the baseline of a 40px 700 price, the delta beside it at 15px, and a 12px legend pushed right — the hovered day, a line key with the index value, a bar key with the day's sales — that follows the crosshair and rests on the last day.
- **Chart:** TradingView Lightweight Charts 4.2.0, vendored and served by the app. Paper ground, 12px Pencil Grey axis text in Noto Sans, horizontal gridlines in Hairline, no vertical gridlines, no scale borders. The pulse line is a 2px ink area series over the flat Chart Wash; sales volume is a histogram on its own hidden scale in the bottom 20%, bars in Volume Grey with today's bar in Pencil Grey. Crosshair lines in Pencil Grey with ink labels. Scroll and zoom are off; dates read dd.MM.yyyy.

### Ticker
- Label in 12px Pencil Grey; the value in 20px 700 with its unit quiet after it; the delta beneath; a 26px sparkline at the foot. Tickers sit on one Hairline, separated by vertical hairlines — no boxes. Missing data shows "—" at 16px 400 Pencil Grey.

### Delta
- A drawn 10px triangle and the absolute value, coloured by the Good-Not-Sign Rule (Ledger Green good, Signal Red bad, Pencil Grey flat or unknown). Complaints invert the sense.

### Sparkline
- A 1.5px Slate Ink line through the days that have a value (a missing day is stepped over, never drawn as zero), the flat Chart Wash beneath, and a small ink dot (radius 2) on the last point.

### Tape (signature)
- Time and sales for the company: newest first, each row a 42px time column in Pencil Grey, an 18px column for a 16px drawn icon, then the person's name in 700 (a button that opens their sheet; underline on hover), the action in Slate Ink, the detail in Pencil Grey. Rows are 13px on 1px Hairlines under a 15px heading with a breathing green live dot (2.4s). Icon colour by kind: arrived, report, resolved, explained in Ledger Green; late in Late Amber; absent, missed report, complaint in Signal Red; left and task events in Slate Ink. A new row arrives with a 6px drop and a Ledger Band flash (520ms, cubic-bezier(0.16, 1, 0.3, 1)).

### Board
- A ranked table: rank in Pencil Grey, a 9px status dot (filled Green present, Amber late, Red absent; hollow Strong Rule when unknown, with the word for screen readers), the name in 700 over the role in 12px Pencil Grey, a 180 × 28px sparkline, the score right-aligned at 18px 700, the delta right-aligned. Headers are 12px 400 Pencil Grey. The whole row is the target and lays Ledger Band on hover (140ms ease-out).

### Person sheet
- A 440px panel from the right (full screen on phones): Paper White, 1px Strong Rule edge, no shadow. Its head — close button, name in 17px 700 over the role, the score at 28px with its delta — closes with the 2px ink masthead rule. Then a 200px line of their days (same chart options; the chosen day marked with an ink dot; a click opens that day), a day switcher between two icon buttons, and the day's timeline in the tape's row grammar. It slides in over 240ms cubic-bezier(0.16, 1, 0.3, 1) and out over 200ms ease-in; Escape closes it and focus returns to what opened it.

### States
- **Loading:** Ledger Band skeleton bars (12px and 22px, 3px corners) in the ticker slots.
- **Empty:** one short line in Pencil Grey (Ҳали ҳаракат йўқ, Ходим йўқ).
- **Error:** the notice — an ink bar at the bottom centre with paper text and an outlined Қайта button.
- **Reduced motion:** every animation and transition is off.

## Do's and Don'ts

### Do:
- **Do** write every user-facing word in Uzbek Cyrillic; keep business and product names as written (PULSE, Garmin, Primus, Londry).
- **Do** keep text to the minimum: a label is one or two words, and numbers, arrows and lines carry the rest.
- **Do** show missing data as "—" in Pencil Grey, and step over a missing day in a line — never draw it as zero.
- **Do** set every figure tabular and lining, with its unit at 13px 400 in Slate Ink.
- **Do** bound regions with the three rule weights: 2px Press Ink for the masthead, 1px Strong Rule under a section head, 1px Hairline between rows.
- **Do** draw icons as 24-unit strokes in `currentColor`, and draw ▲/▼ as filled triangles.
- **Do** give every screen token a graphite value and read chart colours from the CSS custom properties, repainting when the colour scheme changes.
- **Do** turn every animation and transition off under `prefers-reduced-motion`.

### Don't:
- **Don't** build a grid of equal cards or boxed KPI tiles; tickers sit on hairlines.
- **Don't** use shadows, gradients or blur; the chart's fill is one flat wash.
- **Don't** use green, amber or red for decoration, branding, headings or emphasis.
- **Don't** colour a delta by its sign alone: complaints rising are red, and flat or unknown is Pencil Grey.
- **Don't** use emoji or font glyphs (⚠, ✓, ▲ characters) as icons.
- **Don't** add a second typeface, a third weight or italics, or load fonts from a third-party host.
- **Don't** box or round sections, the tape, the board or a sheet; a filled Ledger Band panel (6–8px corners) is only for a single callout inside a report.
- **Don't** add explanatory captions, helper copy or tooltips of prose.
