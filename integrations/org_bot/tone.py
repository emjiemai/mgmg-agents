"""The friendly voice for messages employees get without asking.

2026-09-30, from the owner: employees felt the bot's cheer, motivation and
report messages as alarms — a bold header, an emoji, two paragraphs, just
like a task. These now read like a colleague texting: one short sentence,
lowercase, no line breaks, no bold, and at most one emoji — at the very end.
Tasks keep their card format; they are work, not chat.

``casual`` makes any text follow the rule; ``is_one_short_sentence`` checks
what the AI writes before it goes out.
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


def casual(text: str, emoji: str = "") -> str:
    """One chat line: no markup, one space between words, lowercase, no full stop.

    Any emoji inside ``text`` is removed; ``emoji`` (one) goes at the end.
    Everything the caller passes in must already be HTML-escaped where it
    came from a person; tags are removed here, escaped text is left alone.
    """
    text = _EMOJI.sub("", _TAG.sub("", text or ""))
    text = " ".join(text.split()).lower().rstrip(". ").strip()
    return f"{text} {emoji}" if emoji and is_emoji(emoji) else text


def is_emoji(value: str) -> bool:
    """Whether ``value`` is exactly one emoji (with its joiner/variation marks)."""
    value = (value or "").strip()
    return 0 < len(value) <= 3 and all(_EMOJI.match(char) for char in value)


def is_one_short_sentence(text: str, limit: int = SHORT_MAX) -> bool:
    """Whether ``text`` (already ``casual``) is a single short sentence."""
    return bool(text) and len(text) <= limit and not _SECOND_SENTENCE.search(text)
