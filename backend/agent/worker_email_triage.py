"""
worker_email_triage.py — Email triage worker for Icarus.

Consumes new-email events from the `tasks:email_triage` Redis stream,
classifies each email via Gemma 27B, applies Gmail labels, archives the
message out of the inbox, and notifies DIIZZY via Discord for actionable items.
"""

import os
import re
import json
import logging
from backend.agent.worker_base import WorkerBase

logger = logging.getLogger(__name__)

# ── Classification prompt ────────────────────────────────────────────────────

CLASSIFY_PROMPT = """You are an email triage assistant. Analyze the email below and respond with a JSON object ONLY — no extra text, no markdown fences.

The JSON must contain exactly these fields:
- "category": one of "jobs", "bills", "shopping", "social", "newsletters", "important", "other"
- "urgency": one of "low", "medium", "high", "critical"
- "summary": one sentence summarizing the email
- "action_needed": true or false (does the recipient need to do something?)
- "details": an object with extracted info, or null if not applicable:
  - For jobs: {{"company": "...", "status": "..."}} (e.g. status = "interview invite", "rejection", "offer", "OA sent", "application received")
  - For bills: {{"amount": "...", "due_date": "..."}} (e.g. "$120.00", "2026-03-25")
  - Otherwise: null

EMAIL:
From: {sender}
Subject: {subject}
Date: {date}

{body}

Respond with the JSON object only."""

# Map category → Gmail label
CATEGORY_TO_LABEL = {
    "jobs":        "Icarus/Jobs",
    "bills":       "Icarus/Bills",
    "shopping":    "Icarus/Shopping",
    "social":      "Icarus/Social",
    "newsletters": "Icarus/Newsletters",
    "important":   "Icarus/Important",
    "other":       "Icarus/Other",
}


class EmailTriageWorker(WorkerBase):
    def __init__(self):
        super().__init__(
            stream_name="tasks:email_triage",
            group_name="email_triage_group",
            max_retries=3,
        )

    async def _classify_email(self, sender: str, subject: str, date: str, body: str) -> dict | None:
        """Call Gemma 27B to classify the email. Returns parsed JSON or None."""
        from backend.agent.llm_router import generate

        prompt = CLASSIFY_PROMPT.format(
            sender=sender,
            subject=subject,
            date=date,
            body=body[:2000],  # Truncate long emails
        )

        try:
            response = await generate(
                task_type="scoring",
                messages=[{"role": "user", "text": prompt}],
                max_tokens=512,
            )

            # Strip markdown fences if the model produces them
            cleaned = response.strip()
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"[triage] Failed to parse classification JSON: {e}\nRaw: {response[:300]}")
            return None
        except Exception as e:
            logger.error(f"[triage] Classification failed: {e}")
            return None

    def _validate_classification(self, classification: dict) -> dict:
        """Ensure classification has all expected fields with correct types."""
        valid_categories = {"jobs", "bills", "shopping", "social", "newsletters", "important", "other"}
        valid_urgencies = {"low", "medium", "high", "critical"}

        cat = classification.get("category", "other")
        if cat not in valid_categories:
            cat = "other"

        urg = classification.get("urgency", "low")
        if urg not in valid_urgencies:
            urg = "low"

        details = classification.get("details")
        if not isinstance(details, dict):
            details = None

        return {
            "category": cat,
            "urgency": urg,
            "summary": str(classification.get("summary", ""))[:200],
            "action_needed": bool(classification.get("action_needed", False)),
            "details": details,
        }

    def _extract_email_parts(self, message: dict) -> tuple[str, str, str, str]:
        """Extract sender, subject, date, and body from a Gmail message."""
        headers = message.get("payload", {}).get("headers", [])
        header_map = {h["name"].lower(): h["value"] for h in headers}

        sender = header_map.get("from", "Unknown")
        subject = header_map.get("subject", "(no subject)")
        date = header_map.get("date", "Unknown")

        # Try snippet first (fast), then decode body
        body = message.get("snippet", "")
        if not body:
            payload = message.get("payload", {})
            body = self._decode_body(payload)

        return sender, subject, date, body

    def _decode_body(self, payload: dict, depth: int = 0) -> str:
        """Recursively decode message body from payload."""
        if depth > 5:
            return ""

        # Direct body data
        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            import base64
            try:
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            except Exception:
                return ""

        # Multipart — look for text/plain first, then text/html
        parts = payload.get("parts", [])
        text_part = ""
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    import base64
                    try:
                        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    except Exception:
                        pass
            elif mime == "text/html" and not text_part:
                data = part.get("body", {}).get("data", "")
                if data:
                    import base64
                    try:
                        text_part = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    except Exception:
                        pass
            elif mime.startswith("multipart/"):
                nested = self._decode_body(part, depth + 1)
                if nested:
                    return nested

        return text_part

    def _normalize_key(self, text: str) -> str:
        """Collapse a free-text identifier into a stable-ish entity key."""
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"

    async def _track_item(self, classification: dict, sender: str, subject: str) -> int | None:
        """Upsert this classification into tracked_items if it's a category
        with an actual lifecycle (jobs, bills) — not everything is; shopping/
        social/newsletters/other don't have a "current state" worth tracking,
        they're just mentions. Returns the tracked item's row id, or None."""
        category = classification.get("category")
        details = classification.get("details") or {}

        if category == "jobs":
            company = details.get("company") or sender
            entity_key = self._normalize_key(company)
            state = details.get("status") or "unknown"
        elif category == "bills":
            # No invoice/account number is extracted today — sender+due_date
            # is the best stable-ish identity available. A recurring bill
            # from the same sender with a different due date next month
            # correctly becomes a new row; a reminder about the same bill
            # should share the same due date and update in place.
            due_date = details.get("due_date") or "unknown-date"
            entity_key = self._normalize_key(f"{sender}-{due_date}")
            state = "unpaid"
        else:
            return None

        from backend.agent.tracked_items_repo import upsert_item
        return await upsert_item(
            platform="discord",
            user_id=os.environ.get("DISCORD_OPERATOR_ID", "0"),
            item_type="job_application" if category == "jobs" else "bill",
            entity_key=entity_key,
            state=state,
            summary=classification.get("summary"),
            urgency=classification.get("urgency"),
            payload=details or None,
            source="email_triage",
        )

    async def _notify_discord(self, classification: dict, sender: str, subject: str):
        """Push notification to the Councilor response queue for heartbeat delivery.

        Uses the same pattern as Councilor consultations — the heartbeat in icarus-api
        picks it up, runs it through Qwen (Icarus's voice), and delivers via Discord.
        """
        from backend.database.redis_connection import get_redis_client

        urgency = classification.get("urgency", "unknown")
        category = classification.get("category", "unknown")
        summary = classification.get("summary", "")
        details = classification.get("details") or {}

        # Build structured context for Qwen to rewrite
        lines = [
            f"Urgency: {urgency.upper()}",
            f"From: {sender}",
            f"Subject: {subject}",
            f"Category: {category.capitalize()}",
            f"Summary: {summary}",
        ]
        if category == "jobs" and details:
            lines.append(f"Company: {details.get('company', 'Unknown')}")
            lines.append(f"Status: {details.get('status', 'Update')}")
        elif category == "bills" and details:
            lines.append(f"Amount: {details.get('amount', 'Unknown')}")
            lines.append(f"Due: {details.get('due_date', 'Unknown')}")

        classification_text = "\n".join(lines)

        try:
            redis = get_redis_client()
            response_payload = json.dumps({
                "type": "email_notification",
                "platform": "discord",
                "user_id": os.environ.get("DISCORD_OPERATOR_ID", "0"),
                "message": classification_text,
            })
            await redis.lpush("icarus:councilor:responses", response_payload)
            logger.info(f"[triage] Queued notification for '{subject[:50]}'")
        except Exception as e:
            logger.error(f"[triage] Failed to queue notification: {e}")

    async def process_task(self, task_id: str, data: dict):
        """Process a single email triage task."""
        message_id = data.get("message_id")
        if not message_id:
            logger.warning(f"[triage] Task {task_id} has no message_id — skipping")
            return

        logger.info(f"[triage] Processing message {message_id}")

        try:
            await self._process_message(message_id)
        except Exception as e:
            logger.error(f"[triage] Unhandled error processing {message_id}: {e}", exc_info=True)

    async def _process_message(self, message_id: str):
        """Inner processing logic — separated so process_task can catch all errors."""
        # 1. Fetch full message
        from backend.agent.gmail_tools import gmail_get_message, ensure_label, gmail_apply_label, gmail_archive_message
        message = await gmail_get_message(message_id)
        if not message:
            logger.warning(f"[triage] Could not fetch message {message_id} — may have been deleted")
            return

        # 2. Extract email parts
        sender, subject, date, body = self._extract_email_parts(message)
        logger.info(f"[triage] Email from '{sender}': '{subject[:60]}'")

        # 3. Classify via LLM
        raw_classification = await self._classify_email(sender, subject, date, body)
        logger.info(f"[triage] Raw classification result: {raw_classification}")

        if raw_classification:
            classification = self._validate_classification(raw_classification)
        else:
            logger.warning(f"[triage] Classification failed for {message_id}, defaulting to 'other'")
            classification = {
                "category": "other",
                "urgency": "low",
                "summary": f"Email from {sender}: {subject}",
                "action_needed": False,
                "details": None,
            }

        category = classification["category"]
        urgency = classification["urgency"]
        action_needed = classification["action_needed"]

        logger.info(
            f"[triage] Classified: category={category}, urgency={urgency}, "
            f"action_needed={action_needed}"
        )

        # 4. Apply Gmail label
        label_name = CATEGORY_TO_LABEL.get(category, "Icarus/Other")
        label_id = await ensure_label(label_name)
        if label_id:
            await gmail_apply_label(message_id, label_id)
            logger.info(f"[triage] Applied label '{label_name}' to {message_id}")
        else:
            logger.warning(f"[triage] Could not resolve label '{label_name}'")

        # 5. Archive out of inbox
        await gmail_archive_message(message_id)

        # 5b. Persist structured lifecycle state (jobs/bills only) — this is
        # what a future "what am I missing" digest reads, instead of trying
        # to re-derive current state from scattered email mentions.
        try:
            tracked_item_id = await self._track_item(classification, sender, subject)
        except Exception as e:
            logger.error(f"[triage] Failed to upsert tracked item: {e}")
            tracked_item_id = None

        # 6. Notify if actionable
        if urgency in ("high", "critical") or action_needed:
            await self._notify_discord(classification, sender, subject)
            if tracked_item_id is not None:
                try:
                    from backend.agent.tracked_items_repo import mark_notified
                    await mark_notified(tracked_item_id)
                except Exception as e:
                    logger.error(f"[triage] Failed to mark tracked item notified: {e}")

        # 7. Store result in Redis for observability
        from backend.database.redis_connection import get_redis_client
        redis = get_redis_client()
        result_key = f"icarus:email_triage:{message_id}"
        await redis.set(result_key, json.dumps(classification), ex=86400 * 7)  # 7 day TTL

        logger.info(f"[triage] ✓ Message {message_id} triaged: {category}/{urgency}")


if __name__ == "__main__":
    import sys
    import asyncio
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    worker = EmailTriageWorker()
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        worker.stop()
        sys.exit(0)
