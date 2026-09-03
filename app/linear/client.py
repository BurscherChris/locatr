import httpx
from app.errors import AuthenticationError, LinearError


class LinearClient:
    def __init__(self, api_key: str, api_url: str, timeout: int = 60): self.api_key, self.api_url, self.timeout = api_key, api_url, timeout
    async def execute(self, query: str, variables: dict) -> dict:
        if not self.api_key: raise AuthenticationError("LINEAR_API_KEY is not configured")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.api_url, headers={"Authorization": self.api_key, "Content-Type": "application/json"}, json={"query": query, "variables": variables})
            response.raise_for_status(); data = response.json()
            if data.get("errors"): raise LinearError("Linear rejected request")
            return data["data"]
        except httpx.HTTPError as exc: raise LinearError(f"Linear request failed: {exc}") from exc
    async def update_issue(self, issue_id: str, state_id: str | None = None, description: str | None = None) -> dict:
        return await self.execute("mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}", {"id": issue_id, "input": {k:v for k,v in {"stateId":state_id,"description":description}.items() if v is not None}})
    async def add_comment(self, issue_id: str, body: str) -> dict:
        return await self.execute("mutation($issueId:String!,$body:String!){commentCreate(input:{issueId:$issueId,body:$body}){success}}", {"issueId":issue_id,"body":body})
    async def add_activity(self, issue_id: str, content: str) -> dict:
        # A normal issue comment is universally supported and serves as a visible local-agent activity.
        return await self.add_comment(issue_id, f"Agent activity: {content}")
