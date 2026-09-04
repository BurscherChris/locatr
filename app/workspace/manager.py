from pathlib import Path
from app.errors import WorkspaceError
from app.git.manager import run_git

class WorkspaceManager:
    def __init__(self, root: str, token: str = "", timeout: int = 120, git_name: str = "Neuron Coding Agent", git_email: str = "neuron-agent@localhost"):
        self.root, self.token, self.timeout, self.git_name, self.git_email = Path(root), token, timeout, git_name, git_email

    async def prepare(self, repository_url: str, issue: str, base_branch: str = "main", target_branch: str | None = None) -> Path:
        if not issue or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in issue):
            raise WorkspaceError("invalid issue identifier")
        workspace = self.root / issue / "repository"
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists() and not (workspace / ".git").is_dir():
            raise WorkspaceError("workspace exists but is not a git repository")
        if not workspace.exists():
            try:
                await run_git(workspace.parent, "clone", repository_url, workspace.name, timeout=self.timeout, token=self.token)
            except Exception as exc:
                raise WorkspaceError(f"unable to clone repository: {exc}") from exc
        try:
            await run_git(workspace, "fetch", "origin", base_branch, timeout=self.timeout, token=self.token)
            branch_to_use = target_branch or f"agent/{issue}"
            if target_branch == base_branch:
                # LOW priority: work directly on base_branch
                await run_git(workspace, "checkout", target_branch, timeout=self.timeout)
                await run_git(workspace, "reset", "--hard", f"origin/{base_branch}", timeout=self.timeout)
            else:
                await run_git(workspace, "checkout", "-B", branch_to_use, f"origin/{base_branch}", timeout=self.timeout)
            await run_git(workspace, "config", "user.name", self.git_name, timeout=self.timeout)
            await run_git(workspace, "config", "user.email", self.git_email, timeout=self.timeout)
        except Exception as exc:
            raise WorkspaceError(f"unable to prepare branch: {exc}") from exc
        return workspace
