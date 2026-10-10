"""What OPS Manager Bot knows about MGMG — one block, shared by its prompts.

2026-10-10, from the owner: the bot should understand the company — its
businesses, branches, departments, which system holds what, and the words
the Director uses — and ask when it isn't sure instead of guessing. Before
this, each prompt carried its own few lines (and one still pointed at the
in-house CRM, removed long ago).

Facts only, no figures: no amounts, customers or people's names (the
Director's order of 07.10.2026 — IT reads this file). The departments and
branches come from the code (``roles.ROUTABLE_ROLES``, ``feedback.BRANCHES``)
so they can't drift; keep the rest in step with CLAUDE.md when the business
changes.

  COMPANY_KNOWLEDGE — for the Director's prompts (router, analyst, answers).
  EMPLOYEE_BOT_GUIDE — what the bot does for an employee (their work AI).
"""

from __future__ import annotations

from integrations.org_bot.feedback import BRANCHES
from integrations.org_bot.roles import ROUTABLE_ROLES

_DEPARTMENTS = ", ".join(f"{r.label} ({r.slug})" for r in ROUTABLE_ROLES)
_LONDRY_BRANCHES = ", ".join(BRANCHES["laundry"].values())
_GARMIN_BRANCHES = ", ".join(BRANCHES["garmin"].values())

COMPANY_KNOWLEDGE = f"""\
# THE COMPANY
MGMG (ЭМЖИЕМ; SAP's company database is called MGM), Tashkent, Uzbekistan.
Two business lines — never assume a message is about one of them unless it
says so or the conversation makes it clear:
  - Primus Londry ("Londry" — the brand; never "Laundry"): industrial laundry
    equipment for hotels, hospitals, laundries and factories (washer-
    extractors, tumble dryers, flatwork ironers, chemicals) and services:
    laundry design, installation, maintenance, spare parts. Sold to
    businesses (B2B). Branches: {_LONDRY_BRANCHES}.
  - Garmin: an authorised Garmin retailer — smartwatches, running / outdoor /
    multisport watches, dive computers, cycling and marine electronics; also
    Tanita scales. Shops: {_GARMIN_BRANCHES}. The shop that sells through the
    BILLZ tills is GARMIN ABAY; it enters each cheque into SAP as a sales
    invoice, often a day or more later. The Garmin AI Telegram bot
    (@garminofficialuzbot) brings customers from Instagram and the web catalog.
People: the Operations Director (Директор, раҳбар) runs every department and
both businesses — he is the one who writes to you. Departments (role slugs):
{_DEPARTMENTS}. Some staff work on "эркин график" (come when needed or when
the Director calls): they are never counted late or absent.

# WHICH SYSTEM HOLDS WHAT
  - SAP Business One — B2B sales invoices and credit notes, customer debt
    (open invoices), incoming and outgoing payments, supplier balances, sales
    orders, purchase orders, stock by warehouse, items, business partners,
    sales people. SAP's own amounts are in its local currency, USD; fields
    ending in "Sy" are so'm. Pushed from the SAP gateway every 30 minutes,
    so it is at most about half an hour old (older if the gateway machine is off).
  - 1C «Бухгалтерия для Узбекистана» (on Clobus) — the accounting books, in
    so'm, read live: money in bank accounts and cash desks (accounts 50, 51,
    52), customers' debt (40), payables to suppliers (60), advances given
    (43) and received (63), taxes (64), goods (29), revenue (9010), cost of
    sales (9110), expenses (94).
  - BILLZ — the Garmin shops' tills: cheques, sellers, products, returns, so'm.
  - Verifix — face-ID attendance: who came late, who didn't come, sick leave,
    vacation, arrival and leaving times.
  - The leads Google Sheet ("POSSIBLE Leads") — companies and tenders the Lead
    Agent finds every morning. Handing leads to B2B sales people was stopped
    by the Director on 2026-10-07; only the history of that is kept.
  - The bot's own records — tasks the Director gave and their status; the
    employees' daily reports (asked 16:00, reminder 17:00); KPI (goals,
    results, the Director's ratings, the monthly score on the 1st); written
    permission requests (procedure EMJ-SOP-ADM-01, numbers like
    EMJ-2026-0004); client complaints from the QR pages (Londry or Garmin,
    by branch); customers from the Garmin AI bot.
  - NOT connected — no data at all: Didox (e-invoices), online banking / bank
    statements. Not used by the business: an in-house CRM, amoCRM, MS Planner.

# WORDS THE DIRECTOR USES
  - дебитор, дебиторлик, debitorka, "ким қанча қарз" = customers owe US
    (SAP open invoices and 1C account 40).
  - кредитор, кредиторлик, "етказиб берувчиларга қарз" = WE owe suppliers
    (1C 60 minus advances 43; SAP supplier balances).
  - аванс = paid ahead (to a supplier: 43; from a customer: 63); зачет = an
    advance set off against a debt; акт-сверка = a reconciliation statement
    with one counterparty.
  - касса = cash desk; ҳисоб рақам, р/с = bank account; пул қолдиғи = money
    right now; пул календари, кассовый план = money in / out in the coming days.
  - савдо, сотув, выручка = sales: the shops' sales are BILLZ, B2B sales are
    SAP invoices. қолдиқ, склад, омбор = stock (SAP), or a balance on an
    account (1C) — decide from what follows.
  - ҳисобот, отчёт = usually the employees' daily reports; sometimes a report
    on figures — decide from the rest of the message.
  - топшириқ, задача = work for people; рухсат = a written permission
    request; давомат = attendance; кечикди = late to work (unless a task or
    deadline is meant).
  - солиштир, сверка, сравни = compare (for example 1C against SAP).
  - "ёз", "чиқар", "кўрсат" + something that already exists (invoices, debts,
    leads) = "list it for me", not "create a new one".
"""

EMPLOYEE_BOT_GUIDE = """\
What this bot does for an employee (tell them when it helps):
- Tasks from the Director arrive as cards: ▶️ Бошладим, ✅ Бажардим. Replying
  to a card with a question brings them here.
- The daily report is asked at 16:00 (reminder 17:00) unless the admin
  switched it off for them; they answer in the chat. /hisobot shows,
  changes or deletes TODAY'S report only.
- KPI: /natija — their results for the month's goals; /kpi — their score.
- A written permission (EMJ-SOP-ADM-01): /ruxsat, then the bot asks the
  details and sends it for a decision (the answer comes back here).
- A photo, video or file sent here is asked "what is it for?" and passed to
  the Director as it is.
- /ism — ask the admin to correct their name.
"""
