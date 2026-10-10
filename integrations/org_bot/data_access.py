"""Company data through the bot for accounting and finance (2026-10-10).

The Director's instruction: the accountant (Бухгалтерия) and finance (Молия)
may look up 1C, SAP and BILLZ through OPS Manager Bot. So:

- the admin gives each system to a person on their /xodimlar card (Admin
  Bot) — only people in those two roles; every change goes to
  ``employee_changes``;
- the Director is told at once — who, which systems, who gave it — with a
  button that closes it all; the person is told too;
- that person's messages then go to the analyst (``analyst.py``) with the
  tools of the granted systems only — never Verifix attendance or the bot's
  own records (tasks, KPI, complaints, permissions), which stay the
  Director's; the tools are filtered in code, not just in the prompt;
- answers in the person's language (``employees.lang``, set by the admin).

IT gives the access but sees none of the answers (the Director's order of
07.10.2026): logs keep only which tools ran.
"""

from __future__ import annotations

from typing import Any

from integrations.telegram.bot import escape

SYSTEMS: dict[str, str] = {"sap": "SAP", "1c": "1C", "billz": "Billz"}
ROLES = ("buxgalteriya", "moliya")


def granted(employee: dict[str, Any] | None) -> set[str]:
    """The systems this person may ask about now: none unless active and in Бухгалтерия / Молия."""
    if not employee or employee.get("status", "active") != "active" or employee.get("role") not in ROLES:
        return set()
    return {s for s in (employee.get("data_access") or []) if s in SYSTEMS}


def label(systems: set[str] | list[str]) -> str:
    """"SAP, 1C" — in a fixed order."""
    return ", ".join(SYSTEMS[s] for s in SYSTEMS if s in set(systems))


def card_line(employee: dict[str, Any]) -> str:
    """The /xodimlar card's line."""
    if employee.get("role") not in ROLES:
        return "Маълумотга кириш (SAP, 1C, Billz): фақат Бухгалтерия ва Молия учун"
    have = granted(employee)
    return f"Маълумотга кириш (бот орқали): {label(have) if have else 'йўқ'}"


def card_buttons(employee: dict[str, Any]) -> list[dict[str, str]]:
    """One toggle per system — only for the two roles."""
    if employee.get("role") not in ROLES:
        return []
    have = granted(employee)
    return [{"text": f"{'✅' if s in have else '▫️'} {name}", "callback_data": f"dacc:{s}:{employee['id']}"}
            for s, name in SYSTEMS.items()]


def person(employee: dict[str, Any]) -> str:
    from integrations.org_bot.roles import ROLE_LABELS

    name = (employee.get("full_name") or "").strip() or employee.get("display_name") or "—"
    return f"{name} ({ROLE_LABELS.get(employee.get('role') or '', employee.get('role') or '')})"


def director_notice(employee: dict[str, Any], by: str) -> tuple[str, dict[str, Any] | None]:
    """What the Director sees after every change, with a button that closes it all."""
    have = granted(employee)
    if have:
        text = (f"🔐 <b>Маълумотга кириш</b>\n{escape(person(employee))} энди бот орқали "
                f"<b>{escape(label(have))}</b> маълумотларини сўраши мумкин.\nБерган: админ ({escape(by)}).")
        return text, {"inline_keyboard": [[{"text": "⛔ Ёпиш", "callback_data": f"dax:{employee['id']}"}]]}
    return (f"🔐 <b>Маълумотга кириш ёпилди</b>\n{escape(person(employee))} энди бот орқали маълумот сўрай олмайди.\n"
            f"Ёпган: админ ({escape(by)})."), None


def employee_notice(employee: dict[str, Any]) -> str:
    """What the person is told, in their language."""
    have = granted(employee)
    ru = employee.get("lang") == "ru"
    if not have:
        return ("🔐 Доступ к данным через бота закрыт." if ru
                else "🔐 Бот орқали маълумот сўраш имконингиз ёпилди.")
    systems = label(have)
    if ru:
        return (f"🔐 Вам открыт доступ к данным <b>{escape(systems)}</b> через этого бота. Просто напишите вопрос, "
                "например: «какая кредиторская задолженность?» или «остаток в кассе».")
    return (f"🔐 Сизга бот орқали <b>{escape(systems)}</b> маълумотларини сўраш имкони берилди. Саволингизни ёзинг, "
            "масалан: «кредиторлик қанча?» ёки «кассада қанча пул бор?».")


def lang_label(employee: dict[str, Any]) -> str:
    return "русча" if employee.get("lang") == "ru" else "ўзбекча"
