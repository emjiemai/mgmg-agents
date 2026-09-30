"""Team cheer — three friendly messages a working day, through OPS Manager Bot.

Work runs 09:00–18:00 Tashkent. The owner asked (2026-09-29) for a little
encouragement or a joke at the start, in the middle and at the end of it:

    10:00  morning  a warm wish for the day
    14:00  midday   an Afandi story in one sentence ("қисқаси, афанди бир куни…")
    17:35  evening  a question about their day, to tap

Everyone gets it except the Director — the same people the daily reports ask
(Saturday/Sunday only the weekend workers).

The voice (2026-09-30, ``tone.py``): employees felt the first version — bold
header, emoji, two paragraphs — as one more notification to deal with. Each
message is now one short sentence after the person's name (with ака/опа when
the admin set it), lowercase, one emoji at the very end, sent silently (no
sound): "алишер ака, бугун ҳам зўр кун бўлсин ☀️". The midday joke is Uzbek
traditional humour — a classic Afandi latifa told in one sentence.

The AI writes each slot once a day (the same sentence for everyone); anything
that isn't one short Uzbek Cyrillic sentence is thrown away and a
hand-written one from this file goes out instead. Nobody ever gets nothing,
and nobody gets something odd.

Questions are answered with buttons, not by typing. At 17:35 most people
still have today's report open, and a typed "my day was great" would be
filed as their report or offered to the Director; a tap can't be. The warm
reply to a tap shows as a brief pop-up, not another message. A typed reply
to one of these messages is caught too (``ops_manager``). Taps are stored
only to allow one per person; they are not reported to anyone — a mood
question the boss reads is no longer a friendly question.

Pure logic here (tested offline); ``agents/team-cheer/agent.py`` sends.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from integrations.org_bot.tone import casual, is_emoji, is_one_short_sentence
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

TEXT_MAX = 80  # the sentence after "<name>, "
JOKE_MAX = 230  # an Afandi latifa needs its punchline, still one sentence
LABEL_MAX = 20  # a button
REPLY_MAX = 70  # the pop-up after a tap
OPTIONS_MAX = 4


@dataclass
class Cheer:
    """One slot's sentence, and tap answers when it asks something."""

    text: str
    options: list[dict[str, str]] = field(default_factory=list)  # [{"label", "reply"}]
    emoji: str = ""  # the one emoji at the end
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
        "кичик қадамлар", "жамоа", "ўзига ишонч", "яхши кайфият", "янги нарса ўрганиш", "сабр",
        "ҳамкасбга илиқ сўз", "ишни охирига етказиш",
    ),
    "midday": (
        "афанди ва эшаги", "афанди ва қўшниси", "афанди бозорда", "афанди тўйда", "афанди ва ой",
        "афанди ва қозон", "афанди ва шогирдлари",
    ),
    "evening": (
        "бугунги кун қандай ўтди", "бугун нима хурсанд қилди", "кечки режалар", "бугунги кичик ютуқ",
        "эртанги кайфият",
    ),
}

_SLOT_TASK = {
    "morning": "It is 10:00, work began at 09:00. Write a warm wish or a little encouragement for the day. No answers: \"options\": [].",
    "midday": (
        "It is 14:00, just after lunch. Retell ONE classic, clean Afandi (Nasriddin Afandi) latifa — Uzbek "
        "traditional humour — as ONE sentence that starts with \"қисқаси, афанди бир куни\" and ends with his "
        "punchline in «…» + \"дебди\". Only kind, well-known latifas: nothing about religion, money, wives, "
        "drinking or anyone's looks. At most " + str(JOKE_MAX) + " characters. No answers: \"options\": []."
    ),
    "evening": (
        "It is 17:35; the day ends at 18:00. Ask how their day went, what made them happy, or their evening "
        "plans, with 2-4 short answers, each with a warm reply. A tired or so-so answer gets kindness, never "
        "a lecture."
    ),
}

SYSTEM_PROMPT = f"""\
You write one friendly line that a company's Telegram bot sends to its
employees (MGMG, Tashkent) to make them smile during the working day. They
must not feel it as a notification or a task — it should read like a warm
colleague texting.

The bot writes the person's name and a comma first ("алишер, "), then your
sentence. So write ONE short sentence that follows naturally after a name:
- Uzbek, Cyrillic script only, all lowercase, no emoji, no line breaks, no
  greeting, no name, no exclamation marks in a row. At most {TEXT_MAX}
  characters — around eight to twelve words.
- Warm and simple. Never: politics, religion, ethnicity, gender, appearance,
  age, health, alcohol, money, promises, sarcasm about work, the boss or
  customers, anything that could embarrass anyone.
- Each answer "label" is one to three words (at most {LABEL_MAX} characters),
  lowercase; each "reply" is one short warm sentence (at most {REPLY_MAX}).
- "emoji": exactly one emoji that fits the sentence; the bot puts it at the
  very end. No other emoji anywhere.
- Do not repeat or closely paraphrase any of the RECENT MESSAGES given (for
  the midday latifa: a different latifa).

Answer with JSON only: {{"text": "...", "emoji": "...", "options": [{{"label": "...", "reply": "..."}}]}}
"""


def user_prompt(slot: str, day: date, recent: list[str]) -> str:
    """What the AI is asked for this slot today."""
    themes = _THEMES[slot]
    theme = themes[day.toordinal() % len(themes)]
    lines = [_SLOT_TASK[slot], f"Today's theme idea (optional): {theme}.", "", "RECENT MESSAGES (do not repeat):"]
    lines += [f"- {r}" for r in recent[:30]] or ["- (none yet)"]
    return "\n".join(lines)


_LATIN = re.compile(r"[A-Za-z]")
_MARKUP = re.compile(r"[<>&]")


def _line(value: Any, limit: int) -> str | None:
    """``value`` as one short casual line, or None if it can't be one."""
    if not isinstance(value, str) or _MARKUP.search(value):
        return None
    line = casual(value)
    if _LATIN.search(line) or not is_one_short_sentence(line, limit):
        return None
    return line


def parse_ai(raw: str, slot: str) -> Cheer | None:
    """The AI's answer as a Cheer, or None if it breaks any rule (then the built-in list is used).

    Emoji, capitals and a closing full stop are simply removed; anything
    else wrong — Latin, two sentences, too long, bad answers — rejects it.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = _line(data.get("text"), JOKE_MAX if slot == "midday" else TEXT_MAX)
    if text is None:
        return None
    emoji = str(data.get("emoji") or "").strip()
    emoji = emoji if is_emoji(emoji) else DEFAULT_EMOJI[slot]
    options = data.get("options") or []
    if slot == "morning":
        return Cheer(text=text, emoji=emoji, source="ai")
    if slot == "midday":
        # The latifa is the whole message: it must be one, and there's nothing to tap.
        if not text.startswith("қисқаси, афанди") or "«" not in text or options:
            return None
        return Cheer(text=text, emoji=emoji, source="ai")
    if not isinstance(options, list) or not 2 <= len(options) <= OPTIONS_MAX:
        return None
    cleaned = []
    for option in options:
        if not isinstance(option, dict):
            return None
        label, reply = _line(option.get("label"), LABEL_MAX), _line(option.get("reply"), REPLY_MAX)
        if label is None or reply is None:
            return None
        cleaned.append({"label": label, "reply": reply})
    if len({o["label"] for o in cleaned}) != len(cleaned):
        return None
    return Cheer(text=text, options=cleaned, emoji=emoji, source="ai")


# --------------------------------------------- the built-in list (fallback)

DEFAULT_EMOJI = {"morning": "☀️", "midday": "😄", "evening": "🌙"}

_MORNING: tuple[tuple[str, str], ...] = (
    ("бугун ҳам зўр кун бўлсин", "☀️"),
    ("бугун ишларингиз осон битсин", "🍀"),
    ("яхши кайфият билан бошланган кун яхши тугайди", "😊"),
    ("бир пиёла чой ичиб, кунни хотиржам бошланг", "🍵"),
    ("бугунги биринчи ишингиз омадли чиқсин", "🍀"),
    ("ўзингизга ишонинг, бугун ҳаммаси яхши бўлади", "💪"),
    ("шошилмасдан ишланг, бугун ҳаммасига улгурасиз", "🙂"),
    ("табассум билан бошланг, қолгани ўзи келади", "😊"),
    ("бугун кимгадир илиқ сўз айтинг, кайфият юқумли бўлади", "🤝"),
    ("кеча қийин бўлган бўлса, бугун янги имконият", "🌱"),
)

# Classic Afandi latifas, each told in one sentence with its punchline.
_MIDDAY: tuple[str, ...] = (
    "қисқаси, афанди бир куни тўйга эски тўнда борса ҳеч ким қарамабди, янги тўн кийиб келса тўрга "
    "ўтқазишибди, шунда афанди енгини ошга тутиб «е, тўним, е, ҳурмат сенга экан» дебди",
    "қисқаси, афанди бир куни узугини уйда йўқотиб кўчада қидираётган экан, сабабини сўрашса «уйда "
    "қоронғи, бу ер ёруғ-да» дебди",
    "қисқаси, афанди бир куни эшакка миниб, қопни ўз елкасига олиб кетаётган экан, сўрашса «эшагим "
    "қийналмасин, юкни ўзим кўтардим» дебди",
    "қисқаси, афанди бир куни қудуққа қараса ой тушиб қолибди, арқон ташлаб тортаман деб чалқанча "
    "йиқилибди-да, осмондаги ойни кўриб «ишқилиб, чиқариб олдим» дебди",
    "қисқаси, афанди бир куни эшак сўраб келган қўшнисига «эшак уйда йўқ» дебди, шу пайт эшак ҳанграб "
    "юборибди, қўшни «мана-ку» деса, афанди «менгами ишонасан, эшакками?» дебди",
    "қисқаси, афанди бир куни қўшнисининг қозонини ичига қозонча солиб «қозонингиз туғди» деб "
    "қайтарибди, кейинги сафар «қозонингиз ўлди» дебди, «қозон ҳам ўладими?» деса, «туғишига "
    "ишондингиз-ку» дебди",
)


def _options(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"label": label, "reply": reply} for label, reply in pairs]


_EVENING: tuple[Cheer, ...] = (
    Cheer("ишларингиз билан чарчамадингизми?", _options(
        ("йўқ, зўр", "зўр, шу кайфиятда қолинг"),
        ("бир оз", "уйда яхшилаб дам олинг, чарчаманг"),
        ("жуда", "раҳмат каттакон, бугун кўп ишладингиз, яхши дам олинг"),
    ), emoji="🌙"),
    Cheer("бугун кунингиз қандай ўтди?", _options(
        ("зўр", "ажойиб, эртага ҳам шундай бўлсин"),
        ("яхши", "раҳмат каттакон, меҳнатингиз учун"),
        ("ўртача", "эртага албатта яхшироқ бўлади"),
        ("чарчадим", "яхшилаб дам олинг, бунга лойиқсиз"),
    ), emoji="🌇"),
    Cheer("бугун сизни нима хурсанд қилди?", _options(
        ("иш натижаси", "натижа меҳнатнинг энг ширин меваси"),
        ("ҳамкасблар", "яхши жамоа катта бойлик"),
        ("мижоз раҳмати", "мижоз раҳмати энг яхши баҳо"),
        ("ҳали ҳеч нарса", "унда кечки вақтингиз хурсанд қилсин"),
    ), emoji="😊"),
    Cheer("кечқурун нима режа?", _options(
        ("оила билан", "оила билан вақт энг қимматли вақт"),
        ("спорт", "зўр танлов, соғлом бўлинг"),
        ("дам олиш", "яхшилаб дам олинг, эртага янги куч билан"),
    ), emoji="🌙"),
)


def fallback(slot: str, day: date) -> Cheer:
    """A hand-written message for ``slot``, rotating by date so days differ."""
    n = day.toordinal()
    if slot == "morning":
        text, emoji = _MORNING[n % len(_MORNING)]
        return Cheer(text, emoji=emoji)
    if slot == "midday":
        return Cheer(_MIDDAY[n % len(_MIDDAY)], emoji=DEFAULT_EMOJI["midday"])
    pick = _EVENING[n % len(_EVENING)]
    return Cheer(pick.text, [dict(o) for o in pick.options], emoji=pick.emoji)


# ------------------------------------------------------- what people see


def message_text(cheer: Cheer, name: str) -> str:
    """The one line a person gets: how to call them, a comma, the sentence, one emoji."""
    return casual(f"{escape(name)}, {cheer.text}", cheer.emoji)


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


_TEXT_REPLIES = (
    ("раҳмат, ёзганингиз учун хурсандман", "😊"),
    ("раҳмат, кайфиятингиз доим яхши бўлсин", "🌸"),
    ("раҳмат каттакон, бўлишганингиз учун", "🤗"),
)


def text_reply(message_id: int) -> str:
    """A friendly word back when someone types a reply to a cheer message."""
    text, emoji = _TEXT_REPLIES[message_id % len(_TEXT_REPLIES)]
    return casual(text, emoji)
