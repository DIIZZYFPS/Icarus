"""
triage_repo.py — Durable, reviewable email/spam triage classifications, plus
sender + semantic (FTS5/sqlite-vec) retrieval of past operator corrections
for prompt injection.

record_classification() is the one write call site both worker_email_triage.py
and spam_sweep.py use, for both inbox and spam ("kind") decisions — one
durable TriageClassification row per message, upserted in place on retry
(Redis redelivery, a re-swept spam message), never duplicated. Distinct from
activity_repo.py's ActivityEvent: that's an append-only narrated log with no
mutable review state; this is what the dashboard's Triage tab reads and
writes, and what actually closes the feedback loop.

Correction retrieval mirrors transcript_repo.py's FTS+vec+RRF pattern
exactly, with one deliberate difference: transcript_vec/transcript_fts index
every row unconditionally (every transcript entry is a valid search target).
Here only *reviewed* rows are useful correction examples, so both the FTS
and vec side are written explicitly from record_review() — see
connection.py's init_db() for the matching comment on why those tables
aren't trigger-synced like memory_fts/transcript_fts.
"""

import re
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text, select
import backend.database.connection as db
from backend.database.connection import AsyncSessionLocal
from backend.database.models import TriageClassification
from backend.agent.local_llm import local_embed

logger = logging.getLogger(__name__)

RRF_K = 60  # standard reciprocal rank fusion constant — same as transcript_repo.py


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


def _reciprocal_rank_fusion(*ranked_lists: list[int], k: int = RRF_K) -> list[int]:
    """Merge ranked ID lists by rank position, not raw score — BM25 and
    cosine distance live on incomparable scales, so summing them directly
    would be meaningless. (Copied from transcript_repo.py rather than
    imported — small private helper, consistent with this codebase's
    per-repo-module style.)"""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


_SENDER_DOMAIN_RE = re.compile(r'@([\w.-]+)')


def _parse_sender_domain(sender: str) -> str | None:
    m = _SENDER_DOMAIN_RE.search(sender or "")
    return m.group(1).lower() if m else None


# How much a sender's reviewed track record (approved vs overridden, this
# kind only) can pull the model's own per-message confidence away from what
# it actually said. Capped well below 1.0 even with a long history — a
# normally-reliable sender's genuinely odd email should still get *some*
# pull from its own raw score, not be fully suppressed by reputation alone.
# Ramps linearly from 0 to the cap as reviewed history accumulates, full
# weight reached at RELIABILITY_FULL_WEIGHT_AT reviews for that sender+kind.
RELIABILITY_MAX_WEIGHT = 0.6
RELIABILITY_FULL_WEIGHT_AT = 8


async def adjusted_confidence(raw_confidence: float | None, sender: str, kind: str) -> float | None:
    """The actual mechanism behind "a true learning loop" — without this,
    approving/overriding only ever changed what gets shown in a prompt as a
    few-shot example, never what the pipeline *does*. This blends the raw
    per-message confidence with the sender's reviewed reliability
    (approved / (approved + overridden), this kind only), weighted by how
    much history exists. A sender with a string of overrides drifts back
    into the review queue (and, for spam, can fall below the trash bar)
    even on a message the model itself called confident; a sender with a
    string of approvals needs fewer needless flags. None in, None out — a
    classification with no confidence score has nothing to blend against.
    Best-effort: any DB failure just returns the raw score unchanged,
    never blocks classification."""
    if raw_confidence is None:
        return None
    domain = _parse_sender_domain(sender)
    if not domain:
        return raw_confidence

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT
                    SUM(CASE WHEN review_action = 'approved' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN review_action = 'overridden' THEN 1 ELSE 0 END) as overridden
                FROM triage_classifications
                WHERE sender_domain = :domain AND kind = :kind AND reviewed = 1
            """), {"domain": domain, "kind": kind})
            row = result.fetchone()
    except Exception as e:
        logger.warning(f"[triage_repo] adjusted_confidence lookup failed for {domain}: {e}")
        return raw_confidence

    approved, overridden = row[0] or 0, row[1] or 0
    total = approved + overridden
    if total == 0:
        return raw_confidence

    reliability = approved / total
    weight = min(total / RELIABILITY_FULL_WEIGHT_AT, 1.0) * RELIABILITY_MAX_WEIGHT
    return raw_confidence * (1 - weight) + reliability * weight


# ── Write path ────────────────────────────────────────────────────────────

async def record_classification(
    *, kind: str, message_id: str, sender: str, subject: str | None = None,
    summary: str | None = None, category: str | None = None, urgency: str | None = None,
    action_needed: bool = False, confidence: float | None = None, verdict: str | None = None,
    sensitive: bool = False, action_taken: list[str] | None = None, needs_review: bool = False,
) -> int:
    """Upsert by (kind, message_id) — a Redis stream redelivery or a retried
    spam sweep pass must refresh what the pipeline decided, never create a
    duplicate row. If the row was already reviewed, review state (including
    needs_review) is left untouched: a retry must not silently erase or
    re-surface a human's earlier correction."""
    now = _now_iso()
    sender_domain = _parse_sender_domain(sender)
    action_taken_json = json.dumps(action_taken or [])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TriageClassification).where(
                TriageClassification.kind == kind,
                TriageClassification.message_id == message_id,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            row = TriageClassification(
                kind=kind, message_id=message_id, sender=sender, sender_domain=sender_domain,
                subject=subject, summary=summary, category=category, urgency=urgency,
                action_needed=1 if action_needed else 0, confidence=confidence, verdict=verdict,
                sensitive=1 if sensitive else 0, action_taken=action_taken_json,
                needs_review=1 if needs_review else 0, reviewed=0,
                created_at=now,
            )
            session.add(row)
        else:
            row.sender = sender
            row.sender_domain = sender_domain
            row.subject = subject
            row.summary = summary
            row.category = category
            row.urgency = urgency
            row.action_needed = 1 if action_needed else 0
            row.confidence = confidence
            row.verdict = verdict
            row.sensitive = 1 if sensitive else 0
            row.action_taken = action_taken_json
            if not row.reviewed:
                row.needs_review = 1 if needs_review else 0

        await session.commit()
        await session.refresh(row)
        row_id = row.id

    logger.info(f"[triage_repo] recorded {kind}:{message_id} needs_review={needs_review} (id={row_id})")
    return row_id


async def get_by_id(item_id: int) -> TriageClassification | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TriageClassification).where(TriageClassification.id == item_id))
        return result.scalar_one_or_none()


async def get_by_message_id(kind: str, message_id: str) -> TriageClassification | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TriageClassification).where(
                TriageClassification.kind == kind,
                TriageClassification.message_id == message_id,
            )
        )
        return result.scalar_one_or_none()


async def list_needs_review(kind: str | None = None, limit: int = 50) -> list[TriageClassification]:
    """needs_review=1, reviewed=0. Most-uncertain-first — SQLite sorts NULL
    first in ASC order, which is actually right here too: a classification
    with no confidence score at all (a parse failure) deserves a look at
    least as much as a merely low one, not less."""
    async with AsyncSessionLocal() as session:
        query = select(TriageClassification).where(
            TriageClassification.needs_review == 1,
            TriageClassification.reviewed == 0,
        )
        if kind:
            query = query.where(TriageClassification.kind == kind)
        query = query.order_by(
            TriageClassification.confidence.asc(), TriageClassification.created_at.desc()
        ).limit(max(1, min(limit, 200)))
        result = await session.execute(query)
        return list(result.scalars().all())


async def list_recent(kind: str | None = None, limit: int = 100) -> list[TriageClassification]:
    """Every classification NOT currently sitting in the needs-review
    queue — confidently-handled inbox items, confidently-trashed spam,
    already-reviewed items alike — most recent first, any kind, any
    confidence. This is what makes "I should be able to correct a
    confident, wrong decision" actually possible: correction access isn't
    gated behind the model's own uncertainty, only the *priority ordering*
    (list_needs_review, above) is. Overlaps with nothing in that queue by
    construction, so a card never appears in both sections at once."""
    async with AsyncSessionLocal() as session:
        query = select(TriageClassification).where(
            ~((TriageClassification.needs_review == 1) & (TriageClassification.reviewed == 0))
        )
        if kind:
            query = query.where(TriageClassification.kind == kind)
        query = query.order_by(TriageClassification.created_at.desc()).limit(max(1, min(limit, 300)))
        result = await session.execute(query)
        return list(result.scalars().all())


async def sender_reliability_summary(limit: int = 20) -> list[dict]:
    """approved/overridden reflect reviewed history only (unreviewed rows
    count toward total but neither bucket) — the same two numbers
    adjusted_confidence blends against, surfaced here so the dashboard
    shows the actual reasoning behind why a sender's classifications have
    started drifting toward or away from the review queue."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT sender_domain,
                   COUNT(*) as total,
                   SUM(CASE WHEN reviewed = 1 AND review_action = 'approved' THEN 1 ELSE 0 END) as approved,
                   SUM(CASE WHEN reviewed = 1 AND review_action = 'overridden' THEN 1 ELSE 0 END) as overridden,
                   MAX(created_at) as last_seen
            FROM triage_classifications
            WHERE sender_domain IS NOT NULL
            GROUP BY sender_domain
            ORDER BY total DESC
            LIMIT :limit
        """), {"limit": max(1, min(limit, 100))})
        return [
            {"sender_domain": r[0], "total": r[1], "approved": r[2] or 0, "overridden": r[3] or 0, "last_seen": r[4]}
            for r in result.fetchall()
        ]


# ── Review path ──────────────────────────────────────────────────────────

def _correction_text(row: TriageClassification) -> str:
    parts = [row.sender or "", row.subject or "", row.feedback_note or ""]
    return " | ".join(p for p in parts if p)


async def _index_correction(row: TriageClassification) -> None:
    """Best-effort, both indexes. Delete-then-insert keyed by rowid=row.id
    so re-reviewing the same item is idempotent instead of accumulating
    duplicate index entries. An indexing failure never loses the review
    itself — it just isn't retrievable as a future example, the same
    degrade philosophy transcript_repo.py already uses for its own
    embedding writes."""
    text_blob = _correction_text(row)
    if not text_blob:
        return

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("DELETE FROM triage_corrections_fts WHERE rowid = :id"), {"id": row.id})
            await session.execute(
                text("INSERT INTO triage_corrections_fts(rowid, correction_text) VALUES (:id, :text)"),
                {"id": row.id, "text": text_blob},
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[triage_repo] FTS indexing failed for id={row.id}: {e}")

    if not db.VEC_AVAILABLE:
        return
    try:
        vector = await local_embed(text_blob)
        if vector is None:
            return
        async with AsyncSessionLocal() as session:
            await session.execute(text("DELETE FROM triage_corrections_vec WHERE rowid = :id"), {"id": row.id})
            await session.execute(
                text("INSERT INTO triage_corrections_vec(rowid, embedding) VALUES (:id, :emb)"),
                {"id": row.id, "emb": json.dumps(vector)},
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[triage_repo] vector indexing failed for id={row.id}: {e}")


async def record_review(
    item_id: int, review_action: str,
    corrected_category: str | None = None, corrected_urgency: str | None = None,
    corrected_verdict: str | None = None, feedback_note: str | None = None,
) -> TriageClassification | None:
    """review_action: 'approved' | 'overridden'. Returns None if the item
    doesn't exist so the route can 404 instead of silently no-op'ing."""
    now = _now_iso()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TriageClassification).where(TriageClassification.id == item_id))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.reviewed = 1
        row.needs_review = 0
        row.review_action = review_action
        row.corrected_category = corrected_category
        row.corrected_urgency = corrected_urgency
        row.corrected_verdict = corrected_verdict
        row.feedback_note = feedback_note
        row.reviewed_at = now
        await session.commit()
        await session.refresh(row)

    await _index_correction(row)
    logger.info(f"[triage_repo] reviewed id={item_id} -> {review_action}")
    return row


# ── Retrieval path ───────────────────────────────────────────────────────

async def _sender_recent_matches(
    sender: str, sender_domain: str | None, kind: str, limit: int
) -> list[TriageClassification]:
    """Fast path: reviewed rows from this exact sender or sender_domain,
    kind-scoped, most-recently-reviewed first — "I've corrected this exact
    sender before" is the strongest signal available and needs no FTS/vec."""
    async with AsyncSessionLocal() as session:
        query = select(TriageClassification).where(
            TriageClassification.kind == kind,
            TriageClassification.reviewed == 1,
        )
        if sender_domain:
            query = query.where(
                (TriageClassification.sender == sender) | (TriageClassification.sender_domain == sender_domain)
            )
        else:
            query = query.where(TriageClassification.sender == sender)
        query = query.order_by(TriageClassification.reviewed_at.desc()).limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())


async def _fts_search_corrections(kind: str, fts_query: str, limit: int) -> list[int]:
    if not fts_query:
        return []
    async with AsyncSessionLocal() as session:
        try:
            rows = await session.execute(text("""
                SELECT t.id
                FROM triage_corrections_fts f
                JOIN triage_classifications t ON t.id = f.rowid
                WHERE triage_corrections_fts MATCH :query
                  AND t.kind = :kind AND t.reviewed = 1
                ORDER BY bm25(triage_corrections_fts)
                LIMIT :limit
            """), {"query": fts_query, "kind": kind, "limit": limit})
            return [r[0] for r in rows.fetchall()]
        except Exception as e:
            logger.warning(f"[triage_repo] FTS correction search failed: {e}")
            return []


async def _semantic_search_corrections(kind: str, query_text: str, limit: int) -> list[int]:
    if not db.VEC_AVAILABLE:
        return []
    vector = await local_embed(query_text)
    if vector is None:
        return []
    async with AsyncSessionLocal() as session:
        try:
            # vec0 doesn't know about kind/reviewed — over-fetch the KNN
            # pass and re-scope on our side, same shape as
            # transcript_repo._semantic_search.
            rows = await session.execute(text("""
                SELECT t.id
                FROM triage_corrections_vec v
                JOIN triage_classifications t ON t.id = v.rowid
                WHERE v.embedding MATCH :query AND k = :over_fetch
                  AND t.kind = :kind AND t.reviewed = 1
                ORDER BY v.distance
                LIMIT :limit
            """), {
                "query": json.dumps(vector), "over_fetch": limit * 4,
                "kind": kind, "limit": limit,
            })
            return [r[0] for r in rows.fetchall()]
        except Exception as e:
            logger.warning(f"[triage_repo] semantic correction search failed: {e}")
            return []


async def find_similar_corrections(
    sender: str, subject: str | None, kind: str, limit: int = 3
) -> list[TriageClassification]:
    """Best-effort — any failure here returns [] rather than raising;
    classification must never block on the learning loop being unavailable.

    1. Fast path: reviewed corrections from this exact sender/domain.
    2. If still short of `limit`, FTS+vec+RRF over sender+subject fills the
       rest (excluding anything the fast path already found) — catches
       topically similar corrections from *different* senders. Only
       reviewed=1 rows are ever candidates.
    """
    try:
        sender_domain = _parse_sender_domain(sender)
        fast = await _sender_recent_matches(sender, sender_domain, kind, limit)
        if len(fast) >= limit:
            return fast[:limit]

        remaining = limit - len(fast)
        seen_ids = {r.id for r in fast}
        query_text = f"{sender} {subject or ''}".strip()
        fts_query = _tokenize_query(query_text)
        over_fetch = max(remaining * 3, 6)

        fts_ids = await _fts_search_corrections(kind, fts_query, over_fetch)
        semantic_ids = await _semantic_search_corrections(kind, query_text, over_fetch)
        merged_ids = [i for i in _reciprocal_rank_fusion(fts_ids, semantic_ids) if i not in seen_ids][:remaining]

        if not merged_ids:
            return fast

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(TriageClassification).where(TriageClassification.id.in_(merged_ids)))
            by_id = {r.id: r for r in result.scalars().all()}

        return fast + [by_id[i] for i in merged_ids if i in by_id]
    except Exception as e:
        logger.warning(f"[triage_repo] find_similar_corrections failed: {e}")
        return []


def format_corrections_for_prompt(corrections: list[TriageClassification]) -> str:
    """Empty string when there's nothing to inject — both prompts behave
    exactly as they do today in that case. Otherwise a short block the
    classifier is asked to weigh, one line per correction — approvals and
    overrides phrased distinctly on purpose. A run of "you were wrong"
    lines for everything gives the model nothing to imitate; a confirmed-
    correct example is just as much a training signal as a corrected one,
    and reads as "keep doing this" rather than folding into the same
    corrective tone."""
    if not corrections:
        return ""
    lines = ["Past operator feedback on similar emails — weigh this when judging the current one:"]
    for c in corrections:
        note = f' — "{c.feedback_note}"' if c.feedback_note else ""
        subject = c.subject or ""
        judged = c.verdict or "unknown" if c.kind == "spam" else f"{c.category or '?'}/{c.urgency or '?'}"
        if c.review_action == "approved":
            lines.append(f'- From {c.sender}, subject "{subject}": judged {judged} — the operator confirmed that was correct{note}.')
        else:
            corrected = (c.corrected_verdict or "?") if c.kind == "spam" else \
                f"{c.corrected_category or c.category or '?'}/{c.corrected_urgency or c.urgency or '?'}"
            lines.append(f'- From {c.sender}, subject "{subject}": was judged {judged}, but the operator corrected it to {corrected}{note}.')
    return "\n".join(lines) + "\n"
