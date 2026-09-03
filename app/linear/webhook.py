import hashlib
import hmac
from app.errors import WebhookValidationError


def verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if not secret: raise WebhookValidationError("LINEAR_WEBHOOK_SECRET is not configured")
    if not signature: raise WebhookValidationError("missing Linear webhook signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied): raise WebhookValidationError("invalid Linear webhook signature")
