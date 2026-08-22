"""Single source of truth for the org-chart vocabulary: the 8 human roles and
4 AI agents OPS Manager Bot can route a task to.

Consumed by ``admin.py`` (role-picker buttons), ``ops_manager.py`` (dispatch +
output validation), and ``prompt.py`` (the classification enum) so this
vocabulary is defined exactly once.

The role slugs must match ``database/schema.sql``'s ``employees.role`` CHECK
constraint exactly — Postgres can't import this file, so keep the two in sync
by hand (same pre-existing limitation as ``approvals.category``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    slug: str
    label: str


@dataclass(frozen=True)
class Agent:
    slug: str
    label: str
    data_source: str  # human-readable description of what gets fetched, for docs/logs


ROLES: list[Role] = [
    Role("b2b_sotuv", "B2B Sotuv"),
    Role("it", "IT"),
    Role("buxgalteriya", "Buxgalteriya"),
    Role("hr", "HR"),
    Role("ombor", "Ombor"),
    Role("operatsion_direktor", "Operatsion Direktor"),
    Role("mobilograf", "Mobilograf"),
    Role("aloqa_markazi", "Aloqa Markazi (Call Center)"),
    Role("garmin_sotuv", "Garmin Sotuv"),
]

AGENTS: list[Agent] = [
    Agent("lead_agent", "Lead Agent", "Leads Google Sheet, every row"),
    Agent("finance_agent", "Finance Agent", "every open receivable + recent alerts"),
    Agent("crm_agent", "CRM Agent", "full CRM pipeline snapshot"),
    Agent("reporter_agent", "Reporter Agent", "14 days of daily-brief history"),
    Agent("all_systems", "Barcha tizimlar / All Systems", "combined summary from the four operational systems above (not the Garmin catalog — that's product reference, not an operational status)"),
    Agent("garmin_catalog", "Garmin Katalogi / Garmin Catalog", "static product+price snapshot from garmin.com.uz — see prompt.py's GARMIN_CATALOG"),
]

ROLE_SLUGS: set[str] = {r.slug for r in ROLES}
AGENT_SLUGS: set[str] = {a.slug for a in AGENTS}

ROLE_LABELS: dict[str, str] = {r.slug: r.label for r in ROLES}
AGENT_LABELS: dict[str, str] = {a.slug: a.label for a in AGENTS}

DIRECTOR_ROLE = "operatsion_direktor"

# The Director's own role is a legitimate onboarding choice (role_picker_keyboard
# below still offers all of ROLES, including this one, so the real Director can
# register) but must never be a *routable task target* -- the person composing
# a task via OPS Manager Bot already holds this role, so routing a task "to"
# it would send it back to themselves. Classification (prompt.py's _ROLE_LINES)
# and its code-level validation (ops_manager.validate_classification) both use
# this narrower list/set instead of ROLES/ROLE_SLUGS.
ROUTABLE_ROLES: list[Role] = [r for r in ROLES if r.slug != DIRECTOR_ROLE]
ROUTABLE_ROLE_SLUGS: set[str] = {r.slug for r in ROUTABLE_ROLES}


def role_picker_keyboard(request_id: str) -> dict:
    """Build the inline keyboard shown to a newly-approved employee.

    Args:
        request_id: The ``access_requests.id`` this pick will resolve.

    Returns:
        A Telegram ``reply_markup`` dict, one role per row (8 rows) so labels
        never truncate on a narrow phone screen.
    """
    return {
        "inline_keyboard": [
            [{"text": role.label, "callback_data": f"setrole:{role.slug}:{request_id}"}]
            for role in ROLES
        ]
    }
