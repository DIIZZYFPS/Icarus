import os
import base64
import json
import logging
import asyncio
from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Use read-only scope: these tools only read/list messages.
# Elevate to gmail.modify only if write tools are added in the future.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    """Authenticate and return Gmail service."""
    creds_json = os.getenv("GMAIL_CREDENTIALS_JSON")
    if not creds_json:
        logger.warning("GMAIL_CREDENTIALS_JSON not found in environment")
        return None
    
    try:
        if creds_json.startswith('{'):
            info = json.loads(creds_json)
        else:
            # Assume it's base64 encoded if it doesn't start with {
            info = json.loads(base64.b64decode(creds_json).decode('utf-8'))
        
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        # Note: Gmail API with Service Accounts usually requires Domain-Wide Delegation.
        # If using a regular user token, this part would be different (OAuth2 Flow).
        service = build('gmail', 'v1', credentials=creds)
        return service
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
    except Exception as e:
        logger.error(f"Failed to get gmail message {message_id}: {e}")
        return None
