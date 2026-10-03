"""Billz → SAP: every shop cheque must reach SAP as an A/R invoice the same day.

The owner, 2026-10-03: a sale is rung up in the shop's BILLZ till and the
shop enters it into SAP at the end of the day. Entering late, or not at all,
has cost the business thousands of dollars, so the system checks it instead
of a person.

How SAP looks (the export of 2026-10-02): the Garmin shop's sales are one
A/R invoice per cheque (OINV, mostly to "B2C клиенты"), in so'm, from the
warehouses G.A._01 and 05, entered by the shop's own SAP user ("Гармин
(филиал Абай)"), usually 18:00–20:00 the same day — and sometimes days later
(20.09's sales were entered on 24.09).

Matching, cheque by cheque:
  * a cheque is one BILLZ order: its lines' ``net_sales`` added up (so'm,
    after discounts; a return is negative);
  * an SAP document is a non-cancelled A/R invoice (a credit note counts
    negative) with a line in the shops' warehouses; its so'm amount is
    ``DocTotalSy`` — SAP's system currency is so'm (equal to ``DocTotalFC``
    on every so'm invoice in the export);
  * the same amount (± ``tolerance``) pairs them, nearest date first, a
    shared product code breaking ties; then a cheque and a document of the
    same day sharing a product, with different amounts, are paired as
    "amount differs"; a sale and its later return that never reached SAP
    cancel out.

Reported: cheques with no document (each day of the window, until entered),
amounts that differ, documents entered after the sale day or under another
date (once — the day after they were entered), and yesterday's documents
with no cheque.

Pure functions (tested offline in scripts/selfcheck.py).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from integrations.common.money import format_uzs_short, to_tiyin
from integrations.common.timeutil import fmt_date, parse_sap_date
from integrations.telegram.bot import escape

WINDOW_DAYS = 14  # cheques this old are still checked (a missing one is repeated daily)
MAX_LAG_DAYS = 7  # an SAP document dated or entered within this many days of its cheque can be its pair
MAX_LINES = 8  # bullets per section before "+N more"


@dataclass
class Item:
    code: str
    barcode: str
    name: str
    qty: float


@dataclass
class Cheque:
    key: str
    number: str
    day: date
    shop: str
    amount: int  # so'm, a return negative
    seller: str = ""
    items: list[Item] = field(default_factory=list)


@dataclass
class Doc:
    key: str
    number: str
    day: date
    created: date | None
    created_time: str  # "19:05", or ""
    amount: int  # so'm, a credit note negative
    customer: str = ""
    amount_known: bool = True  # False when the gateway sent no so'm total (DocTotalSy / DocTotalFC)
    items: list[Item] = field(default_factory=list)


@dataclass
class Pair:
    cheque: Cheque
    doc: Doc


@dataclass
class Result:
    day: date  # the day checked (yesterday)
    cheques_day: list[Cheque] = field(default_factory=list)  # that day's cheques
    docs_day: list[Doc] = field(default_factory=list)  # that day's shop documents in SAP
    missing: list[Cheque] = field(default_factory=list)
    amount_diff: list[Pair] = field(default_factory=list)
    late: list[Pair] = field(default_factory=list)
    extra: list[Doc] = field(default_factory=list)
    returned: int = 0  # sale + return pairs that cancelled out
    sap_amounts_missing: bool = False  # some SAP documents came without a so'm total: matched by product and date
    no_cheque_numbers: bool = False  # BILLZ gave lines without a cheque id: only totals compared
    billz_total: int | None = None  # set when only totals are compared (cheques unknown)

    @property
    def billz_amount(self) -> int:
        return self.billz_total if self.billz_total is not None else sum(c.amount for c in self.cheques_day)

    @property
    def sap_amount(self) -> int:
        return sum(d.amount for d in self.docs_day)

    @property
    def ok(self) -> bool:
        if self.no_cheque_numbers:
            return abs(self.billz_amount - self.sap_amount) <= tolerance(self.billz_amount)
        return not (self.missing or self.amount_diff or self.late or self.extra)

    @property
    def status(self) -> str:
        return "ok" if self.ok else "problems"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _code(value: Any) -> str:
    return str(value or "").strip().lower()


def tolerance(amount: int) -> int:
    """How far two amounts may differ and still be the same sale (rounding, tiyin)."""
    return max(1000, int(abs(amount) * 0.002))


# ------------------------------------------------------------------ settings


def warehouse_map(setting: str) -> dict[str, set[str]]:
    """``BILLZ_SAP_WAREHOUSES`` read: shop name (upper case) → warehouse codes.

    "G.A._01,05" applies to every shop (key ""); "GARMIN ABAY=G.A._01,05;
    GARMIN MALIKA=21" maps each shop by its BILLZ name.
    """
    mapping: dict[str, set[str]] = {}
    for part in (setting or "").split(";"):
        part = part.strip()
        if not part:
            continue
        shop, _, codes = part.rpartition("=") if "=" in part else ("", "", part)
        whs = {c.strip() for c in codes.split(",") if c.strip()}
        if whs:
            mapping.setdefault(shop.strip().upper(), set()).update(whs)
    return mapping


def warehouses_for(shop: str, mapping: dict[str, set[str]]) -> frozenset[str]:
    """The SAP warehouses one BILLZ shop sells from (its own entry, else the default)."""
    return frozenset(mapping.get(shop.strip().upper()) or mapping.get("") or set())


# -------------------------------------------------------------------- inputs


def _line_amount(row: dict[str, Any]) -> float:
    if row.get("net_sales") is not None:
        return _num(row.get("net_sales"))
    return _num(row.get("gross_sales")) - _num(row.get("returned_sales_sum"))


def cheques_from_billz(rows: list[dict[str, Any]], day: date) -> tuple[list[Cheque], int]:
    """One day's BILLZ cheque lines grouped into cheques.

    Returns:
        ``(cheques, money_without_cheque_id)`` — so'm on lines that carried no
        cheque id; when that isn't 0 the day can only be compared in total.
    """
    cheques: dict[str, Cheque] = {}
    unkeyed = 0.0
    for row in rows:
        number = str(row.get("order_number") or "").strip()
        key = str(row.get("order_id") or number).strip()
        amount = _line_amount(row)
        if not key:
            unkeyed += amount
            continue
        cheque = cheques.setdefault(
            key,
            Cheque(key=f"{day}:{key}", number=number or key[:8], day=day,
                   shop=str(row.get("shop_name") or "").strip(), amount=0,
                   seller=str(row.get("seller_full_name") or "").strip()),
        )
        cheque.amount += round(amount)
        qty = row.get("net_sold_measurement_value")
        cheque.items.append(Item(
            code=_code(row.get("product_sku")), barcode=_code(row.get("product_barcode")),
            name=str(row.get("product_name") or "").strip(),
            qty=_num(qty if qty is not None else row.get("sold_measurement_value")),
        ))
    return [c for c in cheques.values() if c.amount or c.items], round(unkeyed)


def _som_amount(header: dict[str, Any]) -> tuple[int, bool]:
    """A document's total in so'm and whether it is known: SAP's system
    currency (``DocTotalSy``), else the so'm document total (``DocTotalFC``)."""
    system = round(_num(header.get("DocTotalSy")))
    if system:
        return system, True
    if str(header.get("DocCur") or "").upper() == "UZS":
        foreign = round(_num(header.get("DocTotalFC")))
        if foreign:
            return foreign, True
    return 0, False


def _created_time(value: Any) -> str:
    """SAP's CreateTS (hhmmss as a number) as "19:05"."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n < 10000:  # some tables keep hhmm
        n *= 100
    return f"{n // 10000:02d}:{n // 100 % 100:02d}"


def docs_from_sap(
    sales: list[dict[str, Any]], lines: list[dict[str, Any]], warehouses: frozenset[str]
) -> list[Doc]:
    """Non-cancelled A/R invoices and credit notes with a line in ``warehouses``."""
    by_doc: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_doc[(str(line.get("ObjType") or "13"), str(line.get("DocEntry")))].append(line)
    docs: list[Doc] = []
    for header in sales:
        if str(header.get("CANCELED") or "N").upper() != "N":
            continue
        obj = str(header.get("ObjType") or "13")
        own = [ln for ln in by_doc.get((obj, str(header.get("DocEntry"))), [])
               if str(ln.get("WhsCode") or "").strip() in warehouses]
        day = parse_sap_date(str(header.get("DocDate") or ""))
        if not own or day is None:
            continue
        sign = -1 if obj == "14" else 1
        amount, known = _som_amount(header)
        docs.append(Doc(
            key=f"{obj}:{header.get('DocEntry')}", number=str(header.get("DocNum") or header.get("DocEntry")),
            day=day, created=parse_sap_date(str(header.get("CreateDate") or "")),
            created_time=_created_time(header.get("CreateTS")), amount=sign * amount, amount_known=known,
            customer=str(header.get("CardName") or "").strip(),
            items=[Item(code=_code(ln.get("ItemCode")), barcode=_code(ln.get("CodeBars")),
                        name=str(ln.get("Dscription") or "").strip(), qty=_num(ln.get("Quantity")))
                   for ln in own],
        ))
    return docs


# ------------------------------------------------------------------ matching


def shares_item(cheque: Cheque, doc: Doc) -> bool:
    """Whether a cheque and a document have a product in common (code or barcode)."""
    codes = {c for i in doc.items for c in (i.code, i.barcode) if c}
    return any(c in codes for i in cheque.items for c in (i.code, i.barcode) if c)


def date_gap(cheque: Cheque, doc: Doc) -> int | None:
    """Days between a sale and an SAP document — by the document's date, or by
    when it was entered; None when neither is within ``MAX_LAG_DAYS``.

    The shop sometimes dates an entry wrongly (22.09's sale entered on 22.09
    under 07.09; 30.09's under 25.09), so either date near the sale counts.
    """
    gaps = []
    dated = (doc.day - cheque.day).days
    if -MAX_LAG_DAYS <= dated <= MAX_LAG_DAYS:
        gaps.append(abs(dated))
    if doc.created is not None and 0 <= (doc.created - cheque.day).days <= MAX_LAG_DAYS:
        gaps.append((doc.created - cheque.day).days)
    return min(gaps) if gaps else None


def match(cheques: list[Cheque], docs: list[Doc]) -> tuple[list[Pair], list[Pair], list[Cheque], list[Doc]]:
    """Pair cheques with SAP documents.

    Same amount (± ``tolerance``) and a date near the sale make a pair, a
    shared product first, then the nearest date. A document without a so'm
    total can only pair through a shared product.

    Returns:
        ``(pairs, amount_differs, unmatched_cheques, unmatched_docs)``.
    """
    candidates = []
    for c in cheques:
        for d in docs:
            shared = shares_item(c, d)
            if d.amount_known:
                if (c.amount < 0) != (d.amount < 0) or abs(c.amount - d.amount) > tolerance(c.amount):
                    continue
            elif not shared:
                continue
            gap = date_gap(c, d)
            if gap is None:
                continue
            candidates.append((0 if shared else 1, gap, abs((d.day - c.day).days),
                               abs(c.amount - d.amount) if d.amount_known else 0, c.key, d.key))
    candidates.sort()
    by_c = {c.key: c for c in cheques}
    by_d = {d.key: d for d in docs}
    used_c: set[str] = set()
    used_d: set[str] = set()
    pairs: list[Pair] = []
    for _shared, _gap, _dated, _diff, ck, dk in candidates:
        if ck in used_c or dk in used_d:
            continue
        used_c.add(ck)
        used_d.add(dk)
        pairs.append(Pair(by_c[ck], by_d[dk]))

    # Same day (or the next), same product, different amount: entered with a mistake.
    differs: list[Pair] = []
    for c in sorted((c for c in cheques if c.key not in used_c), key=lambda c: (c.day, c.number)):
        options = sorted(
            (d for d in docs if d.key not in used_d and d.amount_known and shares_item(c, d)
             and date_gap(c, d) in (0, 1)),
            key=lambda d: (date_gap(c, d), abs(d.amount - c.amount)),
        )
        if options:
            used_c.add(c.key)
            used_d.add(options[0].key)
            differs.append(Pair(c, options[0]))

    return (
        pairs,
        differs,
        [c for c in cheques if c.key not in used_c],
        [d for d in docs if d.key not in used_d],
    )


def cancel_returns(cheques: list[Cheque]) -> tuple[list[Cheque], int]:
    """Drop a sale and a later return of the same goods and amount — neither reached SAP, rightly."""
    left = sorted(cheques, key=lambda c: (c.day, c.number))
    gone: set[str] = set()
    pairs = 0
    for ret in (c for c in left if c.amount < 0):
        for sale in left:
            if (sale.key in gone or ret.key in gone or sale.amount <= 0 or sale.day > ret.day
                    or abs(sale.amount + ret.amount) > tolerance(sale.amount)):
                continue
            sale_codes = {x for i in sale.items for x in (i.code, i.barcode) if x}
            ret_codes = {x for i in ret.items for x in (i.code, i.barcode) if x}
            if sale_codes and ret_codes and not sale_codes & ret_codes:
                continue
            gone.update((sale.key, ret.key))
            pairs += 1
            break
    return [c for c in left if c.key not in gone], pairs


def totals_only(day: date, billz_total: int, docs: list[Doc]) -> Result:
    """BILLZ gave lines without cheque ids: only the day's totals can be compared."""
    return Result(day=day, docs_day=[d for d in docs if d.day == day], no_cheque_numbers=True,
                  billz_total=billz_total)


def check(
    cheques: list[Cheque], docs: list[Doc], day: date, *, no_cheque_numbers: bool = False
) -> Result:
    """Compare the window's cheques with SAP and keep what's worth telling.

    Args:
        cheques: BILLZ cheques of the window (``day`` and the days before it).
        docs: SAP documents of the shops' warehouses, from the window's first
            day to today.
        day: The day being checked (yesterday): its numbers are summed, its
            unmatched documents reported, and late entries made on it or
            after are reported.
        no_cheque_numbers: BILLZ gave lines without a cheque id that day.
    """
    pairs, differs, missing, unmatched_docs = match(cheques, docs)
    missing, returned = cancel_returns(missing)
    late = [
        p for p in pairs + differs
        if (p.doc.created or p.doc.day) >= day
        and ((p.doc.created is not None and p.doc.created > p.cheque.day) or p.doc.day != p.cheque.day)
    ]
    return Result(
        day=day,
        cheques_day=[c for c in cheques if c.day == day],
        docs_day=[d for d in docs if d.day == day],
        missing=sorted(missing, key=lambda c: (c.day, c.number), reverse=True),
        amount_diff=sorted(differs, key=lambda p: (p.cheque.day, p.cheque.number), reverse=True),
        late=sorted(late, key=lambda p: (p.cheque.day, p.cheque.number), reverse=True),
        extra=sorted((d for d in unmatched_docs if d.day == day), key=lambda d: d.number),
        returned=returned,
        no_cheque_numbers=no_cheque_numbers,
        sap_amounts_missing=any(not d.amount_known for d in docs),
    )


# -------------------------------------------------------------------- render


def som(amount: int) -> str:
    """So'm, short: "8,7 млн сўм", "640 000 сўм"."""
    return format_uzs_short(to_tiyin(amount))


def _short_date(d: date) -> str:
    return d.strftime("%d.%m")


def _items(items: list[Item], limit: int = 2) -> str:
    names = [i.name for i in items if i.name]
    text = ", ".join(names[:limit])
    return text + ("…" if len(names) > limit else "")


def _section(title: str, bullets: list[str]) -> list[str]:
    lines = [title] + [f"   • {b}" for b in bullets[:MAX_LINES]]
    if len(bullets) > MAX_LINES:
        lines.append(f"   <i>+яна {len(bullets) - MAX_LINES} та</i>")
    return lines


def render(result: Result, *, pushed_at: datetime | None = None, day_end: datetime | None = None) -> str:
    """The Director's message (Telegram HTML, Uzbek Cyrillic).

    Args:
        result: What ``check`` found.
        pushed_at: When SAP's data last arrived (Tashkent time).
        day_end: Midnight after the checked day; a push before it may not
            hold that evening's entries, which the message then says.
    """
    title = f"🧾 <b>Billz ↔ SAP — {fmt_date(result.day)}</b>"
    note = ""
    if pushed_at is not None and day_end is not None and pushed_at < day_end:
        note = (f"\n<i>SAP маълумоти {pushed_at.strftime('%d.%m %H:%M')} ҳолатига — "
                "ундан кейин киритилганлари бу ерда кўринмайди.</i>")
    billz_total = result.billz_amount
    if result.no_cheque_numbers:
        diff = billz_total - result.sap_amount
        verdict = "✅ жами мос" if result.ok else f"🔴 фарқи {som(abs(diff))} ({'Billz кўп' if diff > 0 else 'SAP кўп'})"
        return (f"{title}\nBillz: {som(billz_total)}\nSAP: {len(result.docs_day)} та ҳужжат — "
                f"{som(result.sap_amount)}\n⚠️ Billz чек рақамларини бермади — фақат жами солиштирилди: "
                f"{verdict}.{note}")
    if result.ok:
        if not result.cheques_day:
            return f"{title}\n✅ Кеча дўконларда сотув бўлмаган, SAP'да ҳам ҳужжат йўқ.{note}"
        return (f"{title}\n✅ Кечаги {len(result.cheques_day)} та чекнинг ҳаммаси SAP'га киритилган "
                f"({som(billz_total)}).{note}")

    sap_total = result.sap_amount
    lines = [
        title,
        f"Billz: {len(result.cheques_day)} та чек — {som(billz_total)}",
        f"SAP: {len(result.docs_day)} та ҳужжат"
        + ("" if result.sap_amounts_missing else f" — {som(sap_total)}"),
    ]
    if result.sap_amounts_missing:
        lines.append("⚠️ SAP ҳужжатларида сўмдаги сумма келмади — товар ва сана бўйича солиштирилди.")
    several_shops = len({c.shop for c in result.missing if c.shop}) > 1
    if result.missing:
        total = sum(c.amount for c in result.missing)
        bullets = []
        for c in result.missing:
            what = _items(c.items)
            who = f" · сотувчи {escape(c.seller)}" if c.seller else ""
            kind = "қайтариш" if c.amount < 0 else "чек"
            shop = f" · {escape(c.shop)}" if several_shops and c.shop else ""
            bullets.append(f"{_short_date(c.day)} · {kind} {escape(c.number)} · {som(c.amount)}"
                           + (f" — {escape(what)}" if what else "") + who + shop)
        lines += [""] + _section(
            f"🔴 <b>SAP'га киритилмаган — {len(result.missing)} та, {som(total)}:</b>", bullets)
    if result.amount_diff:
        bullets = [
            f"{_short_date(p.cheque.day)} · чек {escape(p.cheque.number)}: Billz {som(p.cheque.amount)}, "
            f"SAP {som(p.doc.amount)} (№{escape(p.doc.number)})"
            for p in result.amount_diff
        ]
        lines += [""] + _section(f"🟡 <b>Суммаси фарқ қилади — {len(result.amount_diff)} та:</b>", bullets)
    if result.late:
        bullets = []
        for p in result.late:
            entered = p.doc.created or p.doc.day
            amount = f", {som(p.doc.amount)}" if p.doc.amount_known else ""
            if p.doc.created is not None and p.doc.created > p.cheque.day:
                text = (f"{_short_date(p.cheque.day)} сотуви {_short_date(entered)} куни киритилди "
                        f"(№{escape(p.doc.number)}{amount})")
                if p.doc.day != p.cheque.day:
                    text += f", SAP'даги санаси {_short_date(p.doc.day)}"
            else:  # on time, under another date
                text = (f"{_short_date(p.cheque.day)} сотуви SAP'га {_short_date(p.doc.day)} санаси билан "
                        f"киритилган (№{escape(p.doc.number)}{amount})")
            bullets.append(text)
        lines += [""] + _section(
            f"🟠 <b>Кечикиб ёки бошқа сана билан киритилган — {len(result.late)} та:</b>", bullets)
    if result.extra:
        bullets = [
            f"№{escape(d.number)}" + (f" · {som(d.amount)}" if d.amount_known else "")
            + (f" · {escape(d.customer)}" if d.customer else "")
            for d in result.extra
        ]
        lines += [""] + _section(f"⚪ <b>SAP'да бор, Billz'да йўқ — {len(result.extra)} та:</b>", bullets)
    return "\n".join(lines) + note


def render_stale(day: date, pushed_at: datetime | None) -> str:
    """When SAP's data is too old to compare with."""
    when = pushed_at.strftime("%d.%m %H:%M") if pushed_at else "ҳеч қачон"
    return (f"🧾 <b>Billz ↔ SAP — {fmt_date(day)}</b>\n"
            f"SAP маълумоти {when} дан бери янгиланмаган — солиштирилмади.")


def summary(result: Result) -> dict[str, Any]:
    """What's stored in ``billz_sap_checks.result`` (and read by the Director's questions)."""
    def cheque(c: Cheque) -> dict[str, Any]:
        return {"day": str(c.day), "number": c.number, "amount": c.amount, "shop": c.shop, "seller": c.seller,
                "items": [i.name for i in c.items]}

    def doc(d: Doc) -> dict[str, Any]:
        return {"day": str(d.day), "number": d.number, "amount": d.amount, "customer": d.customer,
                "created": str(d.created) if d.created else None}

    return {
        "day": str(result.day),
        "billz": {"cheques": len(result.cheques_day), "amount": result.billz_amount},
        "sap": {"documents": len(result.docs_day), "amount": result.sap_amount},
        "missing": [cheque(c) for c in result.missing],
        "amount_diff": [{"cheque": cheque(p.cheque), "doc": doc(p.doc)} for p in result.amount_diff],
        "late": [{"cheque": cheque(p.cheque), "doc": doc(p.doc)} for p in result.late],
        "extra": [doc(d) for d in result.extra],
        "returned": result.returned,
        "no_cheque_numbers": result.no_cheque_numbers,
    }
