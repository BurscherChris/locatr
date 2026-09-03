import httpx
import pytest
from app.github.client import GitHubClient
from app.errors import GitHubError

@pytest.mark.asyncio
async def test_github_pr_and_failure(monkeypatch):
    async def success(self, method, url, **kwargs): return httpx.Response(201, request=httpx.Request(method, url), json={"number":3,"html_url":"https://pr","state":"open"})
    monkeypatch.setattr(httpx.AsyncClient,"request",success)
    result = await GitHubClient("x","https://api").create_pull_request("o/r","t","h","main","b")
    assert result["number"] == 3
    async def failure(self, method, url, **kwargs): return httpx.Response(500, request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.AsyncClient,"request",failure)
    with pytest.raises(GitHubError): await GitHubClient("x","https://api").repository("o/r")
