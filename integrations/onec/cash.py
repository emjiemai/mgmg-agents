"""Cash and bank balances from 1C — the brief's «💰 Касса» and the Director's questions.

Checked against the real database on 2026-10-02 (Admin Bot /1c): money sits on
the class-5000 accounts of the Uzbek chart (5010 касса, 5110.x расчётные
счета, 52xx валютные…), and ``AccountingRegister_Хозрасчетный/Balance`` gives
``СуммаBalance`` — the balance in so'm, foreign-currency accounts included
(their currency amount is ``ВалютнаяСуммаBalance``, not used here). Only
accounts with postings carry a balance, so summing them never counts a group
account twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from integrations.common.money import format_uzs_short, to_tiyin
from integrations.onec import discover
from integrations.onec.client import OneCClient, OneCError

CASH_PREFIX = "50"  # 5000s: касса; everything else in class 5000 is bank / currency / special accounts


@dataclass
class CashPosition:
    at: datetime
    accounts: list[tuple[str, str, float]] = field(default_factory=list)  # (code, name, so'm), non-zero only

    @property
    def cash(self) -> float:
        return sum(a for code, _n, a in self.accounts if code.startswith(CASH_PREFIX))

    @property
    def bank(self) -> float:
        return sum(a for code, _n, a in self.accounts if not code.startswith(CASH_PREFIX))

    @property
    def total(self) -> float:
        return self.cash + self.bank


def from_report(report: dict, at: datetime) -> CashPosition:
    """A CashPosition from discover.run's report; OneCError if the balances weren't readable."""
    if not report.get("amount_field") or not report.get("accounts"):
        raise OneCError("; ".join(report.get("errors") or []) or "no money accounts or no balance field in 1C")
    names = {a["code"]: a["name"] for a in report["accounts"]}
    rows = [(code, names.get(code, ""), amount) for code, amount in sorted(report["balances"].items()) if amount]
    return CashPosition(at=at, accounts=rows)


def som(amount: float) -> str:
    return format_uzs_short(to_tiyin(amount))


def brief_value(position: CashPosition) -> str:
    """«3,6 млрд сўм (банк 3,52 млрд сўм, нақд 76,7 млн сўм)»."""
    return f"{som(position.total)} (банк {som(position.bank)}, нақд {som(position.cash)})"


def describe(position: CashPosition) -> str:
    """Plain-text data for OPS Manager Bot's answers."""
    lines = [
        f"Money balances from 1C (Бухгалтерия для Узбекистана, accounting balances) as of "
        f"{position.at:%Y-%m-%d %H:%M} Tashkent, in UZS so'm (foreign-currency accounts converted by 1C): "
        f"total {round(position.total):,}; bank accounts {round(position.bank):,}; cash desks {round(position.cash):,}.",
        "Per account (code, name, so'm):",
    ]
    lines += [f"- {code} {name}: {round(amount):,}" for code, name, amount in position.accounts]
    return "\n".join(lines)


async def load(at: datetime, run_id: uuid.UUID | str | None, agent: str) -> CashPosition:
    async with OneCClient(agent=agent, run_id=run_id) as client:
        return from_report(await discover.run(client, at), at)
