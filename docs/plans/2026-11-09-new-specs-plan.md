# Plan — the owner documents of 04–07.11.2026 (without HARI)

**HARI (the HR assistant, both HARI documents) is Шахзод's project, hosted on Render as its own Blueprint (10.11.2026, the owner) — not part of this plan or this repo.**

Read in full on 09.11.2026 (each mapped against this repo by a reader agent; summaries, gaps and questions below).
Nothing here is built yet — this is the order of work and the decisions it waits for.

| Document | What the owner asks for | Biggest blockers |
| --- | --- | --- |
| **EMJIEM AI Agents — full list & communication plan** (04.11) | A catalogue, not a build spec: 16 core agents + 24 modules, one staff entry point, one shared task register, four entries for employees (My tasks · Report a problem · Request approval). Start with **MIYA, SCORE, CASH, PROFIT**; switch others on only with data + a human owner + a measurable benefit. | Bank feed, Didox, CRM (AmoCRM removed), WhatsApp provider, LONDRY machine data; personal-data rule. |
| **AI telephone operator** (07.11, 30-day pilot) | Answer every call to 1203333 (Garmin first) 24/7 in Uzbek/Russian, simple questions by itself, transfer the rest, record + transcribe, 3-line summary to a Telegram group, Friday 1-page report. Built in-house (SIP → STT → LLM + answer book → TTS). ≈ $248/month in the doc's example. | SIP trunk from the 1203333 operator; a 24/7 server with public IP (not Render); natural Uzbek TTS on phone audio (riskiest); OpenRouter-only and personal-data rules; budget approval. |
| **AIPRO — procurement agent** | What to buy / not buy, how much, from whom, when — Garmin, Tanita, Primus, parts, detergents, LONDRY supplies — from demand, stock, incoming, commitments, margin, logistics, cash. Code calculates, AI explains; nothing ordered before a person approves exact lines, supplier, terms, max spend. Excel in → 6-sheet workbook out. | Conflicts with the owner's «never advise what to buy» (27.10); SAP write path for draft POs (gateway is read-only); supplier data (MOQ, lead times, Incoterms) has no source; SAP history kept only 3 days. |
| **OMAI — virtual warehouse manager** | Every stock movement tracked (receiving, put-away, transfers, returns, technicians), blind counts on a calendar, variances followed to closure, ageing, safety checks; Otabek and custodians do the physical work. Plans and checks; never writes off or buys. | SAP movement data needs new gateway tools (OPDN, OWTR, OINM…); no SAP write path; scanners/phones; custodians, checker, approvers with limits; baseline full count. |

## Already in place (what each document can reuse)

- **MIYA = this system's task register + duties register** (T-codes, states, proof → acceptance, shared tasks since
  09.11, 08:45 duty cards, 17:30/22:00, plan vs fact, Friday review, PULSE). These documents assume one task
  register — it exists; nobody should build a parallel one.
- **SCORE**: simple KPI, goals (/maqsad), plans vs fact (/reja), Friday scorecard.
- **CASH (part)**: 30-day cash calendar, 1C cash, receivables, SAP ↔ 1C ↔ Billz reconcile, supplier debt compare.
- **Data**: SAP push every 30 min (sales, lines, payments, AP/AR, stock per item/warehouse, products, PO, orders,
  service calls, equipment, contracts), 1C OData, Billz, Verifix; the analyst's **schema-first SQL** (09.11) — every
  new table these agents add gets an `analyst.*` view and becomes answerable at once.
- **Patterns to copy**: approvals + permission flow (written permissions, tiers), director_clarifications (buttons
  for unclear input), complaint tickets (routes.py, reason codes, hourly re-ask), report PDFs (Jinja2 → WeasyPrint),
  signed-link pages (PULSE), group bindings (lead groups), Excel reading (`common/excel.as_text`), crawler + FTS.

## Decisions the owner must make first (they unblock most of the work)

1. **Personal data** (IT instruction rule 4: server in Uzbekistan + masking before a foreign AI) — blocks call
   recordings, customer data in AIPRO/OMAI explanations. The office-server plan discussed on
   05.11 would satisfy the location part.
2. **Relay rule**: allow structured cases («report a problem», warehouse exceptions, missed calls) as a
   third exception to «no text between employees and the Director» — fixed fields, short free text.
3. **Channel**: OMAI inside OPS Manager Bot (recommended: one entry point, one message budget) or a separate bot.
4. **AIPRO vs «never advise what to buy»**: confirm AIPRO as the explicit exception and who sees its advice.
5. **SAP writes** (draft POs, counting documents): only with the gateway owner (Abdulbosit) — until then AIPRO/OMAI
   create tasks for people to post in SAP.
6. **Money** (IT rule 9: > $100 once or > $50/month): SIP trunk + voice server + STT/TTS (phone), scanners (OMAI),
   any paid feed.
7. **OpenRouter-only exception** for speech (STT/TTS) — or open-source voices on our own server.
8. **People**: store custodians / checker / approvers (OMAI), who takes transferred calls and callbacks.
9. **Texts**: approved vision/mission, SOPs, the phone answer book (30 answers).

## Shared foundations (build once, used by several documents)

| # | Foundation | Serves | Effort |
| --- | --- | --- | --- |
| F1 | Task register extensions: priority, «propose new date» (assigner approves), accepting manager, escalation ladder with deputies and time limits, helper on ⛔ | MIYA/FINAL, OMAI | 6–8 days |
| F2 | Generic **case register** (states draft → sent → received → review → decision → done → closed, reject/reopen, correlation id, dedupe, routing) on the complaint-ticket pattern | OMAI exceptions, phone callbacks, «report a problem» | 1–2 weeks |
| F3 | Versioned **knowledge / policy store** with approval (draft vs approved, version, approver, effective date) | AIPRO policy values, OMAI SOPs/safety lists, phone answer book | 1–2 weeks |
| F4 | **SAP history and movements**: keep daily stock_items for months (not 3 days); gateway tools OPDN/PDN1 receipts, OWTR transfers, OINM journal, RDR1 open order lines | AIPRO, OMAI, forecasting | 1–2 weeks + gateway PC |
| F5 | **Message budget**: one counter per person per day across MIYA, OMAI, merged into the one morning message | OMAI (six daily touches), everything | 2–3 days |

## Suggested order

**Phase A — now (no owner blocker):**
1. F1 on the existing register (MIYA/FINAL, «start first» in the catalogue).
2. Phone operator, cheap parts: the owner's letters to the 1203333 operator and one more (SIP terms, price, timing);
   the answer book as a sheet tab read by header (duties.py pattern); on our side a call-event webhook, calls table,
   Telegram group summary, callback ticket — 4–6 days. The voice server waits for the decisions.

**Phase B — after decisions 2, 3, 4, 5, 8:**
3. F2 case register → «report a problem», OMAI exceptions, missed calls.
4. AIPRO stage 1–2 for one supplier and group (Garmin): SKU/supplier map, policy table (F3), stock history (F4),
   Excel intake in the bot, deterministic engine with the doc's 8 acceptance tests, 6-sheet workbook in Uzbek
   Cyrillic, approval bound to exact lines — 5–6 weeks.
5. OMAI pilot on one warehouse and one transfer route: movement register (receiving against po_open, transfers with
   a transit state), blind count cards in Telegram, recount on variance, adjustment request via approvals — 5–7 weeks.

**Phase C — after decisions 1, 6, 7 (hosting, money, speech):**
6. Phone operator voice agent (SIP + STT + LLM + TTS on our own server), 50 test calls, nights/days off first, then
   daytime — ≈ 4 weeks, gated by the operator and the Uzbek voice test.

## Conflicts to keep in mind (from CLAUDE.md decisions)

Uzbek Cyrillic for every bot text (the documents use English/Latin labels and decision codes); polite «сиз» and the
friendly-voice rule (one short sentence, no emoji, ака/опа set by the admin); IT sees no company figures
(procurement and warehouse values stay closed to the admin — counts-only health); one morning message per person
(OMAI's daily touches merge into it); the name «PULSE» is taken by the Boss's dashboard (the catalogue uses it for
employee feedback); Teams / Asana / n8n are not used; «never advise what to buy» (AIPRO needs the owner's exception).

Full per-document summaries (modules, integrations, gaps with file paths, acceptance criteria, all open questions):
`docs/plans/2026-11-09-spec-details.md`.
