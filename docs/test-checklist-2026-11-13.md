# Test checklist — features of 11–13.11.2026

Wait ~3 minutes after a push for Render to redeploy. ✅ = what you should see. Use your own account as the "employee"
(the Director's task picker lists you) so nobody else is disturbed; tell the Boss before steps that need his taps.

## A. Scheduled jobs (no action — just look)

| # | Check | ✅ Expect |
| - | ----- | -------- |
| 1 | 08:00 | the brief arrives once; Render logs show `morning` claimed, each agent once |
| 2 | 16:00 | report ask once; 17:00 reminder only to those asked ≥ 25 min earlier who didn't answer |
| 3 | Render | the 3 old cron services deleted (by hand) |

## B. Daily reports (12.11) — as an employee in OPS Manager Bot

| # | Send | ✅ Expect |
| - | ---- | -------- |
| 1 | 14:00–16:00 (before the ask), a work report as plain text | saved — not lost (this crashed 26.10–12.11) |
| 2 | 14:00–22:00: «бугун мазам йўқ, ишга чиқа олмадим» | «раҳмат, тушунтиришингиз раҳбариятга етказилди»; the Director gets «📝 Тушунтириш хати … ишга чиқмаганини ўзи ёзди» + ⬜ Сабабли. The work AI does **not** answer «Тушундим…» |
| 3 | «кеча ишга чиқмадим» | same, for yesterday's date |
| 4 | Morning after a missed report, afternoon: type today's report (don't tap 📝 Кечаги) | saved as **today's** report, not sent for approval as yesterday's |
| 5 | Tap 📝 Кечаги ҳисоботни юбориш, send a text between 14:00–22:00 | «…тасдиқлаш учун юборилди» with **↩️ Бу бугунги ҳисоботим**; tap it → «… бугунги ҳисоботингиз сифатида сақланди»; the Director gets «↩️ … бугунги ҳисоботи экан» |
| 6 | Any message that needs no help | the work AI gives one useful line, never just repeats it |

## C. Tasks (13.11) — as the Boss, task to yourself (IT)

| # | Do | ✅ Expect |
| - | -- | -------- |
| 1 | Boss writes «Улуғбек, ... ҳисобот тайёрланг, натижа: PDF файл, жума кунигача» | draft card shows «🎯 Кутилган натижа: …» with a row to drop it; P1–P4 row; no 🔎 row (IT has no department manager) |
| 2 | Task to a Garmin сотув person only | draft shows «🔎 Текширади:» row offering the Garmin manager; pick him → his card shows the proof when ✅ is sent |
| 3 | Same task to Garmin сотув **and** someone else | no 🔎 row |
| 4 | Boss writes «T-2026-11-00NN тугагач, …» (an open task code) | new card: «⏳ T-… кутилмоқда — у қабул қилингач бошланг»; no reminders/overdue; when that task is accepted → «⏳ … энди бошланади — … қабул қилинди» |
| 5 | Employee presses ▶️ on **someone else's** task (forwarded card) | nothing happens — only the assignee can start |
| 6 | Employee ✅ + proof; Boss presses ↩️ Қайтариш | four reasons: Далил етарли эмас / Сифати талабга жавоб бермайди / Натижа бошқа / Бошқа сабаб; employee told the reason; Friday scorecard shows «Қайта ишлаш» |
| 7 | Task due on Saturday/Sunday | reminded in Friday's morning message; Monday-due task still gets its «бугун» line on Monday |
| 8 | Pressed ✅ (proof waiting) on an overdue task | next morning: proof line only, no overdue line |

## D. Escalation + deputies — Admin Bot and next mornings

| # | Do | ✅ Expect |
| - | -- | -------- |
| 1 | Admin `/xodimlar` → the Garmin manager's card | «Ўринбосар: йўқ» + 👥 Ўринбосар |
| 2 | 👥 → pick a Garmin сотув person | «👥 Ўринбосар: … Директорга хабар берилди.»; the Director is told in OPS Manager Bot |
| 3 | ❌ Ўринбосарсиз | removed, the Director told |
| 4 | A Garmin task goes overdue | 08:00 same day: the Garmin manager gets the list (or his deputy, if he is off / excused) |
| 5 | A task blocked (⛔) 2 working days | the department manager is told «тўсиқда»; after 4 working days the Director — only if he didn't give the task |
| 6 | Admin `/texnik` | escalation counts only, no task codes or text |

## E. Switched off (don't test until the owner decides)

- ⛔ → helper task (`TASK_HELPER_ENABLED=false`).
- AI check of proof vs expected result (`TASK_PROOF_HINT_ENABLED=false`).
