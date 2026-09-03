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

def _event_details(payload: dict) -> tuple[str, str, str, str, str | None]:
    event_id = str(payload.get("webhookId") or payload.get("id") or payload.get("eventId") or "")
    event_type = str(payload.get("type") or payload.get("action") or "")
    data = payload.get("data") or payload
    issue = data.get("issue") or payload.get("issue") or {}
    identifier = issue.get("identifier") or data.get("issueIdentifier")
    issue_id = issue.get("id") or data.get("issueId")
    repository = data.get("repositoryUrl") or data.get("repository")
    task = data.get("task") or issue.get("description") or issue.get("title")
    if not event_id or not identifier or not repository or not task: raise WebhookValidationError("unsupported or malformed AgentSession event")
    if "agent" not in event_type.lower() and "session" not in event_type.lower(): raise WebhookValidationError("unsupported webhook event type")
    return event_id, identifier, repository, task, issue_id

@router.post("/webhooks/linear", status_code=status.HTTP_202_ACCEPTED)
async def linear_webhook(request: Request, linear_signature: str | None = Header(default=None)):
    body = await request.body()
    try:
        verify_signature(body, linear_signature, get_settings().linear_webhook_secret)
        payload = json.loads(body)
        if not isinstance(payload, dict): raise WebhookValidationError("payload must be an object")
        event_id, issue, repository, task, issue_id = _event_details(payload)
    except (json.JSONDecodeError, WebhookValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not jobs.accept_event(event_id): return {"status":"duplicate"}
    settings = get_settings()
    async def run():
        return await AgentRunner(settings).run(repository, issue, task, issue_id=issue_id)
    jobs.submit(event_id, run)
    log.info("accepted Linear event event_id=%s issue=%s", event_id, issue)
    return {"status":"accepted", "job_id":event_id}
