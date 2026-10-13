"""The route table: which kind of message goes to which role (the owner's spec of 11.10.2026, section 7).

"A0 BRAIN sends every kind of data only to the position that needs it."
Positions are the roles people pick when they register and the admin
approves (2026-10-13, the owner: "we already set them") — a message goes to
every active person holding the role. Nobody in the role: it falls back to
the Director (the Boss), so nothing is lost — and the message says so.

The Boss gets only company totals and decisions; ordinary complaints and
leads go to the departments (2026-10-13).
"""

from __future__ import annotations

from typing import Any

from integrations.org_bot import store
from integrations.org_bot.roles import DIRECTOR_ROLE

# kind -> the roles that get it, in order of who's first in line.
ROUTES: dict[str, tuple[str, ...]] = {
    "complaint:laundry": ("londry",),        # Londry complaints → the Londry team (Зиёда)
    "complaint:garmin": ("garmin_sotuv",),   # Garmin complaints → Garmin sales (Ширин, …)
}


async def recipients(kind: str) -> tuple[list[dict[str, Any]], bool]:
    """Who gets a message of this kind: (active people in its roles, fell back to the Director).

    An unknown kind goes to the Director — a gap in the table is visible, not silent.
    """
    people: list[dict[str, Any]] = []
    for role in ROUTES.get(kind, ()):
        people += await store.active_employees_by_role(role)
    if people:
        return people, False
    return await store.active_employees_by_role(DIRECTOR_ROLE), True
