import httpx
import pytest
from app.linear.client import LinearClient

@pytest.mark.asyncio
async def test_linear_comment(monkeypatch):
    async def request(self, url, **kwargs): return httpx.Response(200, request=httpx.Request("POST", url), json={"data":{"commentCreate":{"success":True}}})
    monkeypatch.setattr(httpx.AsyncClient,"post",request)
    assert (await LinearClient("key","https://linear").add_comment("i","hello"))["commentCreate"]["success"]
