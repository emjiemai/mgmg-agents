"""Team cheer — three friendly messages a working day, through OPS Manager Bot.

Work runs 09:00–18:00 Tashkent. The owner asked (2026-09-29) for a little
encouragement or a joke at the start, in the middle and at the end of it:

    10:00  morning  encouragement for the day
    14:00  midday   a clean joke or a fun fact, with a fun question to tap
    17:35  evening  thanks for the day, with "how was it?" to tap

Everyone gets it except the Director — the same people the daily reports ask
(Saturday/Sunday only the weekend workers).

The AI writes each slot once a day (the same text for everyone, the
person's name added in front); anything that breaks the rules below — not
Uzbek Cyrillic, too long, badly formed — is thrown away and a hand-written
message from this file goes out instead. Nobody ever gets nothing, and
nobody gets something odd.

Questions are answered with buttons, not by typing. At 17:35 most people
still have today's report open, and a typed "my day was great" would be
filed as their report or offered to the Director; a tap can't be. A typed
reply to one of these messages is caught too (``ops_manager``) and gets a
friendly word back instead. Taps are stored only to allow one per person;
they are not reported to anyone — a mood question the boss reads is no
longer a friendly question.

Pure logic here (tested offline); ``agents/team-cheer/agent.py`` sends.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from integrations.telegram.bot import escape

AGENT = "team-cheer"

# Slot -> the moment it goes out (Tashkent). render.yaml's one cron service
# runs at :00 and :35 of 10, 14 and 17; each run sends the slot whose time has
# just passed and skips the rest (10:35, 14:35 and 17:00 find nothing).
SLOTS: dict[str, time] = {
    "morning": time(10, 0),
    "midday": time(14, 0),
    "evening": time(17, 35),
}
# How late a run may still send its slot (cron can start a few minutes late);
# shorter than the 35 minutes to the next run, so that run can't pick it up.
SLOT_WINDOW_MINUTES = 25

TEXT_MAX = 400
QUESTION_MAX = 110
LABEL_MAX = 28  # a Telegram button shows about this much on a phone
REPLY_MAX = 160


@dataclass
class Cheer:
    """One slot's message: the text, and (midday/evening) a question with tap answers."""

    text: str
    question: str | None = None
    options: list[dict[str, str]] = field(default_factory=list)  # [{"label", "reply"}]
    source: str = "fallback"


def due_slot(now_local: datetime) -> str | None:
    """The slot to send at this moment, if any.

    Args:
        now_local: Current time in Tashkent.
    """
    minutes_now = now_local.hour * 60 + now_local.minute
    for slot, at in SLOTS.items():
        start = at.hour * 60 + at.minute
        if start <= minutes_now < start + SLOT_WINDOW_MINUTES:
            return slot
    return None


# ------------------------------------------------------------------ the AI

_THEMES = {
    "morning": (
        "кичик қадамлар катта натижа беради", "жамоада ишлашнинг кучи", "ўзига ишонч", "яхши кайфият",
        "янги нарса ўрганиш", "сабр ва изчиллик", "ҳамкасбга илиқ сўз", "ишни охирига етказиш қувончи",
    ),
    "midday": (
        "тушликдан кейинги уйқучанлик ҳақида енгил ҳазил", "чой ёки қаҳва", "қизиқ ва тўғри факт",
        "дам олиш куни режалари", "севимли таом", "офис ҳаётидан беозор ҳазил", "энергия даражаси",
    ),
    "evening": (
        "бугунги кун қандай ўтди", "бугун нима хурсанд қилди", "кечки режалар", "бугунги кичик ютуқ",
        "эртанги кунга кайфият", "оила ва дам олиш",
    ),
}

_SLOT_TASK = {
    "morning": (
        "Write a short encouraging message for the start of the working day (it is 10:00, work began at "
        "09:00). No question: set \"question\" to null and \"options\" to []."
    ),
    "midday": (
        "It is 14:00, just after lunch. Write either one short, clean, genuinely funny joke, or one "
        "interesting fact you are CERTAIN is true, to give people a smile. Then a light fun question with "
        "2-4 tap answers (e.g. tea or coffee, favourite season), each with a warm one-line reply."
    ),
    "evening": (
        "It is 17:35; the working day ends at 18:00. Thank people for today's work and wish them a good "
        "evening. Then a question that makes people feel good about their day (how did it go, what made you "
        "happy, evening plans) with 2-4 tap answers, each with a warm one-line reply. Every answer, "
        "including a tired or so-so one, gets a kind reply — never a lecture."
    ),
}

SYSTEM_PROMPT = f"""\
You write short friendly messages that a company's Telegram bot sends to its
employees (MGMG, Tashkent: an industrial laundry equipment business and a
Garmin watch shop) to cheer them up during the working day.

Rules — a message that breaks one is thrown away:
- Uzbek, CYRILLIC script only (ў, қ, ғ, ҳ). No Latin letters at all, not even
  in brand names. Emoji are welcome, one to three.
- Warm, respectful, simple. Addressed to everyone ("сиз"), never to one person.
  Do not include a greeting or a name — the bot adds "Хайрли кун, <name>!".
- Never: politics, religion, ethnicity, gender, appearance, age, health,
  alcohol, money or bonuses, promises of any kind, sarcasm about work, the
  boss or customers, anything that could embarrass or mock anyone.
- A joke must be clean and understandable to everyone. A fact must be true.
- "text" at most {TEXT_MAX} characters; "question" at most {QUESTION_MAX};
  each answer "label" at most {LABEL_MAX} (it is a button); each "reply" at
  most {REPLY_MAX}.
- Do not repeat or closely paraphrase any of the RECENT MESSAGES given.

Answer with JSON only:
{{"text": "...", "question": "..." or null, "options": [{{"label": "...", "reply": "..."}}]}}
"""


def user_prompt(slot: str, day: date, recent: list[str]) -> str:
    """What the AI is asked for this slot today."""
    themes = _THEMES[slot]
    theme = themes[day.toordinal() % len(themes)]
    lines = [_SLOT_TASK[slot], f"Today's theme idea (optional): {theme}.", "", "RECENT MESSAGES (do not repeat):"]
    lines += [f"- {r}" for r in recent[:30]] or ["- (none yet)"]
    return "\n".join(lines)


_LATIN = re.compile(r"[A-Za-z]")
_MARKUP = re.compile(r"[<>]")


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or _LATIN.search(value) or _MARKUP.search(value):
        return None
    return value


def parse_ai(raw: str, slot: str) -> Cheer | None:
    """The AI's answer as a Cheer, or None if it breaks any rule (then the built-in list is used)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = _clean(data.get("text"), TEXT_MAX)
    if text is None:
        return None
    if slot == "morning":
        return Cheer(text=text, source="ai")

    question = _clean(data.get("question"), QUESTION_MAX)
    options = data.get("options")
    if question is None or not isinstance(options, list) or not 2 <= len(options) <= 4:
        return None
    cleaned = []
    for option in options:
        if not isinstance(option, dict):
            return None
        label, reply = _clean(option.get("label"), LABEL_MAX), _clean(option.get("reply"), REPLY_MAX)
        if label is None or reply is None:
            return None
        cleaned.append({"label": label, "reply": reply})
    if len({o["label"] for o in cleaned}) != len(cleaned):
        return None
    return Cheer(text=text, question=question, options=cleaned, source="ai")


# --------------------------------------------- the built-in list (fallback)

_MORNING = (
    "Бугун кичик бир қадам бўлса ҳам олдинга юрсак — бу ҳам ғалаба. Омад сизга! 💪",
    "Яхши кайфият — ишнинг ярми. Бугун ҳам зўр кун бўлади! ☀️",
    "Ҳар бир мураккаб иш кичик қисмлардан иборат. Биринчисидан бошланг — қолгани ўзи келади. 🚀",
    "Сизнинг меҳнатингиз жамоа учун муҳим. Бугун ҳам бирга яхши натижага эришамиз! 🤝",
    "Бир пиёла чой, бир табассум — ва кунни бошлашга тайёрмиз! ☕",
    "Хато қилишдан қўрқманг: ҳаракат қилмаган одамгина хато қилмайди. Олға! ✨",
    "Бугунги мақсад: битта ишни охирига етказиш ва ўзингиздан хурсанд бўлиш. Сиз уддалайсиз! 🎯",
    "Энг яхши ишлар шошилмасдан, лекин тўхтамасдан қилинади. Унумли кун тилаймиз! 🌱",
    "Ҳамкасбингизга бугун бир илиқ сўз айтинг — яхши кайфият юқумли бўлади. 😊",
    "Кеча қийин бўлган бўлса, бугун — янги имконият. Омад! 🌟",
)

_TIRED_OK = "Яхшилаб дам олинг — сиз бунга лойиқсиз! 🌙"

_MIDDAY: tuple[Cheer, ...] = (
    Cheer(
        "Тушликдан кейин энг оғир иш — кўзни очиқ тутиш. 😄 Бир стакан сув ичиб, бир оз юриб келинг!",
        "Ҳозир нима кўпроқ ёрдам берарди?",
        [
            {"label": "☕ Чой ёки қаҳва", "reply": "Демак, бир пиёла — ва яна олға! ☕"},
            {"label": "🚶 Бир оз юриш", "reply": "Беш дақиқа юриш — мияга энг яхши совға! 🚶"},
            {"label": "🎵 Яхши мусиқа", "reply": "Севимли қўшиғингизни эшитинг — кайфият кўтарилади! 🎵"},
        ],
    ),
    Cheer(
        "Иш куни ярмидан ошди — сиз аллақачон ярим йўлни босиб ўтдингиз! 🏁",
        "Ҳозирги энергиянгиз қанча?",
        [
            {"label": "🔋 Тўла", "reply": "Зўр! Шу кучни кечгача сақланг! ⚡"},
            {"label": "🔋 Ярим", "reply": "Ярим — бу ҳам кўп! Бир пиёла чой қолганини тўлдиради. ☕"},
            {"label": "🪫 Чой керак", "reply": "Унда ҳозироқ чой дамланг — сиз бунга лойиқсиз! 🍵"},
        ],
    ),
    Cheer(
        "Кичик маслаҳат: йигирма дақиқа ишлагач, йигирма сония узоққа қаранг — кўзларингиз раҳмат айтади. 👀",
        "Қайси фасл сизга кўпроқ ёқади?",
        [
            {"label": "🌸 Баҳор", "reply": "Баҳор — янгиланиш фасли! 🌸"},
            {"label": "☀️ Ёз", "reply": "Ёз — қуёш ва мева фасли! ☀️"},
            {"label": "🍂 Куз", "reply": "Куз — ҳосил ва олтин барглар! 🍂"},
            {"label": "❄️ Қиш", "reply": "Қиш — иссиқ чой ва илиқ суҳбатлар! ❄️"},
        ],
    ),
    Cheer(
        "Бир пиёла чой — энг яхши «қайта юклаш». 🍵",
        "Сиз қайси жамоадансиз?",
        [
            {"label": "🍵 Кўк чой", "reply": "Классика! Кўк чой — ҳар доим ўз ўрнида. 🍵"},
            {"label": "🫖 Қора чой", "reply": "Қора чой — кучли танлов! 🫖"},
            {"label": "☕ Қаҳва", "reply": "Қаҳва жамоаси ҳам бор экан! ☕"},
        ],
    ),
    Cheer(
        "Ишдаги энг яхши дори — ҳамкасбнинг табассуми. Бугун кимгадир табассум ҳадя қилинг! 😊",
        "Дам олиш кунини қандай ўтказишни ёқтирасиз?",
        [
            {"label": "🏡 Оила билан", "reply": "Оила билан ўтган вақт — энг қимматли вақт! 🏡"},
            {"label": "🌳 Табиатда", "reply": "Тоза ҳаво — энг яхши дам! 🌳"},
            {"label": "🛋 Уйда дам олиб", "reply": "Баъзан энг яхши режа — ҳеч қандай режа йўқлиги! 😌"},
            {"label": "⚽ Спорт билан", "reply": "Зўр! Соғлом тана — соғлом фикр! ⚽"},
        ],
    ),
    Cheer(
        "Компьютер: «Янгиланиш бир дақиқа давом этади». Биз: чой дамлашга улгурамиз. 😄",
        "Энг севимли таомингиз қайси?",
        [
            {"label": "🍚 Палов", "reply": "Палов — ҳар доим тўғри жавоб! 🍚"},
            {"label": "🥟 Сомса", "reply": "Иссиқ сомса — кайфият кафолати! 🥟"},
            {"label": "🍜 Лағмон", "reply": "Лағмон ишқибозлари, салом! 🍜"},
            {"label": "🥗 Бошқа нарса", "reply": "Демак, сизнинг ўз севимли таомингиз бор — зўр! 😋"},
        ],
    ),
)

_EVENING_TEXTS = (
    "Бугунги меҳнатингиз учун раҳмат! Ишни яхши якунлаб, уйга яхши кайфиятда боринг. 🌇",
    "Яна бир кун ортда қолди — сиз кўп иш қилдингиз. Ўзингизни мақтаб қўйинг! 👏",
    "Кун охирига оз қолди. Бугунги энг яхши ишингизни эслаб, бир табассум қилинг. 😊",
    "Раҳмат, бугун ҳам жамоага катта ҳисса қўшдингиз. Хайрли кеч! 🏡",
    "Чарчаган бўлсангиз ҳам — сиз бугун олдинга юрдингиз. Яхши дам олинг! 🌙",
    "Бугунги кичик ютуқлар — эртанги катта натижаларнинг пойдевори. Раҳмат! 🧱",
)

_EVENING_QUESTIONS: tuple[tuple[str, list[dict[str, str]]], ...] = (
    (
        "Бугунги кунингиз қандай ўтди?",
        [
            {"label": "😄 Зўр", "reply": "Ажойиб! Шу кайфиятни эртага ҳам олиб келинг! 😄"},
            {"label": "🙂 Яхши", "reply": "Яхши кун — яхши иш белгиси. Раҳмат! 🙂"},
            {"label": "😐 Ўртача", "reply": "Ҳар кун ҳам бир хил бўлмайди — эртага яхшироқ бўлади! 💪"},
            {"label": "😴 Чарчадим", "reply": _TIRED_OK},
        ],
    ),
    (
        "Бугун сизни нима хурсанд қилди?",
        [
            {"label": "🏆 Ишдаги натижа", "reply": "Натижа — меҳнатнинг энг ширин меваси! 🏆"},
            {"label": "🤝 Ҳамкасбларим", "reply": "Яхши жамоа — катта бойлик! 🤝"},
            {"label": "🌟 Мижоз раҳмати", "reply": "Мижознинг раҳмати — энг яхши баҳо! 🌟"},
            {"label": "🎉 Кечки режам", "reply": "Кечки режангиз зўр ўтсин! 🎉"},
        ],
    ),
    (
        "Кечқурун нима қилмоқчисиз?",
        [
            {"label": "🏡 Оила билан", "reply": "Оила билан ўтган вақт — энг қимматли вақт! 🏡"},
            {"label": "🏃 Спорт", "reply": "Зўр танлов — соғлом тана, соғлом фикр! 🏃"},
            {"label": "😌 Дам олиш", "reply": "Яхшилаб дам олинг — эртага янги куч билан! 😌"},
            {"label": "🤷 Ҳали билмайман", "reply": "Сюрпризли кеч ҳам яхши! 😄"},
        ],
    ),
)


def fallback(slot: str, day: date) -> Cheer:
    """A hand-written message for ``slot``, rotating by date so days differ."""
    n = day.toordinal()
    if slot == "morning":
        return Cheer(_MORNING[n % len(_MORNING)])
    if slot == "midday":
        pick = _MIDDAY[n % len(_MIDDAY)]
        return Cheer(pick.text, pick.question, [dict(o) for o in pick.options])
    question, options = _EVENING_QUESTIONS[n % len(_EVENING_QUESTIONS)]
    return Cheer(_EVENING_TEXTS[n % len(_EVENING_TEXTS)], question, [dict(o) for o in options])


# ------------------------------------------------------- what people see

_HEADERS = {
    "morning": "☀️ <b>Хайрли кун, {name}!</b>",
    "midday": "😄 <b>{name}, бир дақиқалик танаффус!</b>",
    "evening": "🌇 <b>{name}, иш куни якунланяпти!</b>",
}


def message_text(slot: str, cheer: Cheer, name: str) -> str:
    """The Telegram HTML one person gets."""
    parts = [_HEADERS[slot].format(name=escape(name)), escape(cheer.text)]
    if cheer.question:
        parts.append(f"<b>{escape(cheer.question)}</b>")
    return "\n\n".join(parts)


def keyboard(cheer_id: str, cheer: Cheer) -> dict[str, Any] | None:
    """Tap answers, two to a row; None when there is no question."""
    if not cheer.options:
        return None
    buttons = [{"text": o["label"], "callback_data": f"cheer:{cheer_id}:{i}"} for i, o in enumerate(cheer.options)]
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def parse_callback(rest: str) -> tuple[str, int] | None:
    """``"<cheer id>:<answer index>"`` from a ``cheer:`` button, or None."""
    cheer_id, _, index = rest.rpartition(":")
    if not cheer_id or not index.isdigit():
        return None
    return cheer_id, int(index)


def answered_text(sent_text: str, option: dict[str, str]) -> str:
    """The message after a tap: the answer and its reply in place of the buttons."""
    return f"{sent_text}\n\n✅ <i>{escape(option['label'])}</i>\n{escape(option['reply'])}"


_TEXT_REPLIES = (
    "😊 Раҳмат, бўлишганингиз учун!",
    "🤗 Раҳмат! Кайфиятингиз доим яхши бўлсин!",
    "😊 Ёзганингиз учун раҳмат!",
)


def text_reply(message_id: int) -> str:
    """A friendly word back when someone types a reply to a cheer message."""
    return _TEXT_REPLIES[message_id % len(_TEXT_REPLIES)]
