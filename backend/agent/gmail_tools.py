"""
gmail_tools.py — Gmail API integration for Icarus.

Provides both L1 agent tools (list/read messages) and service-layer functions
for the email triage agent (labels, watch, history, archive).
"""

import os
import base64
import json
import logging
import asyncio
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# gmail.modify allows read + label management + archive.
# Only upgrade to gmail.compose if we ever need to send mail.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/pubsub',
]

# ── Label cache ──────────────────────────────────────────────────────────────
# Maps lowercase label name → Gmail label ID.  Populated on first use.
_label_cache: dict[str, str] = {}
_label_cache_loaded = False

# Triage labels — created under an "Icarus/" prefix
TRIAGE_LABELS = [
    "Icarus/Jobs",
    "Icarus/Bills",
    "Icarus/Shopping",
    "Icarus/Social",
    "Icarus/Newsletters",
    "Icarus/Important",
    "Icarus/Other",
]


# ── Service builder ──────────────────────────────────────────────────────────

def _build_service_with_personal_oauth():
    """Build Gmail API client using personal OAuth refresh token."""
    client_id = (os.getenv("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("GMAIL_OAUTH_REFRESH_TOKEN") or "").strip()

    if not (client_id and client_secret and refresh_token):
        return None

    creds = UserCredentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_service_with_service_account(creds_json: str):
    """Deprecated fallback for Workspace service-account setups."""
    if creds_json.startswith("{"):
        info = json.loads(creds_json)
    else:
        info = json.loads(base64.b64decode(creds_json).decode("utf-8"))

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    delegated_user = (os.getenv("GMAIL_IMPERSONATE_USER") or "").strip()
    if delegated_user:
        creds = creds.with_subject(delegated_user)
    logger.warning(
        "Using deprecated GMAIL_CREDENTIALS_JSON service-account auth. "
        "For personal inboxes, set GMAIL_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN."
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds_json = os.getenv("GMAIL_CREDENTIALS_JSON")
    try:
        service = _build_service_with_personal_oauth()
        if service:
            return service
        if creds_json:
            return _build_service_with_service_account(creds_json)
        logger.warning(
            "Gmail auth not configured. Set GMAIL_OAUTH_CLIENT_ID, "
            "GMAIL_OAUTH_CLIENT_SECRET, and GMAIL_OAUTH_REFRESH_TOKEN."
        )
        return None
    except Exception as e:
        logger.error(f"Gmail authentication failed: {e}")
        return None


def _run_sync(fn):
    """Run a synchronous Gmail API call in a thread executor."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, fn)


# ── L1 Agent Tools (exposed to the ADK) ─────────────────────────────────────

async def gmail_list_messages(query: str = "is:unread"):
    """List messages matching query."""
    service = get_gmail_service()
    if not service:
        return "Gmail service not configured."
    try:
        results = await _run_sync(
            lambda: service.users().messages().list(userId='me', q=query).execute()
        )
        return results.get('messages', [])
    except HttpError as e:
        if e.resp is not None and e.resp.status == 400 and "failedPrecondition" in str(e):
            logger.error("Gmail failedPrecondition: check OAuth credentials.")
        else:
            logger.error(f"Failed to list gmail messages: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"Failed to list gmail messages: {e}")
        return f"Error: {e}"


async def gmail_get_message(message_id: str):
    """Retrieve full message content."""
    service = get_gmail_service()
    if not service:
        return None
    try:
        message = await _run_sync(
            lambda: service.users().messages().get(userId='me', id=message_id).execute()
        )
        return message
    except Exception as e:
        logger.error(f"Failed to get gmail message {message_id}: {e}")
        return None


# ── Label Management (used by triage worker) ─────────────────────────────────

async def gmail_list_labels() -> dict[str, str]:
    """List all Gmail labels. Returns {name: id} mapping."""
    service = get_gmail_service()
    if not service:
        return {}
    try:
        results = await _run_sync(
            lambda: service.users().labels().list(userId='me').execute()
        )
        labels = results.get('labels', [])
        return {label['name']: label['id'] for label in labels}
    except Exception as e:
        logger.error(f"Failed to list labels: {e}")
        return {}


async def gmail_create_label(name: str) -> str | None:
    """Create a Gmail label. Returns the label ID, or None on failure."""
    service = get_gmail_service()
    if not service:
        return None
    try:
        body = {
            'name': name,
            'labelListVisibility': 'labelShow',
            'messageListVisibility': 'show',
        }
        result = await _run_sync(
            lambda: service.users().labels().create(userId='me', body=body).execute()
        )
        label_id = result['id']
        logger.info(f"[gmail] Created label '{name}' (id={label_id})")
        return label_id
    except HttpError as e:
        if e.resp.status == 409:
            # Label already exists — fetch and return its ID
            labels = await gmail_list_labels()
            return labels.get(name)
        logger.error(f"Failed to create label '{name}': {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to create label '{name}': {e}")
        return None


async def ensure_label(name: str) -> str | None:
    """Get or create a label by name. Uses cached lookup first.

    This is the primary entry point for the triage worker.
    """
    global _label_cache, _label_cache_loaded

    # Load cache on first call
    if not _label_cache_loaded:
        labels = await gmail_list_labels()
        _label_cache = {k.lower(): v for k, v in labels.items()}
        _label_cache_loaded = True
        logger.info(f"[gmail] Label cache loaded ({len(_label_cache)} labels)")

    # Check cache
    key = name.lower()
    if key in _label_cache:
        return _label_cache[key]

    # Create new label
    label_id = await gmail_create_label(name)
    if label_id:
        _label_cache[key] = label_id
    return label_id


async def gmail_apply_label(message_id: str, label_id: str) -> bool:
    """Apply a label to a Gmail message."""
    service = get_gmail_service()
    if not service:
        return False
    try:
        body = {'addLabelIds': [label_id]}
        await _run_sync(
            lambda: service.users().messages().modify(
                userId='me', id=message_id, body=body
            ).execute()
        )
        return True
    except Exception as e:
        logger.error(f"Failed to apply label to {message_id}: {e}")
        return False


async def gmail_archive_message(message_id: str) -> bool:
    """Archive a message (remove INBOX label)."""
    service = get_gmail_service()
    if not service:
        return False
    try:
        body = {'removeLabelIds': ['INBOX']}
        await _run_sync(
            lambda: service.users().messages().modify(
                userId='me', id=message_id, body=body
            ).execute()
        )
        return True
    except Exception as e:
        logger.error(f"Failed to archive message {message_id}: {e}")
        return False


async def gmail_trash_message(message_id: str) -> bool:
    """Move a message to Trash. Used by spam_sweep.py for high-confidence
    junk found in Spam.

    Deliberately trash, not permanent delete: permanent delete
    (messages.delete) needs the full https://mail.google.com/ OAuth scope,
    broader than the gmail.modify scope actually granted — and trashing
    already achieves the real goal (out of the way) while leaving a 30-day
    recovery window for free, in case the classifier is ever confidently
    wrong. Gmail purges Trash (and Spam, for anything left there) on the
    same 30-day schedule either way, so this only changes *where* the
    30-day clock runs, not whether one exists."""
    service = get_gmail_service()
    if not service:
        return False
    try:
        await _run_sync(
            lambda: service.users().messages().trash(userId='me', id=message_id).execute()
        )
        return True
    except Exception as e:
        logger.error(f"Failed to trash message {message_id}: {e}")
        return False


async def gmail_untrash_message(message_id: str) -> bool:
    """Restore a message out of Trash. Mirrors gmail_trash_message's shape
    exactly. Used by the dashboard Triage tab's override action when the
    operator disagrees with a spam_sweep 'confirmed_junk' trash decision —
    spam_sweep.py's own module docstring says the sweep itself never
    auto-restores anything (a separate confidence judgment with its own
    failure mode); this is a different, operator-triggered, human-in-the-
    loop path, not a contradiction of that restraint."""
    service = get_gmail_service()
    if not service:
        return False
    try:
        await _run_sync(
            lambda: service.users().messages().untrash(userId='me', id=message_id).execute()
        )
        return True
    except Exception as e:
        logger.error(f"Failed to untrash message {message_id}: {e}")
        return False


# ── Message parsing (shared by the triage worker and the spam sweep) ────────

def extract_email_parts(message: dict) -> tuple[str, str, str, str]:
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
        body = _decode_email_body(payload)

    return sender, subject, date, body


def _decode_email_body(payload: dict, depth: int = 0) -> str:
    """Recursively decode message body from payload."""
    if depth > 5:
        return ""

    # Direct body data
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
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
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    pass
        elif mime == "text/html" and not text_part:
            data = part.get("body", {}).get("data", "")
            if data:
                try:
                    text_part = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    pass
        elif mime.startswith("multipart/"):
            nested = _decode_email_body(part, depth + 1)
            if nested:
                return nested

    return text_part


# ── Push Notification Setup ──────────────────────────────────────────────────

async def gmail_setup_watch(topic_name: str) -> dict | None:
    """Register Gmail push notifications via Pub/Sub.

    Must be renewed every 7 days. Returns watch response with historyId
    and expiration, or None on failure.
    """
    service = get_gmail_service()
    if not service:
        return None
    try:
        body = {
            'topicName': topic_name,
            'labelIds': ['INBOX'],
        }
        result = await _run_sync(
            lambda: service.users().watch(userId='me', body=body).execute()
        )
        logger.info(
            f"[gmail] Watch registered. historyId={result.get('historyId')}, "
            f"expires={result.get('expiration')}"
        )
        return result
    except Exception as e:
        logger.error(f"Failed to setup Gmail watch: {e}")
        return None


async def gmail_get_history(start_history_id: str) -> list[str]:
    """Fetch message IDs added since start_history_id.

    Returns a list of new message IDs.
    """
    service = get_gmail_service()
    if not service:
        return []
    try:
        results = await _run_sync(
            lambda: service.users().history().list(
                userId='me',
                startHistoryId=start_history_id,
                historyTypes=['messageAdded'],
            ).execute()
        )
        message_ids = []
        for record in results.get('history', []):
            for msg_added in record.get('messagesAdded', []):
                msg = msg_added.get('message', {})
                msg_id = msg.get('id')
                if msg_id:
                    message_ids.append(msg_id)
        return message_ids
    except HttpError as e:
        if e.resp.status == 404:
            # historyId too old — need to do a full sync
            logger.warning("[gmail] History ID expired, need full re-sync")
            return []
        logger.error(f"Failed to get history: {e}")
        return []
    except Exception as e:
        logger.error(f"Failed to get history: {e}")
        return []
