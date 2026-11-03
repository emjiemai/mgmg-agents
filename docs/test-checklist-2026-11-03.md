# Test checklist — features of 28.10–03.11.2026

Wait ~3 minutes after a push for Render to redeploy. ✅ = what you should see. Nothing here sends anything to other
employees, except the last step (▶️), which is the real start.

## A. As IT — Admin Bot

| # | Send | ✅ Expect |
| - | ---- | -------- |
| 1 | `/vazifalar` | «Вазифалар реестри — манба: …», counts, «Агентга уланиш» (11 of 13 in the bot; Рушан, Жамолиддин not; Умар Хошимов no duties), a ▶️ button. If the sheet can't be read: «❌ Google жавоби: …» with the reason — share the sheet as **Editor** with the address shown, check `DUTIES_SHEET_ID`. **Don't press ▶️ yet.** |
| 2 | `/vazifalar sinov` | Your own card for today arrives in **OPS Manager Bot**; Admin Bot gets «🧪 Синов» + the 08:45 IT check as it would read. Nobody else gets anything. |
| 3 | In OPS Manager, on that card: **1 ✅** | the line turns ✅, its buttons go |
| 4 | **2 ⛔**, then type one sentence | the bot asks why → «Раҳмат, сабабини ёзиб қўйдим» |
| 5 | **3 👎** | «фойдасиз деб белгиланди», 👎 on the line |
| 6 | OPS Manager `/vazifalar` | your own duties (the register's lines) |
| 7 | Admin `/lidlar` | B2B leads on / off only — no names or sums |
| 8 | Admin `/texnik` | lines for `lead-crawler`, `lead-web-search`, `reconcile`, `duties` with counts |

## B. As the Boss — OPS Manager Bot

| # | Send | ✅ Expect |
| - | ---- | -------- |
| 1 | `/farqlar` | «солиштирилмоқда…», then a message (count per section) + an Excel file (8 sheets) |
| 2 | `/reja` | each monthly plan: fact / plan, %, 🟢 / 🔴 against the month's pace; service contracts and chemicals «алоҳида ҳисобланмайди» |
| 3 | `/kiritish` | «SAP'га ким киритмади»: shop cheques not in SAP / late / another amount by seller; late SAP invoices by salesperson; invoices without a salesperson |
| 4 | `/vazifalar` | the register by person, the monthly plans, today's progress, «Агентга уланиш» + acceptance line, ⏸ / ▶️ |
| 5 | `/tahlil` | the week's review so far (before cards start: «реестрдан вазифа берилмади»), plans, connection |
| 6 | `/lidlar` → 📄 | lead statistics, then the PDF table |
| 7 | `/pulse` → open | the «Режа ва факт» and «Лидлар» cards at the bottom |

## C. On schedule (check the next day)

- **09:00** Зайнабхон's discrepancy report (went out 03.11 at 09:04 ✅).
- **15:00** B2B lead questions — 03.11: 7 asked, 0 answered (02.11: 0 of 5) → remind Абдукарим and Ғиёсиддин.
- **Friday 16:30** the review (only while the daily cards are on).

## D. Start for everyone

When A and B pass: **▶️** on `/vazifalar` (either account; the other side is told). Next working day 08:45 everyone
with duties gets their card; at 08:50 Admin Bot gets the IT check; 17:30 the reminder; 22:00 the close.

## E. By hand on the gateway computer

Copy `sap-gateway/src/sales-by-date.js` and restart `npm start` — then `/kiritish` names the SAP user who typed an
invoice («Гармин (филиал Абай)») instead of «SAP фойдаланувчи №10».
