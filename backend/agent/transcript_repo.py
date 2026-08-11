"""
transcript_repo.py — Full-fidelity message transcript.

Every message is logged here unconditionally, by the plumbing in
processor.py — not by model discretion, unlike memory_repo.py's
append_memory. That's the point: it's the structural fix for one agent not
knowing what another agent (or worker) already told the operator, because
nothing here depends on any model choosing to remember it.

MemoryEntry stays what it is — curated, importance-scored, always injected
into context. This is the separate, never-summarized deep archive, searched
on demand via the recall tool, not injected into every prompt.

Search combines FTS5 keyword matching with optional semantic similarity
(sqlite-vec), merged by reciprocal rank fusion rather than a linear score
blend — BM25 and cosine distance aren't on comparable scales. Degrades to
FTS-only when no embedding endpoint is configured (LOCAL_EMBEDDING_URL) or
the sqlite-vec extension didn't load.
"""

import re
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text, select
import backend.database.connection as db
from backend.database.connection import AsyncSessionLocal
from backend.database.models import Transcript
from backend.agent.local_llm import local_embed

logger = logging.getLogger(__name__)

RRF_K = 60  # standard reciprocal rank fusion constant


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokenize_query(query: str) -> str:
    tokens = re.sub(r"[^\w\s]", " ", query.lower()).split()
    seen, result = set(), []
    for t in tokens:
        if len(t) > 1 and t not in seen:
            seen.add(t)
            result.append(t)
    return " ".join(result)


async def log_message(platform: str, user_id: str, role: str, content: str) -> int:
    """Append one message to the permanent transcript. Called unconditionally
    from processor.py for every turn — every notification, every hydration
    prompt, every reply, whether or not any agent thought to log it via
    append_memory. Best-effort semantic indexing: an embedding failure never
    loses the message itself, it just isn't semantically searchable."""
    if not content:
        return -1

    now = _now_iso()
    async with AsyncSessionLocal() as session:
        row = Transcript(platform=platform, user_id=user_id, role=role, content=content, created_at=now)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        row_id = row.id

    if db.VEC_AVAILABLE:
        try:
            vector = await local_embed(content)
            if vector is not None:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("INSERT INTO transcript_vec(rowid, embedding) VALUES (:id, :emb)"),
                        {"id": row_id, "emb": json.dumps(vector)},
                    )
                    await session.commit()
        except Exception as e:
            logger.warning(f"[transcript] embedding index failed for row {row_id}: {e}")

    return row_id


async def _fts_search(platform: str, user_id: str, fts_query: str, limit: int) -> list[int]:
    if not fts_query:
        return []
    async with AsyncSessionLocal() as session:
        try:
            rows = await session.execute(text("""
                SELECT t.id
                FROM transcript_fts
                JOIN transcript t ON t.id = transcript_fts.rowid
                WHERE transcript_fts MATCH :query
                  AND t.platform = :platform AND t.user_id = :user_id
                ORDER BY bm25(transcript_fts)
                LIMIT :limit
            """), {"query": fts_query, "platform": platform, "user_id": user_id, "limit": limit})
            return [r[0] for r in rows.fetchall()]
        except Exception as e:
            logger.warning(f"[transcript] FTS search failed: {e}")
            return []


async def _semantic_search(platform: str, user_id: str, query: str, limit: int) -> list[int]:
    if not db.VEC_AVAILABLE:
        return []
    vector = await local_embed(query)
    if vector is None:
        return []
    async with AsyncSessionLocal() as session:
        try:
            # vec0 doesn't know about platform/user_id — over-fetch the KNN
            # pass and scope on our side.
            rows = await session.execute(text("""
                SELECT t.id
                FROM transcript_vec v
                JOIN transcript t ON t.id = v.rowid
                WHERE v.embedding MATCH :query AND k = :over_fetch
                  AND t.platform = :platform AND t.user_id = :user_id
                ORDER BY v.distance
                LIMIT :limit
            """), {
                "query": json.dumps(vector), "over_fetch": limit * 4,
                "platform": platform, "user_id": user_id, "limit": limit,
            })
            return [r[0] for r in rows.fetchall()]
        except Exception as e:
            logger.warning(f"[transcript] semantic search failed: {e}")
            return []


def _reciprocal_rank_fusion(*ranked_lists: list[int], k: int = RRF_K) -> list[int]:
    """Merge ranked ID lists by rank position, not raw score — BM25 and
    cosine distance live on incomparable scales, so summing them directly
    would be meaningless."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


async def search(platform: str, user_id: str, query: str, limit: int = 10) -> list[Transcript]:
    """Search the full transcript for messages relevant to `query`, scoped to
    one platform/user. Combines FTS5 keyword search with semantic similarity
    (when available) via reciprocal rank fusion. This is an on-demand lookup
    — the recall tool — never something injected into every prompt."""
    fts_query = _tokenize_query(query)
    over_fetch = max(limit * 3, 10)

    fts_ids = await _fts_search(platform, user_id, fts_query, over_fetch)
    semantic_ids = await _semantic_search(platform, user_id, query, over_fetch)

    merged_ids = _reciprocal_rank_fusion(fts_ids, semantic_ids)[:limit]
    if not merged_ids:
        return []

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Transcript).where(Transcript.id.in_(merged_ids)))
        rows = {r.id: r for r in result.scalars().all()}

    return [rows[i] for i in merged_ids if i in rows]
