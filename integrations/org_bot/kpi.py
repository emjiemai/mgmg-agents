"""Daily-report KPI definitions, parsing and formatting.

What each role is asked for at 16:00 lives here, in one place: add a tuple to
``ROLE_METRICS`` and the 16:00 ask, the reply parser, the Director's card and
the KPI scorecard all pick it up with no other change.

Parsing is deliberately deterministic (regex, not an AI call): an employee's
reply arrives once per person per day, the formats people actually use are
narrow ("Uchrashuvlar 3, qo'ng'iroq 20" / "3/20/5/2"), and a regex can be
tested offline in scripts/selfcheck.py without spending a model call on every
message. A number that isn't recognized is never invented — the metric is
simply left unset and shown as "—" so nobody's scorecard silently gains
figures they didn't report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Metric:
    """One number an employee reports, and what counts as a full day of it.

    Attributes:
        key: Stored key in ``daily_reports.metrics``.
        label: Uzbek label shown in the ask, the Director's card and the scorecard.
        daily_target: What a full working day looks like for this number.
        aliases: Lowercased fragments matched in a reply, in any language the
            team actually writes in (Uzbek, Russian, English).
    """

    key: str
    label: str
    daily_target: int
    aliases: tuple[str, ...]


# Not assigned to any role (see ROLE_METRICS below). Daily targets are the
# business's own weekly targets (from the CRM's weekly manager report: 20
# meetings, 75 calls, 30 proposals, 10 qualified leads) divided across a 5-day
# week, ready if numbers are ever turned back on.
SALES_METRICS: tuple[Metric, ...] = (
    Metric("meetings", "Uchrashuvlar", 4, ("uchrashuv", "vstrech", "встреч", "meeting")),
    Metric("calls", "Qo'ng'iroqlar", 15, ("qo'ng'iroq", "qongiroq", "qo'ngiroq", "zvon", "звон", "call")),
    Metric("proposals", "Yuborilgan KP", 6, ("kp", "кп", "taklif", "предложен", "proposal", "offer")),
    Metric("new_leads", "Yangi lidlar", 2, ("lid", "лид", "lead")),
)

# Empty on purpose: the business decided (2026-09-16) that everyone, sales
# included, is only asked what they did today — no numbers. KPI is measured on
# reports submitted and tasks completed (counted from the tasks table, not
# self-reported). To ask a role for numbers again, map it here, e.g.
# {"b2b_sotuv": SALES_METRICS}; the ask, the parser, the Director's card and
# the KPI answers all follow with no other change.
ROLE_METRICS: dict[str, tuple[Metric, ...]] = {}

_NUMBER = r"(\d{1,4})"
# Apostrophes: Uzbek Latin uses several characters interchangeably for the
# same letter (o'/oʻ/o`/o’), and phone keyboards pick different ones.
_APOSTROPHES = {"ʻ": "'", "`": "'", "’": "'", "‘": "'"}


def metrics_for_role(role: str) -> tuple[Metric, ...]:
    """The numbers this role is asked for.

    Args:
        role: An ``org_bot.roles`` role slug.

    Returns:
        That role's metrics, or an empty tuple for a written-report-only role.
    """
    return ROLE_METRICS.get(role, ())


def parse_metrics(text: str, metrics: Sequence[Metric]) -> dict[str, int]:
    """Pull each metric's number out of a free-text reply.

    Two shapes are recognized, in order: labelled ("Uchrashuvlar 3",
    "3 ta uchrashuv", "звонки: 20") and, only when no label matched at all,
    a bare positional line with exactly as many numbers as the role has
    metrics ("3/20/5/2").

    Args:
        text: The employee's reply.
        metrics: The metrics their role reports.

    Returns:
        ``{metric key: value}`` for whatever was recognized — possibly empty,
        never guessed.
    """
    if not text or not metrics:
        return {}

    lowered = text.lower()
    for source, target in _APOSTROPHES.items():
        lowered = lowered.replace(source, target)

    # The gap between a label and its number may not cross a clause
    # separator: in "3 ta uchrashuv, 22 ta qo'ng'iroq" the 22 belongs to the
    # NEXT clause, so a label must never reach past the comma to grab it. A
    # colon is not a separator here — "звонки: 30" is one clause.
    gap = r"[^\d\n,;|/·]{0,20}"

    found: dict[str, int] = {}
    for metric in metrics:
        for alias in metric.aliases:
            escaped = re.escape(alias)
            after = re.search(rf"{escaped}(?P<gap>{gap})(?P<num>\d{{1,4}})", lowered)
            before = re.search(rf"(?P<num>\d{{1,4}})(?P<gap>{gap}){escaped}", lowered)
            candidates = [m for m in (after, before) if m is not None]
            if candidates:
                # Whichever number sits closest to the label wins.
                closest = min(candidates, key=lambda m: len(m.group("gap")))
                found[metric.key] = int(closest.group("num"))
                break

    if found:
        return found

    for line in lowered.splitlines():
        numbers = re.findall(r"\d{1,4}", line)
        if len(numbers) == len(metrics):
            return {metric.key: int(value) for metric, value in zip(metrics, numbers)}
    return {}


def build_request_text(display_name: str, metrics: Sequence[Metric]) -> str:
    """The 16:00 message asking one employee for their day.

    Args:
        display_name: Employee's name, so a forwarded card is obviously theirs.
        metrics: Their role's metrics; the numbers block is omitted when empty.

    Returns:
        Telegram HTML.
    """
    lines = [
        "🕓 <b>Kunlik hisobot / Daily report</b>",
        "",
        f"{display_name}, bugun nima qildingiz? Qisqacha yozib yuboring.",
        "<i>What did you do today? Reply with a short summary.</i>",
    ]
    if metrics:
        example = ", ".join(f"{metric.label} {metric.daily_target}" for metric in metrics)
        lines += [
            "",
            "Raqamlarni ham qo'shing / include your numbers:",
            f"<i>{example}</i>",
        ]
    return "\n".join(lines)


def build_reminder_text(metrics: Sequence[Metric]) -> str:
    """The 17:00 nudge for someone who hasn't answered yet.

    Args:
        metrics: Their role's metrics, to repeat the expected format.

    Returns:
        Telegram HTML.
    """
    lines = [
        "⏰ <b>Eslatma / Reminder</b>",
        "",
        "Bugungi hisobotingiz hali kelmadi. Ish kuni tugashidan oldin yuboring.",
        "<i>Your daily report hasn't arrived yet — please send it before the end of the day.</i>",
    ]
    if metrics:
        example = ", ".join(f"{metric.label} {metric.daily_target}" for metric in metrics)
        lines += ["", f"<i>{example}</i>"]
    return "\n".join(lines)


def format_metrics(values: dict[str, int], metrics: Sequence[Metric]) -> str:
    """One line of numbers against target, for a card or scorecard.

    Args:
        values: Parsed metric values (missing keys render as "—").
        metrics: The role's metrics, defining order and targets.

    Returns:
        e.g. ``"Uchrashuvlar: 3/4 ⚠️ · Qo'ng'iroqlar: 18/15 ✅"``, or "" when
        the role reports no numbers.
    """
    if not metrics:
        return ""
    parts = []
    for metric in metrics:
        value = values.get(metric.key)
        if value is None:
            parts.append(f"{metric.label}: —")
        else:
            marker = "✅" if value >= metric.daily_target else "⚠️"
            parts.append(f"{metric.label}: {value}/{metric.daily_target} {marker}")
    return " · ".join(parts)


def missing_metrics(values: dict[str, int], metrics: Sequence[Metric]) -> list[str]:
    """Labels of the numbers a reply didn't include.

    Used to tell the employee what was missed once, without rejecting the
    written part of a report that did arrive.

    Args:
        values: Parsed metric values.
        metrics: The role's metrics.

    Returns:
        Labels still unset, in the role's own order.
    """
    return [metric.label for metric in metrics if metric.key not in values]
