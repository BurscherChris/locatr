from app.github.client import GitHubClient

class GitHubTools:
    def __init__(self, client: GitHubClient): self.client = client
    async def create_pull_request(self, repository: str, title: str, head: str, base: str, body: str) -> dict:
        data = await self.client.create_pull_request(repository, title, head, base, body)
        return {"number":data["number"],"url":data["html_url"],"state":data["state"]}
    async def get_pull_request(self, repository: str, number: int) -> dict:
        data = await self.client.get_pull_request(repository, number)
        return {"number":data["number"],"url":data["html_url"],"state":data["state"]}
