from app.linear.client import LinearClient

class LinearTools:
    def __init__(self, client: LinearClient): self.client = client
    async def update_linear_issue(self, issue_id: str, state_id: str | None = None, description: str | None = None) -> dict: return await self.client.update_issue(issue_id, state_id, description)
    async def add_linear_comment(self, issue_id: str, body: str) -> dict: return await self.client.add_comment(issue_id, body)
    async def add_linear_activity(self, issue_id: str, content: str) -> dict: return await self.client.add_activity(issue_id, content)
