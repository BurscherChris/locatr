from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class AgentContext:
    run_id: str
    issue: str
    issue_id: str | None
    task: str
    repository: str
    base_branch: str
    workspace: Path
