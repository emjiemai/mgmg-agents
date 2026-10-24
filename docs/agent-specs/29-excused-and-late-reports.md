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
- an unsent report ask that day turns **status `excused`** with its 22:00 minus set to 0 (2026-10-24 — until then
  the row was deleted; nothing is lost now, its text stays; the old minus is kept in `excused_days.report_row`,
  ↩ puts it back as it was, and a mark made on 23.10 restores the deleted row from its copy) → the report calendar
  shows grey; every count and list of reports leaves `excused` rows out (KPI, the weekly scorecard, the brief,
  the bonus, PULSE, the Director's and the Garmin manager's report questions); no report is asked on an excused day;
- when the Garmin manager sets or clears one, the Director is told. PULSE's kept data is cleared at once.

## Late reports (green when approved)

- The morning «кечаги ҳисоботингиз келмади» question (in the one morning message, spec 30) carries **📝 Кечаги
  ҳисоботни юбориш** (yesterday only).
- Tapped, the person's next message within **3 hours** (12 until 2026-10-24 — it could take today's evening report)
  is that report: typed; a voice / video turned into text and sent with the file; a photo / document sent as it is
  with its caption. A reply to today's 16:00 ask is never taken for it. It also answers the morning question (no
  separate explanation card). The person is thanked.
- **Without the tap** (2026-10-24): an answer to the question that IS the report (what they did yesterday, not why
  it didn't come) is recognised (reason / report / other) and goes the same way for approval.
- It goes to the Director and the person's team manager as «🕘 Кечиккан ҳисобот» with **✅ Тасдиқлаш / ❌ Рад этиш**.
  Another employee or a manager outside the team can't decide; the first decision wins.
- **Approved:** the report counts as sent — status submitted, its text, the minus point removed → green in the
  calendar. **Declined:** it stays missed (red, minus kept). The person is told politely either way; the Director is
  told when the Garmin manager decided. No report bonus for a late report (the bonus is settled at 08:00).

## Tests

`scripts/selfcheck.py`, `test_excused_and_late_reports` and `test_morning_verifix_and_admin_sheet` (24.10): who may excuse / decide, Verifix days turned excused, the
store's guards (only unsent asks, first decision wins), /sababli's buttons, both flows end to end with a fake bot,
the friendly voice of every message, the wiring (buttons, menus, no ask on an excused day).

## The admin's report sheet (2026-10-24, the owner)

"Give me access to edit reports and set grey, red or green to employees as Admin." Admin Bot `/xodimlar` → the
person's card → **📊 Ҳисобот варағи (ой)** (`integrations/org_bot/report_sheet.py`) — for people asked for daily
reports. The month as a calendar of buttons (Monday first; ◀ ▶ months, never past this month):

- a tap moves a day on: ▫️ not asked / ⬜ excused → **🟩 sent** → **🟥 not sent** → **⬜ excused** → 🟩 …;
  a day still to come: blank ↔ ⬜ excused in advance; today before 22:00 a 🟥 is still owed (🟨) and closes at 22:00;
- 🟩 = the report counts as sent, minus 0 (a day with no row gets «[админ: юборилган деб белгиланди]»; a row's own
  text is kept); 🟥 = −1 KPI once the day is closed (from 16.10); ⬜ = the same as the Director's ⬜ Сабабли;
- every change is kept in `report_cell_edits`; **✅ Тайёр** sends each active Director one summary (per person, per
  day first → last; a day turned all the way back isn't listed); what is left open reaches him at 22:00 (the API's
  5-minute loop). The employee isn't messaged. PULSE's kept data is cleared at once.

Also fixed on 24.10: since 17.10 the Garmin shop staff's card showed only their week — ака/опа, AI, 📝 daily report,
AI language and data access sat under an `elif`; every non-Director card has them again.