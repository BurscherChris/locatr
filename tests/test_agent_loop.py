import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from app.agent.loop import AgentLoop
from app.agent.runner import build_registry, verify_completion
from app.errors import ToolExecutionError
from app.git.manager import run_git

class ScriptedNeuron:
    def __init__(self): self.calls = 0
    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1: return {"role":"assistant","tool_calls":[{"id":"1","function":{"name":"write_file","arguments":json.dumps({"path":"changed.txt","content":"done\n"})}}]}
        if self.calls == 2: return {"role":"assistant","tool_calls":[{"id":"2","function":{"name":"run_command","arguments":json.dumps({"command":"python3 -c 'print(\"ok\")'"})}}]}
        return {"role":"assistant","content":"verified"}

@pytest.mark.asyncio
async def test_agent_loop_executes_real_tools(settings, tmp_path):
    result = await AgentLoop(ScriptedNeuron(), build_registry(settings, tmp_path, include_remote=False), 5).run("change file", "test")
    assert result["status"] == "completed"
    assert (tmp_path / "changed.txt").read_text() == "done\n"
    assert [entry["tool"] for entry in result["tool_history"]] == ["write_file", "run_command"]

# ---------------------------------------------------------------------------
# Completion gate tests
# ---------------------------------------------------------------------------

@pytest.fixture
def git_workspace(tmp_path):
    """Create a temporary git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_completion_gate_rejects_main_branch(git_workspace, settings):
    with pytest.raises(ToolExecutionError, match="main"):
        await verify_completion(git_workspace, "TEST-1", None, settings)


@pytest.mark.asyncio
async def test_completion_gate_passes_with_branch_and_commit(git_workspace, settings):
    await run_git(git_workspace, "checkout", "-B", "agent/TEST-2")
    (git_workspace / "newfile.md").write_text("# New\n")
    await run_git(git_workspace, "add", ".")
    await run_git(git_workspace, "commit", "-m", "TEST-2: add newfile")
    result = await verify_completion(git_workspace, "TEST-2", None, settings)
    assert result["branch"] == "agent/TEST-2"
    assert result["changes_present"] is True
    assert result["commits_present"] is True


@pytest.mark.asyncio
async def test_completion_gate_detects_no_changes(git_workspace, settings):
    await run_git(git_workspace, "checkout", "-B", "agent/TEST-3")
    result = await verify_completion(git_workspace, "TEST-3", None, settings)
    assert result["branch"] == "agent/TEST-3"
    assert result["changes_present"] is False


@pytest.mark.asyncio
async def test_completion_gate_rejects_unborn_branch(git_workspace, settings):
    empty = git_workspace.parent / "empty_repo"
    empty.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=empty, check=True, capture_output=True)
    with pytest.raises(ToolExecutionError, match="instead of agent"):
        await verify_completion(empty, "TEST-4", None, settings)
