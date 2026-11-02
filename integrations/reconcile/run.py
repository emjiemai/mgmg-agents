"""The 09:00 discrepancy report for accounting: SAP B1 ↔ 1C ↔ Billz (2026-11-02, T-2026-11-0048; rules in core.py).

  * **Who:** active Бухгалтерия people who hold SAP and 1C on ``/xodimlar`` (today: Зайнабхон) — at 09:00 on
    working days, inside the API's 5-minute round, once a day (``reconcile_runs``). ``/farqlar`` sends it now
    to them and to the Director.
  * **What:** one short message (counts per section) and an Excel workbook in the person's language (Uzbek or
    Russian, ``employees.lang``): summary, sales, money in, money out, customers' debt, suppliers' debt, the shop
    day by day, wrong data. The last 30 days (or since SAP's pushed data begins), up to yesterday.
  * Reads only: SAP's pushed data, 1C over OData (GET), Billz. A source that can't be read is named in the
    message and its sections stay empty — never a guessed zero. Logs keep counts only.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local, today_local
from integrations.onec import payables
from integrations.onec.payables import digits, num
from integrations.reconcile import core

AGENT = "reconcile"
log = setup_logging(AGENT)

COMMANDS = ("/farqlar", "/фарқлар", "/sverka", "/сверка")
SEND_HOUR, SEND_UNTIL_MINUTE = 9, 30
SHOP_DAYS = 7
DOC_SETS = {  # 1C document → (section, counterparty field)
    "Document_РеализацияТоваровУслуг": ("sales", "Контрагент_Key"),
    "Document_ПоступлениеНаРасчетныйСчет": ("money_in", "Контрагент"),
    "Document_ПриходныйКассовыйОрдер": ("money_in", "Контрагент"),
    "Document_СписаниеСРасчетногоСчета": ("money_out", "Контрагент"),
    "Document_РасходныйКассовыйОрдер": ("money_out", "Контрагент"),
}
RETAIL_SET = "Document_ОтчетОРозничныхПродажах"
# Only operations with a counterparty that SAP also records (checked live 02.11: 83 of 163 bank receipts were the
# shop's card-terminal money, and payments out held bank fees, salaries, taxes, transfers between our accounts).
OPERATIONS = {
    "Document_ПоступлениеНаРасчетныйСчет": {"ОплатаПокупателя", "ВозвратОтПоставщика"},
    "Document_СписаниеСРасчетногоСчета": {"ОплатаПоставщику", "ВозвратПокупателю"},
}
# How far back SAP's push is complete (push-ar-aging.ps1: sales 14 days, payments 45): each comparison uses its own.
SAP_DAYS = {"sales": 14, "payments": 30, "payments_out": 30}
RECEIVABLE, ADVANCE_IN = ("4010", "4015"), ("6310", "6315")


# ---------------------------------------------------------------------------------------------------- SAP


async def sap_tool(tool: str) -> list[dict[str, Any]]:
    rows = await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
    return [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}") for r in rows]


def shop_warehouses() -> frozenset[str]:
    from integrations.billz.sap_check import warehouse_map

    return frozenset().union(*warehouse_map(settings.billz_sap_warehouses).values())


def retail_cards(sales: list[dict[str, Any]], shop: frozenset[str]) -> set[str]:
    """The shop's customers: cards on the shop warehouses' invoices (30 on 02.11, never on a wholesale invoice)."""
    return {str(r.get("CardCode")).strip() for r in sales if str(r.get("WhsCode") or "").strip() in shop}


def sap_docs(rows: list[dict[str, Any]], inns: dict[str, str], *, sales: bool, shop: frozenset[str],
             skip: set[str] = frozenset()) -> tuple[list[core.Doc], dict[date, float]]:
    """Header-level SAP documents (one per DocEntry; credit notes negative); for sales also the shop's invoices
    by day (kept out of the document matching: 1C has them as retail reports). ``skip``: the shop's customers'
    payments (per cheque in SAP, card-terminal totals in 1C)."""
    docs: dict[str, core.Doc] = {}
    shop_by_day: dict[date, float] = {}
    for r in rows:
        if str(r.get("CANCELED") or r.get("Canceled") or "N") != "N" or not str(r.get("CardCode") or "").strip():
            continue
        if str(r.get("CardCode")).strip() in skip:
            continue
        key = str(r.get("DocEntry"))
        if key in docs:
            continue
        day = core.to_date(r.get("DocDate"))
        if day is None:
            continue
        sign = -1.0 if str(r.get("ObjType") or "") == "14" else 1.0
        amount = sign * num(r.get("DocTotalSy"))
        if sales and str(r.get("WhsCode") or "").strip() in shop:
            shop_by_day[day] = shop_by_day.get(day, 0.0) + amount
            docs[key] = None  # type: ignore[assignment]  # seen, not matched
            continue
        code = str(r.get("CardCode")).strip()
        docs[key] = core.Doc("SAP", day, str(r.get("DocNum") or key), inns.get(code, ""), str(r.get("CardName") or code),
                             amount, "қайтариш" if sign < 0 else "")
    return [d for d in docs.values() if d is not None], shop_by_day


def sap_start(kind: str, today: date) -> date:
    """Where SAP's pushed data of this kind is complete (SAP_DAYS)."""
    return today - timedelta(days=SAP_DAYS[kind])


# ---------------------------------------------------------------------------------------------------- 1C


async def read_onec(start: date, end: date) -> dict[str, Any]:
    """Everything the report needs from 1C, read now (GET only)."""
    from integrations.onec.client import OneCClient

    since = (start - timedelta(days=core.DATE_DAYS)).strftime("%Y-%m-%dT00:00:00")
    until = (end + timedelta(days=core.DATE_DAYS + 1)).strftime("%Y-%m-%dT00:00:00")
    async with OneCClient(agent=AGENT) as client:
        chart = (await client.get("ChartOfAccounts_Хозрасчетный", {"$select": "Ref_Key,Code,Description"})).get("value", [])
        moment = now_local().strftime("%Y-%m-%dT%H:%M:%S")
        rows = (await client.get(f"AccountingRegister_Хозрасчетный/Balance(Period=datetime'{moment}')")).get("value", [])
        parties = (await client.get("Catalog_Контрагенты", {"$select": "Ref_Key,Description,ИНН,IsFolder,DeletionMark"})).get("value", [])
        docs: dict[str, list[dict[str, Any]]] = {}
        for name in (*DOC_SETS, RETAIL_SET):
            try:
                got = (await client.get(name, {"$filter": f"Date ge datetime'{since}' and Date lt datetime'{until}'"})).get("value", [])
            except Exception as exc:  # noqa: BLE001 — a document type this base doesn't have: that part stays empty
                log.warning("1C {} not read: {}", name, type(exc).__name__)
                got = []
            docs[name] = [{k: v for k, v in d.items() if not isinstance(v, list)} for d in got]  # no tabular parts
        currencies = (await client.get("Catalog_Валюты", {"$select": "Ref_Key,Code,Description"})).get("value", [])
        contracts = (await client.get("Catalog_ДоговорыКонтрагентов", {"$select": "Ref_Key,Description"})).get("value", [])
    return {"as_of": moment, "chart": chart, "rows": rows, "parties": parties, "docs": docs,
            "currencies": currencies, "contracts": contracts}


def onec_amount(d: dict[str, Any], som_keys: set[str]) -> float:
    amount = num(d.get("СуммаДокумента"))
    cur = d.get("ВалютаДокумента_Key") or ""
    if not cur or cur in som_keys or cur == "00000000-0000-0000-0000-000000000000":
        return amount
    rate, times = num(d.get("КурсДокумента")), num(d.get("КратностьДокумента")) or 1.0
    return amount * rate / times if rate else amount


def onec_docs(data: dict[str, Any]) -> tuple[dict[str, list[core.Doc]], dict[date, float], set[str], list[dict[str, Any]]]:
    """1C documents by section (posted, not deleted, with a counterparty); retail reports by day; the
    counterparties in use; every document of the period (for the «not posted» check)."""
    parties = {p["Ref_Key"]: p for p in data["parties"]}
    som = {c["Ref_Key"] for c in data["currencies"] if str(c.get("Code")) == "860"}
    out: dict[str, list[core.Doc]] = {"sales": [], "money_in": [], "money_out": []}
    used: set[str] = set()
    every: list[dict[str, Any]] = []
    for name, (section, field) in DOC_SETS.items():
        for d in data["docs"].get(name, []):
            every.append({**d, "_kind": name.split("_", 1)[1]})
            if not d.get("Posted") or d.get("DeletionMark"):
                continue
            if name in OPERATIONS and d.get("ВидОперации") not in OPERATIONS[name]:
                continue
            key = d.get(field) or ""
            if field == "Контрагент" and not str(d.get("Контрагент_Type") or "").endswith("Catalog_Контрагенты"):
                continue  # a bank's own fee, a cash transfer, a person: not a counterparty document
            party = parties.get(key)
            if party is None:
                continue
            used.add(key)
            day = core.to_date(d.get("Date"))
            if day:
                out[section].append(core.Doc("1C", day, str(d.get("Number") or "").strip(), digits(party.get("ИНН")),
                                             str(party.get("Description") or ""), onec_amount(d, som)))
    retail: dict[date, float] = {}
    for d in data["docs"].get(RETAIL_SET, []):
        day = core.to_date(d.get("Date"))
        if day and d.get("Posted") and not d.get("DeletionMark"):
            retail[day] = retail.get(day, 0.0) + onec_amount(d, som)
    return out, retail, used, every


def onec_customer_balances(data: dict[str, Any]) -> list[core.Balance]:
    """What each counterparty owes us in 1C: 4010 + 4015 (debit) minus advances received 6310 + 6315 (credit)."""
    accounts = {a["Ref_Key"]: str(a.get("Code", "")).strip() for a in data["chart"]}
    parties = {p["Ref_Key"]: p for p in data["parties"]}
    sums: dict[str, float] = {}
    for row in data["rows"]:
        code = accounts.get(row.get("Account_Key"), "")
        if code not in RECEIVABLE + ADVANCE_IN or not str(row.get("ExtDimension1_Type", "")).endswith("Catalog_Контрагенты"):
            continue
        key = row.get("ExtDimension1") or ""
        dr, cr = num(row.get("СуммаBalanceDr")), num(row.get("СуммаBalanceCr"))
        sums[key] = sums.get(key, 0.0) + (dr - cr)
    return [core.Balance(digits(parties.get(k, {}).get("ИНН")), str(parties.get(k, {}).get("Description") or "(номи йўқ)"), v)
            for k, v in sums.items()]


def payables_data(data: dict[str, Any]) -> dict[str, Any]:
    """The same 1C read, shaped as ``payables.read_onec`` gives it (accounts 60 / 43) — one 1C read for both."""
    chart = [a for a in data["chart"] if str(a.get("Code", "")).startswith(("60", "43"))]
    keys = {a["Ref_Key"] for a in chart}
    return {"as_of": data["as_of"], "chart": chart, "rows": [r for r in data["rows"] if r.get("Account_Key") in keys],
            "parties": data["parties"], "contracts": data["contracts"], "currencies": data["currencies"]}


# ---------------------------------------------------------------------------------------------------- Billz


async def billz_by_day(days: list[date], run_id: uuid.UUID) -> dict[date, float] | None:
    from integrations.billz.client import BillzClient
    from integrations.billz.sales import day_sales

    try:
        async with BillzClient(agent=AGENT, run_id=run_id) as client:
            rows = await client.shop_days(days[0], days[-1])
    except Exception as exc:  # noqa: BLE001 — named as unread in the message
        log.warning("Billz not read: {}", type(exc).__name__)
        return None
    return {d: day_sales(rows, d).net_sales for d in days}


# ---------------------------------------------------------------------------------------------------- the report


async def build(run_id: uuid.UUID, today: date | None = None) -> core.Report:
    today = today or today_local()
    end = today - timedelta(days=1)
    sales_rows, pay_in, pay_out = await sap_tool("sales"), await sap_tool("payments"), await sap_tool("payments_out")
    customers, suppliers_rows, stock = await sap_tool("customers"), await sap_tool("supplier_balances"), await sap_tool("stock_items")
    start = min(sap_start(k, today) for k in SAP_DAYS)
    report = core.Report(today, start, end)
    if not (sales_rows or pay_in or customers):
        report.missing.append("SAP")
    inns = {str(c.get("CardCode")).strip(): digits(c.get("LicTradNum")) for c in customers + suppliers_rows}
    shop = shop_warehouses()
    retail = retail_cards(sales_rows, shop)
    s_sales, s_shop = sap_docs(sales_rows, inns, sales=True, shop=shop)
    s_in, _ = sap_docs(pay_in, inns, sales=False, shop=shop, skip=retail)
    s_out, _ = sap_docs(pay_out, inns, sales=False, shop=shop)
    report.problems += core.sap_problems([c for c in customers if str(c.get("CardCode")).strip() not in retail],
                                         [r for r in sales_rows if str(r.get("CardCode")).strip() not in retail],
                                         [p for p in pay_in if str(p.get("CardCode")).strip() not in retail], stock, today)

    onec: dict[str, Any] | None = None
    if settings.onec_configured:
        try:
            onec = await read_onec(start, end)
        except Exception as exc:  # noqa: BLE001
            log.warning("1C not read: {}", type(exc).__name__)
    if onec is None:
        report.missing.append("1C")
        retail_1c: dict[date, float] = {}
    else:
        o_docs, retail_1c, used, every = onec_docs(onec)
        report.sales = core.match_docs(s_sales, o_docs["sales"], sap_start("sales", today), end)
        report.money_in = core.match_docs(s_in, o_docs["money_in"], sap_start("payments", today), end)
        report.money_out = core.match_docs(s_out, o_docs["money_out"], sap_start("payments_out", today), end)
        sap_cust = [core.Balance(digits(c.get("LicTradNum")), str(c.get("CardName") or ""), num(c.get("BalanceSys")),
                                 str(c.get("CardCode") or "")) for c in customers if str(c.get("CardCode")).strip() not in retail]
        report.customers = core.compare_balances(sap_cust, onec_customer_balances(onec))
        report.suppliers = payables.compare(payables_data(onec), suppliers_rows, "SAP")
        report.problems += core.onec_problems(onec["parties"], [d for d in every if start <= (core.to_date(d.get("Date")) or start) <= end], used)

    days = [end - timedelta(days=i) for i in range(SHOP_DAYS - 1, -1, -1)]
    billz = await billz_by_day(days, run_id)
    if billz is None:
        report.missing.append("Billz")
    report.shop = core.shop_days(days, billz, s_shop, retail_1c)
    return report


# ---------------------------------------------------------------------------------------------------- texts

STATUS = {
    "uz": {"match": "мос", "date": "санаси бошқа", "amount": "сумма фарқ", "only_sap": "фақат SAP'да", "only_1c": "фақат 1C'да",
           "diff": "фарқ бор"},
    "ru": {"match": "совпадает", "date": "другая дата", "amount": "расхождение суммы", "only_sap": "только в SAP",
           "only_1c": "только в 1С", "diff": "расхождение"},
}
PROBLEM_TEXT = {
    "uz": {"inn_missing": "ИНН йўқ", "inn_bad": "ИНН нотўғри (9 ёки 14 рақам эмас)", "inn_twice": "бир ИНН икки карточкада",
           "no_salesperson": "сотувчи кўрсатилмаган", "future_date": "санаси келажакда", "entered_late": f"{core.LATE_ENTRY_DAYS} кундан кеч киритилган",
           "zero_price": "нархи 0", "payment_no_party": "тўловда контрагент йўқ", "stock_negative": "қолдиқ манфий",
           "unposted": "ҳужжат ўтказилмаган (проведён эмас)"},
    "ru": {"inn_missing": "нет ИНН", "inn_bad": "ИНН неверный (не 9 и не 14 цифр)", "inn_twice": "один ИНН на двух карточках",
           "no_salesperson": "не указан продавец", "future_date": "дата в будущем", "entered_late": f"введён позже {core.LATE_ENTRY_DAYS} дней",
           "zero_price": "цена 0", "payment_no_party": "в платеже нет контрагента", "stock_negative": "отрицательный остаток",
           "unposted": "документ не проведён"},
}
TEXT = {
    "uz": {
        "sheets": ("Хулоса", "Сотув", "Кирим", "Чиқим", "Мижоз қарзи", "Етказиб берувчи қарзи", "Дўкон", "Нотўғри маълумот"),
        "title": "SAP B1 ↔ 1C ↔ Billz солиштириш", "period": "Давр", "made": "Тузилди", "unread": "Ўқилмади",
        "summary_head": ("Бўлим", "Фарқ / хато", "Нима солиштирилди"),
        "sections": {
            "sales": ("Сотув ҳужжатлари", "SAP счёт-фактуралари (дўкондан ташқари) ↔ 1C «Реализация»"),
            "money_in": ("Кирим тўловлари", "SAP кирим тўловлари ↔ 1C банкка тушум / ПКО"),
            "money_out": ("Чиқим тўловлари", "SAP чиқим тўловлари ↔ 1C банкдан ўтказма / РКО"),
            "customers": ("Мижозлар қарзи", "SAP мижоз сальдоси ↔ 1C 4010+4015 − 6310+6315"),
            "suppliers": ("Етказиб берувчилар қарзи", "SAP етказиб берувчи сальдоси ↔ 1C 6010+6015 − 4310+4315"),
            "shop": ("Дўкон, кунма-кун", "Billz ↔ SAP дўкон счётлари ↔ 1C чакана савдо ҳисоботи"),
            "problems": ("Нотўғри маълумот", "ИНН, сотувчи, нарх, сана, қолдиқ, ўтказилмаган ҳужжатлар"),
        },
        "doc_head": ("Ҳолат", "Контрагент", "ИНН", "SAP №", "SAP санаси", "SAP сумма, сўм", "1C №", "1C санаси", "1C сумма, сўм", "Фарқ (1C − SAP), сўм", "Изоҳ"),
        "bal_head": ("Ҳолат", "Контрагент (SAP)", "Контрагент (1C)", "ИНН", "SAP, сўм", "1C, сўм", "Фарқ (1C − SAP), сўм", "Боғланди"),
        "sup_head": ("Контрагент (1C)", "Контрагент (SAP)", "ИНН", "1C нетто, сўм", "SAP, сўм", "Фарқ (1C − SAP), сўм", "Эҳтимолий сабаб"),
        "shop_head": ("Кун", "Billz, сўм", "SAP дўкон, сўм", "1C чакана, сўм", "SAP − Billz", "1C − Billz", "Ҳолат"),
        "prob_head": ("Тизим", "Хато", "Ёзув", "Тафсилот"),
        "shop_ok": "мос", "shop_bad": "фарқ бор", "no_billz": "Billz ўқилмади",
        "note": "Изоҳ: суммалар сўмда (SAP — тизим валютаси, 1C — ҳужжат курсида). Мос = 1 000 сўм ёки 0,5 % гача фарқ. "
                "Ҳужжатлар контрагент (ИНН, бўлмаса номи), сумма ва сана (± 3 кун) бўйича боғланди.",
        "returns": "қайтариш",
    },
    "ru": {
        "sheets": ("Сводка", "Продажи", "Поступления", "Списания", "Дебиторка", "Кредиторка", "Магазин", "Ошибки в данных"),
        "title": "Сверка SAP B1 ↔ 1С ↔ Billz", "period": "Период", "made": "Составлено", "unread": "Не удалось прочитать",
        "summary_head": ("Раздел", "Расхождения / ошибки", "Что сверялось"),
        "sections": {
            "sales": ("Документы продаж", "Счета SAP (кроме магазина) ↔ «Реализация» 1С"),
            "money_in": ("Поступления", "Входящие платежи SAP ↔ поступления на р/с и ПКО 1С"),
            "money_out": ("Списания", "Исходящие платежи SAP ↔ списания с р/с и РКО 1С"),
            "customers": ("Дебиторская задолженность", "Сальдо клиента SAP ↔ 1С 4010+4015 − 6310+6315"),
            "suppliers": ("Кредиторская задолженность", "Сальдо поставщика SAP ↔ 1С 6010+6015 − 4310+4315"),
            "shop": ("Магазин по дням", "Billz ↔ счета магазина SAP ↔ отчёт о розничных продажах 1С"),
            "problems": ("Ошибки в данных", "ИНН, продавец, цена, дата, остатки, непроведённые документы"),
        },
        "doc_head": ("Статус", "Контрагент", "ИНН", "SAP №", "Дата SAP", "Сумма SAP, сум", "1С №", "Дата 1С", "Сумма 1С, сум", "Разница (1С − SAP), сум", "Примечание"),
        "bal_head": ("Статус", "Контрагент (SAP)", "Контрагент (1С)", "ИНН", "SAP, сум", "1С, сум", "Разница (1С − SAP), сум", "Связано по"),
        "sup_head": ("Контрагент (1С)", "Контрагент (SAP)", "ИНН", "1С нетто, сум", "SAP, сум", "Разница (1С − SAP), сум", "Возможная причина"),
        "shop_head": ("День", "Billz, сум", "SAP магазин, сум", "1С розница, сум", "SAP − Billz", "1С − Billz", "Статус"),
        "prob_head": ("Система", "Ошибка", "Запись", "Подробно"),
        "shop_ok": "совпадает", "shop_bad": "расхождение", "no_billz": "Billz не прочитан",
        "note": "Суммы в сумах (SAP — системная валюта, 1С — по курсу документа). Совпадает = разница до 1 000 сум или 0,5 %. "
                "Документы связаны по контрагенту (ИНН, иначе название), сумме и дате (± 3 дня).",
        "returns": "возврат",
    },
}
SECTIONS = ("sales", "money_in", "money_out", "customers", "suppliers", "shop", "problems")


def message(report: core.Report) -> str:
    """The chat message (Uzbek — fixed bot text): counts per section; the details are in the file."""
    c = report.counts()
    names = TEXT["uz"]["sections"]
    lines = [f"🔎 <b>SAP ↔ 1C ↔ Billz солиштириш</b> · {report.day:%d.%m}",
             f"Давр: {report.start:%d.%m}–{report.end:%d.%m}", ""]
    for key in SECTIONS:
        n = c[key]
        lines.append(f"{'⚠️' if n else '✅'} {names[key][0]}: {n if n else 'мос'}")
    if report.missing:
        lines += ["", "❗ Ўқилмади: " + ", ".join(report.missing) + " — бу бўлимлар бўш."]
    lines += ["", "<i>Ҳар бир фарқ ва хато — Excel файлда.</i>"]
    return "\n".join(lines)


def workbook_bytes(report: core.Report, lang: str = "uz") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    t = TEXT.get(lang, TEXT["uz"])
    st = STATUS.get(lang, STATUS["uz"])
    bad = PatternFill("solid", fgColor="FCECEA")
    wb = Workbook()
    sheets = [wb.active] + [wb.create_sheet() for _ in t["sheets"][1:]]
    for ws, name in zip(sheets, t["sheets"]):
        ws.title = name[:31]

    def table(ws, head: tuple[str, ...], rows: list[tuple[list[Any], bool]], widths: list[int]) -> None:
        ws.append(list(head))
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        first = ws.max_row
        for values, problem in rows:
            ws.append(values)
            if problem:
                for cell in ws[ws.max_row]:
                    cell.fill = bad
        for n, w in enumerate(widths, start=1):
            ws.column_dimensions[ws.cell(row=first, column=n).column_letter].width = w
        for row in ws.iter_rows(min_row=first + 1):
            for cell in row:
                if isinstance(cell.value, float):
                    cell.number_format = "# ##0"
        ws.freeze_panes = ws.cell(row=first + 1, column=1)
        ws.auto_filter.ref = f"A{first}:{ws.cell(row=first, column=len(head)).column_letter}{ws.max_row}"

    c = report.counts()
    s0 = sheets[0]
    s0.append([t["title"]])
    s0["A1"].font = Font(bold=True, size=13)
    s0.append([f"{t['period']}: {report.start:%d.%m.%Y}–{report.end:%d.%m.%Y} · {t['made']}: {now_local():%d.%m.%Y %H:%M}"])
    if report.missing:
        s0.append([f"{t['unread']}: " + ", ".join(report.missing)])
    s0.append([])
    table(s0, t["summary_head"], [([t["sections"][k][0], c[k], t["sections"][k][1]], c[k] > 0) for k in SECTIONS], [32, 18, 70])
    s0.append([])
    s0.append([t["note"]])

    def doc_rows(pairs: list[core.DocPair]) -> list[tuple[list[Any], bool]]:
        out = []
        for p in pairs:
            a = p.sap or p.onec
            out.append(([st[p.status], a.name, a.inn,
                         p.sap.number if p.sap else "", p.sap.day if p.sap else None, round(p.sap.amount, 2) if p.sap else None,
                         p.onec.number if p.onec else "", p.onec.day if p.onec else None, round(p.onec.amount, 2) if p.onec else None,
                         round(p.diff, 2), t["returns"] if p.sap and p.sap.note else ""], p.problem))
        return out

    widths = [18, 38, 14, 10, 12, 16, 12, 12, 16, 18, 12]
    for ws, pairs in zip(sheets[1:4], (report.sales, report.money_in, report.money_out)):
        table(ws, t["doc_head"], doc_rows(pairs), widths)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, date):
                    cell.number_format = "DD.MM.YYYY"
    table(sheets[4], t["bal_head"], [([st[p.status], p.sap.name if p.sap else "", p.onec.name if p.onec else "",
                                       (p.sap or p.onec).inn, round(p.sap.amount, 2) if p.sap else None,
                                       round(p.onec.amount, 2) if p.onec else None, round(p.diff, 2), p.how], p.status != "match")
                                     for p in report.customers], [14, 38, 38, 14, 16, 16, 18, 10])
    sup_rows = []
    if report.suppliers is not None:
        for m in report.suppliers.pairs:
            reason = payables.reason(m)
            sup_rows.append(([m.onec.name if m.onec else "", m.sap.name if m.sap else "", (m.onec or m.sap).inn,
                              round(m.onec.net, 2) if m.onec else None, round(m.sap.owed, 2) if m.sap else None,
                              round(m.diff, 2), payables.say(reason, lang)], reason != "Мос"))
        sup_rows.sort(key=lambda r: (not r[1], -abs(r[0][5] or 0)))
    table(sheets[5], t["sup_head"], sup_rows, [38, 38, 14, 16, 16, 18, 60])
    table(sheets[6], t["shop_head"], [([d.day, round(d.billz, 2) if d.billz is not None else t["no_billz"], round(d.sap, 2),
                                        round(d.onec, 2), round(d.sap - d.billz, 2) if d.billz is not None else None,
                                        round(d.onec - d.billz, 2) if d.billz is not None else None,
                                        t["shop_bad"] if d.problem else t["shop_ok"]], d.problem) for d in report.shop],
          [12, 16, 16, 16, 16, 16, 14])
    for row in sheets[6].iter_rows(min_row=2):
        if isinstance(row[0].value, date):
            row[0].number_format = "DD.MM.YYYY"
    pt = PROBLEM_TEXT.get(lang, PROBLEM_TEXT["uz"])
    table(sheets[7], t["prob_head"], [([p.system, pt[p.kind], p.what, p.detail], True) for p in report.problems], [8, 34, 60, 30])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def file_name(report: core.Report, lang: str) -> str:
    return f"{'sverka' if lang == 'ru' else 'farqlar'}-{report.day:%Y-%m-%d}.xlsx"


# ---------------------------------------------------------------------------------------------------- sending


async def recipients() -> list[dict[str, Any]]:
    """Active Бухгалтерия people who hold both SAP and 1C (``/xodimlar``)."""
    return await fetch_all(
        "SELECT * FROM employees WHERE status = 'active' AND role = 'buxgalteriya' AND data_access @> ARRAY['sap', '1c']")


async def send(people: list[dict[str, Any]], run_id: uuid.UUID, report: core.Report | None = None) -> int:
    from integrations.telegram.bot import TelegramBot, TelegramError

    if not people or settings.bots_frozen:
        return 0
    report = report or await build(run_id)
    sent = 0
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        for person in people:
            lang = "ru" if (person.get("lang") or "") == "ru" else "uz"
            try:
                await bot.send_message(message(report), chat_id=str(person["telegram_user_id"]))
                await bot.send_file(workbook_bytes(report, lang), file_name(report, lang), str(person["telegram_user_id"]))
                sent += 1
            except TelegramError as exc:
                log.warning("Discrepancy report not delivered: {}", type(exc).__name__)
    await log_action(agent=AGENT, action="report_sent", target_system="telegram", status="success" if sent else "partial",
                     run_id=run_id, mode="notify", payload={**report.counts(), "sent": sent, "missing": report.missing})
    return sent


async def tick(now: datetime) -> int:
    """The API's 5-minute round: at 09:00–09:30 on a working day, once."""
    from integrations.org_bot import store

    if now.hour != SEND_HOUR or now.minute >= SEND_UNTIL_MINUTE or now.weekday() == 6:
        return 0
    if await store.is_day_off(now.date()):
        return 0
    claimed = await fetch_one("INSERT INTO reconcile_runs (day) VALUES (%s) ON CONFLICT (day) DO NOTHING RETURNING day",
                              (now.date(),))
    if claimed is None:
        return 0
    run_id = uuid.uuid4()
    try:
        return await send(await recipients(), run_id)
    except Exception:
        # Failed before anyone got it: let the next round (until 09:30) try again.
        await fetch_one("DELETE FROM reconcile_runs WHERE day = %s RETURNING day", (now.date(),))
        raise


async def send_now(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    """/farqlar: the report now, to the person who asked (accounting with SAP + 1C, or the Director)."""
    sent = await send([employee], run_id)
    return "reconcile_sent" if sent else "reconcile_failed"
