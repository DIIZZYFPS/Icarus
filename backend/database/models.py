from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, timezone

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")

class Conversation(Base):
    __tablename__ = 'conversations'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String, default="active")

    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey('conversations.id'), nullable=False)
    role = Column(String, nullable=False)  # 'user', 'model', 'system'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    conversation = relationship("Conversation", back_populates="messages")

class Transcript(Base):
    """Full-fidelity message log — every message, every platform, logged
    unconditionally (see backend/agent/transcript_repo.py). Separate from
    MemoryEntry: this is never summarized or deleted, and isn't injected into
    every prompt — it's the deep-search fallback the recall tool queries on
    demand, and the structural fix for cross-head continuity (a notification
    a worker sent is a row here whether or not any agent chose to remember
    it)."""
    __tablename__ = 'transcript'

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    created_at = Column(String, nullable=False, index=True)  # ISO 8601 timestamp


class TrackedItem(Base):
    """Structured, stateful life-entities — things with a lifecycle worth
    tracking current state for, not just a log of mentions: a job
    application (applied -> OA -> interview -> offer/reject), a bill
    (unpaid -> paid), a calendar event. Sibling to MemoryEntry, not a
    replacement — memory stays for narrative/preference facts that don't
    have a "current state"; this is for the ones that do.

    Uniqueness is (platform, user_id, item_type, entity_key) — the same
    entity_key upserts in place rather than creating duplicate rows, so
    "3 emails about the same application" becomes one row with an updated
    state, not three disconnected mentions.
    """
    __tablename__ = 'tracked_items'

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    item_type = Column(String, nullable=False, index=True)   # 'job_application', 'bill', 'calendar_event', ...
    entity_key = Column(String, nullable=False, index=True)  # stable identifier within item_type (e.g. normalized company name)
    state = Column(String, nullable=False)                   # current lifecycle state; meaning is item_type-specific
    summary = Column(Text, nullable=True)
    next_action = Column(Text, nullable=True)
    due_at = Column(String, nullable=True)                   # ISO 8601, nullable
    urgency = Column(String, nullable=True)
    payload = Column(Text, nullable=True)                    # JSON blob for item_type-specific fields
    source = Column(String, nullable=False)                  # 'email_triage', 'calendar', 'agent', ...
    notified = Column(Integer, default=0)                    # 0/1 — was the operator notified about the *current* state
    notified_at = Column(String, nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint('platform', 'user_id', 'item_type', 'entity_key', name='uq_tracked_item_identity'),
    )


class MemoryEntry(Base):
    __tablename__ = 'memory_entries'

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    category = Column(String, nullable=True)
    visibility = Column(String, default="private")  # 'private' or 'global'
    entry = Column(Text, nullable=False)
    importance = Column(Integer, default=5)  # 0-10, auto-scored
    created_at = Column(String, nullable=False)  # ISO 8601 timestamp
    accessed_at = Column(String, nullable=True)
    compacted = Column(Integer, default=0)  # 0=live, 1=compacted
    source = Column(String, default="agent")  # 'agent', 'system', 'user'
