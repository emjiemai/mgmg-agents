"""Mapping of source-system identifiers onto MGMG divisions.

None of the source systems store "division" as such, so each division is
inferred from whatever key that system does carry. The mappings live here, in
one file, instead of being scattered across agents.

ACTION REQUIRED before go-live: replace the placeholder ids below with the real
SAP sales-person codes. Anything unmapped resolves to ``None``, which shows up
in the brief as "Other" rather than being silently dropped.
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
    SERVICE: "Xizmat markazi",
    PROPERTIES: "Ko'chmas mulk",
}

# SAP: sales employee code (OSLP.SlpCode) -> division
DIVISION_BY_SAP_SALESPERSON: dict[int, str] = {
    # 1: ARMIN,
    # 2: IMUS,
}

# SAP: business partner group code (OCRG.GroupCode) -> division, used when the
# invoice has no sales employee assigned.
DIVISION_BY_SAP_BP_GROUP: dict[int, str] = {}


def division_from_sap(sales_person_code: int | None, bp_group: int | None = None) -> str | None:
    """Resolve a division for a SAP document.

    Args:
        sales_person_code: OSLP.SlpCode on the document (-1 when unassigned).
        bp_group: Business partner group code, used as fallback.

    Returns:
        A division key, or ``None`` when nothing maps.
    """
    if sales_person_code is not None and sales_person_code in DIVISION_BY_SAP_SALESPERSON:
        return DIVISION_BY_SAP_SALESPERSON[sales_person_code]
    if bp_group is not None:
        return DIVISION_BY_SAP_BP_GROUP.get(bp_group)
    return None


def label(division: str | None) -> str:
    """Human-readable division name for messages.

    Args:
        division: A division key, or ``None``.

    Returns:
        The display label, or "Boshqa" ("Other") for unmapped values.
    """
    return DIVISION_LABELS.get(division or "", "Boshqa")
