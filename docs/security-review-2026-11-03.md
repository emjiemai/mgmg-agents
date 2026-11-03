# Security and bug review — features of 28.10–03.11.2026

Reviewed: the B2B lead tracker, the lead index (crawler, DeepSeek), the local database copy, the discrepancy report,
the duties register, the daily tasks, plan vs fact, the Friday review, the rollout, `/kiritish`, the leads sheet, the
lead channels, the Sheets client, PULSE's new cards, the simple KPI.

Checked and fine: every SQL value bound (the three string-built queries join fixed text only); every button checks
who pressed it (a person's own card / question; the Director for switches, plans and suggestions; the admin id on
Admin Bot); PULSE's new endpoints 404 for anyone but the Director, and the page writes text only (no innerHTML);
production is read in READ ONLY sessions by the discrepancy report's 1C/SAP readers and the local copy; no secret in
any commit (scanned).

## Fixed

| # | Problem | Fix |
| - | ------- | --- |
| 1 | IT could see company figures on `/db` and in the local copy: B2B sale amounts and what was sold, typed lead answers, the work profiles' money targets — and find them through the search box | `db_viewer.HIDDEN_COLUMNS`: shown as «•••», left out of search and its count, never copied to the local database (and blanked there) |
| 2 | The Friday review's IT copy, an accepted suggestion and the register's «rows to fix» could carry dollar targets | `tone.mask_money` on everything sent to IT |
| 3 | Excel formula injection: a SAP / 1C / product name starting with «=» became a live formula | `common/excel.as_text` on every workbook (discrepancy report, supplier debt, Garmin stock) |
| 4 | A forged button value on a duty card closed the task as «not done» | only ✅ / ⛔ / 👎 accepted; suggestions only ✅ / ❌ |
| 5 | A daily report sent within 3 hours of ✅ / ⛔ on a duty, or of «Сотилди» on a lead, was taken as the duty's number, the reason or a sale | the answer counts when it is a reply to the question, or soon (30 min) and short (a number; a sale ≤ 80, a reason ≤ 160 characters; never a text saying «ҳисобот») |
| 6 | A renamed header or an emptied «Вазифалар» tab would have switched every duty off | the register as last read stays, the reason is told; a person with no duties left loses the register's profile |
| 7 | The Friday review counted a week's / month's task not yet due as «not done» | only answered or ended tasks are judged |
| 8 | `/reja` broke on a plan name with «<» or «&» from the sheet | escaped |
| 9 | A huge news feed could fill the 512 MB server | pages read in chunks, refused past 5 MB |
| 10 | PULSE's «Режа» card and `/reja` rewrote ~130 SAP lines on every refresh (the 0.1-CPU database) | at most every 10 minutes; hourly from the API's round |
| 11 | A double tap on `/lidlar arxiv` ✅ could start the archive twice | the buttons come off before it runs |
