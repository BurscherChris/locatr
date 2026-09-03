import httpx
from app.errors import AuthenticationError, GitHubError


class GitHubClient:
    def __init__(self, token: str, api_url: str, timeout: int = 60): self.token, self.api_url, self.timeout = token, api_url.rstrip("/"), timeout
    def _headers(self) -> dict[str, str]:
        if not self.token: raise AuthenticationError("GITHUB_TOKEN is not configured")
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client: response = await client.request(method, self.api_url + path, headers=self._headers(), **kwargs)
            if response.status_code in (401, 403): raise AuthenticationError("GitHub authentication failed")
            response.raise_for_status(); return response.json()
        except httpx.HTTPStatusError as exc: raise GitHubError(f"GitHub HTTP error: {exc.response.status_code}") from exc
        except httpx.HTTPError as exc: raise GitHubError(f"GitHub request failed: {exc}") from exc
    async def repository(self, owner_repo: str) -> dict: return await self._request("GET", f"/repos/{owner_repo}")
    async def create_pull_request(self, owner_repo: str, title: str, head: str, base: str, body: str) -> dict:
        return await self._request("POST", f"/repos/{owner_repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})
    async def get_pull_request(self, owner_repo: str, number: int) -> dict: return await self._request("GET", f"/repos/{owner_repo}/pulls/{number}")
