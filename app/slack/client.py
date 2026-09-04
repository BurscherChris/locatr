"""Slack Web API client.

Supports thread retrieval, message posting, and user info lookup.
All HTTP calls use timeouts. Tokens are never logged.
"""

import logging
from dataclasses import dataclass

import httpx

from app.errors import AuthenticationError

log = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


class SlackApiError(Exception):
    """Slack API returned an error response."""


@dataclass
class SlackMessage:
    user: str
    text: str
    ts: str
    thread_ts: str = ""


class SlackClient:
    def __init__(self, bot_token: str, timeout: int = 30):
        if not bot_token:
            raise AuthenticationError("SLACK_BOT_TOKEN is not configured")
        self._headers = {"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}
        self._timeout = timeout

    async def _post(self, path: str, json_data: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{SLACK_API_BASE}{path}", headers=self._headers, json=json_data)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackApiError(data.get("error", "unknown_slack_error"))
        return data

    async def _get(self, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{SLACK_API_BASE}{path}", headers=self._headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackApiError(data.get("error", "unknown_slack_error"))
        return data

    async def get_thread_replies(self, channel: str, thread_ts: str) -> list[SlackMessage]:
        """Retrieve all messages in a thread, handling pagination."""
        messages: list[SlackMessage] = []
        cursor: str | None = None
        while True:
            params: dict = {"channel": channel, "ts": thread_ts, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/conversations.replies", params)
            for msg in data.get("messages", []):
                messages.append(SlackMessage(
                    user=msg.get("user", ""),
                    text=msg.get("text", ""),
                    ts=msg.get("ts", ""),
                    thread_ts=msg.get("thread_ts", ""),
                ))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        log.info("Slack thread retrieved channel=%s thread_ts=%s messages=%s", channel, thread_ts, len(messages))
        return messages

    async def post_message(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> dict:
        """Post a message to a channel or thread."""
        payload: dict = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks
        result = await self._post("/chat.postMessage", payload)
        log.info("Slack message posted channel=%s thread_ts=%s ts=%s", channel, thread_ts, result.get("ts", ""))
        return result

    async def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> dict:
        """Update an existing message."""
        payload: dict = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        result = await self._post("/chat.update", payload)
        log.info("Slack message updated channel=%s ts=%s", channel, ts)
        return result

    async def get_user_info(self, user_id: str) -> dict:
        """Get user display name. Returns user_id as fallback."""
        try:
            data = await self._get("/users.info", {"user": user_id})
            profile = data.get("user", {})
            return {"id": user_id, "display_name": profile.get("profile", {}).get("display_name", "") or profile.get("name", "") or user_id}
        except Exception as exc:
            log.warning("Slack user info lookup failed: %s", exc)
            return {"id": user_id, "display_name": user_id}