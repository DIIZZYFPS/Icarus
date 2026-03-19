import os
import re
import asyncio
import logging
import json
from backend.agent.worker_base import WorkerBase

logger = logging.getLogger(__name__)

class EmailPriorityWorker(WorkerBase):
    def __init__(self):
        super().__init__(stream_name="tasks:email_priority", group_name="email_worker_group")
        self.heuristics = [
            (re.compile(r"\b(urgent|critical|important|asap)\b", re.I), 0.9),
            (re.compile(r"\b(meeting|invite|calendar)\b", re.I), 0.7),
            (re.compile(r"\b(newsletter|promotions|ads|sale)\b", re.I), 0.1),
            (re.compile(r"\b(no-reply|noreply)\b", re.I), 0.2),
        ]

    def _get_heuristic_score(self, subject: str, body: str) -> float | None:
        """Apply basic regex heuristics."""
        content = f"{subject} {body}"
        matches = []
        for pattern, score in self.heuristics:
            if pattern.search(content):
                matches.append(score)
        
        if matches:
            return max(matches)
        return None

    async def _get_model_score(self, subject: str, body: str) -> float:
        """Call Gemma 27B API for priority scoring (0.0 to 1.0).
        Uses the free tier via llm_router — no GPU contention with L1."""
        from backend.agent.llm_router import generate

        prompt = (
            "Analyze the priority of the following email from 0.0 (very low) to 1.0 (very high).\n"
            "Consider urgency, professional importance, and action required.\n"
            "Respond ONLY with a single float value between 0.0 and 1.0.\n\n"
            f"Subject: {subject}\n"
            f"Body: {body[:1000]}\n"
        )
        try:
            response = await generate(
                task_type="scoring",
                messages=[{"role": "user", "text": prompt}],
                max_tokens=10,
            )
            match = re.search(r"(\d+\.?\d*|\d*\.\d+)", response)
            if match:
                score = float(match.group(1))
                return max(0.0, min(1.0, score))
        except Exception as e:
            logger.error(f"API scoring failed: {e}")
        return 0.5  # Default middle-ground

    async def process_task(self, task_id: str, data: dict):
        subject = data.get("subject", "No Subject")
        body = data.get("body", "")
        message_id = data.get("message_id")  # Optional extra metadata; task_id is the correlation key

        logger.info(f"Scoring email: {subject} (task_id={task_id})")

        # 1. Try heuristics first (fast)
        score = self._get_heuristic_score(subject, body)
        
        # 2. If heuristic is inconclusive or "middle-ground", use model
        if score is None or 0.3 < score < 0.7:
            logger.info("Heuristics inconclusive, using model scoring...")
            score = await self._get_model_score(subject, body)

        logger.info(f"Final score for {subject}: {score}")

        # 3. Store result keyed by task_id (stream entry id) so callers using
        #    get_worker_result(task_id) can reliably retrieve it.
        result = {
            "task_id": task_id,
            "message_id": message_id,
            "score": score,
            "processed_by": self.consumer_name,
            "status": "completed"
        }
        await self.redis.set(f"icarus:email_score:{task_id}", json.dumps(result), ex=3600)
        # Also push to a response stream for any subscribers
        await self.redis.xadd("councilor:responses", {"type": "email_priority", "data": json.dumps(result)})

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    worker = EmailPriorityWorker()
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        worker.stop()
        sys.exit(0)
