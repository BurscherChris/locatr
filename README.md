# Neuron Coding Agent

Local, Docker-based coding agent for separate target repositories. The agent source never doubles as a target workspace.

```text
Linear -> Webhook -> Local Agent -> Neuron -> constrained tools -> GitHub
                                      |
                                 /workspaces/PI-142/repository
```

## Setup

```bash
cp .env.example .env
docker compose up --build
```

Set your own `NEURON_API_KEY`, `GITHUB_TOKEN`, and (for Linear) `LINEAR_API_KEY` and `LINEAR_WEBHOOK_SECRET` in `.env`. `.env` is ignored by Git. The service listens at `http://localhost:8000`.

```bash
curl http://localhost:8000/health
docker compose run --rm agent agent health
docker compose run --rm agent agent test
```

## Local Task

The CLI and Linear webhook use the same runtime and tool loop:

```bash
docker compose run --rm agent agent run \
  --repo https://github.com/company/lims.git \
  --issue PI-142 \
  --task "Implement the viscosity recommendation endpoint"
```

The workspace manager clones to `/workspaces/PI-142/repository`, checks out the requested base branch, and creates `agent/PI-142`. The named Docker volume persists workspaces. Remove that volume when a clean clone is needed.

## Linear Webhook

Configure Linear to deliver its AgentSession webhook to a public tunnel terminating at:

```text
https://<public-tunnel>/webhooks/linear -> http://localhost:8000/webhooks/linear
```

The endpoint validates the `Linear-Signature` HMAC-SHA256 over the raw request body using `LINEAR_WEBHOOK_SECRET`, accepts supported AgentSession-shaped events, deduplicates event IDs in memory, and immediately returns `202`. Work continues in an asyncio background job. The in-memory idempotency store is intentionally local-process only and can be replaced later with Redis or Postgres.

## API

- `GET /health` returns `{"status":"ok"}`.
- `GET /ready` reports which integrations have credentials without exposing them.
- `POST /webhooks/linear` validates and queues Linear webhook work.

## Linear OAuth

The agent supports Linear OAuth 2.0 authorization-code authentication for app-actor tokens.

### Configure OAuth

1. Create a **Linear OAuth Application** at https://linear.app/settings/api/applications/new.
2. Set the redirect URI to match your public tunnel URL:

   ```
   https://<public-tunnel>/oauth/linear/callback
   ```

3. Enable the `AgentSessionEvent` webhook subscription:

   ```
   https://<public-tunnel>/webhooks/linear
   ```

4. Configure in `.env`:

   ```
   LINEAR_CLIENT_ID=<your-client-id>
   LINEAR_CLIENT_SECRET=<your-client-secret>
   LINEAR_OAUTH_REDIRECT_URI=https://<public-tunnel>/oauth/linear/callback
   LINEAR_OAUTH_SCOPES=read,write,app:assignable,app:mentionable
   LINEAR_OAUTH_ACTOR=app
   LINEAR_TOKEN_STORE_PATH=/data/linear_tokens.json
   ```

5. Start the agent and open the authorize URL in a browser:

   ```
   https://<public-tunnel>/oauth/linear/start
   ```

6. After authorizing, Linear redirects back to `/oauth/linear/callback` and tokens are persisted to the configured path.

### OAuth endpoints

| Endpoint                     | Method | Description                               |
|------------------------------|--------|-------------------------------------------|
| `/oauth/linear/start`        | GET    | Redirects browser to Linear authorization |
| `/oauth/linear/callback`     | GET    | Handles authorization-code callback       |
| `/oauth/linear/status`       | GET    | Reports OAuth state (no tokens)           |
| `/oauth/linear/logout`       | POST   | Revokes token and deletes local storage   |

### Fallback to API key

When OAuth is not configured, the agent falls back to `LINEAR_API_KEY` for Linear API access. Both paths produce the same `LinearClient` interface so the existing agent runtime, tool loop, and webhook handler work unchanged regardless of which authentication method is active.

## Security Model

Target repositories are untrusted. Filesystem tools normalize all paths and reject anything outside the issue workspace, plus `.git`, `.ssh`, and `.env`. Secret-like files are excluded from commits. Commands run only inside the container workspace and use a configurable executable allowlist/denylist. Shell chaining, redirection, command substitution, absolute paths, and parent-directory escapes are rejected.

This is defense in depth, not a claim that Docker is a perfect boundary. Compose does not mount the Docker socket, host credentials, or privileged devices. Do not add those mounts. Git authentication uses `GIT_ASKPASS`, so tokens are not embedded in remote URLs or command arguments. Logs contain only safe run metadata.

The default command policy supports normal development commands such as `pytest`, `npm test`, `npm run build`, `python`, and `git`. Tune `COMMAND_ALLOWLIST` and `COMMAND_DENYLIST` for local needs.

## Development

Run the tests with Docker as above, or locally:

```bash
python -m pip install -e '.[dev]'
pytest
```

External APIs are mocked by the unit suite; no live Neuron, GitHub, or Linear credentials are needed for tests. A live `agent run` requires Neuron and GitHub credentials. Linear is optional in local mode, but creating a PR naturally requires a GitHub token and reachable target remote.
