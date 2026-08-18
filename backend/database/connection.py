import os
import logging
from sqlalchemy import text, event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.database.models import Base

logger = logging.getLogger(__name__)

# Ensure the database directory exists. In Docker, this maps to the volume.
DB_DIR = "/workspace/memory"
# If running locally outside Docker for dev, fallback to a local dir
if not os.path.exists("/workspace"):
    DB_DIR = "./workspace/memory"

os.makedirs(DB_DIR, exist_ok=True)
DB_URL = f"sqlite+aiosqlite:///{DB_DIR}/icarus.db"

# Embedding dimension for the transcript_vec table — must match whatever
# model LOCAL_EMBEDDING_URL serves. Only matters if that's configured; the
# table just goes unused otherwise. Changing this after the table exists
# requires dropping/recreating it. Default matches Qwen3-Embedding-0.6B.
EMBEDDING_DIM = int(os.getenv("LOCAL_EMBEDDING_DIM", "1024"))

engine = create_async_engine(
    DB_URL,
    echo=False,
    connect_args={"check_same_thread": False}
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Set by _load_sqlite_extensions() on the first successful connection.
# search code (transcript_repo.py) checks this before touching transcript_vec
# — vector search degrades to FTS-only when the extension didn't load.
VEC_AVAILABLE = False


@event.listens_for(engine.sync_engine, "connect")
def _load_sqlite_extensions(dbapi_connection, connection_record):
    """Load the sqlite-vec extension onto every new DBAPI connection.

    SQLAlchemy's async sqlite dialect wraps aiosqlite, which wraps the real
    sqlite3.Connection — enable_load_extension/load_extension aren't on the
    async wrapper directly, so we reach through to the underlying connection.
    This touches private attributes (aiosqlite/SQLAlchemy internals), so it's
    defensive: any failure here just means semantic search stays off and FTS5
    still works fine.
    """
    global VEC_AVAILABLE
    try:
        raw = dbapi_connection._connection._conn  # aiosqlite.Connection -> sqlite3.Connection
        raw.enable_load_extension(True)
        try:
            import sqlite_vec
            sqlite_vec.load(raw)
            VEC_AVAILABLE = True
        finally:
            raw.enable_load_extension(False)
    except ImportError:
        logger.info("[db] sqlite-vec not installed — semantic search disabled, FTS5-only.")
    except Exception as e:
        logger.warning(f"[db] Failed to load sqlite-vec extension — semantic search disabled: {e}")


async def init_db():
    async with engine.begin() as conn:
        # Create all regular tables bound to the Base metadata
        await conn.run_sync(Base.metadata.create_all)

        # create_all only creates missing *tables* — it never alters an
        # existing one, so a tracked_items table from before the dismiss
        # feature existed won't pick up these columns on its own. Add them
        # defensively; SQLite has no "ADD COLUMN IF NOT EXISTS" on the
        # versions this ships against, so a duplicate-column error is the
        # expected/ignored outcome on every run after the first.
        for ddl in (
            "ALTER TABLE tracked_items ADD COLUMN dismissed INTEGER DEFAULT 0",
            "ALTER TABLE tracked_items ADD COLUMN dismissed_at VARCHAR",
            "ALTER TABLE tracked_items ADD COLUMN message_id VARCHAR",
            "ALTER TABLE transcript ADD COLUMN conversation_id VARCHAR",
            "ALTER TABLE memory_entries ADD COLUMN conversation_id VARCHAR",
        ):
            try:
                await conn.execute(text(ddl))
            except Exception:
                pass  # column already exists

        # ── memory_entries FTS5 ──────────────────────────────────────────
        await conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
            "USING fts5(entry, content='memory_entries', content_rowid='id')"
        ))
        # External-content FTS5 tables don't sync themselves — they need
        # explicit triggers, or MATCH queries silently return zero rows
        # forever (which is exactly what happened here: memory_fts existed
        # but nothing ever populated it, so every search silently fell back
        # to the recency+importance path). Add the sync triggers...
        await conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_entries BEGIN
                INSERT INTO memory_fts(rowid, entry) VALUES (new.id, new.entry);
            END
        """))
        await conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_entries BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, entry) VALUES('delete', old.id, old.entry);
            END
        """))
        await conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_entries BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, entry) VALUES('delete', old.id, old.entry);
                INSERT INTO memory_fts(rowid, entry) VALUES (new.id, new.entry);
            END
        """))
        # ...and backfill whatever was already inserted before the triggers existed.
        await conn.execute(text("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')"))

        # ── transcript FTS5 ───────────────────────────────────────────────
        await conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts "
            "USING fts5(content, content='transcript', content_rowid='id')"
        ))
        await conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS transcript_ai AFTER INSERT ON transcript BEGIN
                INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
            END
        """))
        await conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS transcript_ad AFTER DELETE ON transcript BEGIN
                INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content);
            END
        """))
        await conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS transcript_au AFTER UPDATE ON transcript BEGIN
                INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content);
                INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
            END
        """))
        await conn.execute(text("INSERT INTO transcript_fts(transcript_fts) VALUES ('rebuild')"))

        # ── transcript semantic index (optional) ────────────────────────
        if VEC_AVAILABLE:
            try:
                await conn.execute(text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS transcript_vec "
                    f"USING vec0(embedding float[{EMBEDDING_DIM}])"
                ))
            except Exception as e:
                logger.warning(f"[db] Failed to create transcript_vec table: {e}")

        # ── triage correction index (FTS5 + optional vec0) ──────────────
        # Deliberately NOT external-content + trigger-synced like memory_fts/
        # transcript_fts above: triage_classifications gets a row for every
        # classified email, but only *reviewed* rows are useful correction
        # examples — indexing all of them would bury real corrections in
        # noise. So both the FTS and vec side are written explicitly from
        # application code (triage_repo.record_review()), not DB triggers —
        # the same explicit-insert style transcript_vec already uses just
        # above (embedding can't happen inside a synchronous SQL trigger
        # anyway), just applied to the FTS side too so both indexes are
        # written from one call site.
        await conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS triage_corrections_fts "
            "USING fts5(correction_text)"
        ))
        if VEC_AVAILABLE:
            try:
                await conn.execute(text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS triage_corrections_vec "
                    f"USING vec0(embedding float[{EMBEDDING_DIM}])"
                ))
            except Exception as e:
                logger.warning(f"[db] Failed to create triage_corrections_vec table: {e}")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
