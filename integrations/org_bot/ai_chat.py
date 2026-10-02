"""AI chat for employees — help with their own work, with none of the company's data.

2026-10-02, from the owner: some employees (B2B sales first) should be able
to talk with the AI about their work, but get no access to the company's
systems or information. So:

- the admin grants it per person (Admin Bot /xodimlar → the card → 🤖);
- the person turns it on with /ai — from then on their plain messages go
  to the AI — and off with /ai again; it also ends after 20 quiet minutes,
  so a later report or message isn't taken by the AI by accident. A Reply
  to one of the bot's own messages (the report ask, a lead question, a
  cheer) keeps its usual meaning even while it's on;
- the AI knows only what MGMG sells (public) and, for Garmin sales, the
  public catalog. It has no SAP, money, stock, reports, tasks, KPI, leads,
  customers or other people, must say so when asked, and never invents
  them. It can't send, save or change anything;
- its answers are Uzbek Cyrillic and always polite ("сиз"); a draft the
  person asks for in another language (a Russian email) may be in it;
- 60 questions a day per person; the chat is kept a day as its memory and
  shown to no one.

Pure logic here (tested offline); ``ops_manager`` runs it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from integrations.org_bot.prompt import GARMIN_CATALOG
from integrations.org_bot.roles import ROLE_LABELS
from integrations.org_bot.tone import casual
from integrations.telegram.bot import escape, sanitize_model_html

AGENT = "ai-chat"
COMMAND = "/ai"
SESSION_MINUTES = 20
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
"""

SYSTEM_PROMPT = """\
You are a helpful AI assistant for one employee of MGMG — {role}. You help
them with THEIR OWN WORK: drafting a message, an email or a commercial offer
for a customer, preparing for a call or a meeting, answering objections,
explaining a product category, planning their day, Excel or Word help,
translating a text they give you.

What you know — public facts only:
{company}{catalog}
What you do NOT have — say so plainly whenever it comes up:
- no access to any company system or data: no SAP, sales figures, prices
  beyond the public catalog, debts, cash, stock, reports, tasks, KPI, leads,
  customers or anything about other employees. Never invent such data —
  not a number, not a name, not a date. When they need it, tell them to ask
  their manager or the Director.
- no actions: you cannot send, save or change anything, or contact anyone.

How you write:
- Uzbek, Cyrillic script. A draft they ask for in another language (e.g. a
  Russian email to a customer) is written in that language.
- Always polite: the respectful "сиз", never "сен" or its verb forms; be
  especially courteous with women.
- Short and practical. Telegram HTML only: <b>, <i>; no Markdown, no tables.
- Keep to work. Politely decline anything harmful, illegal, or about other
  people's private matters.
"""


def session_active(employee: dict[str, Any], now: datetime) -> bool:
    """Whether this person's AI chat is on right now."""
    until = employee.get("ai_chat_until")
    return bool(employee.get("ai_chat")) and until is not None and until > now


def system_prompt(employee: dict[str, Any]) -> str:
    """The AI's rules for this person; Garmin sales also get the public catalog."""
    role = employee.get("role") or ""
    catalog = f"\nThe public Garmin catalog (prices may have changed):\n{GARMIN_CATALOG}\n" if role == "garmin_sotuv" else ""
    return SYSTEM_PROMPT.format(role=ROLE_LABELS.get(role, role) or "employee", company=_COMPANY, catalog=catalog)


def user_prompt(history: list[dict[str, Any]], question: str) -> str:
    """The recent chat, then the new question."""
    lines = []
    if history:
        lines.append("EARLIER IN THIS CHAT:")
        lines += [f"{'Employee' if h['role'] == 'employee' else 'You'}: {h['content']}" for h in history]
        lines.append("")
    lines += ["THE EMPLOYEE NOW ASKS:", question]
    return "\n".join(lines)


def clean_answer(answer: str) -> str:
    """The AI's answer, safe for Telegram HTML and of a sane length."""
    text = sanitize_model_html((answer or "").strip())
    if len(text) > ANSWER_MAX:
        text = text[:ANSWER_MAX].rsplit(" ", 1)[0] + "…"
    return text or casual("кечирасиз, жавоб топа олмадим, саволни бошқача ёзиб кўринг", "🙏")


def on_text(name: str) -> str:
    return casual(
        f"{escape(name)}, AI ёрдамчи ёқилди, ишингиз бўйича саволингизни ёзинг, тугатиш учун яна /ai ни босинг",
        "🤖", keep=[escape(name), "AI"],
    )


def granted_text(name: str) -> str:
    return casual(
        f"{escape(name)}, сизга AI ёрдамчи очилди, ишингиз бўйича савол бериш учун /ai ни босинг",
        "🤖", keep=[escape(name), "AI"],
    )


def off_text() -> str:
    return casual("AI суҳбати ёпилди, раҳмат", "🙂", keep=["AI"])


def not_granted_text() -> str:
    return casual("AI суҳбати сизга ҳали очилмаган, керак бўлса админга айтинг", "🙂", keep=["AI"])


def limit_text() -> str:
    return casual("бугунги AI саволлари чегарасига етдингиз, эртага давом этамиз", "🙂", keep=["AI"])


def error_text() -> str:
    return casual("техник хатолик юз берди, бир оздан кейин қайта ёзинг", "🙏")
