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
from backend.agent.model_bank import get_model_context_length

logger = logging.getLogger(__name__)

# ── Scoring weights ─────────────────────────────────────────────────────────
SCORE_FTS_WEIGHT        = 0.5
SCORE_IMPORTANCE_WEIGHT = 0.3
SCORE_RECENCY_WEIGHT    = 0.2

# ── Compaction settings ──────────────────────────────────────────────────────
# Token-aware, not row-count-aware: a scope full of terse one-liners and a
# scope full of paragraph-length entries hit very different real costs at
# the same row count, so row count alone was never the right trigger. No
# local tokenizer is wired up (this runs against Qwen, not an OpenAI vocab
# tiktoken would model), so token counts here are a standard chars/4
# approximation — good enough to gate a threshold, not meant to be exact.
#
# The thresholds themselves scale off the model's own context length (see
# model_bank.get_model_context_length()) rather than a hand-picked constant
# — a number hardcoded against one context size silently stops making sense
# the moment the GGUF or its --ctx-size changes.
CHARS_PER_TOKEN = 4
COMPACTION_TOKEN_THRESHOLD_FRACTION    = 0.5   # trigger once live memory's estimated
                                                # footprint reaches half the model's context
COMPACTION_CHUNK_TOKEN_BUDGET_FRACTION = 0.15  # cap each summarization call's input at ~15%
                                                # of context — leaves room for the compaction
                                                # prompt, the summary output, and the chars/4
                                                # estimate's own slack
COMPACTION_ROW_CEILING = 300  # backstop: fires past this many rows regardless of the token
                               # estimate, so a flood of tiny entries can't grow the live set
                               # unboundedly just because each one looks cheap.
COMPACTION_KEEP_HEAD   = 10   # oldest entries always kept live — foundational/anchor context
COMPACTION_KEEP_RECENT = 30   # newest entries always kept live — recent context
COMPACTION_PIN_IMPORTANCE = 1.9  # entries at/above this importance are exempt from
                                  # compaction wherever they fall — a "critical"/"never"/
                                  # "always" fact doesn't get summarized away just because
                                  # it aged out of the recency window.

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


def _estimate_tokens(text: str) -> int:
    """Rough chars/4 token estimate — no local tokenizer wired up, so this is
    a budget gate, not an exact count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


async def get_compaction_token_threshold() -> int:
    """Live-derived trigger threshold — a fraction of the model's actual
    context length, not a fixed constant. See get_model_context_length()."""
    context_length = await get_model_context_length()
    return int(context_length * COMPACTION_TOKEN_THRESHOLD_FRACTION)


async def get_compaction_chunk_token_budget() -> int:
    """Live-derived per-summarization-call budget — a fraction of the
    model's actual context length, not a fixed constant."""
    context_length = await get_model_context_length()
    return int(context_length * COMPACTION_CHUNK_TOKEN_BUDGET_FRACTION)


def _to_int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _notify_session(platform: str, user_id: str, chat_id: str | None, text: str) -> None:
    """Best-effort status push into the session compaction ran for — not the
    operator's DM the way escalation/consultation results get relayed,
    literally the platform/chat the compacted entries came from, so a
    Discord server channel gets its own status message there too, not
    redirected somewhere private. Never raises: a notification hiccup must
    not abort compaction itself."""
    try:
        if platform == "telegram":
            from backend.routes.webhook import push_telegram_message
            target = _to_int_or_none(chat_id) or _to_int_or_none(user_id)
            if target:
                await push_telegram_message(target, text)
        elif platform == "discord":
            from backend.agent.discord_bot import push_discord_message
            await push_discord_message(_to_int_or_none(chat_id), text, user_id=user_id)
        # platform in ("global", "system", ...): no live session to notify — skip.
    except Exception as e:
        logger.warning(f"[memory_repo] Compaction status notification failed: {e}")


def _chunk_by_token_budget(
    entries: list["MemoryEntry"], token_budget: int
) -> list[list["MemoryEntry"]]:
    """Greedily group entries into chunks that stay under an estimated token
    budget, instead of a fixed row count — a handful of long entries and a
    pile of short ones shouldn't produce the same-sized call to the
    summarizer. A single entry that alone exceeds the budget still becomes
    its own chunk rather than being dropped or split."""
    chunks: list[list[MemoryEntry]] = []
    current: list[MemoryEntry] = []
    current_tokens = 0
    for entry in entries:
        entry_tokens = _estimate_tokens(entry.entry)
        if current and current_tokens + entry_tokens > token_budget:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(entry)
        current_tokens += entry_tokens
    if current:
        chunks.append(current)
    return chunks


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
    chat_id: str | None = None,
) -> int:
    """Insert a new MemoryEntry row. Returns the new row id.

    Category and importance are auto-detected from entry text when not provided.
    Fires a fire-and-forget compaction task if the live set exceeds the
    threshold. chat_id is the raw transport chat/channel id (not the
    conversation_id) — passed through only so a triggered compaction can
    post its status into the session it's compacting, not persisted on the
    row itself.
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
        asyncio.create_task(_maybe_compact(platform, user_id, conversation_id, chat_id))

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
    token_threshold: int | None = None,
    row_ceiling: int = COMPACTION_ROW_CEILING,
    conversation_id: str | None = None,
) -> bool:
    """Return True if this scope's live, non-summary entries have grown past
    the estimated token threshold, or past a hard row-count backstop
    regardless of token estimate (a flood of tiny entries shouldn't be able
    to grow the live set unboundedly just because each one looks cheap).
    token_threshold defaults to a fraction of the model's live-resolved
    context length (get_compaction_token_threshold()) rather than a fixed
    constant — pass one explicitly to override."""
    if token_threshold is None:
        token_threshold = await get_compaction_token_threshold()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(func.length(MemoryEntry.entry)), 0),
            )
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
        count, total_chars = result.one()

    if count >= row_ceiling:
        return True
    return (total_chars // CHARS_PER_TOKEN) >= token_threshold


async def compact_user_memory(
    platform: str,
    user_id: str,
    keep_recent: int = COMPACTION_KEEP_RECENT,
    keep_head: int = COMPACTION_KEEP_HEAD,
    token_budget: int | None = None,
    summarize_fn: Callable[[str], Awaitable[str]] | None = None,
    conversation_id: str | None = None,
    chat_id: str | None = None,
) -> int:
    """Compact the middle of a scope's memory by summarizing it via LLM.

    The oldest `keep_head` entries (foundational/anchor context — the
    closest thing this log has to a system prompt) and the newest
    `keep_recent` entries ("the last messages") stay live, untouched;
    everything chronologically between them is what's eligible for
    summarization. This mirrors how conversational context compaction
    normally works — protect the beginning and the end, compact the middle —
    rather than the old behavior of picking off whichever entries scored
    lowest on importance regardless of where they fell in time. Entries at
    or above COMPACTION_PIN_IMPORTANCE are exempt even inside the middle
    span. Chunks are sized by an estimated token budget rather than a fixed
    row count, so a handful of long entries don't get crammed into one
    oversized summarization call the way a fixed row count would allow.

    Posts a best-effort status update into the originating session (see
    _notify_session) when compaction starts and again when it finishes.
    chat_id is the raw transport chat/channel id, not the conversation_id —
    it's only used for that notification, never persisted.

    token_budget defaults to a fraction of the model's live-resolved context
    length (get_compaction_chunk_token_budget()) rather than a fixed
    constant — pass one explicitly to override.

    Returns the number of entries compacted (marked compacted=1).
    """
    if summarize_fn is None:
        summarize_fn = _default_summarize
    if token_budget is None:
        token_budget = await get_compaction_chunk_token_budget()

    async with AsyncSessionLocal() as session:
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
            .order_by(MemoryEntry.created_at.asc(), MemoryEntry.id.asc())
        )
        all_entries = list(result.scalars().all())

    if len(all_entries) <= keep_head + keep_recent:
        return 0

    middle = (
        all_entries[keep_head: len(all_entries) - keep_recent]
        if keep_recent > 0
        else all_entries[keep_head:]
    )
    to_compact = [e for e in middle if e.importance < COMPACTION_PIN_IMPORTANCE]

    if not to_compact:
        return 0

    await _notify_session(
        platform, user_id, chat_id,
        f"🗜️ Compacting memory — summarizing {len(to_compact)} older entries to keep "
        f"things lean. Recent context and pinned facts are untouched.",
    )

    total_compacted = 0
    chunks_written = 0

    for chunk in _chunk_by_token_budget(to_compact, token_budget):
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
        chunks_written += 1
        logger.info(f"[memory_repo] Compacted {len(chunk)} entries for {platform}:{user_id}")

    if total_compacted:
        await _notify_session(
            platform, user_id, chat_id,
            f"✅ Memory compaction done — {total_compacted} entries folded into "
            f"{chunks_written} summary note(s).",
        )

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
    chat_id: str | None = None,
) -> None:
    """Fire-and-forget wrapper: run compaction if threshold is exceeded."""
    try:
        if await should_compact(platform, user_id, conversation_id=conversation_id):
            count = await compact_user_memory(
                platform, user_id, conversation_id=conversation_id, chat_id=chat_id,
            )
            if count:
                logger.info(f"[memory_repo] Auto-compacted {count} entries for {platform}:{user_id}")
    except Exception as e:
        logger.error(f"[memory_repo] Auto-compaction error for {platform}:{user_id}: {e}")
