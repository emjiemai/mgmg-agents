"""The last look at an analyst answer before it is sent: spelling only, never the facts (2026-11-11, the owner).

The brain (``OPS_MANAGER_BOT_MODEL``, e.g. DeepSeek) finds the answer; a small fast model
(``ANSWER_CHECK_MODEL``) then fixes its Uzbek Cyrillic (or Russian) spelling and grammar. The checker may not change
what the answer says, and code makes sure of it: the polished text is used only if it keeps exactly the same digits
(in order), the same Latin words (SAP, Garmin Abay, codes), the same capitalised words (names, companies), the same
HTML tags and the same number of lines, at a similar length. Anything else — a changed figure, a dropped name, a
timeout, an error — and the original answer goes, untouched. Logs keep only whether it changed and why not.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import Counter

from integrations.ai.openrouter_client import OpenRouterClient
from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging

log = setup_logging("ops-manager-bot")

AGENT = "ops-manager-bot"
LIMIT = 6.0  # seconds the check may take; longer → the original answer
MAX_CHARS = 4000  # longer answers go as they are (several Telegram messages; rarely worth a check)

SYSTEM = """You proofread ONE short answer a company bot is about to send. Fix ONLY spelling, grammar and \
punctuation of the {language} text. Keep EVERYTHING else exactly: every number, amount, date and code; every name \
of a person, company, product, shop or warehouse, letter for letter; every Latin word; the HTML tags <b>, <i> and \
where they are; every line break, bullet ("• ") and empty line; the order and the meaning. Add nothing, remove \
nothing, don't translate, don't rephrase. If nothing needs fixing, return it unchanged. Return ONLY the text."""
LANGUAGES = {"uz": "Uzbek (Cyrillic script)", "ru": "Russian"}

_DIGITS = re.compile(r"\d")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'._/-]*")
_CAPITAL = re.compile(r"(?<![\w])[A-ZА-ЯЁЎҚҒҲ][\w'ʻ’-]*")
_TAGS = re.compile(r"</?[a-z]+>")


def why_not(original: str, polished: str) -> str | None:
    """None when ``polished`` may replace ``original``; otherwise what it changed that it mustn't."""
    if not polished.strip():
        return "empty"
    if "".join(_DIGITS.findall(original)) != "".join(_DIGITS.findall(polished)):
        return "digits"
    words_a, words_b = _TAGS.sub(" ", original), _TAGS.sub(" ", polished)  # «b» of <b> isn't a Latin word
    if Counter(_LATIN.findall(words_a)) != Counter(_LATIN.findall(words_b)):
        return "latin"
    if Counter(_CAPITAL.findall(words_a)) != Counter(_CAPITAL.findall(words_b)):
        return "names"
    if _TAGS.findall(original) != _TAGS.findall(polished):
        return "tags"
    if original.count("\n") != polished.count("\n"):
        return "lines"
    if not 0.85 <= len(polished) / max(len(original), 1) <= 1.15:
        return "length"
    return None


async def polish(text: str, run_id: uuid.UUID, lang: str = "uz") -> str:
    """The answer with its spelling fixed, or the original when the check is off, fails, or changed a fact."""
    if (not settings.answer_check_enabled or not settings.answer_check_model.strip() or not text.strip()
            or len(text) > MAX_CHARS):
        return text
    started = time.monotonic()
    reason: str | None
    polished = text
    try:
        async with OpenRouterClient(agent=AGENT, run_id=run_id, model_override=settings.answer_check_model,
                                    fallback_override="") as ai:
            candidate = await asyncio.wait_for(
                ai.complete(SYSTEM.format(language=LANGUAGES.get(lang, LANGUAGES["uz"])), text), LIMIT)
        candidate = candidate.strip().strip("`").strip()
        reason = why_not(text, candidate)
        if reason is None:
            polished = candidate
    except Exception as exc:  # noqa: BLE001 — the original answer always goes
        reason = type(exc).__name__
    changed = polished != text
    ms = int((time.monotonic() - started) * 1000)
    log.info("Answer check: {} in {} ms{}", "fixed" if changed else "unchanged", ms, f" (kept: {reason})" if reason else "")
    try:
        await log_action(agent=AGENT, action="answer_check", target_system="openrouter", status="success", run_id=run_id,
                         mode="read", payload={"changed": changed, "kept_original": reason, "ms": ms})
    except Exception:  # noqa: BLE001
        pass
    return polished
