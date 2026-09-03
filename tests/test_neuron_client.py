import httpx
import pytest
from app.neuron.client import NeuronClient
from app.errors import AuthenticationError, NeuronError

@pytest.mark.asyncio
async def test_neuron_sends_chat_request(monkeypatch):
    async def handler(self, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer key"
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices":[{"message":{"role":"assistant","content":"ok"}}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", handler)
    assert (await NeuronClient("https://test", "key", "model").complete([], []))["content"] == "ok"
@pytest.mark.asyncio
async def test_neuron_requires_key():
    with pytest.raises(AuthenticationError): await NeuronClient("https://test", "", "model").complete([], [])
