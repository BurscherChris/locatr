import hashlib
import hmac
import logging

from app.errors import WebhookValidationError

log = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if not secret:
        log.warning("Linear webhook rejected: webhook secret not configured")
        raise WebhookValidationError("LINEAR_WEBHOOK_SECRET is not configured")
    if not signature:
        log.warning("Linear webhook rejected: missing signature")
        raise WebhookValidationError("missing Linear webhook signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        log.warning("Linear webhook rejected: invalid signature")
        raise WebhookValidationError("invalid Linear webhook signature")
    log.info("Linear webhook signature validated")