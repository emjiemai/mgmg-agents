# Agent 2 — amoCRM Follow-up Agent

**Code:** `agents/amocrm-followup/agent.py`
**Trigger:** amoCRM webhook (real time) + nightly sweep
**Mode:** writes to amoCRM and Planner — **gated by `AGENT_WRITES_ENABLED`**
**Owner:** sales operations

## Purpose

A deal with no scheduled next action is a deal that quietly dies. This agent
catches that within minutes of it happening instead of at the next pipeline
review.

## Trigger path

```
amoCRM deal event
   → POST /webhooks/amocrm/{secret}          (FastAPI, integrations/amocrm/webhook_handler.py)
   → row written to amocrm_deal_events        (persist first, always)
   → 200 returned to amoCRM immediately
   → background: process_lead(lead_id)
```

Persisting before processing is deliberate: amoCRM disables a webhook after
repeated non-2xx replies, and a stored event can always be replayed with
`--drain`.

## Decision logic

For each deal event:

| Condition | Action |
| --------- | ------ |
| Deal not found (deleted) | `skipped_not_found` |
| Deal in stage 142 (won) or 143 (lost) | `skipped_closed` |
| Deal has `closest_task_at` set | `skipped_has_task` |
| Otherwise | **create follow-up** |

Creating a follow-up means three writes, in this order:

1. **amoCRM task** on the deal, due in 24 h, assigned to the deal's responsible user
2. **Planner task** in `MS_PLANNER_DEFAULT_PLAN_ID` — Planner is the company's
   single source of truth for tasks
3. **Teams message** to the sales channel naming the manager, deal, value and stage

Steps 2 and 3 are best-effort: if Planner or Teams is unavailable the amoCRM
task still stands and the failure is logged, because a partial follow-up beats
no follow-up.

## Write gate

While `AGENT_WRITES_ENABLED=false` (the default for the first 90 days) every
write is computed in full, logged, and audited with status `dry_run` — nothing
leaves the VPS. Review a week of dry-run rows before opening the gate:

```sql
SELECT occurred_at, target_system, action, payload
FROM agent_actions
WHERE agent = 'amocrm-followup' AND status = 'dry_run'
ORDER BY id DESC LIMIT 50;
```

## Run modes

```bash
python agents/amocrm-followup/agent.py --lead 12345   # one deal
python agents/amocrm-followup/agent.py --sweep        # every stalled deal (nightly safety net)
python agents/amocrm-followup/agent.py --drain        # replay pending webhook events
```

The nightly sweep exists because webhooks are not guaranteed: if the VPS was
down for an hour, those events are simply gone from amoCRM's side.

## Webhook setup in amoCRM

Settings → Integrations → Webhooks. URL:

```
https://<your-host>/webhooks/amocrm/<AMOCRM_WEBHOOK_SECRET>
```

Subscribe to: **Deal added**, **Deal status changed**, **Deal edited**.

amoCRM does not sign webhooks, so the secret in the path is the authentication.
Treat that URL as a credential.

## Rate limits

amoCRM allows 7 requests/second per account and blocks integrations that exceed
it. The client paces every call through a shared limiter (`AMOCRM_MAX_RPS`)
rather than relying on 429 retries.

## Known constraint — Teams messaging

Posting a channel message with **application** permissions
(`ChannelMessage.Send`) is a protected Graph API that Microsoft grants only on
request. The agent therefore posts through an **Incoming Webhook** on the sales
channel (`TEAMS_SALES_WEBHOOK_URL`), which needs no tenant approval. If that
setting is empty the Teams ping is skipped and logged; everything else runs.

## Future

- Escalate to the division head if the follow-up task is still open after 48 h
- Stage-specific task text (a "Proposal sent" deal needs a different nudge)
- Feed stalled-deal counts per manager into the KPI review
