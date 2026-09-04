"""Slack webhook and interaction endpoints."""

import asyncio
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import get_settings
from app.errors import WebhookValidationError
from app.linear.client import LinearClient
from app.neuron.client import NeuronClient
from app.slack.client import SlackClient
from app.slack.events import is_bot_mentioned, is_ticket_command, normalize_event
from app.slack.ticket import (
    PROPOSAL_STATUS_CANCELLED,
    PROPOSAL_STATUS_CREATED,
    PROPOSAL_STATUS_FAILED,
    PROPOSAL_STATUS_PENDING,
    ProposalStore,
    build_proposal_blocks,
    extract_ticket_from_thread,
    slack_priority_to_linear,
)
from app.slack.webhook import verify_slack_signature

log = logging.getLogger(__name__)
router = APIRouter()


def _get_store() -> ProposalStore:
    return ProposalStore(get_settings().slack_ticket_proposal_store_path)


def _get_slack_client() -> SlackClient | None:
    s = get_settings()
    if not s.slack_bot_token:
        return None
    return SlackClient(s.slack_bot_token, s.http_timeout_seconds)


def _get_neuron_client() -> NeuronClient | None:
    s = get_settings()
    if not s.neuron_api_key:
        return None
    return NeuronClient(s.neuron_base_url, s.neuron_api_key, s.neuron_model, s.http_timeout_seconds)


def _get_linear_client() -> LinearClient | None:
    s = get_settings()
    from app.agent.runner import _make_linear_client
    return _make_linear_client(s, with_oauth=True)


# ── Global idempotency set for Slack events (in-memory) ────────────
_processed_slack_events: set[str] = set()


@router.post("/webhooks/slack")
async def slack_webhook(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None, alias="x-slack-request-timestamp"),
    x_slack_signature: str | None = Header(default=None, alias="x-slack-signature"),
):
    body = await request.body()
    body_str = body.decode(errors="replace")

    # ── URL verification (always works, no config/signature required) ─
    # Slack sends url_verification without signature headers. Detect by
    # checking the raw body text before any config or signature check.
    if '"type":"url_verification"' in body_str or '"type": "url_verification"' in body_str:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="malformed payload")
        challenge = payload.get("challenge", "")
        if not challenge:
            raise HTTPException(status_code=400, detail="missing challenge")
        log.info("Slack URL verification challenge=%s", challenge[:20])
        return {"challenge": challenge}

    # ── Configuration check ───────────────────────────────────────────
    s = get_settings()
    if not s.slack_signing_secret or not s.slack_bot_token:
        raise HTTPException(status_code=501, detail="Slack integration not configured")

    # ── Signature verification ────────────────────────────────────────
    try:
        verify_slack_signature(s.slack_signing_secret, body, x_slack_request_timestamp or "", x_slack_signature or "")
    except WebhookValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Parse payload for event callbacks
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed payload")
    event = payload.get("event") or {}
    event_id = payload.get("event_id") or payload.get("event_ts", "")
    team_id = payload.get("team_id", "")

    # Idempotency
    idem_key = f"{team_id}:{event_id}"
    if idem_key in _processed_slack_events:
        log.info("Slack event already processed event_id=%s", event_id)
        return {"status": "ok"}
    if event_id:
        _processed_slack_events.add(idem_key)

    event_type = event.get("type", "")
    log.info("Slack event received type=%s event_id=%s", event_type, event_id)

    # Only handle app_mention or message events
    if event_type not in ("app_mention", "message"):
        return {"status": "ok"}

    # Ignore bot's own messages
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return {"status": "ok"}

    ev = normalize_event(event)
    text = ev.get("text", "")
    channel = ev.get("channel", "")
    thread_ts = ev.get("thread_ts", "")
    ts = ev.get("ts", "")

    # Must be in a thread to create a ticket from it
    if not thread_ts and not ts:
        return {"status": "ok"}

    # Use thread_ts as the source; if the message itself starts a thread, use its ts
    source_ts = thread_ts or ts

    # Check for ticket command
    slack = _get_slack_client()
    if not slack:
        log.warning("Slack client not available")
        return {"status": "ok"}

    if not is_ticket_command(text):
        return {"status": "ok"}

    # ── Retrieve thread ─────────────────────────────────────────────
    try:
        messages = await slack.get_thread_replies(channel, source_ts)
    except Exception as exc:
        log.error("Failed to retrieve Slack thread: %s", exc)
        slack.post_message(channel, "❌ Konnte den Thread nicht abrufen.", thread_ts=source_ts)
        return {"status": "ok"}

    if not messages:
        return {"status": "ok"}

    # ── Neuron extraction ───────────────────────────────────────────
    neuron = _get_neuron_client()
    if not neuron:
        log.warning("Neuron client not available")
        return {"status": "ok"}

    try:
        ticket_data = await extract_ticket_from_thread(neuron, messages)
    except (ValueError, Exception) as exc:
        log.warning("Ticket extraction failed: %s", exc)
        await slack.post_message(channel, f"❌ Ticket-Extraktion fehlgeschlagen: {exc}", thread_ts=source_ts)
        return {"status": "ok"}

    # ── Persist proposal ────────────────────────────────────────────
    store = _get_store()
    proposal = await store.create(channel, source_ts, ts, ticket_data)

    # ── Post proposal to Slack ──────────────────────────────────────
    blocks = build_proposal_blocks(ticket_data, proposal_id=proposal.proposal_id, thread_ts=source_ts)
    try:
        await slack.post_message(channel, "Linear Ticket Vorschlag", thread_ts=source_ts, blocks=blocks)
    except Exception as exc:
        log.error("Failed to post proposal: %s", exc)

    return {"status": "ok"}


def _parse_interaction(body: bytes) -> dict:
    """Parse Slack's URL-encoded interaction payload."""
    import urllib.parse
    try:
        parsed = urllib.parse.parse_qs(body.decode())
        payload_str = parsed.get("payload", [None])[0]
        if not payload_str:
            raise HTTPException(status_code=400, detail="missing payload")
        return json.loads(payload_str)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid payload: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid payload: {exc}") from exc


# ── Interaction idempotency set (in-memory, keyed on interaction + action) ──
_processed_interactions: set[str] = set()


def _build_ack_blocks(title: str, summary: str, priority: str, status_emoji: str, status_text: str) -> list[dict]:
    """Build replacement blocks that remove action buttons and show a status."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🧠 Linear Ticket Vorschlag*\n\n*Titel:*\n{title}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zusammenfassung:*\n{summary}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Priorität:* {priority.capitalize()}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Status:* {status_emoji} {status_text}"}},
    ]


async def _handle_cancel_proposal(
    slack: SlackClient,
    store: ProposalStore,
    channel: str,
    message_ts: str,
    proposal_id: str,
    ticket_data: dict,
) -> None:
    """Cancel a proposal and update the Slack message."""
    title = ticket_data.get("title", "?")
    summary = ticket_data.get("summary", "")
    priority = ticket_data.get("priority", "medium")
    blocks = _build_ack_blocks(title, summary, priority, "❌", "Erstellung abgebrochen")
    try:
        await slack.update_message(channel, message_ts, "❌ Ticket-Erstellung abgebrochen.", blocks=blocks)
    except Exception as exc:
        log.warning("Failed to update Slack message after cancel: %s", exc)
    await store.update_status(proposal_id, PROPOSAL_STATUS_CANCELLED)
    log.info("Proposal cancelled id=%s", proposal_id)


async def _handle_create_linear_issue(
    slack: SlackClient,
    store: ProposalStore,
    channel: str,
    message_ts: str,
    proposal_id: str,
    ticket_data: dict,
    linear: LinearClient,
) -> None:
    """Create a Linear issue and update the Slack message.

    Idempotent: re-fetches the proposal from the store so that concurrent
    invocations for the same *proposal_id* see an up-to-date status.
    """
    proposal = await store.get(proposal_id)
    if proposal and proposal.status == PROPOSAL_STATUS_CREATED:
        log.info("Proposal already created id=%s url=%s", proposal_id, proposal.linear_issue_url)
        title = proposal.ticket_data.get("title", "?")
        summary = proposal.ticket_data.get("summary", "")
        priority = proposal.ticket_data.get("priority", "medium")
        blocks = _build_ack_blocks(title, summary, priority, "✅", f"Linear Ticket erstellt: {proposal.linear_issue_url or proposal.linear_issue_id}")
        await slack.update_message(channel, message_ts, f"✅ Linear Ticket bereits erstellt: {proposal.linear_issue_url or proposal.linear_issue_id}", blocks=blocks)
        return
    ticket_data = proposal.ticket_data if proposal else ticket_data

    title = ticket_data.get("title", "Untitled")
    summary = ticket_data.get("summary", "")
    priority = ticket_data.get("priority", "medium")

    # ── Resolve Linear team ────────────────────────────────────────
    try:
        team_result = await linear.execute("query{teams{nodes{id name}}}", {})
        teams = (team_result.get("teams") or {}).get("nodes") or []
        if not teams:
            blocks = _build_ack_blocks(title, summary, priority, "❌", "Kein Linear-Team gefunden")
            await slack.update_message(channel, message_ts, "❌ Kein Linear-Team gefunden.", blocks=blocks)
            await store.update_status(proposal_id, PROPOSAL_STATUS_FAILED)
            return
        team_id = teams[0]["id"]
    except Exception as exc:
        log.warning("Failed to resolve Linear team: %s", exc)
        blocks = _build_ack_blocks(title, summary, priority, "❌", "Linear-Team konnte nicht ermittelt werden")
        await slack.update_message(channel, message_ts, "❌ Linear-Team konnte nicht ermittelt werden.", blocks=blocks)
        await store.update_status(proposal_id, PROPOSAL_STATUS_FAILED)
        return

    # ── Build description ──────────────────────────────────────────
    description = (
        f"## Summary\n{ticket_data.get('summary', '')}\n\n"
        f"## Problem\n{ticket_data.get('problem', '')}\n\n"
        f"## Proposed Solution\n{ticket_data.get('proposed_solution', '')}\n\n"
        f"## Acceptance Criteria\n" + "\n".join(f"- {c}" for c in ticket_data.get('acceptance_criteria', [])) + "\n\n"
        f"## Source\nSlack thread"
    )
    linear_priority = slack_priority_to_linear(ticket_data.get("priority", "medium"))

    # ── Create issue ───────────────────────────────────────────────
    try:
        result = await linear.create_issue(team_id, title, description, linear_priority)
        issue_data = (result.get("issueCreate") or {}).get("issue") or {}
        issue_id = issue_data.get("identifier", "")
        issue_url = issue_data.get("url", "")
        await store.update_status(proposal_id, PROPOSAL_STATUS_CREATED, issue_id, issue_url)
        blocks = _build_ack_blocks(title, summary, priority, "✅", f"Linear Ticket erstellt: {issue_url or issue_id}")
        await slack.update_message(channel, message_ts, f"✅ Linear Ticket erstellt: {issue_url or issue_id}", blocks=blocks)
        log.info("Linear issue created id=%s proposal=%s", issue_id, proposal_id)
    except Exception as exc:
        log.warning("Linear issue creation failed: %s", exc)
        blocks = _build_ack_blocks(title, summary, priority, "❌", "Linear Ticket konnte nicht erstellt werden")
        await slack.update_message(channel, message_ts, "❌ Linear Ticket konnte nicht erstellt werden.", blocks=blocks)
        await store.update_status(proposal_id, PROPOSAL_STATUS_FAILED)


@router.post("/webhooks/slack/interactions")
async def slack_interactions(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None, alias="x-slack-request-timestamp"),
    x_slack_signature: str | None = Header(default=None, alias="x-slack-signature"),
):
    s = get_settings()
    if not s.slack_signing_secret or not s.slack_bot_token:
        raise HTTPException(status_code=501, detail="Slack integration not configured")

    body = await request.body()

    try:
        verify_slack_signature(s.slack_signing_secret, body, x_slack_request_timestamp or "", x_slack_signature or "")
    except WebhookValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = _parse_interaction(body)
    actions = payload.get("actions") or []
    if not actions:
        return {"status": "ok"}

    action_id = actions[0].get("action_id", "")
    channel = (payload.get("channel") or {}).get("id", "")
    message_ts = (payload.get("message") or {}).get("ts", "")
    action_value = actions[0].get("value", "")

    # ── Idempotency ──────────────────────────────────────────────────
    # Slack interaction payload has a unique hash or use channel+message_ts+action_id
    interaction_id = payload.get("hash", "") or f"{channel}:{message_ts}:{action_id}"
    if interaction_id in _processed_interactions:
        log.info("Interaction already processed id=%s", interaction_id)
        return {"status": "ok"}
    _processed_interactions.add(interaction_id)

    # ── Extract proposal_id and thread_ts from button value ──────────
    proposal_id = ""
    thread_ts = ""
    if action_value:
        try:
            meta = json.loads(action_value)
            proposal_id = meta.get("proposal_id", "")
            thread_ts = meta.get("thread_ts", "")
        except (json.JSONDecodeError, TypeError):
            proposal_id = action_value  # fallback: value is the proposal_id itself

    # ── Immediate acknowledgement to Slack ───────────────────────────
    # Return 200 immediately. The actual work is done in the background.
    # (Slack expects a 200 within 3 seconds; Linear operations may take longer.)

    slack = _get_slack_client()
    linear = _get_linear_client()

    if action_id == "cancel_proposal":
        store = _get_store()
        proposal = await store.get(proposal_id) if proposal_id else None
        if not proposal:
            # Fallback: find pending proposal for this channel
            all_proposals = [p for p in (await store._load()).values()
                             if p.slack_channel_id == channel and p.status == PROPOSAL_STATUS_PENDING]
            if all_proposals:
                proposal = all_proposals[-1]
        ticket_data = proposal.ticket_data if proposal else {"title": "?", "summary": "", "priority": "medium"}
        actual_pid = proposal.proposal_id if proposal else proposal_id
        asyncio.create_task(_handle_cancel_proposal(slack, store, channel, message_ts, actual_pid, ticket_data))
        return {"status": "ok"}

    if action_id == "create_linear_issue":
        if not linear:
            await slack.update_message(channel, message_ts, "❌ Linear-Client nicht verfügbar.")
            return {"status": "ok"}

        store = _get_store()
        proposal = await store.get(proposal_id) if proposal_id else None
        if not proposal:
            all_proposals = [p for p in (await store._load()).values()
                             if p.slack_channel_id == channel and p.status == PROPOSAL_STATUS_PENDING]
            if not all_proposals:
                await slack.update_message(channel, message_ts, "❌ Kein ausstehender Vorschlag gefunden.")
                return {"status": "ok"}
            proposal = all_proposals[-1]

        ticket_data = proposal.ticket_data
        actual_pid = proposal.proposal_id

        asyncio.create_task(
            _handle_create_linear_issue(slack, store, channel, message_ts, actual_pid, ticket_data, linear)
        )
        return {"status": "ok"}

    return {"status": "ok"}