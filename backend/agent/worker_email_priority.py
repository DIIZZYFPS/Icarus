import os
import re
import asyncio
import logging
import json
import httpx
from backend.agent.worker_base import WorkerBase

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://icarus-brain:11434")
EMAIL_SCORER_MODEL = os.getenv("EMAIL_SCORER_MODEL", "qwen2.5:4b")

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
        """Call configured Ollama model for priority scoring (0.0 to 1.0)."""
        prompt = f"""
Analyze the priority of the following email from 0.0 (very low) to 1.0 (very high).
Consider urgency, professional importance, and action required.
Respond ONLY with a single float value between 0.0 and 1.0.

Subject: {subject}
Body: {body[:1000]}
"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{OLLAMA_HOST}/api/generate", json={
                    "model": EMAIL_SCORER_MODEL,
                    "prompt": prompt,
                    "stream": False
                })
                resp.raise_for_status()
                data = resp.json()
                text = data.get("response", "").strip()
                # Extract the first valid float in the response (handles "0.8", "1", ".7", "0.7/1.0", etc.)
                match = re.search(r"(\d+\.?\d*|\d*\.\d+)", text)
                if match:
                    score = float(match.group(1))
                    return max(0.0, min(1.0, score))  # Clamp to [0.0, 1.0]
        except Exception as e:
            logger.error(f"Ollama scoring failed: {e}")
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
