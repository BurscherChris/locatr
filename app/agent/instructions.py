"""Repository-specific instruction discovery and loading.

Searches the workspace for AGENTS.md files. Supports nested discovery:
- workspace/AGENTS.md (root-level, applies broadly)
- workspace/repository/AGENTS.md (inside the cloned repo root)
- workspace/repository/**/AGENTS.md (deeper directories, more specific scope)

Instruction precedence within the repository section:
- More specific (deeper path) instructions are appended after broader ones.
- System/security rules always take precedence over any AGENTS.md content.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

AGENTS_MD_FILENAME = "AGENTS.md"


def find_agents_md(workspace: Path) -> list[Path]:
    """Discover all AGENTS.md files in the workspace.

    Returns a list of paths ordered from broadest to most specific scope.
    Returns empty list if none found.
    """
    found: list[Path] = []

    root_candidates = [
        workspace / AGENTS_MD_FILENAME,
        workspace / "repository" / AGENTS_MD_FILENAME,
    ]
    for path in root_candidates:
        if path.is_file():
            found.append(path)

    repo_root = workspace / "repository"
    if repo_root.is_dir():
        for path in sorted(repo_root.rglob(AGENTS_MD_FILENAME)):
            if path.is_file() and path not in found:
                found.append(path)

    return found


def load_agents_md(workspace: Path, issue: str) -> str:
    """Load all discovered AGENTS.md files and return their combined content.

    Logs whether AGENTS.md was found or not.
    Does not include secrets or sensitive repository content in logs.
    """
    paths = find_agents_md(workspace)
    if not paths:
        log.info("AGENTS.md not found issue=%s workspace=%s", issue, workspace)
        return ""

    sections = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        relative = path.relative_to(workspace)
        size = len(content)
        log.info("AGENTS.md found issue=%s path=%s size=%s", issue, relative, size)
        sections.append(f"### From {relative}\n{content}")

    return "\n\n".join(sections)