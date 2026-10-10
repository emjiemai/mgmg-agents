# The owner's IT instruction (08.10–31.12.2026) — what exists, what's next

Source: «IT менежер учун йўриқнома: AI агентлар ва автоматлаштириш» from the
owner (in force 08.10.2026, 12 weeks to 31.12), received 10.10. This page maps
it to this system; the instruction itself stays with the owner. Statuses as of
10.10.2026. ✅ done · 🟡 part · ⬜ not started · ✋ by hand (not code) ·
❓ needs the owner's decision.

## Phases and their acceptance test

| Phase | Dates | Accepted when | Status |
| ---- | ---- | ---- | ---- |
| Ф0 Security | 08.10–24.10 | IT-01 register 100 %; the owner logged in to every system himself; restore test passed | ⬜ (mostly ✋) |
| Ф1 Foundation | 15.10–14.11 | server, automation, knowledge base, bot v1: ≥ 15 of 20 test questions right | 🟡 bot runs; knowledge base and the 20-question test ⬜ |
| Ф2 Automation | 01.11–05.12 | А1–А8 each passed its test 3 times, its human owner signed | ⬜ |
| Ф3 AI agents | 21.11–31.12 | PULSE on time 10 working days; another person restored from the IT book | 🟡 (the 08:00 brief exists) |

## Ф0 — first two weeks

| № | Task | Due | Status | What the system already gives |
| ---- | ---- | ---- | ---- | ---- |
| IT-1 | Take over access from Abdulbosit (18 systems); owner = main admin | 17.10 | ✋ ⬜ | This system's accounts are in `docs/links.md` §5–6: Render, GitHub (emjiemai), BotFather, OpenRouter, Google service account, BILLZ, Verifix, 1C Clobus user — and **the SAP gateway computer** (its `.env`, `npm start`, the Windows task "MGMG SAP push"), which Abdulbosit runs today. Render ownership is already open in `access-review-2026-10-07.md` §5. |
| IT-2 | Password vault + MFA | 17.10 | ✋ ⬜ | The system's own secrets live only in Render (`mgmg-shared`, `sync: false`); add MFA on Render, GitHub, OpenRouter, Google, the bot owner's Telegram. |
| IT-3 | LONDRY systems (app admin, payment cabinet, token-machine codes, vendor contacts, machine list) | 15.10 | ✋ ⬜ | — (LONDRY isn't connected to anything yet) |
| IT-4 | LONDRY software videos L-07…L-09 | 15.10 | ✋ ⬜ | — |
| IT-5 | Backups 3-2-1 + restore test (SAP, 1C, M365, cameras, bot) | 24.10 | 🟡 | **The bot's database: code done 2026-10-10** — encrypted daily copy on an office computer + second disk, Render's own 3–7-day recovery, restore-test script that writes the report (`scripts/backup/README.md`). Still by hand: the owner's key, two Render settings, the office computer, the restore test with the owner. SAP (with Altitude), 1C (with Aspect), M365 and cameras: ✋ with their vendors. |
| IT-6 | Corporate WhatsApp/Telegram for GARMIN, PRIMUS, LONDRY | 24.10 | ✋ ⬜ | — |
| IT-7 | IT book v0 (systems, admins, where, how to restore) | 24.10 | 🟡 | `README.md` (handover guide), `docs/links.md` (every link), `docs/access-review-2026-10-07.md` (who has what), `docs/agents-status.md`. Still to write: the systems outside this repo, and it must live in Microsoft 365. |

## The 10 rules against this system

| Rule | Status |
| ---- | ---- |
| 1. Owner = main admin, IT = 2nd admin | ⬜ Render, GitHub, BotFather still under IT's accounts — IT-1 |
| 2. MFA everywhere | ✋ ⬜ IT-2 |
| 3. Passwords in a vault | ✋ ⬜ IT-2 (the system's secrets: Render only) |
| 4. **Personal data on a server in Uzbekistan (ЗРУ-547); names and numbers hidden before a foreign AI** | ❓ **Not met.** The database is on Render (no region set in `render.yaml` = Render's default, outside Uzbekistan): employees' names and Telegram ids, attendance, complaint phones. The AI (OpenRouter) receives names: the employee list for routing, customer/supplier names in the Director's answers, the employee's name in the work AI. Needs: a server in Uzbekistan (the instruction budgets 40–150 $/month) and name masking before the AI. |
| 5. AI decides nothing | ✅ tasks are confirmed by the Director before sending; no tool writes (SAP, 1C, BILLZ, Verifix are read-only); KPI score is a formula plus the Director's own rating, not the AI |
| 6. No hidden surveillance | ✅ Verifix is the face-ID terminal staff use openly; the bot reads only what people send it |
| 7. Change log | 🟡 code: git history; the bot: `agent_actions`, `employee_changes`. Changes made by hand in other systems need a log — part of the IT book |
| 8. Least privilege | 🟡 IT sees no figures (the Director's order of 07.10) |
| 9. Purchases > $100 once / $50 a month — owner's written OK | ✋ |
| 10. Everything in the IT book | 🟡 IT-7 |

## The 8 AI agents and А1–А8

| Instruction | What exists here | Gap |
| ---- | ---- | ---- |
| Ходим ёрдамчиси (answers with the SOP number; "билмайман" when unsure) | ✅ the work AI (`15-ai-chat.md`): honest "билмайман", knows the person's tasks and the bot's commands | the SOP library and the employees' duties aren't given yet; answers can't cite an SOP number until they are; the 20-question test ⬜ |
| Колл-AI (calls/chats → first answer, request, hand-over) | ⬜ | telephony / chat access; FAQ and price list; Ф3 |
| LONDRY-AI + А4 (error code or 3 h without payment → technician + Зиёда; 30 min → 2nd person) | ⬜ | access to the machines' / payment system's data — after IT-3 |
| Молия-AI + А2 (07:00 bank ↔ SAP ↔ 1C differences to the accountant) | 🟡 supplier debt 1C ↔ SAP (`21-ap-reconcile.md`) and customer debt from both are done, on request in OPS Manager Bot | bank statements aren't connected; a daily 07:00 list for the accountant isn't scheduled |
| Импорт-AI + А3 (SAP stock < reorder point → draft order + 3 months' sales) | ⬜ | a gateway tool with each item's reorder point; the instruction says Service Layer — here SAP is read only through the gateway's fixed tools |
| Сотув-AI + А1 (AmoCRM lead 15 min unanswered → reminder; 60 min → head) | ❓ | AmoCRM was recorded as not used (removed) — is it in use now? |
| HR-AI + А6 / А7 (contract signed → onboarding; resignation → SOP-03, access closing date) | ⬜ | Microsoft 365 access (the documents / Forms) |
| PULSE (08:30 red/yellow/green to the owner) | 🟡 the 08:00 brief (a picture, red only for what needs attention) goes to the Director | ❓ who gets PULSE — the owner, the Director, both? |
| А5 (cash-collection sheet → difference %; > 1 % yellow, > 3 % red) | ⬜ | the sheet is Microsoft Forms in the instruction; could also be a form in the bot |
| А8 ("resolved" form → knowledge base, the bot re-indexes) | ⬜ | the knowledge base (BRAIN) itself — Ф1 |

## Platform

The instruction scores n8n on a server in Uzbekistan highest (7.5) and wants
the AI model switchable by one setting. Here: the automations are already
code (Python, tested by `scripts/selfcheck.py`), and the model is one setting
(`OPS_MANAGER_BOT_MODEL` + fallbacks, any model on OpenRouter). ❓ The owner
decides whether new flows are built in n8n or in this service — either way
both should move to the Uzbek server (rule 4).

## Decisions needed from the owner

1. Rule 4: move the database and the service to a server in Uzbekistan, and
   mask names before the AI — when, and which provider?
2. AmoCRM: in use again (А1, Колл-AI, Сотув-AI depend on it)?
3. PULSE and the weekly IT report: to the owner, the Director, or both?
4. New automations: n8n, or this service?
5. Microsoft 365: which account the IT book and HR flows live in, and
   access for the system (А5–А8).
