import pytest
from app.workspace.manager import WorkspaceManager

@pytest.mark.asyncio
async def test_workspace_clone_and_branch(tmp_path, git_repository):
    workspace = await WorkspaceManager(str(tmp_path / "workspaces"), timeout=5).prepare(str(git_repository), "PI-142")
    assert (workspace / "message.txt").exists()
    assert (await __import__("app.git.manager",fromlist=["run_git"]).run_git(workspace,"branch","--show-current")) == "agent/PI-142"
