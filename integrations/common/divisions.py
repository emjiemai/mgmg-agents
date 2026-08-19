"""Mapping of source-system identifiers onto MGMG divisions.

None of the source systems store "division" as such, so each division is
inferred from whatever key that system does carry. The mappings live here, in
one file, instead of being scattered across agents.

ACTION REQUIRED before go-live: replace the placeholder ids below with the real
sales-person codes, amoCRM pipeline ids and Verifix department names. Anything
unmapped resolves to ``None``, which shows up in the brief as "Other" rather
than being silently dropped.
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
    SERVICE: "Service center",
    PROPERTIES: "Properties",
}

# SAP: sales employee code (OSLP.SlpCode) -> division
DIVISION_BY_SAP_SALESPERSON: dict[int, str] = {
    # 1: ARMIN,
    # 2: IMUS,
}

# SAP: business partner group code (OCRG.GroupCode) -> division, used when the
# invoice has no sales employee assigned.
DIVISION_BY_SAP_BP_GROUP: dict[int, str] = {}

# amoCRM: pipeline id -> division
DIVISION_BY_AMOCRM_PIPELINE: dict[int, str] = {}

# Verifix: department name (as exported) -> division
DIVISION_BY_VERIFIX_DEPARTMENT: dict[str, str] = {}


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


def division_from_amocrm(pipeline_id: int | None) -> str | None:
    """Resolve a division from an amoCRM pipeline id.

    Args:
        pipeline_id: The lead's pipeline id.

    Returns:
        A division key, or ``None`` when the pipeline is unmapped.
    """
    return DIVISION_BY_AMOCRM_PIPELINE.get(pipeline_id) if pipeline_id is not None else None


def division_from_verifix(department: str | None) -> str | None:
    """Resolve a division from a Verifix department name.

    Args:
        department: Department string as exported by Verifix.

    Returns:
        A division key, or ``None`` when the department is unmapped.
    """
    if not department:
        return None
    return DIVISION_BY_VERIFIX_DEPARTMENT.get(department.strip())


def label(division: str | None) -> str:
    """Human-readable division name for messages.

    Args:
        division: A division key, or ``None``.

    Returns:
        The display label, or "Other" for unmapped values.
    """
    return DIVISION_LABELS.get(division or "", "Other")
