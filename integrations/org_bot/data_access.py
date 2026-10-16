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
# Access that comes with the role itself, not the admin's toggles (2026-10-14,
# the owner): the Garmin manager sees Garmin's SAP (the Garmin warehouses
# only — never the other businesses' customers or debts), Billz and Garmin's
# own records (bot leads, complaints, the catalog). It opens with the role
# itself — no confirmation (the owner, 2026-10-14: "we don't need access from
# the Boss when he is Garmin manager"); the Director is told when the role is
# given or taken, and changing the role closes it.
ROLE_SCOPES: dict[str, frozenset[str]] = {"garmin_manager": frozenset({"sap_garmin", "billz", "garmin"})}
NAMES = {**SYSTEMS, "sap_garmin": "SAP (Garmin омборлари)", "garmin": "Garmin (лидлар, шикоятлар, каталог, Garmin сотув ҳисоботлари)"}


def granted(employee: dict[str, Any] | None) -> set[str]:
    """The systems this person may ask about now: none unless active and in Бухгалтерия / Молия."""
    if not employee or employee.get("status", "active") != "active":
        return set()
    if employee.get("role") in ROLE_SCOPES:
        return set(ROLE_SCOPES[employee["role"]])
    if employee.get("role") not in ROLES:
        return set()
    return {s for s in (employee.get("data_access") or []) if s in SYSTEMS}


def label(systems: set[str] | list[str]) -> str:
    """"SAP, 1C" — in a fixed order."""
    return ", ".join(NAMES[s] for s in NAMES if s in set(systems))


def card_line(employee: dict[str, Any]) -> str:
    """The /xodimlar card's line."""
    if employee.get("role") in ROLE_SCOPES:
        return f"Маълумотга кириш (ролига кўра): {label(ROLE_SCOPES[employee['role']])}"
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
    if have and employee.get("role") in ROLE_SCOPES:
        # Comes with the role (no confirmation, the owner 2026-10-14): closed by changing the role.
        return (f"🔐 <b>Маълумотга кириш</b>\n{escape(person(employee))} — ролига кўра бот орқали "
                f"<b>{escape(label(have))}</b> маълумотларини кўради.\nРолни берган: админ ({escape(by)}). "
                "Ёпиш учун админ ролни ўзгартиради."), None
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
    accounting = "1c" in have
    if ru:
        example = ("«какая кредиторская задолженность?» или «остаток в кассе»" if accounting
                   else "«сколько продали вчера в магазине?» или «какие жалобы по Garmin?»")
        return (f"🔐 Вам открыт доступ к данным <b>{escape(systems)}</b> через этого бота. Просто напишите вопрос, "
                f"например: {example}.")
    example = ("«кредиторлик қанча?» ёки «кассада қанча пул бор?»" if accounting
               else "«кеча дўконда қанча сотилди?» ёки «Garmin шикоятлари қандай?»")
    return (f"🔐 Сизга бот орқали <b>{escape(systems)}</b> маълумотларини сўраш имкони берилди. Саволингизни ёзинг, "
            f"масалан: {example}.")


def lang_label(employee: dict[str, Any]) -> str:
    return "русча" if employee.get("lang") == "ru" else "ўзбекча"
