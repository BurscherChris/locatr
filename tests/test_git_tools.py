import subprocess
import pytest
from app.tools.git import GitTools

@pytest.mark.asyncio
async def test_git_status_and_commit(git_repository):
    (git_repository / "message.txt").write_text("changed\n")
    tools = GitTools(git_repository, "", 5)
    assert "message.txt" in (await tools.git_status())["output"]
    await tools.git_commit("change message")
    assert "change message" in (await tools.git_log())["output"]
