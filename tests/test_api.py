from fastapi.testclient import TestClient
from app.main import app
def test_health_and_ready():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status":"ok"}
        ready = client.get("/ready").json()
        assert ready["status"] == "ready"
        assert {"neuron","github","linear"}.issubset(ready)
