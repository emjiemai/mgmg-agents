"""Offline self-check for the pure logic — no network, no database.

Verifies the parts that would otherwise only be exercised against live SAP,
the CRM and Telegram: money formatting, aging buckets, Telegram message
splitting, org_bot parsing, and brief/receivables rendering.

Run:
    python scripts/selfcheck.py
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests never reach the real database (2026-10-17: an un-stubbed 22:00 step marked production rows,
# and a few audit writes and reads went there too). An environment variable wins over .env, so
# anything a test forgets to fake fails here instead of touching production.
import os  # noqa: E402

os.environ["DATABASE_URL"] = "postgresql://selfcheck@127.0.0.1:9/no-database-in-tests"
# Nor the platforms or the AI (2026-10-17: with BILLZ_SECRET_TOKEN in .env the run called the real
# Billz API and took over 15 minutes): their keys are blanked here; a test that needs one fakes it.
for _key in ("BILLZ_SECRET_TOKEN", "ONEC_ODATA_URL", "ONEC_LOGIN", "ONEC_PASSWORD", "VERIFIX_LOGIN",
             "VERIFIX_PASSWORD", "OPENROUTER_API_KEY"):
    os.environ[_key] = ""

# ...and it fails at once rather than after the pool's 10 s wait per call (the run had grown past 10 minutes).
from integrations.common import db as _db  # noqa: E402


async def _no_database_in_tests():
    raise RuntimeError("selfcheck: no database — fake this call")


_db.get_pool = _no_database_in_tests

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
    parse_callback_data,
    parse_role_and_request,
    validate_classification,
)
from integrations.org_bot.roles import AGENT_SLUGS, ROLE_SLUGS
from integrations.telegram.bot import sanitize_model_html
from integrations.sap.aging import aging_bucket
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


def latin_words(text: str, allow: set[str] | None = None) -> list[str]:
    """Latin-script words left in a message that should be Uzbek Cyrillic.

    Tags, brand names and codes are fine; anything else is a missed
    translation (the business's rule since 2026-09-25).
    """
    import re

    allowed = {"CEO", "IT", "KPI", "SAP", "CRM", "HR", "AI", "QR", "Garmin", "Londry", "EMJ", "SOP", "ADM", "OPS", "Admin", "Bot", "Verifix",
               "Billz", "C"}  # brands; "C" is 1C's
    allowed |= allow or set()
    # Telegram commands (/ismlar, /bekor) can only be Latin.
    plain = re.sub(r"/[a-z_]+", " ", re.sub(r"<[^>]+>", " ", text))
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:'[A-Za-z]+)*", plain) if w not in allowed]


def friendly_problems(text: str, names: tuple[str, ...] = ()) -> list[str]:
    """What breaks the friendly voice (tone.py) in a message, if anything.

    One line, lowercase except the names in it (which must keep their capital,
    2026-10-01), polite ("сиз", never "сен"), no markup, and no emoji (2026-10-19,
    the owner — it was one at the very end).
    """
    from integrations.org_bot import tone

    problems = []
    if "\n" in text:
        problems.append("line break")
    sentence = text
    for name in (*names, *tone.PROPER_NAMES):
        if name.lower() in text.lower() and name not in text:
            problems.append(f"name not capitalised: {name}")
        sentence = sentence.replace(name, "")
    if sentence != sentence.lower():
        problems.append("capital letters")
    if not tone.is_polite(text):
        problems.append("not polite")
    if "<" in text:
        problems.append("markup")
    if tone._EMOJI.search(text):
        problems.append("emoji")
    return problems


def test_references() -> None:
    """Every settings.X and every attribute of a project module that the code uses exists.

    2026-09-26: a removed setting (ops_manager_bot_provider) was still read
    in the answer path, so every question to OPS Manager Bot failed with
    "Хатолик юз берди" — invisible to lint and imports, because Python only
    looks an attribute up when that line runs. This walks every file instead.
    Scope-aware: an import inside one function applies only there, and a
    local variable with the same name as a module hides it.
    """
    print("references")
    import ast
    import importlib

    from integrations.common.config import PROJECT_ROOT, settings

    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    def own_nodes(scope: ast.AST):
        """Nodes of this scope, not of the functions nested inside it."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            yield node
            if not isinstance(node, scope_types):
                stack.extend(ast.iter_child_nodes(node))

    def project_modules(nodes) -> dict[str, object]:
        found: dict[str, object] = {}
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("integrations"):
                for alias in node.names:
                    try:  # only names that are themselves modules ("from x import store")
                        found[alias.asname or alias.name] = importlib.import_module(f"{node.module}.{alias.name}")
                    except ImportError:
                        pass
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("integrations") and alias.asname:
                        found[alias.asname] = importlib.import_module(alias.name)
        return found

    missing: list[str] = []
    files = [
        p for folder in ("integrations", "agents", "scripts")
        for p in (PROJECT_ROOT / folder).rglob("*.py") if "__pycache__" not in p.parts
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_level = project_modules(own_nodes(tree))
        scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            nodes = list(own_nodes(scope))
            local_imports = project_modules(nodes) if scope is not tree else {}
            stored = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            if scope is not tree:
                stored |= {a.arg for a in scope.args.args + scope.args.kwonlyargs}
            modules = {**{k: v for k, v in module_level.items() if k not in stored}, **local_imports}
            for node in nodes:
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                    continue
                owner, attr = node.value.id, node.attr
                where = f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                if owner == "settings" and "settings" not in stored and not hasattr(settings, attr):
                    missing.append(f"{where} settings.{attr}")
                elif owner in modules and not hasattr(modules[owner], attr):
                    missing.append(f"{where} {owner}.{attr}")
    check("no reference to a setting or function that doesn't exist", missing, [])


def test_bot_flows() -> None:
    """The Director's real message paths, end to end, with AI/Telegram/DB faked.

    Added 2026-09-26 after every question failed with "Хатолик юз берди": the
    answer path had never been executed by any test. This runs it — and the
    task path, and every data agent's fetcher on an empty database — so a
    runtime error anywhere in them fails here instead of in production.
    """
    print("bot flows (smoke)")
    import asyncio
    import sys
    import uuid

    from integrations.common import db
    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import ops_manager, roles, store

    load_agent("cash-calendar")  # loaded now so its database functions get faked below

    async def no_rows(*_args, **_kwargs):
        return []

    async def no_row(*_args, **_kwargs):
        return None

    director = {"id": "d", "telegram_user_id": 1, "role": "operatsion_direktor", "status": "active",
                "display_name": "Director", "full_name": "Бобур Алиев"}
    worker = {"id": "w", "telegram_user_id": 2, "role": "it", "status": "active",
              "display_name": "GMHRD", "full_name": "Алишер Каримов"}

    async def employees_by_role(role):
        return [director] if role == "operatsion_direktor" else [worker] if role == "it" else []

    async def active_employees():
        return [director, worker]

    async def new_task(**kwargs):
        return {"id": "t1", **kwargs}

    replies: list[str] = []
    edits: list[tuple] = []

    reply_tos: list = []

    async def fake_reply(_chat_id, _run_id, text, reply_markup=None, reply_to=None):
        replies.append(text)
        reply_tos.append(reply_to)
        return [100]

    class FakeAI:
        classification: dict = {}

        def __init__(self, *args, **kwargs):  # accepts exactly what the real client accepts
            import inspect

            from integrations.ai.openrouter_client import OpenRouterClient

            inspect.signature(OpenRouterClient.__init__).bind(self, *args, **kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def complete_json(self, system, user):
            return dict(FakeAI.classification)

        async def complete(self, system, user, **kwargs):
            return "Жавоб"

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, **kwargs):
            replies.append(text)
            return [200]

        async def _call(self, *args, **kwargs):
            return {"message_id": 201}

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append((text, reply_markup))

        async def _answer_callback(self, query_id, text):
            return None

    class NoSheets:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            from integrations.google.sheets_client import SheetsError

            raise SheetsError("offline")

        async def __aexit__(self, *exc):
            return None

    # Fake every database helper wherever it was imported by name.
    saved: list[tuple[object, str, object]] = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    fakes = {"fetch_all": no_rows, "fetch_one": no_row, "execute": no_row}
    # Captured first: patching db itself mid-loop must not change what we match.
    originals = {name: getattr(db, name) for name in fakes}
    for module in list(sys.modules.values()):
        module_name = getattr(module, "__name__", "")
        if not (module_name.startswith("integrations") or module_name.startswith("mgmg_agent_")):
            continue
        for name, fake in fakes.items():
            if getattr(module, name, None) is originals[name]:
                patch(module, name, fake)
    for name in dir(store):
        if name.startswith("_") or not asyncio.iscoroutinefunction(getattr(store, name)):
            continue
        patch(store, name, no_row if name.startswith(("get_", "create_", "find_", "set_", "mark_", "log_")) else no_rows)
    patch(store, "active_employees_by_role", employees_by_role)
    patch(store, "list_active_employees", active_employees)
    patch(store, "create_task", new_task)
    patch(ops_manager, "OpenRouterClient", FakeAI)
    patch(ops_manager, "TelegramBot", FakeBot)
    patch(ops_manager, "SheetsClient", NoSheets)
    patch(ops_manager, "_reply", fake_reply)

    try:
        for slug in sorted(roles.AGENT_SLUGS):
            try:
                data = asyncio.run(ops_manager._fetch_agent_data(slug))
                ok = isinstance(data, str) and bool(data)
            except Exception as exc:  # noqa: BLE001 — the point is to report it
                ok = False
                print(f"    {slug}: {type(exc).__name__}: {exc}")
            check_true(f"agent '{slug}' answers from an empty database", ok)

        FakeAI.classification = {"target_type": "agent", "target_agent": "xodimlar_kpi", "task_summary": "",
                                 "target_employee": None, "due_date": None}
        replies.clear()
        reply_tos.clear()
        asyncio.run(ops_manager._dispatch_director_task(1, "kechagi hisobotlarni korsat", 11, uuid.uuid4()))
        check("a question gets the AI's answer, not the error", replies, ["Жавоб"])
        check("...as a reply to his question, like every other role's (21.10)", reply_tos, [11])

        # 2026-10-02: a task first comes back to the Director as a list of
        # people to tick; it goes out only on "Юбориш".
        drafts: dict = {}

        async def create_draft(**kwargs):
            drafts["d"] = {"id": "11111111-2222-3333-4444-555555555555", "status": "open", **kwargs}
            return drafts["d"]

        async def get_draft(draft_id):
            return drafts.get("d")

        async def close_draft(draft_id, director, status):
            row = drafts.get("d")
            if not row or row["status"] != "open" or row["director_telegram_user_id"] != director:
                return None
            row["status"] = status
            return row

        patch(store, "create_task_draft", create_draft)
        patch(store, "get_task_draft", get_draft)
        patch(store, "close_task_draft", close_draft)
        FakeAI.classification = {"target_type": "employee", "target_role": "it", "target_employee": "E1",
                                 "task_summary": "Принтерни текширинг", "due_date": None}
        replies.clear()
        asyncio.run(ops_manager._dispatch_director_task(1, "Alisherga ayt printerni tekshirsin", 12, uuid.uuid4()))
        check_true("the Director first gets the list, the named person ticked",
                   any("кимга юборилсин" in r and "Белгиланган: Алишер Каримов" in r for r in replies)
                   and drafts["d"]["selected_ids"] == ["w"])
        check_true("nothing reaches the employee before Юбориш", not any("Янги топшириқ" in r for r in replies))

        async def toggle(draft_id, director, employee_id):
            row = drafts["d"]
            if director != row["director_telegram_user_id"] or row["status"] != "open":
                return None
            chosen = list(row["selected_ids"])
            row["selected_ids"] = [i for i in chosen if i != employee_id] if employee_id in chosen else chosen + [employee_id]
            return row

        patch(store, "toggle_task_draft_person", toggle)
        tick = {"id": "q", "data": f"tdt:{drafts['d']['id']}:0", "from": {"id": 1},
                "message": {"message_id": 5, "chat": {"id": 1}}}
        asyncio.run(ops_manager._handle_callback(tick, uuid.uuid4()))
        check_true("a tap unticks the person, and the card shows it",
                   drafts["d"]["selected_ids"] == [] and "▫️ Алишер Каримов" in str(edits[-1][1]) and "Белгиланган: ҳеч ким" in edits[-1][0])
        empty_send = {"id": "q", "data": f"tds:{drafts['d']['id']}", "from": {"id": 1}, "message": {"message_id": 5, "chat": {"id": 1}}}
        check("Юбориш with nobody ticked sends nothing", asyncio.run(ops_manager._handle_callback(empty_send, uuid.uuid4())), "draft_empty")
        asyncio.run(ops_manager._handle_callback(tick, uuid.uuid4()))
        check("a second tap ticks them again", drafts["d"]["selected_ids"], ["w"])
        replies.clear()
        send = {"id": "q", "data": f"tds:{drafts['d']['id']}", "from": {"id": 1},
                "message": {"message_id": 5, "chat": {"id": 1}}}
        check("Юбориш sends it", asyncio.run(ops_manager._handle_callback(send, uuid.uuid4())), "draft_sent")
        check_true("the task reaches the named person", any("Янги топшириқ" in r and "Принтерни текширинг" in r for r in replies))
        check_true("and the Director's card says who got it, then asks the deadline",
                   edits and "✅ Юборилди: Алишер Каримов (IT)" in edits[-1][0] and "Муддат кўрсатилмади" in edits[-1][0])
        check("a second tap sends nothing more", asyncio.run(ops_manager._handle_callback(send, uuid.uuid4())), "draft_closed")
        stranger = {**send, "from": {"id": 2}}
        drafts["d"]["status"] = "open"
        check("nobody but the Director can send it", asyncio.run(ops_manager._handle_callback(stranger, uuid.uuid4())),
              "unrecognized")
        check_true("no error on the task path", not any("Хатолик" in r for r in replies))

        # 2026-10-10: not understood → one question back, the likely meanings as buttons
        asked: list[tuple[str, object]] = []
        turns: list[str] = []
        clarifications: dict = {}
        dispatched: list[str] = []

        async def reply_with_markup(_chat_id, _run_id, text, reply_markup=None, reply_to=None):
            asked.append((text, reply_markup))
            return [300]

        async def log_turn(_user, role, content):
            turns.append(f"{role}: {content}")

        async def create_clarification(director, question, options):
            clarifications["c"] = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "director_telegram_user_id": director,
                                   "question": question, "options": options, "chosen": None}
            return clarifications["c"]

        async def choose_clarification(clarification_id, director, index):
            row = clarifications.get("c")
            if (not row or row["id"] != clarification_id or row["director_telegram_user_id"] != director
                    or row["chosen"] is not None or index >= len(row["options"])):
                return None
            row["chosen"] = index
            return row

        real_dispatch = ops_manager._dispatch_director_task
        patch(ops_manager, "_reply", reply_with_markup)
        patch(store, "log_conversation_turn", log_turn)
        patch(store, "create_clarification", create_clarification)
        patch(store, "choose_clarification", choose_clarification)
        FakeAI.classification = {"target_type": "clarify", "target_role": None, "target_agent": None,
                                 "task_summary": "Нимани назарда тутдингиз?",
                                 "options": ["Бухгалтерияга: акт-сверка тайёрлансин", "<b>Савол:</b> кредиторлик қанча",
                                             "Савол: кредиторлик қанча", "Тўртинчи", "Бешинчи"],
                                 "target_employee": None, "due_date": None}
        asyncio.run(real_dispatch(1, "сверка", 13, uuid.uuid4()))
        question, markup = asked[-1]
        buttons = [row[0] for row in markup["inline_keyboard"]]
        check("not understood: one question, the meanings as buttons (clean, no repeats, at most three)",
              (question, [b["text"] for b in buttons]),
              ("Нимани назарда тутдингиз?", ["Бухгалтерияга: акт-сверка тайёрлансин", "Савол: кредиторлик қанча", "Тўртинчи"]))
        check_true("each button fits Telegram's 64-byte callback data",
                   all(len(b["callback_data"].encode()) <= 64 for b in buttons))
        check_true("the question and its meanings go into the conversation memory (a typed answer works too)",
                   turns[-1].startswith("bot: Нимани назарда тутдингиз?") and "2) Савол: кредиторлик қанча" in turns[-1])

        async def fake_dispatch(director, text, source_message_id, run_id):
            dispatched.append(text)

        patch(ops_manager, "_dispatch_director_task", fake_dispatch)
        tap = {"id": "q", "data": buttons[1]["callback_data"], "from": {"id": 1}, "message": {"message_id": 9, "chat": {"id": 1}}}
        check("a tap sends that meaning on, as if he had written it",
              (asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), dispatched),
              ("clarification_chosen", ["Савол: кредиторлик қанча"]))
        check_true("...and the question shows what he chose, buttons gone",
                   "✔️ Савол: кредиторлик қанча" in edits[-1][0] and edits[-1][1] == {"inline_keyboard": []})
        check("a second tap does nothing", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "clarification_closed")
        clarifications["c"]["chosen"] = None
        check("only the Director who was asked can choose",
              asyncio.run(ops_manager._handle_callback({**tap, "from": {"id": 2}}, uuid.uuid4())), "clarification_closed")
        check("a broken button is refused",
              asyncio.run(ops_manager._handle_callback({**tap, "data": "clr:x:y"}, uuid.uuid4())), "unrecognized")
        FakeAI.classification = {**FakeAI.classification, "options": "not a list"}
        asked.clear()
        asyncio.run(real_dispatch(1, "сверка", 14, uuid.uuid4()))
        check("no usable meanings: the question alone", asked, [("Нимани назарда тутдингиз?", None)])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


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
    check("format thousands", format_uzs(125000000), f"1{nbsp}250{nbsp}000{nbsp}сўм")
    check("format negative", format_uzs(-125000000), f"-1{nbsp}250{nbsp}000{nbsp}сўм")
    check("format no currency", format_uzs(125000000, with_currency=False), f"1{nbsp}250{nbsp}000")
    check("short mlrd", format_uzs_short(int(1.25e9 * 100)), f"1,25{nbsp}млрд{nbsp}сўм")
    check("short mln", format_uzs_short(340_000_000 * 100), f"340{nbsp}млн{nbsp}сўм")
    check("short small", format_uzs_short(85_000 * 100), f"85{nbsp}000{nbsp}сўм")
    check("short negative", format_uzs_short(-int(2e9 * 100)), f"-2{nbsp}млрд{nbsp}сўм")
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
        f"15{nbsp}000{nbsp}сўм + $9,764.00",
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

    # 2026-10-02: who gets a task is confirmed on a list first (task_picker.py).
    from integrations.org_bot import task_picker

    people = [{"id": "a", "role": "it", "full_name": "Шерзод"}, {"id": "b", "role": "ombor", "full_name": "Алишер"},
              {"id": "c", "role": "it", "full_name": "Бобур"}]
    ordered = task_picker.candidates(people, {"c"})
    check("the bot's guess first, then by department and name", [p["id"] for p in ordered], ["c", "a", "b"])
    draft_id = "11111111-2222-3333-4444-555555555555"
    kb = task_picker.keyboard(draft_id, ordered, {"c"})
    flat = [b for row in kb["inline_keyboard"] for b in row]
    check("ticked and unticked people, then Send (count) and Cancel",
          [b["text"] for b in flat], ["✅ Бобур", "▫️ Шерзод", "▫️ Алишер", "📨 Юбориш (1)", "❌ Бекор"])
    check_true("every button fits 64 bytes", all(len(b["callback_data"].encode()) <= 64 for b in flat))
    check("a tick-box reads back", task_picker.parse_toggle(flat[2]["callback_data"].split(":", 1)[1]), (draft_id, 2))
    check("a broken one doesn't", task_picker.parse_toggle("x"), None)

    # A role must exist in three places kept by hand; a miss is only found
    # when someone picks the role live (the INSERT fails the CHECK).
    import re

    from integrations.org_bot import roles
    from integrations.org_bot.permissions import ROLE_LABELS_CYR

    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    checks = [set(re.findall(r"'([a-z_0-9]+)'", c)) for c in re.findall(r"CHECK \(role IN \(([^)]*)\)", schema)]
    checks = [c for c in checks if "operatsion_direktor" in c]  # employees'; chat history has its own 'role'
    check_true("schema.sql: both role CHECKs list exactly roles.py's roles",
               len(checks) == 2 and all(c == ROLE_SLUGS for c in checks))
    check_true("every role has a name for the SOP form", set(ROLE_LABELS_CYR) == ROLE_SLUGS)
    from integrations.org_bot.prompt import CLASSIFY_SYSTEM_PROMPT

    check("Finance is a role of its own (2026-09-29)", roles.ROLE_LABELS.get("moliya"), "Молия")
    check_true("...next to accounting, not instead of it", "buxgalteriya" in ROLE_SLUGS)
    check_true("...a task can be routed to it", "moliya" in roles.ROUTABLE_ROLE_SLUGS)
    check("Londry is a role, spelled as the brand (2026-10-02)", roles.ROLE_LABELS.get("londry"), "Londry")
    check_true("...never 'Laundry'", not any("laundry" in r.slug or "Laundry" in r.label for r in roles.ROLES))
    check_true("...routable, pickable, and the classifier knows it",
               "londry" in roles.ROUTABLE_ROLE_SLUGS and "- londry: Londry" in CLASSIFY_SYSTEM_PROMPT
               and any(b[0]["callback_data"].startswith("setrole:londry:")
                       for b in roles.role_picker_keyboard("req-1")["inline_keyboard"]))
    check_true("...and new employees can pick it",
               any(b[0]["callback_data"].startswith("setrole:moliya:")
                   for b in roles.role_picker_keyboard("req-1")["inline_keyboard"]))
    check_true("the classifier knows moliya and tells it from buxgalteriya",
               "- moliya: Молия" in CLASSIFY_SYSTEM_PROMPT and "Never swap one for the other" in CLASSIFY_SYSTEM_PROMPT)

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
    from integrations.org_bot.task_register import keyboard as _task_keyboard_for

    fresh_kb = _task_keyboard_for({"id": "task-1", "status": "sent"})
    check("fresh task has accept, blocked and done buttons", sum(len(r) for r in fresh_kb["inline_keyboard"]), 3)
    started_kb = _task_keyboard_for({"id": "task-1", "status": "started"})
    check("accepted task has blocked + done", sum(len(r) for r in started_kb["inline_keyboard"]), 2)
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
    """The CEO brief: the five numbers (A2), honest gaps, and who didn't report."""
    print("brief rendering (A2)")
    from integrations.common.agent_loader import load_agent
    from integrations.sap.figures import Figure
    from integrations.sap.models import ARAging, ARInvoice

    brief = load_agent("ceo-daily-brief")

    empty = brief.BriefData()
    empty.note_failure("sap_orders", RuntimeError("connection refused"))
    text = brief.render(empty)
    check_true("title and time on one line", "CEO кунлик ҳисоботи" in text.splitlines()[0] and "Тошкент" in text.splitlines()[0])
    check_true("goals and tasks that couldn't be read say so", text.count("маълумот олиб бўлмади") >= 2)
    check_true("a missing feed reads 'маълумот йўқ', never a number", "📈 Кечаги сотув: <i>маълумот йўқ</i>" in text)
    check_true("all four number lines are there, no «Касса»",
               all(k in text for k in ("Кечаги сотув", "Захира", "Мижоз қарзи", "Бугунги тўловлар")) and "Касса" not in text)

    aging = ARAging(snapshot_date=date(2026, 9, 25))
    aging.invoices = [
        ARInvoice(doc_entry=1, doc_num=1001, card_code="C1", card_name="A", days_overdue=120, aging_bucket="90_plus",
                  currency="USD", doc_total_tiyin=500_000, balance_due_tiyin=500_000),
        ARInvoice(doc_entry=2, doc_num=1002, card_code="C2", card_name="B", days_overdue=0, aging_bucket="current",
                  currency="USD", doc_total_tiyin=1_000_000, balance_due_tiyin=1_000_000),
    ]
    aging.total_overdue_tiyin = 500_000
    aging.bucket_totals_tiyin = {"90_plus": 500_000}
    aging.bucket_counts = {"90_plus": 1}
    data = brief.BriefData(
        aging=aging,
        sales=Figure(status="ok", totals={"USD": 1_234_000}, count=3, as_of=date(2026, 9, 26)),
        inventory=Figure(status="ok", totals={"USD": 12_000_000}, count=100, capped=True, as_of=date(2026, 9, 26)),
        payments=brief.PaymentsDue(totals={"UZS": 1_500_000_000}, count=2, unclear=1),
        report_rows=[],
        previous={"debt": {"status": "ok", "totals": {"USD": 1_300_000}, "capped": False},
                  "sales": {"status": "ok", "totals": {"USD": 1_234_000}, "capped": False}},
    )
    text = brief.render(data)
    check_true("yesterday's sales with the order count, as entered by 08:00",
               "📈 Кечаги сотув: $12,340.00 (3 та буюртма, 08:00 гача SAP'га киритилгани)" in text)
    check_true("unchanged sales show no change", "Кечаги сотув: $12,340.00 (3 та буюртма, 08:00 гача SAP'га киритилгани)\n" in text)
    check_true("a capped feed is a lower bound", "📦 Захира: камида $120,000.00*" in text)
    check_true("the lower bound is explained", "рақам тўлиқ эмас" in text)
    check_true("debt total, overdue part, 90+ marker", "$15,000.00" in text and "муддати ўтгани $5,000.00 (1 та) 🔴" in text)
    check_true("debt change since the last brief", "кечагига ▲ $2,000.00" in text)
    check_true("today's approved payments", "💳 Бугунги тўловлар: 2 та — 15" in text and "сўм" in text)
    check_true("an unreadable payment date is said, not guessed", "1 та тасдиқланган сўровда сана аниқ эмас" in text)
    check_true("the brief is Uzbek Cyrillic", latin_words(text, allow={"CEO", "SAP"}) == [])
    stored = brief.five_numbers_json(data)
    check("stored for tomorrow's change", stored["debt"]["totals"], {"USD": 1_500_000})
    check("capped flag kept", stored["inventory"]["capped"], True)

    stale = Figure(status="stale", as_of=date(2026, 9, 1))
    check_true("an old feed says since when", "01.09.2026 дан бери янгиланмаган" in brief.render(brief.BriefData(sales=stale)))

    # 2026-09-16: daily reports never reach the Director one by one; the
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
        "ҳаммаси юборди (2/2)" in brief.render(brief.BriefData(report_rows=everyone_in)),
    )
    never_asked = brief.render(brief.BriefData(report_rows=[]))
    check_true(
        "nobody asked is said plainly, not 'everyone reported'",
        "сўралмаган" in never_asked and "ҳаммаси юборди" not in never_asked,
    )
    check_true("removed sections stay removed", all(w not in never_asked for w in ("Reportlar", "Pipeline", "Davomat")))

    import asyncio

    from integrations.common.config import settings

    if not settings.daily_reports_enabled:
        check("switched off: no report rows fetched", asyncio.run(brief._fetch_report_results()), [])


def test_dates_and_figures() -> None:
    """Strict date reading, SAP dashboard figures, and payment due days."""
    print("dates and SAP figures")
    from integrations.common.dates import parse_day
    from integrations.org_bot.permissions import due_day
    from integrations.sap import figures

    ref = date(2026, 9, 23)
    for text, want in (
        ("25.09.2026", date(2026, 9, 25)), ("25.09", date(2026, 9, 25)), ("2026-09-25", date(2026, 9, 25)),
        ("25 сентябр", date(2026, 9, 25)), ("25-sentabr", date(2026, 9, 25)), ("эртага", date(2026, 9, 24)),
        ("бугун 15.00", ref), ("3 кун ичида", date(2026, 9, 26)), ("05.01", date(2027, 1, 5)),
        ("шу ҳафта ичида", None), ("тезроқ", None), ("30.02.2026", None), ("5 млн сўм", None),
    ):
        check(f"date '{text}'", parse_day(text, ref), want)

    from datetime import datetime, timezone

    request = {"execute_by": "эртага", "submitted_at": datetime(2026, 9, 23, 20, 30, tzinfo=timezone.utc)}
    check("'эртага' is the day after it was sent, in Tashkent", due_day(request), date(2026, 9, 25))

    orders = [
        {"DocEntry": 1, "DocDate": "2026-09-25", "DocTotal": 100.5, "CANCELED": "N"},
        {"DocEntry": 2, "DocDate": "2026-09-25T00:00:00", "DocTotal": "200"},
        {"DocEntry": 3, "DocDate": "2026-09-25", "DocTotal": 999, "CANCELED": "Y"},
        {"DocEntry": 4, "DocDate": "2026-09-24", "DocTotal": 50},
    ]
    fig = figures.documents_on(orders, date(2026, 9, 25), "USD", tool="orders", as_of=date(2026, 9, 26))
    check("yesterday's orders, cancelled left out", (fig.totals, fig.count, fig.capped), ({"USD": 30050}, 2, False))
    capped = figures.documents_on(orders * 25, date(2026, 9, 25), "USD", tool="orders", as_of=None)
    check_true("100 rows = the push limit = a lower bound", capped.capped)
    check("rows without the columns -> unknown, not zero",
          figures.documents_on([{"x": 1}], date(2026, 9, 25), "USD", tool="orders", as_of=None).status, "unknown_format")
    check("no rows -> no data", figures.documents_on([], date(2026, 9, 25), "USD", tool="orders", as_of=None).status, "no_data")

    stock = [{"ItemCode": "A", "WhsCode": "01", "StockValue": 1000}, {"ItemCode": "B", "WhsCode": "01", "OnHand": 2, "AvgPrice": 25.5}]
    check("stock value: SAP's own, else on hand x price", figures.inventory_value(stock, "USD", as_of=None).totals, {"USD": 105100})
    check("change only where both days have it", figures.change({"USD": 500, "UZS": 7}, {"USD": 200}), {"USD": 300})


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
    check_true("each age range on its own line (26.10: one long bracket was hard to read)",
               "\n• 90+ кун — 2 та" in text and "\n• 1–30 кун — 1 та" in text and "(" not in text)
    check_true("the total on its own line", text.splitlines()[1].startswith("Муддати ўтган: <b>"))
    check_true("no per-invoice detail", "Customer" not in text)
    check_true("no owner breakdown", "Mas'ul xodim" not in text)
    one_range = receivables.render(aging, min_days=100)
    check_true("single range reads 'ичида'", "2 та ҳисоб-фактура, ҳаммаси 90+ кун ичида" in one_range)
    check_true("the alert is Uzbek Cyrillic", latin_words(text) == [])

    text_30 = receivables.render(aging, min_days=30)
    check_true("min_days filters the 5-day invoice", "1–30 кун" not in text_30)


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
        "Қўнғироқлар" not in kpi.build_request_text("Dmitriy", kpi.metrics_for_role("b2b_sotuv")),
    )
    check_true(
        "reminder has no numbers block",
        "қўнғироқлар" not in kpi.build_reminder_text("Шерзод", kpi.metrics_for_role("garmin_sotuv")),
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
    check_true("hit target marked", "Учрашувлар: 5/4 ✅" in rendered)
    check_true("under target marked", "Қўнғироқлар: 10/15 ⚠️" in rendered)
    check_true("unreported metric shows a dash", "Юборилган КП: —" in rendered)
    check("no metrics renders nothing", kpi.format_metrics({}, ()), "")
    check(
        "missing metrics are named",
        kpi.missing_metrics({"meetings": 5}, sales),
        ["Қўнғироқлар", "Юборилган КП", "Янги лидлар"],
    )

    ask = kpi.build_request_text("Dmitriy", sales)
    check_true("ask names the person", "Dmitriy" in ask)
    check_true("ask shows the numbers format", "қўнғироқлар" in ask)
    check_true("text-only role gets no numbers block", "Қўнғироқлар" not in kpi.build_request_text("Aziz", ()))
    check(
        "Cyrillic labels are parsed too",
        kpi.parse_metrics("3 та учрашув, 22 та қўнғироқ", sales),
        {"meetings": 3, "calls": 22},
    )
    check_true("the ask is Uzbek Cyrillic", latin_words(kpi.build_request_text("Алишер", ())) == [])
    check_true("the reminder is Uzbek Cyrillic", latin_words(kpi.build_reminder_text("Алишер", ())) == [])

    # 2026-09-30: the ask and reminder read like a colleague, not an alarm.
    ask_text, reminder_text = kpi.build_request_text("Алишер ака", ()), kpi.build_reminder_text("Дилноза опа", ())
    check("the ask: one friendly line", friendly_problems(ask_text, ("Алишер ака",)), [])
    check("the reminder: one friendly line", friendly_problems(reminder_text, ("Дилноза опа",)), [])
    check_true("...addressed with ака/опа, the name capitalised, asking kindly",
               ask_text.startswith("Алишер ака, ишларингиз билан чарчамаяпсизми") and "раҳмат каттакон" in ask_text
               and reminder_text.startswith("Дилноза опа,"))
    check_true("...with the numbers in the same line when a role has them",
               friendly_problems(kpi.build_request_text("Алишер", sales), ("Алишер",)) == []
               and "қўнғироқлар" in kpi.build_request_text("Алишер", sales))

    from integrations.org_bot import names

    for employee, expected in (
        ({"full_name": "Алишер Каримов"}, "Алишер"),
        ({"full_name": "Каримова Дилноза", "address_form": "opa"}, "Дилноза опа"),
        ({"full_name": "Alisher Karimov", "address_form": "aka"}, "Алишер ака"),
        ({"full_name": "", "display_name": "Шерзод"}, "Шерзод"),
        ({"full_name": "алишер каримов", "address_form": "aka"}, "Алишер ака"),
    ):
        check(f"called {expected!r}", names.call_name(employee), expected)


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
    check("first question is the full name", permissions.next_missing_field(draft).key, "requester_full_name")
    draft["requester_full_name"] = "Алишер Каримов"
    check("then the position", permissions.next_missing_field(draft).key, "requester_position")
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
    risky = {**draft, "subject": "A&B <тест>", "requester_full_name": "Ali <IT>", "request_no": "EMJ-2026-0008"}
    risky_card = permissions.request_card(risky, for_approver=True)
    check_true("typed '&' is escaped in the card", "A&amp;B" in risky_card)
    check_true("typed '<' is escaped in the card", "&lt;тест&gt;" in risky_card and "<тест>" not in risky_card)
    check_true("requester name is escaped", "Ali &lt;IT&gt;" in risky_card)
    decided = permissions.decision_text({"status": "rejected", "request_no": "X", "decision_note": "нарх > лимит"})
    check_true("decision note is escaped", "нарх &gt; лимит" in decided)
    check_true("/bekor cancels", permissions.is_cancel(" /bekor ") and permissions.is_cancel("/бекор"))
    check_true("an answer is not a cancel", not permissions.is_cancel("бекор қилинган буюртма"))


def test_task_tracker() -> None:
    """A3 deadlines/reminders and the plan's И coefficients (A1, A3)."""
    print("task tracker (A3) and И")
    from datetime import datetime, timezone

    from integrations.org_bot import task_tracker as tt
    from integrations.org_bot.prompt import build_classify_message

    today = date(2026, 9, 23)  # a Wednesday
    check("stated deadline kept", tt.parse_due_date("2026-09-25", today), date(2026, 9, 25))
    check("today is a valid deadline", tt.parse_due_date("2026-09-23", today), today)
    check("no deadline stays none", tt.parse_due_date(None, today), None)
    check("a past date is dropped, not guessed", tt.parse_due_date("2026-09-01", today), None)
    check("garbage is dropped", tt.parse_due_date("juma", today), None)
    check("a year-plus date is a misread", tt.parse_due_date("2028-01-01", today), None)

    check("'Ertaga' button", tt.due_from_choice("1", today), (True, date(2026, 9, 24)))
    check("'Muddatsiz' button is no deadline", tt.due_from_choice("n", today), (True, None))
    check("unknown button refused", tt.due_from_choice("x", today)[0], False)
    buttons = [b for row in tt.deadline_keyboard(987654321)["inline_keyboard"] for b in row]
    check_true("deadline buttons fit Telegram's 64 bytes", all(len(b["callback_data"].encode()) <= 64 for b in buttons))
    check_true("card line names the weekday", "жума" in tt.deadline_line(date(2026, 9, 25), today))
    check_true("card line says 'эртага'", "эртага" in tt.deadline_line(date(2026, 9, 24), today))

    message = build_classify_message("ertaga hisobotni tayyorla", today=today)
    check_true("classifier is told today's date", "Wednesday, 2026-09-23" in message)

    def at(day: int, hour: int) -> datetime:
        return datetime(2026, 9, day, hour - 5, 0, tzinfo=timezone.utc)  # Tashkent = UTC+5

    tasks = [
        {"display_name": "Aziz", "task_summary": "A", "status": "done", "due_date": date(2026, 9, 22), "completed_at": at(22, 17)},
        {"display_name": "Aziz", "task_summary": "B", "status": "done", "due_date": date(2026, 9, 21), "completed_at": at(22, 10)},
        {"display_name": "Dilnoza", "task_summary": "C", "status": "sent", "due_date": date(2026, 9, 22), "completed_at": None},
        {"display_name": "Dilnoza", "task_summary": "D", "status": "sent", "due_date": today, "completed_at": None},
        {"display_name": "Dilnoza", "task_summary": "E", "status": "sent", "due_date": None, "completed_at": None},
    ]
    score = tt.score_tasks(tasks, date(2026, 9, 21), today)
    check("tasks judged (open, due today, not yet late)", score.due, 3)
    check("done by 17:00 on the day counts as on time", score.on_time, 1)
    check("done the day after is late", score.late, 1)
    check("open past deadline is overdue", score.open_overdue, 1)
    check("A3 И", round(score.index, 2), 0.33)

    rows = [
        {"display_name": "Aziz", "status": "submitted", "report_date": today, "submitted_at": at(23, 16)},
        {"display_name": "Bobur", "status": "submitted", "report_date": today, "submitted_at": at(23, 19)},
        {"display_name": "Dilnoza", "status": "asked", "report_date": today, "submitted_at": None},
    ]
    reports = tt.score_reports(rows)
    check("reports: 2 of 3 sent", (reports.reported, reports.asked), (2, 3))
    check("a report after 18:00 isn't on time", reports.on_time, 1)
    check("A1 И = (2/3)*(1/2)", round(reports.index, 2), 0.33)

    text = tt.weekly_text(date(2026, 9, 21), today, score, reports, {"approved": 2, "submitted": 1})
    check_true("scorecard shows the tasks И", "1/3 ўз вақтида" in text and "И = 0.33" in text)
    check_true("below 0.50 is red", "🔴" in text)
    check_true("names who didn't report", "Dilnoza (1 кун)" in text)
    check_true("scorecard is Uzbek Cyrillic", latin_words(text, allow={"Aziz", "Bobur", "Dilnoza", "C"}) == [])
    check_true(
        "reminder is Uzbek Cyrillic",
        latin_words(tt.reminder_text({"due_date": today, "task_summary": "Ҳисобот"}, today)) == [],
    )
    check_true("names the overdue task", "Dilnoza — C" in text)
    check_true("permissions counted", "тасдиқланди 2" in text and "кутилмоқда 1" in text)
    quiet = tt.weekly_text(date(2026, 9, 21), today, tt.TaskScore(), None, None)
    check_true("no reports section when reports are off", "hisobot" not in quiet)
    check_true("typed names are escaped", "&lt;" in tt.overdue_director_text(
        [{"display_name": "A<b>", "task_summary": "x", "due_date": today}]
    ))


def test_names_and_routing() -> None:
    """Every employee's typed name, and the Director addressing one person."""
    print("names and person routing")
    from integrations.org_bot import names
    from integrations.org_bot.ops_manager import _pick_person, _plain
    from integrations.org_bot.prompt import build_classify_message

    worker = {"role": "it", "full_name": None, "display_name": "GMHRD"}
    check_true("an employee without a name must give one", names.needs_name(worker))
    check_true("the Director is never stopped for a name", not names.needs_name({"role": "operatsion_direktor"}))
    check_true("a saved name is enough", not names.needs_name({**worker, "full_name": "Алишер Каримов"}))
    check("Telegram name only as a fallback", names.person_name(worker), "GMHRD")
    check("typed name wins", names.person_name({**worker, "full_name": "Алишер Каримов"}), "Алишер Каримов")
    check_true("the name question is Uzbek Cyrillic", latin_words(names.ASK_TEXT) == [])

    roster = {"E1": {"role": "it", "full_name": "Алишер Каримов"}, "E2": {"role": "ombor", "full_name": "Бобур Алиев"}}
    result = {"target_type": "employee", "target_role": "b2b_sotuv", "target_employee": "e2"}
    person, known = _pick_person(result, roster)
    check_true("named person found", known and person is roster["E2"])
    check("their own department wins over the model's guess", result["target_role"], "ombor")
    check("no person named -> whole department", _pick_person({"target_employee": None}, roster), (None, True))
    check("unknown person -> ask, never broadcast", _pick_person({"target_employee": "E9"}, roster), (None, False))

    message = build_classify_message("Alisherga ayt", roster=["E1 = Алишер Каримов (it)"])
    check_true("classifier sees the employee list", "E1 = Алишер Каримов (it)" in message)
    check("preview drops the AI's tags", _plain("<b>Отчёт</b>  тайёрланг"), "Отчёт тайёрланг")


def test_registration_states() -> None:
    """Between the two approvals, re-registration, and questions in answer windows."""
    print("registration and answer windows")
    import asyncio
    import uuid

    from integrations.org_bot import names, ops_manager, store

    replies: list = []
    requests: list = []
    marked: list = []
    halfway = {"row": None}
    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((text, reply_markup))
        return [1]

    async def in_progress(user):
        return halfway["row"]

    async def none(*args, **kwargs):
        return None

    async def new_request(**kwargs):
        requests.append(kwargs)
        return "access_requested"

    async def mark(user):
        marked.append(user)

    patch(store, "registration_in_progress", in_progress)
    patch(store, "get_pending_access_request", none)
    patch(store, "mark_name_asked", mark)
    patch(ops_manager.admin, "request_access", new_request)
    patch(ops_manager, "_reply", fake_reply)
    sender = {"id": 9, "first_name": "Ali"}
    try:
        # accepted, no role picked yet: the typed name is not a new join request
        halfway["row"] = {"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "status": "approved", "role_status": None}
        outcome = asyncio.run(ops_manager._handle_unregistered_sender(9, sender, uuid.uuid4()))
        check("role not picked: the picker again, no new request", (outcome, requests), ("role_picker_resent", []))
        check_true("...with the role buttons", bool(replies[-1][1] and replies[-1][1]["inline_keyboard"]))
        check_true("...in Uzbek Cyrillic", latin_words(replies[-1][0]) == [])
        halfway["row"] = {**halfway["row"], "role_status": "pending", "requested_role": "it"}
        outcome = asyncio.run(ops_manager._handle_unregistered_sender(9, sender, uuid.uuid4()))
        check("role picked, admin not yet: told to wait, no new request", (outcome, requests), ("role_pending", []))
        halfway["row"] = None
        asyncio.run(ops_manager._handle_unregistered_sender(9, sender, uuid.uuid4()))
        check("a stranger still makes a join request", len(requests), 1)

        # role approved: a new person is asked for their name; someone
        # registered again keeps theirs (their answer would go to the AI)
        request = {"telegram_user_id": 9, "requested_role": "it"}
        asyncio.run(ops_manager.send_registration_confirmed(request, uuid.uuid4(), {"full_name": None}))
        check_true("new person: asked for the name", names.ASK_TEXT in replies[-1][0] and marked == [9])
        marked.clear()
        asyncio.run(ops_manager.send_registration_confirmed(request, uuid.uuid4(), {"full_name": "Алишер Каримов"}))
        check_true("registered again: the saved name is shown, not asked",
                   names.ASK_TEXT not in replies[-1][0] and "Алишер Каримов" in replies[-1][0] and marked == [])
        director = {"telegram_user_id": 9, "requested_role": "operatsion_direktor"}
        asyncio.run(ops_manager.send_registration_confirmed(director, uuid.uuid4(), {"full_name": None}))
        check_true("the Director is never asked", names.ASK_TEXT not in replies[-1][0] and marked == [])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    check_true("a question mark is a question", ops_manager.asks_something("КП қачон?"))
    check_true("Arabic question mark too", ops_manager.asks_something("қачон؟"))
    check_true("a plain answer isn't", not ops_manager.asks_something("эртага учрашамиз"))


def test_report_or_message() -> None:
    """A report never needs Telegram's reply; an unclear message gets one tap."""
    print("report or message")
    from integrations.org_bot.ops_manager import report_message_kind, report_or_ai_keyboard

    # 2026-10-02: no more "or a message for the Director?" — only report or a question for the AI.
    check("plain message -> confirmed first, never guessed", report_message_kind(False, False, False), "ask")
    check("reply to the ask/reminder -> report, even if it asks something", report_message_kind(True, False, True), "report")
    check("reply to a task card -> a question about the task", report_message_kind(False, True, False), "task_question")
    check("a question (\"?\") -> the AI, never the report (17.10)", report_message_kind(False, False, True), "question")
    real_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    buttons = [b for row in report_or_ai_keyboard(real_id)["inline_keyboard"] for b in row]
    check("two choices: report, or a question", [b["callback_data"].split(":")[0] for b in buttons], ["asrep", "asai"])
    check_true("every button fits Telegram's 64 bytes", all(len(b["callback_data"].encode()) <= 64 for b in buttons))
    check_true("buttons are Uzbek Cyrillic", all(latin_words(b["text"]) == [] for b in buttons))


def test_db_viewer() -> None:
    """The /db viewer: locked by default, read-only, escapes what it shows."""
    print("database viewer")
    import base64
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from integrations.api import app as api_app
    from integrations.api import db_viewer
    from integrations.common.config import settings

    fake_rows = {
        "relations": [{"name": "agent_actions", "kind": "r"}, {"name": "employees", "kind": "r"},
                      {"name": "v_ar_aging_latest", "kind": "v"}],
        "columns": [{"column_name": "id"}, {"column_name": "full_name"}, {"column_name": "created_at"}],
        "rows": [{"id": 7, "full_name": "<script>x</script>", "created_at": datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)}],
    }
    seen_queries: list[str] = []

    async def fake_read(query, params=None):
        text = query if isinstance(query, str) else repr(query)
        seen_queries.append(text)
        if "pg_class" in text:
            return fake_rows["relations"]
        if "information_schema.columns" in text:
            return fake_rows["columns"]
        if "count(*)" in text:
            return [{"n": 1}]
        return fake_rows["rows"]

    original_read, original_password, original_log = db_viewer._read, settings.db_viewer_password, db_viewer.log_action
    original_until = db_viewer.PLATFORM_OPEN_UNTIL
    db_viewer.PLATFORM_OPEN_UNTIL = date(2000, 1, 1)  # these checks are about closed platform tables
    db_viewer._read = fake_read

    async def no_audit(**kwargs):  # the test never writes to the real audit log
        return None

    db_viewer.log_action = no_audit
    client = TestClient(api_app.app)
    try:
        settings.db_viewer_password = SecretStr("")
        check("switched off without a password", client.get("/db").status_code, 404)

        settings.db_viewer_password = SecretStr("s3cret-long")
        check("asks for a login", client.get("/db").status_code, 401)
        wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
        check("wrong password refused", client.get("/db", headers=wrong).status_code, 401)

        good = {"Authorization": "Basic " + base64.b64encode(b"any:s3cret-long").decode()}
        index = client.get("/db", headers=good)
        check("right password lets you in", index.status_code, 200)
        check_true("lists the tables", "agent_actions" in index.text and "1 қатор" in index.text)
        check_true("platform data listed with a lock (structure only); the bot's tables open (17.10.2026)",
                   "🔒 v_ar_aging_latest" in index.text and "href='/db/v_ar_aging_latest'" in index.text
                   and "href='/db/employees'>employees" in index.text and "🔒 employees" not in index.text
                   and "href='/db/agent_actions'" in index.text)
        db_viewer.PLATFORM_OPEN_UNTIL = original_until
        check_true("platform tables open to IT only until the Boss's deadline (31.10), then closed by themselves",
                   original_until == date(2026, 10, 31) and db_viewer.is_open("sap_gateway_snapshots", date(2026, 10, 31))
                   and not db_viewer.is_open("sap_gateway_snapshots", date(2026, 11, 1))
                   and db_viewer.is_open("employees", date(2026, 11, 1)) and not db_viewer.is_open("pg_stat_statements"))
        db_viewer.PLATFORM_OPEN_UNTIL = date(2000, 1, 1)
        check_true("SAP pushes as one /db table per kind, with a readable name (17.10)",
                   db_viewer.sap_kind("sap_customers") == "customers" and db_viewer.sap_kind("sap_gateway_snapshots") is None
                   and len(db_viewer.SAP_VIEWS) == 19 and db_viewer.SAP_VIEWS["stock_items"].startswith("Омбор қолдиғи")
                   and "is_open(\"sap_gateway_snapshots\")" in load_source("integrations/api/db_viewer.py"))
        check_true("every SAP / 1C / Billz / Verifix table stays closed, the bot's records are open",
                   not db_viewer.PLATFORM_TABLES & db_viewer.TECHNICAL_TABLES
                   and {"employees", "tasks", "daily_reports", "client_feedback", "kpi_ratings"} <= db_viewer.TECHNICAL_TABLES
                   and {"sap_gateway_snapshots", "ar_aging_snapshots", "billz_sap_checks", "daily_briefs"} <= db_viewer.PLATFORM_TABLES)
        check_true("never indexed or cached", index.headers.get("x-robots-tag", "").startswith("noindex")
                   and index.headers.get("cache-control") == "no-store")

        seen_queries.clear()
        closed = client.get("/db/v_ar_aging_latest", headers=good)
        check_true("a financial table is closed (Director's order 07.10.2026)", "Бу жадвал ёпиқ" in closed.text)
        check_true("...its rows are never read", not any("v_ar_aging_latest" in q and ("SELECT *" in q or "t::text" in q)
                                                         for q in seen_queries)
                   and "&lt;script&gt;" not in closed.text and "<script>x" not in closed.text)
        page = client.get("/db/agent_actions", headers=good)
        check_true("shows a row", page.status_code == 200 and "full_name" in page.text)
        check_true("what the data holds is escaped", "&lt;script&gt;" in page.text and "<script>x" not in page.text)
        check_true("timestamps in Tashkent time", "2026-09-26 08:00:00" in page.text)
        check_true("newest first", any("ORDER BY" in q and "created_at" in q for q in seen_queries))

        seen_queries.clear()
        missing = client.get("/db/pg_authid", headers=good)
        check_true("a table outside the list is refused", "Бундай жадвал йўқ" in missing.text)
        check_true("...without ever being queried", not any("pg_authid" in q for q in seen_queries))

        # ---- a closed table's structure (15.10.2026): counts, columns, when written — never a value
        async def structure_read(query, params=None):
            text = query if isinstance(query, str) else repr(query)
            seen_queries.append(text)
            if "pg_class" in text:
                return [{"name": "sap_gateway_snapshots", "kind": "r"}]
            if "information_schema.columns" in text:
                return [{"column_name": "id", "data_type": "bigint"}, {"column_name": "tool", "data_type": "text"},
                        {"column_name": "snapshot_date", "data_type": "date"},
                        {"column_name": "captured_at", "data_type": "timestamp with time zone"},
                        {"column_name": "raw", "data_type": "jsonb"}]
            if "min(" in text:
                return [{"oldest": datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
                         "newest": datetime(2026, 10, 15, 8, 43, tzinfo=timezone.utc)}]
            if "AS day" in text:
                return [{"day": date(2026, 10, 15), "n": 466}, {"day": date(2026, 10, 14), "n": 470}]
            if "AS k" in text:
                return [{"k": "supplier_balances", "n": 61, "last": datetime(2026, 10, 15, 8, 43, tzinfo=timezone.utc)}]
            if "count(*)" in text:
                return [{"n": 20640}]
            return [{"raw": {"CardName": "SECRET CUSTOMER", "DocTotal": 987654}}]

        db_viewer._read = structure_read
        seen_queries.clear()
        shape = client.get("/db/sap_gateway_snapshots", headers=good)
        check_true("a closed table opens its structure: row count, columns and their types",
                   shape.status_code == 200 and "20640 қатор" in shape.text and "Бу жадвал ёпиқ" in shape.text
                   and "snapshot_date" in shape.text and "jsonb" in shape.text)
        check_true("...when rows were written (Tashkent time), rows per day",
                   "2026-10-15 13:43:00" in shape.text and "2026-08-23 08:00:00" in shape.text and ">466<" in shape.text)
        check_true("...the SAP pushes per kind with the last write", "supplier_balances" in shape.text and ">61<" in shape.text)
        check_true("...never a value: no row is selected, nothing of it is shown",
                   "SECRET CUSTOMER" not in shape.text and "987654" not in shape.text
                   and not any("SELECT *" in q or "t::text" in q for q in seen_queries))
    finally:
        db_viewer._read = original_read
        settings.db_viewer_password = original_password
        db_viewer.log_action = original_log
        db_viewer.PLATFORM_OPEN_UNTIL = original_until


def test_plan_agents() -> None:
    """B2 cash calendar, B4 data quality."""
    print("B2 / B4")
    from datetime import datetime, timezone

    from integrations.common.agent_loader import load_agent
    # ---- B2: 30-day cash calendar
    cc = load_agent("cash-calendar")
    today = date(2026, 9, 28)
    sent = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)
    invoices = [
        {"due_date": date(2026, 9, 30), "balance_due_tiyin": 320000, "currency": "USD"},
        {"due_date": date(2026, 10, 20), "balance_due_tiyin": 550000, "currency": "USD"},
        {"due_date": date(2026, 9, 10), "balance_due_tiyin": 738436, "currency": "USD"},
        {"due_date": None, "balance_due_tiyin": 5, "currency": "USD"},
    ]
    payments = [
        {"request_no": "EMJ-2026-0007", "subject": "Принтер", "amount_tiyin": 1_500_000_000, "currency": "UZS",
         "execute_by": "30.09.2026", "submitted_at": sent},
        {"request_no": "EMJ-2026-0008", "subject": "Мебель", "amount_tiyin": 100, "currency": "UZS",
         "execute_by": "тезроқ", "submitted_at": sent},
        {"request_no": "EMJ-2026-0009", "subject": "Кеча", "amount_tiyin": 100, "currency": "UZS",
         "execute_by": "01.09.2026", "submitted_at": sent},
    ]
    cal = cc.build(invoices, payments, today, capped=False)
    check("four blocks cover the 30 days", (cal.incoming[0].start, cal.incoming[-1].end), (today, date(2026, 10, 27)))
    check("invoice due this week lands in week 1", (cal.incoming[0].count, cal.incoming[0].totals), (1, {"USD": 320000}))
    check("overdue kept apart", (cal.overdue_count, cal.overdue), (1, {"USD": 738436}))
    check("only in-window approved payments go out", [p["no"] for p in cal.outgoing], ["EMJ-2026-0007"])
    check("an unreadable date is counted, not placed", cal.unclear, 1)
    text = cc.render(cal)
    check_true("says there is no balance", "Касса қолдиғи уланмаган" in text)
    check_true("the calendar is Uzbek Cyrillic", latin_words(text, allow={"EMJ"}) == [])
    check_true("a capped invoice feed is a lower bound", "камида" in cc.render(cc.build(invoices, [], today, capped=True)))

    # ---- B4: data quality
    dq = load_agent("data-quality")
    inp = dq.Inputs(today=today)
    inp.invoices = [
        {"doc_num": 2253, "doc_date": date(2026, 9, 1), "due_date": date(2026, 9, 10), "currency": "USD",
         "doc_total_tiyin": 100, "balance_due_tiyin": 100, "sales_person_code": -1},
        {"doc_num": 2332, "doc_date": date(2026, 9, 5), "due_date": None, "currency": "USD",
         "doc_total_tiyin": 100, "balance_due_tiyin": 150, "sales_person_code": 4},
    ]
    inp.feeds = {"invoices": {"at": datetime(2026, 9, 28, 2, tzinfo=timezone.utc), "rows": 20},
                 "payments": {"at": datetime(2026, 9, 28, 2, tzinfo=timezone.utc), "rows": 100}}
    inp.unnamed = ["GMHRD"]
    report = dq.render(inp)
    check_true("no sales person is flagged", "Масъул сотувчиси йўқ очиқ ҳисоб-фактура: 1 та" in report and "#2253" in report)
    check_true("missing due date is flagged", "Тўлов муддати йўқ ҳисоб-фактура: 1 та (#2332)" in report)
    check_true("a balance above the total is flagged", "Қолдиғи нотўғри ҳисоб-фактура: 1 та (#2332)" in report)
    check_true("a capped feed is flagged", "тўловлар — чекланган (100 та" in report)
    check_true("a feed that never came is flagged", "буюртмалар — ҳеч қачон келмаган" in report)
    check_true("unnamed employees are flagged", "Исм ёзмаган ходимлар: 1" in report)
    check_true("the report is Uzbek Cyrillic", latin_words(report, allow={"GMHRD"}) == [])
    clean = dq.Inputs(today=today, feeds={t: {"at": datetime(2026, 9, 28, 2, tzinfo=timezone.utc), "rows": 1}
                                          for t in dq.FEED_LABELS})
    check_true("clean data says so", dq.render(clean).count("✅ тоза") == 2)


def test_employee_admin() -> None:
    """The admin changes a name or a role; an employee asks for a name change."""
    print("employee admin (names, roles)")
    import asyncio
    import uuid

    from integrations.common.config import settings
    from integrations.org_bot import admin, names, ops_manager, store

    worker_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    worker = {"id": worker_id, "telegram_user_id": 2, "role": "it", "status": "active", "display_name": "GMHRD",
              "full_name": "Алишер Каримов", "telegram_username": "gmhrd"}
    director = {**worker, "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "telegram_user_id": 1,
                "role": "operatsion_direktor", "full_name": "Бобур Алиев"}

    for label, (text, keyboard) in {
        "list": admin.employee_list_view([worker, director]),
        "card": admin.employee_card(worker),
        "role picker": admin.role_picker_view(worker),
        "remove confirm": admin.confirm_remove_view(worker),
        "name request": admin.name_request_view(worker),
    }.items():
        buttons = [b for row in keyboard["inline_keyboard"] for b in row]
        check_true(f"{label}: every button fits Telegram's 64 bytes",
                   all(len(b["callback_data"].encode()) <= 64 for b in buttons))
        check_true(f"{label}: Uzbek Cyrillic", latin_words(text, allow={"GMHRD", "gmhrd"}) == [])
    picker = [b["callback_data"] for row in admin.role_picker_view(worker)[1]["inline_keyboard"] for b in row]
    check_true("the current role isn't offered", f"cr:it:{worker_id}" not in picker and f"cr:ombor:{worker_id}" in picker)
    check_true("removal needs a second tap",
               any(b["callback_data"] == f"removeuser:{worker_id}"
                   for row in admin.confirm_remove_view(worker)[1]["inline_keyboard"] for b in row))
    check_true("the Director's card explains when their name is asked",
               "кейинги ёзма рухсат қарорида" in admin.employee_card(director)[0])

    # Weekend workers (2026-09-28): everyone Mon–Fri, Sat/Sun only if switched on.
    from datetime import date

    from integrations.org_bot import kpi

    friday, saturday, sunday = date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)
    check_true("everyone works on a weekday", kpi.works_on(worker, friday))
    check_true("nobody is asked on a weekend by default",
               not kpi.works_on(worker, saturday) and not kpi.works_on(worker, sunday))
    saturday_worker = {**worker, "works_saturday": True}
    check_true("a Saturday worker is asked on Saturday, not Sunday",
               kpi.works_on(saturday_worker, saturday) and not kpi.works_on(saturday_worker, sunday))
    card_buttons = [b["callback_data"] for row in admin.employee_card(worker)[1]["inline_keyboard"] for b in row]
    check_true("the card has Saturday and Sunday switches",
               f"wsat:{worker_id}" in card_buttons and f"wsun:{worker_id}" in card_buttons)
    director_buttons = [b["callback_data"] for row in admin.employee_card(director)[1]["inline_keyboard"] for b in row]
    check_true("no weekend switches for the Director (never asked for reports)",
               not any(b.startswith(("wsat:", "wsun:")) for b in director_buttons))
    check("workdays line", admin.workdays_text({**worker, "works_saturday": True, "works_sunday": True}),
          "Душанба–Жума + шанба, якшанба")
    check_true("the list marks weekend workers", "(+ шанба)" in admin.employee_list_view([saturday_worker])[0])

    sent: list[tuple[str, str]] = []  # (chat, text)
    edits: list[str] = []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, **kwargs):
            sent.append((str(chat_id), text))
            return [1]

        async def _edit_message(self, **kwargs):
            edits.append(kwargs["text"])

        async def _answer_callback(self, *args):
            return None

    async def nothing(*args, **kwargs):
        return None

    people = {worker_id: worker, director["id"]: director}

    async def get_employee(employee_id):
        return people.get(employee_id)

    async def reset_name(employee_id, by):
        return {**people[employee_id], "full_name": None}

    async def change_role(employee_id, role, by):
        return people[employee_id], {**people[employee_id], "role": role}

    toggled: list[tuple[str, str]] = []

    async def toggle_weekend(employee_id, day, by):
        toggled.append((employee_id, day))
        people[employee_id] = {**people[employee_id], f"works_{day}": not people[employee_id].get(f"works_{day}")}
        return people[employee_id]

    requests = [worker, None]

    async def request_change(telegram_user_id):
        return requests.pop(0)

    saved = []
    for obj, name, value in (
        (admin, "TelegramBot", FakeBot), (names, "TelegramBot", FakeBot), (ops_manager, "TelegramBot", FakeBot),
        (admin, "log_action", nothing), (store, "get_employee", get_employee), (store, "reset_employee_name", reset_name),
        (store, "change_employee_role", change_role), (store, "request_name_change", request_change),
        (store, "toggle_weekend_day", toggle_weekend), (settings, "admin_bot_admin_user_id", 0),
    ):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def tap(data):
        callback = {"id": "q", "data": data, "from": {"id": 9, "username": "admin"},
                    "message": {"message_id": 5, "chat": {"id": 9}}}
        return asyncio.run(admin.handle_admin_callback(callback, uuid.uuid4()))

    try:
        tap(f"rename:{worker_id}")
        check_true("rename: the worker is asked for the new name", ("2", names.ASK_CHANGE_TEXT) in sent)
        check_true("rename: the admin sees it happened", "Исм қайта сўралди" in edits[-1])

        sent.clear()
        tap(f"rename:{director['id']}")
        check_true("the Director is never sent the question", not any(chat == "1" for chat, _ in sent))

        sent.clear()
        tap(f"cr:ombor:{worker_id}")
        check_true("role change: the worker is told", any(chat == "2" and "Омбор" in text for chat, text in sent))
        check_true("role change: the card shows it", "Роль ўзгартирилди" in edits[-1])

        sent.clear()
        tap(f"wsat:{worker_id}")
        check("Saturday switch toggles that employee's Saturday", toggled[-1], (worker_id, "saturday"))
        check_true("the card shows the new work days", "Душанба–Жума + шанба" in edits[-1])
        tap(f"wsun:{worker_id}")
        check("Sunday switch toggles Sunday", toggled[-1], (worker_id, "sunday"))
        check_true("both weekend days shown", "Душанба–Жума + шанба, якшанба" in edits[-1])

        sent.clear()
        tap(f"nameno:{worker_id}")
        check_true("a refused request is told to the worker", any("рад этди" in text for _, text in sent))

        sent.clear()
        asyncio.run(ops_manager._request_name_change(worker, uuid.uuid4()))
        check_true("/ism: the admin gets the request card", any("Исм ўзгартириш сўрови" in text for _, text in sent))
        check_true("/ism: the worker is told it went", any("админга юборилди" in text for _, text in sent))
        sent.clear()
        asyncio.run(ops_manager._request_name_change(worker, uuid.uuid4()))
        check_true("/ism twice within an hour isn't sent again", any("аллақачон" in text for _, text in sent))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def test_client_feedback() -> None:
    """QR complaints: validation, the page, what reaches the Director, the printed card."""
    from datetime import timezone
    print("client complaints (QR)")
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from integrations.api import app as api_app
    from integrations.api import feedback_page
    from integrations.org_bot import feedback, qr_card

    # ---- validation
    sub, err = feedback.clean({"place": "garmin", "branch": "abay", "message": "  Навбат  узун  ", "phone": "90 123-45-67"})
    check("message tidied, phone normalised", (sub.message, sub.phone, sub.kind, sub.place),
          ("Навбат узун", "901234567", "complaint", "garmin"))
    check("the business must be chosen", feedback.clean({"message": "Навбат узун"})[1], "place")
    check("an unknown business is refused", feedback.clean({"place": "cafe", "message": "Навбат узун"})[1], "place")
    check("empty message refused", feedback.clean({"place": "laundry", "branch": "yunusobod", "message": " "})[1], "empty")
    check_true("a bad phone is refused, not kept",
               feedback.clean({"place": "laundry", "branch": "yunusobod", "message": "ёмон", "phone": "abc"})[0] is None)
    check("complaints only: an old 'feedback' kind is ignored",
          feedback.clean({"place": "laundry", "branch": "yunusobod", "message": "ёмон", "kind": "feedback"})[0].kind, "complaint")
    anon = feedback.clean({"place": "laundry", "branch": "yunusobod", "message": "Кир ювиш машинаси ишламаяпти"})[0]
    anon_text = feedback.director_text(anon)
    check_true("no name, no phone = anonymous", anon.anonymous and "👤 Аноним" in anon_text)
    check_true("🔴 and the business head the Director's message",
               anon_text.startswith("🔴 <b>Мижоз шикояти — Londry · Юнусобод</b>"))
    check_true("no opinion wording left", "фикр" not in anon_text.lower())
    check("the branch must be picked", feedback.clean({"place": "garmin", "message": "Соат синди"})[1], "branch")
    check("a branch of the other business is refused",
          feedback.clean({"place": "garmin", "branch": "yunusobod", "message": "Соат синди"})[1], "branch")
    check_true("the Director's message is Uzbek Cyrillic", latin_words(anon_text) == [])
    risky = feedback.clean({"place": "garmin", "branch": "abay", "message": "<b>x</b> & y", "name": "<i>"})[0]
    text = feedback.director_text(risky)
    check_true("what the client typed is escaped", "&lt;b&gt;x&lt;/b&gt; &amp; y" in text and "<i>" not in text)
    described = feedback.describe([
        {"created_at": datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc), "kind": "complaint", "place": "garmin",
         "message": "Соат ишламаяпти", "phone": None, "contact_name": None},
        {"created_at": datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc), "kind": "feedback", "place": None,
         "message": "Яхши", "phone": None, "contact_name": None},
    ])
    check_true("the bot's data names the business, and marks old opinions",
               "] Garmin | Соат" in described and "not stated (feedback) | Яхши" in described)

    # ---- three languages (Uzbek Latin dropped 2026-09-28)
    import re

    for header, expected in (
        ("ru-RU,ru;q=0.9,en;q=0.8", "ru"), ("en-US,en;q=0.9", "en"), ("uz-Latn-UZ", "uz_cyrl"),
        ("uz-UZ", "uz_cyrl"), ("uz-Cyrl-UZ,ru;q=0.8", "uz_cyrl"), ("de-DE,de;q=0.9", "uz_cyrl"),
        ("de,ru;q=0.5", "ru"), ("ru;q=0.5,en;q=0.9", "en"), ("", "uz_cyrl"), ("ru;q=abc,en", "en"),
    ):
        check(f"Accept-Language {header!r}", feedback_page.lang_from(None, header), expected)
    check("?lang= wins over the phone's language", feedback_page.lang_from("ru", "en-US"), "ru")
    check("an unknown ?lang= is ignored", feedback_page.lang_from("uz_latn", "en"), "en")
    keys = set(feedback_page.TEXTS["uz_cyrl"])
    check_true("every language has every text",
               all(set(t) == keys for t in feedback_page.TEXTS.values()) and set(feedback_page.TEXTS) == set(feedback.LANGS))
    check_true("every error has a translation in every language",
               all(feedback_page.error_text(k, lang) for k in ("branch", "empty", "too_long", "phone", "rate", "failed")
                   for lang in feedback.LANGS))

    def outside_nav(page_text):
        return re.sub(r"<nav>.*?</nav>", "", page_text.split("<body>")[-1])

    en_page = outside_nav(feedback_page.form_html(lang="en") + feedback_page.thanks_html(True, "en"))
    check_true("en: no Cyrillic left untranslated", not re.search(r"[А-яЁёЎўҚқҒғҲҳ]", en_page))
    ru_page = outside_nav(feedback_page.form_html(lang="ru") + feedback_page.thanks_html(True, "ru"))
    check_true("ru: no Latin words left untranslated", latin_words(ru_page) == [])
    ru_sub = feedback.clean({"place": "garmin", "branch": "abay", "message": "Всё плохо", "lang": "ru"})[0]
    check_true("the Director is told the client's language, in Cyrillic",
               "🌐 Мижоз тили: русча" in feedback.director_text(ru_sub))
    check_true("no language line for the default language", "🌐" not in anon_text)
    check("an unknown language falls back to the default",
          feedback.clean({"place": "garmin", "branch": "abay", "message": "Ёмон", "lang": "zz"})[0].lang, "uz_cyrl")
    for lang in feedback.LANGS:
        page_text = feedback_page.form_html(lang=lang) + feedback_page.thanks_html(True, lang)
        check_true(f"{lang}: no opinion or business choice, no emoji icons",
                   "name='kind'" not in page_text and "name='place'" not in page_text
                   and not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", page_text))

    # ---- the page
    submitted = []

    async def fake_submit(submission):
        submitted.append(submission)
        return 1

    original_submit = feedback.submit
    feedback.submit = fake_submit
    feedback_page._recent.clear()
    client = TestClient(api_app.app)
    try:
        page = client.get("/f")
        check_true("/f (on the printed Londry card) is Londry's form",
                   page.status_code == 200 and "Шикоятни юбориш" in page.text and "action='/f'" in page.text
                   and ">Londry<" in page.text and "Garmin" not in page.text)
        garmin = client.get("/f/garmin")
        check_true("/f/garmin is Garmin's form, posting back to itself",
                   garmin.status_code == 200 and "action='/f/garmin'" in garmin.text and ">Garmin<" in garmin.text
                   and "Londry" not in garmin.text)
        check_true("the page is Uzbek Cyrillic by default (the language links aside)",
                   latin_words(outside_nav(page.text)) == [] and "lang='uz-Cyrl'" in page.text)
        check_true("the language links are on the page, Latin Uzbek gone",
                   all(f"/f?lang={code}" in page.text for code in ("ru", "en")) and "uz_latn" not in page.text)
        check_true("Garmin's language links stay on Garmin's page",
                   all(f"/f/garmin?lang={code}" in garmin.text for code in ("ru", "en")))
        ru = client.get("/f?lang=ru")
        check_true("?lang=ru: Russian page", "lang='ru'" in ru.text and "Отправить жалобу" in ru.text
                   and "name='lang' value='ru'" in ru.text)
        en = client.get("/f/garmin", headers={"Accept-Language": "en-GB,en;q=0.9"})
        check_true("an English phone gets English", "lang='en'" in en.text and ">Send complaint<" in en.text)
        check_true("Londry's page: Londry Юнусобод and Londry Вузгородок buttons",
                   "value='yunusobod'" in page.text and "value='vuzgorodok'" in page.text and "Londry Юнусобод" in page.text
                   and "value='abay'" not in page.text)
        check_true("Garmin's page: Абай and Минор buttons",
                   "value='abay'" in garmin.text and "value='minor'" in garmin.text and "value='yunusobod'" not in garmin.text)
        pre = client.get("/f/garmin?branch=abay")
        check_true("?branch=abay preselects it", "value='abay' required checked" in pre.text)
        no_branch = client.post("/f", data={"message": "Машина ишламаяпти"})
        check_true("no branch picked: asked, beside the buttons, text kept",
                   no_branch.status_code == 400 and "Филиални танланг" in no_branch.text and "Машина ишламаяпти" in no_branch.text)
        en_branch = client.get("/f?lang=en")
        check_true("branch names in English", "Londry Yunusobod" in en_branch.text and "Which branch?" in en_branch.text)
        uz = client.get("/f", headers={"Accept-Language": "uz-Latn-UZ"})
        check_true("an Uzbek phone gets Cyrillic", "Шикоятни юбориш" in uz.text)
        for other in ("/f/laundry", "/f/cafe"):
            moved = client.get(other, follow_redirects=False)
            check_true(f"{other} goes to Londry's page", moved.status_code == 301 and moved.headers["location"] == "/f")

        ok = client.post("/f", data={"branch": "vuzgorodok", "message": "Машина ишламаяпти", "phone": "+998901234567"})
        check_true("a complaint is thanked", ok.status_code == 200 and "Раҳмат" in ok.text)
        ok_garmin = client.post("/f/garmin", data={"branch": "minor", "message": "Соат синди", "place": "laundry"})
        check_true("...on Garmin's page too", ok_garmin.status_code == 200 and "Раҳмат" in ok_garmin.text)
        check("each goes to the Director with its page's business (a posted place is ignored) and branch",
              [(s.place, s.branch) for s in submitted], [("laundry", "vuzgorodok"), ("garmin", "minor")])

        bad = client.post("/f/garmin", data={"branch": "minor", "message": "Ёмон", "phone": "12"})
        check_true("an error keeps what was typed, beside the phone field, on the same page",
                   bad.status_code == 400 and "Ёмон" in bad.text and "action='/f/garmin'" in bad.text
                   and re.search(r"id='p'[^>]*aria-invalid='true'.*нотўғри", bad.text) is not None)
        bad_ru = client.post("/f", data={"branch": "vuzgorodok", "message": "Плохо", "phone": "12", "lang": "ru"})
        check_true("the error is in the client's language", bad_ru.status_code == 400
                   and "Неверный номер" in bad_ru.text and "lang='ru'" in bad_ru.text)
        en_ok = client.post("/f/garmin", data={"branch": "minor", "message": "Broken watch", "lang": "en"})
        check_true("thanks in English, back to the same page", "Thank you!" in en_ok.text and "/f/garmin?lang=en" in en_ok.text)
        check("...and the language reaches the Director", submitted[-1].lang, "en")
        feedback_page._recent.clear()

        submitted.clear()
        bot = client.post("/f", data={"branch": "vuzgorodok", "message": "spam", "website": "http://x"})
        check_true("the hidden field drops bots quietly", bot.status_code == 200 and not submitted)

        for _ in range(5):  # counter cleared above, so five allowed, the sixth waits
            client.post("/f", data={"branch": "vuzgorodok", "message": "Ёмон"})
        limited = client.post("/f", data={"branch": "vuzgorodok", "message": "Ёмон"})
        check("the 6th message in 10 minutes waits", limited.status_code, 429)

        async def broken_submit(submission):
            raise RuntimeError("db down")

        feedback.submit = broken_submit
        feedback_page._recent.clear()
        down = client.post("/f", data={"branch": "vuzgorodok", "message": "Ёмон хизмат"})
        check_true("a failure says so and keeps the text", down.status_code == 503 and "Ёмон хизмат" in down.text)
    finally:
        feedback.submit = original_submit
        feedback_page._recent.clear()

    # ---- the printed card: red, white square, and exactly the right code
    url = "https://example.uz/f/garmin"
    plain = Image.open(io.BytesIO(qr_card.card_png(url))).convert("RGB")
    check("card without a logo: 10×10 cm at 300 dpi", plain.size, (1200, 1200))
    check_true("only red, white and black — no text on the card",
               {c for _, c in plain.getcolors(1 << 20)} <= {qr_card.RED, qr_card.WHITE, qr_card.BLACK})
    for place in feedback.PLACES:
        check_true(f"{place}: its logo file is there (background cut out)",
                   Image.open(qr_card.LOGO_DIR / qr_card.LOGOS[place]).mode == "RGBA")
    card = Image.open(io.BytesIO(qr_card.card_png(url, "garmin"))).convert("RGB")
    check("card with the logo: 10×12.3 cm", card.size, (1200, 1450))
    check("red background", card.getpixel((10, 10)), qr_card.RED)
    x0, y0, module, modules = qr_card.card_geometry(url, with_logo=True)
    band = card.crop((0, 0, 1200, y0 - 2 * module))
    white = [p for p in band.getdata() if p == qr_card.WHITE]
    check_true("the logo is drawn in white on the red band above the code", len(white) > 2000)
    lbox = band.convert("L").point(lambda v: 255 if v > 200 else 0).getbbox()
    check_true("...centred", lbox is not None and abs((lbox[0] + lbox[2]) / 2 - 600) <= 6)
    matrix = qr_card.qr_matrix(url)
    margin = qr_card.QUIET_MODULES
    read = [
        [card.getpixel((x0 + (margin + c) * module + module // 2, y0 + (margin + r) * module + module // 2)) == qr_card.BLACK
         for c in range(len(matrix))]
        for r in range(len(matrix))
    ]
    check_true("the printed modules are exactly the QR code for the URL", read == matrix)
    quiet = [card.getpixel((x0 + module // 2, y0 + i * module + module // 2)) for i in range(modules)]
    check_true("black on white with the required white margin", all(p == qr_card.WHITE for p in quiet))


def test_db_mirror() -> None:
    """The local database copy on the IT computer (2026-10-30, the owner): every 30 minutes while awake, changes
    only (table counters → checksums → 64 slices → COPY), the bot's own tables only."""
    print("local database copy (30.10.2026)")
    import importlib.util

    from integrations.api import db_viewer

    spec = importlib.util.spec_from_file_location("db_mirror", Path(__file__).resolve().parent / "db-mirror" / "mirror.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    check("only the slices that differ, and slices on one side only", m.changed_buckets(
        {1: (3, "a"), 2: (1, "b"), 5: (2, "c")}, {1: (3, "a"), 2: (1, "x"), 7: (1, "d")}), [2, 5, 7])
    check("nothing differs → nothing copied", m.changed_buckets({1: (3, "a")}, {1: (3, "a")}), [])
    now = 1_000_000.0
    seen = {"tasks": (40, now - 600), "agent_actions": (100, now - 600), "leads": (9, now - 25 * 3600)}
    churn = {"tasks": 40, "agent_actions": 104, "leads": 9, "employees": 3}
    check("a table whose counter didn't move is skipped; a changed one, one checked > 24 h ago or a new one is read",
          [t for t in ("tasks", "agent_actions", "leads", "employees") if not m.quiet(t, churn, seen, now)],
          ["agent_actions", "leads", "employees"])
    text = m.bucket_check("tasks", ["id", "status"], ["id"]).as_string(None)
    check_true("the same checksum SQL on both sides: md5 of the row's text, the slice from the key's md5",
               'md5(ROW("id", "status")::text)' in text and "get_byte(decode(md5(ROW(\"id\")::text), 'hex'), 0) % 64" in text)
    src = load_source("scripts/db-mirror/mirror.py")
    check_true("production is only read: a READ ONLY session with a time limit; both sides print values the same way",
               "default_transaction_read_only = on" in src and "statement_timeout" in src and "SET TimeZone = 'UTC'" in src
               and src.count("await local.execute(SESSION)") == 1)
    check_true("only the bot's own tables (IT's permanent /db list) — SAP, 1C, Billz, Verifix keep their structure only",
               m.TECHNICAL_TABLES is db_viewer.TECHNICAL_TABLES and "if table not in TECHNICAL_TABLES:" in src
               and 'sql.SQL("TRUNCATE {}")' in src and "PLATFORM_TABLES" not in src.split('"""', 2)[2]
               and not (db_viewer.TECHNICAL_TABLES & db_viewer.PLATFORM_TABLES))
    check_true("the audit log is insert-only: new rows by id", m.APPEND_ONLY == {"agent_actions": "id"})
    check_true("asleep or offline: the run is skipped quietly, the next one tries", "except psycopg.OperationalError" in src)
    install = load_source("scripts/db-mirror/install.sh")
    check_true("installed as a 30-minute user timer that catches up after sleep; a role that isn't a superuser",
               "OnCalendar=*:00,30" in install and "Persistent=true" in install and "--createdb" in install
               and "--superuser" not in install and "createdb mgmg_mirror" in install)


def test_removed_people_out_of_stats() -> None:
    """30.10.2026 (the owner): a removed person's days stay out of the statistics — the Friday result
    («Юбормаганлар: Абдулбосит (3 кун)» after he left), the brief's «who didn't report», tasks due, the
    Director's report questions. Their rows are kept (history), only not counted."""
    print("removed people out of statistics (30.10.2026)")
    from integrations.org_bot import store

    for fn in (store.tasks_due_between, store.reports_between, store.recent_reports):
        check_true(f"{fn.__name__}: only active people", "e.status = 'active'" in inspect_source(fn))
    src = inspect_source(store)
    check_true("the brief's day before: only active people",
               "WHERE r.report_date = (SELECT max(report_date) FROM daily_reports WHERE report_date < %s)\n          AND r.status <> 'excused' AND e.status = 'active'" in src)


def test_deepseek_web_search() -> None:
    """DeepSeek web search through OpenRouter every 3 days (2026-10-30): Exa results' cited pages into our index."""
    print("DeepSeek web search (30.10.2026)")
    import asyncio
    import uuid

    from integrations.common.config import settings
    from integrations.search import deepseek_web as dw

    check_true("every third calendar day", [dw.due(date(2026, 10, 30) + timedelta(days=i)) for i in range(6)].count(True) == 2)
    body = dw.payload("new hotel Uzbekistan")
    check_true("DeepSeek with OpenRouter's web plugin on Exa, 10 results",
               body["model"].startswith("deepseek/") and body["plugins"] == [{"id": "web", "engine": "exa", "max_results": 10}])
    answer = {"choices": [{"message": {"content": "...", "annotations": [
        {"type": "url_citation", "url_citation": {"url": "https://spot.uz/a", "title": "IHG <b>Garner</b> 2027", "content": "Toshkent"}},
        {"type": "url_citation", "url_citation": {"url": "javascript:x", "title": "bad"}},
        {"type": "file", "file": {}}]}}]}
    check("the cited pages become index items (only real links)",
          [(i["url"], i["title"], i["source"]) for i in dw.citations(answer)], [("https://spot.uz/a", "IHG Garner 2027", "deepseek_web")])

    posted, stored, logged = [], [], []

    class Response:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return answer

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            posted.append(json["messages"][0]["content"])
            return Response(402 if len(posted) == 2 else 200)

    async def fake_store(rows):
        stored.extend(rows)
        return len(rows)

    async def fake_log(**kw):
        logged.append(kw)

    from pydantic import SecretStr

    saved = [(dw, "_client", dw._client), (dw.crawler, "store", dw.crawler.store), (dw, "log_action", dw.log_action),
             (dw, "CONCURRENCY", dw.CONCURRENCY), (settings, "openrouter_api_key", settings.openrouter_api_key)]
    dw._client, dw.crawler.store, dw.log_action, dw.CONCURRENCY = (lambda key: Client()), fake_store, fake_log, 1
    settings.openrouter_api_key = SecretStr("test-key")
    try:
        counts = asyncio.run(dw.search(["q1", "q2", "q3", "q4"], uuid.uuid4()))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check("out of credit (402) stops the rest; what came is kept", (len(posted), counts["new"], counts["failed"]), (2, 1, 1))
    check_true("...logged with counts only", logged[-1]["payload"]["stopped"] and "spot.uz" not in str(logged[-1]))
    src = load_source("agents/lead-agent/agent.py")
    check_true("the Lead Agent runs it on its day, before searching the index",
               "deepseek_web.due(today_local())" in src and src.index("deepseek_web.search(") < src.index("crawler.search("))


def test_lead_index() -> None:
    """Our own lead search index (2026-10-30, the owner: «build a search engine like Tavily and SerpApi»): the
    paid APIs had run out every month (429 / 432); Uzbek news RSS, Telegram channels and Google News are
    collected into our database and searched there; the paid APIs are a small weekly backup."""
    print("lead search index (30.10.2026)")
    import asyncio
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.search import crawler
    from integrations.search.models import RawLead

    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Андижонда 300 ўринли меҳмонхона қурилади</title><link>https://kun.uz/news/1</link>
        <description><![CDATA[<p>Аэропорт ёнида <b>янги</b> меҳмонхона</p>]]></description>
        <pubDate>Thu, 30 Oct 2026 07:10:00 +0500</pubDate></item>
      <item><title></title><link>https://kun.uz/news/2</link></item></channel></rss>"""
    items = crawler.parse_rss(rss, "kun.uz")
    check("RSS: title, link, text without tags, date; an untitled item skipped",
          [(i["url"], i["body"], i["published_at"].isoformat()) for i in items],
          [("https://kun.uz/news/1", "Аэропорт ёнида янги меҳмонхона", "2026-10-30T07:10:00+05:00")])
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Hotel opens</title>
      <link href="https://x.uz/a"/><summary>Tashkent</summary><updated>2026-10-29T10:00:00Z</updated></entry></feed>"""
    check("Atom entries too", [(i["url"], i["title"]) for i in crawler.parse_rss(atom, "x")], [("https://x.uz/a", "Hotel opens")])
    broken = '<rss><channel><atom:link href="x"/><item><title>Обзор</title><link>https://review.uz/1</link></item></channel></rss>'
    check("a feed that isn't valid XML (review.uz) is read by pattern", [i["url"] for i in crawler.parse_rss(broken, "review.uz")],
          ["https://review.uz/1"])
    tg = ('<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="minsportuz/812">'
          '<div class="tgme_widget_message_text js-message_text" dir="auto">Toshkent marafoni&nbsp;2027: <b>homiy</b> kompaniyalar</div>'
          '<time datetime="2026-10-30T10:50:32+00:00" class="time">10:50</time></div></div>'
          '<div class="tgme_widget_message_wrap"><div data-post="minsportuz/813"><div class="tgme_widget_message_photo"></div></div></div>')
    posts = crawler.parse_telegram(tg, "minsportuz")
    check("a public Telegram channel: one item per post with text, its link and time",
          [(p["url"], p["body"], p["source"]) for p in posts],
          [("https://t.me/minsportuz/812", "Toshkent marafoni 2027: homiy kompaniyalar", "telegram:minsportuz")])
    check_true("Google News RSS: free, no key, the last 90 days, Russian queries in Russian",
               crawler.google_news_url("новая гостиница").startswith("https://news.google.com/rss/search?q=")
               and "when:90d" in crawler.google_news_url("x") and "hl=ru" in crawler.google_news_url("новая гостиница"))
    q = crawler.tsquery()
    check_true("the search: an object word AND a signal word, prefixes, Uzbek Cyrillic and Latin, Russian, English",
               q.count(") & (") == 1 and all(f"{w}:*" in q for w in ("гостиниц", "mehmonxon", "меҳмонхон", "hotel", "марафон",
                                                                    "qurilish", "строител", "тендер", "homiy", "sponsor")))
    check_true("checked live sources: 11 news feeds, Telegram channels (no Russian procurement or football channels)",
               len(crawler.RSS_FEEDS) == 11 and "goszakupki_uz" not in crawler.TELEGRAM_CHANNELS
               and "uzreport" not in crawler.TELEGRAM_CHANNELS and "minstroyuz" in crawler.TELEGRAM_CHANNELS)

    # ---- crawl: every source once, one down never stops the others; counts only in the log
    saved = [(crawler, n, getattr(crawler, n)) for n in ("_get", "store", "execute", "log_action")]
    stored, logged, sql = [], [], []

    async def fake_get(client, url):
        if "daryo" in url:
            raise RuntimeError("down")
        return tg if url.startswith("https://t.me/") else rss

    async def fake_store(rows):
        stored.extend(rows)
        return len(rows)

    async def fake_execute(query, params=None):
        sql.append(query)

    async def fake_log(**kw):
        logged.append(kw)

    crawler._get, crawler.store, crawler.execute, crawler.log_action = fake_get, fake_store, fake_execute, fake_log
    try:
        counts = asyncio.run(crawler.crawl(uuid.uuid4(), google_news_queries=["новая гостиница"]))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check("crawl: every source, two daryo feeds down, the rest kept (same URL once)",
          (counts["sources"], counts["failed"], counts["items"]),
          (len(crawler.RSS_FEEDS) + len(crawler.TELEGRAM_CHANNELS) + 1, 2, 2))  # the fake pages share their URLs
    check_true("...old items deleted after 60 days, the run counted, the log has counts and source names only",
               any("DELETE FROM crawl_items" in s for s in sql) and any("INSERT INTO crawl_runs" in s for s in sql)
               and logged[-1]["payload"]["failed"] == 2 and "Андижон" not in str(logged[-1]))
    check_true("the API's round crawls every 3 hours in the day (06–22), nothing at night",
               "crawler.tick(" in load_source("integrations/api/app.py")
               and not asyncio.run(crawler.due(datetime(2026, 10, 30, 2, 0))))

    # ---- the Lead Agent: the index first; SerpAPI / Tavily weekly, Google only, stop at the quota
    agent = load_agent("lead-agent")
    check_true("paid searches: Mondays only, the tender-portal site: searches + the hotel chains (≤ 12)",
               agent.paid_search_due(date(2026, 11, 2)) and not agent.paid_search_due(date(2026, 10, 30))
               and len(agent.PAID_QUERIES) <= 12 and all(q in agent.PAID_QUERIES for q in agent.ALL_TENDER_SITE_QUERIES))
    check_true("429 / 432 is a plan's limit", agent.is_quota_error(RuntimeError("SerpAPI google search failed after retries: 429 from"))
               and agent.is_quota_error(RuntimeError('Tavily search failed: HTTP 432 {"detail": "This request exceeds your plan"}'))
               and not agent.is_quota_error(RuntimeError("timeout")))
    calls = {"serp": [], "tavily": 0, "crawl": []}

    class Serp:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def search(self, query, engine, num=10, freshness=None):
            calls["serp"].append(engine)
            raise RuntimeError("SerpAPI google search failed after retries: 429 from https://serpapi.com")

    class Tav(Serp):
        async def search_news(self, query, days=1, max_results=10):
            calls["tavily"] += 1
            return []

    class Empty(Serp):
        async def recent_projects(self, **kw):
            return []

        async def search_all_keywords(self):
            return [RawLead(source="uzex", title="Тендер", url="https://etender.uzex.uz/1")]

    async def fake_crawl(run_id, google_news_queries=None):
        calls["crawl"].append(len(google_news_queries or []))

    async def fake_search(since):
        return [RawLead(source="index:kun.uz", title="Меҳмонхона қурилади", url="https://kun.uz/news/1")]

    saved = [(agent, n, getattr(agent, n)) for n in ("SerpAPIClient", "TavilyClient", "WorldBankClient", "UzExClient", "today_local")]
    saved += [(crawler, "crawl", crawler.crawl), (crawler, "search", crawler.search),
              (agent.deepseek_web, "search", agent.deepseek_web.search)]

    async def no_web(queries, run_id=None):  # never a paid call from a test
        return {}

    agent.deepseek_web.search = no_web
    agent.SerpAPIClient, agent.TavilyClient, agent.WorldBankClient, agent.UzExClient = Serp, Tav, Empty, Empty
    crawler.crawl, crawler.search = fake_crawl, fake_search
    try:
        agent.today_local = lambda: date(2026, 10, 30)  # a Friday
        friday = asyncio.run(agent.collect_raw_leads(uuid.uuid4()))
        check_true("a weekday: no paid request at all; the index crawled with Google News for every news query",
                   calls["serp"] == [] and calls["tavily"] == 0 and calls["crawl"] == [len(agent.FRESH_QUERIES)])
        check("...tenders first, then our index", [r.source for r in friday], ["uzex", "index:kun.uz"])
        agent.today_local = lambda: date(2026, 11, 2)  # a Monday
        asyncio.run(agent.collect_raw_leads(uuid.uuid4()))
        check_true("Monday: Google only, and the first 429 stops SerpAPI's other queries (not 51 × 3 errors)",
                   set(calls["serp"]) == {"google"} and len(calls["serp"]) <= 4 and calls["tavily"] == len(agent.PAID_QUERIES))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("the schema: the index with its full-text column, the runs",
               "CREATE TABLE IF NOT EXISTS crawl_items" in load_source("database/schema.sql")
               and "to_tsvector('simple'" in load_source("database/schema.sql")
               and "CREATE TABLE IF NOT EXISTS crawl_runs" in load_source("database/schema.sql"))


def test_lead_tracker() -> None:
    """The B2B lead tracker (2026-10-28, the Boss): numbered leads to B2B Sotuv, the 15:00 follow-up (contacted?
    sold — what and how much / not sold — why), the Boss's statistics (/lidlar, Mondays, PULSE, the PDF table)
    and stop / start from the Director's and the admin's /lidlar. Stopped on 07.10, back on 28.10."""
    print("B2B lead tracker (28.10.2026)")
    import asyncio
    import uuid
    from datetime import timezone

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import admin, kpi_score, lead_tracker, leads, ops_manager, store
    from integrations.org_bot.tone import is_polite
    from integrations.pulse import data as pulse_data
    from integrations.telegram import menus

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    root = Path(__file__).resolve().parents[1]
    d = date(2026, 10, 28)

    # ---- the sheet, still read by header (the Lead Agent's own columns)
    check("the sheet's columns are the Lead Agent's own", leads.SHEET_COLUMNS, load_agent("lead-agent").SHEET_COLUMNS)
    check_true("...and the bot's", ops_manager.LEAD_SHEET_COLUMNS is leads.SHEET_COLUMNS)
    sheet = [["company_name", *leads.SHEET_COLUMNS[1:]],
             ["Hilton", "Hilton Tashkent", "hotel", "Tashkent", "under construction", "", "", "", "", "", "", "", "",
              "0.8", "high", "", "", "2026-10-27", "hilton|eq", "equipment_sales"],
             ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]]
    rows = leads.rows_to_leads(sheet)
    check("sheet rows → leads (an unnamed row skipped)", (len(rows), rows[0]["dedupe_key"], rows[0]["date_added"]),
          (1, "hilton|eq", date(2026, 10, 27)))

    # ---- the morning: the newest lead first, a number on every card, the Boss's list
    people = [{"id": "b", "full_name": "Ғиёсиддин Махкамов"}, {"id": "a", "full_name": "Абдукарим Зунунов"}]
    pool = [{"id": 31, "company_name": "Old", "date_added": date(2026, 10, 1)},
            {"id": 33, "company_name": "Hilton", "date_added": date(2026, 10, 27)},
            {"id": 32, "company_name": "Hyatt", "date_added": date(2026, 10, 27)}]
    plan = leads.plan_handout(people, pool)
    check("one lead each, the newest first (same day: the later sheet row)",
          [(p["id"], lead["id"]) for p, lead in plan], [("a", 33), ("b", 32)])
    hilton = {"id": 33, "company_name": "Hilton", "location": "Tashkent", "project_stage": "under construction",
              "priority": "high", "signal_source_url": "https://example.uz/a"}
    card = leads.card_text(hilton, "Меҳмонхона қурилмоқда.", "Тошкент")
    check_true("the card: «Лид №33 Hilton», stage and priority in words, the 15:00 promise",
               "🎯 <b>Лид №33 Hilton</b>" in card and "қурилмоқда" in card and "муҳимлиги юқори" in card and "15:00" in card)
    copy = leads.boss_copy_text([("Абдукарим Зунунов", hilton)], d)
    check_true("the Boss's copy: who got which number («менга ҳам келсин»)",
               "№33 Hilton → Абдукарим Зунунов" in copy and "/lidlar" in copy)
    runner = load_source("scripts/run_morning_agents.py")
    check_true("08:00: the agent is back, right after the Lead Agent fills the sheet",
               (root / "agents" / "lead-handout" / "agent.py").exists()
               and runner.index("agents/lead-agent/agent.py") < runner.index("agents/lead-handout/agent.py --morning"))
    agent_src = load_source("agents/lead-handout/agent.py")
    check_true("...nothing goes out while stopped, on a day off or frozen; Mondays the Boss's statistics",
               "lead_tracker.is_on()" in agent_src and "is_day_off" in agent_src and "bots_frozen" in agent_src
               and "today.weekday() == 0" in agent_src and "lead_tracker.weekly(" in agent_src)
    check_true("15:00 inside the API's 5-minute round (no daytime cron)",
               "lead_tracker.tick(" in load_source("integrations/api/app.py")
               and "DAYTIME_AGENTS: list[str] = []" in runner)
    check_true("15:00–15:25 only", leads.checkin_due(datetime(2026, 10, 28, 15, 0)) and leads.checkin_due(datetime(2026, 10, 28, 15, 24))
               and not leads.checkin_due(datetime(2026, 10, 28, 14, 59)) and not leads.checkin_due(datetime(2026, 10, 28, 15, 25)))

    # ---- the buttons, the amount, the money
    cid = str(uuid.uuid4())
    datas = [b["callback_data"] for kb in (leads.contact_keyboard(cid), leads.result_keyboard(cid), leads.reason_keyboard(cid))
             for row in kb["inline_keyboard"] for b in row]
    check_true("every button fits Telegram's 64 bytes", all(len(x.encode()) <= 64 for x in datas))
    check("button values", [leads.parse_value(f"{cid}:{v}") is not None for v in ("t", "y", "n", "s", "x", "p", "r0", "r5", "r6", "q")],
          [True] * 8 + [False, False])
    for text, want in (("2 та кир ювиш машинаси, 120 млн сўм", (120e6, "UZS")), ("$5 000", (5000.0, "USD")),
                       ("1,5 млрд", (1.5e9, "UZS")), ("350 000 сўм", (350000.0, "UZS")), ("15 минг доллар", (15000.0, "USD")),
                       ("120", None), ("2 та", None), ("ҳали гаплашяпмиз", None)):
        check(f"amount: {text}", leads.parse_amount(text), want)
    check("money in words", [leads.money(120e6), leads.money(1.5e9), leads.money(350000), leads.money(5000, "USD")],
          ["120 млн сўм", "1,5 млрд сўм", "350 000 сўм", "$5 000"])
    check_true("questions to sellers are polite", all(is_polite(t) for t in (
        leads.SALE_QUESTION, leads.SALE_AGAIN, leads.REASON_QUESTION, leads.contact_question(hilton, 2))))

    # ---- the Boss's statistics: given / contacted (days, late) / sold (how much) / not sold (why) / lost
    def row(n, status, given, contacted=None, outcome=None, amount=None, person="Абдукарим Зунунов"):
        return {"lead_id": n, "company_name": f"Co{n}", "person": person, "employee_id": person, "assigned_on": given,
                "contacted_on": contacted, "status": status, "outcome": outcome, "sold_amount": amount, "sold_currency": "UZS"}

    sample = [row(1, "done", date(2026, 10, 20), date(2026, 10, 20), "2 та машина", 120e6),
              row(2, "done", date(2026, 10, 20), date(2026, 10, 22), "1 та", 180e6, "Ғиёсиддин Махкамов"),
              row(3, "dismissed", date(2026, 10, 21), date(2026, 10, 22), "лид эскирган"),
              row(4, "dismissed", date(2026, 10, 14), None, leads.AUTO_REASON, person="Ғиёсиддин Махкамов"),
              row(5, "new", d),
              row(6, "new", date(2026, 10, 25), person="Ғиёсиддин Махкамов"),
              row(7, "in_progress", date(2026, 10, 27), date(2026, 10, 27))]
    s = leads.stats(sample, d, 150e6)
    check("totals", {k: s[k] for k in ("given", "contacted", "avg_days", "late", "sold", "not_sold", "open", "our_fault")},
          {"given": 7, "contacted": 4, "avg_days": 0.8, "late": 3, "sold": 2, "not_sold": 2, "open": 3, "our_fault": 1})
    check("sold sum and the lost opportunity (not sold × the average sale)",
          (s["sold_sum"], s["lost_value"], s["our_fault_value"]), ({"UZS": 300e6}, 300_000_000, 150_000_000))
    check("reasons counted by their button («бошқа сабаб: …» typed counts as «бошқа сабаб»)",
          leads.stats(sample + [row(8, "dismissed", d, d, "бошқа сабаб: директори йўқ")], d, None)["reasons"],
          [("лид эскирган", 1), (leads.AUTO_REASON, 1), ("бошқа сабаб", 1)])
    check("per seller", [(p["name"], p["given"], p["sold"], p["late"]) for p in s["sellers"]],
          [("Абдукарим Зунунов", 4, 1, 0), ("Ғиёсиддин Махкамов", 3, 1, 3)])
    text = leads.stats_text(s, "30 кун", True)
    check_true("the Boss's message: given, contacted in how many days, late, sold how much, not sold by reason, "
               "the lost opportunity, each seller",
               "Берилди: <b>7</b>" in text and "ўртача 0,8 кунда" in text and "Сотилди: <b>2</b> — 300 млн сўм" in text
               and "• лид эскирган — 1" in text and "Бой берилган имконият ≈ 300 млн сўм" in text
               and "кеч боғланилган 1 лид ≈ 150 млн сўм" in text and "• Ғиёсиддин Махкамов — 3 лид" in text
               and "(2 кунда)" in text and latin_words(text, allow={"B2B"}) == [])
    check_true("...before any sale the loss isn't guessed", "биринчи сотувдан кейин" in leads.stats_text(leads.stats(sample[2:], d, None), "7 кун", False))
    table = leads.table_rows(sample, d)
    check("the table: newest first, days to contact, late marked",
          [(t["no"], t["contacted"], t["late"], t["state"]) for t in table[:2]]
          + [(t["no"], t["detail"]) for t in table if t["no"] in (1, 4)],
          [(5, "—", False, "боғланилмаган"), (7, "27.10 · 0 кун", False, "жараёнда"), (1, "120 млн сўм · 2 та машина"),
           (4, leads.AUTO_REASON)])
    check_true("the Director's questions get the numbers and every lead", "№1 Co1" in leads.describe(sample, s)
               and "Lost opportunity estimate: 300 млн сўм" in leads.describe(sample, s))

    # ---- the 15:00 answers (buttons, then typed): the store and Telegram faked
    sent, edits, toasts, calls = [], [], [], []

    class Bot:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_message(self, text, chat_id=None, reply_markup=None):
            sent.append((int(chat_id), text, reply_markup))
            return [500 + len(sent)]

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append((text, reply_markup))

        async def _answer_callback(self, query_id, text):
            toasts.append(text)

        async def send_file(self, content, filename, chat_id, caption=None, **kw):
            sent.append((int(chat_id), filename, None))

    state = {"answer": None, "contacted_on": None, "status": "new", "question": "sale"}

    async def lead_checkin(c):
        return {"id": c, "checkin_day": d, "telegram_user_id": 602, "answer": state["answer"], "status": state["status"],
                "assigned_on": date(2026, 10, 27), "contacted_on": state["contacted_on"], "lead_id": 33,
                "company_name": "Hilton", "project_name": None, "question": None}

    async def record(name, result=None):
        async def fn(*args, **kwargs):
            calls.append((name, args))
            return result
        return fn

    async def by_role(role):
        return [{"telegram_user_id": 900, "full_name": "Шухрат"}] if role == "operatsion_direktor" else []

    async def employee_of(tg):
        return {"telegram_user_id": tg, "status": "active", "full_name": {900: "Шухрат", 602: "Абдукарим Зунунов"}.get(tg, "X"),
                "role": "operatsion_direktor" if tg == 900 else "b2b_sotuv", "id": f"e{tg}"}

    async def waiting(tg, minutes):
        return {"id": cid, "question": state["question"], "question_message_id": 777, "lead_id": 33, "company_name": "Hilton"}

    def tap(value, who=602):
        return {"id": "q", "from": {"id": who}, "data": f"lq:{cid}:{value}",
                "message": {"message_id": 801, "chat": {"id": who}, "text": "?"}}

    def go(coro):
        return asyncio.run(coro)

    try:
        patch(lead_tracker, "TelegramBot", Bot)
        patch(store, "lead_checkin", lead_checkin)
        for name, result in (("mark_lead_contacted", {"id": "a"}), ("answer_lead_checkin", {"id": cid}),
                             ("ask_lead_question", None), ("save_lead_answer", {"id": "a"}), ("set_lead_switch", None)):
            patch(store, name, go(record(name, result)))
        patch(store, "active_employees_by_role", by_role)
        patch(store, "get_employee_by_telegram_id", employee_of)
        patch(store, "pending_lead_question", waiting)

        check("someone else's question: refused", go(ops_manager._handle_callback(tap("t", 603), uuid.uuid4())), "lead_unauthorized")
        check("«✅ Ҳа, бугун»", go(ops_manager._handle_callback(tap("t"), uuid.uuid4())), "lead_contacted")
        check_true("...the day kept, the same message now asks the result",
                   calls[-1] == ("mark_lead_contacted", (cid, 602, d)) and "Натижаси қандай?" in edits[-1][0]
                   and f"lq:{cid}:s" in str(edits[-1][1]))
        go(ops_manager._handle_callback(tap("y"), uuid.uuid4()))
        check("«✅ Ҳа, кеча» keeps yesterday", calls[-1], ("mark_lead_contacted", (cid, 602, d - timedelta(days=1))))
        state["contacted_on"] = date(2026, 10, 27)
        check("«💰 Сотилди»", go(ops_manager._handle_callback(tap("s"), uuid.uuid4())), "lead_sold")
        check_true("...closed as done, then «what and how much?» asked and remembered",
                   ("answer_lead_checkin", (cid, 602, "done")) in calls and sent[-1][1] == leads.SALE_QUESTION
                   and calls[-1][0] == "ask_lead_question" and calls[-1][1][:2] == (cid, "sale"))
        employee = {"telegram_user_id": 602, "full_name": "Абдукарим Зунунов", "role": "b2b_sotuv", "id": "e602"}
        check("words without a number go on to the usual flow (the AI)",
              go(lead_tracker.take_message(employee, {"text": "ҳали ҳужжатлар тайёрланмоқда"}, uuid.uuid4())), None)
        check("a bare «120» is asked again, never guessed",
              go(lead_tracker.take_message(employee, {"text": "120"}, uuid.uuid4())), "lead_sale_unclear")
        check("a reply to another message isn't the answer",
              go(lead_tracker.take_message(employee, {"text": "120 млн", "reply_to_message": {"message_id": 5}}, uuid.uuid4())), None)
        sent.clear()
        check("«2 та кир ювиш машинаси, 120 млн сўм»",
              go(lead_tracker.take_message(employee, {"text": "2 та кир ювиш машинаси, 120 млн сўм"}, uuid.uuid4())), "lead_sale_saved")
        check_true("...kept with the amount; the Boss told at once",
                   calls[-1] == ("save_lead_answer", (cid, "2 та кир ювиш машинаси, 120 млн сўм",
                                                      "2 та кир ювиш машинаси, 120 млн сўм", 120e6, "UZS"))
                   and any(c == 900 and "Лид №33 Hilton сотилди" in t and "120 млн сўм" in t for c, t, _ in sent))
        check("«❌ Сотилмади» → the reasons", go(ops_manager._handle_callback(tap("x"), uuid.uuid4())), "lead_reason_asked")
        check_true("...as buttons on the same message", "Нима сабабдан" in edits[-1][0] and "лид эскирган" in str(edits[-1][1]))
        go(ops_manager._handle_callback(tap("r0"), uuid.uuid4()))
        check("a reason button closes it with that reason", calls[-1], ("answer_lead_checkin", (cid, 602, "dismissed", "лид эскирган")))
        go(ops_manager._handle_callback(tap("r5"), uuid.uuid4()))
        check_true("«бошқа сабаб» asks to write it", calls[-1][0] == "ask_lead_question" and calls[-1][1][1] == "reason")
        state["question"] = "reason"
        check("the written reason is kept", go(lead_tracker.take_message(employee, {"text": "директори алмашди"}, uuid.uuid4())),
              "lead_reason_saved")
        check("...as «бошқа сабаб: …»", calls[-1][1][2], "бошқа сабаб: директори алмашди")
        go(ops_manager._handle_callback(tap("n"), uuid.uuid4()))
        check_true("«❌ Ҳали йўқ»: asked again tomorrow, told when it closes",
                   calls[-1] == ("answer_lead_checkin", (cid, 602, "not_contacted")) and "ёпилади" in edits[-1][0])
        state["answer"] = "done"
        check("answered already", go(ops_manager._handle_callback(tap("p"), uuid.uuid4())), "lead_already_answered")

        # ---- stop / start: the Director (OPS Manager) and the admin (Admin Bot); the other side told
        told_admin = []

        async def tell_admin(text, run_id):
            told_admin.append(text)

        async def figures(days):
            return s, sample, d - timedelta(days=days - 1)

        async def switch():
            return {"enabled": True, "set_by": "Шухрат", "set_at": datetime(2026, 10, 28, 9, 5, tzinfo=timezone.utc)}

        patch(admin, "tell_admin", tell_admin)
        patch(lead_tracker, "figures", figures)
        patch(store, "lead_switch", switch)
        sw = {"id": "q", "from": {"id": 602}, "data": "lsw:off", "message": {"message_id": 9, "chat": {"id": 602}}}
        check("only the Director stops it in OPS Manager", go(ops_manager._handle_callback(sw, uuid.uuid4())), "lead_unauthorized")
        sw["from"]["id"] = 900
        check("the Director's ⏸", go(ops_manager._handle_callback(sw, uuid.uuid4())), "lead_tracker_off")
        check_true("...kept with his name; IT told (no figures)",
                   calls[-1] == ("set_lead_switch", (False, "Шухрат")) and told_admin and "тўхтатди" in told_admin[-1]
                   and "сўм" not in told_admin[-1])
        text, keyboard = go(lead_tracker.director_view())
        check_true("the Director's /lidlar: the statistics, ⏸ and 📄", "Берилди: <b>7</b>" in text
                   and "lsw:off" in str(keyboard) and "lpdf:30" in str(keyboard))
        text, keyboard = lead_tracker.admin_view(None)
        check_true("the admin's /lidlar: on or off only, never started = stopped, no leads or sums (IT sees no figures)",
                   "⏸ Тўхтатилган" in text and "lsw:on" in str(keyboard) and "сўм" not in text and "№" not in text)
        sent.clear()
        go(lead_tracker.set_switch(True, "ulugbek", True, uuid.uuid4()))
        check_true("the admin's ▶️: the Director told", any(c == 900 and "ёқди" in t for c, t, _ in sent))
        check_true("Admin Bot routes /lidlar and ⏸ / ▶️",
                   "/lidlar" in admin.LEADS_COMMANDS and 'action == "lsw"' in inspect_source(admin.handle_admin_callback))

        async def off():
            return False

        async def boom(run_id):
            raise AssertionError("asked while stopped")

        patch(lead_tracker, "is_on", off)
        patch(lead_tracker, "checkin", boom)
        patch(lead_tracker, "now_local", lambda: datetime(2026, 10, 28, 15, 5))
        check("stopped: no 15:00 questions", go(lead_tracker.tick(uuid.uuid4())), 0)

        # ---- PULSE: the Director's leads card
        payload = go(pulse_data.leads(30))
        check_true("PULSE gets the figures, reasons, sellers and every lead",
                   payload["given"] == 7 and payload["soldSum"] == 300_000_000 and payload["lost"] == 300_000_000
                   and payload["reasons"][0] == {"label": "лид эскирган", "n": 1} and len(payload["rows"]) == 7
                   and payload["enabled"] is True)
    finally:
        restore()
    page_src = load_source("integrations/api/pulse_page.py")
    html, js = load_source("integrations/api/static/pulse.html"), load_source("integrations/api/static/pulse.js")
    check_true("PULSE: /pulse/api/leads for the Director only (the Garmin manager's view is his team)",
               '@router.get("/api/leads")' in page_src and "if who[1] is not None:" in page_src
               and 'id="leads-card"' in html and "hidden" in html.split('id="leads-card"')[1][:80]
               and "renderLeads" in js and "/pulse/api/leads?days=" in js)

    # ---- menus, KPI, old buttons
    check_true("menus: the admin's and the Director's /lidlar; B2B сотув their own open leads",
               "lidlar" in dict(menus.ADMIN) and "lidlar" in dict(menus.DIRECTOR) and menus.role_menu("b2b_sotuv") == menus.B2B_SALES
               and "lidlar" in dict(menus.B2B_SALES) and set(menus.EMPLOYEE) <= set(menus.B2B_SALES))
    emp = [{"id": "e1", "full_name": "Алишер", "role": "b2b_sotuv"}]
    lead_rows = [{"employee_id": "e1", "asked": 4, "answered": 3, "closed": 2, "done": 1}]
    month = kpi_score.build(emp, [], [], {}, [], [], date(2026, 10, 1), date(2026, 10, 31), lead_rows=lead_rows)[0]
    check("the 15:00 answers count in Жараён, as before", round(month.parts["process"]), 75)
    check_true("old 15:00 buttons (ld / lc, before 07.10) still just come off",
               'if prefix in ("ld", "lc"):' in inspect_source(ops_manager._handle_callback))

    try:
        import importlib

        importlib.import_module("weasyprint")
    except Exception as exc:  # noqa: BLE001
        print(f"  skip the leads PDF: WeasyPrint unavailable ({type(exc).__name__})")
        return
    from integrations.reports import render

    values = lead_tracker.pdf_view(s, sample, d - timedelta(days=29), True)
    document = render.pdf("leads.html", **values)
    check_true("📄 the table is a PDF, in Uzbek Cyrillic", document[:5] == b"%PDF-" and latin_words(
        render.render_html("leads.html", image=False, **values).split("<body")[1],
        allow={"B2B", "Lead", "Agent", "Co1", "Co2", "Co3", "Co4", "Co5", "Co6", "Co7", "Noto", "Sans"}) == [])


def inspect_source(obj) -> str:
    import inspect

    return inspect.getsource(obj)


def test_politeness_days_off_announcements() -> None:
    """2026-10-01: names keep capitals, "сиз" always, days off, announcements."""
    print("politeness, days off, announcements")
    import asyncio
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import admin, kpi, ops_manager, store, tone

    # ---- names keep their capitals; the sentence stays lowercase
    check("a name keeps its capitals, and no emoji (19.10)", tone.casual("Алишер ака, БУГУН зўр кун ☀️", keep=["Алишер ака"]),
          "Алишер ака, бугун зўр кун")
    check("the 16:00 ask and the 17:00 reminder end without an emoji (the owner, 19.10)",
          [t.rsplit(" ", 1)[-1] for t in (kpi.build_request_text("Улуғбек ака", ()), kpi.build_reminder_text("Улуғбек ака", ()))],
          ["чарчаманг", "каттакон"])

    # ---- "сиз", never "сен"
    for text, polite in (("бугун зўр ишладингиз", True), ("илтимос ёзиб юборсангиз", True), ("нима қилдингиз?", True),
                         ("менгами ишонасан", False), ("сенга раҳмат", False), ("нима қилдинг?", False),
                         ("қилсанг бўлади", False), ("бир минг сўм", True), ("Ҳасан ака", True)):
        check(f"polite: {text!r}", tone.is_polite(text), polite)
    every_fixed_line = [
        kpi.build_request_text("Алишер", ()), kpi.build_reminder_text("Алишер", ()),
        admin.TECH_ERROR_TEXT,
    ]
    check("every fixed line the bot sends is polite", [t for t in every_fixed_line if not tone.is_polite(t)], [])

    class RudeAI:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def complete_json(self, system, user):
            return {"ask": True, "follow_up": "нима қилдинг бугун?"}

    original = ops_manager.OpenRouterClient
    ops_manager.OpenRouterClient = RudeAI
    try:
        question = asyncio.run(ops_manager._report_follow_up("ok", "it", uuid.uuid4()))
    finally:
        ops_manager.OpenRouterClient = original
    check_true("an impolite follow-up becomes the polite default", tone.is_polite(question) and "бажардингиз" in question)

    # ---- days off: the admin's view
    today = date(2026, 10, 1)
    text, keyboard = admin.days_off_view(today, {today})
    buttons = [b for row in keyboard["inline_keyboard"] for b in row]
    check("two weeks of days to tap", len(buttons), 14)
    check("today marked off, named in Uzbek", buttons[0]["text"], "✅ 01.10 пайшанба")
    check_true("...and listed", "Белгиланган: 01.10 пайшанба" in text)
    check_true("every button fits 64 bytes", all(len(b["callback_data"].encode()) <= 64 for b in buttons))

    # ---- days off: nothing reaches employees
    async def off(day):
        return True

    calls = []

    async def must_not_send(*args, **kwargs):
        calls.append(args)
        return 0

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    was_dry = settings.dry_run
    patch(store, "is_day_off", off)
    reports_agent = load_agent("daily-reports")
    patch(reports_agent, "ask_everyone", must_not_send)
    patch(reports_agent, "remind_silent", must_not_send)
    try:
        for name, coro in (("report ask", reports_agent.run("ask", dry_run=True)),
                           ("report reminder", reports_agent.run("remind", dry_run=True))):
            check(f"day off: no {name}", (asyncio.run(coro), len(calls)), (0, 0))
    finally:
        settings.dry_run = was_dry
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    # ---- announcements: preview, confirm once, to everyone
    sent, edits, toasts = [], [], []
    state = {"claimed": False}

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, reply_markup=None, **kwargs):
            sent.append((chat_id, text, reply_markup))
            return [1]

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append(text)

    async def create(text, by):
        return {"id": 7, "text": text}

    async def claim(announcement_id):
        if state["claimed"]:
            return None
        state["claimed"] = True
        return {"id": announcement_id, "text": "кечирасиз, техник хатолик юз берди 🙏"}

    async def employees():
        return [{"telegram_user_id": 1, "display_name": "a"}, {"telegram_user_id": 2, "display_name": "b"}]

    async def finish(*args):
        return None

    async def fake_answer(query_id, text):
        toasts.append(text)

    saved.clear()
    for name, fake in (("create_announcement", create), ("claim_announcement", claim),
                       ("list_active_employees", employees), ("finish_announcement", finish)):
        patch(store, name, fake)
    patch(admin, "TelegramBot", FakeBot)
    patch(admin, "_answer", fake_answer)
    try:
        asyncio.run(admin.handle_admin_message({"text": "/elon", "from": {"id": 5, "username": "admin"}}, uuid.uuid4()))
        preview, keyboard = sent[-1][1], sent[-1][2]
        check_true("/elon alone previews the tech-error notice, to everyone, with Send/Cancel",
                   "техник хатолик юз берди" in preview and "2 ходимга" in preview
                   and [b["callback_data"] for b in keyboard["inline_keyboard"][0]] == ["ann:7", "annx:7"])
        asyncio.run(admin.handle_admin_message({"text": "/elon Эртага ишга соат 10 да келинг", "from": {"id": 5}}, uuid.uuid4()))
        check_true("/elon with text keeps the admin's own words, capitals included", "Эртага ишга соат 10 да келинг" in sent[-1][1])
        sent.clear()
        tap = {"id": "q", "data": "ann:7", "from": {"id": 5}, "message": {"message_id": 3, "chat": {"id": 5}}}
        asyncio.run(admin.handle_admin_callback(tap, uuid.uuid4()))
        check("Send: it reaches every employee", [c for c, _t, _k in sent], ["1", "2"])
        check_true("...and the admin sees how many", edits and "2/2" in edits[-1])
        sent.clear()
        asyncio.run(admin.handle_admin_callback(tap, uuid.uuid4()))
        check("a second tap sends nothing", sent, [])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def test_ai_chat_and_sheet() -> None:
    """2026-10-02: employees talk to the work AI, never to people; the leads sheet read by header."""
    print("work AI for employees, no relay; leads sheet by header")
    import asyncio
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import admin, ai_chat, leads, ops_manager, store

    today = date(2026, 10, 2)
    seller = {"id": "e7", "telegram_user_id": 7, "role": "b2b_sotuv", "full_name": "Алишер Каримов",
              "address_form": "aka", "status": "active", "ai_chat_off": False, "responsibilities": None}

    # ---- what the AI knows: their own work only, honesty first
    tasks = [{"task_summary": "<b>Принтерни</b> текширинг", "status": "started", "due_date": today - timedelta(days=1)},
             {"task_summary": "КП тайёрланг", "status": "sent", "due_date": None}]
    context = ai_chat.work_context(seller, tasks, today)
    check_true("their tasks (deadline, overdue) are in it",
               "Принтерни текширинг (started, due 01.10 (overdue))" in context and "КП тайёрланг (not started, no deadline)" in context)
    check_true("duties not uploaded yet: the AI is told so", "not uploaded yet" in context)
    check_true("uploaded duties are read",
               "Мижозлар билан ишлаш" in ai_chat.work_context({**seller, "responsibilities": "Мижозлар билан ишлаш"}, [], today))
    prompt = ai_chat.system_prompt(seller, context)
    check_true("honesty first: say 'I don't know', never guess",
               "HONESTY FIRST" in prompt and "билмайман" in prompt and "Never guess" in prompt)
    check_true("no company data, no other people, no passing messages to the Director",
               "no SAP" in prompt and "anything about other\nemployees" in prompt and "not to the Director" in prompt)
    check_true("Garmin sales also get the public catalog, B2B doesn't",
               "MARQ" in ai_chat.system_prompt({**seller, "role": "garmin_sotuv"}, context) and "MARQ" not in prompt)
    check_true("a Reply to a task card brings that task", "THEY ARE REPLYING TO THIS TASK CARD: КП тайёрланг"
               in ai_chat.user_prompt([], "қандай бошлай?", {"task_summary": "КП тайёрланг"}))
    for text in (ai_chat.hint_text(), ai_chat.off_text(), ai_chat.limit_text(), ai_chat.error_text()):
        check(f"friendly: {text[:30]}…", friendly_problems(text, ("AI",)), [])

    # ---- the bot: a message goes to the AI, never to a person
    replies, turns, tasks_bg = [], [], []

    class Background:
        def add_task(self, func, *args):
            tasks_bg.append((func, args))

    class FakeAI:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def complete(self, system, user, **kwargs):
            assert "HONESTY FIRST" in system and "КП тайёрланг" in system
            return "Бу маълумот менда йўқ, раҳбарингиздан сўранг"

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((text, reply_markup))
        return [1]

    async def log_turn(user, role, content):
        turns.append(role)

    async def none(*args, **kwargs):
        return None

    async def empty(*args, **kwargs):
        return []

    counter = {"n": 0}

    async def questions_today(user):
        return counter["n"]

    async def open_tasks(user):
        return tasks

    held = {"row": {"id": "h1", "message_text": "КП қандай ёзилади?"}}

    async def create_held(user, text, task_id):
        return {"id": "h1"}

    async def resolve_held(held_id, user, outcome):
        row, held["row"] = held["row"], None
        return row

    async def get_by_tg(user):
        return seller

    pending = {"row": None}

    async def pending_report(user, day):
        return pending["row"]

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    for name, fake in (("log_ai_turn", log_turn), ("recent_ai_turns", empty), ("ai_questions_today", questions_today),
                       ("open_tasks_for_employee", open_tasks),
                       ("report_being_edited", none),
                       ("pending_report", pending_report),
                       ("open_report_followup", none), ("submitted_report_today", none), ("find_task_by_message_id", none),
                       ("create_pending_relay", create_held), ("resolve_pending_relay", resolve_held),
                       ("get_employee_by_telegram_id", get_by_tg), ("report_for_day", none), ("latest_open_relay", none)):
        patch(store, name, fake)
    report_verdict = {"kind": "other"}

    async def classify_report(text, run_id):
        return report_verdict["kind"]

    patch(ops_manager, "_classify_report", classify_report)
    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "_show_typing", none)
    patch(ops_manager, "_answer", none)
    patch(ops_manager, "OpenRouterClient", FakeAI)
    bg = Background()
    try:
        outcome = asyncio.run(ops_manager._handle_employee_message(seller, {"text": "директорга айтинг, эртага келмайман"}, uuid.uuid4(), bg))
        check("an employee's message goes to the AI, not the Director", outcome, "ai_chat")
        func, args = tasks_bg[-1]
        asyncio.run(func(*args))
        check("the AI answers honestly, both turns kept", (replies[-1][0], turns), ("Бу маълумот менда йўқ, раҳбарингиздан сўранг", ["employee", "assistant"]))
        check_true("nothing offers to send anything to the Director",
                   not any("директорга юборилсинми" in r[0].lower() or "relayok" in str(r[1]) for r in replies))
        counter["n"] = ai_chat.DAILY_LIMIT
        asyncio.run(func(*args))
        check("past the daily limit: told kindly", replies[-1][0], ai_chat.limit_text())
        counter["n"] = 0
        check("/ai is just a hint now", asyncio.run(ops_manager._handle_employee_message(seller, {"text": "/ai"}, uuid.uuid4(), bg)), "ai_hint")
        check("an unknown command is ignored", asyncio.run(ops_manager._handle_employee_message(seller, {"text": "/xyz"}, uuid.uuid4(), bg)), "ignored")
        off = {**seller, "ai_chat_off": True}
        check("switched off for this person: told the bot is for work, nothing sent anywhere",
              (asyncio.run(ops_manager._handle_employee_message(off, {"text": "салом"}, uuid.uuid4(), bg)), replies[-1][0]),
              ("ai_off", ai_chat.off_text()))

        # during the report window: a "?" question goes to the AI, never offered as the report (17.10);
        # any other message gets one tap
        pending["row"] = {"id": "r1", "prompt_message_id": 50, "reminder_message_id": None}
        outcome = asyncio.run(ops_manager._handle_employee_message(seller, {"text": "КП қандай ёзилади?"}, uuid.uuid4(), bg))
        check("a question while the report is due goes to the AI, not into the report", outcome, "ai_chat")
        report_verdict["kind"] = "unsure"
        outcome = asyncio.run(ops_manager._handle_employee_message(seller, {"text": "мижоз билан учрашдим"}, uuid.uuid4(), bg))
        buttons = [b["callback_data"].split(":")[0] for row in replies[-1][1]["inline_keyboard"] for b in row]
        check("a message the AI isn't sure about while the report is due: report or question? (only then, 26.10)",
              (outcome, buttons), ("report_or_ai_asked", ["asrep", "asai"]))
        report_verdict["kind"] = "other"
        check("…one the AI says isn't a report goes to the AI, no buttons",
              asyncio.run(ops_manager._handle_employee_message(seller, {"text": "мижоз билан учрашувга бораман"}, uuid.uuid4(), bg)),
              "ai_chat")
        tasks_bg.clear()
        tap = {"id": "q", "data": "asai:h1", "from": {"id": 7}}
        check("'йўқ, бу савол' sends it to the AI", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4(), bg)), "ai_chat")
        check_true("...in the background", len(tasks_bg) == 1)
        old = {"id": "q", "data": "relayok:x", "from": {"id": 7}}
        check("an old 'send to the Director' button does nothing", asyncio.run(ops_manager._handle_callback(old, uuid.uuid4(), bg)), "relay_disabled")
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    # ---- the admin's off switch
    edits = []

    async def toggle(employee_id, by):
        return {**seller, "id": employee_id, "display_name": "a", "ai_chat_off": True}

    async def get_employee(employee_id):
        return {"id": employee_id, "status": "active", "role": "b2b_sotuv", "display_name": "a"}

    async def edit(callback, text, keyboard, run_id):
        edits.append((text, keyboard))

    saved.clear()
    patch(store, "toggle_ai_chat", toggle)
    patch(store, "get_employee", get_employee)
    patch(admin, "_edit", edit)
    patch(admin, "_answer", none)
    try:
        asyncio.run(admin.handle_admin_callback({"id": "q", "data": "aich:e7", "from": {"id": 5}}, uuid.uuid4()))
        check_true("the 🤖 button switches the AI off for one person, and the card shows it",
                   edits and "AI ёрдамчи: ўчирилган" in edits[-1][0] and "⛔ ўчирилган" in str(edits[-1][1]))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


    # ---- the leads sheet, read and written by header (people edit it now)
    cols = list(leads.SHEET_COLUMNS)
    moved = ["notes", "Менинг изоҳим", *[c for c in cols if c != "notes"]]  # a column moved, one added
    row = {c: "" for c in moved}
    row.update({"company_name": "Hyatt", "signal_source_url": "https://x.uz/1", "dedupe_key": "hyatt|equipment_sales",
                "notes": "қўнғироқ қилдим", "Менинг изоҳим": "эртага"})
    records = leads.sheet_records([moved, [row[c] for c in moved]])
    check("a moved column is still found by its header", (records[0]["company_name"], records[0]["signal_source_url"]),
          ("Hyatt", "https://x.uz/1"))
    check("...and every other column too", records[0]["dedupe_key"], "hyatt|equipment_sales")
    check("an unrecognisable header falls back to the standard order",
          leads.column_order(["А", "Б", "В"]), cols)
    lead_agent = load_agent("lead-agent")
    out = lead_agent.to_sheet_row({"company_name": "Hilton", "track": "equipment_sales"}, "2026-10-02", moved)
    check_true("a new lead is written in the sheet's current order",
               out[moved.index("company_name")] == "Hilton" and out[moved.index("date_added")] == "2026-10-02"
               and out[1] == "" and len(out) == len(moved))
    check("column letters", [leads.column_letter(n) for n in (1, 20, 26, 27, 52)], ["A", "T", "Z", "AA", "AZ"])


def test_files_reports_cheer_off() -> None:
    """2026-10-02: files go to the Director when asked; today's report confirmed, changed, deleted."""
    print("files, today's report")
    import asyncio
    import uuid

    from integrations.org_bot import ops_manager, report_tools, store

    # ---- the pure parts
    check("a photo is a file", report_tools.file_kind({"photo": [{}]}), "photo")
    check("text is not", report_tools.file_kind({"text": "салом"}), None)
    with_report = report_tools.purpose_keyboard("11111111-2222-3333-4444-555555555555", True)
    without = report_tools.purpose_keyboard("11111111-2222-3333-4444-555555555555", False)
    labels = [b["text"] for row in with_report["inline_keyboard"] for b in row]
    check("report, Director, cancel", labels, ["бугунги ҳисобот", "директорга юбориш", "бекор қилиш"])
    check_true("no 'today's report' when none was asked today",
               "бугунги ҳисобот" not in [b["text"] for row in without["inline_keyboard"] for b in row])
    check_true("buttons fit 64 bytes", all(len(b["callback_data"].encode()) <= 64 for row in with_report["inline_keyboard"] for b in row))
    caption = report_tools.director_caption("Алишер <К>", "B2B сотув", "report", "омбор расми")
    check_true("the Director sees who, what for, their words — escaped",
               "Алишер &lt;К&gt;" in caption and "бугунги ҳисоботи" in caption and "омбор расми" in caption)
    for text in (report_tools.ask_purpose_text("video"), report_tools.sent_text("report", True), report_tools.sent_text("director", True),
                 report_tools.cancelled_text(), report_tools.no_report_today_text(), report_tools.edit_prompt_text(),
                 report_tools.edited_text(), report_tools.delete_question_text(), report_tools.deleted_text()):
        check(f"friendly: {text[:28]}…", friendly_problems(text), [])
    card, kb = report_tools.report_card({"id": "r1", "status": "submitted", "content": "омборни санадим", "media_count": 2})
    check_true("/hisobot shows the report, its files, ✏️ and 🗑",
               "омборни санадим" in card and "файллар: 2" in card and [b["callback_data"] for b in kb["inline_keyboard"][0]] == ["rpe:r1", "rpd:r1"])
    check("nothing to change before it's sent", report_tools.report_card({"id": "r1", "status": "asked"})[1], None)

    # ---- the bot, with a fake database and Telegram
    worker = {"id": "e7", "telegram_user_id": 7, "role": "b2b_sotuv", "status": "active", "full_name": "Алишер Каримов",
              "display_name": "a"}
    director = {"telegram_user_id": 1}
    replies, edits, copies = [], [], []
    state = {"report": {"id": "r1", "status": "submitted", "content": "эски", "media_count": 0}, "file": None,
             "editing": None, "attached": None}

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def _call(self, method, payload, **kwargs):
            copies.append((method, payload))
            return {"message_id": 99}

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append((text, reply_markup))

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((text, reply_markup))
        return [1]

    async def create_file(user, message_id, kind, caption, file=None):
        state["file"] = {"id": "f1", "message_id": message_id, "caption": caption, "resolved": False, "kind": kind,
                         "file_id": (file or {}).get("file_id"), "file_size": (file or {}).get("size"),
                         "mime": (file or {}).get("mime")}
        return state["file"]

    async def resolve_file(file_id, user, purpose):
        f = state["file"]
        if not f or f["resolved"] or user != 7:
            return None
        f["resolved"] = True
        return f

    async def report_for_day(user, day):
        return state["report"]

    async def attach(user, day, caption, placeholder=None):
        state["attached"] = placeholder
        return state["report"]

    async def report_line(report_id, placeholder, line):
        state["line"] = (placeholder, line)

    async def file_as_text(held, run_id):
        return "бугун 3 та мижоз билан учрашдим"

    async def directors(role):
        return [director]

    async def get_by_tg(user):
        return worker

    async def start_edit(report_id, user, day):
        state["editing"] = report_id
        return state["report"]

    async def being_edited(user, day, minutes):
        return state["report"] if state["editing"] else None

    async def replace(report_id, content):
        state["report"] = {**state["report"], "content": content}
        state["editing"] = None
        return state["report"]

    async def delete(report_id, user, day):
        state["report"] = {**state["report"], "status": "asked", "content": None}
        return state["report"]

    async def none(*args, **kwargs):
        return None

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    for name, fake in (("create_employee_file", create_file), ("resolve_employee_file", resolve_file),
                       ("report_for_day", report_for_day), ("attach_media_to_report", attach),
                       ("active_employees_by_role", directors), ("get_employee_by_telegram_id", get_by_tg),
                       ("set_employee_file_sent", none), ("start_report_edit", start_edit),
                       ("report_being_edited", being_edited), ("replace_report", replace), ("delete_report", delete),
                       ("link_file_to_report", none), ("set_file_text", none), ("add_report_line", report_line)):
        patch(store, name, fake)
    patch(ops_manager, "_file_text", file_as_text)
    patch(ops_manager, "log_action", none)
    patch(ops_manager, "TelegramBot", FakeBot)
    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "_answer", none)

    def tap(data):
        return asyncio.run(ops_manager._handle_callback({"id": "q", "data": data, "from": {"id": 7},
                                                          "message": {"message_id": 5, "chat": {"id": 7}}}, uuid.uuid4()))

    try:
        photo = {"message_id": 42, "photo": [{"file_id": "x"}], "caption": "омбор"}
        check("a photo: asked what it's for", asyncio.run(ops_manager._handle_employee_message(worker, photo, uuid.uuid4())),
              "file_purpose_asked")
        check_true("...never sent to the AI or anyone yet", not copies and replies[-1][0].startswith("бу расм нима учун?"))
        check("'директорга юбориш' forwards the photo itself", tap("fp:f1:d"), "file_director")
        check_true("...to the Director, with who sent it",
                   copies[-1][0] == "copyMessage" and copies[-1][1]["chat_id"] == "1" and copies[-1][1]["message_id"] == 42
                   and "Алишер Каримов" in copies[-1][1]["caption"] and "сизга юборди" in copies[-1][1]["caption"])
        check("a second tap does nothing", tap("fp:f1:d"), "file_already_resolved")

        copies.clear()
        asyncio.run(ops_manager._handle_employee_message(worker, {**photo, "message_id": 43}, uuid.uuid4()))
        check("'бугунги ҳисобот' adds it to today's report", tap("fp:f1:r"), "file_report")
        check_true("...kept with the report («[расм] омбор»), not pushed to the Director, never read by the AI (22.10)",
                   state["attached"] == "[расм] омбор" and copies == [] and state.get("line") is None)
        voice = {"message_id": 45, "voice": {"file_id": "v1", "mime_type": "audio/ogg", "file_size": 30000}}
        asyncio.run(ops_manager._handle_employee_message(worker, voice, uuid.uuid4()))
        replies.clear()
        check("a voice message as the report", tap("fp:f1:r"), "file_report")
        check("...becomes the report's text in place of «[овозли хабар]»", state.get("line"),
              ("[овозли хабар]", "[овозли хабар] бугун 3 та мижоз билан учрашдим"))
        check_true("...and the person is shown it, to fix with /hisobot",
                   any("бугун 3 та мижоз" in r[0] and "/hisobot" in r[0] for r in replies))
        copies.clear()
        asyncio.run(ops_manager._handle_employee_message(worker, {**photo, "message_id": 44}, uuid.uuid4()))
        check("'бекор' sends nothing", (tap("fp:f1:x"), copies), ("file_cancelled", []))

        check("/hisobot shows today's report", asyncio.run(ops_manager._handle_employee_message(worker, {"text": "/hisobot"}, uuid.uuid4())),
              "report_shown")
        check("✏️ starts the change", tap("rpe:r1"), "report_edit_started")
        check("the next message is the new text",
              (asyncio.run(ops_manager._handle_employee_message(worker, {"text": "янги матн"}, uuid.uuid4())), state["report"]["content"]),
              ("report_edited", "янги матн"))
        check("🗑 asks first", tap("rpd:r1"), "report_delete_asked")
        check("'ҳа, ўчириш' deletes it — it's owed again", (tap("rpdy:r1"), state["report"]["status"]), ("report_deleted", "asked"))
        check("a report that isn't today's own can't be touched", tap("rpe:someone-elses"), "report_not_today")
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def load_source(relative: str) -> str:
    """A project file's text (for checks on wiring, not behaviour)."""
    return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")


def test_analyst() -> None:
    """2026-10-07: the Director's question is looked up across the systems by the AI itself — read-only."""
    print("Director's analyst (read-only tools)")
    import asyncio
    import contextlib
    import inspect
    import io
    import json
    import uuid
    from datetime import date, datetime, timezone

    import httpx
    from pydantic import SecretStr

    from integrations.common import db
    from integrations.common.config import settings
    from integrations.onec import client as oc
    from integrations.org_bot import analyst, ops_manager, prompt
    from integrations.sap import push_handler

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        saved.clear()

    # ---- the tools: well-formed, every one handled, nothing that writes
    names = [t["function"]["name"] for t in analyst.TOOLS]
    check("tool names are unique and each has a handler", (len(names) == len(set(names)), set(names)),
          (True, set(analyst.HANDLERS)))
    check_true("every SAP kind is one the gateway pushes", set(analyst.SAP_KINDS) <= set(push_handler.FULL_DATASETS))
    fetchers = inspect.getsource(ops_manager._fetch_agent_data)
    check_true("every company source has a reader", all(f'"{s}"' in fetchers for s in analyst.COMPANY_SOURCES))
    source = inspect.getsource(analyst)
    check_true("the analyst can't write: no execute, no INSERT/UPDATE/DELETE, no raw HTTP",
               not any(w in source for w in ("execute(", "INSERT ", "UPDATE ", "DELETE ", "httpx.", ".post(", ".put(", ".patch(")))
    check_true("database reads are READ ONLY with a timeout",
               "SET TRANSACTION READ ONLY" in inspect.getsource(db.fetch_read_only)
               and "statement_timeout" in inspect.getsource(db.fetch_read_only))
    check_true("the 1C client still has no way to write",
               not any(hasattr(oc.OneCClient, m) for m in ("post", "patch", "put", "delete")))
    check_true("the prompt: never ask which system, look in both, read-only, Uzbek Cyrillic",
               "NEVER ask" in analyst.ANALYST_SYSTEM_PROMPT and "onec_balances '40'" in analyst.ANALYST_SYSTEM_PROMPT
               and "Only READ" in analyst.ANALYST_SYSTEM_PROMPT and "Cyrillic" in analyst.ANALYST_SYSTEM_PROMPT)
    check_true("the router no longer answers a question with 'which system?'",
               'NEVER answer a question with "which system?"' in prompt.CLASSIFY_SYSTEM_PROMPT)

    # ---- the AI client speaks OpenAI-style tools
    from integrations.ai import openrouter_client as orc

    bodies: list[dict] = []

    def ai_handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["messages"][-1]["content"] == "empty":
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "data_sources", "arguments": "{}"}}]}}]})

    @contextlib.asynccontextmanager
    async def ai_no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    async def ai_turn(text):
        client = orc.OpenRouterClient(model_override="m1", fallback_override="")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(ai_handler))
        try:
            return await client.chat([{"role": "user", "content": text}], analyst.TOOLS)
        finally:
            await client._client.aclose()

    patch(orc, "audited", ai_no_audit)
    try:
        turn = asyncio.run(ai_turn("Дебитор"))
        check_true("tools are offered and a tool call comes back, content null",
                   bodies[-1]["tool_choice"] == "auto" and len(bodies[-1]["tools"]) == len(analyst.TOOLS)
                   and turn["tool_calls"][0]["function"]["name"] == "data_sources" and turn["content"] is None)
        try:
            asyncio.run(ai_turn("empty"))
            check_true("an empty turn is an error", False)
        except orc.OpenRouterError:
            check_true("an empty turn is an error (so the next model is tried)", True)
    finally:
        restore()

    # ---- arguments are bounded
    patch(analyst, "_today", lambda: date(2026, 10, 7))
    try:
        check("future dates are cut to today", analyst.period({"date_from": "2026-10-01", "date_to": "2027-01-01"}),
              (date(2026, 10, 1), date(2026, 10, 7)))
        check("from/to swapped back", analyst.period({"date_from": "2026-10-05", "date_to": "2026-10-01"}),
              (date(2026, 10, 1), date(2026, 10, 5)))
        start, end = analyst.period({"date_from": "2025-01-01", "date_to": "2026-10-07"})
        check("a period is at most 92 days", (end - start).days + 1, 92)
        check("no dates: the last 30 days", analyst.period({}), (date(2026, 9, 8), date(2026, 10, 7)))
    finally:
        restore()
    check("account codes are digits only", [analyst._digits(x) for x in ("4010", "40'; drop", "", "1234567")],
          ["4010", None, None, None])
    check("unknown tool", asyncio.run(analyst.run_tool("delete_invoice", {})), "No such tool 'delete_invoice'.")
    check_true("unknown SAP kind is refused", "Unknown kind" in asyncio.run(analyst.run_tool("sap_records", {"kind": "users"})))

    async def broken(args):
        raise RuntimeError("down")

    patch(analyst, "HANDLERS", {**analyst.HANDLERS, "billz_sales": broken})
    try:
        check_true("a system that's down becomes 'unavailable', not a crash",
                   "could not be read right now" in asyncio.run(analyst.run_tool("billz_sales", "{not json")))
    finally:
        restore()

    # ---- SAP rows: date filter, cancelled left out of the sums, grouped
    async def freshness():
        return "SAP data is current.", False

    async def fake_read(query, params=None, timeout="10s"):
        return [{"captured_at": datetime(2026, 10, 7, 3, tzinfo=timezone.utc), "raw": r} for r in (
            {"DocNum": 1, "DocDate": "2026-10-01", "CardName": "Hilton", "DocTotal": 100, "DocTotalSy": 1270000},
            {"DocNum": 2, "DocDate": "2026-10-02", "CardName": "Hilton", "DocTotal": 50, "CANCELED": "Y"},
            {"DocNum": 3, "DocDate": "2026-10-03", "CardName": "Hyatt", "DocTotal": 30},
            {"DocNum": 4, "DocDate": "2026-09-01", "CardName": "Hyatt", "DocTotal": 999})]

    patch(ops_manager, "sap_freshness", freshness)
    patch(analyst, "fetch_read_only", fake_read)
    patch(analyst, "_today", lambda: date(2026, 10, 7))
    try:
        text = asyncio.run(analyst.sap_records({"kind": "sales", "date_from": "2026-10-01", "date_to": "2026-10-07"}))
        check_true("SAP sales: dated rows only, cancelled out of the sums",
                   "3 rows (1 cancelled, left out of sums)" in text and "DocTotal: 130" in text
                   and "DocTotalSy UZS: 1,270,000" in text and "DocNum=4" not in text)
        check_true("...grouped by customer", "- Hilton: 1 rows, 100" in text and "- Hyatt: 1 rows, 30" in text)
    finally:
        restore()

    # ---- 1C balances: by account and counterparty, names from the catalog
    def handler(request):
        path = request.url.path
        if "ChartOfAccounts" in path:
            return httpx.Response(200, json={"value": [
                {"Ref_Key": "a40", "Code": "4010", "Description": "Счета к получению от покупателей"},
                {"Ref_Key": "a50", "Code": "5010", "Description": "Касса"}]})
        if "Balance" in path:
            return httpx.Response(200, json={"value": [
                {"Account_Key": "a40", "ExtDimension1": "c1", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
                 "СуммаBalance": 2000000, "СуммаBalanceDr": 2000000},
                {"Account_Key": "a40", "ExtDimension1": "c2", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
                 "СуммаBalance": 500000, "СуммаBalanceDr": 500000},
                {"Account_Key": "a50", "СуммаBalance": 9}]})
        if "Catalog_" in path:
            return httpx.Response(200, json={"value": [{"Ref_Key": "c1", "Description": "Hilton Tashkent"},
                                                       {"Ref_Key": "c2", "Description": "Hyatt Regency"}]})
        return httpx.Response(404, json={})

    real_init = oc.OneCClient.__init__

    def mocked_init(self, agent="-", run_id=None, transport=None):
        real_init(self, agent, run_id, transport=httpx.MockTransport(handler))

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    patch(oc, "audited", no_audit)
    patch(oc.OneCClient, "__init__", mocked_init)
    for name, value in (("onec_odata_url", "https://clobus.uz/a/acc313/61458/odata/standard.odata/"),
                        ("onec_login", "bot"), ("onec_password", SecretStr("x"))):
        patch(settings, name, value)
    try:
        text = asyncio.run(analyst.onec_balances({"account_prefix": "40"}))
        check_true("1C receivables: the account and each counterparty by name",
                   "Account 4010" in text and "СуммаBalance 2,500,000" in text
                   and "4010 Hilton Tashkent: СуммаBalance 2,000,000" in text and "Hyatt Regency" in text)
        check_true("...only the accounts asked for", "Касса" not in text)
    finally:
        restore()

    # ---- the loop: tools first, then the answer
    calls: list[tuple[str, str]] = []
    sent_messages: list[list[dict]] = []

    class FakeAI:
        def __init__(self, *args, **kwargs):
            self.turn = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def chat(self, messages, tools=None):
            sent_messages.append(list(messages))
            self.turn += 1
            if self.turn == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "sap_receivables", "arguments": "{}"}},
                    {"id": "t2", "type": "function", "function": {"name": "onec_balances", "arguments": '{"account_prefix": "40"}'}}]}
            return {"role": "assistant", "content": "SAP бўйича қарз <b>$100</b>, 1C бўйича 2 500 000 сўм."}

    async def fake_tool(name):
        async def run(args):
            calls.append((name, json.dumps(args)))
            return f"{name} data"
        return run

    async def make():
        return {n: await fake_tool(n) for n in analyst.HANDLERS}

    patch(analyst, "OpenRouterClient", FakeAI)
    patch(analyst, "HANDLERS", asyncio.run(make()))
    try:
        result = asyncio.run(analyst.answer("Дебитор", "", uuid.uuid4(), hint="finance_agent"))
        check("both systems looked up, then answered", (result.tools, result.rounds),
              (["sap_receivables", "onec_balances"], 2))
        check_true("the answer comes back", "1C бўйича" in result.text)
        tool_turns = [m for m in sent_messages[-1] if m.get("role") == "tool"]
        check("each tool result goes back under its call id", [(m["tool_call_id"], m["content"]) for m in tool_turns],
              [("t1", "sap_receivables data"), ("t2", "onec_balances data")])
        check_true("the router's guess is only a hint", "finance_agent" in sent_messages[0][1]["content"])
    finally:
        restore()

    class EndlessAI(FakeAI):
        async def chat(self, messages, tools=None):
            if tools is None:
                return {"role": "assistant", "content": "етарли"}
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "data_sources", "arguments": "{}"}}]}

    patch(analyst, "OpenRouterClient", EndlessAI)
    patch(analyst, "HANDLERS", asyncio.run(make()))
    patch(settings, "ops_analyst_max_rounds", 3)
    try:
        result = asyncio.run(analyst.answer("ҳаммаси", "", uuid.uuid4()))
        check("rounds are capped, then it must answer", (result.rounds, result.text), (3, "етарли"))
    finally:
        restore()

    # ---- the bot: analyst first, the old one-source answer if it fails
    replies: list[str] = []
    old_path: list[str] = []

    async def reply(director_id, run_id, text, reply_to=None):
        replies.append(text)

    async def from_agent(director_id, slug, question, run_id, history="", reply_to=None):
        old_path.append(slug)

    async def good(question, history, run_id, hint=None):
        return analyst.Answer(text="1C бўйича ...", tools=["onec_balances"], rounds=2)

    async def bad(question, history, run_id, hint=None):
        raise RuntimeError("no tools on this model")

    async def no_log(**kwargs):
        return None

    patch(ops_manager, "_reply_and_log", reply)
    patch(ops_manager, "_answer_from_agent", from_agent)
    patch(ops_manager, "log_action", no_log)
    patch(analyst, "answer", good)
    patch(settings, "ops_analyst_enabled", True)
    try:
        asyncio.run(ops_manager._answer_question(1, "finance_agent", "Дебитор", uuid.uuid4()))
        check("the analyst answers the Director", (replies, old_path), (["1C бўйича ..."], []))
        analyst.answer = bad
        asyncio.run(ops_manager._answer_question(1, "finance_agent", "Дебитор", uuid.uuid4()))
        check("analyst down: the old answer still comes", old_path, ["finance_agent"])
        settings.ops_analyst_enabled = False
        analyst.answer = good
        asyncio.run(ops_manager._answer_question(1, "pul_qoldigi", "касса", uuid.uuid4()))
        check("switched off: the old answer", old_path, ["finance_agent", "pul_qoldigi"])
    finally:
        restore()
    # ---- 2026-10-10: supplier debt 1C ↔ SAP from the bot — a text comparison, and the .xlsx on request
    # (invented figures — no real supplier or amount in the repo, the Director's order of 07.10)
    from integrations.onec import payables

    onec_data = {
        "as_of": "2026-10-10T09:00:00",
        "chart": [{"Ref_Key": "a6010", "Code": "6010", "Description": "Поставщики"},
                  {"Ref_Key": "a4310", "Code": "4310", "Description": "Авансы"}],
        "parties": [{"Ref_Key": "p1", "Description": "Namuna MChJ", "ИНН": "300000011"},
                    {"Ref_Key": "p2", "Description": "Ikkinchi firma", "ИНН": "300000012"},
                    {"Ref_Key": "p3", "Description": "Faqat birinchi", "ИНН": ""}],
        "rows": [{"Account_Key": "a6010", "ExtDimension1": "p1", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
                  "СуммаBalanceCr": 3_000_000},
                 {"Account_Key": "a6010", "ExtDimension1": "p2", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
                  "СуммаBalanceCr": 2_000_000},
                 {"Account_Key": "a4310", "ExtDimension1": "p3", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
                  "СуммаBalanceDr": 500_000}],
    }
    sap_rows = [{"CardCode": "S1", "CardName": "Namuna", "LicTradNum": "300000011", "CardType": "S",
                 "Balance": "250", "BalanceSys": "3000000"},
                {"CardCode": "S2", "CardName": "Ikkinchi firma", "LicTradNum": "300000012", "CardType": "S",
                 "Balance": "100", "BalanceSys": "1200000"},
                {"CardCode": "S3", "CardName": "Faqat SAP", "LicTradNum": "", "CardType": "S",
                 "Balance": "50", "BalanceSys": "600000"}]
    compared = payables.compare(onec_data, sap_rows, "SAP шлюзи, 10.10.2026 08:30")
    text = payables.summary(compared)
    check_true("the summary: both totals, counts, the biggest difference with its reason",
               "TOTAL 1C" in text and "net 4 500 000 сўм" in text and "TOTAL SAP: net 4 800 000 сўм" in text
               and "agree 1, differ 1. Only in 1C: 1. Only in SAP: 1." in text
               and "Ikkinchi firma: 1C 2 000 000 сўм / SAP 1 200 000 сўм / 800 000 сўм — Сумма фарқли" in text)
    check_true("no SAP data yet: said, 1C side still given",
               "hasn't pushed" in payables.summary(payables.compare(onec_data, [], "йўқ")))
    from openpyxl import load_workbook as _load_wb

    check("the workbook builds in memory (no file on the server)",
          _load_wb(io.BytesIO(payables.workbook_bytes(compared))).sheetnames,
          ["Солиштириш", "Фақат 1C", "Фақат SAP", "1C тафсилот", "Изоҳ"])

    async def fake_onec(agent="x"):
        return onec_data

    async def fake_pushed(close=False):
        check_true("the service never closes the shared database pool", close is False)
        return sap_rows, "SAP шлюзи, 10.10.2026 08:30"

    patch(payables, "read_onec", fake_onec)
    patch(payables, "pushed_sap_rows", fake_pushed)
    for name, value in (("onec_odata_url", "https://clobus.uz/x/odata/standard.odata/"), ("onec_login", "bot"),
                        ("onec_password", SecretStr("x"))):
        patch(settings, name, value)
    try:
        files: list = []
        analyst._files.set(files)
        analyst._wants_file.set(False)
        text = asyncio.run(analyst.supplier_debt_compare({}))
        check_true("asked in words: a text comparison, no file", "agree 1, differ 1" in text and files == [])
        asyncio.run(analyst.supplier_debt_compare({"excel": True}))
        asyncio.run(analyst.supplier_debt_compare({"excel": True}))
        check_true("excel=true: the .xlsx in Uzbek and in Russian (asked twice, sent once each)",
                   [f[1][:20] for f in files] == ["kreditorlik-1C-SAP-2", "kreditorskaya-1C-SAP"]
                   and files[1][1].endswith("-RU.xlsx") and all(f[0][:2] == b"PK" for f in files))
        ru_book = _load_wb(io.BytesIO(files[1][0]))
        check_true("the Russian workbook: Russian sheets, headers and reasons, and the column explanations under each table",
                   ru_book.sheetnames == ["Сверка", "Только в 1С", "Только в SAP", "1С детализация", "Пояснения"]
                   and ru_book["Сверка"]["K1"].value == "Вероятная причина"
                   and any(c.value == "Суммы различаются: в одной системе нет документа или оплаты, либо сумма другая — нужен акт сверки"
                           for c in ru_book["Сверка"]["K"])
                   and any(c.value == "ПОЯСНЕНИЕ К СТОЛБЦАМ" for c in ru_book["Сверка"]["A"])
                   and any("«К оплате»" in str(c.value or "") for row in ru_book["Сверка"].iter_rows() for c in row))
        uz_book = _load_wb(io.BytesIO(files[0][0]))
        check_true("the Uzbek workbook explains its columns too, with SAP's own column names",
                   any(c.value == "УСТУНЛАР ИЗОҲИ" for c in uz_book["Солиштириш"]["A"])
                   and any("«Название поставщика»" in str(c.value or "") for row in uz_book["Солиштириш"].iter_rows() for c in row))
        files.clear()
        analyst._wants_file.set(True)
        asyncio.run(analyst.supplier_debt_compare({}))
        check("the Director's own word «солиштир» sends the files even if the model forgot", len(files), 2)
        files.clear()
        analyst._lang.set("ru")
        asyncio.run(analyst.supplier_debt_compare({"excel": True}))
        check("someone who reads Russian gets the Russian file first", files[0][1].endswith("-RU.xlsx"), True)
        analyst._lang.set("uz")
    finally:
        analyst._files.set(None)
        analyst._wants_file.set(False)
        restore()
    check_true("file words: солиштир / excel / файл, not a plain question",
               all(analyst.FILE_WORDS.search(q) for q in ("1C ва SAP кредиторликни солиштириб беринг",
                                                          "excel qilib bering", "файлини юборинг"))
               and not analyst.FILE_WORDS.search("кредиторлик қанча?"))
    check_true("supplier debt: the prompt sends the analyst to the comparison tool",
               "supplier_debt_compare" in analyst.ANALYST_SYSTEM_PROMPT and "sap_records ap_open" not in analyst.ANALYST_SYSTEM_PROMPT)

    uploads: list[tuple[str, str]] = []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_file(self, content, filename, chat_id, caption=None, photo=False, reply_to=None):
            uploads.append((filename, chat_id))

        async def send_message(self, text, chat_id=None, reply_markup=None):
            uploads.append(("text", text))

    async def with_file(question, history, run_id, hint=None):
        return analyst.Answer(text="1C ва SAP ...", tools=["supplier_debt_compare"], rounds=2,
                              files=[(b"PK..", "kreditorlik-1C-SAP-2026-10-10.xlsx")])

    replies.clear()
    patch(ops_manager, "_reply_and_log", reply)
    patch(ops_manager, "log_action", no_log)
    patch(ops_manager, "TelegramBot", FakeBot)
    patch(analyst, "answer", with_file)
    patch(settings, "ops_analyst_enabled", True)
    try:
        asyncio.run(ops_manager._answer_question(7, "finance_agent", "солиштириб беринг", uuid.uuid4()))
        check("the text first, then the workbook to the Director", (replies, uploads),
              (["1C ва SAP ..."], [("kreditorlik-1C-SAP-2026-10-10.xlsx", "7")]))
    finally:
        restore()
    check_true("the Director's words and the figures aren't logged",
               "data[:200]" not in inspect.getsource(ops_manager._answer_from_agent)
               and "answer[:300]" not in inspect.getsource(ops_manager._answer_from_agent)
               and "raw_message[:120]" not in inspect.getsource(ops_manager._dispatch_director_task))


def test_it_restrictions() -> None:
    """The Director's order of 07.10.2026: IT keeps technical access only — no figures reach Admin Bot."""
    print("IT restrictions (Director's order 07.10.2026)")
    from datetime import date, datetime, timezone

    from integrations.billz import sap_check as sc
    from integrations.common.agent_loader import load_agent
    from integrations.common.timeutil import TASHKENT
    from integrations.org_bot import tech_report

    # Billz → SAP while on trial: counts only
    cheque = sc.Cheque(key="k", number="77", day=date(2026, 10, 6), amount=12500000, shop="GARMIN ABAY",
                       seller="Алишер", items=[])
    result = sc.Result(day=date(2026, 10, 6), cheques_day=[cheque], missing=[cheque])
    text = sc.technical_text(result)
    check_true("IT's Billz → SAP copy: counts, no amount, cheque, shop or seller",
               "Billz'да 1 та чек" in text and "киритилмаган: 1" in text
               and not any(x in text for x in ("12", "77", "ABAY", "Алишер", "сўм")))
    check_true("all matching says so", "Ҳаммаси мос" in sc.technical_text(sc.Result(day=date(2026, 10, 6))))
    agent = load_agent("billz-sap-check")
    check_true("the trial copy says who confirms it", "Директор" in agent.trial_text("x"))

    # data quality: invoice numbers, never their amounts
    dq = load_agent("data-quality")
    inp = dq.Inputs(today=date(2026, 10, 7))
    inp.invoices = [{"doc_num": 2253, "doc_date": date(2026, 9, 1), "due_date": date(2026, 9, 10), "currency": "USD",
                     "doc_total_tiyin": 123456700, "balance_due_tiyin": 123456700, "sales_person_code": -1}]
    report = dq.render(inp)
    check_true("data quality names the invoice, not its amount", "#2253" in report and "1,234,567" not in report
               and "$" not in report)

    # the technical report: times, counts, ok/failed
    r = tech_report.TechReport(at=datetime(2026, 10, 7, 8, 20, tzinfo=TASHKENT))
    r.connections = [tech_report.Check("1C", True, "41 та объект очиқ"), tech_report.Check("Billz", False, "HTTP 401"),
                     tech_report.Check("Verifix", None, "уланмаган")]
    r.sap_last, r.sap_kinds, r.sap_missing = datetime(2026, 10, 7, 2, 13, tzinfo=timezone.utc), 10, {"ar_open": 3}
    r.brief = "юборилди 07.10 08:03"
    r.runs = [("ceo-daily-brief", 4, 0, datetime(2026, 10, 7, 3, 3, tzinfo=timezone.utc)),
              ("lead-agent", 2, 1, datetime(2026, 10, 7, 3, 9, tzinfo=timezone.utc))]
    r.errors, r.db_size_mb, r.refused, r.analyst = [("lead-agent", "SerpAPI HTTP 429", 1)], 84.2, 2, (5, 1)
    text = tech_report.render(r)
    check_true("technical report: SAP push time, connections, brief delivered, errors, DB, security",
               "охирги юбориш 07.10 07:13" in text and "очиқ ҳисоб-фактуралар (3)" in text and "✅ 1C" in text and "❌ Billz: HTTP 401" in text
               and "Эрталабки брифинг: юборилди" in text and "SerpAPI HTTP 429 ×1" in text and "84.2 MB" in text
               and "Рад этилган уринишлар" in text and "2 та" in text and "AI жавоб берди 5 та" in text)
    r.sap_last = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    check_true("a silent SAP push is flagged", "соатдан бери келмаяпти" in tech_report.render(r))
    # 2026-10-10 (IT-5): the copy outside Render
    check_true("backups not set up: said, with where to read how", "Офисдаги нусха: созланмаган" in text)
    r.backup_set_up, r.backup_last, r.backup_mb = True, datetime(2026, 10, 7, 1, 5, tzinfo=timezone.utc), 12.4
    check_true("the office's last copy: time and size", "✅ Офисдаги нусха: 07.10 06:05, 12.4 MB" in tech_report.render(r))
    r.backup_last, r.backup_failed = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc), 2
    late = tech_report.render(r)
    check_true("no copy for more than a day, or failures: flagged",
               "❌ Офисдаги нусха: охиргиси 05.10" in late and "олинмаган" in late and "2 та хато" in late)
    r.backup_last = None
    check_true("set up but never fetched: flagged", "ҳали бирорта ҳам олинмаган" in tech_report.render(r))
    check_true("the technical report is Uzbek Cyrillic",
               latin_words(text, allow={"SAP", "Billz", "Verifix", "AI", "OpenRouter", "Didox", "Admin", "Bot", "OPS",
                                        "Manager", "MB", "Render", "Recovery", "HTTP", "SerpAPI", "mgmg",
                                        "db", "C", "README"}) == [])
    check_true("/texnik sends it on demand", "/texnik" in load_source("integrations/org_bot/admin.py"))
    check_true("it runs last in the 08:00 job", load_source("scripts/run_morning_agents.py").index("tech-report")
               > load_source("scripts/run_morning_agents.py").index("task-tracker/agent.py --monthly"))


def test_tech_report_errors_fixed() -> None:
    """2026-10-08, from /texnik: a SerpAPI 429 failed the whole batch; an over-long edit was refused."""
    print("errors from the technical report (2026-10-08)")
    import asyncio
    import contextlib

    import httpx

    from integrations.search import serpapi_client as sc
    from integrations.search import tavily_client as tc
    from integrations.telegram import bot as tb

    # ---- a 429 that never clears is a typed error: the engine is skipped, the others go on
    request = httpx.Request("GET", sc.BASE_URL)

    async def always_429(client, method, url, **kwargs):
        raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    saved = [(sc, "request_with_retry", sc.request_with_retry), (sc, "audited", sc.audited),
             (tc, "request_with_retry", tc.request_with_retry), (tc, "audited", tc.audited)]
    sc.request_with_retry, sc.audited, tc.request_with_retry, tc.audited = always_429, no_audit, always_429, no_audit

    async def serp():
        client = sc.SerpAPIClient(agent="t")
        client._client = httpx.AsyncClient()
        try:
            try:
                await client.search("hotel", "google", 10)
                raised = None
            except Exception as exc:  # noqa: BLE001
                raised = exc
            return raised, await client.search_all_engines("hotel")
        finally:
            await client._client.aclose()

    async def tav():
        client = tc.TavilyClient(agent="t")
        client._client = httpx.AsyncClient()
        try:
            await client.search_news("hotel", days=3, max_results=5)
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            await client._client.aclose()

    try:
        raised, leads = asyncio.run(serp())
        check_true("SerpAPI 429 after retries is a SerpAPIError", isinstance(raised, sc.SerpAPIError))
        check("...so every engine is tried and the call returns, not raises", leads, [])
        check_true("Tavily the same", isinstance(asyncio.run(tav()), tc.TavilyError))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    source = load_source("agents/lead-agent/agent.py")
    check_true("the Lead Agent: one failing query never fails the others",
               source.count("except Exception as err:  # noqa: BLE001 — one query must never fail the others") == 2)

    # ---- an over-long edit is shortened, not refused
    short = "<b>Ҳисобот</b> &lt;1&gt;"
    check("a short edit is left as it is", tb.fit_for_edit(short), short)
    long = "<b>Бугунги ҳисоботингиз</b>\n\n" + ("ишладим &amp; ёздим " * 600)
    cut = tb.fit_for_edit(long)
    check_true("a long one fits Telegram's limit, without broken tags",
               len(html_unescape(cut)) <= 4096 and "<b>" not in cut and cut.endswith("…") and "&amp;" in cut)
    sent: list[dict] = []

    async def fake_call(method, payload, **kwargs):
        sent.append(payload)

    async def edit():
        bot = tb.TelegramBot(agent="t", bot_token="x")
        bot._call = fake_call
        await bot._edit_message("7", 801, long)

    asyncio.run(edit())
    check_true("_edit_message sends the shortened text", sent and sent[-1]["text"] == cut)


def html_unescape(text: str) -> str:
    import html

    return html.unescape(text)


def test_review_pages() -> None:
    """2026-10-08: Google review pages /r and /r/garmin — separate from the complaint pages /f."""
    print("Google review pages (/r)")
    import asyncio
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from integrations.api import app as api_app
    from integrations.api import review_page as rp
    from integrations.common.config import settings
    from integrations.org_bot import admin, qr_card

    # ---- only Google's own https links are ever used
    links = rp.review_urls("yunusobod=https://g.page/r/ABC/review; vuzgorodok=http://g.page/r/X;"
                           "abay=https://evil.example/review;minor=https://search.google.com/local/writereview?placeid=P;"
                           "nowhere=https://g.page/r/Z")
    check("valid Google https links kept, the rest dropped", links,
          {"yunusobod": "https://g.page/r/ABC/review",
           "minor": "https://search.google.com/local/writereview?placeid=P"})
    check("a look-alike host is refused", rp.is_google_link("https://g.page.evil.com/r/x"), False)
    check("a login in the link is refused", rp.is_google_link("https://user@g.page/r/x"), False)

    # ---- the page: honest, every branch, Uzbek Cyrillic
    page = rp.page_html("laundry", "uz_cyrl", None, links)
    check_true("Londry's page lists both branches, one live, one 'soon'",
               "/r/go/yunusobod?lang=uz_cyrl" in page and "Londry Вузгородок" in page and "тез орада" in page
               and "/r/go/vuzgorodok" not in page)
    check_true("it says every rating opens the same Google page", "бир хил Google саҳифасини очади" in page)
    check_true("no rating is asked first (no review gating), no script",
               "<script" not in page and "<form" not in page)
    check("Uzbek Cyrillic, no Latin besides names", latin_words(page_text(page), allow={
        "Londry", "Google", "Русский", "English"}), [])
    one = rp.page_html("garmin", "ru", "abay", links)
    check_true("a branch's own card shows only that branch, with a link to the others",
               "Абай" in one and "Минор" not in one and "Другие филиалы" in one)

    # ---- the routes: separate from /f, redirect only to the configured link
    saved = settings.google_review_urls
    settings.google_review_urls = "yunusobod=https://g.page/r/ABC/review"
    original_log = rp.log_action
    clicks = []

    async def fake_log(**kwargs):
        clicks.append(kwargs)

    rp.log_action = fake_log
    client = TestClient(api_app.app)
    try:
        check("/r opens", client.get("/r").status_code, 200)
        check("/r/garmin opens", client.get("/r/garmin").status_code, 200)
        go = client.get("/r/go/yunusobod?url=https://evil.example", follow_redirects=False)
        check("a tap goes to that branch's Google page — never a URL from the address bar",
              (go.status_code, go.headers.get("location")), (303, "https://g.page/r/ABC/review"))
        check_true("...counted with the branch only", clicks and clicks[-1]["action"] == "review_click"
                   and clicks[-1]["target_ref"] == "yunusobod")
        back = client.get("/r/go/abay", follow_redirects=False)
        check("a branch without a link goes back to its page", (back.status_code, back.headers.get("location")),
              (303, "/r/garmin"))
        check("the complaint page /f is untouched", client.get("/f").status_code, 200)
        check_true("...and still a complaint form", "<form" in client.get("/f").text)
    finally:
        settings.google_review_urls = saved
        rp.log_action = original_log

    # ---- the printed card: positive green + five stars, so it can't be taken for a complaint card
    plain = Image.open(io.BytesIO(qr_card.card_png("https://x.uz/f", "laundry")))
    starred = Image.open(io.BytesIO(qr_card.card_png("https://x.uz/r?branch=yunusobod", "laundry", stars=True)))
    check("the review card has a star row under the code", starred.height - plain.height, qr_card.STAR_ROW_H)
    band = starred.crop((0, plain.height - 40, qr_card.CARD_W, starred.height)).convert("RGB")
    whites = sum(1 for px in band.getdata() if px == qr_card.WHITE)
    check_true("...in white, on green — the complaint card stays red",
               whites > 5000 and starred.getpixel((5, 5)) == qr_card.GREEN and plain.getpixel((5, 5)) == qr_card.RED)
    check_true("the page is green with gold stars, not the complaint red",
               "--head:#1e8e3e" in page and "#f2b01e" in page and "content='#1e8e3e'" in page)

    # ---- /qr sharh before the links are set
    sent = []

    class Bot:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_message(self, text, **kwargs):
            sent.append(text)

    saved_bot, saved_url, saved_admin = admin.TelegramBot, settings.public_base_url, settings.admin_bot_admin_user_id
    admin.TelegramBot, settings.public_base_url, settings.admin_bot_admin_user_id = Bot, "https://mgmg-api-eeky.onrender.com", 0
    settings.google_review_urls = ""
    try:
        outcome = asyncio.run(admin.handle_admin_message({"from": {"id": 5}, "text": "/qr sharh"}, uuid_module().uuid4()))
        check("/qr sharh without links says what to set", (outcome, "GOOGLE_REVIEW_URLS" in sent[-1]),
              ("review_qr_no_links", True))
    finally:
        admin.TelegramBot, settings.public_base_url, settings.admin_bot_admin_user_id = saved_bot, saved_url, saved_admin
        settings.google_review_urls = saved


def page_text(page: str) -> str:
    """A page's visible text: tags, the style block and attribute values dropped."""
    page = re.sub(r"<style>.*?</style>", " ", page, flags=re.S)
    import html

    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def uuid_module():
    import uuid

    return uuid


def test_task_register() -> None:
    """The owner's spec of 11.10.2026, Day 1: one task register — code, states, proof, check, history."""
    print("task register (Day 1)")
    import asyncio
    import uuid
    from datetime import date, datetime, timezone

    from integrations.org_bot import ops_manager, store, task_register as tr, task_tracker

    root = Path(__file__).resolve().parents[1]
    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    base = {"id": tid, "code": "T-2026-10-0147", "task_summary": "Принтерни текширинг", "director_telegram_user_id": 9,
            "raw_message": "x", "has_media": False, "due_date": None, "telegram_message_id": 55}

    def buttons(task):
        return [b["text"] for row in tr.keyboard(task)["inline_keyboard"] for b in row]

    check("new: accept, blocked, done", buttons({**base, "status": "sent"}), ["▶️ Қабул қилдим", "⛔ Тўсиқ бор", "✅ Бажардим"])
    check("accepted: blocked, done", buttons({**base, "status": "started"}), ["⛔ Тўсиқ бор", "✅ Бажардим"])
    check("blocked: continue, done", buttons({**base, "status": "blocked"}), ["▶️ Давом эттирдим", "✅ Бажардим"])
    check("waiting for proof: only cancel", buttons({**base, "status": "started", "awaiting": "evidence"}), ["↩️ Бекор қилиш"])
    check("in review or closed: no buttons for the employee",
          (buttons({**base, "status": "review"}), buttons({**base, "status": "done"})), ([], []))
    every = [b["callback_data"] for st in ("sent", "started", "blocked") for row in tr.keyboard({**base, "status": st})["inline_keyboard"]
             for b in row] + [b["callback_data"] for b in tr.review_keyboard(tid)["inline_keyboard"][0]]
    check_true("every button fits Telegram's 64 bytes", all(len(d.encode()) <= 64 for d in every))
    check_true("waiting for proof: the card says no proof, no closing",
               "Далилсиз топшириқ ёпилмайди" in tr.status_line({**base, "status": "started", "awaiting": "evidence"}))
    check("blocked shows its reason", tr.status_line({**base, "status": "blocked", "blocker": "қоғоз йўқ"}), "⛔ Тўсиқ бор: қоғоз йўқ")
    check("proof: text", tr.evidence_of({"text": " тузатилди "}), ("тузатилди", False))
    check("proof: a photo without text", tr.evidence_of({"photo": [{}]}), ("фото", True))
    check("proof: nothing", tr.evidence_of({"text": "  "}), None)
    check_true("the review card: code, what, who, the proof",
               all(x in tr.review_text({**base, "evidence_text": "фото"}, "Алишер") for x in ("T-2026-10-0147", "Принтерни", "Алишер", "Далил:")))
    check_true("a task with proof counts as finished on time (the check comes later)",
               task_tracker.completed_on_time({"status": "review", "due_date": date(2026, 10, 13),
                                                "completed_at": datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)}) is True)

    schema = (root / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("the database: the six states, a code for every task (old ones too), the history table",
               "CHECK (status IN ('sent', 'started', 'blocked', 'review', 'done', 'cancelled'))" in schema
               and "CREATE SEQUENCE IF NOT EXISTS task_code_seq" in schema and "WHERE code IS NULL" in schema
               and "CREATE TABLE IF NOT EXISTS task_events" in schema and "uq_tasks_code" in schema)
    src = load_source("integrations/org_bot/store.py")
    check_true("closing needs proof, and only the one who assigned it can accept or return",
               "AND awaiting = 'evidence'" in src and "director_telegram_user_id = %s AND status = 'review'" in src
               and "nextval('task_code_seq')" in src)

    # the flow, with the database and Telegram faked
    state = {"task": {**base, "status": "started", "awaiting": None}}
    events: list = []
    sent: list = []
    replies: list = []

    async def await_input(task_id, kind, tg):
        if tg != 501:
            return None
        state["task"] = {**state["task"], "awaiting": kind}
        return state["task"]

    async def awaiting(tg, minutes=30):
        return state["task"] if tg == 501 and state["task"].get("awaiting") else None

    async def submit(task_id, by, text, msg_id):
        if state["task"].get("awaiting") != "evidence":
            return None
        state["task"] = {**state["task"], "status": "review", "awaiting": None, "evidence_text": text}
        events.append(("submitted", text, msg_id))
        return state["task"]

    async def block(task_id, by, reason):
        state["task"] = {**state["task"], "status": "blocked", "awaiting": None, "blocker": reason}
        events.append(("blocked", reason))
        return state["task"]

    async def verify(task_id, clicker, accept, by):
        if clicker != 9 or state["task"]["status"] != "review":
            return None
        state["task"] = {**state["task"], "status": "done" if accept else "started"}
        events.append(("closed" if accept else "returned", by))
        return state["task"]

    async def with_employee(task_id):
        return {**state["task"], "employee_telegram_user_id": 501, "display_name": "Алишер Каримов"}

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((chat_id, text))
        return [1]

    async def nothing(*args, **kwargs):
        return None

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, reply_markup=None):
            sent.append(("text", chat_id, text, reply_markup))
            return [77]

        async def _call(self, method, payload, **kwargs):
            sent.append((method, payload["chat_id"], payload.get("caption"), payload.get("reply_markup")))
            return {"message_id": 78}

        async def _edit_message(self, **kwargs):
            sent.append(("edit", kwargs.get("chat_id"), kwargs.get("text"), kwargs.get("reply_markup")))

        async def _answer_callback(self, *args):
            return None

    saved = [(store, n, getattr(store, n)) for n in ("await_task_input", "task_awaiting_input", "submit_task", "block_task",
                                                       "verify_task", "task_with_employee", "set_task_review_message",
                                                       "report_for_day")]
    saved += [(ops_manager, n, getattr(ops_manager, n)) for n in ("_reply", "TelegramBot", "_answer")]
    store.await_task_input, store.task_awaiting_input, store.submit_task = await_input, awaiting, submit
    store.block_task, store.verify_task, store.task_with_employee = block, verify, with_employee
    store.set_task_review_message = store.report_for_day = nothing  # /hisobot: no report today, no database
    ops_manager._reply, ops_manager.TelegramBot, ops_manager._answer = fake_reply, FakeBot, nothing
    employee = {"telegram_user_id": 501, "full_name": "Алишер Каримов", "display_name": "A", "role": "it"}
    try:
        tap = {"id": "q", "data": f"taskdone:{tid}", "from": {"id": 501}, "message": {"message_id": 55, "chat": {"id": 501}}}
        check("✅ Бажардим doesn't close it — it asks for proof", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())),
              "evidence_asked")
        check_true("...the card now asks for proof, with only a cancel button",
                   sent and "далил" in sent[-1][2] and "taskx:" in str(sent[-1][3]))
        out = asyncio.run(ops_manager._handle_employee_message(employee, {"message_id": 60, "text": "картриж алмаштирилди"}, uuid.uuid4()))
        check("the next message is the proof: the task goes to review", (out, state["task"]["status"]), ("task_submitted", "review"))
        review = [x for x in sent if x[0] == "text" and x[1] == "9"]
        check_true("the one who assigned it gets the proof with ✅ Қабул / ↩️ Қайтариш",
                   review and "картриж алмаштирилди" in review[-1][2] and "taskok:" in str(review[-1][3])
                   and "taskback:" in str(review[-1][3]))
        check_true("the employee is told it went for review", any("текширувга юборилди" in t for c, t in replies if c == 501))
        stranger = {"id": "q", "data": f"taskok:{tid}", "from": {"id": 777}, "message": {"message_id": 80, "chat": {"id": 777}}}
        check("nobody else can accept it", asyncio.run(ops_manager._handle_callback(stranger, uuid.uuid4())), "verify_closed")
        back = {**stranger, "data": f"taskback:{tid}", "from": {"id": 9}, "message": {"message_id": 80, "chat": {"id": 9}, "text": "x"}}
        check("↩️ Қайтариш sends it back to the employee", (asyncio.run(ops_manager._handle_callback(back, uuid.uuid4())),
                                                             state["task"]["status"]), ("task_returned", "started"))
        check_true("...who is told to do it again with new proof", "янги далил" in replies[-1][1])
        asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4()))
        sent.clear()
        asyncio.run(ops_manager._handle_employee_message(employee, {"message_id": 61, "photo": [{"file_id": "f"}], "caption": ""},
                                                         uuid.uuid4()))
        check_true("a photo as proof is copied to the one who assigned it, with the buttons",
                   any(x[0] == "copyMessage" and x[1] == "9" and "taskok:" in str(x[3]) for x in sent)
                   and events[-1] == ("submitted", "фото", 61))
        ok = {**back, "data": f"taskok:{tid}", "message": {"message_id": 81, "chat": {"id": 9}, "caption": "x"}}
        check("✅ Қабул closes it", (asyncio.run(ops_manager._handle_callback(ok, uuid.uuid4())), state["task"]["status"]),
              ("task_closed", "done"))
        state["task"] = {**base, "status": "started", "awaiting": "blocker"}
        replies.clear()
        sent.clear()
        out = asyncio.run(ops_manager._handle_employee_message(employee, {"message_id": 62, "text": "қоғоз тугаган"}, uuid.uuid4()))
        check_true("⛔ then the reason: blocked, and the one who assigned it gets the reason",
                   out == "task_blocked" and state["task"]["blocker"] == "қоғоз тугаган"
                   and any(c == 9 and "Сабаби:" in t for c, t in replies))
        state["task"] = {**base, "status": "started", "awaiting": "evidence"}
        check("a command is not taken as proof", asyncio.run(ops_manager._handle_employee_message(
            employee, {"message_id": 63, "text": "/hisobot"}, uuid.uuid4())) != "task_submitted", True)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_review_fixes() -> None:
    """The code review of 2026-10-14: stuck reviews, stale proof, lost toasts, Garmin access, small gaps."""
    print("code-review fixes (2026-10-14)")
    import asyncio
    import uuid

    from integrations.org_bot import admin, analyst, data_access, feedback, ops_manager, store, task_register as tr
    from integrations.telegram import bot as tg

    # 1 / 6: captions within 1024, never cut inside a tag or an entity
    long = {"code": "T-2026-10-0001", "evidence_text": "<b>" + "а&" * 900}
    cap = tr.review_caption(long, "Алишер", "✅ Қабул қилинди")
    check_true("a file's review caption fits 1024 and ends on whole entities",
               len(cap) <= 1024 and cap.endswith("…") and not cap.rstrip("…").endswith(("&", "&amp", "&a")))
    check("a short proof is kept whole", tr.review_caption({"code": "T", "evidence_text": "тайёр"}, "A").endswith("тайёр"), True)

    calls: list = []

    class FailingBot:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def _call(self, method, payload, **kwargs):
            calls.append(method)
            raise ops_manager.TelegramError("can't parse entities")

        async def send_message(self, text, chat_id=None, reply_markup=None):
            calls.append("sendMessage")
            raise ops_manager.TelegramError("chat not found")

        async def _edit_message(self, **kwargs):
            return None

    task = {"id": "t1", "code": "T-1", "task_summary": "x", "director_telegram_user_id": 9, "awaiting": "evidence",
            "status": "started", "telegram_message_id": 5}
    reopened: list = []
    replies: list = []

    async def submit(task_id, by, text, msg_id):
        return {**task, "status": "review", "evidence_text": text}

    async def reopen(task_id):
        reopened.append(task_id)
        return {**task, "status": "started", "awaiting": "evidence"}

    async def fake_reply(chat, run_id, text, reply_markup=None, reply_to=None):
        replies.append(text)
        return [1]

    async def nothing(*a, **k):
        return None

    saved = [(store, "submit_task", store.submit_task), (store, "reopen_submission", store.reopen_submission),
             (store, "task_with_employee", store.task_with_employee), (store, "set_task_review_message", store.set_task_review_message),
             (ops_manager, "TelegramBot", ops_manager.TelegramBot), (ops_manager, "_reply", ops_manager._reply)]
    store.submit_task, store.reopen_submission, store.task_with_employee = submit, reopen, nothing
    store.set_task_review_message, ops_manager.TelegramBot, ops_manager._reply = nothing, FailingBot, fake_reply
    try:
        out = asyncio.run(ops_manager._consume_task_input({"telegram_user_id": 501, "full_name": "Али"}, task,
                                                          {"message_id": 7, "photo": [{}], "caption": "тайёр"}, uuid.uuid4()))
        check_true("the review card can't be sent: tried as a file, then as text, then the task goes back for proof",
                   out == "task_review_failed" and calls == ["copyMessage", "sendMessage"] and reopened == ["t1"]
                   and "қайта юборинг" in replies[-1])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    store_src = load_source("integrations/org_bot/store.py")
    check_true("reopening: back to 'send the proof', never stuck in review",
               "awaiting = 'evidence', awaiting_since = now()" in store_src and "WHERE id = %s AND status = 'review'" in store_src)

    # 2: the proof window is short, and a Reply to another message is not proof
    consumed: list = []

    async def awaiting(tg_id, minutes=30):
        consumed.append(("asked", minutes))
        return {"id": "t1", "awaiting": "evidence", "telegram_message_id": 5}

    async def consume(employee, task_, message, run_id):
        consumed.append("consumed")
        return "task_submitted"

    saved = [(store, "task_awaiting_input", store.task_awaiting_input), (ops_manager, "_consume_task_input", ops_manager._consume_task_input)]
    store.task_awaiting_input, ops_manager._consume_task_input = awaiting, consume
    try:
        asyncio.run(ops_manager._handle_employee_message({"telegram_user_id": 501}, {"message_id": 8, "text": "тайёр"}, uuid.uuid4()))
        check("right after ✅ the next message is the proof, within 30 minutes", consumed, [("asked", 30), "consumed"])
        consumed.clear()
        try:
            asyncio.run(ops_manager._handle_employee_message(
                {"telegram_user_id": 501}, {"message_id": 9, "text": "ҳисобот", "reply_to_message": {"message_id": 77}}, uuid.uuid4()))
        except Exception:  # noqa: BLE001 — the report flow beyond this point needs the database; only the routing matters
            pass
        check("a Reply to another message (the report ask) is not taken as proof", "consumed" in consumed, False)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # 3: the early answer waits long enough, and never takes a cheer reply's pop-up
    check_true("early answer after 1 s (no button is exempt since team cheer went, 17.10)",
               tg.ACK_AFTER >= 1.0 and tg.NO_EARLY_ACK == () and "data=str(" in load_source("integrations/api/app.py"))

    # 4 / 10: the Garmin role's data waits for the Director; one notice on a role change
    admin_src = load_source("integrations/org_bot/admin.py")
    check_true("a role change clears role-given access and sends the Director one notice",
               "scoped = after[\"role\"] in data_access.ROLE_SCOPES" in admin_src and admin_src.count("_tell_director_data_access(after") == 1)
    check_true("no confirmation step for the Garmin role (the owner, 14.10)",
               "_handle_role_access_decision" not in load_source("integrations/org_bot/ops_manager.py")
               and "confirm_role_access" not in store_src)

    # 5: Garmin's SAP — the Garmin warehouses only
    rows_by_tool = {
        "sales_lines": [{"ObjType": "13", "DocEntry": 1, "WhsCode": "G.A._01"}, {"ObjType": "13", "DocEntry": 2, "WhsCode": "PR_01"}],
        "sales": [{"ObjType": "13", "DocEntry": 1, "CardName": "Garmin мижоз", "DocDate": "2026-10-13"},
                  {"ObjType": "13", "DocEntry": 2, "CardName": "Primus меҳмонхона", "DocDate": "2026-10-13"}],
    }

    async def read(query, params=None, timeout="10s"):
        tool = (params or {}).get("k") if isinstance(params, dict) else "sales_lines"
        from datetime import datetime, timezone
        return [{"raw": r, "captured_at": datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)} for r in rows_by_tool.get(tool, [])]

    async def fresh():
        return "SAP fresh", False

    saved = [(analyst, "fetch_read_only", analyst.fetch_read_only), (ops_manager, "sap_freshness", ops_manager.sap_freshness)]
    analyst.fetch_read_only, ops_manager.sap_freshness = read, fresh
    try:
        out = asyncio.run(analyst.garmin_sap({"kind": "sales"}))
        check_true("Garmin's SAP: only invoices with a line in a Garmin warehouse — Primus customers never shown",
                   "Garmin мижоз" in out and "Primus" not in out)
        check("other kinds (customers' debts, payments) aren't offered", "Unknown kind" in asyncio.run(analyst.garmin_sap({"kind": "ar_open"})), True)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # 7 / 8: returned tasks come back to the tracker; finished-with-proof counts in KPI
    check_true("a returned task's reminder and overdue notice may go out again",
               "reminded_at = NULL, overdue_notified_at = NULL" in store_src)
    check_true("KPI counts work finished with proof (in review) like the on-time index", store_src.count("status IN ('done', 'review')") >= 2)

    # 9 / 12: a complaint during the emergency stop is still a ticket; the client's language is kept
    fb_src = load_source("integrations/org_bot/feedback.py")
    check_true("frozen: the ticket is opened, and its first hourly check sends the whole card",
               fb_src.index("open_complaint_ticket") < fb_src.index("if settings.bots_frozen:")
               and "if row.get(\"cards\"):" in fb_src)
    check_true("the client's language is stored and shown on every card, also once resolved",
               "lang=sub.lang" in fb_src and "🌐 Мижоз тили: русча" in feedback.ticket_text(
                   {"place": "garmin", "branch": "abay", "message": "x", "ticket_status": "resolved", "lang": "ru",
                    "resolved_by": "A", "reason_code": "other"}))

    # 11 / 13: both SAP warnings in the Garmin brief; the two briefs share their template pieces
    gb_src = load_source("agents/garmin-brief/agent.py")
    check_true("the Garmin brief keeps both SAP warnings, and builds rows with the shared pieces",
               '" · ".join(notes)' in gb_src and "brief_parts.figure_row" in gb_src
               and "brief_parts" in load_source("agents/ceo-daily-brief/agent.py"))
    # 14: the old Start/Done keyboard is gone
    check_true("the Garmin brief goes to whoever holds the role", "if data_access.granted(p)" in gb_src)
    check_true("one task keyboard (task_register.keyboard), the old one removed",
               "def _task_keyboard" not in load_source("integrations/org_bot/ops_manager.py"))
    _ = (admin, data_access)


def test_data_check_fixes() -> None:
    """15.10.2026: the Director's brief checked against SAP — the causes found and fixed."""
    print("SAP data check fixes (15.10.2026)")
    import asyncio
    import importlib.util
    import uuid
    from datetime import datetime, timezone

    from integrations.org_bot import analyst, store
    from integrations.sap import push_handler

    root = Path(__file__).resolve().parents[1]
    # the old capped invoice push no longer overwrites the complete list
    logged: list = []

    async def recent_full(query, params=None):
        return {"x": 1}

    async def fake_log(**kwargs):
        logged.append(kwargs["action"])

    saved = [(push_handler, "fetch_one", push_handler.fetch_one), (push_handler, "log_action", push_handler.log_action)]
    push_handler.fetch_one, push_handler.log_action = recent_full, fake_log
    try:
        out = asyncio.run(push_handler.handle_ar_aging_push({"invoices": [{"DocEntry": 1, "CardCode": "C", "DocStatus": "O",
                                                                            "CANCELED": "N", "DocTotal": 5}]}, uuid.uuid4()))
        check_true("a capped get_invoices push after the complete one is ignored (today's debt not overwritten)",
                   out.get("ignored") and out["written"] == 0 and logged == ["ar_aging_push_ignored"])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    logged.clear()
    saved_cap = [(push_handler, "fetch_one", push_handler.fetch_one), (push_handler, "log_action", push_handler.log_action)]
    push_handler.fetch_one, push_handler.log_action = recent_full, fake_log
    try:
        capped = asyncio.run(push_handler.handle_gateway_push("customers", {"rows": [{"CardCode": "C1"}]}, uuid.uuid4()))
    finally:
        for obj, name, value in saved_cap:
            setattr(obj, name, value)
    check_true("a capped customers / orders / products / payments push after the complete one is ignored (17.10)",
               capped.get("ignored") and capped["written"] == 0 and logged == ["gateway_push_customers_ignored"])

    js = (root / "sap-gateway" / "src" / "stock-value.js").read_text(encoding="utf-8")
    check_true("the gateway values stock with SAP's own per-warehouse value (OITW.StockValue, then OITW.AvgPrice)",
               'NULLIF(W."StockValue", 0)' in js and 'W."OnHand" * W."AvgPrice"' in js)

    spec = importlib.util.spec_from_file_location("garmin_brief_chk", root / "agents" / "garmin-brief" / "agent.py")
    gb = importlib.util.module_from_spec(spec)
    sys.modules["garmin_brief_chk"] = gb
    spec.loader.exec_module(gb)
    check_true("the Garmin brief carries no stock (19.10, the owner)", not hasattr(gb, "stock_value")
               and "stock_value" not in load_source("agents/garmin-brief/agent.py"))

    async def recent(days=60):
        return [{"place": "garmin", "branch": "abay", "message": "test", "phone": None, "contact_name": None, "kind": "complaint",
                 "created_at": datetime(2026, 10, 14, 12, 59, tzinfo=timezone.utc), "code": "T-2026-10-0031", "ticket_status": "open"},
                {"place": "garmin", "branch": "abay", "message": "консультант", "phone": None, "contact_name": None,
                 "kind": "complaint", "created_at": datetime(2026, 10, 5, 13, 39, tzinfo=timezone.utc)}]

    saved = store.recent_client_feedback
    store.recent_client_feedback = recent
    try:
        out = asyncio.run(analyst.garmin_data({"source": "complaints"}))
        lines = out.splitlines()
        check_true("each complaint carries its own code and state (the 05.10 one is not a ticket)",
                   any("T-2026-10-0031" in ln and "«test»" in ln and "open ticket" in ln for ln in lines)
                   and any("no code" in ln and "консультант" in ln and "not a ticket" in ln for ln in lines)
                   and "QR page" in out)
    finally:
        store.recent_client_feedback = saved

    async def checks(query, params=None, timeout="10s"):
        return [{"check_date": "2026-10-14", "status": "wrong", "checked_at": None,
                 "result": {"billz": {"cheques": 3}, "sap": {"documents": 2},
                            "missing": [{"number": "1043", "day": "2026-10-14", "amount": 25480000, "shop": "GARMIN MINOR",
                                         "seller": "Ширин"}]}}]

    saved = analyst.fetch_read_only
    analyst.fetch_read_only = checks
    try:
        out = asyncio.run(analyst.garmin_data({"source": "billz_sap"}))
        check_true("the Garmin manager can see which cheques are not in SAP, by number", "NOT in SAP: cheque 1043" in out)
    finally:
        analyst.fetch_read_only = saved


def test_employee_change_fields() -> None:
    """15.10.2026: every /xodimlar SAP / 1C / Billz / language toggle failed and flipped back and forth."""
    print("employee change fields and replayed button presses (15.10.2026)")
    import re

    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from integrations.api import app as api_app
    from integrations.common.config import settings

    root = Path(__file__).resolve().parents[1]
    used: set[str] = set()
    for folder in ("integrations", "agents", "scripts"):
        for path in (root / folder).rglob("*.py"):
            if path.name != "selfcheck.py":
                used |= set(re.findall(r"log_employee_change\(\s*[^,]+,\s*\"(\w+)\"", path.read_text(encoding="utf-8")))
    schema = (root / "database" / "schema.sql").read_text(encoding="utf-8")
    last = schema[schema.rindex("ADD CONSTRAINT employee_changes_field_check"):]
    allowed = set(re.findall(r"'(\w+)'", last[:last.index(";")]))
    check_true("the toggles' fields are found in the code", {"data_access", "lang", "role", "full_name"} <= used)
    check("every field the code logs is allowed by the database", sorted(used - allowed), [])

    # A handler that raises must not answer 5xx: Telegram would replay the press and flip the toggle again.
    logged: list[str] = []

    class FakeLog:
        def error(self, fmt, *args):
            logged.append(fmt.format(*args))

    async def broken(*args, **kwargs):
        raise RuntimeError("DETAIL: Failing row contains (Алишер Каримов, sap)")

    saved = [(settings, "admin_bot_webhook_secret", settings.admin_bot_webhook_secret),
             (settings, "ops_manager_bot_webhook_secret", settings.ops_manager_bot_webhook_secret),
             (settings, "admin_bot_telegram_bot_token", settings.admin_bot_telegram_bot_token),
             (settings, "ops_manager_bot_telegram_bot_token", settings.ops_manager_bot_telegram_bot_token),
             (settings, "bots_frozen", settings.bots_frozen), (api_app, "log", api_app.log),
             (api_app.org_admin, "handle_admin_callback", api_app.org_admin.handle_admin_callback),
             (api_app.org_ops_manager, "handle_update", api_app.org_ops_manager.handle_update)]
    try:
        settings.admin_bot_webhook_secret = SecretStr("adm1n-s3cret-long")
        settings.ops_manager_bot_webhook_secret = SecretStr("0ps-s3cret-long")
        settings.admin_bot_telegram_bot_token = settings.ops_manager_bot_telegram_bot_token = SecretStr("")
        settings.bots_frozen = False
        api_app.log = FakeLog()
        api_app.org_admin.handle_admin_callback = broken
        api_app.org_ops_manager.handle_update = broken
        client = TestClient(api_app.app, raise_server_exceptions=False)
        press = {"callback_query": {"id": "1", "data": "dacc:sap:x", "from": {"id": 5}}}
        admin = client.post("/webhooks/telegram/admin/adm1n-s3cret-long", json=press)
        ops = client.post("/webhooks/telegram/ops/0ps-s3cret-long", json=press)
        check_true("a failed Admin Bot press answers 200 — Telegram doesn't replay it",
                   admin.status_code == 200 and admin.json() == {"status": "error"})
        check_true("the same for OPS Manager Bot", ops.status_code == 200 and ops.json() == {"status": "error"})
        check_true("the log names the error and our line, never the row's data",
                   len(logged) == 2 and "RuntimeError" in logged[0] and "selfcheck.py" not in logged[0]
                   and not any("Алишер" in line or "Failing row" in line for line in logged))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_accountant_test_fixes() -> None:
    """15.10.2026, the Бухгалтер test: what the bot couldn't find or paired wrongly."""
    print("accountant test fixes (15.10.2026)")
    import asyncio
    from datetime import datetime, timezone

    from integrations.onec.payables import norm_name
    from integrations.org_bot import analyst, data_access, knowledge, ops_manager

    # 1C ↔ SAP supplier pairing: Latin legal forms are taken off before the name goes into Cyrillic
    check_true("'YaTT \"UMAROVA …\"' (SAP) pairs with 'UMAROVA …' (1C) — the biggest creditor",
               norm_name('YaTT "UMAROVA NAIMA MIRZAXIDOVNA"') == norm_name("UMAROVA NAIMA MIRZAXIDOVNA"))
    check_true("'Tintolav S.R.L.' = 'Tintolav'; 'GARMIN EUROPE LTD' = 'Garmin Europe'; 'Aqua Care LLC' = 'AQUA CARE'",
               norm_name("Tintolav S.R.L.") == norm_name("Tintolav")
               and norm_name("GARMIN EUROPE LTD") == norm_name("Garmin Europe")
               and norm_name("Aqua Care LLC") == norm_name("AQUA CARE"))
    check_true("different suppliers stay apart", norm_name("Primus Parts") != norm_name("Primus FCA"))

    saved = [(analyst, "fetch_read_only", analyst.fetch_read_only), (ops_manager, "sap_freshness", ops_manager.sap_freshness)]

    async def freshness():
        return "SAP data is current.", False

    async def balances(query, params=None, timeout="10s"):
        return [{"captured_at": datetime(2026, 10, 15, 12, tzinfo=timezone.utc), "raw": r} for r in (
            {"CardCode": "S1", "CardName": "Coscom", "Balance": 10, "BalanceSys": 120000},
            {"CardCode": "S2", "CardName": "Payme", "Balance": -2, "BalanceSys": -24000})]

    try:
        ops_manager.sap_freshness = freshness
        analyst.fetch_read_only = balances
        text = asyncio.run(analyst.sap_records({"kind": "supplier_balances", "date_from": "2026-10-01",
                                                "date_to": "2026-10-15"}))
        check_true("supplier balances with a period asked: every row kept, not '0 suppliers'",
                   ": 2 rows." in text and "have no dates" in text and "BalanceSys UZS: 96,000" in text)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # 1C: an account with no rows names the neighbours that have them (revenue is 9020 / 9030 here)
    accounts = {"a": ("9010", "Доходы от реализации готовой продукции"), "b": ("9020.1", "Доходы от реализации товаров"),
                "c": ("9030.1", "Доходы от услуг"), "d": ("9120.1", "Себестоимость товаров")}
    rows = [{"Account_Key": "b", "СуммаTurnoverCr": 5.0}, {"Account_Key": "c", "СуммаTurnoverCr": 2.0},
            {"Account_Key": "d", "СуммаTurnoverDr": 3.0}]
    empty = analyst._onec_report("t", rows, accounts, {}, "9010", ["СуммаTurnoverCr"])
    check_true("9010 empty: says 9020.1 and 9030.1 do have rows", "No rows on accounts starting '9010'" in empty
               and "9020.1, 9030.1" in empty and "9120.1" not in empty)
    check_true("'90' finds the revenue", "Account 9020.1" in analyst._onec_report("t", rows, accounts, {}, "90",
                                                                                   ["СуммаTurnoverCr"]))
    turnovers = next(t for t in analyst.TOOLS if t["function"]["name"] == "onec_turnovers")["function"]["description"]
    check_true("the tool and the company knowledge name this company's revenue and cost accounts",
               "9020 (goods) and 9030 (services)" in turnovers and "9120" in turnovers
               and "9010 isn't used" in knowledge.COMPANY_KNOWLEDGE and "6015 in" in knowledge.COMPANY_KNOWLEDGE)

    # the Billz → SAP check by cheque: for accounting given Billz + SAP, not Billz alone
    names = lambda scope: {t["function"]["name"] for t in analyst.tools_for(scope)}  # noqa: E731
    check_true("billz_sap_check: Billz + SAP only (the Garmin manager has it in garmin_data)",
               "billz_sap_check" in names({"billz", "sap"}) and "billz_sap_check" not in names({"billz"})
               and "billz_sap_check" not in names(data_access.granted({"role": "garmin_manager", "status": "active"})))

    async def checks(query, params=None, timeout="10s"):
        return [{"check_date": "2026-10-14", "status": "wrong", "checked_at": None,
                 "result": {"billz": {"cheques": 2}, "sap": {"documents": 1},
                            "missing": [{"number": "1043", "day": "2026-10-14", "amount": 25480000, "shop": "GARMIN MINOR",
                                         "seller": "Ширина"}]}}]

    try:
        analyst.fetch_read_only = checks
        out = asyncio.run(analyst.billz_sap_check({}))
        same = asyncio.run(analyst.garmin_data({"source": "billz_sap"}))
        check_true("lists the cheques not in SAP, by number; garmin_data gives the same",
                   "NOT in SAP: cheque 1043" in out and out == same)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # live (17.10): Billz read now against the latest SAP push; stored 08:00 results only as a fallback
    from integrations.billz import sap_check
    from integrations.common.config import settings as _settings

    class FakeCheckAgent:
        @staticmethod
        async def billz_cheques(start, end, run_id):
            return ([sap_check.Cheque(key="c1", day=date(2026, 10, 16), number="010001060236", amount=19300000, shop="GARMIN MINOR",
                                      seller="Shirina", items=[])], {}, ["GARMIN MINOR"])

        @staticmethod
        async def _sap_rows(tool):
            return []

        @staticmethod
        def compare(cheques, sales, lines, day, mapping):
            return sap_check.check(cheques, [], day)

        @staticmethod
        async def last_sales_push():
            return datetime(2026, 10, 17, 12, 13, tzinfo=timezone.utc)

    from integrations.common import agent_loader

    saved_live = [(agent_loader, "load_agent", agent_loader.load_agent), (type(_settings), "billz_configured", type(_settings).billz_configured)]
    try:
        agent_loader.load_agent = lambda name: FakeCheckAgent
        type(_settings).billz_configured = property(lambda self: True)
        live = asyncio.run(analyst.billz_sap_check({}))
    finally:
        for obj, name, value in saved_live:
            setattr(obj, name, value)
    check_true("the cheque check is LIVE: Billz now, SAP's last push, cheque by cheque",
               live.startswith("LIVE Billz → SAP check") and "NOT in SAP: cheque 010001060236" in live)

    async def two_line_doc(query, params=None, timeout="10s"):
        return [{"captured_at": datetime(2026, 10, 17, 7, tzinfo=timezone.utc), "raw": r} for r in (
            {"ObjType": "13", "DocEntry": 2496, "LineNum": 0, "DocDate": "2026-10-16", "WhsCode": "05", "ItemCode": "A",
             "DocTotalSy": 15000000, "GTotalSC": 14200000, "LineTotal": 1321.87},
            {"ObjType": "13", "DocEntry": 2496, "LineNum": 1, "DocDate": "2026-10-16", "WhsCode": "05", "ItemCode": "B",
             "DocTotalSy": 15000000, "GTotalSC": 800000, "LineTotal": 59.17})]

    saved_lines = [(analyst, "fetch_read_only", analyst.fetch_read_only), (ops_manager, "sap_freshness", ops_manager.sap_freshness)]
    try:
        analyst.fetch_read_only, ops_manager.sap_freshness = two_line_doc, freshness
        lines_out = asyncio.run(analyst.sap_records({"kind": "sales_lines"}))
    finally:
        for obj, name, value in saved_lines:
            setattr(obj, name, value)
    check_true("invoice lines: the document's total counted once, the lines' so'm summed (17.10: 15M had become 30M)",
               "DocTotalSy UZS: 15,000,000" in lines_out and "GTotalSC UZS: 15,000,000" in lines_out
               and "- A: 1 rows, 14,200,000" in lines_out)

    prompt = " ".join(analyst.employee_prompt("Дониёр", "Бухгалтерия", {"sap", "1c", "billz"}).split())
    sap_desc = analyst.SAP_KINDS["supplier_balances"]
    check_true("SAP supplier balance sign: negative = we owe (checked against 1C, 15.10)",
               "NEGATIVE = WE OWE" in sap_desc and "positive = we owe" not in sap_desc)
    check_true("refusals: other people's attendance / KPI / tasks are the Director's only — no 'ask the admin'",
               "are the Director's only" in prompt and "don't suggest" in prompt
               and "admin can open it only with the Director's approval" in prompt)


def test_report_deadline_penalty() -> None:
    """15.10.2026, the Director: reports until 22:00; no report = minus KPI points every day, and the person is told."""
    print("daily reports: 22:00 and minus points (15.10.2026)")
    import asyncio
    import inspect
    from datetime import datetime

    from integrations.api import app as api_app
    from integrations.common.config import settings
    from integrations.common.timeutil import TASHKENT
    from integrations.org_bot import kpi, kpi_score, ops_manager, report_close, store, tone

    day = date(2026, 10, 15)
    check_true("open at 21:59, closed at 22:00 and on another day",
               store.reports_open(day, datetime(2026, 10, 15, 21, 59, tzinfo=TASHKENT))
               and not store.reports_open(day, datetime(2026, 10, 15, 22, 0, tzinfo=TASHKENT))
               and not store.reports_open(day, datetime(2026, 10, 16, 9, 0, tzinfo=TASHKENT)))
    for fn in (store.pending_report, store.open_report_followup):
        check_true(f"{fn.__name__} stops at 22:00 in SQL", "make_time(%s, 0, 0)" in inspect.getsource(fn))
    check_true("✏️ / 🗑 on a report only until 22:00", "reports_open(today)" in inspect.getsource(ops_manager._handle_report_button))
    check_true("the 16:00 ask and the 17:00 reminder say «соат 22:00 гача»",
               "22:00" in kpi.build_request_text("Алишер ака", []) and "22:00" in kpi.build_reminder_text("Алишер ака", []))
    text = kpi.build_penalty_text("Алишер ака", 1, 3)
    check_true("the minus, told politely, no emoji: name kept, 'сиз', −1 today and −3 this month",
               text.startswith("Алишер ака,") and "1 балл" in text and "−3 балл" in text and not tone._EMOJI.search(text)
               and "KPI" in text and tone.is_polite(text))
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("schema: penalty_points and penalized_at", "ADD COLUMN IF NOT EXISTS penalty_points" in schema
               and "ADD COLUMN IF NOT EXISTS penalized_at" in schema)
    check_true("the API checks every 5 minutes (no new cron)", "_report_close_loop" in inspect.getsource(api_app._startup))

    # close_day: nothing before 22:00; at 22:00 each open ask closed once and its person told
    sent: list[tuple[str, str]] = []
    asked_close: list = []

    async def fake_close(report_date, points):
        asked_close.append((report_date, points))
        return [{"telegram_user_id": 7, "full_name": "Алишер Каримов", "display_name": "a", "address_form": "aka",
                 "month_points": 3}]

    class FakeBot:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_message(self, text, chat_id):
            sent.append((chat_id, text))
            return [1]

    async def no_log(**kwargs):
        return None

    expired: list[int] = []

    async def fake_expire():
        expired.append(1)
        return 0

    saved = [(store, "close_missed_reports", store.close_missed_reports), (report_close, "TelegramBot", report_close.TelegramBot),
             (store, "expire_held_messages", store.expire_held_messages),
             (report_close, "log_action", report_close.log_action), (settings, "report_miss_penalty", settings.report_miss_penalty)]
    try:
        store.close_missed_reports, report_close.TelegramBot, report_close.log_action = fake_close, FakeBot, no_log
        store.expire_held_messages = fake_expire  # never the real database from a test
        settings.report_miss_penalty = 1
        before = asyncio.run(report_close.close_day(datetime(2026, 10, 15, 21, 55, tzinfo=TASHKENT)))
        check_true("before 22:00 nothing closes", before == 0 and not asked_close and not expired)
        asyncio.run(report_close.close_day(datetime(2026, 10, 15, 22, 5, tzinfo=TASHKENT)))
        check_true("15.10 (asked before the rule was told): closed at 22:00 with 0 points, nobody messaged",
                   asked_close == [(day, 0)] and not sent)
        asked_close.clear()
        closed = asyncio.run(report_close.close_day(datetime(2026, 10, 16, 22, 5, tzinfo=TASHKENT)))
        check_true("from 16.10 at 22:00: open asks closed with 1 point, the person told the month's −3",
                   closed == 1 and asked_close == [(date(2026, 10, 16), 1)] and sent and sent[0][0] == "7"
                   and "−3 балл" in sent[0][1])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("22:00 also closes held messages nobody tapped (marked expired, nothing deleted)",
               len(expired) == 2 and "outcome = 'expired'" in inspect.getsource(store.expire_held_messages)
               and "DELETE" not in inspect.getsource(store.expire_held_messages))
    check_true("closing marks once and only active people who get asks",
               "penalized_at IS NULL" in inspect.getsource(store.close_missed_reports)
               and "NOT e.reports_off" in inspect.getsource(store.close_missed_reports))

    # KPI: the minus comes off the month's total and shows in red
    emp = [{"id": "e1", "full_name": "Алишер Каримов", "role": "londry"}]
    reports = [{"employee_id": "e1", "report_date": date(2026, 10, d), "status": "asked", "submitted_at": None,
                "penalty_points": 1, "display_name": "a"} for d in (13, 14)]
    reports.append({"employee_id": "e1", "report_date": date(2026, 10, 15), "status": "submitted",
                    "submitted_at": datetime(2026, 10, 15, 12, tzinfo=TASHKENT), "penalty_points": 0, "display_name": "a"})
    with_minus = kpi_score.build(emp, reports, [], {}, [], [], date(2026, 10, 1), date(2026, 10, 31))[0]
    without = kpi_score.build(emp, [{**r, "penalty_points": 0} for r in reports], [], {}, [], [],
                              date(2026, 10, 1), date(2026, 10, 31))[0]
    check_true("2 missed days: −2 off the total", with_minus.penalty == 2 and with_minus.missed_days == 2
               and round(without.total - with_minus.total) == 2)
    card = kpi_score.card_text(date(2026, 10, 1), with_minus)
    check_true("/kpi card shows it in red", "🔴 Ҳисобот соат 22:00 гача келмагани учун: <b>−2</b> балл (2 кун)" in card)
    check_true("the table and the PDF show it", "🔴−2" in kpi_score.table_text(date(2026, 10, 1), [with_minus], False)
               and kpi_score.table_view(date(2026, 10, 1), [with_minus], False)["people"][0]["penalty"] == 2)


def test_command_menus() -> None:
    """16.10.2026: the ☰ menu with the / commands in both bots — only commands the bots handle."""
    print("bot command menus (16.10.2026)")
    import asyncio
    import inspect
    import re

    from integrations.api import app as api_app
    from integrations.common.config import settings
    from integrations.org_bot import admin, kpi_flow, ops_manager, permissions, report_tools, store
    from integrations.org_bot.roles import DIRECTOR_ROLE
    from integrations.telegram import menus

    for name, items in (("Admin", menus.ADMIN), ("employee", menus.EMPLOYEE), ("Director", menus.DIRECTOR),
                        ("Garmin manager", menus.GARMIN_MANAGER), ("B2B сотув", menus.B2B_SALES)):
        check_true(f"{name} menu: valid Telegram commands, Cyrillic descriptions",
                   all(re.fullmatch(r"[a-z0-9_]{1,32}", c) and 0 < len(d) <= 256 and re.search("[а-яўқғҳ]", d.lower())
                       for c, d in items) and len({c for c, _ in items}) == len(items))
    admin_handles = {*admin.EMPLOYEE_LIST_COMMANDS, *admin.ASK_NAMES_COMMANDS, *admin.DATA_QUALITY_COMMANDS,
                     admin.QR_COMMAND, admin.VERIFIX_COMMAND, admin.BILLZ_COMMAND, *admin.ONEC_COMMANDS, *admin.DB_REVIEW_COMMANDS,
                     *admin.DAY_OFF_COMMANDS, *admin.FLEXIBLE_COMMANDS, *admin.TECH_REPORT_COMMANDS, admin.ANNOUNCE_COMMAND,
                     *admin.LEADS_COMMANDS}
    ops_handles = {*report_tools.REPORT_COMMANDS, *kpi_flow.KPI_COMMANDS, *kpi_flow.RESULT_COMMANDS,
                   *kpi_flow.GOAL_COMMANDS, *kpi_flow.GOALS_COMMANDS, *kpi_flow.RATE_COMMANDS,
                   *permissions.COMMANDS, *permissions.CANCEL_COMMANDS, *ops_manager.NAME_CHANGE_COMMANDS,
                   *ops_manager.report_calendar.COMMANDS, *ops_manager.old_stock.COMMANDS,
                   *ops_manager.competitors.COMMANDS, *ops_manager.PULSE_COMMANDS, *ops_manager.excused.COMMANDS,
                   *ops_manager.stock_list.COMMANDS, *ops_manager.lead_tracker.COMMANDS}
    check_true("every Admin menu command is one Admin Bot handles", all("/" + c in admin_handles for c, _ in menus.ADMIN))
    check_true("every OPS Manager menu command is one it handles",
               all("/" + c in ops_handles for c, _ in menus.EMPLOYEE + menus.DIRECTOR + menus.GARMIN_MANAGER + menus.B2B_SALES))
    check_true("the Director's menu has /maqsad /baho; the employees' doesn't",
               {"maqsad", "baho"} <= {c for c, _ in menus.DIRECTOR} and not {"maqsad", "baho"} & {c for c, _ in menus.EMPLOYEE})

    calls: list[tuple[str, str, dict]] = []

    class FakeBot:
        def __init__(self, agent, bot_token, **kwargs):
            self.token = bot_token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def _call(self, method, payload, mode="notify", target_ref=None):
            calls.append((self.token, method, payload))
            return {}

    async def directors(role):
        return [{"telegram_user_id": 77}] if role == DIRECTOR_ROLE else []

    saved = [(menus, "TelegramBot", menus.TelegramBot), (store, "active_employees_by_role", store.active_employees_by_role),
             (settings, "admin_bot_admin_user_id", settings.admin_bot_admin_user_id),
             (settings, "admin_bot_telegram_bot_token", settings.admin_bot_telegram_bot_token),
             (settings, "ops_manager_bot_telegram_bot_token", settings.ops_manager_bot_telegram_bot_token)]
    try:
        from pydantic import SecretStr

        menus.TelegramBot, store.active_employees_by_role = FakeBot, directors
        settings.admin_bot_admin_user_id = 5
        settings.admin_bot_telegram_bot_token, settings.ops_manager_bot_telegram_bot_token = SecretStr("A"), SecretStr("O")
        done = asyncio.run(menus.sync_all())
        sets = [(t, p.get("scope"), [c["command"] for c in p["commands"]]) for t, m, p in calls if m == "setMyCommands"]
        check_true("sync: Admin menu for the admin only, employees' menu for everyone, the Director's for each Director",
                   done == 3 and ("A", {"type": "chat", "chat_id": 5}, [c for c, _ in menus.ADMIN]) in sets
                   and ("O", None, [c for c, _ in menus.EMPLOYEE]) in sets
                   and ("O", {"type": "chat", "chat_id": 77}, [c for c, _ in menus.DIRECTOR]) in sets)
        check_true("the ☰ button is set to show the commands",
                   sum(1 for _t, m, p in calls if m == "setChatMenuButton" and p["menu_button"] == {"type": "commands"}) == 3)
        calls.clear()
        asyncio.run(menus.sync_person(88, "buxgalteriya"))
        check_true("someone who stops being Director goes back to the employees' menu",
                   calls == [("O", "deleteMyCommands", {"scope": {"type": "chat", "chat_id": 88}})])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("menus set at startup and on a role change, approval and removal",
               "_set_menus" in inspect.getsource(api_app._startup)
               and inspect.getsource(admin).count("menus.sync_person(") == 3)


def test_garmin_memory_and_team_reports() -> None:
    """16.10.2026: the Garmin manager's chat memory, and his team's daily reports."""
    print("Garmin manager: memory and team reports (16.10.2026)")
    import asyncio
    import inspect
    from datetime import datetime

    from integrations.org_bot import analyst, data_access, ops_manager, store

    q = ops_manager.with_quote("ундан кейинги чек-чи?", "Кеча 3 та чек SAP'га киритилмаган: 1041246 …")
    check_true("a reply to the bot's message carries what was quoted", "1041246" in q and q.startswith("ундан кейинги"))
    check("no quote, the question as typed", ops_manager.with_quote("салом", None), "салом")
    handler = inspect.getsource(ops_manager)
    check_true("only the bot's own messages are quoted, task cards keep their own flow",
               '(reply_to.get("from") or {}).get("is_bot")' in handler and "task is None and" in handler)
    check_true("both AIs get the quote", inspect.getsource(ops_manager._answer_data_question_now).count("with_quote(") == 1
               and inspect.getsource(ops_manager._answer_ai_chat_now).count("with_quote(") == 1)
    check_true("memory: 24 earlier messages (was 12)", ops_manager.AI_MEMORY_TURNS == 24
               and "AI_MEMORY_TURNS" in inspect.getsource(ops_manager._answer_data_question_now)
               and "AI_MEMORY_TURNS" in inspect.getsource(ops_manager._answer_ai_chat_now))

    asked: list = []

    async def fake_team(role, since):
        asked.append((role, since))
        return [{"report_date": date(2026, 10, 15), "status": "submitted", "content": "Абай: 3 та соат сотилди",
                 "submitted_at": datetime(2026, 10, 15, 13, 5), "penalty_points": 0, "name": "Ширина"},
                {"report_date": date(2026, 10, 16), "status": "asked", "content": None, "submitted_at": None,
                 "penalty_points": 1, "name": "Жасур"}]

    saved = store.team_reports
    try:
        store.team_reports = fake_team
        out = asyncio.run(analyst.garmin_data({"source": "reports"}))
    finally:
        store.team_reports = saved
    check_true("his team only (Garmin сотув), 14 days", asked and asked[0][0] == "garmin_sotuv")
    check_true("what each wrote, when, and who didn't send one with the minus",
               "Ширина" in out and "3 та соат" in out and "Жасур | NOT sent, −1 KPI" in out)
    prompt = analyst.employee_prompt("Улуғбек", "Garmin менежер",
                                     data_access.granted({"role": "garmin_manager", "status": "active"}))
    check_true("the prompt opens his team's reports and keeps everyone else's closed",
               "HIS TEAM" in prompt and "Anyone else's reports" in prompt)
    check_true("the card names it", "ҳисоботлари" in data_access.NAMES["garmin"])


def test_stock_items() -> None:
    """16.10.2026: «TIMER, DRYER … нечта қолди?» wasn't found — only 5 items per warehouse reached us."""
    print("stock by item (16.10.2026)")
    import asyncio
    import uuid

    from integrations.org_bot import analyst
    from integrations.sap import push_handler

    stored: list[tuple[str, int]] = []

    async def fake_store(dataset, rows, today, run_id, complete, missing):
        stored.append((dataset, len(rows)))
        return {"ok": True, "written": len(rows), "skipped": 0, "missing_columns": missing}

    saved = push_handler._store_full
    try:
        push_handler._store_full = fake_store
        rows = [{"ItemCode": "T1", "ItemName": "TIMER, DRYER 3CYCLE RETAIL 240V 50 Hz", "WhsCode": "01", "WhsName": "Асосий",
                 "OnHand": 4, "AvgPrice": 10, "StockValue": 40},
                {"ItemCode": "T2", "ItemName": "Fenix 8", "WhsCode": "G.A._01", "WhsName": "Абай", "OnHand": 2,
                 "AvgPrice": 500, "StockValue": 1000},
                {"ItemCode": "T1", "ItemName": "TIMER, DRYER 3CYCLE RETAIL 240V 50 Hz", "WhsCode": "02", "WhsName": "Б",
                 "OnHand": 1, "AvgPrice": 10, "StockValue": 10}]
        out = asyncio.run(push_handler.handle_full_push("stock_value", {"rows": rows}, uuid.uuid4()))
    finally:
        push_handler._store_full = saved
    check_true("get_stock_value: totals per warehouse AND every item row kept as stock_items",
               out["ok"] and stored == [("stock_value", 3), ("stock_items", 3)])
    check_true("stock_items keyed by item and warehouse", push_handler.FULL_DATASETS["stock_items"] == ("ItemCode", "WhsCode"))
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("the table accepts the new kind", "'stock_items'" in schema)
    check_true("the analyst sends item questions to stock_items, not the capped inventory",
               "stock_items" in analyst.SAP_KINDS and "only 5 items per warehouse" in analyst.SAP_KINDS["inventory"]
               and "stock_items" in analyst.GARMIN_SAP_KINDS)


def test_sap_everything() -> None:
    """16.10.2026: everything the owner's plan needs from SAP, complete, in one push; no log file on the gateway PC."""
    print("SAP: everything the plan needs (16.10.2026)")
    import json
    import re

    from integrations.org_bot import analyst
    from integrations.sap import push_handler

    root = Path(__file__).resolve().parents[1]
    js = (root / "sap-gateway" / "src" / "complete.js").read_text(encoding="utf-8")
    server = (root / "sap-gateway" / "src" / "server.js").read_text(encoding="utf-8")
    script = (root / "scripts" / "sap-gateway-push" / "push-ar-aging.ps1").read_text(encoding="utf-8")
    more = re.findall(r'@\{ Tool = "(\w+)"; Kind = "(\w+)"; Body = \'([^\']*)\'; Fallback = "(\w*)" \}', script)
    check("the push calls 12 more complete tools", len(more), 12)
    served = set(re.findall(r"app\.post\('/tools/(\w+)'", server))
    check_true("the gateway serves every one", {t for t, _k, _b, _f in more} <= served)
    check_true("each lands in a kind the Command Center stores and the analyst knows",
               all(k in push_handler.FULL_DATASETS and k in analyst.SAP_KINDS for _t, k, _b, _f in more))
    check_true("request bodies are JSON", all(isinstance(json.loads(b), dict) for _t, _k, b, _f in more))
    check_true("a capped tool only while the complete one is missing",
               all(f'if (-not $done["{f}"]) {{ Push-GatewayTool -Tool "{f}"' in script for _t, _k, _b, f in more if f))
    check_true("the gateway SQL: read-only, values bound, no contact details",
               "INSERT" not in js.upper().replace("INSERTS", "") and "UPDATE " not in js.upper() and "DELETE" not in js.upper()
               and "ADD_DAYS(CURRENT_DATE, ?)" in js and not re.search(r'"(Phone1|Cellular|E_Mail|Address|Telephone|Mobil|Email)"', js))
    check_true("no log file on the gateway computer; old ones removed; -Check prints types, never figures",
               "Add-Content" not in script and "Out-File" not in script and "Remove-Item -Force -ErrorAction SilentlyContinue" in script
               and '$col = $value' not in script)
    schema = (root / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("the table accepts item_sales_90d; big pushes fit", "'item_sales_90d'" in schema
               and push_handler.MAX_FULL_ROWS >= 100_000)


def test_cleanup_round_17_10() -> None:
    """17.10.2026: a repeated task reminds instead of a second row; a role change is a clean start;
    the report calendar; the bot's own database reviewed every morning."""
    print("repeated tasks, role change, report calendar, database review (17.10.2026)")
    import asyncio
    import inspect
    import uuid
    from datetime import datetime

    from integrations.common.timeutil import TASHKENT
    from integrations.org_bot import db_review, ops_manager, report_calendar, store, task_register
    from integrations.reports import render

    # ---- the same task again: a reminder on the open card, no second row
    old = {"id": "t1", "code": "T-2026-10-0001", "task_summary": "Юнусобод филиали ҳисоботини тайёрланг",
           "raw_message": "Зиёдага ҳисобот тайёрлашни топшир", "telegram_message_id": 77, "status": "sent"}
    draft = {"task_summary": "Юнусобод филиали ҳисоботини тайёрланг.", "raw_message": "Зиёда ҳисоботни тайёрласин",
             "director_telegram_user_id": 1, "source_message_id": 5, "has_media": False, "due_date": None}
    check_true("the same instruction is recognised; a different one isn't",
               task_register.same_task(old, draft)
               and not task_register.same_task(old, {"task_summary": "Омбордаги қолдиқни санаб чиқинг", "raw_message": "x"}))
    created, events, sent_msgs = [], [], []

    class FakeBot:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def send_message(self, text, chat_id=None, reply_markup=None, reply_to_message_id=None):
            sent_msgs.append((chat_id, text, reply_to_message_id))
            return [900]

    async def open_tasks_of(employee_id):
        return [old] if employee_id == "e1" else []

    async def create_task(**kw):
        created.append(kw["assigned_employee_id"])
        return {"id": "t2", "code": "T-2026-10-0002"}

    async def log_event(task_id, event, actor, note=None):
        events.append((task_id, event))

    async def nothing(*a, **k):
        return None

    saved = [(ops_manager, "TelegramBot", ops_manager.TelegramBot), (store, "open_tasks_of", store.open_tasks_of),
             (store, "create_task", store.create_task), (store, "log_task_event", store.log_task_event),
             (store, "set_task_message_id", store.set_task_message_id)]
    try:
        ops_manager.TelegramBot, store.open_tasks_of, store.create_task = FakeBot, open_tasks_of, create_task
        store.log_task_event, store.set_task_message_id = log_event, nothing
        reminded: list = []
        people = [{"id": "e1", "telegram_user_id": 11, "role": "londry", "full_name": "Зиёда"},
                  {"id": "e2", "telegram_user_id": 12, "role": "londry", "full_name": "Шерзод"}]
        sent = asyncio.run(ops_manager._send_task_cards(draft, people, uuid.uuid4(), reminded))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("who has it open is reminded on the existing card; only the other gets a new task",
               created == ["e2"] and [p["id"] for p in sent] == ["e2"] and [p["id"] for p in reminded] == ["e1"]
               and ("11", task_register.repeated_text(old), 77) in sent_msgs and ("t1", "repeated") in events)
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("the task history accepts 'repeated'", "'cancelled', 'repeated'" in schema)

    # ---- a role change: a clean start (own rows only — a co-assignee keeps theirs)
    src = inspect.getsource(store.change_employee_role)
    check_true("role change: own open tasks cancelled, today's ask closed without a minus, AI memory cleared",
               "WHERE assigned_employee_id = %s AND status IN" in src and "penalty_points = 0" in src
               and "DELETE FROM ai_chat_turns" in src)
    kpi_src = inspect.getsource(store.kpi_month_rows)
    check_true("KPI counts only the role held now, never cancelled tasks",
               "r.role = e.role" in kpi_src and "t.target_role = e.role" in kpi_src and "t.status <> 'cancelled'" in kpi_src)

    # ---- the report calendar
    now = datetime(2026, 10, 17, 18, 0, tzinfo=TASHKENT)
    check("cells: sent / missed / open today / not asked / future",
          [report_calendar.cell({"status": "submitted"}, date(2026, 10, 15), now),
           report_calendar.cell({"status": "asked"}, date(2026, 10, 15), now),
           report_calendar.cell({"status": "asked"}, date(2026, 10, 17), now),
           report_calendar.cell(None, date(2026, 10, 11), now),
           report_calendar.cell(None, date(2026, 10, 20), now)],
          ["sent", "missed", "open", "none", "future"])
    emps = [{"id": "e1", "full_name": "Алишер Каримов", "role": "londry"}, {"id": "d", "full_name": "Бош", "role": "operatsion_direktor"}]
    rows = [{"employee_id": "e1", "report_date": date(2026, 10, 15), "status": "submitted", "penalty_points": 0},
            {"employee_id": "e1", "report_date": date(2026, 10, 16), "status": "asked", "penalty_points": 1}]
    view = report_calendar.build(date(2026, 10, 1), now, emps, rows)
    check_true("one row per employee (not the Director), every day of the month, the minus counted",
               len(view["people"]) == 1 and len(view["days"]) == 31 and view["people"][0].missed == 1
               and view["people"][0].minus == 1 and view["share"] == 50)
    png = render.image("reports_calendar.html", width_css=report_calendar.CALENDAR_WIDTH_CSS, **view)
    check_true("drawn as one picture", png[:4] == b"\x89PNG")
    check_true("the Director's /hisobotlar sends it", "report_calendar.COMMANDS" in inspect.getsource(ops_manager))

    # ---- the database review: the bot's own tables, UPDATE fixes only, counts to IT
    sql = " ".join(c.count_sql + " " + (c.fix_sql or "") for c in db_review.CHECKS).lower()
    check_true("never the platforms' tables", not any(t in sql for t in ("sap_gateway", "ar_aging", "billz", "attendance_snap")))
    check_true("fixes are UPDATEs only, and run_fix refuses anything else",
               all(c.fix_sql is None or c.fix_sql.lstrip().upper().startswith("UPDATE ") for c in db_review.CHECKS))
    try:
        asyncio.run(store.run_fix("DELETE FROM tasks"))
        refused = False
    except ValueError:
        refused = True
    check_true("...a DELETE is refused before it reaches the database", refused)
    findings = [db_review.Finding(db_review.CHECKS[0], 3, 3), db_review.Finding(db_review.CHECKS[3], 2, 0),
                db_review.Finding(db_review.CHECKS[4], 0, 0)]
    msg = db_review.text(findings)
    check_true("the message: what it fixed, what to look at and where — counts only",
               "Ўзи тузатди" in msg and ": 3" in msg and "<code>tasks</code>" in msg and "саволлар" not in msg)
    check("nothing found: one line", db_review.text([db_review.Finding(db_review.CHECKS[0], 0)]),
          "🧹 <b>Бот базаси текшируви</b>\n✅ Ҳаммаси тартибда.")


def test_own_week_and_team_calendar() -> None:
    """17.10.2026: Garmin shop staff set their own week (7 buttons) — no report asked on a day off;
    the Garmin manager sees his team's report calendar."""
    print("own week for shift workers, the Garmin manager's calendar (17.10.2026)")
    import asyncio
    import inspect
    import uuid

    from datetime import datetime

    from integrations.common.timeutil import TASHKENT
    from integrations.org_bot import admin, kpi, ops_manager, report_calendar, store
    from integrations.telegram import menus

    seller = {"id": "e1", "role": "garmin_sotuv", "status": "active", "display_name": "a", "full_name": "Ширина",
              "work_days": "1101011"}  # off Wednesday and Friday
    check_true("their own week decides: asked Mon, Tue, Thu, Sat, Sun — not Wed, Fri",
               [kpi.works_on(seller, date(2026, 10, 12) + timedelta(days=i)) for i in range(7)]
               == [True, True, False, True, False, True, True])
    plain = {"role": "londry", "works_saturday": True}
    check_true("everyone else as before: Mon–Fri plus the weekend switches",
               kpi.week_of(plain) == "1111110" and kpi.works_on(plain, date(2026, 10, 17)) and not kpi.works_on(plain, date(2026, 10, 18)))
    text, keyboard = admin.employee_card(seller)
    days = [b["callback_data"].split(":")[0] for row in keyboard["inline_keyboard"] for b in row if b["callback_data"].startswith("wd")]
    labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row if b["callback_data"].startswith("wd")]
    check_true("Garmin сотув card: seven day buttons, ✅ on working days, no Saturday/Sunday pair",
               days == [f"wd{i}" for i in range(7)] and labels[2] == "☐ Чо" and labels[0] == "✅ Ду"
               and "wsat:" not in str(keyboard) and "Ду, Се, Па, Ша, Як" in text)
    _t, other = admin.employee_card({**plain, "id": "e2", "status": "active", "display_name": "b"})
    check_true("other roles keep the Saturday / Sunday buttons", "wsat:" in str(other) and "wd0:" not in str(other))

    flips: list = []

    async def toggle(employee_id, weekday, by):
        flips.append(weekday)
        return {**seller, "work_days": "1111011"}

    async def get_employee(employee_id):
        return seller

    edits: list = []

    async def edit(callback, text, keyboard, run_id):
        edits.append(text)

    async def none(*a, **k):
        return None

    saved = [(store, "toggle_work_day", store.toggle_work_day), (store, "get_employee", store.get_employee),
             (admin, "_edit", admin._edit), (admin, "_answer", admin._answer)]
    try:
        store.toggle_work_day, store.get_employee, admin._edit, admin._answer = toggle, get_employee, edit, none
        asyncio.run(admin.handle_admin_callback({"id": "q", "data": "wd2:e1", "from": {"id": 5}}, uuid.uuid4()))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("a tap flips that day and the card shows the new week",
               flips == [2] and edits and "Ҳисобот сўраладиган кунлар: Ду, Се, Чо, Па, Ша, Як" in edits[-1])
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("stored as seven 1/0; a role change or re-registration resets it",
               "work_days ~ '^[01]{7}$'" in schema and "work_days = NULL" in inspect.getsource(store.change_employee_role))

    # the Garmin manager's /hisobotlar: his team only, in his menu
    check("the Garmin manager's team", report_calendar.TEAM_OF.get("garmin_manager"), ("garmin_sotuv",))
    gm = {"id": "g1", "role": "garmin_manager", "status": "active", "display_name": "g", "full_name": "Улуғбек"}
    check_true("the Garmin manager is never asked for a report (like the Director)",
               not kpi.asked_for_reports(gm) and not kpi.asked_for_reports({"role": "operatsion_direktor"})
               and kpi.asked_for_reports(seller)
               and "kpi.asked_for_reports(e)" in load_source("agents/daily-reports/agent.py")
               and "'garmin_manager'" in inspect.getsource(store.close_missed_reports))
    gm_text, gm_keyboard = admin.employee_card(gm)
    check_true("...his card says so and has no 📝 switch; he isn't a calendar row; no /hisobot in his menu",
               "сўралмайди" in gm_text and "rpof:" not in str(gm_keyboard) and "wsat:" not in str(gm_keyboard)
               and "Иш кунлари" not in gm_text
               and report_calendar.build(date(2026, 10, 1), datetime(2026, 10, 17, 12, tzinfo=TASHKENT), [gm], [])["people"] == []
               and "hisobot" not in [c for c, _ in menus.GARMIN_MANAGER])
    check_true("...and his messages never go into a report", "kpi.asked_for_reports(employee)" in inspect.getsource(ops_manager._try_daily_report))

    # ---- the Garmin manager is a manager for his team, at the Boss's level (17.10): not scored
    from integrations.org_bot import kpi_flow, knowledge

    check_true("not scored: no KPI card, goals or ratings of his own",
               kpi_flow._team(gm) == ("garmin_sotuv",) and kpi_flow._is_manager(gm)
               and kpi_flow._team({"role": "operatsion_direktor"}) is None and kpi_flow._team(seller) == ()
               and "e[\"role\"] not in NO_REPORT_ROLES" in inspect.getsource(kpi_flow._people))
    flow = inspect.getsource(kpi_flow.handle_message)
    check_true("/kpi his team's table; /maqsad /maqsadlar /natija for his team; /baho the Director's only",
               "_pick_employee(user_id, run_id, _team(employee))" in flow and "_list_goals(user_id, run_id, _team(employee))" in flow
               and flow.index("if _is_director(employee):\n        if command in RATE_COMMANDS") > 0
               and "people = [p for p in people if p.employee_id in ids]" in inspect.getsource(kpi_flow._show_kpi))
    cb = inspect.getsource(kpi_flow.handle_callback)
    check_true("his buttons reach only his team; ratings refused", "_in_team(team, person.get(\"role\"))" in cb
               and 'prefix == "kr" and not director' in cb)
    check_true("his menu: team calendar, excused days, KPI, goals, results, old stock, competitors, PULSE — no /hisobot, "
               "/ruxsat or /ism", [c for c, _ in menus.GARMIN_MANAGER] == ["hisobotlar", "sababli", "kpi", "maqsad", "maqsadlar", "natija", "qoldiq", "eski",
                                                         "raqobat", "pulse", "bekor"])
    check_true("the bot knows who he is", "Murod" in knowledge.COMPANY_KNOWLEDGE and "brother" in knowledge.COMPANY_KNOWLEDGE)
    check_true("his ☰ menu has /hisobotlar; the employees' doesn't",
               ("hisobotlar" in [c for c, _ in menus.role_menu("garmin_manager")])
               and "hisobotlar" not in [c for c, _ in menus.EMPLOYEE] and menus.role_menu("londry") is None)
    check_true("employees' /hisobotlar only for a role with a team", "report_calendar.TEAM_OF.get(" in inspect.getsource(ops_manager))
    shown = inspect.getsource(ops_manager._show_today_report)
    check_true("/hisobot (one letter short) gives a manager with no report of his own the team calendar",
               "TEAM_OF.get(" in shown and "_send_report_calendar(" in shown
               and "report_calendar.COMMANDS + report_tools.REPORT_COMMANDS" in inspect.getsource(ops_manager))


def test_analyst_speed_and_style() -> None:
    """21.10.2026, the Director's SAP test: answers as replies, faster lookups, grouping, short clear answers."""
    print("analyst: speed, grouping, answer style (21.10.2026)")
    import asyncio
    import inspect
    import json
    import time
    import uuid
    from datetime import timezone

    from integrations.org_bot import analyst, old_stock, ops_manager

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    # ---- one round's lookups run together, and one kind is read from the database once per answer
    reads: list[str] = []

    async def fake_read(query, params=None, timeout="10s"):
        kind = (params or {}).get("k")
        reads.append(kind)
        rows = {"sales": [{"DocNum": 1, "DocDate": "2026-10-20", "CardName": "Hilton", "SlpCode": 3, "DocTotalSy": 5_000_000},
                          {"DocNum": 2, "DocDate": "2026-10-20", "CardName": "Hyatt", "SlpCode": 3, "DocTotalSy": 2_000_000},
                          {"DocNum": 3, "DocDate": "2026-10-19", "CardName": "Hyatt", "SlpCode": 7, "DocTotalSy": 1_500_000}],
                "sales_people": [{"SlpCode": 3, "SlpName": "Зунунов Абдукарим"}, {"SlpCode": 7, "SlpName": "Ёрқинов Рустам"}],
                "stock_items": [{"ItemCode": "010-02904-00", "ItemName": "fenix 8 47mm", "WhsCode": "05", "OnHand": 12},
                                {"ItemCode": "010-M2904-00", "ItemName": "Dummy Unit, fenix 8", "WhsCode": "05", "OnHand": 3}]}
        return [{"captured_at": datetime(2026, 10, 21, 8, 43, tzinfo=timezone.utc), "raw": r} for r in rows.get(kind, [])]

    async def freshness():
        return "SAP data is current.", False

    class TwoAtOnce:
        def __init__(self, *args, **kwargs):
            self.turn = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def chat(self, messages, tools=None):
            self.turn += 1
            if self.turn == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "slow_a", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "slow_b", "arguments": "{}"}},
                    {"id": "c", "type": "function", "function": {"name": "sap_records", "arguments": '{"kind": "sales"}'}},
                    {"id": "d", "type": "function", "function": {"name": "sap_records",
                                                                 "arguments": '{"kind": "sales", "search": "Hyatt"}'}}]}
            return {"role": "assistant", "content": "<b>7 000 000 сўм</b>"}

    async def slow(_args):
        await asyncio.sleep(0.4)
        return "ok"

    patch(analyst, "OpenRouterClient", TwoAtOnce)
    patch(analyst, "HANDLERS", {**analyst.HANDLERS, "slow_a": slow, "slow_b": slow})
    patch(analyst, "fetch_read_only", fake_read)
    patch(ops_manager, "sap_freshness", freshness)
    try:
        started = time.monotonic()
        result = asyncio.run(analyst.answer("кечаги сотув", "", uuid.uuid4()))
        took = time.monotonic() - started
    finally:
        restore()
    check_true("a round's lookups run together (two 0.4 s lookups in well under 0.8 s), results in the asked order",
               took < 0.7 and result.tools == ["slow_a", "slow_b", "sap_records", "sap_records"])
    check("one SAP kind is read from the database once per answer (the second lookup searches it in memory)",
          reads.count("sales"), 1)

    # ---- group by any field; a sales person's code shows with the name
    patch(analyst, "fetch_read_only", fake_read)
    patch(ops_manager, "sap_freshness", freshness)
    try:
        by_seller = asyncio.run(analyst.sap_records({"kind": "sales", "group_by": "SlpCode"}))
        stock = asyncio.run(analyst.sap_records({"kind": "stock_items", "search": "fenix 8"}))
        short = asyncio.run(analyst.sap_records({"kind": "sales", "fields": ["DocNum", "CardName"], "limit": 2}))
    finally:
        restore()
    check_true("sales per sales person — SAP CAN split it (the 21.10 answer said it couldn't)",
               "By SlpCode" in by_seller and "- 3 Зунунов Абдукарим: 2 rows, 7,000,000" in by_seller
               and "- 7 Ёрқинов Рустам: 1 rows, 1,500,000" in by_seller)
    check_true("display dummies aren't counted as stock (21.10: «54 дона, Dummy unit'лар ҳам киритилган»)",
               "OnHand: 12" in stock and "1 display dummies" in stock and "Dummy Unit" not in stock.split("Rows")[1])
    check_true("rows show only the fields asked (shorter, faster)", "DocNum=1, CardName=Hilton" in short
               and "DocTotalSy=" not in short.split("Rows")[1])
    check("amounts from 1 000 up are whole («340 408 750,11 сўм» on 21.10)",
          (analyst._amount(340_408_750.11), analyst._amount(54_528.97), analyst._amount(12.5)), ("340,408,750", "54,529", "12.5"))

    # ---- in stock, nothing sold in 90 days — any age
    stock_rows = [{"ItemCode": "A", "ItemName": "fenix 8 51mm", "WhsCode": "G.A._01", "OnHand": 3, "StockValue": 2242},
                  {"ItemCode": "B", "ItemName": "Forerunner 970", "WhsCode": "G.A._01", "OnHand": 2, "StockValue": 900},
                  {"ItemCode": "010-M1", "ItemName": "Dummy Unit", "WhsCode": "G.A._01", "OnHand": 1, "StockValue": 50}]
    idle = old_stock.idle(stock_rows, [{"ItemCode": "A", "LastPurDat": "2026-08-01"}],
                          [{"ItemCode": "B", "WhsCode": "G.A._01", "Quantity": 1}], [], date(2026, 10, 21))
    check("idle stock: on hand, nothing sold in 90 days, whatever its age; dummies out", [r.code for r in idle], ["A"])
    check_true("the analyst offers it (old_stock idle_90d)", "idle_90d" in json.dumps(analyst.TOOLS, ensure_ascii=False))

    # ---- short, clear answers
    prompt = analyst.ANALYST_SYSTEM_PROMPT
    check_true("answer rules: the answer first, at most 8 «• » lines, a titled block per question, whole so'm, "
               "one source line, no SAP field names / HTTP codes",
               all(x in prompt for x in ("First line: the answer itself", "at most 8 lines", "2–4 word <b>title</b>",
                                         "whole so'm", "<i>SAP · 21.10 13:43</i>", "OpenQty", "HTTP codes")))
    check_true("...and how to split totals (group_by), never «SAP can't split it»", "group_by" in prompt
               and "never say SAP can't split it" in prompt)
    check_true("the same rules for everyone the analyst answers (employees with data access, the Garmin manager)",
               "First line: the answer itself" in analyst.employee_prompt("Мурод", "Garmin менежер", {"garmin"}))

    # ---- replies: every answer under its question; files too
    src = inspect.getsource(ops_manager._dispatch_director_task)
    check_true("the Director's answers, questions back and refusals reply to his message",
               src.count("source_message_id") >= 6 and "reply_to=source_message_id" in src)
    check_true("a file (the Excel) also replies to the question",
               "reply_parameters" in load_source("integrations/telegram/bot.py")
               and "reply_to=reply_to" in inspect.getsource(ops_manager._send_files))

    # ---- what the gateway will send once its new src/ is copied
    js = load_source("sap-gateway/src/complete.js")
    check_true("gateway: customers' CreateDate («энг охирги қўшилган»); service calls with the technician's and "
               "assignee's names", 'C."CreateDate"' in js and '"TechnicianName"' in js and '"AssigneeName"' in js)


def test_report_media_and_auto() -> None:
    """22.10.2026: voice / video reports as text, photos kept and sent on request, days off grey, the report
    built from the day's messages when the button was forgotten."""
    print("report files, days off, reports from the day's messages (22.10.2026)")
    import asyncio
    import inspect
    from datetime import timezone

    from integrations.ai import openrouter_client as orc
    from integrations.org_bot import analyst, ops_manager, report_calendar, report_close, report_media, store
    from integrations.telegram import bot as tbot

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    async def none(*args, **kwargs):
        return None

    # ---- what a file is
    check("a photo: its largest size", report_media.file_of({"photo": [{"file_id": "s", "file_size": 10},
                                                                       {"file_id": "L", "file_size": 900}]}),
          {"kind": "photo", "file_id": "L", "mime": "image/jpeg", "size": 900})
    check("a voice note is OGG, a round video MP4",
          (report_media.file_of({"voice": {"file_id": "v"}})["mime"], report_media.file_of({"video_note": {"file_id": "n"}})["mime"]),
          ("audio/ogg", "video/mp4"))
    check("audio formats for the AI", [report_media.audio_format(m) for m in ("audio/ogg", "audio/mpeg", "audio/mp4", None)],
          ["ogg", "mp3", "m4a", "ogg"])
    check("the report's place for a file, and its line once it's text",
          (report_media.placeholder("photo", " омбор "), report_media.report_line("voice", "уч  та мижоз"),
           report_media.report_line("video", "-")),
          ("[расм] омбор", "[овозли хабар] уч та мижоз", "[видео: матнга айлантириб бўлмади]"))
    check("read by the AI: voice up to 20 MB, video up to 18 MB, never a photo or a document",
          [report_media.readable(k, n) for k, n in (("voice", 19_000_000), ("video", 19_000_000), ("video", 5_000_000),
                                                    ("photo", 10), ("document", 10))], [True, False, True, False, False])
    check_true("the prompts: write what was said / shown, add nothing, «-» when nothing",
               "Add nothing" in report_media.LISTEN_PROMPT and "no guesses" in report_media.WATCH_PROMPT
               and report_media.LISTEN_PROMPT.endswith("-") and report_media.WATCH_PROMPT.endswith("-"))

    # ---- audio and video reach the AI as OpenRouter's own parts
    sent: list = []

    async def fake_chat(self, messages, tools=None):
        sent.append(messages)
        return {"role": "assistant", "content": " матн "}

    patch(orc.OpenRouterClient, "chat", fake_chat)
    try:
        client = orc.OpenRouterClient()
        a = asyncio.run(client.media_text("s", "t", b"OggS", "audio", "ogg"))
        asyncio.run(client.media_text("s", "t", b"mp4", "video", "video/mp4"))
    finally:
        restore()
    audio_part, video_part = sent[0][1]["content"][1], sent[1][1]["content"][1]
    check_true("audio as input_audio (base64, format), video as a base64 data: URL",
               a == "матн" and audio_part == {"type": "input_audio", "input_audio": {"data": "T2dnUw==", "format": "ogg"}}
               and video_part["type"] == "video_url" and video_part["video_url"]["url"].startswith("data:video/mp4;base64,"))
    check_true("a longer wait for a video (180 s), the usual 60 s otherwise",
               orc.OpenRouterClient()._timeout == 60.0 and "timeout=180.0" in inspect.getsource(report_media.to_text))

    # ---- a kept file is sent again by its file_id
    calls: list = []

    async def fake_call(self, method, payload, **kwargs):
        calls.append((method, payload))
        return {"message_id": 5}

    patch(tbot.TelegramBot, "_call", fake_call)
    try:
        b = tbot.TelegramBot(bot_token="t")
        asyncio.run(b.send_media("voice", "V1", "9", "<b>Абдурашид</b>", reply_to=3))
        asyncio.run(b.send_media("video_note", "N1", "9", "x"))
    finally:
        restore()
    check_true("by its own method, with the caption, as a reply; a round video without a caption",
               calls[0][0] == "sendVoice" and calls[0][1]["voice"] == "V1" and calls[0][1]["reply_parameters"]["message_id"] == 3
               and calls[1][0] == "sendVideoNote" and "caption" not in calls[1][1])

    # ---- days off are grey, not red
    wed = date(2026, 10, 21)  # a Wednesday
    now = datetime(2026, 10, 22, 12, 0)
    abdurashid = {"id": "e1", "role": "garmin_sotuv", "full_name": "Хожиев Абдурашид", "work_days": "1101111"}
    missed_wed = {"employee_id": "e1", "report_date": wed, "status": "asked", "penalty_points": 1}
    view = report_calendar.build(date(2026, 10, 1), now, [abdurashid], [missed_wed])
    person = view["people"][0]
    check("Абдурашид's Wednesday off: grey, no miss, no minus (22.10)",
          (person.cells[wed.day - 1], person.missed, person.minus), ("none", 0, 0))
    check("a report sent on a day off still counts", report_calendar.cell({"status": "submitted"}, wed, now, False), "sent")
    check_true("the old reds removed once (asks on days off, unsent), marked so it never runs again",
               "isodow" in inspect.getsource(store.delete_missed_on_days_off)
               and "status = 'asked'" in inspect.getsource(store.delete_missed_on_days_off)
               and "CLEANUP_ACTION" in inspect.getsource(report_close.clear_days_off_once)
               and "clear_days_off_once" in load_source("integrations/api/app.py"))
    ran: list = []

    async def removed():
        ran.append(1)
        return 4

    for marker, expect in ((None, 4), ({"x": 1}, 0)):
        async def done(*args, _m=marker, **kwargs):
            return _m
        patch(report_close, "fetch_one", done)
        patch(store, "delete_missed_on_days_off", removed)
        patch(report_close, "log_action", none)
        try:
            check(f"cleanup {'runs once' if marker is None else 'never again'}", asyncio.run(report_close.clear_days_off_once()), expect)
        finally:
            restore()

    # ---- the button forgotten: the report from the day's messages
    at = datetime(2026, 10, 22, 13, 5, tzinfo=timezone.utc)
    chat = {"held": [{"id": "h1", "text": "Хилтонга 2 та машина ўрнатдик", "created_at": at}],
            "ai": [{"text": "нарх қанча?", "created_at": at}],
            "files": [{"id": "f1", "kind": "photo", "caption": "ўрнатилди", "created_at": at, "file_id": "P", "file_size": 9}]}
    lines = report_close.chat_lines(chat, {})
    check_true("everything unconfirmed, in time order, marked where it came from",
               len(lines) == 3 and "[unconfirmed message] Хилтонга 2 та машина" in lines[0] and "[file] [расм] ўрнатилди" in "".join(lines))
    marked, attached, told = [], [], []

    async def owed(day):
        return [{"id": "r9", "telegram_user_id": 7, "full_name": "Алишер Каримов", "address_form": "ака"}]

    async def day_chat(user, day):
        return chat

    async def compose(lines_, run_id):
        return "• Хилтонга 2 та машина ўрнатдим"

    async def mark(report_id, content):
        marked.append(content)
        return {"id": report_id}

    async def attach(ids, report_id):
        attached.extend(ids)

    class Bot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, **kwargs):
            told.append(text)

    for obj, name, value in ((store, "owed_reports", owed), (store, "day_chat", day_chat), (report_close, "_compose", compose),
                             (store, "mark_auto_report", mark), (store, "attach_held_files", attach),
                             (store, "set_file_text", none), (report_close, "TelegramBot", Bot), (report_close, "log_action", none)):
        patch(obj, name, value)
    try:
        built = asyncio.run(report_close.build_from_chat(date(2026, 10, 22)))
    finally:
        restore()
    check("built from the day's messages: sent, marked, its files kept with it", (built, marked, attached),
          (1, ["[чатдан тузилди] • Хилтонга 2 та машина ўрнатдим"], ["f1"]))
    check_true("...and the person is told politely, with the text", told and "тугмасини босмаган экансиз" in told[0]
               and "Хилтонга" in told[0] and friendly_problems(told[0].split("\n")[0], ("Алишер",)) == [])
    check_true("only their own facts — questions or chatter alone are no report",
               "nothing added or guessed" in report_close.COMPOSE_PROMPT and "NOT a report" in report_close.COMPOSE_PROMPT)
    close_src = inspect.getsource(report_close.close_day)
    check_true("22:00: first the reports from the day's messages, then the misses",
               close_src.index("build_from_chat") < close_src.index("expire_held_messages") < close_src.index("close_missed_reports"))

    # ---- one person's report, with its files, to whoever may see it
    people = [{"id": "e1", "telegram_user_id": 11, "role": "garmin_sotuv", "full_name": "Хожиев Абдурашид", "display_name": "k"},
              {"id": "e2", "telegram_user_id": 12, "role": "b2b_sotuv", "full_name": "Зунунов Абдукарим", "display_name": "z"}]

    async def actives():
        return people

    async def rows(query, params=None, timeout="10s"):
        return [{"id": "r1", "report_date": date(2026, 10, 22), "status": "submitted", "content": "[расм] витрина",
                 "submitted_at": at, "auto_built": False, "penalty_points": 0}]

    async def files(user, ids):
        return [{"id": "f1", "kind": "photo", "file_id": "P1", "for_report": "r1", "telegram_user_id": 11, "message_id": 4,
                 "created_at": at, "caption": "витрина", "text": None}]

    patch(store, "list_active_employees", actives)
    patch(analyst, "fetch_read_only", rows)
    patch(store, "report_files", files)
    try:
        queued: list = []
        analyst._media.set(queued)
        analyst._report_roles.set(None)
        boss = asyncio.run(analyst.employee_report({"name": "abdurashid", "date": "2026-10-22"}))
        analyst._report_roles.set(("garmin_sotuv",))
        manager = asyncio.run(analyst.employee_report({"name": "Абдукарим"}))
    finally:
        analyst._media.set(None)
        analyst._report_roles.set(None)
        restore()
    check_true("the Director asks for one person: only that row, and its files go after the answer",
               "Хожиев Абдурашид" in boss and "SENT" in boss and "files: 1 (photo)" in boss and [q["file_id"] for q in queued] == ["P1"]
               and queued[0]["who"] == "Хожиев Абдурашид")
    check_true("the Garmin manager: only his team (a B2B person isn't found)", manager.startswith("No employee") or "No in this" in manager)
    check_true("the Director and the Garmin manager get the files after the answer, by file_id or copied",
               "_send_media(director_id, run_id, result.media, reply_to)" in inspect.getsource(ops_manager._answer_question)
               and "_send_media(telegram_user_id, run_id, result.media, reply_to)" in load_source("integrations/org_bot/ops_manager.py")
               and "send_media" in inspect.getsource(ops_manager._send_media) and "copyMessage" in inspect.getsource(ops_manager._send_media))
    check_true("the analyst is told: one person's report → employee_report", "ONE person's report" in analyst.ANALYST_SYSTEM_PROMPT)
    schema = load_source("database/schema.sql")
    check_true("schema: files keep their file_id, report and text; reports the auto_built mark",
               all(x in schema for x in ("ADD COLUMN IF NOT EXISTS file_id TEXT", "ADD COLUMN IF NOT EXISTS report_id UUID",
                                          "ADD COLUMN IF NOT EXISTS text TEXT", "ADD COLUMN IF NOT EXISTS auto_built BOOLEAN")))


def test_explanations_and_start_times() -> None:
    """22.10.2026, the Boss: «AI сўрасин, тушунтириш хати ёзиб беришсин»; some people start at 10:00."""
    print("explanations, own start times (22.10.2026)")
    import asyncio
    import uuid
    from datetime import time as dtime

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import explanations as ex
    from integrations.org_bot import ops_manager, report_tools, store
    from integrations.verifix import attendance as att

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    # ---- own start time: late from 10:00, not Verifix's 09:00
    row = {"employee_id": 9, "employee_name": "Galimov Rushan", "days": [
        {"date": "21.10.2026", "day_kind": "W", "begin_time": "21.10.2026 09:00:00", "end_time": "21.10.2026 18:00:00",
         "input_time": "21.10.2026 10:41:00", "facts": []}]}
    usual = att.records([row], {}, grace=5)[0]
    own = att.records([row], {}, grace=5, starts={"9": dtime(10, 0)})[0]
    check("Галимов at 10:41: 101 min late on Verifix's 09:00, 41 on his own 10:00",
          (usual.status, usual.late_minutes, own.status, own.late_minutes), ("late", 101, "late", 41))
    check("...and on time when he starts at 11:00", att.records([row], {}, grace=5, starts={"9": dtime(11, 0)})[0].status, "on_time")
    check_true("the 08:00 brief and the bot's answers use them", "starts=await start_times()" in load_source("integrations/verifix/attendance.py"))

    # ---- who was absent, how many days in a row
    def rec(pid, day, status, working=True, name="Зунунов Абдукарим Абдирахманович"):
        r = att.DayRecord(employee_id=pid, name=name, job="", day=day, working=working)
        r.status = status
        return r

    y = date(2026, 10, 21)
    recs = [rec("1", date(2026, 10, 17), "on_time"), rec("1", date(2026, 10, 18), "off", working=False),
            rec("1", date(2026, 10, 19), "absent"), rec("1", date(2026, 10, 20), "absent"), rec("1", y, "absent"),
            rec("2", date(2026, 10, 20), "absent", name="Собитов Абдулбосит"), rec("2", y, "on_time", name="Собитов Абдулбосит"),
            rec("3", y, "flexible", name="Умаров Шухрат")]
    streaks = ex.absent_streaks(recs, y)
    check("absent yesterday: the working days in a row (a day off doesn't break it); not absent yesterday / эркин график: no ask",
          {k: v[1] for k, v in streaks.items()}, {"1": [date(2026, 10, 19), date(2026, 10, 20), y]})
    staff = [{"id": "e1", "full_name": "Абдукарим Зунунов", "telegram_user_id": 11, "role": "b2b_sotuv"},
             {"id": "e2", "full_name": "Abdulbosit Sobitov", "telegram_user_id": 12, "role": "it"},
             {"id": "e3", "full_name": "Абдукарим Зунунов", "telegram_user_id": 13, "role": "b2b_sotuv"}]
    check("Verifix name ↔ the bot's (any order, Latin or Cyrillic); two alike → nobody guessed",
          (ex.match_employee("Собитов Абдулбосит Мухаммад угли", staff)["id"],
           ex.match_employee("Зунунов Абдукарим Абдирахманович", staff), ex.match_employee("Нажотова Зайнабхон", staff)),
          ("e2", None, None))

    # ---- the questions: polite, one line, the days named
    one = ex.ask_text("absent", "Абдукарим ака", [y])
    three = ex.ask_text("absent", "Абдукарим ака", [date(2026, 10, 19), date(2026, 10, 20), y])
    report = ex.ask_text("report", "Ширин опа", [y])
    for text in (one, three, report):
        check(f"friendly: {text[:30]}…", friendly_problems(text, ("Абдукарим ака", "Ширин опа", "Verifix")), [])
    check_true("absent: yesterday's date, «if you did come, say so»; several days: how many and which",
               "21.10" in one and "келган бўлсангиз ҳам" in one and "охирги 3 иш куни (19.10, 20.10, 21.10)" in three
               and "ҳисоботингиз келмади" in report)
    card = ex.card("Абдукарим Зунунов", "B2B сотув", "absent", [date(2026, 10, 20), y], "касал эдим <врач>")
    check_true("the Director's card: who, which days, the words as written (escaped)",
               "Тушунтириш хати" in card and "20.10, 21.10 (2 иш куни)" in card and "&lt;врач&gt;" in card)
    summ = ex.summary(y, [{"name": "А", "kind": "absent", "answered_at": 1}, {"name": "Б", "kind": "report", "answered_at": None}])
    check_true("the next morning: asked / wrote / didn't, by name", "Сўралди: 2 · ёзди: 1 · ёзмади: 1" in summ and "Б (ҳисобот)" in summ)
    check("nobody asked: no summary", ex.summary(y, []), None)
    check("a command or a question («?») is never the explanation",
          [ex.is_explanation(t) for t in ("касал эдим", "/hisobot", "нарх қанча?", "")], [True, False, False, False])

    # ---- the morning: who is asked (the report ask isn't doubled for the absent)
    agent = load_agent("explanations")

    async def load(begin, end, run_id, agent_name, now=None):
        return recs

    async def missed(day):
        return [{"id": "e1", "full_name": "Абдукарим Зунунов", "telegram_user_id": 11, "role": "b2b_sotuv"},
                {"id": "e4", "full_name": "Ширин Умматова", "telegram_user_id": 14, "role": "garmin_sotuv"}]

    from integrations.common.config import settings
    from integrations.verifix import attendance as attendance_mod
    patch(attendance_mod, "load", load)
    patch(store, "missed_reports_of", missed)
    patch(type(settings), "verifix_configured", property(lambda self: True))
    try:
        people = [staff[0], staff[1], {"id": "e4", "full_name": "Ширин Умматова", "telegram_user_id": 14, "role": "garmin_sotuv"}]
        asks, unmatched = asyncio.run(agent.collect(y, people, uuid.uuid4()))
    finally:
        restore()
    check("absent → one ask with the days; a missed report → its own ask; absent + no report → one ask",
          sorted((a[0]["id"], a[1], len(a[2])) for a in asks), [("e1", "absent", 3), ("e4", "report", 1)])
    check_true("08:00, after the brief", load_source("scripts/run_morning_agents.py").index("agents/explanations")
               > load_source("scripts/run_morning_agents.py").index("ceo-daily-brief"))

    # ---- the answer: reply or plain message → the Director (and the Garmin manager for his team)
    employee = {"id": "e4", "telegram_user_id": 14, "role": "garmin_sotuv", "full_name": "Ширин Умматова", "address_form": "опа"}
    waiting = [{"id": "x1", "kind": "report", "days": [y], "asked_for": y, "prompt_message_id": 500}]
    sent, answered, replies, owed = [], [], [], {"row": None}
    verdict = {"kind": "reason"}
    typed: list = []

    async def open_(user, hours=36):
        return [] if answered else waiting

    async def answer(ids, text):
        answered.append((ids, text))
        return [{**waiting[0], "answer": text}]

    async def by_role(role):
        return {"operatsion_direktor": [{"telegram_user_id": 1}], "garmin_manager": [{"telegram_user_id": 2}]}.get(role, [])

    async def report_for_day(user, day):
        return owed["row"]

    async def classify(ask, text, report_ask, run_id):
        return verdict["kind"] if report_ask or verdict["kind"] != "report" else "reason"

    async def take_typed(employee, day, text, run_id):
        typed.append((day, text))
        return "late_report_sent"

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((text, reply_to))
        return [1]

    async def none(*a, **k):
        return None

    class Bot:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, **k):
            sent.append((chat_id, text))
            return [9]

    for obj, name, value in ((store, "open_explanations", open_), (store, "answer_explanations", answer),
                             (store, "active_employees_by_role", by_role), (store, "report_for_day", report_for_day),
                             (store, "set_explanation_delivered", none), (ops_manager, "_classify_answer", classify),
                             (ops_manager.late_report, "take_typed", take_typed),
                             (ops_manager, "_reply", fake_reply), (ops_manager, "TelegramBot", Bot), (ops_manager, "log_action", none)):
        patch(obj, name, value)
    try:
        owed["row"] = {"status": "asked"}
        check("a plain message while today's report is owed is the report's, not the explanation",
              asyncio.run(ops_manager._take_explanation(employee, "бугун 5 та сотув", None, uuid.uuid4())), None)
        check("...but a reply to the ask that gives the reason is",
              asyncio.run(ops_manager._take_explanation(employee, "интернет йўқ эди", 500, uuid.uuid4(), 77)), "explanation_taken")
        check_true("...to the Director and the Garmin manager (his team), and she is thanked under her message",
                   {c for c, _ in sent} == {"1", "2"} and "интернет йўқ эди" in sent[0][1] and replies[-1][1] == 77)
        answered.clear()
        sent.clear()
        owed["row"] = None
        verdict["kind"] = "other"
        check("a plain message the AI says isn't an answer goes on (to the AI chat)",
              asyncio.run(ops_manager._take_explanation(employee, "янги нархлар рўйхати керак", None, uuid.uuid4())), None)
        check("...a reply to the morning message about something else too (it may list tasks, 2026-10-24)",
              asyncio.run(ops_manager._take_explanation(employee, "T-0012 ни бажардим", 500, uuid.uuid4())), None)
        verdict["kind"] = "report"
        check("the missing report typed as the answer (no tap) goes for approval as the late report, not as a card",
              (asyncio.run(ops_manager._take_explanation(employee, "кеча 3 та мижоз, 2 та соат сотдим", 500, uuid.uuid4())),
               typed[-1][0] if typed else None, answered), ("late_report_sent", y, []))
        verdict["kind"] = "reason"
        check("a plain message that answers it is taken",
              asyncio.run(ops_manager._take_explanation(employee, "болам касал эди", None, uuid.uuid4())), "explanation_taken")
        check("a B2B person's goes to the Director only", ops_manager.explanation_managers("b2b_sotuv"), [])
    finally:
        restore()
    keyboard = report_tools.purpose_keyboard("f1", True, explanation=True)
    check_true("a file (a sick note, a voice) can be sent as the explanation while one is asked",
               "fp:f1:e" in str(keyboard) and report_tools.PURPOSES["e"] == "explanation"
               and "fp:f1:e" not in str(report_tools.purpose_keyboard("f1", True)))
    schema = load_source("database/schema.sql")
    check_true("schema: explanations (one per person, kind, day), attendance_starts, files' «explanation» purpose",
               "CREATE TABLE IF NOT EXISTS explanations" in schema and "uq_explanation UNIQUE (employee_id, kind, asked_for)" in schema
               and "CREATE TABLE IF NOT EXISTS attendance_starts" in schema and "'explanation'));" in schema)


def test_pulse() -> None:
    """22.10.2026, the Boss: PULSE — the company and every person's work as live charts, private, read-only."""
    print("PULSE dashboard (22.10.2026)")
    import asyncio
    import inspect
    import uuid
    from datetime import timezone

    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from integrations.api import app as api_app
    from integrations.api import pulse_page
    from integrations.common.config import settings
    from integrations.org_bot import ops_manager
    from integrations.pulse import access, data
    from integrations.pulse import score as sc
    from integrations.telegram import menus
    from integrations.verifix import attendance as att

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    # ---- the score
    check("attendance: on time 1, a little late 0.6, very late 0.3, absent 0, a day off / excused / эркин not counted",
          [sc.attendance_value(s, m) for s, m in (("on_time", 0), ("late", 20), ("late", 90), ("absent", 0), ("off", 0),
                                                  ("excused", 0), ("flexible", 0))], [1.0, 0.6, 0.3, 0.0, None, None, None])
    now = datetime(2026, 10, 22, 15, 0)
    d0, d1 = date(2026, 10, 21), date(2026, 10, 22)
    check("report: sent 1, put together from the chat 0.7, missed 0, still open today not counted",
          [sc.report_value(r, d, now) for r, d in (({"status": "submitted"}, d0), ({"status": "submitted", "auto_built": True}, d0),
                                                  ({"status": "asked"}, d0), ({"status": "asked"}, d1), (None, d0))],
          [1.0, 0.7, 0.0, None, None])
    tasks = [{"status": "done", "due_date": d0, "completed_at": datetime(2026, 10, 21, 12, 0), "created_at": datetime(2026, 10, 20)},
             {"status": "done", "due_date": date(2026, 10, 20), "completed_at": datetime(2026, 10, 21, 9, 0), "created_at": datetime(2026, 10, 19)},
             {"status": "started", "due_date": date(2026, 10, 19), "completed_at": None, "created_at": datetime(2026, 10, 18)},
             {"status": "cancelled", "due_date": d0, "completed_at": None, "created_at": datetime(2026, 10, 18)}]
    check("tasks in play on 21.10: done on time 1, done late 0.5, overdue still open 0, cancelled out → 0.5",
          sc.task_value(tasks, d0), 0.5)
    check("the day's score: only the parts that apply, weighted 40 / 30 / 30",
          (sc.day_score({"attendance": 1.0, "report": 0.0, "tasks": None}), sc.day_score({"attendance": None, "report": None, "tasks": None}),
           sc.day_score({"attendance": 0.6, "report": 1.0, "tasks": 0.5})), (57, None, 69))
    check("▲▼ in points; sales in %", (sc.delta(72.0, 68.5), sc.delta(130.0, 100.0, pct=True), sc.delta(None, 3.0)), (3.5, 30.0, None))

    # ---- the period, assembled
    staff = [data.Person("a", 1, "Ширин Умматова", "garmin_sotuv", "Garmin сотув", "garmin", "v1"),
             data.Person("b", 2, "Абдукарим Зунунов", "b2b_sotuv", "B2B сотув", "primus", "v2")]

    def rec(vid, day, status, late=0, arrived=None):
        r = att.DayRecord(employee_id=vid, name="x", job="", day=day, working=True, arrived=arrived)
        r.status, r.late_minutes = status, late
        return r

    recs = [rec("v1", d1, "on_time", arrived=datetime(2026, 10, 22, 8, 57)), rec("v2", d1, "late", 41, datetime(2026, 10, 22, 9, 41)),
            rec("v1", d0, "on_time"), rec("v2", d0, "absent")]
    dbrows = {"reports": [{"pid": "a", "report_date": d0, "status": "submitted", "auto_built": False},
                          {"pid": "b", "report_date": d0, "status": "asked", "auto_built": False}],
              "tasks": [{**tasks[0], "pid": "a", "id": "t1", "code": "T-1", "task_summary": "x"}],
              "complaints": [{"created_at": datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc), "ticket_status": "open", "place": "garmin"}]}
    o = data.assemble(staff, dbrows, recs, {d0: 5_000_000.0, d1: 7_000_000.0}, None, 7, now)
    check("the index: the company line (everyone's average) and SAP sales as volume",
          (o["index"]["series"][-2]["v"], o["index"]["volume"][-1]["v"]), (50.0, 7000000))
    check("six tickers; a source not read is «—», never 0",
          ([t["key"] for t in o["tickers"]], next(t for t in o["tickers"] if t["key"] == "shop")["value"]),
          (["sales", "shop", "tasks", "reports", "attendance", "complaints"], None))
    check_true("«Шикоят»: the number is the period's new complaints, like its ▲▼ (27.10)",
               '"value": now_p["complaints"], "open": open_now' in load_source("integrations/pulse/data.py"))
    check("the board: best first, today's arrival state", [(p["name"], p["today"]) for p in o["people"]],
          [("Ширин Умматова", "on_time"), ("Абдукарим Зунунов", "late")])
    check("sectors: one per business present", [s["label"] for s in o["sectors"]], ["Garmin", "Primus"])
    check_true("complaints: open now, red when they rise", next(t for t in o["tickers"] if t["key"] == "complaints")["value"] == 1
               and next(t for t in o["tickers"] if t["key"] == "complaints")["invert"])

    # ---- the tape and one person's day
    events = [{"at": datetime(2026, 10, 22, 10, 12, tzinfo=timezone.utc), "event": "accepted", "code": "T-1", "pid": "a"},
              {"at": datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc), "event": "created", "code": "T-9", "pid": "zz"}]
    tape_rows = data.tape_items(staff, recs, events, [{"pid": "a", "submitted_at": datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)}],
                                [], dbrows["complaints"], d1)
    check_true("a complaint's place by the business's name, never the raw key",
               next(r for r in tape_rows if r["kind"] == "complaint")["detail"] == "Garmin")
    check("the tape: newest first, only this scope's people, complaints too",
          [(r["t"], r["label"]) for r in tape_rows],
          [("16:00", "Ҳисобот"), ("15:12", "Бошлади"), ("11:00", "Шикоят келди"), ("09:41", "Келди"), ("08:57", "Келди")])
    day = data.timeline(d1, recs[1], [{"at": datetime(2026, 10, 22, 10, 12, tzinfo=timezone.utc), "event": "closed", "code": "T-1",
                                        "task_summary": "Hilton"}], {"status": "asked"}, [])
    check("one person's day: arrival (late), task steps, the missing report last",
          [(i["t"], i["label"], i["detail"]) for i in day], [("09:41", "Келди", "+41 дақ"), ("15:12", "Қабул қилинди", "T-1 Hilton"), ("", "Ҳисобот йўқ", "")])

    # ---- who may open it
    patch(settings, "ops_manager_bot_webhook_secret", SecretStr("s3cret"))
    patch(settings, "public_base_url", "https://example.test")
    try:
        link = access.link_token(77)
        check("a link token works once signed, for its kind only, not after it expires, not when changed",
              (access.verify(link, "link"), access.verify(link, "session"), access.verify(link, "link", now=10**12),
               access.verify(link[:-2] + "xx", "link")), (77, None, None, None))
        check_true("the link is the service's own address", access.link(77).startswith("https://example.test/pulse/enter?t="))
        roles = {77: "operatsion_direktor", 78: "garmin_manager", 79: "it"}

        async def who(query, params=None):
            role = roles.get(params[0])
            return {"role": role} if role else None

        async def fake_overview(days, scope):
            return {"days": days, "scope": scope}

        patch(pulse_page, "fetch_one", who)
        patch(data, "overview", fake_overview)
        client = TestClient(api_app.app)
        check("no session: the page says the link expired; the API says 401",
              (client.get("/pulse").status_code, client.get("/pulse/api/overview").status_code), (403, 401))
        entered = client.get(f"/pulse/enter?t={access.link_token(77)}", follow_redirects=False)
        cookie = entered.headers.get("set-cookie", "")
        check_true("the link opens a session: cookie HttpOnly, Secure, only for /pulse; the token leaves the address",
                   entered.status_code == 303 and entered.headers["location"] == "/pulse" and "HttpOnly" in cookie
                   and "Secure" in cookie and "Path=/pulse" in cookie)
        boss = TestClient(api_app.app, base_url="https://testserver")
        boss.cookies.set(access.COOKIE, access.session_token(77), path="/pulse")
        manager = TestClient(api_app.app, base_url="https://testserver")
        manager.cookies.set(access.COOKIE, access.session_token(78), path="/pulse")
        it = TestClient(api_app.app, base_url="https://testserver")
        it.cookies.set(access.COOKIE, access.session_token(79), path="/pulse")
        check("the Director sees everyone, the Garmin manager his team, IT nothing",
              (boss.get("/pulse/api/overview?days=7").json(), manager.get("/pulse/api/overview").json()["scope"],
               it.get("/pulse/api/overview").status_code),
              ({"days": 7, "scope": None}, ["garmin_sotuv"], 401))
        page = boss.get("/pulse")
        csp = page.headers.get("content-security-policy", "")
        check_true("the page: no outside scripts, no framing, nothing cached",
                   page.status_code == 200 and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
                   and page.headers.get("cache-control") == "no-store" and "http://" not in page.text
                   and page.text.count("https://") == page.text.count('href="https://www.tradingview.com/"') == 1)
        check("only the page's own files are served", (boss.get("/pulse/static/pulse.js").status_code,
                                                      boss.get("/pulse/static/../app.py").status_code,
                                                      boss.get("/pulse/static/lightweight-charts.4.2.0.js").status_code), (200, 404, 200))
        lib_res = boss.get("/pulse/static/lightweight-charts.4.2.0.js", headers={"Accept-Encoding": "gzip"})
        font_res = boss.get("/pulse/static/NotoSans-Regular.woff2")
        check_true("the library comes gzipped and kept a year; the fonts too",
                   lib_res.headers.get("content-encoding") == "gzip" and "immutable" in lib_res.headers.get("cache-control", "")
                   and font_res.status_code == 200 and font_res.headers.get("content-type") == "font/woff2"
                   and "immutable" in font_res.headers.get("cache-control", ""))
    finally:
        restore()
    lib = load_source("integrations/api/static/lightweight-charts.4.2.0.js")
    check_true("TradingView Lightweight Charts 4.2.0, vendored with its Apache-2.0 licence",
               "Lightweight Charts™ v4.2.0" in lib[:400] and "Apache License" in load_source("integrations/api/static/lightweight-charts.LICENSE.txt"))
    js = load_source("integrations/api/static/pulse.js")
    check_true("the page puts data in as text, never as HTML", ".innerHTML" not in js and "textContent" in js)
    check_true("review fixes: one score per person (the board's), a legend that follows the crosshair, the dot says "
               "what it means, pressed buttons not tabs, a flat wash",
               "row && row.score !== null ? row.score" in js and "subscribeCrosshairMove" in js and "Кечикди" in js
               and 'role="tab"' not in load_source("integrations/api/static/pulse.html") and 'bottomColor: "rgba(0,0,0,0)"' not in js)
    check_true("one person's day: attendance, report, tasks as numbers by their drawn icons (no words), "
               "following the crosshair", 'PARTS = [["attendance", "in"' in js and "pChart.subscribeCrosshairMove" in js
               and 'id="p-parts"' in load_source("integrations/api/static/pulse.html"))
    html = load_source("integrations/api/static/pulse.html")
    check_true("Uzbek Cyrillic, minimal words", latin_words(html.split("<body>")[1], allow={"PULSE", "TradingView", "B2B"}) == []
               and "<script>" not in html)

    # ---- 2026-10-23 (the owner): index and sales on two charts, explained; no businesses card; the tape's key; motion
    check_true("two charts: the index (its meaning in brackets, the score's parts) and daily sales; the businesses card gone",
               'id="chart"' in html and 'id="sales-chart"' in html and "(компаниянинг кунлик балли)" in html
               and "Давомат 40 + ҳисобот 30 + топшириқ 30" in html and 'id="mix"' not in html and "Бизнеслар" not in html
               and "renderMix" not in js and "setCrosshairPosition(" in js)
    check_true("no TradingView logo on the charts; the credit link its licence asks for instead, without a referrer",
               "attributionLogo: false" in js and 'rel="noopener noreferrer"' in html and "графиклари</footer>" in html)
    check_true("the tape explains its marks: a key behind ⓘ, every mark titled",
               'id="tape-key"' in html and 'aria-controls="tape-key"' in html and "badge.title = KEY[" in js
               and all(f"{k}:" in js.split("const KEY = {")[1].split("};")[0] for k in ("in", "late", "out", "task", "report", "complaint")))
    check_true("motion: lines rise left to right, numbers count up, cards and rows rise in — and all of it off for reduced motion",
               "function put(" in js and "autoscaleInfoProvider" in js and "function count(" in js
               and "prefers-reduced-motion: reduce" in js and "@keyframes rise" in load_source("integrations/api/static/pulse.css")
               and "prefers-reduced-motion: reduce" in load_source("integrations/api/static/pulse.css"))

    # ---- speed (2026-10-23): a kept answer at once, an old one rebuilt behind it; one Verifix read shared
    builds: list = []

    async def speed() -> tuple:
        box: dict = {}

        async def build():
            builds.append(1)
            await asyncio.sleep(0)
            return len(builds)

        first = await data._cached(box, "k", 60, build)
        again = await data._cached(box, "k", 60, build)
        box["k"] = (box["k"][0] - 120, box["k"][1])  # aged out
        stale = await data._cached(box, "k", 60, build)
        for _ in range(5):
            await asyncio.sleep(0)
        fresh = await data._cached(box, "k", 60, build)
        return first, again, stale, fresh, len(builds)

    check("the first ask waits; then the kept answer at once; an old one is answered at once and rebuilt behind it",
          asyncio.run(speed()), (1, 1, 1, 2, 2))
    loads: list = []

    async def fake_load(begin, end, run_id, agent, now=None):
        loads.append((begin, end))
        return [att.DayRecord(employee_id="v1", name="Ширин Умматова", job="", day=begin + timedelta(days=i), working=True,
                              status="on_time") for i in range((end - begin).days + 1)]

    patch(att, "load", fake_load)
    data._sources.clear()
    try:
        wide = asyncio.run(data._attendance(date(2026, 9, 1), date(2026, 10, 22), uuid.uuid4(), 600))
        one = asyncio.run(data._attendance(date(2026, 10, 22), date(2026, 10, 22), uuid.uuid4(), 600))
        month = asyncio.run(data._attendance(date(2026, 9, 23), date(2026, 10, 22), uuid.uuid4(), 600))
    finally:
        restore()
        data._sources.clear()
    check("Verifix read once for the 60 days; today's tape and a person's month cut from it",
          (len(loads), len(wide), [r.day for r in one], len(month)), (1, 52, [date(2026, 10, 22)], 30))
    builds_src = inspect.getsource(data._build_overview)
    check_true("the sources are read at the same time", "asyncio.gather(" in builds_src
               and "asyncio.gather(" in inspect.getsource(data._build_tape) and "asyncio.gather(" in inspect.getsource(data.person))
    app_src = load_source("integrations/api/app.py")
    check_true("gzip on; PULSE kept warm for the Director and the Garmin manager's team",
               "GZipMiddleware" in app_src and "pulse_data.keep_warm(" in app_src)
    css = load_source("integrations/api/static/pulse.css")
    fonts = [Path("integrations/api/static") / f"NotoSans-{w}.woff2" for w in ("Regular", "Bold")]
    check_true("fonts: Latin + Cyrillic WOFF2 subsets (< 60 KB each, ў қ ғ ҳ kept), never the 500 KB TTFs",
               all(f.exists() and f.stat().st_size < 60_000 for f in fonts) and ".ttf" not in css and ".woff2" in css
               and "woff2" in load_source("integrations/reports/fonts/LICENSE.md"))

    # ---- the bot: /pulse for the Director and the Garmin manager only
    check_true("/pulse in the Director's and the Garmin manager's ☰ menus, not the employees'",
               "pulse" in [c for c, _ in menus.DIRECTOR] and "pulse" in [c for c, _ in menus.GARMIN_MANAGER]
               and "pulse" not in [c for c, _ in menus.EMPLOYEE])
    sent: list = []

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        sent.append((text, reply_markup))
        return [1]

    async def none(*a, **k):
        return None

    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "log_action", none)
    patch(settings, "ops_manager_bot_webhook_secret", SecretStr("s3cret"))
    patch(settings, "public_base_url", "https://example.test")
    try:
        asyncio.run(ops_manager._send_pulse_link(77, uuid.uuid4()))
    finally:
        restore()
    check_true("the bot answers with a button to that person's own link",
               sent and sent[0][1]["inline_keyboard"][0][0]["url"].startswith("https://example.test/pulse/enter?t="))
    src = inspect.getsource(ops_manager._handle_employee_message)
    check_true("employees: only the Garmin manager's /pulse is answered", 'employee.get("role") == "garmin_manager" and text.lower() in PULSE_COMMANDS' in src)


def test_excused_and_late_reports() -> None:
    """23.10.2026, the Boss: a day with a reason turns grey (Director / Garmin manager); a missed report sent the
    next day goes for approval — approved, it turns green."""
    print("excused days and late reports (23.10.2026)")
    import asyncio
    import inspect
    import uuid

    from integrations.org_bot import excused, explanations, late_report, ops_manager, report_calendar, store
    from integrations.telegram import menus
    from integrations.verifix import attendance as att

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    # ---- who may: the Director anyone scored; the Garmin manager his team; nobody else
    boss, manager, it = {"role": "operatsion_direktor"}, {"role": "garmin_manager"}, {"role": "it"}
    shirin, b2b, gm = {"role": "garmin_sotuv"}, {"role": "b2b_sotuv"}, {"role": "garmin_manager"}
    check("may excuse: Director ✓✓, Garmin manager ✓✗, IT ✗, never the managers themselves",
          [excused.may_excuse(v, p) for v, p in ((boss, shirin), (boss, b2b), (manager, shirin), (manager, b2b), (it, shirin),
                                                 (boss, gm))], [True, True, True, False, False, False])

    # ---- Verifix: an excused absent / late day becomes «excused»; a day that came on time stays
    def rec(vid, name, day, status, late=0):
        return att.DayRecord(employee_id=vid, name=name, job="", day=day, working=True, status=status, late_minutes=late)

    d22, d21 = date(2026, 10, 22), date(2026, 10, 21)
    recs = [rec("v1", "Умматова Ширин Бахтиёровна", d22, "absent"), rec("v1", "Умматова Ширин Бахтиёровна", d21, "absent"),
            rec("v2", "Зунунов Абдукарим", d22, "late", 40), rec("v3", "Алиева Нодира", d22, "on_time")]
    rows = [{"id": "e1", "day": d22, "full_name": "Ширин Умматова", "display_name": "Ширин"},
            {"id": "e2", "day": d22, "full_name": "Абдукарим Зунунов", "display_name": None},
            {"id": "e3", "day": d22, "full_name": "Нодира Алиева", "display_name": None}]
    out = att.excuse(recs, rows)
    check("22.10 absent → excused (21.10 stays absent), late → excused with no minutes, on time stays",
          [(r.status, r.late_minutes) for r in out], [("excused", 0), ("absent", 0), ("excused", 0), ("on_time", 0)])
    check_true("…and it reads «раҳбар рухсати билан»; load() applies it", out[0].excuse == "раҳбар рухсати билан"
               and "excuse(recs, await excused_days(recs))" in inspect.getsource(att.load))

    # ---- the store: only an unsent ask is taken out (kept for ↩); a late report approved = sent, minus off, once
    sql = inspect.getsource(store.excuse_day)
    check_true("excusing takes out only an unsent ask, and keeps it for ↩",
               "status = 'asked'" in sql and "report_row" in sql and "INSERT INTO daily_reports" in inspect.getsource(store.unexcuse_day))
    sql = inspect.getsource(store.decide_late_report)
    check_true("approved: status submitted, its content, minus 0; declined stays asked; the first decision wins",
               "status = 'submitted'" in sql and "penalty_points = 0" in sql and "content = late_content" in sql
               and sql.count("late_decision IS NULL") == 2)
    schema = load_source("database/schema.sql")
    check_true("schema: excused_days and the late-report columns", "CREATE TABLE IF NOT EXISTS excused_days" in schema
               and "ADD COLUMN IF NOT EXISTS late_content" in schema and "ADD COLUMN IF NOT EXISTS late_decision" in schema)

    # ---- /sababli's day buttons
    person = {"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "role": "garmin_sotuv", "full_name": "Ширин Умматова"}
    text, kb = excused.days_view(person, {d22}, date(2026, 10, 23))
    days = [b for row in kb["inline_keyboard"][:-1] for b in row]
    check_true("two weeks back and two ahead (28 days), 22.10 marked ⬜, every button fits Telegram's 64 bytes",
               len(days) == 28 and any(b["text"] == "⬜ Па 22.10" for b in days) and "Сабабли: 22.10" in text
               and all(len(b["callback_data"].encode()) <= 64 for row in kb["inline_keyboard"] for b in row))
    check("the explanation card's button and its undo", (excused.card_keyboard("x", False)["inline_keyboard"][0][0]["callback_data"],
                                                        excused.card_keyboard("x", True)["inline_keyboard"][0][0]["callback_data"]),
          ("exc:x", "exu:x"))

    # ---- the flows, with a fake bot and store
    sent: list = []

    class FakeBot:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send_message(self, text, chat_id=None, reply_markup=None, **k):
            sent.append(("send", chat_id, text, reply_markup))
            return [1]

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            sent.append(("edit", chat_id, text, reply_markup))

        async def _answer_callback(self, qid, text):
            sent.append(("answer", qid, text, None))

        async def _call(self, method, payload, **k):
            sent.append((method, payload.get("chat_id"), "", payload.get("reply_markup")))

    people = {"m": {"id": "m", "telegram_user_id": 78, "role": "garmin_manager", "status": "active", "full_name": "Мурод Умаров"},
              "e1": {"id": "e1", "telegram_user_id": 11, "role": "garmin_sotuv", "status": "active", "full_name": "Ширин Умматова"},
              "b1": {"id": "b1", "telegram_user_id": 12, "role": "b2b_sotuv", "status": "active", "full_name": "Абдукарим Зунунов"},
              "d": {"id": "d", "telegram_user_id": 77, "role": "operatsion_direktor", "status": "active", "full_name": "Бош"}}
    by_tg = {p["telegram_user_id"]: p for p in people.values()}
    excused_set: set = set()
    calls: list = []

    async def get_by_tg(tg):
        return by_tg.get(tg)

    async def get_emp(eid):
        return people.get(eid)

    async def excuse_day(eid, day, by, reason=None):
        calls.append(("excuse", eid, day))
        if (eid, day) in excused_set:
            return False
        excused_set.add((eid, day))
        return True

    async def unexcuse_day(eid, day):
        calls.append(("unexcuse", eid, day))
        return bool(excused_set.discard((eid, day)) or True)

    async def days_of(eid, a, b):
        return {d for e, d in excused_set if e == eid}

    async def by_role(role):
        return [p for p in people.values() if p["role"] == role]

    async def explanation(xid):
        return {"id": xid, "employee_id": "e1", "days": [d21, d22], "answer": "туғилган кун"}

    async def nothing(*a, **k):
        return None

    import integrations.org_bot.excused as ex_mod
    import integrations.org_bot.late_report as lr_mod

    for mod in (ex_mod, lr_mod):
        patch(mod, "TelegramBot", FakeBot)
        patch(mod, "log_action", nothing)
        patch(mod, "today_local", lambda: date(2026, 10, 23))
    patch(store, "get_employee_by_telegram_id", get_by_tg)
    patch(store, "get_employee", get_emp)
    patch(store, "excuse_day", excuse_day)
    patch(store, "unexcuse_day", unexcuse_day)
    patch(store, "excused_days_of", days_of)
    patch(store, "active_employees_by_role", by_role)
    patch(store, "get_explanation", explanation)
    cb = lambda data, who: {"id": "q", "data": data, "from": {"id": who}, "message": {"chat": {"id": who}, "message_id": 5}}  # noqa: E731
    try:
        r1 = asyncio.run(excused.handle_callback("exd", "e1:20261022", cb("exd:e1:20261022", 78), uuid.uuid4()))
        told = [x for x in sent if x[0] == "send" and x[1] == "77"]
        r2 = asyncio.run(excused.handle_callback("exd", "b1:20261022", cb("exd:b1:20261022", 78), uuid.uuid4()))
        r3 = asyncio.run(excused.handle_callback("exd", "e1:20260901", cb("exd:e1:20260901", 78), uuid.uuid4()))
        r4 = asyncio.run(excused.handle_callback("exc", "x1", cb("exc:x1", 77), uuid.uuid4()))
        swapped = [x for x in sent if x[0] == "editMessageReplyMarkup"]
        r5 = asyncio.run(excused.handle_callback("exd", "e1:20261022", cb("exd:e1:20261022", 11), uuid.uuid4()))
    finally:
        pass
    check("the Garmin manager excuses his person's day; not a B2B person's, not a day out of the window; the Director's "
          "card button excuses the card's days; an employee can't", (r1, r2, r3, r4, r5),
          ("excused_set", "excused_unauthorized", "excused_unauthorized", "excused_set", "excused_unauthorized"))
    check_true("the Director is told when the Garmin manager sets one; the card's button turns into ↩",
               bool(told) and "22.10 сабабли деб белгилади" in told[0][2]
               and swapped and swapped[-1][3]["inline_keyboard"][0][0]["callback_data"] == "exu:x1"
               and ("excuse", "e1", d21) in calls)

    # ---- the late report: the tap, the text, the card, the decision
    sent.clear()
    state: dict = {}

    async def await_late(tg, day):
        state["awaiting"] = (tg, day)
        return {"id": "r1"} if day == d22 else None

    async def awaiting(tg, hours):
        return {"id": "r1"} if state.get("awaiting") else None

    async def save(rid, content):
        state["content"] = content
        return {"id": "r1", "report_date": d22, "late_sent_at": None}

    async def open_asks(eid, day):
        return [{"id": "x9"}]

    async def answer(ids, text):
        state["answered"] = (ids, text)
        return []

    async def get_report(rid):
        return {"id": rid, "employee_id": "e1", "report_date": d22}

    decided: list = []

    async def decide(rid, approve, by):
        if decided:
            return None
        decided.append(approve)
        return {"id": rid, "report_date": d22, "late_sent_at": None, "late_content": state.get("content")}

    patch(store, "await_late_report", await_late)
    patch(store, "late_report_awaiting", awaiting)
    patch(store, "save_late_report", save)
    patch(store, "open_report_explanations", open_asks)
    patch(store, "answer_explanations", answer)
    patch(store, "get_report", get_report)
    patch(store, "decide_late_report", decide)
    try:
        t1 = asyncio.run(late_report.handle_callback("lrs", "20261022", cb("lrs:20261022", 11), uuid.uuid4()))
        t0 = asyncio.run(late_report.handle_callback("lrs", "20261020", cb("lrs:20261020", 11), uuid.uuid4()))
        t2 = asyncio.run(late_report.take_message(people["e1"], {"text": "мижоз билан учрашдим, 2 та сотув"}, uuid.uuid4()))
        cards = [x for x in sent if x[0] == "send" and x[3] and "lrok:r1" in str(x[3])]
        t3 = asyncio.run(late_report.handle_callback("lrok", "r1", cb("lrok:r1", 12), uuid.uuid4()))
        t4 = asyncio.run(late_report.handle_callback("lrok", "r1", cb("lrok:r1", 78), uuid.uuid4()))
        t5 = asyncio.run(late_report.handle_callback("lrno", "r1", cb("lrno:r1", 77), uuid.uuid4()))
    finally:
        restore()
    check("tap (yesterday only) → text → to the Director and the Garmin manager → another employee can't decide → "
          "the Garmin manager approves → a second decision is refused", (t1, t0, t2, t3, t4, t5),
          ("late_report_asked", "late_closed", "late_report_sent", "late_unauthorized", "late_report_approved",
           "late_already_decided"))
    check_true("the card goes to both deciders with ✅ / ❌; the morning ask is answered by it; the person is told",
               sorted(x[1] for x in cards) == ["77", "78"] and "Кечиккан ҳисобот" in cards[0][2]
               and state["answered"] == (["x9"], "[кечиккан ҳисобот юборилди]")
               and any(x[1] == "11" and "тасдиқланди" in x[2] for x in sent))
    for text in (late_report.prompt_text(d22), late_report.sent_text(), late_report.decided_text(d22, True),
                 late_report.decided_text(d22, False), explanations.ask_text("report", "Ширин", [d22])):
        check(f"friendly: {text[:30]}…", friendly_problems(text, ("Ширин",)), [])

    # ---- the wiring
    src = inspect.getsource(ops_manager)
    check_true("the bot routes both flows' buttons and /sababli, takes the late report's text, and puts ⬜ on every card",
               "excused.handle_callback(" in src and "late_report.handle_callback(" in src and "excused.handle_message(" in src
               and "late_report.take_message(" in src and "excused.card_keyboard(" in src and '"reply_markup": buttons' in src)
    check_true("the morning «no report» question carries the send-now button; no report is asked on an excused day",
               "late_report.ask_button(days[-1])" in load_source("integrations/org_bot/morning_notes.py")
               and "excused_between(day, day)" in load_source("agents/daily-reports/agent.py"))
    check_true("/sababli in the Director's and the Garmin manager's menus", "sababli" in [c for c, _ in menus.DIRECTOR]
               and "sababli" in [c for c, _ in menus.GARMIN_MANAGER] and "sababli" not in [c for c, _ in menus.EMPLOYEE])
    check_true("the calendar shows a removed ask as grey", report_calendar.cell(None, d22, datetime(2026, 10, 23, 9), True) == "none")


def test_morning_verifix_and_admin_sheet() -> None:
    """24.10.2026, the owner: one morning message per person (one question at most), Verifix days without data,
    the admin's report sheet (green / red / grey), and the bugs the review of 22–23.10 found."""
    print("morning message, Verifix no-data days, admin report sheet (24.10.2026)")
    import asyncio
    import inspect
    import uuid

    from integrations.common.config import settings
    from integrations.org_bot import (admin, agenda, ai_chat, analyst, excused, explanations, kpi, late_report,
                                      morning_notes, report_calendar, report_media, report_sheet, store, task_tracker)
    from integrations.pulse import data as pulse_data
    from integrations.pulse import score as pulse_score
    from integrations.verifix import attendance as att

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    d23, d24 = date(2026, 10, 23), date(2026, 10, 24)

    # ---- one morning message per person, at most one question (morning_notes.compose)
    missed = {"kind": "bonus", "detail": {"report_date": "2026-10-23", "points": 0, "status": "asked", "per": 1}}
    plus = {"kind": "bonus", "detail": {"report_date": "2026-10-23", "points": 1, "month": 5, "status": "submitted", "per": 1}}
    ask_report = {"kind": "ask", "detail": {"kind": "report", "days": ["2026-10-23"], "ids": ["x1"]}}
    ask_absent = {"kind": "ask", "detail": {"kind": "absent", "days": ["2026-10-23"], "ids": ["x2"]}}
    task_due = {"kind": "task", "detail": {"code": "T-2026-10-0012", "summary": "Artel учун нарх юбориш",
                                           "due": "2026-10-24", "today": "2026-10-24", "overdue": False}}
    task_late = {"kind": "task", "detail": {"code": "T-2026-10-0009", "summary": "Склад рўйхати", "due": "2026-10-22",
                                            "today": "2026-10-24", "overdue": True}}
    text, keyboard, ids = morning_notes.compose("Ширин опа", [missed, ask_report])
    check("a missed report: ONE message — «no point» said inside the question, never twice; 📝 under it",
          (text.count("балл берилмади"), "\n" in text, "lrs:20261023" in str(keyboard), ids), (1, False, True, ["x1"]))
    check("...in the friendly voice", friendly_problems(text, ("Ширин опа",)), [])
    text, keyboard, ids = morning_notes.compose("Абдукарим ака", [ask_absent, plus, task_due, task_late])
    lines = text.split("\n")
    check_true("several things: a greeting, the facts as lines (the point first, then tasks — codes and names as "
               "written), and the one question last, alone",
               lines[0] == "Абдукарим ака, хайрли тонг" and lines[1].startswith("• кечаги, 23.10 ҳисоботингиз учун")
               and "T-2026-10-0012 «Artel учун нарх юбориш» топшириғининг муддати бугун" in text
               and "муддати ўтди (22.10), директорга хабар берилди" in text and "«Бажардим» тугмасини" in text
               and lines[-1].startswith("Verifix бўйича кеча, 23.10") and text.count("?") == 0 and keyboard is None
               and ids == ["x2"])
    check("one fact alone: one polite line with the name, as before",
          morning_notes.compose("Ширин опа", [plus])[0],
          "Ширин опа, кечаги, 23.10 ҳисоботингиз учун раҳмат каттакон, KPI баллингизга +1 балл қўшилди, шу ойда жами +5 балл")
    check_true("no emoji to employees, polite «сиз» in every line",
               all(not __import__("integrations.org_bot.tone", fromlist=["x"])._EMOJI.search(t)
                   for t in (morning_notes.compose("А", n)[0] for n in ([missed, ask_report], [ask_absent, plus, task_due])))
               and all(__import__("integrations.org_bot.tone", fromlist=["x"]).is_polite(morning_notes.compose("А", n)[0])
                       for n in ([missed, ask_report], [ask_absent, plus, task_due, task_late])))

    # ---- the producers leave notes; one sender; the order of the 08:00 run

    bonus_src, expl_src = load_source("agents/report-bonus/agent.py"), load_source("agents/explanations/agent.py")
    tasks_src, runner = load_source("agents/task-tracker/agent.py"), load_source("scripts/run_morning_agents.py")
    check_true("the bonus, the question and task reminders are notes now — none of them sends on its own",
               'add_morning_note(row, today_local(), "bonus"' in bonus_src and "send_message" not in bonus_src
               and 'add_morning_note(person, today, "ask"' in expl_src and "explanations.ask_text(" not in expl_src
               and tasks_src.count('add_morning_note(_who(task), today, "task"') == 2
               and "reminder_text(" not in tasks_src and "overdue_employee_text(" not in tasks_src)
    order = [runner.index(a) for a in ("agents/report-bonus", "agents/explanations", "agents/task-tracker/agent.py --morning",
                                       "agents/morning-notes")]
    check_true("08:00: bonus → questions → task reminders → ONE message each (task-tracker --morning runs once)",
               order == sorted(order) and runner.count("agents/task-tracker/agent.py --morning") == 1)
    check_true("Verifix down no longer stops the report questions (the 22.10 agent crashed on it)",
               "except Exception as exc:  # noqa: BLE001 — 2026-10-24: Verifix down" in expl_src)
    check_true("the API sends what the 08:00 job left (30 min) and the sheet's untold changes at 22:00",
               "_morning_notes_left" in load_source("integrations/api/app.py")
               and morning_notes.FLUSH_AFTER_MINUTES == 30)

    claimed = [
        {"id": "n1", "employee_id": "e1", "telegram_user_id": 11, "kind": "bonus", "detail": missed["detail"],
         "full_name": "Ширин Умматова", "address_form": "opa", "employee_status": "active"},
        {"id": "n2", "employee_id": "e1", "telegram_user_id": 11, "kind": "ask", "detail": ask_report["detail"],
         "full_name": "Ширин Умматова", "address_form": "opa", "employee_status": "active"},
        {"id": "n3", "employee_id": "e2", "telegram_user_id": 12, "kind": "task", "detail": task_due["detail"],
         "full_name": "Абдукарим Зунунов", "address_form": "aka", "employee_status": "active"},
        {"id": "n4", "employee_id": "e3", "telegram_user_id": 13, "kind": "bonus", "detail": plus["detail"],
         "full_name": "Кетган Одам", "employee_status": "revoked"},
    ]
    outbox, prompts, marked = [], [], []

    class Bot:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, reply_markup=None, **k):
            outbox.append((chat_id, text, reply_markup))
            return [100 + len(outbox)]

        async def _call(self, method, payload, **k):
            outbox.append((method, payload.get("chat_id"), payload.get("reply_markup")))
            return {}

        async def _edit_message(self, chat_id=None, message_id=None, text="", reply_markup=None):
            outbox.append(("edit", chat_id, text, reply_markup))

        async def _answer_callback(self, qid, text):
            outbox.append(("answer", qid, text, None))

    async def claim(day, older=0):
        return claimed

    async def set_msg(ids, message_id):
        marked.append((tuple(ids), message_id))

    async def set_prompt(xid, mid):
        prompts.append((xid, mid))

    async def nothing(*a, **k):
        return None

    async def alive(ids):
        return set(ids)

    patch(store, "claim_morning_notes", claim)
    patch(store, "unanswered_explanation_ids", alive)
    patch(store, "set_morning_notes_message", set_msg)
    patch(store, "set_explanation_prompt", set_prompt)
    patch(morning_notes, "TelegramBot", Bot)
    patch(morning_notes, "log_action", nothing)
    patch(settings, "bots_frozen", False)
    try:
        counts = asyncio.run(morning_notes.send_day(d24, uuid.uuid4()))
    finally:
        restore()
    check("each active person gets one message; someone removed gets none", (counts, len(outbox)),
          ({"told": 2, "failed": 0, "skipped": 1}, 2))
    check_true("the question's message is the one a reply answers (prompt = that message)",
               prompts == [("x1", 101)] and (("n1", "n2"), 101) in marked and "lrs:20261023" in str(outbox[0][2]))

    async def withdrawn(ids):
        return set()

    outbox.clear()
    patch(store, "claim_morning_notes", claim)
    patch(store, "unanswered_explanation_ids", withdrawn)
    patch(store, "set_morning_notes_message", set_msg)
    patch(store, "set_explanation_prompt", set_prompt)
    patch(morning_notes, "TelegramBot", Bot)
    patch(morning_notes, "log_action", nothing)
    try:
        asyncio.run(morning_notes.send_day(d24, uuid.uuid4()))
    finally:
        restore()
    check_true("a question withdrawn before the message went out isn't asked in it (the point line stays)",
               "балл берилмади" in outbox[0][1] and "сабабини" not in outbox[0][1] and outbox[0][2] is None)

    # ---- what the bot waits for from one person: given to the work AI (agenda.py)
    async def open_x(tg, hours=36):
        return [{"kind": "report", "days": [d23]}]

    async def late_wait(tg, hours):
        return None

    async def today_row(tg, day):
        return {"status": "asked"}

    patch(store, "open_explanations", open_x)
    patch(store, "late_report_awaiting", late_wait)
    patch(store, "report_for_day", today_row)
    patch(agenda, "now_local", lambda: datetime(2026, 10, 24, 17, 0))
    try:
        waiting = asyncio.run(agenda.pending({"telegram_user_id": 11}))
    finally:
        restore()
    context = ai_chat.work_context({"responsibilities": ""}, [], d24, waiting)
    check_true("the work AI knows the open question (and the 📝 way), today's owed report, and to ask for nothing else",
               "report of 23.10 never came" in context and "Кечаги ҳисоботни юбориш" in context
               and "Today's daily report: asked at 16:00" in context and "ask for nothing else" in context
               and "nothing (no open question" in ai_chat.work_context({}, [], d24, []))
    check_true("…wired into the work AI's call", "ai_chat.work_context(employee, tasks, today_local(), waiting)"
               in load_source("integrations/org_bot/ops_manager.py"))

    # ---- Verifix days without data
    def rec(vid, day, status, arrived=None):
        return att.DayRecord(employee_id=vid, name=f"Ходим {vid}", job="", day=day, working=True, status=status,
                             arrived=arrived)

    t = datetime(2026, 10, 22, 9, 30)
    dead = [rec(str(i), date(2026, 10, 22), "absent") for i in range(6)] + [rec("9", date(2026, 10, 22), "on_time", t)]
    fine = [rec(str(i), date(2026, 10, 21), "on_time", t) for i in range(4)] + [rec("8", date(2026, 10, 21), "absent")]
    check("a day with 1 of 7 arrivals is the terminal's fault; 4 of 5 is a normal day; today isn't judged yet",
          (att.broken_days(dead + fine, d24), att.broken_days(dead, date(2026, 10, 22))), ({date(2026, 10, 22)}, set()))
    marked_recs = att.mark_no_data(dead + fine, {date(2026, 10, 22)})
    check("that day nobody is absent or late: «no data»; who did come stays; the other day is untouched",
          sorted({(r.day.day, r.status) for r in marked_recs}), [(21, "absent"), (21, "on_time"), (22, "no_data"), (22, "on_time")])
    summary = att.summarize(marked_recs, date(2026, 10, 22))
    block = att.render_day(summary)
    check_true("the brief says Verifix didn't record them — never «келмади»",
               len(summary.no_data) == 6 and not summary.absent and "Verifix 6 кишининг келишини ёзмаган" in block
               and "келмади ёки кечикди деб ҳисобланмади" in block and "6 киши келмади" not in block
               and "Verifix ёзганларнинг ҳаммаси ўз вақтида келди — 1 киши" in block)
    check("no «why weren't you at work?» for a no-data day", explanations.absent_streaks(marked_recs, date(2026, 10, 22)), {})
    check("PULSE doesn't count it", pulse_score.attendance_value("no_data"), None)
    check_true("KPI doesn't count it as a working day; IT's marks and the automatic ones both apply in load()",
               '"excused", "no_data"' in load_source("integrations/org_bot/kpi_flow.py")
               and "mark_no_data(recs, bad | await marked_bad_days(recs))" in inspect.getsource(att.load))
    check("counts only for IT: per day judged / came", att.day_stats(dead), {date(2026, 10, 22): (7, 1)})
    text, kb = admin.verifix_days_view({d24: (12, 9), d23: (12, 1)}, {d23}, d24, {d23})
    check_true("/verifix days: counts, the automatic flag, a ⛔ / ✅ button per day — no names",
               "9 / 12" in text and "1 / 12" in text and "жуда кам" in text and "Ходим" not in text
               and "⛔ 23.10" in str(kb) and "✅ 24.10" in str(kb) and "vbad:2026-10-23" in str(kb))

    sorry, told = [], []

    async def toggle(day, by):
        return True

    async def withdraw(day):
        return [{"telegram_user_id": 21, "full_name": "Ширин Умматова", "address_form": "opa"}]

    async def bosses(role):
        return [{"telegram_user_id": 1}] if role == "operatsion_direktor" else []

    async def bad_days(a, b):
        return {d23}

    outbox.clear()
    patch(store, "toggle_verifix_bad_day", toggle)
    patch(store, "withdraw_absent_asks", withdraw)
    patch(store, "active_employees_by_role", bosses)
    patch(store, "verifix_bad_days", bad_days)
    patch(admin, "TelegramBot", Bot)
    patch(admin, "today_local", lambda: d24)
    patch(admin, "log_action", nothing)
    patch(admin, "_answer", nothing)
    try:
        outcome = asyncio.run(admin._toggle_verifix_day(
            "2026-10-23", "q", "it", {"message": {"message_id": 5, "chat": {"id": 99}}}, uuid.uuid4()))
    finally:
        restore()
    sorry = [x for x in outbox if x[0] == "21"]
    told = [x for x in outbox if x[0] == "1"]
    check_true("IT marks 23.10: the question about it is withdrawn with an apology, the Director is told, the buttons change",
               outcome == "verifix_day_marked" and len(sorry) == 1 and "кечирасиз" in sorry[0][1]
               and friendly_problems(sorry[0][1], ("Ширин опа", "Verifix")) == []
               and told and "IT белгилади" in told[0][1] and "1 кишидан узр сўралди" in told[0][1]
               and any(x[0] == "editMessageReplyMarkup" for x in outbox))
    check_true("a withdrawn question isn't «didn't write» in the Director's summary",
               explanations.summary(d23, [{"name": "А", "kind": "absent", "answered_at": datetime(2026, 10, 24),
                                           "answer": "[Verifix ишламади — савол бекор қилинди]"}]) is None)
    check_true("IT's 08:00 report says when the terminals didn't record (counts only)",
               "attendance.broken_days(recs, today_local())" in load_source("integrations/org_bot/tech_report.py"))

    # ---- an excused day keeps its report row: status 'excused' (nothing deleted), left out of every count
    sql = inspect.getsource(store.excuse_day)
    check_true("excusing turns the unsent ask 'excused' (its text kept, its minus 0) — no DELETE any more",
               "SET status = 'excused', penalty_points = 0" in sql and "DELETE FROM daily_reports" not in sql
               and "'kept': True" not in sql and '"kept": True' in sql)
    sql = inspect.getsource(store.unexcuse_day)
    check_true("↩ puts it back as it was; a 23.10 mark (the ask deleted then) is put back from its copy",
               "SET status = 'asked', penalty_points = %s" in sql and 'not row.get("kept")' in sql)
    for fn in (store.reports_between, store.recent_reports, store.team_reports, store.kpi_month_rows,
               store.reports_between_rows, store.settle_report_bonus):
        check_true(f"an excused day isn't an ask: {fn.__name__}", "status <> 'excused'" in inspect.getsource(fn))
    check_true("…nor in PULSE, the calendar or the KPI score",
               "status <> 'excused'" in inspect.getsource(pulse_data._db)
               and report_calendar.cell({"status": "excused"}, d23, datetime(2026, 10, 24, 9)) == "none"
               and pulse_score.report_value({"status": "excused"}, d23, datetime(2026, 10, 24, 9)) is None
               and task_tracker.score_reports([{"status": "excused", "report_date": d23}]).asked == 0)
    check_true("schema: 'excused' allowed; verifix_bad_days, report_cell_edits, morning_notes",
               "CHECK (status IN ('asked', 'submitted', 'excused'))" in load_source("database/schema.sql")
               and all(f"CREATE TABLE IF NOT EXISTS {t}" in load_source("database/schema.sql")
                       for t in ("verifix_bad_days", "report_cell_edits", "morning_notes")))

    # ---- the admin's report sheet
    now = datetime(2026, 10, 24, 12, 0)
    check("what a day shows", [report_sheet.state(r, x, d, now) for r, x, d in (
        ({"status": "submitted"}, False, d23), ({"status": "asked"}, False, d23), ({"status": "asked"}, False, d24),
        (None, True, d23), ({"status": "excused"}, False, d23), (None, False, d23), (None, False, date(2026, 10, 30)))],
          ["sent", "missed", "open", "excused", "excused", "none", "future"])
    check("the wheel: grey → green → red → grey (excused) → green",
          [report_sheet.next_target(s) for s in ("none", "sent", "missed", "open", "excused")],
          ["sent", "missed", "excused", "excused", "sent"])
    emp = {"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "telegram_user_id": 11, "role": "garmin_sotuv",
           "full_name": "Ширин Умматова", "status": "active"}
    text, kb = report_sheet.sheet_view(emp, d24, [{"report_date": d23, "status": "asked"},
                                                  {"report_date": date(2026, 10, 22), "status": "submitted"}],
                                       {date(2026, 10, 20)}, now)
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    check_true("October as a calendar: weekday heads, 31 days, ◀ but no ▶ (this month), ✅ Тайёр, back to the card; "
               "every button within Telegram's 64 bytes",
               [b["text"] for b in kb["inline_keyboard"][0]] == list(report_calendar.WEEKDAYS)
               and sum(1 for b in buttons if b["callback_data"].startswith("rsd:")) == 31
               and "🟥23" in str(kb) and "🟩22" in str(kb) and "⬜20" in str(kb) and "🟨24" not in str(kb)
               and "▶️" not in str(kb) and "rsok:" in str(kb) and f"emp:{emp['id']}" in str(kb)
               and all(len(b["callback_data"].encode()) <= 64 for b in buttons)
               and "🟩 1 · 🟥 1 · ⬜ сабабли 1" in text)
    calls: list = []

    async def rec_call(name):
        async def f(*a, **k):
            calls.append((name, a[1] if len(a) > 1 else None, a[2:] if len(a) > 2 else ()))
            return True
        return f

    for name in ("excuse_day", "unexcuse_day", "set_report_sent", "set_report_missed"):
        patch(store, name, asyncio.run(rec_call(name)))
    try:
        asyncio.run(report_sheet.apply(emp, d23, "sent", 7, now))
        asyncio.run(report_sheet.apply(emp, d23, "missed", 7, now))
        asyncio.run(report_sheet.apply(emp, d24, "missed", 7, now))
        asyncio.run(report_sheet.apply(emp, d23, "excused", 7, now))
    finally:
        restore()
    check("green: the mark off, sent; red: the minus on a closed day, none today before 22:00; grey: excused",
          [(c[0], c[2][-2:] if c[0] == "set_report_missed" else ()) for c in calls],
          [("unexcuse_day", ()), ("set_report_sent", ()), ("unexcuse_day", ()), ("set_report_missed", (1, True)),
           ("unexcuse_day", ()), ("set_report_missed", (1, False)), ("excuse_day", ())])
    check_true("…an open day's minus is 0 in the store (it closes at 22:00 as any other)",
               '"p": points if closed else 0' in inspect.getsource(store.set_report_missed))
    edits = [{"employee_id": "e1", "full_name": "Ширин Умматова", "day": d23, "before": "missed", "after": "excused"},
             {"employee_id": "e1", "full_name": "Ширин Умматова", "day": d23, "before": "excused", "after": "sent"},
             {"employee_id": "e1", "full_name": "Ширин Умматова", "day": date(2026, 10, 21), "before": "sent", "after": "missed"},
             {"employee_id": "e1", "full_name": "Ширин Умматова", "day": date(2026, 10, 21), "before": "missed", "after": "excused"},
             {"employee_id": "e1", "full_name": "Ширин Умматова", "day": date(2026, 10, 21), "before": "excused", "after": "sent"}]
    summary = report_sheet.summary_text(edits)
    check_true("the Director's summary: per person, days grouped by change, in words; a day turned back isn't listed",
               summary is not None and "<b>Ширин Умматова</b>\n• юборилмади (−1 балл) → юборилди: 23.10" in summary
               and "21.10" not in summary and "🟥" not in summary)
    many = [{"employee_id": "e9", "full_name": "Умар Хошимов", "day": date(2026, 10, d), "before": "missed",
             "after": "excused"} for d in (14, 16, 18)] + [
        {"employee_id": "e9", "full_name": "Умар Хошимов", "day": date(2026, 10, 17), "before": "none", "after": "excused"}]
    check("…many days: one line per change, the note once",
          report_sheet.summary_text(many).split("\n")[2:5],
          ["<b>Умар Хошимов</b>", "• юборилмади (−1 балл) → сабабли: 14.10, 16.10, 18.10", "• сўралмаган → сабабли: 17.10"])
    check_true("the card: 📊 for people asked for reports — the Garmin shop staff included, with ака/опа, AI, 📝 "
               "(missing on their card since 17.10)",
               all(x in str(admin.employee_card({**emp, "display_name": "Ширин"})[1])
                   for x in ("rsh:", "addr:", "aich:", "rpof:", "lng:", "wd0:"))
               and "rsh:" not in str(admin.employee_card({**emp, "role": "operatsion_direktor", "display_name": "Б"})[1]))

    # ---- late reports: no mistakes (the review of 23.10)
    check("the wait after 📝 is 3 hours (12 swallowed the evening report)", late_report.AWAIT_HOURS, 3)

    async def waiting_row(tg, hours):
        return {"id": "r1", "report_date": d23}

    async def today_ask(tg, day):
        return {"prompt_message_id": 700, "reminder_message_id": 701, "status": "asked"}

    delivered: list = []

    async def deliver(employee, row_id, content, run_id, file_message_id=None):
        delivered.append((row_id, content, file_message_id))
        return "late_report_sent"

    patch(store, "late_report_awaiting", waiting_row)
    patch(store, "report_for_day", today_ask)
    patch(late_report, "deliver", deliver)
    patch(late_report, "_send", nothing)
    try:
        r_today = asyncio.run(late_report.take_message(emp, {"text": "бугун 4 та сотув", "reply_to_message": {"message_id": 700}}, uuid.uuid4()))
        r_photo = asyncio.run(late_report.take_message(emp, {"message_id": 55, "caption": "кечаги ҳисобот",
                                                             "photo": [{"file_id": "p1", "file_size": 10}]}, uuid.uuid4()))
        jobs: list = []

        class Background:
            def add_task(self, *job):
                jobs.append(job)

        r_voice = asyncio.run(late_report.take_message(emp, {"message_id": 56, "voice": {"file_id": "v1", "file_size": 999,
                                                                                         "mime_type": "audio/ogg"}},
                                                       uuid.uuid4(), Background()))
    finally:
        restore()
    check_true("a reply to today's 16:00 ask is today's report; a photo goes on as it is with its caption; a voice "
               "is turned into text first (in the background)",
               r_today is None and r_photo == "late_report_sent" and delivered[0] == ("r1", "[расм] кечаги ҳисобот", 55)
               and r_voice == "late_report_reading" and jobs and jobs[0][0] is late_report._voice_to_late)
    check("a report typed for any day but yesterday isn't a late report",
          asyncio.run(late_report.take_typed(emp, date(2026, 10, 1), "…", uuid.uuid4())), None)

    # ---- smaller fixes from the review
    now = datetime(2026, 10, 24, 10, 0)
    check("the analyst's words for a report day: excused, waiting for approval, a past unsent day, today still open",
          [analyst.report_state(r, now) for r in (
              {"status": "excused", "report_date": d23}, {"status": "asked", "report_date": d23, "late_content": "x"},
              {"status": "asked", "report_date": date(2026, 10, 20), "penalty_points": 0},
              {"status": "asked", "report_date": d24})],
          ["EXCUSED (a day with a reason, set by the Director / manager / admin) — not counted",
           "NOT sent on time; a late report was sent the next day and waits for approval", "NOT sent",
           "not sent yet (open until 22:00)"])
    caption = report_media.media_caption("Ширин Умматова", None, "&" * 900)
    check_true("a file's caption never passes Telegram's 1024 (an escaped «&» cut in half broke the HTML)",
               len(caption) <= 1024 and caption.endswith("…"))
    check_true("PULSE's warm-up rebuilds every round (the page's own 10 minutes let it skip one)",
               "ttl=WARM_EVERY - 60" in inspect.getsource(pulse_data.keep_warm))
    check_true("the employees' guide tells the work AI about the one morning message and 📝",
               "at most ONE message from the bot" in __import__("integrations.org_bot.knowledge", fromlist=["x"]).EMPLOYEE_BOT_GUIDE)
    check_true("the excused flow and the sheet clear PULSE's copy at once", "_forget_pulse()" in inspect.getsource(admin)
               and callable(excused._forget_pulse) and kpi.asked_for_reports(emp))


def test_lead_groups() -> None:
    """26.10.2026, the owner: leads to two Telegram groups (Garmin, Primus), one message per lead."""
    print("lead groups (26.10.2026)")
    import asyncio
    import uuid

    from integrations.common.config import settings
    from integrations.org_bot import lead_groups, ops_manager

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    check("commands with the bot's name", [lead_groups.command(t) for t in ("/lidlar_garmin@MgmgOpsBot", "/LIDLAR_PRIMUS x",
                                                                            "салом", "")],
          ["/lidlar_garmin", "/lidlar_primus", "салом", ""])
    g = lead_groups.garmin_text({"urgency": "now", "name": "Азиз <b>", "username": "aziz", "phone": "+998901234567",
                                 "product_name": "Forerunner 265", "price": 5490000, "summary": "сотиб олмоқчи",
                                 "source": "card", "created_at": datetime(2026, 10, 26, 9, 0)})
    check_true("a Garmin lead: hot / warm, who, phone, product with price, the words — escaped",
               "🔥 Иссиқ" in g and "Азиз &lt;b&gt; @aziz" in g and "+998901234567" in g
               and "Forerunner 265 — 5 490 000 сўм" in g and "сотиб олмоқчи" in g)
    check_true("…a phone added later says so", "телефон қўшилди" in lead_groups.garmin_text({"phone": "+998"}, phone_only=True))
    p = lead_groups.primus_text({"track": "equipment_sales", "company_name": "Hilton Tashkent", "project_name": "New wing",
                                 "industry": "hotel", "location": "Ташкент", "signal_source_url": "https://x.uz/a?b=1&c=2",
                                 "contact_name": "Ali", "contact_method": "info@x.uz", "priority": "high", "confidence": 0.8})
    check_true("a Primus lead: track, company — project, where, source link, contact, priority",
               "🏗 Ускуналар сотуви" in p and "Hilton Tashkent — New wing" in p and "https://x.uz/a?b=1&amp;c=2" in p
               and "Ali, info@x.uz" in p and "high · ишонч 0.8" in p)

    # ---- a group is never a chat with the bot
    sent, done = [], []

    class Bot:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, **k):
            sent.append((chat_id, text))
            return [1]

    async def execute(sql, params=None):
        done.append((sql.split()[0], params))

    async def employee(tg):
        return {"status": "active", "role": "operatsion_direktor"} if tg == 77 else None

    async def nothing(*a, **k):
        return None

    async def must_not(*a, **k):
        raise AssertionError("a group message reached the private chat flow")

    from integrations.org_bot import store

    patch(lead_groups, "TelegramBot", Bot)
    patch(lead_groups, "execute", execute)
    patch(lead_groups, "log_action", nothing)
    patch(store, "get_employee_by_telegram_id", employee)
    patch(ops_manager, "_handle_message", must_not)
    patch(settings, "admin_bot_admin_user_id", 5)
    group = {"id": -100123, "type": "supergroup", "title": "Garmin лидлар"}
    try:
        ignored = asyncio.run(ops_manager.handle_update(
            {"message": {"chat": group, "from": {"id": 77}, "text": "Азизга қўнғироқ қилинг"}}, uuid.uuid4(), None))
        stranger = asyncio.run(ops_manager.handle_update(
            {"message": {"chat": group, "from": {"id": 9}, "text": "/lidlar_garmin"}}, uuid.uuid4(), None))
        by_admin = asyncio.run(ops_manager.handle_update(
            {"message": {"chat": group, "from": {"id": 5}, "text": "/lidlar_garmin@MgmgOpsBot"}}, uuid.uuid4(), None))
        by_boss = asyncio.run(ops_manager.handle_update(
            {"message": {"chat": {**group, "id": -100456}, "from": {"id": 77}, "text": "/lidlar_garmin"}}, uuid.uuid4(), None))
        primus = asyncio.run(ops_manager.handle_update(
            {"message": {"chat": group, "from": {"id": 77}, "text": "/lidlar_primus"}}, uuid.uuid4(), None))
        stop = asyncio.run(ops_manager.handle_update(
            {"message": {"chat": group, "from": {"id": 5}, "text": "/lidlar_stop"}}, uuid.uuid4(), None))
    finally:
        restore()
    check("in a group: the Director's words aren't a task; a stranger can't bind; the admin and the Director can; stop",
          (ignored, stranger, by_admin, by_boss, stop),
          ("group_ignored", "group_ignored", "lead_group_set", "lead_group_set", "lead_group_cleared"))
    check_true("…bound to the right business and told in the group",
               done[0][1][:2] == ("garmin", -100123) and done[1][1][:2] == ("garmin", -100456) and primus == "group_ignored"
               and "Garmin лидлари юборилади" in sent[0][1] and sent[0][0] == "-100123")

    # ---- one message per lead, a gap between
    sent.clear()
    gaps: list = []

    async def group_of(business):
        return -100123 if business == "garmin" else None

    async def sleep(s):
        gaps.append(s)

    patch(lead_groups, "TelegramBot", Bot)
    patch(lead_groups, "group_of", group_of)
    patch(lead_groups, "log_action", nothing)
    patch(lead_groups.asyncio, "sleep", sleep)
    patch(settings, "bots_frozen", False)
    try:
        n = asyncio.run(lead_groups.send("garmin", ["a", "b", "c"]))
        none = asyncio.run(lead_groups.send("primus", ["x"]))
    finally:
        restore()
    check("three leads → three messages to the group, ~3 s apart; no group bound → nothing",
          (n, [s[1] for s in sent], len(gaps), none), (3, ["a", "b", "c"], 2, 0))
    check_true("wired: the Garmin webhook posts each new lead / added phone; the Lead Agent each new lead",
               'background.add_task(lead_groups.send, "garmin"' in load_source("integrations/api/app.py")
               and "lead_groups" not in load_source("agents/lead-agent/agent.py")  # Primus / B2B: stopped 26.10
               and "RETURNING *" in load_source("integrations/garmin/leads.py")
               and "CREATE TABLE IF NOT EXISTS lead_groups" in load_source("database/schema.sql"))


def test_reports_any_time() -> None:
    """26.10.2026, the owner: the AI decides what is a report (asks only when unsure), reports from 14:00, after 22:00
    for approval, «ҳа» sends a held draft, the work AI never says a report was accepted."""
    print("reports from 14:00, the AI decides, no false «accepted» (26.10.2026)")
    import asyncio
    import uuid

    from integrations.org_bot import ai_chat, ops_manager, store

    saved: list = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        while saved:
            obj, name, value = saved.pop()
            setattr(obj, name, value)

    check("a short yes", [ops_manager.is_confirmation(t) for t in ("Ха", "Хоп.", "ok!", "ҳа шундай", "хоп, лекин эртага", "")],
          [True, True, True, True, False, False])
    check("a short message is never a report (no AI call)",
          asyncio.run(ops_manager._classify_report("раҳмат", uuid.uuid4())), "other")
    check("reports open at 14:00", ops_manager.REPORT_OPEN_HOUR, 14)
    check("«the report above» is never a report (Абдукарим 21.10)",
          [ops_manager.is_pointer(t) for t in ("Tepadagi xisobot", "юқоридаги ҳисобот", "как вчера", "кечагидек",
                                               "дўконни очдим, 4 мижоз, 2 сотув", "Qarzdorlar bn ishlavomman")],
          [True, True, True, True, False, False])
    check_true("…answered, not saved, on every path (owed, no ask, a tapped / «ҳа» held message)",
               load_source("integrations/org_bot/ops_manager.py").count("if is_pointer(text):") == 3
               and friendly_problems(ops_manager.pointer_text()) == [])

    # ---- no ask open: before 14:00, 14:00–22:00, after 22:00
    seller = {"id": "e1", "telegram_user_id": 11, "role": "garmin_sotuv", "full_name": "Ширин Умматова"}
    state: dict = {}
    replies: list = []

    async def report_for_day(tg, day):
        return state.get("row")

    async def open_request(employee, day):
        state["opened"] = day
        state["row"] = {"id": "r9", "status": "asked"}
        return state["row"]

    async def pending_report(tg, day):
        return state.get("row") if state.get("opened") else None

    async def save(employee, pending, text, run_id, quiet=False):
        state["saved"] = (pending["id"], text)
        return "daily_report"

    async def classify(text, run_id):
        return state["kind"]

    async def take_typed(employee, day, text, run_id):
        state["typed"] = day
        return "late_report_sent" if state.get("late_ok") else None

    async def reply(tg, run_id, text, reply_markup=None, reply_to=None):
        replies.append(text)
        return [1]

    def at(hour):
        return lambda: datetime(2026, 10, 26, hour, 5)

    patch(store, "report_for_day", report_for_day)
    patch(store, "open_report_request", open_request)
    patch(store, "pending_report", pending_report)
    patch(ops_manager, "_save_daily_report", save)
    patch(ops_manager, "_classify_report", classify)
    patch(ops_manager.late_report, "take_typed", take_typed)
    patch(ops_manager, "_reply", reply)
    results = []
    try:
        for hour, kind, row, late_ok in ((15, "report", None, False), (15, "other", None, False), (10, "report", None, False),
                                         (10, "report", None, True), (22, "report", {"id": "r1", "status": "asked"}, True),
                                         (23, "report", None, False), (15, "report", {"status": "submitted"}, False)):
            state.clear()
            state.update(kind=kind, row=row, late_ok=late_ok)
            patch(ops_manager, "now_local", at(hour))
            results.append((asyncio.run(ops_manager._report_outside_ask(seller, "дўконни очдим, 4 мижоз, 2 сотув", uuid.uuid4())),
                            state.get("opened") is not None, state.get("typed")))
            restore()
            for obj, name, value in ((store, "report_for_day", report_for_day), (store, "open_report_request", open_request),
                                     (store, "pending_report", pending_report), (ops_manager, "_save_daily_report", save),
                                     (ops_manager, "_classify_report", classify), (ops_manager.late_report, "take_typed", take_typed),
                                     (ops_manager, "_reply", reply)):
                patch(obj, name, value)
    finally:
        restore()
    d26, d25 = date(2026, 10, 26), date(2026, 10, 25)
    check("15:00 a report → today's opened and saved (no 16:00 ask needed); not a report → the AI; 10:00 → «from 14:00» "
          "(yesterday's missed one → for approval); 22:05 → today's closed ask for approval; 23:00 not asked → told; "
          "already sent → the AI",
          results, [("daily_report", True, None), (None, False, None), ("daily_report_too_early", False, d25),
                    ("late_report_sent", False, d25), ("late_report_sent", False, d26), ("daily_report_closed", False, None),
                    (None, False, None)])
    check_true("…in the polite voice", all(friendly_problems(t) == [] for t in replies))
    check_true("the late report takes today's after 22:00", "day == now.date() and now.hour >= 22"
               in load_source("integrations/org_bot/late_report.py"))

    # ---- the work AI: never «accepted», a draft gets a real button, one question, the language asked for
    for text in ("Хўп, ҳисоботингиз қабул қилинди.", "Ҳисоботингиз қабул қилинди. Бугунги ишларингиз: тозаланди.", "Хўп, қабул қилинди."):
        out, removed = ai_chat.strip_false_claims(text)
        check_true(f"no false «accepted»: {text[:30]}", removed and "қабул қилинди" not in out and "сақлай олмайман" in out)
    check("…but «calls accepted: 7» and a plain greeting stay",
          [ai_chat.strip_false_claims(t)[1] for t in ("<b>Қабул қилинган:</b> 7 та", "Салом! Қандай ёрдам берай?")], [False, False])
    shown, draft = ai_chat.split_draft("Мана:\nҲИСОБОТ ЛОЙИҲАСИ:\n• Дўконни очдим")
    check("a draft after «ҲИСОБОТ ЛОЙИҲАСИ:» is cut out for the button", (draft, "<b>Ҳисобот лойиҳаси:</b>" in shown),
          ("• Дўконни очдим", True))
    prompt = ai_chat.SYSTEM_PROMPT
    check_true("the AI is told: never say accepted, the draft line, at most one question, the language asked in the chat",
               "You can NEVER save, accept, send or receive a report" in prompt and "ҲИСОБОТ ЛОЙИҲАСИ:" in prompt
               and "at most ONE short clarifying question" in prompt and "answer in Uzbek or in Russian, do so" in prompt
               and "unless they ask in this chat\nfor Uzbek" in ai_chat.RUSSIAN)
    src = load_source("integrations/org_bot/ops_manager.py")
    check_true("wired: every AI answer filtered; a draft held with «📝 Ҳисобот сифатида юбориш»; «ҳа» after it sends it",
               "ai_chat.strip_false_claims(answer)" in src and '"📝 Ҳисобот сифатида юбориш", "callback_data": f"asrep:' in src
               and "latest_open_relay(employee[\"telegram_user_id\"], HELD_CONFIRM_MINUTES)" in src)
    check_true("the «ҳисоботми ёки савол?» buttons only when the AI isn't sure", 'if decided == "unsure":' in src)


def test_garmin_stock_quantity() -> None:
    """27.10.2026, Murod aka: «Garmin остаткасини қўшиш керак, суммаси эмас, товар кол-воси» — stock in pieces."""
    print("Garmin stock as quantity (27.10.2026)")
    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import analyst

    brief = load_agent("garmin-brief")
    whs = [{"WhsCode": "G.A._01", "WhsName": "Garmin Абай"}, {"WhsCode": "G.M_05", "WhsName": "Garmin Минор"},
           {"WhsCode": "Parts", "WhsName": "Parts"}]
    items = [{"WhsCode": "G.A._01", "ItemCode": "FR265", "ItemName": "Forerunner 265", "OnHand": 4, "StockValue": 9e6},
             {"WhsCode": "G.M_05", "ItemCode": "FR265", "ItemName": "Forerunner 265", "OnHand": 2},
             {"WhsCode": "G.A._01", "ItemCode": "F8", "ItemName": "Fenix 8", "OnHand": "3.000"},
             {"WhsCode": "G.A._01", "ItemCode": "010-M1", "ItemName": "Dummy Unit Fenix", "OnHand": 5},
             {"WhsCode": "G.A._01", "ItemCode": "X", "ItemName": "Sold out", "OnHand": 0},
             {"WhsCode": "Parts", "ItemCode": "D51", "ItemName": "Washer part", "OnHand": 56}]
    stock = brief.garmin_stock(items, whs, frozenset({"G.A._01", "G.M_05"}))
    check("pieces in Garmin warehouses only, models, per warehouse — dummies and zeros left out",
          stock, (9, 2, [("Garmin Абай", 7), ("Garmin Минор", 2)]))
    data = brief.GarminData(day=date(2026, 10, 26), stock=stock)
    view = brief.view(data, datetime(2026, 10, 27, 8, 0))
    fig = next(f for f in view["figures"] if f["label"] == "Garmin қолдиғи")
    check("the brief's figure: pieces (дона), not money", (fig["number"], fig["unit"], fig["detail"]),
          ("9", "дона", "2 хил товар · Garmin Абай 7 · Garmin Минор 2"))
    check_true("…one row only, no extra block (the brief stays short)", not any("Қолдиқ" in b["title"] for b in view["blocks"]))
    check_true("the analyst answers «қолдиқ / остаток / кол-во» in pieces for the Garmin manager",
               "STOCK = QUANTITY" in analyst.employee_prompt("Мурод", "Garmin менежер", {"sap_garmin", "billz", "garmin"})
               and "дона (pieces), not money" in analyst.employee_prompt("М", "x", {"sap_garmin"}))


def test_garmin_stock_for_ordering() -> None:
    """27.10.2026, Murod aka: «барча товарлар остаткаси керак» — and the owner the same day: only the product's name
    and how many are left; it isn't the bot's job to say what to buy."""
    print("Garmin stock: names and pieces left, nothing more (27.10.2026)")
    import io

    from openpyxl import load_workbook

    from integrations.garmin import stock_list
    from integrations.org_bot import analyst
    from integrations.telegram import menus

    codes = frozenset({"G.A._01", "05", "06"})
    whs = [{"WhsCode": "G.A._01", "WhsName": "Garmin Abay"}, {"WhsCode": "05", "WhsName": "Garmin-Minor"},
           {"WhsCode": "06", "WhsName": "Faulty Product Garmin"}]
    check("the faulty-goods warehouse isn't stock to sell", stock_list.sellable(codes, whs), frozenset({"G.A._01", "05"}))
    stock = [{"WhsCode": "G.A._01", "ItemCode": "A", "ItemName": "Forerunner 265", "OnHand": 2},
             {"WhsCode": "05", "ItemCode": "A", "ItemName": "Forerunner 265", "OnHand": 1},
             {"WhsCode": "G.A._01", "ItemCode": "B", "ItemName": "fenix 8", "OnHand": 20},
             {"WhsCode": "G.A._01", "ItemCode": "Z", "ItemName": "Sold out", "OnHand": 0},
             {"WhsCode": "G.A._01", "ItemCode": "010-M9", "ItemName": "Dummy Unit", "OnHand": 3},
             {"WhsCode": "06", "ItemCode": "B", "ItemName": "fenix 8", "OnHand": 4},
             {"WhsCode": "Parts", "ItemCode": "P", "ItemName": "Washer part", "OnHand": 50}]
    items = stock_list.build(stock, frozenset({"G.A._01", "05"}))
    check("every product left, by name, pieces per warehouse — nothing sold out, no dummies, no faulty, no other business",
          [(i.name, i.per_wh, i.total) for i in items],
          [("fenix 8", {"G.A._01": 20.0}, 20.0), ("Forerunner 265", {"G.A._01": 2.0, "05": 1.0}, 3.0)])
    names = {"G.A._01": "Garmin Abay", "05": "Garmin-Minor"}
    text = stock_list.summary(items, names)
    check_true("the chat summary: total, products, per warehouse — no advice on what to buy",
               "Жами: <b>23 дона</b>, 2 хил товар" in text and "• Garmin Abay — 22 дона" in text
               and not any(w in text for w in ("буюртма", "тугаган", "кам ", "кўп", "сотил")))
    ws = load_workbook(io.BytesIO(stock_list.workbook_bytes(items, names))).active
    check("the .xlsx: product, code, a column per warehouse, the total",
          ([c.value for c in ws[2]], ws.max_row, bool(ws.auto_filter.ref)),
          (["Товар", "Код", "Garmin Abay", "Garmin-Minor", "Жами қолдиқ"], 4, True))
    prompt = analyst.employee_prompt("М", "x", {"sap_garmin"})
    check_true("/qoldiq for the Garmin manager and the Director; the analyst says what is left and never what to buy",
               "qoldiq" in [c for c, _ in menus.GARMIN_MANAGER] and "qoldiq" in [c for c, _ in menus.DIRECTOR]
               and "garmin_stock_list" in {t["function"]["name"] for t in analyst.tools_for({"sap_garmin", "billz", "garmin"})}
               and "never advise what to buy" in prompt and "буюртма учун" not in str(menus.GARMIN_MANAGER)
               and "never count it in what is left" in prompt)

def test_report_bonus() -> None:
    """22.10.2026, the owner: each morning Garmin сотув get +1 KPI for yesterday's report, or politely none."""
    print("report bonus (22.10.2026)")
    import inspect

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import kpi_score, store

    agent = load_agent("report-bonus")
    base = {"report_date": date(2026, 10, 21), "full_name": "Нодира Алиева", "address_form": "опа"}
    sent = agent.message({**base, "status": "submitted", "auto_built": False, "bonus_points": 1, "month_bonus": 5}, 1)
    built = agent.message({**base, "status": "submitted", "auto_built": True, "bonus_points": 0, "month_bonus": 4}, 1)
    missed = agent.message({**base, "status": "asked", "auto_built": False, "bonus_points": 0, "month_bonus": 4}, 1)
    for text in (sent, built, missed):
        check(f"friendly: {text[:32]}…", friendly_problems(text, ("Нодира", "Нодира опа", "KPI")), [])
    check_true("sent: thanks, +1 and the month's total; built from messages or missed: no point this time, politely, "
               "and how to get it", "+1 балл қўшилди" in sent and "жами +5 балл" in sent and "балл берилмади" in built
               and "ўзингиз юборсангиз" in built and "келмагани учун бу сафар балл берилмади" in missed and "22:00" in missed)
    sql = inspect.getsource(store.settle_report_bonus)
    check_true("once per report; only reports the person sent themselves get the point; active, reports on, the roles set",
               "bonus_at IS NULL" in sql and "NOT r.auto_built" in sql and "e.role = ANY" in sql and "NOT e.reports_off" in sql)
    check("Garmin сотув, +1 by default", (settings.report_bonus_roles, settings.report_bonus_points), ("garmin_sotuv", 1))
    p = kpi_score.EmployeeMonth(employee_id="e", name="x", role_label="", bonus=6, penalty=2)
    p.reports = kpi_score.ReportScore(asked=10, reported=8, on_time=8)
    plain = kpi_score.EmployeeMonth(employee_id="f", name="y", role_label="")
    plain.reports = kpi_score.ReportScore(asked=10, reported=8, on_time=8)
    kpi_score.score([p, plain])
    check("the KPI: + the month's bonus − the minus (never over 100)", round(p.total - plain.total, 1), 4.0)
    check_true("the person's /kpi card says it", "+6</b> балл" in kpi_score.card_text(date(2026, 10, 1), p))
    check_true("the KPI reads bonus_points", "r.bonus_points" in inspect.getsource(store.kpi_month_rows))
    morning = load_source("scripts/run_morning_agents.py")
    check_true("08:00, before the explanations", morning.index("agents/report-bonus") < morning.index("agents/explanations"))
    check_true("schema: bonus_points, bonus_at", "ADD COLUMN IF NOT EXISTS bonus_points" in load_source("database/schema.sql"))


def test_garmin_competitors() -> None:
    """19.10.2026: who else sells Garmin in Uzbekistan (OLX, Google) against our garmin.com.uz prices — offline."""
    print("Garmin competitors (19.10.2026)")
    import inspect
    import json
    from datetime import timezone

    from integrations.api import db_viewer
    from integrations.common.config import settings
    from integrations.garmin import competitors as c
    from integrations.org_bot import analyst, ops_manager
    from integrations.reports import render
    from integrations.telegram import menus

    # model names, as sellers and our site write them
    for title, key in (("Garmin Fenix 8 47mm Amoled", "fenix 8"), ("fēnix 8 — 47 mm, Solar", "fenix 8"),
                       ("Garmin Fenix 8 Pro 51mm", "fenix 8 pro"), ("Garmin Epix Pro Gen 2", "epix 2 pro"),
                       ("Epix Pro (Gen 2) Sapphire", "epix 2 pro"), ("Garmin forerunner 970 black", "forerunner 970"),
                       ("Venu X1 French Gray", "venu x1"), ("Instinct Crossover AMOLED, Tactical, Black", "instinct crossover"),
                       ("MARQ Athlete Gen 2, Carbon", "marq athlete 2"), ("Навигатор гармин Монтана 700", "montana 700"),
                       ("Garmin Instinct 2X Solar", "instinct 2x"), ("Garmin vivoactive5 42mm", "vivoactive 5"),
                       ("Garmin Cirqa Smart Band New 2026", "cirqa"), ("Garmin Instinct E 45 мм Black", "instinct e"),
                       ("Descent Mk3i — 51 mm", "descent mk3i"), ("Гармин Феникс 7", "fenix 7")):
        check(f"model: {title}", c.model_key(title), key)
    check("size from «47 mm» / «45 мм»", (c.size_mm("fēnix 8 — 47 mm"), c.size_mm("Instinct E 45 мм")), (47, 45))
    for title, why in (("Garmin HD350 43mm pro 1:1", "copy"), ("часы копия 1x1 GARMIN 8 Pro", "copy"),
                       ("Remont Garmin soat, Ремонт часов Garmin", "repair"), ("Ремешок Garmin 47 мм Quickfit", "part"),
                       ("Чехол для Smart watch Garmin Fenix 7x", "part"), ("Garmin кабель+зарядка для часов", "part"),
                       ("Безель на часы garmin fenix 7", "part"),
                       ("Polar Pacer спортивные часы", "not_garmin"),
                       ("Пульсометр CooSpo H808S совместим с Garmin", "part"),
                       ("Forerunner 165 Music", None), ("Garmin Venu 3S, оригинал, с коробкой и зарядкой", None),
                       ("Fenix 9 PRO - inReach, 47 mm Titanium + 2 ремешка", None)):
        check(f"left out: {title}", c.left_out(title), why)

    # our prices (garmin.com.uz): by model, that size's when we sell it; «0» (ask) is not a price
    ours = c.OurPrices.from_products([
        {"name": "fenix 8 47mm,AMOLED,Sapphire", "price": 17800000}, {"name": "fēnix 8 — 47 mm, Solar", "price": 18000000},
        {"name": "fenix 8 51mm,AMOLED,Sapphire", "price": 20100000}, {"name": "Forerunner 970", "price": 12000000},
        {"name": "MARQ® Athlete (Gen 3)", "price": 0}])
    check("our range by model and size", (ours.range("fenix 8", 47), ours.range("fenix 8"), ours.range("fenix 8", 43)),
          ((17800000, 18000000), (17800000, 20100000), (17800000, 20100000)))
    check("a «0 so'm» product isn't a price", (ours.count, ours.range("marq athlete 3")), (4, None))

    def olx(id_, title, price, currency="UZS", converted=None, state="new", business=False, user=None, shop=None, prev=None,
            contact=None):
        value = {"value": price, "currency": currency, "converted_value": converted, "negotiable": True}
        if prev:
            value.update(previous_value=prev, converted_previous_value=prev * 12000 if currency == "UYE" else prev)
        params = [{"key": "price", "value": value}]
        if state:
            params.append({"key": "state", "value": {"key": state, "label": state}})
        return {"id": id_, "title": title, "url": f"https://www.olx.uz/d/obyavlenie/{id_}.html", "params": params,
                "business": business, "user": user or {"id": 900 + id_, "name": "Улугбек", "company_name": ""},
                "shop": shop or {"subdomain": None}, "contact": contact or {"name": (user or {}).get("name") or "Улугбек"},
                "created_time": "2026-10-18T07:27:47+05:00",
                "location": {"city": {"name": "Ташкент"}}}

    raw = [olx(1, "Garmin Fenix 8 47mm", 10_280_920), olx(2, "Garmin Forerunner 970", 600, "UYE", 7_257_120, prev=650),
           olx(3, "Garmin fenix 8 47mm", 710, "UYE", 8_587_592, state="used"),
           olx(4, "Garmin tactix 8 smart watch soat", 483_808),
           olx(5, "Garmin Fenix 8 47mm AMOLED", 17_500_000, business=True,
               user={"id": 5, "name": "998901234567", "company_name": "Garmin Shop UZ"}),
           olx(6, "Ремешок на garmin", 50_000), olx(7, "Garmin Venu X1", 12_000_000, business=True, user={"id": 77, "name": "998909779778"}),
           olx(8, "Garmin Instinct 3 45mm", 6_000_000, business=True, user={"id": 88, "name": "x"},
               contact={"name": "Hyper Phone"}, shop={"subdomain": "hyperphone"})]
    offers = [o for o in (c.parse_olx(r) for r in raw) if o is not None]
    check("straps left out; у.е. read in OLX's so'm", ([o.external_id for o in offers], offers[1].price),
          (["1", "2", "3", "4", "5", "7", "8"], 7_257_120))
    check_true("a private seller: no name, no phone; a shop: the name it sells under, its number when that is its name, "
               "its OLX shop page",
               offers[0].seller is None and offers[0].seller_phone is None and offers[4].seller == "Garmin Shop UZ"
               and offers[5].seller is None and offers[5].seller_phone == "+998 90 977 97 78"
               and offers[6].seller == "Hyper Phone" and offers[6].shop_page == "https://hyperphone.olx.uz/")
    offers = offers[:6]  # the counts below are for the first six
    c.compare(offers, ours)
    check("cheaper = a NEW item under 95 % of our lowest price; used and suspicious ones aren't",
          [o.external_id for o in offers if c.cheaper(o)], ["1", "2"])
    check("a new item under 30 % of ours is suspicious (a copy or a wrong price)", [o.external_id for o in offers if c.suspicious(o)], [])
    offers[3].ours = (21_850_000, 26_400_000)
    check_true("...tactix 8 at 484 минг is suspicious, not cheaper", c.suspicious(offers[3]) and not c.cheaper(offers[3]))

    check("the first check marks nothing new", (c.diff({}, offers), any(o.is_new for o in offers)), (False, False))
    check("OLX's own «price before» shows as a drop", offers[1].dropped_from, 650 * 12000)
    prev = {("olx", "1"): 11_000_000, ("olx", "3"): 8_587_592}
    check("then: new since the last check, and cheaper than last time", (c.diff(prev, offers),
          [o.external_id for o in offers if o.is_new], offers[0].dropped_from), (True, ["2", "4", "5", "7"], 11_000_000))

    # web search results: Uzbek pages selling Garmin only, the price only when it is written
    hits = [c.parse_web(r) for r in (
        {"url": "https://shop.uz/garmin-fenix-8", "title": "Garmin Fenix 8 купить в Ташкенте", "snippet": "Цена: 15 900 000 сум"},
        {"url": "https://www.olx.uz/d/x", "title": "Garmin", "snippet": "купить"},
        {"url": "https://garmin.com.uz/catalog", "title": "Garmin", "snippet": "купить"},
        {"url": "https://kun.uz/news/garmin", "title": "Garmin представила fenix 8", "snippet": "цена"},
        {"url": "https://www.amazon.com/garmin", "title": "Garmin fenix 8 buy", "snippet": "price $999"},
        {"url": "https://t.me/garmin_tashkent", "title": "Garmin Ташкент", "snippet": "часы в наличии, доставка"})]
    check("web: a .uz shop and a Telegram channel; not OLX, not ours, not news, not abroad",
          [h.seller if h else None for h in hits], ["shop.uz", None, None, None, None, "t.me"])
    check("web: the price as written in the result", (hits[0].price, hits[5].price), (15_900_000, None))

    # the page and the caption
    report = c.Report(olx=offers, web=[h for h in hits if h], ours=ours, compared=True, web_state="ok",
                      at=datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc))
    v = c.view(report)
    check("the page: cheaper new items, shops (OLX accounts, a site, a Telegram channel), web pages",
          (len(v["cheaper"]), [s["name"] for s in v["shops"]], len(v["web"])),
          (2, ["Garmin Shop UZ", "shop.uz", "t.me/garmin_tashkent", "номи кўрсатилмаган"], 2))
    check("...a shop whose OLX name is its number: no name, the number as its phone", v["shops"][-1]["phones"],
          ["+998 90 977 97 78"])
    pdf = render.pdf("garmin_competitors.html", **v)
    html = render.render_html("garmin_competitors.html", image=False, **v)
    check_true("the PDF is drawn, with the listing links", pdf[:4] == b"%PDF" and "olx.uz/d/obyavlenie/1.html" in html)
    text = c.caption(report) + c.text_fallback(report) + html
    check_true("no private person's name or phone anywhere", "Улугбек" not in text and "998901234567" not in text)
    check_true("no emoji in the caption", not any(0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF for ch in c.caption(report)))
    check("so'm the short way", (c.som(10_280_920), c.som(850_000), c.som(None)), ("10,3 млн", "850 минг", "—"))

    # wiring
    check_true("weekly in the 08:00 job, on its weekday (Thursday), for the Garmin manager",
               "agents/garmin-competitors/agent.py" in load_source("scripts/run_morning_agents.py")
               and settings.garmin_competitors_weekday == 3 and settings.garmin_competitors_enabled
               and 'ROLE = "garmin_manager"' in load_source("agents/garmin-competitors/agent.py"))
    check_true("/raqobat in the Garmin manager's ☰ menu, answered in the background",
               ("raqobat", "Рақобатчилар: Garmin'ни бошқалар қанчага сотмоқда") in menus.GARMIN_MANAGER
               and "background.add_task(_send_competitors" in inspect.getsource(ops_manager._handle_employee_message))
    check_true("/raqobat reuses today's web search (search credits); the weekly run searches fresh",
               "fresh_web=False" in inspect.getsource(ops_manager._send_competitors)
               and "fresh_web=sent == 0" in load_source("agents/garmin-competitors/agent.py"))
    check_true("the bot answers about competitors (garmin_data → competitors)",
               "competitors" in inspect.getsource(analyst.garmin_data) and "competitors.answer_lines()" in inspect.getsource(analyst.garmin_data))
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("schema: garmin_competitor_offers, open in /db (public listings, no company figures)",
               "CREATE TABLE IF NOT EXISTS garmin_competitor_offers" in schema and db_viewer.is_open("garmin_competitor_offers"))
    serp = load_source("integrations/search/serpapi_client.py")
    check_true("Google is searched from Uzbekistan in Russian (gl / hl)", 'params["gl"] = country' in serp
               and 'params["hl"] = language' in serp and 'country="uz", language="ru"' in inspect.getsource(c.search_web))
    check_true("nothing goes to the AI", "openrouter" not in load_source("integrations/garmin/competitors.py").lower())

    # the Director's test of 20.10: «Бир йилдан ошган товарлар қанча?» → "not available"; prices from the August snapshot
    import asyncio

    from integrations.org_bot import old_stock as _ostock
    row = _ostock.Row("A", "fenix 6", "05", "Garmin-Minor", 2, 900.0, date(2025, 9, 1), 414, 0.0)
    saved_load = _ostock.load

    async def fake_load(codes=None):
        return [row], [], datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    _ostock.load = fake_load
    try:
        answer = asyncio.run(analyst.old_stock_tool({}))
    finally:
        _ostock.load = saved_load
    check_true("the analyst has old stock (as /eski): counts, value, each row", "old_stock" in analyst.TOOL_NAMES
               and analyst.TOOL_SYSTEMS["old_stock"] == {"sap"} and "A year or more: 1 rows, $900" in answer
               and "fenix 6 (A) | Garmin-Minor" in answer)
    check_true("our prices for the bot come live from garmin.com.uz (the snapshot only if the site can't be read)",
               "competitors.our_catalog_text()" in inspect.getsource(analyst.garmin_data))

    # Google Programmable Search (20.10): used when its key and engine are set, SerpAPI otherwise
    from pydantic import SecretStr

    from integrations.search import google_cse_client as gcse
    from integrations.search.serpapi_client import SerpAPIClient
    saved = (settings.google_search_api_key, settings.google_search_cx, settings.serpapi_api_key)
    try:
        settings.google_search_api_key, settings.google_search_cx = SecretStr("k"), "cx1"
        on = isinstance(gcse.search_client("t"), gcse.GoogleSearchClient)
        settings.google_search_cx = ""
        settings.serpapi_api_key = SecretStr("")
        off = isinstance(gcse.search_client("t"), SerpAPIClient) and not gcse.search_configured()
    finally:
        settings.google_search_api_key, settings.google_search_cx, settings.serpapi_api_key = saved
    check_true("Google search when GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_CX are set; SerpAPI otherwise", on and off)
    check_true("a failed Google search is handled like a failed SerpAPI one",
               issubclass(gcse.GoogleSearchError, __import__("integrations.search.serpapi_client", fromlist=["x"]).SerpAPIError)
               and "search_client(AGENT, run_id)" in inspect.getsource(c.search_web)
               and "search_client(AGENT, run_id)" in load_source("integrations/garmin/competitor_shops.py"))

    # ---- the shops' contacts (2026-10-20): what each SHOP publishes; a private person is never looked up
    from integrations.garmin import competitor_shops as cs

    check("phones in any form → «+998 90 123 45 67»", [cs.phone(x) for x in ("+998998446162", "(90) 123-45-67", "998 71 1234567", "12345")],
          ["+998 99 844 61 62", "+998 90 123 45 67", "+998 71 123 45 67", None])
    html = ('<a href="tel:+998901234567">call</a> Тел: +998 (97) 732-37-27 · OLX +998 (71) 202-07-07 '
            '<a href="https://instagram.com/p/abc">post</a> <a href="https://www.instagram.com/texnikach/">ig</a> '
            '<a href="https://t.me/share/url?x">s</a> <a href="https://t.me/texnikach">tg</a>')
    check("a page's phones, Instagram, Telegram — not OLX's own number, not «/p/» or «/share»", cs.contacts_in(html),
          (["+998 90 123 45 67", "+998 97 732 37 27"], "texnikach", "texnikach"))
    state = json.dumps({"x": {"y": [{"name": "Texnikach", "contact": {"external_page_url": "http://t.me/texnikach",
                                                                       "phones": ["+998998446162", "998908456162"]},
                                     "address": {"street": "Малика", "number": "Б2"}}]}})
    page = cs.olx_shop_page('<script>window.__PRERENDERED_STATE__= ' + json.dumps(state) + ';</script>')
    check("an OLX shop page: its name, phones, link, address", page,
          {"name": "Texnikach", "phones": ["+998 99 844 61 62", "+998 90 845 61 62"], "link": "http://t.me/texnikach",
           "address": "Малика Б2"})
    check("a page's og: title and description", cs.page_meta('<meta property="og:title" content="Texnikach">'
          '<meta property="og:description" content="Заказ: +998901112233">').get("og:title"), "Texnikach")
    private = c.parse_olx(olx(9, "Garmin Forerunner 970", 9_000_000, user={"id": 9, "name": "Улугбек"},
                              contact={"name": "Улугбек"}))
    web_hits = [c.Offer(source="web", external_id="a", title="Garmin", url="https://t.me/s/texnikach"),
                c.Offer(source="web", external_id="b", title="Garmin", url="https://deviceuz.uz/garmin"),
                c.Offer(source="web", external_id="c", title="Garmin", url="https://www.instagram.com/garmin_tashkent/")]
    shops = cs.gather([private, *offers], web_hits)
    check("shops: OLX business accounts, a Telegram channel (t.me/s/… too), a site, an Instagram — no private person",
          sorted(sh.key for sh in shops), ["ig:garmin_tashkent", "olx:5", "olx:77", "site:deviceuz.uz", "tg:texnikach"])
    olx_shop = next(sh for sh in shops if sh.key == "olx:5")
    olx_shop.telegram = "texnikach"
    check("the same Telegram (or Instagram, site, name) is one shop", len(cs.merge(shops)), 4)
    check_true("«Seller» isn't a name; a lone first name isn't searched (someone else's page), a shop-like one is",
               cs.real_name("Seller") is None and not cs.looks_like_shop(cs.Shop(key="k", name="Умида"))
               and cs.looks_like_shop(cs.Shop(key="k", name="Hyper Phone")) and cs.looks_like_shop(cs.Shop(key="k", name="A.Mobile"))
               and cs.looks_like_shop(cs.Shop(key="k", name="Texnikach", olx_page="https://texnikach.olx.uz/")))
    check_true("an Instagram is taken only when its title or address holds the shop's name",
               cs.name_matches("Texnikach", "Texnikach (@texnikach) • Instagram") and not cs.name_matches("Texnikach", "Mobilon.Uz"))
    shop_rows = [cs.Shop(key="olx:5", name="Texnikach", phones=["+998 99 844 61 62"], instagram="texnikach",
                         telegram="texnikach", address="Малика Б2", offers=[offers[4]])]
    rv = c.view(c.Report(olx=offers, web=[], ours=ours, compared=False, shop_list=shop_rows,
                         at=datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)))
    shop_html = render.render_html("garmin_competitors.html", image=False, **rv)
    check_true("the page lists each shop's name, phone, Instagram, Telegram, address",
               all(x in shop_html for x in ("Рақобатчи дўконлар", "+998 99 844 61 62", "instagram.com/texnikach",
                                             "t.me/texnikach", "Малика Б2")))
    check_true("the caption counts shops with a phone and with an Instagram",
               "телефони бор: 1, Instagram'и бор: 1" in c.caption(c.Report(olx=offers, web=[], ours=ours, compared=False,
                                                                              shop_list=shop_rows, at=None)))
    src = inspect.getsource(cs)
    check_true("pages are read only where robots.txt allows; Google only in the weekly run, a few shops, once a month",
               "can_fetch(USER_AGENT, url)" in src and cs.SEARCH_BUDGET <= 10 and cs.SEARCH_DAYS >= 30
               and "search=fresh_web and report.web_state" in inspect.getsource(c.collect))
    check_true("phones never go to the AI (the analyst gets names and links only)",
               "SELECT name, instagram, telegram, website, address, olx_page FROM garmin_competitor_shops"
               in inspect.getsource(c.answer_lines))
    check_true("schema: garmin_competitor_shops, open in /db", "CREATE TABLE IF NOT EXISTS garmin_competitor_shops" in schema
               and db_viewer.is_open("garmin_competitor_shops"))


def test_old_stock_and_live_views() -> None:
    """17.10.2026: goods a year or more in stock (the Director, the Garmin manager); 1C / Billz in /db, live."""
    print("old stock, 1C / Billz live in /db (17.10.2026)")
    import inspect

    from integrations.api import db_viewer
    from integrations.org_bot import old_stock, ops_manager
    from integrations.telegram import menus

    today = date(2026, 10, 17)
    stock = [{"ItemCode": "A", "ItemName": "fenix 6", "WhsCode": "05", "OnHand": 2, "StockValue": 900},
             {"ItemCode": "B", "ItemName": "Venu", "WhsCode": "01", "OnHand": 1, "StockValue": 300},
             {"ItemCode": "C", "ItemName": "new", "WhsCode": "05", "OnHand": 5, "StockValue": 100},
             {"ItemCode": "D", "ItemName": "gone", "WhsCode": "05", "OnHand": 0, "StockValue": 0}]
    products = [{"ItemCode": "A", "LastPurDat": "2025-09-01"}, {"ItemCode": "B", "LastPurDat": "2025-11-01"},
                {"ItemCode": "C", "LastPurDat": "2026-09-01"}, {"ItemCode": "D", "LastPurDat": "2024-01-01"}]
    sales90 = [{"ItemCode": "A", "WhsCode": "05", "Quantity": 0}]
    whs = [{"WhsCode": "05", "WhsName": "Garmin-Minor"}]
    old, soon = old_stock.classify(stock, products, sales90, whs, today)
    check_true("a year or more: A (13 months); approaching (within 30 days): B; new and empty ones out",
               [r.code for r in old] == ["A"] and [r.code for r in soon] == ["B"] and old[0].whs_name == "Garmin-Minor")
    g_old, g_soon = old_stock.classify(stock, products, sales90, whs, today, frozenset({"05"}))
    check_true("the Garmin manager's list: only the Garmin warehouses", [r.code for r in g_old] == ["A"] and g_soon == [])
    dummies = [{"ItemCode": "010-M2704-00", "ItemName": "Dummy Unit, tactix 7", "WhsCode": "05", "OnHand": 1, "StockValue": 12},
               {"ItemCode": "IXU-87580", "ItemName": "Dummy Unit, Glass monitor", "WhsCode": "05", "OnHand": 1, "StockValue": 12}]
    d_old, _ = old_stock.classify(dummies, [{"ItemCode": d["ItemCode"], "LastPurDat": "2025-04-23"} for d in dummies], [], whs, today)
    check("display dummies are not old stock", d_old, [])
    cap = old_stock.caption("Эски товарлар", old, soon)
    check_true("the message: counts, value, how many didn't sell in 90 days", "<b>1</b> қатор, $900" in cap and "биттаям сотилмаган" in cap)
    agent_src = load_source("agents/old-stock/agent.py")
    check_true("Mondays, once a week; the Director all warehouses, the Garmin manager his",
               "weekday() != 0" in agent_src and "old_stock_weekly" in agent_src
               and "(\"garmin_manager\", await old_stock.garmin_codes_now())" in agent_src
               and "agents/old-stock/agent.py" in load_source("scripts/run_morning_agents.py"))
    src = inspect.getsource(ops_manager)
    check_true("/eski for both, in their menus", "old_stock.send(telegram_user_id, None, run_id)" in src
               and "old_stock.send(telegram_user_id, await old_stock.garmin_codes_now(), run_id)" in src
               and "eski" in [c for c, _ in menus.DIRECTOR] and "eski" in [c for c, _ in menus.GARMIN_MANAGER])

    check_true("1C and Billz in /db: read live, only while the platform tables are open",
               set(db_viewer.LIVE_VIEWS) == {"1c_balances", "1c_turnovers", "billz_cheques", "billz_shops"}
               and "table in LIVE_VIEWS and is_open(\"sap_gateway_snapshots\")" in load_source("integrations/api/db_viewer.py")
               and db_viewer.BILLZ_DAYS[0] == 3)


def test_reregistration_and_order() -> None:
    """The 14.10 test: a removed account came back with its old name and memory; quick questions got mixed answers."""
    print("re-registration and answer order (review 2026-10-14)")
    import asyncio
    import uuid

    from integrations.org_bot import ops_manager, store
    from integrations.telegram import bot as tg

    store_src = load_source("integrations/org_bot/store.py")
    create = store_src[store_src.index("async def create_employee("):store_src.index("async def active_employees_by_role(")]
    check_true("registering again after removal starts afresh: name, duties, settings, language, data access",
               all(f in create for f in ("full_name = CASE WHEN employees.status = 'revoked' THEN NULL",
                                          "responsibilities = CASE WHEN employees.status = 'revoked' THEN NULL",
                                          "lang = CASE WHEN employees.status = 'revoked' THEN 'uz'",
                                          "cheer_off = employees.cheer_off AND employees.status <> 'revoked'",
                                          "data_access = CASE WHEN employees.status = 'revoked' THEN '{}'",
                                          "telegram_username = EXCLUDED.telegram_username", "revoked_at = NULL")))
    revoke = store_src[store_src.index("async def revoke_employee("):store_src.index("# ------------------------------------------------------------- access requests")]
    check_true("removal ends everything tied to the person: AI memory, held messages and files, open tasks, data access",
               all(f in revoke for f in ("DELETE FROM ai_chat_turns", "UPDATE pending_relays", "UPDATE employee_files",
                                          "SET status = 'cancelled'", "data_access = '{}'", "had_data_access")))
    check_true("people who left aren't in overdue notices or the open-task answers",
               store_src.count("AND e.status = 'active'") >= 3)
    check_true("the Admin Bot handler relies on removal itself (no cleanup of its own)",
               "employee.get(\"had_data_access\")" in load_source("integrations/org_bot/admin.py"))

    # the registration message: the name is asked when the row has none (it was reset)
    replies: list = []
    asked: list = []

    async def fake_reply(chat, run_id, text, reply_markup=None, reply_to=None):
        replies.append(text)
        return [1]

    async def mark(tg_id):
        asked.append(tg_id)

    saved = [(ops_manager, "_reply", ops_manager._reply), (store, "mark_name_asked", store.mark_name_asked)]
    ops_manager._reply, store.mark_name_asked = fake_reply, mark
    try:
        asyncio.run(ops_manager.send_registration_confirmed(
            {"requested_role": "garmin_manager", "telegram_user_id": 77}, uuid.uuid4(),
            {"id": "e", "role": "garmin_manager", "status": "active", "full_name": None, "data_access": []}))
        check_true("a re-registered person is asked their name like anyone new", asked == [77] and "Исмингиз:" not in replies[-1])
        check_true("a Garmin manager is told at once what the role opened and how to ask",
                   "SAP (Garmin омборлари)" in replies[-1] and "сўраш имкони берилди" in replies[-1])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # one person's questions one at a time, in order
    log: list = []

    async def slow(employee, text, task, run_id, reply_to, quoted=None):
        log.append(("start", text))
        await asyncio.sleep(0.05)
        log.append(("end", text))

    saved = ops_manager._answer_ai_chat_now
    ops_manager._answer_ai_chat_now = slow
    try:
        async def two_quick():
            emp = {"telegram_user_id": 501}
            await asyncio.gather(ops_manager._answer_ai_chat(emp, "биринчи", None, uuid.uuid4(), 10),
                                 ops_manager._answer_ai_chat(emp, "иккинчи", None, uuid.uuid4(), 11))

        asyncio.run(two_quick())
        check("two quick questions: the second waits until the first is answered", log,
              [("start", "биринчи"), ("end", "биринчи"), ("start", "иккинчи"), ("end", "иккинчи")])
    finally:
        ops_manager._answer_ai_chat_now = saved

    payloads: list = []
    real_call = tg.TelegramBot._call

    async def fake_call(self, method, payload, **kwargs):
        payloads.append(payload)
        return {"message_id": 1}

    tg.TelegramBot._call = fake_call
    try:
        async def threaded():
            async with tg.TelegramBot(bot_token="T9") as bot:
                await bot.send_message("жавоб", chat_id="501", reply_to_message_id=42)

        asyncio.run(threaded())
        check("an answer is sent as a reply to its own question (kept even if that message is gone)",
              payloads[-1].get("reply_parameters"), {"message_id": 42, "allow_sending_without_reply": True})
    finally:
        tg.TelegramBot._call = real_call
    check_true("the employee's message id reaches the answer",
               "_route_to_ai(employee, text, task, run_id, background, message.get(\"message_id\"), quoted)"
               in load_source("integrations/org_bot/ops_manager.py"))


def test_garmin_manager() -> None:
    """2026-10-14, the owner: the Garmin manager — every Garmin complaint, SAP + Billz + Garmin data, a Garmin brief."""
    print("Garmin manager")
    import asyncio
    import importlib.util
    from datetime import date, datetime, timezone

    from integrations.billz.sales import DaySales, ShopDay
    from integrations.common.config import settings
    from integrations.org_bot import ai_chat, analyst, data_access, feedback, roles, routes, store
    from integrations.reports import render

    root = Path(__file__).resolve().parents[1]
    check("a role the admin can approve, named for people", roles.ROLE_LABELS.get("garmin_manager"), "Garmin менежер")
    check_true("...offered in the role picker", "setrole:garmin_manager:" in str(roles.role_picker_keyboard("r1")))
    check("Garmin complaints go to it", routes.ROUTES["complaint:garmin"], ("garmin_manager",))
    gm = {"id": "g1", "telegram_user_id": 451, "status": "active", "role": "garmin_manager", "full_name": "Синов Менежер",
          "display_name": "G", "data_access": []}
    ok_gm = gm
    check("the role opens Garmin's SAP, Billz and Garmin records — no Boss confirmation (the owner, 14.10); never 1C "
          "or all of SAP", data_access.granted(gm), {"sap_garmin", "billz", "garmin"})
    check("taken away with the role", data_access.granted({**gm, "role": "garmin_sotuv"}), set())
    check("no admin toggles for it (the role decides)", data_access.card_buttons(gm), [])
    text, keyboard = data_access.director_notice(gm, "admin1")
    check_true("the Boss is told (no buttons: changing the role closes it)", "ролига кўра" in text and keyboard is None)
    tools = [t["function"]["name"] for t in analyst.tools_for(data_access.granted(ok_gm))]
    check("its tools: Garmin's SAP only, Billz, Garmin records, his team's report with files (22.10) — no general SAP, 1C, attendance or the Boss's records",
          tools, ["data_sources", "billz_sales", "garmin_sap", "garmin_data", "garmin_stock_list", "employee_report"])
    prompt = analyst.employee_prompt("Синов Менежер", "Garmin менежер", data_access.granted(ok_gm))
    check_true("its prompt keeps it on Garmin", "THIS PERSON RUNS GARMIN" in prompt and "garmin_sap" in prompt)
    check_true("the work AI gives it the public Garmin catalog too", "Garmin catalog" in ai_chat.system_prompt(gm, "ctx"))

    async def recent(days=60):
        return [{"place": "garmin", "kind": "complaint", "message": "соат ишламаяпти", "phone": None, "contact_name": None,
                 "created_at": datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc), "branch": "abay", "code": "T-2026-10-0035",
                 "ticket_status": "open"},
                {"place": "laundry", "kind": "complaint", "message": "кир", "phone": None, "contact_name": None,
                 "created_at": datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc), "branch": "yunusobod"}]

    saved = store.recent_client_feedback
    store.recent_client_feedback = recent
    try:
        out = asyncio.run(analyst.garmin_data({"source": "complaints"}))
        check_true("garmin_data complaints: Garmin's only, with the ticket state", "соат ишламаяпти" in out
                   and "кир" not in out and "T-2026-10-0035" in out and "open ticket" in out)
    finally:
        store.recent_client_feedback = saved

    # the morning brief
    spec = importlib.util.spec_from_file_location("garmin_brief", root / "agents" / "garmin-brief" / "agent.py")
    gb = importlib.util.module_from_spec(spec)
    sys.modules["garmin_brief"] = gb
    spec.loader.exec_module(gb)
    sales = [{"ObjType": "13", "DocEntry": 1, "DocNum": 10, "DocDate": "2026-10-13", "CANCELED": "N", "DocTotalSy": "5000000"},
             {"ObjType": "13", "DocEntry": 2, "DocNum": 11, "DocDate": "2026-10-13", "CANCELED": "N", "DocTotalSy": "9000000"},
             {"ObjType": "14", "DocEntry": 3, "DocNum": 12, "DocDate": "2026-10-13", "CANCELED": "N", "DocTotalSy": "1000000"},
             {"ObjType": "13", "DocEntry": 4, "DocNum": 13, "DocDate": "2026-10-12", "CANCELED": "N", "DocTotalSy": "7000000"}]
    lines = [{"ObjType": "13", "DocEntry": 1, "WhsCode": "G.A._01"}, {"ObjType": "13", "DocEntry": 2, "WhsCode": "PR_01"},
             {"ObjType": "14", "DocEntry": 3, "WhsCode": "05"}, {"ObjType": "13", "DocEntry": 4, "WhsCode": "G.A._01"}]
    codes = frozenset({"G.A._01", "05"})
    check("SAP sales: yesterday, Garmin warehouses only, credit notes taken off", gb.sap_sales(sales, lines, date(2026, 10, 13), codes),
          (4000000, 2, 0))
    from integrations.org_bot import old_stock as _os
    whs_rows = [{"WhsCode": "09", "WhsName": "Garmin zahiralar (Tanita)"}, {"WhsCode": "10", "WhsName": "Garmin Ambasador"},
                {"WhsCode": "06", "WhsName": "Faulty Product Garmin"}, {"WhsCode": "01", "WhsName": "Main warehouse"}]
    gcodes = _os.garmin_codes(whs_rows)
    check_true("every warehouse named «Garmin» counts as Garmin's, with the shops (19.10: 09, 10, 06 were missed)",
               {"09", "10", "06", "G.A._01", "05"} <= gcodes and "01" not in gcodes)
    check_true("the Garmin brief's SAP sales use every Garmin warehouse",
               "old_stock.garmin_codes(whs)" in load_source("agents/garmin-brief/agent.py"))
    data = gb.GarminData(day=date(2026, 10, 13),
                         shop_sales=DaySales(day=date(2026, 10, 13), shops=[ShopDay("GARMIN ABAY", 18_400_000, 18_400_000, 4, 0)]),
                         sap_amount=4_000_000, sap_docs=2,
                         complaints_open=[{"code": "T-2026-10-0035", "place": "garmin", "branch": "abay",
                                           "created_at": datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)}],
                         complaints_new=1, leads=6, leads_now=2,
                         check={"missing": [{"number": "1043", "shop": "GARMIN ABAY"}], "amount_diff": []},
                         reports=[{"report_date": date(2026, 10, 13), "status": "missed", "display_name": "Абдурашид",
                                   "role": "garmin_sotuv"}])
    v = gb.view(data, datetime(2026, 10, 14, 8, 5))
    check("five Garmin figures — stock back as pieces on 27.10 — and nothing of the Boss's (no cash, customer debt, payments)",
          [f["label"] for f in v["figures"]],
          ["Дўкон савдоси (Billz)", "SAP'даги сотув", "Garmin қолдиғи", "Очиқ шикоятлар", "Garmin бот лидлари"])
    check_true("SAP sales say they count what was entered by 08:00",
               "08:00 гача SAP'га киритилгани" in v["figures"][1]["detail"])
    check_true("the Billz → SAP block says it is the 08:00 picture", "08:00 ҳолатига" in str(v["blocks"][0]))
    check("blocks: Billz → SAP, who didn't report, open complaints", [b["title"] for b in v["blocks"]],
          ["Billz → SAP", "Ҳисобот юбормаганлар", "Очиқ шикоятлар"])
    check_true("an open complaint shows its age in hours", "18 соат" in v["blocks"][-1]["rows"][0]["what"])
    picture = render.image("brief.html", **v)
    check_true("the same designed picture as the Boss's brief, titled for Garmin", picture[:8] == b"\x89PNG\r\n\x1a\n"
               and "Garmin кунлик ҳисоботи" in render.render_html("brief.html", image=True, page_width=540, page_height=6000,
                                                                     sentinel="#ff00ff", **v))
    check_true("the text fallback carries the same", "Billz → SAP" in gb.text(v) and "18,4 млн" in gb.text(v))
    check_true("the 08:00 job runs it after the Billz → SAP check",
               load_source("scripts/run_morning_agents.py").index("garmin-brief") >
               load_source("scripts/run_morning_agents.py").index("billz-sap-check"))
    check("the Billz → SAP check also goes to the Garmin manager (once off trial)", settings.billz_sap_check_roles, "garmin_manager")

    # the hourly complaint question: whoever is asked can press its buttons
    added: list = []

    async def due(max_asks=3):
        return [{"id": 7, "code": "T-2026-10-0035", "place": "garmin", "branch": "abay", "routed_to": "complaint:garmin",
                 "ticket_status": "open", "message": "соат", "cards": [{"chat": 9, "message": 1}]}]

    async def by_role(role):
        return [{"telegram_user_id": 451}] if role == "garmin_manager" else []

    async def add_cards(fid, cards):
        added.append((fid, cards))

    async def fake_send(row, people, text, keyboard, run_id):
        return [{"chat": p["telegram_user_id"], "message": 900} for p in people]

    saved = [(store, "complaints_due_status_check", store.complaints_due_status_check),
             (store, "active_employees_by_role", store.active_employees_by_role),
             (store, "set_complaint_cards", store.set_complaint_cards), (feedback, "_send_cards", feedback._send_cards)]
    store.complaints_due_status_check, store.active_employees_by_role = due, by_role
    store.set_complaint_cards, feedback._send_cards = add_cards, fake_send
    try:
        asyncio.run(feedback.ask_status(datetime(2026, 10, 14, 11, 0)))
        check("the question's message joins the ticket's cards (so the one asked can press ✅/❌)",
              added, [(7, [{"chat": 451, "message": 900}])])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("1C is read at Tashkent time on any machine", "now_local().strftime" in load_source("integrations/onec/payables.py"))
    check_true("the weekly data check counts blocked and in-review tasks as open",
               "('sent', 'started', 'blocked', 'review')" in load_source("agents/data-quality/agent.py"))


def test_complaint_tickets() -> None:
    """2026-10-13 (the owner: complaints go to the department, not the Boss): routed tickets, ✅ + reason, hourly check."""
    print("complaint tickets (A4/A5)")
    import asyncio
    import uuid
    from datetime import datetime

    from integrations.common.config import settings
    from integrations.org_bot import feedback, ops_manager, routes, store

    people = {"londry": [{"telegram_user_id": 301, "role": "londry"}],
              "garmin_sotuv": [{"telegram_user_id": 401, "role": "garmin_sotuv"}],
              "garmin_manager": [{"telegram_user_id": 451, "role": "garmin_manager"}, {"telegram_user_id": 452, "role": "garmin_manager"}],
              "operatsion_direktor": [{"telegram_user_id": 9, "role": "operatsion_direktor"}]}

    async def by_role(role):
        return list(people.get(role, []))

    saved = [(store, "active_employees_by_role", store.active_employees_by_role)]
    store.active_employees_by_role = by_role
    try:
        check("Londry complaints go to the Londry role, not the Boss",
              asyncio.run(routes.recipients("complaint:laundry")), ([{"telegram_user_id": 301, "role": "londry"}], False))
        check("Garmin complaints go to the Garmin manager(s), not Garmin sales (2026-10-14)",
              [p["telegram_user_id"] for p in asyncio.run(routes.recipients("complaint:garmin"))[0]], [451, 452])
        people["londry"] = []
        check("nobody in the role: the Boss gets it, flagged", asyncio.run(routes.recipients("complaint:laundry")),
              ([{"telegram_user_id": 9, "role": "operatsion_direktor"}], True))
        people["londry"] = [{"telegram_user_id": 301, "role": "londry"}]
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    row = {"id": 12, "code": "T-2026-10-0031", "place": "laundry", "branch": "yunusobod", "message": "<b>машина</b> ишламади",
           "phone": "+998901234567", "contact_name": "", "ticket_status": "open", "cards": [{"chat": 301, "message": 70}]}
    text = feedback.ticket_text(row)
    check_true("the ticket card: code, branch, the client's words escaped, the phone",
               "T-2026-10-0031" in text and "Юнусобод" in text and "&lt;b&gt;" in text and "+998901234567" in text)
    check("one button: ✅ Ҳал бўлди", [b["text"] for r in feedback.ticket_keyboard(row)["inline_keyboard"] for b in r], ["✅ Ҳал бўлди"])
    reasons = [b["callback_data"] for r in feedback.ticket_keyboard(row, picking_reason=True)["inline_keyboard"] for b in r]
    check_true("then a reason code, every button within 64 bytes",
               len(reasons) == len(feedback.REASONS) and all(len(d.encode()) <= 64 for d in reasons))
    check("a resolved card says who and why, no buttons",
          ("Ҳал бўлди — Зиёда, сабаби: Машина ёки тўлов" in feedback.ticket_text({**row, "ticket_status": "resolved",
                                                                                 "resolved_by": "Зиёда", "reason_code": "machine"}),
           feedback.ticket_keyboard({**row, "ticket_status": "resolved"})), (True, {"inline_keyboard": []}))

    # submit: stored, made a ticket, sent to the department
    sent: list = []
    edits: list = []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text, reply_markup))
            return [500 + len(sent)]

        async def _edit_message(self, **kwargs):
            edits.append((kwargs["chat_id"], kwargs["text"], kwargs.get("reply_markup")))

        async def _answer_callback(self, *args):
            return None

    stored: dict = {}

    async def save(**kwargs):
        stored.update({"id": 12, **kwargs, "cards": []})
        return dict(stored)

    async def open_ticket(fid, routed):
        stored.update({"code": "T-2026-10-0031", "ticket_status": "open", "routed_to": routed})
        return dict(stored)

    async def set_cards(fid, cards):
        stored["cards"] = stored.get("cards", []) + cards

    async def nothing(*args, **kwargs):
        return None

    saved = [(store, n, getattr(store, n)) for n in ("save_client_feedback", "open_complaint_ticket", "set_complaint_cards",
                                                       "mark_client_feedback_sent", "active_employees_by_role",
                                                       "get_client_feedback", "resolve_complaint", "complaint_not_yet",
                                                       "complaints_due_status_check", "get_employee_by_telegram_id")]
    saved += [(feedback, "TelegramBot", feedback.TelegramBot), (ops_manager, "TelegramBot", ops_manager.TelegramBot),
              (ops_manager, "_answer", ops_manager._answer), (ops_manager, "log_action", ops_manager.log_action),
              (settings, "bots_frozen", settings.bots_frozen)]
    store.save_client_feedback, store.open_complaint_ticket, store.set_complaint_cards = save, open_ticket, set_cards
    store.mark_client_feedback_sent, store.active_employees_by_role = nothing, by_role
    store.get_employee_by_telegram_id = nothing  # the presser's name: from Telegram, no database
    feedback.TelegramBot = ops_manager.TelegramBot = FakeBot
    ops_manager._answer = ops_manager.log_action = nothing
    settings.bots_frozen = False
    try:
        sub = feedback.Submission(place="laundry", branch="yunusobod", kind=feedback.KIND, message="кир чиқмади",
                                  name="", phone="", lang="uz_cyrl")
        n = asyncio.run(feedback.submit(sub))
        check_true("a Londry complaint: a ticket, sent to the Londry person only — the Boss gets nothing",
                   n == 1 and [c for c, _t, _k in sent] == ["301"] and "T-2026-10-0031" in sent[0][1]
                   and stored["routed_to"] == "complaint:laundry" and stored["cards"] == [{"chat": 301, "message": 501}])

        async def get_fb(fid):
            return dict(stored)

        async def resolve(fid, by, reason):
            stored.update({"ticket_status": "resolved", "resolved_by": by, "reason_code": reason})
            return dict(stored)

        store.get_client_feedback, store.resolve_complaint = get_fb, resolve
        stranger = {"id": "q", "data": "cfok:12", "from": {"id": 777}, "message": {"message_id": 501, "chat": {"id": 777}}}
        check("someone the ticket wasn't sent to can't press it", asyncio.run(ops_manager._handle_callback(stranger, uuid.uuid4())),
              "unrecognized")
        tap = {**stranger, "from": {"id": 301}, "message": {"message_id": 501, "chat": {"id": 301}}}
        check("✅ asks the reason", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "complaint_reason_asked")
        check_true("...on the same card", "Сабаби нима эди?" in edits[-1][1] and "cfr:12:" in str(edits[-1][2]))
        pick = {**tap, "data": "cfr:12:machine"}
        check("the reason closes the ticket", asyncio.run(ops_manager._handle_callback(pick, uuid.uuid4())), "complaint_resolved")
        check_true("every card shows it resolved, with the reason", stored["reason_code"] == "machine"
                   and "Машина ёки тўлов" in edits[-1][1] and edits[-1][2] == {"inline_keyboard": []})
        check("a second tap does nothing", asyncio.run(ops_manager._handle_callback(pick, uuid.uuid4())), "complaint_closed")

        # the hourly question
        stored.update({"ticket_status": "open"})
        sent.clear()

        async def due(max_asks=3):
            return [dict(stored)]

        store.complaints_due_status_check = due
        check("outside 09:00–20:00 nobody is asked", asyncio.run(feedback.ask_status(datetime(2026, 10, 13, 22, 0))), 0)
        check("in working hours an hour-old open ticket is asked about", asyncio.run(feedback.ask_status(datetime(2026, 10, 13, 11, 0))), 1)
        check_true("...with ✅ / ❌, to the department", sent[-1][0] == "301" and "ҳал бўлдими" in sent[-1][1]
                   and "cfno:12" in str(sent[-1][2]))
        store.complaint_not_yet = nothing
        no = {**tap, "data": "cfno:12", "message": {"message_id": 900, "chat": {"id": 301}}}
        check("❌ is noted, asked again later", asyncio.run(ops_manager._handle_callback(no, uuid.uuid4())), "complaint_not_yet")
        settings.bots_frozen = True
        sent.clear()
        check("the emergency stop: stored, nothing sent", (asyncio.run(feedback.submit(sub)), sent), (0, []))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    store_src = load_source("integrations/org_bot/store.py")
    check_true("the database: a ticket closes once, the hourly question is claimed once, at most three times",
               "WHERE id = %s AND ticket_status = 'open' RETURNING *" in store_src and "asks < %s" in store_src)
    check_true("the always-on service runs the hourly check", "_complaint_status_loop" in load_source("integrations/api/app.py"))


def test_fast_buttons() -> None:
    """2026-10-13: buttons were slow — one kept-alive Telegram connection per bot, the spinner stopped within 0.25 s."""
    print("fast buttons")
    import asyncio

    from integrations.telegram import bot as tg

    async def clients():
        async with tg.TelegramBot(bot_token="T1") as a:
            first = a._client
        async with tg.TelegramBot(bot_token="T1") as b:
            second = b._client
        async with tg.TelegramBot(bot_token="T2") as c:
            other = c._client
        await tg.close_shared_clients()
        return first, second, other

    first, second, other = asyncio.run(clients())
    check_true("one connection pool per bot, reused across calls; each bot its own", first is second and first is not other)
    again, _, _ = asyncio.run(clients())
    check_true("a new event loop (a cron run) gets a fresh client, never one from a closed loop", again is not first)

    calls: list[tuple[str, str]] = []
    real_call = tg.TelegramBot._call

    async def fake_call(self, method, payload, **kwargs):
        calls.append((method, payload.get("text", "")))
        return {}

    tg.TelegramBot._call = fake_call
    try:
        async def twice():
            async with tg.TelegramBot(bot_token="T1") as b:
                await b._answer_callback("q-1", "OK")
                await b._answer_callback("q-1", "again")

        asyncio.run(twice())
        check("a button is answered once (Telegram allows one answer)", calls, [("answerCallbackQuery", "OK")])

        async def slow_handler():
            tg.ack_soon("T1", "q-2", delay=0.05)
            await asyncio.sleep(0.15)  # the handler still working
            async with tg.TelegramBot(bot_token="T1") as b:
                await b._answer_callback("q-2", "late text")

        calls.clear()
        asyncio.run(slow_handler())
        check("a slow handler: the spinner is stopped for it, its late answer skipped", calls, [("answerCallbackQuery", "")])

        async def fast_handler():
            tg.ack_soon("T1", "q-3", delay=0.05)
            async with tg.TelegramBot(bot_token="T1") as b:
                await b._answer_callback("q-3", "OK")
            await asyncio.sleep(0.15)

        calls.clear()
        asyncio.run(fast_handler())
        check("a fast handler keeps its own text, no second answer", calls, [("answerCallbackQuery", "OK")])
    finally:
        tg.TelegramBot._call = real_call
    app_src = load_source("integrations/api/app.py")
    check_true("both bots' webhooks stop the spinner early; the service closes the connections on shutdown",
               app_src.count("ack_soon(settings.") == 2 and "close_shared_clients" in app_src)


def test_cheques() -> None:
    """2026-10-14: the Director's cheque photos are read by the AI and go to accounting, or he is asked."""
    print("cheque photos → accounting")
    import asyncio
    import uuid
    from datetime import datetime, timezone

    from integrations.ai import openrouter_client
    from integrations.common.config import settings
    from integrations.org_bot import cheques, ops_manager, store

    today = date(2026, 10, 14)
    check("a photo: its largest size", cheques.image_of({"photo": [{"file_id": "s"}, {"file_id": "l"}]}), ("l", "image/jpeg"))
    check("an image sent as a file", cheques.image_of({"document": {"file_id": "d", "mime_type": "image/png"}}), ("d", "image/png"))
    check("a PDF or a video is not read", (cheques.image_of({"document": {"file_id": "p", "mime_type": "application/pdf"}}),
                                           cheques.image_of({"video": {"file_id": "v"}})), (None, None))
    check("amounts as printed", [cheques.parse_amount(x) for x in ("1 250 000,00", "54.50", "1,250,000.00", "1.250.000", "сўм", None)],
          [1250000.0, 54.5, 1250000.0, 1250000.0, None, None])
    check("dates as printed", [cheques.parse_date(x) for x in ("30.07.2026/13:29:17", "14/10/26 09:05", "2026-10-13T10:00", "31.02.2026")],
          [date(2026, 7, 30), date(2026, 10, 14), date(2026, 10, 13), None])
    check("not a cheque → the old way", (cheques.read_result({"is_cheque": False}), cheques.read_result("x")), (None, None))

    good = {"is_cheque": True, "fields": {
        "fiscal_sign": "123 456 789 012", "fiscal_module": "LG420230630181", "cheque_number": "4572",
        "cheque_date": "13.10.2026 13:29", "rrn": "428612345678", "amount": "1 250 000,00", "currency": "сўм",
        "product": "Қоғоз A4; Ручка", "inn": "305 123 456", "seller": "Канцтовар МЧЖ"}, "unreadable": []}
    reading = cheques.read_result(good)
    check("details tidied: spaces out of the ИНН and the fiscal sign", (reading.fields["inn"], reading.fields["fiscal_sign"]),
          ("305123456", "123456789012"))
    check("every detail there, nothing wrong: send it", cheques.problems(reading, today), [])

    # The owner's sample photo (a Swiss restaurant bill of 2007): a cheque, but not one accounting can use.
    swiss = cheques.read_result({"is_cheque": True, "fields": {
        "fiscal_sign": None, "fiscal_module": None, "cheque_number": "4572", "cheque_date": "30.07.2007/13:29:17",
        "rrn": None, "amount": "54.50", "currency": "CHF", "product": "Latte Macchiato; Gloki; Schweinschnitzel; Chässpätzli",
        "inn": None, "seller": "Berghotel Grosse Scheidegg"}, "unreadable": []})
    found = cheques.problems(swiss, today)
    check_true("the sample: the missing details are named", all(any(label in p for p in found) for label in
               ("Фискал белги", "Фискал модул рақами", "Терминал RRN коди", "Контрагент ИНН")))
    check_true("...not in so'm, and old", any("сўмда эмас: CHF" in p for p in found) and any("чек эски" in p for p in found))
    blurred = cheques.read_result({**good, "fields": {**good["fields"], "rrn": None}, "unreadable": ["rrn", "nonsense"]})
    check("unreadable is told apart from absent", cheques.problems(blurred, today), ["Терминал RRN коди ўқилмади (расм хира ёки кесилган)"])
    bad = cheques.read_result({**good, "fields": {**good["fields"], "inn": "30512", "rrn": "12-34", "cheque_date": "20.10.2026"}})
    found_bad = cheques.problems(bad, today)
    check_true("a wrong ИНН, RRN and a date in the future are caught",
               any("ИНН 9 ёки 14" in p for p in found_bad) and any("RRN 12" in p for p in found_bad)
               and any("сана келажакда" in p for p in found_bad))
    check_true("a 14-digit ПИНФЛ is a valid ИНН", cheques.problems(
        cheques.read_result({**good, "fields": {**good["fields"], "inn": "30101901230012"}}), today) == [])
    check_true("so'm however printed", all(cheques.is_som(x) for x in ("UZS", "сўм", "сум", "so'm", "SUM")) and not cheques.is_som("USD"))

    stored = cheques.stored_fields(reading)
    caption = cheques.accounting_caption(stored, "офис учун", ["RRN чекда кўринмаяпти"])
    check_true("accounting's caption: every detail, the note, why it was sent anyway",
               all(x in caption for x in ("123456789012", "LG420230630181", "4572", "428612345678", "1 250 000",
                                         "305123456", "Қоғоз A4", "Изоҳ: офис учун", "⚠️ Директор шундай юборди")))
    check_true("...fits a Telegram caption", len(cheques.accounting_caption({**stored, "product": "х" * 3000}, None)) <= 1024)
    question = cheques.question_text(cheques.stored_fields(swiss), found)
    allow = {"RRN", "LG420230630181", "CHF", "Latte", "Macchiato", "Gloki", "Schweinschnitzel", "Ch", "ssp", "tzli", "Berghotel",
             "Grosse", "Scheidegg", "A4", "b"}
    check_true("the Director's question is Uzbek Cyrillic", latin_words(question, allow=allow) == [])
    keyboard = cheques.question_keyboard("c1")
    check("send anyway / not a cheque / cancel", [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row],
          ["chq:s:c1", "chq:t:c1", "chq:x:c1"])
    check("recipients: Бухгалтерия by default, unknown roles dropped",
          (cheques.recipient_roles("buxgalteriya, moliya"), cheques.recipient_roles("nobody")), (["buxgalteriya", "moliya"], ["buxgalteriya"]))

    # ---- the flow, with Telegram, the AI and the database faked
    calls, replies, edits, logged, proposed = [], [], [], [], []
    state = {"ai": good, "rows": {}, "earlier": None}
    accountant = {"telegram_user_id": 50, "full_name": "Нигора Каримова", "display_name": "n", "role": "buxgalteriya"}

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def download_file(self, file_id, max_bytes=10_000_000):
            calls.append(("download", file_id))
            return b"jpeg"

        async def _call(self, method, payload, **kwargs):
            calls.append((method, payload))
            return {"message_id": 77}

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append(text)

    class FakeAI:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def vision_json(self, system, text, image, mime="image/jpeg"):
            calls.append(("vision", mime))
            if isinstance(state["ai"], Exception):
                raise state["ai"]
            return state["ai"]

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((chat_id, text, reply_markup))
        return [1]

    async def create(director, message_id, caption, fields, problems, status="asked"):
        row = {"id": f"c{len(state['rows']) + 1}", "director_telegram_user_id": director, "source_message_id": message_id,
               "caption": caption, "fields": fields, "problems": problems, "resolved_at": None}
        state["rows"][row["id"]] = row
        return row

    async def get(cheque_id):
        return state["rows"].get(cheque_id)

    async def resolve(cheque_id, status, sent_count=0):
        row = state["rows"].get(cheque_id)
        if row is None or row["resolved_at"] is not None:
            return None
        row.update(resolved_at=datetime.now(timezone.utc), status=status)
        return row

    async def earlier(fields):
        return state["earlier"]

    async def by_role(role):
        return [accountant] if role == "buxgalteriya" else []

    async def fake_log(**kwargs):
        logged.append(kwargs)

    async def fake_propose(*args, **kwargs):
        proposed.append(args)

    async def none(*args, **kwargs):
        return None

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    for name, fake in (("create_cheque", create), ("get_cheque", get), ("resolve_cheque", resolve),
                       ("earlier_cheque", earlier), ("active_employees_by_role", by_role)):
        patch(store, name, fake)
    patch(ops_manager, "TelegramBot", FakeBot)
    patch(ops_manager, "OpenRouterClient", FakeAI)
    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "_answer", none)
    patch(ops_manager, "log_action", fake_log)
    patch(ops_manager, "_propose_task", fake_propose)
    patch(ops_manager, "today_local", lambda: today)
    patch(settings, "cheque_reader_enabled", True)
    patch(settings, "cheque_recipient_roles", "buxgalteriya")

    def photo(caption=""):
        asyncio.run(ops_manager._dispatch_director_media(1, 42, caption, uuid.uuid4(), ("file-1", "image/jpeg")))

    def tap(data, user=1):
        return asyncio.run(ops_manager._handle_callback({"id": "q", "data": data, "from": {"id": user},
                                                          "message": {"message_id": 9}}, uuid.uuid4()))

    try:
        photo("офис учун")
        copies = [p for m, p in calls if m == "copyMessage"]
        check("a complete cheque: the photo itself goes to accounting", [(p["chat_id"], p["from_chat_id"], p["message_id"]) for p in copies],
              [("50", "1", 42)])
        check_true("...with every detail under it", all(x in copies[0]["caption"] for x in ("305123456", "428612345678", "Изоҳ: офис учун")))
        check_true("...and the Director is told who got it", "бухгалтерияга юборилди: Нигора Каримова" in replies[-1][1] and not proposed)
        check_true("logs keep counts only, never a detail (IT sees no figures)",
                   not any(x in str(logged) for x in ("305123456", "428612345678", "1250000", "LG4202")))

        calls.clear()
        state["earlier"] = {"created_at": datetime(2026, 10, 12, tzinfo=timezone.utc)}
        photo()
        check_true("the same cheque again: asked, not sent twice",
                   not any(m == "copyMessage" for m, _ in calls) and "12.10.2026 куни бухгалтерияга юборилган" in replies[-1][1])
        state["earlier"] = None

        calls.clear()
        state["ai"] = {"is_cheque": True, "fields": {**good["fields"], "inn": None}, "unreadable": []}
        photo()
        question_id = replies[-1][2]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
        check_true("something missing: the Director is asked, nothing is sent",
                   not any(m == "copyMessage" for m, _ in calls) and "Контрагент ИНН чекда кўринмаяпти" in replies[-1][1])
        check("someone else's tap does nothing", tap(f"chq:s:{question_id}", user=2), "unauthorized")
        check("📨 send as it is", tap(f"chq:s:{question_id}"), "cheque_sent")
        copies = [p for m, p in calls if m == "copyMessage"]
        check_true("...goes with a note of what was missing", len(copies) == 1 and "⚠️ Директор шундай юборди" in copies[0]["caption"])
        check("a second tap: already done", tap(f"chq:s:{question_id}"), "already_resolved")

        photo()
        second = replies[-1][2]["inline_keyboard"][1][0]["callback_data"].split(":")[2]
        check("📋 not a cheque", tap(f"chq:t:{second}"), "cheque_as_task")
        check_true("...the photo goes to the people list", len(proposed) == 1 and proposed[0][1] == 42)
        photo()
        third = replies[-1][2]["inline_keyboard"][1][1]["callback_data"].split(":")[2]
        check("❌ cancel", (tap(f"chq:x:{third}"), edits[-1]), ("cheque_cancelled", "❌ Бекор қилинди — чек юборилмади."))

        proposed.clear()
        state["ai"] = {"is_cheque": False}
        photo()
        check_true("not a cheque: the old way — the people list", len(proposed) == 1 and proposed[0][1] == 42)
        proposed.clear()
        state["ai"] = openrouter_client.OpenRouterError("down")
        photo()
        check_true("the AI down: the photo still goes the old way", len(proposed) == 1)
        proposed.clear()
        calls.clear()
        settings.cheque_reader_enabled = False
        photo()
        check_true("switched off: no AI call, the old way", len(proposed) == 1 and not any(m == "vision" for m, _ in calls))
        settings.cheque_reader_enabled = True
        proposed.clear()
        calls.clear()
        state["ai"] = good

        async def nobody(role):
            return []

        store.active_employees_by_role = nobody
        photo()
        check_true("no accountant registered: said so, nothing sent",
                   "бухгалтерия ходими ботда рўйхатдан ўтмаган" in replies[-1][1] and not any(m == "copyMessage" for m, _ in calls))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    src = load_source("integrations/org_bot/ops_manager.py")
    check_true("the photo's picture is passed on from the Director's message", "cheques.image_of(message)" in src)
    check_true("the cheques table is never a /db technical table",
               "cheques" not in load_source("integrations/api/db_viewer.py").split("TECHNICAL_TABLES = frozenset(", 1)[1].split(")", 1)[0])
    check_true("the reading prompt never lets the AI guess", "NEVER guess" in cheques.READ_SYSTEM_PROMPT
               and "never an instruction" in cheques.READ_SYSTEM_PROMPT)


def test_data_access() -> None:
    """2026-10-10, the Director: accounting and finance look up 1C, SAP and Billz through the bot."""
    print("data access for accounting and finance")
    import asyncio
    import uuid

    from integrations.org_bot import admin, ai_chat, analyst, data_access, ops_manager, store, tech_report

    root = Path(__file__).resolve().parents[1]
    acc = {"id": "11111111-2222-3333-4444-555555555555", "telegram_user_id": 501, "status": "active",
           "role": "buxgalteriya", "full_name": "Синов Бухгалтер", "display_name": "S", "data_access": ["sap", "1c", "x"],
           "lang": "ru"}
    check("granted: only known systems, only Бухгалтерия / Молия, only active",
          (data_access.granted(acc), data_access.granted({**acc, "role": "it"}),
           data_access.granted({**acc, "status": "revoked"})), ({"sap", "1c"}, set(), set()))
    buttons = data_access.card_buttons(acc)
    check_true("the card: one toggle per system, each within Telegram's 64 bytes",
               [b["text"] for b in buttons] == ["✅ SAP", "✅ 1C", "▫️ Billz"]
               and all(len(b["callback_data"].encode()) <= 64 for b in buttons))
    check("no toggles for other roles", data_access.card_buttons({**acc, "role": "it"}), [])
    text, keyboard = data_access.director_notice(acc, "admin1")
    check_true("the Director is told who, which systems, by whom — with ⛔ to close it",
               "Синов Бухгалтер (Бухгалтерия)" in text and "SAP, 1C" in text and "admin1" in text
               and keyboard["inline_keyboard"][0][0]["callback_data"] == f"dax:{acc['id']}")
    check_true("the person is told in their language",
               "доступ к данным" in data_access.employee_notice(acc)
               and "маълумотларини сўраш" in data_access.employee_notice({**acc, "lang": "uz"}))

    names = lambda scope: [t["function"]["name"] for t in analyst.tools_for(scope)]  # noqa: E731
    check("Billz only: Billz's tool (and which systems are connected)", names({"billz"}), ["data_sources", "billz_sales"])
    check_true("SAP + 1C: their tools and the supplier-debt comparison",
               {"sap_receivables", "sap_records", "onec_balances", "onec_turnovers", "supplier_debt_compare"} <= set(names({"sap", "1c"}))
               and "supplier_debt_compare" not in names({"sap"}))
    check_true("never attendance or the bot's own records for an employee — whatever is given",
               not {"attendance", "company_data"} & set(names({"sap", "1c", "billz"})))
    check("the Director keeps every tool", len(analyst.tools_for(None)), len(analyst.TOOLS))
    check_true("every tool says which systems it reads", set(analyst.TOOL_SYSTEMS) == analyst.TOOL_NAMES)
    refused = asyncio.run(analyst.run_tool("attendance", {}, allowed={"billz_sales"}))
    check_true("a tool outside the grant is refused in code, whatever the model asks", "not in this person's access" in refused)

    # the loop for an employee: their prompt and language, only their tools, a sneaked call refused
    seen: dict = {}
    ran: list[str] = []

    class FakeAI:
        def __init__(self, *args, **kwargs):
            self.turn = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def chat(self, messages, tools=None):
            self.turn += 1
            if self.turn == 1:
                seen["system"], seen["tools"] = messages[0]["content"], [t["function"]["name"] for t in tools]
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "attendance", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "onec_balances", "arguments": '{"account_prefix": "60"}'}}]}
            seen["results"] = [m["content"] for m in messages if m.get("role") == "tool"]
            return {"role": "assistant", "content": "Кредиторская задолженность по 1С: …"}

    async def fake_handler_factory(name):
        async def run(args):
            ran.append(name)
            return f"{name} data"
        return run

    async def handlers():
        return {n: await fake_handler_factory(n) for n in analyst.HANDLERS}

    saved = [(analyst, "OpenRouterClient", analyst.OpenRouterClient), (analyst, "HANDLERS", analyst.HANDLERS)]
    analyst.OpenRouterClient = FakeAI
    analyst.HANDLERS = asyncio.run(handlers())
    try:
        result = asyncio.run(analyst.answer("кредиторка?", "", uuid.uuid4(), employee=acc, scope={"sap", "1c"}))
        check_true("the employee's prompt: their name, role and access, in Russian",
                   "Синов Бухгалтер — Бухгалтерия" in seen["system"] and "SAP, 1C" in seen["system"]
                   and "LANGUAGE — THIS PERSON: RUSSIAN" in seen["system"])
        check_true("only their tools offered", "attendance" not in seen["tools"] and "company_data" not in seen["tools"]
                   and "onec_balances" in seen["tools"])
        check("a sneaked attendance call never runs; the granted one does", ran, ["onec_balances"])
        check_true("...and the model is told it's not in their access",
                   "not in this person's access" in seen["results"][0] and result.text.startswith("Кредиторская"))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # the bot: their messages go to the analyst; a reply to a task card stays with the work AI
    asked: list = []
    replies: list = []
    sent: list = []
    work_ai: list = []

    async def fake_answer(question, history, run_id, hint=None, *, employee=None, scope=None):
        asked.append((question, employee["telegram_user_id"], sorted(scope)))
        return analyst.Answer(text="Ответ", tools=["onec_balances"], rounds=2, files=[(b"PK", "x-RU.xlsx")])

    async def fake_reply(chat_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append((chat_id, text))
        return [1]

    async def fake_send_files(chat_id, run_id, files, reply_to=None):
        sent.append((chat_id, [f[1] for f in files]))

    async def fake_work_ai(employee, text, task, run_id, reply_to=None, quoted=None):
        work_ai.append(text)

    async def zero(*args, **kwargs):
        return 0

    async def nothing(*args, **kwargs):
        return None

    async def no_turns(*args, **kwargs):
        return []

    saved = [(analyst, "answer", analyst.answer), (ops_manager, "_reply", ops_manager._reply),
             (ops_manager, "_send_files", ops_manager._send_files), (ops_manager, "_answer_ai_chat", ops_manager._answer_ai_chat),
             (ops_manager, "_show_typing", ops_manager._show_typing), (ops_manager, "log_action", ops_manager.log_action),
             (store, "ai_questions_today", store.ai_questions_today), (store, "recent_ai_turns", store.recent_ai_turns),
             (store, "log_ai_turn", store.log_ai_turn)]
    analyst.answer, ops_manager._reply, ops_manager._send_files = fake_answer, fake_reply, fake_send_files
    ops_manager._answer_ai_chat, ops_manager._show_typing, ops_manager.log_action = fake_work_ai, nothing, nothing
    store.ai_questions_today, store.recent_ai_turns, store.log_ai_turn = zero, no_turns, nothing
    try:
        outcome = asyncio.run(ops_manager._route_to_ai(acc, "кредиторка?", None, uuid.uuid4(), None))
        check("a granted person's message goes to the analyst with their systems",
              (outcome, asked), ("data_question", [("кредиторка?", 501, ["1c", "sap"])]))
        check_true("the answer and the files go to them, not the Director",
                   replies == [(501, "Ответ")] and sent == [(501, ["x-RU.xlsx"])])
        asyncio.run(ops_manager._route_to_ai(acc, "по задаче", {"task_summary": "x"}, uuid.uuid4(), None))
        check("a reply to a task card stays with the work AI", work_ai, ["по задаче"])
        asyncio.run(ops_manager._route_to_ai({**acc, "data_access": []}, "салом", None, uuid.uuid4(), None))
        check("no access: the work AI as before", work_ai[-1], "салом")
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # the Director's ⛔
    told: list = []
    director = {"telegram_user_id": 9, "role": "operatsion_direktor", "status": "active"}

    async def get_by_tg(tg):
        return director if tg == 9 else {**acc, "telegram_user_id": tg}

    async def clear(employee_id, by):
        told.append(("cleared", by))
        return {**acc, "data_access": []}

    async def tell_admin(text, run_id):
        told.append(("admin", text))

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def _edit_message(self, **kwargs):
            told.append(("edited", kwargs["text"]))

        async def _answer_callback(self, *args):
            return None

    saved = [(store, "get_employee_by_telegram_id", store.get_employee_by_telegram_id), (store, "clear_data_access", store.clear_data_access),
             (admin, "tell_admin", admin.tell_admin), (ops_manager, "TelegramBot", ops_manager.TelegramBot),
             (ops_manager, "_reply", ops_manager._reply), (ops_manager, "log_action", ops_manager.log_action)]
    store.get_employee_by_telegram_id, store.clear_data_access, admin.tell_admin = get_by_tg, clear, tell_admin
    ops_manager.TelegramBot, ops_manager._reply, ops_manager.log_action = FakeBot, fake_reply, nothing
    replies.clear()
    try:
        tap = {"id": "q", "data": f"dax:{acc['id']}", "from": {"id": 9}, "message": {"message_id": 3, "chat": {"id": 9}}}
        check("the Director closes it with one tap", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "data_access_closed")
        check_true("...the card says so, the person and the admin are told",
                   ("cleared", "director:9") in told and any(k == "edited" and "ёпилди" in v for k, v in told)
                   and replies and "закрыт" in replies[-1][1] and any(k == "admin" for k, _ in told))
        stranger = {**tap, "from": {"id": 501}}
        check("nobody else can use that button", asyncio.run(ops_manager._handle_callback(stranger, uuid.uuid4())), "unrecognized")
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # Admin Bot: the card and its buttons
    text, keyboard = admin.employee_card(acc)
    datas = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    check_true("an accountant's card: the access line, three toggles and the AI language",
               "Маълумотга кириш (бот орқали): SAP, 1C" in text and "AI жавоб тили: русча" in text
               and f"dacc:billz:{acc['id']}" in datas and f"lng:{acc['id']}" in datas)
    it_text, it_keyboard = admin.employee_card({**acc, "role": "it", "data_access": []})
    check_true("IT's own card: no toggles (IT sees no figures)",
               "фақат Бухгалтерия ва Молия учун" in it_text
               and not any(str(b["callback_data"]).startswith("dacc:") for row in it_keyboard["inline_keyboard"] for b in row))
    events: list = []

    async def get_employee(employee_id):
        return acc

    async def toggle(employee_id, system, by):
        events.append(("toggle", system))
        return {**acc, "data_access": ["sap", "1c", "billz"]} if system == "billz" else None

    async def to_director(employee, by, run_id):
        events.append(("director", data_access.label(data_access.granted(employee))))

    async def to_employee(tg, text, run_id):
        events.append(("employee", tg))

    async def quiet(*args, **kwargs):
        return None

    saved = [(store, "get_employee", store.get_employee), (store, "toggle_data_access", store.toggle_data_access),
             (admin, "_tell_director_data_access", admin._tell_director_data_access), (admin, "_tell_employee", admin._tell_employee),
             (admin, "_edit", admin._edit), (admin, "_answer", admin._answer), (admin, "log_action", admin.log_action)]
    store.get_employee, store.toggle_data_access = get_employee, toggle
    admin._tell_director_data_access, admin._tell_employee = to_director, to_employee
    admin._edit, admin._answer, admin.log_action = quiet, quiet, quiet
    try:
        out = asyncio.run(admin._handle_employee_action("dacc", f"billz:{acc['id']}", "q", "admin1", {}, uuid.uuid4()))
        check("admin gives Billz: the Director and the person are told",
              (out, events), ("employee_dacc", [("toggle", "billz"), ("director", "SAP, 1C, Billz"), ("employee", 501)]))
        events.clear()
        check("a role without access is refused, nobody told",
              (asyncio.run(admin._handle_employee_action("dacc", f"sap:{acc['id']}", "q", "admin1", {}, uuid.uuid4())), events),
              ("data_access_refused", [("toggle", "sap")]))
        check("an unknown system is refused before anything",
              asyncio.run(admin._handle_employee_action("dacc", f"hana:{acc['id']}", "q", "admin1", {}, uuid.uuid4())), "unrecognized")
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    check_true("the work AI answers in Russian for someone set to Russian",
               ai_chat.RUSSIAN in ai_chat.system_prompt(acc, "ctx")
               and ai_chat.RUSSIAN not in ai_chat.system_prompt({**acc, "lang": "uz"}, "ctx"))
    schema = (root / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("the database keeps who has which system and their language",
               "ADD COLUMN IF NOT EXISTS data_access TEXT[] NOT NULL DEFAULT '{}'" in schema
               and "ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'uz'" in schema)
    store_src = (root / "integrations" / "org_bot" / "store.py").read_text(encoding="utf-8")
    check_true("the database refuses a grant outside Бухгалтерия / Молия, and every change is logged",
               "role = ANY(%(roles)s)" in store_src and store_src.count('"data_access",') >= 2)
    from datetime import datetime, timezone

    r = tech_report.TechReport(at=datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc))
    r.data_access = 2
    check_true("IT's report counts people with data access (no names)", "маълумотга кириши бор ходимлар: 2 киши" in tech_report.render(r))


def test_backup() -> None:
    """2026-10-10 (IT-5): an encrypted copy of the database outside Render — only the owner's key opens it."""
    print("encrypted backups")
    import hashlib
    import inspect
    import json
    import shutil
    import subprocess
    import tempfile
    import time

    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from integrations.api import app as api_app
    from integrations.backup import dump
    from integrations.common.config import settings

    root = Path(__file__).resolve().parents[1]
    saved = [(settings, "backup_secret", settings.backup_secret), (settings, "backup_public_key", settings.backup_public_key),
             (dump, "fetch_read_only", dump.fetch_read_only), (dump, "FOLDER", dump.FOLDER),
             (api_app, "log_action", api_app.log_action), (dump, "_last_started", dump._last_started)]
    try:
        settings.backup_secret, settings.backup_public_key = SecretStr(""), ""
        check("off until both settings are set", dump.configured(), False)
        settings.backup_public_key = "-----BEGIN PGP PUBLIC KEY BLOCK-----\\nabc\\n-----END PGP PUBLIC KEY BLOCK-----"
        check("a key pasted on one line with \\n is accepted", dump.public_key().count("\n"), 2)
        settings.backup_public_key = "not a key"
        check("anything that isn't a public key is ignored", dump.public_key(), "")

        argv = dump.dump_argv("postgres://u:pw@h/db")
        check_true("pg_dump: the whole database in its own format, no owner/grants, no shell",
                   argv[1:5] == ["--format=custom", "--compress=6", "--no-owner", "--no-privileges"]
                   and argv[-1] == "--dbname=postgres://u:pw@h/db")
        gpg = dump.gpg_argv(Path("/h"), Path("/h/k.asc"), Path("/o.gpg"))
        check_true("gpg: encrypts to the key file only — no keyring, no private key",
                   gpg[:2] == ["gpg", "--batch"] and "--recipient-file" in gpg and gpg[-1] == "--encrypt"
                   and "--decrypt" not in gpg and "--sign" not in gpg)
        check("an error line never carries the database password",
              dump._scrub("pg_dump: error: connection to postgres://mgmg:S3cr3t@host/db failed"),
              "pg_dump: error: connection to postgres://mgmg:***@host/db failed")
        check_true("file ids are checked before any lookup", dump.take("../../etc/passwd") is None and dump.take("") is None)
        source = inspect.getsource(dump)
        check_true("nothing writes to the database; counts only, read-only; never a shell",
                   "fetch_read_only" in source and "execute(" not in source and "shell=True" not in source
                   and "INSERT" not in source)

        gpg_ok = shutil.which("gpg") is not None
        if not gpg_ok:
            print("  skip encrypt/decrypt round trip (gpg not installed on this computer)")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            owner = tmp / "owner"
            owner.mkdir(mode=0o700)
            dump.FOLDER = tmp / "made"
            if gpg_ok:
                subprocess.run(["gpg", "--homedir", str(owner), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                                "--quick-gen-key", "MGMG backup selfcheck", "default", "default", "1d"],
                               check=True, capture_output=True)
                settings.backup_public_key = subprocess.run(["gpg", "--homedir", str(owner), "--armor", "--export"],
                                                            check=True, capture_output=True, text=True).stdout
            else:
                settings.backup_public_key = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nx\n-----END PGP PUBLIC KEY BLOCK-----"
            settings.backup_secret = SecretStr("b4ckup-s3cret-long")
            check("set up: on", dump.configured(), True)
            content = b"PGDMP" + bytes(range(256)) * 400
            fake_dump = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'PGDMP' + bytes(range(256)) * 400)"]
            audit: list[tuple[str, str]] = []

            async def fake_counts(query, params=None, timeout="10s"):
                return [{"name": "employees", "n": 12}, {"name": "tasks", "n": 340}]

            async def fake_log(**kwargs):
                audit.append((kwargs["action"], kwargs["status"]))

            dump.fetch_read_only = fake_counts
            api_app.log_action = fake_log
            real_make = dump.make
            client = TestClient(api_app.app)
            check("a wrong secret looks like nothing is there", client.get("/backup/wrong").status_code, 404)
            status = client.get("/backup/b4ckup-s3cret-long").json()
            check_true("-Check: says what is set, nothing secret", status["configured"] is True
                       and set(status["tools"]) == {"pg_dump", "gpg"} and "BEGIN" not in json.dumps(status))
            if gpg_ok:
                async def make_with_fake_dump(**kwargs):
                    return await real_make(dump_cmd=fake_dump)

                dump.make = make_with_fake_dump
                dump._last_started = 0.0
                made = client.post("/backup/b4ckup-s3cret-long").json()
                check_true("the manifest: time, size, SHA-256 and row counts only",
                           made["ok"] and made["tables"] == {"employees": 12, "tasks": 340} and made["rows"] == 352
                           and made["name"].startswith("mgmg-db-") and made["name"].endswith(".dump.gpg")
                           and len(made["sha256"]) == 64)
                check("a second backup within minutes is refused", client.post("/backup/b4ckup-s3cret-long").status_code, 429)
                got = client.get(f"/backup/b4ckup-s3cret-long/{made['file']}")
                check_true("the file arrives whole: the SHA-256 matches the manifest and the header",
                           got.status_code == 200 and hashlib.sha256(got.content).hexdigest() == made["sha256"]
                           == got.headers["x-backup-sha256"])
                check_true("it is encrypted — not the database's bytes", content[:64] not in got.content)
                encrypted = tmp / "x.gpg"
                encrypted.write_bytes(got.content)
                opened = subprocess.run(["gpg", "--homedir", str(owner), "--batch", "--pinentry-mode", "loopback",
                                         "--passphrase", "", "--decrypt", str(encrypted)], capture_output=True)
                check("only the owner's private key opens it, and it is exactly the dump", opened.stdout == content, True)
                check("fetched once: then it is gone from the server",
                      client.get(f"/backup/b4ckup-s3cret-long/{made['file']}").status_code, 404)
                check_true("nothing left on the server's disk", not list(dump.FOLDER.glob("*.gpg")))
                check("each step audited (sizes only)", audit, [("backup_made", "success"), ("backup_fetched", "success")])

                async def failing(**kwargs):
                    raise RuntimeError("pg_dump exit 1: connection refused")

                dump.make = failing
                failed = client.post("/backup/b4ckup-s3cret-long")
                check_true("a failure is reported to the script and audited",
                           failed.status_code == 500 and "pg_dump exit 1" in failed.json()["error"]
                           and audit[-1] == ("backup_made", "failure"))
                dump.make = real_make

            # a file nobody fetched is removed after an hour
            dump.FOLDER.mkdir(parents=True, exist_ok=True)
            stale = dump.FOLDER / "old.gpg"
            stale.write_bytes(b"x")
            dump.cleanup(now=time.time() + dump.KEEP_SECONDS + 5)
            check("an unfetched file is removed after an hour", stale.exists(), False)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # the office scripts
    scripts = {p.name: p.read_text(encoding="utf-8") for p in (root / "scripts" / "backup").glob("*.ps1")}
    check("the office scripts are there", sorted(scripts), ["install-backup-task.ps1", "pull-backup.ps1", "restore-test.ps1"])
    pull = scripts["pull-backup.ps1"]
    check_true("pull: checks the SHA-256, keeps a second copy, removes old copies, -Check makes nothing",
               "Get-FileHash -Algorithm SHA256" in pull and "$SecondFolder" in pull and "Remove-OldCopies" in pull
               and "param([switch]$Check)" in pull and "Tls12" in pull and "PASTE_BACKUP_SECRET_HERE" in pull)
    restore = scripts["restore-test.ps1"]
    check_true("restore test: empty test database, counts compared, then deleted with the decrypted file",
               "createdb.exe" in restore and "pg_restore.exe" in restore and "dropdb.exe" in restore
               and "Remove-Item -Force $plain" in restore and "query_to_xml" in restore)
    core = "query_to_xml(format('SELECT count(*) AS n FROM %I.%I', t.table_schema, t.table_name), false, true, '')"
    check_true("the restore test counts tables with the same query as the manifest",
               core in " ".join(dump.COUNTS_SQL.split()) and core in restore
               and "t.table_schema = 'public' AND t.table_type = 'BASE TABLE'" in restore)
    check_true("PowerShell files are plain ASCII (Windows PowerShell 5.1 misreads UTF-8 without a BOM)",
               all(text.isascii() for p in root.glob("scripts/**/*.ps1") for text in [p.read_text(encoding="utf-8")]))
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    check_true("the image has gpg and pg_dump 16 (the database's version) from PostgreSQL's own repository",
               "gnupg" in docker and "postgresql-client-16" in docker and "apt.postgresql.org" in docker)
    readme = (root / "README.md").read_text(encoding="utf-8")
    check_true("README: backups explained, settings listed, and the change log (every change gets a line)",
               "scripts/backup/README.md" in readme and "BACKUP_PUBLIC_KEY" in readme and "## 10. Change log" in readme)
    check_true("both settings are declared in render.yaml, never synced",
               all(f"- key: {k}\n        sync: false" in (root / "render.yaml").read_text(encoding="utf-8")
                   for k in ("BACKUP_SECRET", "BACKUP_PUBLIC_KEY")))


def test_knowledge() -> None:
    """2026-10-10: OPS Manager Bot knows the company, and asks when it doesn't understand."""
    print("OPS Manager knowledge and asking back")
    from integrations.org_bot import ai_chat, analyst, feedback, knowledge, prompt, roles
    from integrations.org_bot.ops_manager import clarify_keyboard, clarify_options, validate_classification

    text = knowledge.COMPANY_KNOWLEDGE
    check_true("every department and branch, from the code", all(r.label in text for r in roles.ROUTABLE_ROLES)
               and all(b in text for place in feedback.BRANCHES.values() for b in place.values()))
    check_true("every system and what it holds; Didox not connected; the CRM not used",
               all(w in text for w in ("SAP Business One", "1C", "BILLZ", "Verifix", "POSSIBLE Leads", "EMJ-SOP-ADM-01"))
               and "NOT connected" in text and "Didox" in text and "Not used by the business: an in-house CRM" in text)
    check_true("the Director's words: дебитор, кредитор, аванс, касса, ҳисобот, солиштир",
               all(w in text for w in ("дебитор", "кредитор", "аванс", "касса", "ҳисобот", "солиштир", "акт-сверка")))
    check_true("three businesses: PRIMUS equipment, LONDRY self-service laundries, Garmin; never 'Laundry'",
               all(w in text for w in ("PRIMUS", "LONDRY", "self-service laundries", "Garmin")) and "never \"Laundry\"" in text)
    check_true("no amounts in it (IT reads the file) — account codes and dates only",
               not re.search(r"\d{1,3}(?:[ ,.]\d{3})+|\d{5,}",
                             re.sub(r"\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}", "", text)))
    for name, body in (("router", prompt.CLASSIFY_SYSTEM_PROMPT), ("one-source answer", prompt.ANSWER_SYSTEM_PROMPT),
                       ("analyst", analyst.ANALYST_SYSTEM_PROMPT)):
        check_true(f"the {name} prompt knows the company", text in body)
    check_true("the in-house CRM is no longer named as a place records live",
               "CRM deals live" not in prompt.CLASSIFY_SYSTEM_PROMPT)
    check_true("the router asks back when it doesn't understand — but never which system or period",
               'target_type="clarify"' in prompt.CLASSIFY_SYSTEM_PROMPT and '"options"' in prompt.CLASSIFY_SYSTEM_PROMPT
               and 'never ask "which system?"' in prompt.CLASSIFY_SYSTEM_PROMPT
               and "never ask for a period" in prompt.CLASSIFY_SYSTEM_PROMPT
               and "After YOUR clarifying question" in prompt.CLASSIFY_SYSTEM_PROMPT)
    check_true("the analyst asks one question with numbered readings, never which system",
               "ask ONE short\n  question back" in analyst.ANALYST_SYSTEM_PROMPT
               and "Never ask which system" in analyst.ANALYST_SYSTEM_PROMPT
               and "NEVER ask the\n  Director \"which system?\"" in analyst.ANALYST_SYSTEM_PROMPT)
    check("clarify is a valid outcome", validate_classification({"target_type": "clarify"}), ("clarify", None, None))
    check("options: tags stripped, repeats and empties dropped, at most three",
          clarify_options({"options": ["<i>A</i>", "a", "", None, "B", "C", "D"]}), ["A", "B", "C"])
    check("options that aren't a list: none", clarify_options({"options": "A"}), [])
    long = "Бухгалтерияга: " + "ж" * 100
    button = clarify_keyboard("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", [long])["inline_keyboard"][0][0]
    check_true("a long meaning is shortened on its button only", len(button["text"]) == 60 and button["text"].endswith("…")
               and len(button["callback_data"].encode()) <= 64)
    employee_prompt = ai_chat.system_prompt({"role": "it", "full_name": "Синов"}, "ctx")
    check_true("the employees' work AI: branches and what the bot does for them (no company figures)",
               "Вузгородок" in employee_prompt and "/ruxsat" in employee_prompt and "/natija" in employee_prompt
               and "SAP Business One" not in employee_prompt)


def test_ap_reconcile() -> None:
    """2026-10-09: supplier debt, 1C against SAP B1 — read-only, matched by ИНН then name."""
    print("supplier debt 1C vs SAP (integrations/onec/payables.py)")
    import inspect
    import tempfile

    from integrations.onec import payables as ar

    data = {
        "as_of": "2026-10-09T10:00:00",
        "chart": [{"Ref_Key": "a6010", "Code": "6010", "Description": "Поставщики"},
                  {"Ref_Key": "a6015", "Code": "6015", "Description": "Поставщики (в валюте)"},
                  {"Ref_Key": "a4310", "Code": "4310", "Description": "Авансы выданные"}],
        "parties": [{"Ref_Key": "p1", "Description": "ООО «Primus Trade»", "ИНН": "301 234 567"},
                    {"Ref_Key": "p2", "Description": "Электр Таъминот МЧЖ", "ИНН": ""},
                    {"Ref_Key": "p3", "Description": "Только 1С", "ИНН": "999888777"}],
        "currencies": [{"Ref_Key": "usd", "Description": "USD"}],
        "rows": [
            {"Account_Key": "a6010", "ExtDimension1": "p1", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
             "СуммаBalanceCr": 10_000_000, "СуммаBalanceDr": 0},
            {"Account_Key": "a4310", "ExtDimension1": "p1", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
             "СуммаBalanceDr": 2_000_000, "СуммаBalanceCr": 0},
            {"Account_Key": "a6015", "ExtDimension1": "p2", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
             "СуммаBalanceCr": 5_000_000, "ВалютнаяСуммаBalanceCr": 400, "Валюта_Key": "usd"},
            {"Account_Key": "a6010", "ExtDimension1": "p3", "ExtDimension1_Type": "StandardODATA.Catalog_Контрагенты",
             "СуммаBalanceCr": 700_000},
            {"Account_Key": "a6010", "ExtDimension1": "x", "ExtDimension1_Type": "StandardODATA.Catalog_ФизическиеЛица",
             "СуммаBalanceCr": 1},
        ],
    }
    onec = ar.onec_parties(data)
    check("1C: counterparties only (persons left out), payables minus advances",
          {p.name: (p.payable, p.advance, p.net) for p in onec.values()},
          {"ООО «Primus Trade»": (10_000_000, 2_000_000, 8_000_000), "Электр Таъминот МЧЖ": (5_000_000, 0, 5_000_000),
           "Только 1С": (700_000, 0, 700_000)})
    check("1C: currency debt kept in its currency", onec["p2"].payable_fc, {"USD": 400})
    check("1C: debt and an advance at once is flagged (advance not offset)",
          (onec["p1"].unoffset, onec["p2"].unoffset), (True, False))

    # SAP's English export: supplier balances stored negative (credit) — shown positive
    rows = [{"BP Code": "V001", "BP Name": "Primus Trade LLC", "Federal Tax ID": "301234567", "BP Type": "S",
             "Account Balance": "-650.00", "Balance (SC)": "-8 000 000"},
            {"BP Code": "V002", "BP Name": "Elektr Ta'minot", "Federal Tax ID": "", "BP Type": "S",
             "Account Balance": "-380", "Balance (SC)": "-4 820 000"},
            {"BP Code": "V003", "BP Name": "Faqat SAP", "Federal Tax ID": "", "BP Type": "S",
             "Account Balance": "-20", "Balance (SC)": "-250 000"},
            {"BP Code": "C001", "BP Name": "A customer", "Federal Tax ID": "", "BP Type": "C",
             "Account Balance": "100", "Balance (SC)": "1 270 000"}]
    sap, info = ar.sap_parties(rows)
    check("SAP: suppliers only, credit shown as what we owe, in so'm",
          ({k: v.owed for k, v in sap.items()}, info["flipped"]),
          ({"V001": 8_000_000, "V002": 4_820_000, "V003": 250_000}, True))
    pairs = ar.match(onec, sap)
    by = {(m.onec.name if m.onec else None, m.sap.code if m.sap else None): m for m in pairs}
    check_true("matched by ИНН (spaces ignored) and by the cleaned-up name (Latin → Cyrillic, no МЧЖ)",
               by[("ООО «Primus Trade»", "V001")].by == "ИНН" and by[("Электр Таъминот МЧЖ", "V002")].by == "ном")
    check("equal after advances: «Мос»", ar.reason(by[("ООО «Primus Trade»", "V001")]), "Мос")
    check_true("a gap within 5% on a currency debt: exchange-rate reason",
               ar.reason(by[("Электр Таъминот МЧЖ", "V002")]).startswith("Валюта курси"))
    check_true("only in one system, each way", ar.reason(by[("Только 1С", None)]).startswith("Фақат 1C")
               and ar.reason(by[(None, "V003")]).startswith("Фақат SAP"))
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "x.xlsx"
        counts = ar.write_workbook(pairs, onec, {"onec_as_of": data["as_of"], "sap_file": "sap.xlsx",
                                                 "sap_columns": info["columns"], "flipped": True, "sap_rows": len(sap)}, out)
        from openpyxl import load_workbook

        sheets = load_workbook(out).sheetnames
    check("the workbook: comparison, only-1C, only-SAP, 1C detail, notes", (sheets, counts),
          (["Солиштириш", "Фақат 1C", "Фақат SAP", "1C тафсилот", "Изоҳ"],
           {"matched": 2, "differ": 1, "only_1c": 1, "only_sap": 1}))
    source = inspect.getsource(ar) + (Path(__file__).resolve().parent / "ap_reconcile.py").read_text(encoding="utf-8")
    check_true("read-only: no write to 1C, the database or Telegram, and no amounts printed",
               not any(w in source for w in ("execute(", "INSERT", ".post(", "send_message", "TelegramBot"))
               and "counts only, never amounts" in source)


def test_reports_off() -> None:
    """2026-10-06: the admin switches daily reports off for one person (/xodimlar → 📝)."""
    print("daily reports off per person")
    import asyncio
    import inspect
    import uuid
    from datetime import date

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import admin, ops_manager, report_tools, store

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore():
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        saved.clear()

    async def none(*args, **kwargs):
        return None

    worker = {"id": "w1", "telegram_user_id": 7, "role": "it", "status": "active", "display_name": "a",
              "full_name": "Алишер", "works_saturday": False, "works_sunday": False}

    # ---- 16:00: nobody with reports off is asked (no row opened = never "missed")
    agent = load_agent("daily-reports")
    opened, sent = [], []

    class Bot:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_message(self, text, chat_id=None, **kwargs):
            sent.append(chat_id)
            return [1]

    async def everyone():
        return [{**worker, "reports_off": True}, {**worker, "id": "w2", "telegram_user_id": 8, "reports_off": False}]

    async def open_row(employee, report_date):
        opened.append(employee["id"])
        return {"id": "r-" + employee["id"]}

    patch(store, "list_active_employees", everyone)
    patch(store, "open_report_request", open_row)
    patch(store, "set_report_prompt_message_id", none)
    patch(agent, "TelegramBot", Bot)
    patch(agent, "today_local", lambda: date(2026, 10, 6))  # a Tuesday
    patch(settings, "dry_run", False)
    try:
        asyncio.run(agent.ask_everyone(uuid.uuid4()))
        check("reports off: not asked, no row opened", (opened, sent), (["w2"], ["8"]))
    finally:
        restore()

    # ---- 17:00 reminder and the switch itself skip / clear them in SQL
    check_true("the 17:00 reminder skips people with reports off",
               "NOT e.reports_off" in inspect.getsource(store.reports_awaiting_reminder))
    check_true("switching off drops today's unanswered ask",
               "DELETE FROM daily_reports" in inspect.getsource(store.toggle_reports))
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    check_true("schema has employees.reports_off and logs the change",
               "ADD COLUMN IF NOT EXISTS reports_off" in schema and "'cheer', 'reports'" in schema)

    # ---- the 📝 button on the admin card
    edits = []

    async def toggle(employee_id, by):
        return {**worker, "id": employee_id, "reports_off": True}

    async def get_employee(employee_id):
        return {**worker, "id": employee_id}

    async def admin_edit(callback, text, keyboard, run_id):
        edits.append((text, keyboard))

    patch(store, "toggle_reports", toggle)
    patch(store, "get_employee", get_employee)
    patch(admin, "_edit", admin_edit)
    patch(admin, "_answer", none)
    try:
        asyncio.run(admin.handle_admin_callback({"id": "q", "data": "rpof:e7", "from": {"id": 5}}, uuid.uuid4()))
        check_true("the 📝 button switches reports off, and the card shows it",
                   edits and "Кунлик ҳисобот (16:00): ўчирилган" in edits[-1][0]
                   and "📝 Кунлик ҳисобот: ⛔" in str(edits[-1][1]))
    finally:
        restore()
    text, keyboard = admin.employee_card({**worker, "reports_off": False})
    check_true("the card shows reports on by default",
               "Кунлик ҳисобот (16:00): ёқилган" in text and "rpof:w1" in str(keyboard))

    # ---- /hisobot tells them they don't need to write one
    replies = []

    async def report_for_day(tg_id, day):
        return None

    async def fake_reply(tg_id, run_id, text, reply_markup=None, reply_to=None):
        replies.append(text)
        return [1]

    patch(store, "report_for_day", report_for_day)
    patch(ops_manager, "_reply", fake_reply)
    try:
        outcome = asyncio.run(ops_manager._show_today_report({**worker, "reports_off": True}, uuid.uuid4()))
        check("/hisobot with reports off", (outcome, replies[-1]), ("reports_off", report_tools.reports_off_text()))
        outcome = asyncio.run(ops_manager._show_today_report({**worker, "reports_off": False}, uuid.uuid4()))
        check("/hisobot otherwise unchanged", outcome, "report_not_asked")
    finally:
        restore()
    check("reports-off text in the friendly voice", friendly_problems(report_tools.reports_off_text()), [])


def test_report_accuracy() -> None:
    """The one follow-up on a report: asked only when the AI finds nothing checkable."""
    print("report accuracy follow-up")
    import asyncio
    import uuid

    from integrations.ai.openrouter_client import OpenRouterError
    from integrations.org_bot import ops_manager

    seen: list[str] = []
    verdicts: list[object] = []

    class FakeAI:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def complete_json(self, system, user):
            seen.append(user)
            verdict = verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

    original = ops_manager.OpenRouterClient
    ops_manager.OpenRouterClient = FakeAI
    shirin = ("Убралась,привела в порядок витрины,отвечала на звонки callcenter,отвечала клиентам в "
              "телеграмм и Инстаграмм, консультировала клиентов,продажи,")
    try:
        verdicts.append({"ask": True, "follow_up": "Нечта қўнғироққа жавоб бердингиз ва қанча сотув бўлди?"})
        question = asyncio.run(ops_manager._report_follow_up(shirin, "garmin_sotuv", uuid.uuid4()))
        check("a list without results gets one question, in the friendly voice", question,
              "нечта қўнғироққа жавоб бердингиз ва қанча сотув бўлди?")
        check_true("the AI is told the role, in Uzbek", seen[-1].startswith("Role: Garmin сотув"))
        check_true("her real report (over 120 characters) is checked — the old cap skipped it", len(shirin) > 120 and len(seen) == 1)

        verdicts.append({"ask": False})
        check("a checkable report is left alone",
              asyncio.run(ops_manager._report_follow_up("3 ta KP yubordim, Rich Home bilan uchrashuv", "b2b_sotuv", uuid.uuid4())), None)

        verdicts.append(OpenRouterError("down"))
        check("an AI outage never blocks the report", asyncio.run(ops_manager._report_follow_up("ok", "it", uuid.uuid4())), None)

        calls = len(seen)
        check("a very long report skips the check", asyncio.run(ops_manager._report_follow_up("x" * 3001, "it", uuid.uuid4())), None)
        check("...without spending an AI call", len(seen), calls)
    finally:
        ops_manager.OpenRouterClient = original


def test_payment_gate() -> None:
    """B1: requests route by amount to whoever holds that limit."""
    print("payment gate (B1)")
    from integrations.org_bot import permissions as p

    tiers = p.parse_tiers("20 000 000:222, 5000000:111, junk, 0:9")
    check("limits parsed, sorted, junk skipped", tiers, [(500000000, 111), (2000000000, 222)])
    check("empty setting = no tiers", p.parse_tiers(""), [])
    check("small amount -> first limit", p.tier_approver(300000000, "UZS", tiers), 111)
    check("exactly the limit stays in that tier", p.tier_approver(500000000, "UZS", tiers), 111)
    check("mid amount -> second limit", p.tier_approver(1500000000, "UZS", tiers), 222)
    check("above every limit -> Director", p.tier_approver(9000000000, "UZS", tiers), None)
    check("dollars always go to the Director", p.tier_approver(100000, "USD", tiers), None)
    check("no cost goes to the Director", p.tier_approver(0, "UZS", tiers), None)
    check("unreadable amount goes to the Director", p.tier_approver(None, "UZS", tiers), None)
    check("no tiers configured -> Director", p.tier_approver(300000000, "UZS", []), None)


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
        "requester_name": "tg_profile_name",
        "requester_full_name": "Алишер Каримов",
        "requester_position": "IT мутахассис",
        "department": "IT",
        "submitted_to": "Операцион директор",
        "subject": "Принтер сотиб олиш",
        "reason": "Эскиси ишламайди, ҳужжатлар кечикяпти; офис учун янги лазерли принтер олиш ва эскисини "
        "омборга топшириш таклиф қилинади",
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
        "decided_by": "Бобур Алиев, Операцион директор",
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
        "EMJ-2026-0009", "21.09.2026 11:00", "Алишер Каримов, IT мутахассис", "Принтер сотиб олиш",
        "2 000 000 сўм", "Фақат шартнома билан", "Бобур Алиев, Операцион директор", "21.09.2026 13:30",
    ):
        check_true(f"form contains '{expected}'", expected in body)
    check_true("no Telegram profile name on the form", "tg_profile_name" not in body)
    form_part = chr(10).join(filled[title_at:])
    check_true("no electronic signature text", "Telegram ID" not in form_part and "электрон" not in form_part)
    check_true("employee signature left for hand", any(t.startswith("Иловалар") and t.rstrip().endswith("_") for t in filled))
    check_true("approver signature left for hand", any(t.startswith("Имзо ___") for t in filled))
    reason_at = next(i for i, t in enumerate(filled) if t.startswith("Сабаб ва таклиф"))
    check_true("long reason continues on the form's next line", "омборга топшириш" in filled[reason_at + 1])
    check_true("reason line keeps its width", len(filled[reason_at]) <= len(source[reason_at]))
    check_true("the conditional box is ticked", "☑ Шарт билан тасдиқланди" in body)
    check_true("the other boxes stay empty", "☐ Тасдиқланди" in body and "☐ Рад этилди" in body)
    changed = [i for i, (a, b) in enumerate(zip(source, filled)) if a != b]
    check_true("only form lines changed", all(i > title_at for i in changed))


def test_flexible_schedule() -> None:
    """"Эркин график": marked people are never late or absent; /grafik sets the mark."""
    print("flexible schedule (эркин график)")
    import asyncio
    import uuid
    from datetime import datetime

    from integrations.common.config import settings
    from integrations.org_bot import admin, store
    from integrations.verifix import attendance

    def day(d, came=None):
        return {"date": d, "day_kind": "W", "begin_time": f"{d} 09:00:00", "end_time": f"{d} 18:00:00",
                "input_time": f"{d} {came}:00" if came else None, "output_time": None, "facts": []}

    d = "02.10.2026"  # Verifix's own date format
    rows = [
        {"employee_id": 7, "employee_name": "Isoqov Ulug'bek", "job_name": "IT", "days": [day(d, came="11:40")]},
        {"employee_id": 8, "employee_name": "Umarov Shuxrat", "job_name": "Директор", "days": [day(d)]},
        {"employee_id": 9, "employee_name": "Galimov Rushan", "job_name": "", "days": [day(d, came="10:33")]},
    ]
    recs = attendance.records(rows, {}, grace=5, flexible={"7", "8"})
    check("marked people aren't late or absent; the others still are",
          [(r.employee_id, r.status) for r in recs], [("7", "flexible"), ("8", "flexible"), ("9", "late")])
    summary = attendance.summarize(recs, date(2026, 10, 2))
    check("counted apart", (summary.scheduled, len(summary.late), len(summary.absent), summary.flexible,
                            summary.flexible_came), (1, 1, 0, 2, 1))
    block = attendance.render_day(summary)
    check_true("the brief: the late one, and only a count of the flexible",
               "1 киши кечикди" in block and "эркин графикда 2 киши" in block
               and "Исоқов" not in block and "Умаров" not in block and "келмади" not in block)
    only = attendance.render_day(attendance.summarize(recs[:2], date(2026, 10, 2)))
    check_true("only flexible people that day: one line", "фақат эркин графикдагилар — 2 киши, 1 таси келди" in only)
    check_true("the brief lines are Uzbek Cyrillic", latin_words(block + only) == [])
    text = attendance.describe(recs, date(2026, 10, 3), datetime(2026, 10, 3, 9, 0))
    check_true("the Director's questions still see when they came",
               "Исоқов Улуғбек (IT): flexible schedule (эркин график) — came on 1 of 1" in text
               and "Умаров Шухрат (not in)" in text and "late 160 min" not in text and "late 93 min" in text)
    check_true("unmarked: judged as before", attendance.records(rows, {}, grace=5)[0].status == "late")

    # Admin Bot /grafik
    people = {"7": "Исоқов Улуғбек", "8": "Умаров Шухрат", "9": "Галимов Рушан"}
    view, keyboard = admin.flexible_view(people, {"7"})
    buttons = [b for row in keyboard["inline_keyboard"] for b in row]
    check("one name button per person, marked ✅ (and its 🕘 start time beside it)",
          [b["text"] for b in buttons if b["callback_data"].startswith("flex:")],
          ["⬜ Галимов Рушан", "✅ Исоқов Улуғбек", "⬜ Умаров Шухрат"])
    check_true("button data fits Telegram's 64 bytes", all(len(b["callback_data"].encode()) <= 64 for b in buttons))
    check_true("the screen says what the mark does, in Uzbek Cyrillic",
               "1 / 3" in view and "кечикди" in view and latin_words(view, allow={"Verifix"}) == [])
    # a person's own start time (2026-10-22: "some employees come at 10 a.m.")
    _view, with_start = admin.flexible_view(people, set(), {"9": "10:00"})
    check("🕘 shows each person's own start time, else Verifix's",
          [b["text"] for row in with_start["inline_keyboard"] for b in row if b["callback_data"].startswith("fstart:")],
          ["🕘 10:00", "🕘 Verifix", "🕘 Verifix"])
    check("a tap goes Verifix → 08:00 → 09:00 → 10:00 → 11:00 → Verifix",
          [admin.next_start(x) for x in (None, "08:00", "09:00", "10:00", "11:00", "07:15")],
          ["08:00", "09:00", "10:00", "11:00", None, None])
    check("names read back from the keyboard", admin.people_on_keyboard(keyboard), people)

    marks = {"7"}
    edits: list = []
    toasts: list = []

    async def toggle(verifix_id, name, set_by):
        if verifix_id in marks:
            marks.discard(verifix_id)
            return False
        marks.add(verifix_id)
        return True

    async def current():
        return set(marks)

    async def fake_edit(callback, text, kb, run_id):
        edits.append(kb)

    async def fake_answer(query_id, text):
        toasts.append(text)

    async def no_log(**kwargs):
        return None

    starts_set: dict = {}

    async def start_times():
        return dict(starts_set)

    async def set_start(verifix_id, name, start, set_by):
        if start is None:
            starts_set.pop(verifix_id, None)
        else:
            starts_set[verifix_id] = start

    saved = [(store, "toggle_flexible_schedule", store.toggle_flexible_schedule),
             (store, "flexible_schedule_ids", store.flexible_schedule_ids),
             (store, "attendance_start_times", store.attendance_start_times),
             (store, "set_attendance_start", store.set_attendance_start),
             (admin, "_edit", admin._edit), (admin, "_answer", admin._answer), (admin, "log_action", admin.log_action),
             (settings, "admin_bot_admin_user_id", settings.admin_bot_admin_user_id)]
    store.toggle_flexible_schedule, store.flexible_schedule_ids = toggle, current
    store.attendance_start_times, store.set_attendance_start = start_times, set_start
    admin._edit, admin._answer, admin.log_action, settings.admin_bot_admin_user_id = fake_edit, fake_answer, no_log, 0
    try:
        tap = {"id": "q", "data": "flex:8", "from": {"id": 1}, "message": {"reply_markup": keyboard}}
        check("a tap marks", asyncio.run(admin.handle_admin_callback(tap, uuid.uuid4())), "flexible_set")
        check_true("...the list updates in place and says who",
                   "✅ Умаров Шухрат" in str(edits[-1]) and toasts[-1] == "Умаров Шухрат — эркин график")
        check("a second tap unmarks", asyncio.run(admin.handle_admin_callback(tap, uuid.uuid4())), "flexible_cleared")
        stale = {**tap, "data": "flex:99"}
        check("someone not on the list: open /grafik again",
              asyncio.run(admin.handle_admin_callback(stale, uuid.uuid4())), "unrecognized")
        clock = {**tap, "data": "fstart:9"}
        for _ in range(3):
            asyncio.run(admin.handle_admin_callback(clock, uuid.uuid4()))
        check_true("three taps on 🕘: Галимов Рушан starts at 10:00, and the list shows it",
                   str(starts_set.get("9")) == "10:00:00" and "🕘 10:00" in str(edits[-1]) and toasts[-1] == "Галимов Рушан — 10:00")
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_verifix() -> None:
    """A4 attendance: the Verifix client, late/absent/excused rules, brief, bot, /verifix."""
    print("verifix attendance (A4)")
    import asyncio
    import contextlib
    import json
    import uuid

    import httpx
    from pydantic import SecretStr

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.common.translit import name_to_cyrillic
    from integrations.org_bot import admin, ops_manager
    from integrations.verifix import attendance
    from integrations.verifix import client as vx

    # ---- names arrive in Latin; every message is Cyrillic
    for latin, cyrillic in (
        ("Salimov Mumin", "Салимов Мумин"), ("G'ofurov Sherzod", "Ғофуров Шерзод"),
        ("Yo'ldosheva O'g'iloy", "Йўлдошева Ўғилой"), ("Akhmedov Sanjar", "Ахмедов Санжар"),
        ("Ergashev Yusuf", "Эргашев Юсуф"), ("Ma'rufjon Xolmatov", "Маъруфжон Холматов"),
        ("Qodirova Shaxnoza", "Қодирова Шахноза"), ("Tsoy Yelena", "Цой Елена"),
        ("Oʻrinboyev Gʻayrat", "Ўринбоев Ғайрат"), ("SHERZOD", "ШЕРЗОД"), ("Салимов Мумин", "Салимов Мумин"),
    ):
        check(f"name {latin!r}", name_to_cyrillic(latin), cyrillic)

    # ---- time kinds exactly as Verifix documents them
    documented = [
        {"name": "Явка", "time_kind_id": "81", "letter_code": "Я"},
        {"name": "Опоздание", "time_kind_id": "82", "letter_code": "ОП"},
        {"name": "Ранний Уход", "time_kind_id": "83", "letter_code": "РУ"},
        {"name": "Отсутствие", "time_kind_id": "84", "letter_code": "ОТС"},
        {"name": "Свободное Время", "time_kind_id": "85", "letter_code": "СВ"},
        {"name": "Почасовой Отгул", "time_kind_id": "86", "letter_code": "ПО"},
        {"name": "Выходной", "time_kind_id": "87", "letter_code": "В"},
        {"name": "Больничный", "time_kind_id": "88", "letter_code": "Б"},
        {"name": "Отгул", "time_kind_id": "89", "letter_code": "О"},
        {"name": "Командировка", "time_kind_id": "90", "letter_code": "К"},
        {"name": "Отпуск", "time_kind_id": "91", "letter_code": "ОТ"},
        {"name": "Неоплачиваемый отпуск", "time_kind_id": "92", "letter_code": "НО"},
        {"name": "Сверхурочныe", "time_kind_id": "93", "letter_code": "СУ"},
        {"name": "Kechikish", "time_kind_id": "200", "letter_code": "ОП"},  # renamed: the letter code decides
    ]
    kinds = attendance.classify_kinds(documented)
    check("time kinds classified", kinds, {
        "81": "other", "82": "late", "83": "early", "84": "absent", "85": "other", "86": "hourly_off",
        "87": "other", "88": "sick", "89": "day_off", "90": "trip", "91": "vacation", "92": "unpaid",
        "93": "other", "200": "late",
    })

    # ---- one employee's days, in Verifix's own shape (numbers as strings)
    def day(d, kind="W", start="09:00", end="18:00", came=None, left=None, facts=()):
        return {
            "date": d, "day_kind": kind, "plan_time": "480",
            "begin_time": f"{d} {start}:00" if kind == "W" and start else None,
            "end_time": f"{d} {end}:00" if kind == "W" and end else None,
            "input_time": f"{d} {came}:00" if came else None,
            "output_time": f"{d} {left}:00" if left else None,
            "facts": [{"time_kind_id": k, "fact_value": v} for k, v in facts],
        }

    rows = [
        {"employee_id": "641", "employee_name": "Salimov Mumin", "job_name": "Кассир", "days": [
            day("01.02.2025", kind="R", came="10:46", left="18:57", facts=[(85, 491)]),
            day("03.02.2025", came="08:00", left="19:21"),
            day("04.02.2025", came="09:35", left="18:05", facts=[(82, "35")]),
            day("05.02.2025", came="09:04", left="18:00"),
            day("06.02.2025", facts=[(84, 480)]),
            day("07.02.2025", facts=[(88, "480")]),
            day("10.02.2025", came="10:00", left="18:00", facts=[(86, 60)]),
            day("11.02.2025", came="08:55", left="17:00"),
        ]},
        {"employee_id": "642", "employee_name": "Qodirova Shaxnoza", "job_name": "", "days": [
            day("11.02.2025"),
            day("12.02.2025"),
        ]},
    ]
    recs = attendance.records(rows, kinds, grace=5, now=datetime(2025, 2, 12, 10, 0))
    status = {(r.name, r.day.day): (r.status, r.late_minutes, r.early_minutes, r.excuse) for r in recs}
    check("a day off is never absent, even if they came", status[("Салимов Мумин", 1)][0], "off")
    check("came before the start: on time", status[("Салимов Мумин", 3)][:2], ("on_time", 0))
    check("35 minutes late, from the times themselves", status[("Салимов Мумин", 4)][:2], ("late", 35))
    check("4 minutes late is within the 5-minute grace", status[("Салимов Мумин", 5)][0], "on_time")
    check("a working day with no arrival: absent", status[("Салимов Мумин", 6)][0], "absent")
    check("sick leave excuses the day", status[("Салимов Мумин", 7)], ("excused", 0, 0, "касаллик варақаси"))
    check("an hourly leave excuses coming late", status[("Салимов Мумин", 10)][::3], ("excused", "соатбай жавоб"))
    check("left an hour early is noted", status[("Салимов Мумин", 11)][::2], ("on_time", 60))
    check("a past day with no arrival is absent even when 'now' is given", status[("Қодирова Шахноза", 11)][0], "absent")
    check("today, no arrival yet at 10:00: not yet, not absent", status[("Қодирова Шахноза", 12)][0], "not_yet")
    early_morning = attendance.records(rows[1:], kinds, grace=5, now=datetime(2025, 2, 12, 8, 30))
    check("before the shift starts nobody is missing", early_morning[-1].status, "before_start")

    # ---- the brief's block
    def text_for(d):
        return attendance.render_day(attendance.summarize(recs, d))

    late_day = text_for(date(2025, 2, 4))
    check_true("late day: yellow, who and by how much",
               late_day.startswith("🟡") and "Салимов Мумин — 35 дақиқа кечикди (09:35)" in late_day)
    absent_day = text_for(date(2025, 2, 6))
    check_true("absent day: red", absent_day.startswith("🔴") and "келмади" in absent_day)
    check_true("all on time: green", text_for(date(2025, 2, 3)).startswith("🟢"))
    check_true("sick is shown as excused, not absent", "касаллик варақаси" in text_for(date(2025, 2, 7)))
    check("no working day: no block", text_for(date(2025, 2, 1)), None)
    check_true("the block is Uzbek Cyrillic",
               all(latin_words(t) == [] for t in (late_day, absent_day, text_for(date(2025, 2, 12)))))
    data = attendance.describe(recs, date(2025, 2, 12), datetime(2025, 2, 12, 10, 0))
    check_true("the bot's data lists each late arrival",
               "[2025-02-04] Салимов Мумин: late 35 min (arrived 09:35, start 09:00)" in data)
    check_true("...and today's not-yet-arrived", "Not arrived yet: Қодирова Шахноза" in data)

    brief = load_agent("ceo-daily-brief")
    with_att = brief.render(brief.BriefData(report_rows=[], attendance=attendance.summarize(recs, date(2025, 2, 4))))
    check_true("the brief shows attendance once Verifix is set up", "Давомат" in with_att and "35 дақиқа" in with_att)
    check_true("no Verifix, no attendance block", "Давомат" not in brief.render(brief.BriefData(report_rows=[])))
    check_true("a Verifix failure is said plainly",
               "Verifix'дан маълумот олиб бўлмади" in brief.render(brief.BriefData(report_rows=[], attendance_failed=True)))

    # ---- the client against a fake Verifix
    calls: list[httpx.Request] = []
    state = {"tokens": 0, "fail_once": False, "forbidden": False}

    def handler(request):
        calls.append(request)
        if request.url.path == "/security/oauth/token":
            state["tokens"] += 1
            body = json.loads(request.content)
            ok = body == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "sec", "scope": "read"}
            return httpx.Response(200 if ok else 400, json={"access_token": f"T{state['tokens']}", "expires_in": 10800})
        if state["forbidden"]:
            return httpx.Response(403, text="access denied")
        if state["fail_once"]:
            state["fail_once"] = False
            return httpx.Response(401, text="expired")
        assert request.headers["project_code"] == "vhr"
        page_two = request.headers.get("cursor") == "77"
        payload = {"data": [rows[1] if page_two else rows[0]], "meta": {"count": "1", "next_cursor": "-1" if page_two else "77"}}
        # Verifix labels JSON as text/plain
        return httpx.Response(200, text=json.dumps(payload), headers={"content-type": "text/plain;charset=UTF-8"})

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    saved = [(vx, "audited", vx.audited), (settings, "verifix_client_id", settings.verifix_client_id),
             (settings, "verifix_client_secret", settings.verifix_client_secret),
             (settings, "verifix_enabled", settings.verifix_enabled)]
    vx.audited = no_audit
    settings.verifix_client_id, settings.verifix_client_secret, settings.verifix_enabled = "cid", SecretStr("sec"), True

    async def read(**flags):
        state.update(flags)
        async with vx.VerifixClient(agent="test", transport=httpx.MockTransport(handler)) as c:
            return await c.timesheet(date(2025, 2, 4), date(2025, 2, 4))

    try:
        got = asyncio.run(read())
        check("both pages read, cursor followed", [r["employee_id"] for r in got], ["641", "642"])
        lists = [r for r in calls if r.url.path.endswith("timesheet$export")]
        check("second page asked with the cursor", [r.headers.get("cursor") for r in lists], [None, "77"])
        check_true("bearer token and page size sent",
                   lists[0].headers["authorization"] == "Bearer T1" and lists[0].headers["limit"] == "100")
        check("dates in Verifix's format", json.loads(lists[0].content)["period_begin_date"], "04.02.2025")
        check("one token for the whole run", state["tokens"], 1)

        calls.clear()
        state["tokens"] = 0
        got = asyncio.run(read(fail_once=True))
        check_true("an expired token is renewed once and the read goes on", len(got) == 2 and state["tokens"] == 2)

        try:
            asyncio.run(read(forbidden=True))
            check_true("a refusal raises", False)
        except vx.VerifixError as exc:
            check_true("a refusal says what happened, without the secret", "HTTP 403" in str(exc) and "sec" not in str(exc))
        state["forbidden"] = False

        settings.verifix_client_id = ""
        try:
            asyncio.run(read())
            check_true("not configured raises", False)
        except vx.VerifixError:
            check_true("not configured: refuses before any call", True)
        check_true("the bot says Verifix isn't connected",
                   "not connected" in asyncio.run(ops_manager._fetch_attendance_data()))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("davomat is a data source the Director can ask", "davomat" in AGENT_SLUGS)

    # ---- Admin Bot /verifix
    sent: list[str] = []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, **kwargs):
            sent.append(text)
            return [1]

    class FakeVerifix:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def time_kinds(self):
            return documented

        async def organisation(self):
            return {"filial_name": "ЭМЖИЕМ"}

        async def timesheet(self, begin, end):
            d = begin.strftime("%d.%m.%Y")
            return [{"employee_id": "1", "employee_name": "Salimov Mumin", "days": [day(d, came="09:40")]},
                    {"employee_id": "2", "employee_name": "Ergashev Yusuf", "days": [day(d, came="08:50")]}]

    saved = [(admin, "TelegramBot", admin.TelegramBot), (vx, "VerifixClient", vx.VerifixClient),
             (settings, "admin_bot_admin_user_id", settings.admin_bot_admin_user_id),
             (settings, "verifix_client_id", settings.verifix_client_id),
             (settings, "verifix_client_secret", settings.verifix_client_secret)]
    admin.TelegramBot, vx.VerifixClient, settings.admin_bot_admin_user_id = FakeBot, FakeVerifix, 0
    message = {"from": {"id": 9}, "text": "/verifix"}
    try:
        settings.verifix_client_id = ""
        check("/verifix before setup", asyncio.run(admin.handle_admin_message(message, uuid.uuid4())), "verifix_not_configured")
        check_true("...tells the admin where the keys are", "VERIFIX_CLIENT_ID" in sent[-1] and "OAuth2" in sent[-1])
        settings.verifix_client_id, settings.verifix_client_secret = "cid", SecretStr("sec")
        check("/verifix once set up", asyncio.run(admin.handle_admin_message(message, uuid.uuid4())), "verifix_ok")
        check_true("...says it's connected and readable, with the organisation",
                   "Verifix уланди" in sent[-1] and "Бугунги табел ўқилди: 2 қатор" in sent[-1] and "ЭМЖИЕМ" in sent[-1])
        check_true("...but no names or times for IT (Director's order 07.10.2026)",
                   not any(x in sent[-1] for x in ("Salimov", "Салимов", "Ergashev", "09:40", "кечикди")))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_kpi() -> None:
    """The Director's KPI criteria: scoring, the table and card, goals, results, ratings."""
    print("KPI (Director's criteria)")
    import asyncio
    import uuid
    from datetime import datetime, timezone

    from integrations.org_bot import answer_check, kpi_flow, kpi_score as ks, store

    # ---- small helpers
    check("goal number", ks.parse_number("20 та янги шартнома"), 20.0)
    check("thousands with spaces", ks.parse_number("сотув 1 500 000 сўм"), 1500000.0)
    check("decimal comma", ks.parse_number("12,5 тонна"), 12.5)
    check("a result must be only a number", (ks.only_number("17"), ks.only_number("17 та")), (17.0, None))
    check("goals set before the 25th are for this month", ks.goal_month(date(2026, 9, 24)), date(2026, 9, 1))
    check("from the 25th, for next month", ks.goal_month(date(2026, 12, 25)), date(2027, 1, 1))
    check("/kpi shows last month for the first 5 days", ks.score_period(date(2026, 10, 3)), date(2026, 9, 1))
    check("...then this month", ks.score_period(date(2026, 10, 6)), date(2026, 10, 1))
    check("month end (leap year)", ks.month_end(date(2028, 2, 1)), date(2028, 2, 29))
    check("previous month over the year", ks.previous_month(date(2027, 1, 1)), date(2026, 12, 1))

    # ---- scoring
    def at(d: int, h: int) -> datetime:
        return datetime(2026, 9, d, h - 5, tzinfo=timezone.utc)

    employees = [
        {"id": "a", "full_name": "Алишер Каримов", "display_name": "ak", "role": "b2b_sotuv"},
        {"id": "b", "full_name": "Дилноза Раҳимова", "display_name": "dr", "role": "hr"},
        {"id": "c", "full_name": "Бобур Алиев", "display_name": "ba", "role": "ombor"},
    ]
    reports = [{"employee_id": "a", "display_name": "ak", "status": "submitted", "report_date": date(2026, 9, d),
                "submitted_at": at(d, 17)} for d in range(1, 11)]
    reports += [{"employee_id": "b", "display_name": "dr", "status": "asked" if d % 2 else "submitted",
                 "report_date": date(2026, 9, d), "submitted_at": at(d, 17)} for d in range(1, 11)]
    tasks = [{"employee_id": "a", "display_name": "ak", "status": "done", "due_date": date(2026, 9, d),
              "completed_at": at(d, 12)} for d in (5, 9)]
    goals = [{"employee_id": "a", "title": "20 та шартнома", "target": 20, "actual": 15}]
    ratings = [{"employee_id": "a", "performance": 4, "communication": 4, "interaction": 4, "qualifications": 4}]
    people = ks.build(employees, reports, tasks, {"a": 5}, goals, ratings, date(2026, 9, 1), date(2026, 9, 30),
                      role_labels={"b2b_sotuv": "B2B сотув", "hr": "HR (кадрлар)", "ombor": "Омбор"})
    a, b, c = people
    check("best first; nothing measured last", [p.name for p in people], ["Алишер Каримов", "Дилноза Раҳимова", "Бобур Алиев"])
    check("goals: 15 of 20", round(a.parts["results"]), 75)
    check("tasks on time", a.parts["tasks"], 100.0)
    check("rating 4 of 5 = 80", a.parts["rating"], 80.0)
    check("volume against the team median (15 vs 10)", (a.parts["volume"], b.parts["volume"]), (100.0, 50.0))
    check("weighted total", round(a.total, 1), 88.5)
    check_true("parts with no data are left out, not zero", b.parts["results"] is None and round(b.total) == 50)
    check("nothing measured: no score", (c.total, ks.grade(c.total)), (None, "⚪"))
    check("grades", [ks.grade(x) for x in (88.5, 70, 50)], ["🟢", "🟡", "🔴"])
    att = ks.score([ks.EmployeeMonth("x", "Х", attendance=ks.Attendance(working=20, late=2, absent=1))])[0]
    check("commitment from attendance: (20 − 1 − ½·2) / 20", att.parts["commitment"], 90.0)

    table = ks.table_text(date(2026, 9, 1), people, final=True)
    check_true("table: place, grade, name, score",
               "1. 🟢 <b>Алишер Каримов</b> (B2B сотув) — <b>88</b>" in table and "Бобур" not in table)
    check_true("table is Uzbek Cyrillic", latin_words(table, allow={"OKR", "B2B"}) == [])
    card = ks.card_text(date(2026, 9, 1), a)
    check_true("card shows the goal's progress", "20 та шартнома — 15 / 20 (75%)" in card)
    check_true("card is Uzbek Cyrillic", latin_words(card, allow={"OKR"}) == [])
    rid = str(uuid.uuid4())
    keyboard = ks.rating_keyboard(rid, {"performance": 4})
    buttons = [btn for row in keyboard["inline_keyboard"] for btn in row]
    check("four criteria × 1–5", [len(r) for r in keyboard["inline_keyboard"]], [5, 5, 5, 5])
    check_true("every rating button fits Telegram's 64 bytes", all(len(x["callback_data"].encode()) <= 64 for x in buttons))
    check_true("the chosen mark is shown", buttons[3]["text"] == "•4•")
    check("names match in any order", ks.match_attendance({"a": "Алишер Каримов"}, {"v1": "Каримов Алишер", "v2": "Иванов Иван"}),
          {"a": "v1"})
    check("two people with one name: no guess", ks.match_attendance({"a": "Алишер Каримов"},
                                                                     {"v1": "Каримов Алишер", "v2": "Алишер Каримов"}), {})

    # ---- the conversation, with Telegram and the database faked
    director = {"id": "d", "telegram_user_id": 1, "role": "operatsion_direktor", "status": "active",
                "full_name": "Директор", "display_name": "dir"}
    worker = {"id": "w", "telegram_user_id": 2, "role": "b2b_sotuv", "status": "active",
              "full_name": "Алишер Каримов", "display_name": "ak"}
    people_by_tid = {1: director, 2: worker}
    sent: list[tuple[int, str, object]] = []
    goals_db: dict[str, dict] = {}
    ratings_db: dict[str, dict] = {}
    finals: list = []

    async def fake_send(chat_id, text, run_id, keyboard=None):
        sent.append((chat_id, text, keyboard))
        return [1]

    async def nothing(*args, **kwargs):
        return None

    def goal_view(g):
        return {**g, "name": worker["full_name"], "employee_telegram_user_id": 2}

    async def start_goal_draft(employee_id, month, director_tid):
        goals_db["g1"] = {"id": "g1", "employee_id": employee_id, "month": month, "title": None, "target": None,
                          "actual": None, "status": "draft", "awaiting_actual_by": None}
        return goals_db["g1"]

    async def goal_draft(tid):
        g = goals_db.get("g1")
        return goal_view(g) if g and g["status"] == "draft" and tid == 1 else None

    async def activate_goal(goal_id, title, target):
        goals_db[goal_id].update(title=title, target=target, status="active")
        return goal_view(goals_db[goal_id])

    async def get_goal(goal_id):
        return goal_view(goals_db[goal_id]) if goal_id in goals_db else None

    async def await_goal_actual(goal_id, tid):
        goals_db[goal_id]["awaiting_actual_by"] = tid

    async def goal_awaiting_actual(tid):
        g = next((g for g in goals_db.values() if g["awaiting_actual_by"] == tid), None)
        return goal_view(g) if g else None

    async def set_goal_actual(goal_id, actual, by):
        goals_db[goal_id].update(actual=actual, awaiting_actual_by=None)
        return goal_view(goals_db[goal_id])

    async def set_rating(rating_id, criterion, value, by):
        ratings_db[rating_id][criterion] = value
        return ratings_db[rating_id]

    async def ratings_for_month(month):
        return list(ratings_db.values())

    async def by_tid(tid):
        return people_by_tid.get(tid)

    async def by_id(eid):
        return {"d": director, "w": worker}.get(eid)

    async def good_goal(field, answer, run_id, *, agent, context):
        return (True, None, "20 та янги шартнома") if any(ch.isdigit() for ch in answer) else (False, "Рақам қани?", answer)

    async def record_final(month, run_id):
        finals.append(month)
        return True

    patches = [
        (kpi_flow, "_send", fake_send), (kpi_flow, "_edit", nothing), (kpi_flow, "_answer", nothing),
        (kpi_flow, "send_final", record_final), (answer_check, "check_answer", good_goal),
        (store, "start_goal_draft", start_goal_draft), (store, "goal_draft", goal_draft),
        (store, "activate_goal", activate_goal), (store, "get_goal", get_goal),
        (store, "await_goal_actual", await_goal_actual), (store, "goal_awaiting_actual", goal_awaiting_actual),
        (store, "set_goal_actual", set_goal_actual), (store, "set_rating", set_rating),
        (store, "ratings_for_month", ratings_for_month), (store, "get_employee_by_telegram_id", by_tid),
        (store, "get_employee", by_id),
    ]
    async def list_active():
        return [director, worker]

    patches.append((store, "list_active_employees", list_active))
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)

    def say(who, text):
        return asyncio.run(kpi_flow.handle_message(who, {"text": text}, uuid.uuid4()))

    def tap(tid, data):
        prefix, rest = data.split(":", 1)
        callback = {"id": "q", "data": data, "from": {"id": tid}, "message": {"message_id": 5, "chat": {"id": tid}}}
        return asyncio.run(kpi_flow.handle_callback(prefix, rest, callback, uuid.uuid4()))

    try:
        check("an employee can't set goals", say(worker, "/maqsad"), None)
        check("/maqsad: pick a person", say(director, "/maqsad"), "kpi_pick_employee")
        check_true("...with a button per employee (not the Director)",
                   [b["callback_data"] for row in sent[-1][2]["inline_keyboard"] for b in row] == ["kg:w"])
        check("an employee can't press the Director's buttons", tap(2, "kg:w"), "kpi_unauthorized")
        check("tap the person", tap(1, "kg:w"), "kpi_goal_started")
        check("a goal with no number is asked again", say(director, "yaxshi ishlasin"), "kpi_goal_reasked")
        check("a goal with a number is set", say(director, "20 ta yangi shartnoma"), "kpi_goal_set")
        check_true("...the employee is told", any(c == 2 and "20 та янги шартнома" in t for c, t, _ in sent))
        check("other messages then go on as usual", say(director, "bugun kim kechikdi?"), None)
        check("the employee opens the result entry", tap(2, "ka:g1"), "kpi_actual_asked")
        check("words are not a result", say(worker, "17 ta"), "kpi_actual_reasked")
        check("a number is", say(worker, "17"), "kpi_actual_saved")
        check_true("...with the share of the goal", "17 / 20 (85%)" in sent[-1][1])
        check("then reports go on as usual", say(worker, "Бугун 3 та мижоз билан учрашдим"), None)

        rid = str(uuid.uuid4())
        ratings_db[rid] = {"id": rid, "employee_id": "w", "month": date(2026, 9, 1)}
        check("an employee can't rate", tap(2, f"kr:{rid}:p5"), "kpi_unauthorized")
        for letter in "pci":
            tap(1, f"kr:{rid}:{letter}4")
        check("no final table before every card is complete", finals, [])
        check("the last mark", tap(1, f"kr:{rid}:q5"), "kpi_rated")
        check("...closes the month", finals, [date(2026, 9, 1)])
        check("a bad mark is refused", tap(1, f"kr:{rid}:p9"), "kpi_bad_rating")
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_garmin_leads() -> None:
    """Leads pushed by the Garmin AI bot: validation, the webhook, the Director's data."""
    print("Garmin AI bot leads")
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from integrations.api import app as api_app
    from integrations.common.config import settings
    from integrations.garmin import leads

    lead = {"event": "lead", "lead_id": "6b1f0c1e-1111-4a5b-9c1d-1234567890ab", "chat_id": "555", "urgency": "now",
            "name": "Азиз", "phone": "+998901234567", "product_id": "fenix-8", "product_name": "fēnix 8",
            "price": "13490000", "summary": "Хочет купить сегодня", "lang": "ru", "source": "card",
            "at": "2026-10-01T09:30:00.000Z"}
    event, error = leads.clean(lead)
    check_true("a lead is accepted", error is None and event["chat_id"] == 555 and event["price"] == 13490000.0)
    check("its time is kept", event["at"], datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc))
    check("an unknown urgency is refused", leads.clean({**lead, "urgency": "boiling"})[1], "unknown urgency 'boiling'")
    check("no lead_id, no lead", leads.clean({**lead, "lead_id": ""})[1], "lead_id is required")
    check("a later phone", leads.clean({"event": "phone", "chat_id": 555, "phone": "+998901112233"})[0],
          {"event": "phone", "chat_id": 555, "phone": "+998901112233"})
    check_true("long text is cut, not refused", len(leads.clean({**lead, "summary": "x" * 5000})[0]["summary"]) == 2000)

    stored = []

    async def fake_save(ev):
        stored.append(ev)
        return {"ok": True, "stored": True}

    saved = [(leads, "save", leads.save), (settings, "garmin_leads_secret", settings.garmin_leads_secret)]
    leads.save, settings.garmin_leads_secret = fake_save, SecretStr("s3cret-garmin")
    client = TestClient(api_app.app)
    try:
        check("wrong secret: 401 (the bot won't retry)", client.post("/webhooks/garmin-lead/nope", json=lead).status_code, 401)
        check("bad lead: 422", client.post("/webhooks/garmin-lead/s3cret-garmin", json={**lead, "urgency": "x"}).status_code, 422)
        ok = client.post("/webhooks/garmin-lead/s3cret-garmin", json=lead)
        check_true("a good lead is stored", ok.status_code == 200 and stored[-1]["lead_id"] == lead["lead_id"])

        async def broken(ev):
            raise RuntimeError("db down")

        leads.save = broken
        check("storage down: 503 (the bot retries)", client.post("/webhooks/garmin-lead/s3cret-garmin", json=lead).status_code, 503)
        settings.garmin_leads_secret = SecretStr("")
        check("no secret set: every call refused", client.post("/webhooks/garmin-lead/", json=lead).status_code in (401, 404, 405), True)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    rows = [{**leads.clean(lead)[0], "created_at": datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc)},
            {**leads.clean({**lead, "lead_id": "x" * 12, "urgency": "next", "phone": None})[0],
             "created_at": datetime(2026, 9, 30, 10, tzinfo=timezone.utc)}]
    text = leads.describe(rows)
    check_true("the Director's data: totals and each lead",
               "2 sales leads (1 hot" in text and "1 with a phone" in text and "fēnix 8 (2)" in text and "2026-10-01 09:30" in text)
    check_true("garmin_lidlar is a data source the Director can ask", "garmin_lidlar" in AGENT_SLUGS)


def test_reports() -> None:
    """The brief as a picture and four reports as PDFs (2026-10-05): templates, values, files."""
    print("report images and PDFs")
    import re

    from integrations.billz import sap_check as sc
    from integrations.billz.sales import DaySales, ShopDay
    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.common.timeutil import today_local
    from integrations.org_bot import kpi_score
    from integrations.reports import render
    from integrations.sap import push_handler
    from integrations.sap.figures import Figure, invoices_on
    from integrations.sap.models import ARAging, ARInvoice
    from integrations.verifix import attendance

    def text_of(html: str) -> str:
        """What a reader sees: no styles, no drawn icons, no tags."""
        html = re.sub(r"<style.*?</style>|<svg.*?</svg>", " ", html, flags=re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    check_true("fonts are bundled (Noto Sans, Uzbek Cyrillic)",
               all((render.FONTS / f).exists() for f in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")))

    # ---- the brief: values
    brief = load_agent("ceo-daily-brief")
    invoices = [ARInvoice(doc_entry=i, doc_num=2000 + i, card_code="C", card_name="X", doc_date=date(2026, 1, 1),
                          due_date=date(2026, 2, 1), days_overdue=120 if i < 3 else 10,
                          aging_bucket="90_plus" if i < 3 else "1_30", currency="USD", doc_total_tiyin=100000,
                          paid_to_date_tiyin=0, balance_due_tiyin=100000, sales_person_code=None,
                          sales_person_name=None, division=None) for i in range(6)]
    aging = ARAging(snapshot_date=date(2026, 10, 5), invoices=invoices)
    aging.bucket_counts = {"90_plus": 3, "1_30": 3}
    people = [{"report_date": date(2026, 10, 2), "status": "asked", "display_name": f"Ходим {i}", "role": "it"}
              for i in range(7)]
    goals = [{"name": "Ширин Умматова", "title": "20 та шартнома", "target": 20, "actual": 4},
             {"name": "Отабек Мирпулатов", "title": "Сотув режаси", "target": 100, "actual": None},
             {"name": "Дилноза Раҳимова", "title": "HR ҳужжатлари", "target": 10, "actual": 10}]
    tasks = [{"display_name": "Алишер Каримов", "task_summary": "Омбор ҳисоботи", "status": "started",
              "due_date": today_local() - timedelta(days=3)},
             {"display_name": "Бобур Алиев", "task_summary": "Шартнома", "status": "blocked", "due_date": None},
             {"display_name": "Нигора", "task_summary": "Тўлов", "status": "review", "due_date": None},
             {"display_name": "Ширин", "task_summary": "Қўнғироқ", "status": "sent", "due_date": today_local()},
             {"display_name": "Отабек", "task_summary": "Кейинги ҳафта", "status": "sent",
              "due_date": today_local() + timedelta(days=5)}]
    data = brief.BriefData(
        goals=goals, tasks=tasks,
        sales=Figure(status="ok", totals={}, count=0, as_of=date(2026, 10, 5)), sales_unit="ҳисоб-фактура",
        inventory=Figure(status="unknown_format", as_of=date(2026, 10, 5)), aging=aging,
        payments=brief.PaymentsDue(),
        shop_sales=DaySales(day=date(2026, 10, 4), shops=[ShopDay(name="GARMIN ABAY", net_sales=14_320_000, orders=4)]),
        report_rows=people,
        attendance=attendance.DaySummary(day=date(2026, 10, 4), scheduled=2, flexible=5, absent=[
            attendance.DayRecord(employee_id="1", name="Ёркинов Рустам", job="", day=date(2026, 10, 4), working=True,
                                 status="absent")]),
    )
    v = brief.view(data)
    by_label = {f["label"]: f for f in v["figures"]}
    check("the numbers, no «Касса» (the owner, 16.10.2026)", list(by_label),
          ["Кечаги сотув", "Захира", "Мижоз қарзи", "Бугунги тўловлар"])
    lead = {b["title"]: b for b in v["lead"]}
    check("the brief leads with goals, tasks and reports", list(lead),
          ["Ойлик мақсадлар", "Топшириқлар", "Ҳисобот юбормаганлар"])
    goal_rows = lead["Ойлик мақсадлар"]["rows"]
    check_true("goals: no result first, then the furthest behind; a met goal last and not red",
               "натижа ёзилмаган" in goal_rows[0]["what"] and "4 / 20 (20%)" in goal_rows[1]["what"]
               and goal_rows[2]["alert"] is False and "10 / 10 (100%)" in goal_rows[2]["what"])
    task_block = lead["Топшириқлар"]
    check("tasks: overdue, blocked, in review, due today — not one due later", [r["what"].split(" · ")[0] for r in task_block["rows"]],
          ["3 кун ўтди", "тўсиқ", "текширувда", "бугун"])
    check_true("...counted in the head line, red when overdue or blocked",
               task_block["summary"] == "5 та очиқ · 1 ўтган · 1 тўсиқ" and task_block["tone"] == "alert")
    check("no goals: says how to set them", brief.goals_block(brief.BriefData(goals=[]))["empty"],
          "бу ой мақсад қўйилмаган — /maqsad билан қўйинг")
    check("vision and mission hidden until the owner writes them", v["compass"], [])
    saved_compass = (settings.company_vision, settings.company_mission)
    settings.company_vision, settings.company_mission = "Ўзбекистонда энг ишончли хизмат", ""
    check("...then shown as written", brief.compass(), [{"label": "Визион", "text": "Ўзбекистонда энг ишончли хизмат"}])
    settings.company_vision, settings.company_mission = saved_compass
    check_true("nothing sold / no payments: quiet, not shouted",
               by_label["Кечаги сотув"]["tone"] == "quiet" and by_label["Бугунги тўловлар"]["tone"] == "quiet")
    check_true("unreadable stock says so instead of a number",
               by_label["Захира"]["tone"] == "muted" and "ўқилмади" in by_label["Захира"]["number"])
    check_true("overdue debt is the one red figure, with how old",
               by_label["Мижоз қарзи"]["tone"] == "alert" and "90 кундан" in by_label["Мижоз қарзи"]["sub"])
    blocks = {b["title"]: b for b in v["blocks"] + v["lead"]}
    check("names: five, then '+N'", (len(blocks["Ҳисобот юбормаганлар"]["rows"]), blocks["Ҳисобот юбормаганлар"]["more"]), (5, 2))
    check_true("one shop: its name beside the day, no repeated row",
               "GARMIN ABAY" in blocks["Дўконлар"]["day"] and not blocks["Дўконлар"]["rows"])
    check_true("attendance counts the flexible apart", "эркин графикда 5" in blocks["Давомат"]["summary"])
    brief_html = render.render_html("brief.html", image=True, page_width=540, page_height=6000, sentinel="#ff00ff", **v)
    check_true("the brief reads in Uzbek Cyrillic",
               latin_words(text_of(brief_html), allow={"Billz", "GARMIN", "ABAY", "OPS", "Manager", "Verifix", "C"}) == [])
    check_true("no emoji in the picture (drawn icons only)", "🔴" not in brief_html and "📈" not in brief_html)
    check_true("no «Касса» anywhere in the brief", "Касса" not in brief_html and "Касса" not in brief.caption(data)
               and "Касса" not in brief.render(data))
    check_true("the text brief leads with goals and tasks",
               brief.render(data).index("Ойлик мақсадлар") < brief.render(data).index("Топшириқлар")
               < brief.render(data).index("Рақамлар"))

    # ---- the reports: values
    kpi_people = []
    for name, total in (("Ширин Умматова", 86), ("Отабек Мирпулатов", 53), ("Без маълумот", None)):
        p = kpi_score.EmployeeMonth(employee_id=name, name=name, role_label="IT")
        p.parts = dict.fromkeys(kpi_score.PARTS)
        p.parts["process"] = total
        p.total = total
        kpi_people.append(p)
    kv = kpi_score.table_view(date(2026, 9, 1), kpi_people, final=False)
    check("KPI: measured people ranked, graded", [(r["name"], r["grade"]) for r in kv["people"]],
          [("Ширин Умматова", "high"), ("Отабек Мирпулатов", "low")])
    check_true("...the unmeasured named apart, ratings still to come", kv["unmeasured"] == ["Без маълумот"] and kv["pending"])
    check_true("KPI caption counts the colours", "1 таси 80+" in kpi_score.table_caption(date(2026, 9, 1), kpi_people, False))
    dq = load_agent("data-quality")
    finding = dq._finding("SAP оқимлари: ҳисоб-фактуралар — 2 кун олдин; тўловлар — 2 кун олдин")
    check("data quality: a joined finding becomes a list", [str(x) for x in finding["parts"]],
          ["ҳисоб-фактуралар — 2 кун олдин", "тўловлар — 2 кун олдин"])
    cc = load_agent("cash-calendar")
    cal = cc.build([{"due_date": date(2026, 9, 1), "balance_due_tiyin": 500000, "currency": "USD"}], [], date(2026, 10, 5), capped=False)
    cv = cc.view(cal)
    check_true("cash calendar: overdue first, every week listed", cv["overdue"]["count"] == 1 and len(cv["weeks"]) == 4)

    # ---- the duplicate-line fix (the 2026-10-03 push had no ObjType)
    line_a, line_b = {"DocEntry": 7, "LineNum": 0}, {"DocEntry": 7, "LineNum": 1}
    check("one invoice's lines share one key without ObjType",
          push_handler.full_key("sales", line_a), push_handler.full_key("sales", line_b))
    rows = [{"DocEntry": 7, "DocDate": "2026-10-04", "DocTotal": "100", "CANCELED": "N"}] * 3
    check("an invoice that came as three lines counts once", invoices_on(rows, date(2026, 10, 4), "USD", as_of=None).count, 1)
    docs = sc.docs_from_sap([{"DocEntry": 7, "DocNum": 2430, "DocDate": "2026-10-04", "CANCELED": "N",
                              "DocTotalSy": "100", "DocCur": "UZS"}] * 3,
                            [{"DocEntry": 7, "ItemCode": "x", "WhsCode": "G.A._01"}], frozenset({"G.A._01"}))
    check("...and the Billz check sees it once", len(docs), 1)

    # ---- the files themselves (needs Pango; skipped where it isn't installed)
    try:
        import importlib

        importlib.import_module("weasyprint")
    except Exception as exc:  # noqa: BLE001 — e.g. Windows without GTK/Pango
        print(f"  skip PDF/PNG rendering: WeasyPrint unavailable ({type(exc).__name__})")
        return
    png = render.image("brief.html", **v)
    from io import BytesIO

    import pypdfium2 as pdfium

    from PIL import Image

    picture = Image.open(BytesIO(png))
    check_true("the brief is a PNG 1080 px wide, cut to its content",
               png[:4] == b"\x89PNG" and picture.width == 1080 and 900 < picture.height < 4000)
    check_true("...no sentinel colour left in it", (255, 0, 255) not in [picture.convert("RGB").getpixel((5, y))
                                                                         for y in range(0, picture.height, 40)])
    result = sc.Result(day=date(2026, 10, 4), missing=[sc.Cheque("a", "000903002216", date(2026, 9, 30), "GARMIN ABAY",
                                                                  725000, "Rustam", [sc.Item("1", "", "Band", 1)])])
    for name, template, values in (
        ("cash calendar", "cash_calendar.html", cv),
        ("KPI", "kpi.html", kv),
        ("data quality", "data_quality.html", dq.view(dq.Inputs(today=date(2026, 10, 5)))),
        ("Billz ↔ SAP", "billz_sap.html", sc.pdf_view(result, trial=True)),
    ):
        document = render.pdf(template, **values)
        check_true(f"{name}: a one-page PDF", document[:5] == b"%PDF-" and len(pdfium.PdfDocument(document)) == 1)
        check_true(f"{name}: reads in Uzbek Cyrillic", latin_words(
            text_of(render.render_html(template, image=False, **values)),
            allow={"KPI", "OPS", "Manager", "Admin", "Bot", "IT", "Billz", "SAP", "GARMIN", "ABAY", "Rustam", "Band",
                   "Render", "BILLZ", "CHECK", "TRIAL", "false", "OKR", "C"}) == [])


def test_sap_gateway_code() -> None:
    """The SAP gateway's source (sap-gateway/, 2026-10-09): every column it reads exists in SAP,
    values are bound, and it serves every complete tool the push script calls."""
    print("SAP gateway source")
    import json
    import re

    from integrations.sap import push_handler

    root = Path(__file__).resolve().parents[1]
    gateway = root / "sap-gateway"
    columns = json.loads((root / "scripts" / "sap-gateway-push" / "sap_columns.json").read_text(encoding="utf-8"))["tables"]
    sources = {f.name: f.read_text(encoding="utf-8") for f in sorted((gateway / "src").glob("*.js"))}
    check_true("no secrets in the repo copy", not (gateway / ".env").exists()
               and all("HANA_PASSWORD=" not in s or s.startswith("#") for s in sources.values()))

    wrong = []
    for name, js in sources.items():
        # tableRef('OINV') H ... H."DocEntry"; sales-by-date builds both halves with branch(13, 'OINV', 'INV1')
        aliases: dict[str, set[str]] = {}
        for table, alias in re.findall(r"tableRef\('(\w+)'\)\}\s+(\w+)", js):
            aliases.setdefault(alias, set()).add(table)
        for _obj, head, lines in re.findall(r"branch\((\d+), '(\w+)', '(\w+)'\)", js):
            aliases.setdefault("H", set()).add(head)
            aliases.setdefault("L", set()).add(lines)
        for alias, col in re.findall(r'\b([A-Z])\."(\w+)"', js):
            for table in aliases.get(alias, ()):
                if table in columns and col not in columns[table]:
                    wrong.append(f"{name}: {table}.{col}")
    check("every column the gateway's joined queries read exists in SAP (export of 2026-10-02)", wrong, [])

    served = set(re.findall(r"app\.post\('/tools/(\w+)'", sources["server.js"]))
    listed = set(re.findall(r"name: '(\w+)'", sources["tools.js"]))
    script = (root / "scripts" / "sap-gateway-push" / "push-ar-aging.ps1").read_text(encoding="utf-8")
    called = set(re.findall(r'Push-CompleteTool -Tool "(\w+)"', script))
    check_true("the gateway serves every complete tool the push calls", called <= served)
    check_true("...and describes every tool it serves (GET /tools)", served == listed)
    sales = sources["sales-by-date.js"]
    check_true("sales by date: dates bound, never pasted into the SQL",
               "TO_DATE(?, 'YYYY-MM-DD')" in sales and "'${fromDate}'" not in sales and "[...range, ...range]" in sales)
    check_true("...invoices and credit notes, dated or entered in the range",
               "branch(13, 'OINV', 'INV1')" in sales and "branch(14, 'ORIN', 'RIN1')" in sales and 'H."CreateDate" BETWEEN' in sales)
    check_true("...with every column the Billz check and the brief need",
               all(f'"{c}"' in sales for c in set(push_handler.EXPECTED_COLUMNS["sales"]) | set(push_handler.EXPECTED_COLUMNS["sales_lines"])))
    check_true("open invoices carry the so'm amounts and the seller's name",
               all(f'"{c}"' in sources["open-invoices.js"] for c in push_handler.EXPECTED_COLUMNS["ar_open"]))
    check_true("supplier balances carry what the 1C comparison reads",
               all(f'"{c}"' in sources["supplier-balances.js"] for c in push_handler.EXPECTED_COLUMNS["supplier_balances"]))
    check_true("the HANA client binds parameters", "connection.exec(sql, params," in sources["hana.js"])

    # the 1C <-> SAP comparison reads the gateway's rows as they come
    from integrations.onec import payables as ar
    pushed = [{"CardCode": "S001", "CardName": "Primus LLC", "LicTradNum": "301234567", "CardType": "S",
               "Currency": "UZS", "Balance": "-800.00", "BalanceSys": "-9400000.00", "BalanceFC": "-9400000.00"},
              {"CardCode": "S002", "CardName": "Tanita", "LicTradNum": "", "CardType": "S", "Currency": "USD",
               "Balance": "-120.00", "BalanceSys": "-1410000.00", "BalanceFC": "-120.00"}]
    parties, info = ar.sap_parties(pushed, -1.0)
    check("the gateway's suppliers read with their columns", (parties["S001"].owed, parties["S001"].inn), (9400000.0, "301234567"))
    # SAP's sign is chosen by agreement with 1C, not by guessing (09.10.2026: an advance read as a debt)
    # (invented figures — no real supplier or amount in the repo, the Director's order of 07.10)
    onec = {"X": ar.OneCParty(key="X", name="Namuna taminotchi", inn="300000001", advance=12345678.90)}
    rows = [{"CardCode": "S9", "CardName": "Namuna taminotchi", "LicTradNum": "300000001", "CardType": "S",
             "Balance": "-1003.71", "BalanceSys": "-12345678.90"}]
    sign, how = ar.choose_sign(onec, rows)
    sap_side, _ = ar.sap_parties(rows, sign)
    pair = [m for m in ar.match(onec, sap_side) if m.onec and m.sap][0]
    check("an advance equal in both systems: no difference", (sign, round(pair.diff, 2), how["matched"]), (1.0, 0.0, 1))
    other = {"Y": ar.OneCParty(key="Y", name="Boshqa firma", inn="300000002", payable=500.0)}
    sign, how = ar.choose_sign(other, rows)
    check("no supplier in both systems: no sign claimed from 1C, majority fallback",
          (sign, how["sign_by_1c"], ar.sap_parties(rows, sign)[1]["flipped"]), (None, False, True))
    import tempfile as _tf

    import openpyxl as _ox
    with _tf.TemporaryDirectory() as folder:
        out = Path(folder) / "k.xlsx"
        ar.write_workbook([], other, {"onec_as_of": "", "sap_file": "x", "sap_columns": {}, "flipped": True,
                                      "sign_by_1c": False, "sap_rows": 1}, out)
        note = " ".join(str(c.value or "") for row in _ox.load_workbook(out)["Изоҳ"].iter_rows() for c in row)
    check("the workbook says when the sign was not checked against 1C", "кўпчилик қолдиққа қараб" in note, True)


def test_sap_full_push() -> None:
    """Complete SAP data through new gateway tools: the spec, the script, the receiver."""
    print("SAP complete gateway tools")
    import asyncio
    import contextlib
    import json
    import re
    import uuid

    from integrations.api import app as api_app
    from integrations.common.agent_loader import load_agent
    from integrations.sap import figures, push_handler

    root = Path(__file__).resolve().parents[1]
    folder = root / "scripts" / "sap-gateway-push"
    script = (folder / "push-ar-aging.ps1").read_text(encoding="utf-8")
    spec = (root / "docs" / "sap-gateway-tools.md").read_text(encoding="utf-8")
    columns = json.loads((folder / "sap_columns.json").read_text(encoding="utf-8"))["tables"]

    # The gateway owner's rules (SAP_B1_AI_AGENT_TEACHING_UPDATED.md): the
    # gateway is the only thing that talks to HANA — no SQL, no DB password here.
    check_true("the script holds no SQL and no database connection",
               not re.search(r"\bSELECT\b|OdbcConnection|HanaPassword", script))
    check_true("the script is plain ASCII (Windows PowerShell 5.1 reads it as ANSI)",
               all(ord(ch) < 128 for ch in script))
    check_true("credentials are placeholders in the repo",
               all(re.search(rf'\${v}\s*=\s*"PASTE_', script) for v in ("GatewayToken", "MgmgApiHost", "PushSecret")))
    pushed = re.findall(r'Push-CompleteTool -Tool "(\w+)".*?-Kinds @\(([^)]*)\)', script)
    tools = {tool: re.findall(r'"(\w+)"', kinds) for tool, kinds in pushed}
    check("the complete tools the script uses", sorted(tools),
          ["get_open_invoices", "get_sales_by_date", "get_stock_value", "get_supplier_balances"])
    check_true("...each one is specified for the gateway", all(re.search(rf"### \d+\. `{t}`", spec) for t in tools))
    check_true("...and every kind they fill is one the receiver stores",
               all(k in push_handler.FULL_DATASETS for kinds in tools.values() for k in kinds))
    check_true("complete data posts to /webhooks/sap-data/", "/webhooks/sap-data/$kind/$PushSecret" in script)
    check_true("today's capped tools stay", all(f'-Tool "{t}"' in script for t in ("orders", "inventory", "payments")))
    pushes = script.count("Invoke-PushRequest -Uri")
    check_true("every push the Command Center rejects counts as a failure (exit 1, Task Scheduler shows it)",
               pushes == 3 and script.count("if (-not $push.ok)") + script.count("if ($push.ok)") == pushes)
    installer = (folder / "install-task.ps1").read_text(encoding="utf-8")
    check_true("the scheduled task: the push script every 30 minutes, one copy at a time, on battery too",
               all(s in installer for s in ("push-ar-aging.ps1", "-Minutes 30", "IgnoreNew", "-AllowStartIfOnBatteries",
                                            "-DontStopIfGoingOnBatteries", "-ExecutionPolicy Bypass")))
    check_true("the installer is plain ASCII and stops on a failed check when pasted",
               all(ord(ch) < 128 for ch in installer) and installer.split("\n& {", 1)[-1].rstrip().endswith("}"))
    check_true("the receiving route exists",
               any(getattr(r, "path", "") == "/webhooks/sap-data/{dataset}/{secret}" for r in api_app.app.routes))

    # The SQL proposed to the gateway's maintainer reads only columns SAP has.
    blocks = re.findall(r"### \d+\. `(\w+)`.*?```sql\n(.*?)```", spec, flags=re.S)
    # Tools specified but not pushed yet: asked of the gateway's maintainer, used by hand meanwhile.
    asked: set[str] = set()  # get_supplier_balances was asked 2026-10-09 and is now in the gateway and the push
    check("one SQL template per tool (pushed, or asked and not yet built)",
          sorted(t for t, _ in blocks), sorted(set(tools) | asked))
    wrong = []
    selected: dict[str, set[str]] = {}
    for tool, sql in blocks:
        alias = {a: t for t, a in re.findall(r'"MGM"\."(\w+)"\s+(T\d)', sql)}
        for a, col in re.findall(r'\b(T\d)\."(\w+)"', sql):
            if a not in alias or col not in columns.get(alias[a], []):
                wrong.append(f"{tool}: {alias.get(a, a)}.{col}")
        selected[tool] = set(re.findall(r'(?:\.|AS )"(\w+)"', sql))
    check("every column in the proposed SQL exists in SAP (export of 2026-10-02)", wrong, [])
    check_true("each tool returns the key columns of the kinds it fills",
               all(set(push_handler.FULL_DATASETS[k]) <= selected[t] for t, kinds in tools.items() for k in kinds))
    check_true("Billz needs so'm totals, entry dates and warehouses: they're in get_sales_by_date",
               {"DocTotalSy", "CreateDate", "CreateTS", "WhsCode", "ItemCode", "CodeBars"} <= selected["get_sales_by_date"])

    # the receiver refuses bad input before touching the database
    check_true("unknown kind refused", "unknown dataset" in asyncio.run(
        push_handler.handle_full_push("salaries", {"rows": []}, uuid.uuid4()))["error"])
    check_true("a body without a row list refused", not asyncio.run(
        push_handler.handle_full_push("inventory", {"rows": "x"}, uuid.uuid4()))["ok"])
    too_many = {"rows": [{}] * (push_handler.MAX_FULL_ROWS + 1)}
    check_true("too many rows refused", "too many" in asyncio.run(
        push_handler.handle_full_push("inventory", too_many, uuid.uuid4()))["error"])
    check("an inventory row's key", push_handler.full_key("inventory", {"ItemCode": "010-1", "WhsCode": "G.A._01"}),
          "010-1:G.A._01")
    check_true("a row without its key columns still gets a stable key",
               push_handler.full_key("products", {"x": 1}) == push_handler.full_key("products", {"x": 1})
               and push_handler.full_key("products", {"x": 1}).startswith("unrecognized:"))

    # open invoices replace today's receivables; "complete" comes from the script
    audits: list[dict] = []
    written: list = []

    @contextlib.asynccontextmanager
    async def fake_audited(**kwargs):
        ctx = {"payload": dict(kwargs.get("payload") or {})}
        audits.append(ctx)
        yield ctx

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def execute(self, query, params=None):
            written.append(("execute", query.split()[0]))

        async def executemany(self, query, seq):
            written.append(("executemany", list(seq)))

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    @contextlib.asynccontextmanager
    async def fake_connection():
        yield FakeConn()

    async def no_names():
        return {}

    saved = [(push_handler, n, getattr(push_handler, n)) for n in ("audited", "connection", "_sales_people")]
    push_handler.audited, push_handler.connection, push_handler._sales_people = fake_audited, fake_connection, no_names
    try:
        rows = [{"DocEntry": 1, "DocNum": 2290, "CardCode": "C1", "CardName": "Hyatt", "DocDate": "2026-01-02",
                 "DocDueDate": "2026-01-29", "DocStatus": "O", "CANCELED": "N", "DocTotal": "900", "PaidToDate": "0",
                 "SlpCode": 2, "SlpName": "Ғиёсиддин"}]
        result = asyncio.run(push_handler.handle_full_push("ar_open", {"rows": rows, "complete": False}, uuid.uuid4()))
        check("one open invoice stored", (result["ok"], result["written"]), (True, 1))
        check_true("today's snapshot is replaced, not added to", written[0] == ("execute", "DELETE"))
        check("the seller's name comes with the invoice", written[1][1][0][14], "Ғиёсиддин")
        check_true("a tool that filled its limit is not marked complete", audits[-1]["payload"]["complete"] is False)
        asyncio.run(push_handler.handle_full_push("stock_value", {"rows": [{"WhsCode": "01", "StockValue": 5}]}, uuid.uuid4()))
        check_true("a full answer is marked complete", audits[-1]["payload"]["complete"] is True)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # open invoices -> receivables, with what's already paid taken off
    as_of = date(2026, 10, 2)
    base = {"DocEntry": 7, "DocNum": 2300, "CardCode": "C1", "CardName": "Holiday Inn", "DocDate": "2026-08-01",
            "DocDueDate": "2026-09-01", "DocStatus": "O", "CANCELED": "N", "DocTotal": "1000.50",
            "PaidToDate": "400.25", "SlpCode": 3}
    row = push_handler.aging_row(base, as_of, {3: "Алишер"})
    check("balance = total − paid (tiyin), 31 days late, its bucket, the seller",
          (row[12], row[7], row[8], row[14]), (60025, 31, "31_60", "Алишер"))
    check_true("paid in full: not a debt", push_handler.aging_row({**base, "PaidToDate": "1000.50"}, as_of, {}) is None)
    check_true("cancelled: not a debt", push_handler.aging_row({**base, "CANCELED": "Y"}, as_of, {}) is None)
    check_true("no seller (-1): left empty", push_handler.aging_row({**base, "SlpCode": -1}, as_of, {})[13] is None)

    # a complete push is never "камида"
    check_true("a complete push isn't capped", not figures.push_capped("invoices", {"rows_received": 100, "complete": True}))
    check_true("a gateway push at its limit is", figures.push_capped("invoices", {"rows_received": 100}))
    check_true("no push yet: nothing to flag", not figures.push_capped("invoices", None))
    stock = [{"ItemCode": str(i), "WhsCode": "01", "OnHand": 1, "AvgPrice": 2, "StockValue": 2} for i in range(150)]
    check_true("150 stock rows from a complete push: the whole value",
               not figures.inventory_value(stock, "USD", as_of=as_of, complete=True).capped)
    check_true("150 rows from the gateway would be a lower bound", figures.inventory_value(stock, "USD", as_of=as_of).capped)
    zero = figures.inventory_value([{"WhsCode": "01", "StockValue": "0"}], "USD", as_of=as_of, complete=True)
    check("stock worth exactly nothing is a feed problem, never '$0.00'", zero.status, "unknown_format")
    check("a tool that left a column out is noticed",
          push_handler.missing_columns("stock_value", [{"WhsCode": "01", "WhsName": "A", "Items": 3, "OnHand": 5}]),
          ["StockValue"])
    check("...and so is a column that's always empty",
          push_handler.missing_columns("sales", [{c: 1 for c in push_handler.EXPECTED_COLUMNS["sales"]} | {"DocTotalSy": None}]),
          ["DocTotalSy"])
    script_expected = dict(re.findall(r'"(get_\w+)" = @\(([^)]*)\)', script, flags=re.S))
    script_cols = {t: re.findall(r'"(\w+)"', cols) for t, cols in script_expected.items()}
    check_true("the script's -Check expects what the receiver expects",
               set(script_cols["get_open_invoices"]) == set(push_handler.EXPECTED_COLUMNS["ar_open"])
               and set(script_cols["get_sales_by_date"]) == set(push_handler.EXPECTED_COLUMNS["sales"])
               | set(push_handler.EXPECTED_COLUMNS["sales_lines"])
               # get_stock_value sends item rows (summed on arrival, kept as stock_items): its own columns
               and set(script_cols["get_stock_value"]) == {"ItemCode", "ItemName", "WhsCode", "WhsName", "OnHand", "StockValue"})
    per_warehouse = [{"WhsCode": "01", "StockValue": "300000.50"}, {"WhsCode": "08", "StockValue": "213344.65"}]
    whole = figures.inventory_value(per_warehouse, "USD", as_of=as_of, complete=True)
    check("stock value per warehouse adds up to the whole", (whole.totals, whole.capped), ({"USD": 51334515}, False))

    # yesterday's sales = invoices − credit notes, cancelled ones left out
    sales = [
        {"ObjType": 13, "DocDate": "2026-10-01", "DocTotal": "280.61", "CANCELED": "N"},
        {"ObjType": 13, "DocDate": "2026-10-01 00:00:00", "DocTotal": "73.99", "CANCELED": "N"},
        {"ObjType": 13, "DocDate": "2026-10-01", "DocTotal": "500", "CANCELED": "Y"},
        {"ObjType": 13, "DocDate": "2026-10-01", "DocTotal": "500", "CANCELED": "C"},
        {"ObjType": "14", "DocDate": "2026-10-01", "DocTotal": "73.99", "CANCELED": "N"},
        {"ObjType": 13, "DocDate": "2026-09-30", "DocTotal": "999", "CANCELED": "N"},
    ]
    fig = figures.invoices_on(sales, date(2026, 10, 1), "USD", as_of=as_of)
    check("two invoices, one credit note off, cancellations ignored", (fig.totals, fig.count, fig.capped),
          ({"USD": 28061}, 2, False))

    brief = load_agent("ceo-daily-brief")
    data = brief.BriefData(report_rows=[], sales=fig, sales_unit="ҳисоб-фактура")
    five = brief.render_five(data)
    check_true("the brief counts invoices, with no 'камида'",
               "(2 та ҳисоб-фактура, 08:00 гача SAP'га киритилгани)" in five and "камида" not in five.split("Захира")[0])

    # in so'm when SAP sends it (19.10: "$722" next to Billz's 14,76 млн); a document's total once, not per line
    som_rows = [
        {"ObjType": "13", "DocEntry": 1, "DocDate": "2026-10-18", "DocTotal": "500", "DocTotalSy": "6000000", "CANCELED": "N"},
        {"ObjType": "13", "DocEntry": 1, "DocDate": "2026-10-18", "DocTotal": "500", "DocTotalSy": "6000000", "CANCELED": "N"},
        {"ObjType": "13", "DocEntry": 2, "DocDate": "2026-10-18", "DocTotal": "300", "DocTotalSy": "3600000", "CANCELED": "N"},
        {"ObjType": "14", "DocEntry": 7, "DocDate": "2026-10-18", "DocTotal": "100", "DocTotalSy": "1200000", "CANCELED": "N"},
    ]
    fig = figures.invoices_on(som_rows, date(2026, 10, 18), "USD", as_of=as_of)
    check("sales in so'm, each document once, the credit note off", (fig.totals, fig.count), ({"UZS": 840_000_000}, 2))
    check("a change is only between days in the same currency (no false arrow the day it turned so'm)",
          figures.change({"UZS": 1}, {"USD": 5}), {})

    # overdue: SAP writes the due date = the invoice date (no payment terms) — then the invoice date + AR_PAYMENT_DAYS
    from integrations.sap.push_handler import effective_due
    check("no payment terms in SAP → invoice date + 30 days", effective_due(date(2026, 10, 1), date(2026, 10, 1)),
          date(2026, 10, 31))
    check("no due date at all → invoice date + 30 days", effective_due(date(2026, 10, 1), None), date(2026, 10, 31))
    check("real payment terms are kept", effective_due(date(2026, 10, 1), date(2026, 10, 15)), date(2026, 10, 15))
    from integrations.common.config import settings as _settings
    check("the payment days are a setting (AR_PAYMENT_DAYS, 30)", _settings.ar_payment_days, 30)


def test_billz_sap_check() -> None:
    """Billz → SAP: every shop cheque must reach SAP the same day."""
    print("Billz → SAP check")
    from datetime import datetime

    from integrations.billz import sap_check as sc
    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.common.timeutil import TASHKENT

    check("one list for every shop", sc.warehouse_map("G.A._01, 05"), {"": {"G.A._01", "05"}})
    mapping = sc.warehouse_map("GARMIN ABAY=G.A._01,05; Garmin Malika=21")
    check("per shop, by BILLZ name", (sc.warehouses_for("garmin abay", mapping), sc.warehouses_for("GARMIN MALIKA", mapping)),
          (frozenset({"G.A._01", "05"}), frozenset({"21"})))
    check("a shop not listed and no default: not checked", sc.warehouses_for("Склад", mapping), frozenset())

    day = date(2026, 10, 1)
    rows = [  # BILLZ lines: two cheques, one with two products
        {"order_id": "o1", "order_number": "1201", "shop_name": "GARMIN ABAY", "product_sku": "010-04675-01",
         "product_barcode": "", "product_name": "CIRQA Smart Band", "net_sold_measurement_value": 1,
         "net_sales": 3300000, "seller_full_name": "Абдурашид"},
        {"order_id": "o2", "order_number": "1202", "shop_name": "GARMIN ABAY", "product_sku": "010-13280-00",
         "product_name": "Acc,epixPRO", "net_sold_measurement_value": 1, "net_sales": 500000},
        {"order_id": "o2", "order_number": "1202", "shop_name": "GARMIN ABAY", "product_barcode": "753759319526",
         "product_name": "Band", "net_sold_measurement_value": 1, "net_sales": 370000},
    ]
    cheques, unkeyed = sc.cheques_from_billz(rows, day)
    check("lines grouped into cheques", sorted((c.number, c.amount, len(c.items)) for c in cheques),
          [("1201", 3300000, 1), ("1202", 870000, 2)])
    check_true("every line had a cheque id", not unkeyed)
    check("money on lines without a cheque id is counted apart",
          sc.cheques_from_billz([{"net_sales": 100, "product_name": "x"}, {"order_id": "o9", "net_sales": 5}], day)[1], 100)

    def header(entry, num, docdate, created, total, *, obj=13, canc="N", ts=190516):
        return {"ObjType": obj, "DocEntry": entry, "DocNum": num, "CardName": "B2C клиенты", "DocDate": docdate,
                "CreateDate": created, "CreateTS": ts, "CANCELED": canc, "DocCur": "UZS",
                "DocTotalFC": str(total), "DocTotalSy": str(total)}

    def line(entry, code, whs="G.A._01", obj=13, bar=None):
        return {"ObjType": obj, "DocEntry": entry, "ItemCode": code, "CodeBars": bar, "Dscription": code,
                "Quantity": "1.000000", "WhsCode": whs}

    sales = [
        header(1, 2411, "2026-10-01", "2026-10-01", "3300000.000000"),
        header(2, 2412, "2026-10-01", "2026-10-01", 870000),
        header(3, 2413, "2026-10-01", "2026-10-01", 8100000),           # B2B, other warehouse
        header(4, 2414, "2026-10-01", "2026-10-01", 450000, canc="Y"),  # cancelled
        header(5, 33, "2026-10-01", "2026-10-01", 450000, obj=14),      # credit note
    ]
    lines = [line(1, "010-04675-01"), line(2, "010-13280-00"), line(3, "A39-525N", whs="08"),
             line(4, "x"), line(5, "010-13392-06", whs="05", obj=14)]
    docs = sc.docs_from_sap(sales, lines, frozenset({"G.A._01", "05"}))
    check("shop documents only, cancelled out, credit note negative",
          sorted((d.number, d.amount) for d in docs), [("2411", 3300000), ("2412", 870000), ("33", -450000)])
    check("entry time read", next(d for d in docs if d.number == "2411").created_time, "19:05")

    r = sc.check(cheques, [d for d in docs if d.amount > 0], day)
    check_true("all entered: ok", r.ok and r.status == "ok")
    ok_text = sc.render(r)
    check_true("one ✅ line", "✅" in ok_text and "2 та чек" in ok_text and ok_text.count("\n") == 1)

    # 1202 not entered; 2411 entered a day late; an invoice with no cheque
    late_doc = sc.Doc(key="13:9", number="2420", day=day, created=date(2026, 10, 2), created_time="09:10",
                      amount=3300000, items=[sc.Item("010-04675-01", "", "CIRQA", 1)])
    extra = sc.Doc(key="13:10", number="2421", day=day, created=day, created_time="", amount=1500000, customer="Sherzod Ganiyev")
    r = sc.check(cheques, [late_doc, extra], day)
    check("missing cheque", [c.number for c in r.missing], ["1202"])
    check("entered late, reported", [p.doc.number for p in r.late], ["2420"])
    check("in SAP, not in Billz", [d.number for d in r.extra], ["2421"])
    r_next = sc.check(cheques, [late_doc, extra], date(2026, 10, 3))
    check_true("a late entry is reported once, not every day", not r_next.late)
    text = sc.render(r)
    check_true("each section is there",
               all(s in text for s in ("SAP'га киритилмаган", "Кечикиб ёки бошқа сана", "SAP'да бор, Billz'да йўқ", "1202",
                                       "01.10 сотуви 02.10 куни киритилди")))
    check_true("the message is Uzbek Cyrillic (names, products aside)",
               latin_words(text, allow={"Billz", "CIRQA", "Smart", "Band", "Acc", "epixPRO", "Sherzod", "Ganiyev"}) == [])

    # the 2026-10-03 test run: a sale typed in on the day but under an old date
    # (22.09's CIRQA entered on 22.09 as 07.09), a Tanita scale from G.A._02,
    # and the gateway at first sending no so'm totals
    old_date = sc.Doc(key="13:20", number="2348", day=date(2026, 9, 7), created=date(2026, 9, 22), created_time="",
                      amount=3300000, items=[sc.Item("010-04675-00", "", "CIRQA", 1)])
    sold = sc.Cheque(key="c22", number="000902010236", day=date(2026, 9, 22), shop="GARMIN ABAY", amount=3300000,
                     items=[sc.Item("010-04675-00", "", "CIRQA Smart Band, WW, L-XL, Black", 1)])
    r = sc.check([sold], [old_date], date(2026, 9, 22))
    check("matched by the day it was entered, not its (wrong) date", (r.missing, [p.doc.number for p in r.late]),
          ([], ["2348"]))
    check_true("...and told as 'entered under another date', not as late",
               "22.09 сотуви SAP'га 07.09 санаси билан киритилган (№2348" in sc.render(r))
    check_true("the default warehouses include Garmin Tanita (G.A._02)", "G.A._02" in settings.billz_sap_warehouses)
    no_som = sc.docs_from_sap([{**header(1, 2411, "2026-10-01", "2026-10-01", 0), "DocTotalSy": None, "DocTotalFC": None}],
                              [line(1, "010-04675-01")], frozenset({"G.A._01"}))
    check("no so'm total from the gateway: the amount is unknown, not 0", [d.amount_known for d in no_som], [False])
    r = sc.check([c for c in cheques if c.number == "1201"], no_som, day)
    check_true("...matched by product and date, nothing 'differs' or 'missing'", r.ok and not r.amount_diff)
    unknown_text = sc.render(sc.check(cheques, no_som, day))
    check_true("...and the message says the amounts didn't come",
               "сўмдаги сумма келмади" in unknown_text and "SAP 0 сўм" not in unknown_text)

    # a different amount for the same product the same day
    wrong = sc.Doc(key="13:11", number="2430", day=day, created=day, created_time="", amount=3000000,
                   items=[sc.Item("010-04675-01", "", "CIRQA", 1)])
    r = sc.check([c for c in cheques if c.number == "1201"], [wrong], day)
    check("amount differs, not 'missing'", ([p.doc.number for p in r.amount_diff], r.missing), (["2430"], []))
    check_true("within the tolerance it's the same sale",
               sc.check([c for c in cheques if c.number == "1201"],
                        [sc.Doc(key="13:12", number="2431", day=day, created=day, created_time="", amount=3300000.03 // 1)],
                        day).ok)

    # sold then returned in the shop, never in SAP: nothing to report
    sold = sc.Cheque(key="a", number="1300", day=day, shop="GARMIN ABAY", amount=870000,
                     items=[sc.Item("010-13280-00", "", "Acc", 1)])
    back = sc.Cheque(key="b", number="1301", day=date(2026, 10, 2), shop="GARMIN ABAY", amount=-870000,
                     items=[sc.Item("010-13280-00", "", "Acc", -1)])
    r = sc.check([sold, back], [], date(2026, 10, 2))
    check("a sale and its return cancel out", (r.missing, r.returned), ([], 1))

    pushed = datetime(2026, 10, 1, 19, 30, tzinfo=TASHKENT)
    noted = sc.render(sc.check(cheques[:1], [], day), pushed_at=pushed,
                      day_end=datetime(2026, 10, 2, 0, 0, tzinfo=TASHKENT))
    check_true("an evening push says entries after it aren't seen", "01.10 19:30 ҳолатига" in noted)
    check_true("stale SAP data: says so, compares nothing",
               "янгиланмаган" in sc.render_stale(day, pushed) and "киритилмаган" not in sc.render_stale(day, pushed))

    # the agent: shops grouped by the warehouses they sell from
    agent = load_agent("billz-sap-check")
    malika = sc.Cheque(key="m", number="77", day=day, shop="GARMIN MALIKA", amount=999000)
    merged = agent.compare(cheques + [malika], sales, lines, day, sc.warehouse_map("GARMIN ABAY=G.A._01,05;GARMIN MALIKA=21"))
    check("each shop against its own warehouses", sorted(c.number for c in merged.missing), ["77"])
    check_true("...and the Abay cheques matched", len(merged.cheques_day) == 3)
    summary = sc.summary(merged)
    check_true("stored summary", summary["missing"][0]["number"] == "77" and summary["billz"]["cheques"] == 3)

    # BILLZ without cheque ids: totals only, never a false "missing" or "extra"
    shop_docs = agent.all_shop_docs(sales, lines, sc.warehouse_map("G.A._01,05"))
    check("every mapped warehouse's documents, once", sorted(d.number for d in shop_docs), ["2411", "2412", "33"])
    same = sc.totals_only(day, 3720000, shop_docs)
    check_true("totals match: ok", same.ok and not same.missing and not same.extra)
    off = sc.totals_only(day, 9000000, shop_docs)
    off_text = sc.render(off)
    check_true("totals differ: said once, with the gap, no cheque lists",
               not off.ok and "фақат жами солиштирилди" in off_text and "Billz кўп" in off_text
               and "киритилмаган" not in off_text and "SAP'да бор" not in off_text)

    # on trial the admin gets it first, with a note on how to switch it on for the Director
    check_true("trial is on until the first results are confirmed", settings.billz_sap_check_trial is True)
    check_true("the trial copy says how to switch it on",
               "BILLZ_SAP_CHECK_TRIAL=false" in agent.trial_text("x") and latin_words(agent.trial_text("x"),
               allow={"Render", "BILLZ", "CHECK", "TRIAL", "false", "x"}) == [])

    # what is asked from BILLZ: one day, cheque lines, every shop
    import asyncio
    import contextlib

    import httpx
    from pydantic import SecretStr

    from integrations.billz import client as bz

    seen: list = []

    def handler(request):
        if request.url.path == "/v1/auth/login":
            return httpx.Response(200, json={"data": {"access_token": "A"}})
        seen.append(request)
        return httpx.Response(200, json={"count": 1, "products_stats_by_date": [rows[0]]})

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"payload": {}}

    async def read():
        async with bz.BillzClient(agent="test", transport=httpx.MockTransport(handler)) as client:
            return await client.positions(day, ["s1", "s2"])

    saved = [(bz, "audited", bz.audited), (settings, "billz_secret_token", settings.billz_secret_token),
             (settings, "billz_enabled", settings.billz_enabled)]
    bz.audited, settings.billz_secret_token, settings.billz_enabled = no_audit, SecretStr("key"), True
    try:
        got = asyncio.run(read())
        params = seen[0].url.params
        check_true("one day, by cheque line, every shop",
                   params["start_date"] == params["end_date"] == "2026-10-01"
                   and params["detalization_by_position"] == "true" and params["shop_ids"] == "s1,s2")
        check("the lines come back", len(got), 1)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_billz() -> None:
    """BILLZ shop sales: the client against a fake BILLZ, the brief block, the bot, /billz."""
    print("BILLZ (shop tills)")
    import asyncio
    import contextlib
    import json
    import uuid

    import httpx
    from pydantic import SecretStr

    from integrations.billz import client as bz
    from integrations.billz import sales
    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import admin, ops_manager

    day = date(2026, 9, 30)
    rows = [
        {"date": "2026-09-30", "shop_name": "Garmin Next", "net_gross_sales": 8100000, "gross_sales": 8500000,
         "orders_count": 20, "returns_count": 1},
        {"date": "2026-09-30", "shop_name": "Garmin Samarqand Darvoza", "net_gross_sales": 4350000.5, "gross_sales": 4350000,
         "orders_count": 14, "returns_count": 0},
        {"date": "2026-09-29", "shop_name": "Garmin Next", "net_gross_sales": 999, "orders_count": 1},
    ]
    s = sales.day_sales(rows, day)
    check("one day, biggest shop first", [x.name for x in s.shops], ["Garmin Next", "Garmin Samarqand Darvoza"])
    check("totals", (round(s.net_sales), s.orders), (12450000, 34))
    block = sales.render_day(s)
    check_true("brief block: total, cheques, each shop, returns",
               "Billz" in block and "34 та чек" in block and "Garmin Next" in block and "1 та қайтариш" in block)
    check_true("brief block is Uzbek Cyrillic (shop names aside)",
               latin_words(block, allow={"Garmin", "Next", "Samarqand", "Darvoza", "Billz"}) == [])
    check_true("a day with no sales says so", "сотув бўлмаган" in sales.render_day(sales.day_sales([], day)))
    with_idle = sales.day_sales(rows + [{"date": "2026-09-30", "shop_name": "PRIMUS Склад", "net_gross_sales": 0,
                                         "orders_count": 0}], day)
    check_true("shops that sold nothing (warehouses) are left out", "PRIMUS" not in sales.render_day(with_idle))
    text = sales.describe(rows, [{"seller_name": "Ширин", "net_gross_sales": 5000000, "orders_count": 9,
                                  "average_cheque": 555555}],
                          [{"product_name": "fēnix 8", "net_sales": 18000000, "net_sold_measurement_value": 1}],
                          date(2026, 9, 1), day)
    check_true("the Director's data: shops, sellers, products",
               "Garmin Next: net 8,100,999" in text and "Ширин: 5,000,000, 9 sales" in text and "fēnix 8: 18,000,000, 1 pcs" in text)

    brief = load_agent("ceo-daily-brief")
    check_true("the brief shows shop sales once BILLZ is set up",
               "Дўконлар (Billz)" in brief.render(brief.BriefData(report_rows=[], shop_sales=s)))
    check_true("no BILLZ, no block", "Billz" not in brief.render(brief.BriefData(report_rows=[])))
    check_true("a BILLZ failure is said plainly",
               "Billz'дан маълумот олиб бўлмади" in brief.render(brief.BriefData(report_rows=[], shop_sales_failed=True)))

    # ---- the client against a fake BILLZ
    calls: list[httpx.Request] = []
    state = {"logins": 0, "expire_once": False, "forbidden": False}

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/auth/login":
            state["logins"] += 1
            ok = json.loads(request.content) == {"secret_token": "key-123"}
            return httpx.Response(200 if ok else 403, json={"code": 200, "message": "ok", "error": None,
                                  "data": {"access_token": f"A{state['logins']}", "expires_in": 1296000}})
        if state["forbidden"]:
            return httpx.Response(403, json={"code": 403, "message": "forbidden", "error": "access denied"})
        if state["expire_once"]:
            state["expire_once"] = False
            return httpx.Response(401, json={"code": 401, "message": "token expired"})
        page = int(request.url.params["page"])
        batch = [{"date": "2026-09-30", "shop_name": f"S{(page - 1) * 100 + i}", "net_gross_sales": 1,
                  "orders_count": 1} for i in range(100 if page == 1 else 3)]
        return httpx.Response(200, json={"count": 103, "shop_stats_by_date": batch})

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    saved = [(bz, "audited", bz.audited), (bz, "PAUSE", bz.PAUSE),
             (settings, "billz_secret_token", settings.billz_secret_token), (settings, "billz_enabled", settings.billz_enabled)]
    bz.audited, bz.PAUSE = no_audit, 0
    settings.billz_secret_token, settings.billz_enabled = SecretStr("key-123"), True

    async def read(**flags):
        state.update(flags)
        async with bz.BillzClient(agent="test", transport=httpx.MockTransport(handler)) as c:
            return await c.shop_days(day, day)

    try:
        got = asyncio.run(read())
        check("both pages read (103 rows)", len(got), 103)
        report = [r for r in calls if r.url.path == "/v1/general-report-table"]
        check_true("bearer token, day detalization, dates", report[0].headers["authorization"] == "Bearer A1"
                   and report[0].url.params["detalization"] == "day" and report[0].url.params["start_date"] == "2026-09-30")
        check("one login for the run", state["logins"], 1)
        state["logins"] = 0
        check_true("an expired token: one fresh login, then the read goes on",
                   len(asyncio.run(read(expire_once=True))) == 103 and state["logins"] == 2)
        try:
            asyncio.run(read(forbidden=True))
            check_true("a refusal raises", False)
        except bz.BillzError as exc:
            check_true("a refusal says why, never the key", "HTTP 403" in str(exc) and "key-123" not in str(exc))
        state["forbidden"] = False
        settings.billz_secret_token = SecretStr("")
        try:
            asyncio.run(read())
            check_true("not configured raises", False)
        except bz.BillzError:
            check_true("not configured: refuses before any call", True)
        check_true("the bot says BILLZ isn't connected", "not connected" in asyncio.run(ops_manager._fetch_billz_data()))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
    check_true("billz_savdo is a data source the Director can ask", "billz_savdo" in AGENT_SLUGS)

    sent: list[str] = []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, **kwargs):
            sent.append(text)
            return [1]

    saved = [(admin, "TelegramBot", admin.TelegramBot), (settings, "admin_bot_admin_user_id", settings.admin_bot_admin_user_id),
             (settings, "billz_secret_token", settings.billz_secret_token)]
    admin.TelegramBot, settings.admin_bot_admin_user_id, settings.billz_secret_token = FakeBot, 0, SecretStr("")
    try:
        check("/billz before setup", asyncio.run(admin.handle_admin_message({"from": {"id": 9}, "text": "/billz"}, uuid.uuid4())),
              "billz_not_configured")
        check_true("...says where the key goes", "BILLZ_SECRET_TOKEN" in sent[-1] and "Ключи интеграции" in sent[-1])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_verifix_basic_login() -> None:
    """Verifix with a user's login + password (the docs' Basic auth) and the organisation ID."""
    print("Verifix login + password")
    import asyncio
    import base64
    import contextlib
    import json

    import httpx
    from pydantic import SecretStr

    from integrations.common.config import settings
    from integrations.verifix import client as vx

    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("filial$info"):
            return httpx.Response(200, text=json.dumps({"filial_name": "ЭМЖИЕМ"}))
        return httpx.Response(200, text=json.dumps({"data": [{"name": "Опоздание", "time_kind_id": "82"}],
                                                    "meta": {"next_cursor": "-1"}}))

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    names = ("verifix_client_id", "verifix_client_secret", "verifix_login", "verifix_password", "verifix_filial_id",
             "verifix_enabled")
    saved = [(settings, n, getattr(settings, n)) for n in names] + [(vx, "audited", vx.audited)]
    vx.audited = no_audit
    settings.verifix_client_id, settings.verifix_client_secret = "", SecretStr("")
    settings.verifix_login, settings.verifix_password, settings.verifix_enabled = "admins@emjiem", SecretStr("p@ss"), True
    try:
        settings.verifix_filial_id = ""
        check("login + password without the organisation ID: not configured", settings.verifix_auth, None)
        settings.verifix_filial_id = "161"
        check("login + password + organisation ID: basic", settings.verifix_auth, "basic")

        async def read():
            async with vx.VerifixClient(agent="test", transport=httpx.MockTransport(handler)) as c:
                return await c.organisation(), await c.time_kinds()

        org, kinds = asyncio.run(read())
        expected = "Basic " + base64.b64encode(b"admins@emjiem:p@ss").decode()
        check_true("every call: Basic auth, project_code, filial_id",
                   all(r.headers.get("authorization") == expected and r.headers.get("project_code") == "vhr"
                       and r.headers.get("filial_id") == "161" for r in seen))
        check_true("no token request in this mode", not any(r.url.path.endswith("oauth/token") for r in seen))
        check("the organisation's name, to check the ID", org.get("filial_name"), "ЭМЖИЕМ")
        check("data is read", kinds[0]["name"], "Опоздание")
        settings.verifix_client_id, settings.verifix_client_secret = "cid", SecretStr("sec")
        check("client id + secret win when both are set", settings.verifix_auth, "oauth")
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_onec() -> None:
    """1C over OData: GET-only client, Basic auth, the /1c discovery report."""
    print("1C (Clobus, OData)")
    import asyncio
    import base64
    import contextlib
    from datetime import datetime

    import httpx
    from pydantic import SecretStr

    from integrations.common.config import settings
    from integrations.onec import client as oc
    from integrations.onec import discover
    from integrations.org_bot import admin

    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("standard.odata/"):
            return httpx.Response(200, json={"value": [{"name": n} for n in (
                "ChartOfAccounts_Хозрасчетный", "AccountingRegister_Хозрасчетный", "Catalog_Контрагенты",
                "Document_НачислениеЗарплаты", "Catalog_ФизическиеЛица")]})
        if "ChartOfAccounts" in path:
            return httpx.Response(200, json={"value": [
                {"Ref_Key": "k1", "Code": "5010", "Description": "Касса в национальной валюте", "DeletionMark": False},
                {"Ref_Key": "k2", "Code": "5110", "Description": "Расчетный счет", "DeletionMark": False},
                {"Ref_Key": "k3", "Code": "4010", "Description": "Счета к получению", "DeletionMark": False}]})
        if "Balance" in path:
            return httpx.Response(200, json={"value": [
                {"Account_Key": "k1", "СуммаBalance": 1500000.5}, {"Account_Key": "k2", "СуммаBalance": 98000000},
                {"Account_Key": "k3", "СуммаBalance": 5}]})
        return httpx.Response(404, json={"odata.error": {"message": {"value": "not found"}}})

    @contextlib.asynccontextmanager
    async def no_audit(**kwargs):
        yield {"http_status": None, "payload": {}}

    names = ("onec_odata_url", "onec_login", "onec_password")
    saved = [(settings, n, getattr(settings, n)) for n in names] + [(oc, "audited", oc.audited)]
    oc.audited = no_audit
    settings.onec_odata_url, settings.onec_login = "https://clobus.uz/a/acc313/61458/odata/standard.odata/", "mgmg_bot_odata"
    settings.onec_password = SecretStr("s3cr3t")
    try:
        check_true("configured", settings.onec_configured)

        async def probe():
            async with oc.OneCClient(agent="t", transport=httpx.MockTransport(handler)) as c:
                return await discover.run(c, datetime(2026, 10, 2, 9, 0))

        report = asyncio.run(probe())
        expected = "Basic " + base64.b64encode(b"mgmg_bot_odata:s3cr3t").decode()
        check_true("every call is a GET with Basic auth and $format=json",
                   all(r.method == "GET" and r.headers["authorization"] == expected
                       and r.url.params.get("$format") == "json" for r in seen))
        check_true("Cyrillic names are encoded in the path", any("%D0%A5" in str(r.url) for r in seen))
        check("money accounts by the Uzbek chart (class 5000)", [a["code"] for a in report["accounts"]], ["5010", "5110"])
        check("balance field found", report["amount_field"], "СуммаBalance")
        check("balances per money account", report["balances"], {"5010": 1500000.5, "5110": 98000000.0})
        check("payroll / personal data flagged", discover.sensitive(report["sets"]),
              ["Catalog_ФизическиеЛица", "Document_НачислениеЗарплаты"])
        check_true("the client has no way to write", not any(hasattr(oc.OneCClient, m) for m in ("post", "patch", "put", "delete")))
        text = admin._onec_text(report)
        check_true("/1c report: connected, accounts counted, warning about payroll",
                   "1C уланди" in text and "Пул ҳисобварақлари (5000-синф): 2 та, қолдиғи ўқилди: 2 та" in text
                   and "Иш ҳақи" in text)
        check_true("/1c shows no balances to IT (Director's order 07.10.2026)",
                   not any(x in text for x in ("98", "1 500", "сўм")))
        check_true("/1c without a login says why", "1C'га уланиб бўлмади" in admin._onec_text(
            {"sets": [], "accounts": [], "fields": [], "balances": {}, "errors": ["HTTP 401 — unauthorized"]}))
        settings.onec_password = SecretStr("")
        check_true("no password: not configured", not settings.onec_configured)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_onec_cash() -> None:
    """1C money: the brief's «Касса», the change since yesterday, the Director's data."""
    print("1C cash in the brief")
    from datetime import datetime

    from integrations.common.agent_loader import load_agent
    from integrations.onec import cash

    report = {"accounts": [{"key": "a", "code": "5010", "name": "Основная касса организации"},
                           {"key": "b", "code": "5110.1", "name": "Расчетные счета"},
                           {"key": "c", "code": "5110.2", "name": "Расчетные счета"},
                           {"key": "d", "code": "5210", "name": "Валютные счета внутри Республики"}],
              "balances": {"5010": 76719951.0, "5110.1": 2368215063.0, "5110.2": 1152167476.0, "5210": 0.0},
              "amount_field": "СуммаBalance", "errors": []}
    pos = cash.from_report(report, datetime(2026, 10, 2, 8, 0))
    check("cash desk vs bank (2026-10-02 /1c figures)", (round(pos.cash), round(pos.bank)), (76719951, 3520382539))
    check("zero accounts are left out", [a[0] for a in pos.accounts], ["5010", "5110.1", "5110.2"])
    try:
        cash.from_report({"accounts": [], "balances": {}, "amount_field": None, "errors": ["HTTP 401"]}, pos.at)
        check_true("unreadable balances raise", False)
    except Exception as exc:
        check_true("unreadable balances raise, with 1C's reason", "401" in str(exc))

    brief = load_agent("ceo-daily-brief")
    check_true("the brief no longer shows cash (the owner, 16.10.2026)", "Касса" not in brief.render(brief.BriefData(report_rows=[])))
    data = cash.describe(pos)
    check_true("the Director's data: totals and accounts", "bank accounts 3,520,382,539" in data and "5110.2" in data)
    check_true("pul_qoldigi is a data source the Director can ask", "pul_qoldigi" in AGENT_SLUGS)


def test_sap_freshness() -> None:
    """2026-10-06: SAP stopped pushing on 03.10 14:13 and three briefs showed Saturday as today."""
    print("SAP freshness and the 03-06.10 lessons")
    import asyncio
    import uuid
    from datetime import datetime

    from integrations.common.agent_loader import load_agent
    from integrations.common.timeutil import TASHKENT, now_local
    from integrations.org_bot import ops_manager
    from integrations.sap import push_handler
    from integrations.sap.figures import Figure
    from integrations.sap.models import ARAging, ARInvoice

    brief = load_agent("ceo-daily-brief")
    friday = datetime(2026, 10, 3, 14, 13, tzinfo=TASHKENT)
    silent = brief.BriefData(report_rows=[], sap_pushed_at=friday)
    check_true("SAP quiet for days is silent", brief.sap_silent(silent, now=datetime(2026, 10, 6, 8, 0, tzinfo=TASHKENT)))
    check_true("pushed 20 minutes ago is not",
               not brief.sap_silent(brief.BriefData(sap_pushed_at=now_local())))
    check_true("never pushed: no warning (SAP not set up)", brief.sap_notice(brief.BriefData()) is None)

    text = brief.render(silent)
    check_true("the brief opens with the warning and the exact time",
               "⚠️ <b>SAP 03.10 14:13 дан бери маълумот юбормаяпти" in text)
    stale_sales = Figure(status="stale", as_of=friday.date(), since=friday)
    check_true("yesterday's sales from an old push: 'not updated since', never 0",
               "📈 Кечаги сотув: <i>SAP маълумоти 03.10 14:13 дан бери янгиланмаган</i>"
               in brief.render(brief.BriefData(report_rows=[], sales=stale_sales, sap_pushed_at=friday)))
    aging = ARAging(snapshot_date=friday.date())
    aging.invoices = [ARInvoice(doc_entry=1, doc_num=2150, card_code="C1", card_name="Rich Home", days_overdue=47,
                                aging_bucket="31_60", currency="USD", doc_total_tiyin=976431, balance_due_tiyin=976431)]
    with_debt = brief.BriefData(report_rows=[], aging=aging, sap_pushed_at=friday)
    check_true("debt says when it is from", "(03.10 14:13 ҳолатида)" in brief.render(with_debt))
    view = brief.view(with_debt)
    check_true("the picture carries the warning", view["notice"] and "03.10 14:13" in view["notice"])
    debt_row = next(f for f in view["figures"] if f["label"] == "Мижоз қарзи")
    check("the picture's debt row says when", (debt_row["sub"], debt_row["sub_alert"]), ("03.10 14:13 ҳолатида", True))
    check_true("the caption warns too", "03.10 14:13" in brief.caption(with_debt))
    check_true("a fresh brief has no warning", brief.view(brief.BriefData(report_rows=[]))["notice"] is None)

    sent: list[str] = []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, **kwargs):
            sent.append(text)
            return [1]

    import integrations.telegram.bot as tg
    from pydantic import SecretStr

    from integrations.common.config import settings

    saved = [(tg, "TelegramBot", tg.TelegramBot), (settings, "admin_bot_telegram_bot_token", settings.admin_bot_telegram_bot_token),
             (settings, "admin_bot_telegram_chat_id", settings.admin_bot_telegram_chat_id), (settings, "dry_run", settings.dry_run),
             (settings, "bots_frozen", settings.bots_frozen)]
    tg.TelegramBot = FakeBot
    settings.admin_bot_telegram_bot_token, settings.admin_bot_telegram_chat_id = SecretStr("t"), "9"
    settings.dry_run, settings.bots_frozen = False, False
    try:
        check_true("the admin is told SAP is silent", asyncio.run(brief.tell_admin_sap_silent(silent, uuid.uuid4()))
                   and "SAP маълумот юбормаяпти" in sent[-1] and "Task Scheduler" in sent[-1])
        check_true("…and not when SAP is fine",
                   not asyncio.run(brief.tell_admin_sap_silent(brief.BriefData(sap_pushed_at=now_local()), uuid.uuid4())))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # stock: the gateway sends item rows; they're summed per warehouse
    items = [{"WhsCode": "01", "WhsName": "Asosiy", "ItemCode": "A", "OnHand": 5, "AvgPrice": 0, "StockValue": 0},
             {"WhsCode": "01", "WhsName": "Asosiy", "ItemCode": "B", "OnHand": 2, "AvgPrice": 100, "StockValue": 200},
             {"WhsCode": "04", "WhsName": "Garmin-Abay", "ItemCode": "C", "OnHand": 1, "AvgPrice": 50}]
    per = push_handler.stock_by_warehouse(items)
    check("item rows -> one total per warehouse (value from AvgPrice when StockValue is missing)",
          [(r["WhsCode"], r["Items"], r["OnHand"], r["StockValue"]) for r in per],
          [("01", 2, 7.0, 200.0), ("04", 1, 1.0, 50.0)])
    summary = [{"WhsCode": "01", "WhsName": "A", "Items": 3, "OnHand": 5, "StockValue": 7}]
    check("per-warehouse rows (the spec's shape) are kept as they are", push_handler.stock_by_warehouse(summary), summary)

    # the Director's AI: data age, currency, terms
    pushed_utc = datetime(2026, 10, 3, 9, 13, tzinfo=TASHKENT).astimezone(TASHKENT)

    async def fake_one(sql, params=None):
        if "max(occurred_at)" in sql:
            return {"at": friday}
        return {"payload": {"tool": "customers", "rows_received": 100}}

    async def fake_all(sql, params=None):
        if "v_ar_aging_latest" in sql:
            return [{"doc_num": 2150, "card_name": "Rich Home", "doc_date": date(2026, 8, 17), "days_overdue": 47,
                     "aging_bucket": "31_60", "balance_due_tiyin": 976431, "currency": "USD", "due_date": date(2026, 8, 17),
                     "sales_person_name": None, "captured_at": friday}]
        if "v_sap_gateway_latest" in sql:
            return [{"natural_key": str(i), "raw": {"CardCode": f"C{i}"}, "captured_at": pushed_utc} for i in range(100)]
        if "daily_briefs" in sql:
            return [{"brief_date": date(2026, 10, 3), "ar_overdue_total_tiyin": 3411159, "sections": {"a2": {"debt": {"capped": False}}}},
                    {"brief_date": date(2026, 10, 2), "ar_overdue_total_tiyin": 507377, "sections": {"a2": {"debt": {"capped": True}}}}]
        return []

    saved = [(ops_manager, "fetch_one", ops_manager.fetch_one), (ops_manager, "fetch_all", ops_manager.fetch_all)]
    ops_manager.fetch_one, ops_manager.fetch_all = fake_one, fake_all
    try:
        finance = asyncio.run(ops_manager._fetch_finance_agent_data())
        check_true("debt data: the push stopped, as of when", "WARNING: SAP last pushed data at 2026-10-03 14:13" in finance)
        check_true("…amounts are USD equivalents of so'm invoices", "USD equivalents" in finance and "DocCur" not in finance)
        check_true("…'overdue' is days since the invoice", "due date equal to the invoice date" in finance)
        check_true("…each invoice with its date", "Invoice #2150 dated 2026-08-17" in finance and "not set in SAP" in finance)
        customers = asyncio.run(ops_manager._fetch_sap_gateway_data("customers"))
        check_true("a 100-row list is called partial", "PARTIAL LIST" in customers and "latest 100" in customers)
        history = asyncio.run(ops_manager._fetch_reporter_agent_data())
        check_true("old partial debt days are marked, not compared",
                   "2026-10-02: AR overdue=$5,073.77 (PARTIAL — not comparable)" in history and "(complete)" in history)
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def test_invoice_as_written() -> None:
    """2026-10-06: asked "Сделай в суммах", the bot had only SAP's USD for invoices written in so'm."""
    print("Debt as written on the invoices (so'm)")
    import asyncio
    from datetime import datetime

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.common.timeutil import TASHKENT
    from integrations.org_bot import ops_manager
    from integrations.sap import push_handler
    from integrations.sap.models import ARAging, ARInvoice, doc_balance

    def plain(text: str) -> str:
        return text.replace("\xa0", " ")

    saved_currency = settings.sap_default_currency
    settings.sap_default_currency = "USD"
    try:
        # what the gateway sends for a so'm invoice (the spec's columns)
        row = {"DocEntry": 9, "DocNum": 2150, "CardCode": "C1", "CardName": "Rich Home", "DocDate": "2026-08-17",
               "DocDueDate": "2026-08-17", "DocStatus": "O", "CANCELED": "N", "DocCur": "UZS", "DocTotal": "9764.31",
               "PaidToDate": "0", "DocTotalFC": "124056000.00", "PaidFC": "0.00", "SlpCode": -1}
        stored = push_handler.aging_row(row, date(2026, 10, 3), {})
        check("stored: SAP's USD and the invoice's own so'm", (stored[12], stored[15:]), (976431, ("UZS", 12405600000, 0)))
        check("so'm not sent: kept empty", push_handler.invoice_currency({**row, "DocTotalFC": None}), ("UZS", None, None))
        check("part-paid without PaidFC: the so'm balance isn't guessed",
              push_handler.invoice_currency({**row, "PaidToDate": "100", "PaidFC": None}), ("UZS", None, None))
        check("a USD invoice has no second amount", push_handler.invoice_currency({**row, "DocCur": "USD"}), ("USD", None, None))
    finally:
        settings.sap_default_currency = saved_currency

    som = ARInvoice(doc_entry=9, doc_num=2150, card_code="C1", card_name="Rich Home", days_overdue=47,
                    aging_bucket="31_60", currency="USD", doc_total_tiyin=976431, balance_due_tiyin=976431,
                    doc_currency="UZS", doc_balance_tiyin=doc_balance(12405600000, 0))
    check("as written: so'm", som.as_written, (12405600000, "UZS"))
    check("so'm not sent yet: SAP's USD", som.model_copy(update={"doc_balance_tiyin": None}).as_written, (976431, "USD"))

    brief = load_agent("ceo-daily-brief")
    aging = ARAging(snapshot_date=date(2026, 10, 3), invoices=[som])
    switch_day = brief.BriefData(report_rows=[], aging=aging,
                                 previous={"debt": {"status": "ok", "totals": {"USD": 900_000}, "capped": False}})
    text = plain(brief.render(switch_day))
    check_true("the brief's debt in so'm, as on the invoices", "🧾 Мижоз қарзи: 124 056 000 сўм" in text)
    check_true("…no 'change' on the day so'm replaces $", "кечагига" not in text)
    check("stored with how it was counted", brief.five_numbers_json(switch_day)["debt"]["basis"], "invoice")
    next_day = brief.BriefData(report_rows=[], aging=aging, previous={"debt": {
        "status": "ok", "totals": {"UZS": 12_000_000_000}, "capped": False, "basis": "invoice"}})
    check_true("…compared again from the next day", "кечагига ▲ 4 056 000 сўм" in plain(brief.render(next_day)))
    debt_row = next(f for f in brief.view(switch_day)["figures"] if f["label"] == "Мижоз қарзи")
    check("the picture: short so'm", (debt_row["number"], plain(debt_row["unit"])), ("124,06", "млн сўм"))

    now = datetime.now(TASHKENT)
    rows = [{"doc_num": 2150, "card_name": "Rich Home", "doc_date": date(2026, 8, 17), "days_overdue": 47,
             "aging_bucket": "31_60", "balance_due_tiyin": 976431, "currency": "USD", "due_date": date(2026, 8, 17),
             "sales_person_name": None, "captured_at": now, "doc_currency": "UZS", "doc_total_fc_tiyin": 12405600000,
             "paid_fc_tiyin": 0},
            {"doc_num": 2001, "card_name": "AQUA CARE LLC", "doc_date": date(2026, 9, 1), "days_overdue": 20,
             "aging_bucket": "1_30", "balance_due_tiyin": 10000, "currency": "USD", "due_date": date(2026, 9, 1),
             "sales_person_name": "Ali", "captured_at": now, "doc_currency": "UZS", "doc_total_fc_tiyin": None,
             "paid_fc_tiyin": None}]
    shown: list[dict] = []

    async def fake_one(sql, params=None):
        return {"at": now}

    async def fake_all(sql, params=None):
        return shown if "v_ar_aging_latest" in sql else []

    saved = [(ops_manager, "fetch_one", ops_manager.fetch_one), (ops_manager, "fetch_all", ops_manager.fetch_all)]
    ops_manager.fetch_one, ops_manager.fetch_all = fake_one, fake_all
    try:
        shown[:] = rows
        mixed = plain(asyncio.run(ops_manager._fetch_finance_agent_data()))
        check_true("the bot: each invoice in so'm with SAP's $ beside it",
                   "Invoice #2150 dated 2026-08-17, Rich Home: 124 056 000 сўм (SAP's USD equivalent $9,764.31)" in mixed)
        check_true("…the total worked out, currencies kept apart", "TOTAL open: 124 056 000 сўм + $100.00 (2 invoices)" in mixed)
        check_true("…by age", "31_60: 124 056 000 сўм (1 invoices)" in mixed and "1_30: $100.00 (1 invoices)" in mixed)
        check_true("…says which are which", "1 of 2 invoices show their own so'm amount" in mixed)
        shown[:] = rows[:1]
        check_true("all in so'm: lead with so'm", "Lead with the so'm amounts" in asyncio.run(ops_manager._fetch_finance_agent_data()))
        shown[:] = rows[1:]
        check_true("no so'm yet: never a guessed rate", "never convert with a guessed rate" in asyncio.run(ops_manager._fetch_finance_agent_data()))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def main() -> int:
    """Run every check.

    Returns:
        0 if all checks pass, 1 otherwise.
    """
    from integrations.common.config import settings

    # The Director's analyst calls the real AI: off for every suite but its own
    # (test_analyst switches it on), so no check can reach OpenRouter.
    settings.ops_analyst_enabled = False
    # Cheque photos call the real AI too (test_cheques switches it on).
    settings.cheque_reader_enabled = False
    # No task waits for its proof unless a suite says so (task register, 2026-10-13): every
    # employee message asks the database first, and no suite may reach a real one.
    from integrations.org_bot import store

    async def no_task_waiting(*args, **kwargs):
        return None

    store.task_awaiting_input = no_task_waiting
    for suite in (
        test_references,
        test_bot_flows,
        test_money,
        test_time,
        test_aging,
        test_telegram,
        test_org_bot,
        test_permissions,
        test_permission_form,
        test_task_tracker,
        test_payment_gate,
        test_report_accuracy,
        test_client_feedback,
        test_lead_tracker,
        test_lead_index,
        test_deepseek_web_search,
        test_removed_people_out_of_stats,
        test_db_mirror,
        test_politeness_days_off_announcements,
        test_ai_chat_and_sheet,
        test_files_reports_cheer_off,
        test_reports_off,
        test_analyst,
        test_it_restrictions,
        test_tech_report_errors_fixed,
        test_review_pages,
        test_task_register,
        test_review_fixes,
        test_data_check_fixes,
        test_employee_change_fields,
        test_accountant_test_fixes,
        test_report_deadline_penalty,
        test_command_menus,
        test_garmin_memory_and_team_reports,
        test_stock_items,
        test_sap_everything,
        test_cleanup_round_17_10,
        test_own_week_and_team_calendar,
        test_old_stock_and_live_views,
        test_garmin_competitors,
        test_analyst_speed_and_style,
        test_report_media_and_auto,
        test_explanations_and_start_times,
        test_pulse,
        test_report_bonus,
        test_excused_and_late_reports,
        test_morning_verifix_and_admin_sheet,
        test_lead_groups,
        test_reports_any_time,
        test_garmin_stock_quantity,
        test_garmin_stock_for_ordering,
        test_reregistration_and_order,
        test_garmin_manager,
        test_complaint_tickets,
        test_cheques,
        test_fast_buttons,
        test_data_access,
        test_backup,
        test_knowledge,
        test_ap_reconcile,
        test_verifix,
        test_flexible_schedule,
        test_verifix_basic_login,
        test_employee_admin,
        test_kpi,
        test_garmin_leads,
        test_billz,
        test_sap_full_push,
        test_sap_gateway_code,
        test_reports,
        test_billz_sap_check,
        test_onec,
        test_onec_cash,
        test_sap_freshness,
        test_invoice_as_written,
        test_plan_agents,
        test_db_viewer,
        test_names_and_routing,
        test_registration_states,
        test_report_or_message,
        test_daily_report_kpi,
        test_brief_rendering,
        test_dates_and_figures,
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
