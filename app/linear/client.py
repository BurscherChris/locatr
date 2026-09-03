import httpx
from app.errors import AuthenticationError, LinearError


class LinearClient:
    def __init__(self, api_key: str, api_url: str, timeout: int = 60, token_manager=None):
        self.api_key, self.api_url, self.timeout, self._token_manager = api_key, api_url, timeout, token_manager

    async def execute(self, query: str, variables: dict) -> dict:
        headers = await self._headers()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.api_url, headers=headers, json={"query": query, "variables": variables})
            if response.status_code == 401 and self._token_manager:
                headers = await self._headers(force_refresh=True)
                async with httpx.AsyncClient(timeout=self.timeout) as client2:
                    response = await client2.post(self.api_url, headers=headers, json={"query": query, "variables": variables})
            response.raise_for_status(); data = response.json()
            if data.get("errors"): raise LinearError("Linear rejected request")
            return data["data"]
        except httpx.HTTPError as exc: raise LinearError(f"Linear request failed: {exc}") from exc

    async def _headers(self, force_refresh: bool = False) -> dict[str, str]:
        if self._token_manager:
            try:
                if force_refresh:
                    token = await self._token_manager.get_valid_token()
                else:
                    token = await self._token_manager.get_valid_token()
                return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            except AuthenticationError:
                if self.api_key:
                    return {"Authorization": self.api_key, "Content-Type": "application/json"}
                raise
        if not self.api_key:
            raise AuthenticationError("LINEAR_API_KEY is not configured")
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    async def update_issue(self, issue_id: str, state_id: str | None = None, description: str | None = None) -> dict:
        return await self.execute("mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}", {"id": issue_id, "input": {k:v for k,v in {"stateId":state_id,"description":description}.items() if v is not None}})
    async def add_comment(self, issue_id: str, body: str) -> dict:
        return await self.execute("mutation($issueId:String!,$body:String!){commentCreate(input:{issueId:$issueId,body:$body}){success}}", {"issueId":issue_id,"body":body})
    async def add_activity(self, issue_id: str, content: str) -> dict:
        return await self.add_comment(issue_id, f"Agent activity: {content}")
