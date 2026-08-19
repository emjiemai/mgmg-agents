"""Offline self-check for the pure logic — no network, no database.

Verifies the parts that would otherwise only be exercised against live SAP,
amoCRM and Telegram: money formatting, aging buckets, Telegram message
splitting, webhook payload parsing, and Verifix CSV parsing.

Run:
    python scripts/selfcheck.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.amocrm.webhook_handler import _extract_lead_events
from integrations.common.money import format_uzs, format_uzs_short, from_tiyin, to_tiyin, uzs_to_usd
from integrations.common.timeutil import TASHKENT, days_between, parse_sap_date, to_local, to_utc
from integrations.sap.client import aging_bucket
from integrations.telegram.bot import escape, split_message

FAILURES: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    """Assert equality and record the outcome.

    Args:
        label: Name of the case.
        actual: Value produced.
        expected: Value required.
    """
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {actual!r}, want {expected!r}")
        FAILURES.append(label)


def check_true(label: str, condition: bool) -> None:
    """Assert a condition holds.

    Args:
        label: Name of the case.
        condition: Must be True.
    """
    check(label, bool(condition), True)


def test_money() -> None:
    """Money conversion and Uzbek sum formatting."""
    print("money")
    nbsp = " "
    check("to_tiyin whole", to_tiyin(1250), 125000)
    check("to_tiyin decimal", to_tiyin("1250.75"), 125075)
    check("to_tiyin rounds half up", to_tiyin(0.005), 1)
    check("to_tiyin None", to_tiyin(None), 0)
    check("to_tiyin garbage", to_tiyin("n/a"), 0)
    check("from_tiyin", str(from_tiyin(125075)), "1250.75")
    check("format thousands", format_uzs(125000000), f"1{nbsp}250{nbsp}000{nbsp}so'm")
    check("format negative", format_uzs(-125000000), f"-1{nbsp}250{nbsp}000{nbsp}so'm")
    check("format no currency", format_uzs(125000000, with_currency=False), f"1{nbsp}250{nbsp}000")
    check("short mlrd", format_uzs_short(int(1.25e9 * 100)), f"1,25{nbsp}mlrd{nbsp}so'm")
    check("short mln", format_uzs_short(340_000_000 * 100), f"340{nbsp}mln{nbsp}so'm")
    check("short small", format_uzs_short(85_000 * 100), f"85{nbsp}000{nbsp}so'm")
    check("short negative", format_uzs_short(-int(2e9 * 100)), f"-2{nbsp}mlrd{nbsp}so'm")
    check("usd reference", str(uzs_to_usd(12_800 * 100)), "1.00")


def test_time() -> None:
    """Timezone conversion and SAP date parsing."""
    print("time")
    utc_noon = datetime(2026, 8, 18, 7, 0, tzinfo=to_utc(datetime(2026, 1, 1)).tzinfo)
    check("utc -> tashkent is +5", to_local(utc_noon).hour, 12)
    naive_local = datetime(2026, 8, 18, 9, 0)
    check("naive treated as tashkent", to_utc(naive_local).hour, 4)
    check("tashkent offset", TASHKENT.utcoffset(datetime(2026, 8, 18)), timedelta(hours=5))
    check("parse date", parse_sap_date("2026-08-18"), date(2026, 8, 18))
    check("parse datetime string", parse_sap_date("2026-08-18T00:00:00Z"), date(2026, 8, 18))
    check("parse empty", parse_sap_date(""), None)
    check("parse garbage", parse_sap_date("not-a-date"), None)
    check("days_between", days_between(date(2026, 8, 1), date(2026, 8, 18)), 17)
    check("days_between None", days_between(None), 0)


def test_aging() -> None:
    """AR aging bucket boundaries."""
    print("aging buckets")
    check("not due", aging_bucket(0), "current")
    check("future", aging_bucket(-5), "current")
    check("1 day", aging_bucket(1), "1_30")
    check("30 days", aging_bucket(30), "1_30")
    check("31 days", aging_bucket(31), "31_60")
    check("60 days", aging_bucket(60), "31_60")
    check("61 days", aging_bucket(61), "61_90")
    check("90 days", aging_bucket(90), "61_90")
    check("91 days", aging_bucket(91), "90_plus")


def test_telegram() -> None:
    """Message splitting and HTML escaping."""
    print("telegram")
    check("short message not split", len(split_message("hello")), 1)

    long_text = "\n".join(f"line {i} " + "x" * 100 for i in range(200))
    chunks = split_message(long_text)
    check_true("long message split", len(chunks) > 1)
    check_true("every chunk within limit", all(len(c) <= 3900 for c in chunks))
    check("no content lost", sum(c.count("line ") for c in chunks), 200)

    single_line = "y" * 9000
    hard = split_message(single_line)
    check_true("pathological line hard-cut", all(len(c) <= 3900 for c in hard))
    check("hard-cut keeps all characters", sum(len(c) for c in hard), 9000)

    check("escape ampersand", escape("Rogers & Co <Ltd>"), "Rogers &amp; Co &lt;Ltd&gt;")
    check("escape None", escape(None), "")


def test_webhook_parsing() -> None:
    """amoCRM webhook payloads in both shapes."""
    print("webhook parsing")

    form = {
        "leads[status][0][id]": "12345",
        "leads[status][0][status_id]": "142",
        "leads[status][0][pipeline_id]": "77",
        "leads[status][0][price]": "1500000",
        "leads[status][0][responsible_user_id]": "9",
        "account[subdomain]": "mgmg",
    }
    events = _extract_lead_events(form)
    check("form: one event", len(events), 1)
    check("form: lead id", events[0]["lead_id"], 12345)
    check("form: status", events[0]["status_id"], 142)
    check("form: price to tiyin", events[0]["price_tiyin"], 150_000_000)

    nested = {
        "leads": {
            "add": [{"id": 1, "pipeline_id": 7, "status_id": 3, "price": "500"}],
            "update": [{"id": 2, "pipeline_id": 7, "status_id": 4}],
        }
    }
    events = _extract_lead_events(nested)
    check("json: two events", len(events), 2)
    check("json: ids", sorted(e["lead_id"] for e in events), [1, 2])
    check("json: missing price is None", next(e for e in events if e["lead_id"] == 2)["price_tiyin"], None)

    check("empty payload", _extract_lead_events({}), [])
    check("unrelated payload", _extract_lead_events({"contacts[add][0][id]": "5"}), [])


def test_verifix_csv() -> None:
    """Verifix CSV parsing, column aliases and status inference."""
    print("verifix csv")
    import tempfile

    from integrations.common.config import settings
    from integrations.verifix.client import VerifixClient

    day = date(2026, 8, 18)
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / f"attendance_{day.isoformat()}.csv"
        csv_path.write_text(
            "employee_id;fio;otdel;shift_start;check_in;status;late\n"
            "101;Aliyev A.;Armin;09:00;09:00;present;0\n"
            "102;Karimov B.;IMUS;09:00;09:25;;25\n"
            "103;Yusupova C.;ONDRY;09:00;;absent;0\n"
            "104;Tashkentov D.;Service;09:00;09:40;present;0\n",
            encoding="utf-8",
        )
        original = settings.verifix_csv_dir
        settings.verifix_csv_dir = tmp
        try:
            records = VerifixClient(agent="selfcheck")._read_csv(day)
        finally:
            settings.verifix_csv_dir = original

    check("rows parsed", len(records), 4)
    by_id = {r.employee_id: r for r in records}
    check("present stays present", by_id["101"].status, "present")
    check("blank status + late minutes -> late", by_id["102"].status, "late")
    check("late minutes read", by_id["102"].late_minutes, 25)
    check("absent detected", by_id["103"].status, "absent")
    check("present but late is corrected", by_id["104"].status, "late")
    check("late minutes derived from times", by_id["104"].late_minutes, 40)
    check("name mapped via alias", by_id["101"].employee_name, "Aliyev A.")
    check("department mapped via alias", by_id["101"].department, "Armin")


def test_brief_rendering() -> None:
    """The CEO brief renders with partial and total source failure."""
    print("brief rendering")
    from integrations.common.agent_loader import load_agent
    from integrations.sap.models import ARAging, ARInvoice, CashAccount

    brief = load_agent("ceo-daily-brief")

    empty = brief.BriefData()
    empty.note_failure("sap", RuntimeError("connection refused"))
    text = brief.render(empty)
    check_true("failure shows as unavailable", "SAP unavailable" in text)
    check_true("failed sources named", "No data from: sap" in text)

    aging = ARAging(snapshot_date=date(2026, 8, 18))
    aging.invoices = [
        ARInvoice(
            doc_entry=1,
            doc_num=1001,
            card_code="C001",
            card_name="Buyuk Savdo MChJ",
            days_overdue=120,
            aging_bucket="90_plus",
            doc_total_tiyin=500_000_000,
            balance_due_tiyin=500_000_000,
            sales_person_name="Aliyev A.",
        )
    ]
    aging.total_open_tiyin = 500_000_000
    aging.total_overdue_tiyin = 500_000_000
    aging.bucket_totals_tiyin = {"90_plus": 500_000_000}
    aging.bucket_counts = {"90_plus": 1}

    full = brief.BriefData(
        cash=[CashAccount(account_code="5110", bank_name="Kapital Bank", balance_tiyin=1_200_000_000)],
        aging=aging,
    )
    text = brief.render(full)
    check_true("cash rendered", "Kapital Bank" in text)
    check_true("critical marker on 90+", "🔴" in text)
    check_true("owner named on largest invoice", "Aliyev A." in text)
    check_true("customer name escaped and present", "Buyuk Savdo MChJ" in text)


def test_receivables_rendering() -> None:
    """The receivables alert renders in both the empty and populated cases."""
    print("receivables rendering")
    from integrations.common.agent_loader import load_agent
    from integrations.sap.models import ARAging, ARInvoice

    receivables = load_agent("receivables")

    clean = ARAging(snapshot_date=date(2026, 8, 18), total_open_tiyin=10_000_000)
    text = receivables.render(clean, min_days=1)
    check_true("clean state is green", text.startswith("🟢"))

    aging = ARAging(snapshot_date=date(2026, 8, 18))
    aging.invoices = [
        ARInvoice(
            doc_entry=i,
            doc_num=1000 + i,
            card_code=f"C{i:03d}",
            card_name=f"Customer {i}",
            days_overdue=days,
            aging_bucket=aging_bucket(days),
            doc_total_tiyin=100_000_000,
            balance_due_tiyin=100_000_000,
            sales_person_name="Karimov B." if i % 2 else None,
        )
        for i, days in enumerate([5, 45, 75, 120, 200], start=1)
    ]
    aging.total_open_tiyin = 500_000_000
    aging.total_overdue_tiyin = 500_000_000
    aging.bucket_totals_tiyin = {"90_plus": 200_000_000}

    text = receivables.render(aging, min_days=1)
    check_true("headline critical", text.startswith("🔴"))
    check_true("90+ bucket shown", "90+ days" in text)
    check_true("1-30 bucket shown", "1–30 days" in text)
    check_true("owner breakdown present", "By owner:" in text)
    check_true("unmapped owner falls back to Other", "Other" in text)

    text_30 = receivables.render(aging, min_days=30)
    check_true("min_days filters the 5-day invoice", "1–30 days" not in text_30)


def main() -> int:
    """Run every check.

    Returns:
        0 if all checks pass, 1 otherwise.
    """
    for suite in (
        test_money,
        test_time,
        test_aging,
        test_telegram,
        test_webhook_parsing,
        test_verifix_csv,
        test_brief_rendering,
        test_receivables_rendering,
    ):
        suite()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
