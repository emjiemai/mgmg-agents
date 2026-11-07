"""HARI data model (spec §22). One schema for SQLite (dev/tests) and PostgreSQL (production).

Times are stored in UTC and shown in Asia/Tashkent. Confidential HR data (salary, bank,
passport, medical) has no table here on purpose: phase 1 never stores it (spec §5, §4).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as the UTC they were stored as."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- people

ROLES = ("employee", "manager", "hr", "owner", "accountant", "lawyer", "it_admin")
STATUSES = ("candidate", "offer", "preparing", "active", "absent", "exiting", "archived")
LANGUAGES = ("uz_cyrl", "uz_latn", "ru")


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(20), unique=True)  # E-0001, never changes
    full_name: Mapped[str] = mapped_column(String(200))
    department: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="employee")
    status: Mapped[str] = mapped_column(String(20), default="preparing")
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    mentor_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="uz_cyrl")
    work_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    work_end_hour: Mapped[int] = mapped_column(Integer, default=18)
    work_days: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4,5")  # Mon=0
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    identity_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The same card sent three times creates one employee (spec §5, A01)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)

    manager: Mapped["Employee | None"] = relationship(foreign_keys=[manager_id], remote_side="Employee.id")
    mentor: Mapped["Employee | None"] = relationship(foreign_keys=[mentor_id], remote_side="Employee.id")

    __mapper_args__ = {"version_id_col": version}

    def works_on(self, weekday: int) -> bool:
        return str(weekday) in self.work_days.split(",")


class Invite(Base):
    """One-time link that binds a Telegram account to an employee card (spec §5 step 3–4)."""

    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- knowledge

class KnowledgeDocument(Base):
    """Document passport (spec §12)."""

    __tablename__ = "knowledge_documents"

    doc_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))  # draft | approved | retired
    classification: Mapped[str] = mapped_column(String(20))  # public | internal | role
    roles: Mapped[str] = mapped_column(Text, default="")  # comma list; empty = all roles
    departments: Mapped[str] = mapped_column(Text, default="")  # comma list; empty = all
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner: Mapped[str] = mapped_column(String(100), default="")
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    review_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="uz_cyrl")
    file_hash: Mapped[str] = mapped_column(String(64))
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    doc_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.doc_id", ondelete="CASCADE"))
    doc_version: Mapped[str] = mapped_column(String(20))
    position: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    norm_text: Mapped[str] = mapped_column(Text)  # lower-case Cyrillic, for search


# --------------------------------------------------------------------------- onboarding

class OnboardingPlan(Base):
    __tablename__ = "onboarding_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), unique=True)
    template: Mapped[str] = mapped_column(String(60))
    started_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | done | paused
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    steps: Mapped[list["OnboardingStep"]] = relationship(
        back_populates="plan", order_by="OnboardingStep.position", cascade="all, delete-orphan"
    )


STEP_STATUSES = ("pending", "submitted", "accepted", "returned")


class OnboardingStep(Base):
    __tablename__ = "onboarding_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("onboarding_plans.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(20))
    day_offset: Mapped[int] = mapped_column(Integer)  # 0 = first work day
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20))  # read | task | meeting
    doc_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    acceptor: Mapped[str] = mapped_column(String(20))  # self | mentor | manager | hr
    status: Mapped[str] = mapped_column(String(20), default="pending")
    proof_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_file_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    plan: Mapped[OnboardingPlan] = relationship(back_populates="steps")


# --------------------------------------------------------------------------- HR cases

CASE_STATUSES = ("submitted", "accepted", "in_review", "decided", "closed", "rejected")


class HRCase(Base):
    """Every HR request: type, requester, owner, due time, privacy, decision (spec §32)."""

    __tablename__ = "hr_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(20), unique=True)  # HR-2026-11-0001
    type: Mapped[str] = mapped_column(String(30))
    requester_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    privacy: Mapped[str] = mapped_column(String(20), default="normal")  # normal | sensitive
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    # For complaints: who it is about — that person never handles it (spec §18)
    about_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_response_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requester: Mapped[Employee] = relationship(foreign_keys=[requester_id])
    assignee: Mapped[Employee | None] = relationship(foreign_keys=[assignee_id])


class CaseEvent(Base):
    __tablename__ = "case_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("hr_cases.id", ondelete="CASCADE"))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseCounter(Base):
    __tablename__ = "case_counters"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # 2026-11
    last: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- messaging & audit

class OutboundMessage(Base):
    """Proactive messages HARI sent: counts the daily limit and blocks duplicates (spec §10, A09)."""

    __tablename__ = "outbound_messages"
    __table_args__ = (UniqueConstraint("idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    kind: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    local_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")  # what to (re)send; HARI's own text, not the person's
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedUpdate(Base):
    """Telegram update ids already handled — a replayed update does nothing twice."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    """Who did what to which object, and the outcome. Never message text or personal data."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    object_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    outcome: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)


class AnswerLog(Base):
    """Internal answer passport (spec §30) — without the question or answer text."""

    __tablename__ = "answer_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    employee_id: Mapped[str] = mapped_column(String(36))
    intent: Mapped[str] = mapped_column(String(40))
    source_ids: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(30))  # answered | no_source | refused | fallback
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    was_helpful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
