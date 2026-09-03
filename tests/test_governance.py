import json
import time
from pathlib import Path

import pytest
from app.agent.governance import (
    GovernanceMode,
    GovernanceState,
    check_tool_permitted,
    detect_explicit_approval,
    priority_to_governance,
    LINEAR_PRIORITY_LOW,
    LINEAR_PRIORITY_MEDIUM,
    LINEAR_PRIORITY_HIGH,
    LINEAR_PRIORITY_URGENT,
    LINEAR_PRIORITY_NONE,
)
from app.errors import ToolExecutionError


# ---------------------------------------------------------------------------
# Governance state tests
# ---------------------------------------------------------------------------

class TestGovernanceState:
    def test_autonomous_allows_write_and_master(self):
        g = GovernanceState(GovernanceMode.AUTONOMOUS)
        assert g.can_write_repository is True
        assert g.can_push_master is True
        assert g.requires_pr is False
        assert g.requires_branch is False

    def test_pr_required_blocks_master(self):
        g = GovernanceState(GovernanceMode.PR_REQUIRED)
        assert g.can_write_repository is True
        assert g.can_push_master is False
        assert g.requires_pr is True
        assert g.requires_branch is True

    def test_awaiting_approval_blocks_writes(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        assert g.can_write_repository is False
        assert g.can_push_master is False
        assert g.requires_pr is False
        assert g.requires_branch is True
        assert g.is_approved is False

    def test_approved_allows_writes(self):
        g = GovernanceState(GovernanceMode.APPROVED)
        assert g.can_write_repository is True
        assert g.can_push_master is False
        assert g.requires_pr is True
        assert g.requires_branch is True
        assert g.is_approved is True


# ---------------------------------------------------------------------------
# Priority mapping tests
# ---------------------------------------------------------------------------

class TestPriorityMapping:
    def test_low_priority(self):
        g = priority_to_governance(LINEAR_PRIORITY_LOW)
        assert g.mode == GovernanceMode.AUTONOMOUS

    def test_medium_priority(self):
        g = priority_to_governance(LINEAR_PRIORITY_MEDIUM)
        assert g.mode == GovernanceMode.PR_REQUIRED

    def test_high_priority(self):
        g = priority_to_governance(LINEAR_PRIORITY_HIGH)
        assert g.mode == GovernanceMode.AWAITING_APPROVAL

    def test_urgent_priority(self):
        g = priority_to_governance(LINEAR_PRIORITY_URGENT)
        assert g.mode == GovernanceMode.PR_REQUIRED

    def test_no_priority(self):
        g = priority_to_governance(LINEAR_PRIORITY_NONE)
        assert g.mode == GovernanceMode.PR_REQUIRED

    def test_missing_priority_defaults_safe(self):
        g = priority_to_governance(None)
        assert g.mode == GovernanceMode.PR_REQUIRED


# ---------------------------------------------------------------------------
# Tool enforcement tests
# ---------------------------------------------------------------------------

class TestToolEnforcement:
    def test_awaiting_approval_rejects_write_file(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        with pytest.raises(ToolExecutionError, match="not permitted"):
            check_tool_permitted("write_file", {"path": "x.txt", "content": "data"}, g)

    def test_awaiting_approval_rejects_delete_file(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        with pytest.raises(ToolExecutionError, match="not permitted"):
            check_tool_permitted("delete_file", {"path": "x.txt"}, g)

    def test_awaiting_approval_rejects_git_commit(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        with pytest.raises(ToolExecutionError, match="not permitted"):
            check_tool_permitted("git_commit", {"message": "x"}, g)

    def test_awaiting_approval_rejects_git_push(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        with pytest.raises(ToolExecutionError, match="not permitted"):
            check_tool_permitted("git_push", {"branch": "agent/TEST"}, g)

    def test_awaiting_approval_rejects_create_pr(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        with pytest.raises(ToolExecutionError, match="not permitted"):
            check_tool_permitted("create_pull_request", {"repository": "o/r", "title": "t", "head": "h", "base": "b", "body": "b"}, g)

    def test_awaiting_approval_allows_read(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        check_tool_permitted("read_file", {"path": "x.txt"}, g)  # should not raise

    def test_awaiting_approval_allows_list(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        check_tool_permitted("list_files", {"path": "."}, g)  # should not raise

    def test_awaiting_approval_allows_search(self):
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        check_tool_permitted("search_code", {"query": "test"}, g)  # should not raise

    def test_medium_rejects_master_push(self):
        g = GovernanceState(GovernanceMode.PR_REQUIRED)
        with pytest.raises(ToolExecutionError, match="not permitted"):
            check_tool_permitted("git_push", {"branch": "main"}, g)

    def test_medium_allows_branch_push(self):
        g = GovernanceState(GovernanceMode.PR_REQUIRED)
        check_tool_permitted("git_push", {"branch": "agent/TEST-1"}, g)  # should not raise

    def test_low_allows_master_push(self):
        g = GovernanceState(GovernanceMode.AUTONOMOUS)
        check_tool_permitted("git_push", {"branch": "main"}, g)  # should not raise


# ---------------------------------------------------------------------------
# Approval detection tests
# ---------------------------------------------------------------------------

class TestApprovalDetection:
    def test_exact_approved_detected(self):
        assert detect_explicit_approval([{"body": "APPROVED", "id": "c1"}]) is True

    def test_case_approved_detected(self):
        assert detect_explicit_approval([{"body": "Approved", "id": "c1"}]) is True

    def test_random_comment_not_approved(self):
        assert detect_explicit_approval([{"body": "Looks good!", "id": "c1"}]) is False

    def test_empty_comment_not_approved(self):
        assert detect_explicit_approval([{"body": "", "id": "c1"}]) is False

    def test_thanks_not_approved(self):
        assert detect_explicit_approval([{"body": "thanks", "id": "c1"}]) is False

    def test_multiple_comments_no_approval(self):
        comments = [
            {"body": "first comment", "id": "c1"},
            {"body": "interesting approach", "id": "c2"},
        ]
        assert detect_explicit_approval(comments) is False

    def test_multiple_comments_with_approval(self):
        comments = [
            {"body": "first comment", "id": "c1"},
            {"body": "APPROVED", "id": "c2"},
            {"body": "thanks", "id": "c3"},
        ]
        assert detect_explicit_approval(comments) is True


# ---------------------------------------------------------------------------
# Runner-level governance integration tests
# ---------------------------------------------------------------------------

class TestGovernanceRegistry:
    def test_registry_blocks_write_in_awaiting_approval(self, settings, tmp_path):
        from app.agent.runner import build_registry
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        registry = build_registry(settings, tmp_path, g, include_remote=False)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            registry.execute("write_file", {"path": "secret.txt", "content": "data"})
        )
        assert result.ok is False
        assert "not permitted" in (result.error or "")

    def test_registry_allows_write_in_autonomous(self, settings, tmp_path):
        from app.agent.runner import build_registry
        g = GovernanceState(GovernanceMode.AUTONOMOUS)
        registry = build_registry(settings, tmp_path, g, include_remote=False)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            registry.execute("write_file", {"path": "ok.txt", "content": "data"})
        )
        assert result.ok is True

    def test_registry_blocks_push_main_in_pr_required(self, settings, tmp_path):
        from app.agent.runner import build_registry
        g = GovernanceState(GovernanceMode.PR_REQUIRED)
        registry = build_registry(settings, tmp_path, g, include_remote=False)
        import asyncio
        # git_push will fail with GitError because there's no git repo,
        # but it should first fail with governance error
        result = asyncio.get_event_loop().run_until_complete(
            registry.execute("git_push", {"branch": "main"})
        )
        assert result.ok is False
        assert "not permitted" in (result.error or "")

    def test_registry_allows_branch_push_in_pr_required(self, settings, tmp_path):
        from app.agent.runner import build_registry
        g = GovernanceState(GovernanceMode.PR_REQUIRED)
        registry = build_registry(settings, tmp_path, g, include_remote=False)
        import asyncio
        # Should fail with GitError (no git repo), not governance error
        result = asyncio.get_event_loop().run_until_complete(
            registry.execute("git_push", {"branch": "agent/TEST-1"})
        )
        # The error message should be about git, not governance
        assert result.ok is False
        assert "not permitted" not in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Governance completed workflow tests (model returns tool calls, gates apply)
# ---------------------------------------------------------------------------

class TestGovernanceCompletionGate:
    @pytest.mark.asyncio
    async def test_awaiting_approval_returns_valid_state(self, git_workspace, settings):
        from app.agent.runner import verify_completion
        g = GovernanceState(GovernanceMode.AWAITING_APPROVAL)
        result = await verify_completion(git_workspace, "TEST-1", settings, g)
        assert result.get("status") == "awaiting_approval"
        assert result.get("governance") == "awaiting_approval"

    @pytest.mark.asyncio
    async def test_low_master_accepted(self, git_master_workspace, settings):
        from app.agent.runner import verify_completion
        g = GovernanceState(GovernanceMode.AUTONOMOUS)
        result = await verify_completion(git_master_workspace, "TEST-LOW", settings, g)
        assert result.get("changes_present") is True


# ---------------------------------------------------------------------------
# Fixtures for completion gate tests
# ---------------------------------------------------------------------------

@pytest.fixture
def git_workspace(tmp_path):
    import subprocess
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


@pytest.fixture
def git_master_workspace(git_workspace):
    """Fixture that stays on main branch for LOW priority tests."""
    return git_workspace