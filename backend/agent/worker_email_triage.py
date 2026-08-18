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
from backend.agent.gmail_tools import extract_email_parts

logger = logging.getLogger(__name__)

# ── Classification prompt ────────────────────────────────────────────────────

CLASSIFY_PROMPT = """You are an email triage assistant. Analyze the email below and respond with a JSON object ONLY — no extra text, no markdown fences.

The JSON must contain exactly these fields:
- "category": one of "jobs", "bills", "shopping", "social", "newsletters", "important", "other"
- "urgency": one of "low", "medium", "high", "critical"
- "summary": one sentence summarizing the email
- "action_needed": true or false (does the recipient need to do something?)
- "sensitive": true or false — true if the subject/sender/content is sexually explicit,
  a scam/phishing lure, or otherwise inappropriate to display verbatim in a log or
  dashboard. This is independent of category — a piece of spam can be "other" and
  "sensitive": true at the same time.
- "job_kind": only meaningful when "category" is "jobs" — one of "application_update" or
  "digest". "application_update" means this email concerns the recipient's OWN specific
  candidacy for one role — an application confirmation, OA, interview invite, rejection,
  or offer. "digest" means a jobs-alert/newsletter email listing postings the recipient
  might apply to, with no personal application status attached to any of them (e.g.
  LinkedIn/Handshake/Simplify job-alert emails) — this is true even if the digest happens
  to list only a single job. null for any other category.
- "details": an object with extracted info, or null if not applicable:
  - For jobs: {{"company": "...", "role": "...", "status": "...", "proposed_datetime": "..." or null}}
    (role: the job title/position, as stated or best inferred. status e.g. "interview
    invite", "rejection", "offer", "OA sent", "application received" — for a "digest"
    job_kind, status can be null since there's no single status.
    proposed_datetime: if the email proposes or confirms a specific interview date/time,
    resolve it to ISO 8601 — "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS" — using the email's own
    Date header above to resolve relative phrases like "next Tuesday at 2pm". null if no
    specific date/time is mentioned.)
  - For bills: {{"amount": "...", "due_date": "..."}} (e.g. "$120.00", "2026-03-25")
  - Otherwise: null
- "confidence": a number from 0.0 to 1.0 for how confident you are in this
  classification overall (category + urgency + action_needed together) —
  lower it for ambiguous senders, mixed signals, or content you're guessing
  the intent of. This doesn't change what happens to the email; it only
  decides whether a human takes a second look.

Do not take an email's own claimed urgency at face value. Manufactured pressure —
unexpected winnings/refunds requiring "confirmation", threats of account suspension,
too-good-to-be-true offers, generic "act now" framing from an unfamiliar or
suspicious sender — is a scam pattern, not real urgency. Score these "urgency": "low"
and "action_needed": false regardless of how urgent the email tries to sound; the
correct action is ignoring it, not doing what it asks. Reserve "high"/"critical" and
"action_needed": true for urgency that holds up under skepticism — a real deadline
from a known, legitimate sender.
{corrections_block}
EMAIL:
From: {sender}
Subject: {subject}
Date: {date}

{body}

Respond with the JSON object only."""

# Below spam_sweep.py's TRASH_CONFIDENCE (0.92) on purpose — this gate takes
# no action at all, it only decides whether a classification surfaces in the
# dashboard's silent-by-default review queue, so it can afford to be looser.
# Starting point; tune once real queue volume is visible.
REVIEW_CONFIDENCE = 0.70

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

    async def _classify_email(
        self, sender: str, subject: str, date: str, body: str, corrections_block: str = ""
    ) -> dict | None:
        """Call Gemma 27B to classify the email. Returns parsed JSON or None."""
        from backend.agent.llm_router import generate

        prompt = CLASSIFY_PROMPT.format(
            sender=sender,
            subject=subject,
            date=date,
            body=body[:2000],  # Truncate long emails
            corrections_block=corrections_block,
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

        try:
            confidence = float(classification.get("confidence", 0.0))
            confidence = max(0.0, min(confidence, 1.0))
        except (TypeError, ValueError):
            confidence = 0.0

        # Only meaningful for category=="jobs". Default to "application_update" on a
        # missing/invalid value rather than "digest" — this fails toward the *old*
        # behavior (every jobs email tracked as an application), not a new one. The
        # alternative default risks silently dropping a real interview invite if the
        # model omits the field; an untracked digest is the lesser failure. Revisit
        # once real review-queue volume shows how often the model actually omits this.
        job_kind = classification.get("job_kind")
        if cat == "jobs":
            if job_kind not in ("application_update", "digest"):
                job_kind = "application_update"
        else:
            job_kind = None

        return {
            "category": cat,
            "urgency": urg,
            "summary": str(classification.get("summary", ""))[:200],
            "action_needed": bool(classification.get("action_needed", False)),
            "sensitive": bool(classification.get("sensitive", False)),
            "details": details,
            "confidence": confidence,
            "job_kind": job_kind,
        }

    def _normalize_key(self, text: str) -> str:
        """Collapse a free-text identifier into a stable-ish entity key."""
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"

    async def _track_item(self, classification: dict, sender: str, subject: str, message_id: str) -> int | None:
        """Upsert this classification into tracked_items if it's a category
        with an actual lifecycle (jobs, bills) — not everything is; shopping/
        social/newsletters/other don't have a "current state" worth tracking,
        they're just mentions. Returns the tracked item's row id, or None."""
        category = classification.get("category")
        details = classification.get("details") or {}

        due_at = None
        if category == "jobs":
            # Digests aren't about the recipient's own application — nothing here has
            # a "current state" worth tracking, and upserting one would either create
            # noise rows or (worse) stomp a real application's row if the entity_key
            # happened to collide. See job_kind's docstring in CLASSIFY_PROMPT.
            if classification.get("job_kind") == "digest":
                return None
            company = details.get("company") or sender
            role = details.get("role") or "unknown-role"
            # Keyed on company+role, not company alone — two different roles at the
            # same company must not collide onto one tracked_items row.
            entity_key = self._normalize_key(f"{company}-{role}")
            state = details.get("status") or "unknown"
            due_at = details.get("proposed_datetime") or None
        elif category == "bills":
            # No invoice/account number is extracted today — sender+due_date
            # is the best stable-ish identity available. A recurring bill
            # from the same sender with a different due date next month
            # correctly becomes a new row; a reminder about the same bill
            # should share the same due date and update in place.
            due_date = details.get("due_date") or "unknown-date"
            entity_key = self._normalize_key(f"{sender}-{due_date}")
            state = "unpaid"
            due_at = details.get("due_date") or None
        else:
            return None

        from backend.agent.tracked_items_repo import upsert_item, get_by_identity, promote_to_application

        # A real application-update email about a company+role job_scout
        # already scored should land on THAT row, not spawn a second one.
        # upsert_item's own dedup can't do this — its lookup is scoped to
        # the item_type you pass it, so item_type="job_application" never
        # matches an existing item_type="job_opportunity" row no matter how
        # identical the entity_key is. Check explicitly, promote if found.
        if category == "jobs":
            platform, user_id = "discord", os.environ.get("DISCORD_OPERATOR_ID", "0")
            existing_opportunity = await get_by_identity(
                platform=platform, user_id=user_id,
                item_type="job_opportunity", entity_key=entity_key,
            )
            if existing_opportunity is not None:
                promoted = await promote_to_application(
                    existing_opportunity.id, state=state,
                    summary=classification.get("summary"),
                    due_at=due_at, urgency=classification.get("urgency"),
                    payload=details or None, message_id=message_id,
                )
                if promoted:
                    return existing_opportunity.id
                # Fell through (e.g. it stopped being a job_opportunity
                # between the check and now) — fall back to a normal upsert
                # below rather than silently dropping this classification.

        return await upsert_item(
            platform="discord",
            user_id=os.environ.get("DISCORD_OPERATOR_ID", "0"),
            item_type="job_application" if category == "jobs" else "bill",
            entity_key=entity_key,
            state=state,
            summary=classification.get("summary"),
            urgency=classification.get("urgency"),
            due_at=due_at,
            payload=details or None,
            source="email_triage",
            message_id=message_id,
        )

    def _parse_date_loose(self, text: str | None):
        """Best-effort ISO 8601 parse. Returns None on anything that doesn't
        parse cleanly — the calendar cross-reference just silently doesn't
        fire in that case rather than guessing at a malformed date."""
        if not text:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _find_calendar_conflicts(self, due_at: str | None) -> list:
        """Tracked calendar_event rows falling on the same calendar day as
        due_at. Reads the local tracked_items table (kept in sync by
        calendar_watcher.py every 15 min) rather than calling the Calendar
        API directly — this runs on every actionable classification, so it
        should stay a cheap local read, not a network round trip."""
        target = self._parse_date_loose(due_at)
        if not target:
            return []

        from backend.agent.tracked_items_repo import list_items
        events = await list_items(
            platform="discord",
            user_id=os.environ.get("DISCORD_OPERATOR_ID", "0"),
            item_type="calendar_event",
        )
        return [
            e for e in events
            if (d := self._parse_date_loose(e.due_at)) and d.date() == target.date()
        ]

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
        due_at = None
        if category == "jobs" and details:
            lines.append(f"Company: {details.get('company', 'Unknown')}")
            lines.append(f"Status: {details.get('status', 'Update')}")
            due_at = details.get("proposed_datetime")
            if due_at:
                lines.append(f"Proposed time: {due_at}")
        elif category == "bills" and details:
            lines.append(f"Amount: {details.get('amount', 'Unknown')}")
            lines.append(f"Due: {details.get('due_date', 'Unknown')}")
            due_at = details.get("due_date")

        # Deterministic calendar cross-reference — don't leave this to the
        # model deciding whether to call a tool mid-generation (unreliable,
        # per the smoke-test bleed-through incident); just hand it the
        # actual overlap as a fact, or nothing at all if there isn't one.
        conflicts = await self._find_calendar_conflicts(due_at)
        if conflicts:
            lines.append("Same-day on your calendar:")
            for c in conflicts:
                lines.append(f"  - {c.summary} ({c.due_at})")

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
        sender, subject, date, body = extract_email_parts(message)
        logger.info(f"[triage] Email from '{sender}': '{subject[:60]}'")

        # 3. Classify via LLM — fetch past operator corrections for similar
        # emails first, so the classifier can weigh them (best-effort: any
        # failure here just means an empty corrections_block, never blocks
        # classification itself).
        from backend.agent.triage_repo import find_similar_corrections, format_corrections_for_prompt
        corrections = await find_similar_corrections(sender=sender, subject=subject, kind="inbox", limit=3)
        corrections_block = format_corrections_for_prompt(corrections)

        raw_classification = await self._classify_email(sender, subject, date, body, corrections_block)
        logger.info(f"[triage] Raw classification result: {raw_classification}")

        if raw_classification:
            classification = self._validate_classification(raw_classification)
        else:
            logger.warning(f"[triage] Classification failed for {message_id}, defaulting to 'other'")
            # sensitive=True here is a deliberate fail-safe: the classifier
            # didn't return a usable judgment, so content nature is unknown —
            # mask rather than risk showing something inappropriate verbatim.
            # confidence=None (not 0.0) — a parse failure isn't "very low
            # confidence," it's the absence of a judgment to be confident in.
            classification = {
                "category": "other",
                "urgency": "low",
                "summary": f"Email from {sender}: {subject}",
                "action_needed": False,
                "sensitive": True,
                "details": None,
                "confidence": None,
                "job_kind": None,
            }

        category = classification["category"]
        urgency = classification["urgency"]
        action_needed = classification["action_needed"]

        logger.info(
            f"[triage] Classified: category={category}, urgency={urgency}, "
            f"action_needed={action_needed}"
        )

        thread_id = f"triage-{message_id}"
        sensitive = classification.get("sensitive", False)
        detail = (
            f"[flagged sensitive — subject/sender withheld] → {category}, urgency={urgency}"
            if sensitive else
            f"\"{subject}\" from {sender} → {category}, urgency={urgency}"
        )
        from backend.agent.activity_repo import publish_activity
        await publish_activity(
            actor="triage", event_type="classified",
            action="classified message",
            detail=detail,
            thread_id=thread_id,
            severity="warning" if urgency in ("high", "critical") else "info",
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
        # await gmail_archive_message(message_id)  # TEMP: disabled until rework (archives all emails including actionable ones)

        # 5b. Persist structured lifecycle state (jobs/bills only) — this is
        # what a future "what am I missing" digest reads, instead of trying
        # to re-derive current state from scattered email mentions.
        try:
            tracked_item_id = await self._track_item(classification, sender, subject, message_id)
            if tracked_item_id is not None:
                await publish_activity(
                    actor="triage", event_type="tracked",
                    action="tracked_item upserted",
                    detail=f"{category} · {classification.get('summary', '')}",
                    thread_id=thread_id,
                )
        except Exception as e:
            logger.error(f"[triage] Failed to upsert tracked item: {e}")
            tracked_item_id = None

        # 6. Notify if actionable
        notified = urgency in ("high", "critical") or action_needed
        if notified:
            await self._notify_discord(classification, sender, subject)
            await publish_activity(
                actor="system", event_type="notified",
                action="notified operator", detail=subject,
                thread_id=thread_id,
            )
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

        # 7b. Durable, reviewable record for the dashboard's Triage tab and
        # the correction-retrieval loop — additive only, doesn't change any
        # decision made in steps 1-7 above.
        try:
            from backend.agent.triage_repo import record_classification, adjusted_confidence
            action_taken = ["archived"]
            if tracked_item_id is not None:
                action_taken.append("tracked")
            if notified:
                action_taken.append("notified")
            # Blended with this sender's own reviewed track record — a
            # sender the operator has repeatedly corrected drifts back into
            # the review queue even on a message the model itself called
            # confident. See triage_repo.adjusted_confidence's docstring.
            confidence = await adjusted_confidence(classification.get("confidence"), sender, kind="inbox")
            needs_review = confidence is not None and confidence < REVIEW_CONFIDENCE
            await record_classification(
                kind="inbox", message_id=message_id, sender=sender, subject=subject,
                summary=classification.get("summary"), category=category, urgency=urgency,
                action_needed=action_needed, confidence=confidence,
                sensitive=sensitive, action_taken=action_taken,
                needs_review=needs_review,
            )
        except Exception as e:
            logger.error(f"[triage] Failed to record TriageClassification: {e}")

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
