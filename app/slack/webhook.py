"""Slack Events API signature verification.

Uses Slack's signing secret (HMAC-SHA256) to verify request authenticity.
"""

import hashlib
import hmac
import logging
import time

from app.errors import WebhookValidationError

log = logging.getLogger(__name__)


def verify_slack_signature(signing_secret: str, body: bytes, timestamp: str, signature: str) -> None:
    """Validate a Slack request signature.

    Raises WebhookValidationError if the signature is invalid or the
    timestamp is too old (stale requests are rejected as a CSRF mitigation).
    """
    if not signing_secret:
        raise WebhookValidationError("SLACK_SIGNING_SECRET is not configured")

    if not timestamp or not signature:
        raise WebhookValidationError("missing Slack signature headers")

    # Reject timestamps older than 5 minutes (CSRF protection)
    try:
        request_age = abs(time.time() - int(timestamp))
        if request_age > 300:
            log.warning("Slack request rejected: stale timestamp age=%ss", int(request_age))
            raise WebhookValidationError("stale Slack request timestamp")
    except ValueError:
        raise WebhookValidationError("invalid Slack timestamp") from None

    # Compute expected signature
    sig_basestring = f"v0:{timestamp}:{body.decode()}" if isinstance(body, bytes) else f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        log.warning("Slack request rejected: invalid signature")
        raise WebhookValidationError("invalid Slack request signature")

    log.info("Slack request signature validated")