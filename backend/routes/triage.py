"""
triage.py — REST surface for the dashboard's Triage tab: the review queue
for email/spam classifications, sender reliability, and the review action
that closes the feedback loop (triage_repo.py).

Distinct from dashboard.py's /api/today and /api/activity/recent, which are
read-only snapshots — /api/triage/{id}/review is a real write path that can
call back into Gmail (restoring a trashed message on override) before
recording the correction.
"""

import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class TriageReviewRequest(BaseModel):
    action: Literal["approve", "override"]
    corrected_category: str | None = None
    corrected_urgency: str | None = None
    corrected_verdict: str | None = None
    feedback_note: str | None = None


def _serialize(r) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "message_id": r.message_id,
        "thread_id": f"{r.kind}-{r.message_id}",
        "sender": r.sender,
        "sender_domain": r.sender_domain,
        "subject": r.subject,
        "summary": r.summary,
        "category": r.category,
        "urgency": r.urgency,
        "action_needed": bool(r.action_needed),
        "confidence": r.confidence,
        "verdict": r.verdict,
        "sensitive": bool(r.sensitive),
        "action_taken": json.loads(r.action_taken) if r.action_taken else [],
        "needs_review": bool(r.needs_review),
        "reviewed": bool(r.reviewed),
        "review_action": r.review_action,
        "corrected_category": r.corrected_category,
        "corrected_urgency": r.corrected_urgency,
        "corrected_verdict": r.corrected_verdict,
        "feedback_note": r.feedback_note,
        "created_at": r.created_at,
        "reviewed_at": r.reviewed_at,
    }


@router.get("/api/triage/queue")
async def triage_queue(kind: str | None = None, limit: int = 50):
    from backend.agent.triage_repo import list_needs_review, list_recent

    needs_review = await list_needs_review(kind=kind, limit=limit)
    # Any kind, any confidence, reviewed or not — correction isn't gated
    # behind the model's own uncertainty; see list_recent's docstring.
    recent = await list_recent(kind=kind, limit=100)

    return {
        "needs_review": [_serialize(r) for r in needs_review],
        "recent": [_serialize(r) for r in recent],
        "counts": {
            "needs_review": len(needs_review),
            "inbox": sum(1 for r in needs_review if r.kind == "inbox"),
            "spam": sum(1 for r in needs_review if r.kind == "spam"),
        },
    }


@router.get("/api/triage/senders")
async def triage_senders(limit: int = 20):
    from backend.agent.triage_repo import sender_reliability_summary
    return {"senders": await sender_reliability_summary(limit=limit)}


@router.post("/api/triage/{item_id}/review")
async def review_triage_item(item_id: int, body: TriageReviewRequest):
    from backend.agent.triage_repo import get_by_id, record_review
    from backend.agent.activity_repo import publish_activity

    item = await get_by_id(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Triage item not found")

    review_action = "overridden" if body.action == "override" else "approved"
    action_taken_list = json.loads(item.action_taken) if item.action_taken else []

    # Locked-in behavior: overriding an already-trashed spam item restores
    # it in Gmail as part of this same action, not just recorded feedback.
    # If the restore fails, the review is NOT recorded — a failed restore
    # must never be represented as a successful correction.
    restored = False
    if item.kind == "spam" and review_action == "overridden" and "trashed" in action_taken_list:
        from backend.agent.gmail_tools import gmail_untrash_message
        restored = await gmail_untrash_message(item.message_id)
        if not restored:
            raise HTTPException(status_code=502, detail="Failed to restore message from Gmail Trash — review not recorded")

    updated = await record_review(
        item_id=item_id, review_action=review_action,
        corrected_category=body.corrected_category, corrected_urgency=body.corrected_urgency,
        corrected_verdict=body.corrected_verdict, feedback_note=body.feedback_note,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Triage item not found")

    label = "approved" if review_action == "approved" else "overridden"
    action_desc = f"{label} {item.kind} classification" + (" (restored from Trash)" if restored else "")
    try:
        await publish_activity(
            actor="triage", event_type="reviewed",
            action=action_desc,
            detail=body.feedback_note or f"{item.sender} — {item.subject or ''}",
            thread_id=f"{item.kind}-{item.message_id}",
        )
    except Exception as e:
        logger.warning(f"[triage] Failed to publish review activity event: {e}")

    return {"status": "ok", "id": item_id, "review_action": review_action, "restored": restored}
