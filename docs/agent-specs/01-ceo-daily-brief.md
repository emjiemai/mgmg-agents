# CEO Daily Brief

**Code:** `agents/ceo-daily-brief/agent.py`
**Schedule:** 08:00 Asia/Tashkent (03:00 UTC), daily, inside `mgmg-morning-agents`
**Mode:** read-only from every source; the only write is one Telegram message
**Owner:** automation team

## Purpose

One short Telegram message every morning, in Uzbek Cyrillic like every bot
message:

    ☀️ CEO кунлик ҳисоботи — 25.09.2026. 08:00 Тошкент

    🔴 Ҳисобот юбормаганлар (24.09.2026): 1 / 5
       • Алишер Каримов (IT)

— who didn't send OPS Manager Bot their daily report on the last day they
were asked ("ҳаммаси юборди (5/5)" when everyone did, "кеча ҳеч кимдан
сўралмаган" when nobody was asked).

History of what was cut, at the business's request:
- 2026-09-22: cash (never connected), the receivables headline (the
  Receivables alert follows as its own message) and the CRM pipeline.
- 2026-09-25: the CRM "Reportlar" section — the business doesn't use the
  in-house CRM, so the brief no longer reads it at all.

Cash and receivables are still collected and stored in `daily_briefs` (the
bot's "Kunlik brif tarixi" answers come from there); they're just not shown.

## Inputs

| Source | Data | Method |
| ------ | ---- | ------ |
| SAP B1 | Cash/bank G/L balances | `GET /ChartOfAccounts` |
| SAP B1 | Open A/R invoices, aged | pushed from the SAP gateway's machine (`/webhooks/sap-push`), read from `v_ar_aging_latest` |
| OPS Manager Bot | Who was asked for a daily report and who answered | `daily_reports`, last asked day |

Migrated off amoCRM on 2026-08-18 — MGMG built its own sales CRM
(`sales-crm-roan-six.vercel.app`), a read-only-by-design API (the issued key
has no write scope at all). amoCRM, Verifix attendance and Microsoft Planner
were removed from the project entirely on 2026-09-15.

## Outputs

- One Telegram message via OPS Manager Bot to whoever holds the Director role
- One row in `daily_briefs` (headline figures, full JSON payload, exact message text)
- Snapshot rows in `cash_balance_snapshots`, `amocrm_pipeline_snapshots` (legacy
  table name — the in-house CRM writes here), `crm_stats_snapshots`, and
  `crm_employee_reports`
- Audit rows in `agent_actions` for every API call

## Severity markers

| Marker | Meaning | Triggered by |
| ------ | ------- | ------------ |
| 🔴 | Act today | any 90+ day receivable, negative cash account, >10 stalled deals |
| 🟡 | Watch | any overdue receivable under 90 days, 1–10 stalled deals |
| 🟢 | Fine | nothing outstanding in that section |

## Failure behaviour

**The brief always goes out.** Sources are fetched concurrently and independently:

- One source fails → that section reads `⚠️ <system> unavailable`, the footer
  names the failed systems, and the error is stored in `daily_briefs.source_errors`.
- All three fail (SAP cash, SAP aging, daily reports) → a short failure notice is sent
  instead of a brief, so silence is never mistaken for good news.
- Telegram itself fails → the brief is still written to `daily_briefs` with
  status `failed`, and the exit code is 1 so cron surfaces it.

## Configuration

| Setting | Effect |
| ------- | ------ |
| `DAILY_BRIEF_HOUR_LOCAL` | Documentation only — the actual time comes from cron |
| `DRY_RUN=true` | Print the message instead of sending |

## Runbook

```bash
python agents/ceo-daily-brief/agent.py --dry-run   # see today's brief, send nothing
python agents/ceo-daily-brief/agent.py            # send it
```

**Exit codes:** 0 sent · 1 built but not sent · 2 refused to run (unfilled `.env`)

**Brief did not arrive:**

```sql
SELECT brief_date, status, source_errors FROM daily_briefs ORDER BY id DESC LIMIT 5;
SELECT occurred_at, target_system, action, error_message
FROM agent_actions WHERE status = 'failure' ORDER BY id DESC LIMIT 20;
```

## Future

- Per-division briefs to division heads (same data, filtered by `division`)
- Day-over-day deltas on cash and overdue AR
- Bank balances from Kapital/Asia Alliance APIs instead of SAP-only
