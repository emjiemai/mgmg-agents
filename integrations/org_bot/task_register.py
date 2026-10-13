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
- ↩️ Қайтариш sends it back to ``started`` — the employee does it again and
  sends new proof;
- every step is in ``task_events`` (who, when, the blocker or proof text).

Pure helpers here (tested offline); ``ops_manager`` runs the flow.
"""

from __future__ import annotations

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
                "(нима қилинди, натижа). Далилсиз топшириқ ёпилмайди.")
    elif task.get("awaiting") == "blocker":
        line = "✍️ <b>Тўсиқ нимада?</b> Қисқа ёзинг — топшириқ берган кишига етказилади."
    return line


def keyboard(task: dict[str, Any]) -> dict[str, Any]:
    """The employee's buttons for the task's state."""
    task_id = str(task["id"])
    status = task.get("status") or "sent"
    if task.get("awaiting"):
        return {"inline_keyboard": [[{"text": "↩️ Бекор қилиш", "callback_data": f"taskx:{task_id}"}]]}
    done = {"text": "✅ Бажардим", "callback_data": f"taskdone:{task_id}"}
    block = {"text": "⛔ Тўсиқ бор", "callback_data": f"taskblock:{task_id}"}
    if status == "sent":
        return {"inline_keyboard": [[{"text": "▶️ Қабул қилдим", "callback_data": f"taskstart:{task_id}"}], [block, done]]}
    if status == "started":
        return {"inline_keyboard": [[block, done]]}
    if status == "blocked":
        return {"inline_keyboard": [[{"text": "▶️ Давом эттирдим", "callback_data": f"taskgo:{task_id}"}, done]]}
    return {"inline_keyboard": []}


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
