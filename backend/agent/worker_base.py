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
    def __init__(self, stream_name: str, group_name: str, consumer_name: str = None):
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or f"worker-{uuid.uuid4().hex[:8]}"
        self.redis = get_redis_client()
        self.running = True

    async def setup(self):
        """Create consumer group if it doesn't exist."""
        try:
            await self.redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
            logger.info(f"Created consumer group {self.group_name} on stream {self.stream_name}")
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.info(f"Consumer group {self.group_name} already exists")
            else:
                logger.error(f"Error creating consumer group: {e}")

    @abstractmethod
    async def process_task(self, task_id: str, data: dict):
        """Implement task processing logic."""
        pass

    async def run(self):
        await self.setup()
        logger.info(f"Worker {self.consumer_name} started, listening on {self.stream_name}")

        # Register for heartbeat/health checks
        asyncio.create_task(self._heartbeat())

        while self.running:
            try:
                # Read from group
                # count=1, block=5000ms
                messages = await self.redis.xreadgroup(
                    self.group_name, self.consumer_name, {self.stream_name: ">"}, count=1, block=5000
                )

                if not messages:
                    continue

                for stream, msg_list in messages:
                    for msg_id, data in msg_list:
                        logger.info(f"Processing task {msg_id}")
                        try:
                            # Re-parse JSON if needed
                            task_data = {k: (json.loads(v) if isinstance(v, str) and (v.startswith('{') or v.startswith('[')) else v) for k, v in data.items()}
                            await self.process_task(msg_id, task_data)
                            # Ack message
                            await self.redis.xack(self.stream_name, self.group_name, msg_id)
                        except Exception as e:
                            logger.error(f"Error processing task {msg_id}: {e}")
                            # In a real system, we might move to DLQ or retry
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(1)

    async def _heartbeat(self):
        """Register worker in a hash for monitoring."""
        while self.running:
            try:
                await self.redis.hset("icarus:workers:health", self.consumer_name, json.dumps({
                    "stream": self.stream_name,
                    "last_seen": asyncio.get_event_loop().time(),
                    "status": "alive"
                }))
                await self.redis.expire("icarus:workers:health", 60)
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")
            await asyncio.sleep(10)

    def stop(self):
        self.running = False
