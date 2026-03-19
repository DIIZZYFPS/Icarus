from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
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
