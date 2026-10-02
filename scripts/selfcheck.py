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


def test_report_or_message() -> None:
    """A report never needs Telegram's reply; an unclear message gets one tap."""
    print("report or message")
    from integrations.org_bot.ops_manager import report_message_kind, report_or_relay_keyboard

    check("plain message, no task -> report", report_message_kind(False, False, 0), "report")
    check("reply to the ask/reminder -> report, even with tasks", report_message_kind(True, False, 3), "report")
    check("reply to a task card -> task update", report_message_kind(False, True, 1), "task_update")
    check("plain message with a task in flight -> ask", report_message_kind(False, False, 1), "ask")
    check("plain message with several tasks -> ask", report_message_kind(False, False, 4), "ask")
    real_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    buttons = [b for row in report_or_relay_keyboard(real_id)["inline_keyboard"] for b in row]
    check("three choices", [b["callback_data"].split(":")[0] for b in buttons], ["asrep", "relayok", "relayno"])
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
        "relations": [{"name": "employees", "kind": "r"}, {"name": "v_ar_aging_latest", "kind": "v"}],
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
        check_true("lists the tables", "employees" in index.text and "1 қатор" in index.text)
        check_true("never indexed or cached", index.headers.get("x-robots-tag", "").startswith("noindex")
                   and index.headers.get("cache-control") == "no-store")

        page = client.get("/db/employees", headers=good)
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
    sub, err = feedback.clean({"place": "garmin", "message": "  Навбат  узун  ", "phone": "90 123-45-67"})
    check("message tidied, phone normalised", (sub.message, sub.phone, sub.kind, sub.place),
          ("Навбат узун", "901234567", "complaint", "garmin"))
    check("the business must be chosen", feedback.clean({"message": "Навбат узун"})[1], "place")
    check("an unknown business is refused", feedback.clean({"place": "cafe", "message": "Навбат узун"})[1], "place")
    check("empty message refused", feedback.clean({"place": "laundry", "message": " "})[1], "empty")
    check_true("a bad phone is refused, not kept",
               feedback.clean({"place": "laundry", "message": "ёмон", "phone": "abc"})[0] is None)
    check("complaints only: an old 'feedback' kind is ignored",
          feedback.clean({"place": "laundry", "message": "ёмон", "kind": "feedback"})[0].kind, "complaint")
    anon = feedback.clean({"place": "laundry", "message": "Кир ювиш машинаси ишламаяпти"})[0]
    anon_text = feedback.director_text(anon)
    check_true("no name, no phone = anonymous", anon.anonymous and "👤 Аноним" in anon_text)
    check_true("🔴 and the business head the Director's message",
               anon_text.startswith("🔴 <b>Мижоз шикояти — Londry</b>"))
    check_true("no opinion wording left", "фикр" not in anon_text.lower())
    check_true("the Director's message is Uzbek Cyrillic", latin_words(anon_text) == [])
    risky = feedback.clean({"place": "garmin", "message": "<b>x</b> & y", "name": "<i>"})[0]
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
               all(feedback_page.error_text(k, lang) for k in ("empty", "too_long", "phone", "rate", "failed")
                   for lang in feedback.LANGS))

    def outside_nav(page_text):
        return re.sub(r"<nav>.*?</nav>", "", page_text.split("<body>")[-1])

    en_page = outside_nav(feedback_page.form_html(lang="en") + feedback_page.thanks_html(True, "en"))
    check_true("en: no Cyrillic left untranslated", not re.search(r"[А-яЁёЎўҚқҒғҲҳ]", en_page))
    ru_page = outside_nav(feedback_page.form_html(lang="ru") + feedback_page.thanks_html(True, "ru"))
    check_true("ru: no Latin words left untranslated", latin_words(ru_page) == [])
    ru_sub = feedback.clean({"place": "garmin", "message": "Всё плохо", "lang": "ru"})[0]
    check_true("the Director is told the client's language, in Cyrillic",
               "🌐 Мижоз тили: русча" in feedback.director_text(ru_sub))
    check_true("no language line for the default language", "🌐" not in anon_text)
    check("an unknown language falls back to the default",
          feedback.clean({"place": "garmin", "message": "Ёмон", "lang": "zz"})[0].lang, "uz_cyrl")
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
        uz = client.get("/f", headers={"Accept-Language": "uz-Latn-UZ"})
        check_true("an Uzbek phone gets Cyrillic", "Шикоятни юбориш" in uz.text)
        for other in ("/f/laundry", "/f/cafe"):
            moved = client.get(other, follow_redirects=False)
            check_true(f"{other} goes to Londry's page", moved.status_code == 301 and moved.headers["location"] == "/f")

        ok = client.post("/f", data={"message": "Машина ишламаяпти", "phone": "+998901234567"})
        check_true("a complaint is thanked", ok.status_code == 200 and "Раҳмат" in ok.text)
        ok_garmin = client.post("/f/garmin", data={"message": "Соат синди", "place": "laundry"})
        check_true("...on Garmin's page too", ok_garmin.status_code == 200 and "Раҳмат" in ok_garmin.text)
        check("each goes to the Director with its page's business (a posted place is ignored)",
              [s.place for s in submitted], ["laundry", "garmin"])

        bad = client.post("/f/garmin", data={"message": "Ёмон", "phone": "12"})
        check_true("an error keeps what was typed, beside the phone field, on the same page",
                   bad.status_code == 400 and "Ёмон" in bad.text and "action='/f/garmin'" in bad.text
                   and re.search(r"id='p'[^>]*aria-invalid='true'.*нотўғри", bad.text) is not None)
        bad_ru = client.post("/f", data={"message": "Плохо", "phone": "12", "lang": "ru"})
        check_true("the error is in the client's language", bad_ru.status_code == 400
                   and "Неверный номер" in bad_ru.text and "lang='ru'" in bad_ru.text)
        en_ok = client.post("/f/garmin", data={"message": "Broken watch", "lang": "en"})
        check_true("thanks in English, back to the same page", "Thank you!" in en_ok.text and "/f/garmin?lang=en" in en_ok.text)
        check("...and the language reaches the Director", submitted[-1].lang, "en")
        feedback_page._recent.clear()

        submitted.clear()
        bot = client.post("/f", data={"message": "spam", "website": "http://x"})
        check_true("the hidden field drops bots quietly", bot.status_code == 200 and not submitted)

        for _ in range(5):  # counter cleared above, so five allowed, the sixth waits
            client.post("/f", data={"message": "Ёмон"})
        limited = client.post("/f", data={"message": "Ёмон"})
        check("the 6th message in 10 minutes waits", limited.status_code, 429)

        async def broken_submit(submission):
            raise RuntimeError("db down")

        feedback.submit = broken_submit
        feedback_page._recent.clear()
        down = client.post("/f", data={"message": "Ёмон хизмат"})
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
    """Leads to B2B sales: the sheet, who gets what, the card, 15:00, taps, typed answers, KPI."""
    print("lead hand-out (08:00 / 15:00)")
    import asyncio
    import json
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import kpi_score, leads, ops_manager, store

    # ---- the sheet
    check("the sheet's columns are the Lead Agent's own", leads.SHEET_COLUMNS, load_agent("lead-agent").SHEET_COLUMNS)
    check_true("...and the bot's", ops_manager.LEAD_SHEET_COLUMNS is leads.SHEET_COLUMNS)
    header = list(leads.SHEET_COLUMNS)

    def sheet_row(**cells):
        return [cells.get(c, "") for c in leads.SHEET_COLUMNS]

    rows = [header,
            sheet_row(company_name="Hyatt Regency", project_stage="under construction", priority="High",
                      confidence="0,8", date_added="2026-09-30", dedupe_key="hyatt|equipment_sales"),
            sheet_row(project_name="City Hospital", track="service_maintenance", date_added="2026-09-01"),
            sheet_row(signal="nameless row")]
    parsed = leads.rows_to_leads(rows)
    check("unnamed rows are skipped", len(parsed), 2)
    check("priority, confidence, date read", (parsed[0]["priority"], parsed[0]["confidence"], parsed[0]["date_added"]),
          ("high", 0.8, date(2026, 9, 30)))
    check("a missing dedupe key is made the sheet's way", parsed[1]["dedupe_key"], "city hospital|service_maintenance")

    # ---- who gets what
    today = date(2026, 10, 1)
    pool = [
        {"id": 1, "company_name": "Old low", "priority": "low", "confidence": 0.9, "date_added": date(2026, 9, 1)},
        {"id": 2, "company_name": "Old high", "priority": "high", "confidence": 0.5, "date_added": date(2026, 9, 2)},
        {"id": 3, "company_name": "New medium", "priority": "medium", "confidence": 0.4, "date_added": today},
    ]
    people = [{"id": "e2", "full_name": "Бобур"}, {"id": "e1", "full_name": "Алишер"}]
    plan = leads.plan_handout(people, pool, today)
    check("today's new lead first, then the best older one", [(p["full_name"], lead["id"]) for p, lead in plan],
          [("Алишер", 3), ("Бобур", 2)])
    check("fewer leads than people: the rest get none", len(leads.plan_handout(people, pool[:1], today)), 1)
    for hm, due in (("14:59", False), ("15:00", True), ("15:24", True), ("15:35", False)):
        h, m = map(int, hm.split(":"))
        check(f"15:00 check-in due at {hm}", leads.checkin_due(datetime(2026, 10, 1, h, m)), due)

    # ---- the card
    good = json.dumps({"brief": "Hyatt Regency Тошкентда янги меҳмонхона қурмоқда. Кир ювиш ускуналарини таклиф қилиш мумкин.",
                       "location": "Тошкент"})
    check_true("a Cyrillic summary is used", leads.parse_brief(good) is not None)
    check("an English one is not", leads.parse_brief(json.dumps({"brief": "A new hotel is being built in Tashkent."})), None)
    check("nor a very long one", leads.parse_brief(json.dumps({"brief": "меҳмонхона " * 40})), None)
    lead = {"company_name": "Hyatt <Regency>", "location": "Tashkent", "project_stage": "Under construction",
            "priority": "high", "contact_method": "+998 71 200 00 00", "signal_source_url": "https://uzex.uz/lot/1"}
    card = leads.card_text(lead, leads.fallback_brief(lead))
    check_true("the card: name escaped, stage and priority in Uzbek, the source linked",
               "Hyatt &lt;Regency&gt;" in card and "қурилмоқда" in card and "муҳимлиги юқори" in card
               and 'href="https://uzex.uz/lot/1"' in card and "15:00" in card)
    check_true("...Latin place names written in Cyrillic", "Ташкент" in card)
    import html as html_lib

    check_true("...and nothing else in Latin",
               latin_words(html_lib.unescape(re.sub(r"<[^>]+>", " ", card)), allow={"Hyatt", "Regency"}) == [])
    check("...and stage and priority said once", card.count("қурилмоқда"), 1)

    # ---- the 15:00 line and its buttons
    line = leads.checkin_text("Алишер ака", lead, 3)
    check("one friendly line, counting the days", (friendly_problems(line, ("Алишер ака", "Hyatt &lt;Regency&gt;")),
                                                   "3-кун" in line), ([], True))
    kb = leads.checkin_keyboard(str(uuid.uuid4()))
    choice_kb = leads.choice_keyboard(str(uuid.uuid4()), "dismissed")
    datas = [b["callback_data"] for k in (kb, choice_kb) for row in k["inline_keyboard"] for b in row]
    check("three answers: in progress, dismissed, done", [b["text"] for b in kb["inline_keyboard"][0]],
          ["жараёнда", "рад этилди", "бажарилди"])
    check_true("every button fits Telegram's 64 bytes", all(len(d.encode()) <= 64 for d in datas))
    check("a reason button reads back", leads.parse_choice(choice_kb["inline_keyboard"][1][0]["callback_data"].split(":")[-1]),
          ("dismissed", 2, "бошқадан олишган"))
    check("a forged one doesn't", (leads.parse_choice("p0"), leads.parse_choice("x9")), (None, None))

    # ---- KPI, inside the existing parts
    emp = [{"id": "e1", "full_name": "Алишер", "role": "b2b_sotuv"}]
    lead_rows = [{"employee_id": "e1", "asked": 4, "answered": 3, "closed": 2, "done": 1}]
    goals = [{"employee_id": "e1", "title": "10 та лидни бажариш", "target": 10, "actual": None}]
    month = kpi_score.build(emp, [], [], {}, goals, [], date(2026, 10, 1), date(2026, 10, 31), lead_rows=lead_rows)[0]
    check("15:00 answers count in Жараён", round(month.parts["process"]), 75)
    check("closed leads count in Иш ҳажми", month.volume, 2)
    check("a lead goal is filled from the leads done", month.goals[0].actual, 1.0)
    check("the card says so", "1/" not in kpi_score._detail("process", month) and "3/4 лид" in kpi_score._detail("process", month), True)

    # ---- the agent, with a fake database, AI and Telegram
    agent = load_agent("lead-handout")
    sent, assigned, checkins, briefs = [], [], [], []
    free = [dict(pool[1]), dict(pool[2])]
    sales = [{"id": "e1", "telegram_user_id": 11, "full_name": "Алишер Каримов", "display_name": "a", "address_form": "aka",
              "role": "b2b_sotuv"},
             {"id": "e2", "telegram_user_id": 12, "full_name": "Бобур", "display_name": "b", "role": "b2b_sotuv",
              "works_saturday": False}]

    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, text, chat_id=None, reply_markup=None, **kwargs):
            sent.append((chat_id, text, reply_markup))
            return [700 + len(sent)]

    class FakeAI(FakeBot):
        async def complete(self, system, user, **kwargs):
            return good

    class NoSheet(FakeBot):
        async def __aenter__(self):
            from integrations.google.sheets_client import SheetsError

            raise SheetsError("offline")

    async def by_role(role):
        return sales if role == "b2b_sotuv" else []

    async def free_leads():
        return free

    async def assign(lead_id, employee_id, day):
        if any(a[1] == employee_id for a in assigned):
            return None
        assigned.append((lead_id, employee_id, day))
        return {"id": f"a{lead_id}"}

    async def to_ask(day):
        return [{**sales[0], "assignment_id": "a3", "assigned_on": day - timedelta(days=2), "company_name": "Hyatt"}]

    async def open_checkin(assignment_id, day, user):
        if checkins:
            return None
        checkins.append(assignment_id)
        return {"id": str(uuid.uuid4())}

    async def nothing(*args, **kwargs):
        briefs.append(args)
        return None

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    for name, fake in (("active_employees_by_role", by_role), ("free_leads", free_leads), ("create_lead_assignment", assign),
                       ("open_leads_to_ask", to_ask), ("create_lead_checkin", open_checkin), ("set_lead_brief", nothing),
                       ("set_lead_assignment_message", nothing), ("set_lead_checkin_message", nothing)):
        patch(store, name, fake)
    patch(agent, "TelegramBot", FakeBot)
    patch(agent, "OpenRouterClient", FakeAI)
    patch(agent, "SheetsClient", NoSheet)
    patch(agent, "now_local", lambda: datetime(2026, 10, 1, 8, 0, tzinfo=TASHKENT))
    try:
        asyncio.run(agent.morning(uuid.uuid4()))
        check("each sales person gets one lead, the best first", [(a[0], a[1]) for a in assigned], [(3, "e1"), (2, "e2")])
        check_true("the card carries the AI's Uzbek summary", "Кир ювиш ускуналарини" in sent[0][1])
        check_true("...and it's kept, so it's written once", any(args and args[0] == 3 for args in briefs))
        sent.clear()
        asyncio.run(agent.morning(uuid.uuid4()))
        check("a second morning run gives nobody a second lead", sent, [])

        patch(agent, "now_local", lambda: datetime(2026, 10, 1, 15, 0, tzinfo=TASHKENT))
        sent.clear()
        asyncio.run(agent.checkin(uuid.uuid4()))
        check("15:00: one line per open lead, with the three buttons",
              (sent[0][1], len(sent[0][2]["inline_keyboard"][0])), ("Алишер ака, «Hyatt» лиди қандай кетяпти, 3-кун? 🙂", 3))
        sent.clear()
        asyncio.run(agent.checkin(uuid.uuid4()))
        check("asked once a day", sent, [])
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    # ---- taps and the typed answer in OPS Manager Bot
    edits, replies, toasts, notes, questions = [], [], [], [], []
    checkin_id = str(uuid.uuid4())
    state = {"answered": False, "outcome": False, "report_asked_at": None}

    class EditBot(FakeBot):
        async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
            edits.append((text, reply_markup))

    async def answer_checkin(cid, user, status):
        if state["answered"]:
            return None
        state["answered"] = True
        return {"id": cid, "message_id": 801, "company_name": "Hyatt", "project_name": None, "status": status}

    async def choose(cid, user, outcome):
        if state["outcome"]:
            return None
        state["outcome"] = True
        return {"id": cid, "message_id": 801, "company_name": "Hyatt", "project_name": None, "status": "dismissed"}

    async def fake_reply(chat_id, run_id, text, reply_markup=None):
        replies.append(text)
        return [900]

    async def fake_answer(query_id, text):
        toasts.append(text)

    async def ask_q(cid, question, message_id):
        questions.append((cid, question, message_id))

    now = datetime(2026, 10, 1, 15, 5, tzinfo=TASHKENT)

    async def pending_q(user, minutes):
        return {"id": checkin_id, "question_asked_at": now} if questions else None

    async def q_by_message(user, message_id):
        return {"id": checkin_id} if message_id == 900 else None

    async def save_note(cid, note):
        notes.append(note)
        return {"id": cid}

    async def pending_report(user, day):
        return {"asked_at": state["report_asked_at"]} if state["report_asked_at"] else None

    async def no_report(*args, **kwargs):
        return "daily_report"

    async def no_cheer(*args):
        return None

    saved.clear()
    for name, fake in (("answer_lead_checkin", answer_checkin), ("choose_lead_outcome", choose), ("ask_lead_question", ask_q),
                       ("pending_lead_question", pending_q), ("lead_question_by_message", q_by_message),
                       ("save_lead_note", save_note), ("pending_report", pending_report),
                       ("cheer_delivery_for_message", no_cheer)):
        patch(store, name, fake)
    patch(ops_manager, "TelegramBot", EditBot)
    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "_answer", fake_answer)
    patch(ops_manager, "_try_daily_report", no_report)
    worker = {"telegram_user_id": 11, "role": "b2b_sotuv", "full_name": "Алишер"}
    try:
        tap = {"id": "q", "data": f"ld:{checkin_id}:p", "from": {"id": 11}}
        check("жараёнда is taken", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "lead_in_progress")
        check_true("...the buttons go, and the next step is asked",
                   edits[-1][1] == {"inline_keyboard": []} and questions[-1][1] == leads.IN_PROGRESS_QUESTION
                   and replies[-1] == "кейинги қадамингиз нима ва қачон? 🙂")
        check("a second tap is not recorded", asyncio.run(ops_manager._handle_callback(tap, uuid.uuid4())), "lead_already_answered")

        typed = {"text": "эртага учрашув белгиланди"}
        check("the next message is the answer", asyncio.run(ops_manager._handle_employee_message(worker, typed, uuid.uuid4())), "lead_note")
        check_true("...kept, and thanked in one friendly line", notes == ["эртага учрашув белгиланди"] and friendly_problems(replies[-1]) == [])
        state["report_asked_at"] = now + timedelta(minutes=55)
        check("after the 16:00 report ask, a plain message is the report",
              asyncio.run(ops_manager._handle_employee_message(worker, typed, uuid.uuid4())), "daily_report")
        replied = {"text": "шартнома тайёрланмоқда", "reply_to_message": {"message_id": 900}}
        check("...but a Reply to the lead question is still the lead's",
              asyncio.run(ops_manager._handle_employee_message(worker, replied, uuid.uuid4())), "lead_note")

        state["answered"] = False
        tap_x = {"id": "q", "data": f"ld:{checkin_id}:x", "from": {"id": 11}}
        check("рад этилди is taken", asyncio.run(ops_manager._handle_callback(tap_x, uuid.uuid4())), "lead_dismissed")
        check_true("...and the reasons are offered", len(edits[-1][1]["inline_keyboard"]) == 2 and "нима сабабдан?" in edits[-1][0])
        questions.clear()
        pick = {"id": "q", "data": f"lc:{checkin_id}:x3", "from": {"id": 11}}
        check("a reason is taken", asyncio.run(ops_manager._handle_callback(pick, uuid.uuid4())), "lead_outcome")
        check_true("...'other' asks them to write it", edits[-1][0].endswith("рад этилди, бошқа сабаб ✅")
                   and questions and questions[-1][1] == leads.OTHER_QUESTION)
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def test_politeness_days_off_announcements() -> None:
    """2026-10-01: names keep capitals, "сиз" always, days off, announcements."""
    print("politeness, days off, announcements")
    import asyncio
    import json
    import uuid

    from integrations.common.agent_loader import load_agent
    from integrations.common.config import settings
    from integrations.org_bot import admin, cheer, kpi, leads, ops_manager, store, tone

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
        leads.IN_PROGRESS_QUESTION, leads.OTHER_QUESTION, *leads.DISMISS_REASONS, *leads.DONE_RESULTS,
        admin.TECH_ERROR_TEXT,
    ]
    check("every fixed line the bot sends is polite", [t for t in every_fixed_line if not tone.is_polite(t)], [])
    rude = json.dumps({"text": "бугун нима қилдинг?", "emoji": "🌙",
                       "options": [{"label": "зўр", "reply": "раҳмат"}, {"label": "ёмон", "reply": "раҳмат"}]})
    check("an AI line with 'сен' forms is thrown away", cheer.parse_ai(rude, "evening"), None)
    check("...and an AI lead summary", leads.parse_brief(json.dumps({"brief": "сен бу меҳмонхонага қўнғироқ қил, улар сенга ишонади"})), None)

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
    cheer_agent, reports_agent, leads_agent = load_agent("team-cheer"), load_agent("daily-reports"), load_agent("lead-handout")
    patch(cheer_agent, "send_slot", must_not_send)
    patch(reports_agent, "ask_everyone", must_not_send)
    patch(reports_agent, "remind_silent", must_not_send)
    patch(leads_agent, "morning", must_not_send)
    patch(leads_agent, "checkin", must_not_send)
    try:
        for name, coro in (("cheer", cheer_agent.run("evening", dry_run=True)),
                           ("report ask", reports_agent.run("ask", dry_run=True)),
                           ("report reminder", reports_agent.run("remind", dry_run=True)),
                           ("leads 08:00", leads_agent.run("morning", dry_run=True)),
                           ("leads 15:00", leads_agent.run("checkin", dry_run=True, force=True))):
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
    """2026-10-02: AI chat for granted employees (no company data); the leads sheet read by header."""
    print("AI chat for employees, leads sheet by header")
    import asyncio
    import uuid
    from datetime import timezone

    from integrations.common.agent_loader import load_agent
    from integrations.org_bot import admin, ai_chat, leads, ops_manager, store

    # ---- who may chat, and when it's on
    now = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    later, earlier = now + timedelta(minutes=5), now - timedelta(minutes=5)
    check("granted and running", ai_chat.session_active({"ai_chat": True, "ai_chat_until": later}, now), True)
    check("granted, timed out", ai_chat.session_active({"ai_chat": True, "ai_chat_until": earlier}, now), False)
    check("not granted", ai_chat.session_active({"ai_chat": False, "ai_chat_until": later}, now), False)

    # ---- what the AI knows, and doesn't
    b2b = ai_chat.system_prompt({"role": "b2b_sotuv"})
    garmin = ai_chat.system_prompt({"role": "garmin_sotuv"})
    check_true("no company data, no actions, never invented",
               "no access to any company system or data" in b2b and "Never invent such data" in b2b and "cannot send" in b2b)
    check_true("polite and Uzbek Cyrillic", '"сиз"' in b2b and "Cyrillic" in b2b)
    check_true("Garmin sales also get the public catalog, B2B doesn't", "MARQ" in garmin and "MARQ" not in b2b)
    for text, names_in in ((ai_chat.on_text("Алишер ака"), ("Алишер ака", "AI")), (ai_chat.off_text(), ("AI",)),
                           (ai_chat.not_granted_text(), ("AI",)), (ai_chat.limit_text(), ("AI",)), (ai_chat.error_text(), ())):
        check(f"friendly: {text[:30]}…", friendly_problems(text, names_in), [])

    # ---- the switch and the answers in OPS Manager Bot
    replies, sessions, turns, tasks = [], [], [], []

    class Background:
        def add_task(self, func, *args):
            tasks.append((func, args))

    class FakeAI:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def complete(self, system, user, **kwargs):
            assert "no access to any company system or data" in system
            return "Мана <b>хат</b> лойиҳаси"

    async def fake_reply(chat_id, run_id, text, reply_markup=None):
        replies.append(text)
        return [1]

    async def set_session(user, minutes):
        sessions.append(minutes)

    async def log_turn(user, role, content):
        turns.append((role, content))

    async def no_turns(user, limit=12):
        return []

    counter = {"n": 0}

    async def questions_today(user):
        return counter["n"]

    async def nothing(*args, **kwargs):
        return None

    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    for name, fake in (("set_ai_session", set_session), ("log_ai_turn", log_turn), ("recent_ai_turns", no_turns),
                       ("ai_questions_today", questions_today)):
        patch(store, name, fake)
    patch(ops_manager, "_reply", fake_reply)
    patch(ops_manager, "_show_typing", nothing)
    patch(ops_manager, "OpenRouterClient", FakeAI)
    patch(ops_manager, "now_utc", lambda: now)
    seller = {"telegram_user_id": 7, "role": "b2b_sotuv", "full_name": "Алишер Каримов", "address_form": "aka",
              "ai_chat": False, "ai_chat_until": None}
    bg = Background()
    try:
        check("/ai without the grant: politely refused",
              asyncio.run(ops_manager._maybe_ai_chat(seller, {"text": "/ai"}, uuid.uuid4(), bg)), "ai_not_granted")
        seller["ai_chat"] = True
        check("/ai with the grant turns it on", asyncio.run(ops_manager._maybe_ai_chat(seller, {"text": "/ai"}, uuid.uuid4(), bg)), "ai_on")
        check_true("...for 20 minutes, and says how to stop", sessions[-1] == 20 and "/ai" in replies[-1])
        check("while off, a message goes the usual way",
              asyncio.run(ops_manager._maybe_ai_chat(seller, {"text": "салом"}, uuid.uuid4(), bg)), None)
        seller["ai_chat_until"] = later
        check("while on, a message goes to the AI",
              asyncio.run(ops_manager._maybe_ai_chat(seller, {"text": "мижозга хат ёзиб беринг"}, uuid.uuid4(), bg)), "ai_chat")
        check("...but a Reply to the bot's own message keeps its meaning",
              asyncio.run(ops_manager._maybe_ai_chat(seller, {"text": "x", "reply_to_message": {"message_id": 5}}, uuid.uuid4(), bg)), None)
        func, args = tasks[-1]
        asyncio.run(func(*args))
        check("the answer comes back, and both turns are kept", (replies[-1], [r for r, _c in turns]),
              ("Мана <b>хат</b> лойиҳаси", ["employee", "assistant"]))
        counter["n"] = ai_chat.DAILY_LIMIT
        asyncio.run(func(*args))
        check("past the daily limit: told kindly, no AI call", replies[-1], ai_chat.limit_text())
        check("/ai again turns it off", asyncio.run(ops_manager._maybe_ai_chat(seller, {"text": "/ai"}, uuid.uuid4(), bg)), "ai_off")
        check("...ending the session", sessions[-1], None)
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    # ---- the admin grants it, and the person is told
    told, edits = [], []

    async def toggle(employee_id, by):
        return {"id": employee_id, "telegram_user_id": 7, "role": "b2b_sotuv", "status": "active", "display_name": "a",
                "full_name": "Алишер Каримов", "address_form": "aka", "ai_chat": True}

    async def get_employee(employee_id):
        return {"id": employee_id, "status": "active", "role": "b2b_sotuv", "display_name": "a"}

    async def tell(user, text, run_id):
        told.append(text)

    async def edit(callback, text, keyboard, run_id):
        edits.append((text, keyboard))

    async def fake_answer(query_id, text):
        return None

    saved.clear()
    patch(store, "toggle_ai_chat", toggle)
    patch(store, "get_employee", get_employee)
    patch(admin, "_tell_employee", tell)
    patch(admin, "_edit", edit)
    patch(admin, "_answer", fake_answer)
    try:
        asyncio.run(admin.handle_admin_callback({"id": "q", "data": "aich:e7", "from": {"id": 5}}, uuid.uuid4()))
        check_true("the 🤖 button grants it and tells the person how to start",
                   told and told[-1].startswith("Алишер ака, сизга AI ёрдамчи очилди") and "/ai" in told[-1])
        check_true("...and the card shows it", edits and "AI суҳбат: рухсат берилган" in edits[-1][0])
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
    check("rows_to_leads reads it right too", leads.rows_to_leads([moved, [row[c] for c in moved]])[0]["dedupe_key"],
          "hyatt|equipment_sales")
    check("an unrecognisable header falls back to the standard order",
          leads.column_order(["А", "Б", "В"]), cols)
    lead_agent = load_agent("lead-agent")
    out = lead_agent.to_sheet_row({"company_name": "Hilton", "track": "equipment_sales"}, "2026-10-02", moved)
    check_true("a new lead is written in the sheet's current order",
               out[moved.index("company_name")] == "Hilton" and out[moved.index("date_added")] == "2026-10-02"
               and out[1] == "" and len(out) == len(moved))
    check("column letters", [leads.column_letter(n) for n in (1, 20, 26, 27, 52)], ["A", "T", "Z", "AA", "AZ"])


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
        check_true("...shows today's picture and the organisation",
                   "Verifix уланди" in sent[-1] and "Табелда: 2 ходим" in sent[-1] and "ЭМЖИЕМ" in sent[-1])
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
        check_true("/1c report: connected, accounts, warning about payroll",
                   "1C уланди" in text and "5110" in text and "Иш ҳақи" in text)
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


def main() -> int:
    """Run every check.

    Returns:
        0 if all checks pass, 1 otherwise.
    """
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
        test_verifix,
        test_verifix_basic_login,
        test_employee_admin,
        test_kpi,
        test_garmin_leads,
        test_billz,
        test_onec,
        test_onec_cash,
        test_plan_agents,
        test_db_viewer,
        test_names_and_routing,
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
