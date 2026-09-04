"""Skill loading for the coding agent.

Two namespaces:
1. Agent-core skills — always-loaded workflow/governance skills shipped with the agent.
   Currently: core, testing, git, github, governance.
   These describe how the agent operates, NOT the target repository's technology.

2. Repository-local skills — discovered from the target repository.
   Located at one of: skills/, .agent/skills/, .agents/skills/ inside the workspace.
   These describe the target repository's technology, conventions, and project-specific rules.

Agent-core skills and repository-local skills MUST NOT be mixed.
The agent must not assume that its own skills/python.md applies to the target repository.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Resolve agent-core skills directory relative to the package installation.
# This directory is part of the agent Docker image and is NOT the target repository.
AGENT_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

# Agent-core skills that describe how the agent works (not the target repo).
# These are always loaded because they define the agent's behavior.
AGENT_CORE_SKILLS = ["core", "testing", "git", "github", "governance"]

# Repository-local skill directories to scan inside the target workspace.
REPO_SKILL_DIRS = ["skills", ".agent/skills", ".agents/skills"]


# ---------------------------------------------------------------------------
# Agent-core skills (shipped with the agent)
# ---------------------------------------------------------------------------

def available_agent_skills() -> list[str]:
    if not AGENT_SKILLS_DIR.is_dir():
        log.warning("Agent skill directory not found at %s", AGENT_SKILLS_DIR)
        return []
    return sorted(f.stem for f in AGENT_SKILLS_DIR.iterdir() if f.suffix == ".md")


def load_agent_skill(name: str) -> str | None:
    path = AGENT_SKILLS_DIR / f"{name}.md"
    if not path.is_file():
        log.warning("Agent skill not found: %s expected_path=%s", name, path)
        return None
    content = path.read_text(encoding="utf-8")
    log.info("Loaded agent skill skill=%s source=%s size=%s", name, path.name, len(content))
    return content


def load_agent_skills() -> dict[str, str]:
    """Load all agent-core skills. These are universal and always relevant."""
    result = {}
    for name in AGENT_CORE_SKILLS:
        content = load_agent_skill(name)
        if content is not None:
            result[name] = content
    return result


# ---------------------------------------------------------------------------
# Repository-local skills (discovered in the target workspace)
# ---------------------------------------------------------------------------

def discover_repository_skills(workspace: Path) -> dict[str, Path]:
    """Scan the workspace for repository-local skill directories.

    Returns a dict of {skill_name: path_to_md_file}.
    """
    found: dict[str, Path] = {}
    for rel_dir in REPO_SKILL_DIRS:
        skill_dir = workspace / rel_dir
        if not skill_dir.is_dir():
            continue
        for f in sorted(skill_dir.iterdir()):
            if f.suffix == ".md":
                name = f.stem
                if name not in found:
                    found[name] = f
                    log.info("Repository skill discovered: %s source=%s", name, f)
    if not found:
        log.info("No repository-local skills discovered in %s", workspace)
    return found


def load_repository_skills(workspace: Path) -> dict[str, str]:
    """Load all discovered repository-local skills."""
    discovered = discover_repository_skills(workspace)
    result = {}
    for name, path in discovered.items():
        content = path.read_text(encoding="utf-8")
        log.info("Loaded repository skill skill=%s source=%s size=%s", name, path, len(content))
        result[name] = content
    return result


# ---------------------------------------------------------------------------
# Technology detection (for logging/context only, NOT for loading skills)
# ---------------------------------------------------------------------------

TECH_INDICATORS = {
    "package.json": {"js", "node"},
    "next.config.js": {"nextjs"},
    "next.config.mjs": {"nextjs"},
    "next.config.ts": {"nextjs"},
    "tsconfig.json": {"typescript"},
    "pyproject.toml": {"python"},
    "setup.py": {"python"},
    "requirements.txt": {"python"},
    "go.mod": {"go"},
    "Cargo.toml": {"rust"},
    "pom.xml": {"java", "maven"},
    "build.gradle": {"java", "gradle"},
    "Gemfile": {"ruby"},
    "composer.json": {"php"},
    "Dockerfile": {"docker"},
}


def detect_technologies(workspace: Path) -> list[str]:
    """Scan the workspace for technology indicator files.

    Returns a sorted list of detected technology keywords.
    This is for context/observability only — it does NOT load skills.
    """
    detected: set[str] = set()
    for indicator, techs in TECH_INDICATORS.items():
        if (workspace / indicator).is_file() or (workspace / "repository" / indicator).is_file():
            detected.update(techs)
    result = sorted(detected)
    if result:
        log.info("Technology indicators detected in workspace: %s", result)
    else:
        log.info("No technology indicator files detected in workspace")
    return result