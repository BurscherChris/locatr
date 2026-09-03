"""Priority-based governance for the coding agent.

Governance rules are enforced by the tool layer at runtime, not by prompt
instructions. Forbidden tool calls are rejected with a clear error before
execution.

Priority resolution uses Linear's actual issue priority field (1-4 scale).
"""

import logging
from enum import Enum
from typing import Callable

from app.errors import ToolExecutionError

log = logging.getLogger(__name__)

# Linear issue priorities: 0=no priority, 1=urgent, 2=high, 3=medium, 4=low
LINEAR_PRIORITY_LOW = 4
LINEAR_PRIORITY_MEDIUM = 3
LINEAR_PRIORITY_HIGH = 2
LINEAR_PRIORITY_URGENT = 1
LINEAR_PRIORITY_NONE = 0


class GovernanceMode(Enum):
    AUTONOMOUS = "autonomous"
    PR_REQUIRED = "pr_required"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"


class GovernanceState:
    def __init__(self, mode: GovernanceMode):
        self.mode = mode
        self._approved = False

    def mark_approved(self) -> None:
        self._approved = True
        self.mode = GovernanceMode.APPROVED

    @property
    def is_approved(self) -> bool:
        return self._approved or self.mode in (GovernanceMode.AUTONOMOUS, GovernanceMode.PR_REQUIRED, GovernanceMode.APPROVED)

    @property
    def can_write_repository(self) -> bool:
        """Write operations (write_file, commit, push) require approval for HIGH."""
        return self.mode != GovernanceMode.AWAITING_APPROVAL

    @property
    def can_push_master(self) -> bool:
        """LOW allows master push. MEDIUM and HIGH require a branch."""
        return self.mode == GovernanceMode.AUTONOMOUS

    @property
    def requires_pr(self) -> bool:
        """MEDIUM and HIGH after approval require a PR."""
        return self.mode in (GovernanceMode.PR_REQUIRED, GovernanceMode.APPROVED)

    @property
    def requires_branch(self) -> bool:
        """MEDIUM, HIGH (awaiting or approved) require a feature branch."""
        return self.mode in (GovernanceMode.PR_REQUIRED, GovernanceMode.AWAITING_APPROVAL, GovernanceMode.APPROVED)

    def __repr__(self) -> str:
        return f"GovernanceState(mode={self.mode.value}, approved={self._approved})"


def priority_to_governance(linear_priority: int | None) -> GovernanceState:
    """Resolve Linear priority to a governance state.

    Linear priority scale: 0 (none), 1 (urgent), 2 (high), 3 (medium), 4 (low).

    - 0 (none / missing): safest default → MEDIUM behavior (PR_REQUIRED)
    - 4 (low):  AUTONOMOUS (master push allowed, no PR required)
    - 3 (medium): PR_REQUIRED (branch + PR required, no master push)
    - 2 (high):  AWAITING_APPROVAL (proposal required, then approval)
    - 1 (urgent): treated as MEDIUM (PR_REQUIRED) — high urgency does not bypass governance
    """
    if linear_priority is None:
        log.info("Governance: no priority available, defaulting to PR_REQUIRED")
        return GovernanceState(GovernanceMode.PR_REQUIRED)

    mapping = {
        LINEAR_PRIORITY_LOW: GovernanceMode.AUTONOMOUS,
        LINEAR_PRIORITY_MEDIUM: GovernanceMode.PR_REQUIRED,
        LINEAR_PRIORITY_HIGH: GovernanceMode.AWAITING_APPROVAL,
        LINEAR_PRIORITY_URGENT: GovernanceMode.PR_REQUIRED,
        LINEAR_PRIORITY_NONE: GovernanceMode.PR_REQUIRED,
    }
    mode = mapping.get(linear_priority, GovernanceMode.PR_REQUIRED)
    log.info("Governance: linear_priority=%s -> mode=%s", linear_priority, mode.value)
    return GovernanceState(mode)


# ---- Tool names that are governed ----

WRITE_TOOLS = {"write_file", "delete_file"}
GIT_WRITE_TOOLS = {"git_commit", "git_push", "git_create_branch"}
PR_TOOLS = {"create_pull_request"}
MASTER_PUSH_INDICATORS = {"main", "master"}
BYPASS_TOOLS = {"read_file", "list_files", "search_code", "run_command", "run_tests",
                "git_status", "git_diff", "git_log", "get_pull_request",
                "add_linear_comment", "add_linear_activity", "update_linear_issue"}


def check_tool_permitted(tool_name: str, arguments: dict, state: GovernanceState, branch: str = "") -> None:
    """Raise ToolExecutionError if the tool is forbidden by the current governance state.

    Arguments are inspected only for branch names that indicate master push intent.
    """
    if state.can_write_repository:
        pass  # general write allowed

    if state.mode == GovernanceMode.AWAITING_APPROVAL:
        if tool_name in WRITE_TOOLS:
            raise ToolExecutionError(
                f"governance: {tool_name} is not permitted while awaiting approval (HIGH priority). "
                "Post an implementation proposal and wait for explicit APPROVED confirmation."
            )
        if tool_name in GIT_WRITE_TOOLS:
            raise ToolExecutionError(
                f"governance: {tool_name} is not permitted while awaiting approval (HIGH priority)."
            )
        if tool_name in PR_TOOLS:
            raise ToolExecutionError(
                f"governance: {tool_name} is not permitted while awaiting approval (HIGH priority)."
            )

    if tool_name == "git_push":
        target = arguments.get("branch", "")
        if target in MASTER_PUSH_INDICATORS and not state.can_push_master:
            raise ToolExecutionError(
                f"governance: pushing to '{target}' is not permitted for {state.mode.value} priority. "
                "Use an agent/<issue> branch and create a pull request."
            )

    if tool_name == "git_create_branch":
        target = arguments.get("branch", "")
        if target and target in MASTER_PUSH_INDICATORS and not state.can_push_master:
            raise ToolExecutionError(
                f"governance: working directly on '{target}' is not permitted for {state.mode.value} priority."
            )


def detect_explicit_approval(comments: list[dict]) -> bool:
    """Check if any comment contains an explicit approval marker.

    Only exact marker strings in comment bodies are accepted.
    Does NOT use LLM sentiment analysis.
    """
    for comment in comments:
        body = (comment.get("body") or "").strip()
        if body.upper() == "APPROVED" or body == "Approved":
            log.info("Governance: explicit approval detected in comment id=%s", comment.get("id", ""))
            return True
    return False