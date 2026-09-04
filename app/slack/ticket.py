"""Ticket proposal generation and persistence.

Neuron extracts structured ticket data from Slack threads.
Proposals are stored in a local JSON file until confirmed by the user.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from app.errors import AuthenticationError
from app.neuron.client import NeuronClient
from app.slack.client import SlackMessage

log = logging.getLogger(__name__)

# ── Ticket proposal Pydantic model via dict validation ──────────────

VALID_PRIORITIES = {"low", "medium", "high", "urgent"}

TICKET_EXTRACTION_PROMPT = """You are a ticket extraction assistant. Your task is to analyze a Slack conversation thread and produce a structured ticket proposal.

Rules:
1. Use ONLY information present in the Slack thread. Do NOT hallucinate or invent requirements.
2. Distinguish between explicit requirements (clearly stated) and inferred requirements (reasonably implied).
3. Mark open questions if the conversation was not fully resolved.
4. The priority must be one of: low, medium, high, urgent.
5. Keep the title short and implementation-oriented.
6. The acceptance criteria must be specific and testable.

Return ONLY valid JSON matching the schema below. Do not include markdown formatting or explanation."""

TICKET_SCHEMA_INSTRUCTION = """Response format:
```json
{
  "title": "Short implementation-oriented title",
  "summary": "Concise summary of the requested feature",
  "problem": "What problem are we solving?",
  "proposed_solution": "What should be implemented?",
  "acceptance_criteria": ["Criterion 1", "Criterion 2"],
  "open_questions": ["Question 1"],
  "priority": "medium"
}
```"""


def validate_ticket_data(data: dict) -> dict:
    """Validate structured ticket data from Neuron.

    Returns the validated dict or raises ValueError.
    """
    errors = []
    if not data.get("title") or not isinstance(data["title"], str):
        errors.append("title is required and must be a string")
    if not data.get("summary") or not isinstance(data["summary"], str):
        errors.append("summary is required")
    priority = (data.get("priority") or "").lower()
    if priority not in VALID_PRIORITIES:
        errors.append(f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")
    if not isinstance(data.get("acceptance_criteria"), list):
        errors.append("acceptance_criteria must be a list")
    if errors:
        raise ValueError("; ".join(errors))

    data["priority"] = priority
    return {
        "title": data["title"],
        "summary": data.get("summary", ""),
        "problem": data.get("problem", ""),
        "proposed_solution": data.get("proposed_solution", ""),
        "acceptance_criteria": data.get("acceptance_criteria", []),
        "open_questions": data.get("open_questions", []),
        "priority": priority,
    }


# ── Proposal persistence ───────────────────────────────────────────

PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_CREATED = "created"
PROPOSAL_STATUS_CANCELLED = "cancelled"
PROPOSAL_STATUS_FAILED = "failed"


@dataclass
class TicketProposal:
    proposal_id: str
    slack_channel_id: str
    slack_thread_ts: str
    slack_message_ts: str
    ticket_data: dict
    status: str = PROPOSAL_STATUS_PENDING
    created_at: float = 0.0
    linear_issue_id: str = ""
    linear_issue_url: str = ""


class ProposalStore:
    """File-based proposal store.

    Persists proposals as JSON to a configurable path.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._cache: dict[str, TicketProposal] = {}

    async def _load(self) -> dict[str, TicketProposal]:
        if self._cache:
            return self._cache
        async with self._lock:
            if not self._path.is_file():
                self._cache = {}
                return self._cache
            try:
                raw = json.loads(self._path.read_text())
                self._cache = {}
                for pid, data in raw.items():
                    self._cache[pid] = TicketProposal(**data)
            except Exception as exc:
                log.warning("failed to load proposals: %s", exc)
                self._cache = {}
        return self._cache

    async def _save(self) -> None:
        async with self._lock:
            raw = {pid: asdict(p) for pid, p in self._cache.items()}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(raw, indent=2))
            self._path.chmod(0o600)

    async def create(self, channel: str, thread_ts: str, message_ts: str, ticket_data: dict) -> TicketProposal:
        await self._load()
        proposal = TicketProposal(
            proposal_id=str(uuid.uuid4()),
            slack_channel_id=channel,
            slack_thread_ts=thread_ts,
            slack_message_ts=message_ts,
            ticket_data=ticket_data,
            created_at=time.time(),
        )
        self._cache[proposal.proposal_id] = proposal
        await self._save()
        log.info("Proposal created id=%s channel=%s thread_ts=%s", proposal.proposal_id, channel, thread_ts)
        return proposal

    async def get(self, proposal_id: str) -> TicketProposal | None:
        await self._load()
        return self._cache.get(proposal_id)

    async def update_status(self, proposal_id: str, status: str, linear_issue_id: str = "", linear_issue_url: str = "") -> TicketProposal | None:
        await self._load()
        proposal = self._cache.get(proposal_id)
        if not proposal:
            return None
        proposal.status = status
        if linear_issue_id:
            proposal.linear_issue_id = linear_issue_id
        if linear_issue_url:
            proposal.linear_issue_url = linear_issue_url
        self._cache[proposal_id] = proposal
        await self._save()
        log.info("Proposal updated id=%s status=%s linear_issue=%s", proposal_id, status, linear_issue_id)
        return proposal

    async def find_by_thread(self, channel: str, thread_ts: str) -> TicketProposal | None:
        await self._load()
        for p in self._cache.values():
            if p.slack_channel_id == channel and p.slack_thread_ts == thread_ts:
                return p
        return None


# ── Neuron ticket extraction ───────────────────────────────────────

def _build_thread_context(messages: list[SlackMessage]) -> str:
    """Build a formatted thread context for Neuron."""
    lines = []
    for msg in messages:
        user = msg.user[:8] if msg.user else "unknown"
        lines.append(f"[{user}] {msg.text}")
    return "\n".join(lines)


async def extract_ticket_from_thread(
    neuron_client: NeuronClient,
    messages: list[SlackMessage],
) -> dict:
    """Send the thread to Neuron and return validated ticket data."""
    thread_text = _build_thread_context(messages)
    user_prompt = f"Slack conversation thread:\n\n{thread_text}\n\n{TICKET_SCHEMA_INSTRUCTION}"
    result = await neuron_client.complete(
        messages=[
            {"role": "system", "content": TICKET_EXTRACTION_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        tools=[],
        temperature=0.1,
    )
    content = (result.get("content") or "").strip()

    # Extract JSON from response (handles markdown code blocks)
    import re as _re
    json_match = _re.search(r'\{.*"title".*\}', content, _re.DOTALL)
    if json_match:
        content = json_match.group()

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Neuron returned invalid JSON: {exc}") from exc

    return validate_ticket_data(data)


# ── Linear priority mapping ────────────────────────────────────────

PRIORITY_MAP = {
    "low": 4,
    "medium": 3,
    "high": 2,
    "urgent": 1,
}


def slack_priority_to_linear(priority: str) -> int:
    """Map Slack ticket proposal priority to Linear's numeric scale."""
    return PRIORITY_MAP.get(priority.lower(), 3)


# ── Proposal block builder for Slack ───────────────────────────────

def build_proposal_blocks(ticket: dict) -> list[dict]:
    """Build Slack Block Kit blocks for a ticket proposal."""
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🧠 Linear Ticket Vorschlag*\n\n*Titel:*\n{ticket['title']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Zusammenfassung:*\n{ticket['summary']}"}},
    ]
    if ticket.get("problem"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Problem:*\n{ticket['problem']}"}})
    if ticket.get("proposed_solution"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Vorgeschlagene Lösung:*\n{ticket['proposed_solution']}"}})
    if ticket.get("acceptance_criteria"):
        criteria_text = "\n".join(f"• {c}" for c in ticket["acceptance_criteria"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Acceptance Criteria:*\n{criteria_text}"}})
    if ticket.get("open_questions"):
        questions_text = "\n".join(f"• {q}" for q in ticket["open_questions"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Offene Fragen:*\n{questions_text}"}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Priorität:* {ticket['priority'].capitalize()}"}})
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Create Linear Issue"}, "style": "primary", "value": "create_linear_issue", "action_id": "create_linear_issue"},
            {"type": "button", "text": {"type": "plain_text", "text": "Cancel"}, "style": "danger", "value": "cancel", "action_id": "cancel_proposal"},
        ],
    })
    return blocks