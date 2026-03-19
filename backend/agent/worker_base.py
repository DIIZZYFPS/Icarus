"""
worker_base.py — Base class for Redis Streams worker processes.

Provides:
  - Consumer group setup
  - Message consumption loop with blocking reads
  - Retry logic with exponential backoff
  - Dead Letter Queue (DLQ) for permanently failed tasks
  - Worker health heartbeat
"""

import os
import asyncio
import logging
import signal
import uuid
import json
from abc import ABC, abstractmethod
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)


class WorkerBase(ABC):
    def __init__(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str = None,
        max_retries: int = 3,
    ):
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or f"worker-{uuid.uuid4().hex[:8]}"
        self.redis = get_redis_client()
        self.running = True
        self.max_retries = max_retries
        self._retry_counts: dict[str, int] = {}

    async def setup(self):
        """Create consumer group if it doesn't exist."""
        try:
            await self.redis.xgroup_create(
                self.stream_name, self.group_name, id="0", mkstream=True
            )
            logger.info(
                f"Created consumer group {self.group_name} on stream {self.stream_name}"
            )
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.info(f"Consumer group {self.group_name} already exists")
            else:
                logger.error(f"Error creating consumer group: {e}")

    @abstractmethod
    async def process_task(self, task_id: str, data: dict):
        """Implement task processing logic."""
        pass

    async def _move_to_dlq(self, msg_id: str, data: dict, error: str):
        """Move a permanently failed task to the Dead Letter Queue."""
        dlq_stream = f"{self.stream_name}:dlq"
        try:
            dlq_entry = {
                "original_id": str(msg_id),
                "original_data": json.dumps(data),
                "error": str(error)[:500],
                "worker": self.consumer_name,
                "retries": str(self._retry_counts.get(str(msg_id), 0)),
            }
            await self.redis.xadd(dlq_stream, dlq_entry)
            logger.warning(
                f"Moved task {msg_id} to DLQ ({dlq_stream}) after "
                f"{self.max_retries} retries. Error: {error}"
            )
        except Exception as e:
            logger.error(f"Failed to move task {msg_id} to DLQ: {e}")
        finally:
            # Clean up retry counter
            self._retry_counts.pop(str(msg_id), None)

    async def run(self):
        await self.setup()
        logger.info(
            f"Worker {self.consumer_name} started, listening on {self.stream_name} "
            f"(max_retries={self.max_retries})"
        )

        # Register for heartbeat/health checks
        asyncio.create_task(self._heartbeat())

        while self.running:
            try:
                messages = await self.redis.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_name: ">"},
                    count=1,
                    block=5000,
                )

                if not messages:
                    continue

                for stream, msg_list in messages:
                    for msg_id, data in msg_list:
                        msg_key = str(msg_id)
                        logger.info(f"Processing task {msg_id}")

                        try:
                            # Re-parse JSON if needed
                            task_data = {
                                k: (
                                    json.loads(v)
                                    if isinstance(v, str)
                                    and (v.startswith("{") or v.startswith("["))
                                    else v
                                )
                                for k, v in data.items()
                            }
                            await self.process_task(msg_id, task_data)
                            # Ack message on success
                            await self.redis.xack(
                                self.stream_name, self.group_name, msg_id
                            )
                            self._retry_counts.pop(msg_key, None)

                        except Exception as e:
                            retry_count = self._retry_counts.get(msg_key, 0) + 1
                            self._retry_counts[msg_key] = retry_count

                            if retry_count >= self.max_retries:
                                logger.error(
                                    f"Task {msg_id} failed permanently after "
                                    f"{retry_count} retries: {e}"
                                )
                                await self._move_to_dlq(msg_id, data, str(e))
                                await self.redis.xack(
                                    self.stream_name, self.group_name, msg_id
                                )
                            else:
                                # Exponential backoff
                                delay = 2 ** (retry_count - 1)
                                logger.warning(
                                    f"Task {msg_id} failed (attempt {retry_count}/"
                                    f"{self.max_retries}), retrying in {delay}s: {e}"
                                )
                                await asyncio.sleep(delay)
                                # Don't ack — message will be re-delivered

            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(1)

    async def _heartbeat(self):
        """Register worker in a hash for monitoring."""
        while self.running:
            try:
                await self.redis.hset(
                    "icarus:workers:health",
                    self.consumer_name,
                    json.dumps(
                        {
                            "stream": self.stream_name,
                            "last_seen": asyncio.get_event_loop().time(),
                            "status": "alive",
                        }
                    ),
                )
                await self.redis.expire("icarus:workers:health", 60)
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")
            await asyncio.sleep(10)

    def stop(self):
        self.running = False
