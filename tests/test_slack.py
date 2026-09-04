import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.slack.client import SlackClient, SlackMessage
from app.slack.events import is_ticket_command, normalize_event
from app.slack.ticket import (
    ProposalStore,
    validate_ticket_data,
    build_proposal_blocks,
    slack_priority_to_linear,
    extract_ticket_from_thread,
)
from app.slack.webhook import verify_slack_signature
from app.errors import WebhookValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slack_sig(body, secret=b"secret", timestamp=None):
    ts = timestamp or str(int(time.time()))
    sig_basestring = f"v0:{ts}:{body.decode() if isinstance(body, bytes) else body}"
    return "v0=" + hmac.new(secret, sig_basestring.encode(), hashlib.sha256).hexdigest(), ts


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

class TestSlackSignature:
    def test_valid_signature(self):
        body = b'{"type":"url_verification","challenge":"abc123"}'
        sig, ts = _slack_sig(body)
        verify_slack_signature("secret", body, ts, sig)

    def test_invalid_signature(self):
        body = b'{"type":"url_verification"}'
        # Use a fresh timestamp to avoid stale rejection
        ts = str(int(time.time()))
        with pytest.raises(WebhookValidationError, match="invalid"):
            verify_slack_signature("secret", body, ts, "v0=bad")

    def test_missing_secret(self):
        with pytest.raises(WebhookValidationError, match="not configured"):
            verify_slack_signature("", b"{}", "1234567890", "v0=abc")

    def test_stale_timestamp(self):
        ts = str(int(time.time()) - 600)
        body = b'{}'
        sig, _ = _slack_sig(body, timestamp=ts)
        with pytest.raises(WebhookValidationError, match="stale"):
            verify_slack_signature("secret", body, ts, sig)

    def test_missing_signature_header(self):
        with pytest.raises(WebhookValidationError, match="missing"):
            verify_slack_signature("secret", b"{}", "1234567890", "")


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

class TestTriggerDetection:
    def test_german_ticket_command(self):
        assert is_ticket_command("@Neuron erstelle daraus ein Ticket") is True

    def test_german_ticket_command_variation(self):
        assert is_ticket_command("@Neuron mach daraus bitte ein Linear Ticket") is True

    def test_german_summarize_command(self):
        assert is_ticket_command("@Neuron fasse das als Ticket zusammen") is True

    def test_english_ticket_command(self):
        assert is_ticket_command("@Neuron create a ticket from this") is True

    def test_english_summarize(self):
        assert is_ticket_command("@Neuron summarize this as a ticket") is True

    def test_normal_conversation_not_triggered(self):
        assert is_ticket_command("Was denkt ihr darüber?") is False
        assert is_ticket_command("Kann jemand das implementieren?") is False
        assert is_ticket_command("@John kannst du das anschauen?") is False

    def test_empty_text_not_triggered(self):
        assert is_ticket_command("") is False


# ---------------------------------------------------------------------------
# Webhook route tests
# ---------------------------------------------------------------------------

class TestSlackWebhook:
    def test_url_verification(self, monkeypatch):
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()

        body = json.dumps({"type": "url_verification", "challenge": "challengestring"}).encode()
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack", content=body, headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
            })
            assert resp.status_code == 200
            assert resp.json()["challenge"] == "challengestring"

    def test_missing_config_returns_501(self, monkeypatch):
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        get_settings.cache_clear()
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack", content=b"{}")
            assert resp.status_code == 501

    def test_invalid_signature_returns_400(self, monkeypatch):
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack", content=b"{}", headers={
                "x-slack-request-timestamp": "1234567890",
                "x-slack-signature": "v0=bad",
            })
            assert resp.status_code == 400

    def test_normal_event_returns_ok(self, monkeypatch):
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = json.dumps({
            "token": "test",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {"type": "message", "subtype": "bot_message", "text": "hello", "channel": "C1", "ts": "123.456"},
        }).encode()
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack", content=body, headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
            })
            assert resp.status_code == 200

    def test_duplicate_event_idempotent(self, monkeypatch):
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = json.dumps({
            "token": "t", "team_id": "T1", "event_id": "EvDup",
            "event": {"type": "message", "text": "hello", "channel": "C1", "ts": "123.456"},
        }).encode()
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp1 = client.post("/webhooks/slack", content=body, headers={
                "x-slack-request-timestamp": ts, "x-slack-signature": sig,
            })
            assert resp1.status_code == 200
            resp2 = client.post("/webhooks/slack", content=body, headers={
                "x-slack-request-timestamp": ts, "x-slack-signature": sig,
            })
            assert resp2.status_code == 200

    def test_malformed_json_returns_400(self, monkeypatch):
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        get_settings.cache_clear()
        body = b"not json"
        sig, ts = _slack_sig(body)
        with TestClient(app) as client:
            resp = client.post("/webhooks/slack", content=body, headers={
                "x-slack-request-timestamp": ts, "x-slack-signature": sig,
            })
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Slack API client tests
# ---------------------------------------------------------------------------

class TestSlackClient:
    @pytest.mark.asyncio
    async def test_get_thread_replies(self, monkeypatch):
        async def mock_get(self, path, params):
            assert path == "/conversations.replies"
            return {"ok": True, "messages": [
                {"user": "U1", "text": "parent", "ts": "1.0", "thread_ts": "1.0"},
                {"user": "U2", "text": "reply", "ts": "2.0", "thread_ts": "1.0"},
            ]}
        monkeypatch.setattr("app.slack.client.SlackClient._get", mock_get)
        client = SlackClient("xoxb-test", 30)
        msgs = await client.get_thread_replies("C1", "1.0")
        assert len(msgs) == 2
        assert msgs[0].text == "parent"

    @pytest.mark.asyncio
    async def test_get_thread_replies_pagination(self, monkeypatch):
        call_count = [0]
        async def mock_get(self, path, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"ok": True, "messages": [{"user": "U1", "text": "page1", "ts": "1.0", "thread_ts": "1.0"}], "response_metadata": {"next_cursor": "cursor2"}}
            return {"ok": True, "messages": [{"user": "U2", "text": "page2", "ts": "2.0", "thread_ts": "1.0"}]}
        monkeypatch.setattr("app.slack.client.SlackClient._get", mock_get)
        client = SlackClient("xoxb-test", 30)
        msgs = await client.get_thread_replies("C1", "1.0")
        assert len(msgs) == 2
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_post_message(self, monkeypatch):
        async def mock_post(self, path, json_data):
            return {"ok": True, "ts": "111.222"}
        monkeypatch.setattr("app.slack.client.SlackClient._post", mock_post)
        client = SlackClient("xoxb-test", 30)
        result = await client.post_message("C1", "hello", thread_ts="1.0")
        assert result["ts"] == "111.222"

    @pytest.mark.asyncio
    async def test_slack_api_error(self, monkeypatch):
        from app.slack.client import SlackApiError
        async def mock_get(self, path, params):
            raise SlackApiError("channel_not_found")
        monkeypatch.setattr("app.slack.client.SlackClient._get", mock_get)
        client = SlackClient("xoxb-test", 30)
        with pytest.raises(SlackApiError):
            await client.get_thread_replies("C1", "1.0")

    @pytest.mark.asyncio
    async def test_get_user_info(self, monkeypatch):
        async def mock_get(self, path, params):
            return {"ok": True, "user": {"profile": {"display_name": "Alice"}, "name": "alice"}}
        monkeypatch.setattr("app.slack.client.SlackClient._get", mock_get)
        client = SlackClient("xoxb-test", 30)
        info = await client.get_user_info("U1")
        assert info["display_name"] == "Alice"

    def test_missing_token(self):
        with pytest.raises(Exception, match="not configured"):
            SlackClient("", 30)


# ---------------------------------------------------------------------------
# Ticket data validation
# ---------------------------------------------------------------------------

class TestTicketValidation:
    def test_valid_ticket(self):
        data = validate_ticket_data({
            "title": "Fix login bug",
            "summary": "Users cannot log in",
            "problem": "Auth fails",
            "proposed_solution": "Update JWT library",
            "acceptance_criteria": ["Login works", "Tests pass"],
            "open_questions": ["Deployment?"],
            "priority": "high",
        })
        assert data["title"] == "Fix login bug"
        assert data["priority"] == "high"

    def test_invalid_priority(self):
        with pytest.raises(ValueError, match="priority"):
            validate_ticket_data({
                "title": "Test",
                "summary": "Test",
                "acceptance_criteria": [],
                "priority": "critical",
            })

    def test_missing_title(self):
        with pytest.raises(ValueError, match="title"):
            validate_ticket_data({
                "summary": "Test",
                "acceptance_criteria": [],
                "priority": "medium",
            })

    def test_missing_acceptance_criteria(self):
        with pytest.raises(ValueError, match="acceptance_criteria"):
            validate_ticket_data({
                "title": "Test",
                "summary": "Test",
                "priority": "medium",
            })


# ---------------------------------------------------------------------------
# Ticket extraction (Neuron)
# ---------------------------------------------------------------------------

class TestTicketExtraction:
    @pytest.mark.asyncio
    async def test_extract_valid(self, monkeypatch):
        class MockNeuron:
            async def complete(self, messages, tools, temperature=0.2, max_tokens=None):
                return {"content": json.dumps({
                    "title": "Fix authentication",
                    "summary": "Users cannot authenticate",
                    "problem": "Login returns 500",
                    "proposed_solution": "Update auth middleware",
                    "acceptance_criteria": ["Login works", "Error handling"],
                    "open_questions": [],
                    "priority": "high",
                })}
        result = await extract_ticket_from_thread(MockNeuron(), [SlackMessage(user="U1", text="Login is broken", ts="1.0")])
        assert result["title"] == "Fix authentication"
        assert result["priority"] == "high"

    @pytest.mark.asyncio
    async def test_extract_invalid_json(self, monkeypatch):
        class MockNeuron:
            async def complete(self, messages, tools, temperature=0.2, max_tokens=None):
                return {"content": "not json at all"}
        with pytest.raises(ValueError, match="invalid JSON"):
            await extract_ticket_from_thread(MockNeuron(), [SlackMessage(user="U1", text="test", ts="1.0")])


# ---------------------------------------------------------------------------
# Proposal store
# ---------------------------------------------------------------------------

class TestProposalStore:
    @pytest.mark.asyncio
    async def test_create_and_get(self, tmp_path):
        store = ProposalStore(str(tmp_path / "proposals.json"))
        proposal = await store.create("C1", "1.0", "1.0", {"title": "Test", "summary": "S", "acceptance_criteria": [], "priority": "medium"})
        assert proposal.status == "pending"
        loaded = await store.get(proposal.proposal_id)
        assert loaded is not None
        assert loaded.ticket_data["title"] == "Test"

    @pytest.mark.asyncio
    async def test_update_status(self, tmp_path):
        store = ProposalStore(str(tmp_path / "proposals.json"))
        p = await store.create("C1", "1.0", "1.0", {"title": "T", "summary": "S", "acceptance_criteria": [], "priority": "low"})
        await store.update_status(p.proposal_id, "created", "LOC-1", "https://linear.app/issue/LOC-1")
        loaded = await store.get(p.proposal_id)
        assert loaded.status == "created"
        assert loaded.linear_issue_id == "LOC-1"

    @pytest.mark.asyncio
    async def test_find_by_thread(self, tmp_path):
        store = ProposalStore(str(tmp_path / "proposals.json"))
        p = await store.create("C1", "1.0", "1.0", {"title": "T", "summary": "S", "acceptance_criteria": [], "priority": "medium"})
        found = await store.find_by_thread("C1", "1.0")
        assert found is not None
        assert found.proposal_id == p.proposal_id


# ---------------------------------------------------------------------------
# Proposal blocks
# ---------------------------------------------------------------------------

class TestProposalBlocks:
    def test_build_proposal_blocks(self):
        ticket = {
            "title": "Fix bug",
            "summary": "Bug fix needed",
            "problem": "Crash on startup",
            "proposed_solution": "Add null check",
            "acceptance_criteria": ["Startup works"],
            "open_questions": ["Deploy?"],
            "priority": "urgent",
        }
        blocks = build_proposal_blocks(ticket)
        assert len(blocks) > 0
        all_text = str(blocks)
        assert "Fix bug" in all_text
        assert "Urgent" in all_text or "urgent" in all_text


# ---------------------------------------------------------------------------
# Priority mapping
# ---------------------------------------------------------------------------

class TestPriorityMapping:
    def test_low(self):
        assert slack_priority_to_linear("low") == 4

    def test_medium(self):
        assert slack_priority_to_linear("medium") == 3

    def test_high(self):
        assert slack_priority_to_linear("high") == 2

    def test_urgent(self):
        assert slack_priority_to_linear("urgent") == 1

    def test_unknown_default(self):
        assert slack_priority_to_linear("unknown") == 3