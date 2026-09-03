import hashlib
import logging
import time
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth/linear")

_state_store: OAuthStateStore | None = None
_token_store: LinearTokenFileStore | None = None
_token_manager: LinearTokenManager | None = None


def _state_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _ensure_oauth_configured():
    s = get_settings()
    if not s.linear_client_id or not s.linear_client_secret:
        raise HTTPException(status_code=501, detail="Linear OAuth not configured")
    global _state_store, _token_store, _token_manager
    if _state_store is None:
        _state_store = OAuthStateStore()
    if _token_store is None:
        _token_store = LinearTokenFileStore(s.linear_token_store_path)
    if _token_manager is None:
        client = LinearOAuthClient(
            s.linear_client_id, s.linear_client_secret,
            s.linear_oauth_redirect_uri, s.http_timeout_seconds,
        )
        _token_manager = LinearTokenManager(client, _token_store)
    return s


_SUCCESS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Linear OAuth – Neuron Agent</title>
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; }}
.ok {{ color: #090; font-size: 1.2em; }}
</style></head><body>
<h1>Linear OAuth erfolgreich verbunden.</h1>
<p class="ok">Der Neuron Coding Agent ist jetzt mit Linear verbunden.</p>
<p>Actor: <strong>{actor}</strong></p>
<p>Du kannst dieses Fenster schließen.</p></body></html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Linear OAuth – Fehler</title>
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; }}
.err {{ color: #c00; }}
</style></head><body>
<h1>Linear OAuth – Fehler</h1>
<p class="err">{message}</p>
</body></html>"""


# ---------------------------------------------------------------------------
# GET /oauth/linear/start
# ---------------------------------------------------------------------------

@router.get("/start")
async def oauth_start():
    s = _ensure_oauth_configured()
    log.info("Linear OAuth start initiated")
    log.info("Linear OAuth configuration: actor=%s scopes=%s redirect_uri=%s client_id_present=%s",
             s.linear_oauth_actor, s.linear_oauth_scopes, s.linear_oauth_redirect_uri,
             bool(s.linear_client_id))
    state = _state_store.generate()
    fp = _state_fingerprint(state)
    log.debug("OAuth state generated state_fingerprint=%s expires_in_seconds=%s", fp, _state_store._ttl)
    client = LinearOAuthClient(
        s.linear_client_id, s.linear_client_secret,
        s.linear_oauth_redirect_uri, s.http_timeout_seconds,
    )
    url = client.build_authorize_url(state, s.linear_oauth_scopes, s.linear_oauth_actor)
    log.debug("Redirecting to Linear OAuth authorization endpoint")
    return RedirectResponse(url=url, status_code=302)


# ---------------------------------------------------------------------------
# GET /oauth/linear/callback
# ---------------------------------------------------------------------------

@router.get("/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    s = _ensure_oauth_configured()
    log.info("Linear OAuth callback received has_code=%s has_state=%s has_error=%s",
             bool(code), bool(state), bool(error))

    if error:
        log.warning("Linear OAuth callback contains provider error error=%s error_description=%s",
                    error, error_description or "")
        msg = error_description or error
        return HTMLResponse(_ERROR_HTML.format(message=f"OAuth-Fehler von Linear: {msg}"), status_code=400)

    if not code:
        log.warning("Linear OAuth callback rejected: missing code")
        return HTMLResponse(_ERROR_HTML.format(message="Fehlender Authorization-Code."), status_code=400)

    if not state:
        log.warning("Linear OAuth callback rejected: missing state")
        return HTMLResponse(_ERROR_HTML.format(message="Fehlender State-Parameter."), status_code=400)

    state_fp = _state_fingerprint(state)
    if not _state_store:
        log.warning("Linear OAuth callback rejected: state store unavailable state_fingerprint=%s", state_fp)
        return HTMLResponse(_ERROR_HTML.format(message="State-Prüfung nicht verfügbar."), status_code=400)

    if state not in _state_store._states:
        log.warning("Linear OAuth callback rejected: unknown state state_fingerprint=%s", state_fp)
        return HTMLResponse(_ERROR_HTML.format(message="Ungültiger oder abgelaufener State."), status_code=400)

    stored = _state_store._states[state]
    if stored.consumed:
        log.warning("Linear OAuth callback rejected: state already consumed state_fingerprint=%s", state_fp)
        return HTMLResponse(_ERROR_HTML.format(message="Ungültiger oder abgelaufener State."), status_code=400)

    if time.time() > stored.expires_at:
        log.warning("Linear OAuth callback rejected: expired state state_fingerprint=%s", state_fp)
        return HTMLResponse(_ERROR_HTML.format(message="Ungültiger oder abgelaufener State."), status_code=400)

    _state_store._states[state].consumed = True
    log.info("Linear OAuth state validated state_fingerprint=%s", state_fp)

    try:
        log.info("Exchanging Linear OAuth authorization code")
        client = LinearOAuthClient(
            s.linear_client_id, s.linear_client_secret,
            s.linear_oauth_redirect_uri, s.http_timeout_seconds,
        )
        token_data = await client.exchange_code(code)
        tokens = LinearOAuthTokens.from_token_response(token_data)
        log.info("Linear OAuth tokens received has_access_token=true has_refresh_token=%s expires_in=%s token_type=%s",
                 bool(tokens.refresh_token),
                 int(tokens.expires_at - time.time()),
                 tokens.token_type)
        store = LinearTokenFileStore(s.linear_token_store_path)
        try:
            await store.save(tokens)
            log.info("Linear OAuth tokens stored successfully")
        except Exception:
            log.exception("Failed to persist Linear OAuth tokens")
            raise
        log.info("Linear OAuth installation completed successfully actor=%s scopes=%s",
                 s.linear_oauth_actor, tokens.scope)
        return HTMLResponse(_SUCCESS_HTML.format(actor=s.linear_oauth_actor))
    except AuthenticationError as exc:
        log.warning("OAuth token exchange failed: %s", exc)
        return HTMLResponse(_ERROR_HTML.format(message=str(exc)), status_code=400)
    except Exception as exc:
        log.exception("Linear OAuth callback failed unexpectedly")
        return HTMLResponse(_ERROR_HTML.format(message="Token-Austausch fehlgeschlagen."), status_code=502)


# ---------------------------------------------------------------------------
# GET /oauth/linear/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def oauth_status():
    s = _ensure_oauth_configured()
    store = LinearTokenFileStore(s.linear_token_store_path)
    tokens = await store.load()
    if tokens is None:
        return {
            "configured": True,
            "authenticated": False,
            "hint": "Visit /oauth/linear/start in a browser to authorize the application",
            "start_url": "/oauth/linear/start",
        }
    remaining = max(0.0, tokens.expires_at - time.time())
    return {
        "configured": True,
        "authenticated": True,
        "actor": s.linear_oauth_actor,
        "expires_in": int(remaining),
        "scope": tokens.scope.split(",") if tokens.scope else [],
    }


# ---------------------------------------------------------------------------
# POST /oauth/linear/logout
# ---------------------------------------------------------------------------

@router.post("/logout")
async def oauth_logout():
    s = _ensure_oauth_configured()
    store = LinearTokenFileStore(s.linear_token_store_path)
    tokens = await store.load()
    if tokens:
        try:
            client = LinearOAuthClient(
                s.linear_client_id, s.linear_client_secret,
                s.linear_oauth_redirect_uri, s.http_timeout_seconds,
            )
            await client.revoke_token(tokens.access_token)
        except Exception as exc:
            log.warning("token revoke failed (ignored): %s", exc)
    await store.delete()
    return {"status": "logged_out"}