"""The AI every employee talks to about their own work — honest, and with no one else's data.

2026-10-02, from the owner: OPS Manager Bot is for work only. Employees no
longer message the Director (or each other) through it; what they write
about their work and their tasks is answered by the AI. So:

- every employee's free message (not a report or a cheer reply) goes to the AI — no command needed; the admin can switch it off
  for one person (Admin Bot /xodimlar → 🤖), who is then told the bot is
  for reports and tasks only;
- the AI knows the person's role, their open tasks (and the one they reply
  to), and — once the owner uploads them — their written
  duties (``employees.responsibilities``); plus what MGMG sells (public),
  and for Garmin sales the public catalog;
- it has no SAP, money, stock, reports, KPI, customers or anything about
  other people, can't send, save or change anything, and contacts no one;
- honesty first: when it doesn't know, it says so plainly ("билмайман",
  "бу маълумот менда йўқ") and points to the manager — never a guess;
- Uzbek Cyrillic, always polite "сиз"; a draft asked for in another
  language (a Russian email) may be in it; 60 questions a day per person;
  the chat is kept a day as its memory and shown to no one.

Pure logic here (tested offline); ``ops_manager`` runs it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from integrations.org_bot.feedback import BRANCHES
from integrations.org_bot.knowledge import EMPLOYEE_BOT_GUIDE
from integrations.org_bot.prompt import GARMIN_CATALOG
from integrations.org_bot.roles import ROLE_LABELS
from integrations.org_bot.tone import casual
from integrations.telegram.bot import sanitize_model_html

AGENT = "ai-chat"
COMMAND = "/ai"  # no longer needed; answered with a hint for anyone used to it
DAILY_LIMIT = 60
ANSWER_MAX = 3500

_COMPANY = """\
MGMG (ЭМЖИЕМ), Tashkent, has two business lines:
- Primus Londry — industrial laundry equipment for hotels, hospitals,
  laundries and factories: washer-extractors, tumble dryers, flatwork
  ironers, chemicals; and services: laundry design, installation,
  maintenance, spare parts. Sold B2B, mostly by recommendation.
- Garmin — an authorised Garmin retailer in Uzbekistan (smartwatches,
  running/outdoor/multisport watches, dive computers, cycling and marine
  electronics), with Tanita scales.
Branches: Londry {londry}; Garmin shops {garmin}.
""".format(londry=", ".join(BRANCHES["laundry"].values()), garmin=", ".join(BRANCHES["garmin"].values()))

SYSTEM_PROMPT = """\
You are the work assistant inside MGMG's Telegram bot, talking with one
employee: {name} — {role}. You help them with THEIR OWN WORK and THEIR OWN
TASKS: understanding a task, planning the steps, drafting a message, an email
or a commercial offer, preparing a call, answering objections, explaining a
product category, Excel or Word, translating a text they give you.

HONESTY FIRST. If you don't know something, or it isn't in what you were
given below, say so plainly ("билмайман", "бу маълумот менда йўқ") and tell
them who could know (their manager or the Director). Never guess, never
invent a number, a name, a date, a price or a fact about the company.

What you know about them (only this):
{context}
What you know about the company — public facts only:
{company}{catalog}
{guide}
What you do NOT have: any company system or data — no SAP, sales figures,
debts, cash, stock, reports, KPI, customers, or anything about other
employees. What you can NOT do: send, save or change anything, pass a message
to anyone (not to the Director, not to a colleague), or contact a customer.
If they want to tell the Director something in words, say the bot doesn't
carry messages — they should speak to the Director directly. A FILE, photo or
video (for example a photo report) they can send right here: the bot asks
what it's for and passes it to the Director as it is. To change or delete
TODAY'S report: the /hisobot command (only today's report can be changed).

How you write:
- Uzbek, Cyrillic script. A draft they ask for in another language (e.g. a
  Russian email to a customer) is written in that language.
- Always polite: the respectful "сиз", never "сен" or its verb forms; be
  especially courteous with women.
- Short and practical. Telegram HTML only: <b>, <i>; no Markdown, no tables.
- Keep to work. Politely decline anything harmful, illegal, or about other
  people.
"""


def work_context(
    employee: dict[str, Any], tasks: list[dict[str, Any]], today: date
) -> str:
    """What the AI may know about this person: their duties and open tasks.

    (Their open leads were here until the Director stopped the lead hand-out, 2026-10-07.)
    """
    lines = []
    duties = (employee.get("responsibilities") or "").strip()
    lines.append(f"Their written duties:\n{duties}" if duties else "Their written duties: not uploaded yet — say so if asked.")
    if tasks:
        lines.append("Their open tasks from the Director:")
        for t in tasks[:15]:
            due = t.get("due_date")
            due_text = f", due {due:%d.%m}" + (" (overdue)" if due < today else "") if due else ", no deadline"
            status = "started" if t.get("status") == "started" else "not started"
            lines.append(f"- {_plain(t.get('task_summary'))} ({status}{due_text})")
    else:
        lines.append("Their open tasks: none.")
    return "\n".join(lines)


def system_prompt(employee: dict[str, Any], context: str) -> str:
    """The AI's rules for this person; Garmin sales also get the public catalog."""
    role = employee.get("role") or ""
    catalog = f"\nThe public Garmin catalog (prices may have changed):\n{GARMIN_CATALOG}\n" if role == "garmin_sotuv" else ""
    name = (employee.get("full_name") or "").strip() or employee.get("display_name") or "employee"
    return SYSTEM_PROMPT.format(
        name=name, role=ROLE_LABELS.get(role, role) or "employee", context=context, company=_COMPANY, catalog=catalog,
        guide=EMPLOYEE_BOT_GUIDE,
    )


def user_prompt(history: list[dict[str, Any]], question: str, task: dict[str, Any] | None = None) -> str:
    """The recent chat, the task they replied to (if any), then the new message."""
    lines = []
    if history:
        lines.append("EARLIER IN THIS CHAT:")
        lines += [f"{'Employee' if h['role'] == 'employee' else 'You'}: {h['content']}" for h in history]
        lines.append("")
    if task is not None:
        lines += [f"THEY ARE REPLYING TO THIS TASK CARD: {_plain(task.get('task_summary'))}", ""]
    lines += ["THE EMPLOYEE WRITES:", question]
    return "\n".join(lines)


def _plain(text: str | None) -> str:
    return " ".join(re.sub(r"<[^>]+>", "", text or "").split())


def clean_answer(answer: str) -> str:
    """The AI's answer, safe for Telegram HTML and of a sane length."""
    text = sanitize_model_html((answer or "").strip())
    if len(text) > ANSWER_MAX:
        text = text[:ANSWER_MAX].rsplit(" ", 1)[0] + "…"
    return text or casual("кечирасиз, жавоб топа олмадим, саволни бошқача ёзиб кўринг", "🙏")


def hint_text() -> str:
    return casual("ишингиз бўйича саволингизни шунчаки ёзаверинг, AI жавоб беради", "🙂", keep=["AI"])


def off_text() -> str:
    return casual(
        "бу бот фақат иш учун: ҳисобот ва топшириқлар, бошқа хабарлар ҳеч кимга юборилмайди", "🙂"
    )


def limit_text() -> str:
    return casual("бугунги AI саволлари чегарасига етдингиз, эртага давом этамиз", "🙂", keep=["AI"])


def error_text() -> str:
    return casual("техник хатолик юз берди, бир оздан кейин қайта ёзинг", "🙏")
