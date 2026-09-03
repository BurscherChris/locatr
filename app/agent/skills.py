"""Skill loading for the coding agent.

Skills are reusable domain/workflow instructions stored in skills/*.md
in the agent repository (NOT in the target repository). They are loaded
on demand based on the task context and included in the agent's system context.

The skills directory is resolved relative to the agent package installation
directory, making it independent of the current working directory or Docker
mounts.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def available_skills() -> list[str]:
    if not SKILLS_DIR.is_dir():
        log.warning("Skill directory not found at %s", SKILLS_DIR)
        return []
    return sorted(f.stem for f in SKILLS_DIR.iterdir() if f.suffix == ".md")


def load_skill(name: str) -> str | None:
    path = SKILLS_DIR / f"{name}.md"
    if not path.is_file():
        log.warning("Skill not found: %s expected_path=%s", name, path)
        return None
    content = path.read_text(encoding="utf-8")
    log.info("Loaded built-in skill skill=%s source=%s size=%s", name, path.name, len(content))
    return content


def load_skills(names: list[str]) -> dict[str, str]:
    result = {}
    for name in names:
        content = load_skill(name)
        if content is not None:
            result[name] = content
    return result


def relevant_skills_for_repository(repo_path: str, issue_description: str) -> list[str]:
    """Determine which skills are relevant based on repository content and task.

    Returns skill names in priority order. Built-in skills that are always
    relevant are included unconditionally. Additional skills are selected
    based on repository name and task keywords.
    """
    skills = ["core", "testing", "git", "github"]
    lower_repo = repo_path.lower()
    lower_task = issue_description.lower()

    if "py" in lower_repo or "python" in lower_task or not any(x in lower_repo for x in ["js", "ts", "react", "node"]):
        skills.append("python")

    if any(kw in lower_repo or kw in lower_task for kw in ["react", "js", "ts", "node", "frontend"]):
        skills.append("react")

    for kw in ["sql", "db", "database", "migration", "schema", "postgres", "mysql"]:
        if kw in lower_repo or kw in lower_task:
            skills.append("database")
            break

    for kw in ["api", "graphql", "rest", "endpoint", "http"]:
        if kw in lower_repo or kw in lower_task:
            skills.append("api")
            break

    # Include every built-in skill that exists on disk.  This guarantees that
    # newly added skills/*.md files are automatically picked up without code
    # changes, and no skill goes missing just because the keyword heuristic
    # did not match.
    built_in = set(available_skills())
    for name in sorted(built_in):
        if name not in skills:
            skills.append(name)

    return list(dict.fromkeys(skills))