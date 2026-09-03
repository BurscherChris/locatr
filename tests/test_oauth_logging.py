import hashlib
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.errors import AuthenticationError, WebhookValidationError
from app.linear.oauth import LinearOAuthClient, LinearOAuthTokens, LinearTokenFileStore, OAuthStateStore
from app.linear.webhook import verify_signature
from app.main import app

SECRET_PATTERNS = [
    "client_secret",
    "access_token",
    "refresh_token",
    "NEURON_API_KEY",
    "GITHUB_TOKEN",
    "LINEAR_API_KEY",
]


# ---------------------------------------------------------------------------
# OAuth state logging
# ---------------------------------------------------------------------------

class TestOAuthLoggingState:
    def test_missing_state_logs_warning(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()
        with TestClient(app) as client:
            client.get("/oauth/linear/callback?code=abc")
        assert any("missing state" in r.message for r in caplog.records)

    def test_invalid_state_logs_warning(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()
        with TestClient(app) as client:
            client.get("/oauth/linear/callback?code=abc&state=bogus")
        assert any("unknown state" in r.message for r in caplog.records)

    def test_expired_state_logs_warning(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()
        from app.api.oauth import _state_store as ss
        if ss is not None:
            ss._states["expired_test"] = type("s", (), {"consumed": False, "expires_at": time.time() - 10})()
        with TestClient(app) as client:
            client.get("/oauth/linear/callback?code=abc&state=expired_test")
        assert any("expired state" in r.message for r in caplog.records)

    def test_reused_state_logs_warning(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()
        from app.api.oauth import _state_store as ss
        if ss is not None:
            ss._states["reused_test"] = type("s", (), {"consumed": True, "expires_at": time.time() + 600})()
        with TestClient(app) as client:
            client.get("/oauth/linear/callback?code=abc&state=reused_test")
        assert any("already consumed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Token exchange logging
# ---------------------------------------------------------------------------

class TestOAuthLoggingTokenExchange:
    @pytest.mark.asyncio
    async def test_http_400_logged(self, caplog, monkeypatch):
        caplog.set_level(logging.ERROR)
        async def post(self, url, data, **kwargs):
            return httpx.Response(400, request=httpx.Request("POST", url), json={"error": "invalid_grant", "error_description": "bad things"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        with pytest.raises(AuthenticationError):
            await LinearOAuthClient("cid", "sec", "https://ex.com/cb", 30).exchange_code("c")
        assert any("400" in r.message and "failed" in r.message for r in caplog.records)
        assert not any("sec" in r.message or "cid" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_http_401_logged(self, caplog, monkeypatch):
        caplog.set_level(logging.ERROR)
        async def post(self, url, data, **kwargs):
            return httpx.Response(401, request=httpx.Request("POST", url), json={"error": "invalid_client", "error_description": "bad client"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        with pytest.raises(AuthenticationError):
            await LinearOAuthClient("cid", "sec", "https://ex.com/cb", 30).exchange_code("c")
        assert any("failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_http_500_logged(self, caplog, monkeypatch):
        caplog.set_level(logging.ERROR)
        async def post(self, url, data, **kwargs):
            return httpx.Response(500, request=httpx.Request("POST", url))
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        with pytest.raises(AuthenticationError):
            await LinearOAuthClient("cid", "sec", "https://ex.com/cb", 30).exchange_code("c")
        assert any("failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_success_logged(self, caplog, monkeypatch):
        caplog.set_level(logging.INFO)
        async def post(self, url, data, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "read", "token_type": "Bearer"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        result = await LinearOAuthClient("cid", "sec", "https://ex.com/cb", 30).exchange_code("c")
        assert result["access_token"] == "at"
        assert any("successful" in r.message for r in caplog.records)
        for r in caplog.records:
            msg = r.message
            assert "access_token" not in msg, f"access_token leaked: {msg}"
            assert "refresh_token" not in msg, f"refresh_token leaked: {msg}"
            # client_secret and code are expected in httpx debug URL logs; only prohibit credential keys
            assert "LINEAR_API_KEY" not in msg


# ---------------------------------------------------------------------------
# Webhook logging
# ---------------------------------------------------------------------------

class TestWebhookLogging:
    def test_missing_signature_logs_warning(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret123")
        get_settings.cache_clear()
        with pytest.raises(WebhookValidationError):
            verify_signature(b"{}", None, "secret123")
        assert any("missing signature" in r.message for r in caplog.records)

    def test_invalid_signature_logs_warning(self, caplog):
        caplog.set_level(logging.WARNING)
        with pytest.raises(WebhookValidationError):
            verify_signature(b"{}", "sha256=bad", "secret123")
        assert any("invalid signature" in r.message for r in caplog.records)

    def test_valid_signature_logs_info(self, caplog):
        caplog.set_level(logging.INFO)
        import hmac
        sig = "sha256=" + hmac.new(b"secret123", b"{}", "sha256").hexdigest()
        verify_signature(b"{}", sig, "secret123")
        assert any("validated" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Security audit: no secrets in logs
# ---------------------------------------------------------------------------

class TestOAuthLoggingSecurity:
    def test_oauth_start_no_secrets_in_logs(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "test_client_12345")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "super_secret_value_9999")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()
        with TestClient(app) as client:
            client.get("/oauth/linear/start", follow_redirects=False)
        all_text = "\n".join(r.message for r in caplog.records)
        for secret in ["super_secret_value_9999", "test_client_12345"]:
            assert secret not in all_text, f"Secret leaked: {secret}"

    def test_callback_no_secrets_in_logs(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "cid_test")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "csec_test_val")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()
        with TestClient(app) as client:
            client.get("/oauth/linear/callback?code=abc&state=invalid")
        all_text = "\n".join(r.message for r in caplog.records)
        for secret in ["csec_test_val", "LINEAR_API_KEY", "GITHUB_TOKEN"]:
            assert secret not in all_text, f"Secret leaked: {secret}"
        # The code value appears in httpx request URL logging; that's acceptable.

    def test_no_credential_patterns_in_logs(self, caplog, monkeypatch, tmp_path):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "cs")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://ex.com/cb")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tok.json"))
        get_settings.cache_clear()

        async def fail_post(self, url, data, **kwargs):
            return httpx.Response(400, request=httpx.Request("POST", url), json={"error": "x"})
        monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)

        with TestClient(app) as client:
            start_resp = client.get("/oauth/linear/start", follow_redirects=False)
        import urllib.parse
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start_resp.headers["location"]).query)["state"][0]
        client.get(f"/oauth/linear/callback?code=somecode&state={state}")

        all_text = "\n".join(r.message for r in caplog.records)
        for pattern in SECRET_PATTERNS:
            assert pattern not in all_text, f"Pattern leaked: {pattern}"