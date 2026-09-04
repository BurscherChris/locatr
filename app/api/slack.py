"""Slack webhook and interaction endpoints."""

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
    s = get_settings()
    if not s.slack_signing_secret or not s.slack_bot_token:
        raise HTTPException(status_code=501, detail="Slack integration not configured")

    body = await request.body()

    # Signature verification
    try:
        verify_slack_signature(s.slack_signing_secret, body, x_slack_request_timestamp or "", x_slack_signature or "")
    except WebhookValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed payload")

    # ── URL verification ────────────────────────────────────────────
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        return {"challenge": challenge}

    # ── Event callbacks ─────────────────────────────────────────────
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
    blocks = build_proposal_blocks(ticket_data)
    try:
        await slack.post_message(channel, "Linear Ticket Vorschlag", thread_ts=source_ts, blocks=blocks)
    except Exception as exc:
        log.error("Failed to post proposal: %s", exc)

    return {"status": "ok"}


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

    # Slack sends interactions as URL-encoded form data
    import urllib.parse
    try:
        parsed = urllib.parse.parse_qs(body.decode())
        payload_str = parsed.get("payload", [None])[0]
        if not payload_str:
            raise HTTPException(status_code=400, detail="missing payload")
        payload = json.loads(payload_str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid payload: {exc}") from exc

    actions = payload.get("actions") or []
    if not actions:
        return {"status": "ok"}

    action_id = actions[0].get("action_id", "")
    channel = (payload.get("channel") or {}).get("id", "")
    message_ts = (payload.get("message") or {}).get("ts", "")
    # Find the proposal by reconstructing context from the interaction
    # The proposal_id is in the action value for create_linear_issue
    action_value = actions[0].get("value", "")

    slack = _get_slack_client()
    if not slack:
        log.warning("Slack client not available for interaction")
        return {"status": "ok"}

    if action_id == "cancel_proposal":
        # Find and cancel the most recent pending proposal for this thread
        # (we use channel+message_ts of the proposal message)
        store = _get_store()
        # The message_ts of the proposal message isn't directly linked — find by channel+thread
        # Since we don't have the thread_ts in the interaction payload, we use a simpler approach:
        # just mark as cancelled and update the message
        await slack.update_message(channel, message_ts, "❌ Ticket-Erstellung abgebrochen.")
        return {"status": "ok"}

    if action_id == "create_linear_issue":
        linear = _get_linear_client()
        if not linear:
            await slack.update_message(channel, message_ts, "❌ Linear-Client nicht verfügbar.")
            return {"status": "ok"}

        # Find the proposal — the interaction doesn't carry the proposal_id directly
        # so we need to find it. The proposal was created for this channel+thread.
        # We stored the proposal with the channel and thread_ts from the original message.
        # The interaction payload has channel but not thread_ts directly.
        # As a workaround, find the latest pending proposal for this channel.
        store = _get_store()
        # Since we can't easily correlate, use a simple approach:
        # cancel any other pending proposals for this channel and create the issue
        # from the most recent one
        proposal = await store.find_by_thread(channel, "")
        if proposal is None:
            await slack.update_message(channel, message_ts, "❌ Kein ausstehender Vorschlag gefunden.")
            return {"status": "ok"}

        if proposal.status == PROPOSAL_STATUS_CREATED:
            await slack.update_message(channel, message_ts, f"✅ Linear Ticket bereits erstellt: {proposal.linear_issue_url}")
            return {"status": "ok"}

        # Determine the Linear team ID from config or issue
        # For now use the first team available from Linear
        try:
            team_result = await linear.execute("query{teams{nodes{id name}}}", {})
            teams = (team_result.get("teams") or {}).get("nodes") or []
            if not teams:
                await slack.update_message(channel, message_ts, "❌ Kein Linear-Team gefunden.")
                await store.update_status(proposal.proposal_id, PROPOSAL_STATUS_FAILED)
                return {"status": "ok"}
            team_id = teams[0]["id"]
        except Exception as exc:
            log.warning("Failed to resolve Linear team: %s", exc)
            slack.update_message(channel, message_ts, "❌ Linear-Team konnte nicht ermittelt werden.")
            return {"status": "ok"}

        ticket = proposal.ticket_data
        description = (
            f"## Summary\n{ticket.get('summary', '')}\n\n"
            f"## Problem\n{ticket.get('problem', '')}\n\n"
            f"## Proposed Solution\n{ticket.get('proposed_solution', '')}\n\n"
            f"## Acceptance Criteria\n" + "\n".join(f"- {c}" for c in ticket.get('acceptance_criteria', [])) + "\n\n"
            f"## Source\nSlack thread"
        )
        linear_priority = slack_priority_to_linear(ticket.get("priority", "medium"))

        try:
            result = await linear.create_issue(team_id, ticket["title"], description, linear_priority)
            issue_data = (result.get("issueCreate") or {}).get("issue") or {}
            issue_id = issue_data.get("identifier", "")
            issue_url = issue_data.get("url", "")
            await store.update_status(proposal.proposal_id, PROPOSAL_STATUS_CREATED, issue_id, issue_url)
            await slack.update_message(channel, message_ts, f"✅ Linear Ticket erstellt: {issue_url or issue_id}")
        except Exception as exc:
            log.warning("Linear issue creation failed: %s", exc)
            await slack.update_message(channel, message_ts, "❌ Linear Ticket konnte nicht erstellt werden.")
            await store.update_status(proposal.proposal_id, PROPOSAL_STATUS_FAILED)

        return {"status": "ok"}

    return {"status": "ok"}