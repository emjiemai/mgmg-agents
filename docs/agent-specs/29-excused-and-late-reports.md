# 29 — Excused days and late reports (2026-10-23)

**Asked:** the Boss, on Ширин Умматова's explanation of 22.10 («День Рождения, я заранее отпросилась»): "set for her
grey, and find a way to set employees grey from the Garmin manager and the Boss when they have a reason to not come".
Then: "when they forget to send the report, from yesterday's, their report goes to the Garmin manager or the Boss asking
to approve or decline yesterday's report; it will be set to green when approved."

**Code:** `integrations/org_bot/excused.py`, `integrations/org_bot/late_report.py`, `attendance.excuse`
(`integrations/verifix/attendance.py`), store `excuse_day` / `unexcuse_day` / `excused_between` and the late-report
functions; table `excused_days`; `daily_reports.late_*` columns.

## Excused days (grey)

Who: the Director (anyone scored) and the Garmin manager (Garmin сотув only). Never the managers themselves.

- **The explanation card** (spec 27) carries **⬜ Сабабли (кулранг қилиш)** — every day the card is about is
  excused (the person's explanation is kept as the reason); the button turns into **↩️ Сабаблини бекор қилиш**.
- **/sababli** (both menus): pick a person, then tap days — two weeks back to two weeks ahead (a day off asked for
  in advance). ⬜ = excused; tapped again it comes back.

An excused day:
- Verifix absent / late / not yet → **excused** «раҳбар рухсати билан» (`attendance.load` applies it, so the brief,
  KPI, PULSE and the morning «why absent?» ask all see it);
- an unsent report ask that day is taken out with its 22:00 minus (kept in `excused_days.report_row`; ↩ puts it
  back as it was) → the report calendar shows grey; no report is asked on an excused day;
- when the Garmin manager sets or clears one, the Director is told. PULSE's kept data is cleared at once.

## Late reports (green when approved)

- The morning «кечаги ҳисоботингиз келмади» ask carries **📝 Кечаги ҳисоботни юбориш** (yesterday only).
- Tapped, the person's next text within 12 hours is that report. It also answers the morning ask (no separate
  explanation card). The person is thanked.
- It goes to the Director and the person's team manager as «🕘 Кечиккан ҳисобот» with **✅ Тасдиқлаш / ❌ Рад этиш**.
  Another employee or a manager outside the team can't decide; the first decision wins.
- **Approved:** the report counts as sent — status submitted, its text, the minus point removed → green in the
  calendar. **Declined:** it stays missed (red, minus kept). The person is told politely either way; the Director is
  told when the Garmin manager decided. No report bonus for a late report (the bonus is settled at 08:00).

## Tests

`scripts/selfcheck.py`, `test_excused_and_late_reports`: who may excuse / decide, Verifix days turned excused, the
store's guards (only unsent asks, first decision wins), /sababli's buttons, both flows end to end with a fake bot,
the friendly voice of every message, the wiring (buttons, menus, no ask on an excused day).
