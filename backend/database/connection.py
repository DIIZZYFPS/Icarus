import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.database.models import Base

# Ensure the database directory exists. In Docker, this maps to the volume.
DB_DIR = "/workspace/memory"
# If running locally outside Docker for dev, fallback to a local dir
if not os.path.exists("/workspace"):
    DB_DIR = "./workspace/memory"

os.makedirs(DB_DIR, exist_ok=True)
DB_URL = f"sqlite+aiosqlite:///{DB_DIR}/icarus.db"

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

async def init_db():
    async with engine.begin() as conn:
        # Create all regular tables bounded to the Base metadata
        await conn.run_sync(Base.metadata.create_all)
        # Create FTS5 virtual table for memory full-text search.
        # SQLAlchemy's create_all() doesn't handle virtual tables,
        # so we create it manually. IF NOT EXISTS prevents errors on restart.
        await conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
            "USING fts5(entry, content='memory_entries', content_rowid='id')"
        ))

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
