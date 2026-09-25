"""MGMG's divisions and their display names.

No source system stores "division" as such; ``ar_aging_snapshots.division``
is left empty by the SAP push, so an invoice without a sales person shows as
"Бошқа" (other). Mapping SAP sales-person codes to divisions was never filled
in and was removed with the SAP Service Layer client (2026-09-26).
"""

from __future__ import annotations

ARMIN = "armin"
IMUS = "imus"
ONDRY = "ondry"
SERVICE = "service"
PROPERTIES = "properties"

DIVISION_LABELS: dict[str, str] = {
    ARMIN: "Armin",
    IMUS: "IMUS-Alliance",
    ONDRY: "ONDRY",
    SERVICE: "Хизмат маркази",
    PROPERTIES: "Кўчмас мулк",
}



def label(division: str | None) -> str:
    """Human-readable division name for messages.

    Args:
        division: A division key, or ``None``.

    Returns:
        The display label, or "Бошқа" ("Other") for unmapped values.
    """
    return DIVISION_LABELS.get(division or "", "Бошқа")
