# Test checklist — questions for OPS Manager Bot, features of 14–20.11.2026

Wait ~3 minutes after a push for Render to redeploy. Type the questions in words, as written (Latin Uzbek works too).
✅ = what you should see. Nothing here sends anything to other employees unless the row says so. While the analyst
works, the «⏳ …» line under the question changes; it is deleted before the answer.

## A. 1C and the night fixes (20.11) — as the Director

| # | Ask | ✅ Expect |
| - | --- | -------- |
| 1 | «1С'дан тўлиқ ҳисобот» | bank / cash, receivables, payables, the largest debtors and creditors **and** turnover (90), expenses (94), taxes (64) — within ~4 min. If one part is missing, the answer names it (no silence). |
| 2 | Same question again within 30 min | noticeably faster (the chart and name lists are kept) |
| 3 | «<мижоз номи> бизга қанча қарз — 1С ва SAP'да» | both systems, each labelled; never «қайси тизим?» |
| 4 | «<поставщик номи>га қарзимиз 1С ва SAP'да солиштир, excel» | a short comparison + the `.xlsx` workbook |
| 5 | «1С бўйича ҳозир банкда қанча пул бор» | class-5000 balances, bank and cash separately |
| 6 | Send two heavy questions one right after the other (e.g. #1 and «ноябр сотувини Billz ва SAP'дан солиштир») from both Director accounts | at most two run at once; a third gets «Олдинги саволлар жавоб топилмоқда, навбатда турибди…» and still gets its answer |
| 7 | Admin Bot `/texnik` the next morning | no Verifix failure every 9 minutes (one try per 30 min while «Нет лицензии»); 1C reads without 429 |

## B. Screens asked in words (19.11) — as the Director

The ☰ menu shows **/pulse only**. Each of these should send exactly what the old command sent.

| # | Ask | ✅ Expect (old command) |
| - | --- | -------- |
| 1 | «ходимлар KPI жадвалини кўрсат» | the KPI table (`/kpi`) |
| 2 | «ҳисоботлар календарини юбор» | the calendar picture (`/hisobotlar`) |
| 3 | «ойлик мақсадлар қандай кетяпти» | goals and results (`/maqsadlar`) |
| 4 | «Шахзодга ойлик мақсад қўйиш» | the goal card for that person (`/maqsad`); a name not found → the picker |
| 5 | «Абдурашиднинг кечаги кунини сабабли қил» | his 14-day grid with buttons — **nothing changes until you tap** |
| 6 | «B2B лидлар статистикаси» | lead statistics with ⏸ / ▶️ (`/lidlar`) |
| 7 | «SAP, 1С ва Billz фарқлари Excel файлини юбор» | «солиштирилмоқда…», then the message + Excel (`/farqlar`) |
| 8 | «вазифалар реестрини кўрсат» | the register card with ⏸ / ▶️ (`/vazifalar`) |
| 9 | «бу ой режа ва факт» | plans vs fact (`/reja`) |
| 10 | «SAP'га ким киритмаяпти» | the «who didn't enter SAP» list (`/kiritish`) |
| 11 | «ҳафталик таҳлилни ҳозир қил» | «Ҳафта таҳлили тайёрланмоқда…», then the review (`/tahlil`) |
| 12 | «Garmin қолдиғини Excel'да бер» | the stock workbook (`/qoldiq`) |
| 13 | «бир йилдан ошган эски товарлар» | the old-stock PDF (`/eski`) |
| 14 | «баҳолаш карталарини юбор» | «Ҳозир оддий KPI ишлатилади…» (simple KPI mode) |
| 15 | «PULSE'ни оч» | the PULSE link |
| 16 | One message asking for three things (e.g. «KPI, календарь ва режа-факт») | all three arrive after the answer (at most three per answer) |

## C. As the Garmin manager (19.11)

His ☰ menu is /pulse only; screens are his team's.

| # | Ask | ✅ Expect |
| - | --- | -------- |
| 1 | «жамоа KPI жадвали» | Garmin сотув team only |
| 2 | «жамоа ҳисоботлари календари» | his team's calendar |
| 3 | «рақобатчилар Garmin'ни қанчага сотяпти» | «OLX ва бошқа сайтларни текширяпман…», then the competitors list (no private sellers named) |
| 4 | «Garmin қолдиғи» / «эски товарлар» | the stock Excel / the old-stock PDF for Garmin warehouses only |
| 5 | «жамоамдаги ходимга мақсад қўйиш» | the picker lists his team only |
| 6 | «ходимлар KPI'и» about someone outside his team | refused / only his team |
| 7 | After 08:00 | his brief, then his team's calendar picture |

## D. The Boss's morning package (19.11) — look at 08:00–08:10, no action

| # | ✅ Expect |
| - | -------- |
| 1 | The brief picture **without** «Ойлик мақсадлар» and «Топшириқлар» |
| 2 | The report calendar picture |
| 3 | Yesterday's sales: SAP invoices by salesperson (customer, product × pieces, so'm), Billz cheques by seller |
| 4 | Yesterday's reports in full |
| 5 | What everyone did + what the bot itself did |
| 6 | Arrives **once** (both Director accounts) — not again on a redeploy |

## E. Analyst data questions (14–18.11) — as the Director

| # | Ask | ✅ Expect |
| - | --- | -------- |
| 1 | «16 ноябрда омборда <товар номи> нечта эди» | the stock on that day (`stock_history` starts 16.11 — earlier days: «маълумот йўқ») |
| 2 | «шу ой поставщиклардан қайси товарлар келди» | goods receipts (OPDN) by item / supplier |
| 3 | «омборлар орасида шу ҳафта нима кўчирилди» | transfers (OWTR) |
| 4 | «очиқ буюртмалар қандай» | open sales-order lines |
| 5 | «кечаги сотувни PDF қилиб бер» | a designed PDF document |
| 6 | «ноябр сотувини кунлар бўйича графикда кўрсат» | a chart sent as a photo |
| 7 | «сотув жараёнининг схемасини чиз» | a diagram sent as a photo |
| 8 | «шу ой ким энг кўп сотди» | Billz sellers **and** SAP salespeople, each labelled |
| 9 | A question it can't answer from data | «билмайман» / what is missing — never a guess |

## F. Tasks and the AI checker (14–18.11) — task to yourself (IT)

| # | Do | ✅ Expect |
| - | -- | -------- |
| 1 | Boss writes a task as a list («1. Primus 2. Garmin 3. Londry …») | the bot reads it (17.11 rich messages) — never silent |
| 2 | You ✅, send a **Word or Excel** file as proof | the AI checker decides; a note under the Boss's card with the reason + ↩️ «AI қарорини бекор қилиш» |
| 3 | Boss taps ↩️ | «↩️ AI текширувчи қарори бекор қилинди…» — the card's buttons are his again |
| 4 | One task to you + one more person; only you send proof | only **your** copy closes; the other stays open, nobody is told «… бажарди» |
| 5 | A very long task text | the text above, the card with buttons below; ▶️ changes the buttons |
| 6 | Admin Bot `/aicheck sinov` | seven samples judged, results to IT, nothing changed |

## G. As an employee (your own account)

| # | Send | ✅ Expect |
| - | ---- | -------- |
| 1 | «шу йўриқномани Word қилиб беринг: …» (write a few lines) | a Word file made only from what you wrote |
| 2 | «режамни PDF қилинг: …» | a PDF |
| 3 | A message written with a list | the bot answers it (not silent) |
| 4 | 08:00 on a holiday / birthday from the sheet tabs «Байрамлар» / «Туғилган кунлар» | one greeting line, once |
