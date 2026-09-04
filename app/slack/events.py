"""Slack event handling and trigger detection."""

import logging
import re

log = logging.getLogger(__name__)

# Trigger phrases for ticket creation (case-insensitive, normalized)
_TRIGGER_PATTERNS = [
    re.compile(r'\berstelle\b.*\bticket\b', re.IGNORECASE),
    re.compile(r'\bmach\b.*\bticket\b', re.IGNORECASE),
    re.compile(r'\bfass\w*\b.*\bticket\b', re.IGNORECASE),
    re.compile(r'\bcreate\b.*\bticket\b', re.IGNORECASE),
    re.compile(r'\bsummarize\b.*\bticket\b', re.IGNORECASE),
    re.compile(r'\berstelle\b.*\blinear\b', re.IGNORECASE),
    re.compile(r'\bcreate\b.*\blinear\b', re.IGNORECASE),
]


def is_ticket_command(text: str) -> bool:
    """Determine whether a message is requesting ticket creation.

    Uses pattern matching on normalized text. Only returns True if
    the message clearly asks to create/summarize a ticket.
    """
    if not text:
        return False
    for pattern in _TRIGGER_PATTERNS:
        if pattern.search(text):
            return True
    return False


def is_bot_mentioned(event: dict, bot_user_id: str) -> bool:
    """Check if the bot was mentioned in the event."""
    if not bot_user_id:
        return False
    text = event.get("text", "")
    return f"<@{bot_user_id}>" in text


def normalize_event(event: dict) -> dict:
    """Extract key fields from a Slack event callback."""
    return {
        "type": event.get("type", ""),
        "subtype": event.get("subtype", ""),
        "channel": event.get("channel", event.get("channel_id", "")),
        "user": event.get("user", ""),
        "text": event.get("text", ""),
        "ts": event.get("ts", ""),
        "thread_ts": event.get("thread_ts", ""),
        "event_ts": event.get("event_ts", ""),
    }