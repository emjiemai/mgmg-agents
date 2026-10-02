"""The friendly voice for messages employees get without asking.

2026-09-30, from the owner: employees felt the bot's cheer, motivation and
report messages as alarms — a bold header, an emoji, two paragraphs, just
like a task. These now read like a colleague texting: one short sentence,
lowercase, no line breaks, no bold, and at most one emoji — at the very end.
Tasks keep their card format; they are work, not chat.

2026-10-01, from the owner: names always keep their capital letter
("Алишер ака", «Hyatt Regency») — lowercase is for the sentence,
never for a person or a company. And the bot is always polite: the
respectful "сиз", never "сен" or its verb forms, and extra courtesy to women
(опа) — Uzbek tradition.

``casual`` makes any text follow the rule; ``is_one_short_sentence`` and
``is_polite`` check what the AI writes before it goes out.
"""

from __future__ import annotations

import re

# Emoji, pictographs, dingbats, arrows and the joiners/variation selectors
# that glue emoji together.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u2300-\u23FF"
    "\u25A0-\u25FF\uFE0E\uFE0F\u200D\u20E3]"
)
_TAG = re.compile(r"<[^>]+>")
# A sentence end followed by more words means a second sentence.
_SECOND_SENTENCE = re.compile(r"[.!?…]\s+\S")

SHORT_MAX = 110  # characters, name included
# Names that appear inside the bot's own sentences and keep their capital.
PROPER_NAMES: tuple[str, ...] = ()


def casual(text: str, emoji: str = "", keep: tuple[str, ...] | list[str] = ()) -> str:
    """One chat line: no markup, one space between words, lowercase, no full stop.

    Any emoji inside ``text`` is removed; ``emoji`` (one) goes at the end.
    ``keep`` are the names in it (a person, a company) — they keep their
    capital letters, as do ``PROPER_NAMES``. Everything the caller passes in
    must already be HTML-escaped where it came from a person; tags are
    removed here, escaped text is left alone.
    """
    text = _EMOJI.sub("", _TAG.sub("", text or ""))
    text = " ".join(text.split()).lower().rstrip(". ").strip()
    names = {" ".join(n.split()) for n in (*keep, *PROPER_NAMES) if n and n.strip()}
    for name in sorted(names, key=len, reverse=True):
        # Whole words only, and never inside a /command ("/ai" stays "/ai").
        text = re.sub(rf"(?<![\w/]){re.escape(name.lower())}(?!\w)", lambda _m, n=name: n, text)
    return f"{text} {emoji}" if emoji and is_emoji(emoji) else text


def capitalized(name: str) -> str:
    """A name with its first letter capital ("алишер" → "Алишер"), the rest as typed."""
    name = (name or "").strip()
    return name[:1].upper() + name[1:]


def is_emoji(value: str) -> bool:
    """Whether ``value`` is exactly one emoji (with its joiner/variation marks)."""
    value = (value or "").strip()
    return 0 < len(value) <= 3 and all(_EMOJI.match(char) for char in value)


def is_one_short_sentence(text: str, limit: int = SHORT_MAX) -> bool:
    """Whether ``text`` (already ``casual``) is a single short sentence."""
    return bool(text) and len(text) <= limit and not _SECOND_SENTENCE.search(text)


# "сен" and the verb forms that go with it — the familiar address the bot
# must never use. The polite forms end in -сиз/-сангиз/-дингиз and pass.
_INFORMAL_WORDS = {"сен", "сени", "сенга", "сенинг", "сендан", "сенда", "сенсан", "сенлар", "сенларга", "сенам"}
_INFORMAL_ENDINGS = ("сан", "санми", "санг", "сангми", "динг", "дингми", "сан-ку", "динг-ку")
# Names that only look like a verb form.
_NOT_INFORMAL = {"ҳасан", "ҳусан"}
_WORD = re.compile(r"[а-яёўқғҳ-]+")


def is_polite(text: str) -> bool:
    """Whether ``text`` keeps to the respectful "сиз" — no "сен", no -сан/-санг/-динг verb forms."""
    for word in _WORD.findall((text or "").lower()):
        if word in _NOT_INFORMAL:
            continue
        if word in _INFORMAL_WORDS or word.endswith(_INFORMAL_ENDINGS):
            return False
    return True
