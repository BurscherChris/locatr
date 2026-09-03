import httpx
import json
import pytest
from app.errors import LinearError
from app.linear.client import LinearClient


def _response(status, json_data, url="https://linear"):
    return httpx.Response(status, request=httpx.Request("POST", url), json=json_data)


@pytest.mark.asyncio
async def test_linear_comment(monkeypatch):
    async def post(self, url, headers, json, **kwargs):
        assert headers["Content-Type"] == "application/json"
        assert "x-apollo-operation-name" in headers
        return _response(200, {"data": {"commentCreate": {"success": True}}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert (await LinearClient("key", "https://linear").add_comment("i", "hello"))["commentCreate"]["success"]


@pytest.mark.asyncio
async def test_graphql_errors_raises_linear_error(monkeypatch):
    async def post(self, url, headers, json, **kwargs):
        return _response(200, {"errors": [{"message": "This operation has been blocked as a potential CSRF"}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(LinearError, match="CSRF"):
        await LinearClient("key", "https://linear").add_comment("i", "hello")


@pytest.mark.asyncio
async def test_http_400_raises_linear_error(monkeypatch):
    async def post(self, url, headers, json, **kwargs):
        return _response(400, {"error": "Bad Request"})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(LinearError, match="400"):
        await LinearClient("key", "https://linear").add_comment("i", "hello")


@pytest.mark.asyncio
async def test_content_type_is_application_json(monkeypatch):
    captured = {}
    async def post(self, url, headers, json, **kwargs):
        captured["ct"] = headers.get("Content-Type")
        captured["auth"] = bool(headers.get("Authorization"))
        return _response(200, {"data": {"ok": True}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await LinearClient("key", "https://linear").add_comment("i", "hello")
    assert captured["ct"] == "application/json"
    assert captured["auth"] is True


@pytest.mark.asyncio
async def test_authorization_header_with_api_key(monkeypatch):
    captured = {}
    async def post(self, url, headers, json, **kwargs):
        captured["auth"] = headers.get("Authorization")
        return _response(200, {"data": {"ok": True}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await LinearClient("lin-api-key-abc", "https://linear").add_comment("i", "hello")
    assert captured["auth"] == "lin-api-key-abc"


@pytest.mark.asyncio
async def test_apollo_header_present(monkeypatch):
    captured = {}
    async def post(self, url, headers, json, **kwargs):
        captured["apollo"] = headers.get("x-apollo-operation-name")
        return _response(200, {"data": {"ok": True}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await LinearClient("key", "https://linear").add_comment("i", "hello")
    assert captured["apollo"] == "agentActivity"