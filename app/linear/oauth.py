import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel

from app.errors import AuthenticationError, LinearError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token data model
# ---------------------------------------------------------------------------

class LinearOAuthTokens(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_at: float = 0.0
    refresh_token: str = ""
    scope: str = ""

    @classmethod
    def from_token_response(cls, data: dict) -> "LinearOAuthTokens":
        now = time.time()
        return cls(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_at=now + float(data.get("expires_in", 3600)),
            refresh_token=data.get("refresh_token", ""),
            scope=data.get("scope", ""),
        )

    def is_expired(self, margin: float = 60.0) -> bool:
        return time.time() + margin >= self.expires_at

# ---------------------------------------------------------------------------
# OAuth state store
# ---------------------------------------------------------------------------

@dataclass
class OAuthState:
    value: str
    expires_at: float
    consumed: bool = False


class OAuthStateStore:
    def __init__(self, ttl: int = 600):
        self._states: dict[str, OAuthState] = {}
        self._ttl = ttl

    def generate(self) -> str:
        value = secrets.token_urlsafe(32)
        self._states[value] = OAuthState(value=value, expires_at=time.time() + self._ttl)
        return value

    def validate(self, value: str | None) -> bool:
        if not value or value not in self._states:
            return False
        state = self._states[value]
        if state.consumed:
            return False
        if time.time() > state.expires_at:
            return False
        state.consumed = True
        return True

# ---------------------------------------------------------------------------
# Token file store
# ---------------------------------------------------------------------------

class LinearTokenFileStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def save(self, tokens: LinearOAuthTokens) -> None:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(tokens.model_dump_json())
            self._path.chmod(0o600)

    async def load(self) -> LinearOAuthTokens | None:
        async with self._lock:
            if not self._path.is_file():
                return None
            try:
                return LinearOAuthTokens.model_validate_json(self._path.read_text())
            except Exception as exc:
                log.warning("failed to load OAuth tokens: %s", exc)
                return None

    async def delete(self) -> None:
        async with self._lock:
            if self._path.is_file():
                self._path.unlink()

# ---------------------------------------------------------------------------
# OAuth HTTP client
# ---------------------------------------------------------------------------

LINEAR_AUTHORIZE_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_REVOKE_URL = "https://api.linear.app/oauth/revoke"


class LinearOAuthClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http_timeout: int = 60,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http_timeout = http_timeout

    def build_authorize_url(self, state: str, scopes: str, actor: str) -> str:
        import urllib.parse
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": scopes,
            "state": state,
            "actor": actor,
        }
        return f"{LINEAR_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        return await self._token_request(data)

    async def refresh_access_token(self, refresh_token: str) -> dict:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        return await self._token_request(data)

    async def revoke_token(self, access_token: str) -> None:
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(
                LINEAR_REVOKE_URL,
                data={"token": access_token, "client_id": self.client_id, "client_secret": self.client_secret},
            )
            if resp.status_code >= 400:
                raise LinearError(f"token revoke failed: {resp.status_code}")

    async def _token_request(self, payload: dict) -> dict:
        import time as _time
        start = _time.monotonic()
        log.info("Exchanging Linear OAuth authorization code")
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(
                LINEAR_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        duration_ms = int((_time.monotonic() - start) * 1000)
        body_text = resp.text
        try:
            body = resp.json()
        except Exception:
            body = {}
        log.info("Linear OAuth token exchange response status=%s duration_ms=%s", resp.status_code, duration_ms)
        if resp.status_code >= 400:
            err = body.get("error_description", body.get("error", resp.reason_phrase or "unknown"))
            log.error("Linear OAuth token exchange failed status=%s error=%s error_description=%s",
                      resp.status_code, body.get("error", "unknown"), body.get("error_description", ""))
            raise AuthenticationError(err)
        if resp.status_code == 200:
            log.info("Linear OAuth token exchange successful")
        if "access_token" not in body:
            raise AuthenticationError("token response missing access_token")
        return body

# ---------------------------------------------------------------------------
# Token manager (refresh with async lock)
# ---------------------------------------------------------------------------

_all_token_refresh_locks: dict[str, asyncio.Lock] = {}


class LinearTokenManager:
    def __init__(self, oauth_client: LinearOAuthClient, token_store: LinearTokenFileStore):
        self._client = oauth_client
        self._store = token_store

    async def get_valid_token(self) -> str:
        tokens = await self._store.load()
        if tokens is None:
            raise AuthenticationError("no OAuth tokens available")
        if not tokens.is_expired():
            return tokens.access_token
        lock_key = "linear_token_refresh"
        if lock_key not in _all_token_refresh_locks:
            _all_token_refresh_locks[lock_key] = asyncio.Lock()
        lock = _all_token_refresh_locks[lock_key]
        async with lock:
            tokens = await self._store.load()
            if tokens is None:
                raise AuthenticationError("no OAuth tokens available")
            if not tokens.is_expired():
                return tokens.access_token
            new_data = await self._client.refresh_access_token(tokens.refresh_token)
            new_tokens = LinearOAuthTokens.from_token_response(new_data)
            if not new_tokens.refresh_token:
                new_tokens.refresh_token = tokens.refresh_token
            await self._store.save(new_tokens)
            return new_tokens.access_token

    async def authenticated_header(self) -> dict[str, str]:
        token = await self.get_valid_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}