"""The AI check on one typed answer before it goes onto an official record.

Shared by the written permission form (EMJ-SOP-ADM-01) and by the name every
employee gives the bot. The AI only judges and tidies: an unclear answer is
sent back with a specific follow-up question; an acceptable one is returned
in Uzbek Cyrillic with its spelling fixed — never with anything added,
removed or changed in meaning.
"""

from __future__ import annotations

import uuid

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError
from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.org_bot.permissions import Field

log = setup_logging("answer-check")

VALIDATE_SYSTEM = """You check ONE answer an employee typed in reply to a \
company bot's question, before it is saved on an official record: \
{context}. The record is written in Uzbek, in Cyrillic script.

You are given the field, what a valid answer looks like, the question the \
employee was asked, and their answer. Employees write in Uzbek (Latin or \
Cyrillic), Russian or English.

1. Decide whether the answer genuinely and clearly answers THIS question.
   Reject greetings ("alo", "salom"), filler or test text ("test", "asd", \
"...", "?"), an answer to a different question, and anything too vague to \
stand on an official record. Do NOT reject for spelling, alphabet, language \
or brevity when the meaning is clear.
2. If it is acceptable, write it for the record:
   - in Uzbek Cyrillic (transliterate Uzbek Latin; translate Russian or \
English words into Uzbek, e.g. "IT Specialist" -> "IT мутахассис");
   - with spelling and grammar corrected, and shaped to fit the field \
(e.g. for "Бўлим", "IT bo'limida" -> "IT бўлими");
   - keep abbreviations and brand names as they are (IT, AI, CRM, SAP, \
Telegram, Render);
   - people's names: transliterate to Cyrillic, never change them \
("Ulug'bek Isoqov" -> "Улуғбек Исоқов");
   - NEVER add, remove or change any fact, number, amount, date, time or \
name. Do not expand or embellish: same meaning, roughly the same length.

Return ONLY JSON. Acceptable: {"ok": true, "value": "<the answer for the record>"}. \
Not acceptable: {"ok": false, "follow_up": "<one short, polite question in \
Uzbek Cyrillic that says exactly what is missing or unclear>"}."""


async def check_answer(
    field: Field, answer: str, run_id: uuid.UUID, *, agent: str, context: str
) -> tuple[bool, str | None, str]:
    """Ask the AI whether this answer can go onto the record.

    Args:
        field: The question being answered.
        answer: What the employee typed.
        run_id: UUID grouping this call's audit rows.
        agent: The calling agent's name, for the audit trail.
        context: What the record is, e.g. "a written permission request form".

    Returns:
        ``(acceptable, follow-up question, text for the record)``. If the AI
        can't be reached the answer is accepted as typed when it's more than
        a character or two — a provider outage must not trap an employee in
        an endless loop.
    """
    message = (
        f"Field: {field.label}\n"
        f"A valid answer is: {field.rule}\n"
        f"Question asked: {field.question}\n"
        f"Employee's answer: {answer}"
    )
    # str.replace, not str.format: the prompt's JSON examples contain braces.
    system = VALIDATE_SYSTEM.replace("{context}", context)
    try:
        async with OpenRouterClient(
            agent=agent,
            run_id=run_id,
            provider_override=settings.ops_manager_bot_provider,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            verdict = await ai.complete_json(system, message)
    except OpenRouterError as exc:
        log.warning("Answer check unavailable, accepting '{}' for {}: {}", answer[:60], field.key, exc)
        return len(answer.strip()) >= 2, None, answer.strip()

    if verdict.get("ok") is True:
        value = " ".join(str(verdict.get("value") or "").split())
        return True, None, value or answer.strip()
    follow_up = str(verdict.get("follow_up") or "").strip()
    return False, follow_up or None, ""
