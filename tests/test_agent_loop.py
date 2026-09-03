import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from app.agent.governance import GovernanceState, GovernanceMode
from app.agent.loop import AgentLoop
from app.agent.runner import build_registry, verify_completion
from app.errors import ToolExecutionError
from app.git.manager import run_git


_GOV_PR = GovernanceState(GovernanceMode.PR_REQUIRED)


class ScriptedNeuron:
    def __init__(self): self.calls = 0
    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1: return {"role":"assistant","tool_calls":[{"id":"1","function":{"name":"write_file","arguments":json.dumps({"path":"changed.txt","content":"done\n"})}}]}
        if self.calls == 2: return {"role":"assistant","tool_calls":[{"id":"2","function":{"name":"run_command","arguments":json.dumps({"command":"python3 -c 'print(\"ok\")'"})}}]}
        return {"role":"assistant","content":"verified"}

@pytest.mark.asyncio
async def test_agent_loop_executes_real_tools(settings, tmp_path):
    result = await AgentLoop(ScriptedNeuron(), build_registry(settings, tmp_path, _GOV_PR, include_remote=False), 5).run("change file", "test")
    assert result["status"] == "completed"
    assert (tmp_path / "changed.txt").read_text() == "done\n"
    assert [entry["tool"] for entry in result["tool_history"]] == ["write_file", "run_command"]

# ---------------------------------------------------------------------------
# Completion gate tests
# ---------------------------------------------------------------------------

@pytest.fixture
def git_workspace(tmp_path):
    """Create a temporary git repository with a shared remote"""
    remote = tmp_path / "remote"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=remote, check=True, capture_output=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote, check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_completion_gate_rejects_main_branch(git_workspace, settings):
    with pytest.raises(ToolExecutionError, match="must be agent"):
        await verify_completion(git_workspace, "TEST-1", settings, _GOV_PR)


@pytest.mark.asyncio
async def test_completion_gate_passes_with_push_and_branch(git_workspace, settings):
    await run_git(git_workspace, "checkout", "-B", "agent/TEST-2")
    (git_workspace / "newfile.md").write_text("# New\n")
    await run_git(git_workspace, "add", ".")
    await run_git(git_workspace, "commit", "-m", "TEST-2: add newfile")
    await run_git(git_workspace, "push", "-u", "origin", "agent/TEST-2")
    result = await verify_completion(git_workspace, "TEST-2", settings, _GOV_PR)
    assert result["branch"] == "agent/TEST-2"
    assert result["changes_present"] is True
    assert result["commits_present"] is True
    assert result["push_ok"] is True


@pytest.mark.asyncio
async def test_completion_gate_rejects_unpushed_branch(git_workspace, settings):
    await run_git(git_workspace, "checkout", "-B", "agent/TEST-3")
    (git_workspace / "newfile.md").write_text("# New\n")
    await run_git(git_workspace, "add", ".")
    await run_git(git_workspace, "commit", "-m", "TEST-3: add newfile")
    with pytest.raises(ToolExecutionError, match="not pushed"):
        await verify_completion(git_workspace, "TEST-3", settings, _GOV_PR)


@pytest.mark.asyncio
async def test_completion_gate_rejects_unborn_branch(git_workspace, settings):
    empty = git_workspace.parent / "empty_repo"
    empty.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=empty, check=True, capture_output=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=empty, check=True, capture_output=True)
    clone = git_workspace.parent / "empty_clone"
    subprocess.run(["git", "clone", str(empty), str(clone)], check=True, capture_output=True)
    with pytest.raises(ToolExecutionError, match="must be agent"):
        await verify_completion(clone, "TEST-4", settings, _GOV_PR)
