import logging
from pathlib import Path
from app.errors import GitError
from app.git.manager import run_git

log = logging.getLogger(__name__)

class GitTools:
    def __init__(self, workspace: Path, token: str, timeout: int): self.workspace, self.token, self.timeout = workspace, token, timeout
    async def git_status(self) -> dict: return {"output": await run_git(self.workspace, "status", "--short", timeout=self.timeout)}
    async def git_diff(self) -> dict: return {"output": await run_git(self.workspace, "diff", "--", timeout=self.timeout)}
    async def git_log(self, limit: int = 10) -> dict: return {"output": await run_git(self.workspace, "log", f"-{min(limit, 50)}", "--oneline", timeout=self.timeout)}
    async def git_create_branch(self, branch: str) -> dict:
        if not branch.startswith("agent/"): raise GitError("agent branches must start with agent/")
        return {"branch": await run_git(self.workspace, "checkout", "-B", branch, timeout=self.timeout)}
    async def git_commit(self, message: str) -> dict:
        # Keep credentials and runtime configuration out of agent-created commits.
        await run_git(self.workspace, "add", "-A", "--", ".", ":(exclude).env", ":(exclude)**/.env", ":(exclude).ssh/**", timeout=self.timeout)
        return {"commit": await run_git(self.workspace, "commit", "-m", message, timeout=self.timeout)}
    async def git_push(self, branch: str) -> dict:
        try:
            result = await run_git(self.workspace, "push", "-u", "origin", branch, timeout=self.timeout, token=self.token)
            return {"push": result}
        except Exception as exc:
            log.warning("git_push failed branch=%s error=%s", branch, exc)
            raise
