"""What our 1C publishes, and where the money is — the /1c check.

Built to look before it shows anything: «Бухгалтерия для Узбекистана» uses
the Uzbek chart of accounts (НСБУ 21: 5000s are cash — 5010 касса, 5110
расчётный счёт, 5210 валютный, …), and the OData names of a balance's
fields depend on the configuration. So /1c reports what it finds — the
published objects, the money accounts and the balance fields — and the
Director's «Касса» figure is wired only once that report is checked.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from integrations.onec.client import OneCClient, OneCError

CHART = "ChartOfAccounts_Хозрасчетный"
REGISTER = "AccountingRegister_Хозрасчетный"
MONEY_PREFIXES = ("50", "51", "52", "55", "56", "57")  # НСБУ 21, class 5000: денежные средства
SENSITIVE_WORDS = ("Зарплат", "Сотрудник", "ФизическиеЛица", "Физлиц", "Кадров", "НДФЛ", "Табел", "Отпуск")


def money_accounts(chart: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accounts of class 5000 (cash, bank, currency, special accounts), by code."""
    found = []
    for row in chart:
        code = str(row.get("Code") or "").strip()
        if code[:2] in MONEY_PREFIXES and not row.get("DeletionMark"):
            found.append({"key": row.get("Ref_Key"), "code": code, "name": str(row.get("Description") or "")})
    return sorted(found, key=lambda a: a["code"])


def balance_fields(rows: list[dict[str, Any]]) -> list[str]:
    """Numeric fields of a Balance row (e.g. СуммаBalance), as 1C named them."""
    names: set[str] = set()
    for row in rows[:20]:
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                names.add(key)
    return sorted(names)


def sensitive(sets: list[str]) -> list[str]:
    """Published objects that look like payroll / personal data — they should be unticked."""
    return sorted(s for s in sets if any(w.lower() in s.lower() for w in SENSITIVE_WORDS))


async def run(client: OneCClient, at: datetime) -> dict[str, Any]:
    """Probe 1C step by step; each step's failure is reported, not raised."""
    report: dict[str, Any] = {"sets": [], "accounts": [], "fields": [], "balances": {}, "errors": []}
    try:
        report["sets"] = await client.entity_sets()
    except OneCError as exc:
        report["errors"].append(f"list: {exc}")
        return report
    try:
        chart = (await client.get(CHART, {"$select": "Ref_Key,Code,Description,DeletionMark"})).get("value", [])
        report["accounts"] = money_accounts(chart)
    except OneCError as exc:
        report["errors"].append(f"{CHART}: {exc}")
    period = at.strftime("%Y-%m-%dT%H:%M:%S")
    rows: list[dict[str, Any]] = []
    for resource in (f"{REGISTER}/Balance(Period=datetime'{period}')", f"{REGISTER}/Balance()"):
        try:
            rows = (await client.get(resource)).get("value", [])
            report["balance_call"] = resource.split("/", 1)[1]
            break
        except OneCError as exc:
            report["errors"].append(f"{resource.split('/', 1)[1]}: {exc}")
    report["fields"] = balance_fields(rows)
    money_keys = {a["key"]: a["code"] for a in report["accounts"]}
    amount_field = next((f for f in report["fields"] if f.startswith("Сумма") and f.endswith("Balance")), None)
    report["amount_field"] = amount_field
    if amount_field:
        for row in rows:
            code = money_keys.get(row.get("Account_Key"))
            if code:
                report["balances"][code] = report["balances"].get(code, 0.0) + float(row.get(amount_field) or 0)
    return report
