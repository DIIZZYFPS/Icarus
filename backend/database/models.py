from sqlalchemy import Column, Integer, String, Float, Text, Index
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class MemoryEntry(Base):
    __tablename__ = 'memory_entries'

    id          = Column(Integer, primary_key=True, autoincrement=True)
    platform    = Column(String,  nullable=False, index=True)    # 'global', 'telegram', 'discord'
    user_id     = Column(String,  nullable=False, index=True)    # user id or 'system' for global
    category    = Column(String,  nullable=False, default='fact') # fact|preference|event|restart|project|summary
    visibility  = Column(String,  nullable=False, default='private')  # 'global' or 'private'
    entry       = Column(Text,    nullable=False)
    importance  = Column(Float,   nullable=False, default=1.0)   # 0.0–2.0; higher = more important
    created_at  = Column(String,  nullable=False)                # ISO8601 UTC string
    accessed_at = Column(String,  nullable=False)                # last retrieval timestamp (for LRU compaction)
    compacted   = Column(Integer, nullable=False, default=0)     # 0=live, 1=rolled into a summary entry
    source      = Column(String,  default='agent')               # agent|migration|restart|system|compaction

<<<<<<< Updated upstream
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
=======
    __table_args__ = (
        # Covers the most common query pattern: live entries for a given scope
        Index('ix_memory_scope', 'platform', 'user_id', 'visibility', 'compacted'),
    )
>>>>>>> Stashed changes
