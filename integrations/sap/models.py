"""Pydantic models for the clean dicts the SAP client returns.

The client never hands raw OData back to callers: every method maps SAP's
PascalCase/typed-enum payload onto one of these models, with money already
converted to integer tiyin.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

AgingBucket = Literal["current", "1_30", "31_60", "61_90", "90_plus"]


class ARInvoice(BaseModel):
    """One open A/R invoice with its aging position."""

    doc_entry: int
    doc_num: int | None = None
    card_code: str
    card_name: str | None = None
    doc_date: date | None = None
    due_date: date | None = None
    days_overdue: int = 0
    aging_bucket: AgingBucket = "current"
    currency: str = "UZS"
    doc_total_tiyin: int = 0
    paid_to_date_tiyin: int = 0
    balance_due_tiyin: int = 0
    sales_person_code: int | None = None
    sales_person_name: str | None = None
    division: str | None = None


class ARAging(BaseModel):
    """AR aging for a snapshot date: per-invoice detail plus bucket totals."""

    snapshot_date: date
    invoices: list[ARInvoice] = Field(default_factory=list)
    total_open_tiyin: int = 0
    total_overdue_tiyin: int = 0
    bucket_totals_tiyin: dict[str, int] = Field(default_factory=dict)
    bucket_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def overdue_count(self) -> int:
        """Number of invoices past their due date."""
        return sum(1 for inv in self.invoices if inv.days_overdue > 0)


