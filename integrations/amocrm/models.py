"""Pydantic models for amoCRM/Kommo entities, normalized for our own use."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Lead(BaseModel):
    """One deal (lead) in amoCRM, with money already in tiyin."""

    id: int
    name: str | None = None
    price_tiyin: int = 0
    pipeline_id: int | None = None
    pipeline_name: str | None = None
    status_id: int | None = None
    status_name: str | None = None
    responsible_user_id: int | None = None
    responsible_user_name: str | None = None
    division: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closest_task_at: datetime | None = None
    is_closed: bool = False

    @property
    def has_next_task(self) -> bool:
        """True when the deal has an open task scheduled."""
        return self.closest_task_at is not None

    @property
    def url(self) -> str:
        """Deep link to the deal card (base URL filled in by the client)."""
        return f"/leads/detail/{self.id}"


class Task(BaseModel):
    """One amoCRM task."""

    id: int
    text: str | None = None
    entity_id: int | None = None
    entity_type: str | None = None
    responsible_user_id: int | None = None
    complete_till: datetime | None = None
    is_completed: bool = False

    @property
    def is_overdue(self) -> bool:
        """True when the task is open and its due time has passed."""
        from integrations.common.timeutil import now_utc

        return bool(self.complete_till and not self.is_completed and self.complete_till < now_utc())


class PipelineStage(BaseModel):
    """Aggregated state of one stage within a pipeline."""

    pipeline_id: int
    pipeline_name: str | None = None
    status_id: int
    status_name: str | None = None
    division: str | None = None
    deals_count: int = 0
    deals_value_tiyin: int = 0
    deals_without_task: int = 0


class PipelineSummary(BaseModel):
    """Whole-CRM pipeline snapshot used by the CEO brief."""

    stages: list[PipelineStage] = Field(default_factory=list)
    total_deals: int = 0
    total_value_tiyin: int = 0
    deals_without_task: list[Lead] = Field(default_factory=list)
    new_leads_24h: int = 0

    @property
    def by_pipeline(self) -> dict[str, int]:
        """Total open value per pipeline name, in tiyin."""
        totals: dict[str, int] = {}
        for stage in self.stages:
            key = stage.pipeline_name or str(stage.pipeline_id)
            totals[key] = totals.get(key, 0) + stage.deals_value_tiyin
        return totals
