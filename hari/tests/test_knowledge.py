"""Knowledge base: passport, permission filter before search, versions (spec §12, §24; A06)."""

from __future__ import annotations

from sqlalchemy import select

from hari.db.models import KnowledgeChunk, KnowledgeDocument
from hari.knowledge.loader import chunk_markdown, load_directory
from hari.knowledge.search import search
from tests.conftest import write_doc


async def test_ordinary_employee_finds_internal_rule_in_latin_or_cyrillic(loaded, session_scope, staff, today):
    async with session_scope() as s:
        emp = await staff(s, "Аълоев Анвар", tg=2)
        hits = await search(s, emp, "Таътил қандай сўрайман?", today)
        assert hits and hits[0].doc_id == "LEAVE"
        hits_latin = await search(s, emp, "ta'til qanday so'rayman", today)
        assert hits_latin and hits_latin[0].doc_id == "LEAVE"


async def test_role_document_only_for_that_role_and_department(loaded, session_scope, staff, today):
    async with session_scope() as s:
        seller = await staff(s, "Аълоев Анвар", tg=2)
        tech = await staff(s, "Техников Рушан", department="Сервис", position="Техник", tg=3)
        tech_other_dept = await staff(s, "Техников Жамол", department="Londry", position="Техник", tg=4)
        q = "қисмни нарядга қандай ёзаман"
        assert not await search(s, seller, q, today)
        assert [h.doc_id for h in await search(s, tech, q, today)] == ["SERVICE"]
        assert not await search(s, tech_other_dept, q, today)


async def test_a06_draft_and_expired_documents_are_never_served(loaded, session_scope, staff, today):
    async with session_scope() as s:
        emp = await staff(s, "Аълоев Анвар", tg=2)
        assert not await search(s, emp, "масофавий иш", today)  # draft
        assert not await search(s, emp, "мукофот премия", today)  # effective_to passed


async def test_unverified_person_sees_nothing_internal(loaded, session_scope, staff, today):
    async with session_scope() as s:
        stranger = await staff(s, "Номаълум Одам")  # no verified Telegram
        assert not await search(s, stranger, "таътил сўраш", today)


async def test_new_version_replaces_old_and_removed_file_is_retired(docs, session_scope, staff, today):
    async with session_scope() as s:
        await load_directory(s, docs)
    write_doc(docs, "LEAVE", "Янги тартиб: таътил ариза портал орқали.", title="Таътил тартиби", version="2",
              section="Таътил сўраш")
    (docs / "SERVICE.md").unlink()
    async with session_scope() as s:
        result = await load_directory(s, docs)
        assert "LEAVE" in result.loaded and "SERVICE" in result.retired
        versions = {c.doc_version for c in (await s.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.doc_id == "LEAVE"))).scalars()}
        assert versions == {"2"}
        emp = await staff(s, "Аълоев Анвар", tg=2)
        hits = await search(s, emp, "таътил ариза", today)
        assert hits[0].version == "2" and "портал" in hits[0].text
    async with session_scope() as s:
        assert (await load_directory(s, docs)).unchanged  # same files again: nothing reloaded


async def test_confidential_classes_and_broken_files_are_refused(tmp_path, session_scope):
    write_doc(tmp_path, "PAY", "Ҳамма маоши", cls="salary")
    (tmp_path / "bad.md").write_text("no passport here", encoding="utf-8")
    async with session_scope() as s:
        result = await load_directory(s, tmp_path)
        assert {name for name, _ in result.refused} == {"PAY.md", "bad.md"}
        assert (await s.execute(select(KnowledgeDocument))).scalars().all() == []


def test_chunking_keeps_sections_and_splits_long_text():
    body = "## Бир\n\nбиринчи\n\n## Икки\n\n" + "\n\n".join(["сўз " * 200] * 5)
    chunks = chunk_markdown(body)
    assert chunks[0] == ("Бир", "биринчи")
    assert all(title == "Икки" for title, _ in chunks[1:]) and len(chunks) > 2
