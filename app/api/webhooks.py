import json
import logging
from fastapi import APIRouter, Header, HTTPException, Request, status
from app.agent.runner import AgentRunner
from app.config import get_settings
from app.errors import WebhookValidationError
from app.jobs import JobManager
from app.linear.webhook import verify_signature

log = logging.getLogger(__name__)
router = APIRouter()
jobs = JobManager()


def _safe_log_payload_shape(payload: dict, label: str) -> None:
    event_type = payload.get("type", "")
    action = payload.get("action", "")
    top_keys = list(payload.keys())
    data = payload.get("data") or {}
    data_keys = list(data.keys()) if isinstance(data, dict) else []
    has_agent_session = "agentSession" in data_keys or "agentSession" in top_keys
    has_issue = "issue" in data_keys or "issue" in top_keys
    has_prompt_context = "promptContext" in data_keys or "promptContext" in top_keys
    has_comment = "comment" in data_keys or "comment" in top_keys
    log.info("Webhook payload shape [%s] top_keys=%s data_keys=%s event_type=%s action=%s "
             "has_agent_session=%s has_issue=%s has_prompt_context=%s has_comment=%s",
             label, top_keys, data_keys, event_type, action,
             has_agent_session, has_issue, has_prompt_context, has_comment)


def _classify_event(payload: dict) -> dict:
    _safe_log_payload_shape(payload, "raw")

    event_type = str(payload.get("type") or "")
    action = str(payload.get("action") or "")
    webhook_id = str(payload.get("webhookId") or "")

    data = payload.get("data")
    if isinstance(data, dict) and data.get("agentSession"):
        agent_session = data["agentSession"]
    else:
        agent_session = payload.get("agentSession") or {}
    if not agent_session and isinstance(data, dict):
        agent_session = data.get("agentSession") or {}

    issue = agent_session.get("issue") or {}
    if not issue and isinstance(data, dict):
        issue = data.get("issue") or {}

    identifier = str(issue.get("identifier") or "")
    issue_id = str(issue.get("id") or "")
    repo_sources = [issue.get("repositoryUrl")]
    if isinstance(data, dict):
        repo_sources.append(data.get("repositoryUrl"))
    repo_sources.append(agent_session.get("repositoryUrl"))
    repository = str(next((r for r in repo_sources if r), ""))

    title = str(issue.get("title") or "")
    description = str(issue.get("description") or "")
    task = description or title

    agent_session_source = agent_session.get("id")
    if not agent_session_source and isinstance(data, dict):
        agent_session_source = data.get("agentSessionId")
    agent_session_id = str(agent_session_source or "")

    # The idempotency key must be unique per event delivery.
    # Linear uses the same webhookId for all events from a subscription.
    # The agent_session_id uniquely identifies the session and action
    # distinguishes delivery (created vs prompted vs retry).
    idempotency_key = f"{agent_session_id}:{action}" if agent_session_id else webhook_id

    is_agent_session = (
        event_type.lower() in ("agentsession", "agentsessionevent")
        or action.lower() in ("created", "prompted")
    )

    log.info("Webhook parsed event_type=%s action=%s webhook_id=%s idempotency_key=%s identifier=%s "
             "agent_session_id=%s repo_present=%s task_present=%s",
             event_type, action, webhook_id, idempotency_key, identifier,
             agent_session_id, bool(repository), bool(task))

    return {
        "event_type": event_type,
        "action": action,
        "event_id": idempotency_key,
        "idempotency_key": idempotency_key,
        "webhook_id": webhook_id,
        "issue_identifier": identifier,
        "issue_id": issue_id,
        "repository": repository,
        "task": task,
        "agent_session_id": agent_session_id,
        "is_agent_session": is_agent_session,
        "agent_session": agent_session,
        "issue": issue,
    }


@router.post("/webhooks/linear", status_code=status.HTTP_202_ACCEPTED)
async def linear_webhook(request: Request, linear_signature: str | None = Header(default=None)):
    body = await request.body()
    log.info("Linear webhook received has_signature=%s content_length=%s", bool(linear_signature), len(body))

    try:
        verify_signature(body, linear_signature, get_settings().linear_webhook_secret)
    except WebhookValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("Linear webhook rejected: malformed JSON")
        raise HTTPException(status_code=400, detail="malformed payload") from exc

    if not isinstance(payload, dict):
        log.warning("Linear webhook rejected: payload is not an object")
        raise HTTPException(status_code=400, detail="payload must be an object")

    info = _classify_event(payload)

    if not info["is_agent_session"]:
        log.info("Linear webhook ignored: unsupported event type event_type=%s action=%s",
                 info["event_type"], info["action"])
        return {"status": "ignored", "reason": "unsupported event type"}

    if not info["event_id"]:
        log.warning("Linear webhook rejected: AgentSession event missing event_id")
        raise HTTPException(status_code=400, detail="malformed AgentSession event: missing event_id")

    if not info["issue_identifier"]:
        log.warning("Linear webhook rejected: AgentSession event missing issue identifier")
        raise HTTPException(status_code=400, detail="malformed AgentSession event: missing issue identifier")

    if not info["agent_session_id"]:
        log.warning("Linear webhook rejected: AgentSession event missing agentSession.id")
        raise HTTPException(status_code=400, detail="malformed AgentSession event: missing agentSession")

    if not jobs.accept_event(info["event_id"]):
        log.info("Linear webhook event already processed event_id=%s issue=%s", info["event_id"], info["issue_identifier"])
        return {"status": "duplicate"}

    settings = get_settings()

    repository = info["repository"] or settings.github_repo or ""
    log.debug("Repository resolution event_id=%s issue=%s webhook_repo=%s config_repo=%s",
              info["event_id"], info["issue_identifier"], info["repository"], settings.github_repo)
    if not repository:
        log.warning("Linear webhook rejected: no repository URL in event or GITHUB_REPO config "
                     "event_id=%s issue=%s", info["event_id"], info["issue_identifier"])
        raise HTTPException(status_code=400, detail="AgentSession event: repository URL required "
                            "— set GITHUB_REPO or ensure issue provides repositoryUrl")

    async def run():
        return await AgentRunner(settings).run(
            repository,
            info["issue_identifier"],
            info["task"],
            issue_id=info["issue_id"],
        )

    jobs.submit(info["event_id"], run)
    log.info("Linear webhook accepted event_id=%s action=%s issue=%s job_id=%s",
             info["event_id"], info["action"], info["issue_identifier"], info["event_id"])
    return {"status": "accepted", "job_id": info["event_id"]}