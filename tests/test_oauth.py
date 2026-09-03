import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.errors import AuthenticationError
from app.linear.oauth import (
    LINEAR_TOKEN_URL,
    LinearOAuthClient,
    LinearOAuthTokens,
    LinearTokenFileStore,
    LinearTokenManager,
    OAuthStateStore,
)
from app.main import app


# ---------------------------------------------------------------------------
# State store tests
# ---------------------------------------------------------------------------

class TestOAuthStateStore:
    def test_generate_and_validate(self):
        store = OAuthStateStore(ttl=600)
        state = store.generate()
        assert store.validate(state) is True
        # one-time use
        assert store.validate(state) is False

    def test_missing_state(self):
        assert OAuthStateStore().validate(None) is False
        assert OAuthStateStore().validate("") is False

    def test_invalid_state(self):
        assert OAuthStateStore().validate("bogus") is False

    def test_expired_state(self):
        store = OAuthStateStore(ttl=-1)
        state = store.generate()
        time.sleep(0.01)
        assert store.validate(state) is False

    def test_reused_state(self):
        store = OAuthStateStore(ttl=600)
        state = store.generate()
        assert store.validate(state) is True
        assert store.validate(state) is False


# ---------------------------------------------------------------------------
# Token model tests
# ---------------------------------------------------------------------------

class TestLinearOAuthTokens:
    def test_from_token_response(self):
        tokens = LinearOAuthTokens.from_token_response({
            "access_token": "at1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "rt1",
            "scope": "read,write",
        })
        assert tokens.access_token == "at1"
        assert tokens.refresh_token == "rt1"
        assert tokens.scope == "read,write"
        assert tokens.is_expired() is False

    def test_expiry_detection(self):
        tokens = LinearOAuthTokens(access_token="at1", expires_at=time.time() - 10)
        assert tokens.is_expired() is True

    def test_expiry_with_margin(self):
        tokens = LinearOAuthTokens(access_token="at1", expires_at=time.time() + 30)
        assert tokens.is_expired(margin=60) is True


# ---------------------------------------------------------------------------
# Token store tests
# ---------------------------------------------------------------------------

class TestLinearTokenFileStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "tokens.json")
        store = LinearTokenFileStore(path)
        tokens = LinearOAuthTokens(access_token="at", refresh_token="rt", expires_at=1000.0)
        await store.save(tokens)
        loaded = await store.load()
        assert loaded is not None
        assert loaded.access_token == "at"
        assert loaded.refresh_token == "rt"

    @pytest.mark.asyncio
    async def test_save_sets_0600_permissions(self, tmp_path):
        p = Path(tmp_path / "tokens.json")
        store = LinearTokenFileStore(str(p))
        await store.save(LinearOAuthTokens(access_token="at", refresh_token="rt"))
        assert p.exists()
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    @pytest.mark.asyncio
    async def test_save_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "nested" / "subdir" / "tokens.json"
        store = LinearTokenFileStore(str(nested))
        await store.save(LinearOAuthTokens(access_token="at", refresh_token="rt"))
        assert nested.is_file()

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path):
        path = str(tmp_path / "tokens.json")
        store = LinearTokenFileStore(path)
        await store.save(LinearOAuthTokens(access_token="at", refresh_token="rt"))
        await store.delete()
        assert await store.load() is None

    @pytest.mark.asyncio
    async def test_load_missing(self, tmp_path):
        assert await LinearTokenFileStore(str(tmp_path / "nonexistent.json")).load() is None


# ---------------------------------------------------------------------------
# OAuth HTTP client tests
# ---------------------------------------------------------------------------

class TestLinearOAuthClient:
    def test_build_authorize_url(self):
        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        url = client.build_authorize_url("mystate", "read,write", "app")
        assert "client_id=cid" in url
        assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcallback" in url
        assert "state=mystate" in url
        assert "actor=app" in url
        assert "response_type=code" in url

    def test_authorize_url_contains_scope_and_actor(self):
        url = LinearOAuthClient("cid", "sec", "https://ex.com/cb", 30).build_authorize_url("st", "read,write,app:assignable,app:mentionable", "app")
        assert "scope=read%2Cwrite%2Capp%3Aassignable%2Capp%3Amentionable" in url
        assert "actor=app" in url
        assert "client_id=cid" in url

    @pytest.mark.asyncio
    async def test_exchange_code_has_no_actor(self, monkeypatch):
        captured = {}
        async def post(self, url, data, **kwargs):
            captured["data"] = data
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        await LinearOAuthClient("cid", "sec", "https://ex.com/cb", 30).exchange_code("c")
        assert "actor" not in captured["data"]

    @pytest.mark.asyncio
    async def test_exchange_code(self, monkeypatch):
        async def post(self, url, data, **kwargs):
            assert url == LINEAR_TOKEN_URL
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "authcode123"
            assert data["client_id"] == "cid"
            assert data["client_secret"] == "csecret"
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "read"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        result = await client.exchange_code("authcode123")
        assert result["access_token"] == "at"

    @pytest.mark.asyncio
    async def test_exchange_code_400(self, monkeypatch):
        async def post(self, url, data, **kwargs):
            return httpx.Response(400, request=httpx.Request("POST", url), json={"error": "invalid_grant", "error_description": "bad code"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        with pytest.raises(AuthenticationError, match="bad code"):
            await client.exchange_code("badcode")

    @pytest.mark.asyncio
    async def test_exchange_code_401(self, monkeypatch):
        async def post(self, url, data, **kwargs):
            return httpx.Response(401, request=httpx.Request("POST", url), json={"error": "invalid_client", "error_description": "bad secret"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        with pytest.raises(AuthenticationError, match="bad secret"):
            await LinearOAuthClient("cid", "csecret", "https://ex.com/cb", 30).exchange_code("c")

    @pytest.mark.asyncio
    async def test_exchange_code_500(self, monkeypatch):
        async def post(self, url, data, **kwargs):
            return httpx.Response(500, request=httpx.Request("POST", url))
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        with pytest.raises(AuthenticationError):
            await LinearOAuthClient("cid", "csecret", "https://ex.com/cb", 30).exchange_code("c")

    @pytest.mark.asyncio
    async def test_exchange_code_wrong_redirect_uri(self, monkeypatch):
        captured = {}
        async def post(self, url, data, **kwargs):
            captured["data"] = data
            return httpx.Response(400, request=httpx.Request("POST", url), json={"error": "invalid_grant", "error_description": "redirect_uri does not match"})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        with pytest.raises(AuthenticationError, match="redirect_uri does not match"):
            await LinearOAuthClient("cid", "csecret", "https://wrong.com/cb", 30).exchange_code("c")
        assert captured["data"]["redirect_uri"] == "https://wrong.com/cb"

    @pytest.mark.asyncio
    async def test_refresh_token(self, monkeypatch):
        async def post(self, url, data, **kwargs):
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "old_rt"
            assert data["client_id"] == "cid"
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        result = await client.refresh_access_token("old_rt")
        assert result["access_token"] == "new_at"
        assert result["refresh_token"] == "new_rt"

    @pytest.mark.asyncio
    async def test_revoke_token(self, monkeypatch):
        called_with = {}
        async def post(self, url, data, **kwargs):
            called_with["url"] = url; called_with["data"] = data
            return httpx.Response(200, request=httpx.Request("POST", url))
        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        await client.revoke_token("at_to_revoke")
        assert called_with["data"]["token"] == "at_to_revoke"
        assert called_with["data"]["client_id"] == "cid"


# ---------------------------------------------------------------------------
# Token manager refresh tests
# ---------------------------------------------------------------------------

class TestLinearTokenManager:
    @pytest.mark.asyncio
    async def test_get_valid_token_returns_cached(self, tmp_path):
        path = str(tmp_path / "tokens.json")
        store = LinearTokenFileStore(path)
        tokens = LinearOAuthTokens(access_token="at_valid", refresh_token="rt", expires_at=time.time() + 3600)
        await store.save(tokens)
        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        mgr = LinearTokenManager(client, store)
        token = await mgr.get_valid_token()
        assert token == "at_valid"

    @pytest.mark.asyncio
    async def test_refresh_expired_token(self, tmp_path, monkeypatch):
        path = str(tmp_path / "tokens.json")
        store = LinearTokenFileStore(path)
        tokens = LinearOAuthTokens(access_token="at_expired", refresh_token="rt", expires_at=time.time() - 10)
        await store.save(tokens)

        async def post(self, url, data, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "at_fresh", "refresh_token": "rt_new", "expires_in": 3600})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)

        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        mgr = LinearTokenManager(client, store)
        token = await mgr.get_valid_token()
        assert token == "at_fresh"

        loaded = await store.load()
        assert loaded.access_token == "at_fresh"
        assert loaded.refresh_token == "rt_new"

    @pytest.mark.asyncio
    async def test_refresh_keeps_old_refresh_token_when_not_rotated(self, tmp_path, monkeypatch):
        path = str(tmp_path / "tokens.json")
        store = LinearTokenFileStore(path)
        tokens = LinearOAuthTokens(access_token="at_expired", refresh_token="rt_original", expires_at=time.time() - 10)
        await store.save(tokens)

        async def post(self, url, data, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "at_fresh", "expires_in": 3600})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)

        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        mgr = LinearTokenManager(client, store)
        token = await mgr.get_valid_token()
        assert token == "at_fresh"

        loaded = await store.load()
        assert loaded.refresh_token == "rt_original"  # kept from old

    @pytest.mark.asyncio
    async def test_concurrent_refresh_is_serialized(self, tmp_path, monkeypatch):
        path = str(tmp_path / "tokens.json")
        store = LinearTokenFileStore(path)
        tokens = LinearOAuthTokens(access_token="at_expired", refresh_token="rt", expires_at=time.time() - 10)
        await store.save(tokens)

        call_count = 0

        async def post(self, url, data, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": f"at_fresh_{call_count}", "expires_in": 3600})
        monkeypatch.setattr(httpx.AsyncClient, "post", post)

        client = LinearOAuthClient("cid", "csecret", "https://example.com/callback", 30)
        mgr = LinearTokenManager(client, store)
        results = await asyncio.gather(mgr.get_valid_token(), mgr.get_valid_token(), mgr.get_valid_token())
        assert all(t == "at_fresh_1" for t in results)
        assert call_count == 1  # only one refresh happened


# ---------------------------------------------------------------------------
# HTTP route tests
# ---------------------------------------------------------------------------

class TestOAuthRoutes:
    @pytest.fixture(autouse=True)
    def _configure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LINEAR_CLIENT_ID", "test_cid")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "test_csecret")
        monkeypatch.setenv("LINEAR_OAUTH_REDIRECT_URI", "https://example.com/oauth/linear/callback")
        monkeypatch.setenv("LINEAR_TOKEN_STORE_PATH", str(tmp_path / "tokens.json"))
        get_settings.cache_clear()

    def test_oauth_start_redirects(self):
        with TestClient(app) as client:
            resp = client.get("/oauth/linear/start", follow_redirects=False)
            assert resp.status_code == 302
            location = resp.headers["location"]
            assert "linear.app/oauth/authorize" in location
            assert "response_type=code" in location
            assert "client_id=test_cid" in location

    def test_oauth_start_generates_state(self):
        with TestClient(app) as client:
            resp1 = client.get("/oauth/linear/start", follow_redirects=False)
            resp2 = client.get("/oauth/linear/start", follow_redirects=False)
            loc1 = resp1.headers["location"]
            loc2 = resp2.headers["location"]
            import urllib.parse
            s1 = urllib.parse.parse_qs(urllib.parse.urlparse(loc1).query)["state"][0]
            s2 = urllib.parse.parse_qs(urllib.parse.urlparse(loc2).query)["state"][0]
            assert s1 != s2

    def test_oauth_callback_requires_code(self):
        with TestClient(app) as client:
            resp = client.get("/oauth/linear/callback")
            assert resp.status_code == 400
            assert "Fehlender" in resp.text

    def test_oauth_callback_requires_state(self):
        with TestClient(app) as client:
            resp = client.get("/oauth/linear/callback?code=abc")
            assert resp.status_code == 400
            assert "State" in resp.text

    def test_oauth_callback_rejects_invalid_state(self):
        with TestClient(app) as client:
            resp = client.get("/oauth/linear/callback?code=abc&state=bogus")
            assert resp.status_code == 400
            assert "State" in resp.text

    def test_oauth_callback_handles_linear_error(self):
        with TestClient(app) as client:
            resp = client.get("/oauth/linear/callback?error=access_denied")
            assert resp.status_code == 400
            assert "access_denied" in resp.text

    def test_oauth_callback_exchanges_code(self, monkeypatch):
        async def exchange(self, code):
            return {"access_token": "at_cb", "refresh_token": "rt_cb", "expires_in": 3600, "scope": "read,write"}
        monkeypatch.setattr("app.linear.oauth.LinearOAuthClient.exchange_code", exchange, raising=False)

        with TestClient(app) as client:
            # first get a valid state
            start_resp = client.get("/oauth/linear/start", follow_redirects=False)
            import urllib.parse
            state = urllib.parse.parse_qs(urllib.parse.urlparse(start_resp.headers["location"]).query)["state"][0]
            resp = client.get(f"/oauth/linear/callback?code=validcode&state={state}")
            assert resp.status_code == 200
            assert "verbunden" in resp.text

    def test_oauth_callback_rejects_reused_state(self, monkeypatch):
        async def exchange(self, code):
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        monkeypatch.setattr("app.linear.oauth.LinearOAuthClient.exchange_code", exchange, raising=False)

        with TestClient(app) as client:
            start_resp = client.get("/oauth/linear/start", follow_redirects=False)
            import urllib.parse
            state = urllib.parse.parse_qs(urllib.parse.urlparse(start_resp.headers["location"]).query)["state"][0]
            resp1 = client.get(f"/oauth/linear/callback?code=ok&state={state}")
            assert resp1.status_code == 200
            resp2 = client.get(f"/oauth/linear/callback?code=ok&state={state}")
            assert resp2.status_code == 400

    def test_oauth_callback_rejects_expired_state(self, monkeypatch):
        import time
        from app.api.oauth import _state_store as state_store_ref
        if state_store_ref:
            monkeypatch.setattr(state_store_ref, "_ttl", -1)

        async def exchange(self, code):
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        monkeypatch.setattr("app.linear.oauth.LinearOAuthClient.exchange_code", exchange, raising=False)

        with TestClient(app) as client:
            start_resp = client.get("/oauth/linear/start", follow_redirects=False)
            import urllib.parse
            state = urllib.parse.parse_qs(urllib.parse.urlparse(start_resp.headers["location"]).query)["state"][0]
            time.sleep(0.01)
            resp = client.get(f"/oauth/linear/callback?code=ok&state={state}")
            assert resp.status_code == 400

    def test_oauth_callback_400_from_linear(self, monkeypatch):
        async def failing(self, code):
            raise Exception("some error")
        monkeypatch.setattr("app.linear.oauth.LinearOAuthClient.exchange_code", failing, raising=False)
        with TestClient(app) as client:
            start_resp = client.get("/oauth/linear/start", follow_redirects=False)
            import urllib.parse
            state = urllib.parse.parse_qs(urllib.parse.urlparse(start_resp.headers["location"]).query)["state"][0]
            resp = client.get(f"/oauth/linear/callback?code=c&state={state}")
            assert resp.status_code in (400, 502)

    def test_oauth_callback_does_not_expose_tokens(self, monkeypatch):
        async def exchange(self, code):
            return {"access_token": "at_secret", "refresh_token": "rt_secret", "expires_in": 3600}
        monkeypatch.setattr("app.linear.oauth.LinearOAuthClient.exchange_code", exchange, raising=False)
        import urllib.parse
        with TestClient(app) as client:
            start_resp = client.get("/oauth/linear/start", follow_redirects=False)
            state = urllib.parse.parse_qs(urllib.parse.urlparse(start_resp.headers["location"]).query)["state"][0]
            resp = client.get(f"/oauth/linear/callback?code=random&state={state}")
            assert "at_secret" not in resp.text
            assert "access_token" not in resp.text
            assert "rt_secret" not in resp.text

    def test_oauth_status_does_not_expose_tokens(self):
        with TestClient(app) as client:
            resp = client.get("/oauth/linear/status")
            body = resp.json()
            assert "authenticated" in body
            assert "access_token" not in json.dumps(body)
            assert "client_secret" not in json.dumps(body)

    def test_oauth_logout(self, monkeypatch):
        async def revoke(self, token):
            pass
        monkeypatch.setattr("app.linear.oauth.LinearOAuthClient.revoke_token", revoke, raising=False)
        with TestClient(app) as client:
            resp = client.post("/oauth/linear/logout")
            assert resp.status_code == 200
            assert resp.json()["status"] == "logged_out"

    def test_oauth_not_configured_returns_501(self, monkeypatch):
        import app.api.oauth as oauth_module
        oauth_module._state_store = None
        oauth_module._token_store = None
        oauth_module._token_manager = None
        monkeypatch.setenv("LINEAR_CLIENT_ID", "")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "")
        get_settings.cache_clear()
        with TestClient(app) as client:
            assert client.get("/oauth/linear/start").status_code == 501
            assert client.get("/oauth/linear/callback").status_code == 501
            assert client.get("/oauth/linear/status").status_code == 501