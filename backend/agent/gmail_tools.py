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

# Use read-only scope: these tools only read/list messages.
# Elevate to gmail.modify only if write tools are added in the future.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


def _build_service_with_personal_oauth():
    """Build Gmail API client using personal OAuth refresh token credentials."""
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
    # Disable discovery file cache to avoid noisy oauth2client compatibility logs.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_service_with_service_account(creds_json: str):
    """Deprecated fallback for Workspace service-account setups."""
    if creds_json.startswith("{"):
        info = json.loads(creds_json)
    else:
        # Assume base64 encoded if it doesn't start with {
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
    """Authenticate and return Gmail service."""
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

async def gmail_list_messages(query: str = "is:unread"):
    """List messages matching query."""
    service = get_gmail_service()
    if not service:
        return "Gmail service not configured."
    
    try:
        # Run the synchronous .execute() call in a thread executor to avoid
        # blocking the asyncio event loop.
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None, lambda: service.users().messages().list(userId='me', q=query).execute()
        )
        messages = results.get('messages', [])
        return messages
    except HttpError as e:
        if e.resp is not None and e.resp.status == 400 and "failedPrecondition" in str(e):
            logger.error(
                "Gmail failedPrecondition: personal inboxes require OAuth user credentials. "
                "Service-account auth without domain delegation cannot access users/me."
            )
        else:
            logger.error(f"Failed to list gmail messages: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"Failed to list gmail messages: {e}")
        return f"Error: {e}"

async def gmail_get_message(message_id: str):
    """Retrieve message content."""
    service = get_gmail_service()
    if not service:
        return None
    
    try:
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(
            None, lambda: service.users().messages().get(userId='me', id=message_id).execute()
        )
        return message
    except HttpError as e:
        if e.resp is not None and e.resp.status == 400 and "failedPrecondition" in str(e):
            logger.error(
                "Gmail failedPrecondition on get_message: check OAuth user credentials "
                "or Workspace delegation settings."
            )
        else:
            logger.error(f"Failed to get gmail message {message_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to get gmail message {message_id}: {e}")
        return None
