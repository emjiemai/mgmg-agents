"""AI checker (2026-11-16, the owner: "the Boss is not checking what is done — everything that goes to him needs to be
checked or approved").

Four things that used to wait for the Boss's tap are judged by the AI as soon as they reach him:

  * **explanation** — «why no report / why absent»: a real reason → ⬜ Сабабли (the days turn grey); an empty or
    false one (e.g. «Юборганман» while no report for that day is in the bot) → not excused;
  * **late_report** — «🕘 Кечиккан ҳисобот»: real work described → approved (green); empty / meaningless → declined;
  * **task_proof** — a task «текширувда»: the typed proof against the task and its expected result → accepted or
    returned with a reason code (a proof that is only a file is never read — employees' files stay unread — so it
    stays with the person who checks);
  * **reschedule** — «📅 янги муддат»: a concrete reason, few moves → yes; vague / repeated → no.

When the AI is sure it presses the same button the Boss would (same code path, same messages to the employee,
marked «AI текширувчи»), then puts a note under the card: the verdict, one-line reason and «↩️ AI қарорини бекор
қилиш». Undo puts the record back exactly as it was before (the columns are kept in ``ai_decisions``), brings the
card's buttons back and tells the employee it is being looked at again. When the AI is unsure, nothing is decided:
the note says so and the Boss decides. An explanation the AI excused is undone with the card's own ↩ button.

``AI_CHECKER_ENABLED`` (on) and ``AI_CHECKER_KINDS`` switch it. Logs keep counts and verdicts only, never the text.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable

from integrations.ai.openrouter_client import OpenRouterClient
from integrations.common.config import settings
from integrations.common.db import execute, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.telegram.bot import TelegramBot, escape

AGENT = "ai-checker"
ACTOR = "ai-checker"
log = setup_logging(AGENT)

KINDS = ("explanation", "late_report", "task_proof", "reschedule")
REASON_MAX = 220
# What each pressed button returns when it really decided (anything else: someone was first, or not allowed).
APPLIED = frozenset({"task_closed", "task_returned", "late_report_approved", "late_report_declined", "rescheduled",
                     "reschedule_rejected", "excused_set"})

# Columns a decision can change — kept before and after, so an undo restores exactly what was there.
TASK_COLS = ("status", "completed_at", "completed_by", "verified_at", "proposed_due", "reminded_at",
             "overdue_notified_at", "escalation_level", "escalated_at", "awaiting", "awaiting_since", "returns",
             "due_date", "reschedules")
REPORT_COLS = ("status", "content", "submitted_at", "penalty_points", "late_decision", "late_decided_by",
               "late_decided_at")
TABLES = {"late_report": ("daily_reports", REPORT_COLS), "task_proof": ("tasks", TASK_COLS),
          "reschedule": ("tasks", TASK_COLS)}

SYSTEM = """You are the AI checker of a company's operations bot (Uzbekistan). Things employees send that used to \
wait for the director's approval are judged by you instead. Be fair and strict: approve only what clearly holds, \
reject only what clearly does not, and say "unsure" whenever the facts given are not enough. Never invent facts. \
Employees write in Uzbek (Latin or Cyrillic) or Russian; short and informal is normal.

What you judge:
- explanation: the employee's answer to «why was there no report / why were you absent». approve = a real, specific \
reason (illness, family matter, day off agreed, worked elsewhere on company business, the bot failed...). reject = no \
reason at all («ок», «билмадим», «ҳа», a greeting), or a claim the facts contradict — e.g. «I sent it / юборганман» \
while the facts say no report for that day is in the bot. A late report pasted as the answer counts as a reason.
- late_report: a daily report sent after the deadline. approve = it describes real work done that day (even short: \
«Лондри1 тозалаб, Лондри2 тозалаб, телга жавоб бердим» is real work). reject = empty, a greeting, «ок», «ҳисобот», \
only a reference like «тепада», or text with no work in it.
- task_proof: the typed proof an employee sent for a task. approve = the text shows the task (and the expected \
result, if one is given) is done. reject = the text shows it is not done, partly done, or is unrelated; then pick \
return_code: "e" proof not enough, "q" quality not acceptable, "r" a different result than asked, "o" other. The \
proof's FILE is given when it could be read (2026-11-18): proof_file.content = its text (PDF / Word / Excel — tables \
as rows «a | b | c»), or the attached picture (a photo or a scanned page) — judge what the file shows against the task \
like typed proof; a file that clearly shows the work done is enough even with no typed words. proof_has_file true \
but no proof_file and no picture = it could not be read: "unsure".
- reschedule: an employee asks to move a task's deadline. approve = a concrete reason outside their control or a \
fair amount of work, and it is not moved many times already. reject = vague («улгурмадим», «вақт етмади» alone), or \
moved 2+ times already with no new reason.

Return ONLY JSON: {"decision": "approve" | "reject" | "unsure", "reason": "<one short polite sentence in Uzbek \
Cyrillic, polite «сиз» form, saying why — for the director>", "return_code": "e" | "q" | "r" | "o" | null}"""


@dataclass
class Card:
    """A card that went to a decider: where it is and whether it is a file's caption."""
    chat_id: int
    message_id: int
    is_caption: bool = False


@dataclass
class Verdict:
    decision: str  # approve | reject | unsure
    reason: str
    return_code: str | None = None


_TASKS: set[asyncio.Task] = set()


def enabled(kind: str) -> bool:
    kinds = {k.strip() for k in settings.ai_checker_kinds.split(",") if k.strip()}
    return (settings.ai_checker_enabled and kind in kinds and not settings.bots_frozen
            and bool(settings.openrouter_api_key.get_secret_value().strip()))


def spawn(kind: str, work: Awaitable[Any]) -> None:
    """Run a check in the background — the employee's own reply never waits for the AI."""
    if not enabled(kind):
        close = getattr(work, "close", None)
        if close:
            close()  # never started: no "coroutine was never awaited"
        return

    async def guarded() -> None:
        try:
            await work
        except Exception as exc:  # noqa: BLE001 — the card stays with the Boss, as before
            log.error("AI check failed ({}): {}", kind, type(exc).__name__)

    task = asyncio.get_running_loop().create_task(guarded())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value if value is None or isinstance(value, (int, float, str, bool)) else str(value)


def parse(raw: dict[str, Any]) -> Verdict:
    """The AI's JSON as a verdict; anything odd is «unsure» (nothing gets decided on a doubtful answer)."""
    decision = str(raw.get("decision") or "").strip().lower()
    if decision not in ("approve", "reject"):
        decision = "unsure"
    reason = " ".join(str(raw.get("reason") or "").split())[:REASON_MAX]
    code = raw.get("return_code")
    code = code if code in ("e", "q", "r", "o") else ("e" if decision == "reject" else None)
    from integrations.org_bot import tone

    if decision != "unsure" and (not reason or not tone.is_polite(reason)):
        decision = "unsure"  # a verdict without a fit reason is not acted on
    return Verdict(decision, reason, code)


async def judge(kind: str, facts: dict[str, Any], run_id: uuid.UUID, image: bytes | None = None,
                image_mime: str = "image/jpeg") -> Verdict:
    """One AI call (with the proof's picture when given — the image reading the cheques use). Errors → unsure."""
    try:
        async with OpenRouterClient(agent=AGENT, run_id=run_id, model_override=settings.ops_manager_bot_model,
                                    fallback_override=settings.ops_manager_bot_fallback_models) as ai:
            text = json.dumps({"kind": kind, **facts}, ensure_ascii=False, default=str)
            raw = (await ai.vision_json(SYSTEM, text, image, image_mime) if image is not None
                   else await ai.complete_json(SYSTEM, text))
    except Exception as exc:  # noqa: BLE001
        log.warning("AI checker unavailable: {}", type(exc).__name__)
        return Verdict("unsure", "")
    return parse(raw if isinstance(raw, dict) else {})


# ------------------------------------------------------------------------------------------------ records + notes

async def _snapshot(kind: str, ref_id: str) -> dict[str, Any] | None:
    if kind not in TABLES:
        return None
    table, cols = TABLES[kind]
    row = await fetch_one(f"SELECT {', '.join(cols)} FROM {table} WHERE id = %s", (ref_id,))
    return {c: _plain(row[c]) for c in cols} if row else None


async def _record(kind: str, ref_id: str, verdict: Verdict, card: Card | None, before: dict | None,
                  after: dict | None, read_file: bool = False) -> str:
    row = await fetch_one(
        """INSERT INTO ai_decisions (kind, ref_id, decision, reason, chat_id, card_message_id, card_is_caption,
                                     before, after, read_file)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text AS id""",
        (kind, ref_id, verdict.decision, verdict.reason, card.chat_id if card else None,
         card.message_id if card else None, bool(card and card.is_caption),
         json.dumps(before) if before is not None else None, json.dumps(after) if after is not None else None,
         read_file))
    return row["id"]


def note_text(kind: str, verdict: Verdict, applied: bool) -> str:
    what = {"explanation": ("сабабли деб белгилади", "сабабни асосли деб топмади"),
            "late_report": ("кечиккан ҳисоботни тасдиқлади", "кечиккан ҳисоботни рад этди"),
            "task_proof": ("топшириқни қабул қилди", "топшириқни қайтарди"),
            "reschedule": ("янги муддатга рози бўлди", "муддатни ўзгартирмади")}[kind]
    reason = f" — {escape(verdict.reason)}" if verdict.reason else ""
    if verdict.decision == "unsure" or not applied:
        return f"🤖 AI текширувчи: аниқ хулоса қила олмади{reason}. Ўзингиз ҳал қилинг."
    head = "✅" if verdict.decision == "approve" else "❌"
    line = f"🤖 AI текширувчи {head} {what[0] if verdict.decision == 'approve' else what[1]}{reason}."
    if kind == "explanation" and verdict.decision == "reject":
        line += " Сабабли деб ҳисобласангиз, картадаги ⬜ тугмасини босинг."
    return line


def undo_keyboard(decision_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "↩️ AI қарорини бекор қилиш", "callback_data": f"aiu:{decision_id}"}]]}


def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _note(card: Card, text: str, keyboard: dict | None, run_id: uuid.UUID) -> int | None:
    async with _bot(run_id) as bot:
        ids = await bot.send_message(text, chat_id=str(card.chat_id), reply_markup=keyboard,
                                     reply_to_message_id=card.message_id)
    return ids[0] if ids else None


def _press(card: Card, decider: int) -> dict[str, Any]:
    """The callback the decider's own tap would have produced — marked as the AI's (``ai``), no query id."""
    message: dict[str, Any] = {"chat": {"id": card.chat_id}, "message_id": card.message_id}
    message["caption" if card.is_caption else "text"] = ""
    return {"id": "", "ai": True, "from": {"id": decider, "username": ACTOR}, "message": message}


async def _decide(kind: str, ref_id: str, verdict: Verdict, card: Card | None, decider: int | None,
                  apply: Any, run_id: uuid.UUID, undoable: bool = True, read_file: bool = False) -> str:
    """Apply (when sure), record, put the note under the card. Returns the outcome word (counts only in logs)."""
    applied = False
    before = after = None
    if verdict.decision != "unsure" and card is not None and decider is not None:
        before = await _snapshot(kind, ref_id)
        outcome = await apply(_press(card, decider))
        applied = outcome in APPLIED
        after = await _snapshot(kind, ref_id) if applied else None
    shown = verdict if applied or verdict.decision == "unsure" else Verdict("unsure", verdict.reason)
    if kind == "explanation" and verdict.decision == "reject":
        shown, applied = verdict, True  # nothing to press: the card keeps ⬜ for the Boss
    decision_id = await _record(kind, ref_id, shown, card, before, after, read_file)
    if card is not None:
        keyboard = undo_keyboard(decision_id) if applied and undoable and before is not None else None
        try:
            note_id = await _note(card, note_text(kind, shown, applied), keyboard, run_id)
            if note_id:
                await execute("UPDATE ai_decisions SET note_message_id = %s WHERE id = %s", (note_id, decision_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("AI checker note not sent: {}", type(exc).__name__)
    result = shown.decision if applied or shown.decision == "unsure" else "unsure"
    await log_action(agent=AGENT, action=f"ai_check_{kind}", target_system="postgres", status="success",
                     run_id=run_id, mode="write", payload={"decision": result, "applied": applied, "read_file": read_file})
    return result


def _director_card(cards: list[tuple[Card, int, str | None]]) -> tuple[Card, int] | None:
    """Of the cards sent, the Director's one (no extra «… decided» notice to him), else the first."""
    from integrations.org_bot.roles import DIRECTOR_ROLE

    if not cards:
        return None
    pick = next((c for c in cards if c[2] == DIRECTOR_ROLE), cards[0])
    return pick[0], pick[1]


# ------------------------------------------------------------------------------------------------------ the four

async def review_explanation(row: dict[str, Any], employee: dict[str, Any], answer: str,
                             cards: list[tuple[Card, int, str | None]], run_id: uuid.UUID | None = None) -> str:
    from integrations.org_bot import excused, store

    run_id = run_id or uuid.uuid4()
    days = [d for d in (row.get("days") or [])]
    facts: dict[str, Any] = {"asked_about": row.get("kind"), "days": [str(d) for d in days], "answer": answer,
                             "role": employee.get("role")}
    if row.get("kind") == "report":
        facts["reports_in_bot"] = {str(d): s for d, s in (await store.report_states(str(employee["id"]), days)).items()}
    verdict = await judge("explanation", facts, run_id)
    pick = _director_card(cards)
    card, decider = pick if pick else (None, None)
    if verdict.decision == "reject":
        return await _decide("explanation", str(row["id"]), verdict, card, None, None, run_id)
    return await _decide("explanation", str(row["id"]), verdict, card, decider,
                         lambda cb: excused.handle_callback("exc", str(row["id"]), cb, run_id), run_id, undoable=False)


async def review_late_report(report: dict[str, Any], employee: dict[str, Any],
                             cards: list[tuple[Card, int, str | None]], run_id: uuid.UUID | None = None,
                             has_file: bool = False) -> str:
    from integrations.org_bot import late_report

    run_id = run_id or uuid.uuid4()
    facts = {"report_day": str(report.get("report_date")), "sent_at": str(report.get("late_sent_at")),
             "report": report.get("late_content") or "", "role": employee.get("role"), "has_file": has_file}
    verdict = await judge("late_report", facts, run_id)
    pick = _director_card(cards)
    card, decider = pick if pick else (None, None)
    prefix = "lrok" if verdict.decision == "approve" else "lrno"
    return await _decide("late_report", str(report["id"]), verdict, card, decider,
                         lambda cb: late_report.handle_callback(prefix, str(report["id"]), cb, run_id), run_id)


PROOF_PLACEHOLDERS = frozenset({"фото", "видео", "файл", "овозли хабар", "аудио", "анимация", "видео хабар"})


async def proof_reading(task: dict[str, Any], run_id: uuid.UUID) -> Any:
    """The proof's file as the AI may see it (proof_files.py, 18.11) — None when there's none or it can't be read.
    A proof from before 18.11 has only its message id: the file is recovered from the employee's chat."""
    from integrations.org_bot import proof_files

    f = None
    if task.get("evidence_file_id"):
        f = proof_files.ProofFile(task["evidence_file_id"], task.get("evidence_file_kind") or "document",
                                  task.get("evidence_file_mime") or "", task.get("evidence_file_name") or "")
    elif task.get("evidence_message_id") and task.get("employee_telegram_user_id"):
        held = await _held_file(task)
        try:
            f = held or await proof_files.recover(int(task["employee_telegram_user_id"]),
                                                  int(task["evidence_message_id"]), run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Proof file not recovered: {}", type(exc).__name__)
    if not proof_files.readable(f):
        return None
    try:
        data = await proof_files.download(f, run_id)
    except Exception as exc:  # noqa: BLE001 — too big, gone: unsure
        log.warning("Proof file not downloaded: {}", type(exc).__name__)
        return None
    reading = proof_files.extract(f, data)
    return reading if (reading.text or reading.image) else None


async def _held_file(task: dict[str, Any]) -> Any:
    """A proof chosen from a held file («нима учун?» → топшириқ далили): its file id is in employee_files."""
    from integrations.org_bot import proof_files

    row = await fetch_one(
        """SELECT file_id, kind, mime FROM employee_files WHERE telegram_user_id = %s AND message_id = %s
             AND file_id IS NOT NULL ORDER BY created_at DESC LIMIT 1""",
        (task["employee_telegram_user_id"], task["evidence_message_id"]))
    return proof_files.ProofFile(row["file_id"], row["kind"], (row.get("mime") or "").lower()) if row else None


async def review_task_proof(task: dict[str, Any], card: Card | None, has_file: bool,
                            run_id: uuid.UUID | None = None) -> str:
    from integrations.org_bot import ops_manager, store, task_register

    run_id = run_id or uuid.uuid4()
    task = {**task, **(await store.task_with_employee(str(task["id"])) or {})}  # the employee's chat and card
    typed = str(task.get("evidence_text") or "")
    if has_file and typed.strip() in PROOF_PLACEHOLDERS:
        typed = ""  # «файл» / «фото» is the bot's own label, not what they wrote (18.11)
    facts: dict[str, Any] = {"task": task.get("task_summary") or "", "expected_result": task.get("expected_result"),
                             "proof_text": typed, "proof_has_file": has_file,
                             "returned_before": int(task.get("returns") or 0)}
    reading = await proof_reading(task, run_id) if has_file and settings.ai_checker_read_files else None
    if reading is not None and reading.text:
        facts["proof_file"] = {"type": reading.note, "content": reading.text}
    elif reading is not None and reading.image:
        facts["proof_file"] = {"type": reading.note, "content": "the attached picture"}
    read = reading is not None and bool(reading.text or reading.image)
    verdict = await judge("task_proof", facts, run_id, reading.image if read and reading.image else None,
                          reading.image_mime if read else "image/jpeg")
    decider = task.get("director_telegram_user_id")

    async def apply(cb: dict[str, Any]) -> str:
        if verdict.decision == "approve":
            return await ops_manager._handle_task_verify(True, str(task["id"]), cb, run_id)
        label = task_register.RETURN_REASONS.get(verdict.return_code or "e")
        out = await ops_manager._handle_task_verify(False, str(task["id"]), cb, run_id, reason=label)
        if out == "task_returned" and verdict.reason and task.get("employee_telegram_user_id"):
            await ops_manager._reply(task["employee_telegram_user_id"], run_id,
                                     f"🤖 Изоҳ: {escape(verdict.reason)}", reply_to=task.get("telegram_message_id"))
        return out

    return await _decide("task_proof", str(task["id"]), verdict, card, int(decider) if decider else None, apply, run_id,
                         read_file=read)


async def review_reschedule(task: dict[str, Any], reason: str, card: Card | None,
                            run_id: uuid.UUID | None = None) -> str:
    from integrations.org_bot import ops_manager

    run_id = run_id or uuid.uuid4()
    facts = {"task": task.get("task_summary") or "", "deadline": str(task.get("due_date")),
             "asked_deadline": str(task.get("proposed_due")), "reason": reason,
             "moved_before": int(task.get("reschedules") or 0)}
    verdict = await judge("reschedule", facts, run_id)
    decider = task.get("director_telegram_user_id")
    accept = verdict.decision == "approve"
    return await _decide("reschedule", str(task["id"]), verdict, card, int(decider) if decider else None,
                         lambda cb: ops_manager._handle_reschedule_decision(accept, str(task["id"]), cb, run_id), run_id)


# ---------------------------------------------------------------------------------------------------------- undo

def restore_sql(table: str, before: dict[str, Any], after: dict[str, Any]) -> tuple[str, list[Any]]:
    """Put ``before`` back — only while the row is still exactly as the AI left it (``after``)."""
    cols = list(before)
    sets = ", ".join(f"{c} = %s" for c in cols)
    guard = " AND ".join(f"{c} IS NOT DISTINCT FROM %s" for c in cols)
    return (f"UPDATE {table} SET {sets} WHERE id = %s AND {guard} RETURNING id",
            [before[c] for c in cols] + ["<id>"] + [after.get(c) for c in cols])


async def handle_undo(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """↩️ under the AI's note: the record back as it was, the card's buttons back, the employee told."""
    from integrations.org_bot import late_report, ops_manager, store, task_register
    from integrations.org_bot.roles import DIRECTOR_ROLE

    clicker_id = (callback.get("from") or {}).get("id")
    row = await fetch_one("SELECT * FROM ai_decisions WHERE id = %s", (rest,))
    clicker = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    async with _bot(run_id) as bot:
        answer = bot._answer_callback  # noqa: SLF001
        if row is None or clicker is None or (int(row["chat_id"] or 0) != int(clicker_id)
                                              and clicker.get("role") != DIRECTOR_ROLE):
            await answer(callback.get("id", ""), "Рухсат йўқ")
            return "ai_undo_refused"
        if row["undone_at"] is not None or row["kind"] not in TABLES or not row["before"]:
            await answer(callback.get("id", ""), "Аллақачон бекор қилинган")
            return "ai_undo_closed"
        table, _ = TABLES[row["kind"]]
        before = row["before"] if isinstance(row["before"], dict) else json.loads(row["before"])
        after = row["after"] if isinstance(row["after"], dict) else json.loads(row["after"] or "{}")
        sql, params = restore_sql(table, before, after)
        params[params.index("<id>")] = row["ref_id"]
        restored = await fetch_one(sql, tuple(params))
        if restored is None:
            await answer(callback.get("id", ""), "Ўзгариб бўлган — бекор қилиб бўлмайди")
            return "ai_undo_changed"
        await execute("UPDATE ai_decisions SET undone_at = now(), undone_by = %s WHERE id = %s", (clicker_id, rest))
        keyboard = {"late_report": lambda: late_report.card_keyboard(row["ref_id"]),
                    "task_proof": lambda: task_register.review_keyboard(row["ref_id"]),
                    "reschedule": lambda: task_register.reschedule_keyboard(row["ref_id"])}[row["kind"]]()
        try:
            await bot._call("editMessageReplyMarkup", {  # noqa: SLF001
                "chat_id": str(row["chat_id"]), "message_id": row["card_message_id"], "reply_markup": keyboard},
                mode="write", target_ref=str(row["chat_id"]))
        except Exception as exc:  # noqa: BLE001 — the note below still says what to do
            log.warning("Card buttons not restored: {}", type(exc).__name__)
        message = callback.get("message") or {}
        if message.get("message_id"):
            await bot._edit_message(  # noqa: SLF001
                chat_id=str(row["chat_id"]), message_id=message["message_id"],
                text="↩️ AI текширувчи қарори бекор қилинди — картадаги тугмалар билан ўзингиз ҳал қилинг.",
                reply_markup={"inline_keyboard": []})
        await answer(callback.get("id", ""), "↩️ Бекор қилинди")
    person_chat, code = None, ""
    if row["kind"] == "late_report":
        report = await store.get_report(row["ref_id"])
        person = await store.get_employee(str(report["employee_id"])) if report else None
        person_chat = person.get("telegram_user_id") if person else None
        what = f"{report['report_date']:%d.%m} кечиккан ҳисоботингиз" if report else "кечиккан ҳисоботингиз"
    else:
        full = await store.task_with_employee(row["ref_id"])
        person_chat = (full or {}).get("employee_telegram_user_id")
        code = escape(task_register.code_of(full)) if full else ""
        what = f"{code} топшириғингиз" if row["kind"] == "task_proof" else f"{code} муддат сўровингиз"
        if full:
            await store.log_task_event(row["ref_id"], "ai_undone", str(clicker_id), row["kind"])
            await ops_manager._refresh_task_card(full, run_id)
    if person_chat:
        try:
            await ops_manager._reply(int(person_chat), run_id,
                                     f"ℹ️ {what} бўйича олдинги қарор бекор қилинди — раҳбар қайта кўриб чиқади.")
        except Exception as exc:  # noqa: BLE001
            log.warning("Undo not told: {}", type(exc).__name__)
    await log_action(agent=AGENT, action="ai_check_undone", target_system="postgres", status="success",
                     run_id=run_id, mode="write", payload={"kind": row["kind"]})
    return "ai_undone"


# --------------------------------------------------------------------------- the waiting ones + a live test (16.11)

# Fixed examples for Admin Bot «/aicheck sinov»: the real AI on made-up texts — nothing is changed, nobody is told.
SAMPLES: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("explanation", {"asked_about": "report", "days": ["2026-11-13"], "answer": "Юборганман",
                     "reports_in_bot": {"2026-11-13": "none"}}, "reject"),
    ("explanation", {"asked_about": "report", "days": ["2026-11-13"], "answer": "кеча касал бўлиб қолдим, иситмам бор эди",
                     "reports_in_bot": {"2026-11-13": "none"}}, "approve"),
    ("late_report", {"report_day": "2026-11-13", "report": "Лондри1 тозалаб . Лондри2 тозалаб. Халиям телга жавоб "
                     "бервомман."}, "approve"),
    ("late_report", {"report_day": "2026-11-13", "report": "ок"}, "reject"),
    ("task_proof", {"task": "Витринадаги нархларни янгиланг", "expected_result": "янги нарх ёрлиқлари",
                    "proof_text": "барча нарх ёрлиқлари янгиланди, 24 та маҳсулот", "proof_has_file": False}, "approve"),
    ("task_proof", {"task": "Ойлик ҳисоботни PDF қилиб юборинг", "expected_result": "PDF файл",
                    "proof_text": "эртага қиламан", "proof_has_file": False}, "reject"),
    ("reschedule", {"task": "Омбор хатловини ўтказинг", "deadline": "2026-11-14", "asked_deadline": "2026-11-16",
                    "reason": "улгурмадим", "moved_before": 3}, "reject"),
)


async def self_test(run_id: uuid.UUID | None = None) -> list[tuple[str, str, str]]:
    """Each sample through the real AI: (kind, expected, got). For IT — no company data in or out."""
    run_id = run_id or uuid.uuid4()
    out = []
    for kind, facts, expected in SAMPLES:
        verdict = await judge(kind, facts, run_id)
        out.append((kind, expected, verdict.decision))
    return out


async def run_backlog(run_id: uuid.UUID | None = None, only_tasks: bool = False) -> dict[str, int]:
    """Admin Bot «/aicheck»: what already waited for the Boss before the checker existed — each card is sent to him
    again (the old one may be days up the chat) and judged like a new one. Counts only; once per item
    (anything already in ``ai_decisions`` is skipped)."""
    from integrations.org_bot import excused, explanations, late_report, names, ops_manager, store, task_register
    from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS

    run_id = run_id or uuid.uuid4()
    counts = {"approve": 0, "reject": 0, "unsure": 0, "skipped": 0}
    directors = await store.active_employees_by_role(DIRECTOR_ROLE)
    if not directors:
        return counts
    boss = int(directors[0]["telegram_user_id"])
    items = await store.waiting_for_boss()
    if only_tasks:  # 18.11: the proofs whose files can now be read
        items = {"explanations": [], "late_reports": [], "tasks": items["tasks"]}
    if not any(items.values()):
        return counts
    async with _bot(run_id) as bot:
        await bot.send_message("🤖 AI текширувчи сиз ҳал қилишингизни кутиб турган ишларни кўриб чиқди — ҳар бирининг "
                               "остида сабаби ва «↩️ бекор қилиш» тугмаси бор.", chat_id=str(boss))

    async def send(text: str, keyboard: dict[str, Any]) -> Card | None:
        async with _bot(run_id) as bot:
            ids = await bot.send_message(text, chat_id=str(boss), reply_markup=keyboard)
        return Card(boss, ids[-1]) if ids else None

    for row in items["explanations"]:
        if not enabled("explanation"):
            counts["skipped"] += 1
            continue
        person = await store.get_employee(str(row["employee_id"]))
        if not person:
            counts["skipped"] += 1
            continue
        role = ROLE_LABELS.get(person.get("role") or "", person.get("role") or "")
        card = await send(explanations.card(names.person_name(person), role, row["kind"], list(row["days"]),
                                            row.get("answer") or ""), excused.card_keyboard(str(row["id"]), False))
        out = await review_explanation(row, person, row.get("answer") or "", [(card, boss, DIRECTOR_ROLE)] if card else [],
                                       run_id)
        counts[out] = counts.get(out, 0) + 1
    for row in items["late_reports"]:
        if not enabled("late_report"):
            counts["skipped"] += 1
            continue
        person = await store.get_employee(str(row["employee_id"]))
        if not person:
            counts["skipped"] += 1
            continue
        role = ROLE_LABELS.get(person.get("role") or "", person.get("role") or "")
        card = await send(late_report.card(names.person_name(person), role, row["report_date"], row.get("late_sent_at"),
                                           row.get("late_content") or ""), late_report.card_keyboard(str(row["id"])))
        out = await review_late_report(row, person, [(card, boss, DIRECTOR_ROLE)] if card else [], run_id)
        counts[out] = counts.get(out, 0) + 1
    for row in items["tasks"]:
        if not enabled("task_proof"):
            counts["skipped"] += 1
            continue
        full = await store.task_with_employee(str(row["id"]))
        if not full or full.get("status") != "review" or not full.get("director_telegram_user_id"):
            counts["skipped"] += 1
            continue
        chat = int(full["director_telegram_user_id"])
        review_id, reached = await ops_manager._deliver_review(
            full, full.get("display_name") or "", int(full["employee_telegram_user_id"]),
            task_register.shared_line(await store.open_shared_copies(full)), run_id, [chat],
            full.get("evidence_message_id"))
        if not review_id:
            counts["skipped"] += 1
            continue
        await store.set_task_review_message(str(row["id"]), review_id)
        has_file = bool(full.get("evidence_message_id"))
        out = await review_task_proof(full, Card(int(reached), int(review_id), has_file), has_file, run_id)
        counts[out] = counts.get(out, 0) + 1
    await log_action(agent=AGENT, action="ai_check_backlog", target_system="postgres", status="success",
                     run_id=run_id, mode="write", payload=counts)
    return counts


PROOF_FILES_JOB = "ai_proof_files_backfill"
PROOF_FILES_DAY = date(2026, 11, 18)


async def proof_files_backfill() -> dict[str, int] | None:
    """Once (API start-up, 18.11): the task proofs the checker could only call «unsure» because their files couldn't be
    read are checked again, now with the file. A job_runs lease keeps it to one run; IT gets counts only."""
    from integrations.common import job_runs
    from integrations.org_bot import reopen_fix

    if not enabled("task_proof") or not settings.ai_checker_read_files or not await reopen_fix._schema_ready():
        return None
    if not await job_runs.claim(PROOF_FILES_JOB, PROOF_FILES_DAY):
        return None
    counts = await run_backlog(only_tasks=True)
    await job_runs.finish(PROOF_FILES_JOB, PROOF_FILES_DAY)
    from integrations.org_bot import admin

    try:
        await admin.tell_admin(f"🤖 Файлли далиллар AI текширувчида қайта кўрилди: тасдиқлади {counts.get('approve', 0)}, "
                               f"қайтарди {counts.get('reject', 0)}, аниқ эмас (раҳбарга қолди) {counts.get('unsure', 0)}, "
                               f"ўтказиб юборилди {counts.get('skipped', 0)}.", uuid.uuid4())
    except Exception as exc:  # noqa: BLE001
        log.warning("Backfill counts not told: {}", type(exc).__name__)
    return counts
