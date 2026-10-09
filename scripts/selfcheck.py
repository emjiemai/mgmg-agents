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

    allowed = {"CEO", "IT", "KPI", "SAP", "CRM", "HR", "AI", "QR", "Garmin", "Londry", "EMJ", "SOP", "ADM", "OPS", "Admin", "Bot", "Verifix"}
    allowed |= allow or set()
    # Telegram commands (/ismlar, /bekor) can only be Latin.
    plain = re.sub(r"/[a-z_]+", " ", re.sub(r"<[^>]+>", " ", text))
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:'[A-Za-z]+)*", plain) if w not in allowed]


def friendly_problems(text: str, names: tuple[str, ...] = ()) -> list[str]:
    """What breaks the friendly voice (tone.py) in a message, if anything.

    One line, lowercase except the names in it (which must keep their capital,
    2026-10-01), polite ("сиз", never "сен"), no markup, and at most one emoji
    — at the very end.
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
    emoji_at = [i for i, char in enumerate(text) if tone._EMOJI.match(char) and char not in "\ufe0f\u200d"]
    if len(emoji_at) > 1:
        problems.append("more than one emoji")
    if emoji_at and text[emoji_at[0]:].strip("\ufe0f\u200d ") != text[emoji_at[0]]:
        problems.append("emoji not at the end")
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

    async def fake_reply(_chat_id, _run_id, text, reply_markup=None):
        replies.append(text)
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
        asyncio.run(ops_manager._dispatch_director_task(1, "kechagi hisobotlarni korsat", 11, uuid.uuid4()))
        check("a question gets the AI's answer, not the error", replies, ["Жавоб"])

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
    check_true("cash says plainly it isn't connected", "💰 Касса: <i>уланмаган</i>" in text)
    check_true("a missing feed reads 'маълумот йўқ', never a number", "📈 Кечаги сотув: <i>маълумот йўқ</i>" in text)
    check_true("all five lines are there", all(k in text for k in ("Касса", "Кечаги сотув", "Захира", "Мижоз қарзи", "Бугунги тўловлар")))

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
    check_true("yesterday's sales with the order count", "📈 Кечаги сотув: $12,340.00 (3 та буюртма)" in text)
    check_true("unchanged sales show no change", "Кечаги сотув: $12,340.00 (3 та буюртма)\n" in text)
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
    check_true("age ranges folded into the headline", "90+ кун — 2" in text and "1–30 кун — 1" in text)
    check_true("two lines only", len(text.splitlines()) == 2)
    check_true("no per-invoice detail", "Customer" not in text)
    check_true("no owner breakdown", "Mas'ul xodim" not in text)
    one_range = receivables.render(aging, min_days=100)
    check_true("single range reads 'ичида'", "2 та ҳисоб-фактура, 90+ кун ичида" in one_range)
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

    async def fake_reply(chat_id, run_id, text, reply_markup=None):
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
    check("a plain question -> ask", report_message_kind(False, False, True), "ask")
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

    original_read, original_password = db_viewer._read, settings.db_viewer_password
    db_viewer._read = fake_read
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
        check_true("closed tables listed with a lock and no link",
                   "🔒 employees" in index.text and "/db/employees" not in index.text
                   and "/db/v_ar_aging_latest" not in index.text and "href='/db/agent_actions'" in index.text)
        check_true("never indexed or cached", index.headers.get("x-robots-tag", "").startswith("noindex")
                   and index.headers.get("cache-control") == "no-store")

        seen_queries.clear()
        closed = client.get("/db/v_ar_aging_latest", headers=good)
        check_true("a financial table is closed (Director's order 07.10.2026)", "Бу жадвал ёпиқ" in closed.text)
        check_true("...its rows are never read", not any("v_ar_aging_latest" in q and "LIMIT" in q for q in seen_queries))
        page = client.get("/db/agent_actions", headers=good)
        check_true("shows a row", page.status_code == 200 and "full_name" in page.text)
        check_true("what the data holds is escaped", "&lt;script&gt;" in page.text and "<script>x" not in page.text)
        check_true("timestamps in Tashkent time", "2026-09-26 08:00:00" in page.text)
        check_true("newest first", any("ORDER BY" in q and "created_at" in q for q in seen_queries))

        seen_queries.clear()
        missing = client.get("/db/pg_authid", headers=good)
        check_true("a table outside the list is refused", "Бундай жадвал йўқ" in missing.text)
        check_true("...without ever being queried", not any("pg_authid" in q for q in seen_queries))
    finally:
        db_viewer._read = original_read
        settings.db_viewer_password = original_password


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


def test_team_cheer() -> None:
    """Team cheer: when each slot goes out, the friendly voice, the AI's limits, the send, taps and replies."""
    print("team cheer (10:00 / 17:35)")
    import asyncio
    import json
    import re
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import cheer, ops_manager, store, tone

    # ---- when
    for hm, expected in (("09:00", None), ("10:00", "morning"), ("10:24", "morning"), ("10:35", None),
                         ("14:00", None), ("15:00", None), ("17:00", None), ("17:35", "evening"),
                         ("17:59", "evening"), ("18:00", None)):
        h, m = map(int, hm.split(":"))
        check(f"at {hm}", cheer.due_slot(datetime(2026, 9, 28, h, m)), expected)
    # The one cron service's runs (UTC) must hit each slot exactly once.
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text(encoding="utf-8")
    minutes, hours = re.search(r"name: mgmg-team-cheer\n(?:.*\n)*?\s*schedule: \"([^\"]+)\"", blueprint).group(1).split()[:2]
    runs = [(int(h) + 5, int(m)) for h in hours.split(",") for m in minutes.split(",")]  # Tashkent = UTC+5
    fired = [cheer.due_slot(datetime(2026, 9, 28, h, m)) for h, m in runs]
    check("render.yaml's runs send each slot once, the rest nothing",
          sorted(f for f in fired if f), ["evening", "morning"])
    check("the 14:00 joke is deleted (2026-10-02)", sorted(cheer.SLOTS), ["evening", "morning"])

    # ---- the voice
    check("casual: one line, lowercase, no emoji inside, no full stop",
          tone.casual("Бугун 😀 ҳам\n<b>ЗЎР</b> кун бўлсин.", "☀️"), "бугун ҳам зўр кун бўлсин ☀️")
    check("casual: a second emoji is not added", tone.casual("салом", "☀️☀️"), "салом")
    check_true("two sentences are not one", not tone.is_one_short_sentence("бугун зўр. эртага ҳам зўр"))

    # ---- the built-in messages: every day of two months, in the friendly voice
    ok = True
    for offset in range(60):
        day = date(2026, 10, 1) + timedelta(days=offset)
        for slot in cheer.SLOTS:
            fb = cheer.fallback(slot, day)
            again = cheer.parse_ai(json.dumps({"text": fb.text, "emoji": fb.emoji, "options": fb.options}), slot)
            line = cheer.message_text(fb, "Дилноза опа")
            problems = friendly_problems(line, ("Дилноза опа",)) + latin_words(line)
            if again is None or bool(fb.options) != (slot == "evening") or problems or not fb.emoji:
                ok = False
                print(f"    bad fallback: {slot} {day} {problems}")
    check_true("every built-in message: one friendly line, one emoji at the end, passes the AI's own rules", ok)
    check_true("consecutive days differ",
               cheer.fallback("morning", date(2026, 10, 1)).text != cheer.fallback("morning", date(2026, 10, 2)).text)

    # ---- what the AI may say
    good = {"text": "Бугун кунингиз қандай ўтди? 😊", "emoji": "🌙",
            "options": [{"label": "Зўр", "reply": "Ажойиб, раҳмат!"}, {"label": "чарчадим", "reply": "яхшилаб дам олинг"}]}
    parsed = cheer.parse_ai(json.dumps(good), "evening")
    check_true("a good answer is used, made casual",
               parsed is not None and parsed.source == "ai" and parsed.text == "бугун кунингиз қандай ўтди?"
               and parsed.options[0]["label"] == "зўр" and parsed.emoji == "🌙")
    morning = cheer.parse_ai(json.dumps({**good, "text": "бугун ҳам зўр кун бўлсин"}), "morning")
    check_true("morning drops any answers", morning is not None and not morning.options)
    check("a missing emoji gets the slot's own",
          cheer.parse_ai(json.dumps({**good, "emoji": "зўр"}), "evening").emoji, "🌙")

    def bad(slot="evening", **change):
        return cheer.parse_ai(json.dumps({**good, **change}), slot) is None

    check_true("Latin letters are refused", bad(text="good kun"))
    check_true("two sentences are refused", bad(text="бугун зўр ўтди. эртага ҳам шундай бўлсин"))
    check_true("too long is refused", bad(text="зўр " * 40))
    check_true("markup is refused", bad(text="<b>зўр</b>"))
    check_true("evening needs answers to tap", bad(options=[]))
    check_true("five answers are too many", bad(options=[{"label": f"жавоб {i}", "reply": "раҳмат"} for i in "абвгд"]))
    check_true("a button label too long for a phone is refused",
               bad(options=[{"label": "жуда жуда жуда узун жавоб", "reply": "р"}, good["options"][1]]))
    check_true("two identical buttons are refused", bad(options=[good["options"][0], good["options"][0]]))
    check_true("an answer without its reply is refused", bad(options=[{"label": "чой"}, good["options"][1]]))
    check_true("not JSON is refused", cheer.parse_ai("Мана хабар", "evening") is None)
    prompt = cheer.user_prompt("evening", date(2026, 9, 28), ["эски хабар"])
    check_true("the AI is shown recent messages so it won't repeat them", "- эски хабар" in prompt)

    # ---- what people see
    text = cheer.message_text(parsed, "<Дилноза> опа")
    check("one line: the name (capitals kept), the sentence, one emoji", text,
          "&lt;Дилноза&gt; опа, бугун кунингиз қандай ўтди? 🌙")
    kb = cheer.keyboard(str(uuid.uuid4()), parsed)
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    check_true("buttons fit Telegram's 64-byte limit", all(len(d.encode()) <= 64 for d in datas))
    check("...two to a row", [len(r) for r in kb["inline_keyboard"]], [2])
    check("no buttons without a question", cheer.keyboard("c1", morning), None)
    check("a button's data reads back", cheer.parse_callback(datas[1].split(":", 1)[1])[1], 1)
    check("a broken one doesn't", cheer.parse_callback("abc"), None)
    check("a typed reply gets a friendly line back", friendly_problems(cheer.text_reply(7)), [])

    # ---- the send, with a fake database, AI and Telegram
    agent = load_agent("team-cheer")
    director = {"role": "operatsion_direktor", "telegram_user_id": 1, "display_name": "Д", "full_name": "Директор"}
    worker = {"role": "it", "telegram_user_id": 2, "display_name": "a", "full_name": "Алишер Каримов",
              "address_form": "aka"}
    weekend_off = {"role": "ombor", "telegram_user_id": 3, "display_name": "b", "full_name": "Бобур"}
    sent, deliveries, stored, claims = [], [], [], []

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, reply_markup=None, disable_notification=False, **kwargs):
            sent.append((chat_id, text, reply_markup, disable_notification))
            return [500 + len(sent)]

    class FakeAI:
        answer = json.dumps(good)

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def complete(self, system, user, **kwargs):
            return FakeAI.answer

    async def employees():
        return [director, worker, weekend_off]

    async def claim(day, slot):
        claims.append((day, slot))
        return {"id": "c1"} if len(claims) == 1 else None

    async def set_content(cheer_id, **kwargs):
        stored.append(kwargs)

    async def recent():
        return []

    async def save_delivery(*args):
        deliveries.append(args)

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    patch(store, "list_active_employees", employees)
    patch(store, "claim_cheer", claim)
    patch(store, "set_cheer_content", set_content)
    patch(store, "recent_cheer_texts", recent)
    patch(store, "save_cheer_delivery", save_delivery)
    patch(agent, "TelegramBot", FakeBot)
    patch(agent, "OpenRouterClient", FakeAI)
    patch(agent, "now_local", lambda: datetime(2026, 9, 26, 17, 35, tzinfo=TASHKENT))  # a Saturday
    weekend_off["works_saturday"], worker["works_saturday"] = False, True
    try:
        asyncio.run(agent.send_slot("evening", uuid.uuid4()))
        check("not the Director, not someone off today", [c for c, _, _, _ in sent], ["2"])
        check("one friendly line, first name and ака, with buttons", (sent[0][1], sent[0][2] is not None),
              ("Алишер ака, бугун кунингиз қандай ўтди? 🌙", True))
        check_true("...sent silently", sent[0][3] is True)
        check("the message is remembered for taps and replies", deliveries[0][:3], ("c1", 2, 501))
        check("the AI wrote it", stored[0]["source"], "ai")
        sent.clear()
        asyncio.run(agent.send_slot("evening", uuid.uuid4()))
        check("a second run the same day sends nothing", sent, [])

        claims.clear()
        stored.clear()
        FakeAI.answer = json.dumps({**good, "text": "Hello team"})
        asyncio.run(agent.send_slot("evening", uuid.uuid4()))
        check("an AI answer that breaks a rule is replaced by the built-in one", stored[0]["source"], "fallback")
        check("...and it still goes out, friendly and Cyrillic",
              friendly_problems(sent[-1][1], ("Алишер ака",)) + latin_words(sent[-1][1]), [])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    # ---- taps and typed replies in OPS Manager Bot
    edits, replies, toasts = [], [], []

    class EditBot(FakeBot):
        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append((chat_id, message_id, text, reply_markup))

    answers = {"row": {"id": "d1", "message_id": 501, "text": "Алишер ака, бугун кунингиз қандай ўтди? 🌙",
                       "question": None, "options": parsed.options}}

    async def answer_cheer(cheer_id, user_id, index):
        row, answers["row"] = answers["row"], None
        return row

    async def fake_answer(query_id, text):
        toasts.append(text)

    async def delivery_for(user_id, message_id):
        return {"id": "d1"} if message_id == 501 else None

    async def fake_reply(chat_id, run_id, text, reply_markup=None):
        replies.append(text)

    async def no_report(*args, **kwargs):
        raise AssertionError("a reply to a cheer message was treated as a report")

    saved.clear()
    patch(store, "answer_cheer", answer_cheer)
    patch(store, "cheer_delivery_for_message", delivery_for)

    async def not_editing(*args):
        return None

    patch(store, "report_being_edited", not_editing)
    patch(ops_manager, "TelegramBot", EditBot)
    patch(ops_manager, "_answer", fake_answer)
    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "_try_daily_report", no_report)
    try:
        tap = {"id": "q1", "data": "cheer:c1:1", "from": {"id": 2}}
        check("a tap is taken", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "cheer_answered")
        check("its reply is a pop-up, not a new message", (toasts[-1], replies), ("яхшилаб дам олинг", []))
        check_true("the buttons go, the line stays as it was",
                   edits and edits[0][3] == {"inline_keyboard": []} and edits[0][2] == "Алишер ака, бугун кунингиз қандай ўтди? 🌙")
        check("a second tap is thanked, not recorded", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())),
              "cheer_already_answered")

        message = {"text": "Бугун зўр ўтди!", "reply_to_message": {"message_id": 501}}
        outcome = asyncio.run(ops_manager._handle_employee_message(worker, message, uuid.uuid4()))
        check("a typed reply to it is chat — not a report, not for the Director", outcome, "cheer_reply")
        check_true("...and gets a friendly line back", replies and "раҳмат" in replies[0] and friendly_problems(replies[0]) == [])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
    check_true("no Latin in the pop-ups", not re.search(r"[A-Za-z]{3,}", "".join(toasts)))


def test_lead_handout() -> None:
    """The lead hand-out to B2B sales was stopped by the Director (2026-10-07): nothing goes to them.

    What stays: the leads sheet read by header (the Lead Agent, the Director's
    questions) and the hand-out's history in the KPI and the Director's answers.
    """
    print("lead hand-out removed (2026-10-07)")
    import asyncio
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import ai_chat, kpi_score, leads, ops_manager

    root = Path(__file__).resolve().parents[1]
    # ---- nothing left that sends to B2B Sotuv
    check_true("the hand-out agent is gone", not (root / "agents" / "lead-handout").exists())
    check_true("no job runs it", "lead-handout" not in load_source("scripts/run_morning_agents.py"))
    check_true("its switch is gone", not hasattr(settings, "lead_handout_enabled"))
    check_true("no hand-out logic left", not any(hasattr(leads, n) for n in (
        "plan_handout", "checkin_due", "card_text", "checkin_keyboard", "parse_brief", "SALES_ROLE")))
    blueprint = load_source("render.yaml")
    schedule = re.search(r"name: mgmg-team-cheer\n(?:.*\n)*?\s*schedule: \"([^\"]+)\"", blueprint).group(1)
    check("the daytime job no longer runs at 15:00", schedule.split()[1], "5,12")
    check_true("the work AI no longer gets 'own leads'",
               "own_leads" not in inspect_source(ai_chat.work_context) and "own_leads" not in inspect_source(ops_manager))

    # ---- the sheet, still read by header (the Lead Agent and the Director's questions)
    check("the sheet's columns are the Lead Agent's own", leads.SHEET_COLUMNS, load_agent("lead-agent").SHEET_COLUMNS)
    check_true("...and the bot's", ops_manager.LEAD_SHEET_COLUMNS is leads.SHEET_COLUMNS)

    # ---- an old 15:00 button: nothing recorded, the buttons come off
    edits, toasts = [], []

    class Bot:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append((message_id, text, reply_markup))

    async def fake_answer(query_id, text):
        toasts.append(text)

    saved = [(ops_manager, "TelegramBot", ops_manager.TelegramBot), (ops_manager, "_answer", ops_manager._answer)]
    ops_manager.TelegramBot, ops_manager._answer = Bot, fake_answer
    try:
        tap = {"id": "q", "from": {"id": 7}, "data": f"ld:{uuid.uuid4()}:p",
               "message": {"message_id": 801, "chat": {"id": 7}, "text": "Hyatt — қандай кетяпти?"}}
        check("an old lead button", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "lead_handout_stopped")
        check_true("...says it's stopped and takes the buttons off",
                   toasts == ["лидлар тарқатиш тўхтатилган"] and edits == [(801, "Hyatt — қандай кетяпти?", {"inline_keyboard": []})])
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)

    # ---- history: the Director's answer says it stopped; the KPI still counts what happened
    check_true("the Director's data says the hand-out stopped", "stopped by the Director on 2026-10-07" in leads.describe([]))
    emp = [{"id": "e1", "full_name": "Алишер", "role": "b2b_sotuv"}]
    lead_rows = [{"employee_id": "e1", "asked": 4, "answered": 3, "closed": 2, "done": 1}]
    month = kpi_score.build(emp, [], [], {}, [], [], date(2026, 10, 1), date(2026, 10, 31), lead_rows=lead_rows)[0]
    check("October's past lead answers still count in Жараён", round(month.parts["process"]), 75)


def inspect_source(obj) -> str:
    import inspect

    return inspect.getsource(obj)


def test_politeness_days_off_announcements() -> None:
    """2026-10-01: names keep capitals, "сиз" always, days off, announcements."""
    print("politeness, days off, announcements")
    import asyncio
    import json
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import admin, cheer, kpi, ops_manager, store, tone

    # ---- names keep their capitals; the sentence stays lowercase
    check("a name keeps its capitals", tone.casual("Алишер ака, БУГУН зўр кун", "☀️", keep=["Алишер ака"]),
          "Алишер ака, бугун зўр кун ☀️")

    # ---- "сиз", never "сен"
    for text, polite in (("бугун зўр ишладингиз", True), ("илтимос ёзиб юборсангиз", True), ("нима қилдингиз?", True),
                         ("менгами ишонасан", False), ("сенга раҳмат", False), ("нима қилдинг?", False),
                         ("қилсанг бўлади", False), ("бир минг сўм", True), ("Ҳасан ака", True)):
        check(f"polite: {text!r}", tone.is_polite(text), polite)
    every_fixed_line = [
        *[t for t, _e in cheer._MORNING],
        *[c.text for c in cheer._EVENING], *[o[k] for c in cheer._EVENING for o in c.options for k in ("label", "reply")],
        *[t for t, _e in cheer._TEXT_REPLIES], kpi.build_request_text("Алишер", ()), kpi.build_reminder_text("Алишер", ()),
        admin.TECH_ERROR_TEXT,
    ]
    check("every fixed line the bot sends is polite", [t for t in every_fixed_line if not tone.is_polite(t)], [])
    rude = json.dumps({"text": "бугун нима қилдинг?", "emoji": "🌙",
                       "options": [{"label": "зўр", "reply": "раҳмат"}, {"label": "ёмон", "reply": "раҳмат"}]})
    check("an AI line with 'сен' forms is thrown away", cheer.parse_ai(rude, "evening"), None)

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
    cheer_agent, reports_agent = load_agent("team-cheer"), load_agent("daily-reports")
    patch(cheer_agent, "send_slot", must_not_send)
    patch(reports_agent, "ask_everyone", must_not_send)
    patch(reports_agent, "remind_silent", must_not_send)
    try:
        for name, coro in (("cheer", cheer_agent.run("evening", dry_run=True)),
                           ("report ask", reports_agent.run("ask", dry_run=True)),
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

    async def fake_reply(chat_id, run_id, text, reply_markup=None):
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
                       ("cheer_delivery_for_message", none),
                       ("report_being_edited", none),
                       ("pending_report", pending_report),
                       ("open_report_followup", none), ("submitted_report_today", none), ("find_task_by_message_id", none),
                       ("create_pending_relay", create_held), ("resolve_pending_relay", resolve_held),
                       ("get_employee_by_telegram_id", get_by_tg)):
        patch(store, name, fake)
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

        # during the report window: a plain message is the report, a question gets one tap
        pending["row"] = {"id": "r1", "prompt_message_id": 50, "reminder_message_id": None}
        outcome = asyncio.run(ops_manager._handle_employee_message(seller, {"text": "КП қандай ёзилади?"}, uuid.uuid4(), bg))
        buttons = [b["callback_data"].split(":")[0] for row in replies[-1][1]["inline_keyboard"] for b in row]
        check("a question while the report is due: report or question?", (outcome, buttons), ("report_or_ai_asked", ["asrep", "asai"]))
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
    """2026-10-02: files go to the Director when asked; today's report confirmed, changed, deleted; cheer off per person."""
    print("files, today's report, cheer off")
    import asyncio
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import admin, ops_manager, report_tools, store

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

    async def fake_reply(chat_id, run_id, text, reply_markup=None):
        replies.append((text, reply_markup))
        return [1]

    async def create_file(user, message_id, kind, caption):
        state["file"] = {"id": "f1", "message_id": message_id, "caption": caption, "resolved": False}
        return state["file"]

    async def resolve_file(file_id, user, purpose):
        f = state["file"]
        if not f or f["resolved"] or user != 7:
            return None
        f["resolved"] = True
        return f

    async def report_for_day(user, day):
        return state["report"]

    async def attach(user, day, caption):
        state["attached"] = caption
        return state["report"]

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
                       ("report_being_edited", being_edited), ("replace_report", replace), ("delete_report", delete)):
        patch(store, name, fake)
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

        asyncio.run(ops_manager._handle_employee_message(worker, {**photo, "message_id": 43}, uuid.uuid4()))
        check("'бугунги ҳисобот' adds it to today's report and forwards it", tap("fp:f1:r"), "file_report")
        check_true("...marked as the report", state["attached"] == "омбор" and "бугунги ҳисоботи" in copies[-1][1]["caption"])
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

    # ---- cheer off for one person
    agent = load_agent("team-cheer")
    sent = []

    class CheerBot(FakeBot):
        async def send_message(self, text, chat_id=None, **kwargs):
            sent.append(chat_id)
            return [1]

    async def everyone():
        return [{**worker, "cheer_off": True}, {**worker, "telegram_user_id": 8, "cheer_off": False}]

    async def claim(day, slot):
        return {"id": "c1"}

    saved.clear()
    patch(store, "list_active_employees", everyone)
    patch(store, "claim_cheer", claim)
    patch(store, "set_cheer_content", none)
    patch(store, "save_cheer_delivery", none)
    patch(agent, "TelegramBot", CheerBot)
    patch(agent, "now_local", lambda: datetime(2026, 10, 2, 10, 0, tzinfo=TASHKENT))

    async def fixed(slot, day, run_id):
        from integrations.org_bot import cheer

        return cheer.fallback(slot, day)

    patch(agent, "write", fixed)
    try:
        asyncio.run(agent.send_slot("morning", uuid.uuid4()))
        check("someone with the cheer switched off gets nothing", sent, ["8"])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    edits.clear()

    async def toggle(employee_id, by):
        return {**worker, "id": employee_id, "cheer_off": True}

    async def get_employee(employee_id):
        return {"id": employee_id, "status": "active", "role": "b2b_sotuv", "display_name": "a"}

    async def admin_edit(callback, text, keyboard, run_id):
        edits.append((text, keyboard))

    saved.clear()
    patch(store, "toggle_cheer", toggle)
    patch(store, "get_employee", get_employee)
    patch(admin, "_edit", admin_edit)
    patch(admin, "_answer", none)
    try:
        asyncio.run(admin.handle_admin_callback({"id": "q", "data": "chof:e7", "from": {"id": 5}}, uuid.uuid4()))
        check_true("the 💬 button switches the cheer off, and the card shows it",
                   edits and "Кайфият хабарлари (10:00, 17:35): ўчирилган" in edits[-1][0] and "💬 Кайфият хабарлари: ⛔" in str(edits[-1][1]))
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
        check_true("...grouped by customer", "- Hilton: 100" in text and "- Hyatt: 30" in text)
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

    async def reply(director_id, run_id, text):
        replies.append(text)

    async def from_agent(director_id, slug, question, run_id, history=""):
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
    check_true("the technical report is Uzbek Cyrillic",
               latin_words(text, allow={"SAP", "Billz", "Verifix", "AI", "OpenRouter", "Didox", "Admin", "Bot", "OPS",
                                        "Manager", "MB", "Render", "Recovery", "HTTP", "SerpAPI", "mgmg",
                                        "db", "C"}) == [])
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


def test_ap_reconcile() -> None:
    """2026-10-09: supplier debt, 1C against SAP B1 — read-only, matched by ИНН then name."""
    print("supplier debt 1C vs SAP (scripts/ap_reconcile.py)")
    import importlib.util
    import inspect
    import tempfile

    spec = importlib.util.spec_from_file_location("ap_reconcile", Path(__file__).resolve().parent / "ap_reconcile.py")
    ar = importlib.util.module_from_spec(spec)
    sys.modules["ap_reconcile"] = ar  # dataclasses look their module up there
    spec.loader.exec_module(ar)

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
    source = inspect.getsource(ar)
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
               "ADD COLUMN IF NOT EXISTS reports_off" in schema and "'cheer', 'reports')" in schema)

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

    async def fake_reply(tg_id, run_id, text, reply_markup=None):
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
    check("one button per person, by name, marked ✅",
          [b["text"] for b in buttons], ["⬜ Галимов Рушан", "✅ Исоқов Улуғбек", "⬜ Умаров Шухрат"])
    check_true("button data fits Telegram's 64 bytes", all(len(b["callback_data"].encode()) <= 64 for b in buttons))
    check_true("the screen says what the mark does, in Uzbek Cyrillic",
               "1 / 3" in view and "кечикди" in view and latin_words(view) == [])
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

    saved = [(store, "toggle_flexible_schedule", store.toggle_flexible_schedule),
             (store, "flexible_schedule_ids", store.flexible_schedule_ids),
             (admin, "_edit", admin._edit), (admin, "_answer", admin._answer), (admin, "log_action", admin.log_action),
             (settings, "admin_bot_admin_user_id", settings.admin_bot_admin_user_id)]
    store.toggle_flexible_schedule, store.flexible_schedule_ids = toggle, current
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
    from datetime import datetime

    from integrations.billz import sap_check as sc
    from integrations.billz.sales import DaySales, ShopDay
    from integrations.common.agent_loader import load_agent
    from integrations.onec.cash import CashPosition
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
    data = brief.BriefData(
        cash=CashPosition(at=datetime(2026, 10, 5, 8), accounts=[("5110", "Банк", 4_495_000_000), ("5010", "Касса", 76_720_000)]),
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
    check("the five numbers, always in this order", list(by_label),
          ["Касса", "Кечаги сотув", "Захира", "Мижоз қарзи", "Бугунги тўловлар"])
    check("cash: the number large, its unit apart", (by_label["Касса"]["number"], by_label["Касса"]["unit"]),
          ("4,57", "млрд сўм"))
    check_true("nothing sold / no payments: quiet, not shouted",
               by_label["Кечаги сотув"]["tone"] == "quiet" and by_label["Бугунги тўловлар"]["tone"] == "quiet")
    check_true("unreadable stock says so instead of a number",
               by_label["Захира"]["tone"] == "muted" and "ўқилмади" in by_label["Захира"]["number"])
    check_true("overdue debt is the one red figure, with how old",
               by_label["Мижоз қарзи"]["tone"] == "alert" and "90 кундан" in by_label["Мижоз қарзи"]["sub"])
    blocks = {b["title"]: b for b in v["blocks"]}
    check("names: five, then '+N'", (len(blocks["Ҳисобот юбормаганлар"]["rows"]), blocks["Ҳисобот юбормаганлар"]["more"]), (5, 2))
    check_true("one shop: its name beside the day, no repeated row",
               "GARMIN ABAY" in blocks["Дўконлар"]["day"] and not blocks["Дўконлар"]["rows"])
    check_true("attendance counts the flexible apart", "эркин графикда 5" in blocks["Давомат"]["summary"])
    brief_html = render.render_html("brief.html", image=True, page_width=540, page_height=6000, sentinel="#ff00ff", **v)
    check_true("the brief reads in Uzbek Cyrillic",
               latin_words(text_of(brief_html), allow={"Billz", "GARMIN", "ABAY", "OPS", "Manager", "Verifix", "C"}) == [])
    check_true("no emoji in the picture (drawn icons only)", "🔴" not in brief_html and "📈" not in brief_html)
    check_true("the caption carries the key numbers", "Касса: 4,57 млрд" in brief.caption(data))

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
    import importlib.util
    import sys as _sys

    ar = _sys.modules.get("ap_reconcile")
    if ar is None:
        spec = importlib.util.spec_from_file_location("ap_reconcile", root / "scripts" / "ap_reconcile.py")
        ar = importlib.util.module_from_spec(spec)
        _sys.modules["ap_reconcile"] = ar  # dataclasses look their module up here
        spec.loader.exec_module(ar)
    pushed = [{"CardCode": "S001", "CardName": "Primus LLC", "LicTradNum": "301234567", "CardType": "S",
               "Currency": "UZS", "Balance": "-800.00", "BalanceSys": "-9400000.00", "BalanceFC": "-9400000.00"},
              {"CardCode": "S002", "CardName": "Tanita", "LicTradNum": "", "CardType": "S", "Currency": "USD",
               "Balance": "-120.00", "BalanceSys": "-1410000.00", "BalanceFC": "-120.00"}]
    parties, info = ar.sap_parties(pushed, -1.0)
    check("the gateway's suppliers read with their columns", (parties["S001"].owed, parties["S001"].inn), (9400000.0, "301234567"))
    # SAP's sign is chosen by agreement with 1C, not by guessing (09.10.2026: an advance read as a debt)
    onec = {"X": ar.OneCParty(key="X", name="Tovar-xomashyo birjasi", inn="200933985", advance=32419520.78)}
    rows = [{"CardCode": "S9", "CardName": "Tovar-xomashyo birjasi", "LicTradNum": "200933985", "CardType": "S",
             "Balance": "-2634.82", "BalanceSys": "-32419520.78"}]
    sign, how = ar.choose_sign(onec, rows)
    sap_side, _ = ar.sap_parties(rows, sign)
    pair = [m for m in ar.match(onec, sap_side) if m.onec and m.sap][0]
    check("an advance equal in both systems: no difference", (sign, round(pair.diff, 2), how["matched"]), (1.0, 0.0, 1))


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
               and set(script_cols["get_stock_value"]) == set(push_handler.EXPECTED_COLUMNS["stock_value"]))
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
               "(2 та ҳисоб-фактура)" in five and "камида" not in five.split("Захира")[0])


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
    text = brief.render(brief.BriefData(report_rows=[], cash=pos))
    check_true("the brief shows bank and cash", "💰 Касса: 3,6 млрд сўм (банк 3,52 млрд сўм, нақд 76,72 млн сўм)" in text
               or ("💰 Касса:" in text and "банк" in text and "нақд" in text))
    check_true("not set up: «уланмаган»", "💰 Касса: <i>уланмаган</i>" in brief.render(brief.BriefData(report_rows=[])))
    check_true("1C down: «маълумот йўқ»", "💰 Касса: <i>маълумот йўқ</i>" in brief.render(brief.BriefData(report_rows=[], cash_failed=True)))
    saved = brief.five_numbers_json(brief.BriefData(cash=pos))["cash"]
    check("today's cash is kept for tomorrow's change", saved, {"status": "ok", "totals": {"UZS": round(pos.total * 100)}})
    tomorrow = brief.render(brief.BriefData(report_rows=[], cash=pos, previous={"cash": {"status": "ok", "totals": {"UZS": 0}}}))
    check_true("…and shown as a change", "кечагига" in tomorrow)
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
        test_team_cheer,
        test_lead_handout,
        test_politeness_days_off_announcements,
        test_ai_chat_and_sheet,
        test_files_reports_cheer_off,
        test_reports_off,
        test_analyst,
        test_it_restrictions,
        test_tech_report_errors_fixed,
        test_review_pages,
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
