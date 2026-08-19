"""Pydantic models for MGMG's own sales CRM (sales-crm-roan-six.vercel.app).

Field names here match the live API responses observed on 2026-08-18 from
``/api/external/{deals,contacts,manager-tasks,stats}``. This is an internal
tool, not a documented third-party API — if the CRM's schema changes, these
models are the first place to check.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Contact(BaseModel):
    """One contact/customer record."""

    id: int
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    note: str | None = None
    created_by: int | None = None
    created_by_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Deal(BaseModel):
    """One deal, with money already converted to tiyin."""

    id: int
    title: str | None = None
    contact_id: int | None = None
    amount_tiyin: int = 0
    currency: str = "UZS"
    stage_id: int | None = None
    stage_name: str | None = None  # filled in from /stats' stage list
    assigned_to: int | None = None
    assigned_to_name: str | None = None  # not in the API; filled in if ever available
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    has_next_task: bool = False  # set by cross-referencing manager-tasks

    @property
    def is_closed(self) -> bool:
        """True once the deal reached a terminal stage (won or lost).

        Terminal stage ids are read from the live /stats response
        (stage_id 4 = "Deal/Paid project", stage_id 6 = "Fail") — see
        ``client.TERMINAL_STAGE_IDS``.
        """
        from integrations.crm.client import TERMINAL_STAGE_IDS

        return self.stage_id in TERMINAL_STAGE_IDS


class ManagerTask(BaseModel):
    """One manager task.

    ``deal_id`` is best-effort: the live API returned an empty list for this
    endpoint, so the field actually used to link a task to a deal was
    unconfirmed at write time. See ``client._extract_deal_id`` for the
    candidate key names tried, and its warning log if none match.
    """

    id: int
    text: str | None = None
    deal_id: int | None = None
    assigned_to: int | None = None
    due_at: datetime | None = None
    is_completed: bool = False


class StageBreakdown(BaseModel):
    """One pipeline stage's aggregate, as returned by /stats."""

    stage_id: int
    name: str | None = None
    color: str | None = None
    count: int = 0
    value_tiyin: int = 0


class MonthlyBreakdown(BaseModel):
    """One month's aggregate, as returned by /stats."""

    label: str
    count: int = 0
    value_tiyin: int = 0


class CRMStats(BaseModel):
    """Whole-CRM summary, as returned by /api/external/stats."""

    total_deals: int = 0
    total_value_tiyin: int = 0
    total_contacts: int = 0
    won_deals: int = 0
    won_value_tiyin: int = 0
    conversion_rate: float = 0.0
    by_stage: list[StageBreakdown] = Field(default_factory=list)
    monthly: list[MonthlyBreakdown] = Field(default_factory=list)


class PipelineSummary(BaseModel):
    """CEO-brief-ready pipeline snapshot, built from deals + tasks + stats.

    Shaped to match what the old amoCRM ``PipelineSummary`` provided, so the
    CEO daily brief's rendering code didn't need to change.
    """

    stages: list[StageBreakdown] = Field(default_factory=list)
    total_deals: int = 0
    total_value_tiyin: int = 0
    deals_without_task: list[Deal] = Field(default_factory=list)
    new_leads_24h: int = 0

    @property
    def by_pipeline(self) -> dict[str, int]:
        """Total value per stage name, in tiyin — kept for parity with the brief's rendering."""
        return {s.name or str(s.stage_id): s.value_tiyin for s in self.stages}
