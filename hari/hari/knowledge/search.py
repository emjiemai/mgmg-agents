"""Search that only ever looks at documents this person may read (spec §12 step 8, §24).

The permission filter runs BEFORE ranking, so a chunk the person may not see can never
reach the model, the answer or a link. Ranking is BM25 over word prefixes (a cheap stemmer
for Uzbek suffixes: таътил / таътилга / таътилимни). Latin and Cyrillic queries match the same text.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari.access import can_read_document
from hari.db.models import Employee, KnowledgeChunk, KnowledgeDocument
from hari.translit import normalize

PREFIX = 5
# Share of the question's meaningful words that must appear in a piece of text. The model is the second
# gate: it says "need a person" when the sources found do not really answer.
MIN_COVERAGE = 0.34

STOPWORDS = {
    "ва", "билан", "учун", "нима", "нимa", "қандай", "мен", "менга", "менинг", "мени", "бу", "шу", "у",
    "ҳам", "қачон", "қаерда", "керак", "бор", "йўқ", "бўлади", "бўлса", "қилиш", "қилсам", "қилаш",
    "мумкинми", "мумкин", "нега", "ким", "қайси", "нечта", "қанча", "агар", "лекин", "ёки", "эди",
    "биз", "сиз", "сизнинг", "уни", "бир", "ни", "га", "да", "дан", "ми", "ҳақида", "айтинг", "айтиб",
    "беринг", "илтимос", "салом", "ассалому", "алайкум",
    "и", "в", "на", "как", "что", "мне", "я", "для", "по", "не", "это", "или", "мой", "моя",
}


def tokens(text: str) -> list[str]:
    words = normalize(text).split()
    return [w[:PREFIX] for w in words if len(w) >= 3 and w not in STOPWORDS]


def _same_root(query_tok: str, doc_tok: str) -> bool:
    if doc_tok.startswith(query_tok):
        return True
    return len(doc_tok) >= 4 and query_tok.startswith(doc_tok)


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    version: str
    section: str
    text: str
    score: float


async def allowed_chunks(session: AsyncSession, emp: Employee, today: date) -> list[tuple[KnowledgeChunk, KnowledgeDocument]]:
    docs = (await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.status == "approved"))).scalars().all()
    readable = {d.doc_id: d for d in docs if can_read_document(emp, d, today)}
    if not readable:
        return []
    chunks = (await session.execute(
        select(KnowledgeChunk).where(KnowledgeChunk.doc_id.in_(readable.keys()))
    )).scalars().all()
    # Only the live version of a document is ever served.
    return [(c, readable[c.doc_id]) for c in chunks if c.doc_version == readable[c.doc_id].version]


async def search(session: AsyncSession, emp: Employee, query: str, today: date, k: int = 4) -> list[Hit]:
    q_tokens = set(tokens(query))
    if not q_tokens:
        return []
    pool = await allowed_chunks(session, emp, today)
    if not pool:
        return []

    raw_tf = [Counter(w[:PREFIX] for w in c.norm_text.split()) for c, _ in pool]
    # A short root matches its longer forms both ways: «қисм» ~ «қисмни», «наряд» ~ «нарядга».
    docs_tokens = [{q: sum(cnt for t, cnt in tf.items() if _same_root(q, t)) for q in q_tokens} for tf in raw_tf]
    n = len(pool)
    avg_len = sum(sum(t.values()) for t in raw_tf) / n or 1.0
    df = Counter(q for t in docs_tokens for q, f in t.items() if f)
    k1, b = 1.4, 0.75

    hits: list[Hit] = []
    for (chunk, doc), tf, raw in zip(pool, docs_tokens, raw_tf):
        length = sum(raw.values()) or 1
        score, matched = 0.0, 0
        for tok in q_tokens:
            f = tf.get(tok, 0)
            if not f:
                continue
            matched += 1
            idf = max(math.log(1 + (n - df[tok] + 0.5) / (df[tok] + 0.5)), 0.2)
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg_len))
        if matched and matched / len(q_tokens) >= MIN_COVERAGE:
            hits.append(Hit(chunk.id, doc.doc_id, doc.title, doc.version, chunk.section, chunk.text, round(score, 3)))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]
