"""The one task register — Day 1 of the owner's spec of 11.10.2026 («EMJIEM AI ТЕХ ЗАД», sections 5, 7.1, 8).

"A report is not the result. Every deviation → task → owner → deadline →
proof → acceptance." So every task the bot carries now has:

- a code, ``T-2026-10-0147`` (``tasks.code``, from a sequence);
- one owner (the employee) and the one who assigned it (who checks it);
- states, shown on the card and in the Director's answers:

  | state (``tasks.status``) | means | employee's buttons |
  | ---- | ---- | ---- |
  | ``sent`` | Яратилди — waiting to be accepted | ▶️ Қабул қилдим · ⛔ Тўсиқ бор · ✅ Бажардим |
  | ``started`` | Қабул қилинди / ижрода | ⛔ Тўсиқ бор · ✅ Бажардим |
  | ``blocked`` | Тўсиқ бор (with the reason) | ▶️ Давом эттирдим · ✅ Бажардим |
  | ``review`` | Текширувда — done, proof sent | — (the one who assigned it: ✅ Қабул / ↩️ Қайтариш) |
  | ``done`` | Ёпилди — accepted | — |
  | ``cancelled`` | Бекор қилинди | — |

- **"Done" needs proof**: ✅ asks for a photo, a file or a short text; the
  next message is the proof (``tasks.awaiting``). Without it nothing closes.
  2026-11-04: the ask is a NEW message under the card (``prompt_text``) — an
  edited day-old card far up the chat wasn't seen (03.11: six ✅, no proof, all
  still red). A reply to that message or to the card is the task's proof however
  much later; a plain message only within ``AWAIT_MINUTES``. Several waiting:
  each proof's thanks names the rest (``waiting_line``); ✅ still without proof
  the next morning — one line in the morning message.
- ↩️ Қайтариш sends it back to ``started`` — the employee does it again and
  sends new proof;
- every step is in ``task_events`` (who, when, the blocker or proof text).

Pure helpers here (tested offline); ``ops_manager`` runs the flow.
"""

from __future__ import annotations

import re
from typing import Any

from integrations.telegram.bot import escape, sanitize_model_html

OPEN = ("sent", "started", "blocked", "review")
STATUS_LABELS = {
    "sent": "Яратилди — қабул қилинмаган",
    "started": "Қабул қилинди — ижрода",
    "blocked": "Тўсиқ бор",
    "review": "Текширувда",
    "done": "Ёпилди",
    "cancelled": "Бекор қилинди",
}
STATUS_ICONS = {"sent": "🆕", "started": "▶️", "blocked": "⛔", "review": "🔎", "done": "✅", "cancelled": "✖️"}
EVIDENCE_MAX = 1500
BLOCKER_MAX = 500
CAPTION_MAX = 1024  # Telegram's limit for a photo / file caption
AWAIT_MINUTES = 30  # after ✅ / ⛔, only a message within this long is taken as the proof / reason

# Priority (2026-11-10, the owner's МИЯ document §5.2): 1 шошилинч — today, money or a client at stake; 2 муҳим — this
# week (the default); 3 оддий — within two weeks; 4 кутиши мумкин — no deadline.
PRIORITIES = {1: ("🔴", "Шошилинч"), 2: ("🟠", "Муҳим"), 3: ("🟢", "Оддий"), 4: ("⚪", "Кутиши мумкин")}
DEFAULT_PRIORITY = 2
_URGENT = re.compile(r"шошилинч|зудлик|тезда|тезроқ|ҳозироқ|бугуноқ|срочн|немедленн|shoshilinch|zudlik|tezda|tezroq|"
                     r"hoziroq|bugunoq|urgent", re.I)
_CAN_WAIT = re.compile(r"кутиши мумкин|шошилмасдан|шошилинч эмас|вақт бўлганда|не срочн|bo'sh vaqt|shoshilmasdan", re.I)
RESCHEDULE_DAYS = (1, 2, 7)  # the employee's choices for a new deadline: +1, +2 days, +1 week


def guess_priority(text: str | None) -> int:
    """The Director's own words decide the starting priority (one tap changes it on the card): «шошилинч», «тезда»,
    «срочно» → 1; «кутиши мумкин», «шошилмасдан» → 4; otherwise 2."""
    if _CAN_WAIT.search(text or ""):
        return 4
    return 1 if _URGENT.search(text or "") else DEFAULT_PRIORITY


def priority_label(priority: int | None) -> str:
    icon, word = PRIORITIES.get(int(priority or DEFAULT_PRIORITY), PRIORITIES[DEFAULT_PRIORITY])
    return f"{icon} {word}"


def priority_line(task: dict[str, Any]) -> str:
    """On the employee's card: only when it isn't the default (P2 says nothing new)."""
    priority = int(task.get("priority") or DEFAULT_PRIORITY)
    return "" if priority == DEFAULT_PRIORITY else f"<b>{priority_label(priority)}</b>"


def plain(html_text: str | None) -> str:
    """AI-written card text without its tags, on one line — safe to cut and escape."""
    return " ".join(re.sub(r"<[^>]+>", "", html_text or "").split())


def fit(head: str, body_plain: str, tail: str = "", limit: int = CAPTION_MAX) -> str:
    """``head`` + the escaped ``body_plain`` + ``tail``, the body shortened (never mid-tag) to fit ``limit``."""
    room = limit - len(head) - len(tail)
    body = escape(body_plain)
    if len(body) > room:
        low, high = 0, len(body_plain)  # the longest prefix whose escaped form + "…" fits
        while low < high:
            mid = (low + high + 1) // 2
            if len(escape(body_plain[:mid])) + 1 <= room:
                low = mid
            else:
                high = mid - 1
        body = escape(body_plain[:low]) + "…" if low else ""
    return head + body + tail


def code_of(task: dict[str, Any]) -> str:
    return str(task.get("code") or "")


def status_line(task: dict[str, Any]) -> str:
    """The line under a card that says where the task is."""
    status = task.get("status") or "sent"
    line = f"{STATUS_ICONS.get(status, '')} {STATUS_LABELS.get(status, status)}"
    if status == "blocked" and task.get("blocker"):
        line += f": {escape(str(task['blocker']))}"
    if task.get("awaiting") == "evidence":
        line = ("✍️ Бажарилганини тасдиқловчи <b>далил</b> юборинг: фото, файл ёки қисқа матн "
                "(нима қилинди, натижа) — шу хабарга жавоб қилиб. Далилсиз топшириқ ёпилмайди.")
    elif task.get("awaiting") == "blocker":
        line = "✍️ <b>Тўсиқ нимада?</b> Қисқа ёзинг (шу хабарга жавоб қилиб) — топшириқ берган кишига етказилади."
    elif task.get("awaiting") == "reschedule":
        line = "✍️ <b>Нега муддат етмаяпти?</b> Қисқа ёзинг (шу хабарга жавоб қилиб) — топшириқ берган кишига боради."
    elif task.get("proposed_due") and status in ("sent", "started", "blocked"):
        line += f" · 📅 янги муддат сўралди: {task['proposed_due']:%d.%m}, жавоб кутилмоқда"
    return line


def prompt_text(task: dict[str, Any]) -> str:
    """The ask after ✅ / ⛔ as a new message under the card (2026-11-04): which task, what to send, how."""
    code = escape(code_of(task))
    what = escape(plain(task.get("task_summary"))[:90])
    if task.get("awaiting") == "blocker":
        return (f"✍️ <b>{code}</b> «{what}» — <b>тўсиқ нимада?</b> Қисқа ёзинг, шу хабарга жавоб қилиб — "
                "топшириқ берган кишига етказилади.")
    if task.get("awaiting") == "reschedule":
        new = task.get("proposed_due")
        return (f"✍️ <b>{code}</b> «{what}» — янги муддат <b>{new:%d.%m}</b>. <b>Нега етмаяпти?</b> Қисқа ёзинг, шу "
                "хабарга жавоб қилиб — топшириқ берган киши розилик берса, муддат ўзгаради.")
    return (f"✍️ <b>{code}</b> «{what}» — бажарилганини тасдиқловчи <b>далил</b> юборинг: фото, файл ёки қисқа "
            "матн (нима қилинди, натижа), шу хабарга жавоб қилиб. Далил келгач топшириқ текширувга ўтади; "
            "далилсиз ёпилмайди.")


def waiting_line(codes: list[str]) -> str:
    """After one proof, the person's other tasks still waiting for theirs (several ✅ in a row)."""
    return (f"Яна {len(codes)} та топшириқ далил кутяпти: {escape(', '.join(codes))} — ҳар бирининг "
            "«далил юборинг» хабарига ёки картасига жавоб қилиб юборинг.")


def keyboard(task: dict[str, Any]) -> dict[str, Any]:
    """The employee's buttons for the task's state."""
    task_id = str(task["id"])
    status = task.get("status") or "sent"
    if task.get("awaiting"):
        return {"inline_keyboard": [[{"text": "↩️ Бекор қилиш", "callback_data": f"taskx:{task_id}"}]]}
    done = {"text": "✅ Бажардим", "callback_data": f"taskdone:{task_id}"}
    block = {"text": "⛔ Тўсиқ бор", "callback_data": f"taskblock:{task_id}"}
    # A new deadline can be asked when there is one and no answer to an earlier ask is pending (2026-11-10).
    later = ([[{"text": "📅 Муддатни узайтириш", "callback_data": f"taskres:{task_id}"}]]
             if task.get("due_date") and not task.get("proposed_due") else [])
    if status == "sent":
        return {"inline_keyboard": [[{"text": "▶️ Қабул қилдим", "callback_data": f"taskstart:{task_id}"}], [block, done]]
                + later}
    if status == "started":
        return {"inline_keyboard": [[block, done]] + later}
    if status == "blocked":
        return {"inline_keyboard": [[{"text": "▶️ Давом эттирдим", "callback_data": f"taskgo:{task_id}"}, done]] + later}
    return {"inline_keyboard": []}


def new_due(due: Any, today: Any, days: int) -> Any:
    """The deadline asked for: ``days`` after the current one, or after today when it has already passed."""
    from datetime import timedelta

    return max(due or today, today) + timedelta(days=days)


def reschedule_choices(task: dict[str, Any], today: Any) -> tuple[str, dict[str, Any]]:
    """«Қачонгача?» and the three dates as buttons (+ cancel)."""
    task_id = str(task["id"])
    buttons = [{"text": f"{new_due(task.get('due_date'), today, d):%d.%m}" + (" (+1 ҳафта)" if d == 7 else f" (+{d} кун)"),
                "callback_data": f"tdp:{task_id}:{d}"} for d in RESCHEDULE_DAYS]
    text = (f"📅 <b>{escape(code_of(task))}</b> — ҳозирги муддат {task['due_date']:%d.%m}. Қачонгача узайтирилсин?"
            if task.get("due_date") else f"📅 <b>{escape(code_of(task))}</b> — қачонгача?")
    return text, {"inline_keyboard": [buttons, [{"text": "↩️ Бекор", "callback_data": f"tdpx:{task_id}"}]]}


def reschedule_request_text(task: dict[str, Any], who: str, reason: str) -> str:
    """To the one who assigned it: who asks, from which date to which, why; the second ask is marked."""
    times = int(task.get("reschedules") or 0)
    again = f"\n⚠️ Бу топшириқ муддати аввал {times} марта узайтирилган." if times else ""
    return (f"📅 <b>{escape(code_of(task))} — муддатни узайтириш сўралди</b>\n{sanitize_model_html(task['task_summary'])}"
            f"\n— {escape(who)}\n\n{task['due_date']:%d.%m} → <b>{task['proposed_due']:%d.%m}</b>\n<b>Сабаби:</b> "
            f"{escape(reason)}{again}")


def reschedule_keyboard(task_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ Розиман", "callback_data": f"tdpa:{task_id}"},
                                 {"text": "❌ Йўқ, муддат ўзгармайди", "callback_data": f"tdpr:{task_id}"}]]}


def review_keyboard(task_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ Қабул", "callback_data": f"taskok:{task_id}"},
                                 {"text": "↩️ Қайтариш", "callback_data": f"taskback:{task_id}"}]]}


def evidence_of(message: dict[str, Any]) -> tuple[str, bool] | None:
    """(the proof as text, has a file) from an employee's message; None if it's empty."""
    text = (message.get("text") or message.get("caption") or "").strip()
    has_file = any(message.get(f) for f in ("photo", "video", "document", "voice", "audio", "animation", "video_note"))
    if not text and not has_file:
        return None
    if not text:
        kind = next(f for f in ("photo", "video", "document", "voice", "audio", "animation", "video_note") if message.get(f))
        text = {"photo": "фото", "video": "видео", "document": "файл", "voice": "овозли хабар",
                "audio": "аудио", "animation": "анимация", "video_note": "видео хабар"}[kind]
    return text[:EVIDENCE_MAX], has_file


def review_text(task: dict[str, Any], who: str) -> str:
    """The card the one who assigned it gets: what, who, the proof — accept or return."""
    return (f"🔎 <b>{escape(code_of(task))} — текширувда</b>\n{sanitize_model_html(task['task_summary'])}\n"
            f"— {escape(who)}\n\n<b>Далил:</b> {escape(str(task.get('evidence_text') or ''))}")


def review_caption(task: dict[str, Any], who: str, status: str = "") -> str:
    """``review_text`` as a file's caption: within 1024 characters, the proof shortened, never cut mid-tag."""
    head = (f"🔎 <b>{escape(code_of(task))} — текширувда</b>\n— {escape(who)}\n"
            + (f"{status}\n" if status else "") + "\n<b>Далил:</b> ")
    return fit(head, str(task.get("evidence_text") or ""))


def shared_line(others: list[dict[str, Any]]) -> str:
    """On the assigner's review card when the task went to several people (2026-11-09): accepting closes theirs too."""
    if not others:
        return ""
    names = ", ".join(escape(str(o.get("display_name") or "")) for o in others)
    return f"👥 Умумий топшириқ — қабул қилсангиз {names} нусхаси ҳам ёпилади."


def blocked_text(task: dict[str, Any], who: str) -> str:
    return (f"⛔ <b>{escape(code_of(task))} — тўсиқ бор</b>\n{sanitize_model_html(task['task_summary'])}\n"
            f"— {escape(who)}\n\n<b>Сабаби:</b> {escape(str(task.get('blocker') or ''))}")


def answer_line(task: dict[str, Any]) -> str:
    """One task for the Director's questions (``topshiriqlar``): code, state, blocker."""
    status = task.get("status") or "sent"
    line = f"{code_of(task)} [{STATUS_LABELS.get(status, status)}]"
    if status == "blocked" and task.get("blocker"):
        line += f" (тўсиқ: {task['blocker']})"
    return line


# ------------------------------------------------- the same task again (2026-10-17)

# The owner: a task the Director gives again while it is still open isn't added a second time —
# the person is reminded on the card they already have. This close counts as the same task.
SAME_TASK_RATIO = 0.8


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"<[^>]+>|[^\w\s]", " ", (text or "").lower()).split())


def same_task(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two tasks are the same instruction: their summaries (or the Director's words) nearly equal."""
    from difflib import SequenceMatcher

    for key in ("task_summary", "raw_message"):
        x, y = _norm(a.get(key)), _norm(b.get(key))
        if x and y and (x == y or SequenceMatcher(None, x, y).ratio() >= SAME_TASK_RATIO):
            return True
    return False


def repeated_text(task: dict[str, Any]) -> str:
    """The reminder on the existing card when the Director gives the same task again."""
    return (f"🔁 Директор бу топшириқни яна берди — {code_of(task)} ҳали тугамаган. "
            "Илтимос, бажариб, «✅ Бажардим» тугмасини босинг.")
