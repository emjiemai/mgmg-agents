---
version: 1
slug: "integrations-api-static-pulse-html"
primary_target: "integrations/api/static/pulse.html"
related_targets: ["integrations/api/pulse_page.py"]
---

# PULSE — surface brief

Scope: /pulse (integrations/api/pulse_page.py, integrations/api/static/pulse.*). Mode: Operate.
Audience: the Director (everyone) and the Garmin manager (his team); phone and desktop equally.
Job: see at a glance how the company is doing, then who is driving it, then what one person did today.
Constraints: Uzbek Cyrillic, minimal text (the owner: "please don't add more text"); read-only; inherits
the reports' world (Noto Sans, ink on white with hairline rules, red/amber/green only as state).
Chosen: "Chart and tape" (decision round 16c4c037).

## Direction contract

THESIS: A trading screen for a company: the pulse line with its sales volume, today's actions printed on a time-and-sales tape beside it, the people as a ranked board. Refuses the card-grid dashboard of equal tiles.

OWN-WORLD: The morning brief's paper: white ground, near-black ink, 1px rules, Noto Sans with tabular figures; green ▲ / red ▼ only as price direction and state; dark mode is the same system on a graphite ground. No shadows, no gradients, no rounded card soup — rules and alignment carry structure.

STORY: The Boss opens the link, reads the index number and its arrow first, glances at the six tickers, watches the tape for who did what today, scans the ranked board, taps a person to see their line and their day.

FIRST VIEWPORT: Top bar: PULSE, the time, 7/30/90 segmented switch. Row of six tickers (label, value, ▲▼, sparkline). Left two-thirds: the index number large, the pulse line over sales volume bars (TradingView). Right third: the tape, newest first, time · name · action. Phone: tickers scroll horizontally, chart full width, tape below it, then the board.

FORM: Chart and tape — position 6 on my ordered list of seven; seed key 55aa170c.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
