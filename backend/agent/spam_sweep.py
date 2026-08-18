"""
spam_sweep.py — periodic second-opinion pass over Gmail's Spam folder.

Separate from worker_email_triage.py on purpose: the main triage pipeline
only ever sees INBOX (gmail_setup_watch is scoped to labelIds=['INBOX']),
so Spam is otherwise completely invisible to Icarus — if Gmail's own filter
buries a real email (a recruiter, a personal contact) in Spam, nobody finds
out. This sweep exists to catch that narrow case, without turning into a
second full triage pipeline: the default assumption for anything in Spam is
that Gmail's filter was right, and this only acts in two situations —

  - "possibly_legitimate": notify the operator. Never auto-restore the
    email — trivial for a human to pull out of Spam themselves once flagged,
    and restoring automatically would need another confidence judgment call
    with its own failure mode.
  - "confirmed_junk" at a strict confidence bar: trash it (not permanently
    delete — see gmail_trash_message's docstring for why). Everything else
    (uncertain, or confirmed_junk below the bar) is left alone; Gmail's own
    30-day purge handles cleanup regardless.

Every outcome here is also persisted to triage_classifications (see
triage_repo.py) for the dashboard's Triage tab. "Never auto-restore" above
is still true of this sweep's own autonomous behavior — but the Triage tab
lets the *operator* override a trash decision, which calls
gmail_untrash_message directly. That's a separate, human-in-the-loop path,
not a contradiction: the restraint here is specifically about the sweep
never second-guessing itself unprompted.

Dedup is a Redis key per message ID with a TTL matched to Gmail's own
30-day Spam/Trash retention — after that, the message doesn't exist to
re-classify anyway, so the key can just expire instead of growing forever.
"""

import os
import re
import json
import logging
import asyncio

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 4 * 3600  # spam hygiene doesn't need push-notification urgency
SWEEP_QUERY = "in:spam newer_than:7d"
SWEEP_BATCH_LIMIT = 20  # per cycle, so one giant backlog doesn't burn a huge batch of LLM calls at once
SEEN_KEY_TTL = 35 * 24 * 3600  # a few days past Gmail's own 30-day Spam/Trash retention

# Deliberately strict — trashing is still an action with a consequence even
# though it's recoverable for 30 days, so the bar for taking it stays high.
TRASH_CONFIDENCE = 0.92

SPAM_SWEEP_PROMPT = """You are reviewing one email that Gmail's spam filter already placed in Spam. Your job is NOT to re-decide whether it's spam in general — assume the filter is usually right, because it almost always is. Your only job is to catch the rare case where the filter misfired on a real, wanted email: a recruiter, a personal contact, a legitimate business notification addressed specifically to the recipient.

Respond with a JSON object ONLY — no extra text, no markdown fences. Fields:
- "verdict": one of "confirmed_junk", "possibly_legitimate", "uncertain"
- "confidence": a number from 0.0 to 1.0 for how confident you are in that verdict
- "sensitive": true or false — true if the subject/sender/content is sexually explicit, a scam/phishing lure, or otherwise inappropriate to display verbatim in a log or dashboard
- "reason": one short sentence

Default to "confirmed_junk" for anything with bulk-marketing patterns, phishing indicators, adult/scam content, or generic unsolicited offers — that is the overwhelming majority of real Spam content. Only choose "possibly_legitimate" if the email reads as clearly personal or addressed specifically to the recipient by name with real context (a named human sender, a specific job title/company, a genuine reply-like structure). If you are not confident either way, choose "uncertain" rather than guessing.
{corrections_block}
EMAIL:
From: {sender}
Subject: {subject}
Date: {date}

{body}

Respond with the JSON object only."""


def _seen_key(message_id: str) -> str:
    return f"icarus:spam_sweep:seen:{message_id}"


async def _already_seen(redis, message_id: str) -> bool:
    return bool(await redis.exists(_seen_key(message_id)))


async def _mark_seen(redis, message_id: str) -> None:
    await redis.set(_seen_key(message_id), "1", ex=SEEN_KEY_TTL)


async def _classify(sender: str, subject: str, date: str, body: str, corrections_block: str = "") -> dict | None:
    from backend.agent.llm_router import generate

    prompt = SPAM_SWEEP_PROMPT.format(
        sender=sender, subject=subject, date=date, body=body[:2000], corrections_block=corrections_block
    )
    try:
        response = await generate(task_type="scoring", messages=[{"role": "user", "text": prompt}], max_tokens=256)
        cleaned = re.sub(r'^```(?:json)?\s*', '', response.strip())
        cleaned = re.sub(r'\s*```$', '', cleaned)
        result = json.loads(cleaned)
    except Exception as e:
        logger.warning(f"[spam_sweep] Classification failed, treating as uncertain: {e}")
        return None

    verdict = result.get("verdict")
    if verdict not in ("confirmed_junk", "possibly_legitimate", "uncertain"):
        verdict = "uncertain"
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "verdict": verdict,
        "confidence": max(0.0, min(confidence, 1.0)),
        "sensitive": bool(result.get("sensitive", False)),
        "reason": str(result.get("reason", ""))[:200],
    }


async def _notify_possibly_legitimate(
    sender: str,
    subject: str,
    reason: str,
    source_id: str | None = None,
):
    """Same delivery path worker_email_triage.py uses — the heartbeat picks
    this up, runs it through Qwen for Icarus's voice, and delivers via
    Discord."""
    from backend.database.redis_connection import get_redis_client

    text = (
        f"Spam-folder review flagged a possible false positive.\n"
        f"From: {sender}\nSubject: {subject}\nWhy: {reason}\n\n"
        f"Still sitting in Spam — pull it out yourself if it's real; I haven't touched it."
    )
    try:
        redis = get_redis_client()
        payload = json.dumps({
            "type": "email_notification",
            "platform": "discord",
            "user_id": os.environ.get("DISCORD_OPERATOR_ID", "0"),
            "message": text,
            "source_id": source_id,
        })
        await redis.lpush("icarus:councilor:responses", payload)
    except Exception as e:
        logger.error(f"[spam_sweep] Failed to queue notification: {e}")


async def _sweep_once():
    from backend.database.redis_connection import get_redis_client
    from backend.agent.gmail_tools import gmail_list_messages, gmail_get_message, gmail_trash_message, extract_email_parts
    from backend.agent.activity_repo import publish_activity

    redis = get_redis_client()

    messages = await gmail_list_messages(query=SWEEP_QUERY)
    if isinstance(messages, str):  # error string, or "not configured"
        return
    if not messages:
        return

    checked = 0
    for m in messages:
        if checked >= SWEEP_BATCH_LIMIT:
            break
        message_id = m.get("id")
        if not message_id or await _already_seen(redis, message_id):
            continue
        checked += 1

        message = await gmail_get_message(message_id)
        if not message:
            await _mark_seen(redis, message_id)
            continue

        sender, subject, date, body = extract_email_parts(message)

        from backend.agent.triage_repo import find_similar_corrections, format_corrections_for_prompt
        corrections = await find_similar_corrections(sender=sender, subject=subject, kind="spam", limit=3)
        corrections_block = format_corrections_for_prompt(corrections)

        result = await _classify(sender, subject, date, body, corrections_block)
        await _mark_seen(redis, message_id)

        if result is None:
            continue  # classification failed — leave it alone, don't guess

        thread_id = f"spam-{message_id}"
        safe_subject_line = (
            "[flagged sensitive — subject/sender withheld]"
            if result["sensitive"] else f"\"{subject}\" from {sender}"
        )

        # Blended with this sender's own reviewed track record — a domain
        # the operator has repeatedly pulled back out of "confirmed junk"
        # drifts below the trash bar even on a message the model itself
        # called confident, so the *action* itself (not just the review
        # flag) responds to a pattern of mistakes. See
        # triage_repo.adjusted_confidence's docstring.
        from backend.agent.triage_repo import adjusted_confidence
        confidence = await adjusted_confidence(result["confidence"], sender, kind="spam")

        # Anything the sweep didn't confidently trash is worth a look on the
        # Triage tab — confidently-trashed items are still overridable
        # there, just not in the "needs attention" queue by default.
        needs_review = result["verdict"] != "confirmed_junk" or confidence < TRASH_CONFIDENCE
        action_taken: list[str] = []

        if result["verdict"] == "possibly_legitimate":
            await _notify_possibly_legitimate(
                sender,
                subject,
                result["reason"],
                source_id=str(m.get("id")) if m.get("id") else None,
            )
            await publish_activity(
                actor="triage", event_type="spam_flagged",
                action="flagged possible false positive in Spam",
                detail=f"{safe_subject_line} — {result['reason']}",
                thread_id=thread_id, severity="warning",
            )
            logger.info(f"[spam_sweep] Possibly legitimate: {subject[:60]!r} from {sender} — {result['reason']}")
            action_taken = ["notified"]

        elif result["verdict"] == "confirmed_junk" and confidence >= TRASH_CONFIDENCE:
            trashed = await gmail_trash_message(message_id)
            await publish_activity(
                actor="triage", event_type="spam_trashed" if trashed else "spam_trash_failed",
                action="trashed confirmed junk" if trashed else "failed to trash confirmed junk",
                detail=f"{safe_subject_line} — confidence={confidence:.2f}",
                thread_id=thread_id,
            )
            logger.info(f"[spam_sweep] Trashed ({confidence:.2f} adjusted confidence): {subject[:60]!r}")
            # Only claim "trashed" if it actually succeeded — the Triage
            # tab's override action decides whether to call Gmail's untrash
            # based on this list, and a failed trash has nothing to restore.
            action_taken = ["trashed"] if trashed else ["trash_failed"]

        # else: uncertain, or confirmed_junk below the confidence bar —
        # deliberately no Discord ping and no activity event, same as
        # before. Gmail's own 30-day purge handles it either way; nothing
        # here is worth narrating out loud. It's still persisted below
        # though, so it's not silently lost — just quietly queued.

        try:
            from backend.agent.triage_repo import record_classification
            await record_classification(
                kind="spam", message_id=message_id, sender=sender, subject=subject,
                verdict=result["verdict"], confidence=confidence,
                sensitive=result["sensitive"], action_taken=action_taken,
                needs_review=needs_review,
            )
        except Exception as e:
            logger.error(f"[spam_sweep] Failed to record TriageClassification: {e}")


async def run_spam_sweep():
    logger.info(f"[spam_sweep] Started. Sweeping Spam every {SWEEP_INTERVAL_SECONDS}s.")
    while True:
        try:
            await _sweep_once()
        except Exception as e:
            logger.error(f"[spam_sweep] Sweep error: {e}")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
