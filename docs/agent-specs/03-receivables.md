# Agent 3 — Receivables Agent

**Code:** `agents/receivables/agent.py`
**Schedule:** 09:00 Asia/Tashkent (04:00 UTC), daily — one hour after the CEO brief
**Mode:** fully read-only; the only write is one Telegram message
**Owner:** finance

## Purpose

Turn the AR aging report from something someone has to pull into something that
arrives with a name attached to every number. An aging bucket nobody owns does
not get collected.

## Inputs

`GET /Invoices` on the SAP Service Layer, filtered to
`DocumentStatus eq 'bost_Open' and Cancelled eq 'tNO'`, then aged locally
against today's Tashkent date. Sales employee names come from `/SalesPersons`.

Invoices whose balance (`DocTotal - PaidToDate`) is zero or negative are
dropped — SAP leaves fully-settled invoices open until the period is closed,
and reporting them as receivables would overstate the number.

## Buckets

| Bucket | Days overdue | Severity |
| ------ | ------------ | -------- |
| Not due | ≤ 0 | not reported |
| 1–30 | 1–30 | 🟢 info |
| 31–60 | 31–60 | 🟡 warning |
| 61–90 | 61–90 | 🟡 warning |
| 90+ | 91+ | 🔴 critical |

Boundaries are inclusive at the top: 30 days is `1_30`, 31 days is `31_60`.

## Output

One Telegram message structured as:

1. Headline: total overdue, invoice count, total open AR
2. Each bucket worst-first (90+ → 1–30), top 5 invoices per bucket, remainder
   summarized as `+N more, X mln so'm`
3. **By owner** — total overdue per responsible person, largest first

Every invoice line names: customer, amount, days overdue, invoice number, owner.
Where SAP has no sales employee, the division owner is named; where the division
is unmapped, the line reads `Other` rather than being dropped.

## Database effects

- `ar_aging_snapshots` — one row per open invoice per day (feeds Power BI trends)
- `alerts` — one row **per bucket** with overdue money in it, so the CEO can
  acknowledge the 90+ bucket without dismissing the rest
- `agent_actions` — every SAP call

## Failure behaviour

If SAP cannot be reached the agent sends a 🔴 critical Telegram alert saying so
and exits 2. Silence is never an acceptable outcome for a financial control.

## Runbook

```bash
python agents/receivables/agent.py --dry-run       # print, send nothing
python agents/receivables/agent.py                 # send
python agents/receivables/agent.py --min-days 30   # only 30+ days overdue
```

**Exit codes:** 0 sent · 1 built but not sent · 2 SAP unreachable or `.env` incomplete

**Numbers look wrong:** compare against SAP's own aging report for the same
date, then check what was actually captured:

```sql
SELECT aging_bucket, count(*), sum(balance_due_tiyin)/100 AS uzs
FROM ar_aging_snapshots
WHERE snapshot_date = current_date
GROUP BY aging_bucket ORDER BY 1;
```

The usual cause of a mismatch is multi-currency: this agent ages the document
total in its document currency and does not convert. If foreign-currency
invoices are material, add conversion before trusting the total.

## Verify before go-live

- [ ] `CASH_ACCOUNT_CODES` in `integrations/sap/client.py` lists the real bank G/L codes
- [ ] `DIVISION_BY_SAP_SALESPERSON` in `integrations/common/divisions.py` is filled in
- [ ] One day's output reconciled line-by-line against SAP's aging report
- [ ] Confirmed with finance that "open" in SAP means what the report assumes

## Future

- Per-owner Telegram messages instead of one CEO message
- Payment-promise tracking (customer said they would pay on date X)
- Escalation ladder: 30 d → manager, 60 d → division head, 90 d → CEO + legal
- Multi-currency conversion at the CBU rate
