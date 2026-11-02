"""SAP B1 ↔ 1C ↔ Billz — every comparison and every wrong record, for accounting (2026-11-02, the Boss's T-2026-11-0048).

«Ҳар куни соат 9:00 да SAP, 1C ва Billz маълумотларини солиштириб, барча фарқлар, хатолар ва номутаносибликлар
бўйича ҳисоботни Зайнабхонга юборинг» — and the owner: she gets the wrong data too, not only the differences;
AmoCRM and Didox come later. Pure logic here (tested offline); ``run.py`` reads the systems and sends.

  * **documents** (``match_docs``): sales (SAP A/R invoices ↔ 1C «Реализация»), money in (SAP incoming payments ↔
    1C bank receipts), money out (SAP outgoing payments ↔ 1C bank / cash payments) — paired by counterparty (ИНН,
    else the cleaned-up name), amount (≤ 0.5 % or 1 000 сўм) and date (± ``DATE_DAYS``): matched, matched on another
    date, amount differs, only in SAP, only in 1C;
  * **balances** (``compare_balances``): customers' debt (SAP's balance ↔ 1C 4010/4015 − advances 6310/6315); the
    suppliers' debt comes from ``onec/payables.py`` as before;
  * **shop** (``shop_days``): Billz ↔ SAP shop invoices ↔ 1C retail reports, day by day;
  * **wrong data** (``sap_problems``, ``onec_problems``): a missing or malformed ИНН, one ИНН on two cards, a sale
    without a salesperson or with a zero price, a date in the future, an invoice entered days late, stock below
    zero, an unposted 1C document.
Amounts are so'm: SAP's system-currency totals (``DocTotalSy`` / ``BalanceSys``), 1C converted at the document's rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from integrations.onec.payables import digits, norm_name, num

DATE_DAYS = 3
TOLERANCE_SOM = 1000.0
TOLERANCE_SHARE = 0.005
LATE_ENTRY_DAYS = 3


def tolerance(amount: float) -> float:
    return max(TOLERANCE_SOM, abs(amount) * TOLERANCE_SHARE)


def to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


@dataclass
class Doc:
    system: str           # 'SAP' | '1C'
    day: date
    number: str
    inn: str
    name: str
    amount: float         # so'm
    note: str = ""

    def same_party(self, other: "Doc") -> bool:
        if len(self.inn) >= 9 and len(other.inn) >= 9:
            return self.inn == other.inn
        return bool(norm_name(self.name)) and norm_name(self.name) == norm_name(other.name)


@dataclass
class DocPair:
    sap: Doc | None
    onec: Doc | None
    status: str           # match | date | amount | only_sap | only_1c

    @property
    def diff(self) -> float:
        return (self.onec.amount if self.onec else 0.0) - (self.sap.amount if self.sap else 0.0)

    @property
    def problem(self) -> bool:
        return self.status != "match"


def match_docs(sap: list[Doc], onec: list[Doc], start: date, end: date, days: int = DATE_DAYS) -> list[DocPair]:
    """Pair the period's documents. 1C documents a few days outside the period may still pair (entered on another
    day); only those inside it are reported as missing."""
    left = list(onec)
    pairs: list[DocPair] = []
    inside = [d for d in sorted(sap, key=lambda d: (d.day, d.number)) if start <= d.day <= end]
    rest: list[Doc] = []
    for s in inside:  # 1: same counterparty, same amount, date within ±days
        best = min((o for o in left if s.same_party(o) and abs(o.amount - s.amount) <= tolerance(s.amount)
                    and abs((o.day - s.day).days) <= days),
                   key=lambda o: (abs((o.day - s.day).days), abs(o.amount - s.amount)), default=None)
        if best is None:
            rest.append(s)
            continue
        left.remove(best)
        pairs.append(DocPair(s, best, "match" if best.day == s.day else "date"))
    for s in rest:  # 2: same counterparty and date, another amount
        best = min((o for o in left if s.same_party(o) and abs((o.day - s.day).days) <= days),
                   key=lambda o: (abs((o.day - s.day).days), abs(o.amount - s.amount)), default=None)
        if best is not None:
            left.remove(best)
        pairs.append(DocPair(s, best, "amount" if best is not None else "only_sap"))
    pairs += [DocPair(None, o, "only_1c") for o in left if start <= o.day <= end]
    order = {"amount": 0, "only_sap": 1, "only_1c": 2, "date": 3, "match": 4}
    return sorted(pairs, key=lambda p: (order[p.status], -(abs(p.diff))))


# ---------------------------------------------------------------------------------------------------- balances


@dataclass
class Balance:
    inn: str
    name: str
    amount: float         # so'm, positive = the counterparty owes us
    code: str = ""
    detail: str = ""


@dataclass
class BalancePair:
    sap: Balance | None
    onec: Balance | None
    how: str = ""         # ИНН | ном

    @property
    def diff(self) -> float:
        return (self.onec.amount if self.onec else 0.0) - (self.sap.amount if self.sap else 0.0)

    @property
    def status(self) -> str:
        if self.sap is None:
            return "only_1c"
        if self.onec is None:
            return "only_sap"
        return "match" if abs(self.diff) <= tolerance(max(abs(self.sap.amount), abs(self.onec.amount))) else "diff"


def pair_balances(sap: list[Balance], onec: list[Balance]) -> list[BalancePair]:
    left = {i: b for i, b in enumerate(onec)}
    by_inn = {b.inn: i for i, b in left.items() if len(b.inn) >= 9}
    by_name: dict[str, int] = {}
    for i, b in left.items():
        by_name.setdefault(norm_name(b.name), i)
    pairs = []
    for s in sorted(sap, key=lambda b: -abs(b.amount)):
        key, how = None, ""
        if len(s.inn) >= 9 and by_inn.get(s.inn) in left:
            key, how = by_inn[s.inn], "ИНН"
        elif norm_name(s.name) and by_name.get(norm_name(s.name)) in left:
            key, how = by_name[norm_name(s.name)], "ном"
        pairs.append(BalancePair(s, left.pop(key) if key is not None else None, how))
    return pairs + [BalancePair(None, b) for b in left.values()]


def compare_balances(sap: list[Balance], onec: list[Balance]) -> list[BalancePair]:
    """Counterparties with a balance in either system, paired; SAP's sign is the one under which the paired
    counterparties agree best (as the supplier comparison learnt on 09.10). Zero balances on both sides are left out."""
    sap = [b for b in sap if abs(b.amount) > TOLERANCE_SOM]
    onec = [b for b in onec if abs(b.amount) > TOLERANCE_SOM]
    best = None
    for sign in (1.0, -1.0):
        flipped = [Balance(b.inn, b.name, sign * b.amount, b.code, b.detail) for b in sap]
        pairs = pair_balances(flipped, onec)
        total = sum(abs(p.diff) for p in pairs if p.sap and p.onec)
        if best is None or total < best[0]:
            best = (total, pairs)
    order = {"diff": 0, "only_sap": 1, "only_1c": 2, "match": 3}
    return sorted(best[1], key=lambda p: (order[p.status], -abs(p.diff)))


# ---------------------------------------------------------------------------------------------------- the shop


@dataclass
class ShopDay:
    day: date
    billz: float | None
    sap: float
    onec: float

    @property
    def problem(self) -> bool:
        base = self.billz if self.billz is not None else self.sap
        return any(abs(v - base) > tolerance(base) for v in (self.sap, self.onec) if v is not None) or (
            self.billz is not None and abs(self.billz - self.sap) > tolerance(self.billz))


def shop_days(days: list[date], billz: dict[date, float] | None, sap: dict[date, float],
              onec: dict[date, float]) -> list[ShopDay]:
    return [ShopDay(d, None if billz is None else billz.get(d, 0.0), sap.get(d, 0.0), onec.get(d, 0.0)) for d in days]


# ---------------------------------------------------------------------------------------------------- wrong data


@dataclass
class Problem:
    system: str           # SAP | 1C
    kind: str             # a key of the report's texts (PROBLEM_TEXT)
    what: str             # the record: a card, a document
    detail: str = ""


def _inn_ok(inn: str) -> bool:
    return len(inn) in (9, 14)


def sap_problems(customers: list[dict[str, Any]], sales: list[dict[str, Any]], payments: list[dict[str, Any]],
                 stock: list[dict[str, Any]], today: date) -> list[Problem]:
    """SAP records that are wrong or incomplete — each one named, so it can be fixed."""
    out: list[Problem] = []
    by_inn: dict[str, list[dict[str, Any]]] = {}
    active = {str(r.get("CardCode")) for r in sales} | {str(r.get("CardCode")) for r in payments}
    for c in customers:
        code, name, inn = str(c.get("CardCode") or ""), str(c.get("CardName") or ""), digits(c.get("LicTradNum"))
        what = f"{code} {name}".strip()
        if inn and not _inn_ok(inn):
            out.append(Problem("SAP", "inn_bad", what, inn))
        elif not inn and (abs(num(c.get("BalanceSys"))) > TOLERANCE_SOM or code in active):
            out.append(Problem("SAP", "inn_missing", what))
        if _inn_ok(inn):
            by_inn.setdefault(inn, []).append(c)
    for inn, cards in by_inn.items():
        if len(cards) > 1:
            out.append(Problem("SAP", "inn_twice", ", ".join(f"{c.get('CardCode')} {c.get('CardName')}" for c in cards), inn))
    seen: set[str] = set()
    for r in sales:
        if str(r.get("CANCELED") or "N") != "N":
            continue
        doc = str(r.get("DocNum") or r.get("DocEntry"))
        what = f"№{doc} · {r.get('CardName') or ''}".strip(" ·")
        day, made = to_date(r.get("DocDate")), to_date(r.get("CreateDate"))
        if doc not in seen:
            seen.add(doc)
            if str(r.get("SlpCode")) in ("-1", "", "None"):
                out.append(Problem("SAP", "no_salesperson", what))
            if day and day > today:
                out.append(Problem("SAP", "future_date", what, f"{day:%d.%m.%Y}"))
            if day and made and (made - day).days > LATE_ENTRY_DAYS:
                out.append(Problem("SAP", "entered_late", what, f"{day:%d.%m} → {made:%d.%m}"))
        if num(r.get("Price")) <= 0 and num(r.get("Quantity")) > 0 and str(r.get("ObjType") or "13") == "13":
            out.append(Problem("SAP", "zero_price", what, str(r.get("Dscription") or r.get("ItemCode") or "")))
    for p in payments:
        if str(p.get("Canceled") or "N") == "N" and not str(p.get("CardCode") or "").strip():
            out.append(Problem("SAP", "payment_no_party", f"№{p.get('DocNum')}", f"{to_date(p.get('DocDate')) or ''}"))
    for s in stock:
        if num(s.get("OnHand")) < 0:
            out.append(Problem("SAP", "stock_negative", f"{s.get('ItemCode')} {s.get('ItemName') or ''}".strip(),
                               f"{s.get('WhsCode')}: {num(s.get('OnHand')):g}"))
    return out


def onec_problems(parties: list[dict[str, Any]], docs: list[dict[str, Any]], used: set[str]) -> list[Problem]:
    """1C records that are wrong or incomplete: counterparties in use without a (valid) ИНН, one ИНН twice,
    documents of the period left unposted."""
    out: list[Problem] = []
    by_inn: dict[str, list[str]] = {}
    for p in parties:
        if p.get("IsFolder") or p.get("DeletionMark"):
            continue
        inn, name = digits(p.get("ИНН")), str(p.get("Description") or "")
        if p.get("Ref_Key") in used:
            if not inn:
                out.append(Problem("1C", "inn_missing", name))
            elif not _inn_ok(inn):
                out.append(Problem("1C", "inn_bad", name, inn))
        if _inn_ok(inn):
            by_inn.setdefault(inn, []).append(name)
    for inn, names in by_inn.items():
        if len(names) > 1:
            out.append(Problem("1C", "inn_twice", ", ".join(names), inn))
    for d in docs:
        if not d.get("Posted") and not d.get("DeletionMark"):
            out.append(Problem("1C", "unposted", f"{d.get('_kind', '')} №{d.get('Number', '')}".strip(),
                               f"{to_date(d.get('Date')) or ''}"))
    return out


@dataclass
class Report:
    day: date
    start: date
    end: date
    sales: list[DocPair] = field(default_factory=list)
    money_in: list[DocPair] = field(default_factory=list)
    money_out: list[DocPair] = field(default_factory=list)
    customers: list[BalancePair] = field(default_factory=list)
    suppliers: Any = None            # payables.Comparison, or None when 1C / SAP couldn't be read
    shop: list[ShopDay] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # sources that couldn't be read

    def counts(self) -> dict[str, int]:
        sup = 0
        if self.suppliers is not None:
            sup = sum(1 for m in self.suppliers.pairs if not (m.onec and m.sap) or abs(m.diff) > tolerance(
                max(abs(m.onec.net), abs(m.sap.owed))))
        return {
            "sales": sum(p.problem for p in self.sales), "money_in": sum(p.problem for p in self.money_in),
            "money_out": sum(p.problem for p in self.money_out),
            "customers": sum(p.status != "match" for p in self.customers), "suppliers": sup,
            "shop": sum(d.problem for d in self.shop), "problems": len(self.problems),
        }
