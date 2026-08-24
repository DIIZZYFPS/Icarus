"""
backfill_email_triage.py — One-off backlog catch-up for worker_email_triage.py.

Context: the Gmail OAuth refresh token expired 2026-08-19 ~07:47, which
silently disabled gmail_watcher.py (push notifications never re-registered)
for about 2.5 days until the token was refreshed on 2026-08-21. Every inbox
message that arrived in that window was never dispatched to
tasks:email_triage and so was never classified/scored/labeled.

This mirrors gmail_watcher._dispatch_to_triage exactly (same stream, same
{"message_id": ...} payload) so the already-running EmailTriageWorker
consumer picks these up and processes them completely normally — labels,
tracked_items, confidence scoring, notifications, all of it. Nothing here
talks to the worker directly; it only enqueues.

Skips anything already present in triage_classifications (kind="inbox") so
this is safe to re-run without creating duplicate work.

Run inside icarus-api (needs its network/env for Gmail + Redis + DB):
    docker compose exec icarus-api python backend/scripts/backfill_email_triage.py [--query "..."] [--dry-run]
"""

import argparse
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backfill_email_triage")

# Gmail's after: filter is day-granularity — one day before the outage
# started so we don't miss anything at the boundary. The per-message DB
# check below (not this query) is what actually prevents reprocessing
# already-triaged mail.
DEFAULT_QUERY = "in:inbox after:2026/08/18"


async def _list_all_messages(query: str) -> list[dict]:
    """gmail_list_messages() (the L1 tool) only fetches one page — fine for
    chat use, not for a multi-day backlog that can run past Gmail's 100-per-
    page cap. Walk nextPageToken directly against the same service client."""
    from backend.agent.gmail_tools import get_gmail_service, _run_sync

    service = get_gmail_service()
    if not service:
        raise RuntimeError("Gmail service not configured.")

    all_messages: list[dict] = []
    page_token = None
    while True:
        results = await _run_sync(
            lambda pt=page_token: service.users().messages().list(
                userId='me', q=query, pageToken=pt
            ).execute()
        )
        all_messages.extend(results.get('messages', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            break
    return all_messages


async def main(query: str, dry_run: bool) -> None:
    from backend.agent.triage_repo import get_by_message_id
    from backend.database.redis_connection import get_redis_client

    messages = await _list_all_messages(query)
    logger.info(f"Query {query!r} matched {len(messages)} message(s)")

    redis = get_redis_client()
    dispatched, skipped = 0, 0
    for m in messages:
        message_id = m["id"]
        existing = await get_by_message_id(kind="inbox", message_id=message_id)
        if existing is not None:
            skipped += 1
            continue

        if dry_run:
            logger.info(f"[dry-run] would dispatch {message_id}")
        else:
            await redis.xadd("tasks:email_triage", {"message_id": message_id})
            logger.info(f"Dispatched {message_id}")
        dispatched += 1

    logger.info(f"Done. dispatched={dispatched} already_triaged_skipped={skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="List what would be dispatched without enqueuing")
    args = parser.parse_args()
    asyncio.run(main(args.query, args.dry_run))
