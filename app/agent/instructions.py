"""Repository-specific instruction discovery and loading."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

AGENTS_MD_FILENAME = "AGENTS.md"


def find_agents_md(workspace: Path) -> Path | None:
    """Search for AGENTS.md in the workspace directory.

    Searches the workspace root. Does not search parent directories
    because workspaces are isolated.
    """
    candidates = [
        workspace / AGENTS_MD_FILENAME,
        workspace / "repository" / AGENTS_MD_FILENAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_agents_md(workspace: Path, issue: str) -> str:
    """Load AGENTS.md if present and return its content.

    Logs whether AGENTS.md was found or not.
    Does not include secrets or sensitive repository content in logs.
    """
    path = find_agents_md(workspace)
    if path is None:
        log.info("AGENTS.md not found issue=%s workspace=%s", issue, workspace)
        return ""

    content = path.read_text(encoding="utf-8")
    size = len(content)
    log.info("AGENTS.md found issue=%s path=%s size=%s", issue, path, size)
    return content