# CEO Daily Brief — the five numbers (A2)

**Code:** `agents/ceo-daily-brief/agent.py`, figures in `integrations/sap/figures.py`
**Schedule:** 08:00 Asia/Tashkent (03:00 UTC), daily, inside `mgmg-morning-agents`
**Mode:** read-only; the only writes are one Telegram photo and one `daily_briefs` row
**Form (2026-10-05):** a designed **picture** with a short caption, not a long text —
`18-reports.md`. The text below is still built: it is stored, read by the
Director's questions, and sent if the picture can't be drawn.
**Source:** A2 "5 рақам дашборди" in the owner's plan (ЭМЖИЕМ AI Агентлар Тизими)

## What the Director gets

    ☀️ CEO кунлик ҳисоботи — 26.09.2026. 08:00 Тошкент

    📊 5 рақам
    💰 Касса: уланмаган
    📈 Кечаги сотув: $12,340.00 (8 та буюртма)
    📦 Захира: камида $120,000.00*
    🧾 Мижоз қарзи: $15,200.00 (кечагига ▲ $800.00), муддати ўтгани $7,384.36 (16 та)
    💳 Бугунги тўловлар: 2 та — 15 000 000 сўм
    * SAP'дан фақат чекланган миқдордаги ёзув келди — рақам тўлиқ эмас.

    🔴 Ҳисобот юбормаганлар (25.09.2026): 1 / 5
       • Алишер Каримов (IT)

The overdue-debt detail by age follows as its own message (receivables).

## Where each number comes from

| Line | Source | Notes |
| ---- | ------ | ----- |
| 💰 Касса | — | **Not connected.** The SAP gateway has no cash tool, and the SAP Service Layer was never reachable from Render (its client was removed 2026-09-25). Shown as "уланмаган", never as a number. |
| 📈 Кечаги сотув | SAP orders (ORDR) pushed by the gateway, `DocDate` = yesterday, cancelled left out | |
| 📦 Захира | SAP stock (OITW) pushed by the gateway: `StockValue`, else `OnHand × AvgPrice` | |
| 🧾 Мижоз қарзи | SAP open invoices (`v_ar_aging_latest`) | Overdue part, and 🔴 when anything is 90+ days late |
| 💳 Бугунги тўловлар | Approved written permissions (B1) whose "Бажариш муддати" is today | The payment gate makes that form the single channel for spending. A date that can't be read (`integrations/common/dates.py` reads only explicit dates and бугун/эртага/N кун) is counted as "сана аниқ эмас", never put on a guessed day. |

**Change since yesterday** is shown when the previous sent brief has the same
number, in the same currency, and neither day's figure is a lower bound.

## Honesty rules

- **Push limits.** Every SAP gateway tool is pulled with a row limit
  (`limit: 100`, products 20 — `scripts/sap-gateway-push/push-ar-aging.ps1`).
  A push that returns exactly the limit probably had more, so the total is a
  lower bound: shown as **"камида"** with the footnote. For invoices the row
  count comes from the push's own audit row
  (`agent_actions.payload.rows_received`), because closed invoices are
  filtered out before they're stored. **To make stock and sales complete, the
  gateway push needs a higher limit or date filtering — check with whoever
  runs the gateway machine.**
- **Stale feeds.** A SAP feed not pushed for more than 3 days reads
  "… дан бери янгиланмаган" instead of a number.
- **SAP gone quiet (2026-10-06).** The gateway pushes every 30 minutes, day
  and night. When nothing has arrived for 3 hours (`SAP_SILENT_HOURS`):
  - the brief opens with «⚠️ SAP 03.10 14:13 дан бери маълумот юбормаяпти —
    сотув, захира ва қарз рақамлари шу вақтга тегишли» (text, picture and
    caption);
  - **Кечаги сотув** reads "SAP маълумоти 03.10 14:13 дан бери янгиланмаган"
    whenever the snapshot was taken before today — yesterday wasn't over (or
    wasn't pushed), so a "0" would be a guess;
  - **Захира** and **Мижоз қарзи** keep their numbers with "(03.10 14:13
    ҳолатида)";
  - the admin gets an Admin Bot message to check the gateway's computer.
  Why: SAP stopped pushing on 03.10 at 14:13 (also 18.09 10:47 → 19.09 and
  24.09 10:17 → 30.09), and the briefs of 04–06.10 showed Saturday's figures
  as today's — "Кечаги сотув: 0" included.
- **Debt as written on the invoices (2026-10-06).** Every SAP invoice is
  written in so'm (`DocCur` = UZS), but SAP's `DocTotal`/`PaidToDate` are its
  local currency, USD — the Director couldn't find "$9,764.31" in SAP and
  asked for so'm. Once the gateway sends `DocTotalFC`/`PaidFC`, **Мижоз
  қарзи** is each invoice's own so'm balance (picture: "124,06 млн сўм");
  until then, SAP's USD as before. The day the basis changes isn't compared
  with yesterday (`basis` in the stored numbers: "local" → "invoice").
- **Unreadable rows.** Rows without the needed SAP columns give
  "SAP маълумоти ўқилмади", not a zero.
- **Failures.** One source failing never stops the brief ("маълумот йўқ" on
  that line); if every source fails, a short failure notice is sent instead.
  Telegram failing → the brief is still stored with status `failed`, exit code 1.

## Daily reports section

Who didn't send OPS Manager Bot their daily report on the last day anyone was
asked ("ҳаммаси юборди (5/5)" when everyone did; "кеча ҳеч кимдан
сўралмаган" when nobody was asked or reports are switched off). Individual
reports never reach the Director.

## Storage

One row in `daily_briefs` per run: `ar_overdue_total_tiyin`, the five numbers
in `sections.a2` (tomorrow's change is computed from it), the exact message
text and any source errors. The `pipeline_*`, `new_leads_24h`,
`deals_without_task` and `cash_total_tiyin` columns stay empty since the CRM
and SAP-cash sections were removed.

## History

- 2026-09-22: cut to reports only, at the business's request.
- 2026-09-25: CRM "Reportlar" section removed — the in-house CRM isn't used.
- 2026-09-25: A2 restored — the owner's plan specifies these five numbers.

## Runbook

```bash
python agents/ceo-daily-brief/agent.py --dry-run   # print with real data, send nothing
python agents/ceo-daily-brief/agent.py            # send it
```

**Exit codes:** 0 sent · 1 built but not sent · 2 refused to run (bot token unset)

**Brief did not arrive:**

```sql
SELECT brief_date, status, source_errors FROM daily_briefs ORDER BY id DESC LIMIT 5;
SELECT occurred_at, target_system, action, error_message
FROM agent_actions WHERE status = 'failure' ORDER BY id DESC LIMIT 20;
```
