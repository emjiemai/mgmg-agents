"""Offline self-check for the pure logic — no network, no database.

Verifies the parts that would otherwise only be exercised against live SAP,
the CRM and Telegram: money formatting, aging buckets, Telegram message
splitting, org_bot parsing, and brief/receivables rendering.

Run:
    python scripts/selfcheck.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.common.money import (
    format_money,
    format_money_by_currency,
    format_uzs,
    format_uzs_short,
    from_tiyin,
    to_tiyin,
    uzs_to_usd,
)
from integrations.common.timeutil import TASHKENT, days_between, parse_sap_date, to_local, to_utc
from integrations.org_bot.ops_manager import (
    _task_card_text,
    _task_keyboard,
    parse_callback_data,
    parse_role_and_request,
    validate_classification,
)
from integrations.org_bot.roles import AGENT_SLUGS, ROLE_SLUGS
from integrations.telegram.bot import sanitize_model_html
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

    # 2026-09-07: SAP AR invoices turned out to include real USD-denominated
    # ones that format_uzs/format_uzs_short were silently mislabeling as
    # so'm (right number, wrong currency). format_money/format_money_by_currency
    # replace every such call site in receivables + ceo-daily-brief.
    check("format_money UZS unchanged", format_money(125000000, "UZS"), format_uzs(125000000))
    check("format_money USD", format_money(976400, "USD"), "$9,764.00")
    check("format_money None currency defaults UZS", format_money(125000000, None), format_uzs(125000000))
    check("format_money unknown currency code", format_money(150000, "EUR"), "1,500.00 EUR")
    check(
        "format_money_by_currency single currency == format_money",
        format_money_by_currency([(125000000, "UZS")]),
        format_uzs(125000000),
    )
    check(
        "format_money_by_currency sums within a currency, not across",
        format_money_by_currency([(1_000_000, "UZS"), (500_000, "UZS"), (976_400, "USD")]),
        f"15{nbsp}000{nbsp}so'm + $9,764.00",
    )
    check("format_money_by_currency empty", format_money_by_currency([]), format_uzs(0))


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


def test_org_bot() -> None:
    """OPS Manager Bot's callback parsers and classification validator."""
    print("org_bot")

    check("callback: setrole splits once", parse_callback_data("setrole:it:req-1"), ("setrole", "it:req-1"))
    check("callback: taskdone splits once", parse_callback_data("taskdone:task-1"), ("taskdone", "task-1"))
    check("callback: no colon is malformed", parse_callback_data("garbage"), None)

    check("role+request: splits once", parse_role_and_request("it:req-1"), ("it", "req-1"))
    check("role+request: id itself may contain colons", parse_role_and_request("it:req:1"), ("it", "req:1"))
    check("role+request: no colon is malformed", parse_role_and_request("it"), None)

    check_true("every role slug is a known role", all(r in ROLE_SLUGS for r in ("it", "hr", "ombor")))
    check_true("bogus role slug is rejected", "not_a_role" not in ROLE_SLUGS)

    # 2026-09-16: a picked role now needs the admin's second Accept.
    from integrations.org_bot.admin import role_decision_keyboard

    role_buttons = role_decision_keyboard("3fa85f64-5717-4562-b3fc-2c963f66afa6")["inline_keyboard"][0]
    check(
        "role request card has Accept + Reject",
        [b["callback_data"].split(":", 1)[0] for b in role_buttons],
        ["role_approve", "role_reject"],
    )
    check_true(
        "role request callback fits Telegram's 64-byte limit",
        all(len(b["callback_data"].encode()) <= 64 for b in role_buttons),
    )

    check(
        "classify: valid employee route",
        validate_classification({"target_type": "employee", "target_role": "it", "target_agent": None}),
        ("employee", "it", None),
    )
    check(
        "classify: valid agent route",
        validate_classification({"target_type": "agent", "target_role": None, "target_agent": "lead_agent"}),
        ("agent", None, "lead_agent"),
    )
    check(
        "classify: explicit none is valid, not an error",
        validate_classification({"target_type": "none"}),
        ("none", None, None),
    )
    check(
        "classify: refused (guardrail path) is valid, not an error",
        validate_classification({"target_type": "refused", "task_summary": "Kechirasiz, bunga yordam bera olmayman."}),
        ("refused", None, None),
    )
    check(
        "classify: employee with unknown role slug is rejected",
        validate_classification({"target_type": "employee", "target_role": "made_up_role"}),
        None,
    )
    check(
        "classify: agent with unknown agent slug is rejected",
        validate_classification({"target_type": "agent", "target_agent": "made_up_agent"}),
        None,
    )
    check(
        "classify: employee route missing its role is rejected",
        validate_classification({"target_type": "employee", "target_role": None}),
        None,
    )
    check("classify: malformed response is rejected", validate_classification({}), None)
    check_true("agent vocabulary is non-empty", len(AGENT_SLUGS) > 0)

    check_true("task card text carries the summary", "Ombor" in _task_card_text("Ombor tekshiruvi"))
    fresh_kb = _task_keyboard("task-1")
    check("fresh task has Start + Done buttons", len(fresh_kb["inline_keyboard"][0]), 2)
    started_kb = _task_keyboard("task-1", started=True)
    check("started task has only the Done button", len(started_kb["inline_keyboard"][0]), 1)
    check_true("Start button carries the right callback_data", "taskstart:task-1" in str(fresh_kb))
    check_true("Done button carries the right callback_data", "taskdone:task-1" in str(fresh_kb))
    check_true("started keyboard has no Start button left", "taskstart:" not in str(started_kb))

    # The real bug: escape() alone turns a model's deliberate <b> into
    # literal visible "<b>" text once Telegram's HTML parser unescapes
    # &lt;b&gt; back to a literal "<" for display. sanitize_model_html must
    # let the allowed tags through as real tags while still neutralizing
    # anything else, so a stray "<" from underlying data can't break the
    # HTML parse or render as unintended markup.
    check(
        "sanitize: allowed <b> survives as a real tag",
        sanitize_model_html("Eng yaxshi lid: <b>JW Marriott</b>"),
        "Eng yaxshi lid: <b>JW Marriott</b>",
    )
    check(
        "sanitize: allowed <i> survives as a real tag",
        sanitize_model_html("<i>Eslatma</i>: tekshiring"),
        "<i>Eslatma</i>: tekshiring",
    )
    check(
        "sanitize: an unrelated tag is neutralized, not rendered",
        sanitize_model_html("narx < 100 va <script>alert(1)</script>"),
        "narx &lt; 100 va &lt;script&gt;alert(1)&lt;/script&gt;",
    )
    check("sanitize: None becomes empty string", sanitize_model_html(None), "")


def test_brief_rendering() -> None:
    """The CEO brief renders with partial and total source failure."""
    print("brief rendering")
    from integrations.common.agent_loader import load_agent
    from integrations.crm.models import EmployeeReport
    from integrations.sap.models import ARAging, ARInvoice, CashAccount

    brief = load_agent("ceo-daily-brief")

    empty = brief.BriefData()
    empty.note_failure("sap", RuntimeError("connection refused"))
    text = brief.render(empty)
    check_true("failure shows as not-connected", "Hali ulanmagan" in text)
    check_true("failed sources named", "Ma'lumot yo'q: sap" in text)

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

    yesterday = date.today() - timedelta(days=1)
    full = brief.BriefData(
        cash=[CashAccount(account_code="5110", bank_name="Kapital Bank", balance_tiyin=1_200_000_000)],
        aging=aging,
        reports=[
            EmployeeReport(
                id=1,
                manager_name="Ulug'bek AI",
                report_type="daily",
                report_date=datetime(yesterday.year, yesterday.month, yesterday.day),
                content="OPS Manager botdagi bug fixlar, formatlash to'g'irlash",
            )
        ],
    )
    text = brief.render(full)
    check_true("cash rendered", "Kapital Bank" in text)
    check_true("critical marker on 90+", "🔴" in text)
    # 2026-09-07: the brief's receivables section was deliberately trimmed to
    # a headline total, not per-invoice detail (owner/customer names) --
    # that detail now lives only in the standalone Receivables Agent alert,
    # which already sends it moments later. Assert the new headline +
    # signpost instead of names that no longer belong in this section.
    check_true("overdue total shown", format_money(500_000_000, "UZS") in text)
    check_true("points to the detailed alert instead of repeating it", "batafsili keyingi xabarda" in text)
    # 2026-09-08: previous day's employee-submitted CRM reports, added after
    # a Director's actual submitted report never showed up anywhere the bot
    # would surface it proactively -- only on direct question.
    check_true("yesterday's report shown", "OPS Manager botdagi bug fixlar" in text)
    check_true("reporting employee named", "Ulug'bek AI" in text)
    # 2026-09-15: Verifix attendance and Microsoft Planner were removed from
    # the project, so their sections must not come back.
    check_true("no attendance section", "Davomat" not in text)
    check_true("no Planner tasks section", "Vazifalar" not in text)

    no_reports = brief.BriefData(reports=[])
    text = brief.render(no_reports)
    check_true("no reports yesterday is stated plainly", "Hech kim report yozmagan" in text)

    # 2026-09-16: daily reports no longer reach the Director one by one; the
    # brief names only who didn't report on the last day they were asked.
    asked_day = date(2026, 9, 15)
    rows = [
        {"report_date": asked_day, "status": "submitted", "display_name": "Aziz", "role": "it"},
        {"report_date": asked_day, "status": "asked", "display_name": "Dmitriy", "role": "b2b_sotuv"},
    ]
    text = brief.render(brief.BriefData(report_rows=rows))
    check_true("non-reporter named", "Dmitriy" in text)
    check_true("someone who reported is not named", "Aziz" not in text)
    check_true("missed out of asked is shown", "1 / 2" in text)
    everyone_in = [dict(r, status="submitted") for r in rows]
    check_true(
        "everyone reporting is stated",
        "hammasi yubordi (2/2)" in brief.render(brief.BriefData(report_rows=everyone_in)),
    )
    never_asked = brief.render(brief.BriefData(report_rows=[]))
    check_true(
        "no section before anyone was ever asked",
        "Kunlik hisobotlar" not in never_asked and "yubormaganlar" not in never_asked,
    )

    # 2026-09-18: with daily reports switched off, the brief must not keep
    # naming the last asked day's non-reporters. (Returns before any DB call.)
    import asyncio

    from integrations.common.config import settings

    if not settings.daily_reports_enabled:
        check("switched off: no report rows fetched", asyncio.run(brief._fetch_report_results()), [])


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
    check_true("90+ bucket shown", "90+ kun" in text)
    check_true("1-30 bucket shown", "1–30 kun" in text)
    check_true("owner breakdown present", "Mas'ul xodim bo'yicha:" in text)
    check_true("unmapped owner falls back to Boshqa", "Boshqa" in text)

    text_30 = receivables.render(aging, min_days=30)
    check_true("min_days filters the 5-day invoice", "1–30 kun" not in text_30)


def test_daily_report_kpi() -> None:
    """Parsing employees' reported numbers, and how they render against target."""
    print("daily report KPI")
    from integrations.org_bot import kpi

    # 2026-09-16: nobody is asked for numbers — every role, sales included,
    # just reports what they did today.
    check("B2B Sotuv is not asked for numbers", kpi.metrics_for_role("b2b_sotuv"), ())
    check("Garmin Sotuv is not asked for numbers", kpi.metrics_for_role("garmin_sotuv"), ())
    check_true(
        "B2B Sotuv's ask has no numbers block",
        "Qo'ng'iroqlar" not in kpi.build_request_text("Dmitriy", kpi.metrics_for_role("b2b_sotuv")),
    )
    check_true(
        "reminder has no numbers block",
        "Qo'ng'iroqlar" not in kpi.build_reminder_text(kpi.metrics_for_role("garmin_sotuv")),
    )

    # The parser and formatter stay tested against the (currently unassigned)
    # sales set, so switching numbers back on for a role is safe.
    sales = kpi.SALES_METRICS
    check("sales metric set still defined", len(sales), 4)

    check(
        "labelled Uzbek reply",
        kpi.parse_metrics("Bugun uchrashuvlar 3, qo'ng'iroqlar 22, KP 5, yangi lidlar 2", sales),
        {"meetings": 3, "calls": 22, "proposals": 5, "new_leads": 2},
    )
    check(
        "number before the label, and a different apostrophe",
        kpi.parse_metrics("3 ta uchrashuv, 22 ta qoʻngʻiroq", sales),
        {"meetings": 3, "calls": 22},
    )
    check(
        "Russian labels",
        kpi.parse_metrics("встречи: 4, звонки: 30", sales),
        {"meetings": 4, "calls": 30},
    )
    check(
        "bare positional line",
        kpi.parse_metrics("3/20/5/2", sales),
        {"meetings": 3, "calls": 20, "proposals": 5, "new_leads": 2},
    )
    # A report with no numbers must never be given invented ones — the
    # Director's scorecard has to distinguish "nothing reported" from "zero".
    check("prose with no numbers stays empty", kpi.parse_metrics("Bugun ofisda ishladim.", sales), {})
    check("wrong count of bare numbers is not positional", kpi.parse_metrics("3 20", sales), {})
    check("no metrics for this role means no parsing", kpi.parse_metrics("uchrashuv 3", ()), {})

    rendered = kpi.format_metrics({"meetings": 5, "calls": 10}, sales)
    check_true("hit target marked", "Uchrashuvlar: 5/4 ✅" in rendered)
    check_true("under target marked", "Qo'ng'iroqlar: 10/15 ⚠️" in rendered)
    check_true("unreported metric shows a dash", "Yuborilgan KP: —" in rendered)
    check("no metrics renders nothing", kpi.format_metrics({}, ()), "")
    check(
        "missing metrics are named",
        kpi.missing_metrics({"meetings": 5}, sales),
        ["Qo'ng'iroqlar", "Yuborilgan KP", "Yangi lidlar"],
    )

    ask = kpi.build_request_text("Dmitriy", sales)
    check_true("ask names the person", "Dmitriy" in ask)
    check_true("ask shows the numbers format", "Qo'ng'iroqlar" in ask)
    check_true("text-only role gets no numbers block", "Qo'ng'iroqlar" not in kpi.build_request_text("Aziz", ()))


def test_permissions() -> None:
    """Written permission requests: intake wording, amounts, question order."""
    print("permissions (EMJ-SOP-ADM-01)")
    from integrations.org_bot import permissions

    # Starting a request
    check_true("command starts a request", permissions.wants_permission("/ruxsat"))
    check_true("Cyrillic request starts one", permissions.wants_permission("Рухсат керак: янги принтер"))
    check_true("Latin request starts one", permissions.wants_permission("ruxsat kerak, mehmonxonaga borish"))
    check_true("Russian request starts one", permissions.wants_permission("прошу разрешение на закупку"))
    # A mention is not a request: this one must reach the Director as a normal
    # message, not silently open a form.
    check_true("mentioning permission is not a request", not permissions.wants_permission("Директор рухсат берди"))
    check_true("unrelated message is not a request", not permissions.wants_permission("Бугун омборда ишладим"))

    # Amounts
    check("plain sum", permissions.parse_amount("5 000 000 сўм"), (500000000, "UZS"))
    check("dotted sum", permissions.parse_amount("5.000.000 so'm"), (500000000, "UZS"))
    check("dollars", permissions.parse_amount("$1200"), (120000, "USD"))
    check("no cost", permissions.parse_amount("0"), (0, "UZS"))
    check("unparseable stays unparsed", permissions.parse_amount("ҳали аниқ эмас"), (None, "UZS"))
    check("zero renders as zero", permissions.format_amount(0, "UZS"), "0")
    check("sum is grouped", permissions.format_amount(500000000, "UZS"), "5 000 000 сўм")
    check("raw answer is kept when unparsed", permissions.format_amount(None, "UZS", "тахминан 2 млн"), "тахминан 2 млн")

    # Question order follows the SOP form, and an unparseable amount still counts
    draft: dict = {}
    check("first question is the position", permissions.next_missing_field(draft).key, "requester_position")
    draft["requester_position"] = "IT мутахассис"
    check("then the department", permissions.next_missing_field(draft).key, "department")
    draft["department"] = "IT"
    check("then the subject", permissions.next_missing_field(draft).key, "subject")
    draft["subject"] = "Принтер сотиб олиш"
    check("then the reason", permissions.next_missing_field(draft).key, "reason")
    draft["reason"] = "Эскиси ишламайди"
    check("then the amount", permissions.next_missing_field(draft).key, "amount")
    draft["amount_raw"] = "тахминан 2 млн"
    check("unparsed amount is not re-asked", permissions.next_missing_field(draft).key, "execute_by")
    for key, value in (
        ("execute_by", "25.09.2026"),
        ("decision_needed_by", "23.09.2026 12:00"),
        ("urgency", "оддий"),
        ("attachments", "йўқ"),
    ):
        draft[key] = value
    check("complete form asks nothing", permissions.next_missing_field(draft), None)

    # Buttons — with a REAL 36-character id. A short fake id is exactly what
    # hid the 65-byte "approved_conditional" button that made Telegram reject
    # every approver card (2026-09-21).
    real_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    buttons = [b for row in permissions.decision_keyboard(real_id)["inline_keyboard"] for b in row]
    check(
        "four SOP outcomes",
        [permissions.DECISION_CODES[b["callback_data"].split(":")[1]] for b in buttons],
        ["approved", "approved_conditional", "rejected", "info_needed"],
    )
    confirm = [b for row in permissions.confirm_keyboard(real_id)["inline_keyboard"] for b in row]
    for button in buttons + confirm:
        check_true(
            f"{button['callback_data'].rsplit(':', 1)[0]} button fits Telegram's 64-byte limit with a real id",
            len(button["callback_data"].encode()) <= 64,
        )

    card = permissions.request_card({**draft, "request_no": "EMJ-2026-0007", "amount_tiyin": None}, for_approver=True)
    check_true("card carries the request number", "EMJ-2026-0007" in card)
    check_true("card uses the SOP's own field wording", "Нимага рухсат сўралади" in card)

    # 2026-09-21: an unescaped "&" or "<" in a typed answer made Telegram
    # reject the approver's card, so the Director silently received nothing.
    risky = {**draft, "subject": "A&B <тест>", "requester_name": "Ali <IT>", "request_no": "EMJ-2026-0008"}
    risky_card = permissions.request_card(risky, for_approver=True)
    check_true("typed '&' is escaped in the card", "A&amp;B" in risky_card)
    check_true("typed '<' is escaped in the card", "&lt;тест&gt;" in risky_card and "<тест>" not in risky_card)
    check_true("requester name is escaped", "Ali &lt;IT&gt;" in risky_card)
    decided = permissions.decision_text({"status": "rejected", "request_no": "X", "decision_note": "нарх > лимит"})
    check_true("decision note is escaped", "нарх &gt; лимит" in decided)
    check_true("/bekor cancels", permissions.is_cancel(" /bekor ") and permissions.is_cancel("/бекор"))
    check_true("an answer is not a cancel", not permissions.is_cancel("бекор қилинган буюртма"))


def test_permission_form() -> None:
    """The company's own SOP .docx is filled in place — never regenerated."""
    print("permission form (company docx filled in place)")
    from datetime import datetime, timezone

    from docx import Document

    from integrations.org_bot import docx_form

    check("blank after label is filled", docx_form.fill_blank_after("Бўлим: ______", "Бўлим", "IT"), "Бўлим: IT")
    check("empty answer leaves the blank", docx_form.fill_blank_after("Бўлим: ______", "Бўлим", ""), "Бўлим: ______")
    check(
        "only the chosen box is ticked",
        docx_form.tick_decision("☐ Тасдиқланди     ☐ Шарт билан тасдиқланди", "approved_conditional"),
        "☐ Тасдиқланди     ☑ Шарт билан тасдиқланди",
    )

    request = {
        "request_no": "EMJ-2026-0009",
        "requester_name": "Test Xodim",
        "requester_position": "IT мутахассис",
        "department": "IT",
        "submitted_to": "Director (Операцион директор)",
        "subject": "Принтер сотиб олиш",
        "reason": "Эскиси ишламайди",
        "amount_raw": "2 000 000 сўм",
        "execute_by": "25.09.2026",
        "decision_needed_by": "23.09.2026 12:00",
        "urgency": "оддий",
        "attachments": "йўқ",
        "requester_telegram_user_id": 111,
        "submitted_at": datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc),
        "status": "approved_conditional",
        "approved_terms": "1 800 000 сўм, 30.09.2026 гача",
        "decision_note": "Фақат шартнома билан",
        "decided_by": "Director (Операцион директор)",
        "decided_by_telegram_user_id": 222,
        "decided_at": datetime(2026, 9, 21, 8, 30, tzinfo=timezone.utc),
    }
    source = [p.text for p in Document(str(docx_form.TEMPLATE_PATH)).paragraphs]
    filled = [p.text for p in Document(str(docx_form.fill_form(request))).paragraphs]
    body = chr(10).join(filled)
    check("same paragraphs as the company's file", len(filled), len(source))
    title_at = next(i for i, t in enumerate(source) if docx_form.FORM_TITLE in t)
    check("page 1 is untouched (incl. director's approval line)", filled[: title_at + 1], source[: title_at + 1])
    for expected in (
        "EMJ-2026-0009", "21.09.2026 11:00", "Test Xodim, IT мутахассис", "Принтер сотиб олиш",
        "2 000 000 сўм", "Telegram ID 111", "Фақат шартнома билан", "Telegram ID 222", "21.09.2026 13:30",
    ):
        check_true(f"form contains '{expected}'", expected in body)
    check_true("the conditional box is ticked", "☑ Шарт билан тасдиқланди" in body)
    check_true("the other boxes stay empty", "☐ Тасдиқланди" in body and "☐ Рад этилди" in body)
    changed = [i for i, (a, b) in enumerate(zip(source, filled)) if a != b]
    check_true("only form lines changed", all(i > title_at for i in changed))


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
        test_org_bot,
        test_permissions,
        test_permission_form,
        test_daily_report_kpi,
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
