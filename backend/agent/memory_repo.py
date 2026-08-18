"""
memory_repo.py — Single module owning all database interactions for Icarus memory.

All other modules (tools.py, processor.py, main.py) interact with memory
exclusively through this module's public API. Direct use of AsyncSessionLocal
outside this file is discouraged.
"""

import re
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from sqlalchemy import select, update, text, func, or_, and_
from backend.database.connection import AsyncSessionLocal
from backend.database.models import MemoryEntry

logger = logging.getLogger(__name__)

# ── Scoring weights ─────────────────────────────────────────────────────────
SCORE_FTS_WEIGHT        = 0.5
SCORE_IMPORTANCE_WEIGHT = 0.3
SCORE_RECENCY_WEIGHT    = 0.2

# ── Compaction settings ──────────────────────────────────────────────────────
COMPACTION_THRESHOLD  = 100   # live entries per user before compaction fires
COMPACTION_CHUNK_SIZE = 20    # max entries per summarization call
COMPACTION_KEEP_RECENT = 30   # always keep the N most recent entries live

# ── Auto-category heuristics ─────────────────────────────────────────────────
_CATEGORY_RULES = [
    ('restart',    re.compile(r'\b(restart|reboot|container)\b',                    re.I)),
    ('preference', re.compile(r'\b(prefer|always|never|style|format)\b',            re.I)),
    ('project',    re.compile(r'\b(project|repo|branch|pr|deploy|issue)\b',         re.I)),
    ('event',      re.compile(r'\b(completed|failed|escalated|dispatched|finished)\b', re.I)),
]

# ── Auto-importance heuristics ───────────────────────────────────────────────
_IMPORTANCE_BOOSTS = [
    (+0.5, re.compile(r'\b(critical|always|never|broken|failed|error)\b',    re.I)),
    (+0.3, re.compile(r'\b(prefer|important|remember|key)\b',                re.I)),
    (-0.3, re.compile(r'\b(fyi|note|minor|casual)\b',                        re.I)),
]

# ── Compaction prompt ────────────────────────────────────────────────────────
_COMPACT_PROMPT = (
    "You are a memory compaction assistant for an AI agent named Icarus. "
    "The following are timestamped memory log entries. Summarize them into "
    "2–5 concise sentences, preserving all factual details, preferences, and "
    "outcomes. Discard duplicates and redundant phrasing.\n\nENTRIES:\n"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auto_category(entry: str) -> str:
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(entry):
            return category
    return 'fact'


def _auto_importance(entry: str) -> float:
    importance = 1.0
    for delta, pattern in _IMPORTANCE_BOOSTS:
        if pattern.search(entry):
            importance += delta
    return max(0.1, min(2.0, importance))


def _tokenize_query(query: str) -> str:
    """Produce a space-separated FTS5 query string from the user message.
    Strips punctuation, lowercases, deduplicates, and drops single-char tokens."""
    tokens = re.sub(r"[^\w\s]", " ", query.lower()).split()
    seen = set()
    result = []
    for t in tokens:
        if len(t) > 1 and t not in seen:
            seen.add(t)
            result.append(t)
    return " ".join(result)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def store_entry(
    platform: str,
    user_id: str,
    entry: str,
    visibility: str = "private",
    conversation_id: str | None = None,
    category: str | None = None,
    importance: float | None = None,
    source: str = "agent",
) -> int:
    """Insert a new MemoryEntry row. Returns the new row id.

    Category and importance are auto-detected from entry text when not provided.
    Fires a fire-and-forget compaction task if the live count exceeds the threshold.
    """
    now = _now_iso()
    resolved_category   = category   if category   is not None else _auto_category(entry)
    resolved_importance = importance if importance is not None else _auto_importance(entry)

    async with AsyncSessionLocal() as session:
        row = MemoryEntry(
            platform=platform,
            user_id=user_id,
            conversation_id=conversation_id if visibility != "global" else None,
            category=resolved_category,
            visibility=visibility,
            entry=entry,
            importance=resolved_importance,
            created_at=now,
            accessed_at=now,
            compacted=0,
            source=source,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        row_id = row.id
        logger.debug(f"[memory_repo] stored entry id={row_id} platform={platform} user={user_id} cat={resolved_category}")

    # Fire-and-forget compaction check (does not block the caller)
    if visibility == "private":
        asyncio.create_task(_maybe_compact(platform, user_id, conversation_id))

    return row_id


async def retrieve_relevant(
    platform: str,
    user_id: str,
    query: str,
    limit: int = 30,
    read_only: bool = False,
    conversation_id: str | None = None,
) -> list[MemoryEntry]:
    """Return up to `limit` MemoryEntry rows relevant to the query.

    Scoring combines FTS5 BM25, entry importance, and recency decay.
    Falls back to a pure recency+importance sort when the query produces no FTS hits.
    When read_only=True, only global/system entries are returned.
    """
    fts_query = _tokenize_query(query)
    now = datetime.now(timezone.utc)
    entries: list[MemoryEntry] = []

    async with AsyncSessionLocal() as session:
        # ── FTS path ──────────────────────────────────────────────────────────
        if fts_query:
            try:
                if read_only:
                    scope_clause = "me.visibility = 'global'"
                elif conversation_id is None:
                    scope_clause = "(me.visibility = 'global' OR (me.platform = :platform AND me.user_id = :user_id AND me.conversation_id IS NULL))"
                else:
                    scope_clause = "(me.visibility = 'global' OR (me.platform = :platform AND me.user_id = :user_id AND me.conversation_id = :conversation_id))"

                rows = await session.execute(text(f"""
                    SELECT me.id, me.platform, me.user_id, me.category, me.visibility,
                           me.entry, me.importance, me.created_at, me.accessed_at,
                           me.compacted, me.source,
                           bm25(memory_fts) AS fts_score
                    FROM memory_entries me
                    JOIN memory_fts ON memory_fts.rowid = me.id
                    WHERE memory_fts MATCH :query
                      AND me.compacted = 0
                      AND {scope_clause}
                    ORDER BY fts_score
                    LIMIT :over_fetch
                """), {
                    "query": fts_query,
                    "platform": platform,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "over_fetch": limit * 3,
                })
                raw_rows = rows.fetchall()
            except Exception as e:
                logger.warning(f"[memory_repo] FTS query failed ({e}), falling back to recency sort")
                raw_rows = []
        else:
            raw_rows = []

        if raw_rows:
            # Re-rank: combine FTS BM25, importance, recency
            scored = []
            for r in raw_rows:
                fts_score  = r.fts_score        # negative in SQLite; more negative = better match
                importance = r.importance
                try:
                    created = datetime.fromisoformat(r.created_at.replace("Z", "+00:00"))
                    days_old = max(0, (now - created).total_seconds() / 86400)
                except Exception:
                    days_old = 30
                recency = 1.0 / (1 + days_old / 30)
                final = (-fts_score * SCORE_FTS_WEIGHT) + (importance * SCORE_IMPORTANCE_WEIGHT) + (recency * SCORE_RECENCY_WEIGHT)
                scored.append((final, r))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = [r for _, r in scored[:limit]]

            # Re-fetch as ORM objects so callers get typed MemoryEntry instances
            ids = [r.id for r in top]
            result = await session.execute(
                select(MemoryEntry).where(MemoryEntry.id.in_(ids))
            )
            entries = list(result.scalars().all())
            # Preserve scored order
            id_order = {rid: idx for idx, rid in enumerate(ids)}
            entries.sort(key=lambda e: id_order.get(e.id, 999))
        else:
            # ── Fallback: recency + importance ───────────────────────────────
            if read_only:
                scope_filter = MemoryEntry.visibility == 'global'
            elif conversation_id is None:
                scope_filter = or_(
                    MemoryEntry.visibility == 'global',
                    and_(
                        MemoryEntry.platform == platform,
                        MemoryEntry.user_id == user_id,
                        MemoryEntry.conversation_id.is_(None),
                    )
                )
            else:
                scope_filter = or_(
                    MemoryEntry.visibility == 'global',
                    and_(
                        MemoryEntry.platform == platform,
                        MemoryEntry.user_id == user_id,
                        MemoryEntry.conversation_id == conversation_id,
                    )
                )

            result = await session.execute(
                select(MemoryEntry)
                .where(MemoryEntry.compacted == 0)
                .where(scope_filter)
                .order_by(MemoryEntry.importance.desc(), MemoryEntry.created_at.desc())
                .limit(limit)
            )
            entries = list(result.scalars().all())

        # Update accessed_at for all returned entries
        if entries:
            entry_ids = [e.id for e in entries]
            await session.execute(
                update(MemoryEntry)
                .where(MemoryEntry.id.in_(entry_ids))
                .values(accessed_at=_now_iso())
            )
            await session.commit()

    return entries


async def should_compact(
    platform: str,
    user_id: str,
    threshold: int = COMPACTION_THRESHOLD,
    conversation_id: str | None = None,
) -> bool:
    """Return True if live non-summary entry count for this scope exceeds threshold."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count())
            .select_from(MemoryEntry)
            .where(
                MemoryEntry.platform == platform,
                MemoryEntry.user_id == user_id,
                MemoryEntry.compacted == 0,
                MemoryEntry.category != 'summary',
                MemoryEntry.conversation_id == conversation_id
                if conversation_id is not None
                else MemoryEntry.conversation_id.is_(None),
            )
        )
        count = result.scalar_one()
    return count >= threshold


async def compact_user_memory(
    platform: str,
    user_id: str,
    keep_recent: int = COMPACTION_KEEP_RECENT,
    summarize_fn: Callable[[str], Awaitable[str]] | None = None,
    conversation_id: str | None = None,
) -> int:
    """Compact older memory entries by summarizing them via LLM.

    Selects all non-compacted, non-summary entries ordered by importance ASC,
    created_at ASC (least important oldest first). Skips the `keep_recent` most
    recently created entries. Chunks the rest and summarizes each chunk.

    Returns the number of entries compacted (marked compacted=1).
    """
    if summarize_fn is None:
        summarize_fn = _default_summarize

    async with AsyncSessionLocal() as session:
        # Get all live non-summary entries, least important + oldest first
        result = await session.execute(
            select(MemoryEntry)
            .where(
                MemoryEntry.platform == platform,
                MemoryEntry.user_id == user_id,
                MemoryEntry.compacted == 0,
                MemoryEntry.category != 'summary',
                MemoryEntry.conversation_id == conversation_id
                if conversation_id is not None
                else MemoryEntry.conversation_id.is_(None),
            )
            .order_by(MemoryEntry.importance.asc(), MemoryEntry.created_at.asc())
        )
        all_entries = list(result.scalars().all())

    if len(all_entries) <= keep_recent:
        return 0

    # Keep the most recent `keep_recent` entries live regardless
    to_compact = all_entries[:-keep_recent] if keep_recent > 0 else all_entries

    if not to_compact:
        return 0

    total_compacted = 0

    # Chunk and summarize
    for i in range(0, len(to_compact), COMPACTION_CHUNK_SIZE):
        chunk = to_compact[i:i + COMPACTION_CHUNK_SIZE]
        text_block = "\n".join(
            f"[{e.created_at}] [{e.category.upper()}] {e.entry}" for e in chunk
        )

        try:
            summary_text = await summarize_fn(text_block)
        except Exception as e:
            logger.error(f"[memory_repo] Compaction summarize_fn failed: {e}. Aborting chunk.")
            continue  # Don't compact entries whose summary failed

        # Determine visibility of summary: global if any source was global
        chunk_visibility = "global" if any(e.visibility == "global" for e in chunk) else "private"
        now = _now_iso()

        async with AsyncSessionLocal() as session:
            # Insert summary entry
            summary_row = MemoryEntry(
                platform=platform,
                user_id=user_id,
                conversation_id=conversation_id,
                category='summary',
                visibility=chunk_visibility,
                entry=summary_text,
                importance=1.5,
                created_at=now,
                accessed_at=now,
                compacted=0,
                source='compaction',
            )
            session.add(summary_row)

            # Mark source entries as compacted
            chunk_ids = [e.id for e in chunk]
            await session.execute(
                update(MemoryEntry)
                .where(MemoryEntry.id.in_(chunk_ids))
                .values(compacted=1)
            )
            await session.commit()

        total_compacted += len(chunk)
        logger.info(f"[memory_repo] Compacted {len(chunk)} entries for {platform}:{user_id}")

    return total_compacted


async def migrate_from_log(log_path: str) -> int:
    """Parse a memory.log flat file and insert entries into SQLite as MemoryEntry rows.

    Idempotent: skips lines whose (created_at, entry) combination already exists.
    On completion, renames log_path → log_path + '.migrated' to prevent re-processing.
    Returns the number of new rows inserted.
    """
    import os

    if not os.path.exists(log_path):
        return 0

    # Regex: [TIMESTAMP] [TAG] entry text
    line_re = re.compile(
        r'^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]\s+\[([^\]]+)\]\s+(.+)$'
    )

    inserted = 0

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        logger.error(f"[memory_repo] Migration: failed to read {log_path}: {e}")
        return 0

    async with AsyncSessionLocal() as session:
        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            m = line_re.match(raw_line)
            if not m:
                logger.debug(f"[memory_repo] Migration: skipping unparseable line: {raw_line[:80]}")
                continue

            timestamp, tag, entry_text = m.group(1), m.group(2), m.group(3).strip()

            # Determine platform/user/visibility from tag
            tag_upper = tag.upper()
            if tag_upper in ('GLOBAL', 'SYSTEM'):
                plat, uid, vis = 'global', 'system', 'global'
                cat = 'fact'
            elif tag_upper == 'RESTART':
                plat, uid, vis = 'global', 'system', 'global'
                cat = 'restart'
            elif ':' in tag:
                parts = tag.split(':', 1)
                plat, uid, vis = parts[0].lower(), parts[1], 'private'
                cat = _auto_category(entry_text)
            else:
                plat, uid, vis = 'global', 'system', 'global'
                cat = 'fact'

            # Idempotency check: skip if (created_at, entry) already exists
            exists = await session.execute(
                select(MemoryEntry.id)
                .where(MemoryEntry.created_at == timestamp)
                .where(MemoryEntry.entry == entry_text)
                .limit(1)
            )
            if exists.scalar_one_or_none() is not None:
                continue

            row = MemoryEntry(
                platform=plat,
                user_id=uid,
                category=cat,
                visibility=vis,
                entry=entry_text,
                importance=_auto_importance(entry_text),
                created_at=timestamp,
                accessed_at=timestamp,
                compacted=0,
                source='migration',
            )
            session.add(row)
            inserted += 1

        if inserted > 0:
            await session.commit()

    # Always rename the log to mark migration as complete and prevent re-processing on next boot
    migrated_path = log_path + '.migrated'
    try:
        os.rename(log_path, migrated_path)
        logger.info(f"[memory_repo] Migration complete: {inserted} entries inserted, log renamed to {migrated_path}")
    except Exception as e:
        logger.warning(f"[memory_repo] Migration: could not rename log file: {e}")

    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _default_summarize(text_block: str) -> str:
    """Summarize a block of memory entries using the local model."""
    from backend.agent.local_llm import local_generate
    return await local_generate(
        messages=[{"role": "user", "text": _COMPACT_PROMPT + text_block + "\n\nSUMMARY:"}],
        max_tokens=400,
    )


async def _maybe_compact(
    platform: str,
    user_id: str,
    conversation_id: str | None = None,
) -> None:
    """Fire-and-forget wrapper: run compaction if threshold is exceeded."""
    try:
        if await should_compact(platform, user_id, conversation_id=conversation_id):
            count = await compact_user_memory(platform, user_id, conversation_id=conversation_id)
            if count:
                logger.info(f"[memory_repo] Auto-compacted {count} entries for {platform}:{user_id}")
    except Exception as e:
        logger.error(f"[memory_repo] Auto-compaction error for {platform}:{user_id}: {e}")
