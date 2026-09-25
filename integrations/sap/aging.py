"""Receivables aging buckets — the one SAP rule the push path needs.

The SAP B1 Service Layer client that used to live next to this was removed
(2026-09-25): it has never been reachable from Render. SAP data arrives only
by push from the gateway's own machine (``push_handler.py``).
"""

from __future__ import annotations


def aging_bucket(days_overdue: int) -> str:
    """Classify an invoice into an aging bucket.

    Args:
        days_overdue: Days past the due date (0 or negative = not yet due).

    Returns:
        One of 'current', '1_30', '31_60', '61_90', '90_plus'.
    """
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1_30"
    if days_overdue <= 60:
        return "31_60"
    if days_overdue <= 90:
        return "61_90"
    return "90_plus"
