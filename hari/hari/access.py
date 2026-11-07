"""Who may see or do what (spec §4). Default: no right given = no access.

Every check here runs on the server with the employee loaded from the database by their
verified Telegram id. Nothing typed in chat ("I am the director", another person's ID)
changes the result.
"""

from __future__ import annotations

from datetime import date

from hari.db.models import Employee, KnowledgeDocument, OnboardingStep

STAFF_STATUSES = {"preparing", "active", "absent", "exiting"}
HR_ROLES = {"hr", "owner"}


def _csv(value: str) -> set[str]:
    return {v.strip().lower() for v in (value or "").split(",") if v.strip()}


def is_staff(emp: Employee) -> bool:
    """A candidate or someone with only an offer has no internal rights (spec §5 step 3)."""
    return emp.status in STAFF_STATUSES and emp.identity_verified_at is not None


def can_read_document(emp: Employee, doc: KnowledgeDocument, today: date) -> bool:
    if doc.status != "approved":
        return False  # drafts are never handed out as rules (spec §33)
    if doc.effective_from and doc.effective_from > today:
        return False
    if doc.effective_to and doc.effective_to < today:
        return False
    if doc.classification == "public":
        return True
    if not is_staff(emp):
        return False
    if doc.classification == "internal":
        return True
    if doc.classification == "role":
        roles, depts = _csv(doc.roles), _csv(doc.departments)
        role_ok = not roles or emp.role.lower() in roles or emp.position.lower() in roles
        dept_ok = not depts or emp.department.lower() in depts
        return role_ok and dept_ok
    return False  # any unknown class is closed


def can_manage_employees(emp: Employee) -> bool:
    return is_staff(emp) and emp.role in HR_ROLES


def can_view_employee(viewer: Employee, target: Employee) -> bool:
    if viewer.id == target.id:
        return True
    if not is_staff(viewer):
        return False
    if viewer.role in HR_ROLES:
        return True
    return target.manager_id == viewer.id or target.mentor_id == viewer.id


def can_accept_step(actor: Employee, step: OnboardingStep, owner_of_plan: Employee) -> bool:
    """Only the named human accepts a step; HR is the backup. Nobody accepts their own work."""
    if actor.id == owner_of_plan.id or not is_staff(actor):
        return False
    if actor.role in HR_ROLES:
        return True
    if step.acceptor == "mentor":
        return owner_of_plan.mentor_id == actor.id
    if step.acceptor == "manager":
        return owner_of_plan.manager_id == actor.id
    return False


def can_handle_case(actor: Employee, assignee_id: str | None, about_employee_id: str | None) -> bool:
    if not is_staff(actor) or actor.id == about_employee_id:
        return False  # a person a complaint is about never handles it (spec §18)
    return actor.id == assignee_id or actor.role in HR_ROLES
