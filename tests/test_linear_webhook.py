import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def _sig(body, secret=b"secret"):
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


_BASE_AGENT_SESSION = {
    "id": "as-1",
    "issue": {
        "id": "i-1",
        "identifier": "PI-142",
        "title": "Implement the viscosity recommendation endpoint",
        "description": "Build the REST API endpoint for viscosity recommendations",
        "repositoryUrl": "https://github.com/company/lims.git",
    },
}


def _agent_session_created(overrides=None, flat=False):
    payload = {
        "webhookId": "evt-as-created-1",
        "type": "AgentSessionEvent" if flat else "AgentSession",
        "action": "created",
    }
    if flat:
        payload["agentSession"] = dict(_BASE_AGENT_SESSION)
        payload["data"] = {"promptContext": {"guidance": "Follow existing patterns", "previousComments": []}}
    else:
        payload["data"] = {
            "agentSession": dict(_BASE_AGENT_SESSION),
            "promptContext": {"guidance": "Follow existing architecture patterns", "previousComments": []},
        }
    if overrides:
        _deep_merge(payload, overrides)
    return json.dumps(payload).encode()


def _agent_session_prompted(overrides=None, flat=False):
    payload = {
        "webhookId": "evt-as-prompted-1",
        "type": "AgentSessionEvent" if flat else "AgentSession",
        "action": "prompted",
    }
    if flat:
        payload["agentSession"] = dict(_BASE_AGENT_SESSION)
        payload["data"] = {"comment": {"body": "Please proceed with the implementation"}}
    else:
        payload["data"] = {
            "agentSession": dict(_BASE_AGENT_SESSION),
            "comment": {"body": "Please proceed with the implementation"},
        }
    if overrides:
        _deep_merge(payload, overrides)
    return json.dumps(payload).encode()


def _deep_merge(base, overrides):
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def test_invalid_signature_returns_400(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=b"{}", headers={"Linear-Signature": "sha256=bad"})
        assert resp.status_code == 400


def test_missing_signature_returns_400(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=b"{}")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Non-agent events → 202/ignored
# ---------------------------------------------------------------------------

def test_issue_event_ignored_not_400(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = json.dumps({"webhookId": "evt-issue-1", "type": "Issue", "data": {"id": "issue-1"}}).encode()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"


def test_unknown_event_ignored(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = json.dumps({"webhookId": "evt-unknown-1", "type": "Comment", "data": {}}).encode()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"


# ---------------------------------------------------------------------------
# AgentSession created event → 202 + job enqueued
# ---------------------------------------------------------------------------

def test_agent_session_created_accepted(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = _agent_session_created()
    calls = []

    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue, task, issue_id))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][1] == "PI-142"
    assert "viscosity" in calls[0][2]


# ---------------------------------------------------------------------------
# AgentSession prompted event → 202 + job enqueued
# ---------------------------------------------------------------------------

def test_agent_session_prompted_accepted(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = _agent_session_prompted()
    calls = []

    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue, task, issue_id))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][1] == "PI-142"


# ---------------------------------------------------------------------------
# Flat AgentSessionEvent (Linear's actual payload shape)
# ---------------------------------------------------------------------------

def test_flat_agentsession_created_accepted(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = _agent_session_created({"webhookId": "evt-flat-created-1"}, flat=True)
    calls = []

    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue, task, issue_id))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][1] == "PI-142"


def test_flat_agentsession_prompted_accepted(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = _agent_session_prompted({"webhookId": "evt-flat-prompted-1"}, flat=True)
    calls = []

    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue, task, issue_id))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][1] == "PI-142"


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

def test_agent_session_deduplicated(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = _agent_session_created({"webhookId": "evt-dedup-test-1"})

    async def fake_run(self, *args, **kwargs):
        return {"status": "completed"}
    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)

    with TestClient(app) as client:
        r1 = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert r1.status_code == 202
        assert r1.json()["status"] == "accepted"
        r2 = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert r2.status_code == 202
        assert r2.json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# Malformed / missing fields
# ---------------------------------------------------------------------------

def test_agent_session_missing_event_id(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = json.dumps({"type": "AgentSession", "action": "created", "data": {"agentSession": {"issue": {"identifier": "X-1"}}}}).encode()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 400


def test_agent_session_missing_issue_identifier(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = json.dumps({"webhookId": "e1", "type": "AgentSession", "action": "created", "data": {"agentSession": {"id": "as-1"}}}).encode()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 400


def test_malformed_json_returns_400(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=b"not json", headers={"Linear-Signature": _sig(b"not json")})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# AgentSession with optional fields missing (tolerates missing repository/task)
# ---------------------------------------------------------------------------

def test_agent_session_minimal_fields(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()
    body = json.dumps({
        "webhookId": "evt-min-1",
        "type": "AgentSession",
        "action": "created",
        "data": {
            "agentSession": {
                "id": "as-min-1",
                "issue": {"id": "i-min", "identifier": "MIN-1", "title": "Minimal task"},
            },
        },
    }).encode()
    calls = []

    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue, task, issue_id))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][1] == "MIN-1"


# ---------------------------------------------------------------------------
# Background job lifecycle
# ---------------------------------------------------------------------------

def test_background_job_logs_failure(monkeypatch):
    """A background job that raises an exception must log it and be visible."""
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_REPO", "https://github.com/org/repo.git")
    get_settings.cache_clear()

    body = json.dumps({
        "webhookId": "evt-background-fail-1",
        "type": "AgentSessionEvent",
        "action": "created",
        "agentSession": {"id": "as-bg-1", "issue": {"id": "i-bg", "identifier": "BG-1", "title": "bg test"}},
        "data": {},
    }).encode()

    async def failing_runner(self, repo, issue, task, base_branch="main", issue_id=None):
        raise RuntimeError("simulated background failure")

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", failing_runner)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"

    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    from app.api.webhooks import jobs
    job = jobs.jobs.get("evt-background-fail-1")
    assert job is not None
    assert job.status == "failed"
    assert "simulated background failure" in (job.error or "")


# ---------------------------------------------------------------------------
# Repository resolution: webhook with repo URL
# ---------------------------------------------------------------------------

def test_repo_resolution_from_webhook(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    get_settings.cache_clear()

    body = json.dumps({
        "webhookId": "evt-repo-webhook-1",
        "type": "AgentSessionEvent",
        "action": "created",
        "agentSession": {
            "id": "as-repo-1",
            "issue": {"id": "i-repo", "identifier": "REPO-1", "title": "repo test", "repositoryUrl": "https://github.com/corp/project.git"},
        },
        "data": {},
    }).encode()

    calls = []
    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][0] == "https://github.com/corp/project.git"


# ---------------------------------------------------------------------------
# Repository resolution: missing repo URL → requires GITHUB_REPO
# ---------------------------------------------------------------------------

def test_repo_resolution_missing_repo_url(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_REPO", "")
    get_settings.cache_clear()

    body = json.dumps({
        "webhookId": "evt-repo-missing-1",
        "type": "AgentSessionEvent",
        "action": "created",
        "agentSession": {"id": "as-no-repo", "issue": {"id": "i-no", "identifier": "NONE-1", "title": "no repo"}},
        "data": {},
    }).encode()

    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 400, resp.text
        assert "repository URL" in resp.text or "GITHUB_REPO" in resp.text


# ---------------------------------------------------------------------------
# Repository resolution: missing repo URL but GITHUB_REPO configured
# ---------------------------------------------------------------------------

def test_repo_resolution_from_github_repo_config(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_REPO", "https://github.com/myorg/myteam.git")
    get_settings.cache_clear()

    body = json.dumps({
        "webhookId": "evt-repo-config-1",
        "type": "AgentSessionEvent",
        "action": "created",
        "agentSession": {"id": "as-cfg-1", "issue": {"id": "i-cfg", "identifier": "CFG-1", "title": "config repo"}},
        "data": {},
    }).encode()

    calls = []
    async def fake_run(self, repo, issue, task, base_branch="main", issue_id=None):
        calls.append((repo, issue))
        return {"status": "completed"}

    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        resp = client.post("/webhooks/linear", content=body, headers={"Linear-Signature": _sig(body)})
        assert resp.status_code == 202, resp.text
    import asyncio
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))
    assert len(calls) == 1
    assert calls[0][0] == "https://github.com/myorg/myteam.git"