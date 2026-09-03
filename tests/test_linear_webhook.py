import hashlib
import hmac
from fastapi.testclient import TestClient
from app.config import get_settings
from app.main import app

def _signature(body): return hmac.new(b"secret", body, hashlib.sha256).hexdigest()
def test_linear_webhook_validates_and_deduplicates(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "secret"); get_settings.cache_clear()
    body = b'{"webhookId":"evt-1","type":"AgentSession","data":{"issue":{"id":"i","identifier":"PI-1","title":"work"},"repositoryUrl":"https://example/repo.git"}}'
    async def fake_run(self, *args, **kwargs): return {"status":"completed"}
    monkeypatch.setattr("app.api.webhooks.AgentRunner.run", fake_run)
    with TestClient(app) as client:
        response = client.post("/webhooks/linear", content=body, headers={"Linear-Signature":_signature(body)})
        assert response.status_code == 202
        assert client.post("/webhooks/linear", content=body, headers={"Linear-Signature":_signature(body)}).json()["status"] == "duplicate"
        assert client.post("/webhooks/linear", content=body, headers={"Linear-Signature":"wrong"}).status_code == 400
