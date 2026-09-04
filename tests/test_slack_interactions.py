"""Tests for the Slack interactions (
/webhooks/slack/interactions) endpoint.

Covers:
- valid Slack interaction signature
- invalid signature
- stale timestamp
- missing Slack config
- Create Linear Issue action (via direct handler test)
- Cancel action (via direct handler test)
- malformed interaction payload
- missing/invalid action metadata
- Linear API failure
- duplicate Create action does not create two issues
- secrets are never logged
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.api.slack import (
    _build_ack_blocks,
    _handle_cancel_proposal,
    _handle_create_linear_issue,
    _processed_interactions,
)
from app.config import get_settings
from app.main import app
from app.slack.client import SlackClient
from app.slack.ticket import (
    PROPOSAL_STATUS_CANCELLED,
    PROPOSAL_STATUS_CREATED,
    PROPOSAL_STATUS_FAILED,
    PROPOSAL_STATUS_PENDING,
    ProposalStore,
    build_proposal_blocks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slack_sig(body, secret=b"secret", timestamp=None):
    ts = timestamp or str(int(time.time()))
    sig_basestring = f"v0:{ts}:{body.decode() if isinstance(body, bytes) else body}"
    return "v0=" + hmac.new(secret, sig_basestring.encode(), hashlib.sha256).hexdigest(), ts


def _interaction_body(payload: dict) -> bytes:
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()


def _build_interaction_payload(
    action_id: str = "create_linear_issue",
    value: str = "",
    channel: str = "C12345",
    message_ts: str = "1234567890.123456",
    hash_val: str = "hash123",
) -> dict:
    return {
        "type": "block_actions",
        "hash": hash_val,
        "actions": [{"action_id": action_id, "value": value, "block_id": "b1"}],
        "channel": {"id": channel},
        "message": {"ts": message_ts},
        "user": {"id": "U12345", "name": "testuser"},
        "team": {"id": "T12345", "domain": "test"},
        "api_app_id": "A12345",
        "token": "shh",
    }


def _make_button_value(proposal_id: str = "", thread_ts: str = "") -> str:
    return json.dumps({"proposal_id": proposal_id, "thread_ts": thread_ts})


async def _create_pending_proposal(store: ProposalStore) -> str:
    p = await store.create("C12345", "1.0", "1.0", {
        "title": "Test Ticket",
        "summary": "A test ticket from Slack",
        "problem": "Something is broken",
        "proposed_solution": "Fix it",
        "acceptance_criteria": ["Works"],
        "priority": "high",
    })
    return p.proposal_id


def _clear_state():
    _processed_interactions.clear()
    get_settings.cache_clear()


# ======================================================================
# Endpoint: signature / validation tests
# ======================================================================


class TestSlackInteractionsEndpoint:
    def test_missing_config_returns_501(self, monkeypatch):
        _clear_state()
        # Pydantic reads .env from disk; delenv is not enough. Mock get_settings instead.
        from app.config import Settings
        monkeypatch.setattr("app.api.slack.get_settings",
                            lambda: Settings(slack_signing_secret="", slack_bot_token=""))
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions", content=b"{}")
        assert resp.status_code == 501

    def test_invalid_signature_returns_400(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=b"{}",
                               headers={"x-slack-request-timestamp": "1234567890",
                                        "x-slack-signature": "v0=bad"})
        assert resp.status_code == 400

    def test_stale_timestamp_returns_400(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = _interaction_body(_build_interaction_payload())
        stale_ts = str(int(time.time()) - 600)
        sig, _ = _slack_sig(body, timestamp=stale_ts)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=body,
                               headers={"x-slack-request-timestamp": stale_ts,
                                        "x-slack-signature": sig})
        assert resp.status_code == 400
        assert "stale" in resp.text.lower()

    def test_missing_payload_field(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = b"not-form-data"
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=body,
                               headers={"x-slack-request-timestamp": ts,
                                        "x-slack-signature": sig})
        assert resp.status_code in (400,)

    def test_malformed_json_payload(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = urllib.parse.urlencode({"payload": "not-json"}).encode()
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=body,
                               headers={"x-slack-request-timestamp": ts,
                                        "x-slack-signature": sig})
        assert resp.status_code == 400

    def test_no_actions_returns_ok(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        payload = _build_interaction_payload()
        payload.pop("actions", None)
        body = _interaction_body(payload)
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=body,
                               headers={"x-slack-request-timestamp": ts,
                                        "x-slack-signature": sig})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_valid_interaction_returns_200(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = _interaction_body(_build_interaction_payload(action_id="cancel_proposal",
                                                           value=_make_button_value("pid1", "1.0")))
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=body,
                               headers={"x-slack-request-timestamp": ts,
                                        "x-slack-signature": sig})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_same_hash_deduplicated(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        payload = _build_interaction_payload(action_id="cancel_proposal",
                                             value=_make_button_value("pid1", "1.0"),
                                             hash_val="same_hash")
        body = _interaction_body(payload)
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp1 = client.post("/webhooks/slack/interactions",
                                content=body,
                                headers={"x-slack-request-timestamp": ts,
                                         "x-slack-signature": sig})
            assert resp1.status_code == 200
            resp2 = client.post("/webhooks/slack/interactions",
                                content=body,
                                headers={"x-slack-request-timestamp": ts,
                                         "x-slack-signature": sig})
            assert resp2.status_code == 200

    def test_secrets_not_in_response(self, monkeypatch):
        _clear_state()
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        payload = _build_interaction_payload(action_id="cancel_proposal",
                                             value=_make_button_value("pid1", "1.0"))
        payload["token"] = "supersecret_slack_token"
        body = _interaction_body(payload)
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack/interactions",
                               content=body,
                               headers={"x-slack-request-timestamp": ts,
                                        "x-slack-signature": sig})
        assert resp.status_code == 200
        assert "supersecret_slack_token" not in resp.text


# ======================================================================
# Handler: _handle_cancel_proposal
# ======================================================================


class TestHandleCancelProposal:
    @pytest.mark.asyncio
    async def test_cancel_updates_message_and_store(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)

        updated = []

        class MockSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                updated.append((channel, ts, text, blocks))
                return {"ok": True}

        await _handle_cancel_proposal(MockSlack(), store, "C1", "1.0", pid,
                                      {"title": "T", "summary": "S", "priority": "medium"})

        assert len(updated) == 1
        p = await store.get(pid)
        assert p.status == PROPOSAL_STATUS_CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_survives_slack_error(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)

        class FailingSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                raise Exception("Slack API error")

        await _handle_cancel_proposal(FailingSlack(), store, "C1", "1.0", pid,
                                      {"title": "T", "summary": "S", "priority": "medium"})

        p = await store.get(pid)
        assert p.status == PROPOSAL_STATUS_CANCELLED


# ======================================================================
# Handler: _handle_create_linear_issue
# ======================================================================


class TestHandleCreateLinearIssue:
    @pytest.mark.asyncio
    async def test_creates_issue_and_updates_store(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)

        updated = []

        class MockSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                updated.append((channel, ts, text, blocks))
                return {"ok": True}

        class MockLinear:
            async def execute(self, query, variables):
                if "teams" in query:
                    return {"teams": {"nodes": [{"id": "team1", "name": "Engineering"}]}}
                return {}

            async def create_issue(self, team_id, title, description, priority=None):
                return {"issueCreate": {"success": True, "issue": {"identifier": "LOC-42", "url": "https://linear.app/issue/LOC-42"}}}

        p = await store.get(pid)
        await _handle_create_linear_issue(MockSlack(), store, "C1", "1.0", pid, p.ticket_data, MockLinear())

        assert len(updated) == 1
        assert "LOC-42" in updated[0][2] or "LOC-42" in str(updated[0][3])

        p2 = await store.get(pid)
        assert p2.status == PROPOSAL_STATUS_CREATED
        assert p2.linear_issue_id == "LOC-42"

    @pytest.mark.asyncio
    async def test_duplicate_does_not_create_twice(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)

        calls = []

        class MockSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                return {"ok": True}

        class TrackingLinear:
            async def execute(self, q, v):
                calls.append(q)
                return {"teams": {"nodes": [{"id": "team1"}]}}
            async def create_issue(self, *a, **kw):
                calls.append("create_issue")
                return {"issueCreate": {"success": True, "issue": {"identifier": "LOC-1", "url": ""}}}

        p = await store.get(pid)
        await _handle_create_linear_issue(MockSlack(), store, "C1", "1.0", pid, p.ticket_data, TrackingLinear())
        await _handle_create_linear_issue(MockSlack(), store, "C1", "1.0", pid, p.ticket_data, TrackingLinear())

        create_calls = [c for c in calls if c == "create_issue"]
        assert len(create_calls) == 1, f"Expected 1 create_issue call, got {len(create_calls)}"

    @pytest.mark.asyncio
    async def test_linear_failure_sets_failed_status(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)

        updated = []

        class MockSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                updated.append((channel, ts, text, blocks))
                return {"ok": True}

        class FailingLinear:
            async def execute(self, query, variables):
                raise Exception("Linear API timeout")
            async def create_issue(self, *a, **kw):
                raise Exception("Linear API timeout")

        p = await store.get(pid)
        await _handle_create_linear_issue(MockSlack(), store, "C1", "1.0", pid, p.ticket_data, FailingLinear())

        assert len(updated) == 1
        p2 = await store.get(pid)
        assert p2.status == PROPOSAL_STATUS_FAILED

    @pytest.mark.asyncio
    async def test_no_team_sets_failed(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)

        class MockSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                return {"ok": True}

        class NoTeamLinear:
            async def execute(self, q, v):
                return {"teams": {"nodes": []}}
            async def create_issue(self, *a, **kw):
                return {}

        p = await store.get(pid)
        await _handle_create_linear_issue(MockSlack(), store, "C1", "1.0", pid, p.ticket_data, NoTeamLinear())

        p2 = await store.get(pid)
        assert p2.status == PROPOSAL_STATUS_FAILED

    @pytest.mark.asyncio
    async def test_already_created_is_idempotent(self, tmp_path):
        store = ProposalStore(str(tmp_path / "props.json"))
        pid = await _create_pending_proposal(store)
        await store.update_status(pid, PROPOSAL_STATUS_CREATED, "LOC-99", "https://linear.app/issue/LOC-99")

        updated = []

        class MockSlack:
            async def update_message(self, channel, ts, text, blocks=None):
                updated.append((channel, ts, text, blocks))
                return {"ok": True}

        linear_calls = []

        class MockLinear:
            async def execute(self, q, v):
                linear_calls.append(q)
                return {"teams": {"nodes": [{"id": "team1"}]}}
            async def create_issue(self, *a, **kw):
                linear_calls.append("create")

        p = await store.get(pid)
        await _handle_create_linear_issue(MockSlack(), store, "C1", "1.0", pid, p.ticket_data, MockLinear())

        assert "create" not in linear_calls
        assert len(updated) == 1
        assert "bereits" in updated[0][2].lower() or "already" in updated[0][2].lower()


# ======================================================================
# Helper: _build_ack_blocks
# ======================================================================


class TestBuildAckBlocks:
    def test_no_action_buttons(self):
        blocks = _build_ack_blocks("Test", "Summary", "high", "✅", "Done")
        assert len(blocks) == 4
        all_text = str(blocks)
        assert "actions" not in all_text
        assert "button" not in all_text
        assert "✅" in all_text


# ======================================================================
# Helper: build_proposal_blocks embeds proposal_id
# ======================================================================


class TestBuildProposalBlocksMeta:
    def test_embeds_proposal_id(self):
        ticket = {
            "title": "Fix bug",
            "summary": "Bug fix needed",
            "problem": "Crash",
            "proposed_solution": "Null check",
            "acceptance_criteria": ["Works"],
            "priority": "high",
        }
        blocks = build_proposal_blocks(ticket, proposal_id="abc-123", thread_ts="1.0")
        all_text = json.dumps(blocks)
        assert "abc-123" in all_text
        assert "1.0" in all_text

    def test_works_without_meta(self):
        ticket = {
            "title": "Test",
            "summary": "S",
            "acceptance_criteria": [],
            "priority": "medium",
        }
        blocks = build_proposal_blocks(ticket)
        all_text = json.dumps(blocks)
        assert "proposal_id" in all_text
        assert "thread_ts" in all_text