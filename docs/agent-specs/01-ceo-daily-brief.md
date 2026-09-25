# CEO Daily Brief — the five numbers (A2)

**Code:** `agents/ceo-daily-brief/agent.py`, figures in `integrations/sap/figures.py`
**Schedule:** 08:00 Asia/Tashkent (03:00 UTC), daily, inside `mgmg-morning-agents`
**Mode:** read-only; the only writes are one Telegram message and one `daily_briefs` row
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
| 💰 Касса | — | **Not connected.** The SAP gateway has no cash tool, and the SAP Service Layer was never reachable from Render (its client was removed 2026-09-26). Shown as "уланмаган", never as a number. |
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
- 2026-09-26: A2 restored — the owner's plan specifies these five numbers.

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
