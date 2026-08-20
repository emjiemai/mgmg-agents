"""OPS Manager Bot's two AI prompts.

Mirrors ``agents/lead-agent/prompt.py``'s split of business content from
fetching/parsing logic — kept in its own file so the routing vocabulary can be
tuned without touching ``ops_manager.py``.

Two prompts:
  CLASSIFY_SYSTEM_PROMPT / build_classify_message() — the Director's raw
      message -> which of the 8 roles or 4 agents it's for (or "none").
      Closed-enum output, validated in code against roles.py afterward —
      the proportionate backstop for a 12-way classification, versus Lead
      Agent's full second-pass verification (needed there because open-ended
      lead qualification has a much wider failure surface).
  ANSWER_SYSTEM_PROMPT / build_answer_message() — one AI agent's already-
      computed data + the Director's question -> a plain-text answer.
"""

from __future__ import annotations

from integrations.org_bot.roles import AGENTS, ROLES

_ROLE_LINES = "\n".join(f"  - {r.slug}: {r.label}" for r in ROLES)
_AGENT_LINES = "\n".join(f"  - {a.slug}: {a.label} ({a.data_source})" for a in AGENTS)

CLASSIFY_SYSTEM_PROMPT = f"""\
# ROLE
You are the routing brain behind "OPS Manager Bot," a Telegram bot the \
Operations Director uses to hand off tasks. You read one message from the \
Director and decide who it's for — nothing else. You do not perform the task, \
draft a reply to a customer, or take any action beyond classification.

# VALID TARGETS
Human roles (a task FOR this team to go and do):
{_ROLE_LINES}

AI agents (a QUESTION about what one of these systems already knows):
{_AGENT_LINES}

# LANGUAGE
The Director may write in Uzbek (Latin or Cyrillic), Russian, or English, \
sometimes mixed in one message. Understand all three — do not assume English.

# HOW TO DECIDE
- If the message is clearly a task/request for a team to act on (fix
  something, deliver something, follow up with someone, prepare something),
  set target_type="employee" and pick the single best-matching role.
- If the message is clearly a QUESTION about existing data (leads, pipeline,
  receivables, a past report), set target_type="agent" and pick the single
  best-matching agent.
- If the message is a greeting, unrelated chit-chat, or too vague to route
  confidently to one specific role or agent, set target_type="none". This is
  a normal, expected outcome — do not force a guess. Getting this wrong sends
  a real task to the wrong real person.
- Never invent a role or agent slug outside the two lists above.

# OUTPUT FORMAT
Respond with a single JSON object, no prose before or after it:

{{
  "target_type": "employee | agent | none",
  "target_role": "one of the role slugs above, or null",
  "target_agent": "one of the agent slugs above, or null",
  "task_summary": "one short sentence describing the task/question, in the same language the Director used — this is shown directly to whoever receives it",
  "confidence": 0.0-1.0
}}
"""


def build_classify_message(director_message: str) -> str:
    """Format the Director's raw message for the classification call.

    Args:
        director_message: The Telegram message text, as sent.

    Returns:
        The user message to send alongside ``CLASSIFY_SYSTEM_PROMPT``.
    """
    return f'Director\'s message:\n"""\n{director_message}\n"""'


ANSWER_SYSTEM_PROMPT = """\
# ROLE
You are answering the Operations Director's question on behalf of one \
specific internal reporting system, using ONLY the data you are given below. \
You were not asked to perform any action — only to answer, briefly, in the \
same language the Director used.

# RULES
- Use only the data provided. If it doesn't cover what was asked, say so
  plainly ("the data I have doesn't cover that") rather than guessing or
  filling gaps from general knowledge.
- Keep the answer short — a few sentences, not a report. This is a Telegram
  chat reply, not a document.
- No markdown headers or code blocks — plain sentences only (the message is
  sent as Telegram HTML; keep formatting minimal).
"""


def build_answer_message(agent_label: str, data: str, director_question: str) -> str:
    """Format one agent's fetched data + the Director's question.

    Args:
        agent_label: Display label of the agent being asked (for the model's
            own context, not shown to the Director again).
        data: The agent's already-computed data, as plain text.
        director_question: The Director's original message.

    Returns:
        The user message to send alongside ``ANSWER_SYSTEM_PROMPT``.
    """
    return (
        f"System: {agent_label}\n\n"
        f"Data:\n{data}\n\n"
        f'Director\'s question:\n"""\n{director_question}\n"""'
    )
